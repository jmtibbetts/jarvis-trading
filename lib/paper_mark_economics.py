"""What a paper position is worth at a mark — in ITS OWN units.

THE DEFECT THIS EXISTS TO REMOVE. `jobs/paper_trading` computed

    pl_dollar = (current_price - entry) * qty * side

For a PBTC position that is 26 CONTRACTS at a 0.01 multiplier — 0.26 BTC of
exposure — so a $100 adverse move is $26, not $2,600. The arithmetic was
wrong by exactly 100x, and it was NOT display-only: that number is what the
catastrophic backstop, the dynamic risk distance and the profit lock read,
and it is what the LLM position manager was shown. An exit can be executed
perfectly and still be the wrong exit, because the DECISION was made from
quantity arithmetic that mistook contracts for coins.

ONE AUTHORITY, THREE ROUTES. The basis comes from the frozen B1 settlement
header — entry fill, quantity unit, multiplier, side — and never from
`resolve(symbol)`, which answers 1.0 COIN for BTC/USD and would reintroduce
the same error one layer down.

    CANONICAL   contract-native arithmetic from the ledger
    LEGACY      the EXISTING legacy arithmetic, untouched — the 667
                historical positions are not retro-fitted with contract
                semantics they never traded under
    HYBRID      no economics at all. A management decision from a
                half-known basis is the thing to avoid, not to approximate.

AND THE MARK ITSELF HAS A SOURCE. For canonical economics the price comes
from the frozen product's own book; if that book is stale, halted, desynced
or missing, this ABSTAINS. It does not fall back to spot, to MarketAsset,
to yfinance, or to the stored `current_price` — an account decision made
from a substituted product is the same class of error as a substituted
fill.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass

logger = logging.getLogger(__name__)

CANONICAL = "CANONICAL"
LEGACY = "LEGACY"
HYBRID = "HYBRID"


@dataclass(frozen=True)
class MarkBasis:
    """Everything needed to price one position at a mark, and which economy
    it belongs to."""
    route: str
    position_side: str | None          # long | short
    entry_fill: float
    qty: float
    multiplier: float | None
    quantity_unit: str | None
    margin: float
    leverage: float
    symbol: str | None = None
    product: str | None = None
    instrument_id: str | None = None

    @property
    def usable(self) -> bool:
        return (self.route in (CANONICAL, LEGACY)
                and self.position_side in ("long", "short")
                and self.multiplier is not None
                and math.isfinite(self.multiplier) and self.multiplier > 0
                and self.qty > 0 and self.entry_fill > 0)


@dataclass(frozen=True)
class CanonicalMark:
    """A read-only price for a canonical position, from its own product."""
    ok: bool
    bid: float | None = None
    ask: float | None = None
    mid: float | None = None
    instrument_id: str | None = None
    product: str | None = None
    reason: str | None = None
    detail: str | None = None


def basis_for_position(position_id: str) -> MarkBasis | None:
    """Read the unit basis from the LEDGER, one short session.

    Canonical positions take entry fill / unit / multiplier from the frozen
    header — the same facts settlement uses — so management and settlement
    can never disagree about what one unit is.
    """
    from lib import exit_dispatch as ED
    from app.database import (PaperPosition, PaperPositionSettlement, get_db)

    with get_db() as db:
        pos = db.query(PaperPosition).filter(
            PaperPosition.id == position_id).first()
        if pos is None:
            return None
        header = None
        if ED._canonical_ledger_available():
            header = db.query(PaperPositionSettlement).filter(
                PaperPositionSettlement.position_id == position_id).first()
        route = ED.classify_position(pos, header)
        qty = abs(float(pos.qty or 0))
        margin = float(pos.margin_used or 0.0)
        leverage = max(1.0, float(pos.leverage or 1.0))
        side = pos.side if pos.side in ("long", "short") else None
        symbol = pos.symbol
        entry = float(pos.entry_price or 0.0)
        if route == CANONICAL and header is not None:
            basis = MarkBasis(
                route=CANONICAL, position_side=header.position_side,
                entry_fill=float(header.actual_entry_fill), qty=qty,
                multiplier=float(header.multiplier),
                quantity_unit=header.quantity_unit, margin=margin,
                leverage=leverage, symbol=header.symbol,
                product=header.product,
                instrument_id=header.instrument_id)
        elif route == LEGACY:
            # LEGACY KEEPS LEGACY ARITHMETIC. Its qty has always meant
            # "units at the entry price"; a multiplier of 1.0 is what that
            # economy actually traded under, and changing it now would
            # rewrite the history of 667 positions.
            basis = MarkBasis(
                route=LEGACY, position_side=side, entry_fill=entry, qty=qty,
                multiplier=1.0, quantity_unit=None, margin=margin,
                leverage=leverage, symbol=symbol)
        else:
            basis = MarkBasis(
                route=HYBRID, position_side=side, entry_fill=entry, qty=qty,
                multiplier=None, quantity_unit=None, margin=margin,
                leverage=leverage, symbol=symbol)
        db.expunge_all()
    return basis


def gross_at_mark(basis: MarkBasis, mark: float) -> float | None:
    """Unrealized gross P&L at `mark`, or None when the basis is unusable.

    LEVERAGE DOES NOT APPEAR. It decides how much CAPITAL is committed, not
    how much a price move is worth — multiplying by it here would square the
    exposure, which is a defect this book has already paid for once.
    """
    if not basis.usable:
        return None
    try:
        px = float(mark)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(px) or px <= 0:
        return None
    sign = 1.0 if basis.position_side == "long" else -1.0
    return (px - basis.entry_fill) * basis.qty * basis.multiplier * sign


def notional_at(basis: MarkBasis, price: float) -> float | None:
    """Exposure in dollars — qty x price x multiplier, never qty x price."""
    if not basis.usable:
        return None
    return basis.qty * float(price) * basis.multiplier


def price_distance_for_usd(basis: MarkBasis, usd: float) -> float | None:
    """How far the PRICE must move to be worth `usd` on this position.

    $350 across 26 contracts at 0.01 is $1,346.15 of price, not $13.46. A
    stop placed at the second distance is a different trade from the one
    risk authorized.
    """
    if not basis.usable:
        return None
    denom = basis.qty * basis.multiplier
    if denom <= 0:
        return None
    return float(usd) / denom


def return_pct_on_margin(basis: MarkBasis, gross: float | None
                         ) -> float | None:
    """Account return, stated on the denominator it actually uses. Distinct
    from the underlying's percentage move — see `underlying_move_pct`."""
    if gross is None or not basis.margin:
        return None
    return gross / basis.margin * 100.0


