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

# ── Vocabulary ───────────────────────────────────────────────────────────
# RECOGNISABLE DIRECTIONAL TEXT IS NOT THE SAME THING AS UNAMBIGUOUS
# DIRECTIONAL EVIDENCE. That is the whole rule this module enforces.
#
# `LONGSHORT` contains "short". `BUYSELL` contains "buy". Substring matching
# resolves both to a confident side, and the side it picks is decided by
# which test happens to run first — so reordering the checks changes the
# answer without changing the input. Ordering cannot fix this; it only
# changes which wrong answer wins. Conflicting evidence has to be DETECTED
# and refused.
#
# Exact token aliases are the primary mechanism. Substring markers remain
# as a fallback for decorated forms ("Bearish", "breakout_up") and are
# scanned for BOTH sides before either is believed.

# Whole-token aliases, matched after splitting on separators.
_LONG_ALIASES = frozenset({
    "long", "buy", "bull", "bullish", "call", "bounce", "up", "l",
})
_SHORT_ALIASES = frozenset({
    "short", "sell", "bear", "bearish", "put", "down", "s",
})

# Substring markers — the fallback for decorated words no token match
# reaches. "sell" is included here and in the alias set: it was absent
# from BOTH, so `BUYSELL` and `SELLBUY` resolved to LONG on the strength
# of "buy" with nothing to contradict it.
_SHORT_MARKERS = ("short", "bear", "put", "sell")
_LONG_MARKERS = ("long", "bull", "call", "buy", "bounce", "breakout_up")

# Decorations that are never directional on their own.
_NOISE_TOKENS = frozenset({
    "leveraged", "leverage", "x", "2x", "3x", "5x", "10x", "20x", "25x",
    "50x", "100x", "mode", "entry", "position", "trade", "signal", "",
})

# Provenance labels, so a caller can tell an explicit alias from a guess
# and an unknown value from a contradictory one.
SRC_EXACT = "explicit_alias"
SRC_TOKEN = "token_alias"
SRC_SUBSTRING = "substring_marker"
SRC_AMBIGUOUS = "ambiguous_raw_value"
SRC_UNRECOGNISED = "unrecognised_raw_value"
SRC_EMPTY = "empty"


def _split_tokens(d: str) -> list[str]:
    import re
    return [t for t in re.split(r"[^a-z0-9]+", d) if t]


def parse_side_detailed(direction: str | None) -> dict:
    """Canonical direction parse WITH provenance.

    Returns {"side": LONG|SHORT|None, "source": <SRC_*>, "raw": <input>,
             "ambiguous": bool, "evidence": [...]}.

    `side is None` means REFUSE. `ambiguous` separates "this value named
    two sides" from "this value named none" — they need different handling
    upstream, because the first is a data-quality defect worth surfacing
    and the second is usually just an empty field.
    """
    raw = direction
    d = str(direction or "").strip().lower()
    if not d:
        return {"side": None, "source": SRC_EMPTY, "raw": raw,
                "ambiguous": False, "evidence": []}

    # 1. Exact whole-value alias — the documented structured values.
    flat = d.replace("-", "_").replace(" ", "_").replace("/", "_")
    if flat in _LONG_ALIASES:
        return {"side": LONG, "source": SRC_EXACT, "raw": raw,
                "ambiguous": False, "evidence": [flat]}
    if flat in _SHORT_ALIASES:
        return {"side": SHORT, "source": SRC_EXACT, "raw": raw,
                "ambiguous": False, "evidence": [flat]}

    # 2. Token aliases. `buy_maybe` -> {buy, maybe} -> LONG, because "buy"
    #    is an intentionally supported long alias and "maybe" is not
    #    directional. `long_short` -> {long, short} -> two sides -> refuse.
    tokens = _split_tokens(flat)
    tok_long = sorted({t for t in tokens if t in _LONG_ALIASES})
    tok_short = sorted({t for t in tokens if t in _SHORT_ALIASES})
    if tok_long and tok_short:
        return {"side": None, "source": SRC_AMBIGUOUS, "raw": raw,
                "ambiguous": True, "evidence": tok_long + tok_short}
    if tok_long:
        return {"side": LONG, "source": SRC_TOKEN, "raw": raw,
                "ambiguous": False, "evidence": tok_long}
    if tok_short:
        return {"side": SHORT, "source": SRC_TOKEN, "raw": raw,
                "ambiguous": False, "evidence": tok_short}

    # 3. Substring fallback for decorated words, scanning BOTH sides before
    #    believing either. This is where LONGSHORT and BUYSELL are caught:
    #    they are one token, match no alias, and contain both vocabularies.
    sub_long = [m for m in _LONG_MARKERS if m in flat]
    sub_short = [m for m in _SHORT_MARKERS if m in flat]
    if sub_long and sub_short:
        return {"side": None, "source": SRC_AMBIGUOUS, "raw": raw,
                "ambiguous": True, "evidence": sub_long + sub_short}
    if sub_long:
        return {"side": LONG, "source": SRC_SUBSTRING, "raw": raw,
                "ambiguous": False, "evidence": sub_long}
    if sub_short:
        return {"side": SHORT, "source": SRC_SUBSTRING, "raw": raw,
                "ambiguous": False, "evidence": sub_short}

    return {"side": None, "source": SRC_UNRECOGNISED, "raw": raw,
            "ambiguous": False, "evidence": []}


def is_ambiguous_direction(direction: str | None) -> bool:
    """True when the value named MORE THAN ONE side.

    Distinct from "unknown": these rows must be excluded from P&L
    reconstruction, win/loss counts, qualified-trade sets, smart-money
    scoring, alpha entry observations and confluence — and they are worth
    reporting, because a stream producing them has a real defect upstream.
    """
    return parse_side_detailed(direction)["ambiguous"]


def parse_side_strict(direction: str | None) -> str | None:
    """LONG, SHORT, or None — and None means REFUSE, never assume.

    Order creation must use this. The permissive normalize_side below
    turns any unrecognized string — a typo, an empty field, a new LLM
    phrasing nobody mapped — into a LONG position. That is a fine reading
    rule for legacy display rows; as an order path it means garbage input
    buys things. Unknown direction on an order is a validation error, not
    a long.
    """
    return parse_side_detailed(direction)["side"]


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
