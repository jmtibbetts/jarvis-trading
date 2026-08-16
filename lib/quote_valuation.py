"""What a quote leg was worth IN DOLLARS, at the moment it traded.

Wallet scoring reconstructed round trips from transfer legs and treated the
quote amount as the trade's value. That is true for USDC and false for SOL,
and the two were summed as though they shared a unit:

    500 USDC  ->  cost_basis = 500
    3 SOL     ->  cost_basis = 3

then `sum(t["pnl"] for t in wins)` added them together and called the
result dollars. Worse than the cross-trade aggregation the audit describes:
a wallet that bought with USDC and sold for SOL had `proceeds - cost_basis`
subtracting SOL from dollars INSIDE a single trade.

Three rules here, and they are the whole module:

  1. A quantity without a unit is not a value. Every amount is paired with
     the asset it is denominated in before anything is added.
  2. A HISTORICAL trade needs a HISTORICAL price. Valuing a 2024 SOL trade
     at today's SOL price is not a rounding error — SOL has traded between
     $8 and $260 in this dataset's window.
  3. No price is better than an invented one. An unresolvable quote yields
     UNKNOWN and the trade is excluded from scoring, counted, and reported.

Provenance travels with every number, because "assumed the peg held" and
"read the hourly close" are different claims and the difference matters
during exactly the events worth measuring.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# Quality vocabulary, shared with the rest of the desk's provenance model.
MEASURED = "MEASURED"          # read from stored market data
ESTIMATED = "ESTIMATED"        # a documented assumption (the peg)
UNAVAILABLE = "UNAVAILABLE"    # no credible price — value is UNKNOWN

# Quote assets this desk can value. Anything else is UNKNOWN by design:
# a token-to-token swap has no dollar value without a price for both legs.
SOL_SYMBOLS = frozenset({"SOL", "WSOL", "wSOL", "So11111111111111111111111111111111111111112"})

# Dollar stables usable as a quote. Resolved through the pegged-asset
# registry so this is not a fourth hardcoded stablecoin list — see
# lib/pegged_assets for why a flat set is the wrong shape.
_PEG_PRICE_USD = 1.0

# How far from the requested timestamp an hourly bar may sit and still be
# called that moment's price. Beyond this the answer is UNKNOWN rather than
# a stale close wearing a historical timestamp.
MAX_BAR_DISTANCE_HOURS = 6

_sol_bars: list[tuple[float, float]] | None = None      # (epoch_s, close)


def _is_stable_quote(symbol: str | None) -> bool:
    if not symbol:
        return False
    try:
        from lib.pegged_assets import lookup
        a = lookup(symbol)
        # USD-pegged and actually targeting par. A yield wrapper such as
        # sUSDe is NOT worth a dollar and must not be valued as one.
        return bool(a and a.is_usd_pegged and a.targets_par)
    except Exception:
        return str(symbol).upper() in ("USDC", "USDT", "PYUSD", "USDS")


def _is_sol_quote(symbol: str | None) -> bool:
    return str(symbol or "") in SOL_SYMBOLS or str(symbol or "").upper() == "SOL"


def _load_sol_bars() -> list[tuple[float, float]]:
    """SOL/USD hourly closes, ascending, loaded once.

    Read straight from the OHLCV cache: 43,100 hourly bars back to 2021,
    which covers any wallet history Helius will return.
    """
    global _sol_bars
    if _sol_bars is not None:
        return _sol_bars
    rows: list[tuple[float, float]] = []
    try:
        from lib.ohlcv_cache import OHLCVBar, get_cache_db
        with get_cache_db() as db:
            bars = (db.query(OHLCVBar.ts, OHLCVBar.close)
                    .filter(OHLCVBar.symbol == "SOL/USD",
                            OHLCVBar.timeframe == "1H")
                    .order_by(OHLCVBar.ts.asc()).all())
            for ts, close in bars:
                try:
                    t = datetime.fromisoformat(str(ts))
                    if t.tzinfo is None:
                        t = t.replace(tzinfo=timezone.utc)
                    rows.append((t.timestamp(), float(close)))
                except (TypeError, ValueError):
                    continue
    except Exception as e:
        logger.warning(f"[QuoteValuation] SOL history unavailable: {e}")
    _sol_bars = rows
    return rows


def reset_cache() -> None:
    """Drop the in-process bar cache (tests, and after a backfill)."""
    global _sol_bars
    _sol_bars = None


def _sol_price_at(ts: float) -> tuple[float | None, str]:
    bars = _load_sol_bars()
    if not bars:
        return None, "no SOL/USD history in the OHLCV cache"
    import bisect
    idx = bisect.bisect_left(bars, (ts,))
    best, best_d = None, None
    for i in (idx - 1, idx):
        if 0 <= i < len(bars):
            d = abs(bars[i][0] - ts)
            if best_d is None or d < best_d:
                best, best_d = bars[i], d
    if best is None:
        return None, "no bar near that timestamp"
    if best_d > MAX_BAR_DISTANCE_HOURS * 3600:
        hrs = best_d / 3600.0
        return None, f"nearest SOL bar is {hrs:.1f}h away — too stale to price this trade"
    return best[1], f"ohlcv_cache SOL/USD 1H, {best_d / 60.0:.0f}m from the trade"


def quote_price_usd(symbol: str | None, timestamp_s: float | None) -> dict:
    """USD price of one unit of a QUOTE asset, at a moment in time.

    Always returns a dict; `price_usd` is None when the answer is unknown,
    and the caller must treat that as unpriced rather than substituting a
    number of its own.
    """
    out = {
        "quote_symbol": symbol,
        "price_usd": None,
        "source": None,
        "quality": UNAVAILABLE,
        "as_of": None,
        "reason": None,
    }

    if _is_stable_quote(symbol):
        out.update(price_usd=_PEG_PRICE_USD, source="stable_assumed_peg",
                   quality=ESTIMATED, as_of=timestamp_s,
                   reason=("dollar peg assumed at par; depeg periods exist and "
                           "are not modelled here"))
        return out

    if _is_sol_quote(symbol):
        if not timestamp_s:
            out["reason"] = "no trade timestamp — a historical price needs one"
            return out
        price, why = _sol_price_at(float(timestamp_s))
        if price is None:
            out["reason"] = why
            return out
        out.update(price_usd=price, source=why, quality=MEASURED,
                   as_of=timestamp_s)
        return out

    out["reason"] = (f"{symbol!r} is not a quote asset this desk can value; "
                     f"a token-to-token swap has no dollar value without a "
                     f"price for both legs")
    return out


def value_in_usd(amount: float | None, symbol: str | None,
                 timestamp_s: float | None) -> dict:
    """`amount` of `symbol` at `timestamp_s`, in dollars, with provenance."""
    p = quote_price_usd(symbol, timestamp_s)
    try:
        amt = float(amount)
    except (TypeError, ValueError):
        amt = None
    usd = None
    if amt is not None and p["price_usd"] is not None:
        usd = amt * p["price_usd"]
    return {
        "quote_amount": amt,
        "quote_symbol": symbol,
        "quote_price_usd": p["price_usd"],
        "usd_value": usd,
        "price_source": p["source"],
        "price_quality": p["quality"],
        "price_as_of": p["as_of"],
        "reason": p["reason"],
    }


def is_valuable_quote(symbol: str | None) -> bool:
    """Whether this asset can serve as a priceable quote leg at all."""
    return _is_stable_quote(symbol) or _is_sol_quote(symbol)
