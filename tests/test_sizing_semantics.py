"""Sizing invariants: confidence never sizes, failure never trades.

The three defects these pin (all verified at HEAD before fixing):
  - the executor wrote the COMPOSITE SCORE into a field named
    "confidence", and risk_manager clamped it into a Kelly win
    probability — an inverted number bet as p(win)
  - when the risk engine crashed, the executor priced a budget FROM
    confidence and traded anyway (fail-open)
  - after risk approval, a 1-2x "conviction" multiplier enlarged the
    position from the same score
"""
import inspect
import unittest
from unittest.mock import patch

from lib.risk_manager import calculate_position_size

SIG = {"asset_symbol": "NVDA", "direction": "Long", "confidence": 90,
       "entry_price": 100.0, "stop_loss": 96.0, "target_price": 110.0,
       "asset_class": "Equity", "timeframe": "4H", "strategy": None}
REGIME = {"risk": "low"}


def _stats(p_win=0.55, lo=0.50, hi=0.60, n=100, win_r=1.5, loss_r=1.0):
    return {"raw_sample": n, "p_win": p_win, "p_win_ci": [lo, hi],
            "avg_win_r": win_r, "avg_loss_r": loss_r}


class ConfidenceNeverSizesTests(unittest.TestCase):
    def test_confidence_10_and_95_size_identically(self):
        """Invariant #1: model self-belief has zero sizing effect."""
        with patch("lib.expectancy.lookup", return_value=None):
            low = calculate_position_size(dict(SIG, confidence=10), 100_000, REGIME)
            high = calculate_position_size(dict(SIG, confidence=95), 100_000, REGIME)
        self.assertEqual(low.dollar_size, high.dollar_size)
        self.assertGreater(high.dollar_size, 0)

    def test_no_measured_evidence_means_no_kelly(self):
        """Fixed-fractional only — Kelly contributes nothing without stats."""
        with patch("lib.expectancy.lookup", return_value=None):
            sz = calculate_position_size(SIG, 100_000, REGIME)
        self.assertEqual(sz.kelly_fraction, 0.0)
        self.assertGreater(sz.dollar_size, 0)

    def test_thin_sample_means_no_kelly(self):
        with patch("lib.expectancy.lookup", return_value=_stats(n=10)):
            sz = calculate_position_size(SIG, 100_000, REGIME)
        self.assertEqual(sz.kelly_fraction, 0.0)

    def test_kelly_uses_the_lower_bound_not_the_point(self):
        """Uncertainty must SHRINK size: a wide interval with the same
        point estimate produces a smaller Kelly fraction."""
        with patch("lib.expectancy.lookup",
                   return_value=_stats(p_win=0.60, lo=0.58)):
            tight = calculate_position_size(SIG, 100_000, REGIME)
        with patch("lib.expectancy.lookup",
                   return_value=_stats(p_win=0.60, lo=0.45)):
            wide = calculate_position_size(SIG, 100_000, REGIME)
        self.assertGreater(tight.kelly_fraction, wide.kelly_fraction)


class LifecycleShrinksRiskTests(unittest.TestCase):
    def test_multiplier_scales_the_risk_budget(self):
        """Invariant #11: REDUCED/EXPERIMENTAL actually reduce size — on
        the risk budget, before quantity is solved."""
        with patch("lib.expectancy.lookup", return_value=None):
            full = calculate_position_size(SIG, 100_000, REGIME, lifecycle_multiplier=1.0)
            half = calculate_position_size(SIG, 100_000, REGIME, lifecycle_multiplier=0.5)
            quarter = calculate_position_size(SIG, 100_000, REGIME, lifecycle_multiplier=0.25)
        self.assertAlmostEqual(half.dollar_size, full.dollar_size * 0.5, delta=full.dollar_size * 0.02)
        self.assertAlmostEqual(quarter.dollar_size, full.dollar_size * 0.25, delta=full.dollar_size * 0.02)


class ExecutorSemanticsTests(unittest.TestCase):
    def test_fail_open_is_gone(self):
        from jobs import execute_signals
        src = inspect.getsource(execute_signals)
        self.assertNotIn("500 + (conf - 55)", src,
                         "the confidence-priced fallback budget survived")
        self.assertIn("RISK ENGINE ERROR", src)

    def test_conviction_multiplier_is_gone(self):
        from jobs import execute_signals
        src = inspect.getsource(execute_signals)
        self.assertNotIn("conviction_mult", src)

    def test_composite_no_longer_masquerades_as_confidence(self):
        from jobs import execute_signals
        src = inspect.getsource(execute_signals)
        self.assertNotIn("s.composite_score or s.confidence or 65", src)


if __name__ == "__main__":
    unittest.main()
