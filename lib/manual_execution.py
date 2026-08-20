"""A trade the OPERATOR placed, at a venue this program cannot reach.

WHY THIS IS A FIRST-CLASS MODE, NOT A FALLBACK. A large part of real
trading happens where no usable execution API exists: venues with UI-only
access, restricted or unavailable trading endpoints, DEX front-ends, and
accounts whose authority is deliberately not granted to this program.
`lib/venue_capabilities.py` already carries that status as `UI_ONLY`. A
missing API is a fact about the venue, and it does not make the trade
invisible, unmeasurable, or unworthy of learning.

The lifecycle this supports is the canonical one, with one substitution:

    JARVIS observes -> thesis -> recommendation
        -> THE HUMAN EXECUTES
        -> the actual execution evidence is recorded here
        -> linked to the originating thesis
        -> canonical RealizedOutcome
        -> learning

WHAT THIS MODULE REFUSES TO DO

**It never claims JARVIS submitted the order.** `lib/execution_mode.py`
holds that bit and `MANUAL_OPERATOR.submitted_by_jarvis` is False. There is
no adapter here and no `submit()`, because there is nothing to submit to.

**It never fabricates a thesis.** A trade the operator took independently
carries `thesis_id = None`. Manufacturing a plausible historical
recommendation so the record looks complete would put a claim in the
evidence base that JARVIS never made and then score itself against it.

**It never rewrites the recommendation to match the execution.** The
`RecommendationSnapshot` is frozen at recommendation time. If the operator
entered three minutes later at a worse price, BOTH numbers survive: that
gap is the entire measurement of execution quality, and averaging it away
leaves a system that cannot tell a bad thesis from a late entry.

**It never turns UNKNOWN into zero.** Every cost is `float | None`. A fee
nobody evidenced is None, and a net P&L computed from a None cost is None —
see `net_pnl_usd`. A zero would assert the venue charged nothing.

**It never touches the virtual economy.** Recording a manual trade moves no
paper cash, opens no paper position and funds no DEX wallet. That boundary
is load-bearing: a training book funded by external evidence would measure
the simulator against a balance the simulator never produced.

**One trade is ONE observation.** Scale-ins, partial exits, funding events
and fee rebates are FACTS WITHIN a trade, not separate market events. The
trade binds to a single `thesis_id` and produces at most one
`RealizedOutcome`, so a position that scaled out four times still votes
once. `lib/trade_thesis.py` holds the counting rule; this module obeys it.

THE ECONOMICS ARE PYTHON'S, NOT A MODEL'S. Gross P&L is computed from the
legs by running the book forward in time on a weighted-average cost basis —
deterministic, replayable, and stated as `PNL_BASIS`. Nothing here asks an
LLM for arithmetic.

WHERE THE OPERATOR REPORTS A REALIZED FIGURE, IT IS PRESERVED AS ITS OWN
FACT and reconciled against the component-derived figure rather than
overwriting it or being overwritten. `lib/venue_reconciliation.py` owns that
vocabulary; an unexplained gap stays UNEXPLAINED, because it is the honest
measurement of what this system does not yet model about that venue.
"""
from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from lib.instruments import (CONTRACTS, COINS, CRYPTO_PERP, CRYPTO_SPOT,
                             DEX_SPOT, EQUITY_SHORT, EQUITY_SPOT, ETF_SPOT,
                             COMMODITY_FUTURE, FX_SPOT, FX_UNITS,
                             INDEX_FUTURE, SHARES, TOKEN_UNITS)
# Leg kinds are IMPORTED, not redefined: a manual scale-in is another ENTRY
# leg and a manual scale-out is a PARTIAL_EXIT, in the same vocabulary the
# paper settlement ledger already uses. A second set of leg names would make
# the two ledgers unreadable side by side for no gain.
from lib.paper_settlement import (LEG_ENTRY, LEG_FINAL_EXIT,
                                  LEG_PARTIAL_EXIT)

logger = logging.getLogger(__name__)

MANUAL_EXECUTION_VERSION = "manual_execution_v1"

# How gross P&L is derived from the legs. Stated, never assumed: a reader
# who does not know the basis cannot check the number.
PNL_BASIS = "WEIGHTED_AVERAGE_COST"

UNKNOWN = "UNKNOWN"

# Quantity comparisons. Manual entry is decimal text; exact float equality
# would make a full close look like a 1e-16 remainder forever.
QTY_TOL = 1e-9


# ── Lifecycle ────────────────────────────────────────────────────────────
# Mapped onto what a manual trade can actually be, with no state invented
# beyond that. CANCELLED and ABANDONED are deliberately separate: a trade
# that was never opened is not the same fact as one whose fate is unknown.
DRAFT = "DRAFT"                        # recorded, not yet asserted as live
OPEN = "OPEN"                          # at least one entry, nothing closed
PARTIALLY_CLOSED = "PARTIALLY_CLOSED"  # some quantity out, some still on
CLOSED = "CLOSED"                      # flat. The only state with an outcome
CANCELLED = "CANCELLED"                # never executed; no economics exist
ABANDONED = "ABANDONED"                # opened, and its end is not evidenced

STATES = (DRAFT, OPEN, PARTIALLY_CLOSED, CLOSED, CANCELLED, ABANDONED)
TERMINAL_STATES = frozenset({CLOSED, CANCELLED, ABANDONED})

# The ONLY permitted transitions. Anything else refuses — a book that can
# reopen a closed trade can also silently rewrite a settled result.
_TRANSITIONS: dict[str, frozenset] = {
    DRAFT: frozenset({OPEN, CANCELLED}),
    OPEN: frozenset({PARTIALLY_CLOSED, CLOSED, ABANDONED}),
    PARTIALLY_CLOSED: frozenset({PARTIALLY_CLOSED, CLOSED, ABANDONED}),
    CLOSED: frozenset(),
    CANCELLED: frozenset(),
    ABANDONED: frozenset(),
}

