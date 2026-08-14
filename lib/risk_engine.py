"""One sizing authority. Live, paper, and Auto Sim all solve here.

Phase 1's unification (implementation plan §5): three books shared the
risk-first DOCTRINE after Phase 0 but not the IMPLEMENTATION — live sized
in dollars inside risk_manager, paper in lib/paper_engine.size_position,
Auto Sim in its own margin arithmetic. Three copies of the same algorithm
is how one of them drifts back into margin-first sizing while the others
don't, and nobody notices until the invariant test that only one book has
fails.

The algorithm, stated once (doc §5):

    validate            entry/stop present, coherent; risk distance > 0
    freeze stop         the stop is an INPUT; nothing here moves it
    risk budget         supplied by the caller — account policy
                        (base % x lifecycle x regime) happens upstream,
                        because policy differs per book; ARITHMETIC does not
    qty by risk         qty = budget / risk_per_unit  (multiplier-aware)
    constrain           venue caps, whole contracts, free-cash financing —
                        constraints only ever SHRINK
    derive leverage     from the stop and the venue (max_safe_leverage);
                        an explicit request is a ceiling, never a floor
    revalidate          loss_at_stop <= budget after every rounding step

Returns a typed RiskDecision (lib/decision_types) with the limiting
constraint named — "which rule bound this trade" is a question every
sizing bug hunt starts with.
"""
from __future__ import annotations

import logging

from lib.decision_types import RiskDecision

logger = logging.getLogger(__name__)


def solve_position(*, entry: float, stop: float, risk_budget_usd: float,
                   free_cash: float, symbol: str = "",
                   requested_leverage: float | None = None,
                   max_margin_frac_of_cash: float = 0.15,
                   notional_cap_usd: float | None = None,
                   whole_units: bool = False,
                   lifecycle_multiplier: float = 1.0) -> RiskDecision:
    """Solve quantity from risk. The single entry point for every book.

    `risk_budget_usd` arrives WITH account policy already applied (base
    risk %, lifecycle, regime) — those are per-book decisions. Everything
    after the budget is shared arithmetic and lives here.
    """
    try:
        entry = float(entry or 0)
        stop = float(stop or 0)
        budget = float(risk_budget_usd or 0)
        free_cash = float(free_cash or 0)
    except (TypeError, ValueError):
        return RiskDecision.rejection("non-numeric sizing inputs")

    if entry <= 0:
        return RiskDecision.rejection("missing or non-positive entry")
    if budget <= 0:
        return RiskDecision.rejection("no risk budget")
    stop_distance = abs(entry - stop) if stop > 0 else 0.0
    if stop_distance <= 0:
        return RiskDecision.rejection("no stop distance — cannot size by risk")

    from lib.instruments import get_spec, is_futures, margin_required, suggest_micro, whole_contracts
    from lib.paper_engine import max_safe_leverage

    spec = get_spec(symbol) if symbol else None
    mult = float(spec.multiplier if spec else 1.0)
    unit_value = entry * mult
    risk_per_unit = stop_distance * mult

    qty = budget / risk_per_unit
    limiting = "risk"

    # Whole-unit instruments (futures contracts, integer shares) round DOWN.
    futures = bool(symbol and is_futures(symbol))
    if futures:
        qty = whole_contracts(symbol, qty)
        if qty < 1:
            micro = suggest_micro(symbol)
            hint = f" Try {micro}." if micro else ""
            return RiskDecision.rejection(
                f"{symbol}: one contract risks ${risk_per_unit:,.0f} at this stop, "
                f"over the ${budget:,.0f} budget.{hint}")
        limiting = "whole-contracts"
    elif whole_units:
        qty = float(int(qty))
        if qty < 1:
            return RiskDecision.rejection(
                f"one unit at ${entry:,.2f} risks ${risk_per_unit:,.2f}, "
                f"over the ${budget:,.0f} budget")
        limiting = "whole-units"

    notional = qty * unit_value

    if notional_cap_usd and notional > notional_cap_usd > 0:
        scale = notional_cap_usd / notional
        qty *= scale
        if futures or whole_units:
            qty = float(int(qty))
            if qty < 1:
                return RiskDecision.rejection("notional cap leaves no whole unit")
        notional = qty * unit_value
        limiting = "notional-cap"

    # Leverage from the stop and the venue — never from a score, and an
    # explicit request only ever lowers the cap.
    safe = max_safe_leverage(entry, stop, symbol,
                             requested=requested_leverage, notional_hint=notional)
    leverage = max(1.0, float(safe["leverage"]))

    # Financing: margin at the derived leverage, bounded by free cash.
    cash_cap = free_cash * max(0.0, min(1.0, max_margin_frac_of_cash))
    if futures:
        margin = float(margin_required(symbol, qty))
        if cash_cap > 0 and margin > cash_cap:
            return RiskDecision.rejection(
                f"{symbol}: {qty:.0f} contract(s) need ${margin:,.0f} margin, "
                f"over the ${cash_cap:,.0f} free-cash cap")
    else:
        margin = notional / leverage
        if cash_cap > 0 and margin > cash_cap:
            scale = cash_cap / margin
            qty *= scale
            notional *= scale
            margin = cash_cap
            limiting = "cash"

    if qty <= 0 or margin <= 0:
        return RiskDecision.rejection("sized to zero after constraints")

    loss_at_stop = qty * risk_per_unit
    # Revalidate: constraints only shrink, so the loss can never exceed the
    # budget — if it somehow does, that is a bug worth crashing loudly on
    # in tests and refusing on in production.
    if loss_at_stop > budget * 1.0001:
        return RiskDecision.rejection(
            f"internal: loss_at_stop ${loss_at_stop:,.2f} exceeds budget "
            f"${budget:,.2f} — refusing")

    # No rounding in the authority: rounding margin and notional
    # independently broke margin x leverage == notional by cents, which is
    # exactly the kind of self-inconsistency a single sizing engine exists
    # to make impossible. Display layers round; arithmetic does not.
    return RiskDecision(
        allowed_risk_usd=budget,
        stop_distance=stop_distance,
        qty=qty,
        notional=notional,
        margin=margin,
        leverage=leverage,
        lifecycle_multiplier=lifecycle_multiplier,
        limiting_constraint=limiting,
    )
