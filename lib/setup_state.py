"""A setup is one thing that moves through states, not a new signal every cycle.

The scanner re-derived every setup from scratch on each pass and wrote a new
row. Measured earlier in this codebase: 12,845 of 39,235 signals were
duplicate regenerations of setups that already existed. That is not a
cosmetic problem.

  - The same setup counted many times in every statistic computed from the
    signals table, so calibration was weighting one idea by how many scan
    cycles it survived.
  - "Superseded" churn made it impossible to ask how long a setup took to
    trigger, or how often one was abandoned before it did.
  - An operator watching a level approach saw a stream of near-identical
    cards rather than one thing getting closer.

A setup has an identity — symbol, direction, timeframe, strategy, and the
level it is about — and a state that advances:

    WATCHING     the premise exists but price is nowhere near the trigger
    APPROACHING  price is closing on the level
    TRIGGERED    the level has been taken
    CONFIRMED    the break held rather than failing or sweeping
    RETESTING    price has come back to the broken level
    ENTRY_READY  the entry condition is met right now
    INVALIDATED  the premise is gone — a terminal state, not a pause
    EXPIRED      it never resolved inside its own horizon

Transitions are deterministic and one-way except where the chart genuinely
allows a step back (a retest that fails returns to WATCHING rather than
pretending the setup is intact). INVALIDATED and EXPIRED are terminal: a
setup that comes back is a NEW setup, because the thing that made the
original one interesting has demonstrably stopped being true.
"""
from __future__ import annotations

import hashlib
import logging

logger = logging.getLogger(__name__)

WATCHING = "WATCHING"
APPROACHING = "APPROACHING"
TRIGGERED = "TRIGGERED"
CONFIRMED = "CONFIRMED"
RETESTING = "RETESTING"
ENTRY_READY = "ENTRY_READY"
INVALIDATED = "INVALIDATED"
EXPIRED = "EXPIRED"

STATES = (WATCHING, APPROACHING, TRIGGERED, CONFIRMED, RETESTING,
          ENTRY_READY, INVALIDATED, EXPIRED)
TERMINAL = (INVALIDATED, EXPIRED)

# Where each state may go next. Anything not listed is refused, so a bug
# cannot walk a setup from WATCHING straight to ENTRY_READY and skip the
# trigger that was supposed to justify it.
ALLOWED = {
    WATCHING:    (APPROACHING, TRIGGERED, INVALIDATED, EXPIRED),
    APPROACHING: (WATCHING, TRIGGERED, INVALIDATED, EXPIRED),
    TRIGGERED:   (CONFIRMED, INVALIDATED, EXPIRED),
    CONFIRMED:   (RETESTING, ENTRY_READY, INVALIDATED, EXPIRED),
    RETESTING:   (ENTRY_READY, WATCHING, INVALIDATED, EXPIRED),
    ENTRY_READY: (CONFIRMED, INVALIDATED, EXPIRED),
    INVALIDATED: (),
    EXPIRED:     (),
}

# How close, in ATR, price must be to the trigger level to be APPROACHING.
# In ATR because 1% away is imminent on a quiet equity and remote on an alt.
APPROACH_ATR = 2.0

# Within this distance of the level after a confirmed break, price is
# retesting it rather than having simply not left yet.
RETEST_ATR = 0.75


def _f(v, default=None):
    try:
        if v is None or isinstance(v, bool):
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def setup_key(symbol: str, direction: str, timeframe: str, strategy: str | None,
              level: float | None) -> str:
    """Stable identity for a setup.

    The level is rounded to four significant figures before hashing. Without
    that, a level recomputed as 63,860.61 instead of 63,860.60 is a
    different setup, and the deduplication this exists for silently stops
    working — which is exactly how the duplicate regenerations happened.
    """
    lv = _f(level)
    if lv is not None and lv != 0:
        import math
        digits = 4 - int(math.floor(math.log10(abs(lv)))) - 1
        lv = round(lv, max(-6, digits))
    raw = f"{str(symbol).upper()}|{str(direction).lower()}|{timeframe}|" \
          f"{strategy or 'unclassified'}|{lv}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def can_transition(current: str, nxt: str) -> bool:
    if current == nxt:
        return True
    return nxt in ALLOWED.get(current, ())


