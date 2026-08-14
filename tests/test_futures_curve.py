"""Curve snapshots — the math and the record, offline.

The live fetch was verified against six real contracts across three
venues on 2026-08-14 (CL backwardated: U26 82.30 > V26 81.20 > Z26
77.95); these tests pin the stats math and the storage contract that
snapshot rides on.
"""
import os
import tempfile
import unittest
from unittest.mock import patch

from lib.futures_curve import curve_stats, sync_curves, yahoo_ticker


def _pts(*pairs):
    return [{"code": c, "price": p, "dte": d} for c, p, d in pairs]


class CurveStatsTests(unittest.TestCase):
    def test_backwardation_pays_the_long(self):
        # The real CL strip from the live verification.
        s = curve_stats(_pts(("CLU26", 82.30, 6), ("CLV26", 81.20, 37),
                             ("CLZ26", 77.95, 98)))
        self.assertEqual(s["structure"], "backwardation")
        self.assertLess(s["spread_pct"], 0)
        self.assertGreater(s["annualized_roll_pct"], 0)   # roll PAYS
        self.assertLess(s["slope_pct"], 0)

    def test_contango_charges_the_long(self):
        s = curve_stats(_pts(("NGV26", 2.80, 40), ("NGX26", 3.00, 70),
                             ("NGZ26", 3.20, 100)))
        self.assertEqual(s["structure"], "contango")
        self.assertLess(s["annualized_roll_pct"], 0)

    def test_annualization_uses_the_actual_gap(self):
        # 1% spread over a 30-day gap ≈ 12.17%/yr, negative for a long.
        s = curve_stats(_pts(("A", 100.0, 10), ("B", 101.0, 40)))
        self.assertAlmostEqual(s["annualized_roll_pct"], -12.17, places=2)

    def test_one_point_has_no_shape(self):
        s = curve_stats(_pts(("A", 100.0, 10)))
        self.assertIsNone(s["structure"])


class TickerTests(unittest.TestCase):
    def test_venue_suffixes(self):
        from lib.futures_contracts import contract
        self.assertEqual(yahoo_ticker(contract("CL", "V", 2026)), "CLV26.NYM")
        self.assertEqual(yahoo_ticker(contract("GC", "V", 2026)), "GCV26.CMX")
        self.assertEqual(yahoo_ticker(contract("ES", "Z", 2026)), "ESZ26.CME")


class SyncTests(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("JARVIS_EVENTS_DB_PATH")
        d = tempfile.mkdtemp(prefix="jarvis-test-events-")
        os.environ["JARVIS_EVENTS_DB_PATH"] = os.path.join(d, "ev.db")

    def tearDown(self):
        if self._prev is not None:
            os.environ["JARVIS_EVENTS_DB_PATH"] = self._prev

    def _fake_curve(self, root, asof=None, depth=4):
        return {"root": root, "as_of": "2026-08-14",
                "points": _pts((f"{root}U26", 82.30, 6),
                               (f"{root}V26", 81.20, 37)),
                **curve_stats(_pts((f"{root}U26", 82.30, 6),
                                   (f"{root}V26", 81.20, 37)))}

    def test_snapshot_stored_with_roll_provenance_and_bucket_dedup(self):
        from lib.event_store import get_store
        with patch("lib.futures_curve.fetch_curve", side_effect=self._fake_curve):
            r1 = sync_curves(roots=("CL",))
            r2 = sync_curves(roots=("CL",))     # same bucket → deduped
        self.assertEqual(r1["stored"], 1)
        self.assertEqual(r2["stored"], 0)
        rows = get_store().read("CL", "curve_snapshot", since_ts=0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["front_code"], "CLU26")
        self.assertEqual(rows[0]["structure"], "backwardation")

    def test_unfetchable_root_is_skipped_not_fabricated(self):
        with patch("lib.futures_curve.fetch_curve", return_value=None):
            r = sync_curves(roots=("CL",))
        self.assertEqual(r["stored"], 0)
        self.assertEqual(r["fetched"], [])


if __name__ == "__main__":
    unittest.main()