# ── Vocabulary reused from the canonical taxonomies ──────────────────────
PRODUCTS = frozenset({EQUITY_SPOT, EQUITY_SHORT, ETF_SPOT, INDEX_FUTURE,
                      COMMODITY_FUTURE, FX_SPOT, CRYPTO_SPOT, CRYPTO_PERP,
                      DEX_SPOT})
QUANTITY_UNITS = frozenset({SHARES, COINS, CONTRACTS, FX_UNITS, TOKEN_UNITS})

LEG_KINDS = frozenset({LEG_ENTRY, LEG_PARTIAL_EXIT, LEG_FINAL_EXIT})
EXIT_LEG_KINDS = frozenset({LEG_PARTIAL_EXIT, LEG_FINAL_EXIT})

LONG = "long"
SHORT = "short"

MARGIN_ISOLATED = "ISOLATED"
MARGIN_CROSS = "CROSS"
MARGIN_NONE = "NONE"
MARGIN_MODES = frozenset({MARGIN_ISOLATED, MARGIN_CROSS, MARGIN_NONE,
                          UNKNOWN})

LIQUIDITY_MAKER = "MAKER"
LIQUIDITY_TAKER = "TAKER"
LIQUIDITY_ROLES = frozenset({LIQUIDITY_MAKER, LIQUIDITY_TAKER, UNKNOWN})

# ── Cost events: economics that are NOT a price leg ──────────────────────
# A funding payment moved money and filled nothing. Modelling it as a leg
# would put a fill price on an event that has none.
FUNDING_PAID = "FUNDING_PAID"
FUNDING_RECEIVED = "FUNDING_RECEIVED"
FEE_REBATE = "FEE_REBATE"
LIQUIDATION_FEE = "LIQUIDATION_FEE"
NETWORK_FEE = "NETWORK_FEE"
BORROW_CHARGE = "BORROW_CHARGE"
ROLLOVER_CHARGE = "ROLLOVER_CHARGE"
OTHER_VERIFIED_COST = "OTHER_VERIFIED_COST"

COST_EVENT_KINDS = frozenset({
    FUNDING_PAID, FUNDING_RECEIVED, FEE_REBATE, LIQUIDATION_FEE,
    NETWORK_FEE, BORROW_CHARGE, ROLLOVER_CHARGE, OTHER_VERIFIED_COST})

# WHICH CANONICAL COST FIELD EACH EVENT LANDS IN, and with which sign.
#
# FUNDING IS A TRANSFER, NOT A FEE. Funding RECEIVED carries -1 so it
# reduces `funding_usd`, which `RealizedOutcome.finalize` subtracts from
# gross — so receiving funding correctly IMPROVES the result. A model that
# always charged funding as a cost would understate every short that was
# paid to hold its position.
_COST_EVENT_MAP: dict[str, tuple[str, float]] = {
    FUNDING_PAID: ("funding_usd", 1.0),
    FUNDING_RECEIVED: ("funding_usd", -1.0),
    FEE_REBATE: ("commission_usd", -1.0),
    NETWORK_FEE: ("network_fees_usd", 1.0),
    BORROW_CHARGE: ("borrow_cost_usd", 1.0),
    ROLLOVER_CHARGE: ("rollover_usd", 1.0),
    # NO DEDICATED CANONICAL FIELD EXISTS for either of these. They are
    # real explicit venue charges, so they are folded into commission_usd
    # rather than dropped — and `provenance["cost_category_map"]` records
    # exactly how many dollars arrived from where, so the fold is auditable
    # and reversible. Inventing a RealizedOutcome field would ripple into
    # the persisted schema and the learning projection's validation; losing
    # the dollars would understate what the trade cost. See the handoff's
    # UNKNOWNs.
    LIQUIDATION_FEE: ("commission_usd", 1.0),
    OTHER_VERIFIED_COST: ("commission_usd", 1.0),
}


class ManualExecutionError(ValueError):
    """Manual input that cannot be accepted as stated.

    MANUAL INPUT IS UNTRUSTED INPUT. Every refusal here names what was
    wrong; none of them guesses a correction. A quantity typed with a
    misplaced decimal is not repairable by this program, and repairing it
    would put a number in the economic record that the operator never
    entered.
    """


class IncompleteManualTrade(ManualExecutionError):
    """A realized quantity was requested from a trade that cannot supply one."""


# ── Validation primitives ────────────────────────────────────────────────
def _finite(value, what: str) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise ManualExecutionError(f"{what} is not a number: {value!r}")
    if math.isnan(f):
        raise ManualExecutionError(
            f"{what} is NaN. NaN propagates silently through every "
            f"aggregate it touches and turns a book into an unreadable one")
    if math.isinf(f):
        raise ManualExecutionError(
            f"{what} is infinite, which no venue reported and no position "
            f"can hold")
    return f


def _positive(value, what: str) -> float:
    f = _finite(value, what)
    if f <= 0:
        raise ManualExecutionError(
            f"{what} must be greater than zero; got {f!r}")
    return f


def _non_negative_or_none(value, what: str) -> float | None:
    """None stays None. THIS IS THE UNKNOWN-IS-NOT-ZERO GATE."""
    if value is None:
        return None
    f = _finite(value, what)
    if f < 0:
        raise ManualExecutionError(
            f"{what} must not be negative; got {f!r}. A credit is recorded "
            f"as its own event kind, not as a negative charge")
    return f