def underlying_move_pct(basis: MarkBasis, mark: float) -> float | None:
    """The PRICE move, signed by side. This is what the existing tier policy
    thresholds describe, and it is NOT a return on capital — the two are
    kept apart so no prompt or log can blur them."""
    if not basis.usable:
        return None
    sign = 1.0 if basis.position_side == "long" else -1.0
    return ((float(mark) - basis.entry_fill) / basis.entry_fill) * 100.0 * sign


def canonical_market_mark(position_id: str) -> CanonicalMark:
    """A READ-ONLY price for a canonical position, from ITS OWN product.

    No order, no fee, no settlement, and no DB transaction held across the
    provider read. When the exact book cannot be trusted this abstains:
    substituting another market's price to make an account decision is the
    same error as substituting a fill.
    """
    from lib import execution_policy as POL
    from lib.canonical_exit import PERSISTED_CANONICAL_ENTRY, read_exit_snapshot
    from lib.routing_identity import RESOLVED, RoutingIdentity

    snap = read_exit_snapshot(position_id)
    if isinstance(snap, dict):
        return CanonicalMark(ok=False, reason=snap.get("error"),
                             detail=snap.get("detail"))

    identity = RoutingIdentity(
        symbol=snap.symbol, asset_class=snap.asset_class,
        product=snap.product, venue=snap.venue,
        instrument_id=snap.instrument_id, identity_status=RESOLVED,
        product_identity_source=PERSISTED_CANONICAL_ENTRY,
        provenance={"source": PERSISTED_CANONICAL_ENTRY,
                    "position_id": position_id})
    ready = POL.execution_readiness(snap.symbol, snap.asset_class,
                                    routing_identity=identity)
    if not ready.ok:
        return CanonicalMark(ok=False, reason=ready.reason,
                             detail=ready.detail,
                             instrument_id=snap.instrument_id,
                             product=snap.product)
    if (ready.snapshot.instrument_id
            and ready.snapshot.instrument_id != snap.instrument_id):
        return CanonicalMark(ok=False, reason="EXECUTION_INSTRUMENT_MISMATCH",
                             detail=(f"book confirmed "
                                     f"{ready.snapshot.instrument_id!r}, not "
                                     f"the frozen {snap.instrument_id!r}"),
                             instrument_id=snap.instrument_id,
                             product=snap.product)
    bid = float(ready.snapshot.bid)
    ask = float(ready.snapshot.ask)
    return CanonicalMark(ok=True, bid=bid, ask=ask, mid=(bid + ask) / 2.0,
                         instrument_id=snap.instrument_id,
                         product=snap.product)
