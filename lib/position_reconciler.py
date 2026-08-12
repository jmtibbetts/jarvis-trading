"""What to do when a NEW signal arrives for a symbol already held.

Before this module the answer was "skip it" — the new information was
thrown away, and (because the held-set ignored unfilled orders) sometimes
a duplicate entry was placed instead. Both are wrong: a fresh signal on an
open position is an UPDATE, not a new trade.

Decisions, in order of how much they touch the account:

  HOLD      same direction. The position is already expressing this view,
            so the working order is LEFT ALONE — re-signalling a direction
            is not new information worth churning broker orders for. The
            trailing/tier logic in manage_positions keeps adjusting stops
            and take-profits on its own schedule; that is where ongoing
            level management belongs.

  FLIP      the direction SWAPPED. This is the one case that changes the
            order: close the open position and let the opposite-side signal
            enter. Gated by a fast deterministic re-check of the position
            (not the 60-90s deep verify — this runs inside the execution
            loop) so a stale price can't trigger a pointless flip.

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
    """FLIP when the new signal is the opposite side, HOLD otherwise."""
    pos_short = float(position.get("qty") or 0) < 0 or _is_short(position.get("direction"))
    sig_short = _is_short(signal.get("direction"))
    return "FLIP" if pos_short != sig_short else "HOLD"


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


def evaluate_flip(verification: dict | None) -> dict:
    """Should a direction swap actually flip the position?

    Yes by default — a swapped signal IS the trigger to change the order.
    The only refusal is a verdict that says the re-check itself was blind
    (no price available), because flipping on unknown data is guessing.
    """
    v = verification or {}
    verdict = str(v.get("verdict") or "").upper()
    if verdict == "DATA_UNAVAILABLE":
        return {"flip": False, "reason": "no fresh price to verify against — position left alone"}
    if verdict == "INVALIDATED":
        return {"flip": True, "reason": "position already invalidated at the current price"}
    return {"flip": True, "reason": f"signal direction swapped (position re-check: {verdict or 'ok'})"}
