import os, glob, json, anthropic, re, requests, sqlite3, statistics
from datetime import datetime, timedelta

REPORTS_DIR = "/reports/"
RESULTS_DIR = "/reports/results/"
DB_PATH     = "/reports/users.db"
os.makedirs(RESULTS_DIR, exist_ok=True)

def _date_key(f):
    m = re.search(r'(\d+)-(\d+)-(\d{4})', os.path.basename(f))
    return (int(m.group(3)), int(m.group(1)), int(m.group(2))) if m else (0, 0, 0)

def _name_cleanliness(f):
    name = os.path.basename(f).lower()
    score = 0
    for bad in (" copy", "(1)", "(2)", "(3)", " ready", " txt.txt"):
        if bad in name:
            score += 1
    return (score, len(name))

def get_all_reports():
    files = glob.glob(os.path.join(REPORTS_DIR, "*.txt"))
    dated = [(f, _date_key(f)) for f in files if _date_key(f) != (0, 0, 0)]
    by_date = {}
    for f, dk in dated:
        if dk not in by_date or _name_cleanliness(f) < _name_cleanliness(by_date[dk]):
            by_date[dk] = f
    sorted_files = sorted(by_date.values(), key=_date_key)
    reports = []
    for f in sorted_files:
        with open(f, "r", errors="ignore") as fp:
            reports.append({"name": os.path.basename(f), "content": fp.read()})
    return reports

