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
                        illiquid: bool = False,
                        venue: str | None = None) -> tuple[float, str]:
    """(spread as a fraction of price, source label).

    Preference order: a spread the caller quoted, then a LIVE measurement
    from the venue, then a conservative default. The defaults exist for
    when nothing is known — measured against Kraken, DEFAULT_CRYPTO_SPREAD_PCT
    was 16x too wide for BTC, which inflated the minimum viable stop and
    rejected sound trades. A measurement beats a guess; a failed
    measurement falls back to the guess rather than to optimism.
    """
    if quoted_spread_pct is not None and quoted_spread_pct >= 0:
        return float(quoted_spread_pct), "quoted"
    if illiquid:
        return DEFAULT_ILLIQUID_SPREAD_PCT, "default_illiquid"

    if is_crypto_symbol(symbol):
        # 1. The live book, if the stream has it. This is the current
        #    spread, not a snapshot from up to five minutes ago — which
        #    matters most in exactly the fast conditions where a stale
        #    quote is least like the fill you will get.
        try:
            from lib.kraken_stream import live_spread_pct
            live, why = live_spread_pct(symbol)
            if live is not None and live >= 0:
                return max(live, 0.00001), f"live: {why}"
        except Exception:
            pass
        # 2. A REST measurement, cached briefly.
        try:
            from lib.venues import measured_spread_pct, DEFAULT_VENUE
            measured, why = measured_spread_pct(symbol, venue or DEFAULT_VENUE)
            if measured is not None and measured >= 0:
                return max(measured, 0.00001), why
        except Exception:
            pass
        # 3. The conservative default. Every failure lands HERE, never on
        #    a number that makes the trade look cheaper.
        return DEFAULT_CRYPTO_SPREAD_PCT, "default_crypto"
    return DEFAULT_EQUITY_SPREAD_PCT, "default_equity"


def fee_pct(symbol: str, maker: bool = False, venue: str | None = None,
            leveraged: bool = False) -> float:
    """Per-side fee as a fraction of notional, for the venue that will fill it.

    Venue matters more than it looks: Kraken's base taker fee is 0.40% against
    Alpaca's 0.25%, and P0 rejects trades whose costs exceed 0.50R — so a fee
    assumption wrong by 60% moves the line between tradeable and not. Falls
    back to the module constants when the venue registry is unavailable, so a
    missing import can never make a trade look free.
    """
    if not is_crypto_symbol(symbol):
        return EQUITY_FEE_PCT
    # A LEVERAGED position is a perpetual, and perpetuals are priced on a
    # different schedule entirely — 0.05% taker against spot's 0.80%, a 16x
    # difference. Charging spot fees on a derivative overstates its cost by
    # that factor, which in a system that rejects above 0.50R means refusing
    # setups that are comfortably viable.
    if leveraged:
        try:
            from lib.venues import futures_fee_for
            rate, _ = futures_fee_for(symbol, maker=maker)
            if rate is not None:
                return rate
        except Exception:
            pass
    try:
        from lib.venues import fee_for, DEFAULT_VENUE
        rate, _ = fee_for(venue or DEFAULT_VENUE, maker=maker, asset_class="crypto")
        return rate
    except Exception:
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
    source = "measured"
    if funding_rate_8h is None:
        # The rate is COLLECTED — crypto_derivatives_snapshots has been
        # storing it all along — but nothing ever handed it to this function,
        # so every caller got 0.0 and funding silently did not exist. It is
        # negligible on a scalp and material on a hold: at the standard
        # 0.01%/8h, four weeks costs 0.84% of NOTIONAL, which on 9x leverage
        # is 7.6% of the margin behind the trade.
        funding_rate_8h, source = _latest_funding_rate(symbol)
        if funding_rate_8h is None:
            return 0.0, "unknown_rate_excluded"
    periods = hold_hours / 8.0
    paid = float(funding_rate_8h) * periods
    return (-paid if is_short else paid), source


# The published baseline perpetual funding rate: 0.01% per 8-hour period.
# Used only when no measurement is on file, and never in place of one.
DEFAULT_FUNDING_RATE_8H = 0.0001


def _latest_funding_rate(symbol: str) -> tuple[float | None, str]:
    """Most recent measured 8-hour funding rate for this underlying.

    A missing measurement falls back to the published baseline rather than
    to zero: pricing an unknown funding rate as free is the same class of
    error as pricing an unknown fee as free, and it flatters exactly the
    trades that hold longest.
    """
    base = str(symbol or "").upper().split("/")[0].replace("XBT", "BTC").strip()
    try:
        from app.database import get_db, CryptoDerivativesSnapshot
        with get_db() as db:
            row = (db.query(CryptoDerivativesSnapshot)
                     .filter(CryptoDerivativesSnapshot.symbol == base)
                     .order_by(CryptoDerivativesSnapshot.fetched_at.desc())
                     .first())
            if row is not None and row.funding_rate is not None:
                return float(row.funding_rate), "measured"
    except Exception:
        pass
    return DEFAULT_FUNDING_RATE_8H, "default_baseline"


# Equity short borrow. Selling a stock short means borrowing it, and the
# lender charges an annualised fee that accrues daily. General-collateral
# names are cheap; hard-to-borrow names are not, and a squeeze candidate —
# exactly the setup a short-interest signal surfaces — can cost more per
# year than the trade expects to make. Treating borrow as free made every
# equity short look cheaper than it is.
DEFAULT_BORROW_RATE_ANNUAL = 0.0050      # 50 bps, typical general collateral
HARD_TO_BORROW_RATE_ANNUAL = 0.30        # 30%, a genuinely tight name
TRADING_DAYS_PER_YEAR = 252


