import unittest

from lib.performance_analytics import (
    daily_equity_curve, compute_max_drawdown, compute_sharpe_ratio, signal_source_breakdown,
)


class DailyEquityCurveTests(unittest.TestCase):
    def test_collapses_to_last_snapshot_per_day(self):
        snapshots = [
            {"snapshot_at": "2026-01-01T09:00:00+00:00", "equity": 100000.0},
            {"snapshot_at": "2026-01-01T15:00:00+00:00", "equity": 101000.0},
            {"snapshot_at": "2026-01-02T09:00:00+00:00", "equity": 99000.0},
        ]
        curve = daily_equity_curve(snapshots)
        self.assertEqual(curve, [("2026-01-01", 101000.0), ("2026-01-02", 99000.0)])

    def test_skips_rows_missing_fields(self):
        snapshots = [{"snapshot_at": "", "equity": 100.0}, {"snapshot_at": "2026-01-01", "equity": None}]
        self.assertEqual(daily_equity_curve(snapshots), [])


class MaxDrawdownTests(unittest.TestCase):
    def test_computes_largest_peak_to_trough_decline(self):
        curve = [("d1", 100.0), ("d2", 120.0), ("d3", 90.0), ("d4", 110.0), ("d5", 80.0)]
        result = compute_max_drawdown(curve)
        # peak 120 (d2) -> trough 80 (d5): (120-80)/120 = 33.33%
        self.assertAlmostEqual(result["max_drawdown_pct"], 33.33, places=1)
        self.assertEqual(result["peak_date"], "d2")
        self.assertEqual(result["trough_date"], "d5")

    def test_monotonically_rising_curve_has_zero_drawdown(self):
        curve = [("d1", 100.0), ("d2", 110.0), ("d3", 120.0)]
        result = compute_max_drawdown(curve)
        self.assertEqual(result["max_drawdown_pct"], 0.0)

    def test_insufficient_data_returns_zero(self):
        self.assertEqual(compute_max_drawdown([])["max_drawdown_pct"], 0.0)
        self.assertEqual(compute_max_drawdown([("d1", 100.0)])["max_drawdown_pct"], 0.0)


class SharpeRatioTests(unittest.TestCase):
    def test_returns_none_with_too_few_points(self):
        self.assertIsNone(compute_sharpe_ratio([("d1", 100.0), ("d2", 101.0)]))

    def test_returns_none_when_returns_have_zero_variance(self):
        curve = [("d1", 100.0), ("d2", 101.0), ("d3", 102.01)]  # constant 1% daily return
        self.assertIsNone(compute_sharpe_ratio(curve))

    def test_positive_for_a_steadily_rising_curve_with_variance(self):
        curve = [("d1", 100.0), ("d2", 102.0), ("d3", 101.0), ("d4", 104.0), ("d5", 106.0)]
        sharpe = compute_sharpe_ratio(curve)
        self.assertIsNotNone(sharpe)
        self.assertGreater(sharpe, 0)


class SignalSourceBreakdownTests(unittest.TestCase):
    def test_groups_and_computes_win_rate_per_source(self):
        outcomes = [
            {"signal_source": "watchlist", "outcome": "WIN", "pnl_pct": 5.0},
            {"signal_source": "watchlist", "outcome": "LOSS", "pnl_pct": -2.0},
            {"signal_source": "scanner", "outcome": "WIN", "pnl_pct": 3.0},
            {"signal_source": "scanner", "outcome": "WIN", "pnl_pct": 4.0},
        ]
        result = {row["signal_source"]: row for row in signal_source_breakdown(outcomes)}
        self.assertEqual(result["watchlist"]["win_rate_pct"], 50.0)
        self.assertEqual(result["scanner"]["win_rate_pct"], 100.0)
        self.assertAlmostEqual(result["scanner"]["avg_pnl_pct"], 3.5, places=2)

    def test_missing_source_grouped_as_unknown(self):
        outcomes = [{"outcome": "WIN", "pnl_pct": 1.0}]
        result = signal_source_breakdown(outcomes)
        self.assertEqual(result[0]["signal_source"], "unknown")

    def test_empty_input_returns_empty_list(self):
        self.assertEqual(signal_source_breakdown([]), [])


if __name__ == "__main__":
    unittest.main()
