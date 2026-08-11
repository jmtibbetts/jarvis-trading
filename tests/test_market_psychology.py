import unittest

from lib.market_psychology import (
    breadth_component, compute_psychology_index, compute_rate_of_change,
    funding_component, label_for, liquidation_component, long_short_component,
    vix_component,
)


class LabelTests(unittest.TestCase):
    def test_label_boundaries(self):
        self.assertEqual(label_for(0), "EXTREME_FEAR")
        self.assertEqual(label_for(19.9), "EXTREME_FEAR")
        self.assertEqual(label_for(20), "FEAR")
        self.assertEqual(label_for(50), "NEUTRAL")
        self.assertEqual(label_for(65), "GREED")
        self.assertEqual(label_for(85), "EXTREME_GREED")
        self.assertEqual(label_for(100), "EXTREME_GREED")

    def test_none_score_has_no_label(self):
        self.assertIsNone(label_for(None))


class VixComponentTests(unittest.TestCase):
    def test_low_vix_reads_as_greed(self):
        """Real shape from live data: VIX 15.5 against a year of history
        ranging 13.5-31.0 sits near the bottom — complacency, i.e. greed."""
        hist = [13.5 + i * 0.07 for i in range(252)]  # 13.5 .. ~31
        r = vix_component(15.5, hist)
        self.assertGreater(r["score"], 60)
        self.assertLess(r["percentile"], 40)

    def test_high_vix_reads_as_fear(self):
        hist = [13.5 + i * 0.07 for i in range(252)]
        r = vix_component(30.0, hist)
        self.assertLess(r["score"], 20)

    def test_abstains_without_enough_history(self):
        """Ranking against a handful of observations would be noise, so this
        abstains rather than emitting a confident-looking number."""
        self.assertIsNone(vix_component(15.0, [14.0, 15.0, 16.0]))
        self.assertIsNone(vix_component(15.0, None))
        self.assertIsNone(vix_component(None, [1.0] * 100))


class BreadthComponentTests(unittest.TestCase):
    def test_all_advancing_is_max_greed(self):
        self.assertEqual(breadth_component([1.0] * 10)["score"], 100.0)

    def test_all_declining_is_max_fear(self):
        self.assertEqual(breadth_component([-1.0] * 10)["score"], 0.0)

    def test_even_split_is_neutral(self):
        r = breadth_component([1.0] * 5 + [-1.0] * 5)
        self.assertEqual(r["score"], 50.0)
        self.assertEqual(r["advancing"], 5)
        self.assertEqual(r["declining"], 5)

    def test_abstains_on_thin_universe(self):
        self.assertIsNone(breadth_component([1.0, -1.0]))
        self.assertIsNone(breadth_component([]))

    def test_nones_are_excluded_not_counted_as_declining(self):
        r = breadth_component([1.0, 1.0, 1.0, 1.0, 1.0, None])
        self.assertEqual(r["universe_size"], 5)
        self.assertEqual(r["score"], 100.0)


class FundingComponentTests(unittest.TestCase):
    def test_zero_funding_is_neutral(self):
        self.assertEqual(funding_component([0.0])["score"], 50.0)

    def test_positive_funding_is_greed(self):
        self.assertGreater(funding_component([0.0003])["score"], 50)

    def test_negative_funding_is_fear(self):
        self.assertLess(funding_component([-0.0003])["score"], 50)

    def test_extreme_funding_clamps(self):
        self.assertEqual(funding_component([0.05])["score"], 100.0)
        self.assertEqual(funding_component([-0.05])["score"], 0.0)

    def test_abstains_with_no_data(self):
        self.assertIsNone(funding_component([]))
        self.assertIsNone(funding_component(None))


