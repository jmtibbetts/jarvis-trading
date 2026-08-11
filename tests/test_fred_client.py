import unittest
from unittest.mock import MagicMock, patch

from lib import fred_client

# FRED's documented, stable observations schema:
# https://fred.stlouisfed.org/docs/api/fred/series_observations.html
def _fred_response(dates_values: list[tuple[str, str]]) -> dict:
    return {
        "observations": [{"date": d, "value": v} for d, v in dates_values],
    }


class IsConfiguredTests(unittest.TestCase):
    def test_false_when_no_key(self):
        with patch.object(fred_client, "FRED_API_KEY", ""):
            self.assertFalse(fred_client.is_configured())

    def test_true_when_key_present(self):
        with patch.object(fred_client, "FRED_API_KEY", "fake-key"):
            self.assertTrue(fred_client.is_configured())


class FetchSeriesTests(unittest.TestCase):
    def test_returns_empty_without_key(self):
        with patch.object(fred_client, "FRED_API_KEY", ""):
            self.assertEqual(fred_client.fetch_series("CPIAUCSL"), [])

    def test_parses_observations_and_filters_missing_values(self):
        """FRED represents a not-yet-released data point as value="." —
        must be dropped, not parsed as a float (which would raise)."""
        payload = _fred_response([("2026-06-01", "3.1"), ("2026-05-01", "."), ("2026-04-01", "3.0")])
        mock_resp = MagicMock()
        mock_resp.json.return_value = payload
        mock_resp.raise_for_status = lambda: None
        with patch.object(fred_client, "FRED_API_KEY", "fake-key"), \
             patch("httpx.get", return_value=mock_resp):
            rows = fred_client.fetch_series("CPIAUCSL")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], {"date": "2026-06-01", "value": 3.1})

    def test_network_failure_returns_empty_list(self):
        import httpx
        with patch.object(fred_client, "FRED_API_KEY", "fake-key"), \
             patch("httpx.get", side_effect=httpx.ConnectError("boom")):
            self.assertEqual(fred_client.fetch_series("CPIAUCSL"), [])


class ShiftMonthsTests(unittest.TestCase):
    def test_shifts_within_same_year(self):
        self.assertEqual(fred_client._shift_months("2026-06-01", 3), "2026-03-01")

    def test_shifts_across_year_boundary(self):
        self.assertEqual(fred_client._shift_months("2026-06-01", 12), "2025-06-01")
        self.assertEqual(fred_client._shift_months("2026-02-01", 3), "2025-11-01")

    def test_shifts_across_multiple_year_boundaries(self):
        self.assertEqual(fred_client._shift_months("2026-01-01", 25), "2023-12-01")


class YoyPctTests(unittest.TestCase):
    def test_computes_correct_yoy_percentage(self):
        """CPI index 310 now vs 300 a year ago = +3.33% YoY — this is the
        actual standard 'inflation rate' calculation, not a raw level diff."""
        rows = [{"date": "2026-06-01", "value": 310.0}, {"date": "2026-05-01", "value": 308.0},
                {"date": "2025-06-01", "value": 300.0}, {"date": "2025-05-01", "value": 299.0}]
        result = fred_client._yoy_pct(rows, periods_back=12)
        self.assertIsNotNone(result)
        self.assertEqual(result["compared_to"], "2025-06-01")
        self.assertAlmostEqual(result["value"], (310.0 - 300.0) / 300.0 * 100, places=2)

    def test_matches_by_calendar_date_not_list_position(self):
        """Regression test: an earlier version indexed 12 rows back in the
        list, which silently misaligned the whole comparison whenever any
        single month upstream had been filtered out (FRED's value="."
        for a not-yet-revised month) — this is exactly what happened with
        real live CPI data, comparing June to May instead of June to June.
        Fixed by matching on the actual calendar date instead."""
        # May 2025 is MISSING (simulates a filtered "." value) — a
        # positional offset of 12 would have landed on April instead.
        rows = [{"date": "2026-06-01", "value": 310.0}, {"date": "2026-05-01", "value": 308.0},
                {"date": "2026-04-01", "value": 307.0}, {"date": "2025-06-01", "value": 300.0},
                {"date": "2025-04-01", "value": 296.0}]  # note: no 2025-05-01
        result = fred_client._yoy_pct(rows, periods_back=12)
        self.assertEqual(result["compared_to"], "2025-06-01")
        self.assertAlmostEqual(result["value"], (310.0 - 300.0) / 300.0 * 100, places=2)

    def test_no_matching_date_returns_none(self):
        rows = [{"date": "2026-06-01", "value": 310.0}, {"date": "2026-05-01", "value": 305.0}]
        self.assertIsNone(fred_client._yoy_pct(rows, periods_back=12))

    def test_zero_year_ago_value_returns_none_not_exception(self):
        rows = [{"date": "2026-06-01", "value": 5.0}, {"date": "2025-06-01", "value": 0.0}]
        self.assertIsNone(fred_client._yoy_pct(rows, periods_back=12))

    def test_empty_rows_returns_none(self):
        self.assertIsNone(fred_client._yoy_pct([], periods_back=12))

    def test_quarterly_gdp_uses_same_12_month_lookup(self):
        """GDP observations are one per quarter (dated on quarter-start
        months) — the same calendar-date shift works without a separate
        'quarterly' code path, since 12 months always means 4 quarters."""
        rows = [{"date": "2026-04-01", "value": 22000.0}, {"date": "2026-01-01", "value": 21800.0},
                {"date": "2025-04-01", "value": 21500.0}, {"date": "2025-01-01", "value": 21300.0}]
        result = fred_client._yoy_pct(rows, periods_back=12)
        self.assertEqual(result["compared_to"], "2025-04-01")
        self.assertAlmostEqual(result["value"], (22000.0 - 21500.0) / 21500.0 * 100, places=2)


