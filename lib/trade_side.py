"""Canonical direction (side) semantics for the whole system.

Direction strings arrive from the LLM, the UI, the database, and the paper
engine in many shapes — "Long", "Short_Leveraged", "Long_10x", "Bounce",
"leveraged short" — and each consumer used to re-derive their meaning with
its own `startswith("short")` or `"short" in x`. That duplication is how
long-only geometry leaked into shared code: lib/risk_manager.py validated
every signal with `stop >= entry or target <= entry`, which is the LONG
layout, so every short was rejected as "Invalid price levels" before its
R:R was ever computed.

The geometry, stated once:

    LONG   stop < entry < target      risk is below, reward above
    SHORT  target < entry < stop      reward is below, risk above

Distances are direction-independent ONCE the layout has been validated —
but validation must come first. A malformed signal is never silently
reinterpreted into the other side; it is rejected with a reason, because a
"short" whose stop sits below entry is not a short with a typo, it is a
signal whose author disagreed with itself.
"""
from __future__ import annotations

LONG = "long"
SHORT = "short"

# Directions that mean "short" regardless of decoration (_5x, _Leveraged...).
_SHORT_MARKERS = ("short", "bear", "put")


def normalize_side(direction: str | None) -> str:
    """Any direction string -> LONG or SHORT. Unknown defaults to LONG,
    matching the historical behaviour of every call site this replaces."""
    d = str(direction or "").strip().lower().replace("-", "_").replace(" ", "_")
    return SHORT if any(m in d for m in _SHORT_MARKERS) else LONG


def is_short(direction: str | None) -> bool:
    return normalize_side(direction) == SHORT


def leverage_from_direction(direction: str | None) -> float | None:
    """Explicit multiplier baked into a direction ("Long_10x" -> 10.0), or
    None when the direction carries no instruction."""
    import re
    m = re.search(r"(\d+)\s*x", str(direction or ""), re.I)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    if "leverag" in str(direction or "").lower():
        return 2.0
    return None


def validate_levels(direction: str | None, entry: float, stop: float,
                    target: float) -> tuple[bool, str | None]:
    """(ok, reason). Checks the LAYOUT for the stated side — never repairs it."""
    try:
        entry, stop, target = float(entry or 0), float(stop or 0), float(target or 0)
    except (TypeError, ValueError):
        return False, "non-numeric price level"
    if entry <= 0 or stop <= 0 or target <= 0:
        return False, "missing or non-positive price level"

    if is_short(direction):
        if stop <= entry:
            return False, f"short stop {stop:g} must sit ABOVE entry {entry:g}"
        if target >= entry:
            return False, f"short target {target:g} must sit BELOW entry {entry:g}"
    else:
        if stop >= entry:
            return False, f"long stop {stop:g} must sit BELOW entry {entry:g}"
        if target <= entry:
            return False, f"long target {target:g} must sit ABOVE entry {entry:g}"
    return True, None


def risk_distance(entry: float, stop: float) -> float:
    """Per-unit loss if the stop is hit. Direction-independent by absolute
    value — valid only after validate_levels has passed."""
    return abs(float(entry) - float(stop))


def reward_distance(entry: float, target: float) -> float:
    return abs(float(target) - float(entry))


def rr_ratio(entry: float, stop: float, target: float) -> float:
    """Reward-to-risk. 0.0 when risk distance is zero (degenerate levels)."""
    risk = risk_distance(entry, stop)
    return (reward_distance(entry, target) / risk) if risk > 0 else 0.0


def loss_at_stop(qty: float, entry: float, stop: float) -> float:
    """Dollar loss if the stop fills exactly — the invariant every sizing
    path must respect: loss_at_stop <= allowed account risk."""
    return abs(float(qty)) * risk_distance(entry, stop)


def stop_side_ok(direction: str | None, entry: float, stop: float) -> bool:
    """Cheap layout check used by callers that only hold a stop."""
    if is_short(direction):
        return float(stop) > float(entry)
    return float(stop) < float(entry)
