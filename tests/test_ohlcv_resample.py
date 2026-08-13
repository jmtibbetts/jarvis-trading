"""2-day and weekly bars are built here, not fetched.

No venue publishes a 2-day interval, and weekly bars need a week boundary
the engine controls rather than whatever a given exchange decided. That
makes resample_daily the only place these two timeframes come from — and a
mis-aggregated bar is the worst kind of bug in this codebase, because it
looks entirely plausible on screen. A weekly bar whose high came from the
wrong week still draws a normal-looking candle; only the breakout level
computed from it is wrong.

The fixture is built so every aggregate has one known correct answer.
"""
import unittest

import pandas as pd

from lib.ohlcv import RESAMPLE_FROM_DAILY, TF_CONFIG, resample_daily


def _daily(start, days):
    """One bar per calendar day, each with a distinct, checkable shape.

    Day i: open=i, close=i+0.5, high=i+9, low=i-9, volume=1.
    So a bucket's open is its first i, close its last i+0.5, high its last
    i+9, low its first i-9, volume its bar count. Any aggregation error
    lands on a value the arithmetic cannot produce by accident.
    """
    idx = pd.date_range(start, periods=days, freq="D")
    i = range(days)
    return pd.DataFrame(
        {"open": [float(x) for x in i],
         "high": [x + 9.0 for x in i],
         "low": [x - 9.0 for x in i],
         "close": [x + 0.5 for x in i],
         "volume": [1.0] * days},
        index=idx,
    )


AFTER = pd.Timestamp("2030-01-01")   # far past every fixture: nothing is in progress


class WeeklyBucketsAWholeTradingWeekTests(unittest.TestCase):
    """The bug this rule replaced: "W-MON" closes buckets ON Monday, so a
    bar labelled Mon Aug 10 spanned Tue Aug 4 -> Mon Aug 10 — the tail of
    one trading week glued to the head of the next. Every weekly level was
    measured across a boundary no trader draws."""

    def test_a_weekly_bar_runs_monday_to_sunday(self):
        df = _daily("2026-07-27", 21)        # a Monday
        out = resample_daily(df, RESAMPLE_FROM_DAILY["1W"], now=AFTER)
        first = out.index[0]
        self.assertEqual(first.day_name(), "Monday")
        self.assertEqual(first.date(), pd.Timestamp("2026-07-27").date())

    def test_every_label_is_a_monday(self):
        out = resample_daily(_daily("2026-07-29", 40), RESAMPLE_FROM_DAILY["1W"], now=AFTER)
        for ts in out.index:
            self.assertEqual(ts.day_name(), "Monday", ts)

    def test_a_week_does_not_borrow_from_its_neighbour(self):
        """Week 1 is days 0-6, week 2 days 7-13. If the boundary were off by
        one, week 2's open would be 6.0 rather than 7.0."""
        out = resample_daily(_daily("2026-07-27", 21), RESAMPLE_FROM_DAILY["1W"], now=AFTER)
        self.assertEqual(out.iloc[0]["open"], 0.0)
        self.assertEqual(out.iloc[1]["open"], 7.0)
        self.assertEqual(out.iloc[2]["open"], 14.0)

    def test_a_full_week_holds_seven_days(self):
        out = resample_daily(_daily("2026-07-27", 21), RESAMPLE_FROM_DAILY["1W"], now=AFTER)
        self.assertEqual(list(out["volume"]), [7.0, 7.0, 7.0])


class OhlcvDoesNotAggregateUniformlyTests(unittest.TestCase):
    """open=first, close=last, high=max, low=min, volume=sum. Using the
    wrong one anywhere produces a chart that never existed."""

    def setUp(self):
        self.out = resample_daily(_daily("2026-07-27", 14),
                                  RESAMPLE_FROM_DAILY["1W"], now=AFTER)

    def test_open_is_the_first_open_not_the_mean(self):
        self.assertEqual(self.out.iloc[0]["open"], 0.0)     # mean would be 3.0

    def test_close_is_the_last_close(self):
        self.assertEqual(self.out.iloc[0]["close"], 6.5)    # day 6 -> 6+0.5

    def test_high_is_the_max_and_low_the_min(self):
        self.assertEqual(self.out.iloc[0]["high"], 15.0)    # day 6 -> 6+9
        self.assertEqual(self.out.iloc[0]["low"], -9.0)     # day 0 -> 0-9

    def test_volume_is_summed_not_averaged(self):
        self.assertEqual(self.out.iloc[0]["volume"], 7.0)   # mean would be 1.0

    def test_the_bar_is_internally_consistent(self):
        for _, r in self.out.iterrows():
            self.assertGreaterEqual(r["high"], max(r["open"], r["close"]))
            self.assertLessEqual(r["low"], min(r["open"], r["close"]))


