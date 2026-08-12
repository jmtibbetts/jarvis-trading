"""Auto Sim must pay to trade.

It used to price every trade as free: `_pnl` was the raw price move, nothing
charged a fee, nothing crossed a spread. Over 166 trades under the current
leverage policy the book reported +$128 while the venue fees it never paid
came to $6,864 — the sign of the result was decided entirely by the missing
side of the ledger.

A simulator whose losses are optional cannot be compared against the paper
book, which is the only reason it exists.
"""
import unittest
from types import SimpleNamespace

from lib import auto_simulator as sim


def _position(**kw):
    base = dict(symbol="BTC/USD", side="long", qty=1.0, entry_price=100.0,
                fees=0.0, margin_used=1000.0)
    base.update(kw)
    return SimpleNamespace(**base)


class FeesAreChargedTests(unittest.TestCase):
    def test_a_flat_trade_loses_the_round_trip(self):
        """Closing where you opened is not free."""
        p = _position(fees=12.5)
        self.assertEqual(sim._gross_pnl(p, 100.0), 0.0)
        self.assertEqual(sim._pnl(p, 100.0), -12.5)

    def test_net_is_gross_minus_fees_for_both_sides(self):
        for side, exit_px in (("long", 110.0), ("short", 90.0)):
            p = _position(side=side, fees=4.0)
            self.assertAlmostEqual(sim._gross_pnl(p, exit_px), 10.0, msg=side)
            self.assertAlmostEqual(sim._pnl(p, exit_px), 6.0, msg=side)

    def test_a_real_symbol_is_charged_something(self):
        fee, why = sim._round_trip_fee("BTC/USD", 10_000.0, 10.0, 50_000.0)
        self.assertGreater(fee, 0.0)
        self.assertTrue(why)

    def test_fee_lookup_failure_charges_rather_than_zeroes(self):
        """Every failure path must land on a cost, never on free trading."""
        fee, why = sim._round_trip_fee("!!NOT-A-SYMBOL!!", 10_000.0, 1.0, 1.0)
        self.assertGreater(fee, 0.0, "a failed lookup made the trade free")


class SpreadIsCrossedTests(unittest.TestCase):
    """The spread always moves against the trade, entering and exiting."""

    def test_buying_fills_above_mid_and_selling_below(self):
        buy, _ = sim._fill_price("BTC/USD", 100.0, "long", entering=True)
        sell, _ = sim._fill_price("BTC/USD", 100.0, "long", entering=False)
        self.assertGreater(buy, 100.0)
        self.assertLess(sell, 100.0)

    def test_a_short_is_the_mirror_image(self):
        entry, _ = sim._fill_price("BTC/USD", 100.0, "short", entering=True)
        exit_, _ = sim._fill_price("BTC/USD", 100.0, "short", entering=False)
        self.assertLess(entry, 100.0, "shorting sells — fills at the bid")
        self.assertGreater(exit_, 100.0, "covering buys — fills at the ask")

    def test_a_round_trip_at_an_unchanged_price_loses_the_spread(self):
        for side in ("long", "short"):
            entry, _ = sim._fill_price("BTC/USD", 100.0, side, entering=True)
            exit_, _ = sim._fill_price("BTC/USD", 100.0, side, entering=False)
            p = _position(side=side, entry_price=entry, qty=1.0)
            self.assertLess(sim._gross_pnl(p, exit_), 0.0, f"{side} crossed for free")


class ReservedAtOpenTests(unittest.TestCase):
    """Unrealized and realized P&L stay on one basis: the cost to unwind is
    charged the moment the position exists, so nothing ever looks flat when
    closing it would lose money."""

    def test_an_untouched_position_shows_the_cost_of_leaving(self):
        p = _position(fees=30.0)
        self.assertEqual(sim._pnl(p, p.entry_price), -30.0)

    def test_liquidation_accounts_for_fees(self):
        """Fees eat margin like any other loss."""
        margin = 1000.0
        p = _position(fees=100.0, qty=10.0, margin_used=margin)
        # a 90-point adverse move is -900 gross, which alone survives...
        self.assertGreater(sim._gross_pnl(p, 10.0), -margin)
        # ...but not once the round trip is paid.
        self.assertLessEqual(sim._pnl(p, 10.0), -margin)


if __name__ == "__main__":
    unittest.main()
