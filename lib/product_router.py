"""One thesis, several ways to express it — pick on economics, not habit.

THE MARKET THESIS IS VENUE-NEUTRAL. "BTC goes up over the next four hours"
is a claim about the market. It says nothing about whether that claim is
best held as spot, as margin, as a perpetual, or as a regulated future,
and those are genuinely different trades:

    CRYPTO_SPOT    no funding, no liquidation, capital-intensive
    CRYPTO_PERP    funding accrues with a sign, liquidation before the stop
    INDEX_FUTURE   whole contracts, a multiplier, exchange margin, expiry

The same directional view expressed through them can be profitable in one
and unprofitable in another, purely on carry and fees. A desk that always
reaches for the same product is not selecting — it is defaulting, and it
will attribute the cost of that default to the strategy.

WHAT THIS ROUTER IS NOT ALLOWED TO DO. It may not invent a product a venue
cannot execute. Kraken Pro shows a human 11,000 US equities; the API
Center documents no stock trading contract. Expressing an equity thesis as
"Kraken equity spot" would be routing to an endpoint that does not exist,
so capability is checked BEFORE economics — there is no point pricing a
trade that cannot be placed.

SPY VS MES is the interesting case, and the reason product selection is
worth training at all. Same index exposure, completely different unit
economics: shares against a $5-per-point contract, a percentage commission
against a flat one, overnight margin interest against exchange margin.
Which one wins depends on size and hold, and the answer is measurable.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

ROUTER_VERSION = "product_router_v1"

# Refusals, each a distinct lesson.
NO_CAPABILITY = "NO_CAPABILITY"
NO_INSTRUMENT = "NO_INSTRUMENT"
UNPRICEABLE = "UNPRICEABLE"
BELOW_MIN_SIZE = "BELOW_MIN_SIZE"
NEGATIVE_NET = "NEGATIVE_NET"


@dataclass
class Expression:
    """One way of holding a thesis, priced end to end."""
    venue: str
    product: str
    instrument_id: str | None = None
    symbol: str | None = None

    quantity: float = 0.0
    quantity_unit: str | None = None
    multiplier: float = 1.0
    notional_usd: float = 0.0
    margin_usd: float | None = None

    gross_r: float | None = None
    cost_r: float | None = None
    net_r: float | None = None
    cost_breakdown: dict = field(default_factory=dict)

    eligible: bool = False
    reason: str | None = None
    detail: str | None = None

    # PROVENANCE. Preserved on every expression so a learning row can say
    # WHICH venue's economics produced it — generic simulated crypto and
    # Kraken-realistic simulated crypto must never pool.
    capability_status: str | None = None
    data_entitlement: str | None = None
    fee_tier: str | None = None
    venue_spec_version: str | None = None
    market_data_source: str | None = None
    execution_model_version: str | None = None
    router_version: str = ROUTER_VERSION

    def as_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)


def price_expression(*, venue: str, product: str, symbol: str,
                     side: str, entry: float, stop: float,
                     risk_budget_usd: float,
                     gross_r: float | None = None,
                     hold_hours: float = 0.0,
                     market_data_source: str | None = None) -> Expression:
    """Price ONE way of holding the thesis, or say why it cannot be held.

    Capability is checked FIRST. Pricing a trade the venue cannot execute
    produces a number that looks like a comparison and is not one.
    """
    from lib.instruments import UnsupportedInstrument, resolve
    from lib.venue_capabilities import capability

    cap = capability(venue, product)
    e = Expression(
        venue=str(venue).lower(), product=product, symbol=symbol,
        capability_status=cap.status, data_entitlement=cap.data_entitlement,
        market_data_source=market_data_source,
        execution_model_version="fill_v1")

    if not cap.executable:
        e.reason = NO_CAPABILITY
        e.detail = (f"{venue}/{product} is {cap.status}"
                    f"{f' — {cap.reason}' if cap.reason else ''}")
        return e

    try:
        inst = resolve(symbol, venue=venue, product=product).require_executable()
    except UnsupportedInstrument as ex:
        e.reason, e.detail = NO_INSTRUMENT, str(ex)
        return e

    e.instrument_id = inst.instrument_id
    e.quantity_unit = inst.quantity_unit
    e.multiplier = float(inst.multiplier or 1.0)

    distance = abs(float(entry) - float(stop))
    if distance <= 0 or float(entry) <= 0:
        e.reason, e.detail = UNPRICEABLE, "no risk distance"
        return e

    # SIZE IN THIS PRODUCT'S OWN UNITS. Contracts are whole; shares and
    # coins are not. Rounding a contract up would take more risk than the
    # caller authorised.
    risk_per_unit = distance * e.multiplier
    if e.quantity_unit == "CONTRACTS":
        qty = float(int(float(risk_budget_usd) // risk_per_unit))
        if qty < 1:
            e.reason = BELOW_MIN_SIZE
            e.detail = (f"${risk_budget_usd:,.0f} is below the "
                        f"${risk_per_unit:,.0f} risk of one contract")
            return e
    else:
        qty = float(risk_budget_usd) / risk_per_unit

    e.quantity = qty
    e.notional_usd = qty * float(entry) * e.multiplier
    if inst.initial_margin:
        e.margin_usd = qty * float(inst.initial_margin)

    # ── Cost, in this product's own terms ────────────────────────────────
    from lib.transaction_costs import estimate_costs
    try:
        costs = estimate_costs(
            symbol, float(entry), float(stop), venue=venue,
            hold_hours=hold_hours,
            leveraged=(product == "CRYPTO_PERP"),
            is_short=(str(side).lower().startswith("s")))
    except Exception as ex:
        e.reason, e.detail = UNPRICEABLE, f"cost model failed: {ex}"
        return e

    if not costs.get("ok") or costs.get("total_r") is None:
        e.reason = UNPRICEABLE
        e.detail = costs.get("reason") or "cost in R is unknown"
        return e

    e.cost_r = float(costs["total_r"])
    e.cost_breakdown = {k: costs.get(k) for k in
                        ("spread_r", "fees_r", "slippage_r", "funding_r",
                         "borrow_r")}
    e.fee_tier = costs.get("fee_venue")

    if gross_r is not None:
        e.net_r = float(gross_r) - e.cost_r
        e.gross_r = float(gross_r)

    e.eligible = True
    e.detail = (f"{qty:g} {e.quantity_unit.lower()} "
                f"(${e.notional_usd:,.0f} notional), "
                f"{e.cost_r:.3f}R of cost")
    return e


def route(*, symbol: str, side: str, entry: float, stop: float,
          risk_budget_usd: float, candidates: list,
          gross_r: float | None = None, hold_hours: float = 0.0,
          market_data_source: str | None = None) -> dict:
    """Price every way of holding the thesis and rank them by NET.

    Returns every expression, including the refused ones. A refusal is a
    finding: "this thesis is only expressible as a perpetual" is useful,
    and so is "the equity version cannot be routed because the venue has no
    API for it".
    """
    priced = [
        price_expression(venue=v, product=p, symbol=symbol, side=side,
                         entry=entry, stop=stop,
                         risk_budget_usd=risk_budget_usd, gross_r=gross_r,
                         hold_hours=hold_hours,
                         market_data_source=market_data_source)
        for v, p in candidates or []
    ]
    eligible = [e for e in priced if e.eligible]

    best = None
    if eligible:
        scored = [e for e in eligible if e.net_r is not None]
        # Rank on NET when edge is known, on COST when it is not — never on
        # notional or leverage, which measure appetite rather than value.
        best = (max(scored, key=lambda x: x.net_r) if scored
                else min(eligible, key=lambda x: x.cost_r))

    return {
        "symbol": symbol, "side": side,
        "expressions": [e.as_dict() for e in priced],
        "eligible": len(eligible),
        "refused": {e.reason: e.detail for e in priced if not e.eligible},
        "best": best.as_dict() if best else None,
        "best_reason": (
            f"{best.venue}/{best.product} at {best.cost_r:.3f}R cost"
            + (f", {best.net_r:+.3f}R net" if best and best.net_r is not None else "")
            if best else "no expression of this thesis is executable"),
        "router_version": ROUTER_VERSION,
        "note": ("The thesis is venue-neutral; this chose how to express it. "
                 "Capability is checked before economics — a product the "
                 "venue cannot execute is not a cheaper option."),
    }
