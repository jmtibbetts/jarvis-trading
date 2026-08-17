"""The boundary where a plan becomes a fill — and the only thing that
changes when live trading eventually arrives.

THE DESIGN REQUIREMENT. The same canonical chain

    TradeDecision -> RiskDecision -> OrderPlan

must reach a virtual venue today and a real one later WITHOUT the strategy
layer being rebuilt. If enabling Kraken means touching signal generation,
sizing or management, the architecture has failed and the rewrite happens
under time pressure with real money on the other side.

So everything above this line is venue-neutral and everything below it is
an adapter. Strategy produces an OrderPlan; the adapter decides what a fill
means at its venue.

THREE GATES, IN THIS ORDER, AND THE ORDER MATTERS.

    1. PLATFORM MODE     may this program place a real order at all?
    2. VENUE CAPABILITY  can this venue execute this product by API?
    3. PLAN vs RISK      is the plan still inside what risk authorised?

Mode first because it is the cheapest check and the most consequential.
Capability before risk because pricing and sizing a trade the venue cannot
place wastes the work and produces a number that reads like a comparison.
And the risk check LAST, immediately before submission, because everything
between sizing and submission is an opportunity for a plan to drift.

NOTHING MAY ENLARGE A PLAN. Execution may shrink an order — a venue
minimum, a rounded contract, a thinner book — and may never grow one.
A plan that exceeds its RiskDecision is refused rather than clamped,
because a silent clamp hides the defect that produced it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

ADAPTER_VERSION = "execution_venue_v1"

# Venue families.
VIRTUAL_CEX = "VIRTUAL_CEX"
VIRTUAL_DEX = "VIRTUAL_DEX"
LIVE_KRAKEN = "LIVE_KRAKEN"
SHADOW = "SHADOW"

# Refusals. Each names a different problem with a different remedy.
REFUSED_MODE = "REFUSED_PLATFORM_MODE"
REFUSED_CAPABILITY = "REFUSED_VENUE_CAPABILITY"
REFUSED_RISK = "REFUSED_EXCEEDS_RISK"
REFUSED_NO_ADAPTER = "REFUSED_NO_ADAPTER"
REFUSED_NOT_IMPLEMENTED = "REFUSED_NOT_IMPLEMENTED"


@dataclass
class VenueSubmission:
    """The result of offering a plan to a venue. A refusal is a result."""
    accepted: bool
    venue_family: str
    reason: str | None = None
    detail: str | None = None
    execution = None                    # an ExecutionResult when accepted
    adapter_version: str = ADAPTER_VERSION
    provenance: dict = field(default_factory=dict)


class ExecutionVenue:
    """What every venue must be able to answer.

    Deliberately small. A wide interface tempts adapters to expose
    venue-specific behaviour upward, and the moment strategy code branches
    on which venue it is talking to, the boundary is gone.
    """

    family: str = "UNSET"
    is_live: bool = False

    def supports(self, product: str) -> bool:
        raise NotImplementedError

    def submit(self, plan, *, risk=None, **kw) -> VenueSubmission:
        raise NotImplementedError


class VirtualCexAdapter(ExecutionVenue):
    """Simulated broker/exchange. Fills come from lib/virtual_orders."""

    family = VIRTUAL_CEX
    is_live = False

    def supports(self, product: str) -> bool:
        return product in ("EQUITY_SPOT", "EQUITY_SHORT", "ETF_SPOT",
                           "INDEX_FUTURE", "COMMODITY_FUTURE", "FX_SPOT",
                           "CRYPTO_SPOT", "CRYPTO_PERP")

    def submit(self, plan, *, risk=None, quote=None, bar=None,
               **kw) -> VenueSubmission:
        from lib.virtual_orders import (LIMIT, MARKET, VirtualOrder,
                                        execute_limit, execute_market)

        order = VirtualOrder(
            symbol=plan.symbol, side=plan.side, quantity=plan.qty,
            order_type=(LIMIT if str(plan.order_type).lower() == "limit"
                        else MARKET),
            limit_price=(plan.entry if str(plan.order_type).lower() == "limit"
                         else None))

        if order.order_type == LIMIT:
            if not bar:
                return VenueSubmission(
                    False, self.family, REFUSED_NO_ADAPTER,
                    "a limit order cannot be simulated without a bar")
            res = execute_limit(order, bar_high=bar["high"], bar_low=bar["low"])
        else:
            if not quote:
                return VenueSubmission(
                    False, self.family, REFUSED_NO_ADAPTER,
                    "a market order cannot be simulated without a two-sided "
                    "quote — filling at a reference price would hand the "
                    "model half the spread")
            res = execute_market(order, quote)

        out = VenueSubmission(bool(res.filled), self.family,
                              provenance={"fill_model": res.fill_model,
                                          "price_model": res.price_model})
        out.execution = res
        if not res.filled:
            out.reason = res.state
            out.detail = res.reject_reason
        return out


class VirtualDexAdapter(ExecutionVenue):
    """Simulated on-chain execution. A pool is not an order book, and this
    adapter exists so nothing upstream has to know that."""

    family = VIRTUAL_DEX
    is_live = False

    def supports(self, product: str) -> bool:
        return product == "DEX_SPOT"

    def submit(self, plan, *, risk=None, reserve_usd=None,
               sol_price_usd: float = 0.0, gas_balance_sol: float = 0.0,
               **kw) -> VenueSubmission:
        from lib.dex_swap_math import quote_swap, spendable_native

        gas = spendable_native(gas_balance_sol)
        if not gas["can_transact"]:
            return VenueSubmission(False, self.family, "INSUFFICIENT_GAS",
                                   gas["reason"], provenance={"gas": gas})
        if not reserve_usd:
            return VenueSubmission(
                False, self.family, REFUSED_NO_ADAPTER,
                "no pool reserves — a swap cannot be priced against an "
                "unknown book")

        notional = float(plan.notional or (plan.qty * plan.entry))
        q = quote_swap(notional, float(reserve_usd),
                       sol_price_usd=sol_price_usd)
        if not q.get("ok"):
            return VenueSubmission(False, self.family, "NO_ROUTE",
                                   q.get("reason"))
        return VenueSubmission(True, self.family,
                               provenance={"quote": q,
                                           "gas_paid_separately": True})


class KrakenAdapter(ExecutionVenue):
    """The future live venue. DECLARED, NOT IMPLEMENTED.

    It exists so the boundary is real today rather than aspirational: the
    dispatcher already routes to it, the mode gate already refuses it, and
    the tests already prove a plan cannot reach a live venue while the
    platform is virtual. When live execution is eventually built, the work
    is inside this class and nowhere else.

    Implementing it now would be worse than useless — untested order
    submission code against real money, sitting in a repository, waiting
    for someone to flip a flag.
    """

    family = LIVE_KRAKEN
    is_live = True

    def supports(self, product: str) -> bool:
        from lib.venue_capabilities import capability
        return capability("kraken", product).executable

    def submit(self, plan, *, risk=None, **kw) -> VenueSubmission:
        return VenueSubmission(
            False, self.family, REFUSED_NOT_IMPLEMENTED,
            "the Kraken live adapter is declared but not implemented — "
            "the boundary exists so strategy code need not change when it "
            "is, and building untested live submission before it is needed "
            "would be a liability rather than a feature")


_ADAPTERS: dict[str, ExecutionVenue] = {
    VIRTUAL_CEX: VirtualCexAdapter(),
    VIRTUAL_DEX: VirtualDexAdapter(),
    LIVE_KRAKEN: KrakenAdapter(),
}


def adapter_for(venue_family: str) -> ExecutionVenue | None:
    return _ADAPTERS.get(str(venue_family or "").upper())


def submit(plan, *, venue_family: str, risk=None, product: str | None = None,
           venue: str | None = None, **kw) -> VenueSubmission:
    """THE boundary. Three gates, then the adapter.

    Everything above this call is venue-neutral. Everything below it is an
    adapter's business.
    """
    fam = str(venue_family or "").upper()
    adapter = adapter_for(fam)
    if adapter is None:
        return VenueSubmission(False, fam, REFUSED_NO_ADAPTER,
                               f"no adapter registered for {fam}")

    # ── GATE 1: platform mode ────────────────────────────────────────────
    # Cheapest and most consequential. A live adapter cannot be reached at
    # all while the platform is virtual, regardless of what anything else
    # decided.
    if adapter.is_live:
        from lib.platform_mode import (LiveExecutionDisabled,
                                       assert_may_increase_exposure)
        try:
            assert_may_increase_exposure(f"{fam} order submission")
        except LiveExecutionDisabled as e:
            return VenueSubmission(False, fam, REFUSED_MODE, str(e))

    # ── GATE 2: venue capability ─────────────────────────────────────────
    prod = product or getattr(plan, "product", None)
    if prod and not adapter.supports(prod):
        return VenueSubmission(
            False, fam, REFUSED_CAPABILITY,
            f"{fam} cannot execute {prod}")
    if venue and prod:
        from lib.venue_capabilities import capability
        cap = capability(venue, prod)
        if adapter.is_live and not cap.executable:
            return VenueSubmission(
                False, fam, REFUSED_CAPABILITY,
                f"{venue}/{prod} is {cap.status} — {cap.reason}")

    # ── GATE 3: the plan must still fit the risk it was sized against ────
    # LAST, immediately before submission, because everything between
    # sizing and here is an opportunity for a plan to drift. Refused rather
    # than clamped: a silent clamp hides the defect that produced it.
    if risk is not None and hasattr(plan, "check"):
        verdict = plan.check(risk)
        if not verdict.ok:
            # The verdict names the ceiling that was breached. "The order
            # was rejected" is the least actionable sentence a risk system
            # can produce, and the gate now checks far more than quantity:
            # a refused decision, a non-finite size, an unparseable side, an
            # instrument with no verified units, leverage, and the money at
            # risk once the stop is priced through the contract multiplier.
            return VenueSubmission(
                False, fam, REFUSED_RISK,
                f"{verdict.reason} — execution may shrink an order, "
                f"never enlarge one")

    return adapter.submit(plan, risk=risk, **kw)


def registry() -> dict:
    """What can be routed to, and which of it is live."""
    return {
        "version": ADAPTER_VERSION,
        "adapters": {
            fam: {"family": a.family, "is_live": a.is_live,
                  "implemented": not isinstance(a, KrakenAdapter)}
            for fam, a in _ADAPTERS.items()},
        "note": ("Strategy produces an OrderPlan; only the adapter changes "
                 "between virtual and live. If enabling a live venue "
                 "requires touching strategy code, the boundary has failed."),
    }
