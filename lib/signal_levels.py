"""Direction and distance checks for signal entry, stop, and target levels."""

from __future__ import annotations

import math
from lib import trade_side


SCALP_TIMEFRAMES = {"1m", "3m", "5m", "15m", "30m"}

# A stop closer than this many ATRs from entry sits inside normal price noise
# for the symbol and risks getting stopped out by chop rather than a real
# thesis break. A stop farther than this many ATRs risks far more capital
# than the setup's own volatility calls for.
MIN_STOP_ATR_MULT = 0.5
MAX_STOP_ATR_MULT = 5.0


def clamp_stop_to_atr(signal: dict, atr_pct: float | None) -> tuple[dict, bool, str]:
    """Widen or tighten an LLM/scanner-picked stop_loss so its distance from
    entry falls within a sane multiple of the symbol's own ATR%, instead of
    trusting whatever flat distance the signal happened to carry. A quiet
    stock and a wild one otherwise get sized identically even though the
    same dollar stop distance means very different things for each.

    Returns (signal, clamped, reason). `signal` is mutated in place and
    also returned for convenience; `clamped` is True if stop_loss changed.
    """
    try:
        entry = float(signal.get("entry_price") or 0)
        stop = float(signal.get("stop_loss") or 0)
    except (TypeError, ValueError):
        return signal, False, "non-numeric levels"

    if not entry or not stop or not atr_pct or atr_pct <= 0:
        return signal, False, "missing entry/stop/atr"

    is_short = trade_side.is_short(signal.get("direction"))
    stop_distance_pct = abs(entry - stop) / entry * 100.0
    max_distance_pct = atr_pct * MAX_STOP_ATR_MULT

    # A stop must clear TWO floors, whichever is higher:
    #   1. volatility  — at least MIN_STOP_ATR_MULT of the symbol's own ATR,
    #      so it is not sitting inside normal noise;
    #   2. economics   — wide enough that the round-trip spread, fees and
    #      slippage stay under the cost ceiling. Measured on the live book,
    #      this was the binding constraint nobody was checking: a 0.3%
    #      crypto stop carries ~1.0% of round-trip cost, i.e. 3.4R of cost
    #      before the trade can be right about anything. Sixty of two
    #      hundred live signals failed it.
    atr_floor_pct = atr_pct * MIN_STOP_ATR_MULT
    cost_floor_pct = 0.0
    try:
        from lib.transaction_costs import min_viable_stop_pct
        cost_floor_pct = min_viable_stop_pct(
            signal.get("asset_symbol") or "",
            order_type=signal.get("order_type", "market"),
        ) * 100.0
    except Exception:
        cost_floor_pct = 0.0
    min_distance_pct = max(atr_floor_pct, cost_floor_pct)
    binding = "cost" if cost_floor_pct > atr_floor_pct else "ATR"

    # The two floors can CONTRADICT: when the cost floor exceeds the widest
    # stop the symbol's own volatility justifies, there is no stop that is
    # both economically viable and structurally sane. Widening anyway would
    # manufacture a fantasy trade — a "5m scalp" needing a 6% target that
    # its timeframe will never deliver. The honest answer is that this
    # symbol cannot be traded profitably on this timeframe right now.
    if cost_floor_pct > max_distance_pct:
        signal["untradeable_reason"] = (
            f"costs need a {cost_floor_pct:.2f}% stop but {MAX_STOP_ATR_MULT}x ATR "
            f"is only {max_distance_pct:.2f}% — no viable stop exists at this "
            f"volatility; the spread and fees exceed the move available"
        )
        return signal, False, signal["untradeable_reason"]

    target_distance_pct = None
    if stop_distance_pct < min_distance_pct:
        target_distance_pct = min_distance_pct
        reason = (f"stop {stop_distance_pct:.2f}% < {min_distance_pct:.2f}% floor "
                  f"({binding}-bound; ATR {atr_pct:.2f}%, cost {cost_floor_pct:.2f}%) — widened")
    elif stop_distance_pct > max_distance_pct:
        target_distance_pct = max_distance_pct
        reason = f"stop {stop_distance_pct:.2f}% > {MAX_STOP_ATR_MULT}x ATR ({atr_pct:.2f}%) — tightened"
    else:
        return signal, False, "within ATR band"

    precision = 6 if entry < 1 else 2
    if is_short:
        new_stop = round(entry * (1 + target_distance_pct / 100.0), precision)
    else:
        new_stop = round(entry * (1 - target_distance_pct / 100.0), precision)
    # Preserve the setup's reward-to-risk when the stop moves. Widening the
    # stop alone would quietly convert a 3:1 into a 1:1 — the trade the
    # operator reviewed would not be the trade that executes.
    try:
        old_risk = abs(entry - stop)
        old_target = float(signal.get("target_price") or 0)
        if old_risk > 0 and old_target > 0:
            rr = abs(old_target - entry) / old_risk
            new_risk = abs(entry - new_stop)
            if is_short:
                signal["target_price"] = round(entry - new_risk * rr, precision)
            else:
                signal["target_price"] = round(entry + new_risk * rr, precision)
            reason += f", target moved to hold {rr:.2f}:1"
    except (TypeError, ValueError):
        pass

    signal["stop_loss"] = new_stop
    return signal, True, reason


def validate_signal_levels(signal: dict) -> tuple[bool, str]:
    try:
        entry = float(signal.get("entry_price") or 0)
        target = float(signal.get("target_price") or 0)
        stop = float(signal.get("stop_loss") or 0)
    except (TypeError, ValueError):
        return False, "non-numeric price level"

    if not all(math.isfinite(value) and value > 0 for value in (entry, target, stop)):
        return False, "price levels must be finite and positive"

    is_short = trade_side.is_short(signal.get("direction"))
    if is_short and not target < entry < stop:
        return False, "short requires target < entry < stop"
    if not is_short and not stop < entry < target:
        return False, "long requires stop < entry < target"

    timeframe = str(signal.get("timeframe") or "").lower()
    max_target_distance = 0.20 if timeframe in SCALP_TIMEFRAMES else 0.60
    max_stop_distance = 0.12 if timeframe in SCALP_TIMEFRAMES else 0.35
    target_distance = abs(target - entry) / entry
    stop_distance = abs(stop - entry) / entry
    if target_distance > max_target_distance:
        return False, f"target is {target_distance:.1%} from entry"
    if stop_distance > max_stop_distance:
        return False, f"stop is {stop_distance:.1%} from entry"
    return True, ""