def build_history_context(reports):
    if not reports or len(reports) <= 1:
        return ""
    historical = reports[:-1]
    if len(historical) <= 10:
        return "\n".join(f"=== {r['name']} ===\n{r['content'][:800]}" for r in historical)
    recent  = historical[-10:]
    older   = historical[:-10]
    step    = max(1, len(older) // 20)
    sampled = older[::step]
    recent_text = "\n\n".join(f"=== {r['name']} ===\n{r['content'][:800]}" for r in recent)
    older_text  = "\n".join(f"• {r['name']}: {r['content'][:150].strip()}" for r in sampled)
    return (
        f"RECENT REPORTS — last 10 full:\n{recent_text}\n\n"
        f"EARLIER REPORTS — sampled ({len(sampled)} of {len(older)}):\n{older_text}"
    )

def get_latest_report(reports):
    if not reports:
        return None, None
    latest = reports[-1]
    return latest["name"], latest["content"]

# ── Item 4: Market Regime Detection ──────────────────────────────────────────

def get_market_regime():
    """Fetch 220 SPY daily bars → classify Bull / Neutral / Choppy / Bear / Crash."""
    key    = os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_SECRET_KEY")
    if not key:
        return None
    try:
        start = (datetime.utcnow() - timedelta(days=320)).strftime("%Y-%m-%d")
        r = requests.get(
            "https://data.alpaca.markets/v2/stocks/SPY/bars",
            params={"timeframe": "1Day", "limit": 220, "adjustment": "split",
                    "start": start, "sort": "asc"},
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
            timeout=10
        )
        bars = r.json().get("bars", [])
        if len(bars) < 50:
            return None

        closes   = [b["c"] for b in bars]
        ma50     = sum(closes[-50:]) / 50
        ma200    = sum(closes[-200:]) / 200 if len(closes) >= 200 else sum(closes) / len(closes)
        current  = closes[-1]

        returns   = [(closes[i] - closes[i-1]) / closes[i-1] for i in range(1, len(closes))]
        daily_vol = statistics.stdev(returns[-20:]) if len(returns) >= 20 else 0
        ann_vol   = round(daily_vol * (252 ** 0.5) * 100, 1)
        pct_from_200 = round((current - ma200) / ma200 * 100, 2)
        above_200 = current > ma200
        above_50  = current > ma50

        if not above_200 and ann_vol > 40:
            regime, icon = "CRASH", "🔴"
        elif not above_200 and ann_vol > 25:
            regime, icon = "BEAR", "🔴"
        elif not above_200:
            regime, icon = "BEAR", "🟠"
        elif above_200 and above_50 and ann_vol < 20:
            regime, icon = "BULL", "🟢"
        elif above_200 and ann_vol > 30:
            regime, icon = "CHOPPY", "🟡"
        else:
            regime, icon = "NEUTRAL", "🟡"

        return {
            "regime": regime, "icon": icon,
            "spy": round(current, 2),
            "ma50": round(ma50, 2), "ma200": round(ma200, 2),
            "pct_from_200": pct_from_200, "ann_vol": ann_vol,
            "above_200": above_200, "above_50": above_50,
        }
    except Exception as e:
        print(f"Regime detection error: {e}")
        return None

# ── Live Snapshot Prices (used both for portfolio risk and trade-plan grounding) ──

def _fetch_snapshot_prices(tickers):
    """Return {ticker: price} from Alpaca snapshots. Empty dict on failure."""
    if not tickers:
        return {}
    key    = os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_SECRET_KEY")
    if not key:
        return {}
    try:
        r = requests.get(
            "https://data.alpaca.markets/v2/stocks/snapshots",
            params={"symbols": ",".join(sorted(set(tickers)))},
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
            timeout=8,
        )
        data = r.json()
        out = {}
        for t, snap in data.items():
            p = (snap or {}).get("latestTrade", {}).get("p", 0)
            if p:
                out[t] = float(p)
        return out
    except Exception as e:
        print(f"Snapshot price fetch error: {e}")
        return {}

# Words that look like tickers but aren't. Keep this tight — false negatives are
# fine (we still ground against Alpaca), false positives waste an API slot.
_TICKER_STOPWORDS = {
    "A","I","AM","PM","AN","AND","ANY","ARE","AS","AT","BE","BUT","BY","CAN","DO",
    "EOD","ETA","ETF","FED","FOMC","FOR","FROM","GDP","HAS","HE","I","IF","IN","IS",
    "IT","ITS","JOB","ME","MY","NEW","NO","NOT","NOW","OF","OK","ON","OR","OUR","OUT",
    "PER","RSI","SO","THE","TO","UP","US","USA","USD","VS","WAS","WE","WHO","WHY",
    "WILL","WITH","YOU","YOUR","CPI","PCE","PPI","NFP","MA","IPO","CEO","CFO","IV",
    "ATM","ITM","OTM","DTE","TLDR","FYI","Q1","Q2","Q3","Q4","YTD","DD","YOY","MOM",
    "AI","ML","UI","UX","API","CRM","SAAS","LLM","GPT","ATH","ATL",
}

# ── Tradier options chain (deterministic options-companion attachment) ───────

def _tradier_expirations(ticker):
    token = os.getenv("TRADIER_TOKEN")
    if not token:
        return []
    try:
        r = requests.get(
            "https://api.tradier.com/v1/markets/options/expirations",
            params={"symbol": ticker, "includeAllRoots": "true", "strikes": "false"},
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=6,
        )
        dates = r.json().get("expirations", {}).get("date", [])
        return dates if isinstance(dates, list) else [dates]
    except Exception as e:
        print(f"  Tradier expirations({ticker}) failed: {e}")
        return []

def _tradier_chain(ticker, expiration):
    token = os.getenv("TRADIER_TOKEN")
    if not token:
        return None
    try:
        r = requests.get(
            "https://api.tradier.com/v1/markets/options/chains",
            params={"symbol": ticker, "expiration": expiration, "greeks": "false"},
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=10,
        )
        raw = (r.json().get("options") or {}).get("option", [])
        if isinstance(raw, dict):
            raw = [raw]
        return raw
    except Exception as e:
        print(f"  Tradier chain({ticker}, {expiration}) failed: {e}")
        return None

def _pick_expiration(expirations, target_dte=45):
    """Pick the listed expiration closest to target DTE from today."""
    if not expirations:
        return None
    today = datetime.utcnow().date()
    best, best_gap = None, None
    for e in expirations:
        try:
            d = datetime.fromisoformat(e).date()
        except Exception:
            continue
        dte = (d - today).days
        if dte < 14:  # too short — theta will dominate
            continue
        gap = abs(dte - target_dte)
        if best_gap is None or gap < best_gap:
            best, best_gap = e, gap
    return best

# ── Trade Plan Grading (learning loop) ────────────────────────────────────────

def _ensure_grades_table():
    if not os.path.exists(DB_PATH):
        return False
    try:
        con = sqlite3.connect(DB_PATH)
        con.execute("""
            CREATE TABLE IF NOT EXISTS trade_plan_grades (
                plan_file      TEXT    NOT NULL,
                trade_idx      INTEGER NOT NULL,
                ticker         TEXT    NOT NULL,
                direction      TEXT,
                entry          REAL,
                stop_loss      REAL,
                target         REAL,
                generated_at   TEXT,
                status         TEXT,
                resolved_at    TEXT,
                resolved_price REAL,
                updated_at     TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (plan_file, trade_idx)
            )
        """)
        con.commit()
        con.close()
        return True
    except Exception as e:
        print(f"  grades table init failed: {e}")
        return False

def _fetch_historical_bars(ticker, start_iso, days=45):
    """Daily OHLC bars from start_iso forward, up to `days` ahead."""
    key    = os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_SECRET_KEY")
    if not key:
        return None
    try:
        r = requests.get(
            f"https://data.alpaca.markets/v2/stocks/{ticker}/bars",
            params={"timeframe": "1Day", "start": start_iso, "limit": days,
                    "adjustment": "split", "sort": "asc"},
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
            timeout=10,
        )
        bars = r.json().get("bars", [])
        return [{"date": b["t"][:10], "high": b["h"], "low": b["l"], "close": b["c"]}
                for b in bars]
    except Exception as e:
        print(f"  bars fetch failed for {ticker}: {e}")
        return None

def _evaluate_trade(trade, bars, *, expire_days=30):
    """Walk daily bars; return (status, resolved_at, resolved_price).
    If both stop and target hit same day, conservatively call it STOPPED."""
    try:
        entry  = float(trade.get("entry"))
        stop   = float(trade.get("stop_loss"))
        target = float(trade.get("target"))
    except (TypeError, ValueError):
        return "INVALID", None, None
    direction = str(trade.get("direction", "LONG")).upper()
    if not bars:
        return "OPEN", None, None
    for i, b in enumerate(bars):
        if i >= expire_days:
            break
        if direction == "LONG":
            hit_stop   = b["low"]  <= stop
            hit_target = b["high"] >= target
        else:
            hit_stop   = b["high"] >= stop
            hit_target = b["low"]  <= target
        if hit_stop:
            return "STOPPED", b["date"], stop
        if hit_target:
            return "HIT_TARGET", b["date"], target
    if len(bars) >= expire_days:
        return "EXPIRED", None, None
    return "OPEN", None, None

def grade_past_plans(max_plans=30):
    """Grade every trade in the last `max_plans` saved plans. Skips trades
    already resolved (HIT_TARGET, STOPPED, EXPIRED). Re-evaluates only
    OPEN ones each run. Returns aggregate stats for prompt injection."""
    if not _ensure_grades_table():
        return None
    plan_files = sorted(glob.glob(os.path.join(RESULTS_DIR, "*_trade_plan.json")))
    plan_files = plan_files[-max_plans:]
    if not plan_files:
        return None

    today_iso = datetime.utcnow().date().isoformat()
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    new_grades = updates_from_open = 0

    for plan_file in plan_files:
        try:
            with open(plan_file) as f:
                plan = json.load(f)
        except Exception:
            continue
        plan_name = os.path.basename(plan_file)
        gen_at = plan.get("generated_at", "")
        gen_date = gen_at[:10] if gen_at else ""
        if not gen_date or gen_date >= today_iso:
            continue  # skip today/future
        for idx, t in enumerate(plan.get("trades", [])):
            existing = con.execute(
                "SELECT status FROM trade_plan_grades WHERE plan_file=? AND trade_idx=?",
                (plan_name, idx)
            ).fetchone()
            if existing and existing["status"] in ("HIT_TARGET", "STOPPED", "EXPIRED", "INVALID"):
                continue
            ticker = str(t.get("ticker", "")).upper()
            if not ticker:
                continue
            try:
                start_date = (datetime.fromisoformat(gen_date) + timedelta(days=1)).date().isoformat()
            except Exception:
                continue
            bars = _fetch_historical_bars(ticker, start_date)
            status, resolved_at, resolved_price = _evaluate_trade(t, bars)
            if status == "INVALID":
                continue
            con.execute("""
                INSERT INTO trade_plan_grades
                    (plan_file, trade_idx, ticker, direction, entry, stop_loss, target,
                     generated_at, status, resolved_at, resolved_price, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(plan_file, trade_idx) DO UPDATE SET
                    status=excluded.status,
                    resolved_at=excluded.resolved_at,
                    resolved_price=excluded.resolved_price,
                    updated_at=datetime('now')
            """, (plan_name, idx, ticker, t.get("direction"), t.get("entry"),
                  t.get("stop_loss"), t.get("target"), gen_at,
                  status, resolved_at, resolved_price))
            new_grades += 1
            if existing:
                updates_from_open += 1
    con.commit()

    rows = con.execute("SELECT status, COUNT(*) AS n FROM trade_plan_grades GROUP BY status").fetchall()
    stats = {r["status"]: r["n"] for r in rows}
    recent_hits = [dict(r) for r in con.execute(
        "SELECT ticker, generated_at, entry, target FROM trade_plan_grades "
        "WHERE status='HIT_TARGET' ORDER BY resolved_at DESC LIMIT 3"
    ).fetchall()]
    recent_stops = [dict(r) for r in con.execute(
        "SELECT ticker, generated_at, entry, stop_loss FROM trade_plan_grades "
        "WHERE status='STOPPED' ORDER BY resolved_at DESC LIMIT 3"
    ).fetchall()]
    con.close()
    total = sum(stats.values())
    print(f"  Graded {new_grades} trades ({updates_from_open} resolved from OPEN); "
          f"totals: {stats}")
    return {"total": total, "by_status": stats,
            "recent_hits": recent_hits, "recent_stops": recent_stops}

def build_accuracy_context(stats):
    """Compact 5-line accuracy block injected into the analyzer prompt."""
    if not stats or not stats.get("total"):
        return ""
    by = stats["by_status"]
    hit, stop = by.get("HIT_TARGET", 0), by.get("STOPPED", 0)
    open_, expired = by.get("OPEN", 0), by.get("EXPIRED", 0)
    resolved = hit + stop
    hit_rate = (hit / resolved * 100) if resolved else 0
    lines = [
        f"ANALYZER TRACK RECORD ({stats['total']} prior plan trades graded):",
        f"  HIT TARGET: {hit}  STOPPED: {stop}  EXPIRED no-resolution: {expired}  still OPEN: {open_}",
        f"  Hit rate on resolved trades: {hit_rate:.0f}%",
    ]
    if stats["recent_hits"]:
        wins = ", ".join(f"{h['ticker']} {h['generated_at'][:10]}" for h in stats["recent_hits"])
        lines.append(f"  Recent winners: {wins}")
    if stats["recent_stops"]:
        losses = ", ".join(f"{s['ticker']} {s['generated_at'][:10]}" for s in stats["recent_stops"])
        lines.append(f"  Recent losers: {losses}")
    lines.append(
        "  CALIBRATE: if stop-rate is high, recent picks were too aggressive — tighten "
        "conviction or widen R:R; if expired-rate is high, horizons were too long."
    )
    return "\n".join(lines)

def _get_options_budget():
    """Read budget from the shared settings table (set via dashboard UI).
    Fall back to OPTIONS_BUDGET env var, then $500. Errors are swallowed —
    a missing setting must never block the analyzer."""
    try:
        if os.path.exists(DB_PATH):
            con = sqlite3.connect(DB_PATH)
            r = con.execute("SELECT value FROM settings WHERE key='options_budget'").fetchone()
            con.close()
            if r and r[0]:
                return float(r[0])
    except Exception as e:
        print(f"  settings lookup failed: {e}")
    try:
        return float(os.getenv("OPTIONS_BUDGET", "500"))
    except (TypeError, ValueError):
        return 500.0

def attach_options_companions(trades, *, budget=None, min_dte=14, max_dte=50):
    """For each shares trade, attach a real options companion priced at or
    under `budget` (premium × 100). Walks expirations longest-DTE-first within
    [min_dte, max_dte]; within each, walks strikes outward from ATM in the
    trade's direction and takes the first contract that fits the budget. This
    gives the highest-delta contract you can afford. Drops the block if no
    contract fits — shares-only is honest, fake premiums are not."""
    if budget is None:
        budget = _get_options_budget()
    if not os.getenv("TRADIER_TOKEN"):
        print("  TRADIER_TOKEN not set — skipping options companions")
        for t in trades:
            t["options"] = None
        return
    print(f"  Options budget: ${budget:.0f} per contract (premium × 100)")
    for t in trades:
        t["options"] = _pick_options_companion(t, budget, min_dte, max_dte)
        ticker = t.get("ticker", "?")
        if t["options"] is None:
            print(f"    {ticker}: no contract fits ${budget:.0f} budget — shares only")

def _pick_options_companion(trade, budget, min_dte, max_dte):
    ticker = str(trade.get("ticker", "")).upper()
    direction = str(trade.get("direction", "LONG")).upper()
    spot = trade.get("entry")
    if not ticker or not spot:
        return None
    spot = float(spot)
    opt_type = "call" if direction == "LONG" else "put"

    exps = _tradier_expirations(ticker)
    if not exps:
        return None

    today = datetime.utcnow().date()
    dated = []
    for e in exps:
        try:
            d = datetime.fromisoformat(e).date()
        except Exception:
            continue
        dte = (d - today).days
        if min_dte <= dte <= max_dte:
            dated.append((dte, e))
    # Longest DTE first — less theta — but cap at the bottom of the range.
    dated.sort(reverse=True)

    for dte, exp in dated:
        chain = _tradier_chain(ticker, exp)
        if not chain:
            continue
        candidates = [o for o in chain
                      if o.get("option_type") == opt_type and o.get("strike")]
        if not candidates:
            continue
        # Strike must be within the same setup as the shares plan:
        # LONG calls — strike ≤ shares target (so if shares hit target, the call
        # is ITM at expiry and profits). SHORT puts — strike ≥ shares target.
        # Walk strikes ATM-outward — the first that fits the budget is the
        # highest-delta affordable contract within the share-target window.
        share_target = trade.get("target")
        try:
            share_target = float(share_target) if share_target else None
        except (TypeError, ValueError):
            share_target = None

        if direction == "LONG":
            max_strike = spot * 1.25
            if share_target:
                max_strike = min(max_strike, share_target)
            candidates = sorted(
                (c for c in candidates
                 if spot * 0.97 <= float(c["strike"]) <= max_strike),
                key=lambda o: float(o["strike"])
            )
        else:
            min_strike = spot * 0.75
            if share_target:
                min_strike = max(min_strike, share_target)
            candidates = sorted(
                (c for c in candidates
                 if min_strike <= float(c["strike"]) <= spot * 1.03),
                key=lambda o: -float(o["strike"])
            )

        for c in candidates:
            ask  = c.get("ask")  or 0
            last = c.get("last") or 0
            bid  = c.get("bid")  or 0
            premium = float(ask) if ask else float(last)
            if premium <= 0:
                continue
            cost = premium * 100
            if cost > budget:
                continue
            strike = float(c["strike"])
            return {
                "type": opt_type,
                "strike": strike,
                "expiration": exp,
                "entry": round(premium, 2),
                "stop_loss": round(premium * 0.50, 2),
                "target":    round(premium * 2.00, 2),
                "rationale": (
                    f"{opt_type.upper()} ${strike:g} {exp} (~{dte} DTE, "
                    f"{(strike-spot)/spot*100:+.1f}% from spot). "
                    f"Cost ~${cost:.0f} under ${budget:.0f} cap. "
                    f"Stop at 50% of premium, target at 100% gain. "
                    f"Tradier bid {bid} / ask {ask}."
                ),
            }
    return None

def _dte(expiration):
    try:
        return (datetime.fromisoformat(expiration).date() - datetime.utcnow().date()).days
    except Exception:
        return "?"

def _extract_candidate_tickers(reports, max_tickers=40):
    """Pull tickers from today's report plus the last 5. Look for $TICKER and bare
    1–5 letter uppercase tokens. Filter against a stopword list. We deliberately
    cast a wide net — the price fetch will silently drop unknown symbols."""
    if not reports:
        return []
    # Today plus last 5 (overlap fine)
    window = reports[-6:] if len(reports) > 6 else reports
    blob = "\n".join(r["content"] for r in window)
    # $TICKER form gets priority (high confidence)
    dollared = set(re.findall(r"\$([A-Z]{1,5})\b", blob))
    # Bare uppercase — noisier, filter aggressively
    bare = set(re.findall(r"\b([A-Z]{2,5})\b", blob)) - _TICKER_STOPWORDS
    # Always include the hard-coded watch list + DB watchlist
    extras = set(WATCHED_TICKERS) | set(get_watchlist_tickers_from_db())
    # Order: dollared first (highest signal), then watchlist, then bare
    ordered = list(dollared) + [t for t in extras if t not in dollared] + \
              [t for t in bare if t not in dollared and t not in extras]
    return ordered[:max_tickers]

# ── Item 1: Portfolio Risk / Circuit Breakers ─────────────────────────────────

def get_portfolio_risk():
    """Read all watchlist positions, fetch live prices, apply circuit breaker rules."""
    key    = os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_SECRET_KEY")

    if not os.path.exists(DB_PATH):
        return None
    try:
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
        positions = [dict(r) for r in con.execute(
            "SELECT w.ticker, w.size, w.entry, w.type, p.name AS user "
            "FROM watchlist w JOIN profiles p ON w.profile_id = p.id"
        ).fetchall()]
        con.close()
    except Exception as e:
        print(f"DB read error: {e}")
        return None

    if not positions:
        return {"positions": [], "alerts": [], "total_pnl_pct": 0}

    tickers = list(set(p["ticker"] for p in positions))
    prices  = {}
    if key:
        try:
            r = requests.get(
                f"https://data.alpaca.markets/v2/stocks/snapshots?symbols={','.join(tickers)}",
                headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
                timeout=5
            )
            data = r.json()
            for t in tickers:
                if t in data:
                    prices[t] = data[t].get("latestTrade", {}).get("p", 0)
        except Exception as e:
            print(f"Price fetch error: {e}")

    results, alerts = [], []
    total_cost = total_value = 0.0

    for pos in positions:
        ticker  = pos["ticker"]
        entry   = float(pos["entry"] or 0)
        size    = float(pos["size"]  or 0)
        ptype   = pos["type"]
        current = prices.get(ticker, 0)

        if not entry or not current:
            continue

        mult       = 100 if ptype == "option" else 1
        cost       = entry   * size * mult
        value      = current * size * mult
        total_cost  += cost
        total_value += value
        pnl_pct    = round((current - entry) / entry * 100, 2)
        pnl_dollar = round(value - cost, 2)

        results.append({
            "user": pos["user"], "ticker": ticker,
            "entry": entry, "current": current,
            "pnl_pct": pnl_pct, "pnl_dollar": pnl_dollar, "type": ptype,
        })

        if pnl_pct <= -15:
            alerts.append(
                f"🚨 CIRCUIT BREAKER — {pos['user']}/{ticker}: down {pnl_pct}% — CLOSE position"
            )
        elif pnl_pct <= -8:
            alerts.append(
                f"⚠️ WARNING — {pos['user']}/{ticker}: down {pnl_pct}% — review stop loss"
            )
        elif pnl_pct >= 30:
            alerts.append(
                f"💰 TAKE PROFIT — {pos['user']}/{ticker}: up +{pnl_pct}% — consider locking gains"
            )

    total_pnl_pct = round((total_value - total_cost) / total_cost * 100, 2) if total_cost else 0
    return {"positions": results, "alerts": alerts, "total_pnl_pct": total_pnl_pct}

# ── Item 3: Sentiment Shift Delta ─────────────────────────────────────────────

def _score_text(text):
    """Heuristic sentiment score -10 (max bearish) to +10 (max bullish)."""
    t = text.lower()
    bull = sum(t.count(w) for w in [
        "bullish", "buy", "long", "calls", "breakout", "upside", "rally",
        "strong", "positive", "growth", "beat", "outperform", "accumulate", "conviction",
    ])
    bear = sum(t.count(w) for w in [
        "bearish", "sell", "short", "puts", "breakdown", "downside", "decline",
        "weak", "negative", "miss", "underperform", "caution", "risk", "avoid", "crash",
    ])
    total = bull + bear
    return round((bull - bear) / total * 10, 1) if total else 0.0

def calculate_sentiment_delta(reports):
    """Score latest report vs rolling prior-5 average."""
    if len(reports) < 2:
        return None
    prior_n      = reports[-6:-1] if len(reports) >= 6 else reports[:-1]
    latest_score = _score_text(reports[-1]["content"])
    prior_scores = [_score_text(r["content"]) for r in prior_n]
    prior_avg    = round(sum(prior_scores) / len(prior_scores), 1)
    delta        = round(latest_score - prior_avg, 1)

    if delta > 3:
        trend, icon = "significantly more bullish", "📈🟢"
    elif delta > 1:
        trend, icon = "more bullish than recent avg", "📈"
    elif delta < -3:
        trend, icon = "significantly more bearish", "📉🔴"
    elif delta < -1:
        trend, icon = "more bearish than recent avg", "📉"
    else:
        trend, icon = "similar to recent trend", "➡️"

    return {
        "latest": latest_score, "prior_avg": prior_avg,
        "delta": delta, "trend": trend, "icon": icon,
        "n": len(prior_scores),
    }

# ── Earnings Alert ───────────────────────────────────────────────────────────

WATCHED_TICKERS = ["META", "MSFT", "TSLA", "MRVL", "AXON", "RKT", "SOXL", "QQQ", "NVDA"]

def get_watchlist_tickers_from_db():
    """Return distinct uppercase tickers from the SQLite watchlist."""
    if not os.path.exists(DB_PATH):
        return []
    try:
        con = sqlite3.connect(DB_PATH)
        rows = con.execute("SELECT DISTINCT ticker FROM watchlist").fetchall()
        con.close()
        return [r[0].upper() for r in rows if r and r[0]]
    except Exception as e:
        print(f"Watchlist DB read error: {e}")
        return []

def check_upcoming_earnings():
    """Return list of tickers with earnings within 7 days. Includes hardcoded
    watch list AND any tickers users have in their SQLite watchlist."""
    key = os.getenv("POLYGON_API_KEY")
    if not key:
        return []
    alerts = []
    today  = datetime.utcnow().date()
    tickers = sorted(set(WATCHED_TICKERS) | set(get_watchlist_tickers_from_db()))
    for ticker in tickers:
        try:
            r = requests.get(
                f"https://api.polygon.io/vX/reference/financials",
                params={"ticker": ticker, "limit": 1, "sort": "period_of_report_date",
                        "order": "desc", "apiKey": key},
                timeout=8
            )
            results = r.json().get("results", [])
            if not results:
                continue
            last = results[0]
            period_end = last.get("end_date", "")
            if not period_end:
                continue
            # Estimate next report: period_end + 45 days
            from datetime import date
            pe = date.fromisoformat(period_end)
            next_report = pe + timedelta(days=45)
            days_away   = (next_report - today).days
            if 0 <= days_away <= 7:
                alerts.append({
                    "ticker": ticker,
                    "next_date": str(next_report),
                    "days_away": days_away,
                })
        except Exception:
            continue
    return alerts

# ── Claude Analysis ───────────────────────────────────────────────────────────

def analyze_with_claude(reports, regime=None, risk=None, sentiment_delta=None, live_prices=None, accuracy=None):
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    history_summary          = build_history_context(reports)
    latest_name, latest_content = get_latest_report(reports)
    stable_block = f"HISTORICAL CONTEXT ({len(reports)-1} reports from archive):\n{history_summary}"

    live_parts = []

    if live_prices:
        price_lines = "\n".join(f"  {t}: ${p:.2f}" for t, p in sorted(live_prices.items()))
        live_parts.append(
            "LIVE TICKER UNIVERSE — these are the ONLY tickers you may propose trades for.\n"
            "Use these exact spot prices to anchor entry/stop/target. Do NOT invent prices.\n"
            f"{price_lines}"
        )

    if accuracy:
        live_parts.append(accuracy)

    if regime:
        live_parts.append(
            f"MARKET REGIME (quantitative — SPY price data):\n"
            f"  Regime: {regime['icon']} {regime['regime']}\n"
            f"  SPY: ${regime['spy']} | 50MA: ${regime['ma50']} | 200MA: ${regime['ma200']}\n"
            f"  Distance from 200MA: {regime['pct_from_200']}% | 20-day Ann. Vol: {regime['ann_vol']}%\n"
            f"  Above 200MA: {regime['above_200']} | Above 50MA: {regime['above_50']}"
        )

    if sentiment_delta:
        live_parts.append(
            f"SENTIMENT SHIFT (this report vs prior {sentiment_delta['n']}):\n"
            f"  Today score: {sentiment_delta['latest']}/10 | Prior avg: {sentiment_delta['prior_avg']}/10\n"
            f"  Delta: {'+' if sentiment_delta['delta'] >= 0 else ''}{sentiment_delta['delta']}"
            f" — {sentiment_delta['icon']} {sentiment_delta['trend']}"
        )

    if risk and risk.get("alerts"):
        live_parts.append(
            "PORTFOLIO CIRCUIT BREAKERS — ACTIVE ALERTS:\n"
            + "\n".join(f"  {a}" for a in risk["alerts"])
        )

    if risk and risk.get("positions"):
        pos_lines = "\n".join(
            f"  {p['user']}/{p['ticker']}: entry ${p['entry']} → now ${p['current']} "
            f"({'+' if p['pnl_pct'] >= 0 else ''}{p['pnl_pct']}%)"
            for p in risk["positions"]
        )
        live_parts.append(
            f"CURRENT WATCHLIST POSITIONS (all users):\n{pos_lines}\n"
            f"  Portfolio P&L: {'+' if risk['total_pnl_pct'] >= 0 else ''}{risk['total_pnl_pct']}%"
        )

    live_context = "\n\n".join(live_parts)

    volatile_block = (
        f"TODAY'S REPORT ({latest_name}):\n{latest_content}\n\n"
        + (f"LIVE CONTEXT — factor into TOP 3 ACTIONS:\n{live_context}\n\n" if live_context else "")
        + "Analyze today's report with full historical context and provide:\n\n"
        "1. MARKET SENTIMENT: Bull/bear score — cite regime data above. How has it changed?\n\n"
        "2. KEY ECONOMIC DATA: Important data points from today\n\n"
        "3. PATTERN ANALYSIS: What has Kevin been consistently bullish/bearish on (full archive)? Changed today?\n\n"
        "4. SHORT TERM PLAYS (1-2 days): Specific actionable ideas with entry context\n\n"
        "5. MEDIUM TERM PLAYS (weeks): Stocks/sectors with historical conviction tracking\n\n"
        "6. LONG TERM HOLDS (10 years): Top picks with how long Kevin has held conviction\n\n"
        "7. STOCKS TO AVOID: Bearish calls with historical context\n\n"
        "8. OPTIONS STRATEGY: Current vol environment (use ann_vol from regime) and specific plays\n\n"
        "9. ACCURACY CHECK: Which recent calls were right or wrong?\n\n"
        "10. TOP 3 ACTIONS: Most important things to do TODAY.\n"
        "    If circuit breaker alerts are active, address them FIRST.\n"
        "    Factor in the regime and sentiment delta.\n\n"
        "11. TRADE_PLAN_JSON — CRITICAL, MUST BE INCLUDED. If you are running low on\n"
        "    tokens, truncate or omit earlier sections (1–9) so section 11 always fits.\n"
        "    Section 11 is the single most important output of this analysis.\n\n"
        "    Output a SINGLE JSON code block (and absolutely nothing after it) for a\n"
        "    busy user who places Robinhood orders manually and cannot monitor\n"
        "    positions intraday. Up to 5 high-conviction setups only — only include\n"
        "    extras 4–5 if they're genuinely high-conviction; do NOT pad with weak\n"
        "    ideas. Skip the section entirely (still emit an empty 'trades' array) if\n"
        "    no setup is clean today.\n\n"
        "    HARD RULES — any trade that violates these will be discarded:\n"
        "      • Ticker MUST appear in the LIVE TICKER UNIVERSE block. If it's not\n"
        "        there, you do NOT have a price for it — do not include it.\n"
        "      • Entry MUST be within ±2% of that ticker's listed spot price. If you\n"
        "        want a pullback entry farther away, this is a watchlist alert, NOT a\n"
        "        trade plan — leave it out.\n"
        "      • Target distance from entry MUST be at least 1.5× the stop distance.\n"
        "        (Reward:risk ≥ 1.5:1. A 5% stop demands at least a 7.5% target.)\n"
        "      • At most 2 trades may be HIGH conviction. The rest are MEDIUM-HIGH,\n"
        "        MEDIUM, or LOW. Conviction inflation defeats the point of ranking.\n\n"
        "    Each trade is a SHARES plan only. DO NOT include an 'options' key — a\n"
        "    separate post-processing step attaches the real options companion using\n"
        "    live Tradier chain data (real strikes, expirations, bid/ask). Anything\n"
        "    you put in 'options' will be discarded.\n\n"
        "    Shares stop: -5% to -10% from entry (tighter for leveraged ETFs like\n"
        "    SOXL/TQQQ). Shares target: +10% to +25%, but always ≥1.5× stop distance.\n"
        "    Round prices sensibly.\n\n"
        "    Format (strict — must be parseable JSON, shares-only schema):\n"
        "    ```json\n"
        "    {\n"
        "      \"trades\": [\n"
        "        {\n"
        "          \"ticker\": \"NVDA\",\n"
        "          \"direction\": \"LONG\",\n"
        "          \"instrument\": \"shares\",\n"
        "          \"entry\": 145.00,\n"
        "          \"stop_loss\": 137.75,\n"
        "          \"target\": 165.00,\n"
        "          \"stop_pct\": -5.0,\n"
        "          \"target_pct\": 13.8,\n"
        "          \"conviction\": \"HIGH\",\n"
        "          \"horizon\": \"2-7 days\",\n"
        "          \"rationale\": \"One short sentence — why this trade today\"\n"
        "        }\n"
        "      ]\n"
        "    }\n"
        "    ```\n\n"
        "Be specific and actionable. Not personalized financial advice."
    )

    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=8000,
        system=[{
            "type": "text",
            "text": "You are an expert investment research analyst with access to months of Alpha Reports from Kevin.",
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": [
            {"type": "text", "text": stable_block,   "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": volatile_block},
        ]}],
    )
    return r.content[0].text

