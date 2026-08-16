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
