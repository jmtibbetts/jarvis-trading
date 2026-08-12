"""What a trade costs before it can make anything.

A setup with a genuine edge can still be a losing trade once the spread,
fees, slippage and funding are paid. This module estimates those costs in
R (multiples of the risk taken), so they can be subtracted from expected
gross R and the decision made on NET expectancy.

Everything here is deterministic arithmetic over measured inputs. Where an
input is unknown the model uses an explicitly-labelled conservative default
rather than pretending the cost is zero — a missing measurement must never
make a trade look cheaper than it is.

Costs are expressed two ways:
  *_pct   fraction of notional (what the venue actually charges)
  *_r     that cost divided by the risk distance, so it is directly
          comparable with expected R

The R conversion is the point. A 0.10% round-trip fee is trivial on a trade
risking 5% and ruinous on a scalp risking 0.15%: same fee, 40x the impact on
expectancy. Costs must be compared against RISK, not against price.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# ── Venue defaults ───────────────────────────────────────────────────────
# Alpaca: US equities commission-free; crypto charges a taker fee.
EQUITY_FEE_PCT = 0.0
CRYPTO_TAKER_FEE_PCT = 0.0025      # 0.25% per side
CRYPTO_MAKER_FEE_PCT = 0.0015

# Conservative spread assumptions when no quote is available.
DEFAULT_EQUITY_SPREAD_PCT = 0.0005     # 5 bps
DEFAULT_CRYPTO_SPREAD_PCT = 0.0010     # 10 bps
DEFAULT_ILLIQUID_SPREAD_PCT = 0.0040   # 40 bps for thin names

# Measured from this system's own fills (Execution Slippage panel): the
# median was -0.21% and the mean -0.50% across 50 fills. Using the MEDIAN
# as the default because the mean is skewed by a few bad prints, but never
# assuming better than measured reality.
DEFAULT_SLIPPAGE_PCT = 0.0021

# A market order crosses the spread; a resting limit does not.
MARKET_ORDER_SPREAD_MULTIPLIER = 1.0
LIMIT_ORDER_SPREAD_MULTIPLIER = 0.0


def is_crypto_symbol(symbol: str) -> bool:
    s = str(symbol or "").upper()
    return "/" in s or s.endswith("USD")


def estimate_spread_pct(symbol: str, quoted_spread_pct: float | None = None,
                        illiquid: bool = False) -> tuple[float, str]:
    """(spread as a fraction of price, source label)."""
    if quoted_spread_pct is not None and quoted_spread_pct >= 0:
        return float(quoted_spread_pct), "quoted"
    if illiquid:
        return DEFAULT_ILLIQUID_SPREAD_PCT, "default_illiquid"
    if is_crypto_symbol(symbol):
        return DEFAULT_CRYPTO_SPREAD_PCT, "default_crypto"
    return DEFAULT_EQUITY_SPREAD_PCT, "default_equity"


def fee_pct(symbol: str, maker: bool = False) -> float:
    """Per-side fee as a fraction of notional."""
    if not is_crypto_symbol(symbol):
        return EQUITY_FEE_PCT
    return CRYPTO_MAKER_FEE_PCT if maker else CRYPTO_TAKER_FEE_PCT


def funding_cost_pct(symbol: str, hold_hours: float,
                     funding_rate_8h: float | None = None,
                     is_short: bool = False) -> tuple[float, str]:
    """Perpetual funding paid over the expected hold.

    Funding is a TRANSFER: longs pay shorts when the rate is positive, and
    shorts pay longs when it is negative. A short with positive funding
    RECEIVES it, so this can legitimately return a negative cost.
    """
    if not is_crypto_symbol(symbol) or not hold_hours or hold_hours <= 0:
        return 0.0, "not_applicable"
    if funding_rate_8h is None:
        return 0.0, "unknown_rate_excluded"
    periods = hold_hours / 8.0
    paid = float(funding_rate_8h) * periods
    return (-paid if is_short else paid), "measured"


def estimate_costs(symbol: str, entry: float, stop: float, *,
                   quoted_spread_pct: float | None = None,
                   slippage_pct: float | None = None,
                   order_type: str = "market",
                   maker: bool = False,
                   hold_hours: float = 0.0,
                   funding_rate_8h: float | None = None,
                   is_short: bool = False,
                   illiquid: bool = False) -> dict:
    """Full round-trip cost estimate, in both percent and R."""
    entry = float(entry or 0)
    risk_distance = abs(entry - float(stop or 0))
    if entry <= 0:
        return {"ok": False, "reason": "no entry price"}

    spread_pct, spread_src = estimate_spread_pct(symbol, quoted_spread_pct, illiquid)
    crossing = (MARKET_ORDER_SPREAD_MULTIPLIER
                if str(order_type).lower() == "market" else LIMIT_ORDER_SPREAD_MULTIPLIER)
    # Half-spread per side, crossed on both entry and exit for market orders.
    spread_cost_pct = spread_pct * crossing

    per_side_fee = fee_pct(symbol, maker=maker)
    fee_cost_pct = per_side_fee * 2.0          # in and out

    slip = DEFAULT_SLIPPAGE_PCT if slippage_pct is None else float(slippage_pct)
    slip_cost_pct = abs(slip) * 2.0            # both sides

    fund_pct, fund_src = funding_cost_pct(symbol, hold_hours, funding_rate_8h, is_short)

    total_pct = spread_cost_pct + fee_cost_pct + slip_cost_pct + fund_pct

    # Convert to R. Without a risk distance there is no R to speak of.
    def to_r(pct: float) -> float | None:
        if risk_distance <= 0:
            return None
        return (pct * entry) / risk_distance

    return {
        "ok": True,
        "symbol": symbol,
        "spread_pct": round(spread_cost_pct, 6),
        "spread_source": spread_src,
        "fees_pct": round(fee_cost_pct, 6),
        "slippage_pct": round(slip_cost_pct, 6),
        "slippage_source": "measured" if slippage_pct is not None else "default_from_measured_median",
        "funding_pct": round(fund_pct, 6),
        "funding_source": fund_src,
        "total_pct": round(total_pct, 6),
        "total_r": round(to_r(total_pct), 4) if to_r(total_pct) is not None else None,
        "spread_r": round(to_r(spread_cost_pct), 4) if to_r(spread_cost_pct) is not None else None,
        "fees_r": round(to_r(fee_cost_pct), 4) if to_r(fee_cost_pct) is not None else None,
        "slippage_r": round(to_r(slip_cost_pct), 4) if to_r(slip_cost_pct) is not None else None,
        "funding_r": round(to_r(fund_pct), 4) if to_r(fund_pct) is not None else None,
        "risk_distance": risk_distance,
        "note": (
            "Round-trip estimate. Costs in R are the number that matters: the same "
            "fee is trivial on a wide stop and decisive on a tight one."
        ),
    }


def net_expected_r(gross_expected_r: float, costs: dict) -> dict:
    """Subtract costs from gross expectancy and say whether it survives."""
    cost_r = (costs or {}).get("total_r")
    if cost_r is None:
        return {
            "gross_expected_r": round(float(gross_expected_r), 4),
            "expected_cost_r": None,
            "net_expected_r": None,
            "tradeable": False,
            "reason": "cost in R is unknown (no risk distance) — cannot verify the edge survives",
        }
    net = float(gross_expected_r) - float(cost_r)
    return {
        "gross_expected_r": round(float(gross_expected_r), 4),
        "expected_cost_r": round(float(cost_r), 4),
        "net_expected_r": round(net, 4),
        "tradeable": net > 0,
        "reason": (
            f"costs consume {cost_r:.3f}R of {gross_expected_r:.3f}R gross edge"
            if net <= 0 else
            f"{net:.3f}R survives after {cost_r:.3f}R of costs"
        ),
    }
