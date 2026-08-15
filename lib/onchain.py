"""On-chain fundamentals — Coin Metrics community API, free and keyless.

Block 2 of the staged crypto-data plan. This is the SLOW context layer:
network-level facts that move on a daily clock and describe conditions,
never entries. MVRV at 2.4 is not a signal; it is the weather the
signals happen in.

Two disciplines carried from everything upstream:

**Daily means daily.** Coin Metrics defines these at 1-day frequency, so
polling hourly would manufacture four extra copies of one observation
and teach the ablation that stale data is fresh data. Synced once a day.

**Ask what's granted, don't assume.** The community tier gates metrics
per asset, and a guessed metric name returns 403 for the whole request —
one wrong name silently costs every other metric in the batch. The
catalog is queried and the request is built from what it grants (checked
live 2026-08-16: 31 metrics for BTC including CapMVRVCur, AdrActCnt,
TxCnt, SplyCur, CapMrktCurUSD, HashRate; FeeMeanUSD and CapRealUSD are
NOT granted and asking for them fails the batch).

LICENSING: community data is non-commercial. If this desk ever ships as
a product, this source needs revisiting before it goes out the door —
noted here because a license violation discovered late is expensive.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx

logger = logging.getLogger(__name__)

CM_BASE = "https://community-api.coinmetrics.io/v4"

# Desk symbol -> Coin Metrics asset id.
ASSETS = {"BTC/USD": "btc", "ETH/USD": "eth"}

# What we'd like, in priority order. Intersected with what the catalog
# actually grants before any request is sent.
WANTED = (
    "CapMVRVCur",      # market value / realized value — the classic cycle gauge
    "AdrActCnt",       # active addresses — network demand
    "TxCnt",           # transaction count — usage
    "CapMrktCurUSD",   # market cap
    "SplyCur",         # circulating supply
    "HashRate",        # security/production cost (BTC-meaningful)
)

_catalog_cache: dict[str, set] = {}


def granted_metrics(asset: str) -> set:
    """What the community tier actually serves for this asset. Cached —
    the catalog changes on the order of releases, not minutes."""
    if asset in _catalog_cache:
        return _catalog_cache[asset]
    try:
        r = httpx.get(f"{CM_BASE}/catalog-v2/asset-metrics",
                      params={"assets": asset, "page_size": 1}, timeout=40.0)
        r.raise_for_status()
        data = r.json().get("data") or []
        ms = data[0].get("metrics") if data else {}
        keys = set(ms.keys()) if isinstance(ms, dict) else {
            m.get("metric") for m in (ms or [])}
    except Exception as e:
        logger.debug(f"[OnChain] catalog for {asset} failed: {e}")
        keys = set()
    _catalog_cache[asset] = keys
    return keys


def fetch_metrics(asset: str, days: int = 400) -> list[dict]:
    """Daily rows for one asset, requesting only granted metrics."""
    metrics = [m for m in WANTED if m in granted_metrics(asset)]
    if not metrics:
        return []
    start = (datetime.now(timezone.utc) - timedelta(days=days)).date()
    try:
        r = httpx.get(f"{CM_BASE}/timeseries/asset-metrics", params={
            "assets": asset, "metrics": ",".join(metrics),
            "frequency": "1d", "start_time": start.isoformat(),
            "page_size": 10_000}, timeout=60.0)
        r.raise_for_status()
        return r.json().get("data") or []
    except Exception as e:
        logger.debug(f"[OnChain] {asset} metrics failed: {e}")
        return []


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def sync() -> dict:
    """Daily job body: store every asset's rows as OfficialStat events.

    The observation date IS the release date for these — Coin Metrics
    publishes a day's metrics after that day closes, so as_of and the
    release stamp coincide at day-end. Stamped at 00:00 UTC of the
    FOLLOWING day: a metric describing the 15th was not knowable during
    the 15th, and a replay that thinks otherwise has a crystal ball.
    """
    from lib.event_store import get_store
    from lib.market_events import OfficialStat, event_to_dict, make_meta

    events, per_asset = [], {}
    for symbol, asset in ASSETS.items():
        rows = fetch_metrics(asset)
        n = 0
        for row in rows:
            day = str(row.get("time", ""))[:10]
            if not day:
                continue
            try:
                release = (datetime.fromisoformat(day)
                           .replace(tzinfo=timezone.utc)
                           + timedelta(days=1)).timestamp()
            except ValueError:
                continue
            for metric in WANTED:
                val = _f(row.get(metric))
                if val is None:
                    continue
                events.append(event_to_dict(OfficialStat(
                    meta=make_meta("coinmetrics", "cm_community_v4", release),
                    symbol=symbol, series=f"cm_{metric}", value=val,
                    as_of=day,
                    dedup_key=f"coinmetrics:cm_{metric}:{symbol}:{day}")))
                n += 1
        per_asset[symbol] = {"rows": len(rows), "stats": n}
    stored = get_store().append(events) if events else 0
    out = {"source": "coinmetrics", "assets": per_asset,
           "fetched": len(events), "stored": stored}
    logger.info(f"[OnChain] sync: {out}")
    return out


def latest_context(symbol: str, asof: datetime | None = None) -> dict:
    """Released on-chain state for one symbol — for the candidate context
    join. Percentile of MVRV against its own trailing 2 years, because
    the level alone means nothing without the asset's own history."""
    from lib.sector_engine import _pctile, _series as released

    asof = asof or datetime.now(timezone.utc)
    out: dict = {}
    mvrv = released(symbol, "cm_CapMVRVCur", asof)
    if mvrv:
        out["mvrv"] = round(mvrv[-1][1], 3)
        pct = _pctile(mvrv, years=2)
        if pct is not None:
            out["mvrv_pctile_2y"] = pct
        out["mvrv_age_days"] = (asof.date() - mvrv[-1][0]).days
    active = released(symbol, "cm_AdrActCnt", asof)
    if active:
        out["active_addresses"] = active[-1][1]
        pct = _pctile(active, years=2)
        if pct is not None:
            out["active_addr_pctile_2y"] = pct
    # A stale daily source is worse than none: drop the block rather than
    # let a week-old reading masquerade as today's network state.
    if out.get("mvrv_age_days", 0) > 4:
        return {}
    return out
