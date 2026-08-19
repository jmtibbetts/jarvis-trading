"""Competing execution assumptions, run side by side on ONE observation.

WHY. The active simulator prices every fill as top-of-book plus a fixed
0.21% adverse move. That number is not invented and it is not circular — it
came from 50 real Alpaca broker fills recorded by `lib/execution_recorder`
(`record_intent` in jobs/execute_signals, `record_fill` in
jobs/manage_positions), which are external observations, not simulator
output. Provenance category B, not C.

The problem is what it is being applied TO. Those were EQUITY and crypto
SPOT fills at a retail broker. They are now the slippage assumption for
CONTRACT PERPETUALS on Bitnomial — a different venue, a different
instrument type, a different microstructure — and the assumption is a flat
percentage that does not move with order size, spread or visible liquidity.
An order for 5 contracts and an order for 5,000 get the same haircut.

So: measure it. Same thesis, same instrument, same quantity, same book
snapshot, several models. Compare EXECUTION FIDELITY, never simulated
profit — the model that invents the most money is the one to distrust.

WHAT THE BOOK ACTUALLY GIVES US, proven rather than assumed:

    quantity is in CONTRACTS
        Bitnomial's product spec states volume, open_interest and
        block_volume are all in contracts. Corroborated economically across
        three products with three different multipliers: PBTC 50 contracts
        x 0.01 BTC x $64,275 = $32k top-of-book, PETH 25 x 0.5 x $1,909 =
        $24k, PSOL 10 x 5 x $76.81 = $3.8k. All plausible. Read as
        underlying instead, PBTC's top level would be $3.2M, which is not.

    price is INTEGER TICKS
        price_usd = raw x price_increment. PBTC 5.0, PETH 0.2, PSOL 0.01.

    ONLY THE TOP 10 LEVELS EXIST
        Bitnomial publishes ten levels a side and — their words — "Level
        updates are NOT sent when a level goes out of scope". Everything
        past level ten is invisible, and the deepest visible levels can be
        stale. So depth is a FLOOR on liquidity, never a total, and a model
        that runs out of visible book must say so instead of inventing the
        rest.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

# Truthful names. The current model does not look at depth, so it does not
# get a name containing "ORDERBOOK".
TOP_OF_BOOK_FIXED_SLIPPAGE_V1 = "TOP_OF_BOOK_FIXED_SLIPPAGE_V1"
DEPTH_VWAP_V1 = "DEPTH_VWAP_V1"

FILLED = "FILLED"
PARTIALLY_FILLED = "PARTIALLY_FILLED"
INSUFFICIENT_VISIBLE_DEPTH = "INSUFFICIENT_VISIBLE_DEPTH"
NO_BOOK = "NO_BOOK"

BUY = "BUY"
SELL = "SELL"


@dataclass
class BookSnapshot:
    """One side-complete view of a book, in the venue's own units."""
    instrument_id: str
    price_increment: float
    contract_size: float
    bids: list = field(default_factory=list)   # [(raw_price, contracts)] desc
    asks: list = field(default_factory=list)   # [(raw_price, contracts)] asc
    observed_at: str | None = None
    state: str | None = None

    @property
    def usable(self) -> bool:
        return bool(self.bids and self.asks and self.price_increment > 0
                    and self.state in (None, "OK"))

    def usd(self, raw) -> float:
        return float(raw) * float(self.price_increment)

    def best_bid(self):
        return self.usd(self.bids[0][0]) if self.bids else None

    def best_ask(self):
        return self.usd(self.asks[0][0]) if self.asks else None


@dataclass
class FillResult:
    """What one model says would happen. Units are explicit everywhere."""
    model: str
    state: str
    requested_contracts: float
    filled_contracts: float = 0.0
    unfilled_contracts: float = 0.0
    fill_price: float | None = None        # USD per unit of underlying
    vwap: float | None = None
    reference_price: float | None = None   # the touched side, before costs
    effective_bps: float | None = None     # vs the touched side
    spread_cost_usd: float | None = None
    impact_usd: float | None = None
    levels_consumed: int = 0
    visible_contracts: float | None = None
    detail: str | None = None

    def as_row(self) -> dict:
        return {
            "model_name": self.model, "fill_state": self.state,
            "requested_contracts": self.requested_contracts,
            "filled_contracts": self.filled_contracts,
            "unfilled_contracts": self.unfilled_contracts,
            "fill_price": self.fill_price, "vwap": self.vwap,
            "reference_price": self.reference_price,
            "effective_bps": self.effective_bps,
            "spread_cost_usd": self.spread_cost_usd,
            "impact_usd": self.impact_usd,
            "levels_consumed": self.levels_consumed,
            "visible_contracts": self.visible_contracts,
            "detail": self.detail,
        }


def _bps(fill: float, reference: float, side: str) -> float | None:
    """Adverse cost in basis points, positive means worse than the touch."""
    if not reference:
        return None
    diff = (fill - reference) if side == BUY else (reference - fill)
    return round(diff / reference * 10_000.0, 4)


