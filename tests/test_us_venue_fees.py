"""US Kraken Pro pricing: per contract, not per cent.

Kraken states that the international Futures maker/taker tiers (0.020% /
0.050%) do NOT apply to US customers. US perpetuals list through Bitnomial
at a flat $0.15/contract/side all-in, and CME products carry a per-contract
Kraken commission plus exchange, NFA and clearing charges.

The two schedules have OPPOSITE shapes, which is why using the wrong one is
not a small error:

  percentage  scale-neutral — 0.05% costs the same proportion at any size
  per-contract REGRESSIVE — $0.30 is 0.30% of a $100 position and 0.003%
              of a $10,000 one

So the international schedule understates cost on small positions and
overstates it on large ones for a US account.
"""
import unittest

from lib.venues import (us_perpetual_fee, us_futures_fee, us_fee_as_pct_of_notional,
                        futures_fee_for, US_PERPETUAL_FEE_PER_SIDE)


class USPerpetualFeeTests(unittest.TestCase):
    def test_round_trip_is_twice_the_per_side(self):
        fee, _ = us_perpetual_fee(1)
        self.assertAlmostEqual(fee, US_PERPETUAL_FEE_PER_SIDE * 2)

    def test_fee_scales_with_contract_count_not_price(self):
        one, _ = us_perpetual_fee(1)
        ten, _ = us_perpetual_fee(10)
        self.assertAlmostEqual(ten, one * 10)

    def test_no_maker_taker_distinction_exists(self):
        """The US schedule has one price; a signature taking `maker` would
        imply a choice that is not offered."""
        import inspect
        self.assertNotIn("maker", inspect.signature(us_perpetual_fee).parameters)

    def test_explanation_names_the_components(self):
        _, why = us_perpetual_fee(1)
        for part in ("Kraken", "exchange/clearing", "NFA"):
            self.assertIn(part, why)


class RegressiveShapeTests(unittest.TestCase):
    """The property that makes per-contract pricing different in kind."""

    def test_same_fee_is_a_smaller_percentage_on_a_larger_position(self):
        fee, _ = us_perpetual_fee(1)
        small = us_fee_as_pct_of_notional(100, fee)
        large = us_fee_as_pct_of_notional(100_000, fee)
        self.assertGreater(small, large * 100)

    def test_tiny_positions_are_punished(self):
        """0.30% on a $100 position — worse than any spot percentage tier."""
        fee, _ = us_perpetual_fee(1)
        self.assertGreater(us_fee_as_pct_of_notional(100, fee), 0.0025)

    def test_large_positions_are_nearly_free(self):
        fee, _ = us_perpetual_fee(1)
        self.assertLess(us_fee_as_pct_of_notional(100_000, fee), 0.0001)


class CMECommissionTests(unittest.TestCase):
    def test_micros_cost_less_than_e_minis(self):
        micro, _ = us_futures_fee("MES=F")
        emini, _ = us_futures_fee("ES=F")
        self.assertLess(micro, emini)

    def test_commission_is_declared_a_lower_bound(self):
        """Exchange, NFA and clearing are added on top and Kraken publishes
        the total only in the order form. Presenting this as the full cost
        would understate it."""
        _, why = us_futures_fee("ES=F")
        self.assertIn("lower bound", why)
        self.assertIn("EXCLUDES", why)

    def test_unknown_contract_returns_none_rather_than_guessing(self):
        fee, why = us_futures_fee("ZC=F")
        self.assertIsNone(fee)
        self.assertIn("no published", why)


class RegionGateTests(unittest.TestCase):
    def test_us_region_refuses_the_international_percentage_schedule(self):
        rate, why = futures_fee_for("BTC/USD", region="us")
        self.assertIsNone(rate)
        self.assertIn("PER CONTRACT", why)

    def test_international_region_still_returns_a_percentage(self):
        rate, _ = futures_fee_for("BTC/USD", region="international")
        self.assertIsNotNone(rate)
        self.assertGreater(rate, 0)


if __name__ == "__main__":
    unittest.main()
