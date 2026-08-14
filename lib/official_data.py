"""Official differentiated data — CFTC COT, FINRA short volume, EIA.

Phase 4A of the hardening plan, staged to what the book actually trades.
Everything here is SHADOW-ONLY: stats land in the raw event store as
OfficialStat events and influence nothing until an OOS ablation earns
them a place. Three disciplines carried by every row:

**as_of vs release.** A COT report describes Tuesday but becomes public
Friday 15:30 ET. `as_of` is the Tuesday; meta.exchange_ts is the Friday.
A replay that joins on as_of has a three-day crystal ball — the join key
is ALWAYS the release time. (Release times are the published schedule,
not per-row API data; the convention is documented per source below.)

**Idempotent sync.** Officials publish weekly/daily; jobs poll more often.
The dedup_key (source:series:symbol:as_of) makes any overlap one row at
the storage layer — no sync bookkeeping to corrupt.

**Live-verified shapes.** Every dataset id, contract code and file format
below was verified against the real endpoint on 2026-08-14, including
that CME lists BITCOIN (133741) and ETHER (146021) — institutional
positioning for the two instruments this desk trades most.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

import httpx

logger = logging.getLogger(__name__)

# ── CFTC COT (legacy futures-only, Socrata, keyless) ─────────────────────────
COT_URL = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"

# Contract market code -> the desk's instrument identity. Verified live.
COT_MARKETS = {
    "088691": "GC=F",     # GOLD - COMMODITY EXCHANGE INC.
    "084691": "SI=F",     # SILVER - COMMODITY EXCHANGE INC.
    "085692": "HG=F",     # COPPER- #1 - COMMODITY EXCHANGE INC.
    "067651": "CL=F",     # WTI-PHYSICAL - NYMEX
    "023651": "NG=F",     # NAT GAS NYME - NYMEX
    "13874A": "ES=F",     # E-MINI S&P 500 - CME
    "209742": "NQ=F",     # NASDAQ MINI - CME
    "133741": "BTC/USD",  # BITCOIN - CME (institutional positioning proxy)
    "146021": "ETH/USD",  # ETHER CASH SETTLED - CME
}


def cot_release_ts(report_date: str) -> float:
    """The Friday 15:30 ET after a Tuesday report date, as epoch UTC.

    CFTC's published schedule, not per-row data. 19:30 UTC is exact under
    EDT and one hour early under EST — a deliberate conservative bias: the
    join may believe data arrived slightly LATER than it did, never
    earlier. Holiday-delayed releases (published the following Monday)
    carry the same conservative property.
    """
    d = datetime.fromisoformat(report_date[:10]).replace(tzinfo=timezone.utc)
    days_to_friday = (4 - d.weekday()) % 7
    release = d + timedelta(days=days_to_friday)
    return release.replace(hour=19, minute=30).timestamp()


def parse_cot_row(row: dict) -> list[dict]:
    """One Socrata row -> the stat series the desk shadows.

    Net positions rather than raw longs/shorts: the QUESTION each series
    answers is "which way does this cohort lean" — speculators
    (noncommercial), hedgers (commercial), small traders (nonreportable).
    """
    code = str(row.get("cftc_contract_market_code") or "").strip()
    symbol = COT_MARKETS.get(code)
    if not symbol:
        return []

    def _i(key):
        try:
            return int(float(row[key]))
        except (KeyError, TypeError, ValueError):
            return None

    as_of = str(row.get("report_date_as_yyyy_mm_dd") or "")[:10]
    if not as_of:
        return []
    nc_l, nc_s = _i("noncomm_positions_long_all"), _i("noncomm_positions_short_all")
    c_l, c_s = _i("comm_positions_long_all"), _i("comm_positions_short_all")
    nr_l, nr_s = _i("nonrept_positions_long_all"), _i("nonrept_positions_short_all")
    oi = _i("open_interest_all")

    out = []
    for series, value in (
        ("cot_noncomm_net", nc_l - nc_s if None not in (nc_l, nc_s) else None),
        ("cot_comm_net", c_l - c_s if None not in (c_l, c_s) else None),
        ("cot_nonrept_net", nr_l - nr_s if None not in (nr_l, nr_s) else None),
        ("cot_open_interest", oi),
    ):
        if value is None:
            continue
        out.append({"symbol": symbol, "series": series,
                    "value": float(value), "as_of": as_of})
    return out


def sync_cot(reports_back: int = 3) -> dict:
    """Pull the last N reports for every tracked market; dedup does the rest."""
    codes = ",".join(f"'{c}'" for c in COT_MARKETS)
    params = {
        "$select": ("cftc_contract_market_code,report_date_as_yyyy_mm_dd,"
                    "noncomm_positions_long_all,noncomm_positions_short_all,"
                    "comm_positions_long_all,comm_positions_short_all,"
                    "nonrept_positions_long_all,nonrept_positions_short_all,"
                    "open_interest_all"),
        "$where": f"cftc_contract_market_code in({codes})",
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": str(len(COT_MARKETS) * reports_back),
    }
    with httpx.Client(timeout=30.0) as client:
        rows = client.get(COT_URL, params=params).json()
    stats = [s for row in rows for s in parse_cot_row(row)]
    stored = _store_stats("cftc", "cot_legacy_socrata_v1", stats,
                          release_ts_fn=lambda s: cot_release_ts(s["as_of"]))
    return {"source": "cftc_cot", "fetched": len(stats), "stored": stored}


# ── FINRA daily short volume (keyless CDN file) ──────────────────────────────
FINRA_URL = "https://cdn.finra.org/equity/regsho/daily/CNMSshvol{yyyymmdd}.txt"


def parse_finra_file(text: str, universe: set[str]) -> list[dict]:
    """Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market.

    The ratio is the useful series — half of daily volume printing short
    is unremarkable (market-maker mechanics); the DRIFT of the ratio for
    one symbol against its own history is the shadow feature.
    """
    out = []
    for line in text.splitlines():
        parts = line.strip().split("|")
        if len(parts) < 5 or parts[0] == "Date":
            continue
        date_raw, sym = parts[0], parts[1].upper()
        if sym not in universe:
            continue
        try:
            short_vol, total_vol = float(parts[2]), float(parts[4])
        except ValueError:
            continue
        if total_vol <= 0:
            continue
        as_of = f"{date_raw[:4]}-{date_raw[4:6]}-{date_raw[6:8]}"
        out.append({"symbol": sym, "series": "finra_short_ratio",
                    "value": round(short_vol / total_vol, 4), "as_of": as_of})
        out.append({"symbol": sym, "series": "finra_total_volume",
                    "value": total_vol, "as_of": as_of})
    return out


def _equity_universe(days: int = 14, cap: int = 50) -> set[str]:
    """The equities the desk actually considered recently — the file covers
    every listed symbol; storing all of it would be §46 malpractice."""
    from sqlalchemy import text

    from app.database import engine

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with engine.connect() as c:
        rows = c.execute(text("""
            SELECT DISTINCT symbol FROM candidate_signals
            WHERE asset_class = 'Equity' AND created_at > :cutoff
            LIMIT :cap"""), {"cutoff": cutoff, "cap": cap}).fetchall()
    return {str(s).upper() for (s,) in rows}


def sync_finra_short_volume(max_days_back: int = 5) -> dict:
    """Latest available daily file (published ~18:00 ET same trading day);
    weekends/holidays step back until a file exists."""
    universe = _equity_universe()
    if not universe:
        return {"source": "finra_short", "skipped": "no recent equity candidates"}
    day = datetime.now(timezone.utc).date()
    for _ in range(max_days_back):
        url = FINRA_URL.format(yyyymmdd=day.strftime("%Y%m%d"))
        try:
            with httpx.Client(timeout=30.0) as client:
                r = client.get(url)
            if r.status_code == 200 and "|" in r.text[:200]:
                stats = parse_finra_file(r.text, universe)
                # Published the same evening; 23:00 UTC is a conservative
                # (late-biased) release stamp per the COT rationale.
                release = datetime.combine(
                    day, datetime.min.time(), tzinfo=timezone.utc
                ).replace(hour=23).timestamp()
                stored = _store_stats("finra", "regsho_daily_v1", stats,
                                      release_ts_fn=lambda s: release)
                return {"source": "finra_short", "file_date": day.isoformat(),
                        "universe": len(universe), "fetched": len(stats),
                        "stored": stored}
        except httpx.HTTPError as e:
            logger.debug(f"[OfficialData] FINRA fetch {day} failed: {e}")
        day -= timedelta(days=1)
    return {"source": "finra_short", "skipped": "no file found in window"}


# ── EIA weekly storage (free key required) ───────────────────────────────────
EIA_SERIES = (
    # (route, facet series id, our series name, instrument)
    ("petroleum/stoc/wstk", "WCESTUS1", "eia_crude_stocks_kbbl", "CL=F"),
    ("natural-gas/stor/wkly", "NW2_EPG0_SWO_R48_BCF", "eia_natgas_storage_bcf", "NG=F"),
)


def sync_eia(length: int = 4) -> dict:
    """Weekly petroleum + natgas storage. Gated on EIA_API_KEY (free,
    instant, eia.gov/opendata) — absent key reports itself rather than
    silently narrowing coverage.

    `length` is rows-per-series, newest first. The routine sync takes 4;
    backfill_eia() takes the archive — the v2 API serves full weekly
    history (decades) in one request per series, and the dedup keys make
    re-pulling any of it idempotent.
    """
    key = (os.getenv("EIA_API_KEY") or "").strip()
    if not key:
        return {"source": "eia", "skipped": "EIA_API_KEY not set (free at eia.gov/opendata)"}
    stats, errors = [], []
    with httpx.Client(timeout=60.0) as client:
        for route, series_id, series, symbol in EIA_SERIES:
            try:
                data = client.get(
                    f"https://api.eia.gov/v2/{route}/data/",
                    params={"api_key": key, "frequency": "weekly",
                            "data[0]": "value",
                            "facets[series][]": series_id,
                            "sort[0][column]": "period",
                            "sort[0][direction]": "desc",
                            "length": str(min(int(length), 5000))},
                ).json()
                for row in (data.get("response") or {}).get("data") or []:
                    stats.append({"symbol": symbol, "series": series,
                                  "value": float(row["value"]),
                                  "as_of": str(row["period"])})
            except Exception as e:
                errors.append(f"{series}: {e}")
    # Petroleum releases Wed 10:30 ET, natgas Thu 10:30 ET, both for the
    # prior Friday's week — as_of + 5 days at 15:30 UTC biases late.
    def _release(s):
        d = datetime.fromisoformat(s["as_of"]).replace(tzinfo=timezone.utc)
        return (d + timedelta(days=5)).replace(hour=15, minute=30).timestamp()
    stored = _store_stats("eia", "eia_v2_weekly_v1", stats, release_ts_fn=_release)
    return {"source": "eia", "fetched": len(stats), "stored": stored,
            "errors": errors or None}


def backfill_eia() -> dict:
    """One-shot archive pull: the full weekly history per series (crude
    stocks reach back to 1982). Shadow features get decades of context on
    day one instead of accumulating a year before a seasonal z-score means
    anything. Idempotent — run it whenever."""
    return sync_eia(length=5000)


# ── Shared persistence ───────────────────────────────────────────────────────

def _store_stats(source: str, schema: str, stats: list[dict],
                 release_ts_fn) -> int:
    """Straight to the store — official syncs run in scheduler threads on
    multi-hour cadences, so the streaming queue's never-block guarantee
    buys nothing here, and a synchronous write means the job's return
    value reports what actually landed."""
    from lib.event_store import get_store
    from lib.market_events import OfficialStat, event_to_dict, make_meta

    events = []
    for s in stats:
        events.append(event_to_dict(OfficialStat(
            meta=make_meta(source, schema, release_ts_fn(s)),
            symbol=s["symbol"], series=s["series"], value=s["value"],
            as_of=s["as_of"],
            dedup_key=f"{source}:{s['series']}:{s['symbol']}:{s['as_of']}",
        )))
    return get_store().append(events)


def sync_all() -> dict:
    """The job body: every official source, each failing independently."""
    out = {}
    for name, fn in (("cot", sync_cot), ("finra", sync_finra_short_volume),
                     ("eia", sync_eia)):
        try:
            out[name] = fn()
        except Exception as e:
            logger.warning(f"[OfficialData] {name} sync failed: {e}")
            out[name] = {"error": str(e)[:200]}
    logger.info(f"[OfficialData] sync: {out}")
    return out
