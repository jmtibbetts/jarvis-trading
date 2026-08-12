"""Live market data over Kraken's WebSocket v2.

REST answers "what was the price", polled. This answers "what is happening",
pushed — which matters for two things the desk currently guesses at:

  spread     lib/venues measures it from a REST snapshot cached 5 minutes.
             A real bid/ask arrives here on every change, so the cost model
             can price a trade against the book as it stands rather than as
             it stood.

  trade flow every signal today is derived from candles. A candle says the
             price rose; the tape says whether it rose on real buying. This
             streams individual prints with their side, which is genuinely
             new information rather than a re-derivation of OHLC.

Public channels only — no credentials. Kraken's private WS needs a token
minted from the REST key, and since this system deliberately holds only
read scopes there is nothing to subscribe to that REST cannot already read.

The connection is best-effort and self-healing: if it drops, callers fall
back to the REST path. A market-data feed must never be able to take the
desk down.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import statistics
import threading
from collections import deque
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

WS_URL = "wss://ws.kraken.com/v2"
MAX_TRADES_PER_SYMBOL = 500      # enough for flow stats, bounded memory
RECONNECT_DELAY_SECONDS = 5.0

# symbol -> latest quote / recent prints. Written by the stream thread and
# read by callers; the dict and deque operations used here are atomic under
# the GIL, so these access patterns need no lock.
_quotes: dict[str, dict] = {}
_trades: dict[str, deque] = {}
_state = {"running": False, "connected": False, "since": None, "error": None}


def latest_quote(symbol: str) -> dict | None:
    """Most recent bid/ask, or None when the stream has not seen it."""
    return _quotes.get(symbol.upper())


def live_spread_pct(symbol: str) -> tuple[float | None, str]:
    """Current spread as a fraction of the mid, straight off the book."""
    q = latest_quote(symbol)
    if not q or not q.get("bid") or not q.get("ask"):
        return None, "no live quote"
    bid, ask = float(q["bid"]), float(q["ask"])
    if bid <= 0 or ask < bid:
        return None, "crossed or empty book"
    mid = (bid + ask) / 2
    age = (datetime.now(timezone.utc) - q["at"]).total_seconds()
    return (ask - bid) / mid, f"live book, {age:.1f}s old"


def trade_flow(symbol: str, window: int = 200) -> dict | None:
    """Buy/sell pressure from the actual tape.

    Reports counts AND volume, because they answer different questions: a
    hundred small buys against three large sells is buying by count and
    selling by weight, and only the volume view sees the latter.
    """
    prints = list(_trades.get(symbol.upper()) or [])[-window:]
    if not prints:
        return None
    buys = [p for p in prints if p["side"] == "buy"]
    sells = [p for p in prints if p["side"] == "sell"]
    buy_vol = sum(p["qty"] for p in buys)
    sell_vol = sum(p["qty"] for p in sells)
    total_vol = buy_vol + sell_vol
    sizes = [p["qty"] for p in prints]
    return {
        "prints": len(prints),
        "buy_count": len(buys),
        "sell_count": len(sells),
        "buy_volume": round(buy_vol, 8),
        "sell_volume": round(sell_vol, 8),
        # +1.0 = all buying, -1.0 = all selling, 0 = balanced
        "flow_imbalance": round((buy_vol - sell_vol) / total_vol, 4) if total_vol else 0.0,
        "median_size": round(statistics.median(sizes), 8) if sizes else 0.0,
        "largest": round(max(sizes), 8) if sizes else 0.0,
        "note": "live tape; volume imbalance is the weightier read, not count",
    }


def status() -> dict:
    return {**_state, "symbols": sorted(_quotes),
            "tracked_trades": {k: len(v) for k, v in _trades.items()}}


async def _consume(symbols: list[str]) -> None:
    import websockets
    while _state["running"]:
        try:
            async with websockets.connect(WS_URL, open_timeout=20, ping_interval=20) as ws:
                await ws.send(json.dumps({"method": "subscribe", "params": {
                    "channel": "ticker", "symbol": symbols}}))
                await ws.send(json.dumps({"method": "subscribe", "params": {
                    "channel": "trade", "symbol": symbols}}))
                _state.update(connected=True,
                              since=datetime.now(timezone.utc).isoformat(), error=None)
                logger.info(f"[KrakenWS] streaming {len(symbols)} symbols")

                async for raw in ws:
                    if not _state["running"]:
                        break
                    msg = json.loads(raw)
                    channel = msg.get("channel")
                    if channel == "ticker":
                        for d in msg.get("data") or []:
                            sym = str(d.get("symbol", "")).upper()
                            _quotes[sym] = {
                                "bid": d.get("bid"), "ask": d.get("ask"),
                                "last": d.get("last"), "at": datetime.now(timezone.utc),
                            }
                    elif channel == "trade":
                        for d in msg.get("data") or []:
                            sym = str(d.get("symbol", "")).upper()
                            book = _trades.setdefault(sym, deque(maxlen=MAX_TRADES_PER_SYMBOL))
                            book.append({
                                "price": float(d.get("price") or 0),
                                "qty": float(d.get("qty") or 0),
                                "side": str(d.get("side") or "").lower(),
                                "at": datetime.now(timezone.utc),
                            })
        except Exception as e:
            _state.update(connected=False, error=f"{type(e).__name__}: {str(e)[:80]}")
            logger.info(f"[KrakenWS] disconnected ({e}); retry in {RECONNECT_DELAY_SECONDS}s")
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.sleep(RECONNECT_DELAY_SECONDS)
    _state["connected"] = False


def start(symbols: list[str]) -> dict:
    """Begin streaming in a daemon thread. Safe to call twice."""
    if _state["running"]:
        return {"ok": True, "already_running": True, **status()}
    try:
        import websockets  # noqa: F401
    except ImportError:
        return {"ok": False, "reason": "websockets package not installed"}

    syms = [s.upper() for s in symbols if s]
    _state["running"] = True

    def _runner():
        try:
            asyncio.run(_consume(syms))
        except Exception as e:
            _state.update(running=False, connected=False, error=str(e)[:100])

    threading.Thread(target=_runner, name="kraken-ws", daemon=True).start()
    return {"ok": True, "streaming": syms}


def stop() -> None:
    _state["running"] = False
