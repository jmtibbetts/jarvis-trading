"""Agreement between independent measurements is evidence. Agreement
between restatements of one measurement is not.

Two defects this replaces:

RSI, Stochastic, CCI, Williams %R and MFI are five arithmetic treatments of
"where is price inside its recent range". When price is extended they all
say so at once, and the old scorer counted that as five confirmations —
which is how a signal reached high confidence on a single fact.

And disagreement was averaged into one `ta_confluence` number, so a setup
with bullish structure and bearish flow scored the same as one with two
mild positives. A mean is the one summary guaranteed to hide a
contradiction.
"""
import unittest

from lib.evidence import (CATEGORY_WEIGHTS, MAX_CONTRADICTION_PENALTY,
                          contradiction_penalty, gather)


def bullish_structure():
    """Price broken out and above its channels, nothing else stated."""
    return {
        "donchian": {"breakout_up": True, "breakout_down": False},
        "keltner": {"position": "above"},
        "support_resistance": {"position_in_range": 0.5},
    }


def bearish_flow():
    """Distribution: the classic objection a price thesis has to answer."""
    return {"obv_trend": "falling", "mfi": 85, "volume": {"dry": True}}


def extended_momentum():
    """One observation — price is stretched — reported five ways."""
    return {
        "rsi": 78,
        "stochastic": {"signal": "overbought"},
        "cci": 150,
        "williams_r": -8,
        "macd": {"crossover": "bearish"},
    }


class CorrelatedIndicatorsCountOnceTests(unittest.TestCase):

    def test_five_restatements_produce_one_category(self):
        ev = gather(extended_momentum(), "Short")
        momentum = [c for c in ev["supporting"] if c["category"] == "momentum"]
        self.assertEqual(len(momentum), 1, "momentum must resolve to a single verdict")

    def test_they_cannot_outweigh_independent_categories(self):
        """Five agreeing momentum readings must not outweigh structure and
        trend combined — that inversion is the whole defect."""
        d = {**extended_momentum(), **bullish_structure(),
             "supertrend": {"direction": "up"}, "ema": {"ema9": 11, "ema21": 10},
             "adx": {"value": 30}}
        ev = gather(d, "Long")
        mom = next((c for c in ev["contradicting"] if c["category"] == "momentum"), None)
        self.assertIsNotNone(mom, "extended momentum argues against a long here")
        mom_w = mom["weight"] * mom["strength"]
        self.assertLess(mom_w, ev["supporting_weight"])

    def test_a_category_that_disagrees_with_itself_is_not_evidence(self):
        """RSI oversold while Stochastic is overbought is not a signal."""
        ev = gather({"rsi": 25, "stochastic": {"signal": "overbought"}}, "Long")
        mom = next(c for c in ev["neutral"] if c["category"] == "momentum")
        self.assertEqual(mom["verdict"], "neutral")
        self.assertEqual(mom["strength"], 0.0)


class BothSidesSurviveTests(unittest.TestCase):
    """The acceptance condition from the plan: a signal with bullish price
    and bearish flow must SHOW both, and score below one with neither."""

    def setUp(self):
        self.conflicted = gather({**bullish_structure(), **bearish_flow()}, "Long")
        self.clean = gather(bullish_structure(), "Long")

    def test_the_supporting_side_is_listed(self):
        self.assertTrue(self.conflicted["supporting"])
        self.assertIn("structure", [c["category"] for c in self.conflicted["supporting"]])

    def test_the_contradicting_side_is_listed_not_absorbed(self):
        cats = [c["category"] for c in self.conflicted["contradicting"]]
        self.assertIn("volume", cats)
        self.assertEqual(self.conflicted["contradiction_count"], 1)

    def test_the_objection_is_readable_not_just_counted(self):
        vol = next(c for c in self.conflicted["contradicting"] if c["category"] == "volume")
        self.assertTrue(vol["readings"])
        self.assertTrue(any("OBV" in r or "distribution" in r for r in vol["readings"]))

    def test_the_summary_names_both_sides(self):
        s = self.conflicted["summary"]
        self.assertIn("structure", s)
        self.assertIn("volume", s)
        self.assertIn("contradict", s)

    def test_a_contradicted_setup_confluences_below_a_clean_one(self):
        self.assertLess(self.conflicted["confluence"], self.clean["confluence"])

    def test_a_contradicted_setup_is_penalised_and_a_clean_one_is_not(self):
        self.assertGreater(contradiction_penalty(self.conflicted), 0)
        self.assertEqual(contradiction_penalty(self.clean), 0.0)


