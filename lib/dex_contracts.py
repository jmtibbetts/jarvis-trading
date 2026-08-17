"""ONE canonical wire contract for the virtual DEX book.

The route serializing closed swaps read `t.pnl_pct`, `t.total_fees_usd` and
`t.exit_reason`. `DexTrade` has none of them — it stores `net_pnl_pct`,
`total_costs_usd` and `reason` — so `/onchain/dex/trades` raised
`AttributeError: 'DexTrade' object has no attribute 'pnl_pct'` for every
real closed row. It had never fired only because the book had no closed
swaps yet; the first one would have taken the endpoint down.

Three names drifted because three layers each invented their own. This
module is the one place they are defined, so the ORM, the route, the
frontend type and the component cannot disagree again.

## Costs are not "fees"

The old wire name `total_fees_usd` was wrong on the merits, not just
inconsistent. On a DEX the money leaves through three different doors:

    pool_fees      the venue's cut, a percentage the pool charges
    network_fees   the chain's cut, paid in SOL, indifferent to your size
    impact         YOUR size against the pool's depth — nobody charges it,
                   it is the price moving away from you as you trade

Impact is not a fee. Calling the total "fees" invites the reader to think a
cheaper venue would fix it, when the real remedy for impact is a smaller
size or a deeper pool. `total_costs_usd` is the sum; the three components
stay separately addressable because they call for different responses.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

# Bumped when a field is renamed or removed — additive changes do not need
# it. The frontend checks this against what it was built for, so a stale
# bundle says so instead of rendering blanks.
DEX_TRADE_CONTRACT_VERSION = 2


@dataclass(frozen=True)
class ClosedDexTrade:
    """One closed swap, as the wire and the frontend both see it."""

    id: str
    mint: str | None
    symbol: str | None
    dex: str | None
    position_id: str | None

    qty_tokens: float | None
    notional_usd: float | None
    entry_price_usd: float | None
    exit_price_usd: float | None

    # Gross is the price move. Net is what the book actually kept. On a
    # thin pool the gap between them can exceed the move itself.
    gross_pnl_usd: float | None
    net_pnl_usd: float | None
    net_pnl_pct: float | None

    # The three doors money leaves through, never collapsed into one.
    pool_fees_usd: float | None
    network_fees_usd: float | None
    total_costs_usd: float | None

    # Impact is reported as a percentage per side, because it is a property
    # of size against depth rather than a charge with a dollar rate.
    entry_impact_pct: float | None
    exit_impact_pct: float | None

    reason: str | None
    opened_at: str | None
    closed_at: str | None
    hold_minutes: float | None

    @property
    def impact_cost_usd(self) -> float | None:
        """What impact cost, derived: total less the two explicit charges.

        Kept as a derived property rather than a stored column so it can
        never disagree with the components it is computed from.
        """
        if self.total_costs_usd is None:
            return None
        explicit = (self.pool_fees_usd or 0.0) + (self.network_fees_usd or 0.0)
        return round(self.total_costs_usd - explicit, 6)

    def as_dict(self) -> dict:
        return {**asdict(self), "impact_cost_usd": self.impact_cost_usd}

    @classmethod
    def from_row(cls, t) -> "ClosedDexTrade":
        """Build from the ORM row. THE one place column names are read.

        Attribute access is direct and unguarded on purpose: a renamed
        column should fail loudly here, in one obvious place, rather than
        be papered over with getattr defaults that would let the contract
        drift again silently.
        """
        return cls(
            id=t.id, mint=t.mint, symbol=t.symbol, dex=t.dex,
            position_id=t.position_id,
            qty_tokens=t.qty_tokens, notional_usd=t.notional_usd,
            entry_price_usd=t.entry_price_usd, exit_price_usd=t.exit_price_usd,
            gross_pnl_usd=t.gross_pnl_usd, net_pnl_usd=t.net_pnl_usd,
            net_pnl_pct=t.net_pnl_pct,
            pool_fees_usd=t.pool_fees_usd,
            network_fees_usd=t.network_fees_usd,
            total_costs_usd=t.total_costs_usd,
            entry_impact_pct=t.entry_impact_pct,
            exit_impact_pct=t.exit_impact_pct,
            reason=t.reason,
            opened_at=t.opened_at, closed_at=t.closed_at,
            hold_minutes=t.hold_minutes,
        )


def closed_trades_payload(rows) -> dict:
    """The full `/onchain/dex/trades` body, from ORM rows."""
    trades = [ClosedDexTrade.from_row(t).as_dict() for t in rows]
    return {
        "trades": trades,
        "count": len(trades),
        "contract_version": DEX_TRADE_CONTRACT_VERSION,
        "note": (
            "total_costs_usd = pool_fees_usd + network_fees_usd + impact. "
            "Impact is not a fee — it is your size against the pool's depth, "
            "and the remedy for it is a smaller size or a deeper pool rather "
            "than a cheaper venue."
        ),
    }
