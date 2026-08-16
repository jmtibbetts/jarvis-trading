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


# ── Depth certainty drives SIZING, not just display ──────────────────────
# UNCERTAIN DEPTH MUST MAKE JARVIS LESS CERTAIN ABOUT EXECUTABLE LIQUIDITY.
#
# Leaving these as provenance labels would let the simulator size a
# concentrated pool it has only modelled exactly as aggressively as one
# whose reserves it actually read. The label would be honest and the
# behaviour would not.
#
# Coefficients are a starting point to be CALIBRATED against realized
# impact, not derived truths — which is why predicted impact is stored
# alongside the realized figure.
DEPTH_SIZE_FACTOR = {
    "VERIFIED": 1.00,                 # real reserves, full ceiling
    "ASSUMED_BALANCED_POOL": 0.60,    # half-of-total is an assumption
    "MODELLED_ESTIMATE": 0.30,        # local depth genuinely unknown
}
# What to multiply predicted impact by when deciding whether a size is
# acceptable. Uncertainty is asymmetric: being wrong about depth is far
# more expensive than trading smaller than necessary.
DEPTH_IMPACT_MULTIPLIER = {
    "VERIFIED": 1.0, "ASSUMED_BALANCED_POOL": 1.5, "MODELLED_ESTIMATE": 2.5,
}
DEFAULT_DEPTH_CONFIDENCE = "MODELLED_ESTIMATE"


def depth_adjusted_size(size_usd: float, depth_confidence: str | None) -> dict:
    """Shrink a proposed size by how well the depth is actually known.

    An UNKNOWN confidence falls to the most conservative factor, not the
    most permissive — a pool nobody classified is not a pool anybody
    measured.
    """
    conf = depth_confidence if depth_confidence in DEPTH_SIZE_FACTOR else DEFAULT_DEPTH_CONFIDENCE
    factor = DEPTH_SIZE_FACTOR[conf]
    return {
        "size_usd": float(size_usd or 0) * factor,
        "requested_usd": float(size_usd or 0),
        "depth_confidence": conf,
        "size_factor": factor,
        "impact_multiplier": DEPTH_IMPACT_MULTIPLIER[conf],
        "reason": (f"depth is {conf}; size scaled to {factor:.0%} and "
                   f"predicted impact weighted "
                   f"{DEPTH_IMPACT_MULTIPLIER[conf]:.1f}x for uncertainty"),
    }


def failed_transaction_cost(*, priority_lamports: int = 0,
                            sol_price_usd: float = 0.0,
                            reached_chain: bool = True) -> dict:
    """A transaction that reached the chain and failed STILL COSTS GAS.

    No asset exchange, a real fee. Modelling failure as free teaches the
    desk that bad route selection is costless, so it never learns to avoid
    routes that revert — and reverts cluster exactly where the opportunity
    looks best.
    """
    if not reached_chain:
        return {"network_fee_sol": 0.0, "network_fee_usd": 0.0,
                "tokens_out": 0.0, "reached_chain": False,
                "reason": "rejected before submission — nothing was spent"}
    lamports = BASE_FEE_LAMPORTS + max(0, int(priority_lamports))
    sol = lamports / LAMPORTS_PER_SOL
    return {
        "network_fee_sol": sol,
        "network_fee_usd": sol * float(sol_price_usd or 0),
        "tokens_out": 0.0,
        "reached_chain": True,
        "reason": ("transaction executed and failed — gas consumed, no swap"),
    }


def spendable_native(balance_sol: float, *, priority_lamports: int = 0,
                     reserve_multiple: float = 3.0) -> dict:
    """How much SOL can be swapped while leaving enough to transact.

    A wallet that spends its last lamport on a swap cannot pay for that
    swap. Allowing it teaches a transaction the chain would simply refuse,
    and the state it produces — tokens held, no gas — is precisely the one
    the simulator most needs to be able to reach honestly.
    """
    lamports = BASE_FEE_LAMPORTS + max(0, int(priority_lamports))
    reserve = (lamports / LAMPORTS_PER_SOL) * max(1.0, float(reserve_multiple))
    bal = float(balance_sol or 0)
    spendable = max(0.0, bal - reserve)
    return {
        "balance_sol": bal,
        "execution_reserve_sol": reserve,
        "max_spendable_sol": spendable,
        "can_transact": bal >= reserve,
        "reason": (None if spendable > 0 else
                   f"balance {bal:.9f} SOL is below the {reserve:.9f} SOL "
                   f"needed to execute a transaction at all"),
    }


