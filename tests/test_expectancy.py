"""A win rate is not an edge, and a gross edge is not a business.

45% wins averaging +2R against -1R losses is profitable. 60% wins
averaging +0.4R against -1R losses is a slow bankruptcy — and the second
one looks better on every dashboard this codebase had, because the
composite score ranked setups by how much evidence supported them, which
is a different question from how much money they make.

Costs finish the argument. Measured live: a BTC 15m setup with a 0.08%
stop carries 13R of round-trip cost. No win rate rescues that, and nothing
in the engine could say so — the setup would score well and be taken.
"""
import unittest

from lib.expectancy import (HIERARCHY, MIN_NET_R, MIN_SAMPLE, NO_TRADE,
                            _r_of, evaluate, lookup, summary, wilson_interval)


class ResultInRTests(unittest.TestCase):
    """R is the only unit in which a 15m scalp and a weekly position can be
    averaged together. 1% is a full stop on one and noise on the other."""

    def test_a_full_stop_is_minus_one_r(self):
        self.assertAlmostEqual(_r_of(100, 98, 98, "Long"), -1.0, places=6)

    def test_target_at_three_times_risk_is_three_r(self):
        self.assertAlmostEqual(_r_of(100, 98, 106, "Long"), 3.0, places=6)

    def test_shorts_are_measured_the_same_way(self):
        self.assertAlmostEqual(_r_of(100, 102, 94, "Short"), 3.0, places=6)
        self.assertAlmostEqual(_r_of(100, 102, 102, "Short"), -1.0, places=6)

    def test_no_risk_distance_is_not_computable(self):
        self.assertIsNone(_r_of(100, 100, 105, "Long"))
        self.assertIsNone(_r_of(None, 98, 105, "Long"))
        self.assertIsNone(_r_of(100, 98, None, "Long"))


class WilsonTests(unittest.TestCase):
    """A 60% win rate over 10 trades and over 4,000 are different claims. A
    point estimate makes them look identical."""

    def test_a_thin_sample_produces_a_wide_interval(self):
        lo10, hi10 = wilson_interval(6, 10)
        lo4k, hi4k = wilson_interval(2400, 4000)
        self.assertGreater(hi10 - lo10, hi4k - lo4k)

    def test_it_stays_inside_zero_and_one(self):
        for wins, n in ((0, 5), (5, 5), (0, 1), (1, 1)):
            lo, hi = wilson_interval(wins, n)
            self.assertGreaterEqual(lo, 0.0)
            self.assertLessEqual(hi, 1.0)

    def test_no_sample_claims_nothing(self):
        self.assertEqual(wilson_interval(0, 0), (0.0, 1.0))


class HierarchyTests(unittest.TestCase):
    """A number from 12 trades in an exactly-matching bucket is worse than
    one from 4,000 in a broader one — so the answer must say which it came
    from."""

    def test_the_first_level_is_the_most_specific(self):
        self.assertEqual(HIERARCHY[0],
                         ("strategy", "asset_class", "direction", "timeframe"))

    def test_every_level_narrows_toward_overall(self):
        self.assertEqual(HIERARCHY[-1], ())
        for a, b in zip(HIERARCHY, HIERARCHY[1:]):
            self.assertGreaterEqual(len(a), len(b))

    def test_a_lookup_names_its_bucket_and_sample(self):
        got = lookup("unclassified", "equity", "long", "4H")
        if got is None:
            self.skipTest("no outcome history in this environment")
        self.assertIn("bucket", got)
        self.assertGreaterEqual(got["sample"], MIN_SAMPLE)
        self.assertIn("exact_match", got)

    def test_an_unknown_combination_falls_back_rather_than_failing(self):
        got = lookup("no_such_strategy", "equity", "long", "4H")
        if got is None:
            self.skipTest("no outcome history in this environment")
        self.assertFalse(got["exact_match"],
                         "an unmatched strategy reported an exact match")


class NetExpectancyDecidesTests(unittest.TestCase):

    def _sig(self, symbol, cls, direction, tf, entry, stop):
        return {"asset_symbol": symbol, "asset_class": cls, "direction": direction,
                "timeframe": tf, "entry_price": entry, "stop_loss": stop}

    def test_a_stop_inside_the_spread_is_refused(self):
        """The measured case: costs of 13R on a 0.08% stop. No win rate
        rescues that, and the old scorer would have ranked it well."""
        r = evaluate(self._sig("BTC/USD", "crypto", "Long", "15m", 63800.0, 63750.0))
        if r["verdict"] == "UNKNOWN":
            self.skipTest("no outcome history in this environment")
        self.assertEqual(r["verdict"], NO_TRADE)
        self.assertLess(r["net"]["net_expected_r"], 0)

    def test_a_wide_stop_on_a_measured_edge_is_allowed(self):
        r = evaluate(self._sig("NVDA", "equity", "Long", "4H", 224.0, 218.0))
        if r["verdict"] == "UNKNOWN":
            self.skipTest("no outcome history in this environment")
        self.assertEqual(r["verdict"], "TRADE")
        self.assertGreaterEqual(r["net"]["net_expected_r"], MIN_NET_R)

    def test_the_same_setup_flips_on_stop_width_alone(self):
        """Identical symbol, direction and timeframe — only the risk
        distance differs, and that alone decides whether costs eat it."""
        wide = evaluate(self._sig("NVDA", "equity", "Long", "4H", 224.0, 218.0))
        tight = evaluate(self._sig("NVDA", "equity", "Long", "4H", 224.0, 223.5))
        if wide["verdict"] == "UNKNOWN":
            self.skipTest("no outcome history in this environment")
        self.assertEqual(wide["verdict"], "TRADE")
        self.assertEqual(tight["verdict"], NO_TRADE)

    def test_the_reason_shows_the_arithmetic(self):
        r = evaluate(self._sig("NVDA", "equity", "Long", "4H", 224.0, 218.0))
        if r["verdict"] == "UNKNOWN":
            self.skipTest("no outcome history in this environment")
        self.assertIn("R", r["reason"])
        self.assertIn("trades", r["reason"])