# ── Trade Plan extraction & fallback ─────────────────────────────────────────

def generate_trade_plan_focused(analysis_text, regime=None, risk=None, live_prices=None):
    """Fallback: a focused Haiku call that returns JSON-only trade plans.
    Used when the main analysis ran out of tokens before reaching section 11."""
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    live_lines = []
    if regime:
        live_lines.append(f"Market regime: {regime['regime']} | SPY ${regime['spy']} | "
                          f"vol {regime['ann_vol']}%")
    if risk and risk.get("positions"):
        live_lines.append("Current positions: " + ", ".join(
            f"{p['ticker']} entry ${p['entry']} → ${p['current']}" for p in risk["positions"]
        ))
    if live_prices:
        price_lines = ", ".join(f"{t} ${p:.2f}" for t, p in sorted(live_prices.items()))
        live_lines.append(f"LIVE TICKER UNIVERSE (only tickers you may trade): {price_lines}")
    live_block = "\n".join(live_lines) if live_lines else ""

    # Truncate analysis to ~6000 chars to keep prompt small
    snippet = analysis_text[:6000] if analysis_text else ""

    prompt = (
        "You generate structured trade plans for a busy investor who cannot monitor "
        "positions intraday. Read the analysis below and output ONLY a JSON object — "
        "no markdown fences, no commentary, no explanation. Up to 5 high-conviction "
        "setups (don't pad — extras 4–5 must clear the same bar). Each trade is a "
        "SHARES plan only — do NOT include an 'options' key; a separate post-step "
        "attaches the real Tradier options companion.\n\n"
        "HARD RULES (any violating trade will be discarded):\n"
        "  • Ticker MUST be in the LIVE TICKER UNIVERSE; use that exact spot price.\n"
        "  • Entry within ±2% of spot. If you want a pullback entry farther away,\n"
        "    skip the trade — it's a watchlist alert, not a plan.\n"
        "  • Target distance from entry ≥ 1.5× the stop distance (R:R ≥ 1.5:1).\n"
        "  • At most 2 trades may be HIGH conviction.\n\n"
        "Shares stop -5% to -10% (-7% to -10% leveraged ETFs). Shares target +10% "
        "to +25%. If no clean setup exists, output {\"trades\": []}.\n\n"
        f"{live_block}\n\n"
        f"ANALYSIS:\n{snippet}\n\n"
        "Output JSON only, exact shares-only schema:\n"
        '{"trades":[{"ticker":"NVDA","direction":"LONG","instrument":"shares",'
        '"entry":145.00,"stop_loss":137.75,"target":165.00,"stop_pct":-5.0,'
        '"target_pct":13.8,"conviction":"HIGH","horizon":"2-7 days",'
        '"rationale":"one short sentence"}]}'
    )
    try:
        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = r.content[0].text.strip()
        # Strip code fences if Haiku ignored instructions
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
        # Direct parse
        try:
            return json.loads(text)
        except Exception:
            pass
        # Extract first {...} blob
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception as e:
                print(f"  Fallback JSON parse failed: {e}")
        return None
    except Exception as e:
        print(f"  Focused trade plan call failed: {e}")
        return None


