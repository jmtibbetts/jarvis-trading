"""One realized outcome must move exactly one number.

Historical performance previously reached the decision through five
independent paths that nested: calibrated confidence is a COMPONENT of the
composite score, the composite score is the BASE of the opportunity score,
and fusion then added the same win rate on top — so a good record moved
the same signal four times as though four studies had agreed.
"""
import unittest

from lib.historical_edge import (get_edge, describe_for_ui, MIN_SAMPLE_FOR_EDGE,
                                 PURPOSE_CALIBRATION, PURPOSE_UNCERTAINTY, PURPOSE_DISPLAY)

GOOD = {"total_trades": 40, "wins": 32, "win_rate": 0.8}
BAD = {"total_trades": 40, "wins": 8, "win_rate": 0.2}


class PurposeContractTests(unittest.TestCase):
    def test_uncertainty_consumers_cannot_see_the_win_rate(self):
        """The mechanism that prevents the double count: a consumer sizing
        for uncertainty is not given the number it must not re-judge."""
        edge = get_edge(GOOD, purpose=PURPOSE_UNCERTAINTY)
        self.assertNotIn("win_rate", edge)
        self.assertIn("sample", edge)
        self.assertIn("withheld", edge["note"])

    def test_calibration_consumers_do_see_it(self):
        edge = get_edge(GOOD, purpose=PURPOSE_CALIBRATION)
        self.assertAlmostEqual(edge["win_rate"], 0.8)
        self.assertIsNotNone(edge["shrunk_win_rate"])

    def test_unknown_purpose_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            get_edge(GOOD, purpose="whatever")

    def test_uncertainty_is_identical_for_opposite_records(self):
        """Same sample, opposite outcomes -> identical risk restraint."""
        self.assertEqual(get_edge(GOOD, purpose=PURPOSE_UNCERTAINTY),
                         get_edge(BAD, purpose=PURPOSE_UNCERTAINTY))


class ShrinkageTests(unittest.TestCase):
    def test_three_from_three_is_not_a_hundred_percent_edge(self):
        edge = get_edge({"total_trades": 3, "wins": 3}, purpose=PURPOSE_CALIBRATION)
        self.assertLess(edge["shrunk_win_rate"], 0.8)
        self.assertFalse(edge["proven"])

    def test_small_samples_are_not_evidence(self):
        for n in range(MIN_SAMPLE_FOR_EDGE):
            self.assertFalse(get_edge({"total_trades": n}, purpose=PURPOSE_CALIBRATION)["proven"])

    def test_shrinkage_relaxes_as_the_sample_grows(self):
        small = get_edge({"total_trades": 10, "wins": 8}, purpose=PURPOSE_CALIBRATION)
        large = get_edge({"total_trades": 400, "wins": 320}, purpose=PURPOSE_CALIBRATION)
        self.assertLess(abs(large["shrunk_win_rate"] - 0.8), abs(small["shrunk_win_rate"] - 0.8))

    def test_missing_history_never_invents_an_edge(self):
        edge = get_edge(None, purpose=PURPOSE_CALIBRATION)
        self.assertFalse(edge["proven"])
        self.assertIsNone(edge["shrunk_win_rate"])


class NormalisationTests(unittest.TestCase):
    def test_fraction_and_percentage_both_land_on_a_fraction(self):
        self.assertAlmostEqual(get_edge({"total_trades": 20, "win_rate": 0.8},
                                        purpose=PURPOSE_DISPLAY)["win_rate"], 0.8)
        self.assertAlmostEqual(get_edge({"total_trades": 20, "win_rate": 80.0},
                                        purpose=PURPOSE_DISPLAY)["win_rate"], 0.8)

    def test_win_rate_derived_from_counts_when_absent(self):
        self.assertAlmostEqual(get_edge({"total_trades": 40, "wins": 30},
                                        purpose=PURPOSE_DISPLAY)["win_rate"], 0.75)


class EndToEndSingleCountTests(unittest.TestCase):
    """The invariant, checked through the real scoring path."""

    def test_opportunity_score_ignores_the_record(self):
        from lib.signal_fusion import compute_opportunity_score
        good = compute_opportunity_score(60.0, "Long", historical=GOOD)
        bad = compute_opportunity_score(60.0, "Long", historical=BAD)
        self.assertEqual(good["opportunity_score"], bad["opportunity_score"])

    def test_leverage_ignores_the_record(self):
        from lib.leverage_policy import decide
        kw = dict(regime={"risk": "low"}, sample=40, consecutive_losses=0, atr_pct=1.0)
        self.assertEqual(decide(80, 55, win_rate=0.8, **kw)["leverage"],
                         decide(80, 55, win_rate=0.2, **kw)["leverage"])

    def test_calibration_is_the_one_place_it_still_moves_a_number(self):
        from lib.signal_scorer import _calibrate_confidence
        good, _ = _calibrate_confidence(70.0, {"total_trades": 40, "wins": 32})
        bad, _ = _calibrate_confidence(70.0, {"total_trades": 40, "wins": 8})
        self.assertGreater(good, bad)

    def test_display_still_tells_the_operator_everything(self):
        text = describe_for_ui(GOOD)
        self.assertIn("40 trades", text)
        self.assertIn("80%", text)


if __name__ == "__main__":
    unittest.main()
