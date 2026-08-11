import unittest
from datetime import date
from unittest.mock import patch

import httpx

from lib import short_interest
from lib.short_interest import (
    DAYS_TO_COVER_SENTINEL, _row_to_dict, _to_business_day, candidate_settlement_dates,
    compute_squeeze_score, discover_latest_settlement_date, fetch_symbol_short_interest,
    looks_like_fund_or_spac,
)

# Shaped exactly like the real live FINRA response captured while building
# this (AAPL @ settlement 2026-07-15).
REAL_AAPL_ROW = {
    "stockSplitFlag": None,
    "previousShortPositionQuantity": 140526320,
    "averageDailyVolumeQuantity": 47952794,
    "issueName": "Apple Inc. Common Stock",
    "currentShortPositionQuantity": 146547784,
    "changePreviousNumber": 6021464,
    "accountingYearMonthNumber": 20260715,
    "settlementDate": "2026-07-15",
    "marketClassCode": "NNM",
    "symbolCode": "AAPL",
    "daysToCoverQuantity": 3.06,
    "issuerServicesGroupExchangeCode": "R",
    "revisionFlag": None,
    "changePercent": 4.28,
}


class BusinessDayTests(unittest.TestCase):
    def test_saturday_rolls_back_to_friday(self):
        self.assertEqual(_to_business_day(date(2026, 8, 15)), date(2026, 8, 14))

    def test_sunday_rolls_back_to_friday(self):
        self.assertEqual(_to_business_day(date(2026, 8, 16)), date(2026, 8, 14))

    def test_weekday_unchanged(self):
        self.assertEqual(_to_business_day(date(2026, 8, 12)), date(2026, 8, 12))


class CandidateDatesTests(unittest.TestCase):
    def test_newest_first_and_semi_monthly(self):
        dates = candidate_settlement_dates(today=date(2026, 8, 10), months_back=2)
        self.assertEqual(dates[0], "2026-07-31")
        self.assertIn("2026-07-15", dates)

    def test_future_dates_excluded(self):
        """A settlement date later than today can't have been published yet."""
        dates = candidate_settlement_dates(today=date(2026, 8, 10), months_back=1)
        self.assertNotIn("2026-08-31", dates)
        self.assertNotIn("2026-08-14", dates)

    def test_month_rollover_into_previous_year(self):
        dates = candidate_settlement_dates(today=date(2026, 1, 20), months_back=2)
        self.assertTrue(any(d.startswith("2025-12") for d in dates))


class RowToDictTests(unittest.TestCase):
    def test_maps_real_finra_fields(self):
        row = _row_to_dict(REAL_AAPL_ROW)
        self.assertEqual(row["symbol"], "AAPL")
        self.assertEqual(row["current_short_shares"], 146547784)
        self.assertEqual(row["days_to_cover"], 3.06)
        self.assertEqual(row["change_percent"], 4.28)

    def test_missing_fields_become_none_not_zero(self):
        row = _row_to_dict({})
        self.assertIsNone(row["days_to_cover"])
        self.assertIsNone(row["current_short_shares"])


class SqueezeScoreTests(unittest.TestCase):
    def test_real_aapl_row_scores_sanely(self):
        result = compute_squeeze_score(_row_to_dict(REAL_AAPL_ROW))
        self.assertIsNotNone(result["squeeze_score"])
        self.assertTrue(0 <= result["squeeze_score"] <= 100)

    def test_high_days_to_cover_scores_higher_than_low(self):
        low = compute_squeeze_score({"days_to_cover": 1.0, "change_percent": 0.0})
        high = compute_squeeze_score({"days_to_cover": 12.0, "change_percent": 0.0})
        self.assertGreater(high["squeeze_score"], low["squeeze_score"])

    def test_rising_short_interest_scores_higher_than_covering(self):
        rising = compute_squeeze_score({"days_to_cover": 5.0, "change_percent": 40.0})
        covering = compute_squeeze_score({"days_to_cover": 5.0, "change_percent": -40.0})
        self.assertGreater(rising["squeeze_score"], covering["squeeze_score"])

    def test_days_to_cover_component_capped_at_100(self):
        result = compute_squeeze_score({"days_to_cover": 500.0, "change_percent": 0.0})
        self.assertEqual(result["days_to_cover_component"], 100.0)

    def test_no_data_gives_none_score_not_zero(self):
        """A missing score must be distinguishable from a genuine zero."""
        result = compute_squeeze_score({"days_to_cover": None, "change_percent": None})
        self.assertIsNone(result["squeeze_score"])

    def test_float_percentage_always_none_and_documented(self):
        """FINRA has no shares-outstanding field, so this must never be
        silently approximated — it stays None with an explanation."""
        result = compute_squeeze_score(_row_to_dict(REAL_AAPL_ROW))
        self.assertIsNone(result["short_interest_pct_of_float"])
        self.assertIn("unavailable", result["float_note"])

    def test_partial_data_still_scores(self):
        result = compute_squeeze_score({"days_to_cover": 8.0, "change_percent": None})
        self.assertIsNotNone(result["squeeze_score"])

    def test_scores_spread_rather_than_saturating(self):
        """Regression: an earlier 1->10 days-to-cover scale saturated, so
        every genuinely crowded name collapsed to an indistinguishable 100
        and the ranking carried no information."""
        scores = [
            compute_squeeze_score({"days_to_cover": d, "change_percent": c})["squeeze_score"]
            for d, c in ((41.3, 28.8), (22.0, 21.0), (20.7, 12.8), (20.8, 6.6))
        ]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertGreater(len(set(scores)), 1)
        self.assertTrue(all(s < 100 for s in scores))


