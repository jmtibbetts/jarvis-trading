"""Three trades is not a win rate.

The gate was `total < 3`, so three observations reached the
signal-generation prompt as "This exact TA setup has occurred 3 times —
3/3 wins (100% win rate)". Nothing downstream could tell that the 100% came
from three trades.

Measured on the live table when this was written: 45 of 70 stored patterns
had fewer than 10 observations, several with n=1 recording a win rate of
1.0. Meanwhile calibration.py requires 30 observations and expectancy.py
requires 25 before either will state a rate at all — so the LLM was handed
a confident number from three samples while the deterministic engines,
reading the same history, correctly refused to conclude anything.

That is one part of the system undermining another, and it is what these
tests exist to prevent recurring.
"""
import unittest

from lib.expectancy import MIN_SAMPLE as EXPECTANCY_MIN
from lib.learning_engine import (PATTERN_EARLY_SAMPLE,
                                 PATTERN_MEASURED_SAMPLE, describe_pattern)

DESC = "Dir=LONG | RSI=high | Bias=bullish"


class TinySamplesStateNoRateTests(unittest.TestCase):
    """The specific regression: a percentage printed beside a tiny n."""

    def test_a_perfect_record_from_one_trade_shows_no_percentage(self):
        out = describe_pattern(total=1, wins=1, desc=DESC)
        self.assertNotIn("100", out)
        self.assertNotIn("%", out.split("Pattern:")[0].replace("P&L", ""))

    def test_three_out_of_three_no_longer_claims_a_win_rate(self):
        out = describe_pattern(total=3, wins=3, desc=DESC)
        self.assertNotIn("100%", out)
        self.assertIn("3 times", out)

    def test_it_says_plainly_that_the_sample_is_too_small(self):
        out = describe_pattern(total=4, wins=1, desc=DESC)
        self.assertIn("too small", out.lower())

    def test_singular_reads_correctly(self):
        self.assertIn("1 time before", describe_pattern(total=1, wins=0, desc=DESC))

    def test_no_observations_says_nothing_at_all(self):
        self.assertEqual(describe_pattern(0, 0, DESC), "")
        self.assertEqual(describe_pattern(None, None, DESC), "")


class TieredByEvidenceTests(unittest.TestCase):

    def test_the_early_tier_states_a_rate_but_qualifies_it(self):
        out = describe_pattern(total=PATTERN_EARLY_SAMPLE, wins=7, desc=DESC)
        self.assertIn("70%", out)
        self.assertIn("EARLY EVIDENCE", out)

    def test_the_measured_tier_still_defers_to_expectancy(self):
        out = describe_pattern(total=PATTERN_MEASURED_SAMPLE + 20, wins=30, desc=DESC)
        self.assertIn("expectancy", out.lower())
        self.assertNotIn("EARLY EVIDENCE", out)

    def test_every_tier_defers_rather_than_asserting_authority(self):
        """At no sample size may pattern memory present itself as the
        statistical verdict."""
        for n in (1, 5, 9, 10, 24, 25, 100, 5000):
            out = describe_pattern(total=n, wins=n // 2, desc=DESC).lower()
            self.assertTrue(
                any(k in out for k in ("too small", "early evidence",
                                       "descriptive context")),
                f"n={n} stated a conclusion without qualifying it: {out!r}")

    def test_the_boundaries_are_where_they_are_declared(self):
        below = describe_pattern(PATTERN_EARLY_SAMPLE - 1, 5, DESC)
        at = describe_pattern(PATTERN_EARLY_SAMPLE, 5, DESC)
        self.assertIn("too small", below.lower())
        self.assertIn("EARLY EVIDENCE", at)

        early = describe_pattern(PATTERN_MEASURED_SAMPLE - 1, 12, DESC)
        measured = describe_pattern(PATTERN_MEASURED_SAMPLE, 12, DESC)
        self.assertIn("EARLY EVIDENCE", early)
        self.assertNotIn("EARLY EVIDENCE", measured)


class AgreesWithTheDeterministicEnginesTests(unittest.TestCase):
    """Two independently-tuned thresholds eventually disagree. Binding to
    the expectancy engine's own constant means they cannot."""

    def test_the_measured_bar_is_the_expectancy_bar(self):
        self.assertEqual(PATTERN_MEASURED_SAMPLE, EXPECTANCY_MIN)

    def test_pattern_memory_never_speaks_before_expectancy_would(self):
        """Below the expectancy engine's own minimum, pattern memory must
        not present a rate as settled."""
        out = describe_pattern(EXPECTANCY_MIN - 1, EXPECTANCY_MIN - 1, DESC)
        self.assertIn("EARLY EVIDENCE", out)

    def test_the_early_tier_sits_below_the_measured_tier(self):
        self.assertLess(PATTERN_EARLY_SAMPLE, PATTERN_MEASURED_SAMPLE)


class RobustnessTests(unittest.TestCase):

    def test_junk_input_does_not_raise(self):
        for total, wins in ((None, None), ("x", "y"), (-5, 2), (3, None)):
            try:
                describe_pattern(total, wins, DESC)
            except (TypeError, ValueError):
                self.fail(f"raised on total={total!r} wins={wins!r}")

    def test_the_pattern_description_always_survives(self):
        for n in (1, 12, 40):
            self.assertIn(DESC, describe_pattern(n, 1, DESC))

    def test_average_pnl_is_reported_once_a_rate_is(self):
        out = describe_pattern(40, 20, DESC, avg_pnl=-1.25)
        self.assertIn("-1.25%", out)


if __name__ == "__main__":
    unittest.main()
