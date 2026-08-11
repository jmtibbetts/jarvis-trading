"""
FINRA Off-Exchange (ATS/Dark Pool) Transparency data — free, unauthenticated
Query API (api.finra.org). No vendor, no API key.

This is FINRA's own Regulation ATS transparency dataset: weekly aggregate
share volume, trade count, and notional value that each Alternative Trading
System (dark pool) reported for each NMS symbol. Verified live against the
real API while building this — including its actual reporting cadence:

  IMPORTANT — this is delayed, aggregated data, not real-time order flow:
  NMS Tier 1 (S&P 500 / Russell 1000): published ~2 weeks after the trading
  week ends. Tier 2 / OTC: ~4 weeks. There is no free source for individual
  real-time dark-pool prints — that requires a paid vendor (e.g. a direct
  ATS data feed). Never present this as live/real-time activity.

Query mechanics (verified live, not documented anywhere obvious):
  - POST https://api.finra.org/data/group/otcMarket/name/weeklySummary
  - weekStartDate and tierIdentifier are partition keys — sorting requires
    both as EQUAL filters, so this client discovers the latest populated
    week by walking backward from the current Monday instead of sorting.
  - A query matching zero rows returns HTTP 204 (empty body), not a 200
    with an empty JSON array — must be handled explicitly or `.json()`
    raises on the empty body.
  - summaryTypeCode selects the aggregation level:
      ATS_W_SMBL      — one row per symbol, all ATS venues combined
      ATS_W_SMBL_FIRM — one row per symbol PER VENUE (MPID/marketParticipantName)
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://api.finra.org/data/group/otcMarket/name/weeklySummary"
HTTP_TIMEOUT = 15.0
USER_AGENT = "Jarvis Trading AI admin@jarvis-trading.local"
CACHE_TTL_SECONDS = 12 * 3600  # this dataset only updates weekly

_top_activity_cache: dict = {}
_top_activity_cache_time: float = 0.0


def _monday_on_or_before(d) -> "datetime.date":
    return d - timedelta(days=d.weekday())


def _post(body: dict, client: httpx.Client | None = None) -> list[dict]:
    headers = {"User-Agent": USER_AGENT, "Content-Type": "application/json", "Accept": "application/json"}
    owns_client = client is None
    c = client or httpx.Client(timeout=HTTP_TIMEOUT)
    try:
        resp = c.post(BASE_URL, json=body, headers=headers)
        if resp.status_code == 204:
            return []  # FINRA's "zero matching rows" response — no body, not an error
        resp.raise_for_status()
        if not resp.content:
            return []
        data = resp.json()
        return data if isinstance(data, list) else []
    except httpx.HTTPError as e:
        logger.warning(f"[FINRA ATS] Request failed: {e}")
        return []
    except ValueError as e:
        logger.warning(f"[FINRA ATS] Malformed response: {e}")
        return []
    finally:
        if owns_client:
            c.close()


def discover_latest_week(tier: str = "T1", max_weeks_back: int = 8, client: httpx.Client | None = None) -> str | None:
    """Walk backward from this Monday until a populated week is found.
    Necessary because the publish delay varies (holidays, revisions) and
    FINRA has no "latest available partition" discovery endpoint."""
    this_monday = _monday_on_or_before(datetime.now(timezone.utc).date())
    for weeks_back in range(1, max_weeks_back + 1):
        candidate = (this_monday - timedelta(weeks=weeks_back)).isoformat()
        rows = _post({
            "compareFilters": [
                {"compareType": "equal", "fieldName": "summaryTypeCode", "fieldValue": "ATS_W_SMBL"},
                {"compareType": "equal", "fieldName": "weekStartDate", "fieldValue": candidate},
                {"compareType": "equal", "fieldName": "tierIdentifier", "fieldValue": tier},
            ],
            "limit": 1,
        }, client)
        if rows:
            return candidate
    return None


def fetch_week_symbols(week_start: str, tier: str = "T1", limit: int = 600, client: httpx.Client | None = None) -> list[dict]:
    """All symbols' ATS-combined activity for one week (ATS_W_SMBL)."""
    return _post({
        "compareFilters": [
            {"compareType": "equal", "fieldName": "summaryTypeCode", "fieldValue": "ATS_W_SMBL"},
            {"compareType": "equal", "fieldName": "weekStartDate", "fieldValue": week_start},
            {"compareType": "equal", "fieldName": "tierIdentifier", "fieldValue": tier},
        ],
        "limit": limit,
    }, client)


