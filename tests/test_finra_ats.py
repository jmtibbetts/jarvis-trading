import unittest
from unittest.mock import MagicMock, patch

from lib import finra_ats

# Real row shapes (trimmed) — verified live against api.finra.org while
# building this: AAPL's actual ATS-combined weekly summary and per-venue
# breakdown for the week of 2026-07-20.
REAL_SYMBOL_ROW = {
    "issueSymbolIdentifier": "AAPL", "issueName": "Apple Inc. Common Stock",
    "totalWeeklyShareQuantity": 37839888, "totalWeeklyTradeCount": 676325,
    "totalNotionalSum": 12349750810.60, "weekStartDate": "2026-07-20",
    "initialPublishedDate": "2026-08-10", "tierIdentifier": "T1",
    "summaryTypeCode": "ATS_W_SMBL",
}
REAL_VENUE_ROW = {
    "issueSymbolIdentifier": "AAPL", "MPID": "UBSA", "marketParticipantName": "UBS ATS",
    "totalWeeklyShareQuantity": 6311870, "totalWeeklyTradeCount": 143534,
    "totalNotionalSum": 2058000000.0, "weekStartDate": "2026-07-20",
}


class PostTests(unittest.TestCase):
    def test_204_response_returns_empty_list_not_exception(self):
        """Regression test: FINRA returns HTTP 204 with an empty body for a
        query matching zero rows, not a 200 with []. Calling .json() on an
        empty body raises — this must be checked before parsing, or every
        'week not published yet' probe during discovery would crash."""
        mock_resp = MagicMock()
        mock_resp.status_code = 204
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.post.return_value = mock_resp
            result = finra_ats._post({}, MockClient.return_value.__enter__.return_value)
        self.assertEqual(result, [])

    def test_network_failure_returns_empty_list(self):
        client = MagicMock()
        client.post.side_effect = __import__("httpx").ConnectError("boom")
        self.assertEqual(finra_ats._post({}, client), [])

    def test_normal_200_response_parsed(self):
        client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b"[{}]"
        mock_resp.json.return_value = [REAL_SYMBOL_ROW]
        mock_resp.raise_for_status = lambda: None
        client.post.return_value = mock_resp
        result = finra_ats._post({}, client)
        self.assertEqual(result, [REAL_SYMBOL_ROW])


class DiscoverLatestWeekTests(unittest.TestCase):
    def test_walks_backward_until_populated_week_found(self):
        """Simulates the real-world pattern observed live: the two most
        recent Mondays are empty (not published yet), the third has data."""
        call_count = {"n": 0}

        def fake_post(body, client=None):
            call_count["n"] += 1
            week = next(f["fieldValue"] for f in body["compareFilters"] if f["fieldName"] == "weekStartDate")
            return [REAL_SYMBOL_ROW] if call_count["n"] >= 3 else []

        with patch.object(finra_ats, "_post", side_effect=fake_post):
            result = finra_ats.discover_latest_week(max_weeks_back=8)
        self.assertIsNotNone(result)
        self.assertEqual(call_count["n"], 3)

    def test_returns_none_if_nothing_found_within_max_weeks(self):
        with patch.object(finra_ats, "_post", return_value=[]):
            result = finra_ats.discover_latest_week(max_weeks_back=3)
        self.assertIsNone(result)


class GetTopActivityTests(unittest.TestCase):
    def setUp(self):
        finra_ats._top_activity_cache = {}
        finra_ats._top_activity_cache_time = 0.0

    def test_computes_week_over_week_change(self):
        latest = [{**REAL_SYMBOL_ROW, "totalWeeklyShareQuantity": 40_000_000}]
        prior = [{**REAL_SYMBOL_ROW, "totalWeeklyShareQuantity": 20_000_000}]

        with patch.object(finra_ats, "discover_latest_week", return_value="2026-07-20"), \
             patch.object(finra_ats, "fetch_week_symbols", side_effect=[latest, prior]):
            snapshot = finra_ats.get_top_activity(limit=10)

        self.assertIsNotNone(snapshot)
        row = snapshot["symbols"][0]
        self.assertEqual(row["symbol"], "AAPL")
        self.assertEqual(row["wow_pct"], 100.0)

    def test_missing_prior_week_gives_none_wow_not_error(self):
        latest = [REAL_SYMBOL_ROW]
        with patch.object(finra_ats, "discover_latest_week", return_value="2026-07-20"), \
             patch.object(finra_ats, "fetch_week_symbols", side_effect=[latest, []]):
            snapshot = finra_ats.get_top_activity(limit=10)
        self.assertIsNone(snapshot["symbols"][0]["wow_pct"])

    def test_ranked_by_share_volume_descending(self):
        latest = [
            {**REAL_SYMBOL_ROW, "issueSymbolIdentifier": "SMALL", "totalWeeklyShareQuantity": 1000},
            {**REAL_SYMBOL_ROW, "issueSymbolIdentifier": "BIG", "totalWeeklyShareQuantity": 9_000_000},
        ]
        with patch.object(finra_ats, "discover_latest_week", return_value="2026-07-20"), \
             patch.object(finra_ats, "fetch_week_symbols", side_effect=[latest, []]):
            snapshot = finra_ats.get_top_activity(limit=10)
        self.assertEqual(snapshot["symbols"][0]["symbol"], "BIG")

    def test_no_latest_week_returns_none(self):
        with patch.object(finra_ats, "discover_latest_week", return_value=None):
            self.assertIsNone(finra_ats.get_top_activity())

    def test_caches_between_calls(self):
        with patch.object(finra_ats, "discover_latest_week", return_value="2026-07-20") as mock_discover, \
             patch.object(finra_ats, "fetch_week_symbols", side_effect=[[REAL_SYMBOL_ROW], [], [REAL_SYMBOL_ROW], []]):
            finra_ats.get_top_activity(limit=10)
            finra_ats.get_top_activity(limit=10)
        self.assertEqual(mock_discover.call_count, 1)


class ReportingDelayTests(unittest.TestCase):
    def test_computes_delay_in_days(self):
        delay = finra_ats._reporting_delay_days("T1", "2026-07-20", "2026-08-10")
        self.assertEqual(delay, 21)

    def test_missing_published_date_returns_none(self):
        self.assertIsNone(finra_ats._reporting_delay_days("T1", "2026-07-20", None))


class GetSymbolVenuesTests(unittest.TestCase):
    def test_returns_venues_sorted_by_volume(self):
        rows = [
            {**REAL_VENUE_ROW, "MPID": "SMALL", "marketParticipantName": "Small Venue", "totalWeeklyShareQuantity": 100},
            REAL_VENUE_ROW,  # UBS ATS, 6.3M shares
        ]
        with patch.object(finra_ats, "fetch_symbol_venues", return_value=sorted(rows, key=lambda r: -r["totalWeeklyShareQuantity"])):
            result = finra_ats.get_symbol_venues("AAPL", week_start="2026-07-20")
        self.assertEqual(result["venues"][0]["mpid"], "UBSA")
        self.assertEqual(result["venues"][0]["name"], "UBS ATS")

    def test_no_week_and_discovery_fails_returns_none(self):
        with patch.object(finra_ats, "discover_latest_week", return_value=None):
            self.assertIsNone(finra_ats.get_symbol_venues("AAPL"))


if __name__ == "__main__":
    unittest.main()