def _ts(value, what: str, *, required: bool = True) -> str | None:
    if value in (None, ""):
        if required:
            raise ManualExecutionError(f"{what} is required")
        return None
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise ManualExecutionError(
            f"{what} is not an ISO-8601 timestamp: {value!r}")
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.isoformat()


def _as_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def parse_state_transition(current: str, target: str) -> str:
    """Refuse an impossible lifecycle move, by name."""
    if current not in STATES:
        raise ManualExecutionError(f"{current!r} is not a manual trade state")
    if target not in STATES:
        raise ManualExecutionError(f"{target!r} is not a manual trade state")
    if target not in _TRANSITIONS[current]:
        raise ManualExecutionError(
            f"{current} -> {target} is not a permitted transition. "
            f"From {current} the trade may become: "
            f"{', '.join(sorted(_TRANSITIONS[current])) or 'nothing — it is terminal'}")
    return target


# ── The frozen JARVIS side ───────────────────────────────────────────────
@dataclass(frozen=True)
class RecommendationSnapshot:
    """What JARVIS said, AT THE TIME IT SAID IT. Never updated afterwards.

    This is the calibration baseline. Every field here has an `actual`
    counterpart on the trade, and the whole value of the manual desk is in
    comparing them:

        recommended entry vs actual entry     -> entry timing/slippage
        expected fee vs actual fee            -> cost-model accuracy
        expected funding vs actual funding    -> carry-model accuracy
        expected R vs realized R              -> edge-model accuracy

    Absent when the operator traded independently. It is NOT reconstructed
    from the execution: a recommendation inferred backwards from what
    happened would score the system against a claim it never made, and it
    would score perfectly.
    """

    thesis_id: str | None = None
    signal_id: str | None = None
    decision_id: str | None = None
    recommended_at: str | None = None

    direction: str | None = None
    venue: str | None = None
    product: str | None = None
    symbol: str | None = None

    entry: float | None = None
    stop: float | None = None
    targets: tuple = ()
    leverage: float | None = None

    expected_fee_usd: float | None = None
    expected_funding_usd: float | None = None
    expected_cost_usd: float | None = None
    expected_r: float | None = None
    confidence: float | None = None

    # Evidence AS OF the recommendation. Frozen, never recomputed — a
    # decision rebuilt from today's tables is the decision you would make
    # now, which is not the one being judged.
    evidence_ref: dict = field(default_factory=dict)

    version: str = MANUAL_EXECUTION_VERSION

    def as_dict(self) -> dict:
        return asdict(self)


# ── Execution facts ──────────────────────────────────────────────────────
@dataclass
class ManualExecutionLeg:
    """One fill the operator actually got. Never a strategy outcome.

    A trade may carry many: an initial entry, scale-ins, partial exits, a
    final exit. They are ordered by `at`, because the running cost basis
    depends on the order the market filled them in and nothing else.
    """

    kind: str
    quantity: float
    fill_price: float
    at: str

    # UNKNOWN STAYS None. A leg whose fee was not evidenced does not get a
    # zero; it makes the trade's net P&L UNKNOWN, which is the truth.
    fee_usd: float | None = None
    liquidity_role: str = UNKNOWN

    # What the operator MEANT to do, where they said so. Its gap from
    # `fill_price` is manual slippage and is attribution, never a charge —
    # the difference is already inside the fill.
    decision_price: float | None = None

    leg_id: str | None = None
    venue_order_ref: str | None = None
    exit_reason: str | None = None
    evidence_type: str | None = None
    evidence_source: str | None = None
    notes: str = ""

    def __post_init__(self):
        if self.kind not in LEG_KINDS:
            raise ManualExecutionError(
                f"{self.kind!r} is not a leg kind. Known: "
                f"{', '.join(sorted(LEG_KINDS))}")
        self.quantity = _positive(self.quantity, "leg quantity")
        self.fill_price = _positive(self.fill_price, "leg fill_price")
        self.at = _ts(self.at, "leg timestamp")
        self.fee_usd = _non_negative_or_none(self.fee_usd, "leg fee_usd")
        if self.decision_price is not None:
            self.decision_price = _positive(
                self.decision_price, "leg decision_price")
        if self.liquidity_role not in LIQUIDITY_ROLES:
            raise ManualExecutionError(
                f"{self.liquidity_role!r} is not a liquidity role. Known: "
                f"{', '.join(sorted(LIQUIDITY_ROLES))}")

    @property
    def is_exit(self) -> bool:
        return self.kind in EXIT_LEG_KINDS

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class ManualCostEvent:
    """Money that moved without a fill.

    Funding settlements, fee rebates, a liquidation penalty, gas. These
    have an amount and a time and no price, which is exactly why they are
    not legs.
    """

    kind: str
    amount_usd: float
    at: str

    event_id: str | None = None
    evidence_type: str | None = None
    evidence_source: str | None = None
    notes: str = ""

    def __post_init__(self):
        if self.kind not in COST_EVENT_KINDS:
            raise ManualExecutionError(
                f"{self.kind!r} is not a cost event kind. Known: "
                f"{', '.join(sorted(COST_EVENT_KINDS))}")
        # MAGNITUDE, not sign. Direction is carried by the KIND — funding
        # paid and funding received are different events, not one event
        # with a sign the operator has to get right. A negative amount here
        # would silently invert a real payment.
        self.amount_usd = _positive(self.amount_usd, f"{self.kind} amount_usd")
        self.at = _ts(self.at, f"{self.kind} timestamp")

    @property
    def canonical_field(self) -> str:
        return _COST_EVENT_MAP[self.kind][0]

    @property
    def signed_usd(self) -> float:
        """Contribution to its canonical cost field, sign included."""
        return self.amount_usd * _COST_EVENT_MAP[self.kind][1]

    def as_dict(self) -> dict:
        return asdict(self)


