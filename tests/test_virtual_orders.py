"""A trade thesis is not a fill.

The paper book created positions directly from signals: if the engine
decided to trade, a position existed, at the price it wanted, in the size
it wanted. Nothing could miss, be rejected, fill halfway, or cost more than
the model assumed.

A system trained that way learns a market that does not exist. It cannot
learn that its edge evaporates when it crosses a spread, that its limits sit
unfilled exactly when it is right, or that its stops gap. For a
short-horizon strategy those are not edge cases — they ARE the economics.

The three prompt tests this covers: a market buy must fill worse than the
decision mid (9), an untouched limit must produce no position (10), and a
gap through a stop must fill on the far side of the gap (11).
"""
import unittest

from lib.virtual_orders import (CONSERVATIVE_BAR_TOUCH, FILLED, MARKET,
                                OPTIMISTIC_BAR_TOUCH, REJECT_INVALID_SIDE,
                                REJECT_NO_QUOTE,
                                REJECT_UNSUPPORTED_INSTRUMENT, REJECTED,
                                UNFILLED, ExecutionResult, Quote,
                                VirtualOrder, execute_limit, execute_market,
                                execute_stop)


def q(bid=99.95, ask=100.05):
    return Quote(bid=bid, ask=ask, as_of="2026-08-16T12:00:00Z", source="test")


class MarketFillTests(unittest.TestCase):
    """Prompt test 9 — no magic mid fills."""

    def test_a_buy_fills_above_the_mid(self):
        r = execute_market(VirtualOrder("NVDA", "Long", 10, MARKET), q())
        self.assertEqual(r.state, FILLED)
        self.assertGreater(r.fill_price, r.decision_mid)
        self.assertGreaterEqual(r.fill_price, r.ask)

    def test_a_sell_fills_below_the_mid(self):
        r = execute_market(VirtualOrder("NVDA", "Short", 10, MARKET), q())
        self.assertLess(r.fill_price, r.decision_mid)
        self.assertLessEqual(r.fill_price, r.bid)

    def test_slippage_always_works_against_the_order(self):
        buy = execute_market(VirtualOrder("NVDA", "Long", 10, MARKET), q())
        sell = execute_market(VirtualOrder("NVDA", "Short", 10, MARKET), q())
        self.assertGreater(buy.fill_price, buy.ask)
        self.assertLess(sell.fill_price, sell.bid)

    def test_a_leveraged_short_still_sells_the_bid(self):
        r = execute_market(VirtualOrder("BTC/USD", "Short_10x", 1, MARKET), q())
        self.assertLess(r.fill_price, r.decision_mid)

    def test_spread_and_slippage_are_attributed_separately(self):
        r = execute_market(VirtualOrder("NVDA", "Long", 10, MARKET), q())
        self.assertGreater(r.spread_cost_usd, 0)
        self.assertGreater(r.slippage_usd, 0)

    def test_a_wider_spread_costs_more(self):
        tight = execute_market(VirtualOrder("NVDA", "Long", 10, MARKET),
                               q(99.99, 100.01))
        wide = execute_market(VirtualOrder("NVDA", "Long", 10, MARKET),
                              q(99.50, 100.50))
        self.assertGreater(wide.spread_cost_usd, tight.spread_cost_usd)
        self.assertGreater(wide.fill_price, tight.fill_price)

    def test_a_futures_fill_carries_the_multiplier(self):
        r = execute_market(VirtualOrder("MES=F", "Long", 1, MARKET),
                           q(4999.0, 5001.0))
        self.assertEqual(r.multiplier, 5)
        self.assertEqual(r.quantity_unit, "CONTRACTS")

    def test_no_quote_is_a_rejection_not_a_mid_fill(self):
        r = execute_market(VirtualOrder("NVDA", "Long", 10, MARKET),
                           Quote(bid=0, ask=0))
        self.assertEqual(r.state, REJECTED)
        self.assertEqual(r.reject_reason, REJECT_NO_QUOTE)
        self.assertIsNone(r.fill_price)

    def test_an_unsupported_instrument_is_refused(self):
        r = execute_market(VirtualOrder("UNKNOWN=F", "Long", 1, MARKET), q())
        self.assertEqual(r.reject_reason, REJECT_UNSUPPORTED_INSTRUMENT)

    def test_an_unreadable_side_is_refused(self):
        for d in ("Aggressive_Moon_Mode", "", None, "LONGSHORT"):
            r = execute_market(VirtualOrder("NVDA", d, 10, MARKET), q())
            self.assertEqual(r.reject_reason, REJECT_INVALID_SIDE, repr(d))


