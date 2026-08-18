"""What a paper position actually costs, per leg, with the units kept apart.

Two cost models live here on purpose. They are not versions of one idea; they
are different economics, and 667 existing positions were opened under the
older one.

── LEGACY (cost_model = legacy_round_trip_v1) ────────────────────────────

What the code actually does today, established by reading it rather than by
reading its comments. `PaperPosition.fees` is described as a "reserve", but
no fee dollars ever leave cash at entry:

    ENTRY      cash = C0 - margin
               PaperPosition.fees = F_rt      (estimated ROUND TRIP)

    FINAL      pnl  = gross - F_rt_remaining - funding
               cash += margin + pnl

So the stored fee is a DEFERRED ROUND-TRIP CHARGE, settled at close, not a
reservation. The full-close identity is therefore:

    C_final = C0 + gross - F_rt - funding

TWO DEFECTS IN THAT MODEL, both confirmed in code and both fixed for the
canonical model below.

1. PARTIAL EXITS PAY NO FUNDING. `_funding_cost_usd()` derives its holding
   period from `age_minutes(opened_at)` — always to NOW — and the partial
   close path never calls it at all. Final close then charges funding on the
   REMAINING quantity only. Quantity that scaled out early therefore holds
   for days and pays zero funding.

2. A SCALE-OUT VOTES TWICE. Partial close increments `total_trades` and
   possibly `winning_trades`, and calls the learning recorder; final close
   does all of it again. One thesis becomes two trades and two observations
   purely because it scaled out. ONE THESIS DOES NOT VOTE TWICE.

Legacy positions keep legacy fee treatment, because `fees` cannot be
decomposed into entry and exit halves after the fact and inventing that
split would be fabrication. Their outcomes are labelled mixed and must not
calibrate the new executor.

── CANONICAL (cost_model = per_leg_v2) ───────────────────────────────────

An estimate is an estimate; a charge is a charge.

    estimated_round_trip   planning and the expectancy gate ONLY. Never cash.
    entry fee              from the actual entry fill. Charged once, at entry.
    exit fee               from EACH actual exit fill. Charged once per leg.
    holding cost           accrued over the quantity AND interval actually
                           held, so a scale-out pays for the days it was on.

    ENTRY      cash = C0 - actual_margin - entry_fee
    EXIT LEG   cash += released_margin + gross_leg - exit_fee_leg - funding_leg
    IDENTITY   C_final = C0 + sum(gross) - entry_fee - sum(exit_fees)
                              - sum(funding)

That identity is asserted directly in the tests, not argued for here.

SPREAD, SLIPPAGE AND IMPACT ARE NOT IN ANY OF THAT. They are already inside
the fill prices, so gross carries them. Charging them again as costs would
bill the same economics twice.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

COST_MODEL_LEGACY = "legacy_round_trip_v1"
COST_MODEL_CANONICAL = "per_leg_v2"

# The execution model these costs belong to. Bumped because entry and exit
# prices, fill quantities and cost timing all changed materially; pooling
# outcomes across the boundary would compare two different simulators.
EXECUTION_MODEL_LEGACY = "virtual_cex_direct_mark_v1"
EXECUTION_MODEL_CANONICAL = "virtual_cex_venue_book_v2"

# Leg kinds, so a ledger row is never mistaken for a strategy vote.
LEG_ENTRY = "ENTRY"
LEG_PARTIAL_EXIT = "PARTIAL_EXIT"
LEG_FINAL_EXIT = "FINAL_EXIT"

# The durable ledger's version (B1). Stamped on every NEW canonical
# settlement header and leg at write time; never inferred for historical
# positions — absence means legacy, exactly like NULL provenance.
SETTLEMENT_VERSION = "paper_settlement_v1"

# Canonical leg execution sides. Deterministic from the entry order — a LONG
# position enters by BUYING, a SHORT by SELLING — never inferred from price
# movement.
EXECUTION_SIDE_BUY = "buy"
EXECUTION_SIDE_SELL = "sell"


def funding_for_interval(symbol: str, notional: float, is_short: bool,
                         hours_held: float) -> float:
    """Funding for an EXPLICIT interval, never for "until now".

    The defect this replaces read the clock: `age_minutes(opened_at)`. That
    is wrong twice over for a scale-out — the exited quantity stopped
    accruing when it left, and the wall clock cannot be replayed in a test.
    Passing the interval makes both deterministic.

    Funding is a TRANSFER, so this may legitimately be negative and IMPROVE
    a short's result. A model that always charges it as a cost understates
    every short.
    """
    if not notional or hours_held is None or hours_held <= 0:
        return 0.0
    try:
        from lib.transaction_costs import funding_cost_pct
        pct, _src = funding_cost_pct(symbol, float(hours_held), is_short=is_short)
        return float(pct) * abs(float(notional))
    except Exception as e:
        logger.debug("[Settle] funding unavailable for %s: %s", symbol, e)
        return 0.0


@dataclass
class SettlementLeg:
    """One executed leg and the cash it moved. Never a strategy outcome."""

    kind: str
    quantity: float = 0.0
    fill_price: float = 0.0
    gross_pnl_usd: float = 0.0
    explicit_fee_usd: float = 0.0       # commission etc. for THIS leg
    funding_usd: float = 0.0            # for THIS quantity over ITS interval
    released_margin_usd: float = 0.0
    hours_held: float = 0.0
    execution_id: str | None = None
    at: str | None = None

    @property
    def cash_delta(self) -> float:
        """What this leg moves in the account."""
        return (self.released_margin_usd + self.gross_pnl_usd
                - self.explicit_fee_usd - self.funding_usd)

    @property
    def net_pnl_usd(self) -> float:
        """P&L for the leg, excluding the margin that was only ever ours."""
        return self.gross_pnl_usd - self.explicit_fee_usd - self.funding_usd


@dataclass
class PositionSettlement:
    """Every leg of one position, and the totals they imply.

    ACCOUNTING realises cash incrementally, leg by leg. LEARNING votes once,
    from `net_pnl_usd` below, when the position is fully resolved. Those are
    deliberately different operations on the same data and must not share a
    counter.
    """

    position_id: str | None = None
    cost_model: str = COST_MODEL_CANONICAL
    execution_model: str = EXECUTION_MODEL_CANONICAL
    entry_fee_usd: float = 0.0
    committed_margin_usd: float = 0.0
    legs: list = field(default_factory=list)

    def add(self, leg: SettlementLeg) -> SettlementLeg:
        self.legs.append(leg)
        return leg

    # ── Aggregates, computed from the legs rather than tracked alongside ──
    @property
    def exit_legs(self) -> list:
        return [l for l in self.legs if l.kind != LEG_ENTRY]

    @property
    def gross_pnl_usd(self) -> float:
        return sum(l.gross_pnl_usd for l in self.exit_legs)

    @property
    def exit_fees_usd(self) -> float:
        return sum(l.explicit_fee_usd for l in self.exit_legs)

    @property
    def funding_usd(self) -> float:
        return sum(l.funding_usd for l in self.exit_legs)

    @property
    def total_explicit_cost_usd(self) -> float:
        """Entry fee charged ONCE, every exit fee once, funding once."""
        return self.entry_fee_usd + self.exit_fees_usd + self.funding_usd

    @property
    def net_pnl_usd(self) -> float:
        return self.gross_pnl_usd - self.total_explicit_cost_usd

    @property
    def closed_quantity(self) -> float:
        return sum(l.quantity for l in self.exit_legs)

    def return_pct(self, gross: bool = False) -> float | None:
        """ROI ON COMMITTED MARGIN — the paper book's documented contract.

        Not R, and not return on notional. R IS NOT PERCENT.
        """
        if not self.committed_margin_usd:
            return None
        pnl = self.gross_pnl_usd if gross else self.net_pnl_usd
        return pnl / self.committed_margin_usd * 100.0

    def cash_delta_total(self) -> float:
        """Total account movement from just before entry to full close.

        Entry moved (-margin - entry_fee); each exit leg moved its own
        cash_delta, which returns the margin it released.
        """
        entry_move = -(self.committed_margin_usd + self.entry_fee_usd)
        return entry_move + sum(l.cash_delta for l in self.exit_legs)


def settle_entry(*, committed_margin_usd: float, entry_fee_usd: float,
                 cost_model: str = COST_MODEL_CANONICAL) -> float:
    """Cash delta at entry.

    LEGACY charges no fee at entry — the stored round-trip is deferred to
    close — so passing a legacy model here refuses to take one, rather than
    silently introducing a charge those positions never paid.
    """
    if cost_model == COST_MODEL_LEGACY:
        if entry_fee_usd:
            raise ValueError(
                "the legacy cost model charges no entry fee; its stored "
                "PaperPosition.fees is a DEFERRED round-trip charge settled "
                "at close, and taking one here would double-charge")
        return -float(committed_margin_usd)
    return -(float(committed_margin_usd) + float(entry_fee_usd))


def legacy_close_fee(stored_round_trip_fee: float, closing_qty: float,
                     total_qty: float) -> float:
    """The legacy pro-rata share of the deferred round-trip charge.

    Preserved exactly, because `fees` cannot be decomposed into entry and
    exit halves after the fact. Inventing that split would be fabrication,
    and adding a new per-leg exit fee on top of it would charge the exit
    side twice.
    """
    if not total_qty:
        return float(stored_round_trip_fee or 0.0)
    share = float(closing_qty) / float(total_qty)
    return float(stored_round_trip_fee or 0.0) * share
