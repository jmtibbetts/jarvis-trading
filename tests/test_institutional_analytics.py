import unittest

from lib.institutional_analytics import (
    aggregate_by_ticker, compare_quarters, compute_institutional_component,
)


def _h(ticker, filer, shares, value=1_000_000.0, issuer="Some Corp"):
    return {"ticker": ticker, "filer_name": filer, "shares": shares, "value_usd": value, "issuer_name": issuer}


class AggregateByTickerTests(unittest.TestCase):
    def test_combines_multiple_managers(self):
        result = aggregate_by_ticker([
            _h("AAPL", "Fund A", 100), _h("AAPL", "Fund B", 250), _h("MSFT", "Fund A", 50),
        ])
        self.assertEqual(result["AAPL"]["holder_count"], 2)
        self.assertEqual(result["AAPL"]["total_shares"], 350)
        self.assertEqual(result["MSFT"]["holder_count"], 1)

    def test_unresolved_cusips_excluded_not_guessed(self):
        """A holding whose CUSIP didn't resolve to a ticker must be dropped
        from ticker-level aggregation rather than bucketed under a guess."""
        result = aggregate_by_ticker([_h("AAPL", "Fund A", 100), {"ticker": None, "shares": 999, "value_usd": 5}])
        self.assertEqual(list(result.keys()), ["AAPL"])
        self.assertEqual(result["AAPL"]["total_shares"], 100)

    def test_holders_sorted_by_value_desc(self):
        result = aggregate_by_ticker([
            _h("AAPL", "Small", 10, value=1000), _h("AAPL", "Big", 20, value=99999),
        ])
        self.assertEqual(result["AAPL"]["holders"][0]["filer_name"], "Big")

    def test_empty_input(self):
        self.assertEqual(aggregate_by_ticker([]), {})


class CompareQuartersTests(unittest.TestCase):
    def test_no_prior_quarter_is_flagged_not_treated_as_new_accumulation(self):
        """The single most important correctness guarantee here: on first
        ingestion there is no prior quarter, and every position must NOT be
        reported as freshly accumulated."""
        current = aggregate_by_ticker([_h("AAPL", "Fund A", 100)])
        rows = compare_quarters(current, {})
        self.assertTrue(rows[0]["insufficient_history"])
        self.assertEqual(rows[0]["status"], "no_prior_quarter")
        self.assertIsNone(rows[0]["share_delta"])
        self.assertIsNone(rows[0]["share_change_pct"])

    def test_increased_position(self):
        current = aggregate_by_ticker([_h("AAPL", "Fund A", 150)])
        prior = aggregate_by_ticker([_h("AAPL", "Fund A", 100)])
        rows = compare_quarters(current, prior)
        self.assertEqual(rows[0]["status"], "increased")
        self.assertEqual(rows[0]["share_delta"], 50)
        self.assertEqual(rows[0]["share_change_pct"], 50.0)

    def test_decreased_position(self):
        current = aggregate_by_ticker([_h("AAPL", "Fund A", 40)])
        prior = aggregate_by_ticker([_h("AAPL", "Fund A", 100)])
        rows = compare_quarters(current, prior)
        self.assertEqual(rows[0]["status"], "decreased")
        self.assertEqual(rows[0]["share_change_pct"], -60.0)

    def test_unchanged_position(self):
        current = aggregate_by_ticker([_h("AAPL", "Fund A", 100)])
        prior = aggregate_by_ticker([_h("AAPL", "Fund A", 100)])
        rows = compare_quarters(current, prior)
        self.assertEqual(rows[0]["status"], "unchanged")
        self.assertEqual(rows[0]["share_change_pct"], 0.0)

    def test_newly_reported_when_prior_quarter_exists_but_lacks_ticker(self):
        current = aggregate_by_ticker([_h("NVDA", "Fund A", 100)])
        prior = aggregate_by_ticker([_h("AAPL", "Fund A", 100)])
        row = next(r for r in compare_quarters(current, prior) if r["ticker"] == "NVDA")
        self.assertEqual(row["status"], "newly_reported")
        self.assertFalse(row["insufficient_history"])
        self.assertEqual(row["prior_shares"], 0.0)

    def test_holder_delta_tracked(self):
        current = aggregate_by_ticker([_h("AAPL", "A", 50), _h("AAPL", "B", 50), _h("AAPL", "C", 50)])
        prior = aggregate_by_ticker([_h("AAPL", "A", 100)])
        rows = compare_quarters(current, prior)
        self.assertEqual(rows[0]["holder_delta"], 2)

    def test_zero_prior_shares_does_not_divide_by_zero(self):
        current = aggregate_by_ticker([_h("AAPL", "A", 100)])
        prior = aggregate_by_ticker([_h("AAPL", "A", 0)])
        rows = compare_quarters(current, prior)
        self.assertIsNone(rows[0]["share_change_pct"])


class InstitutionalComponentTests(unittest.TestCase):
    def test_none_row_returns_none(self):
        self.assertIsNone(compute_institutional_component(None))

    def test_insufficient_history_abstains_rather_than_scoring_neutral(self):
        row = {"insufficient_history": True, "share_change_pct": None}
        self.assertIsNone(compute_institutional_component(row))

    def test_missing_pct_returns_none(self):
        row = {"insufficient_history": False, "share_change_pct": None}
        self.assertIsNone(compute_institutional_component(row))

    def test_accumulation_is_positive(self):
        row = {"insufficient_history": False, "share_change_pct": 25.0, "holder_count": 5, "status": "increased"}
        self.assertGreater(compute_institutional_component(row)["score"], 0)

    def test_distribution_is_negative(self):
        row = {"insufficient_history": False, "share_change_pct": -25.0, "holder_count": 5, "status": "decreased"}
        self.assertLess(compute_institutional_component(row)["score"], 0)

    def test_score_caps_at_100(self):
        row = {"insufficient_history": False, "share_change_pct": 500.0, "holder_count": 5, "status": "increased"}
        self.assertEqual(compute_institutional_component(row)["score"], 100.0)

    def test_is_less_sensitive_than_insider_component_scaling(self):
        """A 10% 13F share change should score lower than the same nominal
        magnitude would under the insider component's tighter scaling —
        13F position changes are lumpier and less informative per unit."""
        row = {"insufficient_history": False, "share_change_pct": 10.0, "holder_count": 3, "status": "increased"}
        self.assertAlmostEqual(compute_institutional_component(row)["score"], 20.0, places=1)


if __name__ == "__main__":
    unittest.main()
