"""One definition of "does this execution describe its plan?" (B0/B2B).

Written for canonical ENTRY in B0; canonical EXIT needs the identical check,
and two definitions of execution agreement is how the two paths drift into
accepting different contradictions. The logic is side-neutral on purpose: it
compares an ExecutionResult against the OrderPlan that produced it, whatever
the order was for.
"""
from __future__ import annotations

import math


def execution_disagreement(plan, execution) -> str | None:
    """Where the ExecutionResult contradicts the plan that produced it.

    Returns a human-readable description of the FIRST disagreement, or None
    when the two describe one order. Checked before economic settlement:
    a contradiction here is not a fill to record, it is a stage that
    re-derived quantity semantics on its own — and the answer to that is
    refusal, never patching the result until it fits.

    Quantities may only relate downward (a venue may shrink, never enlarge);
    unit and multiplier must be THE SAME FACT on both objects wherever the
    plan states them. The multiplier tolerance is representation-only.
    """
    req = float(execution.requested_quantity or 0.0)
    filled = float(execution.filled_quantity or 0.0)
    plan_qty = float(plan.qty)
    if not (math.isfinite(req) and math.isfinite(filled)):
        return (f"execution quantities are not finite "
                f"(requested {execution.requested_quantity!r}, filled "
                f"{execution.filled_quantity!r})")
    if req > plan_qty + 1e-9:
        return (f"execution requested {req:g} against a plan of "
                f"{plan_qty:g} — a venue may shrink an order, never "
                f"enlarge one")
    if filled > req + 1e-9:
        return (f"execution filled {filled:g} of a requested {req:g} — "
                f"a fill larger than its own order is not a fill")
    plan_unit = getattr(plan, "quantity_unit", None)
    if plan_unit is not None and execution.quantity_unit != plan_unit:
        return (f"the plan counts {plan_unit!r} but the execution settled "
                f"in {execution.quantity_unit!r}")
    plan_mult = getattr(plan, "multiplier", None)
    if plan_mult is not None:
        try:
            em = float(execution.multiplier)
        except (TypeError, ValueError):
            em = float("nan")
        if not math.isfinite(em) or abs(em - float(plan_mult)) > \
                1e-12 * max(1.0, abs(float(plan_mult))):
            return (f"the plan's multiplier is {plan_mult!r} but the "
                    f"execution's is {execution.multiplier!r}")
    return None
