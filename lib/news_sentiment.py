"""Daily net news sentiment per symbol, from articles Jarvis already stores.

Measured 2026-08-13 over 1,291 labelled outcomes: sentiment LEVEL predicts
nothing (more-positive days were marginally worse, consistent with the
news score component measuring inverted), but direction ALIGNMENT split
hard — signals trading WITH the day's news mood won 52.5% (+0.498%/trade)
while signals trading AGAINST it won 26.0% (-0.755%).

Caveat that keeps this out of the composite: the sample spans ~4 mostly
falling days, so "aligned" is heavily shorts-in-a-down-week and the split
may be one regime's artifact. It is therefore RECORDED on every candidate
(like preceding_move_pct) and judged out of sample as resolutions
accumulate — never scored by hand.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict

logger = logging.getLogger(__name__)

# One day's mood from one article is noise.
MIN_ARTICLES = 3
# |net| below this is a mixed day — no alignment claim either way.
MIXED_BAND = 0.15

_CACHE: dict = {"built_at": 0.0, "map": {}}
_TTL_S = 900.0
_LOCK = threading.Lock()


def _build_map() -> dict:
    """(BASE_SYMBOL, YYYY-MM-DD) -> net sentiment in [-1, 1]."""
    from sqlalchemy import text

    from app.database import engine

    cells: dict = defaultdict(lambda: {"pos": 0, "neg": 0, "n": 0})
    with engine.connect() as c:
        rows = c.execute(text(
            "SELECT affected_assets, sentiment, substr(published_at,1,10) "
            "FROM news_items WHERE affected_assets IS NOT NULL "
            "AND affected_assets != '' AND published_at IS NOT NULL "
            "AND published_at >= date('now', '-10 days')")).fetchall()
    for assets, s, day in rows:
        # affected_assets is a comma-separated string ('XLE,USO,GLD'),
        # verified against live rows — NOT JSON despite its plural name.
        for sym in str(assets).split(","):
            base = sym.strip().upper().split("/")[0]
            if not base or len(base) > 6:
                continue
            cell = cells[(base, day)]
            cell["n"] += 1
            if s == "positive":
                cell["pos"] += 1
            elif s == "negative":
                cell["neg"] += 1
    return {k: (v["pos"] - v["neg"]) / v["n"]
            for k, v in cells.items() if v["n"] >= MIN_ARTICLES}


def _cached_map() -> dict:
    with _LOCK:
        if time.time() - _CACHE["built_at"] > _TTL_S:
            try:
                _CACHE["map"] = _build_map()
                _CACHE["built_at"] = time.time()
            except Exception as e:
                logger.debug(f"[NewsSentiment] map build failed: {e}")
        return _CACHE["map"]


def net_sentiment(symbol: str, day: str) -> float | None:
    """Net news mood for a symbol on a day, or None when fewer than
    MIN_ARTICLES were tagged — silence, not a zero that reads as neutral."""
    base = str(symbol or "").upper().split("/")[0]
    return _cached_map().get((base, day))


def alignment(symbol: str, direction: str | None, day: str) -> str | None:
    """'with' | 'against' | 'mixed' | None — how the trade's direction sits
    relative to the day's news mood. None when there is no measured mood."""
    net = net_sentiment(symbol, day)
    if net is None:
        return None
    if abs(net) < MIXED_BAND:
        return "mixed"
    is_long = str(direction or "Long").lower().startswith("l")
    return "with" if (net > 0) == is_long else "against"
