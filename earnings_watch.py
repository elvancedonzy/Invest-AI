"""Daily earnings watcher — pushes a Home Assistant notification if any ticker
in the user's SQLite watchlist has earnings within 7 days.

Runs independently of the Alpha Report analyzer so the user gets a morning
heads-up even on days when Kevin doesn't post a report.
"""
import os, sqlite3, requests
from datetime import datetime

from earnings import fetch_real_earnings

DB_PATH = "/reports/users.db"


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
        out = {}
        for ticker, user in rows:
            if not ticker:
                continue
            out.setdefault(ticker.upper(), set()).add(user)
        return out
    except Exception as e:
        print(f"Watchlist DB read error: {e}")
        return {}


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
        timing = f" {a['timing']}" if a.get("timing") else ""
        lines.append(f"• {a['ticker']} — in {a['days_away']}d ({a['next_date']}{timing}) [{users}]")
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

    tickers = get_watchlist_tickers()
    if not tickers:
        print("No watchlist holdings — nothing to check")
        return
    print(f"Checking {len(tickers)} watchlist tickers: {sorted(tickers.keys())}")

    alerts = []
    for ticker, users in tickers.items():
        e = fetch_real_earnings(ticker)
        if not e or e.get("days_away") is None:
            continue
        print(f"  {ticker}: next {e['next_date']} ({e['days_away']}d, {e.get('source','?')})")
        if 0 <= e["days_away"] <= 7:
            alerts.append({
                "ticker":    ticker,
                "next_date": datetime.strptime(e["next_date"], "%Y-%m-%d").strftime("%b %d"),
                "days_away": e["days_away"],
                "timing":    e.get("timing", ""),
                "users":     users,
            })

    if alerts:
        print(f"Triggering HA notification for {len(alerts)} earnings alerts")
        notify_home_assistant(alerts)
    else:
        print("No earnings within 7 days — no notification")


main()