# ── The trade ────────────────────────────────────────────────────────────
@dataclass
class ManualTrade:
    """One manually executed trade: identity, evidence, legs and costs.

    Everything economic is derived from the legs and cost events. Nothing
    is stored alongside them as a second copy, so a ledger and a summary
    cannot disagree.
    """

    trade_id: str
    venue: str
    product: str
    symbol: str
    direction: str
    quantity_unit: str

    account_label: str = "default"
    instrument_id: str | None = None
    multiplier: float = 1.0

    state: str = DRAFT
    opened_at: str | None = None
    closed_at: str | None = None

    leverage: float | None = None
    margin_mode: str = UNKNOWN
    collateral_usd: float | None = None
    # WHOSE MONEY the collateral is. Defaults to UNKNOWN, which is NOT
    # owned — see lib/account_economics. A deposit-match promotion does not
    # become equity because a trade used it as margin.
    collateral_capital_kind: str = "CAPITAL_UNKNOWN"

    stop_used: float | None = None
    targets_used: tuple = ()
    initial_risk_usd: float | None = None

    # The JARVIS side. None means the operator traded independently, and
    # that is a legitimate, complete record — not a missing field.
    recommendation: RecommendationSnapshot | None = None

    legs: list = field(default_factory=list)
    cost_events: list = field(default_factory=list)

    # COST CATEGORIES THE OPERATOR AFFIRMATIVELY STATES WERE NOT CHARGED.
    #
    # This is the difference between "we never found out" and "we checked,
    # and it was zero" — and the two must not look alike. A promotional
    # zero-commission window really does charge no commission, and a spot
    # equity trade that incurred no regulatory fee really incurred none.
    # Without this, the honest UNKNOWN rule would make every such trade's
    # net P&L permanently unknowable, which pressures a future maintainer
    # into weakening the rule itself.
    #
    # A DECLARATION IS EVIDENCE, NOT AN ASSUMPTION. It is recorded with the
    # trade's provenance, and declaring a category that also carries a
    # charge is a contradiction and refuses.
    declared_absent_costs: tuple = ()

    # What the venue itself said the trade made. PRESERVED AS ITS OWN FACT,
    # never forced to agree with the component sum — see `reconciliation()`.
    operator_reported_realized_pnl_usd: float | None = None
    operator_reported_evidence_type: str | None = None

    evidence_type: str | None = None
    evidence_source: str | None = None
    notes: str = ""

    engine_epoch: str | None = None
    version: str = MANUAL_EXECUTION_VERSION

    def __post_init__(self):
        from lib.trade_side import parse_side_strict

        if self.product not in PRODUCTS:
            raise ManualExecutionError(
                f"{self.product!r} is not a supported product. Known: "
                f"{', '.join(sorted(PRODUCTS))}. Options are deliberately "
                f"absent: the instrument taxonomy carries no option "
                f"contract identity, and recording one as a near-miss "
                f"product would price it with the wrong economics")
        if self.quantity_unit not in QUANTITY_UNITS:
            raise ManualExecutionError(
                f"{self.quantity_unit!r} is not a quantity unit. Known: "
                f"{', '.join(sorted(QUANTITY_UNITS))}")
        side = parse_side_strict(self.direction)
        if side is None:
            raise ManualExecutionError(
                f"direction {self.direction!r} states no side. A trade "
                f"whose direction cannot be read has no sign, and assuming "
                f"long would record the opposite of what happened")
        self.direction = side

        # THE MULTIPLIER MUST BE STATED FOR CONTRACTS. 26 contracts at a
        # 0.01 multiplier are not 26 coins, and that exact confusion once
        # priced a position 100x high and fed it to the risk backstop.
        if self.quantity_unit == CONTRACTS and self.multiplier in (None, 1.0):
            raise ManualExecutionError(
                f"{self.symbol} is counted in CONTRACTS, so its multiplier "
                f"must be stated explicitly. Defaulting to 1.0 would price "
                f"the contract as though one unit were one coin or share")
        self.multiplier = _positive(self.multiplier, "multiplier")

        if self.leverage is not None:
            lev = _positive(self.leverage, "leverage")
            if lev > 1000.0:
                raise ManualExecutionError(
                    f"leverage {lev:g}x exceeds anything a venue offers; "
                    f"this is a typo, and guessing the intended value would "
                    f"put a fabricated number in the risk record")
            self.leverage = lev
        if self.margin_mode not in MARGIN_MODES:
            raise ManualExecutionError(
                f"{self.margin_mode!r} is not a margin mode. Known: "
                f"{', '.join(sorted(MARGIN_MODES))}")
        if self.state not in STATES:
            raise ManualExecutionError(f"{self.state!r} is not a state")

        self.collateral_usd = _non_negative_or_none(
            self.collateral_usd, "collateral_usd")
        self.initial_risk_usd = _non_negative_or_none(
            self.initial_risk_usd, "initial_risk_usd")
        if self.stop_used is not None:
            self.stop_used = _positive(self.stop_used, "stop_used")
        self.opened_at = _ts(self.opened_at, "opened_at", required=False)
        self.closed_at = _ts(self.closed_at, "closed_at", required=False)
        if self.operator_reported_realized_pnl_usd is not None:
            # A reported P&L may legitimately be negative.
            self.operator_reported_realized_pnl_usd = _finite(
                self.operator_reported_realized_pnl_usd,
                "operator_reported_realized_pnl_usd")

        # Fail on an unknown capital kind rather than silently defaulting
        # to something ownable.
        from lib.account_economics import COST_CATEGORIES, capital_spec
        capital_spec(self.collateral_capital_kind)

        self.declared_absent_costs = tuple(self.declared_absent_costs or ())
        bad = [c for c in self.declared_absent_costs
               if c not in COST_CATEGORIES]
        if bad:
            raise ManualExecutionError(
                f"{bad!r} are not cost categories, so they cannot be "
                f"declared absent. Known: {', '.join(COST_CATEGORIES)}")

    # ── Derived identity ────────────────────────────────────────────────
    @property
    def sign(self) -> int:
        return -1 if self.direction == SHORT else 1

    @property
    def thesis_id(self) -> str | None:
        """The claim this trade responded to, or None. NEVER fabricated.

        Note that a DISAGREEMENT keeps the link: if JARVIS said short and
        the operator went long, `thesis_id` still points at JARVIS's claim
        while `direction` records what the human actually did. Deriving a
        fresh thesis from the operator's side instead would hide the
        disagreement, which is the most informative case there is.
        """
        return self.recommendation.thesis_id if self.recommendation else None

    @property
    def ordered_legs(self) -> list:
        """Chronological. The cost basis depends on fill order, not entry order."""
        return sorted(self.legs, key=lambda l: (l.at, 0 if not l.is_exit else 1))

    # ── The economics ───────────────────────────────────────────────────
    def _walk(self) -> dict:
        """Run the book forward leg by leg. THE deterministic authority.

        Weighted-average cost, recomputed as entries arrive, so a scale-in
        after a partial exit is basis-correct rather than approximated.
        Each exit leg's gross is settled against the basis in force AT THAT
        MOMENT, which is what a venue does and what a later recomputation
        from a single blended entry price cannot reproduce.
        """
        open_qty = 0.0
        wac = 0.0
        entry_qty_total = 0.0
        entry_notional = 0.0
        per_leg: list = []

        for leg in self.ordered_legs:
            if leg.kind == LEG_ENTRY:
                new_qty = open_qty + leg.quantity
                wac = ((wac * open_qty) + (leg.fill_price * leg.quantity)) / new_qty
                open_qty = new_qty
                entry_qty_total += leg.quantity
                entry_notional += leg.fill_price * leg.quantity
                per_leg.append({"leg": leg, "gross_pnl_usd": 0.0,
                                "basis_price": wac,
                                "remaining_qty_after": open_qty})
                continue

            if leg.quantity > open_qty + QTY_TOL:
                raise ManualExecutionError(
                    f"exit leg at {leg.at} closes {leg.quantity:g} but only "
                    f"{open_qty:g} is open. A book cannot close what it "
                    f"never held, and netting the excess into a reverse "
                    f"position would invent a trade the operator never made")
            gross = ((leg.fill_price - wac) * leg.quantity
                     * self.multiplier * self.sign)
            open_qty = max(0.0, open_qty - leg.quantity)
            per_leg.append({"leg": leg, "gross_pnl_usd": gross,
                            "basis_price": wac,
                            "remaining_qty_after": open_qty})

        return {
            "per_leg": per_leg,
            "open_quantity": open_qty,
            "entry_quantity": entry_qty_total,
            "entry_vwap": (entry_notional / entry_qty_total
                           if entry_qty_total else None),
            "cost_basis": wac if entry_qty_total else None,
        }

    @property
    def open_quantity(self) -> float:
        return self._walk()["open_quantity"]

    @property
    def closed_quantity(self) -> float:
        return sum(l.quantity for l in self.legs if l.is_exit)

    @property
    def is_flat(self) -> bool:
        return (bool(self.legs)
                and self._walk()["open_quantity"] <= QTY_TOL)

    @property
    def entry_vwap(self) -> float | None:
        return self._walk()["entry_vwap"]

    @property
    def exit_vwap(self) -> float | None:
        """Quantity-weighted exit price. DISPLAY, never the P&L authority."""
        exits = [l for l in self.legs if l.is_exit]
        q = sum(l.quantity for l in exits)
        if not q:
            return None
        return sum(l.fill_price * l.quantity for l in exits) / q

    @property
    def gross_pnl_usd(self) -> float | None:
        """Realized gross on the CLOSED quantity. None until something closed.

        Deliberately excludes open quantity: unrealized movement is not a
        realized result and must never reach a learning row.
        """
        walk = self._walk()
        exits = [r for r in walk["per_leg"] if r["leg"].is_exit]
        if not exits:
            return None
        return sum(r["gross_pnl_usd"] for r in exits)

    # ── Costs, and the categories that are allowed to exist ─────────────
    def applicable_cost_categories(self) -> tuple:
        """Which explicit costs this PRODUCT can actually incur.

        Delegated to `lib.product_cost_profile`, the authority that already
        refuses the forbidden inferences: leverage does not imply borrowing,
        a short does not imply a borrowed underlying, and funding is not
        interest. A category that cannot exist for this product is not
        UNKNOWN — it is structurally absent, and demanding evidence for it
        would make every perpetual's net P&L permanently unknowable.
        """
        from lib.product_cost_profile import (borrowing_applies,
                                              funding_applies, profile_for,
                                              NETWORK_GAS, NOT_APPLICABLE)

        prof = profile_for(self.product)
        cats = ["commission_usd"]
        if funding_applies(self.product):
            cats.append("funding_usd")
        if borrowing_applies(self.product, is_short=self.direction == SHORT):
            cats.append("borrow_cost_usd")
        if prof.network_fee_model == NETWORK_GAS:
            cats.append("network_fees_usd")
        if prof.regulatory_fee_model != NOT_APPLICABLE:
            cats.append("regulatory_fees_usd")
        return tuple(cats)

    def costs_usd(self) -> dict:
        """Every explicit charge, by canonical field. None means UNKNOWN.

        Commission is the sum of the legs' fees — and it is UNKNOWN if ANY
        leg's fee is unevidenced, because a partial sum of a total is not
        the total. A rebate reduces it; that is why a rebate is a separate
        event kind rather than a negative fee.
        """
        out: dict = {}
        applicable = set(self.applicable_cost_categories())
        declared = set(self.declared_absent_costs)

        leg_fees = [l.fee_usd for l in self.legs]
        if leg_fees and all(f is not None for f in leg_fees):
            out["commission_usd"] = float(sum(leg_fees))
        elif "commission_usd" in declared:
            # A zero-fee promotional window, declared rather than inferred.
            out["commission_usd"] = 0.0
        else:
            out["commission_usd"] = None

        charged: set = set()
        if any(f for f in leg_fees if f):
            # A leg that charged a fee is itself a commission charge, so a
            # declaration of absence contradicts it just as an event would.
            charged.add("commission_usd")
        for ev in self.cost_events:
            fld = ev.canonical_field
            charged.add(fld)
            if fld in out and out.get(fld) is None:
                # An UNKNOWN base cannot be added to. Adding a known rebate
                # to an unknown commission would report the rebate as the
                # whole cost.
                continue
            out[fld] = float(out.get(fld) or 0.0) + ev.signed_usd

        # A DECLARATION AND A CHARGE CANNOT BOTH BE TRUE.
        contradiction = sorted(declared & charged)
        if contradiction:
            raise ManualExecutionError(
                f"trade {self.trade_id} declares {contradiction} absent "
                f"while also carrying a charge in {'that category' if len(contradiction) == 1 else 'those categories'}. "
                f"One of the two is wrong, and this program will not choose "
                f"which")

        for cat in declared:
            out.setdefault(cat, 0.0)

        # Categories the product HAS but nothing evidenced -> UNKNOWN.
        for cat in applicable:
            out.setdefault(cat, None)
        return out

    def unknown_cost_categories(self) -> tuple:
        """Applicable costs with no evidence. Non-empty means net is UNKNOWN."""
        costs = self.costs_usd()
        return tuple(sorted(c for c in self.applicable_cost_categories()
                            if costs.get(c) is None))

    @property
    def explicit_costs_usd(self) -> float | None:
        costs = self.costs_usd()
        if self.unknown_cost_categories():
            return None
        return float(sum(v for v in costs.values() if v is not None))

    @property
    def net_pnl_usd(self) -> float | None:
        """Gross minus every explicit charge — or None.

        NONE IS THE POINT. If a fee or a funding payment this product
        actually incurs was never evidenced, the net result is not known,
        and reporting gross-minus-what-we-happen-to-have would understate
        the cost of every trade whose paperwork was incomplete. The
        operator's own reported figure, when supplied, is preserved
        separately and reconciled — never substituted here.
        """
        gross = self.gross_pnl_usd
        costs = self.explicit_costs_usd
        if gross is None or costs is None:
            return None
        return gross - costs

    @property
    def net_r(self) -> float | None:
        net = self.net_pnl_usd
        if net is None or not self.initial_risk_usd:
            return None
        return net / float(self.initial_risk_usd)

    @property
    def owned_collateral_usd(self) -> float | None:
        """How much of the margin is actually the operator's money."""
        from lib.account_economics import owned_amount_usd

        return owned_amount_usd(self.collateral_usd,
                                self.collateral_capital_kind)

    # ── Reconciliation ──────────────────────────────────────────────────
    def reconciliation(self) -> dict:
        """Operator/venue-reported realized P&L vs the component sum.

        NEITHER SIDE WINS. A venue's own figure is strong evidence about
        what the account received; the component sum is what this system
        can account for. Forcing them equal — in either direction — deletes
        the only measurement of what the cost model still does not know.
        The vocabulary is `lib/venue_reconciliation.py`'s, not a new one.
        """
        from lib.venue_reconciliation import (RECONCILIATION_MODEL_VERSION,
                                              UNKNOWN as VR_UNKNOWN)

        reported = self.operator_reported_realized_pnl_usd
        derived = self.net_pnl_usd
        base = {
            "reconciliation_model_version": RECONCILIATION_MODEL_VERSION,
            "manual_execution_version": MANUAL_EXECUTION_VERSION,
            "operator_reported_realized_pnl_usd": reported,
            "component_derived_net_pnl_usd": derived,
            "operator_reported_evidence_type":
                self.operator_reported_evidence_type,
        }
        if reported is None and derived is None:
            return {**base, "status": VR_UNKNOWN,
                    "detail": ("neither a reported figure nor a complete "
                               "component set exists")}
        if reported is None:
            return {**base, "status": "COMPONENT_ONLY",
                    "detail": ("no venue-reported figure to check the "
                               "components against")}
        if derived is None:
            return {**base, "status": "MODEL_INCOMPLETE",
                    "unknown_cost_categories":
                        list(self.unknown_cost_categories()),
                    "detail": ("the reported figure stands on its own; the "
                               "components cannot be summed because a cost "
                               "this product incurs was never evidenced. "
                               "The reported value is NOT back-solved into "
                               "the missing category — that would invent a "
                               "measurement")}
        delta = reported - derived
        notional = None
        if self.entry_vwap and self._walk()["entry_quantity"]:
            notional = abs(self.entry_vwap * self._walk()["entry_quantity"]
                           * self.multiplier)
        status = ("RECONCILED" if abs(delta) <= 0.005
                  else "UNEXPLAINED_VENUE_COST")
        return {
            **base,
            "status": status,
            "delta_usd": round(delta, 6),
            "delta_bps": (round(1e4 * delta / notional, 3)
                          if notional else VR_UNKNOWN),
            "detail": ("the venue charged or credited something the "
                       "component model does not represent; preserved as "
                       "unexplained rather than absorbed into a fitted "
                       "cost" if status == "UNEXPLAINED_VENUE_COST" else
                       "the components account for the reported figure"),
        }

    # ── Attribution against the recommendation ──────────────────────────
    def recommendation_vs_actual(self) -> dict:
        """The gap between what JARVIS said and what the operator got.

        Reported as PAIRS, never as a single blended difference: the whole
        point is to tell a sound thesis executed late apart from a thesis
        with no edge, and one number cannot say which happened.
        """
        rec = self.recommendation
        if rec is None:
            return {"linked": False,
                    "detail": ("the operator traded independently; there is "
                               "no recommendation to compare against, and "
                               "no thesis is invented to supply one")}
        actual_entry = self.entry_vwap
        entry_gap = (None if (rec.entry is None or actual_entry is None)
                     else actual_entry - rec.entry)
        costs = self.costs_usd()
        return {
            "linked": True,
            "thesis_id": rec.thesis_id,
            "direction": {"recommended": rec.direction,
                          "actual": self.direction,
                          "followed": (rec.direction is not None
                                       and rec.direction == self.direction)},
            "venue": {"recommended": rec.venue, "actual": self.venue},
            "product": {"recommended": rec.product, "actual": self.product},
            "entry": {"recommended": rec.entry, "actual": actual_entry,
                      "gap": entry_gap},
            "stop": {"recommended": rec.stop, "actual": self.stop_used},
            "leverage": {"recommended": rec.leverage,
                         "actual": self.leverage},
            "fee_usd": {"expected": rec.expected_fee_usd,
                        "actual": costs.get("commission_usd")},
            "funding_usd": {"expected": rec.expected_funding_usd,
                            "actual": costs.get("funding_usd")},
            "r": {"expected": rec.expected_r, "realized": self.net_r},
            "note": ("expected and actual are separate facts and neither is "
                     "overwritten by the other; the recommendation is frozen "
                     "at recommendation time"),
        }

    def as_dict(self) -> dict:
        walk = self._walk()
        return {
            "trade_id": self.trade_id,
            "execution_mode": _manual_mode(),
            "submitted_by_jarvis": False,
            "account_label": self.account_label,
            "venue": self.venue,
            "product": self.product,
            "symbol": self.symbol,
            "instrument_id": self.instrument_id,
            "direction": self.direction,
            "quantity_unit": self.quantity_unit,
            "multiplier": self.multiplier,
            "state": self.state,
            "opened_at": self.opened_at,
            "closed_at": self.closed_at,
            "leverage": self.leverage,
            "margin_mode": self.margin_mode,
            "collateral_usd": self.collateral_usd,
            "collateral_capital_kind": self.collateral_capital_kind,
            "owned_collateral_usd": self.owned_collateral_usd,
            "stop_used": self.stop_used,
            "targets_used": list(self.targets_used),
            "initial_risk_usd": self.initial_risk_usd,
            "thesis_id": self.thesis_id,
            "recommendation": (self.recommendation.as_dict()
                               if self.recommendation else None),
            "legs": [l.as_dict() for l in self.ordered_legs],
            "cost_events": [e.as_dict() for e in self.cost_events],
            "entry_quantity": walk["entry_quantity"],
            "closed_quantity": self.closed_quantity,
            "open_quantity": walk["open_quantity"],
            "entry_vwap": walk["entry_vwap"],
            "exit_vwap": self.exit_vwap,
            "cost_basis": walk["cost_basis"],
            "pnl_basis": PNL_BASIS,
            "gross_pnl_usd": self.gross_pnl_usd,
            "costs_usd": self.costs_usd(),
            "applicable_cost_categories": list(
                self.applicable_cost_categories()),
            "declared_absent_costs": list(self.declared_absent_costs),
            "unknown_cost_categories": list(self.unknown_cost_categories()),
            "net_pnl_usd": self.net_pnl_usd,
            "net_r": self.net_r,
            "reconciliation": self.reconciliation(),
            "recommendation_vs_actual": self.recommendation_vs_actual(),
            "evidence_type": self.evidence_type,
            "evidence_source": self.evidence_source,
            "engine_epoch": self.engine_epoch,
            "version": self.version,
        }


