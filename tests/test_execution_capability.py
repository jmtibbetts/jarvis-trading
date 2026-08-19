"""Visible depth is a floor on liquidity, not a total.

Bitnomial publishes ten levels a side and stops updating levels that fall
out of scope. Two false statements are available and both must be refused:

    "the rest definitely fills"        — the fixed-slippage model's claim
    "the rest definitely cannot fill"  — the naive depth model's claim

The truthful statement is narrower, and these tests pin it.
"""
import unittest

from lib import execution_capability as EC


def book(**over):
    """A PBTC-shaped book. Sizes are CONTRACTS."""
    base = dict(
        state="OK", age_s=0.2, market_state="Open",
        depth_asks=[(12860, 50), (12861, 100), (12863, 60)],
        depth_bids=[(12855, 50), (12854, 50), (12853, 40)],
    )
    base.update(over)
    return base


def assess(qty, side=EC.BUY, **over):
    return EC.assess(side=side, risk_authorized_qty=qty, book=book(**over),
                     instrument_id="PBTCUCZ50", quantity_unit="CONTRACTS",
                     multiplier=0.01)


class WithinVisibleDepthTests(unittest.TestCase):

    def test_a_small_order_is_fully_supported(self):
        c = assess(30)
        self.assertEqual(c.state, EC.FULLY_SUPPORTED_BY_VISIBLE_DEPTH)
        self.assertEqual(c.final_submittable_qty, 30)
        self.assertEqual(c.shrink_qty, 0.0)
        self.assertTrue(c.executable)

    def test_an_order_exactly_at_visible_depth_is_supported(self):
        c = assess(210)                    # 50+100+60
        self.assertEqual(c.state, EC.FULLY_SUPPORTED_BY_VISIBLE_DEPTH)
        self.assertEqual(c.final_submittable_qty, 210)

    def test_the_levels_required_are_reported(self):
        c = assess(120)                    # spans two levels
        self.assertEqual(c.levels_required, 2)


class BeyondVisibleDepthTests(unittest.TestCase):

    def test_the_remainder_is_declared_UNKNOWN_not_unfillable(self):
        c = assess(500)
        self.assertEqual(c.state,
                         EC.VISIBLE_DEPTH_EXHAUSTED_REMAINDER_UNKNOWN)
        self.assertEqual(c.final_submittable_qty, 210)
        self.assertEqual(c.shrink_qty, 290)
        # The wording matters as much as the number: nothing may read this
        # as proof the rest cannot fill.
        self.assertIn("not known to be", c.detail.lower())
        self.assertNotIn("cannot fill in reality", c.detail.lower())

    def test_it_is_still_executable_at_the_reduced_quantity(self):
        c = assess(500)
        self.assertTrue(c.executable,
                        "a shrunk order is still a valid order")

    def test_an_empty_side_is_not_executable(self):
        c = assess(10, depth_asks=[])
        self.assertEqual(c.state, EC.NO_EXECUTABLE_VISIBLE_LIQUIDITY)
        self.assertFalse(c.executable)


class RiskIsTheCeilingTests(unittest.TestCase):
    """The one-way valve: execution may shrink risk, never enlarge it."""

    def test_a_deep_book_does_not_enlarge_the_order(self):
        c = assess(5, depth_asks=[(12860, 100_000)])
        self.assertEqual(c.final_submittable_qty, 5)
        self.assertLessEqual(c.final_submittable_qty, c.risk_authorized_qty)

    def test_the_valve_holds_across_every_shape(self):
        for qty in (1, 10, 210, 211, 5_000):
            for asks in ([(12860, 1)], [(12860, 10_000)],
                         [(12860, 50), (12861, 100)]):
                with self.subTest(qty=qty, asks=asks):
                    c = assess(qty, depth_asks=asks)
                    self.assertLessEqual(c.final_submittable_qty, qty)

    def test_zero_authorization_yields_nothing(self):
        c = assess(0)
        self.assertFalse(c.executable)
        self.assertEqual(c.final_submittable_qty, 0)


