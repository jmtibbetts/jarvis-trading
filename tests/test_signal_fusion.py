import unittest

from lib.signal_fusion import (
    compute_anomaly_flags, compute_dark_pool_component, compute_insider_component,
    compute_opportunity_score, compute_options_component, compute_smart_money_alignment,
)


class InsiderComponentTests(unittest.TestCase):
    def test_none_cluster_returns_none(self):
        self.assertIsNone(compute_insider_component(None))

    def test_zero_net_returns_zero_score(self):
        result = compute_insider_component({"net_value": 0, "flags": []})
        self.assertEqual(result["score"], 0.0)

    def test_positive_net_gives_positive_score(self):
        result = compute_insider_component({"net_value": 1_000_000, "flags": []})
        self.assertGreater(result["score"], 0)
        self.assertAlmostEqual(result["score"], 50.0, places=1)

    def test_negative_net_gives_negative_score(self):
        result = compute_insider_component({"net_value": -1_000_000, "flags": []})
        self.assertLess(result["score"], 0)

    def test_officer_buying_boosts_score(self):
        plain = compute_insider_component({"net_value": 500_000, "flags": []})
        boosted = compute_insider_component({"net_value": 500_000, "flags": ["OFFICER_BUYING"]})
        self.assertGreater(boosted["score"], plain["score"])

    def test_score_caps_at_100(self):
        result = compute_insider_component({"net_value": 50_000_000, "flags": ["OFFICER_BUYING"]})
        self.assertEqual(result["score"], 100.0)


class OptionsComponentTests(unittest.TestCase):
    def test_none_returns_none(self):
        self.assertIsNone(compute_options_component(None))

    def test_missing_ratio_returns_null_score_not_crash(self):
        result = compute_options_component({"put_call_ratio": None})
        self.assertIsNone(result["score"])

    def test_ratio_below_1_is_bullish_tilt(self):
        result = compute_options_component({"put_call_ratio": 0.5, "iv_skew": 0.02})
        self.assertGreater(result["score"], 0)

    def test_ratio_above_1_is_bearish_tilt(self):
        result = compute_options_component({"put_call_ratio": 2.0, "iv_skew": 0.02})
        self.assertLess(result["score"], 0)

    def test_ratio_of_1_is_neutral(self):
        result = compute_options_component({"put_call_ratio": 1.0})
        self.assertEqual(result["score"], 0.0)


class DarkPoolComponentTests(unittest.TestCase):
    def test_none_returns_none(self):
        self.assertIsNone(compute_dark_pool_component(None))

    def test_no_wow_baseline_gives_flat_floor_score(self):
        result = compute_dark_pool_component({"wow_pct": None})
        self.assertEqual(result["activity_score"], 30.0)

    def test_large_wow_change_raises_activity_score_regardless_of_sign(self):
        up = compute_dark_pool_component({"wow_pct": 80.0})
        down = compute_dark_pool_component({"wow_pct": -80.0})
        self.assertEqual(up["activity_score"], down["activity_score"])
        self.assertGreater(up["activity_score"], 30.0)

    def test_activity_score_caps_at_100(self):
        result = compute_dark_pool_component({"wow_pct": 500.0})
        self.assertEqual(result["activity_score"], 100.0)


class SmartMoneyAlignmentTests(unittest.TestCase):
    def test_no_sources_returns_no_data(self):
        result = compute_smart_money_alignment()
        self.assertIsNone(result["alignment_score"])
        self.assertEqual(result["agreement"], "no_data")

    def test_dark_pool_alone_does_not_produce_directional_score(self):
        """Core correctness guarantee: dark pool activity alone (no insider/
        options) must never produce a directional alignment score, since
        FINRA's data has no buy/sell direction."""
        result = compute_smart_money_alignment(dark_pool={"activity_score": 90.0, "wow_pct": 120.0})
        self.assertIsNone(result["alignment_score"])
        self.assertEqual(result["agreement"], "no_data")
        self.assertEqual(result["components"]["dark_pool_activity"]["activity_score"], 90.0)

    def test_single_directional_source(self):
        result = compute_smart_money_alignment(insider={"score": 60.0, "net_value": 3e6, "flags": []})
        self.assertEqual(result["agreement"], "single_source")
        self.assertGreater(result["alignment_score"], 50)

    def test_aligned_sources(self):
        result = compute_smart_money_alignment(
            insider={"score": 40.0, "net_value": 1e6, "flags": []},
            options={"score": 30.0, "put_call_ratio": 0.6},
        )
        self.assertEqual(result["agreement"], "aligned")
        self.assertGreater(result["alignment_score"], 50)

    def test_mixed_sources(self):
        result = compute_smart_money_alignment(
            insider={"score": 60.0, "net_value": 3e6, "flags": []},
            options={"score": -60.0, "put_call_ratio": 2.5},
        )
        self.assertEqual(result["agreement"], "mixed")
        self.assertAlmostEqual(result["alignment_score"], 50.0, places=1)