class LimitFillTests(unittest.TestCase):
    """Prompt test 10 — an untouched limit produces no position."""

    def _order(self, side="Long", px=99.0):
        from lib.virtual_orders import LIMIT
        return VirtualOrder("NVDA", side, 10, LIMIT, limit_price=px)

    def test_an_untouched_limit_is_unfilled(self):
        r = execute_limit(self._order(), bar_high=101.0, bar_low=99.5)
        self.assertEqual(r.state, UNFILLED)
        self.assertEqual(r.filled_quantity, 0.0)
        self.assertIsNone(r.fill_price)

    def test_a_bar_that_merely_TOUCHED_does_not_fill_conservatively(self):
        """The lookahead this prevents: filled at the best price on exactly
        the bars where being filled mattered most."""
        r = execute_limit(self._order(), bar_high=101.0, bar_low=99.0)
        self.assertEqual(r.state, UNFILLED)
        self.assertTrue(r.provenance["touched"])
        self.assertFalse(r.provenance["traded_through"])

    def test_a_bar_that_traded_through_fills(self):
        r = execute_limit(self._order(), bar_high=101.0, bar_low=98.5)
        self.assertEqual(r.state, FILLED)
        self.assertEqual(r.fill_price, 99.0)

    def test_the_optimistic_model_fills_on_a_touch_and_says_so(self):
        r = execute_limit(self._order(), bar_high=101.0, bar_low=99.0,
                          model=OPTIMISTIC_BAR_TOUCH)
        self.assertEqual(r.state, FILLED)
        self.assertEqual(r.price_model, OPTIMISTIC_BAR_TOUCH)

    def test_the_model_used_is_always_recorded(self):
        r = execute_limit(self._order(), bar_high=101.0, bar_low=98.0)
        self.assertEqual(r.price_model, CONSERVATIVE_BAR_TOUCH)

    def test_a_sell_limit_needs_the_bar_above_it(self):
        o = self._order(side="Short", px=101.0)
        self.assertEqual(execute_limit(o, bar_high=100.5, bar_low=99.0).state,
                         UNFILLED)
        self.assertEqual(execute_limit(o, bar_high=101.5, bar_low=99.0).state,
                         FILLED)


class StopFillTests(unittest.TestCase):
    """Prompt test 11 — a stop is a trigger, not a fill price."""

    def _order(self, side="Long", stop=98.0):
        from lib.virtual_orders import STOP
        return VirtualOrder("NVDA", side, 10, STOP, stop_price=stop)

    def test_an_untriggered_stop_is_unfilled(self):
        r = execute_stop(self._order(), bar_open=100.0, bar_high=101.0,
                         bar_low=99.0)
        self.assertEqual(r.state, UNFILLED)

    def test_a_normal_trigger_fills_near_the_stop(self):
        r = execute_stop(self._order(), bar_open=99.5, bar_high=99.5,
                         bar_low=97.5)
        self.assertEqual(r.state, FILLED)
        self.assertFalse(r.gap_through_stop)
        self.assertLessEqual(r.fill_price, 98.0)

    def test_a_gap_fills_at_the_open_not_the_stop(self):
        """THE lie this prevents. Booking 98.0 here caps every tail loss
        the strategy will ever record."""
        r = execute_stop(self._order(), bar_open=91.0, bar_high=92.0,
                         bar_low=90.0)
        self.assertEqual(r.state, FILLED)
        self.assertTrue(r.gap_through_stop)
        self.assertEqual(r.fill_price, 91.0)
        self.assertLess(r.fill_price, 98.0)

    def test_the_gap_is_flagged_so_it_can_be_measured(self):
        r = execute_stop(self._order(), bar_open=91.0, bar_high=92.0,
                         bar_low=90.0)
        self.assertTrue(r.gap_through_stop)
        self.assertGreater(r.slippage_usd, 0)

    def test_a_short_stop_gaps_upward(self):
        o = self._order(side="Short", stop=102.0)
        r = execute_stop(o, bar_open=109.0, bar_high=110.0, bar_low=108.0)
        self.assertTrue(r.gap_through_stop)
        self.assertEqual(r.fill_price, 109.0)
        self.assertGreater(r.fill_price, 102.0)

    def test_a_gapped_futures_stop_carries_the_multiplier(self):
        from lib.virtual_orders import STOP
        o = VirtualOrder("MES=F", "Long", 1, STOP, stop_price=5000.0)
        r = execute_stop(o, bar_open=4900.0, bar_high=4950.0, bar_low=4880.0)
        self.assertTrue(r.gap_through_stop)
        self.assertEqual(r.multiplier, 5)
        self.assertAlmostEqual(r.slippage_usd, 100.0 * 1 * 5)


class StateMachineTests(unittest.TestCase):
    def test_a_refusal_is_a_result_not_an_absence(self):
        r = execute_market(VirtualOrder("NVDA", "Long", 10, MARKET),
                           Quote(bid=0, ask=0))
        self.assertIsInstance(r, ExecutionResult)
        self.assertFalse(r.filled)

    def test_unfilled_is_distinct_from_rejected(self):
        from lib.virtual_orders import LIMIT
        unf = execute_limit(VirtualOrder("NVDA", "Long", 10, LIMIT,
                                         limit_price=99.0),
                            bar_high=101.0, bar_low=99.5)
        rej = execute_market(VirtualOrder("NVDA", "Long", 10, MARKET),
                             Quote(bid=0, ask=0))
        self.assertEqual(unf.state, UNFILLED)
        self.assertEqual(rej.state, REJECTED)
        self.assertNotEqual(unf.state, rej.state)

    def test_every_fill_records_the_model_that_produced_it(self):
        r = execute_market(VirtualOrder("NVDA", "Long", 10, MARKET), q())
        self.assertTrue(r.fill_model)
        self.assertTrue(r.price_model)


if __name__ == "__main__":
    unittest.main()
