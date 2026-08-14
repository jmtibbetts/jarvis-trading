"""Canonical market events — one shape per fact, three clocks on every one.

The platform doc's §6/§8/§10, built before any storage migration because
these properties are cheap now and impossible to retrofit:

**Three clocks.** An event carries the time the venue says it happened
(exchange_ts), the time it arrived here (ingest_ts), and the time it was
processed (process_ts). Replay needs the first; staleness detection needs
the second; pipeline-lag measurement needs all three. A single "timestamp"
field silently collapses these and the ambiguity is unrecoverable — nobody
can later tell whether a 3-second gap was network lag or a slow consumer.

**Provenance on every observation.** source, source_schema_version and
ingest_version travel WITH the event. When a venue changes its message
format (they do, without notice), the rows written before and after the
change are distinguishable, and a model trained across the boundary can be
audited instead of quietly poisoned.

**Backpressure that counts.** BoundedEventQueue drops the OLDEST event
when full — for market data the newest fact is the valuable one — and
counts every drop per source. A pipeline that silently drops data reports
completeness it doesn't have; the drop counter is the difference between
"we kept up" and "we think we kept up".
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import asdict, dataclass, field

# Bumped whenever normalization logic changes meaning — same rule as the
# feature schema: rows written under different ingest logic must be
# distinguishable forever.
INGEST_VERSION = "ingest_v1_2026-08-14"


@dataclass(frozen=True)
class EventMeta:
    """The clocks and provenance every canonical event carries."""
    source: str                  # venue/provider, e.g. "binance", "coinbase"
    source_schema_version: str   # the venue's message format as we understand it
    exchange_ts: float | None    # venue's own clock (None: venue sent none)
    ingest_ts: float             # our clock at arrival
    process_ts: float            # our clock when normalized
    ingest_version: str = INGEST_VERSION

    @property
    def clock_skew_ms(self) -> float | None:
        """ingest minus exchange — network + venue clock error combined.
        None when the venue supplied no timestamp; 0.0 must never stand in
        for "unknown" (a zero would read as a perfectly synced clock)."""
        if self.exchange_ts is None:
            return None
        return round((self.ingest_ts - self.exchange_ts) * 1000.0, 3)


def make_meta(source: str, source_schema_version: str,
              exchange_ts: float | None) -> EventMeta:
    now = time.time()
    return EventMeta(source=source,
                     source_schema_version=source_schema_version,
                     exchange_ts=exchange_ts, ingest_ts=now, process_ts=now)


@dataclass(frozen=True)
class TradeEvent:
    """One print: a trade that actually happened at a venue."""
    meta: EventMeta
    symbol: str
    price: float
    size: float
    side: str | None = None      # aggressor side when the venue reports it
    kind: str = "trade"


@dataclass(frozen=True)
class QuoteEvent:
    """Top of book at an instant."""
    meta: EventMeta
    symbol: str
    bid: float
    ask: float
    bid_size: float | None = None
    ask_size: float | None = None
    kind: str = "quote"


@dataclass(frozen=True)
class BookSnapshotEvent:
    """Top-N depth snapshot, health-stamped at capture.

    Depth is stored as [[price, size], ...] exactly as displayed. `health`
    is the BookHealth verdict AT CAPTURE TIME — a consumer reading this row
    a year later must be able to see that the book was crossed or stale
    when the numbers were taken, not just the numbers.
    """
    meta: EventMeta
    symbol: str
    bids: tuple
    asks: tuple
    health_valid: bool
    health_reason: str | None = None
    kind: str = "book_snapshot"


@dataclass(frozen=True)
class DerivativesObservation:
    """Funding / OI / liquidation / long-short — one measured value with
    its own observation time, which is NOT the time we fetched it."""
    meta: EventMeta
    symbol: str
    metric: str                  # funding_rate | open_interest | ...
    value: float
    kind: str = "derivatives"


@dataclass(frozen=True)
class OnChainEvent:
    """A chain-level observation (flow, balance change, contract event)."""
    meta: EventMeta
    symbol: str
    metric: str
    value: float
    chain: str | None = None
    kind: str = "onchain"


def event_to_dict(ev) -> dict:
    """Flat dict for storage: meta fields promoted to the top level so the
    store can index them without JSON-path gymnastics."""
    d = asdict(ev)
    meta = d.pop("meta")
    meta["clock_skew_ms"] = ev.meta.clock_skew_ms
    return {**meta, **d}


class BoundedEventQueue:
    """Drop-oldest bounded buffer with per-source drop accounting.

    Synchronous and lock-free by design: producers are asyncio callbacks
    and scheduler threads that must NEVER block on a slow consumer —
    blocking a WebSocket reader stalls the reconnect logic behind it.
    deque(maxlen=...) gives atomic drop-oldest under the GIL; drops are
    detected by size bookkeeping and counted, never silent.
    """

    def __init__(self, maxsize: int = 10_000, name: str = "events"):
        self.name = name
        self.maxsize = int(maxsize)
        self._q: deque = deque(maxlen=self.maxsize)
        self.pushed = 0
        self.dropped: dict[str, int] = {}

    def push(self, event) -> bool:
        """True if stored without evicting; False when the oldest was
        dropped to make room (counted against the INCOMING event's source —
        the source producing faster than the drain is the one to see)."""
        was_full = len(self._q) >= self.maxsize
        self._q.append(event)
        self.pushed += 1
        if was_full:
            src = getattr(getattr(event, "meta", None), "source", "?")
            self.dropped[src] = self.dropped.get(src, 0) + 1
            return False
        return True

    def drain(self, limit: int = 1000) -> list:
        out = []
        while self._q and len(out) < limit:
            out.append(self._q.popleft())
        return out

    def stats(self) -> dict:
        return {"name": self.name, "size": len(self._q),
                "maxsize": self.maxsize, "pushed": self.pushed,
                "dropped": dict(self.dropped),
                "dropped_total": sum(self.dropped.values())}


# One registry so the Ops surface can enumerate every queue in the process
# without each module exporting its own accessor.
_queues: dict[str, BoundedEventQueue] = {}


def get_queue(name: str, maxsize: int = 10_000) -> BoundedEventQueue:
    q = _queues.get(name)
    if q is None:
        q = _queues[name] = BoundedEventQueue(maxsize=maxsize, name=name)
    return q


def all_queue_stats() -> list[dict]:
    return [q.stats() for q in _queues.values()]
