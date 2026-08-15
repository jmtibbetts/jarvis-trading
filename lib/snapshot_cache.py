"""Serve the last good answer instantly; recompute behind it.

Measured 2026-08-15 on the running desk, via 127.0.0.1 so the numbers are
the server's and not a resolver's:

    /api/brief                    172.40s   (7,757 bytes)
    /api/performance/analytics     76.94s   (530 bytes)
    /api/intelligence/status       18.01s   (507 bytes)
    /api/positions/with-signals     8.12s

Those are not big payloads. They are expensive DERIVATIONS — bar walks,
replays, provider round-trips — recomputed from scratch on every request,
by every panel, for every operator refresh, and again from zero after a
restart. The Morning Brief sitting on "assembling…" for three minutes is
that, and it is why the desk feels dead after a bluescreen.

The rule this module enforces (§140.3): **stale with a timestamp beats
empty.** A panel showing a reading from four minutes ago, labelled as
four minutes old, is useful. The same panel blocking for three minutes,
or showing nothing, is not — and the operator cannot tell the blocking
case from a broken one, which is the §4 failure this whole audit is about.

Deliberately NOT a generic memoizer:

- The stored snapshot outlives the process. A restart must not cost three
  minutes of blank screens, which an in-memory cache cannot help with.
- Every served payload carries its own age. A cache that hides staleness
  would be trading one silent failure for another.
- A recompute that FAILS never destroys the last good value. The panel
  keeps showing the older reading and the failure is recorded on it —
  same contract as FeedTracker on the frontend.
"""
from __future__ import annotations

import json
import logging
import threading
import time

logger = logging.getLogger(__name__)

# One recompute per key at a time. Without this, four panels asking for a
# cold brief would each start their own 172-second derivation.
_INFLIGHT: dict[str, threading.Thread] = {}
_LOCK = threading.Lock()


def _now() -> float:
    return time.time()


def _read(key: str) -> dict | None:
    from app.database import SnapshotCache, get_db
    try:
        with get_db() as db:
            row = db.query(SnapshotCache).filter(SnapshotCache.key == key).first()
            if row is None or not row.payload:
                return None
            return {
                "payload": json.loads(row.payload),
                "computed_at": float(row.computed_at or 0),
                "compute_ms": int(row.compute_ms or 0),
                "last_error": row.last_error,
            }
    except Exception as e:
        logger.debug(f"[SnapshotCache] read {key} failed: {e}")
        return None


def _write(key: str, payload, compute_ms: int, error: str | None = None) -> None:
    from app.database import SnapshotCache, get_db, new_id
    try:
        with get_db() as db:
            row = db.query(SnapshotCache).filter(SnapshotCache.key == key).first()
            if row is None:
                row = SnapshotCache(id=new_id(), key=key)
                db.add(row)
            if payload is not None:
                row.payload = json.dumps(payload, default=str)
                row.computed_at = _now()
                row.compute_ms = compute_ms
            # An error is recorded WITHOUT clearing the payload — the last
            # good reading is the most useful thing we still have.
            row.last_error = error
    except Exception as e:
        logger.warning(f"[SnapshotCache] write {key} failed: {e}")


def _recompute(key: str, compute) -> None:
    started = _now()
    try:
        value = compute()
        _write(key, value, int((_now() - started) * 1000), None)
        logger.info(f"[SnapshotCache] {key} refreshed in "
                    f"{(_now() - started):.1f}s")
    except Exception as e:
        _write(key, None, 0, f"{type(e).__name__}: {str(e)[:160]}")
        logger.warning(f"[SnapshotCache] {key} refresh failed: {e}")
    finally:
        with _LOCK:
            _INFLIGHT.pop(key, None)


def _kick(key: str, compute) -> bool:
    """Start a background refresh unless one is already running."""
    with _LOCK:
        if key in _INFLIGHT and _INFLIGHT[key].is_alive():
            return False
        t = threading.Thread(target=_recompute, args=(key, compute),
                             name=f"snap:{key}", daemon=True)
        _INFLIGHT[key] = t
    t.start()
    return True


def cached(key: str, ttl_s: float, compute, *, block_if_cold: bool = True):
    """Return `compute()`'s result, served from the last snapshot when possible.

    - Fresh snapshot  -> returned immediately, no work.
    - Stale snapshot  -> returned immediately, refresh starts behind it.
    - No snapshot     -> computed inline (once), unless `block_if_cold` is
                         False, in which case the caller gets None and a
                         refresh starts. Use that for panels that would
                         rather render "building…" than hang.

    The result carries a `_snapshot` block: when it was computed, how old
    that is, whether a refresh is running, and the last refresh error if
    there was one. Panels render the age from it — a number nobody can see
    the age of is a number nobody should trust.
    """
    snap = _read(key)
    now = _now()

    if snap is None:
        if not block_if_cold:
            _kick(key, compute)
            return None
        started = now
        value = compute()
        _write(key, value, int((_now() - started) * 1000), None)
        snap = {"payload": value, "computed_at": _now(),
                "compute_ms": int((_now() - started) * 1000), "last_error": None}

    age = max(0.0, now - snap["computed_at"])
    stale = age > ttl_s
    refreshing = False
    if stale:
        refreshing = _kick(key, compute)

    payload = snap["payload"]
    if isinstance(payload, dict):
        payload = {**payload, "_snapshot": {
            "computed_at": snap["computed_at"],
            "age_seconds": round(age, 1),
            "ttl_seconds": ttl_s,
            "stale": stale,
            "refreshing": refreshing,
            "compute_ms": snap["compute_ms"],
            "last_error": snap["last_error"],
        }}
    return payload


def invalidate(key: str) -> None:
    _write(key, None, 0, None)
    from app.database import SnapshotCache, get_db
    try:
        with get_db() as db:
            db.query(SnapshotCache).filter(SnapshotCache.key == key).delete()
    except Exception as e:
        logger.debug(f"[SnapshotCache] invalidate {key} failed: {e}")


def status() -> list[dict]:
    """Every snapshot with its age — for the Ops panel."""
    from app.database import SnapshotCache, get_db
    out = []
    try:
        with get_db() as db:
            for row in db.query(SnapshotCache).all():
                out.append({
                    "key": row.key,
                    "age_seconds": round(max(0.0, _now() - float(row.computed_at or 0)), 1),
                    "compute_ms": int(row.compute_ms or 0),
                    "bytes": len(row.payload or ""),
                    "last_error": row.last_error,
                })
    except Exception as e:
        logger.debug(f"[SnapshotCache] status failed: {e}")
    return sorted(out, key=lambda r: -r["compute_ms"])