def quote_swap_native(amount_in: float, reserve_in: float, reserve_out: float,
                      *, dex: str | None = None, fee_bps: int | None = None,
                      priority_lamports: int = DEFAULT_PRIORITY_LAMPORTS,
                      sol_price_usd: float = 0.0) -> dict:
    """x*y=k in NATIVE TOKEN UNITS, from the pool's actual reserves.

    Preferred over the USD form whenever real reserves are available. The
    USD path has to assume the pool is balanced — half the reported total
    on each side — and a pool that is not balanced prices very differently
    from one that is. With both reserves known, no assumption is needed:
    the curve is evaluated on the numbers the chain reports.

    Returns tokens out, so nothing downstream has to divide by a mid price
    to recover a quantity the AMM already determined exactly.
    """
    amount_in = float(amount_in or 0)
    reserve_in = float(reserve_in or 0)
    reserve_out = float(reserve_out or 0)
    if amount_in <= 0:
        return {"ok": False, "reason": "amount must be positive"}
    if reserve_in <= 0 or reserve_out <= 0:
        return {"ok": False, "reason": "pool reserves unavailable on one side"}

    bps = fee_bps if fee_bps is not None else pool_fee_bps(dex)
    fee_rate = bps / 10_000.0

    fee_in = amount_in * fee_rate
    a_eff = amount_in - fee_in

    # Exact constant product: out = (a * Y) / (X + a)
    tokens_out = (a_eff * reserve_out) / (reserve_in + a_eff)

    spot_price = reserve_out / reserve_in           # out per in, before impact
    ideal_out = a_eff * spot_price
    impact_tokens = ideal_out - tokens_out
    impact_pct = (impact_tokens / ideal_out * 100.0) if ideal_out else 0.0

    lamports = BASE_FEE_LAMPORTS + max(0, int(priority_lamports))
    network_fee_sol = lamports / LAMPORTS_PER_SOL

    return {
        "ok": True,
        "amount_in": amount_in,
        "tokens_out": tokens_out,
        "effective_price": (amount_in / tokens_out) if tokens_out else None,
        "spot_price": spot_price,
        "pool_fee_tokens_in": fee_in,
        "price_impact_tokens": impact_tokens,
        "price_impact_pct": round(impact_pct, 4),
        "reserve_in": reserve_in, "reserve_out": reserve_out,
        # Gas leaves the SOL balance; it does not shrink tokens_out.
        "network_fee_sol": round(network_fee_sol, 9),
        "network_fee_usd": round(network_fee_sol * float(sol_price_usd or 0), 6),
        "gas_paid_separately": True,
        "fee_bps": bps,
        "depth_model": "CONSTANT_PRODUCT_AMM",
        "depth_confidence": "VERIFIED",
        "provenance": "native reserves — no balanced-pool assumption",
    }


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
    # THE POOL OUTPUT IS NOT REDUCED BY GAS.
    #
    # This used to return `out_usd - network_fee_usd`, which models a chain
    # where the AMM hands you fewer tokens because the validator was paid.
    # That is not what happens: the pool gives you exactly what the curve
    # says, and the network fee is debited SEPARATELY from the wallet's SOL
    # balance.
    #
    # It matters beyond bookkeeping. Netting gas out of the output hides
    # the one failure this simulator should teach — a wallet with tokens
    # but no SOL cannot transact at all — and it silently scales the fee
    # with trade size when a Solana fee is flat.
    received_usd = out_usd
    # Charged against the gas balance, not the position.
    network_fee_sol = lamports / LAMPORTS_PER_SOL

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
        # Paid in SOL from the wallet, NEVER deducted from the pool output.
        # A wallet holding tokens but no SOL cannot transact, and that is a
        # real training signal rather than a rounding detail.
        "network_fee_sol": round(network_fee_sol, 9),
        "gas_paid_separately": True,
        "total_cost_usd": round(total_cost_usd, 6),
        "total_cost_pct": round(100.0 * total_cost_usd / amount_usd, 4),
        "price_impact_pct": round(impact_pct, 4),
        "fee_bps": bps,
        "priority_lamports": lamports,
        "impact_severity": severity,
        # Provenance for the estimate, carried with it.
        "depth_model": ("CONCENTRATED_LIQUIDITY" if concentrated
                        else "CONSTANT_PRODUCT_AMM"),
        # Half-of-total is an ASSUMPTION that the pool is balanced. Say so,
        # rather than presenting a modelled depth as a measured one — and
        # for a concentrated pool the local depth around the current tick
        # may be nothing like half the total, in either direction.
        "depth_confidence": ("MODELLED_ESTIMATE" if concentrated
                             else "ASSUMED_BALANCED_POOL"),
        "provenance": ("concentrated liquidity — half-of-total-reserve is a "
                       "ROUGH proxy and the error direction is unknown; "
                       "prefer quote_swap_native with real reserves"
                       if concentrated else
                       "constant product, ASSUMING half of total reserve per "
                       "side; prefer quote_swap_native when reserves are known"),
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
