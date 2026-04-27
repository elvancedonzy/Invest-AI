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

def check_upcoming_earnings():
    """Return list of tickers with earnings within 7 days, using Polygon financials."""
    key = os.getenv("POLYGON_API_KEY")
    if not key:
        return []
    alerts = []
    today  = datetime.utcnow().date()
    for ticker in WATCHED_TICKERS:
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

def analyze_with_claude(reports, regime=None, risk=None, sentiment_delta=None):
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    history_summary          = build_history_context(reports)
    latest_name, latest_content = get_latest_report(reports)
    stable_block = f"HISTORICAL CONTEXT ({len(reports)-1} reports from archive):\n{history_summary}"

    live_parts = []

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
        "Be specific and actionable. Not personalized financial advice."
    )

    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=3000,
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

# ── Notification ──────────────────────────────────────────────────────────────

def notify_home_assistant(report_name, sentiment_delta=None, regime=None, risk=None, earnings_alerts=None):
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

        parts.append("Open dashboard → Top 3 Actions")

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

    print(f"Analyzing {len(reports)} reports with Claude...")
    result = analyze_with_claude(reports, regime=regime, risk=risk, sentiment_delta=sd)

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
        f.write(result)

    print(result)
    print(f"\nSaved to: {out}")

    print("Checking upcoming earnings...")
    earnings_alerts = check_upcoming_earnings()
    if earnings_alerts:
        for ea in earnings_alerts:
            print(f"  ⚠️ {ea['ticker']} earnings in {ea['days_away']} days ({ea['next_date']})")
    else:
        print("  No earnings within 7 days for watched tickers")

    notify_home_assistant(latest_name, sentiment_delta=sd, regime=regime,
                          risk=risk, earnings_alerts=earnings_alerts)

main()
