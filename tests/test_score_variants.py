"""The shadow variants must be shadows: pure functions, honest abstention.

Variant C encodes the 2026-08-13 decomposition — ta_confluence measured
-1.035%/trade top-to-bottom quintile while carrying the composite's largest
weight (0.20), conflict_ratio measured +0.758% while being fined 12 points,
volatility measured +1.051% on a 0.04 weight. A variant that silently
defaulted missing components would manufacture confident scores from
nothing, which is precisely the failure being corrected.
"""
import unittest

from lib.score_variants import (C_WEIGHTS, VARIANT_SCHEMA_VERSION,
                                compute_variants, variant_b, variant_c)

FULL = {
    "ta_confluence": 90.0, "conflict_ratio": 0.1, "volatility": 40.0,
    "regime": 80.0, "news": 60.0, "freshness": 95.0, "data_quality": 90.0,
    "liquidity": 70.0, "rr": 50.0, "calibrated_confidence": 65.0,
    "volume": 55.0,
}


class VariantBTests(unittest.TestCase):
    def test_b_is_the_mirror_of_the_live_score(self):
        self.assertEqual(variant_b(82.0), 18.0)
        self.assertEqual(variant_b(0.0), 100.0)

    def test_b_without_a_composite_abstains(self):
        self.assertIsNone(variant_b(None))
        self.assertIsNone(variant_b("not a number"))


class VariantCEncodesTheMeasurementsTests(unittest.TestCase):
    """Direction checks: each measured-inverted component must LOWER C as
    it rises; the fined-but-healthy conflict ratio must RAISE it."""

    def _score(self, **overrides):
        return variant_c({**FULL, **overrides})

    def test_high_ta_confluence_lowers_c(self):
        """The live formula's largest positive weight; measured -1.035%."""
        self.assertLess(self._score(ta_confluence=95.0),
                        self._score(ta_confluence=20.0))

    def test_conflict_raises_c(self):
        """The live formula fines conflict >= 0.5 twelve points; measured
        +0.758% with the best MFE in the table."""
        self.assertGreater(self._score(conflict_ratio=0.6),
                           self._score(conflict_ratio=0.0))

    def test_volatility_raises_c(self):
        self.assertGreater(self._score(volatility=90.0),
                           self._score(volatility=10.0))

    def test_measured_inverted_context_components_lower_c(self):
        for comp in ("regime", "news", "freshness"):
            self.assertLess(self._score(**{comp: 95.0}),
                            self._score(**{comp: 10.0}), comp)

    def test_weights_sum_to_one_and_flips_match_the_decomposition(self):
        self.assertAlmostEqual(sum(w for w, _ in C_WEIGHTS.values()), 1.0)
        flipped = {k for k, (_w, f) in C_WEIGHTS.items() if f}
        self.assertEqual(flipped, {"ta_confluence", "regime", "news", "freshness"})

    def test_c_stays_in_score_range(self):
        for bd in (FULL,
                   {**FULL, "ta_confluence": 0, "conflict_ratio": 1.0, "volatility": 100},
                   {**FULL, "ta_confluence": 100, "conflict_ratio": 0.0, "volatility": 0}):
            v = variant_c(bd)
            self.assertTrue(0.0 <= v <= 100.0, v)


class AbstentionTests(unittest.TestCase):
    """An unavailable prediction abstains; it never defaults favorably."""

    def test_empty_breakdown_yields_no_score(self):
        self.assertIsNone(variant_c({}))
        self.assertIsNone(variant_c(None))

    def test_missing_thesis_components_yield_no_score(self):
        """Without the components the decomposition turned on, the variant
        cannot test its own thesis and must say nothing."""
        self.assertIsNone(variant_c({"rr": 50.0, "news": 60.0}))
        no_vol = {k: v for k, v in FULL.items() if k != "volatility"}
        self.assertIsNone(variant_c(no_vol))

    def test_junk_component_values_do_not_raise(self):
        v = variant_c({**FULL, "regime": "broken", "rr": None})
        self.assertTrue(v is None or 0.0 <= v <= 100.0)


class ComputeVariantsTests(unittest.TestCase):
    def test_the_bundle_is_version_pinned(self):
        out = compute_variants(82.0, FULL)
        self.assertEqual(out["schema"], VARIANT_SCHEMA_VERSION)
        self.assertEqual(out["B"], 18.0)
        self.assertIsNotNone(out["C"])

    def test_no_variant_key_named_a(self):
        """A is the live composite, stored on the signal row itself —
        duplicating it here would invite the two copies to drift."""
        self.assertNotIn("A", compute_variants(82.0, FULL))



class LatenessFeatureTests(unittest.TestCase):
    """preceding_move_pct rides in the breakdown as a recorded measurement.

    It must NOT enter the composite: outcomes against it are U-shaped
    (early entries and heavy momentum both win, the middle loses), so any
    hand-chosen sign would be wrong for half the distribution. It exists
    for the shadow variants and future models to learn from.
    """

    def test_it_is_recorded_signed_into_the_trade_direction(self):
        from lib.signal_scorer import score_signal
        ta = {"4H": {"bias": "bullish", "rsi": 60, "macd": {},
                     "preceding_return_5": 2.5}}
        long_sig = {"asset_symbol": "T/USD", "direction": "Long",
                    "timeframe": "4H", "entry_price": 100, "target_price": 110,
                    "stop_loss": 95, "confidence": 60}
        out = score_signal(dict(long_sig), ta, {}, set())
        self.assertEqual(out["score_breakdown"]["preceding_move_pct"], 2.5)
        out = score_signal({**long_sig, "direction": "Short"}, ta, {}, set())
        self.assertEqual(out["score_breakdown"]["preceding_move_pct"], -2.5)

    def test_missing_ta_yields_none_not_zero(self):
        """Zero would train as 'no preceding move', which is a claim."""
        from lib.signal_scorer import score_signal
        out = score_signal({"asset_symbol": "T/USD", "direction": "Long",
                            "timeframe": "4H", "entry_price": 100,
                            "target_price": 110, "stop_loss": 95,
                            "confidence": 60}, {}, {}, set())
        self.assertIsNone(out["score_breakdown"]["preceding_move_pct"])

    def test_it_does_not_move_the_composite(self):
        """The measurement is cargo, not a scoring input."""
        from lib.signal_scorer import score_signal
        base = {"asset_symbol": "T/USD", "direction": "Long", "timeframe": "4H",
                "entry_price": 100, "target_price": 110, "stop_loss": 95,
                "confidence": 60}
        ta_a = {"4H": {"bias": "bullish", "rsi": 60, "macd": {},
                       "preceding_return_5": 0.1}}
        ta_b = {"4H": {"bias": "bullish", "rsi": 60, "macd": {},
                       "preceding_return_5": 9.9}}
        a = score_signal(dict(base), ta_a, {}, set())
        b = score_signal(dict(base), ta_b, {}, set())
        self.assertEqual(a["composite_score"], b["composite_score"])
if __name__ == "__main__":
    unittest.main()
