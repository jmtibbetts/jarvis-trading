import unittest

from lib.orderbook_stream import OrderBook


class ApplySnapshotTests(unittest.TestCase):
    def test_stores_bids_and_asks(self):
        book = OrderBook()
        book.apply_snapshot([(100.0, 1.0), (99.0, 2.0)], [(101.0, 1.5), (102.0, 0.5)])
        self.assertEqual(book.bids, {100.0: 1.0, 99.0: 2.0})
        self.assertEqual(book.asks, {101.0: 1.5, 102.0: 0.5})

    def test_zero_size_levels_dropped_on_snapshot(self):
        """A snapshot shouldn't ever legitimately contain a zero-size level,
        but defend against it anyway — a zero-size resting order is meaningless."""
        book = OrderBook()
        book.apply_snapshot([(100.0, 1.0), (99.0, 0.0)], [(101.0, 0.0)])
        self.assertEqual(book.bids, {100.0: 1.0})
        self.assertEqual(book.asks, {})

    def test_snapshot_replaces_prior_state(self):
        book = OrderBook()
        book.apply_snapshot([(100.0, 1.0)], [(101.0, 1.0)])
        book.apply_snapshot([(200.0, 5.0)], [(201.0, 5.0)])
        self.assertEqual(book.bids, {200.0: 5.0})


class ApplyUpdateTests(unittest.TestCase):
    def test_buy_side_adds_or_updates_bid(self):
        book = OrderBook()
        book.apply_snapshot([], [])
        book.apply_update("buy", 100.0, 2.5)
        self.assertEqual(book.bids, {100.0: 2.5})

    def test_sell_side_adds_or_updates_ask(self):
        book = OrderBook()
        book.apply_snapshot([], [])
        book.apply_update("sell", 101.0, 3.0)
        self.assertEqual(book.asks, {101.0: 3.0})

    def test_zero_size_removes_the_level(self):
        """This is Coinbase's actual protocol for a canceled/filled order at
        a price level — size=0 in an l2update means 'this level is gone',
        not 'set it to zero and keep it in the book'."""
        book = OrderBook()
        book.apply_snapshot([(100.0, 1.0)], [])
        book.apply_update("buy", 100.0, 0.0)
        self.assertEqual(book.bids, {})

    def test_removing_nonexistent_level_is_a_noop(self):
        book = OrderBook()
        book.apply_update("buy", 999.0, 0.0)
        self.assertEqual(book.bids, {})


class TopLevelsTests(unittest.TestCase):
    def test_bids_sorted_descending_asks_ascending(self):
        """Bids ranked best-first means highest price; asks ranked
        best-first means lowest price — get this backwards and every
        'best bid/ask' downstream calculation is wrong."""
        book = OrderBook()
        book.apply_snapshot([(100.0, 1), (102.0, 1), (98.0, 1)], [(105.0, 1), (103.0, 1), (110.0, 1)])
        levels = book.top_levels(10)
        self.assertEqual([p for p, _ in levels["bids"]], [102.0, 100.0, 98.0])
        self.assertEqual([p for p, _ in levels["asks"]], [103.0, 105.0, 110.0])

    def test_respects_n_limit(self):
        book = OrderBook()
        book.apply_snapshot([(float(i), 1.0) for i in range(50)], [])
        levels = book.top_levels(5)
        self.assertEqual(len(levels["bids"]), 5)

    def test_empty_book_returns_empty_levels(self):
        book = OrderBook()
        levels = book.top_levels(10)
        self.assertEqual(levels["bids"], [])
        self.assertEqual(levels["asks"], [])
        self.assertIsNone(levels["best_bid"])
        self.assertIsNone(levels["spread"])


class ComputeStatsTests(unittest.TestCase):
    def test_spread_and_bps_computed_correctly(self):
        stats = OrderBook.compute_stats([(100.0, 1.0)], [(101.0, 1.0)])
        self.assertEqual(stats["best_bid"], 100.0)
        self.assertEqual(stats["best_ask"], 101.0)
        self.assertEqual(stats["spread"], 1.0)
        self.assertAlmostEqual(stats["spread_bps"], 100.0, places=1)  # 1/100 = 1% = 100bps

    def test_imbalance_positive_when_bid_heavy(self):
        stats = OrderBook.compute_stats([(100.0, 9.0)], [(101.0, 1.0)])
        self.assertEqual(stats["imbalance"], 0.8)  # (9-1)/10

    def test_imbalance_negative_when_ask_heavy(self):
        stats = OrderBook.compute_stats([(100.0, 1.0)], [(101.0, 9.0)])
        self.assertEqual(stats["imbalance"], -0.8)

    def test_balanced_book_has_zero_imbalance(self):
        stats = OrderBook.compute_stats([(100.0, 5.0)], [(101.0, 5.0)])
        self.assertEqual(stats["imbalance"], 0.0)

    def test_one_sided_book_has_no_spread(self):
        """No bid side at all means no meaningful spread (can't compute
        ask-minus-bid), but imbalance is still well-defined — a book with
        zero bid depth and nonzero ask depth is maximally ask-heavy (-1.0)."""
        stats = OrderBook.compute_stats([], [(101.0, 1.0)])
        self.assertIsNone(stats["spread"])
        self.assertEqual(stats["imbalance"], -1.0)

    def test_empty_book_has_none_stats_not_exception(self):
        stats = OrderBook.compute_stats([], [])
        self.assertIsNone(stats["best_bid"])
        self.assertIsNone(stats["best_ask"])
        self.assertIsNone(stats["spread"])
        self.assertIsNone(stats["imbalance"])


if __name__ == "__main__":
    unittest.main()
