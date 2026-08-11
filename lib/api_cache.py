"""Persistent serve-stale-while-revalidate cache for panel routes that
depend on slow external APIs (FX rates, CoinGecko, web search).

Problem it solves: after a server restart every in-memory cache is empty, so
the first hit on each panel paid the full upstream cost inline (measured:
13s for /fx/rates cold) — the UI felt broken. Now the last-known payload is
persisted in SQLite and served IMMEDIATELY (flagged stale), while a single
background thread refreshes it. Pages are instant from the first request
after boot; data catches up seconds later and the next poll picks it up.

Usage:
    payload, is_stale = serve_with_refresh("fx_rates", ttl_seconds=900, fetch_fn=build)
`fetch_fn` must return a JSON-serializable dict, or raise/return None on
failure (the stale payload keeps being served — never a blank panel).
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_refreshing: set[str] = set()
_refresh_lock = threading.Lock()


def _get_row(key: str):
    from app.database import get_db, ApiCacheEntry
    with get_db() as db:
        row = db.query(ApiCacheEntry).filter(ApiCacheEntry.key == key).first()
        if not row:
            return None, None
        try:
            return json.loads(row.payload), datetime.fromisoformat(row.fetched_at)
        except Exception:
            return None, None


def put_cached(key: str, payload: dict) -> None:
    from app.database import get_db, ApiCacheEntry
    with get_db() as db:
        db.merge(ApiCacheEntry(
            key=key,
            payload=json.dumps(payload, default=str),
            fetched_at=datetime.now(timezone.utc).isoformat(),
        ))


def _refresh_in_background(key: str, fetch_fn) -> None:
    def worker():
        try:
            fresh = fetch_fn()
            if fresh:
                put_cached(key, fresh)
        except Exception as e:
            logger.info(f"[ApiCache] Background refresh failed for {key}: {e}")
        finally:
            with _refresh_lock:
                _refreshing.discard(key)

    with _refresh_lock:
        if key in _refreshing:
            return  # single-flight — one refresh per key at a time
        _refreshing.add(key)
    threading.Thread(target=worker, daemon=True, name=f"apicache-{key}").start()


def serve_with_refresh(key: str, ttl_seconds: float, fetch_fn) -> tuple[dict | None, bool]:
    """Returns (payload, is_stale).

    - Fresh persisted payload (within ttl): served as-is, no upstream calls.
    - Stale persisted payload: served immediately (is_stale=True) and a
      background refresh starts — the caller never blocks on upstream.
    - Nothing persisted yet (true first run): fetch inline once.
    """
    payload, fetched_at = _get_row(key)
    now = datetime.now(timezone.utc)
    if payload is not None and fetched_at is not None:
        if (now - fetched_at).total_seconds() < ttl_seconds:
            return payload, False
        _refresh_in_background(key, fetch_fn)
        return payload, True
    try:
        fresh = fetch_fn()
    except Exception as e:
        logger.info(f"[ApiCache] Inline fetch failed for {key}: {e}")
        fresh = None
    if fresh:
        put_cached(key, fresh)
    return fresh, False
