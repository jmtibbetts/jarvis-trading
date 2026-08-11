import unittest

import numpy as np
import pandas as pd

from lib.portfolio_risk import (
    MIN_VAR_DAYS, breadth_above_smas, concentration_summary, correlation_matrix,
    historical_var, returns_frame,
)


def _walk(n, drift=0.0, vol=0.01, seed=1, start=100.0):
    rng = np.random.default_rng(seed)
    rets = rng.normal(drift, vol, n)
    return pd.Series(start * np.cumprod(1 + rets))


class ReturnsFrameTests(unittest.TestCase):
    def test_builds_pct_returns(self):
        closes = {"A": pd.Series([100.0, 110.0, 99.0])}
        rf = returns_frame(closes)
        self.assertAlmostEqual(float(rf["A"].iloc[0]), 0.10, places=6)
        self.assertAlmostEqual(float(rf["A"].iloc[1]), -0.10, places=6)

    def test_too_short_series_dropped(self):
        rf = returns_frame({"A": pd.Series([100.0]), "B": _walk(50)})
        self.assertNotIn("A", rf.columns)
        self.assertIn("B", rf.columns)

    def test_empty_input(self):
        self.assertTrue(returns_frame({}).empty)


class CorrelationMatrixTests(unittest.TestCase):
    def test_identical_series_correlate_at_one(self):
        s = _walk(100)
        rf = returns_frame({"A": s, "B": s.copy()})
        m = correlation_matrix(rf)
        self.assertAlmostEqual(m["A"]["B"], 1.0, places=3)

    def test_inverse_series_correlate_at_minus_one(self):
        s = _walk(100)
        inv = pd.Series(1 / s)
        rf = returns_frame({"A": s, "B": inv})
        m = correlation_matrix(rf)
        self.assertLess(m["A"]["B"], -0.95)

    def test_insufficient_overlap_is_null_not_zero(self):
        """A pair with too few common days must report null — zero would be
        an affirmative claim of no relationship."""
        rf = returns_frame({"A": _walk(100), "B": _walk(10, seed=2)})
        m = correlation_matrix(rf, min_overlap=30)
        self.assertIsNone(m["A"]["B"])


class ConcentrationTests(unittest.TestCase):
    def test_flags_highly_correlated_pair(self):
        s = _walk(100)
        noisy_clone = s * (1 + np.random.default_rng(3).normal(0, 0.001, len(s)))
        rf = returns_frame({"A": s, "B": pd.Series(noisy_clone), "C": _walk(100, seed=9)})
        m = correlation_matrix(rf)
        summary = concentration_summary(m, {"A": 1000.0, "B": 1000.0, "C": 1000.0})
        flagged = {(p["a"], p["b"]) for p in summary["high_correlation_pairs"]}
        self.assertIn(("A", "B"), flagged)

    def test_no_pairs_measured_gives_null_average(self):
        summary = concentration_summary({"A": {"A": 1.0}}, {"A": 1000.0})
        self.assertIsNone(summary["avg_pairwise_correlation"])
        self.assertEqual(summary["pairs_measured"], 0)