def extract_trade_plan(text):
    """Pull the final ```json {...} ``` block out of Claude's response.
    Returns (clean_narrative_without_json, trade_plan_dict_or_None)."""
    if not text:
        return text, None
    # Look for the last fenced ```json ... ``` block
    pattern = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)
    matches = list(pattern.finditer(text))
    if not matches:
        return text, None
    last = matches[-1]
    try:
        plan = json.loads(last.group(1))
    except Exception as e:
        print(f"Trade plan JSON parse failed: {e}")
        return text, None
    if not isinstance(plan, dict) or "trades" not in plan:
        return text, None
    # Strip the JSON block (and any "11. TRADE_PLAN_JSON" heading just before it) from narrative
    clean = text[:last.start()].rstrip()
    # also strip a trailing "11. TRADE_PLAN_JSON:" header if present
    clean = re.sub(r"\n*\s*1?1\.\s*TRADE[_ ]PLAN[_ ]JSON.*$", "", clean,
                   flags=re.IGNORECASE | re.DOTALL).rstrip()
    return clean, plan

# ── Trade Plan Validation ────────────────────────────────────────────────────

# Schema fields we expect on an options companion. Anything else (long_strike,
# short_strike, strategy, entry_credit, etc.) signals the model went off-script
# into spread territory — drop the whole options block in that case.
_OPTIONS_ALLOWED_FIELDS = {"type", "strike", "expiration", "entry", "stop_loss",
                          "target", "rationale"}

