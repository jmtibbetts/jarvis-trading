"""Analog retrieval — pinned on synthetic bars where the right answer is
known by construction: a repeating pattern must find its own repetitions,
analogs must not overlap each other or the present, and thin history must
refuse rather than serve anecdotes.
"""
import unittest

import numpy as np
import pandas as pd

from lib.analogs import (
    MIN_SEPARATION,
    SELF_EXCLUSION,
    WINDOW,
    find_analogs,
)


def _bars(close: np.ndarray, freq_min: int = 15) -> pd.DataFrame:
    idx = pd.date_range("2021-01-01", periods=len(close),
                        freq=f"{freq_min}min", tz="UTC")
    return pd.DataFrame({
        "open": close, "high": close * 1.001, "low": close * 0.999,
        "close": close, "volume": np.full(len(close), 100.0),
    }, index=idx)


class AnalogTests(unittest.TestCase):
    def test_periodic_market_finds_its_own_period(self):
        # A strong sine over noise: the moments most similar to NOW sit
        # one full period apart, and their forward paths all rhyme.
        rng = np.random.default_rng(7)
        n, period = 12_000, 480
        t = np.arange(n)
        close = 100 * np.exp(0.05 * np.sin(2 * np.pi * t / period)
                             + rng.normal(0, 0.002, n).cumsum() * 0.05)
        out = find_analogs(_bars(close))
        self.assertIsNotNone(out)
        self.assertGreaterEqual(len(out["analogs"]), 5)
        # Phase alignment: chosen analogs cluster near multiples of the
        # period relative to the anchor — allow slack for noise.
        anchor = n - 1
        offsets = []
        for a in out["analogs"]:
            idx = _bars(close).index.get_loc(pd.Timestamp(a["time"]))
            offsets.append((anchor - idx) % period)
        aligned = sum(1 for o in offsets
                      if min(o, period - o) <= period * 0.15)
        self.assertGreaterEqual(aligned / len(offsets), 0.6,
                                f"phase offsets {offsets}")

    def test_analogs_never_overlap_each_other_or_the_present(self):
        rng = np.random.default_rng(3)
        close = 100 * np.exp(rng.normal(0, 0.004, 8000).cumsum())
        out = find_analogs(_bars(close))
        self.assertIsNotNone(out)
        bars = _bars(close)
        idxs = sorted(bars.index.get_loc(pd.Timestamp(a["time"]))
                      for a in out["analogs"])
        for a, b in zip(idxs, idxs[1:]):
            self.assertGreaterEqual(b - a, MIN_SEPARATION)
        self.assertLessEqual(max(idxs), len(bars) - SELF_EXCLUSION)

    def test_thin_history_refuses(self):
        close = 100 + np.arange(WINDOW * 3, dtype=float)
        self.assertIsNone(find_analogs(_bars(close)))

    def test_forward_summary_reports_sample_sizes(self):
        rng = np.random.default_rng(11)
        close = 100 * np.exp(rng.normal(0.0002, 0.004, 9000).cumsum())
        out = find_analogs(_bars(close))
        self.assertIsNotNone(out)
        for h, s in out["forward_summary"].items():
            self.assertGreaterEqual(s["n"], 5, h)
            self.assertLessEqual(s["iqr_pct"][0], s["median_pct"])
            self.assertGreaterEqual(s["iqr_pct"][1], s["median_pct"])


class TransmissionRuleTests(unittest.TestCase):
    def test_refinery_attack_maps_to_crude_up(self):
        from lib.threat_transmission import map_threat
        rows = map_threat("Drone strike sets major oil refinery ablaze")
        self.assertTrue(any(r["instrument"] == "CL=F" and
                            r["pressure"] == "up" for r in rows))

    def test_carrier_deployment_maps_to_oil_gold_defense(self):
        from lib.threat_transmission import map_threat
        rows = map_threat(
            "US sends second carrier strike group to the region")
        got = {(r["instrument"], r["pressure"]) for r in rows}
        self.assertIn(("CL=F", "up"), got)
        self.assertIn(("GC=F", "up"), got)
        self.assertIn(("NOC", "up"), got)

    def test_unrelated_news_maps_to_nothing(self):
        from lib.threat_transmission import map_threat
        self.assertEqual(map_threat(
            "Officials attend regional trade summit"), [])


if __name__ == "__main__":
    unittest.main()
