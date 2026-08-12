"""What to do when a NEW signal arrives for a symbol already held.

Before this module the answer was "skip it" — the new information was
thrown away, and (because the held-set ignored unfilled orders) sometimes
a duplicate entry was placed instead. Both are wrong: a fresh signal on an
open position is an UPDATE, not a new trade.

Decisions, in order of how much they touch the account:

  AMEND     same direction, better levels -> move the protective orders.
            Stops are RATCHET-ONLY (tighter or unchanged, never looser),
            matching the discipline used everywhere else in this system.
            A looser stop from a newer signal is deliberately ignored.

  CONFLICT  opposite direction. Never acted on blind: the caller runs a
            deep verify on the EXISTING position first, and only closes it
            when the evidence says the position itself is failing
            (INVALIDATED, or the AI disagreeing at high confidence).
            Otherwise it stays and an alert is raised for the operator —
            a contradicting signal is not proof the position is wrong.

  NOOP      nothing materially better to do.

Everything here is pure decision logic over plain dicts so it can be
tested without a broker.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# A level must move by more than this to be worth re-submitting orders for
# (broker churn has a cost, and micro-adjustments add nothing).
MIN_LEVEL_IMPROVEMENT_PCT = 0.25

# How sure the AI must be that an existing position is failing before a
# contradicting signal is allowed to close it.
CONFLICT_CLOSE_CONFIDENCE = 75


def _is_short(direction: str | None) -> bool:
    return str(direction or "").lower().startswith("short")


def _pct_diff(a: float, b: float) -> float:
    """Percentage move from a to b, relative to a."""
    return abs(b - a) / a * 100 if a else 0.0


def classify(signal: dict, position: dict) -> str:
    """AMEND / CONFLICT / NOOP for a new signal against an open position."""
    pos_short = float(position.get("qty") or 0) < 0 or _is_short(position.get("direction"))
    sig_short = _is_short(signal.get("direction"))
    if pos_short != sig_short:
        return "CONFLICT"
    return "AMEND"


def plan_amendment(signal: dict, position: dict) -> dict | None:
    """New protective levels for a same-direction update, or None when the
    new signal offers nothing better.

    Stop: adopted only if TIGHTER than the current one (closer to price on
    the risk side). Target: adopted whenever it moved materially — a target
    is an expectation, not a risk control, so it may move either way.
    """
    pos_short = float(position.get("qty") or 0) < 0 or _is_short(position.get("direction"))
    cur_stop = float(position.get("stop_loss") or 0)
    cur_target = float(position.get("target_price") or 0)
    new_stop = float(signal.get("stop_loss") or 0)
    new_target = float(signal.get("target_price") or 0)

    changes: dict = {}

    if new_stop > 0:
        if cur_stop <= 0:
            changes["stop_loss"] = new_stop          # nothing there — take it
        else:
            # "Tighter" means a HIGHER stop for longs, LOWER for shorts.
            tighter = new_stop < cur_stop if pos_short else new_stop > cur_stop
            if tighter and _pct_diff(cur_stop, new_stop) >= MIN_LEVEL_IMPROVEMENT_PCT:
                changes["stop_loss"] = new_stop
            elif not tighter:
                changes["stop_rejected"] = (
                    f"new stop {new_stop:g} is looser than {cur_stop:g} — ignored (stops ratchet one way)"
                )

    if new_target > 0 and (cur_target <= 0 or _pct_diff(cur_target, new_target) >= MIN_LEVEL_IMPROVEMENT_PCT):
        changes["target_price"] = new_target

    actionable = {k: v for k, v in changes.items() if k in ("stop_loss", "target_price")}
    if not actionable:
        return None
    return {
        "action": "AMEND",
        "changes": actionable,
        "notes": changes.get("stop_rejected"),
        "basis": (
            f"Fresh {signal.get('direction')} signal on an existing position — "
            f"protective levels updated rather than opening a second entry."
        ),
    }


def evaluate_conflict(verification: dict | None) -> dict:
    """Given a deep verify of the EXISTING position, decide whether a
    contradicting signal is allowed to close it."""
    v = verification or {}
    verdict = str(v.get("verdict") or "").upper()
    ai = v.get("llm_assessment") or {}
    stance = str(ai.get("assessment") or "").upper()
    try:
        conf = float(ai.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0

    if verdict == "INVALIDATED":
        return {"close": True, "reason": "position already invalidated at current price"}
    if stance == "DISAGREE" and conf >= CONFLICT_CLOSE_CONFIDENCE:
        return {"close": True, "reason": f"AI rejects the open position at {conf:.0f}% confidence"}
    return {
        "close": False,
        "reason": (
            f"contradicting signal, but the open position still verifies "
            f"({verdict or 'unknown'}{f', AI {stance} {conf:.0f}%' if stance else ''}) — keeping it"
        ),
    }
