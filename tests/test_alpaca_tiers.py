"""Alpaca crypto volume tiers, and why leverage belongs on the perpetual.

Alpaca's base rate was already correct in the venue table, but the ladder
was flat — so any volume discount was invisible to the cost model, and a
high-volume account was priced as though it paid retail rates.
"""
import unittest

from lib.venues import fee_for, futures_fee_for


class AlpacaTierTests(unittest.TestCase):
    def test_tiers_step_down_with_volume(self):
        rates = [fee_for("alpaca", volume_30d=v)[0]
                 for v in (0, 100_000, 1_000_000, 100_000_000)]
        self.assertEqual(rates, sorted(rates, reverse=True))

    def test_maker_reaches_zero_at_the_top_tier(self):
        self.assertEqual(fee_for("alpaca", maker=True, volume_30d=100_000_000)[0], 0.0)

    def test_base_tier_is_unchanged(self):
        """The published entry rate: 0.25% taker, 0.15% maker."""
        self.assertAlmostEqual(fee_for("alpaca", volume_30d=0)[0], 0.0025)
        self.assertAlmostEqual(fee_for("alpaca", maker=True, volume_30d=0)[0], 0.0015)

    def test_maker_is_cheaper_than_taker_at_every_tier(self):
        for v in (0, 100_000, 1_000_000, 10_000_000, 100_000_000):
            self.assertLessEqual(fee_for("alpaca", maker=True, volume_30d=v)[0],
                                 fee_for("alpaca", volume_30d=v)[0], f"at ${v:,}")


class PerpetualIsCheapestTests(unittest.TestCase):
    """The structural reason leverage belongs on the derivative rather than
    on spot margin: at EVERY volume tier the perpetual costs less."""

    def test_perpetuals_beat_both_spot_venues_at_every_tier(self):
        for v in (0, 500_000, 10_000_000, 100_000_000):
            perp, _ = futures_fee_for("BTC/USD", volume_30d=v)
            self.assertIsNotNone(perp)
            for venue in ("alpaca", "kraken"):
                spot, _ = fee_for(venue, volume_30d=v, use_account=False)
                self.assertLess(perp, spot, f"{venue} at ${v:,}")

    def test_the_gap_is_largest_for_a_small_account(self):
        """Where it matters most: no volume, retail tier."""
        perp, _ = futures_fee_for("BTC/USD", volume_30d=0)
        kraken_spot, _ = fee_for("kraken", volume_30d=0, use_account=False)
        self.assertGreater(kraken_spot / perp, 5)


if __name__ == "__main__":
    unittest.main()
