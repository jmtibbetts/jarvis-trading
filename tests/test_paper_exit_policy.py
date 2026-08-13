"""Exit policy: what closes a paper position, and in what order.

Measured over 218 closed trades, before this change:

    exit path               n   win%      net P&L     avg
    dollar cap ($15)      103     4%    -5,687.58  -55.22
    signal's own stop      67    27%    -3,602.87  -53.77
    margin call             3     0%    -1,940.53 -646.84
    AI EXIT (deep verify)  28    21%      -117.62   -4.20
    target / scale-out     17   100%    +4,097.30    +241

Two findings drove the rewrite. The fixed $15 cap and the ATR stop bled at
the SAME rate, so recalibrating either changes nothing — mechanical stops
were the problem. And every trade that reached its target won; only 17 of
218 got there because the stops killed the rest first.

The $15 was applied to positions carrying $7,000-$12,000 of notional, a
trigger at a 0.12-0.21% move against crypto whose ATR is 2-5%. And being a
comparison made by a 15-minute job rather than a price on the position, it
exited at -$55 on average and -$379 at worst: 3.7x and 25x its own bound.
"""
import unittest

from jobs.paper_trading import (catastrophic_loss_usd, catastrophic_stop_price,
                                CATASTROPHIC_LOSS_PCT_OF_MARGIN)


class BackstopScalesWithMarginTests(unittest.TestCase):
    """A fixed dollar figure cannot serve a $500 position and a $12,000 one."""

    def test_the_cap_is_a_share_of_margin(self):
        self.assertAlmostEqual(catastrophic_loss_usd(1000.0),
                               1000.0 * CATASTROPHIC_LOSS_PCT_OF_MARGIN / 100.0)

    def test_a_bigger_position_may_lose_proportionally_more(self):
        self.assertAlmostEqual(catastrophic_loss_usd(2000.0),
                               catastrophic_loss_usd(1000.0) * 2)

    def test_it_never_exceeds_the_margin_itself(self):
        for margin in (100.0, 1000.0, 12_000.0):
            self.assertLess(catastrophic_loss_usd(margin), margin)


class BackstopIsAPriceTests(unittest.TestCase):
    """The half that was missing: a level on the position, not a comparison
    made whenever the poll next runs."""

    def test_a_long_stops_below_entry_and_a_short_above(self):
        long_stop = catastrophic_stop_price(100.0, 100.0, 1000.0, is_short=False)
        short_stop = catastrophic_stop_price(100.0, 100.0, 1000.0, is_short=True)
        self.assertLess(long_stop, 100.0)
        self.assertGreater(short_stop, 100.0)

    def test_the_price_reaches_exactly_the_cap(self):
        entry, qty, margin = 100.0, 100.0, 1000.0
        stop = catastrophic_stop_price(entry, qty, margin, is_short=False)
        self.assertAlmostEqual((entry - stop) * qty, catastrophic_loss_usd(margin))

    def test_it_declines_rather_than_guessing_without_a_quantity(self):
        self.assertIsNone(catastrophic_stop_price(100.0, 0.0, 1000.0, is_short=False))
        self.assertIsNone(catastrophic_stop_price(0.0, 100.0, 1000.0, is_short=False))


class NotInsideTheNoiseTests(unittest.TestCase):
    """The specific failure being corrected: a stop closer to entry than the
    instrument's ordinary hourly movement is not a risk limit, it is a coin
    flip on the next tick."""

    REAL_POSITIONS = [
        # (symbol, entry, qty, margin) — from the live paper book
        ("AAVE/USD", 89.447, 102.28286, 953.01),
        ("ALT/USD", 0.005908, 1_600_419.0, 946.0),
        ("ARB/USD", 0.07439735, 146_155.0, 946.0),
    ]

    def test_every_real_position_stops_outside_a_one_percent_move(self):
        for sym, entry, qty, margin in self.REAL_POSITIONS:
            stop = catastrophic_stop_price(entry, qty, margin, is_short=True)
            move_pct = abs(stop - entry) / entry * 100
            self.assertGreater(move_pct, 1.0, f"{sym}: stop only {move_pct:.3f}% from entry")

    def test_the_old_fixed_cap_would_have_been_inside_the_noise(self):
        """Documents what was wrong, so it cannot quietly return."""
        for sym, entry, qty, _margin in self.REAL_POSITIONS:
            old_move_pct = 15.0 / (qty * entry) * 100
            self.assertLess(old_move_pct, 0.25,
                            f"{sym}: the $15 cap triggered at {old_move_pct:.3f}%")


if __name__ == "__main__":
    unittest.main()