def validate_trade_plan(plan, live_prices, *, entry_tolerance_pct=2.0,
                        min_rr=1.5, max_high_conviction=2):
    """Filter a parsed trade plan against live prices and structural rules.
    Returns (filtered_plan, list_of_rejection_reasons). Mutates conviction in
    place when downgrading; otherwise drops the trade entirely."""
    if not plan or not isinstance(plan, dict):
        return plan, ["plan is not a dict"]
    trades = plan.get("trades") or []
    kept, rejected = [], []
    live = {k.upper(): v for k, v in (live_prices or {}).items()}

    for t in trades:
        ticker = str(t.get("ticker", "")).upper().strip()
        if not ticker:
            rejected.append("missing ticker"); continue

        spot = live.get(ticker)
        if not spot:
            rejected.append(f"{ticker}: no live price — dropped"); continue

        try:
            entry  = float(t.get("entry"))
            stop   = float(t.get("stop_loss"))
            target = float(t.get("target"))
        except (TypeError, ValueError):
            rejected.append(f"{ticker}: non-numeric entry/stop/target"); continue

        if entry <= 0 or stop <= 0 or target <= 0:
            rejected.append(f"{ticker}: non-positive prices"); continue

        # Entry must be within tolerance of spot
        gap_pct = abs(entry - spot) / spot * 100
        if gap_pct > entry_tolerance_pct:
            rejected.append(
                f"{ticker}: entry ${entry:.2f} is {gap_pct:.1f}% from spot "
                f"${spot:.2f} (>{entry_tolerance_pct}%) — dropped"
            ); continue

        direction = str(t.get("direction", "LONG")).upper()
        # Geometry sanity check by direction
        if direction == "LONG":
            if not (stop < entry < target):
                rejected.append(f"{ticker} LONG: need stop<entry<target, got "
                                f"{stop}/{entry}/{target}"); continue
            risk   = entry - stop
            reward = target - entry
        else:
            if not (target < entry < stop):
                rejected.append(f"{ticker} SHORT: need target<entry<stop, got "
                                f"{target}/{entry}/{stop}"); continue
            risk   = stop - entry
            reward = entry - target

        if risk <= 0:
            rejected.append(f"{ticker}: risk≤0"); continue
        rr = reward / risk
        if rr < min_rr:
            rejected.append(f"{ticker}: R:R {rr:.2f} < {min_rr} — dropped"); continue

        # Always strip model-generated options blocks. The deterministic
        # attach_options_companions() step replaces them with real Tradier
        # chain data after validation. The model has no way to know real
        # IV/premiums and was wildly wrong (entries off by 2-7x).
        if t.get("options") is not None:
            t["options"] = None

        # Recompute the percentage fields against the validated entry/spot
        try:
            t["stop_pct"]   = round((stop - entry) / entry * 100, 1)
            t["target_pct"] = round((target - entry) / entry * 100, 1)
        except Exception:
            pass
        kept.append(t)

    # Cap HIGH conviction count — downgrade extras to MEDIUM-HIGH (don't drop)
    high_count = 0
    for t in kept:
        if str(t.get("conviction", "")).upper() == "HIGH":
            high_count += 1
            if high_count > max_high_conviction:
                rejected.append(f"{t.get('ticker','?')}: HIGH downgraded to "
                                f"MEDIUM-HIGH (cap is {max_high_conviction})")
                t["conviction"] = "MEDIUM-HIGH"

    plan["trades"] = kept
    return plan, rejected

