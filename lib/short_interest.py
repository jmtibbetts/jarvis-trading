"""
FINRA Consolidated Short Interest — free, unauthenticated Query API
(api.finra.org), same host and POST mechanics as lib/finra_ats.py. No
vendor, no API key.

Verified live against the real API while building this:

  IMPORTANT — this is SEMI-MONTHLY, DELAYED data, not a live short book:
  FINRA collects short interest as of two settlement dates per month (the
  15th and month-end, rolled back to a business day) and publishes each
  roughly 8 business days later. A position reported for 2026-07-15 is
  already weeks old by the time it's queryable. Never present it as the
  current short position — always surface settlement_date alongside.

  Real response fields (confirmed, AAPL @ 2026-07-15):
    currentShortPositionQuantity   146,547,784
    previousShortPositionQuantity  140,526,320
    averageDailyVolumeQuantity      47,952,794
    daysToCoverQuantity                   3.06
    changePercent                         4.28
    settlementDate / symbolCode / issueName / marketClassCode

What this deliberately does NOT compute: short interest as a percentage of
float. FINRA's dataset has no shares-outstanding or float figure, and there
is no free field here to derive one from — so that classic squeeze metric
is genuinely unavailable rather than approximated. Days-to-cover (short
position / average daily volume) IS supplied by FINRA directly and is used
instead. See compute_squeeze_score for what the score is and isn't.
"""
from __future__ import annotations

import calendar
import logging
import time
from datetime import date, datetime, timedelta, timezone

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest"
HTTP_TIMEOUT = 20.0
USER_AGENT = "Jarvis Trading AI admin@jarvis-trading.local"
CACHE_TTL_SECONDS = 6 * 3600  # semi-monthly dataset; no value in polling harder

_latest_date_cache: dict = {}
_latest_date_cache_time: float = 0.0


def _post(body: dict, client: httpx.Client | None = None) -> list[dict]:
    """Mirrors lib/finra_ats._post, including FINRA's HTTP 204 ("zero matching
    rows", empty body) behavior — calling .json() on that raises."""
    headers = {"User-Agent": USER_AGENT, "Content-Type": "application/json", "Accept": "application/json"}
    owns_client = client is None
    c = client or httpx.Client(timeout=HTTP_TIMEOUT)
    try:
        resp = c.post(BASE_URL, json=body, headers=headers)
        if resp.status_code == 204 or not resp.content:
            return []
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []
    except httpx.HTTPError as e:
        logger.warning(f"[ShortInterest] Request failed: {e}")
        return []
    except ValueError as e:
        logger.warning(f"[ShortInterest] Malformed response: {e}")
        return []
    finally:
        if owns_client:
            c.close()


def _to_business_day(d: date) -> date:
    """Roll a weekend date back to the preceding Friday. FINRA settlement
    dates fall on business days; this does NOT account for market holidays,
    which is why callers probe several candidates rather than trusting one."""
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def candidate_settlement_dates(today: date | None = None, months_back: int = 4) -> list[str]:
    """Semi-monthly FINRA settlement dates (15th and month-end, rolled back
    off weekends), newest first, starting from the current month."""
    today = today or datetime.now(timezone.utc).date()
    out: list[str] = []
    year, month = today.year, today.month
    for _ in range(months_back):
        last_day = date(year, month, calendar.monthrange(year, month)[1])
        for candidate in (_to_business_day(last_day), _to_business_day(date(year, month, 15))):
            if candidate <= today:
                out.append(candidate.isoformat())
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return out


