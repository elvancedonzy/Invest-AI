"""
Backfill track_record.txt from all Alpha Reports.

For each report:
  1. Claude Haiku extracts specific trade calls as JSON
  2. Alpaca historical bars verify if the target was hit within 21 days
  3. Result written to /reports/track_record.txt (skips duplicates)

Run as a one-off K8s job — see backfill-job.yaml
"""
import os, glob, json, anthropic, re, requests, time
from datetime import datetime, timedelta

REPORTS_DIR       = "/reports/"
TRACK_RECORD_PATH = "/reports/track_record.txt"
MAX_VERIFY_BARS   = 21   # trading days to check for target hit
RECENT_DAYS       = 25   # reports within this window → OPEN (not enough history yet)


# ── Report helpers ────────────────────────────────────────────────────────────

def _date_key(f):
    m = re.search(r'(\d+)-(\d+)-(\d{4})', os.path.basename(f))
    return (int(m.group(3)), int(m.group(1)), int(m.group(2))) if m else (0, 0, 0)

def _date_str(f):
    m = re.search(r'(\d+)-(\d+)-(\d{4})', os.path.basename(f))
    if not m:
        return None
    y, mo, d = int(m.group(3)), int(m.group(1)), int(m.group(2))
    return f"{y}-{mo:02d}-{d:02d}"

def get_all_reports():
    files  = glob.glob(os.path.join(REPORTS_DIR, "*.txt"))
    dated  = [(f, _date_key(f)) for f in files if _date_key(f) != (0, 0, 0)]
    sorted_files = sorted(dated, key=lambda x: x[1])
    out = []
    for f, _ in sorted_files:
        ds = _date_str(f)
        with open(f, "r", errors="ignore") as fp:
            out.append({"name": os.path.basename(f), "date": ds, "content": fp.read()})
    return out


# ── Track-record helpers ──────────────────────────────────────────────────────

def load_existing_keys():
    """Return set of 'date_TICKER' strings already in track_record.txt."""
    if not os.path.exists(TRACK_RECORD_PATH):
        return set()
    keys = set()
    try:
        with open(TRACK_RECORD_PATH, "r", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or line.lower().startswith("date,"):
                    continue
                parts = line.split(",", 2)
                if len(parts) >= 2:
                    keys.add(f"{parts[0].strip()}_{parts[1].strip().upper()}")
    except Exception:
        pass
    return keys

def ensure_header():
    if not os.path.exists(TRACK_RECORD_PATH):
        with open(TRACK_RECORD_PATH, "w") as f:
            f.write("# Kevin Alpha Report Trade Log — auto-backfilled\n")
            f.write("# Format: date,ticker,call,entry,target,outcome,notes\n")
            f.write("date,ticker,call,entry,target,outcome,notes\n")
        print("Created track_record.txt")
    else:
        # Check if header line exists — add if missing
        with open(TRACK_RECORD_PATH, "r") as f:
            content = f.read()
        if "date,ticker,call" not in content.lower():
            with open(TRACK_RECORD_PATH, "a") as f:
                f.write("date,ticker,call,entry,target,outcome,notes\n")

def append_entry(date, ticker, direction, entry, target, outcome, notes):
    notes = notes.replace(",", ";")[:120]
    line  = f"{date},{ticker},{direction},{entry},{target},{outcome},{notes}"
    with open(TRACK_RECORD_PATH, "a") as f:
        f.write(line + "\n")


# ── Claude extraction ─────────────────────────────────────────────────────────

def extract_calls(report_content, report_date):
    """Ask Claude Haiku to extract explicit trade calls as a JSON array."""
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    prompt = f"""You are extracting explicit trade calls from an investment research report dated {report_date}.

REPORT (first 3000 chars):
{report_content[:3000]}

Return ONLY a JSON array of specific, actionable trade calls.
Rules:
- Only include calls where Kevin explicitly recommends a specific ticker
- Must have a direction (buy/sell/calls/puts) or be an explicit avoid
- Ignore vague bullish/bearish commentary without a specific action
- Entry and target should be numeric strings if mentioned, otherwise null
- If a percentage target is given (e.g. "20% upside from $50"), calculate the target price

JSON format:
[
  {{
    "ticker": "SOXL",
    "direction": "BUY CALLS",
    "entry": "22.50",
    "target": "27.00",
    "timeframe": "short",
    "notes": "brief reason from report"
  }}
]

direction options: BUY / SELL / BUY CALLS / BUY PUTS / AVOID
timeframe: short (1-3 days) / medium (1-4 weeks) / long (months+)
entry / target: numeric string or null

Return [] if no specific actionable calls.
Return ONLY the JSON array — no explanation, no markdown."""

    try:
        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}]
        )
        text = r.content[0].text.strip()

        # Strip markdown code fences if present
        md = re.search(r'```(?:json)?\s*([\s\S]*?)```', text)
        if md:
            text = md.group(1).strip()

        calls = json.loads(text)
        return calls if isinstance(calls, list) else []
    except json.JSONDecodeError as e:
        print(f"  JSON parse error: {e} — raw: {text[:200]}")
        return []
    except Exception as e:
        print(f"  Claude error: {e}")
        return []