def top_of_book_fixed_slippage(book: BookSnapshot, side: str,
                               contracts: float, *,
                               slippage_pct: float = 0.0021) -> FillResult:
    """The CURRENT model, named for what it actually does.

    Touches one side, applies a flat adverse percentage, and fills the whole
    order at that price regardless of size. It never reads depth, so it can
    never report a partial fill — which is precisely the behaviour under
    test.
    """
    if not book.usable:
        return FillResult(model=TOP_OF_BOOK_FIXED_SLIPPAGE_V1, state=NO_BOOK,
                          requested_contracts=contracts,
                          unfilled_contracts=contracts,
                          detail=f"book state {book.state!r}")
    ref = book.best_ask() if side == BUY else book.best_bid()
    if ref is None or not math.isfinite(ref) or ref <= 0:
        return FillResult(model=TOP_OF_BOOK_FIXED_SLIPPAGE_V1, state=NO_BOOK,
                          requested_contracts=contracts,
                          unfilled_contracts=contracts,
                          detail="no touchable price")
    price = ref * (1 + slippage_pct) if side == BUY else ref * (1 - slippage_pct)
    underlying = contracts * book.contract_size
    return FillResult(
        model=TOP_OF_BOOK_FIXED_SLIPPAGE_V1, state=FILLED,
        requested_contracts=contracts, filled_contracts=contracts,
        unfilled_contracts=0.0, fill_price=price, vwap=price,
        reference_price=ref, effective_bps=_bps(price, ref, side),
        impact_usd=abs(price - ref) * underlying,
        levels_consumed=1,
        visible_contracts=_visible(book, side),
        detail=f"flat {slippage_pct * 100:.4f}% regardless of size")


def depth_vwap(book: BookSnapshot, side: str, contracts: float) -> FillResult:
    """Walk the visible book, best level first, in CONTRACTS.

    NEVER assumes liquidity past the last visible level. Bitnomial publishes
    ten levels a side and stops sending updates for levels that fall out of
    scope, so the visible total is a FLOOR on real liquidity — filling the
    remainder at the last price would be inventing the most expensive part
    of the order, in the flattering direction.
    """
    if not book.usable:
        return FillResult(model=DEPTH_VWAP_V1, state=NO_BOOK,
                          requested_contracts=contracts,
                          unfilled_contracts=contracts,
                          detail=f"book state {book.state!r}")
    levels = book.asks if side == BUY else book.bids
    ref = book.best_ask() if side == BUY else book.best_bid()
    if not levels or ref is None:
        return FillResult(model=DEPTH_VWAP_V1, state=NO_BOOK,
                          requested_contracts=contracts,
                          unfilled_contracts=contracts,
                          detail="no levels on the touched side")

    remaining = float(contracts)
    notional = 0.0          # sum(price_usd * contracts)
    taken = 0.0
    used = 0
    for raw_price, size in levels:
        if remaining <= 0:
            break
        avail = float(size or 0)
        if avail <= 0:
            continue
        take = min(remaining, avail)
        notional += book.usd(raw_price) * take
        remaining -= take
        taken += take
        used += 1

    visible = _visible(book, side)
    if taken <= 0:
        return FillResult(model=DEPTH_VWAP_V1,
                          state=INSUFFICIENT_VISIBLE_DEPTH,
                          requested_contracts=contracts,
                          unfilled_contracts=contracts,
                          reference_price=ref, visible_contracts=visible,
                          detail="no size on the touched side")

    vwap = notional / taken
    state = FILLED if remaining <= 1e-12 else PARTIALLY_FILLED
    underlying = taken * book.contract_size
    return FillResult(
        model=DEPTH_VWAP_V1, state=state,
        requested_contracts=contracts, filled_contracts=taken,
        unfilled_contracts=max(0.0, remaining), fill_price=vwap, vwap=vwap,
        reference_price=ref, effective_bps=_bps(vwap, ref, side),
        impact_usd=abs(vwap - ref) * underlying,
        levels_consumed=used, visible_contracts=visible,
        detail=(None if state == FILLED else
                f"visible book held {visible:g} contracts; "
                f"{remaining:g} left unfilled rather than invented"))


def _visible(book: BookSnapshot, side: str) -> float:
    levels = book.asks if side == BUY else book.bids
    return float(sum(float(q or 0) for _p, q in levels))


MODELS = {
    TOP_OF_BOOK_FIXED_SLIPPAGE_V1: top_of_book_fixed_slippage,
    DEPTH_VWAP_V1: depth_vwap,
}


def run_all(book: BookSnapshot, side: str, contracts: float) -> list[FillResult]:
    """ONE observation, every model. These are counterfactual siblings, not
    separate opportunities — the caller keeps them under one observation id
    so nothing downstream can count them as two chances to trade."""
    return [fn(book, side, contracts) for fn in MODELS.values()]