def discover_latest_settlement_date(client: httpx.Client | None = None, force_refresh: bool = False) -> str | None:
    """First candidate date that actually has published rows. Probing is
    necessary because the publish lag varies and FINRA exposes no
    'latest available partition' endpoint (same constraint as the ATS
    dataset — see lib/finra_ats.discover_latest_week)."""
    global _latest_date_cache, _latest_date_cache_time
    now = time.time()
    if not force_refresh and _latest_date_cache.get("date") and (now - _latest_date_cache_time) < CACHE_TTL_SECONDS:
        return _latest_date_cache["date"]

    for candidate in candidate_settlement_dates():
        rows = _post({
            "compareFilters": [{"compareType": "equal", "fieldName": "settlementDate", "fieldValue": candidate}],
            "limit": 1,
        }, client)
        if rows:
            _latest_date_cache = {"date": candidate}
            _latest_date_cache_time = now
            return candidate
    return None


def _row_to_dict(row: dict) -> dict:
    return {
        "symbol": row.get("symbolCode"),
        "issue_name": row.get("issueName"),
        "settlement_date": row.get("settlementDate"),
        "current_short_shares": row.get("currentShortPositionQuantity"),
        "previous_short_shares": row.get("previousShortPositionQuantity"),
        "change_shares": row.get("changePreviousNumber"),
        "change_percent": row.get("changePercent"),
        "avg_daily_volume": row.get("averageDailyVolumeQuantity"),
        "days_to_cover": row.get("daysToCoverQuantity"),
        "market_class": row.get("marketClassCode"),
    }


def fetch_symbol_short_interest(symbol: str, settlement_date: str | None = None) -> dict | None:
    """Latest published short interest for one symbol."""
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        settlement = settlement_date or discover_latest_settlement_date(client)
        if not settlement:
            return None
        rows = _post({
            "compareFilters": [
                {"compareType": "equal", "fieldName": "settlementDate", "fieldValue": settlement},
                {"compareType": "equal", "fieldName": "symbolCode", "fieldValue": symbol.upper()},
            ],
            "limit": 1,
        }, client)
    if not rows:
        return None
    result = _row_to_dict(rows[0])
    result["squeeze"] = compute_squeeze_score(result)
    result["reporting_lag_days"] = _reporting_lag_days(result["settlement_date"])
    return result


def _reporting_lag_days(settlement_date: str | None) -> int | None:
    if not settlement_date:
        return None
    try:
        settled = datetime.strptime(settlement_date, "%Y-%m-%d").date()
    except ValueError:
        return None
    return (datetime.now(timezone.utc).date() - settled).days


def compute_squeeze_score(row: dict) -> dict:
    """SHORT_SQUEEZE_SCORE (0-100) from the two things FINRA actually
    supplies: days-to-cover (how many normal sessions of volume it would
    take shorts to exit — the crowding/exit-difficulty measure) and the
    semi-monthly change in short position (whether shorts are still
    building).

    This is a measure of squeeze FUEL, not a prediction that a squeeze will
    happen — a squeeze also needs a catalyst and buying pressure, which
    this dataset cannot see. Short-interest-as-%-of-float, the other classic
    input, is unavailable here (see module docstring) and is deliberately
    not approximated. Components are returned so the score is never an
    unexplained number."""
    dtc = row.get("days_to_cover")
    change_pct = row.get("change_percent")

    # Days to cover, scaled across 1 -> 20 sessions. An earlier 1 -> 10 scale
    # saturated: real crowded names routinely exceed 10, so everything above
    # it collapsed to an indistinguishable 100.
    if dtc is None:
        dtc_component = None
    else:
        dtc_component = round(max(0.0, min(100.0, (float(dtc) - 1) / 19 * 100)), 1)

    # Rising short interest adds fuel; falling (covering already underway)
    # subtracts. Scaled across +/-100%, since semi-monthly swings of 50%+ are
    # common and a tighter cap flattened the distribution.
    if change_pct is None:
        change_component = None
    else:
        change_component = round(max(-100.0, min(100.0, float(change_pct))), 1)

    # Days-to-cover is the structural crowding measure and carries most of
    # the weight; the period-over-period change is a secondary confirmation
    # of whether shorts are still adding.
    if dtc_component is None and change_component is None:
        score = None
    elif dtc_component is None:
        score = round((change_component + 100) / 2, 1)
    elif change_component is None:
        score = dtc_component
    else:
        score = round(dtc_component * 0.7 + ((change_component + 100) / 2) * 0.3, 1)

    return {
        "squeeze_score": score,
        "days_to_cover_component": dtc_component,
        "short_change_component": change_component,
        "short_interest_pct_of_float": None,
        "float_note": "short interest as % of float is unavailable - FINRA's dataset has no shares-outstanding figure",
        "interpretation": "measures squeeze fuel (crowding + short buildup), not a prediction that a squeeze will occur",
    }


