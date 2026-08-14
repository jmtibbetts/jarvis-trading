"""Phase 5 parity instrument — the gate any adapter swap must pass.

Pinned properties: bucketing gives each venue one voice per interval,
bps math is symmetric, corrupt books never vote, and the verdict ladder
(no_overlap / insufficient_overlap / divergent / parity) reflects what
was measured rather than what was hoped.
"""
import os
import tempfile
import time
import unittest

from lib.feed_parity import (
    SAME_VENUE_BPS,
    _bucketize,
    pairwise_parity,
    parity_report,
)


def _series(base_ts, mids, step=5.0):
    return [(base_ts + i * step, m) for i, m in enumerate(mids)]


class PairwiseTests(unittest.TestCase):
    def test_identical_series_is_parity_at_zero_bps(self):
        t = 1_000_000.0
        s = {"a": _series(t, [100.0] * 20), "b": _series(t, [100.0] * 20)}
        (p,) = pairwise_parity(s, threshold_bps=SAME_VENUE_BPS)
        self.assertEqual(p["verdict"], "parity")
        self.assertEqual(p["median_bps"], 0.0)
        self.assertEqual(p["coverage"], 1.0)

    def test_ten_bps_apart_fails_the_tight_gate(self):
        t = 1_000_000.0
        s = {"incumbent": _series(t, [100.0] * 20),
             "candidate": _series(t, [100.10] * 20)}   # 10 bps
        (p,) = pairwise_parity(s, threshold_bps=SAME_VENUE_BPS)
        self.assertEqual(p["verdict"], "divergent")
        self.assertAlmostEqual(p["median_bps"], 10.0, delta=0.1)

    def test_bps_is_symmetric_in_the_pair(self):
        t = 1_000_000.0
        s1 = {"a": _series(t, [100.0] * 10), "b": _series(t, [101.0] * 10)}
        s2 = {"a": _series(t, [101.0] * 10), "b": _series(t, [100.0] * 10)}
        (p1,) = pairwise_parity(s1, 50.0)
        (p2,) = pairwise_parity(s2, 50.0)
        self.assertEqual(p1["median_bps"], p2["median_bps"])

    def test_disjoint_windows_report_no_overlap_confidence(self):
        s = {"a": _series(1_000_000.0, [100.0] * 10),
             "b": _series(2_000_000.0, [100.0] * 10)}
        (p,) = pairwise_parity(s, 50.0)
        self.assertIn(p["verdict"], ("no_overlap", "insufficient_overlap"))

    def test_bucket_keeps_the_last_voice_per_interval(self):
        pts = [(0.0, 100.0), (1.0, 101.0), (4.9, 102.0), (5.1, 200.0)]
        b = _bucketize(pts, bucket_s=5.0)
        self.assertEqual(b[0], 102.0)     # last inside bucket 0
        self.assertEqual(b[1], 200.0)


class StoreIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("JARVIS_EVENTS_DB_PATH")
        d = tempfile.mkdtemp(prefix="jarvis-test-events-")
        os.environ["JARVIS_EVENTS_DB_PATH"] = os.path.join(d, "ev.db")

    def tearDown(self):
        if self._prev is not None:
            os.environ["JARVIS_EVENTS_DB_PATH"] = self._prev

    def _seed(self, source, kind, rows):
        from lib.event_store import get_store
        get_store().append(rows)

    def test_report_reads_quotes_and_healthy_books_only(self):
        from lib.event_store import get_store
        now = time.time()
        evs = []
        for i in range(20):
            ts = now - 100 + i * 5
            evs.append({"kind": "quote", "symbol": "BTC/USD",
                        "source": "kraken", "ingest_ts": ts,
                        "bid": 50_000.0, "ask": 50_002.0})
            evs.append({"kind": "book_snapshot", "symbol": "BTC",
                        "source": "coinbase", "ingest_ts": ts,
                        "health_valid": True,
                        "bids": [[50_001.0, 1.0]], "asks": [[50_003.0, 1.0]]})
            # A crossed book that must NOT vote:
            evs.append({"kind": "book_snapshot", "symbol": "BTC",
                        "source": "binance", "ingest_ts": ts,
                        "health_valid": False,
                        "bids": [[60_000.0, 1.0]], "asks": [[50_000.0, 1.0]]})
        get_store().append(evs)
        rep = parity_report("BTC", window_min=10)
        self.assertIn("kraken", rep["venues"])
        self.assertIn("coinbase", rep["venues"])
        self.assertNotIn("binance", rep["venues"])    # unhealthy: silenced
        (pair,) = rep["pairs"]
        self.assertEqual(pair["verdict"], "parity")   # ~0.4 bps apart
        self.assertLess(pair["median_bps"], 1.0)


class ProbeContractTests(unittest.TestCase):
    def test_unknown_venue_refuses(self):
        from lib.cryptofeed_probe import run_probe
        with self.assertRaises(KeyError):
            run_probe("shipping")

    def test_probe_module_imports_without_cryptofeed(self):
        # The heavy dependency loads inside run_probe, never at module
        # import — environments without cryptofeed still run every test.
        import sys
        import lib.cryptofeed_probe  # noqa: F401
        self.assertTrue("lib.cryptofeed_probe" in sys.modules)


if __name__ == "__main__":
    unittest.main()
