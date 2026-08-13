"""The chart timeframe decides what an adverse move means.

3% refutes a 5-minute scalp and is ordinary noise on a weekly position. The
management loop judged every open trade as though it shared one horizon, so
one of those two was always managed wrong — and the book holds both at once.

The hold table already existed in three copies (signals API, Telegram
formatter, signal card), each a bare string map with no minute windows, so
none of them could be used by the exit logic. lib/trade_horizon.py is the
single source: same labels the operator already reads, plus the numbers the
loop needs.
"""
import unittest

from lib.trade_horizon import (HORIZONS, expected_hold_minutes, hold_estimate,
                               hold_map, hold_status, is_scalp, room_multiplier,
                               format_duration)
from jobs.paper_trading import _tier


class LabelsMatchWhatTheUIAlreadyShowedTests(unittest.TestCase):
    """Consolidating three copies must not change any text on screen."""

    def test_the_labels_are_unchanged(self):
        for tf, expected in (("1m", "<30 min"), ("5m", "<1 hr"), ("15m", "1-4 hr"),
                             ("1H", "4-24 hr"), ("4H", "1-5 days"), ("1D", "1-4 weeks")):
            self.assertEqual(hold_estimate(tf), expected, tf)

    def test_an_unknown_timeframe_says_so_rather_than_guessing(self):
        self.assertEqual(hold_estimate("13h"), "varies")
        self.assertEqual(hold_estimate(None), "varies")

    def test_the_whole_map_is_available_for_the_formatters(self):
        m = hold_map()
        self.assertEqual(m["1D"], "1-4 weeks")
        self.assertEqual(set(m), set(HORIZONS))


class WindowMatchesLabelTests(unittest.TestCase):
    """The minutes ARE the label.

    Any drift between the two is a trap: the operator reads "hold 1-4 weeks"
    on the card, so a window that quietly meant 3-14 days would judge a
    three-week-old position stale while the screen still said it was on
    schedule. That mismatch existed in the first version of this table.
    """

    DAY = 1_440

    def test_one_to_four_weeks_really_means_one_to_four_weeks(self):
        lo, hi = expected_hold_minutes("1D")
        self.assertEqual(hold_estimate("1D"), "1-4 weeks")
        self.assertAlmostEqual(lo / self.DAY, 7, delta=0.1)
        self.assertAlmostEqual(hi / self.DAY, 28, delta=0.1)

    def test_hour_labels_match_their_windows(self):
        for tf, lo_h, hi_h in (("15m", 1, 4), ("30m", 2, 8), ("1H", 4, 24)):
            lo, hi = expected_hold_minutes(tf)
            self.assertAlmostEqual(lo / 60, lo_h, delta=0.1, msg=tf)
            self.assertAlmostEqual(hi / 60, hi_h, delta=0.1, msg=tf)

    def test_day_labels_match_their_windows(self):
        for tf, lo_d, hi_d in (("2H", 1, 3), ("4H", 1, 5)):
            lo, hi = expected_hold_minutes(tf)
            self.assertAlmostEqual(lo / self.DAY, lo_d, delta=0.1, msg=tf)
            self.assertAlmostEqual(hi / self.DAY, hi_d, delta=0.1, msg=tf)

    def test_sub_hour_labels_are_not_exceeded(self):
        self.assertLessEqual(expected_hold_minutes("1m")[1], 30)
        self.assertLessEqual(expected_hold_minutes("5m")[1], 60)


class HoldWindowTests(unittest.TestCase):
    def test_longer_timeframes_expect_longer_holds(self):
        order = ["1m", "5m", "15m", "1H", "4H", "1D", "1W"]
        lows = [expected_hold_minutes(tf)[0] for tf in order]
        self.assertEqual(lows, sorted(lows))

    def test_a_scalp_is_measured_in_minutes_and_a_daily_in_days(self):
        self.assertLess(expected_hold_minutes("5m")[1], 120)
        self.assertGreater(expected_hold_minutes("1D")[0], 24 * 60)

    def test_scalp_classification(self):
        self.assertTrue(is_scalp("5m"))
        self.assertFalse(is_scalp("1D"))


class RoomScalesWithHorizonTests(unittest.TestCase):
    """A trail calibrated for a 5-minute chart stops a daily position out on
    its first ordinary pullback."""

    def test_a_daily_gets_more_room_than_a_scalp(self):
        self.assertGreater(room_multiplier("1D"), room_multiplier("5m"))

    def test_the_scaling_is_damped_not_proportional(self):
        """A 1D hold is ~18x a 1H hold but must not get an 18x wider trail."""
        ratio = expected_hold_minutes("1D")[0] / expected_hold_minutes("1H")[0]
        self.assertGreater(ratio, 15)
        self.assertLess(room_multiplier("1D") / room_multiplier("1H"), 5)

    def test_it_stays_within_sane_bounds(self):
        for tf in list(HORIZONS) + [None, "nonsense"]:
            self.assertGreaterEqual(room_multiplier(tf), 0.6)
            self.assertLessEqual(room_multiplier(tf), 4.0)


class RefutedByTimeTests(unittest.TestCase):
    """A trade far past its expected hold without resolving is no longer the
    trade that was entered — a failure no stop-loss can express."""

    def test_a_fresh_position_is_early(self):
        s = hold_status("1D", "2026-08-12T23:59:00+00:00")
        self.assertIn(s["state"], ("early", "within expected hold"))

    def test_a_scalp_open_for_days_is_stale(self):
        s = hold_status("5m", "2026-08-01T00:00:00+00:00")
        self.assertEqual(s["state"], "stale")

    def test_the_same_age_reads_differently_by_timeframe(self):
        """Six hours is stale for a 5m setup and early for a 1D one."""
        opened = "2026-08-12T18:00:00+00:00"
        self.assertEqual(hold_status("5m", opened)["state"], "stale")
        self.assertIn(hold_status("1D", opened)["state"], ("early", "within expected hold"))

    def test_a_missing_timestamp_does_not_invent_an_age(self):
        self.assertEqual(hold_status("1H", None)["state"], "unknown")


class TierRespectsHorizonTests(unittest.TestCase):
    """The loss cut is widened for longer horizons; the profit take is not."""

    def test_a_daily_position_is_not_cut_at_the_scalp_threshold(self):
        """-4% crypto cut: fires on a 5m setup, must not on a 1D one."""
        self.assertIsNotNone(_tier(-4.5, True, "5m"))
        self.assertEqual(_tier(-4.5, True, "5m")["action"], "close")
        daily = _tier(-4.5, True, "1D")
        self.assertTrue(daily is None or daily["action"] != "close",
                        "a 1D setup was cut for a 4.5% move")

    def test_a_daily_position_is_still_cut_eventually(self):
        self.assertEqual(_tier(-25.0, True, "1D")["action"], "close")

    def test_profit_taking_is_not_scaled_by_horizon(self):
        for tf in ("5m", "1D", None):
            self.assertEqual(_tier(12.0, True, tf)["action"], "close")

    def test_behaviour_without_a_timeframe_is_unchanged(self):
        self.assertEqual(_tier(-4.5, True, None)["action"], "close")


class DurationFormatTests(unittest.TestCase):
    def test_it_reads_in_the_right_unit(self):
        self.assertIn("min", format_duration(45))
        self.assertIn("h", format_duration(300))
        self.assertIn("days", format_duration(5_000))
        self.assertIn("weeks", format_duration(30_000))


if __name__ == "__main__":
    unittest.main()