class OpportunityScoreTests(unittest.TestCase):
    def test_no_smart_money_or_history_returns_base_unchanged(self):
        result = compute_opportunity_score(70.0, "Long")
        self.assertEqual(result["opportunity_score"], 70.0)
        self.assertEqual(result["breakdown"]["smart_money_adjustment"], 0.0)

    def test_confirming_smart_money_boosts_long_signal(self):
        smart_money = {"alignment_score": 90.0}
        result = compute_opportunity_score(60.0, "Long", smart_money=smart_money)
        self.assertGreater(result["opportunity_score"], 60.0)
        self.assertIn("confirm", result["breakdown"]["smart_money_note"])

    def test_conflicting_smart_money_lowers_long_signal(self):
        smart_money = {"alignment_score": 10.0}
        result = compute_opportunity_score(60.0, "Long", smart_money=smart_money)
        self.assertLess(result["opportunity_score"], 60.0)
        self.assertIn("conflict", result["breakdown"]["smart_money_note"])

    def test_confirming_smart_money_boosts_short_signal_when_bearish(self):
        smart_money = {"alignment_score": 10.0}
        result = compute_opportunity_score(60.0, "Short", smart_money=smart_money)
        self.assertGreater(result["opportunity_score"], 60.0)

    def test_score_clamped_to_100(self):
        smart_money = {"alignment_score": 100.0}
        historical = {"total_trades": 50, "win_rate": 1.0}
        result = compute_opportunity_score(99.0, "Long", smart_money=smart_money, historical=historical)
        self.assertEqual(result["opportunity_score"], 100.0)

    def test_score_clamped_to_0(self):
        smart_money = {"alignment_score": 0.0}
        historical = {"total_trades": 50, "win_rate": 0.0}
        result = compute_opportunity_score(1.0, "Long", smart_money=smart_money, historical=historical)
        self.assertEqual(result["opportunity_score"], 0.0)

    def test_insufficient_history_is_not_scored(self):
        result = compute_opportunity_score(60.0, "Long", historical={"total_trades": 2, "win_rate": 1.0})
        self.assertEqual(result["breakdown"]["historical_adjustment"], 0.0)

    def test_history_no_longer_moves_the_opportunity_score(self):
        """History reaches this score ALREADY, through calibrated confidence
        inside the base composite. Adding it again here counted one outcome
        twice — see lib/historical_edge."""
        good = compute_opportunity_score(60.0, "Long", historical={"total_trades": 20, "win_rate": 0.8})
        bad = compute_opportunity_score(60.0, "Long", historical={"total_trades": 20, "win_rate": 0.2})
        self.assertEqual(good["opportunity_score"], bad["opportunity_score"])
        self.assertEqual(good["breakdown"]["historical_adjustment"], 0.0)

    def test_history_is_still_shown_to_the_operator(self):
        """Removed from the arithmetic, not from the interface."""
        out = compute_opportunity_score(60.0, "Long", historical={"total_trades": 20, "win_rate": 0.8})
        note = out["breakdown"]["historical_note"]
        self.assertIn("20 trades", note)
        self.assertIn("80% win rate", note)
        self.assertIn("already counted", note)

    def test_win_rate_fraction_and_percentage_both_normalise(self):
        """The original bug this file guarded: signal_accuracy stores a
        FRACTION. Normalisation moved into historical_edge, so it is tested
        there rather than through a score that no longer varies."""
        from lib.historical_edge import get_edge, PURPOSE_DISPLAY
        as_fraction = get_edge({"total_trades": 20, "win_rate": 0.8}, purpose=PURPOSE_DISPLAY)
        as_percent = get_edge({"total_trades": 20, "win_rate": 80.0}, purpose=PURPOSE_DISPLAY)
        self.assertAlmostEqual(as_fraction["win_rate"], 0.8)
        self.assertAlmostEqual(as_percent["win_rate"], 0.8)

    def test_exactly_even_win_rate_is_neutral(self):
        result = compute_opportunity_score(60.0, "Long", historical={"total_trades": 20, "win_rate": 0.5})
        self.assertEqual(result["breakdown"]["historical_adjustment"], 0.0)


class AnomalyFlagsTests(unittest.TestCase):
    def test_no_sources_gives_null_score(self):
        result = compute_anomaly_flags()
        self.assertIsNone(result["anomaly_score"])
        self.assertEqual(result["flags"], [])

    def test_dark_pool_spike_flagged(self):
        result = compute_anomaly_flags(dark_pool={"wow_pct": 75.0})
        self.assertEqual(len(result["flags"]), 1)
        self.assertEqual(result["flags"][0]["flag"], "DARK_POOL_WOW_SPIKE")
        self.assertEqual(result["anomaly_score"], 100.0)

    def test_small_wow_change_not_flagged(self):
        result = compute_anomaly_flags(dark_pool={"wow_pct": 5.0})
        self.assertEqual(result["flags"], [])
        self.assertEqual(result["anomaly_score"], 0.0)

    def test_large_liquidation_flagged(self):
        result = compute_anomaly_flags(liquidation_summary={"total_liquidated_usd": 10_000_000})
        self.assertEqual(len(result["flags"]), 1)
        self.assertEqual(result["flags"][0]["flag"], "LARGE_LIQUIDATION_CLUSTER")

    def test_extreme_put_call_ratio_flagged_both_directions(self):
        high = compute_anomaly_flags(options_summary={"put_call_ratio": 3.0})
        low = compute_anomaly_flags(options_summary={"put_call_ratio": 0.2})
        self.assertEqual(len(high["flags"]), 1)
        self.assertEqual(len(low["flags"]), 1)

    def test_multiple_sources_partial_flags_gives_fractional_score(self):
        result = compute_anomaly_flags(
            dark_pool={"wow_pct": 75.0}, liquidation_summary={"total_liquidated_usd": 100},
        )
        self.assertEqual(result["sources_evaluated"], 2)
        self.assertEqual(result["anomaly_score"], 50.0)


if __name__ == "__main__":
    unittest.main()
