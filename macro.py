"""Macro-event awareness for the trade-plan validator.

Three signals:
  1. FOMC meeting days     (hardcoded calendar — Fed publishes annually)
  2. CPI release days      (hardcoded calendar — BLS publishes annually)
  3. VIX level             (Yahoo Finance unofficial — no key needed; Finnhub
                            free tier blocks index data)

Used by analyzer.py:validate_trade_plan after the earnings gate.
"""
import json
import os
import requests
from datetime import datetime, timedelta, date

VIX_CACHE_PATH = "/reports/vix_cache.json"
VIX_CACHE_TTL_SECS = 6 * 3600

# FOMC meeting dates published yearly by the Federal Reserve.
# Source: https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
# Update Q4 each year when next year's schedule drops.
FOMC_DATES = [
    # 2026
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
    # 2027 placeholder — populate when Fed publishes
]

# BLS CPI release dates, published annually.
# Source: https://www.bls.gov/schedule/news_release/cpi.htm
CPI_DATES = [
    # 2026
    "2026-01-14", "2026-02-12", "2026-03-12", "2026-04-10",
    "2026-05-13", "2026-06-11", "2026-07-15", "2026-08-12",
    "2026-09-11", "2026-10-15", "2026-11-13", "2026-12-10",
]


def upcoming_macro_events(days_horizon=2):
    """Events scheduled in [today, today+horizon].
    Each entry: {type: 'FOMC'|'CPI', date: 'YYYY-MM-DD', days_away: int}."""
    today = date.today()
    horizon = today + timedelta(days=days_horizon)
    out = []
    for d in FOMC_DATES:
        try:
            ed = date.fromisoformat(d)
        except ValueError:
            continue
        if today <= ed <= horizon:
            out.append({"type": "FOMC", "date": d, "days_away": (ed - today).days})
    for d in CPI_DATES:
        try:
            ed = date.fromisoformat(d)
        except ValueError:
            continue
        if today <= ed <= horizon:
            out.append({"type": "CPI", "date": d, "days_away": (ed - today).days})
    return sorted(out, key=lambda x: x["days_away"])


def _load_vix_cache():
    try:
        with open(VIX_CACHE_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_vix_cache(payload):
    try:
        os.makedirs(os.path.dirname(VIX_CACHE_PATH), exist_ok=True)
        with open(VIX_CACHE_PATH, "w") as f:
            json.dump(payload, f)
    except Exception:
        pass


def get_vix():
    """Current VIX level via Yahoo Finance unofficial endpoint. Returns float or None.
    Cached 6h on disk to avoid quote-call spam.
    Yahoo's free chart endpoint returns the index value without an API key — Finnhub's
    free tier rejects ^VIX with 'Market data subscription required for CFD indices.'"""
    cache = _load_vix_cache()
    now_ts = datetime.utcnow().timestamp()
    if cache and (now_ts - cache.get("cached_ts", 0)) < VIX_CACHE_TTL_SECS:
        v = cache.get("vix")
        if v is not None:
            return v

    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX",
            params={"interval": "1d", "range": "1d"},
            headers={"User-Agent": "Mozilla/5.0 (compatible; InvestAI/1.0)"},
            timeout=5,
        )
        meta = (r.json().get("chart", {}).get("result") or [{}])[0].get("meta") or {}
        vix = float(meta["regularMarketPrice"]) if meta.get("regularMarketPrice") else None
    except Exception:
        vix = None

    _save_vix_cache({"vix": vix, "cached_ts": now_ts})
    return vix


def check_macro_risk():
    """Summary of macro state right now. Returns:
       {events: [...], vix: float|None, severity: 'crisis'|'elevated'|'event'|'clear'}.
       severity is highest-precedence: crisis > elevated > event > clear."""
    events = upcoming_macro_events(days_horizon=2)
    vix = get_vix()

    if vix is not None and vix > 40:
        severity = "crisis"
    elif vix is not None and vix > 30:
        severity = "elevated"
    elif events:
        severity = "event"
    else:
        severity = "clear"

    return {"events": events, "vix": vix, "severity": severity}
