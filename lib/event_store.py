"""Raw event storage behind an interface — SQLite first, measured before
anything bigger.

The platform doc's own rule (§46): DO NOT migrate storage first. Instrument
actual bytes/day on one Tier-1 symbol and make the ClickHouse decision on
that measurement, not on an architecture diagram. This module is both the
storage and the instrument.

Events land in their OWN database file (events.db beside the operator DB) —
raw market data at stream rates must never be able to bloat or lock the
trading ledger. The EventStore interface is the seam: a ClickHouse
implementation slots in behind it if — and only if — the measured volume
says SQLite can't carry it.

Watchlist tiers (§47) gate what gets persisted at all:
  Tier 1  full depth snapshots + trades     (the few symbols that earn it)
  Tier 2  quotes/trades, no depth
  Tier 3  bars only — pays NOTHING here
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time

logger = logging.getLogger(__name__)

# ── Watchlist tiers (§47) ────────────────────────────────────────────────────
TIER_1 = {"BTC", "ETH"}          # standing L2 streams already exist for these
TIER_2: set[str] = set()
TIER_1_SNAPSHOT_INTERVAL_SEC = 5.0   # persist cadence; display stays at 0.5s


def tier_of(symbol: str) -> int:
    s = str(symbol or "").upper().split("/")[0]
    if s in TIER_1:
        return 1
    if s in TIER_2:
        return 2
    return 3


def _db_path() -> str:
    if os.environ.get("JARVIS_EVENTS_DB_PATH"):
        return os.environ["JARVIS_EVENTS_DB_PATH"]
    base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(base), "data", "events.db")


class EventStore:
    """The interface. Implementations must be safe to call from scheduler
    threads and asyncio callbacks alike."""

    def append(self, events: list[dict]) -> int:  # pragma: no cover - interface
        raise NotImplementedError

    def read(self, symbol: str, kind: str, since_ts: float,
             limit: int = 1000) -> list[dict]:  # pragma: no cover - interface
        raise NotImplementedError

    def bytes_by_day(self) -> list[dict]:  # pragma: no cover - interface
        raise NotImplementedError


class SQLiteEventStore(EventStore):
    """Append-only event log with per-row byte accounting.

    `payload_bytes` is measured at write time on the serialized payload —
    the number the §46 migration decision needs, kept as a column so
    "bytes per day per kind per symbol" is one GROUP BY, not a table scan
    with length() over months of JSON.
    """

    def __init__(self, path: str | None = None):
        self.path = path or _db_path()
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._lock = threading.Lock()
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind          TEXT NOT NULL,
                    symbol        TEXT NOT NULL,
                    source        TEXT,
                    exchange_ts   REAL,
                    ingest_ts     REAL NOT NULL,
                    process_ts    REAL,
                    clock_skew_ms REAL,
                    source_schema_version TEXT,
                    ingest_version        TEXT,
                    payload       TEXT NOT NULL,
                    payload_bytes INTEGER NOT NULL
                )""")
            c.execute("""CREATE INDEX IF NOT EXISTS ix_events_lookup
                         ON events (symbol, kind, ingest_ts)""")

    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def append(self, events: list[dict]) -> int:
        if not events:
            return 0
        rows = []
        for e in events:
            payload = json.dumps(e, sort_keys=True, default=str)
            rows.append((
                e.get("kind", "?"), e.get("symbol", "?"), e.get("source"),
                e.get("exchange_ts"), e.get("ingest_ts") or time.time(),
                e.get("process_ts"), e.get("clock_skew_ms"),
                e.get("source_schema_version"), e.get("ingest_version"),
                payload, len(payload.encode("utf-8")),
            ))
        with self._lock, self._conn() as c:
            c.executemany("""INSERT INTO events
                (kind, symbol, source, exchange_ts, ingest_ts, process_ts,
                 clock_skew_ms, source_schema_version, ingest_version,
                 payload, payload_bytes)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""", rows)
        return len(rows)

    def read(self, symbol: str, kind: str, since_ts: float,
             limit: int = 1000) -> list[dict]:
        with self._conn() as c:
            cur = c.execute("""SELECT payload FROM events
                WHERE symbol = ? AND kind = ? AND ingest_ts >= ?
                ORDER BY ingest_ts ASC LIMIT ?""",
                (symbol, kind, since_ts, limit))
            return [json.loads(p) for (p,) in cur.fetchall()]

    def bytes_by_day(self) -> list[dict]:
        """THE §46 measurement: actual stored bytes per day per symbol per
        kind. The storage-migration decision reads this table, nothing else."""
        with self._conn() as c:
            cur = c.execute("""
                SELECT date(ingest_ts, 'unixepoch') AS day, symbol, kind,
                       COUNT(*), SUM(payload_bytes)
                FROM events GROUP BY day, symbol, kind
                ORDER BY day DESC, SUM(payload_bytes) DESC""")
            return [{"day": d, "symbol": s, "kind": k,
                     "events": n, "bytes": b}
                    for d, s, k, n, b in cur.fetchall()]

    def summary(self) -> dict:
        with self._conn() as c:
            total, tbytes = c.execute(
                "SELECT COUNT(*), COALESCE(SUM(payload_bytes),0) FROM events"
            ).fetchone()
        try:
            file_bytes = os.path.getsize(self.path)
        except OSError:
            file_bytes = None
        return {"path": self.path, "events": total,
                "payload_bytes": tbytes, "file_bytes": file_bytes}


_store: SQLiteEventStore | None = None
_store_lock = threading.Lock()


def get_store() -> SQLiteEventStore:
    global _store
    with _store_lock:
        if _store is None or _store.path != _db_path():
            _store = SQLiteEventStore()
        return _store
