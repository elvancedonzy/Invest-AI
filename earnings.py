"""Real-earnings calendar with Finnhub primary + Polygon /financials estimate fallback.

Single source of truth for any code that needs to know when a ticker reports next.
Used by main.py (dashboard /earnings card, Ask Claude context), analyzer.py
(validate_trade_plan gate, daily watch), and earnings_watch.py (HA notifier).

Cache: /reports/earnings_cache.json (NFS, shared between API pod and CronJob),
6-hour TTL. Real earnings dates don't change minute-by-minute.
"""
import json
import os
import requests
from datetime import datetime, timedelta, date

CACHE_PATH = "/reports/earnings_cache.json"
CACHE_TTL_SECS = 6 * 3600

FISCAL_PERIOD_ENDS = {
    "Q1": (3, 31), "Q2": (6, 30), "Q3": (9, 30), "Q4": (12, 31),
    "FY": (12, 31),
}


def _load_cache():
    try:
        with open(CACHE_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(cache):
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        with open(CACHE_PATH, "w") as f:
            json.dump(cache, f)
    except Exception:
        pass


def _fetch_finnhub(ticker):
    """Real confirmed earnings via Finnhub /calendar/earnings.
    Returns (next_date: date | None, timing: str, responded: bool).
    `responded=True` means the API call succeeded (even if upcoming list is empty).
    Callers should only fall back to estimates when `responded=False`."""
    key = os.getenv("FINNHUB_KEY")
    if not key:
        return None, "", False
    today = date.today()
    # Wider 365d horizon — earnings that just reported still appear in the
    # response (with epsActual populated); we filter those out below. A 90d
    # window misses tickers whose next quarter hasn't been scheduled yet.
    horizon = today + timedelta(days=365)
    try:
        r = requests.get(
            "https://finnhub.io/api/v1/calendar/earnings",
            params={"from": today.isoformat(), "to": horizon.isoformat(),
                    "symbol": ticker.upper(), "token": key},
            timeout=8,
        )
        if r.status_code != 200:
            return None, "", False
        data = r.json() or {}
        items = data.get("earningsCalendar")
        if items is None:
            return None, "", False
        upcoming = []
        for it in items:
            d = it.get("date")
            if not d:
                continue
            try:
                ed = datetime.strptime(d, "%Y-%m-%d").date()
            except Exception:
                continue
            # Strict future-only filter. If `epsActual` is populated the event
            # has already reported, even if the date is technically today.
            if ed < today:
                continue
            if ed == today and it.get("epsActual") is not None:
                continue
            upcoming.append((ed, str(it.get("hour", "") or "").lower()))
        if not upcoming:
            return None, "", True  # API responded, no upcoming events
        upcoming.sort(key=lambda x: x[0])
        ed, hour = upcoming[0]
        timing = "BMO" if hour == "bmo" else "AMC" if hour == "amc" else ""
        return ed, timing, True
    except Exception:
        return None, "", False


def _fetch_estimate(ticker):
    """Polygon /financials estimate — fallback only.
    Returns next_date as date, or None."""
    key = os.getenv("POLYGON_API_KEY")
    if not key:
        return None
    try:
        r = requests.get(
            "https://api.polygon.io/vX/reference/financials",
            params={"ticker": ticker, "limit": 1, "timeframe": "quarterly",
                    "order": "desc", "apiKey": key},
            timeout=6,
        )
        items = r.json().get("results", [])
        if not items:
            return None
        latest = items[0]
        fiscal = latest.get("fiscal_period", "")
        fy     = int(latest.get("fiscal_year", 0) or 0)
        filing = latest.get("filing_date")
        today  = date.today()
        if filing:
            nd = datetime.strptime(filing, "%Y-%m-%d").date() + timedelta(days=91)
        elif fiscal in FISCAL_PERIOD_ENDS and fy:
            m, d = FISCAL_PERIOD_ENDS[fiscal]
            pe = date(fy, m, d)
            if pe < today - timedelta(days=180):
                pe = date(fy + 1, m, d)
            nd = pe + timedelta(days=45)
        else:
            return None
        while nd < today:
            nd += timedelta(days=91)
        return nd
    except Exception:
        return None


def fetch_real_earnings(ticker):
    """Return {next_date, days_away, timing, source} or None.
    Per-ticker disk cache at /reports/earnings_cache.json, 6h TTL."""
    if not ticker:
        return None
    ticker = ticker.upper().strip()
    today = date.today()

    cache = _load_cache()
    entry = cache.get(ticker)
    now_ts = datetime.utcnow().timestamp()
    if entry and (now_ts - entry.get("cached_ts", 0)) < CACHE_TTL_SECS:
        nd_iso = entry.get("next_date_iso")
        if nd_iso:
            try:
                nd = date.fromisoformat(nd_iso)
            except Exception:
                nd = None
            if nd and nd >= today:
                return {
                    "next_date": nd.isoformat(),
                    "days_away": (nd - today).days,
                    "timing":    entry.get("timing", ""),
                    "source":    entry.get("source", ""),
                }
        else:
            return None  # cached negative result

    nd, timing, finnhub_ok = _fetch_finnhub(ticker)
    if nd:
        source = "finnhub"
    elif finnhub_ok:
        # Finnhub responded successfully but has no upcoming earnings for this
        # ticker (typically because next quarter hasn't been scheduled yet).
        # That's truthful — don't paper over with a guess.
        source = ""
    else:
        nd = _fetch_estimate(ticker)
        source = "estimate" if nd else ""
        timing = ""

    cache[ticker] = {
        "next_date_iso": nd.isoformat() if nd else None,
        "timing": timing,
        "source": source,
        "cached_ts": now_ts,
    }
    _save_cache(cache)

    if not nd:
        return None
    return {
        "next_date": nd.isoformat(),
        "days_away": (nd - today).days,
        "timing":    timing,
        "source":    source,
    }


def get_calendar(tickers):
    """Return a list of earnings entries sorted by days_away.
    Each entry: {ticker, next_date, days_away, timing, source}."""
    seen = set()
    out = []
    for t in tickers:
        if not t:
            continue
        u = t.upper().strip()
        if u in seen:
            continue
        seen.add(u)
        e = fetch_real_earnings(u)
        if e:
            out.append({"ticker": u, **e})
    out.sort(key=lambda x: (x["days_away"] is None,
                            x["days_away"] if x["days_away"] is not None else 999))
    return out
