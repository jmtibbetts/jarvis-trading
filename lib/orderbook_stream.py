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


BOOK_STALE_SECONDS = 10.0


@dataclass(frozen=True)
class BookHealth:
    """The §9 contract: derived numbers carry the state of the book they
    came from. `valid=False` means MANDATORY ABSTENTION — imbalance and
    spread from a crossed, one-sided or stale book are not noisy, they are
    fabricated, and every consumer must see None rather than a number."""
    valid: bool
    reason: str            # ok | crossed | empty_side | stale | never_updated
    age_seconds: float


@dataclass
class OrderBook:
    """Bids/asks as {price: size} — a plain dict is fine at the depth these
    feeds actually need (top-N for display), even though Coinbase's full
    snapshot has tens of thousands of levels; only sorting on read is
    needed, no ongoing sorted-structure maintenance."""
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    updated_at: float = 0.0

    def health(self) -> BookHealth:
        if not self.updated_at:
            return BookHealth(False, "never_updated", float("inf"))
        age = time.time() - self.updated_at
        if age > BOOK_STALE_SECONDS:
            return BookHealth(False, "stale", age)
        if not self.bids or not self.asks:
            return BookHealth(False, "empty_side", age)
        if max(self.bids) >= min(self.asks):
            # A crossed book is a venue-side glitch or a broken diff
            # stream — either way the depth numbers are fiction.
            return BookHealth(False, "crossed", age)
        return BookHealth(True, "ok", age)

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
        h = self.health()
        if h.valid:
            stats = self.compute_stats(top_bids, top_asks)
        else:
            # Abstention is the contract, not an optimization: every derived
            # number from an invalid book is None, and the reason rides
            # along so the UI can say WHY there is no imbalance right now.
            stats = {k: None for k in ("best_bid", "best_ask", "spread",
                                       "spread_bps", "bid_depth",
                                       "ask_depth", "imbalance")}
        return {
            "bids": [[p, q] for p, q in top_bids],
            "asks": [[p, q] for p, q in top_asks],
            **stats,
            "health": {"valid": h.valid, "reason": h.reason,
                       "age_seconds": round(min(h.age_seconds, 9e9), 3)},
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


# ── Tier-1 persistence (Phase 3) ─────────────────────────────────────────────
# Display refreshes at 0.5s; the raw-event log takes one health-stamped
# snapshot per TIER_1_SNAPSHOT_INTERVAL_SEC per stream, pushed through a
# bounded queue so the WebSocket reader can never block on SQLite. The
# flusher drains to the event store off the hot path.
_persist_marks: dict[str, float] = {}
EVENT_FLUSH_INTERVAL_SECONDS = 10.0


def _maybe_persist_snapshot(exchange: str, display_symbol: str,
                            snapshot: dict, exchange_ts: float | None):
    try:
        from lib.event_store import TIER_1_SNAPSHOT_INTERVAL_SEC, tier_of
        from lib.market_events import BookSnapshotEvent, get_queue, make_meta

        if tier_of(display_symbol) != 1:
            return
        key = f"{exchange}:{display_symbol}"
        now = time.time()
        if now - _persist_marks.get(key, 0.0) < TIER_1_SNAPSHOT_INTERVAL_SEC:
            return
        _persist_marks[key] = now
        h = snapshot.get("health") or {}
        ev = BookSnapshotEvent(
            meta=make_meta(exchange, f"{exchange}_l2_v1", exchange_ts),
            symbol=display_symbol,
            bids=tuple(tuple(l) for l in snapshot.get("bids") or ()),
            asks=tuple(tuple(l) for l in snapshot.get("asks") or ()),
            health_valid=bool(h.get("valid")),
            health_reason=h.get("reason"),
        )
        get_queue("book_snapshots").push(ev)
    except Exception as e:
        # Persistence must never be able to take down the display stream.
        logger.debug(f"[OrderBook] snapshot persist skipped: {e}")


async def _flush_events_loop():
    """Drains EVERY registered event queue into the store — book snapshots
    here, the Kraken adapter's trades and quotes, whatever registers next.
    SQLite writes run in a worker thread — this coroutine shares the
    request-serving loop."""
    from lib.event_store import get_store
    from lib.market_events import drain_all, event_to_dict

    while True:
        await asyncio.sleep(EVENT_FLUSH_INTERVAL_SECONDS)
        batch = drain_all(limit_per_queue=2000)
        if not batch:
            continue
        try:
            rows = [event_to_dict(e) for e in batch]
            await asyncio.to_thread(get_store().append, rows)
        except Exception as e:
            logger.warning(f"[OrderBook] event flush failed ({len(batch)} events): {e}")


def _parse_iso_ts(s) -> float | None:
    """Coinbase l2update carries an ISO8601 event time; snapshots don't."""
    from lib.market_events import parse_iso_ts
    return parse_iso_ts(s)


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
                    # depth20 partial-book messages carry no venue event
                    # time; None is the honest exchange_ts (skew unknown).
                    _maybe_persist_snapshot("binance", display_symbol, snapshot, None)
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
                    _maybe_persist_snapshot("coinbase", display_symbol, snapshot,
                                            _parse_iso_ts(data.get("time")))
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
    # One flusher for all streams: drains Tier-1 snapshot events to the
    # raw-event store off the hot path (Phase 3).
    tasks.append(asyncio.create_task(_flush_events_loop()))
    return tasks