def fetch_symbol_venues(symbol: str, week_start: str, client: httpx.Client | None = None) -> list[dict]:
    """Per-venue (dark pool) breakdown for one symbol in one week (ATS_W_SMBL_FIRM)."""
    rows = _post({
        "compareFilters": [
            {"compareType": "equal", "fieldName": "issueSymbolIdentifier", "fieldValue": symbol.upper()},
            {"compareType": "equal", "fieldName": "summaryTypeCode", "fieldValue": "ATS_W_SMBL_FIRM"},
            {"compareType": "equal", "fieldName": "weekStartDate", "fieldValue": week_start},
        ],
        "limit": 100,
    }, client)
    return sorted(rows, key=lambda r: -(r.get("totalWeeklyShareQuantity") or 0))


def _reporting_delay_days(tier: str, week_start: str, initial_published: str | None) -> int | None:
    if not initial_published:
        return None
    try:
        week_date = datetime.strptime(week_start, "%Y-%m-%d").date()
        published_date = datetime.strptime(initial_published, "%Y-%m-%d").date()
        return (published_date - week_date).days
    except ValueError:
        return None


def get_top_activity(tier: str = "T1", limit: int = 25, force_refresh: bool = False) -> dict | None:
    """Top-N symbols by raw ATS share volume for the latest available week,
    each with week-over-week % change computed from the prior populated
    week (two bulk fetches, not a full historical trend — cheap and honest
    about what it actually measures: this week vs last week, not a
    statistical baseline)."""
    global _top_activity_cache, _top_activity_cache_time
    cache_key = f"{tier}:{limit}"
    now = time.time()
    if not force_refresh and _top_activity_cache.get("key") == cache_key and (now - _top_activity_cache_time) < CACHE_TTL_SECONDS:
        return _top_activity_cache["data"]

    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        latest_week = discover_latest_week(tier, client=client)
        if not latest_week:
            return None
        latest_rows = fetch_week_symbols(latest_week, tier, client=client)
        if not latest_rows:
            return None

        prior_monday = (datetime.strptime(latest_week, "%Y-%m-%d").date() - timedelta(weeks=1)).isoformat()
        prior_rows = fetch_week_symbols(prior_monday, tier, client=client)
        prior_by_symbol = {r["issueSymbolIdentifier"]: r for r in prior_rows if r.get("issueSymbolIdentifier")}

        ranked = sorted(latest_rows, key=lambda r: -(r.get("totalWeeklyShareQuantity") or 0))[:limit]
        results = []
        for r in ranked:
            symbol = r.get("issueSymbolIdentifier")
            prior = prior_by_symbol.get(symbol)
            wow_pct = None
            if prior and prior.get("totalWeeklyShareQuantity"):
                wow_pct = round(
                    (r["totalWeeklyShareQuantity"] - prior["totalWeeklyShareQuantity"]) / prior["totalWeeklyShareQuantity"] * 100, 1
                )
            results.append({
                "symbol": symbol,
                "issuer_name": r.get("issueName"),
                "shares": r.get("totalWeeklyShareQuantity"),
                "trade_count": r.get("totalWeeklyTradeCount"),
                "notional": r.get("totalNotionalSum"),
                "wow_pct": wow_pct,
                "week_start": latest_week,
                "published_at": r.get("initialPublishedDate"),
                "reporting_delay_days": _reporting_delay_days(tier, latest_week, r.get("initialPublishedDate")),
            })

    snapshot = {"tier": tier, "week_start": latest_week, "symbols": results, "fetched_at": datetime.now(timezone.utc).isoformat()}
    _top_activity_cache = {"key": cache_key, "data": snapshot}
    _top_activity_cache_time = now
    return snapshot


def get_symbol_venues(symbol: str, week_start: str | None = None) -> dict | None:
    """Per-venue breakdown for one symbol. Defaults to the latest discovered
    T1 week if week_start isn't given (a caller drilling into a row from
    get_top_activity should already have the week_start and pass it through
    to avoid a redundant discovery call)."""
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        week = week_start or discover_latest_week(client=client)
        if not week:
            return None
        venues = fetch_symbol_venues(symbol, week, client=client)
    return {
        "symbol": symbol.upper(),
        "week_start": week,
        "venues": [
            {
                "mpid": v.get("MPID"),
                "name": v.get("marketParticipantName"),
                "shares": v.get("totalWeeklyShareQuantity"),
                "trade_count": v.get("totalWeeklyTradeCount"),
                "notional": v.get("totalNotionalSum"),
            }
            for v in venues
        ],
    }