class LongShortComponentTests(unittest.TestCase):
    def test_balanced_ratio_is_neutral(self):
        self.assertEqual(long_short_component([1.0])["score"], 50.0)

    def test_ratio_is_symmetric_on_log_scale(self):
        """2.0 and 0.5 are the same imbalance in opposite directions and must
        sit equally far from neutral — the reason this uses log2 rather than a
        linear mapping."""
        high = long_short_component([2.0])["score"]
        low = long_short_component([0.5])["score"]
        self.assertAlmostEqual(high - 50, 50 - low, places=6)

    def test_more_longs_is_greed(self):
        self.assertGreater(long_short_component([1.66])["score"], 50)

    def test_ignores_nonpositive_ratios(self):
        self.assertIsNone(long_short_component([0.0, -1.0]))


class LiquidationComponentTests(unittest.TestCase):
    def test_longs_liquidated_is_fear(self):
        r = liquidation_component(1_000_000, 0)
        self.assertEqual(r["score"], 0.0)
        self.assertEqual(r["skew"], -1.0)

    def test_shorts_liquidated_is_greed(self):
        self.assertEqual(liquidation_component(0, 1_000_000)["score"], 100.0)

    def test_balanced_is_neutral(self):
        self.assertEqual(liquidation_component(500_000, 500_000)["score"], 50.0)

    def test_abstains_when_nothing_liquidated(self):
        self.assertIsNone(liquidation_component(0, 0))
        self.assertIsNone(liquidation_component(None, None))

    def test_skew_is_size_independent(self):
        """A small market and a large one with the same skew must score the
        same — the reading is about which side is losing, not volume."""
        small = liquidation_component(100, 300)["score"]
        large = liquidation_component(1_000_000, 3_000_000)["score"]
        self.assertEqual(small, large)


class CompositeTests(unittest.TestCase):
    def test_no_components_returns_none_not_fifty(self):
        """The critical guarantee: with no inputs the index must be None, not
        a neutral-looking 50 that reads as a real measurement."""
        r = compute_psychology_index({"vix": None, "breadth": None})
        self.assertIsNone(r["score"])
        self.assertIsNone(r["label"])
        self.assertEqual(r["components_available"], 0)

    def test_single_component_drives_score(self):
        r = compute_psychology_index({"vix": {"score": 80.0}})
        self.assertEqual(r["score"], 80.0)
        self.assertEqual(r["components_available"], 1)

    def test_absent_component_does_not_drag_toward_zero(self):
        """Weights renormalize over present components only. Two components
        both at 80 must yield 80, not 80 scaled down by the missing weight."""
        r = compute_psychology_index({"vix": {"score": 80.0}, "breadth": {"score": 80.0}})
        self.assertEqual(r["score"], 80.0)

    def test_weighted_blend(self):
        r = compute_psychology_index({"vix": {"score": 100.0}, "breadth": {"score": 0.0}})
        # vix weight .30, breadth .25 -> 100*.30/(.55) ≈ 54.5
        self.assertAlmostEqual(r["score"], 54.5, places=1)
        self.assertEqual(r["components_available"], 2)

    def test_components_always_returned_alongside_composite(self):
        comps = {"vix": {"score": 70.0, "detail": "x"}, "breadth": None}
        r = compute_psychology_index(comps)
        self.assertIn("vix", r["components"])
        self.assertIn("breadth", r["components"])

    def test_label_matches_score(self):
        self.assertEqual(compute_psychology_index({"vix": {"score": 90.0}})["label"], "EXTREME_GREED")
        self.assertEqual(compute_psychology_index({"vix": {"score": 10.0}})["label"], "EXTREME_FEAR")


class RateOfChangeTests(unittest.TestCase):
    def test_computes_delta_and_daily_rate(self):
        r = compute_rate_of_change(70.0, 50.0, hours=12)
        self.assertEqual(r["delta"], 20.0)
        self.assertEqual(r["per_day"], 40.0)
        self.assertEqual(r["direction"], "toward_greed")

    def test_falling_index(self):
        self.assertEqual(compute_rate_of_change(30.0, 60.0, hours=24)["direction"], "toward_fear")

    def test_missing_inputs_return_none(self):
        self.assertIsNone(compute_rate_of_change(None, 50.0, 12))
        self.assertIsNone(compute_rate_of_change(50.0, None, 12))
        self.assertIsNone(compute_rate_of_change(50.0, 40.0, 0))


if __name__ == "__main__":
    unittest.main()
