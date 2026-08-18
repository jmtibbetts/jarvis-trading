"""What a trade actually made — computed ONCE, by whoever executed it.

THE EXCHANGE DECIDES. LEARNING RECORDS.

`learning_engine.record_trade_outcome` re-derived P&L from entry, exit,
direction and qty, and got it wrong in two independent ways:

    if direction.upper() in ("SELL", "SHORT", "SELL_SHORT"):
        pnl_pct = -pnl_pct

"Short_10x" is not in that tuple. Neither is "Short_5x", "Short_Leveraged"
or "Bearish". Every leveraged short therefore kept a LONG sign, so a
winning short was recorded as a loss and a losing short as a win — and
those rows are what pattern memory, regime performance and calibration
learned from.

    pnl_usd = pnl_pct / 100.0 * entry_price * qty

No multiplier. One MES contract moving 10 points is $50, not $10. One gold
contract moving $10 is $1,000, not $10. Futures outcomes were understated
by the entire contract multiplier, so futures looked like a low-impact
asset class and every comparison against equities was distorted.

Both defects came from the same cause: a second implementation of
arithmetic that already existed and was already correct somewhere else.
The simulator knows the side it opened, the multiplier it sized with and
the fees it charged. Asking learning to reconstruct that from four loose
scalars is asking it to guess.

COSTS ARE CHARGED EXACTLY ONCE. Spread, slippage and impact are ATTRIBUTED
here — they are already inside the fill price, so subtracting them again
would double-count. Commissions, funding, borrow and network fees are
EXPLICIT LEDGER CHARGES and are subtracted. The two groups are kept in
separate fields precisely so nobody has to remember which is which.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field

logger = logging.getLogger(__name__)

# Bumped when the arithmetic changes materially. Outcomes from different
# versions are NOT comparable and must not be pooled.
OUTCOME_VERSION = "outcome_v1"
# Outcomes produced by the persistent multi-leg settlement ledger (B2A).
# A DISTINCT version, not a relabel: outcome_v1 rows were built from one
# entry and one synthetic exit, and stamping them v2 retroactively would
# claim ledger provenance they never had. Old builders keep OUTCOME_VERSION;
# the settlement-native builder stamps this.
SETTLEMENT_OUTCOME_VERSION = "outcome_v2_settlement"

WIN = "WIN"
LOSS = "LOSS"
BREAKEVEN = "BREAKEVEN"

# Evidence sources. `paper_mode` as a boolean could not express these, and
# they are not interchangeable observations.
VIRTUAL_CEX_AGENT = "VIRTUAL_CEX_AGENT"
VIRTUAL_DEX_AGENT = "VIRTUAL_DEX_AGENT"
SHADOW_CEX = "SHADOW_CEX"
SHADOW_DEX = "SHADOW_DEX"
REPLAY = "REPLAY"
COUNTERFACTUAL = "COUNTERFACTUAL"
BACKTEST = "BACKTEST"
LIVE_CEX = "LIVE_CEX"
LIVE_DEX = "LIVE_DEX"

SOURCES = frozenset({VIRTUAL_CEX_AGENT, VIRTUAL_DEX_AGENT, SHADOW_CEX,
                     SHADOW_DEX, REPLAY, COUNTERFACTUAL, BACKTEST,
                     LIVE_CEX, LIVE_DEX})

# Exit reasons, kept apart because they mean different things about the
# strategy. A forced liquidation is not a stop.
VOLUNTARY_EXIT = "VOLUNTARY_EXIT"
STOP_EXIT = "STOP_EXIT"
TARGET_EXIT = "TARGET_EXIT"
MARGIN_CALL = "MARGIN_CALL"
FORCED_LIQUIDATION = "FORCED_LIQUIDATION"
# An ADMINISTRATIVE portfolio operation — a book reset — not a decision the
# strategy made about this trade. It gets full financial history and no
# learning vote: teaching the desk that "the thesis exited here" because an
# operator reseeded the wallet would poison the training set with an event
# the market had no part in.
ADMINISTRATIVE_RESET = "ADMINISTRATIVE_RESET"


@dataclass
class RealizedOutcome:
    """One closed position, normalized. Learning consumes THIS."""

    # ── Identity, so an outcome can be attributed ────────────────────────
    thesis_id: str | None = None
    signal_id: str | None = None
    position_id: str | None = None

    source: str = VIRTUAL_CEX_AGENT
    venue_type: str | None = None          # CEX | DEX | SHADOW
    venue: str | None = None
    product: str | None = None
    instrument_id: str | None = None
    symbol: str | None = None

    # ── What was held ────────────────────────────────────────────────────
    side: str | None = None                # long | short, canonical
    quantity: float = 0.0
    quantity_unit: str | None = None
    multiplier: float = 1.0

    # ── Prices. Decision vs actual is what makes attribution possible ────
    decision_entry_price: float | None = None
    actual_entry_fill: float = 0.0
    decision_exit_price: float | None = None
    actual_exit_fill: float = 0.0

    # ── Money ────────────────────────────────────────────────────────────
    gross_pnl_usd: float = 0.0

    # ATTRIBUTION — already inside the fill, never subtracted again.
    spread_attribution_usd: float = 0.0
    slippage_attribution_usd: float = 0.0
    price_impact_attribution_usd: float = 0.0

    # EXPLICIT LEDGER CHARGES — subtracted from gross.
    commission_usd: float = 0.0
    regulatory_fees_usd: float = 0.0
    pool_fees_usd: float = 0.0
    network_fees_usd: float = 0.0
    funding_usd: float = 0.0
    borrow_cost_usd: float = 0.0
    rollover_usd: float = 0.0

    net_pnl_usd: float = 0.0

    initial_risk_usd: float | None = None
    gross_r: float | None = None
    net_r: float | None = None

    # ── Percentage return, and WHAT IT IS A PERCENTAGE OF ────────────────
    #
    # R IS NOT PERCENT. Learning derived `pnl_pct` as `net_r * 100`, which
    # is true only by coincidence: $50 of profit on $100 of initial risk is
    # +0.5R, and calling that "+50%" asserts a denominator nobody stated.
    # On a leveraged paper position the established contract is ROI on
    # COMMITTED MARGIN (lib/paper_engine: raw_pnl / margin * 100) — a
    # different number — and the fallback that guessed NOTIONAL produced a
    # third. Three units, one field, no way to tell them apart afterwards.
    #
    # So the producer states the number AND its basis, or states neither. A
    # percentage whose denominator is unknown is not a percentage, and
    # MISSING IS NOT ZERO.
    gross_return_pct: float | None = None
    net_return_pct: float | None = None
    return_pct_basis: str | None = None    # MARGIN | NOTIONAL | EQUITY

    outcome: str = BREAKEVEN
    exit_reason: str | None = None

    opened_at: str | None = None
    closed_at: str | None = None
    hold_minutes: float | None = None

    strategy: str | None = None
    timeframe: str | None = None
    setup_type: str | None = None

    engine_epoch: str | None = None
    outcome_version: str = OUTCOME_VERSION
    execution_model: str | None = None
    cost_model_version: str | None = None
    provenance: dict = field(default_factory=dict)

    # ── Diagnostics. NEVER the realized return ───────────────────────────
    mfe_pct: float | None = None
    mae_pct: float | None = None
    mfe_r: float | None = None
    mae_r: float | None = None

    @property
    def explicit_fees_usd(self) -> float:
        """Everything charged to the ledger. Attribution is NOT here."""
        return (self.commission_usd + self.regulatory_fees_usd
                + self.pool_fees_usd + self.network_fees_usd
                + self.funding_usd + self.borrow_cost_usd + self.rollover_usd)

    def as_dict(self) -> dict:
        return {**asdict(self), "explicit_fees_usd": self.explicit_fees_usd}


def _side_multiplier(side: str | None) -> int | None:
    from lib.trade_side import SHORT, parse_side_strict
    parsed = parse_side_strict(side)
    if parsed is None:
        return None
    return -1 if parsed == SHORT else 1


def build(*, symbol: str, direction: str, entry_fill: float, exit_fill: float,
          quantity: float, source: str = VIRTUAL_CEX_AGENT,
          instrument=None, initial_risk_usd: float | None = None,
          **kw) -> RealizedOutcome:
    """Compute a realized outcome ONCE, correctly.

    Raises on an unreadable direction rather than assuming long — an
    outcome whose sign cannot be established is not a data point, and
    recording it as a long is how the training set learns the inverse of
    what happened.
    """
    from lib.instruments import resolve

    sign = _side_multiplier(direction)
    if sign is None:
        raise ValueError(
            f"cannot realize an outcome for {symbol}: direction "
            f"{direction!r} is unreadable, and assuming long would record "
            f"the opposite of what happened")

    inst = instrument or resolve(symbol)
    mult = float(inst.multiplier or 1.0)

    # THE multiplier. One MES contract moving 10 points is $50, not $10.
    gross = (float(exit_fill) - float(entry_fill)) * float(quantity) * mult * sign

    o = RealizedOutcome(
        symbol=symbol, source=source,
        instrument_id=inst.instrument_id, product=inst.product,
        side="short" if sign < 0 else "long",
        quantity=float(quantity), quantity_unit=inst.quantity_unit,
        multiplier=mult,
        actual_entry_fill=float(entry_fill), actual_exit_fill=float(exit_fill),
        gross_pnl_usd=gross, initial_risk_usd=initial_risk_usd,
        **{k: v for k, v in kw.items()
           if k in RealizedOutcome.__dataclass_fields__},
    )
    finalize(o)
    return o


def finalize(o: RealizedOutcome) -> RealizedOutcome:
    """Net out explicit charges and derive R. Attribution is NOT subtracted.

    Spread, slippage and impact are already embedded in the fill prices, so
    charging them again would take the same cost twice — the exact
    double-count the accounting rule forbids.
    """
    o.net_pnl_usd = o.gross_pnl_usd - o.explicit_fees_usd

    if o.initial_risk_usd and o.initial_risk_usd > 0:
        o.gross_r = o.gross_pnl_usd / o.initial_risk_usd
        o.net_r = o.net_pnl_usd / o.initial_risk_usd

    # Judged on NET: a trade that made money gross and lost it to costs is
    # a losing trade, and the desk needs to learn that as one.
    if o.net_pnl_usd > 0:
        o.outcome = WIN
    elif o.net_pnl_usd < 0:
        o.outcome = LOSS
    else:
        o.outcome = BREAKEVEN
    return o


def attribute_execution(o: RealizedOutcome, *,
                        decision_entry: float | None = None,
                        decision_exit: float | None = None) -> RealizedOutcome:
    """Split the gap between the DECISION price and the ACTUAL fill.

    This is attribution, not a charge. The difference is already inside
    `gross_pnl_usd` because the fills are the real ones; recording it here
    lets JARVIS learn whether a thesis was sound but expensively executed,
    which is a different lesson from a thesis with no edge.
    """
    sign = -1 if o.side == "short" else 1
    notional_unit = float(o.quantity) * float(o.multiplier or 1.0)

    if decision_entry:
        o.decision_entry_price = float(decision_entry)
        o.slippage_attribution_usd += (
            (float(o.actual_entry_fill) - float(decision_entry))
            * notional_unit * sign)
    if decision_exit:
        o.decision_exit_price = float(decision_exit)
        o.slippage_attribution_usd += (
            (float(decision_exit) - float(o.actual_exit_fill))
            * notional_unit * sign)
    return o


def build_from_settlement(header, legs, *, strategy: str | None = None,
                          timeframe: str | None = None,
                          thesis_id: str | None = None) -> RealizedOutcome:
    """The settlement-native builder (B2A): ledger rows in, one outcome out.

    NOT `build()`. That path computes gross from one entry and one exit,
    which is correct for the single-fill world it was written in and WRONG
    for a multi-leg position: recomputing gross from a weighted exit price
    silently re-derives what the ledger already settled, and the two drift
    at the fourth decimal. Here the LEDGER IS THE FINANCIAL TRUTH:

        gross          = SUM(exit leg gross)         never recomputed
        exit fill      = quantity-weighted VWAP      display/attribution only
        decision exit  = decision VWAP, or None when ANY leg lacks one —
                         an average of only the favorable subset is not an
                         average
        fees           = header entry fee ONCE (the ENTRY leg's fee is the
                         same charge's second durable view, not a second
                         charge) + each exit leg's fee, mapped by basis
        carry          = each exit leg's holding cost, mapped by kind
        returns        = on COMMITTED MARGIN, stated as such; R on the
                         header's initial risk. R IS NOT PERCENT.

    Stamped SETTLEMENT_OUTCOME_VERSION — outcome_v1 rows keep their own
    provenance and are never relabelled.
    """
    from datetime import datetime

    from lib.fee_authority import REGULATORY_PER_SHARE
    from lib.holding_cost_authority import KIND_BORROW, KIND_FUNDING
    from lib.paper_settlement import LEG_ENTRY

    exit_legs = [l for l in legs if l.kind != LEG_ENTRY]
    if not exit_legs:
        raise ValueError(
            f"position {header.position_id} has no exit legs — there is no "
            f"realized outcome to build")

    closed_qty = sum(float(l.filled_qty or 0.0) for l in exit_legs)
    if closed_qty <= 0:
        raise ValueError(f"position {header.position_id} closed no quantity")

    # Financial truth: the sum of what each leg settled.
    gross = sum(float(l.gross_pnl_usd or 0.0) for l in exit_legs)

    # Display truth: what the exits averaged, weighted by size.
    exit_vwap = (sum(float(l.fill_price) * float(l.filled_qty)
                     for l in exit_legs) / closed_qty)
    if all(l.decision_price is not None for l in exit_legs):
        decision_exit = (sum(float(l.decision_price) * float(l.filled_qty)
                             for l in exit_legs) / closed_qty)
    else:
        decision_exit = None                       # missing is missing

    # Explicit charges, by category. The entry fee is counted ONCE, from
    # the header; the ENTRY leg carries the same dollars as audit, and
    # summing both would charge one fee twice.
    commission = 0.0
    regulatory = 0.0
    if header.entry_fee_usd:
        if header.entry_fee_basis == REGULATORY_PER_SHARE:
            regulatory += float(header.entry_fee_usd)
        else:
            commission += float(header.entry_fee_usd)
    funding = 0.0
    borrow = 0.0
    for l in exit_legs:
        fee = float(l.explicit_fee_usd or 0.0)
        if l.fee_basis == REGULATORY_PER_SHARE:
            regulatory += fee
        else:
            commission += fee
        hc = float(l.holding_cost_usd or 0.0)
        if l.holding_cost_type == KIND_BORROW:
            borrow += hc
        elif l.holding_cost_type == KIND_FUNDING or hc:
            funding += hc

    spread = sum(float(l.spread_attribution_usd or 0.0) for l in legs)
    slippage = sum(float(l.slippage_attribution_usd or 0.0) for l in legs)
    impact = sum(float(l.impact_attribution_usd or 0.0) for l in legs)

    final_leg = exit_legs[-1]
    closed_at = final_leg.created_at
    hold_minutes = None
    try:
        t0 = datetime.fromisoformat(str(header.opened_at))
        t1 = datetime.fromisoformat(str(closed_at))
        hold_minutes = (t1 - t0).total_seconds() / 60.0
    except (TypeError, ValueError):
        pass

    o = RealizedOutcome(
        thesis_id=thesis_id, signal_id=header.signal_id,
        position_id=header.position_id,
        source=VIRTUAL_CEX_AGENT, venue_type="CEX",
        venue=header.venue, product=header.product,
        instrument_id=header.instrument_id, symbol=header.symbol,
        side=header.position_side,
        quantity=float(header.original_quantity),
        quantity_unit=header.quantity_unit,
        multiplier=float(header.multiplier),
        decision_entry_price=header.decision_entry_price,
        actual_entry_fill=float(header.actual_entry_fill),
        decision_exit_price=decision_exit,
        actual_exit_fill=exit_vwap,
        gross_pnl_usd=gross,
        spread_attribution_usd=spread,
        slippage_attribution_usd=slippage,
        price_impact_attribution_usd=impact,
        commission_usd=commission, regulatory_fees_usd=regulatory,
        funding_usd=funding, borrow_cost_usd=borrow,
        initial_risk_usd=header.initial_risk_usd,
        exit_reason=final_leg.exit_reason,
        opened_at=header.opened_at, closed_at=closed_at,
        hold_minutes=hold_minutes,
        strategy=strategy, timeframe=timeframe,
        engine_epoch=header.engine_epoch,
        outcome_version=SETTLEMENT_OUTCOME_VERSION,
        execution_model=header.execution_model,
        cost_model_version=header.cost_model,
    )
    finalize(o)

    # ROI on COMMITTED MARGIN — the paper book's documented contract, and
    # the basis is stated so nobody later guesses NOTIONAL.
    margin = float(header.committed_margin_usd or 0.0)
    if margin > 0:
        o.gross_return_pct = o.gross_pnl_usd / margin * 100.0
        o.net_return_pct = o.net_pnl_usd / margin * 100.0
        o.return_pct_basis = "MARGIN"
    return o
