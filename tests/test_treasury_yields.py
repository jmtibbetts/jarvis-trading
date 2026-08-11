import os
import unittest
from unittest.mock import patch

from lib import treasury_yields

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "treasury_yield_curve_sample.csv")


def _load_fixture() -> str:
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        return f.read()


class ParseCsvTests(unittest.TestCase):
    """Fixture is a real (trimmed) response from Treasury's public daily
    yield curve CSV export, fetched live while building this."""

    def test_parses_real_fixture_newest_first(self):
        rows = treasury_yields._parse_csv(_load_fixture())
        self.assertEqual(len(rows), 19)
        self.assertEqual(rows[0]["date"], "2026-08-10")
        self.assertEqual(rows[-1]["date"], "2026-07-15")

    def test_extracts_key_tenors_correctly(self):
        rows = treasury_yields._parse_csv(_load_fixture())
        latest = rows[0]
        self.assertEqual(latest["2yr"], 4.25)
        self.assertEqual(latest["10yr"], 4.72)
        self.assertEqual(latest["30yr"], 5.25)
        self.assertEqual(latest["3mo"], 3.89)

    def test_skips_rows_without_a_date(self):
        text = 'Date,"2 Yr","10 Yr"\n,4.0,4.5\n08/10/2026,4.25,4.72\n'
        rows = treasury_yields._parse_csv(text)
        self.assertEqual(len(rows), 1)

    def test_empty_csv_returns_empty_list(self):
        self.assertEqual(treasury_yields._parse_csv('Date,"2 Yr","10 Yr"\n'), [])


class ComputeSpreadsTests(unittest.TestCase):
    def test_positive_spread_not_inverted(self):
        spreads = treasury_yields.compute_spreads({"2yr": 3.0, "10yr": 4.0, "3mo": 3.5})
        self.assertEqual(spreads["spread_2s10s"], 1.0)
        self.assertEqual(spreads["spread_3m10y"], 0.5)
        self.assertFalse(spreads["2s10s_inverted"])
        self.assertFalse(spreads["3m10y_inverted"])

    def test_negative_spread_is_inverted(self):
        """This is the real, well-documented shape of an inverted curve
        (short-term yields exceed long-term — the market pricing in cuts)."""
        spreads = treasury_yields.compute_spreads({"2yr": 4.5, "10yr": 4.0, "3mo": 5.0})
        self.assertEqual(spreads["spread_2s10s"], -0.5)
        self.assertTrue(spreads["2s10s_inverted"])
        self.assertTrue(spreads["3m10y_inverted"])

    def test_missing_tenor_returns_none_not_exception(self):
        spreads = treasury_yields.compute_spreads({"2yr": 4.0})
        self.assertIsNone(spreads["spread_2s10s"])
        self.assertFalse(spreads["2s10s_inverted"])


class GetYieldCurveSnapshotTests(unittest.TestCase):
    def setUp(self):
        treasury_yields._cache = {}
        treasury_yields._cache_time = 0.0

    def test_builds_snapshot_from_real_fixture(self):
        with patch.object(treasury_yields, "fetch_yield_curve", return_value=treasury_yields._parse_csv(_load_fixture())):
            snapshot = treasury_yields.get_yield_curve_snapshot()
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["latest"]["date"], "2026-08-10")
        self.assertIn("spread_2s10s", snapshot["latest"])
        self.assertEqual(len(snapshot["trend"]), 19)

    def test_caches_between_calls(self):
        with patch.object(treasury_yields, "fetch_yield_curve", return_value=treasury_yields._parse_csv(_load_fixture())) as mock_fetch:
            treasury_yields.get_yield_curve_snapshot()
            treasury_yields.get_yield_curve_snapshot()
            self.assertEqual(mock_fetch.call_count, 1)

    def test_force_refresh_bypasses_cache(self):
        with patch.object(treasury_yields, "fetch_yield_curve", return_value=treasury_yields._parse_csv(_load_fixture())) as mock_fetch:
            treasury_yields.get_yield_curve_snapshot()
            treasury_yields.get_yield_curve_snapshot(force_refresh=True)
            self.assertEqual(mock_fetch.call_count, 2)

    def test_empty_current_year_falls_back_to_previous_year(self):
        real_rows = treasury_yields._parse_csv(_load_fixture())
        with patch.object(treasury_yields, "fetch_yield_curve", side_effect=[[], real_rows]) as mock_fetch:
            snapshot = treasury_yields.get_yield_curve_snapshot()
        self.assertIsNotNone(snapshot)
        self.assertEqual(mock_fetch.call_count, 2)

    def test_all_sources_empty_returns_none(self):
        with patch.object(treasury_yields, "fetch_yield_curve", return_value=[]):
            self.assertIsNone(treasury_yields.get_yield_curve_snapshot())


if __name__ == "__main__":
    unittest.main()
