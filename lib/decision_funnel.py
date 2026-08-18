"""The decisions that ended before canonical entry was ever called.

THE HOLE THIS CLOSES.

`DecisionObservation` is built in exactly one place — inside
`canonical_entry.open_canonical_position`. That is correct for everything
that reaches it, and it means every candidate killed EARLIER left nothing
behind. Two such paths existed in the live paper job:

    no usable price   ->  skipped_no_price += 1 ; continue
    AI rejected       ->  skipped_ai       += 1 ; continue

The AI path is the serious one. It sat immediately before
`open_canonical_position`, so the model's own refusals — the single richest
signal about decision quality we have — were counted and discarded. That is
the original failure of this system reproduced one layer earlier:

    candidate -> NO_TRADE -> forgotten

and it is exactly what left 11,775 historical rejections unanswerable.

ONE EVENT, ONE OBSERVATION. This does NOT write a row per gate. A candidate
accumulates artifacts as it descends, and exactly one row is written at the
point it terminates, carrying the terminal verdict. `observation_id` is
derived from the market event, and the unique index on it means a retried
scheduler cycle resolves to the same row rather than letting one event vote
twice.

JUDGMENT IS WRITTEN ONCE AND NOT REWRITTEN. Nothing here persists a
provisional TRADE to be corrected later; the row is written when the terminal
verdict is known.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Binding reasons for the paths that terminate before canonical entry. Named
# so analytics can separate "we could not see a price" from "the model said
# no" — a DATA problem and a THESIS problem call for opposite responses.
NO_DECISION_PRICE = "NO_DECISION_PRICE"
AI_REJECTED_ENTRY = "AI_REJECTED_ENTRY"
ALREADY_OPEN = "ALREADY_OPEN_FOR_SYMBOL"
AUTO_TRADE_DISABLED = "AUTO_TRADE_DISABLED"


def observe_terminal_refusal(signal: dict, *, decision: str, reason: str,
                             decision_price=None, gates: dict | None = None,
                             source: str | None = None,
                             venue_failure: bool = False,
                             edge=None) -> str | None:
    """Persist the ONE observation for a candidate that ends here.

    Returns the observation id, or None if it could not be recorded. Never
    raises into the caller: losing a candidate is bad, but killing the whole
    paper cycle over an audit-trail write would be worse, and the failure is
    logged rather than swallowed silently.
    """
    from lib import decision_observation as DO
    from lib import runtime_mode as RM

    try:
        src = source or (DO.FORWARD_EVIDENCE_ONLY if RM.is_evidence_only()
                         else DO.FORWARD_REJECTED_OBSERVATION)
        row = DO.build(
            signal=signal, decision=decision, binding_reason=reason,
            decision_price=decision_price, gates=dict(gates or {}),
            source=src, venue_data_failure=venue_failure,
            execution_state=DO.EXEC_NOT_APPLICABLE,
            **_edge_fields(edge))
        return DO.record(row)
    except Exception as e:                    # never break the cycle
        logger.warning("[DecisionFunnel] could not record %s refusal for %s: %s",
                       reason, signal.get("asset_symbol"), e)
        return None


def _edge_fields(edge) -> dict:
    """Carry the ORIGINAL T0 edge measurement, never a later recomputation.

    `MeasuredEdge` is produced once while the decision is being made. Reading
    it here — rather than rerunning expectancy when the row is written — is
    what makes the stored numbers what JARVIS actually knew at T0 instead of
    what today's model would say about yesterday's market.
    """
    if edge is None:
        return {}
    def g(*names):
        for n in names:
            v = getattr(edge, n, None) if not isinstance(edge, dict) else edge.get(n)
            if v is not None:
                return v
        return None
    return {
        "gross_expected_r": g("gross_expected_r"),
        "estimated_cost_r": g("expected_cost_r", "estimated_cost_r"),
        "edge_threshold_r": g("threshold_used", "min_net_r"),
    }
