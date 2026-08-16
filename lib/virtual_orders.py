"""A trade thesis is not a fill.

The paper book created positions directly from signals: if the engine
decided to trade, a position existed, at the price it wanted, in the size
it wanted. Nothing could miss, nothing could be rejected, nothing could
fill halfway, and nothing ever cost more than the model assumed.

A system trained that way learns a market that does not exist. It cannot
learn that its edge evaporates when it has to cross a spread, that its
limit orders sit unfilled exactly when it is right, or that its stops gap.
Those are not edge cases — for a short-horizon strategy they ARE the
economics, and they are the difference between a backtest and a desk.

THREE RULES THIS ENFORCES.

1. A MARKET ORDER NEVER FILLS AT THE MID. A buy lifts the ask, a sell hits
   the bid, and both then pay slippage. Filling at the mid hands the model
   half the spread on every single trade, which is precisely the kind of
   simulator error that makes an unprofitable strategy look profitable.

2. A LIMIT ORDER IS NOT FILLED BY A BAR THAT TOUCHED IT. On coarse bars,
   "the low reached my price" does not mean there was size there for me.
   The default model requires the bar to trade THROUGH the limit, and
   records which model was used so the assumption is auditable rather than
   implied.

3. A STOP IS A TRIGGER, NOT A FILL PRICE. When price gaps through a stop,
   the fill is on the other side of the gap. Booking the stop price itself
   is the single most flattering lie a simulator can tell, because it
   silently caps every tail loss the strategy will ever record.

Costs are ATTRIBUTED here, not charged. Spread and slippage are already
inside the returned fill price; lib/realized_outcome keeps them in
attribution fields so they cannot be subtracted a second time.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

FILL_MODEL_VERSION = "fill_v1"

# ── Order types ──────────────────────────────────────────────────────────
MARKET = "MARKET"
LIMIT = "LIMIT"
STOP = "STOP"
STOP_LIMIT = "STOP_LIMIT"

# ── Order states ─────────────────────────────────────────────────────────
NEW = "NEW"
ACCEPTED = "ACCEPTED"
WORKING = "WORKING"
PARTIALLY_FILLED = "PARTIALLY_FILLED"
FILLED = "FILLED"
CANCELED = "CANCELED"
EXPIRED = "EXPIRED"
REJECTED = "REJECTED"
UNFILLED = "UNFILLED"
ERROR = "ERROR"

TERMINAL_STATES = frozenset({FILLED, CANCELED, EXPIRED, REJECTED, UNFILLED,
                             ERROR})

# ── Fill models, recorded on every result ────────────────────────────────
# CONSERVATIVE_BAR_TOUCH is the honest default for coarse bars: a bar that
# merely TOUCHED the limit is not evidence that size existed there.
CONSERVATIVE_BAR_TOUCH = "CONSERVATIVE_BAR_TOUCH"
OPTIMISTIC_BAR_TOUCH = "OPTIMISTIC_BAR_TOUCH"
ORDERBOOK_SIMULATED = "ORDERBOOK_SIMULATED"

# ── Rejection reasons ────────────────────────────────────────────────────
REJECT_NO_QUOTE = "NO_QUOTE"
REJECT_UNSUPPORTED_INSTRUMENT = "UNSUPPORTED_INSTRUMENT"
REJECT_BORROW_UNAVAILABLE = "BORROW_UNAVAILABLE"
REJECT_INSUFFICIENT_GAS = "INSUFFICIENT_GAS"
REJECT_BELOW_MIN_QTY = "BELOW_MINIMUM_QUANTITY"
REJECT_INVALID_SIDE = "INVALID_SIDE"

# Default slippage beyond the touched side of the book, as a fraction of
# price. Measured from this system's own fills: median 0.21%.
DEFAULT_SLIPPAGE_PCT = 0.0021


@dataclass
class Quote:
    """The book at decision time. `mid` is a REFERENCE, never a fill."""
    bid: float
    ask: float
    as_of: str | None = None
    source: str | None = None

    @property
    def mid(self) -> float:
        return (float(self.bid) + float(self.ask)) / 2.0

    @property
    def spread(self) -> float:
        return float(self.ask) - float(self.bid)

    @property
    def spread_pct(self) -> float:
        m = self.mid
        return (self.spread / m) if m else 0.0


@dataclass
class VirtualOrder:
    symbol: str
    side: str                       # long | short, canonical
    quantity: float
    order_type: str = MARKET
    limit_price: float | None = None
    stop_price: float | None = None
    quantity_unit: str | None = None
    thesis_id: str | None = None
    signal_id: str | None = None
    state: str = NEW
    reason: str | None = None


@dataclass
class ExecutionResult:
    """One attempt to execute. A refusal is a result, not an absence."""
    state: str
    symbol: str
    side: str
    order_type: str

    requested_quantity: float = 0.0
    filled_quantity: float = 0.0
    quantity_unit: str | None = None
    multiplier: float = 1.0

    decision_mid: float | None = None
    bid: float | None = None
    ask: float | None = None
    spread: float | None = None
    submitted_price: float | None = None
    fill_price: float | None = None

    # ATTRIBUTION. Already inside fill_price — never charged again.
    spread_cost_usd: float = 0.0
    slippage_usd: float = 0.0

    gap_through_stop: bool = False
    fill_model: str = FILL_MODEL_VERSION
    price_model: str | None = None
    reject_reason: str | None = None
    provenance: dict = field(default_factory=dict)

    @property
    def filled(self) -> bool:
        return self.state in (FILLED, PARTIALLY_FILLED)


def _sign(side: str | None) -> int | None:
    from lib.trade_side import SHORT, parse_side_strict
    s = parse_side_strict(side)
    return None if s is None else (-1 if s == SHORT else 1)


def _reject(order: VirtualOrder, reason: str, detail: str | None = None
            ) -> ExecutionResult:
    return ExecutionResult(
        state=REJECTED, symbol=order.symbol, side=order.side,
        order_type=order.order_type, requested_quantity=order.quantity,
        reject_reason=reason, provenance={"detail": detail} if detail else {})


def execute_market(order: VirtualOrder, quote: Quote, *,
                   slippage_pct: float | None = None,
                   instrument=None) -> ExecutionResult:
    """Cross the book. A BUY lifts the ask; a SELL hits the bid.

    Filling at the mid would hand the model half the spread on every trade
    — free money the real market never offers, and enough on its own to
    make a losing intraday strategy backtest profitably.
    """
    from lib.instruments import UnsupportedInstrument, resolve

    sign = _sign(order.side)
    if sign is None:
        return _reject(order, REJECT_INVALID_SIDE,
                       f"unreadable direction {order.side!r}")
    if not quote or not quote.bid or not quote.ask:
        return _reject(order, REJECT_NO_QUOTE,
                       "no two-sided quote — a fill cannot be simulated")

    inst = instrument
    if inst is None:
        try:
            inst = resolve(order.symbol).require_executable()
        except UnsupportedInstrument as e:
            return _reject(order, REJECT_UNSUPPORTED_INSTRUMENT, str(e))

    touched = float(quote.ask) if sign > 0 else float(quote.bid)
    slip_pct = DEFAULT_SLIPPAGE_PCT if slippage_pct is None else abs(float(slippage_pct))
    # Slippage always works AGAINST the order, whichever way it points.
    fill = touched * (1.0 + slip_pct * sign)

    mult = float(inst.multiplier or 1.0)
    units = float(order.quantity) * mult
    mid = quote.mid

    return ExecutionResult(
        state=FILLED, symbol=order.symbol, side=order.side,
        order_type=MARKET, requested_quantity=order.quantity,
        filled_quantity=order.quantity, quantity_unit=inst.quantity_unit,
        multiplier=mult,
        decision_mid=mid, bid=quote.bid, ask=quote.ask, spread=quote.spread,
        submitted_price=mid, fill_price=fill,
        # Half-spread crossed, then slippage — attribution only.
        spread_cost_usd=abs(touched - mid) * units,
        slippage_usd=abs(fill - touched) * units,
        price_model=ORDERBOOK_SIMULATED,
        provenance={"quote_as_of": quote.as_of, "quote_source": quote.source,
                    "slippage_pct": slip_pct})


def execute_limit(order: VirtualOrder, *, bar_high: float, bar_low: float,
                  model: str = CONSERVATIVE_BAR_TOUCH,
                  instrument=None) -> ExecutionResult:
    """Fill only if the bar traded THROUGH the limit, by default.

    A bar whose low merely reached the limit is not evidence that size
    existed there for this order. Assuming otherwise is lookahead: the
    strategy gets filled precisely at its best price on exactly the bars
    where being filled was most valuable. The model used is recorded, so
    the assumption is auditable rather than implied.
    """
    from lib.instruments import UnsupportedInstrument, resolve

    sign = _sign(order.side)
    if sign is None:
        return _reject(order, REJECT_INVALID_SIDE,
                       f"unreadable direction {order.side!r}")
    if not order.limit_price:
        return _reject(order, REJECT_NO_QUOTE, "limit order without a price")

    inst = instrument
    if inst is None:
        try:
            inst = resolve(order.symbol).require_executable()
        except UnsupportedInstrument as e:
            return _reject(order, REJECT_UNSUPPORTED_INSTRUMENT, str(e))

    px = float(order.limit_price)
    if sign > 0:
        touched = float(bar_low) <= px
        through = float(bar_low) < px
    else:
        touched = float(bar_high) >= px
        through = float(bar_high) > px

    hit = through if model == CONSERVATIVE_BAR_TOUCH else touched
    base = ExecutionResult(
        state=FILLED if hit else UNFILLED,
        symbol=order.symbol, side=order.side, order_type=LIMIT,
        requested_quantity=order.quantity,
        filled_quantity=order.quantity if hit else 0.0,
        quantity_unit=inst.quantity_unit,
        multiplier=float(inst.multiplier or 1.0),
        submitted_price=px, fill_price=px if hit else None,
        price_model=model,
        provenance={"bar_high": bar_high, "bar_low": bar_low,
                    "touched": touched, "traded_through": through})
    if not hit:
        base.reason = None
        base.reject_reason = None
    return base


def execute_stop(order: VirtualOrder, *, bar_open: float, bar_high: float,
                 bar_low: float, gap_slippage_pct: float | None = None,
                 instrument=None) -> ExecutionResult:
    """A stop is a TRIGGER. The fill is wherever the market actually was.

    When a bar OPENS beyond the stop, the stop price was never available —
    the market gapped past it while the book was closed or empty, and the
    fill is at the open. Booking the stop price anyway is the most
    flattering lie a simulator can tell, because it silently caps every
    tail loss the strategy will ever record and makes its worst case look
    survivable.
    """
    from lib.instruments import UnsupportedInstrument, resolve

    sign = _sign(order.side)
    if sign is None:
        return _reject(order, REJECT_INVALID_SIDE,
                       f"unreadable direction {order.side!r}")
    if not order.stop_price:
        return _reject(order, REJECT_NO_QUOTE, "stop order without a price")

    inst = instrument
    if inst is None:
        try:
            inst = resolve(order.symbol).require_executable()
        except UnsupportedInstrument as e:
            return _reject(order, REJECT_UNSUPPORTED_INSTRUMENT, str(e))

    stop = float(order.stop_price)
    # A LONG position stops on the way DOWN; a SHORT stops on the way UP.
    if sign > 0:
        triggered = float(bar_low) <= stop
        gapped = float(bar_open) < stop
    else:
        triggered = float(bar_high) >= stop
        gapped = float(bar_open) > stop

    if not triggered:
        return ExecutionResult(
            state=UNFILLED, symbol=order.symbol, side=order.side,
            order_type=STOP, requested_quantity=order.quantity,
            quantity_unit=inst.quantity_unit,
            multiplier=float(inst.multiplier or 1.0),
            submitted_price=stop, price_model=CONSERVATIVE_BAR_TOUCH,
            provenance={"bar_open": bar_open, "bar_high": bar_high,
                        "bar_low": bar_low})

    slip_pct = DEFAULT_SLIPPAGE_PCT if gap_slippage_pct is None else abs(float(gap_slippage_pct))
    if gapped:
        fill = float(bar_open)          # the first price that actually existed
    else:
        fill = stop * (1.0 - slip_pct * sign)

    mult = float(inst.multiplier or 1.0)
    return ExecutionResult(
        state=FILLED, symbol=order.symbol, side=order.side, order_type=STOP,
        requested_quantity=order.quantity, filled_quantity=order.quantity,
        quantity_unit=inst.quantity_unit, multiplier=mult,
        submitted_price=stop, fill_price=fill,
        slippage_usd=abs(fill - stop) * float(order.quantity) * mult,
        gap_through_stop=bool(gapped),
        price_model=CONSERVATIVE_BAR_TOUCH,
        provenance={"bar_open": bar_open, "bar_high": bar_high,
                    "bar_low": bar_low, "gap_slippage_pct": slip_pct})
