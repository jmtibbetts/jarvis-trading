"""Which tokens are becoming unusually active RIGHT NOW, for them.

The question this replaces is "which token has the most volume", which is
a different question and mostly the wrong one:

    TOKEN A   $20M/day, normal daily volume $20M      -> not interesting
    TOKEN B   $600k/day, but 5m volume just went
              $1,200 -> $85,000 and 20 tph -> 1,400   -> very interesting

B ranks far above A here even though A is 30x larger, because the subject
is CHANGE, not SIZE.

Two things make that measurable, and the second is why this module needs a
database at all:

1. Acceleration against the token's OWN baseline, from stored snapshots.
   The market API's own buckets only support "is h1 busier than h24/24",
   which is far too coarse to see a 70x jump in 5m activity.

2. A MEDIAN baseline, not a mean. One prior spike in the window would drag
   a mean up and hide the very surge being looked for.

Kept deliberately separate, because conflating them is how a dump gets
bought:

    ACTIVITY SURGE SCORE   how unusual the activity is      (0-100)
    DIRECTIONAL BIAS       what the activity looks like     (bull/bear/mixed)

Enormous transaction acceleration with heavy sell volume and falling
liquidity is a real surge and a bearish one.
"""
from __future__ import annotations

import json
import logging
import os
import statistics
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# Baselines need enough observations to be a baseline. Below this a token
# is scored by the new-token model instead, and says so.
MIN_SNAPSHOTS_FOR_BASELINE = 6
BASELINE_WINDOW_HOURS = 6

# Floors. Dividing by a near-zero baseline manufactures huge ratios out of
# nothing — "$50 became $5,000" is a 100x that means nothing.
VOLUME_FLOOR_USD = 250.0
TXN_FLOOR = 3.0
WALLET_FLOOR = 2.0