class HistoricalVarTests(unittest.TestCase):
    def test_var_is_positive_loss_figure_with_sample_size(self):
        rf = returns_frame({"A": _walk(300), "B": _walk(300, seed=5)})
        var = historical_var(rf, {"A": 6000.0, "B": 4000.0}, gross_value=10_000.0)
        self.assertIsNotNone(var)
        self.assertGreater(var["var_pct"], 0)
        self.assertGreaterEqual(var["expected_shortfall_pct"], var["var_pct"])
        self.assertGreaterEqual(var["sample_days"], MIN_VAR_DAYS)
        self.assertAlmostEqual(var["var_usd"], var["var_pct"] / 100 * 10_000, places=1)

    def test_abstains_below_sample_floor(self):
        rf = returns_frame({"A": _walk(30)})
        self.assertIsNone(historical_var(rf, {"A": 1000.0}))

    def test_partial_days_never_zero_filled(self):
        """A short-history symbol must never be zero-filled into the joint
        distribution (a zero return is a claim). It is excluded and reported
        instead; the surviving VaR covers only the remaining symbols."""
        rf = returns_frame({"A": _walk(300), "B": _walk(40, seed=7)})
        var = historical_var(rf, {"A": 1000.0, "B": 1000.0})
        self.assertIsNotNone(var)
        self.assertEqual(var["symbols_included"], ["A"])
        self.assertEqual(var["symbols_excluded_short_history"], ["B"])
        # And with only the short symbol held, it genuinely abstains:
        self.assertIsNone(historical_var(rf, {"B": 1000.0}))

    def test_no_held_symbols_returns_none(self):
        rf = returns_frame({"A": _walk(300)})
        self.assertIsNone(historical_var(rf, {}))

    def test_short_history_symbol_excluded_and_reported_not_fatal(self):
        """Regression for a live finding: individually long histories whose
        JOINT window was ~38 days abstained entirely. The symbol whose
        removal most grows the joint sample is dropped, reported, and
        excluded from the dollar scaling."""
        rf = returns_frame({"A": _walk(300), "B": _walk(300, seed=5), "C": _walk(40, seed=7)})
        var = historical_var(rf, {"A": 5000.0, "B": 3000.0, "C": 2000.0}, gross_value=10_000.0)
        self.assertIsNotNone(var)
        self.assertEqual(var["symbols_excluded_short_history"], ["C"])
        self.assertEqual(sorted(var["symbols_included"]), ["A", "B"])
        self.assertEqual(var["included_gross_usd"], 8000.0)
        # dollar VaR scales to the 8k actually measured, not the 10k book
        self.assertAlmostEqual(var["var_usd"], var["var_pct"] / 100 * 8000.0, places=1)

    def test_timestamp_alignment_across_asset_classes(self):
        """Regression: crypto daily bars stamp 00:00 UTC, equity bars at
        exchange hours — raw-timestamp joins produced zero common rows."""
        import pandas as pd
        idx_eq = pd.date_range("2026-01-01 21:00", periods=100, freq="D", tz="UTC")
        idx_cr = pd.date_range("2026-01-01 00:00", periods=100, freq="D", tz="UTC")
        eq = pd.Series(_walk(100).values, index=idx_eq)
        cr = pd.Series(_walk(100, seed=2).values, index=idx_cr)
        rf = returns_frame({"EQ": eq, "CR": cr})
        joint = rf.dropna()
        self.assertGreaterEqual(len(joint), 90)


class BreadthTests(unittest.TestCase):
    def test_uptrending_symbols_above_smas(self):
        closes = {f"S{i}": pd.Series(np.linspace(100, 200, 250)) for i in range(4)}
        b = breadth_above_smas(closes)
        self.assertEqual(b["windows"]["sma20"]["pct_above"], 100.0)
        self.assertEqual(b["windows"]["sma200"]["eligible"], 4)

    def test_downtrending_symbols_below_smas(self):
        closes = {f"S{i}": pd.Series(np.linspace(200, 100, 250)) for i in range(4)}
        b = breadth_above_smas(closes)
        self.assertEqual(b["windows"]["sma50"]["pct_above"], 0.0)

    def test_coverage_reported_when_history_short(self):
        """A symbol with 30 closes counts for SMA20 but not SMA200 — the
        eligible counts must say so instead of pretending full coverage."""
        closes = {"LONG": pd.Series(np.linspace(100, 200, 250)),
                  "SHORT_HIST": pd.Series(np.linspace(100, 120, 30))}
        b = breadth_above_smas(closes)
        self.assertEqual(b["windows"]["sma20"]["eligible"], 2)
        self.assertEqual(b["windows"]["sma200"]["eligible"], 1)
        self.assertEqual(b["universe_size"], 2)

    def test_empty_universe(self):
        b = breadth_above_smas({})
        self.assertEqual(b["universe_size"], 0)
        self.assertIsNone(b["windows"]["sma20"]["pct_above"])


if __name__ == "__main__":
    unittest.main()
