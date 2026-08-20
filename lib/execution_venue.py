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

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:      # runtime import would be circular
    from lib.virtual_orders import ExecutionResult

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
# The plan and the exact instrument it will be filled as disagree about what
# one unit IS. Not adjudicated — the fill model must never get to pick which
# side's arithmetic wins (B0).
REFUSED_UNIT_BASIS = "REFUSED_UNIT_BASIS_MISMATCH"


@dataclass
class VenueSubmission:
    """The result of offering a plan to a venue. A refusal is a result."""
    accepted: bool
    venue_family: str
    reason: str | None = None
    detail: str | None = None
    # A REAL DATACLASS FIELD, not a class attribute.
    #
    # `execution = None` without an annotation is shared class state that
    # dataclasses never sees: it is absent from fields(), never initialised
    # per instance, and assigning it on one submission is invisible to the
    # type checker and to anything reflecting over the contract. F13.
    execution: "ExecutionResult | None" = None
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


def _plan_instrument_mismatch(plan, instrument) -> str | None:
    """The disagreements a fill model must never adjudicate (B0).

    Compared only where BOTH sides state a value — a legacy plan with no
    basis meets no instrument claim, and an instrument-less legacy submit
    meets no plan claim, so neither changes behaviour. Where both speak,
    they must say the same thing: unit, contract identity, and multiplier
    (to representation tolerance only — a float round-trip, never a
    different contract size).
    """
    if instrument is None or plan is None:
        return None
    plan_unit = getattr(plan, "quantity_unit", None)
    inst_unit = getattr(instrument, "quantity_unit", None)
    if plan_unit and inst_unit and plan_unit != inst_unit:
        return (f"the plan counts {plan_unit!r} but {getattr(instrument, 'instrument_id', '?')} "
                f"fills in {inst_unit!r}")
    plan_iid = getattr(plan, "instrument_id", None)
    inst_iid = getattr(instrument, "instrument_id", None)
    if plan_iid and inst_iid and plan_iid != inst_iid:
        return (f"the plan was built for {plan_iid!r} but the supplied "
                f"execution identity is {inst_iid!r}")
    plan_mult = getattr(plan, "multiplier", None)
    inst_mult = getattr(instrument, "multiplier", None)
    if plan_mult is not None and inst_mult is not None:
        try:
            pm, im = float(plan_mult), float(inst_mult)
        except (TypeError, ValueError):
            return (f"unreadable multiplier on the plan ({plan_mult!r}) or "
                    f"the instrument ({inst_mult!r})")
        if abs(pm - im) > 1e-12 * max(1.0, abs(im)):
            return (f"the plan's multiplier {pm!r} is not the instrument's "
                    f"{im!r}")
    return None


