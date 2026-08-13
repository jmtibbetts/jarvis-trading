"""Confidence has to mean something before anything can be built on it.

Measured over 8,899 recorded outcomes, the model's self-reported confidence
was INVERTED at the extremes:

    stated confidence      n      actual win%
            90+           32          28.1%
            80-89        199          37.2%
            70-79        315          25.7%
            60-69         36          44.4%     <- least confident won most

Every gate takes that number as an input — the focus floor, the execution
criteria, the leverage ladder — and treats it as a probability. The old
calibration capped evidence at weight 0.35, so the model's guess kept 65%
of the vote however much history contradicted it: a 90% signal still
reported ~68% against a measured 28%.

What actually predicts, measured:

    timeframe   n      win%              score band    win%
        1H    635     66.4%                  70-79    39.4%
        1D   4597     42.2%                  60-69    30.6%
         5m   286     41.4%                   <60     22.8%
        4H   3222     27.8%
"""
import unittest
from unittest import mock

from lib import calibration as cal


class WinRateFromPnlTests(unittest.TestCase):
    """A win is decided on realised P&L, not on the outcome column.

    That column stores 'WIN'/'LOSS'/'BREAKEVEN' in UPPERCASE, so any query
    comparing it to 'win' matches nothing — which is exactly how a
    0/8899 = 0.0% win rate got computed while 2,839 wins sat in the table.
    """

    def test_the_uppercase_trap_is_documented_in_code(self):
        import inspect
        src = inspect.getsource(cal.build_table)
        self.assertIn("UPPERCASE", src)

    def test_a_real_table_is_built_from_recorded_outcomes(self):
        t = cal.build_table(force=True)
        self.assertIsNotNone(t.get("overall"))
        self.assertGreater(t["overall"]["total"], 0)
        self.assertGreater(t["overall"]["wins"], 0,
                           "zero wins means the outcome comparison is broken again")


class EvidenceEventuallyWinsTests(unittest.TestCase):
    """The old cap of 0.35 meant it never could."""

    def test_a_large_sample_moves_the_number_most_of_the_way(self):
        c, why = cal.calibrate(90.0, "4H")
        self.assertGreater(why["weight"], 0.5, "evidence is still capped too low")
        self.assertLess(c, 50.0, "a 90% claim survived a measured 27.8% bucket")

    def test_confidence_barely_matters_once_evidence_is_strong(self):
        """Two signals on the same timeframe, wildly different self-reports,
        should land close together — because the timeframe is what predicts."""
        hi, _ = cal.calibrate(90.0, "4H")
        lo, _ = cal.calibrate(60.0, "4H")
        self.assertLess(abs(hi - lo), 10.0)

    def test_the_good_timeframe_scores_above_the_bad_one(self):
        """The whole point: a 1H setup must not be buried under a 4H one
        claiming a higher number."""
        one_hour, _ = cal.calibrate(70.0, "1H")
        four_hour, _ = cal.calibrate(90.0, "4H")
        self.assertGreater(one_hour, four_hour)


class SpecificityTests(unittest.TestCase):
    """Sample size says how RELIABLE a rate is; specificity says how much it
    is about THIS setup. A base rate over 8,899 mixed trades is highly
    reliable and barely relevant."""

    def test_the_base_rate_does_not_dominate(self):
        _, why = cal.calibrate(80.0, None)
        self.assertEqual(why["bucket"], "overall")
        self.assertLess(why["weight"], 0.5,
                        "the global base rate would set every signal to one number")

    def test_a_symbols_own_record_outranks_the_aggregate(self):
        c, why = cal.calibrate(90.0, "1H", historical={"total_trades": 40, "wins": 4})
        self.assertEqual(why["bucket"], "this symbol")
        self.assertLess(c, 60.0)

    def test_a_tiny_symbol_sample_defers_to_the_aggregate(self):
        _, why = cal.calibrate(90.0, "1H", historical={"total_trades": 3, "wins": 3})
        self.assertNotEqual(why["bucket"], "this symbol")

    def test_an_unknown_score_band_is_not_treated_as_a_category(self):
        """'unknown' is the rows with no composite_score recorded — during
        live scoring that is EVERY signal, since the score is still being
        computed. Calibrating against it means calibrating against an
        artifact of the join."""
        _, why = cal.calibrate(80.0, None, composite_score=None)
        self.assertNotIn("unknown", str(why["bucket"]))


class TimeframeEdgeTests(unittest.TestCase):
    def test_the_measured_ordering_is_reflected(self):
        self.assertGreater(cal.timeframe_edge("1H")["edge"],
                           cal.timeframe_edge("4H")["edge"])

    def test_a_thin_timeframe_reports_no_edge_rather_than_a_guess(self):
        e = cal.timeframe_edge("1W")
        self.assertIsNone(e["win_rate"])
        self.assertEqual(e["edge"], 0.0)

    def test_the_edge_is_damped_in_the_score(self):
        from lib.signal_scorer import TIMEFRAME_EDGE_WEIGHT
        self.assertLess(TIMEFRAME_EDGE_WEIGHT, 1.0,
                        "a horizon must not outweigh the setup's own evidence")


class EverySignalReportsItsBasisTests(unittest.TestCase):
    """A win rate without its sample size is an assertion, not evidence."""

    def test_the_bucket_and_sample_travel_with_the_number(self):
        _, why = cal.calibrate(80.0, "1H")
        for key in ("bucket", "sample", "win_rate", "weight", "source"):
            self.assertIn(key, why)

    def test_insufficient_history_says_so_rather_than_inventing_a_rate(self):
        with mock.patch.object(cal, "build_table", return_value={
            "timeframe": {}, "tf_score": {}, "score": {}, "overall": {"wins": 0, "total": 0},
        }):
            c, why = cal.calibrate(80.0, "1H")
            self.assertEqual(c, 80.0)
            self.assertEqual(why["source"], "insufficient_history")


if __name__ == "__main__":
    unittest.main()