class BookQualityTests(unittest.TestCase):

    def test_a_stale_book_cannot_size_an_order(self):
        c = assess(10, age_s=30.0)
        self.assertEqual(c.state, EC.STALE_BOOK)
        self.assertFalse(c.executable)
        self.assertEqual(c.shrink_qty, 10)

    def test_a_book_with_no_age_is_treated_as_stale(self):
        c = assess(10, age_s=None)
        self.assertEqual(c.state, EC.STALE_BOOK)

    def test_a_desynced_book_cannot_size_an_order(self):
        c = assess(10, state="DESYNCED")
        self.assertEqual(c.state, EC.INVALID_BOOK)
        self.assertFalse(c.executable)

    def test_no_book_at_all(self):
        c = EC.assess(side=EC.BUY, risk_authorized_qty=10, book=None)
        self.assertEqual(c.state, EC.NO_MARKET_DATA)
        self.assertFalse(c.executable)


class SideCorrectnessTests(unittest.TestCase):

    def test_a_buy_reads_asks_and_a_sell_reads_bids(self):
        buy = assess(200, side=EC.BUY)      # asks total 210
        sell = assess(200, side=EC.SELL)    # bids total 140
        self.assertEqual(buy.state, EC.FULLY_SUPPORTED_BY_VISIBLE_DEPTH)
        self.assertEqual(sell.state,
                         EC.VISIBLE_DEPTH_EXHAUSTED_REMAINDER_UNKNOWN)
        self.assertEqual(sell.final_submittable_qty, 140)


class ProvenanceTests(unittest.TestCase):

    def test_the_decision_is_recordable(self):
        p = assess(500).as_provenance()
        for key in ("capability_state", "risk_authorized_qty",
                    "visible_executable_qty", "final_submittable_qty",
                    "shrink_qty", "shrink_reason", "book_age_s",
                    "instrument_id", "quantity_unit", "multiplier"):
            self.assertIn(key, p)
        self.assertEqual(p["quantity_unit"], "CONTRACTS")
        self.assertEqual(p["multiplier"], 0.01)

    def test_the_multiplier_is_carried_but_never_applied(self):
        """Quantities stay in the venue's own unit. Applying the multiplier
        here would silently convert contracts to underlying and reintroduce
        the exact 100x class of defect this project already paid for."""
        c = assess(30)
        self.assertEqual(c.final_submittable_qty, 30)     # not 0.3
        self.assertEqual(c.multiplier, 0.01)


class NoInventedParticipationFactorTests(unittest.TestCase):
    """A '25% of visible depth' rule would be another 0.21% — a number with
    no measurement behind it. The cap is the visible book itself."""

    def test_the_cap_is_exactly_the_visible_book(self):
        c = assess(1_000)
        self.assertEqual(c.final_submittable_qty, c.visible_executable_qty)


if __name__ == "__main__":
    unittest.main()


class ProductsWithoutADepthFeedTests(unittest.TestCase):
    """Equities and spot pairs publish no ladder. Refusing them because
    Bitnomial does not quote them would be the same category error as
    pricing a perpetual from spot — silence is not a liquidity finding."""

    def test_a_product_with_no_ladder_is_not_refused(self):
        c = EC.assess(side=EC.BUY, risk_authorized_qty=100, book=None,
                      instrument_id=None, quantity_unit="SHARES",
                      multiplier=1.0, expects_depth=False)
        self.assertEqual(c.state, EC.DEPTH_NOT_PUBLISHED)
        self.assertTrue(c.executable)
        self.assertEqual(c.final_submittable_qty, 100)
        self.assertEqual(c.shrink_qty, 0.0)

    def test_a_perp_that_SHOULD_have_a_book_and_has_none_is_refused(self):
        """The distinction: an expected book that is missing is a fault, not
        an abstention."""
        c = EC.assess(side=EC.BUY, risk_authorized_qty=100, book=None,
                      instrument_id="PBTCUCZ50", quantity_unit="CONTRACTS",
                      multiplier=0.01, expects_depth=True)
        self.assertEqual(c.state, EC.NO_MARKET_DATA)
        self.assertFalse(c.executable)

    def test_abstention_never_enlarges_the_order(self):
        c = EC.assess(side=EC.BUY, risk_authorized_qty=7, book=None,
                      expects_depth=False)
        self.assertEqual(c.final_submittable_qty, 7)
