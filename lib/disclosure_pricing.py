"""Reconstructed market prices for disclosed trades — labelled as estimates.

STOCK Act filings disclose an AMOUNT RANGE and dates. They never disclose
an execution price, a share count, or a time of day. The operator asked to
see "what they bought, when and how much at what price", and the honest
answer to the last part is that the price is not in the filing and cannot
be. What CAN be done is joining the disclosed transaction date to the
desk's own daily bars and saying what the security traded at that day.

Everything here is therefore an ESTIMATE and is named as one in every
field it produces. The rules that keep it honest:

- The estimate is the day's OHLC, not a single invented number. A filing
  says "on 2026-06-30" — it does not say at what point in that session, so
  a range is the truthful shape and the close is offered only as a
  reference point.
- Implied share counts come from the disclosed RANGE, so they are a range
  too: low_amount/price to high_amount/price. A midpoint would look like
  a measurement and is not one.
- No bar, no estimate. A ticker the cache has never held returns None
  rather than a nearby day's price, because "close enough" on a gapping
  security is how a reconstruction becomes a fabrication.
- Nothing here is evidence of anything. A disclosed trade is a legally
  required disclosure; the pricing is context for reading it.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# How far either side of the disclosed date to look for a session. Markets
# close at weekends and holidays and a filing may name a non-trading day;
# 4 days covers a long weekend without wandering into a different week.
_WINDOW_DAYS = 4


def _parse_date(d) -> datetime | None:
    if not d:
        return None
    try:
        return datetime.fromisoformat(str(d)[:10]).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def price_on_date(ticker: str, date_str: str) -> dict | None:
    """The session's OHLC for `ticker` on `date_str`, from cached daily bars.

    Returns None when there is no bar for that exact session — see the
    module docstring on why a neighbouring day is not substituted.
    """
    if not ticker or not date_str:
        return None
    day = _parse_date(date_str)
    if day is None:
        return None
    try:
        from lib.ohlcv_cache import get_cached_range
        df = get_cached_range(ticker.upper(), "1D",
                              day - timedelta(days=_WINDOW_DAYS),
                              day + timedelta(days=_WINDOW_DAYS))
    except Exception as e:
        logger.debug(f"[DisclosurePricing] {ticker} {date_str}: {e}")
        return None
    if df is None or not len(df):
        return None

    target = day.date().isoformat()
    for idx, row in df.iterrows():
        # Index is the bar timestamp; compare on the date only.
        stamp = str(getattr(idx, "date", lambda: idx)())[:10]
        if stamp != target:
            continue
        try:
            o, h, l, c = (float(row["open"]), float(row["high"]),
                          float(row["low"]), float(row["close"]))
        except (KeyError, TypeError, ValueError):
            return None
        if c <= 0:
            return None
        return {"open": o, "high": h, "low": l, "close": c, "session": stamp}
    return None


def estimate_trade(trade: dict) -> dict:
    """Add `price_estimate` to one serialized disclosure row.

    The added block is either None — meaning "no bar, no claim" — or a dict
    whose every key says what it is:

        basis            what the numbers came from, in words
        session          the trading day actually used
        close/open/high/low   that session's real prices
        implied_shares_low/high   from the DISCLOSED RANGE, not a midpoint
        note             the standing caveat, carried with the data

    Deliberately returns a NEW dict. The serialized filing is what the
    government published; the estimate travels beside it, never merged into
    the disclosed fields.
    """
    ticker = trade.get("ticker")
    px = price_on_date(ticker, trade.get("transaction_date"))
    if not px:
        return {**trade, "price_estimate": None}

    lo, hi = trade.get("amount_low"), trade.get("amount_high")
    close = px["close"]
    shares_low = (float(lo) / close) if lo else None
    shares_high = (float(hi) / close) if hi else None

    return {**trade, "price_estimate": {
        "basis": "daily bar for the disclosed transaction date",
        "session": px["session"],
        "open": round(px["open"], 4),
        "high": round(px["high"], 4),
        "low": round(px["low"], 4),
        "close": round(close, 4),
        "implied_shares_low": round(shares_low, 2) if shares_low else None,
        "implied_shares_high": round(shares_high, 2) if shares_high else None,
        "note": ("ESTIMATE. The filing discloses an amount range and a date, "
                 "never a price, a size or a time of day. These are that "
                 "session's actual prices; the share counts follow from the "
                 "disclosed range, not from anything filed."),
    }}


def estimate_trades(trades: list[dict]) -> tuple[list[dict], dict]:
    """`estimate_trade` over a list, with a coverage summary.

    Coverage is returned because a panel showing estimates for a third of
    the rows should say so — 'priced 12 of 37' is the difference between a
    partial view and a wrong one.
    """
    out = [estimate_trade(t) for t in trades or []]
    priced = sum(1 for t in out if t.get("price_estimate"))
    return out, {
        "priced": priced,
        "total": len(out),
        "unpriced": len(out) - priced,
        "note": ("Unpriced rows have no cached daily bar for that exact "
                 "session — no nearby day is substituted."),
    }
