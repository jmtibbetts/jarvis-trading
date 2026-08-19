"""A failed write must not destroy the evidence it was trying to write.

MEASURED IN PRODUCTION. Two "evidence flush failed: database is locked"
warnings in a 35-minute collection run, each carrying a batch of 36-61
Bitnomial quote samples. Those rows are gone. `flush_samples` drained the
buffer BEFORE attempting the insert:

    batch, _BUF[:] = list(_BUF), []      # buffer emptied
    with get_db() as db:
        db.bulk_insert_mappings(...)     # raises -> batch already gone

and the flush loop logs a warning and carries on. Under a write-contended
SQLite database that is a steady, silent leak of paid-feed evidence, and it
gets worse as the write rate rises.

Evidence we paid to collect and cannot re-fetch is exactly the thing that
must survive a transient failure.
"""
import unittest
from unittest.mock import patch


class AFailedFlushKeepsTheEvidenceTests(unittest.TestCase):

    def setUp(self):
        from lib import range_collector as RC
        RC.reset_stream_state()
        with RC._BUF_LOCK:
            RC._BUF.clear()
        self.addCleanup(self._drain)

    def _drain(self):
        from lib import range_collector as RC
        with RC._BUF_LOCK:
            RC._BUF.clear()

    def _fill(self, n):
        from lib import range_collector as RC
        with RC._BUF_LOCK:
            for i in range(n):
                RC._BUF.append({
                    "product": "CRYPTO_PERP", "venue": "kraken_derivatives_us",
                    "symbol": "BTC/USD", "instrument_id": "PBTCUCZ50",
                    "market_data_source": "test",
                    "observed_at": f"2026-08-19T00:00:{i:02d}+00:00",
                    "bid": 64000.0 + i, "ask": 64010.0 + i,
                    "mid": 64005.0 + i, "sample_reason": "CHANGE"})

    def test_a_locked_database_does_not_lose_the_batch(self):
        from lib import range_collector as RC
        self._fill(12)

        class Boom(Exception):
            pass

        def locked(*a, **k):
            raise Boom("database is locked")

        with patch.object(RC, "get_db", locked) if hasattr(RC, "get_db") \
                else patch("app.database.get_db", locked):
            with self.assertRaises(Exception):
                RC.flush_samples()

        self.assertEqual(RC.buffered_count(), 12,
                         "the batch was destroyed by a failed write — this "
                         "is paid evidence that cannot be re-fetched")

    def test_the_retry_after_a_failure_writes_the_rows(self):
        from lib import range_collector as RC
        self._fill(7)

        def locked(*a, **k):
            raise RuntimeError("database is locked")

        with patch("app.database.get_db", locked):
            try:
                RC.flush_samples()
            except Exception:
                pass
        self.assertEqual(RC.buffered_count(), 7)
        written = RC.flush_samples()          # real DB this time
        self.assertEqual(written, 7, "the retry did not write the batch")
        self.assertEqual(RC.buffered_count(), 0)

    def test_a_successful_flush_still_drains(self):
        from lib import range_collector as RC
        self._fill(5)
        self.assertEqual(RC.flush_samples(), 5)
        self.assertEqual(RC.buffered_count(), 0)

    def test_the_buffer_cannot_grow_without_bound(self):
        """A database down for hours must not exhaust memory. Requeueing is
        bounded, and anything actually dropped is COUNTED rather than
        quietly forgotten."""
        from lib import range_collector as RC
        cap = getattr(RC, "MAX_BUFFERED_SAMPLES", None)
        self.assertIsNotNone(cap, "no bound on the evidence buffer")
        self._fill(cap + 50)      # deliberately OVER the cap

        def locked(*a, **k):
            raise RuntimeError("database is locked")
        with patch("app.database.get_db", locked):
            try:
                RC.flush_samples()
            except Exception:
                pass
        self.assertLessEqual(RC.buffered_count(), cap)
        self.assertGreater(RC.dropped_sample_count(), 0,
                           "rows were dropped without being counted")


if __name__ == "__main__":
    unittest.main()
