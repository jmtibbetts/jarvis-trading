"""The incubator — coins accumulating history until the desk can judge
them.

A newly listed coin can't produce a signal: the TA engine wants ~200
bars of context, the expectancy table wants resolved outcomes, and a
scanner pointed at 3 days of history manufactures noise. But "can't
signal yet" should never mean "invisible" — the operator asked exactly
this: where are the newer coins the desk is BUILDING data on?

This module answers from the bar cache itself: any crypto symbol whose
1H history is young or thin is 'incubating', with a measured progress
fraction toward the graduation bar. Graduation is about DATA
sufficiency only — it makes a coin eligible for scanning, it does not
bless it as tradeable (the gate still decides that, on evidence).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# 1H bars before the scanners can see enough context to be honest:
# ~200 bars of TA warmup plus margin. On a 24/7 coin that is ~3 weeks
# of history — a coin younger than that produces vibes, not analysis.
GRADUATION_BARS_1H = 500
# "New" = first cached bar within this window; an old coin with a thin
# cache is a BACKFILL gap, not a new listing, and is labelled as such.
NEW_LISTING_DAYS = 60


def incubator_report(limit: int = 15) -> dict:
    from sqlalchemy import text

    from lib.ohlcv_cache import get_cache_db

    now = datetime.now(timezone.utc)
    rows = []
    with get_cache_db() as conn:
        # backfill_status already tracks earliest_ts, latest_ts and
        # bar_count per (symbol, timeframe) — the exact three values this
        # GROUP BY recomputed by scanning 38.2 million bar rows. Measured
        # 2,039 ms against 0 ms, on a query the Morning Brief runs to draw
        # the Incubator panel.
        got = conn.execute(text("""
            SELECT symbol, earliest_ts, latest_ts, bar_count
            FROM backfill_status
            WHERE timeframe = '1H' AND symbol LIKE '%/%'
        """)).fetchall()
    for sym, first, last, n in got:
        try:
            first_dt = datetime.fromisoformat(str(first)).replace(
                tzinfo=timezone.utc) if "T" in str(first) else \
                datetime.strptime(str(first)[:19], "%Y-%m-%d %H:%M:%S"
                                  ).replace(tzinfo=timezone.utc)
        except Exception:
            continue
        age_days = (now - first_dt).days
        if n >= GRADUATION_BARS_1H:
            continue
        rows.append({
            "symbol": sym,
            "bars_1h": n,
            "first_bar": str(first)[:10],
            "age_days": age_days,
            "progress_pct": round(100.0 * n / GRADUATION_BARS_1H, 1),
            # A young coin is incubating; an old coin with thin bars is
            # a coverage gap the backfill should close — different fixes.
            "kind": ("new_listing" if age_days <= NEW_LISTING_DAYS
                     else "coverage_gap"),
        })
    rows.sort(key=lambda r: r["age_days"])
    incubating = [r for r in rows if r["kind"] == "new_listing"]
    gaps = [r for r in rows if r["kind"] == "coverage_gap"]
    return {
        "graduation_bars_1h": GRADUATION_BARS_1H,
        "incubating": incubating[:limit],
        "coverage_gaps": gaps[:limit],
        "counts": {"incubating": len(incubating),
                   "coverage_gaps": len(gaps)},
        "note": ("graduation = enough DATA to scan honestly, not a "
                 "blessing to trade — the gate still decides on evidence"),
    }
