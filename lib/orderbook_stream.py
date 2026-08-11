"""
Crypto Level 2 order book — live WebSocket streams from Binance and
Coinbase, free public market data, no API key. Verified live against both
real exchange feeds while building this, including two real constraints
that don't show up until you actually connect:

  - Binance.com (wss://stream.binance.com) rejects connections from US IPs
    with HTTP 451 (a real, enforced geo-block, not a sandbox artifact) —
    defaults to Binance.US (BINANCE_WS_HOST env var to override for
    non-US deployments, since binance.com has deeper liquidity/more pairs).
  - Coinbase's "level2_batch" channel sends a FULL order book snapshot on
    subscribe (tens of thousands of price levels, 1MB+) followed by
    incremental l2update diffs — the `websockets` library's default 1MB
    message cap rejects the snapshot outright unless raised. Binance's
    depth20@100ms stream is architecturally simpler: each message IS a
    complete top-20 snapshot, no diff/state reconciliation needed.

Both stream runners are long-lived and reconnect with exponential backoff
on any failure — these connections WILL eventually drop (network blips,
exchange-side restarts) and must recover without operator intervention.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field

import websockets

logger = logging.getLogger(__name__)

BINANCE_WS_HOST = os.getenv("BINANCE_WS_HOST", "stream.binance.us:9443")
COINBASE_WS_URL = "wss://ws-feed.exchange.coinbase.com"
COINBASE_MAX_MESSAGE_SIZE = 20_000_000  # real snapshots observed ~1.2MB; generous headroom
BROADCAST_THROTTLE_SECONDS = 0.5  # cap outbound update rate regardless of exchange message rate
RECONNECT_BASE_SECONDS = 2.0
RECONNECT_MAX_SECONDS = 60.0


@dataclass
class OrderBook:
    """Bids/asks as {price: size} — a plain dict is fine at the depth these
    feeds actually need (top-N for display), even though Coinbase's full
    snapshot has tens of thousands of levels; only sorting on read is
    needed, no ongoing sorted-structure maintenance."""
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    updated_at: float = 0.0

    def apply_snapshot(self, bids: list[tuple[float, float]], asks: list[tuple[float, float]]):
        self.bids = {p: q for p, q in bids if q > 0}
        self.asks = {p: q for p, q in asks if q > 0}
        self.updated_at = time.time()

    def apply_update(self, side: str, price: float, size: float):
        book = self.bids if side == "buy" else self.asks
        if size <= 0:
            book.pop(price, None)
        else:
            book[price] = size
        self.updated_at = time.time()

    def top_levels(self, n: int = 20) -> dict:
        top_bids = sorted(self.bids.items(), key=lambda kv: -kv[0])[:n]
        top_asks = sorted(self.asks.items(), key=lambda kv: kv[0])[:n]
        return {
            "bids": [[p, q] for p, q in top_bids],
            "asks": [[p, q] for p, q in top_asks],
            **self.compute_stats(top_bids, top_asks),
        }

    @staticmethod
    def compute_stats(top_bids: list[tuple[float, float]], top_asks: list[tuple[float, float]]) -> dict:
        best_bid = top_bids[0][0] if top_bids else None
        best_ask = top_asks[0][0] if top_asks else None
        spread = round(best_ask - best_bid, 8) if best_bid is not None and best_ask is not None else None
        spread_bps = round(spread / best_bid * 10_000, 2) if spread is not None and best_bid else None
        bid_depth = sum(q for _, q in top_bids)
        ask_depth = sum(q for _, q in top_asks)
        total_depth = bid_depth + ask_depth
        imbalance = round((bid_depth - ask_depth) / total_depth, 4) if total_depth else None
        return {
            "best_bid": best_bid, "best_ask": best_ask,
            "spread": spread, "spread_bps": spread_bps,
            "bid_depth": round(bid_depth, 8), "ask_depth": round(ask_depth, 8),
            "imbalance": imbalance,  # +1 = all bid-side depth, -1 = all ask-side, 0 = balanced
        }


# exchange -> symbol -> OrderBook (Binance) or raw top-N dict (both, post-throttle)
_books: dict[str, dict[str, OrderBook]] = {"binance": {}, "coinbase": {}}
_latest_snapshot: dict[str, dict] = {}  # f"{exchange}:{display_symbol}" -> top_levels() dict


def get_latest_snapshot(exchange: str, display_symbol: str) -> dict | None:
    return _latest_snapshot.get(f"{exchange}:{display_symbol.upper()}")


async def _reconnect_loop(name: str, run_once):
    """Wraps a single-connection coroutine with exponential-backoff
    reconnect — these streams must survive indefinitely without an
    operator restarting the process every time a connection drops."""
    delay = RECONNECT_BASE_SECONDS
    while True:
        try:
            await run_once()
            delay = RECONNECT_BASE_SECONDS  # clean disconnect — reset backoff
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[OrderBook:{name}] Connection error: {type(e).__name__}: {e} — reconnecting in {delay:.0f}s")
        await asyncio.sleep(delay)
        delay = min(delay * 1.6, RECONNECT_MAX_SECONDS)


async def run_binance_stream(binance_symbol: str, display_symbol: str, on_update=None):
    """binance_symbol e.g. 'btcusdt' (lowercase, no separator)."""
    url = f"wss://{BINANCE_WS_HOST}/ws/{binance_symbol.lower()}@depth20@100ms"

    async def once():
        last_broadcast = 0.0
        async with websockets.connect(url, open_timeout=15) as ws:
            logger.info(f"[OrderBook:binance] Connected — {display_symbol}")
            async for raw in ws:
                data = json.loads(raw)
                bids = [(float(p), float(q)) for p, q in data.get("bids", [])]
                asks = [(float(p), float(q)) for p, q in data.get("asks", [])]
                book = OrderBook()
                book.apply_snapshot(bids, asks)
                now = time.time()
                if now - last_broadcast >= BROADCAST_THROTTLE_SECONDS:
                    last_broadcast = now
                    snapshot = {"exchange": "binance", "symbol": display_symbol, **book.top_levels(20), "ts": now}
                    _latest_snapshot[f"binance:{display_symbol}"] = snapshot
                    if on_update:
                        await on_update(snapshot)

    await _reconnect_loop(f"binance:{display_symbol}", once)


async def run_coinbase_stream(coinbase_product: str, display_symbol: str, on_update=None):
    """coinbase_product e.g. 'BTC-USD'."""
    async def once():
        book = OrderBook()
        last_broadcast = 0.0
        async with websockets.connect(COINBASE_WS_URL, open_timeout=15, max_size=COINBASE_MAX_MESSAGE_SIZE) as ws:
            await ws.send(json.dumps({"type": "subscribe", "product_ids": [coinbase_product], "channels": ["level2_batch"]}))
            logger.info(f"[OrderBook:coinbase] Subscribed — {display_symbol}")
            async for raw in ws:
                data = json.loads(raw)
                msg_type = data.get("type")
                if msg_type == "snapshot":
                    bids = [(float(p), float(q)) for p, q in data.get("bids", [])]
                    asks = [(float(p), float(q)) for p, q in data.get("asks", [])]
                    book.apply_snapshot(bids, asks)
                elif msg_type == "l2update":
                    for side, price, size in data.get("changes", []):
                        book.apply_update(side, float(price), float(size))
                else:
                    continue  # "subscriptions" ack, heartbeats, etc. — not book state

                now = time.time()
                if now - last_broadcast >= BROADCAST_THROTTLE_SECONDS:
                    last_broadcast = now
                    snapshot = {"exchange": "coinbase", "symbol": display_symbol, **book.top_levels(20), "ts": now}
                    _latest_snapshot[f"coinbase:{display_symbol}"] = snapshot
                    if on_update:
                        await on_update(snapshot)

    await _reconnect_loop(f"coinbase:{display_symbol}", once)


# display_symbol -> (binance_symbol, coinbase_product). Deliberately small —
# each entry is a standing WebSocket connection per exchange, kept open for
# the life of the process.
DEFAULT_WATCHLIST = {
    "BTC": ("btcusdt", "BTC-USD"),
    "ETH": ("ethusdt", "ETH-USD"),
}


def start_orderbook_streams(watchlist: dict[str, tuple[str, str]] | None = None, on_update=None) -> list[asyncio.Task]:
    """Launches one background task per exchange per symbol on the CURRENT
    running event loop — call this from FastAPI's lifespan startup (the
    same loop uvicorn serves requests on), not from an APScheduler job
    thread. Returns the tasks so the caller can cancel them on shutdown."""
    watchlist = watchlist or DEFAULT_WATCHLIST
    tasks = []
    for display_symbol, (binance_symbol, coinbase_product) in watchlist.items():
        tasks.append(asyncio.create_task(run_binance_stream(binance_symbol, display_symbol, on_update)))
        tasks.append(asyncio.create_task(run_coinbase_stream(coinbase_product, display_symbol, on_update)))
    return tasks
