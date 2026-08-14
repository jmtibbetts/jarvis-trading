"""Feed parity — the instrument that gates any adapter swap (Phase 5).

The plan's rule is "per-venue promotion on measured parity only": no feed
implementation (cryptofeed included, when its Windows build unblocks)
replaces a running adapter until its numbers demonstrably match. This
module IS the measurement. It compares independent observations of the
same instrument as time-aligned mid-price series and reports agreement
in basis points, pair by pair.

Two distinct uses, one engine:

  cross-venue   Kraken vs Binance.US vs Coinbase — running continuously
                from the store. Disagreement here includes REAL cross-
                venue basis (thin books genuinely trade apart), so its
                verdicts are labeled against a loose threshold and serve
                as a feed-health baseline, not an error count.
  same-venue    a candidate implementation (e.g. cryptofeed:coinbase) vs
                the incumbent adapter for the SAME venue. Real basis is
                zero by construction; the tight threshold applies, and
                THIS is the comparison that promotes or refuses a swap.

Everything reads from the raw event store — the parity of what was
actually RECORDED, not of what the adapters claim they saw.
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

BUCKET_SECONDS = 5.0            # matches the Tier-1 persist cadence
CROSS_VENUE_BPS = 20.0          # loose: includes real inter-venue basis
SAME_VENUE_BPS = 2.0            # tight: same venue must agree with itself
MIN_COVERAGE = 0.5              # both series present in >=50% of buckets


def _mid_series_from_store(symbol_base: str, window_min: int,
                           now: float | None = None) -> dict[str, list]:
    """Per-venue (bucket, mid) series for one asset, from stored events."""
    from lib.event_store import get_store

    now = now or time.time()
    since = now - window_min * 60
    store = get_store()
    series: dict[str, list] = {}

    for row in store.read(f"{symbol_base}/USD", "quote", since_ts=since,
                          limit=5000):
        bid, ask = row.get("bid"), row.get("ask")
        if bid and ask and float(bid) > 0 and float(ask) >= float(bid):
            series.setdefault(row.get("source", "?"), []).append(
                (row["ingest_ts"], (float(bid) + float(ask)) / 2))

    for row in store.read(symbol_base, "book_snapshot", since_ts=since,
                          limit=5000):
        if not row.get("health_valid"):
            continue        # a corrupt book must not vote on parity
        bids, asks = row.get("bids") or [], row.get("asks") or []
        if bids and asks:
            bid, ask = float(bids[0][0]), float(asks[0][0])
            if bid > 0 and ask >= bid:
                series.setdefault(row.get("source", "?"), []).append(
                    (row["ingest_ts"], (bid + ask) / 2))
    return series


def _bucketize(points: list, bucket_s: float = BUCKET_SECONDS) -> dict:
    """Last observation per bucket — one voice per venue per interval."""
    out = {}
    for ts, mid in sorted(points):
        out[int(ts // bucket_s)] = mid
    return out


def pairwise_parity(series_by_label: dict[str, list],
                    threshold_bps: float) -> list[dict]:
    """Every label pair's agreement, from raw (ts, mid) series.

    Deliberately generic over labels: "kraken" vs "coinbase" today,
    "cryptofeed:coinbase" vs "coinbase" the day a candidate runs.
    """
    buckets = {label: _bucketize(pts)
               for label, pts in series_by_label.items() if pts}
    labels = sorted(buckets)
    pairs = []
    for i, a in enumerate(labels):
        for b in labels[i + 1:]:
            shared = sorted(set(buckets[a]) & set(buckets[b]))
            union = len(set(buckets[a]) | set(buckets[b]))
            if not shared or not union:
                pairs.append({"a": a, "b": b, "verdict": "no_overlap",
                              "shared_buckets": 0})
                continue
            diffs = []
            for k in shared:
                ma, mb = buckets[a][k], buckets[b][k]
                mean = (ma + mb) / 2
                if mean > 0:
                    diffs.append(abs(ma - mb) / mean * 10_000)
            diffs.sort()
            coverage = len(shared) / union
            median = diffs[len(diffs) // 2]
            p95 = diffs[int(0.95 * (len(diffs) - 1))]
            if coverage < MIN_COVERAGE:
                verdict = "insufficient_overlap"
            elif median <= threshold_bps:
                verdict = "parity"
            else:
                verdict = "divergent"
            pairs.append({
                "a": a, "b": b, "verdict": verdict,
                "median_bps": round(median, 3), "p95_bps": round(p95, 3),
                "max_bps": round(diffs[-1], 3),
                "shared_buckets": len(shared),
                "coverage": round(coverage, 3),
                "threshold_bps": threshold_bps,
            })
    return pairs


def parity_report(symbol_base: str = "BTC", window_min: int = 60) -> dict:
    """Cross-venue parity for one asset from the store — the standing
    feed-health baseline. Same-venue candidate comparisons reuse
    pairwise_parity() directly with the tight threshold."""
    series = _mid_series_from_store(symbol_base.upper(), window_min)
    return {
        "symbol": symbol_base.upper(),
        "window_min": window_min,
        "venues": {label: len(pts) for label, pts in series.items()},
        "pairs": pairwise_parity(series, threshold_bps=CROSS_VENUE_BPS),
        "note": ("cross-venue disagreement includes real inter-venue "
                 "basis; the tight same-venue threshold "
                 f"({SAME_VENUE_BPS} bps) is what gates an adapter swap"),
    }