# ETFs, closed-end funds and SPAC units dominate a naive short-interest
# ranking but don't squeeze the way an ordinary share does: ETF shares are
# created and redeemed on demand by authorized participants, so a large short
# position carries no trapped-shorts dynamic (it's usually hedging or
# market-making inventory). FINRA publishes no security-type field, so this
# is a NAME HEURISTIC over issueName - it is imperfect by construction, which
# is why it's a toggle (exclude_funds) and why the excluded count is reported
# rather than hidden.
_FUND_NAME_MARKERS = (
    # generic vehicle words
    " etf", "etf ", " etn", "etn ", " fund", "fund ", " trust", "trust ",
    "index tr", " portfolio", " acquisition", " depositary",
    # share classes that aren't ordinary common stock
    " warrant", " unit", " right", " preferred", " notes due",
    # major ETF/ETN issuers whose product names often omit "ETF" entirely
    "ishares", "spdr", "proshares", "vaneck", "invesco", "direxion", "wisdomtree",
    "global x", "allianzim", "calamos", "ipath", "franklin", "janus henderson",
    "federated hermes", "amplify", "first trust", "pacer", "roundhill", "defiance",
    "simplify", "innovator", "xtrackers", "dimensional", "avantis", "american century",
    "ft vest", "ft cboe", "alliancebernstein", "neos ", "tidal ",
    "thrivent", "allspring", "schwab strategic", "vanguard", "fidelity covington",
    "goldman sachs etf", "jpmorgan etf", "t. rowe price exchange",
)


# Nasdaq's 5-character ticker convention: a 5th-letter W (warrant), U (unit)
# or R (rights) marks a non-ordinary share class. This matters because FINRA
# TRUNCATES issueName to exactly 30 characters (verified live: "Guardforce AI
# Co., Limited War", "Kensington Capital Acquisition") — the very words that
# identify these instruments are usually cut off, so name matching alone
# cannot catch them. The ticker suffix survives truncation.
_NON_ORDINARY_TICKER_SUFFIXES = ("W", "WS", "WW", "U", "R", "RT")


def looks_like_fund_or_spac(issue_name: str | None, symbol: str | None = None) -> bool:
    """Heuristic: does this look like a fund/ETN/SPAC/warrant/unit rather
    than ordinary common stock? Imperfect by construction — FINRA publishes
    no security-type field and truncates issueName to 30 chars — so this is
    a toggle, and what it removes is always reported in `excluded`."""
    if symbol and len(symbol) == 5:
        for suffix in _NON_ORDINARY_TICKER_SUFFIXES:
            if symbol.upper().endswith(suffix):
                return True
    if not issue_name:
        return False
    name = f" {issue_name.lower()} "
    return any(marker in name for marker in _FUND_NAME_MARKERS)


# Real exchange listings only. OTC is excluded deliberately: it's ~43% of
# FINRA's rows and is dominated by illiquid foreign ordinaries whose tiny
# average volume produces enormous, meaningless days-to-cover ratios — they
# would swamp any squeeze ranking while being untradeable in practice.
EXCHANGE_CLASS_CODES = ("NNM", "NYSE", "ARCA", "AMEX", "SC", "BZX")

# FINRA caps daysToCoverQuantity at exactly 999.99 as a sentinel for
# "effectively infinite" (verified live: 853 rows carry that exact value at
# one settlement date). It is not a measurement and must never be scored.
DAYS_TO_COVER_SENTINEL = 999.99