# ── Notification ──────────────────────────────────────────────────────────────

def notify_home_assistant(report_name, sentiment_delta=None, regime=None, risk=None, earnings_alerts=None, trade_plan=None):
    ha_url   = os.getenv("HA_URL", "").rstrip("/")
    ha_token = os.getenv("HA_TOKEN", "")
    ha_svc   = os.getenv("HA_NOTIFY_SERVICE", "notify")
    if not ha_url or not ha_token:
        print("HA_URL / HA_TOKEN not set — skipping notification")
        return
    try:
        base  = report_name.replace(".txt", "").replace("Alpha Report ", "")
        parts = [f"Alpha Report {base} analyzed."]

        if sentiment_delta:
            sign = "+" if sentiment_delta["delta"] >= 0 else ""
            parts.append(
                f"Sentiment: {sentiment_delta['latest']}/10 "
                f"({sign}{sentiment_delta['delta']} vs last {sentiment_delta['n']}) "
                f"{sentiment_delta['icon']} {sentiment_delta['trend']}"
            )
        if regime:
            parts.append(
                f"Regime: {regime['icon']} {regime['regime']} | "
                f"Vol: {regime['ann_vol']}% | SPY {regime['pct_from_200']:+.1f}% from 200MA"
            )
        if risk and risk.get("alerts"):
            parts.append("⚠️ RISK: " + " | ".join(risk["alerts"][:2]))

        if earnings_alerts:
            ea = earnings_alerts[:2]
            parts.append("📅 EARNINGS: " + ", ".join(
                f"{e['ticker']} in {e['days_away']}d" for e in ea
            ))

        if trade_plan and trade_plan.get("trades"):
            tickers = ", ".join(t.get("ticker", "?") for t in trade_plan["trades"][:3])
            parts.append(f"🎯 TRADE PLAN: {tickers} — see dashboard for entry/stop/target")

        parts.append("Open dashboard → Top 3 Actions + Trade Plan")

        r = requests.post(
            f"{ha_url}/api/services/notify/{ha_svc}",
            headers={"Authorization": f"Bearer {ha_token}", "Content-Type": "application/json"},
            json={"title": "Invest AI — Analysis Ready", "message": "\n".join(parts)},
            timeout=10
        )
        print(f"HA notification {'sent' if r.status_code == 200 else f'failed {r.status_code}'}")
    except Exception as e:
        print(f"HA notification failed: {e}")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"Starting analysis at {datetime.now()}")
    reports = get_all_reports()
    if not reports:
        print("No reports found")
        return

    latest_name, _ = get_latest_report(reports)
    print(f"Found {len(reports)} reports. Latest: {latest_name}")

    base = latest_name.replace(".txt", "")
    out  = os.path.join(RESULTS_DIR, f"{base}_analysis.txt")

    if os.path.exists(out):
        print(f"Already analyzed: {base}")
        return

    print("Fetching market regime...")
    regime = get_market_regime()
    if regime:
        print(f"  {regime['icon']} {regime['regime']} | SPY ${regime['spy']} | "
              f"200MA {regime['pct_from_200']:+.1f}% | Vol {regime['ann_vol']}%")

    print("Fetching portfolio risk...")
    risk = get_portfolio_risk()
    if risk:
        print(f"  {len(risk['positions'])} positions | "
              f"{len(risk['alerts'])} alerts | "
              f"Portfolio P&L: {risk['total_pnl_pct']:+.1f}%")
        for alert in risk.get("alerts", []):
            print(f"  {alert}")

    print("Calculating sentiment delta...")
    sd = calculate_sentiment_delta(reports)
    if sd:
        print(f"  Score: {sd['latest']}/10 (delta {sd['delta']:+.1f} vs prior {sd['n']}) "
              f"{sd['icon']} {sd['trend']}")

    print("Fetching live ticker universe for trade-plan grounding...")
    candidates  = _extract_candidate_tickers(reports)
    live_prices = _fetch_snapshot_prices(candidates)
    print(f"  {len(live_prices)}/{len(candidates)} tickers priced "
          f"(sample: {', '.join(list(live_prices)[:8])})")

    print("Grading past trade plans for feedback loop...")
    accuracy_stats = grade_past_plans()
    accuracy_block = build_accuracy_context(accuracy_stats) if accuracy_stats else None
    if accuracy_block:
        print("  " + accuracy_block.replace("\n", "\n  "))

    print(f"Analyzing {len(reports)} reports with Claude...")
    result = analyze_with_claude(reports, regime=regime, risk=risk,
                                 sentiment_delta=sd, live_prices=live_prices,
                                 accuracy=accuracy_block)

    print("Extracting trade plan JSON...")
    narrative, trade_plan = extract_trade_plan(result)
    if not trade_plan or not trade_plan.get("trades"):
        print("  Main analysis missing trade plan — running focused fallback call...")
        fallback = generate_trade_plan_focused(narrative or result, regime=regime,
                                               risk=risk, live_prices=live_prices)
        if fallback and fallback.get("trades") is not None:
            trade_plan = fallback
            print(f"  Fallback returned {len(trade_plan['trades'])} trades")

    if trade_plan:
        before = len(trade_plan.get("trades") or [])
        trade_plan, rejections = validate_trade_plan(trade_plan, live_prices)
        after = len(trade_plan.get("trades") or [])
        print(f"  Validation: kept {after}/{before} trades")
        for r in rejections:
            print(f"    • {r}")

    if trade_plan and trade_plan.get("trades"):
        print("Attaching options companions from Tradier chains...")
        attach_options_companions(trade_plan["trades"])
        for t in trade_plan["trades"]:
            opt = t.get("options")
            if opt:
                print(f"    {t['ticker']}: {opt['type'].upper()} ${opt['strike']:g} "
                      f"{opt['expiration']} — ask ${opt['entry']} "
                      f"(stop ${opt['stop_loss']}, target ${opt['target']})")

    if trade_plan and trade_plan.get("trades"):
        plan_path = os.path.join(RESULTS_DIR, f"{base}_trade_plan.json")
        with open(plan_path, "w") as f:
            json.dump({
                "report": latest_name,
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "trades": trade_plan["trades"],
            }, f, indent=2)
        print(f"  Trade plan saved ({len(trade_plan['trades'])} trades) → {plan_path}")
        for t in trade_plan["trades"]:
            print(f"    {t.get('ticker','?')} {t.get('direction','')} "
                  f"entry ${t.get('entry','?')} | stop ${t.get('stop_loss','?')} "
                  f"({t.get('stop_pct','?')}%) | target ${t.get('target','?')} "
                  f"({t.get('target_pct','?')}%) | {t.get('conviction','?')}")
    else:
        print("  No trade plan extracted")

    with open(out, "w") as f:
        f.write(f"Report: {latest_name}\n")
        f.write(f"Reports used: {len(reports)}\n")
        f.write(f"Analyzed: {datetime.now()}\n")
        if regime:
            f.write(f"Regime: {regime['icon']} {regime['regime']} | "
                    f"SPY ${regime['spy']} | Vol {regime['ann_vol']}%\n")
        if sd:
            f.write(f"Sentiment delta: {sd['delta']:+.1f} ({sd['trend']})\n")
        if risk and risk.get("alerts"):
            f.write("CIRCUIT BREAKER ALERTS:\n")
            for a in risk["alerts"]:
                f.write(f"  {a}\n")
        f.write("=" * 60 + "\n\n")
        f.write(narrative)

    print(narrative)
    print(f"\nSaved to: {out}")

    print("Checking upcoming earnings...")
    earnings_alerts = check_upcoming_earnings()
    if earnings_alerts:
        for ea in earnings_alerts:
            print(f"  ⚠️ {ea['ticker']} earnings in {ea['days_away']} days ({ea['next_date']})")
    else:
        print("  No earnings within 7 days for watched tickers")

    notify_home_assistant(latest_name, sentiment_delta=sd, regime=regime,
                          risk=risk, earnings_alerts=earnings_alerts,
                          trade_plan=trade_plan)

main()