def _manual_mode() -> str:
    from lib.execution_mode import MANUAL_OPERATOR

    return MANUAL_OPERATOR


# ── The canonical outcome ────────────────────────────────────────────────
def realized_outcome(trade: ManualTrade):
    """Build the canonical `RealizedOutcome` from ACTUAL manual economics.

    REFUSES ON AN INCOMPLETE TRADE. A partial close has realized cash and
    an open remainder; turning that into a final outcome would let one
    thesis vote before it finished, and vote again when it did. The trade
    must be flat and CLOSED.

    Gross comes from the leg walk, never recomputed from a single blended
    entry and exit — the same discipline `realized_outcome.build_from_
    settlement` applies to the virtual ledger, for the same reason.

    The source is `MANUAL_OPERATOR`, so learning can always tell a human's
    execution from the executor it is trying to measure.
    """
    from lib import realized_outcome as ro
    from lib.execution_mode import MANUAL_OPERATOR, outcome_source

    if trade.state != CLOSED:
        raise IncompleteManualTrade(
            f"trade {trade.trade_id} is {trade.state}, not {CLOSED}. A "
            f"realized outcome is the FINAL result of a position; producing "
            f"one now would record a verdict the trade has not reached")
    if not trade.is_flat:
        raise IncompleteManualTrade(
            f"trade {trade.trade_id} still holds {trade.open_quantity:g} "
            f"{trade.quantity_unit}; a position with quantity on is not "
            f"realized")

    gross = trade.gross_pnl_usd
    if gross is None:
        raise IncompleteManualTrade(
            f"trade {trade.trade_id} has no exit legs, so no gross result "
            f"exists to realize")

    costs = trade.costs_usd()
    unknown = trade.unknown_cost_categories()

    # WHERE EACH DOLLAR CAME FROM. Two cost-event kinds have no dedicated
    # canonical field and are folded into commission_usd; this map is what
    # makes that fold auditable instead of lossy.
    category_map: dict = {}
    for ev in trade.cost_events:
        category_map.setdefault(ev.canonical_field, {}).setdefault(
            ev.kind, 0.0)
        category_map[ev.canonical_field][ev.kind] += ev.signed_usd

    o = ro.RealizedOutcome(
        thesis_id=trade.thesis_id,
        signal_id=(trade.recommendation.signal_id
                   if trade.recommendation else None),
        position_id=trade.trade_id,
        source=outcome_source(MANUAL_OPERATOR),
        venue_type=("DEX" if trade.product == DEX_SPOT else "CEX"),
        venue=trade.venue,
        product=trade.product,
        instrument_id=trade.instrument_id,
        symbol=trade.symbol,
        side=trade.direction,
        quantity=trade._walk()["entry_quantity"],
        quantity_unit=trade.quantity_unit,
        multiplier=trade.multiplier,
        decision_entry_price=(trade.recommendation.entry
                              if trade.recommendation else None),
        actual_entry_fill=float(trade.entry_vwap or 0.0),
        actual_exit_fill=float(trade.exit_vwap or 0.0),
        gross_pnl_usd=float(gross),
        commission_usd=float(costs.get("commission_usd") or 0.0),
        regulatory_fees_usd=float(costs.get("regulatory_fees_usd") or 0.0),
        network_fees_usd=float(costs.get("network_fees_usd") or 0.0),
        funding_usd=float(costs.get("funding_usd") or 0.0),
        borrow_cost_usd=float(costs.get("borrow_cost_usd") or 0.0),
        rollover_usd=float(costs.get("rollover_usd") or 0.0),
        initial_risk_usd=trade.initial_risk_usd,
        exit_reason=_final_exit_reason(trade),
        opened_at=trade.opened_at,
        closed_at=trade.closed_at,
        hold_minutes=_hold_minutes(trade),
        engine_epoch=trade.engine_epoch,
        execution_model=MANUAL_EXECUTION_VERSION,
        cost_model_version=MANUAL_EXECUTION_VERSION,
        provenance={
            "execution_mode": MANUAL_OPERATOR,
            # THE BIT THAT MUST NEVER BE TRUE HERE.
            "submitted_by_jarvis": False,
            "pnl_basis": PNL_BASIS,
            "leg_count": len(trade.legs),
            "cost_event_count": len(trade.cost_events),
            "cost_category_map": category_map,
            "declared_absent_costs": list(trade.declared_absent_costs),
            "unknown_cost_categories": list(unknown),
            # An outcome built over an unevidenced cost is NOT complete,
            # and says so rather than looking identical to one that is.
            "net_pnl_is_complete": not unknown,
            "operator_reported_realized_pnl_usd":
                trade.operator_reported_realized_pnl_usd,
            "reconciliation": trade.reconciliation(),
            "account_label": trade.account_label,
            "collateral_capital_kind": trade.collateral_capital_kind,
            "evidence_type": trade.evidence_type,
            "evidence_source": trade.evidence_source,
        },
    )
    ro.finalize(o)

    # Return-on-margin, stated with its basis — and ONLY where the margin
    # was real. R IS NOT PERCENT, and a percentage of an unknown
    # denominator is not a percentage.
    if trade.collateral_usd:
        o.gross_return_pct = o.gross_pnl_usd / float(trade.collateral_usd) * 100.0
        o.net_return_pct = o.net_pnl_usd / float(trade.collateral_usd) * 100.0
        o.return_pct_basis = "MARGIN"

    # Manual slippage against the recommendation: attribution, not a charge.
    if trade.recommendation and trade.recommendation.entry:
        ro.attribute_execution(o, decision_entry=trade.recommendation.entry)
    return o