class NeverNoTradeOnMissingDataTests(unittest.TestCase):
    """NO_TRADE must mean measured negative expectancy. Refusing on absent
    evidence is how a system talks itself into never trading — the exact
    deadlock the epoch quarantine produced earlier."""

    def test_no_history_is_unknown_not_no_trade(self):
        r = evaluate({"asset_symbol": "ZZZZ", "asset_class": "equity",
                      "direction": "Long", "timeframe": "13h",
                      "entry_price": 100.0, "stop_loss": 98.0,
                      "strategy": "nothing_like_this"})
        self.assertIn(r["verdict"], ("TRADE", NO_TRADE, "UNKNOWN"))
        if r["expectancy"] is None:
            self.assertEqual(r["verdict"], "UNKNOWN")

    def test_no_risk_distance_is_unknown_not_no_trade(self):
        r = evaluate({"asset_symbol": "NVDA", "asset_class": "equity",
                      "direction": "Long", "timeframe": "4H",
                      "entry_price": 100.0, "stop_loss": 100.0})
        self.assertEqual(r["verdict"], "UNKNOWN")

    def test_a_missing_signal_does_not_raise(self):
        for bad in ({}, {"asset_symbol": "X"}, {"entry_price": "n/a"}):
            self.assertIn(evaluate(bad)["verdict"], ("TRADE", NO_TRADE, "UNKNOWN"))


class RobustnessTests(unittest.TestCase):
    """When the point estimate says TRADE and the lower bound of the
    win-rate interval says otherwise, the edge is inside the noise."""

    def test_robust_is_reported_separately_from_the_verdict(self):
        r = evaluate({"asset_symbol": "NVDA", "asset_class": "equity",
                      "direction": "Long", "timeframe": "4H",
                      "entry_price": 224.0, "stop_loss": 218.0})
        if r["verdict"] == "UNKNOWN":
            self.skipTest("no outcome history in this environment")
        self.assertIn("robust", r)
        self.assertIsNotNone(r["net_lower"])

    def test_the_lower_bound_is_never_the_more_optimistic_number(self):
        got = lookup("unclassified", "equity", "long", "4H")
        if got is None:
            self.skipTest("no outcome history in this environment")
        self.assertLessEqual(got["gross_expected_r_lower"], got["gross_expected_r"])


class SummaryTests(unittest.TestCase):

    def test_only_buckets_with_evidence_are_published(self):
        s = summary()
        for b in s["buckets"]:
            self.assertGreaterEqual(b["sample"], MIN_SAMPLE, b["bucket"])

    def test_every_row_carries_its_denominator(self):
        for b in summary()["buckets"]:
            self.assertIn("raw_sample", b)
            self.assertIn("p_win_ci", b)


if __name__ == "__main__":
    unittest.main()


class WiredIntoTheScorerTests(unittest.TestCase):
    """A gate nothing consults is a report. The verdict has to travel with
    the signal so downstream execution can refuse it."""

    def _score(self, entry, stop, tf="4H", symbol="NVDA", cls="equity"):
        from lib.signal_scorer import score_signal
        return score_signal(
            {"asset_symbol": symbol, "asset_class": cls, "direction": "Long",
             "confidence": 70, "timeframe": tf, "entry_price": entry,
             "stop_loss": stop, "target_price": entry * 1.06},
            {tf: {"bias": "bullish", "rsi": 55}}, {"risk": "medium"})

    def test_the_verdict_reaches_the_breakdown(self):
        out = self._score(224.0, 218.0)
        ev = out["score_breakdown"].get("expectancy")
        if not ev or ev["verdict"] == "UNKNOWN":
            self.skipTest("no outcome history in this environment")
        self.assertIn(ev["verdict"], ("TRADE", NO_TRADE))
        self.assertIn("net_expected_r", ev)

    def test_a_cost_doomed_setup_is_flagged_no_trade_on_the_signal(self):
        out = self._score(63800.0, 63750.0, tf="15m", symbol="BTC/USD", cls="crypto")
        ev = out["score_breakdown"].get("expectancy")
        if not ev or ev["verdict"] == "UNKNOWN":
            self.skipTest("no outcome history in this environment")
        self.assertEqual(ev["verdict"], NO_TRADE)
        self.assertTrue(out.get("no_trade"))
        self.assertTrue(out.get("no_trade_reason"))

    def test_a_viable_setup_is_not_flagged(self):
        out = self._score(224.0, 218.0)
        ev = out["score_breakdown"].get("expectancy")
        if not ev or ev["verdict"] != "TRADE":
            self.skipTest("bucket not tradeable in this environment")
        self.assertFalse(out.get("no_trade"))

    def test_the_sample_size_travels_with_the_verdict(self):
        """A verdict without its denominator is the thing this whole
        codebase keeps getting burned by."""
        out = self._score(224.0, 218.0)
        ev = out["score_breakdown"].get("expectancy")
        if not ev or ev["verdict"] == "UNKNOWN":
            self.skipTest("no outcome history in this environment")
        self.assertIsNotNone(ev["sample"])
        self.assertIsNotNone(ev["bucket"])
