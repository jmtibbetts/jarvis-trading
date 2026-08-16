"""DEX execution is not CEX execution, and the book has to know it.

The failure this guards against is subtle: run swaps through the broker
paper engine and every number looks plausible. A $25,000 position in a
memecoin, 5x leverage, a short — all of it prices fine and none of it
exists. On a constant-product pool that same $25,000 into $50,000 of
liquidity is 49.9% price impact: half the stake gone on entry, before the
trade is even wrong.
"""
import unittest

from lib.dex_swap_math import (max_size_for_impact, pool_fee_bps, quote_swap,
                               round_trip_cost)


class ConstantProductMathTests(unittest.TestCase):
    def test_impact_matches_the_closed_form(self):
        """impact = A_eff / (X + A_eff), X = reserve/2."""
        q = quote_swap(1000, 50_000, fee_bps=25)
        a_eff = 1000 * (1 - 0.0025)
        expected = 100 * a_eff / (25_000 + a_eff)
        self.assertAlmostEqual(q["price_impact_pct"], expected, places=3)

    def test_impact_grows_faster_than_size(self):
        """The whole reason DEX sizing cannot come from equity alone."""
        pool = 50_000
        small = quote_swap(100, pool, fee_bps=25)["price_impact_pct"]
        big = quote_swap(5_000, pool, fee_bps=25)["price_impact_pct"]
        self.assertLess(small, 1.0)
        self.assertGreater(big, 15.0)
        self.assertGreater(big / small, 50 * 0.8)   # super-linear

    def test_a_position_that_would_destroy_itself_on_entry(self):
        """$25k into a $50k pool. The book must be able to SEE this."""
        q = quote_swap(25_000, 50_000, fee_bps=25)
        self.assertGreater(q["price_impact_pct"], 45.0)
        self.assertEqual(q["impact_severity"], "severe")

    def test_costs_are_itemised_not_lumped(self):
        """Pool fee, own-size impact and network fee have different
        remedies; one number hides which applies."""
        q = quote_swap(1000, 500_000, fee_bps=25, sol_price_usd=75)
        for key in ("pool_fee_usd", "price_impact_usd", "network_fee_usd"):
            self.assertIn(key, q)
            self.assertGreater(q[key], 0)
        self.assertAlmostEqual(
            q["total_cost_usd"],
            q["pool_fee_usd"] + q["price_impact_usd"] + q["network_fee_usd"],
            places=6)

    def test_deeper_pools_cost_less(self):
        thin = quote_swap(1000, 50_000, fee_bps=25)["total_cost_pct"]
        deep = quote_swap(1000, 5_000_000, fee_bps=25)["total_cost_pct"]
        self.assertGreater(thin, deep * 5)

    def test_round_trip_prices_the_exit_against_a_pool_you_moved(self):
        rt = round_trip_cost(1000, 50_000, fee_bps=25)
        self.assertGreater(rt["round_trip_cost_pct"], 7.0)
        self.assertGreater(rt["breakeven_move_pct"], rt["round_trip_cost_pct"])

    def test_max_size_solves_to_the_requested_impact(self):
        for reserve in (50_000, 500_000, 5_000_000):
            size = max_size_for_impact(reserve, 1.0, fee_bps=25)
            back = quote_swap(size, reserve, fee_bps=25)
            self.assertAlmostEqual(back["price_impact_pct"], 1.0, places=2)

    def test_zero_liquidity_is_refused_not_priced(self):
        self.assertFalse(quote_swap(1000, 0)["ok"])
        self.assertFalse(quote_swap(0, 50_000)["ok"])

    def test_pool_fee_lookup_by_dex(self):
        self.assertEqual(pool_fee_bps("raydium"), 25)
        self.assertEqual(pool_fee_bps("pump.fun"), 100)
        self.assertEqual(pool_fee_bps("something-unknown"), 30)

    def test_concentrated_liquidity_declares_its_own_uncertainty(self):
        """Half-of-total-reserve is right for constant product and wrong
        for DLMM/Whirlpool. The quote must say which it assumed."""
        cp = quote_swap(1000, 500_000, fee_bps=25)
        cl = quote_swap(1000, 500_000, fee_bps=25, concentrated=True)
        self.assertFalse(cp["concentrated"])
        self.assertTrue(cl["concentrated"])
        # `depth_model` is now the model NAME and `depth_confidence` states
        # how much to trust it — a concentrated pool's local depth around
        # the current tick may be nothing like half the total, in either
        # direction, so it is MODELLED_ESTIMATE rather than a measurement.
        self.assertEqual(cl["depth_model"], "CONCENTRATED_LIQUIDITY")
        self.assertEqual(cl["depth_confidence"], "MODELLED_ESTIMATE")
        self.assertIn("ROUGH", cl["provenance"])
        # Even the constant-product path declares that "half of total" is
        # an ASSUMPTION about pool balance, not something it measured.
        self.assertEqual(cp["depth_model"], "CONSTANT_PRODUCT_AMM")
        self.assertEqual(cp["depth_confidence"], "ASSUMED_BALANCED_POOL")


class DexBookRulesTests(unittest.TestCase):
    def test_size_is_bounded_by_pool_depth_before_equity(self):
        from lib.dex_paper import size_for_pool
        s = size_for_pool(50_000, 10_000, dex="raydium")
        self.assertTrue(s["ok"])
        self.assertEqual(s["bound_by"], "impact_cap")
        self.assertLess(s["size_usd"], 1_000)      # not the $10k of cash

    def test_a_deep_pool_lets_equity_bind_instead(self):
        from lib.dex_paper import size_for_pool
        s = size_for_pool(5_000_000, 10_000, dex="raydium")
        self.assertEqual(s["bound_by"], "cash")
        self.assertAlmostEqual(s["size_usd"], 10_000, places=2)

    def test_a_pool_too_thin_to_trade_is_refused_with_a_reason(self):
        from lib.dex_paper import size_for_pool
        s = size_for_pool(12_000, 10_000, dex="raydium")
        self.assertFalse(s["ok"])
        self.assertEqual(s["bound_by"], "pool_too_thin")
        self.assertIn("below the", s["reason"])

    def test_the_book_offers_no_leverage_and_no_shorts(self):
        """Not a policy choice — a constant-product pool cannot lend, so a
        book advertising either is describing a venue that does not exist."""
        from lib.dex_paper import summary
        limits = summary()["limits"]
        self.assertIn("does not lend", limits["leverage"])
        self.assertIn("spot", limits["shorting"])


if __name__ == "__main__":
    unittest.main()