class TwoDayTests(unittest.TestCase):
    def test_a_2d_bar_holds_exactly_two_days(self):
        out = resample_daily(_daily("2026-07-27", 10), RESAMPLE_FROM_DAILY["2D"], now=AFTER)
        self.assertEqual(set(out["volume"]), {2.0})

    def test_2d_buckets_do_not_overlap(self):
        out = resample_daily(_daily("2026-07-27", 10), RESAMPLE_FROM_DAILY["2D"], now=AFTER)
        self.assertEqual(list(out["open"]), [0.0, 2.0, 4.0, 6.0, 8.0])
        self.assertEqual(list(out["close"]), [1.5, 3.5, 5.5, 7.5, 9.5])


class InProgressBucketIsNotABarTests(unittest.TestCase):
    """A partial week reports a high and low the week has not finished
    making. Every level derived from it — breakout, channel edge, ATR —
    would change under the engine's feet on the next tick."""

    def test_a_half_finished_week_is_dropped(self):
        df = _daily("2026-07-27", 10)                    # 7 + 3 days
        out = resample_daily(df, RESAMPLE_FROM_DAILY["1W"],
                             now=pd.Timestamp("2026-08-05"))   # mid week 2
        self.assertEqual(len(out), 1)
        self.assertEqual(out.index[-1].date(), pd.Timestamp("2026-07-27").date())

    def test_a_finished_week_is_kept(self):
        df = _daily("2026-07-27", 14)
        out = resample_daily(df, RESAMPLE_FROM_DAILY["1W"],
                             now=pd.Timestamp("2026-08-10"))   # week 2 closed
        self.assertEqual(len(out), 2)

    def test_a_half_finished_2d_bar_is_dropped(self):
        df = _daily("2026-07-27", 5)                     # 2 + 2 + 1
        out = resample_daily(df, RESAMPLE_FROM_DAILY["2D"],
                             now=pd.Timestamp("2026-07-31T12:00"))
        self.assertEqual(list(out["volume"]), [2.0, 2.0])

    def test_only_one_week_of_data_yields_nothing_rather_than_a_partial(self):
        out = resample_daily(_daily("2026-07-27", 3), RESAMPLE_FROM_DAILY["1W"],
                             now=pd.Timestamp("2026-07-30"))
        self.assertIsNone(out)


class DegenerateInputTests(unittest.TestCase):
    def test_no_data_is_none_not_an_exception(self):
        self.assertIsNone(resample_daily(None, RESAMPLE_FROM_DAILY["1W"]))
        self.assertIsNone(resample_daily(pd.DataFrame(), RESAMPLE_FROM_DAILY["1W"]))

    def test_uppercase_columns_are_handled(self):
        df = _daily("2026-07-27", 14)
        df.columns = [c.capitalize() for c in df.columns]
        out = resample_daily(df, RESAMPLE_FROM_DAILY["1W"], now=AFTER)
        self.assertEqual(out.iloc[0]["open"], 0.0)

    def test_a_bare_rule_string_still_works(self):
        """Older callers passed just the rule; span is inferred, not guessed
        at runtime from index spacing."""
        out = resample_daily(_daily("2026-07-27", 14), "W", now=AFTER)
        self.assertEqual(out.iloc[0]["volume"], 7.0)

    def test_the_source_is_labelled_as_derived(self):
        out = resample_daily(_daily("2026-07-27", 14), RESAMPLE_FROM_DAILY["1W"], now=AFTER)
        self.assertIn("resampled_from_1D", out.attrs.get("source", ""))


class WiringTests(unittest.TestCase):
    def test_both_derived_timeframes_are_configured(self):
        for tf in ("2D", "1W"):
            self.assertIn(tf, TF_CONFIG, tf)
            self.assertIn(tf, RESAMPLE_FROM_DAILY, tf)

    def test_the_daily_history_is_deep_enough_to_build_them(self):
        """2D and 1W are resampled from the DAILY series, so it is 1D's depth
        that governs them, not the (unused) bar counts on their own rows.

        Crypto trades seven days a week: a 252-bar daily series is 36 weekly
        bars, which is not enough to read a weekly trend from. The daily
        series must carry a year of WEEKS at the worst case of 7 bars each.
        """
        self.assertGreaterEqual(TF_CONFIG["1D"][1], 52 * 7)


if __name__ == "__main__":
    unittest.main()
