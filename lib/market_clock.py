"""One market clock, from the exchange's own calendar. Never UTC arithmetic.

The check this replaces lived in five places across three jobs as:

    weekday < 5 and 13:30 <= UTC < 20:00

That is the NYSE session *during US daylight time only*. When DST ends
(2026-11-01), the real session becomes 14:30-21:00 UTC and the hard-coded
version silently starts trading the first hour of a closed market and
skipping the last hour of an open one. It also trades every holiday that
falls on a weekday and runs full-length on half-days.

Alpaca's clock/calendar endpoints ARE the venue's own session schedule —
holidays, half-days, and DST included — and this desk already
authenticates to them. This module is a cached veneer over that truth.

Fail-closed contract: if the venue clock cannot be reached and the cache
is stale, `is_equity_market_open()` returns False. Not knowing whether
the market is open is not permission to trade it. Crypto is always open;
futures trade paper-only here and inherit the permissive answer their
simulator expects.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# The venue clock moves once a day; asking more than once a minute buys
# nothing. The STALE limit is how long a cached answer may keep answering
# after refresh failures — one clock outage must not flip the desk's idea
# of the session for the rest of the day.
CLOCK_TTL_SEC = 60.0
CLOCK_STALE_LIMIT_SEC = 30 * 60.0

_lock = threading.Lock()
_cache: dict = {"at": 0.0, "clock": None}


def _fetch_clock():
    from lib.alpaca_client import get_trading_client
    c = get_trading_client().get_clock()
    return {
        "is_open": bool(c.is_open),
        "next_open": str(c.next_open),
        "next_close": str(c.next_close),
        "venue_timestamp": str(c.timestamp),
    }


def equity_clock(now: float | None = None) -> dict | None:
    """The venue's session state, cached. None when genuinely unknown."""
    now = now if now is not None else time.time()
    with _lock:
        age = now - _cache["at"]
        if _cache["clock"] is not None and age < CLOCK_TTL_SEC:
            return _cache["clock"]
    try:
        fresh = _fetch_clock()
        with _lock:
            _cache["clock"] = fresh
            _cache["at"] = now
        return fresh
    except Exception as e:
        logger.warning(f"[MarketClock] venue clock unavailable: {e}")
        with _lock:
            if _cache["clock"] is not None and (now - _cache["at"]) < CLOCK_STALE_LIMIT_SEC:
                return _cache["clock"]
    return None


def is_equity_market_open() -> bool:
    """FAIL CLOSED: unknown means not open. Not knowing whether the market
    is open is not permission to trade it."""
    clock = equity_clock()
    return bool(clock and clock.get("is_open"))


def market_status(asset_class: str | None) -> dict:
    """Session state per asset class, one shape for every caller."""
    ac = str(asset_class or "").strip().lower()
    if ac in ("crypto", "cryptocurrency"):
        return {"asset_class": "crypto", "is_open": True, "source": "always"}
    if ac in ("futures", "forex", "fx"):
        # Paper-only asset classes: their simulator prices whenever data
        # flows. A real futures venue calendar arrives with live futures.
        return {"asset_class": ac, "is_open": True, "source": "paper-only"}
    clock = equity_clock()
    if clock is None:
        return {"asset_class": "equity", "is_open": False, "source": "unknown-fail-closed"}
    return {"asset_class": "equity", "is_open": bool(clock.get("is_open")),
            "next_open": clock.get("next_open"), "next_close": clock.get("next_close"),
            "source": "alpaca"}
