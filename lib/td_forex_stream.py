"""Live forex over Twelve Data's WebSocket — the desk's first real-time
FX feed.

Crypto streams from Kraken, equities from Alpaca; forex had nothing —
the macro context (DXY-adjacent crosses, risk pairs like AUD/JPY) was
polled REST snapshots at best. The Grow plan carries 8 WebSocket
credits; they buy exactly the eight majors/crosses below.

Same discipline as every adapter:
  - provider parsing stays HERE; what leaves is a canonical PriceTick
  - TD stamps each tick with ITS OWN epoch timestamp -> exchange_ts is
    the venue clock and skew is measured, not assumed
  - persistence is throttled to 30s per symbol: forex macro context
    does not need tick resolution, and honest bytes/day beats a
    28MB/day tick firehose nobody asked for (§46)
  - ticks are CACHED under the desk's =X identities (EURUSD=X), never
    under TD's slash spelling — the slash namespace belongs to crypto
  - best-effort and self-healing; a dead feed can never take the desk
    down, and status() says exactly what state it is in
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

WS_URL = "wss://ws.twelvedata.com/v1/quotes/price"
RECONNECT_DELAY_SECONDS = 10.0
PERSIST_INTERVAL_SEC = 30.0

# Exactly the 8 the plan's WebSocket credits cover. TD spelling -> desk
# canonical identity.
FX_SYMBOLS = {
    "EUR/USD": "EURUSD=X", "GBP/USD": "GBPUSD=X", "USD/JPY": "USDJPY=X",
    "USD/CHF": "USDCHF=X", "AUD/USD": "AUDUSD=X", "USD/CAD": "USDCAD=X",
    "NZD/USD": "NZDUSD=X", "EUR/GBP": "EURGBP=X",
}

# desk symbol -> latest tick, for live reads (spread source for FX has
# no book here — this is last-price context, and callers know it).
_ticks: dict[str, dict] = {}
_persist_marks: dict[str, float] = {}
_state = {"running": False, "connected": False, "since": None,
          "error": None, "events": 0}


def latest_price(desk_symbol: str) -> dict | None:
    return _ticks.get(desk_symbol.upper())


def status() -> dict:
    return {**_state, "symbols": sorted(_ticks)}


def handle_price_event(d: dict) -> None:
    """One TD 'price' event -> in-memory tick + throttled canonical event."""
    td_sym = str(d.get("symbol") or "").upper()
    desk_sym = FX_SYMBOLS.get(td_sym)
    if desk_sym is None:
        return
    try:
        price = float(d.get("price"))
    except (TypeError, ValueError):
        return
    ts = d.get("timestamp")
    _ticks[desk_sym] = {"price": price, "td_ts": ts,
                        "at": datetime.now(timezone.utc)}
    _state["events"] += 1

    now = time.time()
    if now - _persist_marks.get(desk_sym, 0.0) < PERSIST_INTERVAL_SEC:
        return
    _persist_marks[desk_sym] = now
    try:
        from lib.market_events import PriceTick, get_queue, make_meta
        get_queue("td_forex_ticks").push(PriceTick(
            meta=make_meta("twelvedata", "td_ws_price_v1",
                           float(ts) if ts is not None else None),
            symbol=desk_sym, price=price))
    except Exception as e:
        logger.debug(f"[TDForexWS] tick event skipped: {e}")


async def _consume() -> None:
    import websockets

    from lib.twelvedata import _api_key

    while _state["running"]:
        try:
            url = f"{WS_URL}?apikey={_api_key()}"
            async with websockets.connect(url, open_timeout=20,
                                          ping_interval=20) as ws:
                await ws.send(json.dumps({
                    "action": "subscribe",
                    "params": {"symbols": ",".join(FX_SYMBOLS)}}))
                _state.update(connected=True, error=None,
                              since=datetime.now(timezone.utc).isoformat())
                logger.info(f"[TDForexWS] subscribed {len(FX_SYMBOLS)} pairs")
                async for raw in ws:
                    if not _state["running"]:
                        break
                    msg = json.loads(raw)
                    ev = msg.get("event")
                    if ev == "price":
                        handle_price_event(msg)
                    elif ev == "subscribe-status" and msg.get("fails"):
                        # The plan's WS credits decide what sticks; a
                        # rejected pair is a fact worth surfacing, not
                        # a silent shrug.
                        _state["error"] = f"rejected: {msg['fails']}"
                        logger.warning(f"[TDForexWS] {_state['error']}")
        except Exception as e:
            _state.update(connected=False,
                          error=f"{type(e).__name__}: {str(e)[:80]}")
            logger.info(f"[TDForexWS] disconnected ({e}); "
                        f"retry in {RECONNECT_DELAY_SECONDS}s")
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.sleep(RECONNECT_DELAY_SECONDS)
    _state["connected"] = False


def start() -> dict:
    """Begin streaming in a daemon thread. Safe to call twice; inert
    without a Twelve Data key."""
    if _state["running"]:
        return {"ok": True, "already_running": True, **status()}
    import os
    if not os.getenv("TWELVE_DATA_API_KEY", "").strip():
        return {"ok": False, "reason": "TWELVE_DATA_API_KEY not set"}
    try:
        import websockets  # noqa: F401
    except ImportError:
        return {"ok": False, "reason": "websockets package not installed"}

    _state["running"] = True

    def _runner():
        try:
            asyncio.run(_consume())
        except Exception as e:
            _state.update(running=False, connected=False, error=str(e)[:100])

    threading.Thread(target=_runner, name="td-forex-ws", daemon=True).start()
    return {"ok": True, "streaming": sorted(FX_SYMBOLS.values())}


def stop() -> None:
    _state["running"] = False