class MomChangeTests(unittest.TestCase):
    def test_computes_period_over_period_change(self):
        """Nonfarm payrolls convention: report the MoM change in thousands,
        not the cumulative employment level (which is a huge, uninformative number)."""
        rows = [{"date": "2026-06-01", "value": 159200.0}, {"date": "2026-05-01", "value": 159000.0}]
        result = fred_client._mom_change(rows)
        self.assertEqual(result["value"], 200.0)
        self.assertEqual(result["compared_to"], "2026-05-01")

    def test_single_observation_returns_none(self):
        self.assertIsNone(fred_client._mom_change([{"date": "2026-06-01", "value": 100.0}]))


class LevelTests(unittest.TestCase):
    def test_returns_latest_value_as_is(self):
        """Unemployment rate and Fed funds rate are already percentages —
        no transformation needed, unlike the index-based series."""
        rows = [{"date": "2026-06-01", "value": 4.2}, {"date": "2026-05-01", "value": 4.1}]
        result = fred_client._level(rows)
        self.assertEqual(result["value"], 4.2)
        self.assertIsNone(result["compared_to"])

    def test_empty_returns_none(self):
        self.assertIsNone(fred_client._level([]))


class GetMacroSnapshotTests(unittest.TestCase):
    def setUp(self):
        fred_client._cache = {}
        fred_client._cache_time = 0.0

    def test_returns_none_without_api_key(self):
        with patch.object(fred_client, "FRED_API_KEY", ""):
            self.assertIsNone(fred_client.get_macro_snapshot())

    def test_builds_snapshot_with_key_configured(self):
        rows = [{"date": f"2026-{(6 - i) % 12 + 1:02d}-01", "value": 100.0 + i} for i in range(30)]
        with patch.object(fred_client, "FRED_API_KEY", "fake-key"), \
             patch.object(fred_client, "fetch_series", return_value=rows):
            snapshot = fred_client.get_macro_snapshot()
        self.assertIsNotNone(snapshot)
        self.assertIn("cpi", snapshot["readings"])
        self.assertIn("unemployment_rate", snapshot["readings"])
        # every configured series should have been attempted
        self.assertEqual(set(snapshot["readings"].keys()), set(fred_client.SERIES_CONFIG.keys()))

    def test_all_series_failing_returns_none(self):
        with patch.object(fred_client, "FRED_API_KEY", "fake-key"), \
             patch.object(fred_client, "fetch_series", return_value=[]):
            self.assertIsNone(fred_client.get_macro_snapshot())

    def test_caches_between_calls(self):
        rows = [{"date": f"2026-{(6 - i) % 12 + 1:02d}-01", "value": 100.0 + i} for i in range(30)]
        with patch.object(fred_client, "FRED_API_KEY", "fake-key"), \
             patch.object(fred_client, "fetch_series", return_value=rows) as mock_fetch:
            fred_client.get_macro_snapshot()
            calls_after_first = mock_fetch.call_count
            fred_client.get_macro_snapshot()
            self.assertEqual(mock_fetch.call_count, calls_after_first)


if __name__ == "__main__":
    unittest.main()
