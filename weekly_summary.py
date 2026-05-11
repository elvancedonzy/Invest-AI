"""Friday afternoon weekly summary — pushes a Home Assistant notification with
the week's portfolio P&L, anything older than 10 days that should be closed,
and a nudge to do the Sunday review.

Designed for the busy user who only touches the dashboard once a week.
"""
import os, glob, sqlite3, requests
from datetime import datetime, timedelta

REPORTS_DIR = "/reports/"
RESULTS_DIR = "/reports/results/"
DB_PATH     = "/reports/users.db"


def get_positions():
    """Return list of {user, ticker, size, entry, type, added_at}."""
    if not os.path.exists(DB_PATH):
        return []
    try:
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT w.ticker, w.size, w.entry, w.type, w.added_at, p.name AS user "
            "FROM watchlist w JOIN profiles p ON w.profile_id = p.id"
        ).fetchall()
        con.close()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"DB read error: {e}")
        return []


def get_prices(tickers):
    key    = os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_SECRET_KEY")
    if not key or not tickers:
        return {}
    try:
        r = requests.get(
            f"https://data.alpaca.markets/v2/stocks/snapshots?symbols={','.join(tickers)}",
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
            timeout=8,
        )
        data = r.json()
        out = {}
        for t in tickers:
            if t in data:
                out[t] = data[t].get("latestTrade", {}).get("p", 0)
        return out
    except Exception as e:
        print(f"Price fetch error: {e}")
        return {}


def _days_old(added_at):
    if not added_at:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            d = datetime.strptime(added_at[:19], fmt)
            return (datetime.utcnow() - d).days
        except Exception:
            continue
    return None


def get_latest_report_name():
    files = glob.glob(os.path.join(REPORTS_DIR, "*.txt"))
    if not files:
        return None
    latest = max(files, key=os.path.getmtime)
    return os.path.basename(latest)


def build_summary():
    positions = get_positions()
    tickers   = sorted({p["ticker"].upper() for p in positions if p.get("ticker")})
    prices    = get_prices(tickers)

    lines = ["📅 Weekly Trading Review"]
    lines.append("")

    if not positions:
        lines.append("No open positions in watchlist. Nothing to review.")
        return "\n".join(lines)

    total_cost = total_value = 0.0
    stale_lines = []
    winners = losers = 0

    for p in positions:
        ticker  = p["ticker"].upper()
        entry   = float(p.get("entry") or 0)
        size    = float(p.get("size")  or 0)
        ptype   = (p.get("type") or "stock").lower()
        current = prices.get(ticker, 0)
        if not entry or not current or not size:
            continue
        mult = 100 if ptype == "option" else 1
        cost  = entry * size * mult
        value = current * size * mult
        total_cost  += cost
        total_value += value
        pnl_pct = (current - entry) / entry * 100
        if pnl_pct >= 0:
            winners += 1
        else:
            losers += 1
        age = _days_old(p.get("added_at"))
        if age is not None and age >= 10:
            stale_lines.append(
                f"• {p['user']}/{ticker} — {age}d old, P&L {pnl_pct:+.1f}% — Kevin's window has closed"
            )

    if total_cost > 0:
        pnl_dollar = total_value - total_cost
        pnl_pct    = pnl_dollar / total_cost * 100
        sign = "+" if pnl_dollar >= 0 else ""
        lines.append(
            f"Portfolio: {sign}${pnl_dollar:,.2f} ({sign}{pnl_pct:.1f}%) — "
            f"{winners} winners / {losers} losers"
        )
    else:
        lines.append("Portfolio: no priced positions")

    if stale_lines:
        lines.append("")
        lines.append("⏰ Close these — held >10 days:")
        lines.extend(stale_lines)

    latest = get_latest_report_name()
    if latest:
        lines.append("")
        lines.append(f"Latest Alpha Report: {latest}")

    lines.append("")
    lines.append("Sunday review: open dashboard, close stale positions, "
                 "log this week's calls in track_record.txt. 15 min — then done.")

    return "\n".join(lines)


def notify_home_assistant(body):
    ha_url   = os.getenv("HA_URL", "").rstrip("/")
    ha_token = os.getenv("HA_TOKEN", "")
    ha_svc   = os.getenv("HA_NOTIFY_SERVICE", "notify")
    if not ha_url or not ha_token:
        print("HA_URL / HA_TOKEN not set — skipping notification")
        print("---- Would have sent ----")
        print(body)
        return
    try:
        r = requests.post(
            f"{ha_url}/api/services/notify/{ha_svc}",
            headers={"Authorization": f"Bearer {ha_token}",
                     "Content-Type": "application/json"},
            json={"title": "Invest AI — Weekly Review", "message": body},
            timeout=10,
        )
        print(f"HA notification {'sent' if r.status_code == 200 else f'failed {r.status_code}'}")
    except Exception as e:
        print(f"HA notification failed: {e}")


def main():
    print(f"Weekly summary started at {datetime.now()}")
    body = build_summary()
    print(body)
    print("---")
    notify_home_assistant(body)


main()
