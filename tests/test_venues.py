"""Venue-aware costs and order validity.

The cost model previously assumed one crypto fee for every venue — Alpaca's
0.25% taker. Kraken's base tier is 0.40%, verified live from their public
AssetPairs endpoint. Since P0 rejects trades whose costs exceed 0.50R, a
fee wrong by 60% silently moves the line between tradeable and not.
"""
import unittest

from lib.venues import (fee_for, validate_order, is_tradeable_on,
                        kraken_pair_specs, VENUE_FEES)
from lib.transaction_costs import estimate_costs, min_viable_stop_pct


class FeeScheduleTests(unittest.TestCase):
    def test_kraken_is_dearer_than_alpaca_at_base_volume(self):
        self.assertGreater(fee_for("kraken")[0], fee_for("alpaca")[0])

    def test_maker_is_always_cheaper_than_taker(self):
        for venue in ("alpaca", "kraken"):
            self.assertLess(fee_for(venue, maker=True)[0], fee_for(venue)[0])

    def test_volume_tiers_step_down_and_never_up(self):
        rates = [fee_for("kraken", volume_30d=v)[0]
                 for v in (0, 10_000, 50_000, 100_000, 1_000_000)]
        self.assertEqual(rates, sorted(rates, reverse=True))

    def test_default_tier_is_the_expensive_one(self):
        """Assuming a discount you have not earned understates cost."""
        self.assertEqual(fee_for("kraken")[0], VENUE_FEES["kraken"]["crypto"]["taker"][0][1])

    def test_unknown_venue_is_never_free(self):
        rate, why = fee_for("someexchange")
        self.assertGreater(rate, 0)
        self.assertIn("unknown venue", why)

    def test_explanation_names_venue_and_tier(self):
        _, why = fee_for("kraken", volume_30d=100_000)
        self.assertIn("kraken", why)
        self.assertIn("100,000", why)


class CostModelIsVenueAwareTests(unittest.TestCase):
    def test_same_trade_costs_more_on_the_dearer_venue(self):
        a = estimate_costs("BTC/USD", 100.0, 98.0, venue="alpaca")
        k = estimate_costs("BTC/USD", 100.0, 98.0, venue="kraken")
        self.assertGreater(k["total_r"], a["total_r"])

    def test_min_viable_stop_widens_on_the_dearer_venue(self):
        self.assertGreater(min_viable_stop_pct("BTC/USD", venue="kraken"),
                           min_viable_stop_pct("BTC/USD", venue="alpaca"))

    def test_result_records_which_venue_priced_it(self):
        self.assertEqual(estimate_costs("BTC/USD", 100.0, 98.0, venue="kraken")["fee_venue"],
                         "kraken")

    def test_equities_are_unaffected_by_crypto_venue(self):
        self.assertEqual(estimate_costs("NVDA", 100.0, 98.0, venue="kraken")["fees_pct"], 0.0)


class OrderValidityTests(unittest.TestCase):
    """Live checks against Kraken's published pair specs."""

    def test_btc_is_listed(self):
        ok, why = is_tradeable_on("kraken", "BTC/USD")
        self.assertTrue(ok, why)

    def test_nonsense_symbol_is_not_listed(self):
        ok, _ = is_tradeable_on("kraken", "NOTACOIN/USD")
        self.assertFalse(ok)

    def test_size_below_the_minimum_is_refused(self):
        out = validate_order("kraken", "BTC/USD", 0.000001, 95000.0)
        self.assertFalse(out["ok"])
        self.assertIn("below kraken minimum", out["reason"])

    def test_price_off_the_tick_grid_is_refused_and_names_the_valid_price(self):
        out = validate_order("kraken", "BTC/USD", 0.01, 95000.037)
        self.assertFalse(out["ok"])
        self.assertIn("off the", out["reason"])
        self.assertIn("nearest valid", out["reason"])

    def test_a_valid_order_passes(self):
        self.assertTrue(validate_order("kraken", "BTC/USD", 0.01, 95000.0)["ok"])

    def test_specs_expose_margin_leverage(self):
        spec = kraken_pair_specs("BTC/USD")
        self.assertIsNotNone(spec)
        self.assertGreaterEqual(spec["max_leverage"], 2)


if __name__ == "__main__":
    unittest.main()