def _final_exit_reason(trade: ManualTrade) -> str | None:
    exits = [l for l in trade.ordered_legs if l.is_exit]
    return exits[-1].exit_reason if exits else None


def _hold_minutes(trade: ManualTrade) -> float | None:
    t0, t1 = _as_dt(trade.opened_at), _as_dt(trade.closed_at)
    if t0 is None or t1 is None:
        return None
    return (t1 - t0).total_seconds() / 60.0


def arm_result(trade: ManualTrade):
    """The operator's arm on the thesis. ONE THESIS, ONE SAMPLE.

    Returned as `lib.trade_thesis.ArmResult` with `arm=OPERATOR`, so
    `sample_count` continues to count DISTINCT THESES: an operator trade
    alongside the agent's trade on the same claim is one market
    observation with two policy results, exactly like the shadow arm.

    Returns None for an unlinked trade — there is no claim to be an arm of.
    """
    from lib.trade_thesis import ArmResult, OPERATOR

    tid = trade.thesis_id
    if not tid:
        return None
    return ArmResult(
        thesis_id=tid,
        arm=OPERATOR,
        traded=bool(trade.legs),
        signal_id=(trade.recommendation.signal_id
                   if trade.recommendation else None),
        execution_id=trade.trade_id,
        policy=trade.account_label,
        net_r=trade.net_r,
        net_pnl_usd=trade.net_pnl_usd,
        entry_fill=trade.entry_vwap,
        exit_reason=_final_exit_reason(trade),
        venue_type=("DEX" if trade.product == DEX_SPOT else "CEX"),
    )
