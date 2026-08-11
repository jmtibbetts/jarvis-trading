import unittest

from lib.insider_analytics import cluster_summary, group_by_ticker, rank_clusters


def _tx(ticker="AAPL", owner="A", cik="1", code="P", officer=False, value=100_000):
    return {
        "ticker": ticker, "owner_name": owner, "owner_cik": cik,
        "is_officer": officer, "transaction_code": code, "total_value": value,
    }


class ClusterSummaryTests(unittest.TestCase):
    def test_single_buyer_no_cluster_flag(self):
        summary = cluster_summary([_tx(owner="A", cik="1", code="P")])
        self.assertEqual(summary["distinct_buyers"], 1)
        self.assertNotIn("MULTIPLE_INSIDERS_BUYING", summary["flags"])

    def test_two_distinct_buyers_flags_cluster(self):
        txs = [_tx(owner="A", cik="1", code="P"), _tx(owner="B", cik="2", code="P")]
        summary = cluster_summary(txs)
        self.assertEqual(summary["distinct_buyers"], 2)
        self.assertIn("MULTIPLE_INSIDERS_BUYING", summary["flags"])
        self.assertIn("BUY_ONLY_CLUSTER", summary["flags"])

    def test_two_distinct_sellers_flags_cluster(self):
        txs = [_tx(owner="A", cik="1", code="S"), _tx(owner="B", cik="2", code="S")]
        summary = cluster_summary(txs)
        self.assertIn("MULTIPLE_INSIDERS_SELLING", summary["flags"])
        self.assertIn("SELL_ONLY_CLUSTER", summary["flags"])

    def test_officer_buy_flagged(self):
        summary = cluster_summary([_tx(owner="CEO", cik="1", code="P", officer=True)])
        self.assertIn("OFFICER_BUYING", summary["flags"])
        self.assertEqual(summary["officer_buyers"], ["CEO"])

    def test_non_officer_buy_not_flagged_as_officer(self):
        summary = cluster_summary([_tx(owner="Director", cik="1", code="P", officer=False)])
        self.assertNotIn("OFFICER_BUYING", summary["flags"])

    def test_buy_and_sell_present_no_directional_cluster_flag(self):
        txs = [_tx(owner="A", cik="1", code="P", value=50_000), _tx(owner="B", cik="2", code="S", value=30_000)]
        summary = cluster_summary(txs)
        self.assertNotIn("BUY_ONLY_CLUSTER", summary["flags"])
        self.assertNotIn("SELL_ONLY_CLUSTER", summary["flags"])
        self.assertEqual(summary["net_value"], 20_000)

    def test_same_owner_repeated_buys_counts_as_one_distinct_buyer(self):
        txs = [_tx(owner="A", cik="1", code="P", value=10_000), _tx(owner="A", cik="1", code="P", value=20_000)]
        summary = cluster_summary(txs)
        self.assertEqual(summary["distinct_buyers"], 1)
        self.assertEqual(summary["buy_count"], 2)
        self.assertNotIn("MULTIPLE_INSIDERS_BUYING", summary["flags"])

    def test_no_transactions_no_flags(self):
        summary = cluster_summary([])
        self.assertEqual(summary["flags"], [])
        self.assertEqual(summary["net_value"], 0)


class GroupByTickerTests(unittest.TestCase):
    def test_groups_correctly(self):
        txs = [_tx(ticker="AAPL"), _tx(ticker="MSFT"), _tx(ticker="AAPL")]
        groups = group_by_ticker(txs)
        self.assertEqual(len(groups["AAPL"]), 2)
        self.assertEqual(len(groups["MSFT"]), 1)

    def test_skips_transactions_missing_ticker(self):
        txs = [_tx(ticker="AAPL"), {"owner_name": "X", "transaction_code": "P"}]
        groups = group_by_ticker(txs)
        self.assertEqual(list(groups.keys()), ["AAPL"])


class RankClustersTests(unittest.TestCase):
    def test_only_flagged_tickers_included(self):
        txs = [
            _tx(ticker="AAPL", owner="A", cik="1", code="P"),
            _tx(ticker="AAPL", owner="B", cik="2", code="P"),  # 2 buyers -> flagged
            _tx(ticker="MSFT", owner="C", cik="3", code="P"),  # 1 buyer -> not flagged
        ]
        clusters = rank_clusters(txs)
        tickers = [c["ticker"] for c in clusters]
        self.assertIn("AAPL", tickers)
        self.assertNotIn("MSFT", tickers)

    def test_sorted_by_absolute_net_value_descending(self):
        txs = [
            _tx(ticker="SMALL", owner="A", cik="1", code="P", value=10_000),
            _tx(ticker="SMALL", owner="B", cik="2", code="P", value=10_000),
            _tx(ticker="BIG", owner="C", cik="3", code="P", value=1_000_000),
            _tx(ticker="BIG", owner="D", cik="4", code="P", value=1_000_000),
        ]
        clusters = rank_clusters(txs)
        self.assertEqual(clusters[0]["ticker"], "BIG")
        self.assertEqual(clusters[1]["ticker"], "SMALL")

    def test_empty_input_returns_empty_list(self):
        self.assertEqual(rank_clusters([]), [])


if __name__ == "__main__":
    unittest.main()