class PenaltyShapeTests(unittest.TestCase):

    def test_more_independent_objections_cost_more(self):
        one = gather({**bullish_structure(), **bearish_flow()}, "Long")
        two = gather({**bullish_structure(), **bearish_flow(),
                      "supertrend": {"direction": "down"},
                      "ema": {"ema9": 9, "ema21": 10},
                      "vwap": {"position": "below"},
                      "adx": {"value": 32}}, "Long")
        self.assertGreater(two["contradiction_count"], one["contradiction_count"])
        self.assertGreater(contradiction_penalty(two), contradiction_penalty(one))

    def test_a_heavier_category_objects_more_loudly(self):
        """Trend disagreeing outweighs volatility being unhelpful."""
        self.assertGreater(CATEGORY_WEIGHTS["trend"], CATEGORY_WEIGHTS["volatility"])

    def test_the_penalty_is_capped(self):
        everything_against = gather({
            "donchian": {"breakout_down": True}, "keltner": {"position": "below"},
            "support_resistance": {"position_in_range": 0.95},
            "supertrend": {"direction": "down"}, "ema": {"ema9": 9, "ema21": 10},
            "vwap": {"position": "below"}, "adx": {"value": 40},
            **extended_momentum(), **bearish_flow(), "atr": {"pct": 25},
        }, "Long")
        self.assertLessEqual(contradiction_penalty(everything_against),
                             MAX_CONTRADICTION_PENALTY)

    def test_direction_flips_which_side_evidence_lands_on(self):
        d = {**bullish_structure(), **bearish_flow()}
        long_ev, short_ev = gather(d, "Long"), gather(d, "Short")
        self.assertIn("structure", [c["category"] for c in long_ev["supporting"]])
        self.assertIn("structure", [c["category"] for c in short_ev["contradicting"]])


class NonDirectionalAndMissingDataTests(unittest.TestCase):

    def test_volatility_only_objects_it_never_endorses(self):
        """ATR says whether levels are readable, not which way to trade."""
        for direction in ("Long", "Short"):
            ev = gather({"atr": {"pct": 30}}, direction)
            self.assertNotIn("volatility", [c["category"] for c in ev["supporting"]])

    def test_workable_volatility_is_neutral_not_supportive(self):
        ev = gather({"atr": {"pct": 2.0}}, "Long")
        self.assertIn("volatility", [c["category"] for c in ev["neutral"]])

    def test_no_data_is_no_evidence_rather_than_agreement(self):
        ev = gather({}, "Long")
        self.assertEqual(ev["supporting"], [])
        self.assertEqual(ev["contradicting"], [])
        self.assertEqual(ev["confluence"], 50.0)
        self.assertEqual(contradiction_penalty(ev), 0.0)

    def test_junk_values_do_not_raise(self):
        for d in ({"rsi": "n/a"}, {"atr": {"pct": None}}, {"adx": {"value": "x"}},
                  {"volume": None}, {"donchian": "nope"}):
            self.assertIsInstance(gather(d, "Long"), dict)

    def test_crypto_positioning_reads_as_contrarian(self):
        """Crowded longs paying funding is an objection to buying, not a
        confirmation of it."""
        ev = gather({}, "Long", derivatives={"funding_rate": 0.002})
        self.assertIn("derivatives", [c["category"] for c in ev["contradicting"]])


