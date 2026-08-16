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

# Directions that affirmatively mean "long". Strict parsing requires a
# POSITIVE match on one of these — absence of "short" is not evidence of
# "long", it is evidence of a malformed direction.
_LONG_MARKERS = ("long", "bull", "call", "buy", "bounce", "breakout_up")


def parse_side_strict(direction: str | None) -> str | None:
    """LONG, SHORT, or None — and None means REFUSE, never assume.

    Order creation must use this. The permissive normalize_side below
    turns any unrecognized string — a typo, an empty field, a new LLM
    phrasing nobody mapped — into a LONG position. That is a fine reading
    rule for legacy display rows; as an order path it means garbage input
    buys things. Unknown direction on an order is a validation error, not
    a long.
    """
    d = str(direction or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not d:
        return None
    has_short = any(m in d for m in _SHORT_MARKERS)
    has_long = any(m in d for m in _LONG_MARKERS)
    # BOTH markers is ambiguous, not short. Checking short first and
    # returning on the first hit silently resolved "longshort" — and any
    # future phrasing that mentions both sides — to SHORT. An input that
    # names two sides has not stated one.
    if has_short and has_long:
        return None
    if has_short:
        return SHORT
    if has_long:
        return LONG
    return None


def normalize_side(direction: str | None) -> str:
    """Any direction string -> LONG or SHORT. Unknown defaults to LONG.

    PERMISSIVE — for reading legacy rows and display only. Every order
    path (live executor, paper engine, auto sim) must use
    parse_side_strict and treat None as NO_TRADE."""
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
    """(ok, reason). Checks the LAYOUT for the stated side — never repairs it.

    STRICT on direction. This used to call the permissive `is_short()`,
    which turns any unrecognised string into a LONG — so a validator whose
    entire job is to refuse malformed input would happily validate
    `direction="Aggressive_Moon_Mode"` against long-side geometry and pass
    it. A validation function must not contain the repair it exists to
    prevent.
    """
    try:
        entry, stop, target = float(entry or 0), float(stop or 0), float(target or 0)
    except (TypeError, ValueError):
        return False, "non-numeric price level"
    if entry <= 0 or stop <= 0 or target <= 0:
        return False, "missing or non-positive price level"

    side = parse_side_strict(direction)
    if side is None:
        return False, (f"unparseable direction {direction!r} — a side cannot be "
                       f"assumed, and an unknown one must never buy")

    if side == SHORT:
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
    """Cheap layout check used by callers that only hold a stop.

    STRICT, for the same reason `validate_levels` is: an unknown direction
    has no correct stop side, so the honest answer is False rather than
    "assume long and check against that".
    """
    side = parse_side_strict(direction)
    if side is None:
        return False
    if side == SHORT:
        return float(stop) > float(entry)
    return float(stop) < float(entry)
