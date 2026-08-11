"""
FRED (Federal Reserve Economic Data, St. Louis Fed) client — free API, but
unlike SEC EDGAR and Treasury.gov it DOES require a personal API key
(instant, free signup: https://fred.stlouisfed.org/docs/api/api_key.html).
Set FRED_API_KEY in .env. Every function here degrades to returning None/[]
when the key is missing rather than raising, the same pattern already used
for optional-key crypto providers (lib/crypto_market_data.py).

Series IDs are FRED's own permanent identifiers — these are stable,
well-documented government data series, not something that changes:
  CPIAUCSL   CPI, all urban consumers (index, monthly)
  CPILFESL   Core CPI, ex food & energy (index, monthly)
  PCEPI      PCE price index (index, monthly)
  PCEPILFE   Core PCE, ex food & energy (index, monthly)
  UNRATE     Unemployment rate (%, monthly)
  PAYEMS     Total nonfarm payrolls (thousands of persons, monthly)
  GDPC1      Real GDP (billions, chained dollars, quarterly)
  FEDFUNDS   Effective federal funds rate (%, monthly)
  ICSA       Initial jobless claims (weekly)

CPI/PCE are index levels, not rates — "inflation" always means the
year-over-year change in the index, not the index value itself, so those
series report a computed YoY %, while UNRATE/FEDFUNDS (already rates) and
PAYEMS (reported as month-over-month change, the standard convention)
use their own natural units instead. Getting this transformation wrong
would misrepresent real economic data, so each series' treatment mirrors
how it's conventionally reported (e.g. "CPI rose 3.1% YoY"), not a
one-size-fits-all "latest value."
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://api.stlouisfed.org/fred/series/observations"
HTTP_TIMEOUT = 15.0
CACHE_TTL_SECONDS = 6 * 3600  # most of these series update monthly/quarterly at most

FRED_API_KEY = os.getenv("FRED_API_KEY", "")

# key -> (series_id, unit_label, treatment)
# treatment: "yoy_pct" (index -> year-over-year %), "level" (already a rate/%),
# "mom_change" (report the period-over-period change, not the raw level)
SERIES_CONFIG = {
    "cpi":               ("CPIAUCSL", "% YoY", "yoy_pct"),
    "core_cpi":          ("CPILFESL", "% YoY", "yoy_pct"),
    "pce":               ("PCEPI", "% YoY", "yoy_pct"),
    "core_pce":          ("PCEPILFE", "% YoY", "yoy_pct"),
    "unemployment_rate": ("UNRATE", "%", "level"),
    "fed_funds_rate":    ("FEDFUNDS", "%", "level"),
    "nonfarm_payrolls":  ("PAYEMS", "k jobs, MoM", "mom_change"),
    "real_gdp":          ("GDPC1", "% YoY", "yoy_pct"),
    "jobless_claims":    ("ICSA", "claims", "level"),
}

_cache: dict = {}
_cache_time: float = 0.0


def is_configured() -> bool:
    return bool(FRED_API_KEY)


def fetch_series(series_id: str, limit: int = 26) -> list[dict]:
    """Most recent `limit` observations, newest first. FRED represents a
    missing/not-yet-released data point as value="." — filtered out here."""
    if not FRED_API_KEY:
        return []
    params = {
        "series_id": series_id, "api_key": FRED_API_KEY, "file_type": "json",
        "sort_order": "desc", "limit": limit,
    }
    try:
        resp = httpx.get(BASE_URL, params=params, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        logger.warning(f"[FRED] Fetch failed for {series_id}: {e}")
        return []

    rows = []
    for obs in data.get("observations", []):
        raw_value = obs.get("value")
        if raw_value in (None, ".", ""):
            continue
        try:
            rows.append({"date": obs["date"], "value": float(raw_value)})
        except (KeyError, ValueError):
            continue
    return rows


def _shift_months(date_str: str, months_back: int) -> str:
    year, month, day = (int(p) for p in date_str.split("-"))
    total = (year * 12 + (month - 1)) - months_back
    new_year, new_month = divmod(total, 12)
    return f"{new_year:04d}-{new_month + 1:02d}-{day:02d}"


def _yoy_pct(rows: list[dict], periods_back: int = 12) -> dict | None:
    """rows must be newest-first. Looks up the comparison point by calendar
    date (latest date minus 12 months), not by list position — FRED
    occasionally has a missing/revised-away month, and a positional offset
    of 12 silently drifts to 11-or-13-months-back once any row upstream of
    it was filtered out, which is exactly wrong for a YoY calculation."""
    if not rows:
        return None
    latest = rows[0]
    target_date = _shift_months(latest["date"], periods_back)
    match = next((r for r in rows if r["date"] == target_date), None)
    if not match or not match["value"]:
        return None
    pct = (latest["value"] - match["value"]) / abs(match["value"]) * 100
    return {"date": latest["date"], "value": round(pct, 2), "compared_to": match["date"]}


def _mom_change(rows: list[dict]) -> dict | None:
    if len(rows) < 2:
        return None
    latest, prior = rows[0], rows[1]
    return {"date": latest["date"], "value": round(latest["value"] - prior["value"], 1), "compared_to": prior["date"]}


def _level(rows: list[dict]) -> dict | None:
    if not rows:
        return None
    return {"date": rows[0]["date"], "value": rows[0]["value"], "compared_to": None}


def get_macro_snapshot(force_refresh: bool = False) -> dict | None:
    """Latest reading for every series in SERIES_CONFIG, each transformed
    the conventional way for that series type. Returns None if no API key
    is configured (not an error — this module is opt-in)."""
    global _cache, _cache_time
    if not FRED_API_KEY:
        return None

    now = time.time()
    if not force_refresh and _cache and (now - _cache_time) < CACHE_TTL_SECONDS:
        return _cache

    readings = {}
    any_success = False
    for key, (series_id, unit, treatment) in SERIES_CONFIG.items():
        rows = fetch_series(series_id, limit=30 if treatment.startswith("yoy") else 6)
        if not rows:
            readings[key] = None
            continue
        if treatment == "yoy_pct":
            result = _yoy_pct(rows, periods_back=12)
        elif treatment == "mom_change":
            result = _mom_change(rows)
        else:
            result = _level(rows)
        if result:
            result["unit"] = unit
            result["series_id"] = series_id
            any_success = True
        readings[key] = result

    if not any_success:
        return None

    snapshot = {"readings": readings, "fetched_at": datetime.now(timezone.utc).isoformat()}
    _cache = snapshot
    _cache_time = now
    return snapshot