class FundHeuristicTests(unittest.TestCase):
    def test_obvious_fund_names_filtered(self):
        for name in ("iShares Core S&P 500 ETF", "SPDR Gold Trust", "Global X Uranium", "iPath Select MLP ETN"):
            self.assertTrue(looks_like_fund_or_spac(name), name)

    def test_ordinary_common_stock_not_filtered(self):
        for name in ("Apple Inc. Common Stock", "Imperial Oil Limited", "Sangoma Technologies Corporati"):
            self.assertFalse(looks_like_fund_or_spac(name), name)

    def test_ticker_suffix_catches_names_truncated_past_the_marker(self):
        """FINRA truncates issueName to exactly 30 chars, which cuts the very
        word that identifies these instruments ("...Limited War" for a
        warrant, "Kensington Capital Acquisition" with Corp/Unit cut off).
        The Nasdaq 5th-letter convention survives truncation — verified
        against real rows while building this."""
        self.assertTrue(looks_like_fund_or_spac("Guardforce AI Co., Limited War", "GFAIW"))
        self.assertTrue(looks_like_fund_or_spac("Kensington Capital Acquisition", "KCACU"))

    def test_four_letter_ticker_not_treated_as_warrant(self):
        """The 5th-letter convention only applies to 5-character tickers —
        a normal 4-letter ticker ending in W must not be misclassified."""
        self.assertFalse(looks_like_fund_or_spac("Wendy's Company Common Stock", "WEN"))
        self.assertFalse(looks_like_fund_or_spac("Sallie Mae Common Stock", "SLMW"[:4]))

    def test_missing_name_and_symbol_is_not_filtered(self):
        self.assertFalse(looks_like_fund_or_spac(None))
        self.assertFalse(looks_like_fund_or_spac(None, None))


class DiscoverLatestTests(unittest.TestCase):
    def setUp(self):
        short_interest._latest_date_cache = {}
        short_interest._latest_date_cache_time = 0.0

    def test_returns_first_populated_candidate(self):
        """Real observed behavior: the most recent settlement date isn't
        published yet (FINRA lags ~8 business days), so discovery must fall
        through to the prior one rather than giving up."""
        def fake_post(body, client=None):
            value = body["compareFilters"][0]["fieldValue"]
            return [REAL_AAPL_ROW] if value == "2026-07-15" else []

        with patch.object(short_interest, "_post", side_effect=fake_post), \
             patch("lib.short_interest.candidate_settlement_dates", return_value=["2026-07-31", "2026-07-15"]):
            self.assertEqual(discover_latest_settlement_date(), "2026-07-15")

    def test_no_data_anywhere_returns_none(self):
        with patch.object(short_interest, "_post", return_value=[]):
            self.assertIsNone(discover_latest_settlement_date())


class FetchSymbolTests(unittest.TestCase):
    def setUp(self):
        short_interest._latest_date_cache = {}
        short_interest._latest_date_cache_time = 0.0

    def test_returns_enriched_row(self):
        with patch.object(short_interest, "_post", return_value=[REAL_AAPL_ROW]), \
             patch.object(short_interest, "discover_latest_settlement_date", return_value="2026-07-15"):
            result = fetch_symbol_short_interest("AAPL")
        self.assertEqual(result["symbol"], "AAPL")
        self.assertIn("squeeze", result)
        self.assertIsNotNone(result["reporting_lag_days"])

    def test_unknown_symbol_returns_none(self):
        with patch.object(short_interest, "_post", return_value=[]), \
             patch.object(short_interest, "discover_latest_settlement_date", return_value="2026-07-15"):
            self.assertIsNone(fetch_symbol_short_interest("NOTAREALTICKER"))


class PostErrorHandlingTests(unittest.TestCase):
    def test_204_returns_empty_list_not_exception(self):
        """FINRA answers a zero-row query with 204 and an empty body;
        calling .json() on that raises. Same trap already hit in
        lib/finra_ats.py — regression-guarded here too."""
        class FakeClient:
            def post(self, *a, **k):
                return httpx.Response(204, request=httpx.Request("POST", "https://x"))
            def close(self): pass

        self.assertEqual(short_interest._post({}, FakeClient()), [])

    def test_http_error_returns_empty_list(self):
        class FakeClient:
            def post(self, *a, **k):
                raise httpx.ConnectError("network down")
            def close(self): pass

        self.assertEqual(short_interest._post({}, FakeClient()), [])


if __name__ == "__main__":
    unittest.main()
