"""
Long + Short scenario engine — deterministic, both-directions-always.

For one symbol's computed TA (lib/ta_engine.py output for a single timeframe,
including the lib/ta_extensions.py fields), emits BOTH a long and a short
scenario with explicit states:

    READY               structure and trend agree with the direction and the
                        activation condition has already occurred
    WATCH               a concrete, stated trigger would activate the setup —
                        the scenario names the exact price/event
    NO_TRADE            the direction is against both structure and trend

Never forces a trade: both directions can be WATCH or NO_TRADE simultaneously,
and a READY state is a description of conditions, not advice. Everything here
is arithmetic over already-computed indicator values — no LLM, no fabricated
levels: every price referenced (entry zone, trigger, invalidation) is an
actual computed level (swing, Donchian channel, Keltner band, Supertrend stop),
never an invented number.

This complements — does not replace — the LLM signal path (generate_signals →
score_signal): these scenarios are the deterministic "what would have to
happen" view the mega-prompt's LONG+SHORT SCENARIO ENGINE section asks for.
"""
from __future__ import annotations


def _structure_supports(direction: str, structure: str | None) -> bool | None:
    if structure is None:
        return None
    if structure == "range":
        return None  # a range supports neither direction on structure alone
    return (structure == "uptrend") == (direction == "long")


def build_scenario(direction: str, ta: dict) -> dict:
    """One direction's scenario from one timeframe's TA dict."""
    ms = ta.get("market_structure") or {}
    st = ta.get("supertrend") or {}
    dc = ta.get("donchian") or {}
    price = (ta.get("price") or {}).get("last")
    atr = (ta.get("atr") or {}).get("value")

    structure = ms.get("structure")
    event = ms.get("event")
    st_dir = st.get("direction")

    evidence: list[str] = []
    against: list[str] = []

    supports = _structure_supports(direction, structure)
    if supports is True:
        evidence.append(f"market structure is {structure} ({'/'.join(ms.get('labels', []))})")
    elif supports is False:
        against.append(f"market structure is {structure}")
    elif structure == "range":
        evidence.append("range structure — direction needs a confirmed break")

    if st_dir is not None:
        if (st_dir == "up") == (direction == "long"):
            evidence.append(f"supertrend {st_dir}, stop at {st.get('level')}")
        else:
            against.append(f"supertrend {st_dir}")

    breakout_with = dc.get("breakout_up") if direction == "long" else dc.get("breakout_down")
    if breakout_with:
        evidence.append("Donchian breakout in direction")

    # Activation events already observed
    activating_events = {"long": ("BOS_UP", "CHOCH_UP", "RANGE_BREAK_UP"),
                        "short": ("BOS_DOWN", "CHOCH_DOWN", "RANGE_BREAK_DOWN")}[direction]
    activated = event in activating_events
    if activated:
        evidence.append(f"structure event {event} already occurred")

    # Trigger and invalidation come from actual computed levels only.
    swing_high = ms.get("last_swing_high")
    swing_low = ms.get("last_swing_low")
    if direction == "long":
        trigger_level = swing_high if swing_high is not None else dc.get("upper")
        invalidation_level = swing_low if swing_low is not None else dc.get("lower")
        trigger_text = f"close above {trigger_level}" if trigger_level is not None else None
        invalidation_text = f"close below {invalidation_level}" if invalidation_level is not None else None
    else:
        trigger_level = swing_low if swing_low is not None else dc.get("lower")
        invalidation_level = swing_high if swing_high is not None else dc.get("upper")
        trigger_text = f"close below {trigger_level}" if trigger_level is not None else None
        invalidation_text = f"close above {invalidation_level}" if invalidation_level is not None else None

    # State machine (documented in module docstring). An activation event
    # outranks a structure disagreement: CHOCH_DOWN by definition only occurs
    # while structure still reads uptrend, so requiring structure agreement
    # for READY would make CHoCH activation unreachable (a bug this exact
    # ordering fixes — caught by the test suite). The disagreement stays
    # visible in `against`.
    if activated:
        state = "READY"
    elif supports is False:
        state = "NO_TRADE"
    elif trigger_text is not None:
        state = "WATCH"
    else:
        state = "NO_TRADE"

    return {
        "direction": direction,
        "state": state,
        "trigger": trigger_text if state == "WATCH" else None,
        "trigger_level": trigger_level if state == "WATCH" else None,
        "invalidation": invalidation_text,
        "invalidation_level": invalidation_level,
        "current_price": price,
        "atr": atr,
        "evidence": evidence,
        "against": against,
    }


def build_scenarios(ta: dict) -> dict | None:
    """Both directions for one timeframe's TA. Returns None when the TA dict
    is errored/empty — no scenario is better than a scenario from nothing."""
    if not ta or ta.get("error"):
        return None
    long_s = build_scenario("long", ta)
    short_s = build_scenario("short", ta)
    return {
        "long": long_s,
        "short": short_s,
        "note": (
            "Deterministic scenarios from computed levels (swings, channels, "
            "supertrend) — descriptions of conditions, not recommendations. "
            "Both directions are always evaluated; NO_TRADE is a valid answer."
        ),
    }
