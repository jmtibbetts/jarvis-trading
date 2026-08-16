"""Spot FX was priced as an equity, and slippage dominated the result.

`is_crypto_symbol` was the only classifier in transaction_costs, so
`NZDUSD=X` fell to the equity branch and was charged the 0.21% slippage
median measured from this system's own equity and crypto fills. Measured on
a live candidate: 37.9R of cost, 33.9R of it slippage, on the deepest
market in the world.
"""
import unittest

from lib.transaction_costs import (
    DEFAULT_SLIPPAGE_PCT, estimate_costs, default_slippage_pct, is_fx_symbol,
    min_viable_stop_pct,
)


class ClassificationTests(unittest.TestCase):
    def test_fx_is_recognised(self):
        for s in ("NZDUSD=X", "EURUSD=X", "GBPJPY=X"):
            self.assertTrue(is_fx_symbol(s), s)

    def test_non_fx_is_not_swept_in(self):
        for s in ("AAPL", "BTC/USD", "ES=F", "SPY"):
            self.assertFalse(is_fx_symbol(s), s)


class SlippageTests(unittest.TestCase):
    def test_fx_does_not_inherit_the_equity_crypto_median(self):
        """The 0.21% median came from equity and crypto fills and says
        nothing about spot FX."""
        self.assertLess(default_slippage_pct("EURUSD=X"), DEFAULT_SLIPPAGE_PCT)
        self.assertEqual(default_slippage_pct("AAPL"), DEFAULT_SLIPPAGE_PCT)

    def test_slippage_source_is_labelled_fx(self):
        c = estimate_costs("EURUSD=X", 1.08, 1.07)
        self.assertEqual(c["slippage_source"], "default_fx")
        self.assertEqual(c["spread_source"], "default_fx")

    def test_a_measured_slippage_still_wins(self):
        c = estimate_costs("EURUSD=X", 1.08, 1.07, slippage_pct=0.005)
        self.assertEqual(c["slippage_source"], "measured")


class RegressionTests(unittest.TestCase):
    """The operator-reported candidate, verbatim."""

    ENTRY, STOP = 0.589379, 0.589306      # a 0.0124% stop

    def test_the_37R_estimate_is_gone(self):
        c = estimate_costs("NZDUSD=X", self.ENTRY, self.STOP)
        self.assertLess(c["total_r"], 6.0,
                        "FX priced as equity produced 37.9R here")

    def test_the_tight_stop_is_still_refused(self):
        """Correcting the cost model must not turn a sub-pip stop into a
        tradeable one — it should be refused for a believable reason."""
        c = estimate_costs("NZDUSD=X", self.ENTRY, self.STOP)
        self.assertGreater(c["total_r"], 0.5)

    def test_slippage_no_longer_dominates(self):
        c = estimate_costs("NZDUSD=X", self.ENTRY, self.STOP)
        self.assertLess(c["slippage_r"], c["total_r"] * 0.6,
                        "slippage was 33.9R of the 37.9R total")

    def test_fx_round_trip_is_cheaper_than_equity(self):
        fx = estimate_costs("EURUSD=X", 1.08, 1.0692)["total_pct"]
        eq = estimate_costs("AAPL", 200.0, 198.0)["total_pct"]
        self.assertLess(fx, eq)


class ViableStopTests(unittest.TestCase):
    def test_fx_floor_is_realistic(self):
        """A 0.94% minimum stop on EURUSD excluded FX from the desk
        entirely; majors trade on far tighter structure than that."""
        floor = min_viable_stop_pct("EURUSD=X")
        self.assertLess(floor, 0.003)
        self.assertGreater(floor, 0.0)

    def test_other_classes_are_unchanged(self):
        self.assertAlmostEqual(min_viable_stop_pct("AAPL"), 0.0094, places=4)
        self.assertGreater(min_viable_stop_pct("BTC/USD"),
                           min_viable_stop_pct("EURUSD=X"))


if __name__ == "__main__":
    unittest.main()
