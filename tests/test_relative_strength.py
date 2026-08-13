"""Is it leading, or floating on the tide?

Not RSI. RSI asks whether a chart is stretched against its own history;
relative strength asks whether it is outperforming what actually drives it.
Most crypto moves are BTC moves and most equity moves are index moves — a
SOL breakout during a BTC rally may carry no information at all.

Without this the system buys beta and calls it a setup.
"""
import unittest

import pandas as pd

from lib.relative_strength import (BENCHMARKS, LOOKBACKS, MIN_BARS, benchmarks_for,
                                   compute_relative_strength)


def _bars(closes):
    return pd.DataFrame({"close": closes})


class BenchmarkSelectionTests(unittest.TestCase):
    def test_crypto_is_judged_against_crypto(self):
        self.assertIn("BTC/USD", benchmarks_for("SOL/USD"))

    def test_equities_are_judged_against_an_index(self):
        self.assertIn("SPY", benchmarks_for("NVDA"))

    def test_an_asset_is_never_compared_to_itself(self):
        """Returning 1.0 would read as "perfectly in line" rather than
        "not applicable"."""
        self.assertNotIn("BTC/USD", benchmarks_for("BTC/USD"))
        self.assertNotIn("SPY", benchmarks_for("SPY"))

    def test_classes_without_a_meaningful_benchmark_get_none(self):
        """Comparing crude oil to SPY is not relative strength, it is noise
        with a ratio sign."""
        self.assertEqual(BENCHMARKS["futures"], [])
        self.assertEqual(BENCHMARKS["forex"], [])


class OutperformanceTests(unittest.TestCase):
    def test_rising_faster_reads_as_leading(self):
        n = 40
        asset = [100 * (1.02 ** i) for i in range(n)]
        bench = [100 * (1.005 ** i) for i in range(n)]
        rs = compute_relative_strength(_bars(asset), _bars(bench), "BTC/USD")
        self.assertEqual(rs["state"], "LEADING")
        self.assertGreater(rs["rs_20"], 0)

    def test_falling_less_ALSO_reads_as_leading(self):
        """The point of a ratio: both can be falling and the asset still
        outperforming. An absolute-return view would call this bearish."""
        n = 40
        asset = [100 * (0.995 ** i) for i in range(n)]
        bench = [100 * (0.98 ** i) for i in range(n)]
        rs = compute_relative_strength(_bars(asset), _bars(bench), "BTC/USD")
        self.assertEqual(rs["state"], "LEADING")

    def test_moving_together_is_not_leading(self):
        n = 40
        same = [100 * (1.01 ** i) for i in range(n)]
        rs = compute_relative_strength(_bars(same), _bars(list(same)), "BTC/USD")
        self.assertNotEqual(rs["state"], "LEADING")
        self.assertAlmostEqual(rs["rs_20"], 0.0, places=3)

    def test_lagging_is_detected(self):
        n = 40
        asset = [100 * (1.001 ** i) for i in range(n)]
        bench = [100 * (1.02 ** i) for i in range(n)]
        rs = compute_relative_strength(_bars(asset), _bars(bench), "BTC/USD")
        self.assertEqual(rs["state"], "LAGGING")


class RatioBreakoutTests(unittest.TestCase):
    def test_a_new_ratio_high_is_flagged(self):
        n = 40
        asset = [100 * (1.01 ** i) for i in range(n)]
        bench = [100.0] * n
        rs = compute_relative_strength(_bars(asset), _bars(bench), "BTC/USD")
        self.assertTrue(rs["rs_breakout"])

    def test_a_new_ratio_low_is_flagged(self):
        n = 40
        asset = [100 * (0.99 ** i) for i in range(n)]
        bench = [100.0] * n
        rs = compute_relative_strength(_bars(asset), _bars(bench), "BTC/USD")
        self.assertTrue(rs["rs_breakdown"])


class InsufficientDataTests(unittest.TestCase):
    """Missing is not zero — a relative strength of 0 reads as "exactly in
    line", which is a claim, not an absence."""

    def test_short_history_returns_none(self):
        short = [100.0] * (MIN_BARS - 5)
        self.assertIsNone(compute_relative_strength(_bars(short), _bars(short), "BTC/USD"))

    def test_the_floor_is_what_the_longest_lookback_needs(self):
        """An arbitrary margin above it silently excluded whole asset
        classes: equity 1H history is ~24 bars, and a floor of 25 reported
        "no benchmark data" for every stock while the data was present."""
        self.assertEqual(MIN_BARS, max(LOOKBACKS) + 1)

    def test_a_missing_benchmark_returns_none_not_a_number(self):
        ok = [100.0 + i for i in range(40)]
        self.assertIsNone(compute_relative_strength(_bars(ok), _bars([]), "BTC/USD"))


if __name__ == "__main__":
    unittest.main()
