"""Two execution assumptions, one observation, compared honestly.

The active simulator prices every fill as top-of-book plus a flat 0.21%,
regardless of order size. The number came from 50 real Alpaca broker fills —
external observations, not simulator output, so NOT circular — but they were
EQUITY and crypto SPOT fills at a retail broker, and they are now the
assumption for CONTRACT PERPETUALS on Bitnomial. These tests pin the
difference that matters: what happens as the order gets large relative to
the visible book.
"""
import unittest

from lib import fill_models as FM


def book(**over):
    """A PBTC-shaped book: ticks of 5.0 USD, 0.01 BTC per contract."""
    base = dict(
        instrument_id="PBTCUCZ50", price_increment=5.0, contract_size=0.01,
        # raw ticks; asks ascending, bids descending, sizes in CONTRACTS
        asks=[(12860, 50), (12861, 100), (12863, 60), (12883, 5)],
        bids=[(12855, 50), (12854, 50), (12853, 50), (12850, 60)],
        state="OK")
    base.update(over)
    return FM.BookSnapshot(**base)


class TheUnitsAreContractsTests(unittest.TestCase):

    def test_prices_convert_through_the_tick(self):
        b = book()
        self.assertAlmostEqual(b.best_ask(), 64_300.0)   # 12860 * 5.0
        self.assertAlmostEqual(b.best_bid(), 64_275.0)   # 12855 * 5.0

    def test_visible_depth_is_counted_in_contracts(self):
        r = FM.depth_vwap(book(), FM.BUY, 10)
        self.assertEqual(r.visible_contracts, 215.0)     # 50+100+60+5


class TheFixedModelIgnoresSizeTests(unittest.TestCase):
    """Its defining behaviour, and the reason it is under test."""

    def test_a_tiny_order_and_a_huge_one_get_the_same_price(self):
        small = FM.top_of_book_fixed_slippage(book(), FM.BUY, 1)
        huge = FM.top_of_book_fixed_slippage(book(), FM.BUY, 10_000)
        self.assertEqual(small.fill_price, huge.fill_price)
        self.assertEqual(huge.state, FM.FILLED,
                         "the fixed model cannot report a partial fill")
        self.assertEqual(huge.unfilled_contracts, 0.0)

    def test_it_fills_beyond_all_visible_liquidity(self):
        """215 contracts are visible; it fills 10,000 without noticing."""
        r = FM.top_of_book_fixed_slippage(book(), FM.BUY, 10_000)
        self.assertGreater(r.filled_contracts, r.visible_contracts * 40)

    def test_the_adverse_direction_is_correct_on_both_sides(self):
        buy = FM.top_of_book_fixed_slippage(book(), FM.BUY, 1)
        sell = FM.top_of_book_fixed_slippage(book(), FM.SELL, 1)
        self.assertGreater(buy.fill_price, buy.reference_price)
        self.assertLess(sell.fill_price, sell.reference_price)
        self.assertGreater(buy.effective_bps, 0)
        self.assertGreater(sell.effective_bps, 0)


class TheDepthModelWalksTheBookTests(unittest.TestCase):

    def test_an_order_inside_the_top_level_pays_the_touch(self):
        r = FM.depth_vwap(book(), FM.BUY, 20)
        self.assertEqual(r.state, FM.FILLED)
        self.assertAlmostEqual(r.vwap, 64_300.0)
        self.assertEqual(r.levels_consumed, 1)
        self.assertAlmostEqual(r.effective_bps, 0.0)

    def test_an_order_through_two_levels_pays_a_blend(self):
        # 50 @ 64300 + 30 @ 64305  ->  (50*64300 + 30*64305) / 80
        r = FM.depth_vwap(book(), FM.BUY, 80)
        expected = (50 * 64_300.0 + 30 * 64_305.0) / 80
        self.assertEqual(r.state, FM.FILLED)
        self.assertAlmostEqual(r.vwap, expected, places=6)
        self.assertEqual(r.levels_consumed, 2)
        self.assertGreater(r.effective_bps, 0)

    def test_a_sell_walks_the_bids_downward(self):
        r = FM.depth_vwap(book(), FM.SELL, 80)
        expected = (50 * 64_275.0 + 30 * 64_270.0) / 80
        self.assertAlmostEqual(r.vwap, expected, places=6)
        self.assertGreater(r.effective_bps, 0, "a sell through the book must "
                                               "be adverse, not favourable")

    def test_it_never_invents_liquidity_past_the_visible_book(self):
        """The load-bearing property. Bitnomial publishes ten levels and
        stops updating levels that fall out of scope, so the visible total
        is a FLOOR — filling the remainder at the last price would invent
        the most expensive part of the order, in the flattering direction."""
        r = FM.depth_vwap(book(), FM.BUY, 1_000)
        self.assertEqual(r.state, FM.PARTIALLY_FILLED)
        self.assertAlmostEqual(r.filled_contracts, 215.0)
        self.assertAlmostEqual(r.unfilled_contracts, 785.0)
        self.assertIn("rather than invented", r.detail)

    def test_an_empty_side_is_not_a_fill(self):
        r = FM.depth_vwap(book(asks=[]), FM.BUY, 5)
        self.assertIn(r.state, (FM.NO_BOOK, FM.INSUFFICIENT_VISIBLE_DEPTH))
        self.assertEqual(r.filled_contracts, 0.0)

    def test_zero_size_levels_are_skipped_not_counted(self):
        r = FM.depth_vwap(book(asks=[(12860, 0), (12861, 40)]), FM.BUY, 40)
        self.assertEqual(r.state, FM.FILLED)
        self.assertAlmostEqual(r.vwap, 64_305.0)


class TheModelsDisagreeWhereItMattersTests(unittest.TestCase):
    """The whole point of running both."""

    def test_they_agree_closely_on_a_small_order(self):
        a = FM.top_of_book_fixed_slippage(book(), FM.BUY, 5)
        b = FM.depth_vwap(book(), FM.BUY, 5)
        self.assertLess(abs(a.effective_bps - b.effective_bps), 25.0)

    def test_they_disagree_completely_on_a_large_one(self):
        a = FM.top_of_book_fixed_slippage(book(), FM.BUY, 1_000)
        b = FM.depth_vwap(book(), FM.BUY, 1_000)
        self.assertEqual(a.state, FM.FILLED)
        self.assertEqual(b.state, FM.PARTIALLY_FILLED)
        self.assertAlmostEqual(a.filled_contracts, 1_000.0)
        self.assertAlmostEqual(b.filled_contracts, 215.0)

    def test_one_observation_produces_one_result_per_model(self):
        results = FM.run_all(book(), FM.BUY, 60)
        self.assertEqual(len(results), 2)
        self.assertEqual(len({r.model for r in results}), 2)
        for r in results:
            self.assertEqual(r.requested_contracts, 60)

    def test_an_unusable_book_produces_no_fill_from_either_model(self):
        for r in FM.run_all(book(state="DESYNCED"), FM.BUY, 10):
            self.assertEqual(r.state, FM.NO_BOOK)
            self.assertEqual(r.filled_contracts, 0.0)


if __name__ == "__main__":
    unittest.main()
