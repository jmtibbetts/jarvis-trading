"""Cryptofeed prototype probe — the Phase 5 promotion measurement.

Runs cryptofeed's normalizer for ONE venue for a bounded window, collects
its top-of-book mids, and compares them against the incumbent adapter's
stored series for the same venue and window through the parity gate at
the TIGHT same-venue threshold. Same venue, same instrument, same
moments: real basis is zero by construction, so surviving the gate means
the implementations agree about reality — the plan's precondition for
any per-venue swap.

This is a probe, not a wiring: nothing here runs on a schedule, feeds
the store, or touches execution. It exists to be invoked, produce a
measured verdict, and be quoted in the promotion decision.

Windows note: cryptofeed's order_book C extension does not compile under
MSVC upstream (GCC-only architecture gates and __builtin_expect). The
venv carries order_book 1.0.1 with two local patches — a portable CRC32
fallback branch and an EXPECT() no-op shim — recorded in
vendor/patches/order_book-1.0.1-msvc.patch for reproducible reinstalls.
"""
from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

# cryptofeed exchange id -> (our store's source label, cryptofeed symbol)
PROBE_VENUES = {
    "coinbase": ("COINBASE", "BTC-USD"),
    "kraken": ("KRAKEN", "BTC-USD"),
}


def run_probe(venue: str = "coinbase", seconds: int = 90,
              symbol_base: str = "BTC") -> dict:
    """Collect cryptofeed mids for `seconds`, then judge against the
    incumbent's stored series for the same window. Returns the parity
    verdict plus enough context to be quoted."""
    from cryptofeed import FeedHandler
    from cryptofeed.defines import L2_BOOK

    from lib.feed_parity import SAME_VENUE_BPS, pairwise_parity

    if venue not in PROBE_VENUES:
        raise KeyError(f"unknown probe venue {venue!r} — "
                       f"available: {sorted(PROBE_VENUES)}")
    exchange_id, cf_symbol = PROBE_VENUES[venue]

    collected: list[tuple[float, float]] = []

    async def _on_book(book, receipt_ts):
        try:
            bids, asks = book.book.bids, book.book.asks
            if len(bids) and len(asks):
                bid, ask = float(bids.index(0)[0]), float(asks.index(0)[0])
                if bid > 0 and ask >= bid:
                    collected.append((receipt_ts, (bid + ask) / 2))
        except Exception:
            pass

    from cryptofeed.exchanges import EXCHANGE_MAP
    fh = FeedHandler(config={"uvloop": False, "log": {"disabled": True}})
    fh.add_feed(EXCHANGE_MAP[exchange_id](
        symbols=[cf_symbol], channels=[L2_BOOK],
        callbacks={L2_BOOK: _on_book}))

    start = time.time()
    runner = threading.Thread(
        target=lambda: fh.run(install_signal_handlers=False), daemon=True)
    runner.start()
    time.sleep(seconds)
    try:
        fh.stop()
    except Exception:
        pass

    window_min = max(3, int(seconds / 60) + 2)
    from lib.feed_parity import _mid_series_from_store
    incumbent = _mid_series_from_store(symbol_base, window_min).get(venue, [])
    # Judge only the overlap: the probe started mid-window, and buckets
    # the incumbent recorded before it are not disagreement.
    incumbent = [(ts, mid) for ts, mid in incumbent if ts >= start]

    pairs = pairwise_parity(
        {venue: incumbent, f"cryptofeed:{venue}": collected},
        threshold_bps=SAME_VENUE_BPS)
    verdict = pairs[0] if pairs else {"verdict": "no_data"}
    out = {
        "venue": venue,
        "seconds": seconds,
        "cryptofeed_observations": len(collected),
        "incumbent_observations": len(incumbent),
        "threshold_bps": SAME_VENUE_BPS,
        "parity": verdict,
        "promotable": verdict.get("verdict") == "parity",
        "note": ("same-venue comparison: real basis is zero by "
                 "construction; 'parity' here means the implementations "
                 "agree about reality"),
    }
    logger.info(f"[CryptofeedProbe] {out}")
    return out