# Above this many sessions-to-unwind the name is an illiquid stub rather
# than a tradeable squeeze setup, regardless of what the ratio says.
MAX_CREDIBLE_DAYS_TO_COVER = 60.0

_PAGE_SIZE = 5000
_MAX_PAGES_PER_EXCHANGE = 3


def fetch_exchange_universe(settlement: str, client: httpx.Client | None = None) -> list[dict]:
    """All exchange-listed rows for a settlement date, paginated per exchange.
    FINRA's endpoint returns rows in symbol-alphabetical order and caps a
    single response at a few thousand rows, so an unpaginated fetch silently
    returns only the A-names — verified live while building this."""
    rows: list[dict] = []
    for exchange in EXCHANGE_CLASS_CODES:
        for page in range(_MAX_PAGES_PER_EXCHANGE):
            body = {
                "compareFilters": [
                    {"compareType": "equal", "fieldName": "settlementDate", "fieldValue": settlement},
                    {"compareType": "equal", "fieldName": "marketClassCode", "fieldValue": exchange},
                ],
                "limit": _PAGE_SIZE,
            }
            if page:
                body["offset"] = page * _PAGE_SIZE
            batch = _post(body, client)
            rows.extend(batch)
            if len(batch) < _PAGE_SIZE:
                break
        else:
            logger.warning(
                f"[ShortInterest] {exchange} hit the {_MAX_PAGES_PER_EXCHANGE}-page ceiling "
                f"({_MAX_PAGES_PER_EXCHANGE * _PAGE_SIZE} rows); ranking may omit later symbols"
            )
    return rows


def get_top_squeeze_candidates(limit: int = 25, min_days_to_cover: float = 3.0,
                                exclude_funds: bool = True) -> dict | None:
    """Highest-squeeze-fuel exchange-listed symbols for the latest published
    settlement date. Excluded counts are reported rather than silently
    dropped, so the caller can see what the ranking did and didn't consider."""
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        settlement = discover_latest_settlement_date(client)
        if not settlement:
            return None
        raw_rows = fetch_exchange_universe(settlement, client)
    if not raw_rows:
        return None

    excluded = {
        "sentinel_days_to_cover": 0, "implausible_days_to_cover": 0,
        "below_min_days_to_cover": 0, "no_short_position": 0, "funds_and_spacs": 0,
    }
    scored = []
    for raw in raw_rows:
        row = _row_to_dict(raw)
        dtc = row.get("days_to_cover")
        if not row.get("current_short_shares") or dtc is None:
            excluded["no_short_position"] += 1
            continue
        dtc = float(dtc)
        if dtc == DAYS_TO_COVER_SENTINEL:
            excluded["sentinel_days_to_cover"] += 1
            continue
        if dtc > MAX_CREDIBLE_DAYS_TO_COVER:
            excluded["implausible_days_to_cover"] += 1
            continue
        if dtc < min_days_to_cover:
            excluded["below_min_days_to_cover"] += 1
            continue
        if exclude_funds and looks_like_fund_or_spac(row.get("issue_name"), row.get("symbol")):
            excluded["funds_and_spacs"] += 1
            continue
        row["squeeze"] = compute_squeeze_score(row)
        if row["squeeze"]["squeeze_score"] is not None:
            scored.append(row)

    scored.sort(key=lambda r: -r["squeeze"]["squeeze_score"])
    return {
        "settlement_date": settlement,
        "reporting_lag_days": _reporting_lag_days(settlement),
        "candidates": scored[:max(1, min(limit, 100))],
        "universe_size": len(raw_rows),
        "qualified_count": len(scored),
        "excluded": excluded,
        "exchanges_included": list(EXCHANGE_CLASS_CODES),
        "funds_excluded": exclude_funds,
        "fund_filter_note": "ETF/fund/SPAC exclusion is a name heuristic over issueName - FINRA publishes no security-type field",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
