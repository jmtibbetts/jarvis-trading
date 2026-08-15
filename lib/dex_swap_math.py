"""What a DEX swap actually costs — constant-product AMM execution.

A CEX fill and a DEX swap are different events and modelling one with the
other produces fiction. On a central exchange you take an order book at a
quoted price and pay a percentage commission. On an AMM there is no book:
the pool IS the counterparty, the price moves as YOUR trade consumes it,
and the move is a deterministic function of trade size against pool depth.

    CEX                          DEX (AMM)
    ─────────────────────────    ──────────────────────────────────
    order book depth             pool reserves
    slippage vs the book         price impact you cause yourself
    % commission                 pool fee + priority fee + base fee
    leverage, shorts             spot only; no borrowing at the pool
    symbol/USD                   token/token, routed
    rejected order costs 0       a failed transaction still costs fees

The impact math is exact for a constant-product pool:

    A_eff  = A x (1 - fee)
    out    = A_eff x Y / (X + A_eff)
    impact = A_eff / (X + A_eff)

`impact` is what you lose to your own size — the difference between the
quoted spot price and the average price you actually get. It is not a
random draw and it is not a fudge factor; a $1,000 order into a $50,000
pool moves that pool roughly 4% whether anyone likes it or not.

KNOWN MODELLING LIMIT, stated because it changes the numbers: GeckoTerminal
reports `reserve_in_usd` as TOTAL pool liquidity, so each side is taken as
half. That is right for a balanced constant-product pool (Raydium AMM v4,
most Pump.fun pools) and WRONG for concentrated liquidity (Meteora DLMM,
Orca Whirlpool), where depth near spot can be far deeper or far thinner
than half the nominal reserve. For concentrated pools this is an estimate
whose direction is unknown, and `depth_model` says so on every quote.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Pool fee by DEX, in basis points. Defaults chosen from published fee
# tiers; a pool that reports its own fee should override.
POOL_FEE_BPS = {
    "raydium": 25,        # 0.25% classic AMM v4
    "orca": 30,           # 0.30% standard; Whirlpools vary by tier
    "meteora": 25,        # DLMM varies per bin step
    "pumpswap": 25,
    "pump.fun": 100,      # 1% on the bonding curve
    "lifinity": 20,
    "phoenix": 0,         # order book, not an AMM
}
DEFAULT_FEE_BPS = 30

# Solana base fee is 5,000 lamports per signature. Priority fee is what
# actually varies and is what gets a swap landed in a contested block.
LAMPORTS_PER_SOL = 1_000_000_000
BASE_FEE_LAMPORTS = 5_000
DEFAULT_PRIORITY_LAMPORTS = 100_000        # ~0.0001 SOL, a modest tip

# Above this the quote is flagged: you are the market, not a participant.
IMPACT_WARN_PCT = 1.0
IMPACT_SEVERE_PCT = 5.0


def _norm(s: str) -> str:
    """One normalisation, applied to BOTH sides of the comparison.

    This stripped dots from the table's names but not from the incoming
    key, so "pump.fun" never matched its own entry and quietly took the
    30bps default instead of its real 100bps — a 4x fee understatement on
    the venue most likely to be traded here.
    """
    return str(s or "").lower().replace(" ", "").replace("_", "").replace(".", "")


def pool_fee_bps(dex: str | None) -> int:
    if not dex:
        return DEFAULT_FEE_BPS
    key = _norm(dex)
    # Longest name first: "pumpswap" must not be shadowed by a shorter
    # entry that happens to be a substring of it.
    for name in sorted(POOL_FEE_BPS, key=len, reverse=True):
        if _norm(name) in key:
            return POOL_FEE_BPS[name]
    return DEFAULT_FEE_BPS


def quote_swap(amount_usd: float, reserve_usd: float, *,
               dex: str | None = None, fee_bps: int | None = None,
               priority_lamports: int = DEFAULT_PRIORITY_LAMPORTS,
               sol_price_usd: float = 0.0,
               concentrated: bool = False) -> dict:
    """Price one swap of `amount_usd` against a pool holding `reserve_usd`.

    Returns every component separately. A single "cost" number hides which
    part of a bad fill was the fee and which was your own size, and those
    have completely different remedies — one is the pool's price, the other
    is a reason to trade smaller.
    """
    amount_usd = float(amount_usd or 0)
    reserve_usd = float(reserve_usd or 0)
    if amount_usd <= 0:
        return {"ok": False, "reason": "amount must be positive"}
    if reserve_usd <= 0:
        return {"ok": False, "reason": "no pool liquidity to price against"}

    bps = fee_bps if fee_bps is not None else pool_fee_bps(dex)
    fee_rate = bps / 10_000.0

    # Half the reported total sits on the side being paid into.
    x_reserve = reserve_usd / 2.0

    a_eff = amount_usd * (1.0 - fee_rate)
    impact = a_eff / (x_reserve + a_eff)          # fraction, 0..1
    out_usd = a_eff * (1.0 - impact)

    pool_fee_usd = amount_usd * fee_rate
    impact_usd = a_eff - out_usd

    lamports = BASE_FEE_LAMPORTS + max(0, int(priority_lamports))
    network_fee_usd = (lamports / LAMPORTS_PER_SOL) * float(sol_price_usd or 0)

    total_cost_usd = pool_fee_usd + impact_usd + network_fee_usd
    received_usd = out_usd - network_fee_usd

    impact_pct = impact * 100.0
    severity = ("severe" if impact_pct >= IMPACT_SEVERE_PCT
                else "high" if impact_pct >= IMPACT_WARN_PCT else "normal")

    return {
        "ok": True,
        "amount_usd": round(amount_usd, 6),
        "reserve_usd": round(reserve_usd, 2),
        # What you get, and what each part of the difference was for.
        "received_usd": round(received_usd, 6),
        "pool_fee_usd": round(pool_fee_usd, 6),
        "price_impact_usd": round(impact_usd, 6),
        "network_fee_usd": round(network_fee_usd, 6),
        "total_cost_usd": round(total_cost_usd, 6),
        "total_cost_pct": round(100.0 * total_cost_usd / amount_usd, 4),
        "price_impact_pct": round(impact_pct, 4),
        "fee_bps": bps,
        "priority_lamports": lamports,
        "impact_severity": severity,
        # Provenance for the estimate, carried with it.
        "depth_model": ("concentrated liquidity — half-of-total-reserve is a "
                        "ROUGH proxy and the error direction is unknown"
                        if concentrated else
                        "constant product, half of total reserve per side"),
        "concentrated": bool(concentrated),
    }


def round_trip_cost(amount_usd: float, reserve_usd: float, **kw) -> dict:
    """Buy then sell the same size — what the position must overcome.

    The number that decides whether a setup is worth taking at all. On a
    thin pool the round trip alone can exceed the move being traded, which
    is the difference between a bad entry and a trade that could never have
    won.
    """
    buy = quote_swap(amount_usd, reserve_usd, **kw)
    if not buy.get("ok"):
        return buy
    # The sell prices against a pool that YOUR buy just moved.
    sell_reserve = reserve_usd + buy["price_impact_usd"]
    sell = quote_swap(buy["received_usd"], sell_reserve, **kw)
    if not sell.get("ok"):
        return sell

    out = sell["received_usd"]
    cost = amount_usd - out
    return {
        "ok": True,
        "amount_usd": round(amount_usd, 6),
        "returned_usd": round(out, 6),
        "round_trip_cost_usd": round(cost, 6),
        "round_trip_cost_pct": round(100.0 * cost / amount_usd, 4),
        "breakeven_move_pct": round(100.0 * cost / max(out, 1e-9), 4),
        "buy": buy,
        "sell": sell,
    }


def max_size_for_impact(reserve_usd: float, max_impact_pct: float,
                        dex: str | None = None,
                        fee_bps: int | None = None) -> float:
    """Largest trade whose own impact stays within `max_impact_pct`.

    Solved rather than searched. From impact = A_eff/(X + A_eff):

        A_eff = X x i / (1 - i)          then undo the fee

    This is the position-sizing primitive for DEX: on-chain, size is
    limited by pool depth long before it is limited by account equity.
    """
    reserve_usd = float(reserve_usd or 0)
    i = float(max_impact_pct or 0) / 100.0
    if reserve_usd <= 0 or i <= 0 or i >= 1:
        return 0.0
    bps = fee_bps if fee_bps is not None else pool_fee_bps(dex)
    x_reserve = reserve_usd / 2.0
    a_eff = x_reserve * i / (1.0 - i)
    return round(a_eff / (1.0 - bps / 10_000.0), 6)
