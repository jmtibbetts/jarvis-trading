"""Point-in-time market context, attached to every candidate at birth.

The 4C engines can say "copper specs are at their 99th percentile" — but
no outcome row knows what the context was WHEN ITS SETUP WAS JUDGED, so
the question the whole apparatus exists to answer ("does positioning
extremity predict outcomes for THIS desk?") has no joinable data. This
module closes the loop: a compact, versioned context dict is built from
released data only and stored on the candidate row at record time.

Rules inherited from everything upstream:

  stored at birth   like shadow variants — never recomputed in
                    hindsight, so ablation on it cannot leak
  release-filtered  a COT report that hadn't dropped yet is invisible,
                    same discipline as the sector engine
  absent is absent  a missing feed is a missing key, never a zero
  non-fatal         context bookkeeping must never cost a candidate row

The context is deliberately SMALL — a dozen named numbers per candidate,
chosen because each has a hypothesis attached (crowding, carry, squeeze
fuel, inventory surprise). This is not a feature dump; every key here is
something the ablation can individually confirm or kill.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Version pinned like every schema in this system: keys may be added
# under a new version, never silently redefined.
CONTEXT_SCHEMA_VERSION = "ctx_v1_2026-08-15"

# One context per (symbol) per this many seconds — candidates arrive in
# scan batches and the underlying stats move on weekly/hourly clocks.
_CACHE_TTL_SEC = 300.0
_cache: dict[str, tuple[float, dict]] = {}


def _base(symbol: str) -> str:
    return str(symbol or "").upper().split("/")[0].strip()


def _latest_stat(symbol: str, series: str, asof: datetime) -> tuple | None:
    """(as_of_date, value) of the newest RELEASED row, or None."""
    from lib.sector_engine import _series as released_series
    hist = released_series(symbol, series, asof)
    return hist[-1] if hist else None


def _cot_block(stat_symbol: str, asof: datetime) -> dict:
    from lib.sector_engine import _pctile, _series as released_series
    spec = released_series(stat_symbol, "cot_noncomm_net", asof)
    if not spec:
        return {}
    return {
        "cot_spec_net": spec[-1][1],
        "cot_spec_pctile_3y": _pctile(spec),
        "cot_age_days": (asof.date() - spec[-1][0]).days,
    }


def _derivatives_block(symbol: str, asof: datetime) -> dict:
    """Latest funding/OI/long-short observations from the event store.
    Derivatives observations are keyed by BASE symbol ('BTC'), one row
    per (venue, metric); the freshest OKX row wins per metric."""
    from lib.event_store import get_store
    out: dict = {}
    newest: dict[str, tuple[float, float]] = {}
    for row in get_store().read(_base(symbol), "derivatives",
                                since_ts=0, limit=2000):
        ts = row.get("ingest_ts") or 0
        if ts > asof.timestamp() or row.get("meta", {}).get("source") == "cryptocom":
            continue
        m = row.get("metric")
        if m and ts >= newest.get(m, (0, 0))[0]:
            newest[m] = (ts, row.get("value"))
    for metric, key in (("funding_rate", "funding_rate"),
                        ("open_interest_usd", "oi_usd"),
                        ("long_short_ratio", "long_short_ratio")):
        if metric in newest:
            out[key] = newest[metric][1]
            age_h = (asof.timestamp() - newest[metric][0]) / 3600
            # Stale derivatives state is worse than none: the desk would
            # be reasoning about a market that has moved on.
            if age_h > 24:
                out.pop(key, None)
    return out


def _curve_block(root: str, asof: datetime) -> dict:
    from lib.sector_engine import _latest_curve
    c = _latest_curve(root, asof)
    if not c or (asof.timestamp() - (c.get("ingest_ts") or 0)) > 86400:
        return {}
    out = {"curve_structure": c.get("structure")}
    if c.get("annualized_roll_pct") is not None:
        out["annualized_roll_pct"] = c.get("annualized_roll_pct")
    return out


def _finra_block(symbol: str, asof: datetime) -> dict:
    row = _latest_stat(symbol, "finra_short_ratio", asof)
    if row is None or (asof.date() - row[0]).days > 7:
        return {}
    return {"finra_short_ratio": row[1],
            "finra_age_days": (asof.date() - row[0]).days}


def _eia_block(root: str, asof: datetime) -> dict:
    from lib.sector_engine import _fundamental_block
    series = {"CL": ("eia_crude_stocks_kbbl", "kbbl"),
              "NG": ("eia_natgas_storage_bcf", "bcf")}.get(root)
    if not series:
        return {}
    block = _fundamental_block(f"{root}=F", series[0], asof, series[1])
    if "abstain" in block:
        return {}
    out = {}
    if block.get("seasonal_z_5y") is not None:
        out["eia_seasonal_z"] = block["seasonal_z_5y"]
    if block.get("change_z_3y") is not None:
        out["eia_change_z"] = block["change_z_3y"]
    return out


def build_context(symbol: str, asof: datetime | None = None) -> dict | None:
    """The context dict for one instrument, from released data only.

    Returns None when nothing is known — an empty context is not worth a
    row, and the ablation treats absent context as absent, not neutral.
    """
    from lib.futures_contracts import root_of
    from lib.instruments import asset_class_of, canonical

    asof = asof or datetime.now(timezone.utc)
    sym = canonical(symbol)
    ctx: dict = {}
    cls = asset_class_of(sym)

    if cls == "Crypto":
        ctx.update(_derivatives_block(sym, asof))
        # Slow network state (daily): MVRV and activity percentiles. Only
        # BTC/ETH have community coverage, so most coins get nothing —
        # which is an absent key, never a fabricated neutral.
        try:
            from lib.onchain import latest_context
            ctx.update(latest_context(sym, asof))
        except Exception as e:
            logger.debug(f"[CandidateContext] onchain skipped: {e}")
        # CME positioning exists for BTC/ETH only; other bases just skip.
        ctx.update(_cot_block(sym, asof))
    elif cls == "Futures":
        root = root_of(sym)
        if root:
            ctx.update(_cot_block(f"{root}=F", asof))
            ctx.update(_curve_block(root, asof))
            ctx.update(_eia_block(root, asof))
    elif cls == "Equity":
        ctx.update(_finra_block(sym, asof))

    if not ctx:
        return None
    ctx["schema"] = CONTEXT_SCHEMA_VERSION
    return ctx


def context_for_candidate(symbol: str) -> dict | None:
    """build_context with a short cache — candidates arrive in scan
    batches; the stats underneath move on weekly/hourly clocks."""
    key = str(symbol or "").upper()
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < _CACHE_TTL_SEC:
        return hit[1]
    try:
        ctx = build_context(key)
    except Exception as e:
        logger.debug(f"[CandidateContext] {key} failed (non-fatal): {e}")
        ctx = None
    _cache[key] = (now, ctx)
    return ctx
