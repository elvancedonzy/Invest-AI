"""Backfill track_record.txt from the archived Alpha Reports.

One-shot CLI tool. For each Alpha Report .txt in REPORTS_DIR:
  1. Parse the date from the filename.
  2. Skip the report if all (date, ticker) pairs are already in the manual
     track_record.txt — idempotent on re-runs.
  3. Ask Claude Haiku to extract Kevin's explicit BUY/AVOID calls as CSV rows.
  4. For each extracted call, fetch Alpaca daily bars over the 21d window and
     grade outcome: HIT / MISS / OPEN.
  5. Append the rows to /reports/track_record_backfill.txt (never overwrites
     the manual file).

Usage:
  python backfill_track_record.py [--limit N] [--dry-run] [--reports-dir DIR]
                                  [--no-grade]

Env vars required:
  ANTHROPIC_API_KEY   - for extraction (Haiku)
  ALPACA_API_KEY, ALPACA_SECRET_KEY - for outcome grading (skip --no-grade)

Cost: ~$0.0008 per report with Haiku → ~$0.10 for the full 110-report corpus.
"""
import argparse
import csv
import io
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from glob import glob

import anthropic
import requests

DEFAULT_REPORTS_DIR = "/reports"
TRACK_RECORD_FILE   = "track_record.txt"
BACKFILL_FILE       = "track_record_backfill.txt"

# Regex to pull M-D-YYYY out of filenames like "Alpha Report 5-20-2026.txt"
# or "1-12-2026 Alpha Report.txt".
DATE_RE = re.compile(r"(\d{1,2})-(\d{1,2})-(\d{4})")

EXTRACTION_PROMPT = """You are extracting Kevin's explicit BUY/AVOID stock calls from a Meet Kevin Alpha Report.

Report date: {report_date}

Return CSV rows, one per call, EXACTLY in this format (no header):
date,ticker,call,entry,target,,notes

Field rules:
- date: always {report_date}
- ticker: explicit US stock ticker (uppercase, e.g. TSLA, NVDA, QQQ). Skip if no ticker.
- call: BUY or AVOID (uppercase). BUY for "I'm buying", "should rally to", "going to". AVOID for "I'm bearish", "stay away", "don't touch".
- entry: dollar entry price Kevin specified, or blank if not specified.
- target: dollar target price Kevin specified, or blank if not specified. If range like "714-715", use midpoint 714.5.
- (leave the outcome column blank — will be graded later)
- notes: short reason (max 60 chars, NO COMMAS, NO QUOTES).

EXCLUDE:
- General macro commentary without a specific ticker.
- The recurring "TOP 13 STOCKS for next 10 YEARS" long-term list — it repeats daily and would inflate the record.
- Hypotheticals ("if X happens then Y").
- Generic watchlist mentions without a clear BUY or AVOID stance.
- Mentions of indexes/ETFs as macro commentary (e.g. "SPY at 600" is not a call).

Output ONLY the CSV rows. No header, no explanation, no markdown fences.
If there are zero explicit calls, output nothing at all.

Report text:
---
{report_text}
---
"""


def parse_report_date(filename):
    m = DATE_RE.search(os.path.basename(filename))
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
    except ValueError:
        return None


