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
