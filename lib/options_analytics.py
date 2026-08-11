"""
Options flow intelligence — real chain data from Alpaca's options market
data API (already the broker/data provider this whole app runs on; no new
vendor, uses the same credentials as everything else). Verified live
against the real account while building this: Alpaca's snapshot gives
real implied volatility and full Greeks (delta/gamma/theta/vega/rho) for
the large majority of contracts, plus live bid/ask quotes.

IMPORTANT — what this can and can't honestly claim: Alpaca's options
snapshot has NO open interest field and NO cumulative daily volume (only
the single latest trade's size). That rules out the two classic "unusual
options activity" signals (volume-vs-OI, OI change) — this module doesn't
attempt them, rather than approximating with data that isn't actually
there. What it computes instead is genuinely backed by what the API
returns: put/call ratio by contract-with-a-recent-trade, IV skew, a
market-implied expected move from the ATM straddle, and aggregate visible
delta/gamma across the fetched chain slice (explicitly labeled as a
snapshot of the fetched strikes/expirations, not total market-wide
dealer positioning, which needs OI this API doesn't provide).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# OCC option symbol: {root}{YYMMDD}{C|P}{strike * 1000, zero-padded to 8 digits}
# e.g. "AAPL260810P00255000" -> AAPL, 2026-08-10, Put, strike 255.00
_OCC_RE = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")


def parse_occ_symbol(symbol: str) -> dict | None:
    m = _OCC_RE.match(symbol)
    if not m:
        return None
    root, date_str, opt_type, strike_str = m.groups()
    try:
        expiration = datetime.strptime(date_str, "%y%m%d").date()
    except ValueError:
        return None
    return {
        "root": root,
        "expiration": expiration.isoformat(),
        "type": "call" if opt_type == "C" else "put",
        "strike": int(strike_str) / 1000.0,
    }


def snapshot_to_row(symbol: str, snapshot) -> dict | None:
    """Flatten one Alpaca OptionsSnapshot + its OCC-parsed symbol into a
    plain dict for the pure aggregation functions below."""
    parsed = parse_occ_symbol(symbol)
    if not parsed:
        return None
    quote = snapshot.latest_quote
    trade = snapshot.latest_trade
    greeks = snapshot.greeks
    bid = getattr(quote, "bid_price", None) if quote else None
    ask = getattr(quote, "ask_price", None) if quote else None
    mid = round((bid + ask) / 2, 4) if bid is not None and ask is not None and (bid > 0 or ask > 0) else None
    return {
        "symbol": symbol,
        **parsed,
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "last_trade_price": getattr(trade, "price", None) if trade else None,
        "last_trade_size": getattr(trade, "size", None) if trade else None,
        "has_recent_trade": trade is not None,
        "implied_volatility": snapshot.implied_volatility,
        "delta": getattr(greeks, "delta", None) if greeks else None,
        "gamma": getattr(greeks, "gamma", None) if greeks else None,
        "theta": getattr(greeks, "theta", None) if greeks else None,
        "vega": getattr(greeks, "vega", None) if greeks else None,
    }


def summarize_chain(rows: list[dict], current_price: float | None = None) -> dict:
    """Pure aggregation over already-flattened chain rows (see
    snapshot_to_row). Every metric here is computed only from fields the
    API actually returns — see module docstring for what's deliberately
    NOT included and why."""
    calls = [r for r in rows if r["type"] == "call"]
    puts = [r for r in rows if r["type"] == "put"]
    traded_calls = [r for r in calls if r["has_recent_trade"]]
    traded_puts = [r for r in puts if r["has_recent_trade"]]

    put_call_ratio = round(len(traded_puts) / len(traded_calls), 3) if traded_calls else None

    call_ivs = [r["implied_volatility"] for r in calls if r["implied_volatility"] is not None]
    put_ivs = [r["implied_volatility"] for r in puts if r["implied_volatility"] is not None]
    avg_call_iv = round(sum(call_ivs) / len(call_ivs), 4) if call_ivs else None
    avg_put_iv = round(sum(put_ivs) / len(put_ivs), 4) if put_ivs else None
    iv_skew = round(avg_put_iv - avg_call_iv, 4) if avg_call_iv is not None and avg_put_iv is not None else None

    total_call_delta = round(sum(r["delta"] for r in calls if r["delta"] is not None), 2)
    total_put_delta = round(sum(r["delta"] for r in puts if r["delta"] is not None), 2)
    total_gamma = round(sum(r["gamma"] for r in rows if r["gamma"] is not None), 4)

    expirations = sorted({r["expiration"] for r in rows})
    nearest_expiration = expirations[0] if expirations else None

    top_iv = sorted(
        (r for r in rows if r["implied_volatility"] is not None),
        key=lambda r: -r["implied_volatility"],
    )[:5]

    expected_move = None
    if current_price and nearest_expiration:
        expected_move = compute_expected_move(rows, current_price, nearest_expiration)

    return {
        "contracts_analyzed": len(rows),
        "call_count": len(calls), "put_count": len(puts),
        "traded_call_count": len(traded_calls), "traded_put_count": len(traded_puts),
        "put_call_ratio": put_call_ratio,
        "avg_call_iv": avg_call_iv, "avg_put_iv": avg_put_iv, "iv_skew": iv_skew,
        "total_call_delta": total_call_delta, "total_put_delta": total_put_delta,
        "total_gamma": total_gamma,
        "expirations_covered": expirations,
        "nearest_expiration": nearest_expiration,
        "expected_move": expected_move,
        "top_iv_contracts": [
            {"symbol": r["symbol"], "type": r["type"], "strike": r["strike"], "expiration": r["expiration"], "iv": r["implied_volatility"]}
            for r in top_iv
        ],
    }


def compute_expected_move(rows: list[dict], current_price: float, expiration: str) -> dict | None:
    """Market-implied expected move from the ATM straddle: the combined
    price of the call+put closest to the current price, at the given
    expiration — a standard, honest options-derived metric that needs
    only quotes, not open interest or volume."""
    candidates = [r for r in rows if r["expiration"] == expiration and r["mid"] is not None]
    if not candidates:
        return None
    atm_strike = min({r["strike"] for r in candidates}, key=lambda s: abs(s - current_price))
    call = next((r for r in candidates if r["strike"] == atm_strike and r["type"] == "call"), None)
    put = next((r for r in candidates if r["strike"] == atm_strike and r["type"] == "put"), None)
    if not call or not put:
        return None
    straddle_price = call["mid"] + put["mid"]
    return {
        "strike": atm_strike,
        "expiration": expiration,
        "straddle_price": round(straddle_price, 2),
        "expected_move_pct": round(straddle_price / current_price * 100, 2) if current_price else None,
        "expected_move_low": round(current_price - straddle_price, 2),
        "expected_move_high": round(current_price + straddle_price, 2),
    }


def get_chain_summary(underlying: str, current_price: float, dte_max: int = 45, strike_pct_range: float = 0.15) -> dict | None:
    """Fetches a real Alpaca options chain slice (near-term expirations,
    strikes within strike_pct_range of current_price) and summarizes it.
    Returns None on any fetch failure — options data requires the account
    have market data access; a plain connectivity/entitlement failure
    shouldn't crash the caller."""
    try:
        from lib.alpaca_client import get_option_data_client
        from alpaca.data.requests import OptionChainRequest
    except ImportError:
        return None

    today = datetime.now(timezone.utc).date()
    try:
        client = get_option_data_client()
        req = OptionChainRequest(
            underlying_symbol=underlying.upper(),
            expiration_date_gte=today,
            expiration_date_lte=today + timedelta(days=dte_max),
            strike_price_gte=round(current_price * (1 - strike_pct_range), 2),
            strike_price_lte=round(current_price * (1 + strike_pct_range), 2),
        )
        chain = client.get_option_chain(req)
    except Exception as e:
        logger.warning(f"[Options] Chain fetch failed for {underlying}: {e}")
        return None

    rows = [row for sym, snap in chain.items() if (row := snapshot_to_row(sym, snap))]
    if not rows:
        return None
    summary = summarize_chain(rows, current_price=current_price)
    summary["underlying"] = underlying.upper()
    summary["current_price"] = current_price
    summary["fetched_at"] = datetime.now(timezone.utc).isoformat()
    return summary
