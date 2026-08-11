"""
US Treasury daily yield curve — free, unauthenticated, no API key.

Uses the Treasury's own public CSV export (verified live while building
this), not a paid data vendor:

  https://home.treasury.gov/resource-center/data-chart-center/interest-rates/
    daily-treasury-rates.csv/{year}/all?type=daily_treasury_yield_curve
    &field_tdr_date_value={year}&page&_format=csv

Only "_format=csv" is supported by this endpoint (JSON returns 406) — the
csv stdlib module handles parsing, no extra dependency needed.

The 2s10s and 3m10y spreads are the two classic yield-curve-inversion
recession indicators (Fed research generally treats 3m10y as the more
reliable of the two, but both are reported since they're both widely
watched). A negative spread means the curve is inverted for that pair.
"""
from __future__ import annotations

import csv
import io
import logging
import time
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv"
HTTP_TIMEOUT = 15.0
CACHE_TTL_SECONDS = 6 * 3600  # yields update once/business day — no need to refetch often

# Column name -> canonical tenor key used in the returned dicts.
TENOR_COLUMNS = {
    "1 Mo": "1mo", "1.5 Month": "1.5mo", "2 Mo": "2mo", "3 Mo": "3mo",
    "4 Mo": "4mo", "6 Mo": "6mo", "1 Yr": "1yr", "2 Yr": "2yr", "3 Yr": "3yr",
    "5 Yr": "5yr", "7 Yr": "7yr", "10 Yr": "10yr", "20 Yr": "20yr", "30 Yr": "30yr",
}

_cache: dict = {}
_cache_time: float = 0.0


def _parse_csv(text: str) -> list[dict]:
    """Parse the Treasury CSV into [{date, "2yr": 4.25, "10yr": 4.72, ...}, ...],
    newest first (matches the source's own ordering)."""
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for raw in reader:
        date_str = raw.get("Date")
        if not date_str:
            continue
        try:
            date_iso = datetime.strptime(date_str, "%m/%d/%Y").date().isoformat()
        except ValueError:
            continue
        row = {"date": date_iso}
        for col, key in TENOR_COLUMNS.items():
            raw_val = raw.get(col)
            try:
                row[key] = float(raw_val) if raw_val not in (None, "", "N/A") else None
            except ValueError:
                row[key] = None
        rows.append(row)
    return rows


def fetch_yield_curve(year: int | None = None) -> list[dict]:
    """Fetch the full daily yield curve history for one calendar year."""
    year = year or datetime.now(timezone.utc).year
    url = f"{BASE_URL}/{year}/all?type=daily_treasury_yield_curve&field_tdr_date_value={year}&page&_format=csv"
    try:
        resp = httpx.get(url, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        logger.warning(f"[Treasury] Yield curve fetch failed: {e}")
        return []
    return _parse_csv(resp.text)


def compute_spreads(curve_row: dict) -> dict:
    """2s10s and 3m10y spreads (in percentage points) from one day's curve row."""
    ten = curve_row.get("10yr")
    two = curve_row.get("2yr")
    three_mo = curve_row.get("3mo")
    spread_2s10s = round(ten - two, 3) if ten is not None and two is not None else None
    spread_3m10y = round(ten - three_mo, 3) if ten is not None and three_mo is not None else None
    return {
        "spread_2s10s": spread_2s10s,
        "spread_3m10y": spread_3m10y,
        "2s10s_inverted": spread_2s10s is not None and spread_2s10s < 0,
        "3m10y_inverted": spread_3m10y is not None and spread_3m10y < 0,
    }


def get_yield_curve_snapshot(force_refresh: bool = False) -> dict | None:
    """Cached snapshot: latest day's full curve + spreads + a short trend of
    2yr/10yr/spread over the last 20 business days, for a sparkline."""
    global _cache, _cache_time
    now = time.time()
    if not force_refresh and _cache and (now - _cache_time) < CACHE_TTL_SECONDS:
        return _cache

    rows = fetch_yield_curve()
    if not rows:
        # Year boundary edge case: early January has no rows yet for the new
        # year until Treasury publishes the first business day — fall back
        # to the previous year's file, which still has the latest rows since
        # Treasury's "current year" file only starts populating after Jan 1.
        rows = fetch_yield_curve(datetime.now(timezone.utc).year - 1)
    if not rows:
        return None

    latest = rows[0]
    trend = [
        {"date": r["date"], "2yr": r.get("2yr"), "10yr": r.get("10yr"), **compute_spreads(r)}
        for r in rows[:20]
    ]
    snapshot = {
        "latest": {**latest, **compute_spreads(latest)},
        "trend": trend,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache = snapshot
    _cache_time = now
    return snapshot
