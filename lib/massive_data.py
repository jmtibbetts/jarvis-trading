"""
Massive market data — direct REST integration, entitlement-aware.

Why REST and not the WebSocket the SDK also offers: verified live against
the real account — the WS handshake authenticates and then returns
"Your plan doesn't include websocket access", and snapshots return
NOT_AUTHORIZED. What this plan DOES include (all verified live):

    previous-close aggregates   equities and crypto (X:BTCUSD form)
    daily aggregates            with vwap AND per-bar transaction counts
                                (a field Alpaca bars don't carry)
    ticker details              company reference data

The plan is also tightly rate-limited (~5 requests/minute — news/minute-agg
probes exhausted retries), so this module is deliberately SPARING: at most
two API calls per summary, a 5-minute in-process cache, and it is invoked
only from user-initiated paths (the analyst's market_data intent), never
from schedulers. If the plan is upgraded later, the WebSocket client from
the SDK docs can be added as a streaming source — building it now would
ship dead code behind an entitlement wall.

Also why this exists instead of Massive's MCP server: the hosted MCP
endpoint authenticates via OAuth JWT (fine for LM Studio's interactive UI,
verified live: a raw API key gets "JWT validation failed"), while the REST
API takes the key directly.
"""
from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 300
_cache: dict[str, tuple[float, dict]] = {}

# Plan-level rate budget: ~5 requests/minute (verified live — faster probing
# exhausted retries). A summary costs 2 calls, so this window admits at most
# 2 summaries/minute and rejects further ones CLEANLY instead of letting the
# SDK spin in its own retry loop against 429s.
RATE_LIMIT_CALLS = 5
RATE_WINDOW_SECONDS = 60.0
_call_times: list[float] = []


def _budget_allows(calls_needed: int) -> bool:
    now = time.time()
    while _call_times and now - _call_times[0] > RATE_WINDOW_SECONDS:
        _call_times.pop(0)
    return len(_call_times) + calls_needed <= RATE_LIMIT_CALLS


def _spend_budget(calls: int):
    _call_times.extend([time.time()] * calls)


def is_configured() -> bool:
    return bool(os.getenv("MASSIVE_API_KEY"))


def _to_massive_symbol(symbol: str) -> str:
    """App-native symbols to Massive's: crypto BASE/USD -> X:BASEUSD."""
    s = (symbol or "").upper().strip()
    if "/" in s:
        base, quote = s.split("/", 1)
        return f"X:{base}{quote}"
    return s


def get_market_summary(symbol: str, days: int = 10) -> dict | None:
    """Previous close + recent daily bars for one symbol. Two API calls,
    cached 5 minutes. Returns None when unconfigured or on any failure —
    callers fall back to their next source rather than surfacing an error."""
    if not is_configured():
        return None
    key = f"{symbol}:{days}"
    now = time.time()
    cached = _cache.get(key)
    if cached and now - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]

    if not _budget_allows(2):
        logger.info(f"[Massive] Rate budget exhausted — skipping fresh fetch for {symbol}")
        return cached[1] if cached else None
    _spend_budget(2)

    try:
        from datetime import date, timedelta
        from massive import RESTClient
        client = RESTClient(api_key=os.environ["MASSIVE_API_KEY"])
        msym = _to_massive_symbol(symbol)

        prev_rows = client.get_previous_close_agg(msym)
        prev = prev_rows[0] if prev_rows else None

        end = date.today()
        start = end - timedelta(days=days * 2)  # calendar padding over weekends
        aggs = list(client.list_aggs(msym, 1, "day", start.isoformat(), end.isoformat(), limit=days))
    except Exception as e:
        logger.warning(f"[Massive] Summary fetch failed for {symbol}: {e}")
        return None

    if prev is None and not aggs:
        return None

    bars = [{
        "open": a.open, "high": a.high, "low": a.low, "close": a.close,
        "volume": a.volume, "vwap": a.vwap, "transactions": a.transactions,
        "timestamp_ms": a.timestamp,
    } for a in aggs[-days:]]

    change_pct = None
    if len(bars) >= 2 and bars[-2]["close"]:
        change_pct = round((bars[-1]["close"] - bars[-2]["close"]) / bars[-2]["close"] * 100, 2)

    summary = {
        "provider": "massive",
        "symbol": symbol,
        "massive_symbol": msym,
        "previous_close": {
            "close": prev.close, "open": prev.open, "high": prev.high, "low": prev.low,
            "volume": prev.volume, "vwap": prev.vwap,
        } if prev else None,
        "daily_bars": bars,
        "last_close_change_pct": change_pct,
        "note": (
            "Massive REST daily data (previous close + daily aggregates with "
            "vwap and per-bar transaction counts). Plan has no websocket/"
            "snapshot entitlement — this is end-of-day/previous-session data, "
            "not live quotes."
        ),
    }
    _cache[key] = (now, summary)
    return summary