class WiredIntoTheScoreTests(unittest.TestCase):
    """Exposed but unused would leave the measured failure mode in place."""

    def _score(self, ta_4h):
        from lib.signal_scorer import score_signal
        sig = {"asset_symbol": "TEST", "direction": "Long", "confidence": 70,
               "timeframe": "4H", "entry_price": 100.0, "target_price": 106.0,
               "stop_loss": 98.0}
        return score_signal(dict(sig), {"4H": dict(ta_4h)}, {"risk": "medium"})

    def test_the_scorer_exposes_both_sides(self):
        out = self._score({**bullish_structure(), **bearish_flow(), "bias": "bullish"})
        ev = out.get("evidence")
        self.assertIsNotNone(ev, "score_signal must attach category evidence")
        self.assertTrue(ev["contradicting"])
        self.assertTrue(ev["supporting"])

    def test_contradiction_lowers_the_composite(self):
        clean = self._score({**bullish_structure(), "bias": "bullish"})
        dirty = self._score({**bullish_structure(), **bearish_flow(), "bias": "bullish"})
        self.assertLess(dirty["composite_score"], clean["composite_score"])

    def test_the_penalty_is_itemised_in_the_breakdown(self):
        out = self._score({**bullish_structure(), **bearish_flow(), "bias": "bullish"})
        bd = out["score_breakdown"]
        self.assertLess(bd["contradiction_penalty"], 0)
        self.assertGreaterEqual(bd["contradiction_count"], 1)


if __name__ == "__main__":
    unittest.main()


class NonDirectionalCategoriesOnlyObjectTests(unittest.TestCase):
    """Volatility has no opinion on direction. Left on the ordinary path it
    was recorded as SUPPORTING a short whenever conditions were too wild to
    read anything — "the chaos agrees with me"."""

    WILD = {"atr": {"pct": 30}}

    def test_it_never_supports_either_direction(self):
        for direction in ("Long", "Short"):
            ev = gather(self.WILD, direction)
            self.assertNotIn("volatility", [c["category"] for c in ev["supporting"]], direction)

    def test_it_objects_to_both_directions_identically(self):
        long_p = contradiction_penalty(gather(self.WILD, "Long"))
        short_p = contradiction_penalty(gather(self.WILD, "Short"))
        self.assertEqual(long_p, short_p)
        self.assertGreater(long_p, 0)

    def test_it_is_labelled_a_caveat_not_a_disagreement(self):
        ev = gather(self.WILD, "Long")
        vol = next(c for c in ev["contradicting"] if c["category"] == "volatility")
        self.assertEqual(vol["verdict"], "caveat")


class PersistedFormIsCompactTests(unittest.TestCase):
    """score_breakdown is stored as JSON on every signal row and the scanner
    alone writes ~1,300 a day. The full block is ~2KB, most of it neutral
    categories saying nothing."""

    def setUp(self):
        from lib.evidence import compact
        self.full = gather({**bullish_structure(), **bearish_flow(),
                            "atr": {"pct": 2.0}}, "Long")
        self.small = compact(self.full)

    def test_both_sides_survive_compaction(self):
        self.assertEqual(len(self.small["supporting"]), len(self.full["supporting"]))
        self.assertEqual(len(self.small["contradicting"]), len(self.full["contradicting"]))

    def test_the_readings_survive(self):
        for c in self.small["contradicting"]:
            self.assertTrue(c["readings"])

    def test_neutral_categories_are_named_but_not_expanded(self):
        """"We looked there and found nothing" must stay recoverable — it is
        different from never having looked."""
        self.assertIn("neutral_categories", self.small)
        self.assertTrue(all(isinstance(x, str) for x in self.small["neutral_categories"]))

    def test_it_is_materially_smaller(self):
        import json
        big = len(json.dumps(self.full, default=str))
        small = len(json.dumps(self.small, default=str))
        self.assertLess(small, big)

    def test_none_in_none_out(self):
        from lib.evidence import compact
        self.assertIsNone(compact(None))

    def test_the_scorer_persists_the_compact_form(self):
        from lib.signal_scorer import score_signal
        out = score_signal(
            {"asset_symbol": "T", "direction": "Long", "confidence": 70,
             "timeframe": "4H", "entry_price": 100.0, "target_price": 106.0,
             "stop_loss": 98.0},
            {"4H": {**bullish_structure(), **bearish_flow(), "bias": "bullish"}},
            {"risk": "medium"})
        stored = out["score_breakdown"]["evidence"]
        self.assertIn("neutral_categories", stored)
        self.assertNotIn("neutral", stored)
