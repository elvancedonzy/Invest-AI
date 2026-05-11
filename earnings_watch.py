"""Daily earnings watcher — pushes a Home Assistant notification if any ticker
in the user's SQLite watchlist has earnings within 7 days.

Runs independently of the Alpha Report analyzer so the user gets a morning
heads-up even on days when Kevin doesn't post a report.
"""
import os, sqlite3, requests
from datetime import datetime, timedelta, date

DB_PATH = "/reports/users.db"

FISCAL_PERIOD_ENDS = {
    "Q1": (3, 31), "Q2": (6, 30), "Q3": (9, 30), "Q4": (12, 31),
    "FY": (12, 31),
}


def get_watchlist_tickers():
    if not os.path.exists(DB_PATH):
        return []
    try:
        con = sqlite3.connect(DB_PATH)
        rows = con.execute(
            "SELECT DISTINCT w.ticker, p.name AS user "
            "FROM watchlist w JOIN profiles p ON w.profile_id = p.id"
        ).fetchall()
        con.close()
        # group by ticker → set of users
        out = {}
        for ticker, user in rows:
            if not ticker:
                continue
            out.setdefault(ticker.upper(), set()).add(user)
        return out
    except Exception as e:
        print(f"Watchlist DB read error: {e}")
        return {}


def estimate_next_earnings(ticker, polygon_key):
    """Return (next_date, days_away) or (None, None)."""
    try:
        r = requests.get(
            "https://api.polygon.io/vX/reference/financials",
            params={"ticker": ticker, "limit": 1,
                    "sort": "period_of_report_date", "order": "desc",
                    "apiKey": polygon_key},
            timeout=8,
        )
        items = r.json().get("results", [])
        if not items:
            return None, None
        latest = items[0]
        fiscal = latest.get("fiscal_period", "")
        fy     = int(latest.get("fiscal_year", 0) or 0)
        filing = latest.get("filing_date")
        today  = datetime.utcnow().date()

        if filing:
            next_date = datetime.strptime(filing, "%Y-%m-%d").date() + timedelta(days=91)
        elif fiscal in FISCAL_PERIOD_ENDS and fy:
            m, d = FISCAL_PERIOD_ENDS[fiscal]
            period_end = date(fy, m, d)
            if period_end < today - timedelta(days=180):
                period_end = date(fy + 1, m, d)
            next_date = period_end + timedelta(days=45)
        else:
            return None, None

        while next_date < today:
            next_date += timedelta(days=91)
        return next_date, (next_date - today).days
    except Exception as e:
        print(f"Polygon error for {ticker}: {e}")
        return None, None


def notify_home_assistant(alerts):
    ha_url   = os.getenv("HA_URL", "").rstrip("/")
    ha_token = os.getenv("HA_TOKEN", "")
    ha_svc   = os.getenv("HA_NOTIFY_SERVICE", "notify")
    if not ha_url or not ha_token:
        print("HA_URL / HA_TOKEN not set — skipping notification")
        return
    if not alerts:
        return
    # Compose body
    lines = ["⚠️ Earnings within 7 days for your watchlist:"]
    for a in alerts:
        users = ", ".join(sorted(a["users"]))
        lines.append(f"• {a['ticker']} — in {a['days_away']}d ({a['next_date']}) [{users}]")
    lines.append("")
    lines.append("Consider exiting before the report. Open dashboard to review.")
    body = "\n".join(lines)
    try:
        r = requests.post(
            f"{ha_url}/api/services/notify/{ha_svc}",
            headers={"Authorization": f"Bearer {ha_token}",
                     "Content-Type": "application/json"},
            json={"title": "Invest AI — Earnings Watch", "message": body},
            timeout=10,
        )
        print(f"HA notification {'sent' if r.status_code == 200 else f'failed {r.status_code}'}")
    except Exception as e:
        print(f"HA notification failed: {e}")


def main():
    print(f"Earnings watch started at {datetime.now()}")
    polygon_key = os.getenv("POLYGON_API_KEY")
    if not polygon_key:
        print("POLYGON_API_KEY not set — aborting")
        return

    tickers = get_watchlist_tickers()
    if not tickers:
        print("No watchlist holdings — nothing to check")
        return
    print(f"Checking {len(tickers)} watchlist tickers: {sorted(tickers.keys())}")

    alerts = []
    for ticker, users in tickers.items():
        next_date, days_away = estimate_next_earnings(ticker, polygon_key)
        if next_date is None:
            continue
        print(f"  {ticker}: next ~{next_date} ({days_away}d)")
        if 0 <= days_away <= 7:
            alerts.append({
                "ticker":    ticker,
                "next_date": next_date.strftime("%b %d"),
                "days_away": days_away,
                "users":     users,
            })

    if alerts:
        print(f"Triggering HA notification for {len(alerts)} earnings alerts")
        notify_home_assistant(alerts)
    else:
        print("No earnings within 7 days — no notification")


main()