# Contribution of each component to the composite. Overridable so the
# weights can be tuned without editing code.
DEFAULT_WEIGHTS = {
    "volume_accel": 0.25,
    "txn_accel": 0.25,
    "wallet_accel": 0.15,
    "buy_pressure": 0.10,
    "liquidity_accel": 0.10,
    "price_accel": 0.05,
    "breadth": 0.10,
}


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _cfg(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


def enabled() -> bool:
    return (os.getenv("TOKEN_SURGE_DISCOVERY_ENABLED", "true").strip().lower()
            not in ("0", "false", "no", "off"))


def min_liquidity_usd() -> float:
    return _cfg("TOKEN_SURGE_MIN_LIQUIDITY_USD", 25_000.0)


def min_volume_usd() -> float:
    return _cfg("TOKEN_SURGE_MIN_VOLUME_USD", 5_000.0)


def surge_threshold() -> float:
    return _cfg("TOKEN_SURGE_MIN_SCORE", 70.0)


def extreme_threshold() -> float:
    return _cfg("TOKEN_SURGE_EXTREME_SCORE", 90.0)


def weights() -> dict:
    raw = os.getenv("TOKEN_SURGE_WEIGHTS", "").strip()
    if not raw:
        return dict(DEFAULT_WEIGHTS)
    try:
        got = json.loads(raw)
        return {k: float(got.get(k, v)) for k, v in DEFAULT_WEIGHTS.items()}
    except (ValueError, TypeError):
        logger.warning("[TokenSurge] TOKEN_SURGE_WEIGHTS unparseable — using defaults")
        return dict(DEFAULT_WEIGHTS)


def snapshot_from_pool(pool: dict) -> dict | None:
    """Flatten one GeckoTerminal pool into a storable observation."""
    a = pool.get("attributes") or {}
    rel = pool.get("relationships") or {}
    token_id = ((rel.get("base_token") or {}).get("data") or {}).get("id") or ""
    mint = token_id.split("_", 1)[1] if "_" in token_id else ""
    if not mint:
        return None
    vol = a.get("volume_usd") or {}
    txn = a.get("transactions") or {}
    chg = a.get("price_change_percentage") or {}
    m5, h1 = txn.get("m5") or {}, txn.get("h1") or {}
    return {
        "mint": mint, "pool_address": a.get("address"), "symbol": a.get("name"),
        "price_usd": _f(a.get("base_token_price_usd")),
        "liquidity_usd": _f(a.get("reserve_in_usd")),
        "volume_m5": _f(vol.get("m5")), "volume_m15": _f(vol.get("m15")),
        "volume_m30": _f(vol.get("m30")), "volume_h1": _f(vol.get("h1")),
        "volume_h6": _f(vol.get("h6")), "volume_h24": _f(vol.get("h24")),
        "buys_m5": int(_f(m5.get("buys"))), "sells_m5": int(_f(m5.get("sells"))),
        "buyers_m5": int(_f(m5.get("buyers"))), "sellers_m5": int(_f(m5.get("sellers"))),
        "buys_h1": int(_f(h1.get("buys"))), "sells_h1": int(_f(h1.get("sells"))),
        "buyers_h1": int(_f(h1.get("buyers"))), "sellers_h1": int(_f(h1.get("sellers"))),
        "price_change_m5": _f(chg.get("m5")), "price_change_h1": _f(chg.get("h1")),
        "price_change_h6": _f(chg.get("h6")), "price_change_h24": _f(chg.get("h24")),
    }


def _median(vals: list[float], floor: float) -> float:
    clean = [v for v in vals if v is not None]
    if not clean:
        return floor
    return max(statistics.median(clean), floor)


def baseline_from(history: list[dict]) -> dict | None:
    """Median 5m activity over the token's own recent snapshots.

    Median rather than mean deliberately: one earlier spike inside the
    window would pull a mean up and mask the surge being hunted.
    """
    if len(history) < MIN_SNAPSHOTS_FOR_BASELINE:
        return None
    return {
        "volume_m5": _median([h.get("volume_m5") for h in history], VOLUME_FLOOR_USD),
        "txns_m5": _median([(_f(h.get("buys_m5")) + _f(h.get("sells_m5")))
                            for h in history], TXN_FLOOR),
        "wallets_m5": _median([(_f(h.get("buyers_m5")) + _f(h.get("sellers_m5")))
                               for h in history], WALLET_FLOOR),
        "liquidity_usd": _median([h.get("liquidity_usd") for h in history], 1.0),
        "samples": len(history),
    }


def _ratio_to_points(r: float) -> float:
    """Map an acceleration ratio onto 0-100 with diminishing returns.

    Linear scaling would let one 500x ratio on a dust token dominate every
    other component. 1x scores 0, 3x about 50, 10x about 78, 50x about 96.
    """
    import math
    if r <= 1:
        return 0.0
    return min(100.0, 100.0 * math.log10(r) / math.log10(60.0))


def score_snapshot(current: dict, history: list[dict]) -> dict:
    """The surge score, its bias, and every input that produced them."""
    w = weights()
    base = baseline_from(history)

    liq = _f(current.get("liquidity_usd"))
    v_m5 = _f(current.get("volume_m5"))
    buys, sells = _f(current.get("buys_m5")), _f(current.get("sells_m5"))
    buyers, sellers = _f(current.get("buyers_m5")), _f(current.get("sellers_m5"))
    txns = buys + sells
    wallets = buyers + sellers

    out = {
        "mint": current.get("mint"), "symbol": current.get("symbol"),
        "liquidity_usd": liq, "volume_m5": v_m5,
        "buys_m5": int(buys), "sells_m5": int(sells),
        "buyers_m5": int(buyers), "sellers_m5": int(sellers),
        "volume_accel": None, "txn_accel": None, "wallet_accel": None,
        "liquidity_accel": None,
        "buy_pressure": (buys / txns) if txns else None,
        "surge_score": 0.0, "bias": "unknown",
        "baseline_quality": "insufficient", "baseline_samples": len(history),
        "reasons": [],
    }

    # Absolute significance gates. Relative acceleration alone would rank a
    # $50 -> $5,000 dust pool above a genuinely liquid opportunity.
    if liq < min_liquidity_usd():
        out["reasons"].append(
            f"liquidity ${liq:,.0f} below the ${min_liquidity_usd():,.0f} floor")
        return out
    if v_m5 < (min_volume_usd() / 12.0):
        out["reasons"].append(
            f"5m volume ${v_m5:,.0f} too small to be significant")
        return out

    if base is None:
        # NEW TOKEN: no history is not the same as no activity, and
        # penalising it for being new would miss every launch. Scored on
        # absolute breadth instead, and labelled so nothing downstream
        # mistakes this for a measured baseline.
        out["baseline_quality"] = "new_token"
        breadth = min(100.0, (wallets / 50.0) * 100.0)
        vol_pts = min(100.0, (v_m5 / 25_000.0) * 100.0)
        score = 0.5 * breadth + 0.5 * vol_pts
        out["surge_score"] = round(min(score, 85.0), 2)   # capped: unproven
        out["reasons"].append(
            f"only {len(history)} snapshot(s) — scored on absolute activity, "
            f"capped at 85 until a baseline exists")
    else:
        out["baseline_quality"] = "measured"
        v_acc = v_m5 / base["volume_m5"]
        t_acc = txns / base["txns_m5"]
        w_acc = wallets / base["wallets_m5"]
        l_acc = liq / base["liquidity_usd"]
        out.update(volume_accel=round(v_acc, 3), txn_accel=round(t_acc, 3),
                   wallet_accel=round(w_acc, 3), liquidity_accel=round(l_acc, 3))

        price_accel = abs(_f(current.get("price_change_m5")))
        breadth_pts = min(100.0, (wallets / max(txns, 1.0)) * 100.0)
        bp = out["buy_pressure"] if out["buy_pressure"] is not None else 0.5

        score = (
            w["volume_accel"] * _ratio_to_points(v_acc)
            + w["txn_accel"] * _ratio_to_points(t_acc)
            + w["wallet_accel"] * _ratio_to_points(w_acc)
            + w["buy_pressure"] * (abs(bp - 0.5) * 200.0)
            + w["liquidity_accel"] * _ratio_to_points(max(l_acc, 1.0))
            + w["price_accel"] * min(100.0, price_accel * 5.0)
            + w["breadth"] * breadth_pts
        )
        out["surge_score"] = round(min(score, 100.0), 2)

    # BIAS is computed separately and never folded into the score above.
    # A dump is a genuine surge; calling it bullish because it is busy is
    # how the activity signal becomes a buy signal for exit liquidity.
    bp = out["buy_pressure"]
    liq_falling = (out.get("liquidity_accel") or 1.0) < 0.9
    c_h1 = _f(current.get("price_change_h1"))
    if bp is None:
        out["bias"] = "unknown"
    elif bp >= 0.62 and c_h1 >= 0 and not liq_falling:
        out["bias"] = "bullish"
    elif bp <= 0.38 or (liq_falling and c_h1 < -5):
        out["bias"] = "bearish"
    else:
        out["bias"] = "mixed"

    # Wash-trading heuristic: lots of transactions, almost no distinct
    # participants. Reduces confidence rather than accusing anyone.
    if txns >= 20 and wallets and (wallets / txns) < 0.15:
        out["surge_score"] = round(out["surge_score"] * 0.5, 2)
        out["reasons"].append(
            f"{int(txns)} trades from only {int(wallets)} wallets — "
            f"low participant breadth, score halved")

    return out


# ═════════════════════════════════════════════════════════════════════════
# THE CANONICAL PIPELINE
#
# Everything above is pure scoring. Everything below is the ONE production
# path, and it exists because there were two definitions of "surge":
#
#   wallet_discovery.surge_metrics()   h1/h6/h24 buckets, NO baseline —
#                                      used by the scheduled discovery pass
#   token_surge.score_snapshot()       measured self-baselines — reachable
#                                      ONLY from the /onchain/surge route,
#                                      and called there as
#                                      `score_snapshot(snap, [])`
#
# The empty-list literal meant the rigorous implementation ran permanently
# in new-token mode: every token scored as though it had no history, which
# is precisely the mode it was built to escape. Meanwhile TokenActivitySnapshot
# and TokenSurgeState were declared in the schema with ZERO writers and ZERO
# readers anywhere in the codebase.
#
# scan_and_score() is now the only way a surge score is produced. Discovery
# and the UI consume the same rows.
# ═════════════════════════════════════════════════════════════════════════

SNAPSHOT_RETENTION_HOURS = 48

# Hysteresis. A token scoring 85 for ten consecutive scans is one surge,
# not ten — the difference between an alert and a stuck alarm.
STATE_NORMAL = "NORMAL"
STATE_SURGING = "SURGING"
STATE_COOLDOWN = "COOLDOWN"
# A surge must fall meaningfully below the threshold before it is over, so
# a score oscillating around the line does not emit an event per scan.
SURGE_EXIT_MARGIN = 10.0


def persist_snapshot(session, snap: dict):
    """Append one observation. Never updated in place — a baseline that
    gets rewritten is not a baseline."""
    from app.database import TokenActivitySnapshot

    row = TokenActivitySnapshot(
        mint=snap.get("mint"), pool_address=snap.get("pool_address"),
        symbol=snap.get("symbol"), network="solana",
        price_usd=snap.get("price_usd"), liquidity_usd=snap.get("liquidity_usd"),
        volume_m5=snap.get("volume_m5"), volume_m15=snap.get("volume_m15"),
        volume_m30=snap.get("volume_m30"), volume_h1=snap.get("volume_h1"),
        volume_h6=snap.get("volume_h6"), volume_h24=snap.get("volume_h24"),
        buys_m5=snap.get("buys_m5"), sells_m5=snap.get("sells_m5"),
        buyers_m5=snap.get("buyers_m5"), sellers_m5=snap.get("sellers_m5"),
        buys_h1=snap.get("buys_h1"), sells_h1=snap.get("sells_h1"),
        buyers_h1=snap.get("buyers_h1"), sellers_h1=snap.get("sellers_h1"),
        price_change_m5=snap.get("price_change_m5"),
        price_change_h1=snap.get("price_change_h1"),
        price_change_h6=snap.get("price_change_h6"),
        price_change_h24=snap.get("price_change_h24"),
    )
    session.add(row)
    return row


def load_history(session, mint: str, *, hours: int = BASELINE_WINDOW_HOURS,
                 exclude_id: str | None = None) -> list[dict]:
    """This token's own recent observations, oldest last.

    `exclude_id` drops the snapshot just written, so the current reading is
    never part of the baseline it is measured against — including it would
    drag the median toward the very spike being detected.
    """
    from app.database import TokenActivitySnapshot

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    q = (session.query(TokenActivitySnapshot)
         .filter(TokenActivitySnapshot.mint == mint)
         .filter(TokenActivitySnapshot.captured_at >= cutoff))
    if exclude_id:
        q = q.filter(TokenActivitySnapshot.id != exclude_id)
    rows = q.order_by(TokenActivitySnapshot.captured_at.desc()).limit(200).all()
    return [{
        "volume_m5": r.volume_m5, "buys_m5": r.buys_m5, "sells_m5": r.sells_m5,
        "buyers_m5": r.buyers_m5, "sellers_m5": r.sellers_m5,
        "liquidity_usd": r.liquidity_usd, "captured_at": r.captured_at,
    } for r in rows]


def _update_state(session, scored: dict) -> dict:
    """Carry the surge state forward, and stamp WHEN it started.

    `surge_started_at` is the T0 pre-surge wallet discovery searches
    backwards from. It is set on the transition into SURGING and cleared on
    the return to NORMAL, so a token that has been surging for two hours
    still reports the moment it began rather than the moment it was
    last scanned.
    """
    from app.database import TokenSurgeState, now_iso

    mint = scored.get("mint")
    if not mint:
        return scored
    row = session.query(TokenSurgeState).filter(
        TokenSurgeState.mint == mint).first()
    if row is None:
        row = TokenSurgeState(mint=mint, first_seen_at=now_iso())
        session.add(row)
        session.flush()

    score = float(scored.get("surge_score") or 0.0)
    now = now_iso()
    was = row.state or STATE_NORMAL

    if score >= surge_threshold():
        if was != STATE_SURGING:
            row.state = STATE_SURGING
            row.surge_started_at = now
            row.last_event_at = now
            row.last_event_score = score
        row.peak_score = max(float(row.peak_score or 0.0), score)
    elif score < (surge_threshold() - SURGE_EXIT_MARGIN):
        if was == STATE_SURGING:
            row.state = STATE_COOLDOWN
        else:
            row.state = STATE_NORMAL
            row.surge_started_at = None
            row.peak_score = 0.0
    # Between the threshold and the exit margin the state is held, which is
    # the hysteresis band.

    row.pool_address = scored.get("pool_address") or row.pool_address
    row.symbol = scored.get("symbol") or row.symbol
    row.surge_score = score
    row.bias = scored.get("bias")
    row.baseline_quality = scored.get("baseline_quality")
    row.metrics_json = json.dumps({k: scored.get(k) for k in (
        "volume_accel", "txn_accel", "wallet_accel", "liquidity_accel",
        "buy_pressure", "liquidity_usd", "volume_m5", "baseline_samples")})
    row.last_scan_at = now
    row.scans = int(row.scans or 0) + 1

    scored["state"] = row.state
    scored["surge_started_at"] = row.surge_started_at
    scored["peak_score"] = row.peak_score
    scored["scans"] = row.scans
    return scored


def prune_snapshots(session, *, hours: int = SNAPSHOT_RETENTION_HOURS) -> int:
    from app.database import TokenActivitySnapshot
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    n = (session.query(TokenActivitySnapshot)
         .filter(TokenActivitySnapshot.captured_at < cutoff).delete())
    return int(n or 0)


def scan_and_score(*, paths=("trending_pools", "new_pools"),
                   limit: int = 100, persist: bool = True) -> dict:
    """THE surge pass. GeckoTerminal -> snapshot -> persist -> baseline ->
    score -> state. Every consumer reads this result.

    `persist=False` exists for read-only callers that must not write (a UI
    poll on a replica, say). It is NOT a second definition: the scoring is
    identical, only the storage is skipped, and `baseline_quality` still
    reports honestly because history is read either way.
    """
    from app.database import get_db
    from lib.geckoterminal import solana_pools

    out, errors, seen = [], [], set()
    with get_db() as session:
        for path in paths:
            try:
                for pool in solana_pools(path, errors=errors):
                    snap = snapshot_from_pool(pool)
                    if not snap or snap["mint"] in seen:
                        continue
                    seen.add(snap["mint"])
                    row_id = None
                    if persist:
                        row = persist_snapshot(session, snap)
                        session.flush()
                        row_id = row.id
                    history = load_history(session, snap["mint"],
                                           exclude_id=row_id)
                    scored = score_snapshot(snap, history)
                    scored["pool_address"] = snap.get("pool_address")
                    if persist:
                        scored = _update_state(session, scored)
                    out.append(scored)
            except Exception as e:
                errors.append(f"{path}: {type(e).__name__}: {str(e)[:120]}")
        if persist:
            try:
                prune_snapshots(session)
            except Exception as e:
                logger.debug(f"[TokenSurge] prune skipped: {e}")

    out.sort(key=lambda s: s.get("surge_score") or 0, reverse=True)
    measured = sum(1 for s in out if s.get("baseline_quality") == "measured")
    return {
        "tokens": out[:max(1, min(limit, 200))],
        "scanned": len(out),
        "measured_baselines": measured,
        "new_tokens": len(out) - measured,
        "errors": errors,
        "thresholds": {"surge": surge_threshold(),
                       "extreme": extreme_threshold()},
        "provenance": ("MEASURED market data; surge score CALCULATED against "
                       "each token's own stored history"),
    }


def surging_tokens(session=None, *, min_score: float | None = None) -> list[dict]:
    """Tokens currently in a surge, with the T0 discovery needs.

    Reads the SAME persisted state the scan writes, so wallet discovery and
    the UI cannot disagree about what is surging.
    """
    from app.database import TokenSurgeState, get_db

    threshold = surge_threshold() if min_score is None else min_score

    def _run(s):
        rows = (s.query(TokenSurgeState)
                .filter(TokenSurgeState.state == STATE_SURGING)
                .filter(TokenSurgeState.surge_score >= threshold)
                .order_by(TokenSurgeState.surge_score.desc()).all())
        return [{
            "mint": r.mint, "pool": r.pool_address, "symbol": r.symbol,
            "surge_score": r.surge_score, "peak_score": r.peak_score,
            "bias": r.bias, "baseline_quality": r.baseline_quality,
            "surge_started_at": r.surge_started_at,
            "last_scan_at": r.last_scan_at, "scans": r.scans,
            "metrics": json.loads(r.metrics_json) if r.metrics_json else {},
        } for r in rows]

    if session is not None:
        return _run(session)
    with get_db() as db:
        return _run(db)
