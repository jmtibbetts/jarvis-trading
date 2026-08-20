"""What SQLite is actually doing, measured at the one boundary everything
passes through.

WHERE THIS LIVES AND WHY. JARVIS reaches SQLite through two SQLAlchemy
session context managers -- `app.database.get_db` for the operator store
(and, via JARVIS_DB_PATH, the forward-evidence store) and
`lib.ohlcv_cache.get_cache_db` for the bar cache. Both have the same shape:
yield, commit, rollback on error, close. That is the lowest shared reliable
boundary, so the timing and the error classification happen there once
rather than as timers scattered through hundreds of call sites.

Raw `sqlite3` users (lib/event_store, lib/signal_replay and friends) do NOT
pass through it and are therefore NOT counted here. Reporting them as zero
would be a lie of omission, so their coverage is declared explicitly in the
snapshot instead.

MEASURING MUST NOT BECOME THE PROBLEM. Everything is in-memory and bounded:
fixed-length deques, no metrics table, no row written per operation. A
metrics store that writes to SQLite in order to observe SQLite would create
the contention it claims to detect. Every entry point is wrapped so that an
instrumentation failure can never propagate into the caller -- observing the
desk must not be able to stop it.

BUSY IS NOT A SYNONYM FOR "DATABASE ERROR". Python 3.11+ exposes
`sqlite_errorname` on the underlying exception, which is authoritative;
string-matching the message is the fallback, not the plan. Disk-full, I/O
and corruption errors are classified as themselves and never folded into
lock contention, because retrying corruption is how corruption spreads.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Bounded on purpose: rolling windows, not history.
_WINDOW = 200

# Error classes. Deliberately distinct -- see the module note.
BUSY = "BUSY"
LOCKED = "LOCKED"
READ_ONLY = "READ_ONLY"
DISK_FULL = "DISK_FULL"
IO_ERROR = "IO_ERROR"
CORRUPT = "CORRUPT"
CONSTRAINT = "CONSTRAINT"
OTHER = "OTHER"

CLASSES = (BUSY, LOCKED, READ_ONLY, DISK_FULL, IO_ERROR, CORRUPT,
           CONSTRAINT, OTHER)

# sqlite_errorname -> our class. Authoritative when available.
_BY_ERRORNAME = {
    "SQLITE_BUSY": BUSY,
    "SQLITE_BUSY_SNAPSHOT": BUSY,
    "SQLITE_BUSY_TIMEOUT": BUSY,
    "SQLITE_LOCKED": LOCKED,
    "SQLITE_LOCKED_SHAREDCACHE": LOCKED,
    "SQLITE_READONLY": READ_ONLY,
    "SQLITE_FULL": DISK_FULL,
    "SQLITE_IOERR": IO_ERROR,
    "SQLITE_CORRUPT": CORRUPT,
    "SQLITE_NOTADB": CORRUPT,
    "SQLITE_CONSTRAINT": CONSTRAINT,
}

# Fallback only. Ordered: "database is locked" is SQLITE_BUSY in SQLite's
# wording, while "database table is locked" is SQLITE_LOCKED -- the reverse
# of what the words suggest, which is exactly why the errorname is preferred.
_BY_MESSAGE = (
    ("database is locked", BUSY),
    ("database schema is locked", LOCKED),
    ("database table is locked", LOCKED),
    ("attempt to write a readonly database", READ_ONLY),
    ("readonly database", READ_ONLY),
    ("database or disk is full", DISK_FULL),
    ("disk i/o error", IO_ERROR),
    ("database disk image is malformed", CORRUPT),
    ("file is not a database", CORRUPT),
    ("constraint failed", CONSTRAINT),
)

_lock = threading.Lock()


def _blank() -> dict:
    return {
        "write_tx_ms": deque(maxlen=_WINDOW),
        "read_tx_ms": deque(maxlen=_WINDOW),
        "write_count": 0,
        "read_count": 0,
        "write_failures": 0,
        "errors": defaultdict(int),
        "last_error_at": None,
        "last_error_class": None,
        "last_busy_at": None,
        "last_write_failure_at": None,
        "busy_wait_ms": deque(maxlen=_WINDOW),
    }


_stores: dict[str, dict] = defaultdict(_blank)


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def classify(exc: BaseException) -> str:
    """Which SQLite failure this is. Never guesses BUSY."""
    orig = getattr(exc, "orig", None) or exc
    name = getattr(orig, "sqlite_errorname", None)
    if name:
        # Extended codes look like SQLITE_IOERR_WRITE; the base is the class.
        if name in _BY_ERRORNAME:
            return _BY_ERRORNAME[name]
        for prefix, cls in _BY_ERRORNAME.items():
            if name.startswith(prefix + "_"):
                return cls
    text = str(orig).lower()
    for needle, cls in _BY_MESSAGE:
        if needle in text:
            return cls
    return OTHER


def record_transaction(store: str, *, duration_ms: float, wrote: bool,
                       busy_wait_ms: float | None = None) -> None:
    """One completed transaction. Never raises."""
    try:
        with _lock:
            s = _stores[store]
            if wrote:
                s["write_tx_ms"].append(duration_ms)
                s["write_count"] += 1
            else:
                s["read_tx_ms"].append(duration_ms)
                s["read_count"] += 1
            if busy_wait_ms is not None:
                s["busy_wait_ms"].append(busy_wait_ms)
    except Exception:                                        # noqa: BLE001
        pass


def record_error(store: str, exc: BaseException, *, wrote: bool = False,
                 duration_ms: float | None = None) -> str:
    """One failed transaction, classified. Never raises."""
    cls = OTHER
    try:
        cls = classify(exc)
        with _lock:
            s = _stores[store]
            s["errors"][cls] += 1
            s["last_error_at"] = _utc()
            s["last_error_class"] = cls
            if cls == BUSY:
                s["last_busy_at"] = _utc()
                if duration_ms is not None:
                    s["busy_wait_ms"].append(duration_ms)
            if wrote:
                s["write_failures"] += 1
                s["last_write_failure_at"] = _utc()
    except Exception:                                        # noqa: BLE001
        pass
    return cls


def _stat(values) -> tuple:
    if not values:
        return None, None
    return round(sum(values) / len(values), 2), round(max(values), 2)


def _wal_bytes(path: str | None):
    """Size of the -wal sidecar, or UNKNOWN if it cannot be read.

    Deliberately does NOT call `PRAGMA wal_checkpoint`: that command
    CHECKPOINTS. A status endpoint that mutates the thing it reports on is
    not observability.
    """
    if not path:
        return "UNKNOWN"
    try:
        wal = path + "-wal"
        return os.path.getsize(wal) if os.path.exists(wal) else 0
    except OSError:
        return "UNKNOWN"


def _pragmas(path: str | None) -> dict:
    """File-level SQLite settings, read through a separate read-only handle.

    ONLY THE SETTINGS THAT BELONG TO THE DATABASE. `journal_mode` and
    `wal_autocheckpoint` are properties of the file and survive across
    connections, so a probe connection reports them truthfully.

    `busy_timeout` and `foreign_keys` are PER-CONNECTION. Reading them here
    would report this probe's own defaults -- measured at 2000ms and OFF --
    while the application engine sets 30000ms and ON. That is not the
    application's configuration, it is the measuring instrument's, and
    printing it as though it were the store's would be fabrication of
    exactly the kind this module exists to avoid. They are reported
    separately by `engine_settings`, from the code that actually sets them.
    """
    out = {"journal_mode": "UNKNOWN", "wal_autocheckpoint_pages": "UNKNOWN"}
    if not path or not os.path.exists(path):
        return out
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        try:
            for key, sql in (("journal_mode", "PRAGMA journal_mode"),
                             ("wal_autocheckpoint_pages",
                              "PRAGMA wal_autocheckpoint")):
                row = conn.execute(sql).fetchone()
                out[key] = row[0] if row else "UNKNOWN"
        finally:
            conn.close()
    except sqlite3.Error:
        pass
    return out


# Per-connection pragmas, taken from the engine that actually sets them
# rather than from a probe connection. UNKNOWN where no engine declares one.
_ENGINE_SETTINGS = {
    "jarvis": {"busy_timeout_ms": 30000, "foreign_keys": "ON",
               "source": "app.database set_sqlite_pragma"},
    "forward_evidence": {"busy_timeout_ms": 30000, "foreign_keys": "ON",
                         "source": "app.database set_sqlite_pragma "
                                   "(via JARVIS_DB_PATH)"},
    # NOTE, not normalised here: this engine sets WAL and synchronous=NORMAL
    # but declares NO busy_timeout, unlike the operator engine. Changing a
    # contention setting is not an instrumentation change, so it is
    # REPORTED rather than quietly fixed.
    "ohlcv_cache": {"busy_timeout_ms": "UNKNOWN (engine sets none)",
                    "foreign_keys": "UNKNOWN (engine sets none)",
                    "source": "lib.ohlcv_cache set_pragma"},
}


def engine_settings(store: str) -> dict:
    return dict(_ENGINE_SETTINGS.get(
        store, {"busy_timeout_ms": "UNKNOWN", "foreign_keys": "UNKNOWN",
                "source": "no engine declares pragmas for this store"}))


def _one_store(name: str, path) -> dict:
    """Metrics for a single store. Raises only for its own store."""
    s = _stores.get(name) or _blank()
    avg_w, max_w = _stat(s["write_tx_ms"])
    avg_b, max_b = _stat(s["busy_wait_ms"])
    return {
        **_pragmas(path),
        "engine_settings": engine_settings(name),
        "wal_bytes": _wal_bytes(path),
        "busy_count": s["errors"].get(BUSY, 0),
        "locked_count": s["errors"].get(LOCKED, 0),
        "write_failures": s["write_failures"],
        "recent_write_count": s["write_count"],
        "recent_read_count": s["read_count"],
        "avg_write_tx_ms": avg_w if avg_w is not None else "UNKNOWN",
        "max_write_tx_ms": max_w if max_w is not None else "UNKNOWN",
        "avg_busy_wait_ms": avg_b if avg_b is not None else "UNKNOWN",
        "max_busy_wait_ms": max_b if max_b is not None else "UNKNOWN",
        "last_busy_at": s["last_busy_at"],
        "last_write_failure_at": s["last_write_failure_at"],
        "last_error_class": s["last_error_class"],
        "errors_by_class": dict(s["errors"]),
        "sample_window": _WINDOW,
    }


def snapshot(paths: dict[str, str] | None = None) -> dict:
    """Bounded status for Ops. Anything unmeasurable reports UNKNOWN.

    `paths` maps store name -> file path; a store with no known path still
    reports its counters, with the file-derived fields UNKNOWN.

    PER-STORE ISOLATION. Each store is computed in its own try block, so one
    unreadable entry cannot blank the entire report. An all-or-nothing
    snapshot turns a single bad store into total blindness, which is a worse
    failure than the store being wrong -- and it is exactly what happened
    the first time this was tested.
    """
    paths = paths or {}
    try:
        with _lock:
            names = sorted(set(_stores) | set(paths))
            stores = {}
            for name in names:
                try:
                    stores[name] = _one_store(name, paths.get(name))
                except Exception as exc:                     # noqa: BLE001
                    stores[name] = {"error": f"unavailable: {exc}"}
    except Exception as exc:                                 # noqa: BLE001
        return {"stores": {}, "error": f"snapshot unavailable: {exc}"}

    return {
        "stores": stores,
        # Stated rather than implied: these numbers describe traffic through
        # the SQLAlchemy session boundary only.
        "coverage": "SQLAlchemy session boundary (app.database.get_db, "
                    "lib.ohlcv_cache.get_cache_db). Raw sqlite3 callers are "
                    "NOT counted and are not reported as zero.",
        # Checkpoint age needs PRAGMA wal_checkpoint, which performs a
        # checkpoint. Not measured rather than measured destructively.
        "checkpoint_age": "UNKNOWN (not readable without checkpointing)",
    }


def reset(store: str | None = None) -> None:
    """Test helper. Clears counters without touching any database."""
    with _lock:
        if store is None:
            _stores.clear()
        else:
            _stores.pop(store, None)


class observe:
    """Time one session block and classify whatever it raises.

    Used by the session context managers. A failure inside this class must
    never reach the caller, so every hook is defensive.
    """

    def __init__(self, store: str):
        self.store = store
        self.started = None
        self.wrote = False

    def __enter__(self):
        try:
            self.started = time.perf_counter()
        except Exception:                                    # noqa: BLE001
            self.started = None
        return self

    def finish(self, exc: BaseException | None, wrote: bool) -> None:
        try:
            if self.started is None:
                return
            ms = (time.perf_counter() - self.started) * 1000.0
            if exc is not None:
                record_error(self.store, exc, wrote=wrote, duration_ms=ms)
            else:
                record_transaction(self.store, duration_ms=ms, wrote=wrote)
        except Exception:                                    # noqa: BLE001
            pass