def borrow_cost_pct(symbol: str, hold_hours: float, *, is_short: bool,
                    borrow_rate_annual: float | None = None,
                    hard_to_borrow: bool = False) -> tuple[float, str]:
    """Stock-borrow cost over the expected hold, as a fraction of notional.

    Longs never pay it. Crypto is excluded because this system's crypto
    shorts are perpetual-swap positions, whose carry is funding — already
    modelled separately, and double-counting it would reject valid trades.
    """
    if not is_short or is_crypto_symbol(symbol) or not hold_hours or hold_hours <= 0:
        return 0.0, "not_applicable"
    if borrow_rate_annual is None:
        rate = HARD_TO_BORROW_RATE_ANNUAL if hard_to_borrow else DEFAULT_BORROW_RATE_ANNUAL
        source = "default_hard_to_borrow" if hard_to_borrow else "default_general_collateral"
    else:
        rate, source = float(borrow_rate_annual), "measured"
    # Borrow accrues on calendar days, including weekends.
    return rate * (float(hold_hours) / 24.0) / 365.0, source


def estimate_costs(symbol: str, entry: float, stop: float, *,
                   quoted_spread_pct: float | None = None,
                   slippage_pct: float | None = None,
                   order_type: str = "market",
                   maker: bool = False,
                   hold_hours: float = 0.0,
                   funding_rate_8h: float | None = None,
                   is_short: bool = False,
                   illiquid: bool = False,
                   borrow_rate_annual: float | None = None,
                   hard_to_borrow: bool = False,
                   venue: str | None = None,
                   leveraged: bool = False) -> dict:
    """Full round-trip cost estimate, in both percent and R."""
    entry = float(entry or 0)
    risk_distance = abs(entry - float(stop or 0))
    if entry <= 0:
        return {"ok": False, "reason": "no entry price"}

    spread_pct, spread_src = estimate_spread_pct(symbol, quoted_spread_pct, illiquid, venue)
    crossing = (MARKET_ORDER_SPREAD_MULTIPLIER
                if str(order_type).lower() == "market" else LIMIT_ORDER_SPREAD_MULTIPLIER)
    # Half-spread per side, crossed on both entry and exit for market orders.
    spread_cost_pct = spread_pct * crossing

    # Futures charge a FLAT fee per contract, not a percentage of notional.
    # Applying the percentage model to an ES contract charged $1,942 instead
    # of $4.50 — a 430x overcharge that would reject every futures trade.
    from lib.instruments import is_futures, get_spec
    if is_futures(symbol):
        spec = get_spec(symbol)
        contract_notional_value = entry * spec.multiplier
        fee_cost_pct = ((spec.commission * 2.0) / contract_notional_value
                        if contract_notional_value > 0 else 0.0)
    else:
        per_side_fee = fee_pct(symbol, maker=maker, venue=venue, leveraged=leveraged)
        fee_cost_pct = per_side_fee * 2.0          # in and out

    slip = DEFAULT_SLIPPAGE_PCT if slippage_pct is None else float(slippage_pct)
    slip_cost_pct = abs(slip) * 2.0            # both sides

    fund_pct, fund_src = funding_cost_pct(symbol, hold_hours, funding_rate_8h, is_short)
    borrow_pct, borrow_src = borrow_cost_pct(
        symbol, hold_hours, is_short=is_short,
        borrow_rate_annual=borrow_rate_annual, hard_to_borrow=hard_to_borrow)

    total_pct = spread_cost_pct + fee_cost_pct + slip_cost_pct + fund_pct + borrow_pct

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
        "fee_venue": (venue or __import__("lib.venues", fromlist=["DEFAULT_VENUE"]).DEFAULT_VENUE)
                     if is_crypto_symbol(symbol) else "equity",
        "slippage_pct": round(slip_cost_pct, 6),
        "slippage_source": "measured" if slippage_pct is not None else "default_from_measured_median",
        "funding_pct": round(fund_pct, 6),
        "funding_source": fund_src,
        "borrow_pct": round(borrow_pct, 6),
        "borrow_source": borrow_src,
        "borrow_r": round(to_r(borrow_pct), 4) if to_r(borrow_pct) is not None else None,
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


def min_viable_stop_pct(symbol: str, *, max_cost_r: float = 0.50,
                        order_type: str = "market", maker: bool = False,
                        quoted_spread_pct: float | None = None,
                        slippage_pct: float | None = None,
                        illiquid: bool = False,
                        venue: str | None = None,
                        leveraged: bool = False) -> float:
    """The tightest stop, as a fraction of entry, that can still pay for itself.

    This is the inverse of estimate_costs. Cost in R is

        cost_r = total_cost_pct * entry / risk_distance
               = total_cost_pct / stop_distance_fraction

    so requiring cost_r <= max_cost_r gives

        stop_distance_fraction >= total_cost_pct / max_cost_r

    Measured against the live book, this is why tight-stop crypto scalping
    could not work: a 0.3% stop against ~1.0% round-trip cost is 3.4R of
    cost before the trade has any chance to be right. Rather than generate
    those signals and reject them downstream, the level builder widens the
    stop to this floor — or the signal is dropped if that floor breaks its
    timeframe's structure.
    """
    spread_pct, _ = estimate_spread_pct(symbol, quoted_spread_pct, illiquid, venue)
    crossing = (MARKET_ORDER_SPREAD_MULTIPLIER
                if str(order_type).lower() == "market" else LIMIT_ORDER_SPREAD_MULTIPLIER)
    slip = DEFAULT_SLIPPAGE_PCT if slippage_pct is None else abs(float(slippage_pct))
    total_cost_pct = spread_pct * crossing + fee_pct(symbol, maker=maker, venue=venue, leveraged=leveraged) * 2.0 + slip * 2.0
    if max_cost_r <= 0:
        return float("inf")
    return total_cost_pct / max_cost_r