# ── Alpaca verification ───────────────────────────────────────────────────────

def fetch_bars(ticker, from_date, n_days=35):
    """Fetch n_days of daily bars starting from from_date."""
    key    = os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_SECRET_KEY")
    if not key:
        return []
    try:
        end = (datetime.strptime(from_date, "%Y-%m-%d") + timedelta(days=n_days + 10)).strftime("%Y-%m-%d")
        r = requests.get(
            f"https://data.alpaca.markets/v2/stocks/{ticker}/bars",
            params={"timeframe": "1Day", "start": from_date, "end": end,
                    "limit": n_days + 10, "adjustment": "split", "sort": "asc"},
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
            timeout=10
        )
        return r.json().get("bars", [])
    except Exception as e:
        print(f"  Alpaca error ({ticker}): {e}")
        return []

def verify_call(ticker, direction, entry_str, target_str, report_date):
    """
    Returns (outcome, notes).
    outcome: HIT / MISS / OPEN
    """
    today      = datetime.utcnow().date()
    report_dt  = datetime.strptime(report_date, "%Y-%m-%d").date()
    days_since = (today - report_dt).days

    # Too recent to have a verdict
    if days_since < RECENT_DAYS:
        return "OPEN", f"recent ({days_since}d ago) — monitoring"

    # No target → can't objectively verify
    if not target_str:
        return "OPEN", "no price target specified"

    try:
        target = float(target_str)
        entry  = float(entry_str) if entry_str else None
    except (ValueError, TypeError):
        return "OPEN", "non-numeric price"

    bars = fetch_bars(ticker, report_date, n_days=40)
    if not bars:
        return "OPEN", "no Alpaca bar data"

    # Skip the entry-day bar (assume next-day fill), then check up to MAX_VERIFY_BARS
    check_bars = bars[1 : MAX_VERIFY_BARS + 1] if len(bars) > 1 else bars[:MAX_VERIFY_BARS]
    if not check_bars:
        return "OPEN", "insufficient bars"

    is_long  = direction.upper() in ("BUY", "BUY CALLS")
    is_short = direction.upper() in ("SELL", "BUY PUTS", "AVOID")

    for idx, bar in enumerate(check_bars):
        if is_long and bar["h"] >= target:
            pct = round((target - entry) / entry * 100, 1) if entry else "?"
            return "HIT", f"+{pct}% target hit in ~{idx+1}d"
        if is_short and bar["l"] <= target:
            pct = round((entry - target) / entry * 100, 1) if entry else "?"
            return "HIT", f"+{pct}% short target hit in ~{idx+1}d"

    last_close = check_bars[-1]["c"]
    pct_move   = round((last_close - entry) / entry * 100, 1) if entry else "?"
    return "MISS", f"target not reached in {len(check_bars)}d (last ${last_close:.2f} {pct_move:+}%)"


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{'='*60}")
    print(f"Backfill started: {datetime.now()}")
    print(f"{'='*60}\n")

    ensure_header()
    existing = load_existing_keys()
    print(f"Existing entries to skip: {len(existing)}\n")

    reports   = get_all_reports()
    n         = len(reports)
    added     = 0
    skipped   = 0

    for i, report in enumerate(reports):
        print(f"[{i+1}/{n}] {report['name']} ({report['date']})")

        calls = extract_calls(report["content"], report["date"])

        if not calls:
            print("  → No actionable calls\n")
            time.sleep(0.5)
            continue

        tickers_found = [c.get("ticker", "?") for c in calls]
        print(f"  Extracted {len(calls)} calls: {tickers_found}")

        for call in calls:
            ticker    = (call.get("ticker") or "").upper().strip()
            direction = (call.get("direction") or "BUY").upper().strip()
            entry     = str(call.get("entry")  or "").strip()
            target    = str(call.get("target") or "").strip()
            c_notes   = str(call.get("notes")  or "").strip()

            # Sanity checks
            if not ticker or len(ticker) > 6 or not ticker.isalpha():
                continue

            # Skip if already logged
            key = f"{report['date']}_{ticker}"
            if key in existing:
                skipped += 1
                continue

            # Verify outcome against Alpaca
            if direction == "AVOID":
                outcome = "MISS"
                notes   = f"Kevin said avoid. {c_notes}"
            else:
                print(f"  Verifying {ticker} {direction} entry={entry or '?'} target={target or '?'} ...")
                outcome, notes = verify_call(ticker, direction, entry, target, report["date"])
                time.sleep(0.4)  # Alpaca rate courtesy

            append_entry(report["date"], ticker, direction, entry, target, outcome, notes)
            existing.add(key)
            added += 1
            print(f"  → {ticker} {outcome}: {notes}")

        # Pause between reports to respect Claude rate limits
        time.sleep(1.2)
        print()

    print(f"\n{'='*60}")
    print(f"Backfill complete at {datetime.now()}")
    print(f"  Added  : {added}")
    print(f"  Skipped: {skipped} (already in file)")
    print(f"  Total  : {len(existing)}")
    print(f"{'='*60}\n")

main()
