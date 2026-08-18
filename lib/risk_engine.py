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
                   lifecycle_multiplier: float = 1.0,
                   execution_instrument=None) -> RiskDecision:
    """Solve quantity from risk. The single entry point for every book.

    `risk_budget_usd` arrives WITH account policy already applied (base
    risk %, lifecycle, regime) — those are per-book decisions. Everything
    after the budget is shared arithmetic and lives here.

    `execution_instrument` is an exact `InstrumentIdentity` — what ONE unit
    of the thing being traded actually is. When supplied it is the AUTHORITY
    for the multiplier, the quantity unit, the step and the minimum, and the
    bare symbol is never consulted for any of them. PBTCUCZ50 is 0.01 BTC per
    contract; asking `get_spec("BTC/USD")` answers 1.0 coin, and a quantity
    that means one thing to risk and another to execution is wrong by 100x
    while looking identical from both sides.

    `None` preserves the legacy path exactly, symbol-derived semantics and
    all — every book migrates when it is ready, not because this changed.
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

    from lib.instruments import (UnexecutableQuantity, get_spec, is_futures,
                                 margin_required, normalize_quantity_down,
                                 suggest_micro, whole_contracts)
    from lib.paper_engine import max_safe_leverage

    exact = execution_instrument is not None

    if exact:
        # The instrument speaks for itself. No get_spec(), no is_futures() —
        # consulting the bare symbol here is the entire defect this path
        # exists to remove.
        mult = float(execution_instrument.multiplier or 1.0)
        unit_name = str(execution_instrument.quantity_unit or "UNITS")
        minimum = execution_instrument.minimum_quantity
        minimum = float(minimum) if minimum is not None else None
    else:
        spec = get_spec(symbol) if symbol else None
        mult = float(spec.multiplier if spec else 1.0)
        unit_name, minimum = None, None

    unit_value = entry * mult
    risk_per_unit = stop_distance * mult

    def executable(q: float, context: str) -> tuple[float | None, str | None]:
        """Shrink `q` to something the venue can fill: (qty, None) or
        (None, refusal).

        The minimum is an ELIGIBILITY FLOOR. Falling below it means there is
        no executable size at this budget, never that the size becomes the
        minimum — the difference between refusing a trade and opening one.
        """
        try:
            n = normalize_quantity_down(q, execution_instrument)
        except UnexecutableQuantity as e:
            return None, f"{context}: {e}"
        if minimum is not None and n < minimum:
            return None, (
                f"{context}: {q:,.6g} {unit_name} is below the {minimum:g} "
                f"{unit_name} minimum — no executable size at this budget")
        return n, None

    qty = budget / risk_per_unit
    limiting = "risk"

    if exact:
        # Every shrinking constraint below re-normalises. This is the first
        # of them, and the reason the theoretical quantity is never allowed
        # to survive as-is: 0.94 contracts is not 1 contract, it is none.
        sized, why = executable(qty, "risk budget")
        if sized is None:
            return RiskDecision.rejection(why)
        if sized < qty:
            limiting = "executable-quantity"
        qty = sized
        # The ceiling every later constraint is measured against. Each one
        # takes a min() and then shrinks to the step, so this should be
        # unreachable — which is exactly why it is worth stating: the
        # constraints are what would have to break for it to fire.
        risk_ceiling = qty

    # Whole-unit instruments (futures contracts, integer shares) round DOWN.
    # An exact instrument has already answered this question with its own
    # step, so the symbol-derived rules are skipped rather than re-applied.
    futures = (not exact) and bool(symbol and is_futures(symbol))
    if futures:
        qty = whole_contracts(symbol, qty)
        if qty < 1:
            micro = suggest_micro(symbol)
            hint = f" Try {micro}." if micro else ""
            return RiskDecision.rejection(
                f"{symbol}: one contract risks ${risk_per_unit:,.0f} at this stop, "
                f"over the ${budget:,.0f} budget.{hint}")
        limiting = "whole-contracts"
    elif whole_units and not exact:
        qty = float(int(qty))
        if qty < 1:
            return RiskDecision.rejection(
                f"one unit at ${entry:,.2f} risks ${risk_per_unit:,.2f}, "
                f"over the ${budget:,.0f} budget")
        limiting = "whole-units"

    notional = qty * unit_value

    if notional_cap_usd and notional > notional_cap_usd > 0:
        if exact:
            # Take the cap as a CONTINUOUS ceiling, then shrink to something
            # executable. Scaling qty by the ratio instead would leave 3.7
            # contracts — a number the notional, the margin and the loss all
            # agree on, and which no venue will fill.
            proposed = min(qty, notional_cap_usd / unit_value)
            sized, why = executable(proposed, "notional cap")
            if sized is None:
                return RiskDecision.rejection(why)
            qty = sized
        else:
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
    if requested_leverage is not None and requested_leverage <= 1.0:
        # A cash account: leverage is a FACT of the venue relationship,
        # not something to derive. 1x means the notional is fully funded.
        leverage = 1.0
    else:
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
            if exact:
                # THE ONE THAT MATTERS. The generic branch below sets
                # `margin = cash_cap` after scaling quantity continuously,
                # which reads a CEILING as an instruction to spend it all —
                # and at a step of 1 contract that is not even possible. Here
                # the cap bounds the notional, the quantity shrinks to the
                # step, and the margin is whatever that quantity actually
                # costs. It lands BELOW the cap, and that is correct.
                proposed = min(qty, (cash_cap * leverage) / unit_value)
                sized, why = executable(proposed, "free-cash cap")
                if sized is None:
                    return RiskDecision.rejection(why)
                qty = sized
                notional = qty * unit_value
                margin = notional / leverage
            else:
                scale = cash_cap / margin
                qty *= scale
                notional *= scale
                margin = cash_cap
            limiting = "cash"

    if exact:
        # Step 8 — the last word on quantity, after every constraint has had
        # its turn. A no-op when the path above was correct; when it was not,
        # this is what stops a fractional contract reaching a venue.
        final, why = executable(qty, "final normalisation")
        if final is None:
            return RiskDecision.rejection(why)
        if final != qty:
            qty = final
            notional = qty * unit_value
            margin = notional / leverage

        if qty > risk_ceiling + 1e-12:
            return RiskDecision.rejection(
                f"internal sizing inconsistency: a constraint enlarged the "
                f"quantity from {risk_ceiling:g} to {qty:g} — constraints "
                f"only ever shrink")

        # Step 9 — the economics must FOLLOW from the quantity, not merely
        # accompany it. A decision that looks plausible and disagrees with
        # itself is worse than a refusal, because nothing downstream can see
        # the disagreement.
        tol = 1e-6 * max(1.0, abs(notional))
        if abs(notional - qty * entry * mult) > tol:
            return RiskDecision.rejection(
                "internal sizing inconsistency: notional is not "
                "qty x entry x multiplier")
        if abs(margin * leverage - notional) > tol:
            return RiskDecision.rejection(
                "internal sizing inconsistency: margin x leverage is not "
                "the notional")

    if qty <= 0 or margin <= 0:
        return RiskDecision.rejection("sized to zero after constraints")

    loss_at_stop = qty * risk_per_unit
    # Revalidate: constraints only shrink, so the loss can never exceed the
    # budget — if it somehow does, that is a bug worth crashing loudly on
    # in tests and refusing on in production.
    #
    # TWO TOLERANCES, BECAUSE THE TWO PATHS CONTROL DIFFERENT AMOUNTS OF
    # THEIR OWN ARITHMETIC. Legacy quantities pass through display-adjacent
    # rounding this engine does not own, so legacy keeps its historical
    # 0.01% economic forgiveness. The exact path computed every digit of its
    # own quantity, so the only legitimate excess is float representation —
    # parts per billion — and the tolerance must stay far below one quantity
    # step's risk. A guard that forgives 0.01% would approve precisely the
    # enlarged-by-a-hair quantity it exists to refuse, firing only on errors
    # already larger than a contract.
    if exact:
        budget_bound = budget + 1e-9 * max(1.0, budget)
    else:
        budget_bound = budget * 1.0001
    if loss_at_stop > budget_bound:
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
        # The unit basis travels WITH the quantity. Downstream stages that
        # re-derive it from the symbol are the reason it has to.
        quantity_unit=unit_name if exact else None,
        multiplier=mult if exact else None,
    )
