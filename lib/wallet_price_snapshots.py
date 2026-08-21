"""Forward price evidence for the exact mints the shadow desk needs.

THE GAP THIS CLOSES. `token_activity_snapshots` is written by exactly one
producer — `lib/token_surge.scan_and_score` — and that pass scans whatever
GeckoTerminal calls TRENDING. It is a discovery sweep, not a watchlist, so
the mints a watched wallet actually touched are covered only by luck:
measured on the live store, 45 of 558 event mints, inside a single
nine-hour window that ended and never resumed. A thesis needs a price NEAR
ITS OWN EVENT and another NEAR ITS DUE TIME, and neither exists for a mint
nobody scanned.

This module asks for named mints instead. It does NOT define a second
market-data system: the fetch is the existing `lib/geckoterminal` client,
the flattening is `token_surge.snapshot_from_pool`, and the row is
`token_surge.persist_snapshot`. Only the SELECTION is new.

WHY POOLS AND NOT THE CHEAPER TOKEN ENDPOINT. GeckoTerminal will return a
token's price on its own, which would be a third of the payload. It would
also produce snapshot rows with NULL transaction buckets, and
`token_surge.baseline_from` coerces a missing `buys_m5` to ZERO when it
takes the median — so every price-only row would drag a token's baseline
down and manufacture a surge that never happened, in the pass that feeds
wallet discovery. `include=top_pools` returns the same object the surge
scanner already stores, for the same one call per thirty mints, and cannot
contaminate the baseline.

MISSING STAYS MISSING. A mint the provider does not cover produces no row
and is reported as UNSUPPORTED. It is never stored as a zero, and today's
price is never written under an older timestamp.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

SNAPSHOT_COLLECTOR_VERSION = "wallet_price_snapshots_v1"

#: GeckoTerminal accepts thirty addresses per multi-token request.
MINTS_PER_CALL = 30
#: Bounded per cycle. Four calls covers a whole cycle's priority set on the
#: measured data and leaves the keyless rate budget to the surge scanner.
MAX_CALLS_PER_CYCLE = 4
MAX_MINTS_PER_CYCLE = MINTS_PER_CALL * MAX_CALLS_PER_CYCLE

#: A snapshot this fresh already answers the question; asking again inside
#: the window spends a call to store a duplicate.
FRESH_ENOUGH_SECONDS = 600

#: Priority reasons, strongest first. The order IS the policy.
DUE_CHECKPOINT = "DUE_OUTCOME_CHECKPOINT"
NEW_EVENT_REFERENCE = "NEW_EVENT_REFERENCE_PRICE"
PENDING_ELIGIBILITY = "PENDING_WALLET_QUALITY_MEASUREMENT"
BACKGROUND_COVERAGE = "BACKGROUND_COVERAGE"

PRIORITY_ORDER = (DUE_CHECKPOINT, NEW_EVENT_REFERENCE, PENDING_ELIGIBILITY,
                  BACKGROUND_COVERAGE)

_ENV_PREFIX = "JARVIS_WALLET_PRICE_"


def _cfg_int(name: str, default: int) -> int:
    raw = os.getenv(f"{_ENV_PREFIX}{name}")
    if raw is None:
        return default
    try:
        return max(0, int(str(raw).strip()))
    except (TypeError, ValueError):
        return default


def max_calls_per_cycle() -> int:
    return _cfg_int("MAX_CALLS", MAX_CALLS_PER_CYCLE)


def max_mints_per_cycle() -> int:
    return _cfg_int("MAX_MINTS", MAX_MINTS_PER_CYCLE)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt) -> str | None:
    return None if dt is None else dt.isoformat()


# -- Quote assets ---------------------------------------------------------
#
# THE BLOCKER THIS EXISTS FOR, measured rather than assumed. A watched
# wallet's buys are quoted in SOL, and `lib/quote_valuation` will not value
# a SOL leg from a bar more than six hours from the trade — correctly, since
# SOL moved from $77.44 to $89.40 across the two days in question. The
# hourly SOL/USD series is filled by `lib/ohlcv.fetch_multi_timeframe`,
# which was a scheduler job; with the scheduler off the series went 39.8
# HOURS stale, so every recent round trip came back UNPRICED and every
# wallet came back UNSCORED.
#
# That is why one of five watched wallets showed 225 transfer legs, 50 of
# them token-against-SOL, and ZERO scoreable round trips. Not the wallet's
# behaviour — the quote asset had no price.
#
# Refreshed here because a wallet score is downstream of it, using the
# EXISTING canonical fetcher and the existing cache. No second market-data
# system, no new provider, and the freshness rule is not relaxed: a stale
# SOL price still refuses to value a trade.
QUOTE_SERIES = ("SOL/USD",)
QUOTE_TIMEFRAME = "1H"

#: Refreshing costs one provider call, so it is skipped while the series is
#: still inside the tolerance that would accept it anyway.
QUOTE_REFRESH_AFTER_SECONDS = 1800


def quote_series_state(*, now=None) -> dict:
    """Age and usability of each quote series wallet scoring depends on."""
    from lib import quote_valuation as Q

    now = now or _now()
    out = {"series": [], "stale": 0, "fresh": 0,
           "max_bar_distance_hours": Q.MAX_BAR_DISTANCE_HOURS}
    try:
        Q.reset_cache()
        bars = Q._load_sol_bars()
    except Exception as e:                                   # noqa: BLE001
        logger.warning("[WalletPrices] SOL series unavailable: %s", e)
        out["series"].append({"symbol": "SOL/USD", "state": "UNAVAILABLE",
                              "detail": str(e)[:160]})
        return out

    if not bars:
        out["series"].append({"symbol": "SOL/USD", "state": "NO_HISTORY",
                              "bars": 0, "last_bar_at": None,
                              "age_hours": None})
        out["stale"] += 1
        return out

    age_h = (now.timestamp() - bars[-1][0]) / 3600.0
    usable = age_h <= Q.MAX_BAR_DISTANCE_HOURS
    out["series"].append({
        "symbol": "SOL/USD", "timeframe": QUOTE_TIMEFRAME,
        "state": "FRESH" if usable else "STALE",
        "bars": len(bars),
        "last_bar_at": _iso(datetime.fromtimestamp(bars[-1][0],
                                                   tz=timezone.utc)),
        "last_price_usd": bars[-1][1],
        "age_hours": round(age_h, 2),
        "usable_for_scoring": usable,
    })
    out["fresh" if usable else "stale"] += 1
    return out


def refresh_quote_series(*, now=None, force: bool = False,
                         fetch=None) -> dict:
    """Top up the hourly series for every quote asset scoring depends on.

    NEVER RAISES, and never widens the freshness rule. If the provider has
    nothing newer, the series stays stale and says so — a stale quote price
    must keep refusing to value a trade.
    """
    now = now or _now()
    out = {"attempted": 0, "refreshed": 0, "skipped_fresh": 0,
           "errors": [], "before": None, "after": None}

    before = quote_series_state(now=now)
    out["before"] = before
    needs = [s for s in before["series"]
             if force or s.get("state") != "FRESH"
             or (s.get("age_hours") or 0) * 3600 > QUOTE_REFRESH_AFTER_SECONDS]
    if not needs:
        out["skipped_fresh"] = len(before["series"])
        out["after"] = before
        return out

    if fetch is None:
        try:
            from lib.ohlcv import fetch_multi_timeframe as fetch
        except Exception as e:                               # noqa: BLE001
            out["errors"].append(f"fetcher unavailable: {str(e)[:160]}")
            out["after"] = before
            return out

    for symbol in QUOTE_SERIES:
        out["attempted"] += 1
        try:
            got = fetch(symbol, [QUOTE_TIMEFRAME]) or {}
            df = got.get(QUOTE_TIMEFRAME)
            if df is not None and len(df):
                out["refreshed"] += 1
        except Exception as e:                               # noqa: BLE001
            logger.warning("[WalletPrices] %s refresh failed: %s", symbol, e)
            out["errors"].append(
                f"{symbol}: {type(e).__name__}: {str(e)[:140]}")

    out["after"] = quote_series_state(now=_now())
    return out


# -- Selection ------------------------------------------------------------
def priority_mints(*, limit: int | None = None, now=None) -> list[dict]:
    """The exact mints worth a price right now, strongest reason first.

    Deduplicated on the mint, keeping the STRONGEST reason: a mint that is
    both a due checkpoint and a new event is fetched once, for the
    checkpoint.
    """
    from sqlalchemy import text

    from app.database import engine
    from lib import wallet_event_classifier as C
    from lib import wallet_shadow_intel as W
    CURRENT_ONLY_E = W.CURRENT_ONLY_E

    now = now or _now()
    cap = int(limit if limit is not None else max_mints_per_cycle())
    if cap <= 0:
        return []

    picked: dict = {}

    def add(mint, reason, detail):
        if not mint or mint in picked:
            return
        # A quote asset is what a position is priced IN, not a position.
        if C.is_quote_asset(mint):
            return
        picked[mint] = {"mint": mint, "reason": reason, "detail": detail}

    with engine.connect() as conn:
        # 1. DUE CHECKPOINTS. An outcome whose due time has passed and has
        #    no qualifying price is the only work with a deadline: the
        #    window closes and the answer is lost.
        for mint, horizon, due in conn.execute(text(
                "SELECT e.subject_mint, o.horizon, o.due_at "
                "FROM wallet_shadow_outcomes o "
                "JOIN wallet_shadow_events e ON e.id = o.event_id "
                "WHERE o.status = :st AND o.due_at <= :now "
                f"  AND {CURRENT_ONLY_E} "
                "ORDER BY o.due_at LIMIT 400"),
                {"st": W.UNRESOLVED, "now": _iso(now)}).fetchall():
            add(mint, DUE_CHECKPOINT, f"{horizon} checkpoint due {due}")

        # 2. NEW EVENT REFERENCES. An event only gets one chance at an
        #    event-time price: once it is older than the freshness policy,
        #    no snapshot taken later can ever qualify. These are fetched
        #    while that is still true.
        window = _iso(now - timedelta(seconds=W.PRICE_MAX_AGE_SECONDS))
        for mint, when in conn.execute(text(
                "SELECT subject_mint, MAX(event_time) FROM "
                "wallet_shadow_events e WHERE event_time >= :w "
                f"  AND {CURRENT_ONLY_E} "
                "  AND subject_mint IS NOT NULL "
                "GROUP BY subject_mint ORDER BY MAX(event_time) DESC "
                "LIMIT 200"), {"w": window}).fetchall():
            add(mint, NEW_EVENT_REFERENCE,
                f"event at {when} is still inside the "
                f"{W.PRICE_MAX_AGE_SECONDS}s reference window")

        # 3. PENDING ELIGIBILITY. A classified trading event refused ONLY
        #    for the want of a price is one snapshot away from an answer.
        for mint, reason in conn.execute(text(
                "SELECT subject_mint, refusal_reason FROM "
                "wallet_shadow_events e WHERE state = :st "
                f"  AND {CURRENT_ONLY_E} "
                "  AND refusal_reason IN (:r1, :r2) "
                "  AND subject_mint IS NOT NULL "
                "ORDER BY event_time DESC LIMIT 200"),
                {"st": W.STATE_REFUSED, "r1": W.NO_PRICE,
                 "r2": W.STALE_PRICE}).fetchall():
            add(mint, PENDING_ELIGIBILITY,
                f"refused {reason}; a qualifying price would settle it")

        # 4. BACKGROUND. Everything else this desk has ever seen, newest
        #    first, so coverage widens instead of standing still.
        for (mint,) in conn.execute(text(
                "SELECT subject_mint FROM wallet_shadow_events e "
                f"WHERE subject_mint IS NOT NULL AND {CURRENT_ONLY_E} "
                "GROUP BY subject_mint ORDER BY MAX(event_time) DESC "
                "LIMIT 400")).fetchall():
            add(mint, BACKGROUND_COVERAGE, "widening coverage")

    ordered = sorted(picked.values(),
                     key=lambda r: PRIORITY_ORDER.index(r["reason"]))
    return _drop_fresh(ordered, now=now)[:cap]


def _drop_fresh(rows: list, *, now) -> list:
    """Skip mints whose stored price is already newer than the window."""
    from sqlalchemy import text

    from app.database import engine

    if not rows:
        return []
    cutoff = _iso(now - timedelta(seconds=FRESH_ENOUGH_SECONDS))
    mints = [r["mint"] for r in rows]
    fresh: set = set()
    with engine.connect() as conn:
        chunk = 400
        for i in range(0, len(mints), chunk):
            part = mints[i:i + chunk]
            marks = ",".join(f":m{j}" for j in range(len(part)))
            params = {f"m{j}": v for j, v in enumerate(part)}
            params["cut"] = cutoff
            for (mint,) in conn.execute(text(
                    f"SELECT DISTINCT mint FROM token_activity_snapshots "
                    f"WHERE mint IN ({marks}) AND captured_at >= :cut"),
                    params).fetchall():
                fresh.add(mint)
    return [r for r in rows if r["mint"] not in fresh]


# -- Collection -----------------------------------------------------------
def collect(mints=None, *, limit: int | None = None,
            max_calls: int | None = None, fetch=None, now=None) -> dict:
    """Store one snapshot per requested mint that the provider covers.

    NEVER RAISES. A provider failure leaves the mint uncovered and named,
    which is the truthful outcome — an absent price must never become a
    zero or a stale price reused as a current one.
    """
    now = now or _now()
    budget_calls = int(max_calls if max_calls is not None
                       else max_calls_per_cycle())
    cap = int(limit if limit is not None else max_mints_per_cycle())

    stats = {"considered": 0, "requested": 0, "provider_calls": 0,
             "snapshots": 0, "covered": [], "unsupported": [],
             "by_reason": {}, "errors": [],
             "budget_calls": budget_calls, "budget_mints": cap,
             "version": SNAPSHOT_COLLECTOR_VERSION}
    if budget_calls <= 0 or cap <= 0:
        return stats

    try:
        rows = (list(mints) if mints is not None
                else priority_mints(limit=cap, now=now))
    except Exception as e:                                   # noqa: BLE001
        logger.warning("[WalletPrices] selection failed: %s", e)
        stats["errors"].append(f"selection: {type(e).__name__}: {e}"[:200])
        return stats

    rows = [{"mint": r, "reason": BACKGROUND_COVERAGE, "detail": "requested"}
            if isinstance(r, str) else r for r in rows]
    stats["considered"] = len(rows)
    rows = rows[:min(cap, budget_calls * MINTS_PER_CALL)]
    if not rows:
        return stats

    for r in rows:
        stats["by_reason"][r["reason"]] = \
            stats["by_reason"].get(r["reason"], 0) + 1

    if fetch is None:
        from lib.geckoterminal import get as fetch

    from app.database import get_db
    from lib.token_surge import persist_snapshot, snapshot_from_pool

    wanted_order = [r["mint"] for r in rows]
    stats["requested"] = len(wanted_order)
    covered: set = set()

    with get_db() as session:
        for i in range(0, len(wanted_order), MINTS_PER_CALL):
            if stats["provider_calls"] >= budget_calls:
                break
            batch = wanted_order[i:i + MINTS_PER_CALL]
            errors: list = []
            stats["provider_calls"] += 1
            try:
                body = fetch(
                    f"networks/solana/tokens/multi/{','.join(batch)}"
                    f"?include=top_pools", errors=errors)
            except Exception as e:                           # noqa: BLE001
                stats["errors"].append(
                    f"batch {i // MINTS_PER_CALL}: "
                    f"{type(e).__name__}: {str(e)[:120]}")
                continue
            if errors:
                stats["errors"].extend(str(x)[:160] for x in errors[:3])
            if not body:
                # None means UNKNOWN, never "empty". The batch is
                # uncovered and says so.
                continue

            wanted = set(batch)
            best: dict = {}
            for pool in (body.get("included") or []):
                snap = snapshot_from_pool(pool)
                if not snap:
                    continue
                # A top pool can be returned for a mint on the QUOTE side.
                # Storing it under the requested mint would attach one
                # token's price to another's identity, so only the pool
                # whose BASE token is the mint we asked for is kept.
                if snap["mint"] not in wanted:
                    continue
                prev = best.get(snap["mint"])
                if prev is None or (snap.get("liquidity_usd") or 0) > \
                        (prev.get("liquidity_usd") or 0):
                    best[snap["mint"]] = snap

            for mint, snap in best.items():
                if snap.get("price_usd") is None:
                    continue
                persist_snapshot(session, snap)
                covered.add(mint)
                stats["snapshots"] += 1

    stats["covered"] = sorted(covered)
    stats["unsupported"] = sorted(set(wanted_order) - covered)
    return stats


# -- What the desk shows --------------------------------------------------
def coverage(*, now=None) -> dict:
    """Price coverage over the mints this desk actually cares about."""
    from sqlalchemy import text

    from app.database import engine
    from lib import wallet_shadow_intel as W
    CURRENT_ONLY_E = W.CURRENT_ONLY_E

    now = now or _now()
    out = {"event_mints": 0, "priced_mints": 0, "fresh_mints": 0,
           "stale_mints": 0, "unpriced_mints": 0,
           "due_checkpoints": 0, "resolved_checkpoints": 0,
           "unresolved_checkpoints": 0, "pending_mints": None,
           "last_snapshot_at": None, "snapshot_rows": 0,
           "fresh_window_seconds": W.PRICE_MAX_AGE_SECONDS,
           "version": SNAPSHOT_COLLECTOR_VERSION, "state": "MEASURED"}
    fresh_cut = _iso(now - timedelta(seconds=W.PRICE_MAX_AGE_SECONDS))
    try:
        with engine.connect() as conn:
            out["event_mints"] = conn.execute(text(
                "SELECT COUNT(DISTINCT subject_mint) FROM "
                f"wallet_shadow_events e WHERE subject_mint IS NOT NULL "
                f"AND {CURRENT_ONLY_E}"
            )).scalar() or 0
            out["priced_mints"] = conn.execute(text(
                "SELECT COUNT(*) FROM (SELECT DISTINCT e.subject_mint "
                "FROM wallet_shadow_events e JOIN token_activity_snapshots t "
                f"ON t.mint = e.subject_mint WHERE {CURRENT_ONLY_E})")).scalar() or 0
            out["fresh_mints"] = conn.execute(text(
                "SELECT COUNT(*) FROM (SELECT DISTINCT e.subject_mint "
                "FROM wallet_shadow_events e JOIN token_activity_snapshots t "
                f"ON t.mint = e.subject_mint WHERE t.captured_at >= :cut "
                f"AND {CURRENT_ONLY_E})"),
                {"cut": fresh_cut}).scalar() or 0
            out["snapshot_rows"] = conn.execute(text(
                "SELECT COUNT(*) FROM token_activity_snapshots")).scalar() or 0
            out["last_snapshot_at"] = conn.execute(text(
                "SELECT MAX(captured_at) FROM token_activity_snapshots"
            )).scalar()
            out["due_checkpoints"] = conn.execute(text(
                "SELECT COUNT(*) FROM wallet_shadow_outcomes "
                "WHERE status = :st AND due_at <= :now"),
                {"st": W.UNRESOLVED, "now": _iso(now)}).scalar() or 0
            out["resolved_checkpoints"] = conn.execute(text(
                "SELECT COUNT(*) FROM wallet_shadow_outcomes "
                "WHERE status = :st"), {"st": W.RESOLVED}).scalar() or 0
            out["unresolved_checkpoints"] = conn.execute(text(
                "SELECT COUNT(*) FROM wallet_shadow_outcomes "
                "WHERE status = :st"), {"st": W.UNRESOLVED}).scalar() or 0
    except Exception as e:                                   # noqa: BLE001
        logger.warning("[WalletPrices] coverage unavailable: %s", e)
        out["state"] = "UNAVAILABLE"
        out["detail"] = str(e)[:200]
        return out

    # STALE is a measured third state, not the leftover after fresh.
    out["stale_mints"] = max(0, out["priced_mints"] - out["fresh_mints"])
    out["unpriced_mints"] = max(0, out["event_mints"] - out["priced_mints"])
    try:
        out["pending_mints"] = len(priority_mints(limit=1000, now=now))
    except Exception:                                        # noqa: BLE001
        out["pending_mints"] = None
    return out