def load_existing_pairs(track_record_path):
    """Return set of (date_iso, ticker_upper) already in the manual file."""
    pairs = set()
    if not os.path.exists(track_record_path):
        return pairs
    with open(track_record_path, "r", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or line.lower().startswith("date,"):
                continue
            parts = line.split(",", 6)
            if len(parts) < 2:
                continue
            d = parts[0].strip()
            t = parts[1].strip().upper()
            if d and t:
                pairs.add((d, t))
    return pairs


def extract_calls(client, report_date, report_text, model="claude-haiku-4-5-20251001"):
    """Call Claude to extract CSV rows. Returns list of dicts."""
    prompt = EXTRACTION_PROMPT.format(
        report_date=report_date.isoformat(),
        report_text=report_text[:40000],  # cap to ~40k chars to keep token usage tight
    )
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
    except Exception as e:
        print(f"  Anthropic error: {e}")
        return []

    if not text:
        return []

    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("```"):
            continue
        try:
            parts = list(csv.reader([line]))[0]
        except Exception:
            continue
        if len(parts) < 3:
            continue
        # Pad to 7 columns
        while len(parts) < 7:
            parts.append("")
        rows.append({
            "date":    parts[0].strip(),
            "ticker":  parts[1].strip().upper(),
            "call":    parts[2].strip().upper(),
            "entry":   parts[3].strip(),
            "target":  parts[4].strip(),
            "outcome": parts[5].strip().upper() or "OPEN",
            "notes":   parts[6].strip().replace(",", ";"),
        })
    return rows


def alpaca_bars(ticker, start, end, alpaca_key, alpaca_secret):
    """Fetch daily bars for ticker over [start, end]. Returns list of dicts or None."""
    try:
        r = requests.get(
            f"https://data.alpaca.markets/v2/stocks/{ticker}/bars",
            params={"timeframe": "1Day", "adjustment": "split", "limit": 60,
                    "start": start.isoformat(), "end": end.isoformat(), "sort": "asc"},
            headers={"APCA-API-KEY-ID": alpaca_key, "APCA-API-SECRET-KEY": alpaca_secret},
            timeout=10,
        )
        if r.status_code != 200:
            return None
        return r.json().get("bars") or []
    except Exception:
        return None


def grade_outcome(row, alpaca_key, alpaca_secret, window_days=21):
    """Return (outcome, notes_suffix). Mutates nothing."""
    ticker = row["ticker"]
    call   = row["call"]
    try:
        call_dt = date.fromisoformat(row["date"])
    except Exception:
        return "OPEN", ""

    start = call_dt
    end   = call_dt + timedelta(days=window_days + 5)  # buffer for weekends
    today = date.today()
    if end > today:
        # Window not yet complete — leave as OPEN
        return "OPEN", "window not complete"

    bars = alpaca_bars(ticker, start, end, alpaca_key, alpaca_secret)
    if bars is None:
        return "OPEN", "no price data"
    bars = bars[:window_days]
    if not bars:
        return "OPEN", "no bars"

    def f(v, default=None):
        try:
            return float(v)
        except Exception:
            return default

    try:
        entry  = f(row["entry"])  or f(bars[0].get("o"))  # open of first day if missing
        target = f(row["target"])
    except Exception:
        return "OPEN", ""

    if entry is None:
        return "OPEN", "no entry"

    highs = [f(b.get("h"), 0) for b in bars]
    lows  = [f(b.get("l"), 9e9) for b in bars]
    max_high = max(highs) if highs else 0
    min_low  = min(lows)  if lows  else 9e9

    if call == "BUY":
        if target is not None and max_high >= target:
            for i, b in enumerate(bars):
                if f(b.get("h"), 0) >= target:
                    return "HIT", f"target hit day {i+1}"
        if min_low <= entry * 0.95:
            return "MISS", "5% stop triggered"
        if target is None:
            return "OPEN", "no target"
        return "OPEN", "target not reached"

    if call == "AVOID":
        # Avoiding was right if price dropped 5% within window
        if min_low <= entry * 0.95:
            return "HIT", "dropped 5%+ as expected"
        if max_high >= entry * 1.05:
            return "MISS", "rallied 5%+ against avoid"
        return "OPEN", "no decisive move"

    return "OPEN", ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reports-dir", default=DEFAULT_REPORTS_DIR,
                    help="Directory containing Alpha Report .txt files")
    ap.add_argument("--limit", type=int, default=0,
                    help="Process only the N most recent reports (0 = all)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print extracted rows; don't write the backfill file")
    ap.add_argument("--no-grade", action="store_true",
                    help="Skip Alpaca outcome grading (all rows stay OPEN)")
    args = ap.parse_args()

    if not os.path.isdir(args.reports_dir):
        sys.exit(f"reports dir not found: {args.reports_dir}")

    anthropic_key = os.getenv("ANTHROPIC_API_KEY")
    if not anthropic_key:
        sys.exit("ANTHROPIC_API_KEY not set")

    alpaca_key    = os.getenv("ALPACA_API_KEY")
    alpaca_secret = os.getenv("ALPACA_SECRET_KEY")
    if not args.no_grade and (not alpaca_key or not alpaca_secret):
        sys.exit("ALPACA_API_KEY / ALPACA_SECRET_KEY required for grading "
                 "(or pass --no-grade)")

    client = anthropic.Anthropic(api_key=anthropic_key)

    track_path = os.path.join(args.reports_dir, TRACK_RECORD_FILE)
    existing_pairs = load_existing_pairs(track_path)
    print(f"Found {len(existing_pairs)} existing (date,ticker) pairs in manual file")

    # Collect reports, sort newest-first by parsed date.
    reports = []
    for path in glob(os.path.join(args.reports_dir, "*.txt")):
        if os.path.basename(path).lower() in ("track_record.txt", "track_record_backfill.txt"):
            continue
        d = parse_report_date(path)
        if d:
            reports.append((d, path))
    reports.sort(key=lambda x: x[0], reverse=True)
    if args.limit:
        reports = reports[:args.limit]
    print(f"Processing {len(reports)} report(s)")

    backfill_path = os.path.join(args.reports_dir, BACKFILL_FILE)
    output_rows = []
    new_count = 0
    skip_count = 0

    for report_date, path in reports:
        try:
            with open(path, "r", errors="ignore") as f:
                text = f.read()
        except Exception as e:
            print(f"[{report_date}] read failed: {e}")
            continue
        print(f"[{report_date}] {os.path.basename(path)}")
        rows = extract_calls(client, report_date, text)
        if not rows:
            print(f"  no calls extracted")
            time.sleep(1)
            continue

        kept = []
        for row in rows:
            # Force the date to match the report
            row["date"] = report_date.isoformat()
            pair = (row["date"], row["ticker"])
            if pair in existing_pairs:
                skip_count += 1
                continue
            existing_pairs.add(pair)  # avoid duplicates within this run too
            if not args.no_grade and row["call"] in ("BUY", "AVOID"):
                outcome, note_suffix = grade_outcome(row, alpaca_key, alpaca_secret)
                row["outcome"] = outcome
                if note_suffix:
                    row["notes"] = (row["notes"] + " | " + note_suffix).strip(" |")[:120]
            kept.append(row)
            new_count += 1

        print(f"  +{len(kept)} new, {len(rows)-len(kept)} dup-skipped")
        output_rows.extend(kept)
        time.sleep(1)

    print()
    print(f"Total new rows: {new_count}  |  skipped (already in manual): {skip_count}")

    if args.dry_run:
        print("--- DRY RUN OUTPUT (first 30 rows) ---")
        for r in output_rows[:30]:
            print(f"{r['date']},{r['ticker']},{r['call']},{r['entry']},{r['target']},{r['outcome']},{r['notes']}")
        if len(output_rows) > 30:
            print(f"... and {len(output_rows)-30} more")
        return

    if not output_rows:
        print("Nothing to write.")
        return

    # Sort chronologically for readability
    output_rows.sort(key=lambda r: (r["date"], r["ticker"]))
    with open(backfill_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date","ticker","call","entry","target","outcome","notes"])
        f.write("# Auto-generated by backfill_track_record.py — REVIEW before merging into track_record.txt\n")
        for r in output_rows:
            w.writerow([r["date"], r["ticker"], r["call"], r["entry"],
                        r["target"], r["outcome"], r["notes"]])
    print(f"Wrote {len(output_rows)} rows → {backfill_path}")


if __name__ == "__main__":
    main()