class VirtualCexAdapter(ExecutionVenue):
    """Simulated broker/exchange. Fills come from lib/virtual_orders."""

    family = VIRTUAL_CEX
    is_live = False

    def supports(self, product: str) -> bool:
        return product in ("EQUITY_SPOT", "EQUITY_SHORT", "ETF_SPOT",
                           "INDEX_FUTURE", "COMMODITY_FUTURE", "FX_SPOT",
                           "CRYPTO_SPOT", "CRYPTO_PERP")

    def submit(self, plan, *, risk=None, quote=None, bar=None,
               instrument=None, **kw) -> VenueSubmission:
        from lib.virtual_orders import (LIMIT, MARKET, VirtualOrder,
                                        execute_limit, execute_market)

        # A PLAN THAT STATES ITS BASIS MUST AGREE WITH THE INSTRUMENT IT IS
        # ABOUT TO BE FILLED AS — checked HERE, before the fill model runs,
        # because virtual_orders must never be the party that decides which
        # of two stated bases wins. A plan in CONTRACTS handed an identity in
        # COINS is a 100x disagreement wearing the same number.
        mismatch = _plan_instrument_mismatch(plan, instrument)
        if mismatch:
            return VenueSubmission(False, self.family, REFUSED_UNIT_BASIS,
                                   mismatch)

        order = VirtualOrder(
            symbol=plan.symbol, side=plan.side, quantity=plan.qty,
            order_type=(LIMIT if str(plan.order_type).lower() == "limit"
                        else MARKET),
            limit_price=(plan.entry if str(plan.order_type).lower() == "limit"
                         else None),
            # The unit travels WITH the order, so an ExecutionResult can be
            # audited against what was requested, not only against what the
            # instrument implied.
            quantity_unit=getattr(plan, "quantity_unit", None))

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
            # THE EXACT INSTRUMENT TRAVELS WITH THE ORDER. Without it
            # virtual_orders re-resolves the symbol and a frozen perpetual
            # silently becomes spot-in-coins.
            res = execute_market(order, quote, instrument=instrument)

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
               sol_price_usd: float = 0.0,
               gas_balance_sol: float | None = None,
               fee_action: str = "NORMAL_ENTRY",
               priority_level: str = "NORMAL",
               pool_address: str | None = None,
               mint: str | None = None,
               fee_fetch=None,
               **kw) -> VenueSubmission:
        from lib import dex_wallet as DW
        from lib.dex_swap_math import quote_swap
        from lib.virtual_orders import ExecutionResult

        # THE PERSISTED WALLET IS THE ECONOMIC AUTHORITY (P0-3).
        #
        # `gas_balance_sol` used to be believed outright — a caller could
        # make an impossible trade executable by typing a bigger number.
        # When a wallet exists, the ledger decides and the caller's figure
        # is recorded as provenance only. The legacy argument survives
        # SOLELY for callers running without a wallet (old tests, ad-hoc
        # probes), where there is no persisted truth for it to override.
        # A CALLER CANNOT INVENT A WALLET. When no persisted wallet exists
        # the answer is refusal, never the caller's number: trusting it
        # would let an unfunded account execute by describing a balance it
        # does not have. Funding is an explicit, provenanced event
        # (lib.dex_wallet.fund_wallet), not a function argument.
        if not DW.initialized():
            return VenueSubmission(
                False, self.family, "WALLET_NOT_FUNDED",
                "no persisted DEX wallet exists; virtual balances are "
                "created only by an explicit funding event, never by a "
                "caller-supplied balance",
                provenance={"gas_authority": "NONE_WALLET_UNFUNDED",
                            "caller_supplied_ignored": (
                                float(gas_balance_sol)
                                if gas_balance_sol is not None else None)})
        # ── WHAT DOES THIS TRANSACTION COST RIGHT NOW? ──────────────────
        #
        # THE DEFECT THIS CLOSES. `DW.gas_state()` with no estimate answers
        # STATIC_POLICY_ONLY — an operability floor derived from the
        # protocol base fee — and `quote_swap` below then priced the network
        # fee from a 100,000-lamport constant. Neither number had any
        # relationship to the live fee market, and the ExecutionResult
        # carried the constant onward into RealizedOutcome as though it were
        # a measured cost.
        #
        # STATIC GAS MAY NOT BECOME CANONICAL EXECUTION TRUTH. The fee is
        # measured here or the submission is refused: there is no path where
        # the estimator fails and a default quietly takes over.
        from lib import dex_network_cost as NC
        priced = NC.price_transaction(
            action=fee_action, priority_level=priority_level,
            mint=mint or getattr(plan, "mint", None),
            pool_address=pool_address,
            sol_price_usd=sol_price_usd,
            notional_usd=float(plan.notional
                               or (float(plan.qty or 0) * float(plan.entry or 0))
                               or 0.0),
            fetch=fee_fetch)
        network_cost = NC.fee_provenance(priced)
        if not priced["ok"]:
            return VenueSubmission(
                False, self.family,
                priced["refusal_reason"] or "NETWORK_FEE_REFUSED",
                priced["detail"],
                provenance={"gas_authority": "PERSISTED_WALLET",
                            "network_cost": network_cost})

        # THE RESERVE IS THE AUTHORIZED BID — not the raw measurement, and
        # not a static floor. What the wallet must hold is what we would
        # actually pay, and if policy refused the market price there is no
        # bid to reserve for.
        gas = DW.gas_state(fee_authorization=priced["authorization"])
        gas_authority = "PERSISTED_WALLET"
        if gas_balance_sol is not None:
            # Conservative per-call limit only: it may shrink what the
            # ledger permits, never raise it. `is not None` rather than
            # truthiness — an explicit 0.0 means "spend no gas", and
            # reading that as "no cap" would grant the whole wallet.
            gas["caller_supplied_limit"] = float(gas_balance_sol)
            gas["max_spendable_sol"] = min(
                float(gas["max_spendable_sol"]), float(gas_balance_sol))
            if float(gas_balance_sol) <= 0.0:
                gas["can_transact"] = False
                gas["reason"] = ("the caller capped gas spending at "
                                 f"{float(gas_balance_sol):.9f} SOL")
        if not gas.get("can_transact"):
            return VenueSubmission(False, self.family, "INSUFFICIENT_GAS",
                                   gas.get("reason"),
                                   provenance={"gas": gas,
                                               "gas_authority": gas_authority})
        if not reserve_usd:
            return VenueSubmission(
                False, self.family, REFUSED_NO_ADAPTER,
                "no pool reserves — a swap cannot be priced against an "
                "unknown book")

        notional = float(plan.notional or (plan.qty * plan.entry))
        q = quote_swap(notional, float(reserve_usd),
                       sol_price_usd=sol_price_usd,
                       priority_lamports=priced["priority_lamports_for_quote"])
        if not q.get("ok"):
            return VenueSubmission(False, self.family, "NO_ROUTE",
                                   q.get("reason"))

        # THE SAME CANONICAL RESULT THE CEX ADAPTER RETURNS (P0-5).
        #
        # Cost semantics are identical across venues, and the separations
        # are load-bearing:
        #   explicit_fees   = the pool's schedule fee (the venue's cut)
        #   network_fees    = gas, charged by the chain to run the tx
        #   price_impact    = the pool moving because this trade happened —
        #                     NOT a fee, and never summed into one
        #   spread          = None. A pool has no bid/ask; zero would claim
        #                     a measurement that was never made.
        received = float(q.get("received_usd") or 0.0)
        px = float(plan.entry or 0.0)
        qty_tokens = (received / px) if px > 0 else 0.0
        avg_price = (notional / qty_tokens) if qty_tokens > 0 else None
        res = ExecutionResult(
            state="FILLED", symbol=plan.symbol, side=plan.side,
            order_type="market",
            requested_quantity=float(plan.qty or 0.0),
            filled_quantity=qty_tokens,
            quantity_unit=getattr(plan, "quantity_unit", None) or "TOKENS",
            decision_mid=px or None,
            fill_price=avg_price,
            venue="VIRTUAL_DEX", product="DEX_SPOT",
            instrument_id=getattr(plan, "instrument_id", None),
            price_impact_usd=float(q.get("price_impact_usd") or 0.0),
            explicit_fees_usd=float(q.get("pool_fee_usd") or 0.0),
            network_fees_usd=float(q.get("network_fee_usd") or 0.0),
            price_model="DEX_XYK_POOL",
            # MEASURED, AUTHORIZED and ACTUAL are three different facts and
            # travel separately. `network_fees_usd` above is the modelled
            # charge for this simulated fill; the two lamport figures record
            # what the network indicated and what policy permitted, so a
            # later reconciliation can tell an accurate estimate from a
            # capped bid that got included anyway.
            measured_network_fee_lamports=priced[
                "measured_total_network_fee_lamports"],
            authorized_network_fee_lamports=priced["authorized_bid_lamports"],
            fee_provenance=network_cost,
            provenance={"quote": q, "gas_authority": gas_authority,
                        "network_cost": network_cost},
        )
        out = VenueSubmission(True, self.family,
                              provenance={"quote": q,
                                          "gas_authority": gas_authority,
                                          "network_cost": network_cost,
                                          "gas_paid_separately": True})
        out.execution = res
        return out


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