def evaluate(current: str | None, *, ta: dict, direction: str,
             level: float | None, bars_elapsed: int = 0,
             max_bars: int | None = None) -> dict:
    """The state this setup is in now, given the chart.

    Deliberately reads only the deterministic structure output — a state
    machine driven by a model's opinion would move on rewording rather than
    on price.
    """
    current = current if current in STATES else WATCHING
    if current in TERMINAL:
        return {"state": current, "changed": False, "reason": "terminal"}

    if max_bars is not None and bars_elapsed > max_bars:
        return _to(current, EXPIRED,
                   f"{bars_elapsed} bars without resolving (limit {max_bars})")

    is_short = str(direction or "").lower().startswith("short")
    # Each nested block is defended separately. A malformed TA payload must
    # leave the setup where it was, not crash the scan that was checking a
    # hundred other setups behind it.
    d = ta if isinstance(ta, dict) else {}
    structure = d.get("structure")
    structure = structure if isinstance(structure, dict) else {}
    breaks = structure.get("breaks")
    breaks = breaks if isinstance(breaks, list) else []
    lv = _f(level)

    # A break of OUR level, in OUR direction.
    ours = None
    for b in (x for x in breaks if isinstance(x, dict)):
        if lv is not None and _f(b.get("level_price")) is not None:
            if abs(_f(b["level_price"]) - lv) / max(abs(lv), 1e-9) > 0.005:
                continue
        if (b.get("direction") == "down") == is_short:
            ours = b
            break

    if ours:
        outcome = ours.get("outcome")
        if outcome == "held":
            atrs = d.get("atr_distances")
            atrs = atrs if isinstance(atrs, dict) else {}
            back = _f(atrs.get("to_support") if not is_short else atrs.get("to_resistance"))
            if back is not None and abs(back) <= RETEST_ATR and current in (CONFIRMED, RETESTING):
                return _to(current, RETESTING,
                           f"price back within {abs(back):.2f} ATR of the broken level")
            if current in (WATCHING, APPROACHING):
                return _to(current, TRIGGERED, f"took {ours['level_price']:g}")
            if current == TRIGGERED:
                return _to(current, CONFIRMED, "break held")
            if current == RETESTING:
                return _to(current, ENTRY_READY, "retest holding — entry is defined")
            return {"state": current, "changed": False, "reason": "break still holding"}
        if outcome in ("failed", "sweep"):
            # The premise was that this level would give way. It did not.
            return _to(current, INVALIDATED,
                       f"{outcome} at {ours['level_price']:g} — the break did not hold")

    # No break yet: how close are we?
    atrs = d.get("atr_distances")
    atrs = atrs if isinstance(atrs, dict) else {}
    to_level = _f(atrs.get("to_resistance") if not is_short else atrs.get("to_support"))
    if to_level is not None:
        if abs(to_level) <= APPROACH_ATR:
            if current == WATCHING:
                return _to(current, APPROACHING, f"{abs(to_level):.2f} ATR from the level")
            return {"state": current, "changed": False,
                    "reason": f"{abs(to_level):.2f} ATR from the level"}
        if current == APPROACHING:
            return _to(current, WATCHING, f"backed off to {abs(to_level):.2f} ATR")
    return {"state": current, "changed": False, "reason": "no change"}


def _to(current: str, nxt: str, reason: str) -> dict:
    if not can_transition(current, nxt):
        logger.debug(f"[Setup] refused {current} -> {nxt}: {reason}")
        return {"state": current, "changed": False,
                "reason": f"refused transition to {nxt}"}
    return {"state": nxt, "changed": nxt != current, "reason": reason,
            "previous": current}


# Only these states are worth acting on. WATCHING and APPROACHING are
# awareness, not trades — surfacing them as signals is what produced a
# stream of near-identical cards for one idea getting closer.
ACTIONABLE = (ENTRY_READY, CONFIRMED)


def is_actionable(state: str | None) -> bool:
    return state in ACTIONABLE
