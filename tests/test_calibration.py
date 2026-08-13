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


def _table(**over):
    """A controlled calibration table.

    These tests originally read the live database and asserted things like
    "4H measures 27.8%". That made them assertions about DATA, not about
    behaviour — so quarantining the pre-epoch outcomes broke eight of them
    while the code was working correctly. The numbers below mirror what was
    actually measured, but the tests now prove the logic regardless of what
    the database holds.
    """
    t = {
        "timeframe": {
            "1H": {"wins": 421, "total": 635, "win_rate": 66.4},
            "4H": {"wins": 896, "total": 3222, "win_rate": 27.8},
            "1D": {"wins": 1939, "total": 4597, "win_rate": 42.2},
        },
        "tf_score": {}, "score": {}, "strategy": {}, "strategy_tf": {},
        "overall": {"wins": 3427, "total": 8899},
    }
    t.update(over)
    return t


def _with_table(testcase, **over):
    patch = mock.patch.object(cal, "build_table", return_value=_table(**over))
    patch.start()
    testcase.addCleanup(patch.stop)



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

    def test_the_table_builds_without_error_against_the_real_database(self):
        """Structure only. Asserting CONTENT here made the test an assertion
        about the database rather than the code, and it broke when the
        pre-epoch outcomes were quarantined while the code was correct."""
        t = cal.build_table(force=True)
        for key in ("timeframe", "tf_score", "score", "strategy", "overall"):
            self.assertIn(key, t)

    def test_a_win_is_decided_on_pnl_not_the_outcome_column(self):
        import inspect
        src = inspect.getsource(cal.build_table)
        self.assertIn("pnl_pct", src)
        self.assertNotIn("== 'win'", src)


class EvidenceEventuallyWinsTests(unittest.TestCase):
    """The old cap of 0.35 meant it never could."""

    def setUp(self):
        _with_table(self)

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

    def setUp(self):
        _with_table(self)

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
    def setUp(self):
        _with_table(self)

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

    def setUp(self):
        _with_table(self)

    def test_the_bucket_and_sample_travel_with_the_number(self):
        _, why = cal.calibrate(80.0, "1H")
        for key in ("bucket", "sample", "win_rate", "weight", "source"):
            self.assertIn(key, why)

    def test_insufficient_history_says_so_rather_than_inventing_a_rate(self):
        with mock.patch.object(cal, "build_table", return_value={
            "timeframe": {}, "tf_score": {}, "score": {}, "overall": {"wins": 0, "total": 0},
        }):
            c, why = cal.calibrate(80.0, "1H")
            self.assertEqual(why["source"], "insufficient_history")
            # Capped rather than passed through — see NoEvidenceIsNotConfidenceTests.
            self.assertEqual(c, cal.NO_EVIDENCE_CEILING)


class EpochQuarantineTests(unittest.TestCase):
    """Outcomes from the old machine cannot calibrate the new one.

    Measured on 8,903 pre-epoch outcomes: 93.6% were closed by an exit rule
    that no longer exists — the $15 noise cap firing at a 0.12% move,
    closing a position when its ENTRY signal expired, un-scaled tier cuts,
    LLM exits with no horizon context — and only 6.0% ever reached their
    target. Add the contract-multiplier error on futures and 6-decimal
    rounding on sub-cent prices, and the win/loss labels describe a machine
    that is gone.

    They are QUARANTINED, not deleted: still available for analysis, simply
    not permitted to calibrate.
    """

    def test_only_the_current_epoch_is_read(self):
        import inspect
        src = inspect.getsource(cal.build_table)
        self.assertIn("engine_epoch", src)
        self.assertIn("CURRENT_EPOCH", src)

    def test_the_reason_for_the_epoch_is_recorded_in_code(self):
        import inspect
        src = inspect.getsource(cal)
        self.assertIn("93.6%", src)


class NoEvidenceIsNotConfidenceTests(unittest.TestCase):
    """"We do not know" must not present as "90% sure".

    With the old data quarantined there is no history yet, so calibration
    returns insufficient_history — and without a cap that silently hands
    back the model's own number, which was measured INVERTED (90%+ signals
    won 28%). A signal with nothing behind it must not outrank one with a
    measured edge because the model felt strongly.
    """

    def _no_history(self):
        return mock.patch.object(cal, "build_table", return_value={
            "timeframe": {}, "tf_score": {}, "score": {}, "strategy": {},
            "strategy_tf": {}, "overall": {"wins": 0, "total": 0},
        })

    def test_a_high_claim_is_capped_when_nothing_supports_it(self):
        with self._no_history():
            c, why = cal.calibrate(95.0, "4H")
        self.assertEqual(c, cal.NO_EVIDENCE_CEILING)
        self.assertEqual(why["capped_at"], cal.NO_EVIDENCE_CEILING)

    def test_a_modest_claim_passes_through_untouched(self):
        with self._no_history():
            c, why = cal.calibrate(50.0, "4H")
        self.assertEqual(c, 50.0)
        self.assertIsNone(why["capped_at"])

    def test_the_ceiling_is_below_anything_that_reads_as_conviction(self):
        self.assertLess(cal.NO_EVIDENCE_CEILING, 60.0)


if __name__ == "__main__":
    unittest.main()
