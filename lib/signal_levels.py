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
    min_distance_pct = atr_pct * MIN_STOP_ATR_MULT
    max_distance_pct = atr_pct * MAX_STOP_ATR_MULT

    target_distance_pct = None
    if stop_distance_pct < min_distance_pct:
        target_distance_pct = min_distance_pct
        reason = f"stop {stop_distance_pct:.2f}% < {MIN_STOP_ATR_MULT}x ATR ({atr_pct:.2f}%) — widened"
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
