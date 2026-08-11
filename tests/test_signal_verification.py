import unittest
from unittest.mock import patch

from lib.signal_verification import STALE_ENTRY_PCT, verify_levels, verify_signal


class VerifyLevelsTests(unittest.TestCase):
    def test_long_confirmed_when_price_near_entry(self):
        r = verify_levels("Long", entry=100.0, target=110.0, stop=95.0, current=100.5)
        self.assertEqual(r["verdict"], "CONFIRMED")
        self.assertTrue(all(c["ok"] for c in r["checks"]))
        self.assertIsNone(r["suggested_update"])

    def test_long_invalidated_when_stop_breached(self):
        r = verify_levels("Long", entry=100.0, target=110.0, stop=95.0, current=94.0)
        self.assertEqual(r["verdict"], "INVALIDATED")

    def test_long_invalidated_when_target_already_reached(self):
        r = verify_levels("Long", entry=100.0, target=110.0, stop=95.0, current=111.0)
        self.assertEqual(r["verdict"], "INVALIDATED")

    def test_short_mirrors(self):
        # short: stop above entry, target below
        ok = verify_levels("Short", entry=100.0, target=92.0, stop=104.0, current=100.4)
        self.assertEqual(ok["verdict"], "CONFIRMED")
        stopped = verify_levels("Short", entry=100.0, target=92.0, stop=104.0, current=105.0)
        self.assertEqual(stopped["verdict"], "INVALIDATED")
        done = verify_levels("Short", entry=100.0, target=92.0, stop=104.0, current=91.0)
        self.assertEqual(done["verdict"], "INVALIDATED")

    def test_stale_entry_with_geometry_preserving_update(self):
        """Price drifted 3% above entry (beyond the 1.5% threshold) but is
        still inside stop/target: STALE_ENTRY, and the suggested update
        re-anchors preserving the ABSOLUTE level distances."""
        r = verify_levels("Long", entry=100.0, target=112.0, stop=96.0, current=103.0)
        self.assertEqual(r["verdict"], "STALE_ENTRY")
        u = r["suggested_update"]
        self.assertEqual(u["entry_price"], 103.0)
        self.assertEqual(u["stop_loss"], 99.0)      # 103 + (96-100)
        self.assertEqual(u["target_price"], 115.0)  # 103 + (112-100)
        # geometry preserved: same absolute distances
        self.assertAlmostEqual(u["target_price"] - u["entry_price"], 12.0)
        self.assertAlmostEqual(u["entry_price"] - u["stop_loss"], 4.0)

    def test_invalidation_outranks_staleness(self):
        """A price that is both far from entry AND through the stop is
        INVALIDATED — a re-anchor suggestion there would resurrect a dead
        trade."""
        r = verify_levels("Long", entry=100.0, target=110.0, stop=95.0, current=93.0)
        self.assertEqual(r["verdict"], "INVALIDATED")
        self.assertIsNone(r["suggested_update"])

    def test_drift_threshold_boundary(self):
        just_inside = verify_levels("Long", 100.0, 110.0, 95.0, current=100.0 + STALE_ENTRY_PCT - 0.01)
        self.assertEqual(just_inside["verdict"], "CONFIRMED")


class VerifySignalTests(unittest.TestCase):
    def _sig(self, **over):
        base = dict(asset_symbol="NVDA", asset_class="Equity", direction="Long",
                    entry_price=100.0, target_price=110.0, stop_loss=95.0)
        base.update(over)
        return base

    def test_missing_levels_is_data_unavailable(self):
        r = verify_signal(self._sig(entry_price=0))
        self.assertEqual(r["verdict"], "DATA_UNAVAILABLE")

    @patch("lib.signal_verification.fetch_current_price", return_value=(None, None, None))
    def test_no_price_source_is_data_unavailable_never_guessed(self, _):
        r = verify_signal(self._sig())
        self.assertEqual(r["verdict"], "DATA_UNAVAILABLE")
        self.assertIsNone(r["current_price"])

    @patch("lib.signal_verification.fetch_current_price", return_value=(100.2, "massive", "previous_session"))
    def test_full_result_carries_provenance(self, _):
        r = verify_signal(self._sig())
        self.assertEqual(r["verdict"], "CONFIRMED")
        self.assertEqual(r["price_source"], "massive")
        self.assertEqual(r["price_asof"], "previous_session")
        self.assertIn("verified_at", r)
        self.assertIn("end-of-day", r["note"])


if __name__ == "__main__":
    unittest.main()
