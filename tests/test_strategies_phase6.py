"""Nine more named strategies, three of which were unbuildable until the
structure engine existed.

breakout_retest, failed_breakout and liquidity_sweep_reversal are
distinguished ONLY by what a break did afterwards — held, failed, or swept.
While "price is above resistance" was the whole vocabulary, all three were
the same setup, and the two that trade AGAINST the break were being taken
in the direction of it.

The rule every strategy here obeys: a NECESSARY condition, so a setup that
does not actually meet the premise scores zero rather than partial credit.
Partial credit is how "breakout" ends up tagged on a chart with no
breakout, which poisons the attribution the tags exist to collect.
"""
import unittest

from lib.regime_axes import STRATEGY_FIT
from lib.strategies import (MIN_MATCH, STRATEGIES,
                            UNBUILDABLE_WITHOUT_ORDER_FLOW, classify)


def with_break(outcome, direction="up", price=120.0, bars_ago=2,
               vol=1.5, distance=0.8):
    return {"structure": {"levels": [], "divergences": [], "breaks": [
        {"outcome": outcome, "direction": direction, "level_price": price,
         "bars_ago": bars_ago, "break_volume_ratio": vol,
         "distance_atr": distance, "detail": f"{outcome} detail",
         "level_kind": "resistance" if direction == "up" else "support"}]}}


class ThreeStrategiesOneBreakTests(unittest.TestCase):
    """The same price event — a level exceeded — is three different trades
    depending on what happened next."""

    def test_a_break_that_held_is_a_retest_setup_not_a_reversal(self):
        d = with_break("held", "up")
        out = classify(d, "Long")
        self.assertGreater(out["all"]["breakout_retest"]["score"], 0)
        self.assertEqual(out["all"]["failed_breakout"]["score"], 0.0)
        self.assertEqual(out["all"]["liquidity_sweep_reversal"]["score"], 0.0)

    def test_a_failed_break_is_traded_AGAINST_the_break(self):
        """Broke up and was reclaimed -> the trade is SHORT."""
        d = with_break("failed", "up")
        self.assertGreater(classify(d, "Short")["all"]["failed_breakout"]["score"], 0)
        self.assertEqual(classify(d, "Long")["all"]["failed_breakout"]["score"], 0.0)

    def test_a_sweep_is_traded_against_the_wick(self):
        d = with_break("sweep", "up")
        self.assertGreater(
            classify(d, "Short")["all"]["liquidity_sweep_reversal"]["score"], 0)
        self.assertEqual(
            classify(d, "Long")["all"]["liquidity_sweep_reversal"]["score"], 0.0)

    def test_a_retest_is_traded_WITH_the_break(self):
        d = with_break("held", "up")
        self.assertGreater(classify(d, "Long")["all"]["breakout_retest"]["score"], 0)
        self.assertEqual(classify(d, "Short")["all"]["breakout_retest"]["score"], 0.0)

    def test_no_break_at_all_matches_none_of_the_three(self):
        for name in ("breakout_retest", "failed_breakout", "liquidity_sweep_reversal"):
            self.assertEqual(classify({}, "Long")["all"][name]["score"], 0.0, name)


class NecessaryConditionsTests(unittest.TestCase):
    """Partial credit is how "breakout" gets tagged on a chart with no
    breakout, which poisons the attribution the tags exist to collect."""

    def test_squeeze_expansion_requires_actual_compression(self):
        expanded = {"atr_profile": {"state": "EXPANSION", "percentile": 90,
                                    "expanding": True},
                    "donchian": {"breakout_up": True},
                    "supertrend": {"direction": "up"}}
        self.assertEqual(classify(expanded, "Long")["all"]["squeeze_expansion"]["score"], 0.0)
        coiled = {**expanded, "atr_profile": {"state": "CONTRACTION", "percentile": 10,
                                              "expanding": True}}
        self.assertGreater(classify(coiled, "Long")["all"]["squeeze_expansion"]["score"], 0)

    def test_momentum_ignition_requires_real_participation(self):
        quiet = {"volume": {"surge_ratio": 1.0}, "atr_profile": {"expanding": True},
                 "supertrend": {"flipped_this_bar": True},
                 "macd": {"crossover": "bullish"}}
        self.assertEqual(classify(quiet, "Long")["all"]["momentum_ignition"]["score"], 0.0)
        loud = {**quiet, "volume": {"surge_ratio": 2.4}}
        self.assertGreater(classify(loud, "Long")["all"]["momentum_ignition"]["score"], 0)

    def test_vwap_reclaim_requires_being_on_the_right_side(self):
        below = {"vwap": {"position": "below"}, "bias": "bearish",
                 "atr_distances": {"to_vwap": 0.6}}
        self.assertEqual(classify(below, "Long")["all"]["vwap_reclaim"]["score"], 0.0)
        self.assertGreater(classify(below, "Short")["all"]["vwap_reclaim"]["score"], 0)

    def test_relative_strength_breakout_requires_leadership(self):
        lagging = {"relative_strength": {"state": "underperforming", "rs_slope": -0.4},
                   "donchian": {"breakout_up": True}}
        self.assertEqual(
            classify(lagging, "Long")["all"]["relative_strength_breakout"]["score"], 0.0)
        self.assertGreater(
            classify(lagging, "Short")["all"]["relative_strength_breakout"]["score"], 0)

    def test_funding_squeeze_requires_a_real_funding_number(self):
        """No derivatives feed must mean no match, not a neutral guess."""
        self.assertEqual(classify({}, "Short")["all"]["funding_squeeze"]["score"], 0.0)
        self.assertEqual(
            classify({"derivatives": {}}, "Short")["all"]["funding_squeeze"]["score"], 0.0)

    def test_funding_squeeze_is_contrarian(self):
        """Crowded longs paying funding are fuel for a move DOWN."""
        crowded_long = {"derivatives": {"funding_rate": 0.002, "oi_change_pct": 9},
                        "bias": "bearish"}
        self.assertGreater(
            classify(crowded_long, "Short")["all"]["funding_squeeze"]["score"], 0)
        self.assertEqual(
            classify(crowded_long, "Long")["all"]["funding_squeeze"]["score"], 0.0)

    def test_divergence_reversal_requires_a_regular_divergence(self):
        hidden_only = {"structure": {"breaks": [], "levels": [], "divergences": [
            {"kind": "hidden_bullish", "bias": "bullish", "regular": False,
             "indicator": "rsi", "strength": 0.9, "age_bars": 1}]}}
        self.assertEqual(
            classify(hidden_only, "Long")["all"]["divergence_reversal"]["score"], 0.0)
        regular = {"structure": {"breaks": [], "levels": [], "divergences": [
            {"kind": "regular_bullish", "bias": "bullish", "regular": True,
             "indicator": "rsi", "strength": 0.9, "age_bars": 1}]}}
        self.assertGreater(
            classify(regular, "Long")["all"]["divergence_reversal"]["score"], 0)


class EveryStrategyBehavesTests(unittest.TestCase):

    def test_all_fourteen_are_registered(self):
        self.assertEqual(len(STRATEGIES), 14)

    def test_none_of_them_match_an_empty_chart(self):
        """A setup with no data must classify as nothing, not as whatever
        needs the fewest conditions."""
        out = classify({}, "Long")
        self.assertIsNone(out["strategy"])
        for name, detail in out["all"].items():
            self.assertEqual(detail["score"], 0.0, name)

    def test_every_score_is_a_fraction(self):
        d = {**with_break("held", "up"), "volume": {"surge_ratio": 3.0},
             "vwap": {"position": "above"}, "bias": "bullish",
             "atr_profile": {"state": "CONTRACTION", "percentile": 5, "expanding": True},
             "donchian": {"breakout_up": True}, "supertrend": {"direction": "up"},
             "relative_strength": {"state": "outperforming", "rs_slope": 0.3}}
        for direction in ("Long", "Short"):
            for name, detail in classify(d, direction)["all"].items():
                self.assertGreaterEqual(detail["score"], 0.0, name)
                self.assertLessEqual(detail["score"], 1.0, name)

    def test_every_match_reports_the_conditions_that_fired(self):
        out = classify(with_break("held", "up"), "Long")
        for name, detail in out["all"].items():
            if detail["score"] > 0:
                self.assertTrue(detail["conditions"], name)

    def test_junk_data_does_not_raise(self):
        for bad in ({"structure": "nope"}, {"volume": None}, {"vwap": 5},
                    {"derivatives": {"funding_rate": "n/a"}},
                    {"atr_profile": {"percentile": None}}):
            for direction in ("Long", "Short"):
                self.assertIsInstance(classify(bad, direction), dict)

    def test_a_strong_match_clears_the_classification_bar(self):
        d = with_break("sweep", "up", bars_ago=1, vol=2.0)
        out = classify(d, "Short")
        self.assertGreaterEqual(out["all"]["liquidity_sweep_reversal"]["score"], MIN_MATCH)


class RegimeFitCoversThemTests(unittest.TestCase):
    """A strategy with no regime rule is never marked down. Shipping nine
    new ones without fits would silently exempt them from Phase 4."""

    def test_every_strategy_has_a_regime_rule(self):
        for name in STRATEGIES:
            self.assertIn(name, STRATEGY_FIT, f"{name} has no regime fit")

    def test_squeeze_expansion_wants_the_regime_others_avoid(self):
        """It is the one strategy that WANTS compression."""
        self.assertIn("compressed", STRATEGY_FIT["squeeze_expansion"]["volatility"])
        self.assertNotIn("compressed", STRATEGY_FIT["momentum_ignition"]["volatility"])

    def test_funding_squeeze_is_gated_on_positioning_not_price(self):
        self.assertIn("flow", STRATEGY_FIT["funding_squeeze"])

    def test_no_rule_names_a_state_no_axis_produces(self):
        """A typo in a state name silently disables the rule — the fit would
        never match and the strategy would be penalised everywhere."""
        valid = {
            "trend": {"uptrend", "downtrend", "choppy_up", "choppy_down", "flat"},
            "volatility": {"expanding", "elevated", "normal", "quiet", "compressed"},
            "liquidity": {"heavy", "normal", "thin", "very_thin"},
            "flow": {"crowded_long", "crowded_short", "balanced"},
        }
        for strategy, rule in STRATEGY_FIT.items():
            for axis, allowed in rule.items():
                self.assertIn(axis, valid, f"{strategy} names unknown axis {axis}")
                for state in allowed:
                    self.assertIn(state, valid[axis],
                                  f"{strategy}/{axis} names unknown state {state}")


class HonestGapsTests(unittest.TestCase):
    """Free OHLCV cannot distinguish absorption from ordinary two-way
    volume. A detector built from bar volume alone would produce confident
    labels from a measurement that was never taken."""

    def test_order_flow_strategies_are_documented_not_faked(self):
        for name in UNBUILDABLE_WITHOUT_ORDER_FLOW:
            self.assertNotIn(name, STRATEGIES)


if __name__ == "__main__":
    unittest.main()


class VwapReclaimIsAnEventNotAStateTests(unittest.TestCase):
    """Position alone is a state. A name sitting 8 ATR above VWAP for three
    weeks satisfies "above" and has reclaimed nothing. Measured live, the
    position-only version matched 10 of 36 chart/direction combinations —
    more than any other strategy — which would have made vwap_reclaim the
    label on every trending chart."""

    def test_far_above_vwap_is_not_a_reclaim(self):
        far = {"vwap": {"position": "above"}, "bias": "bullish",
               "atr_distances": {"to_vwap": 8.0}}
        self.assertEqual(classify(far, "Long")["all"]["vwap_reclaim"]["score"], 0.0)

    def test_close_to_vwap_on_the_right_side_is(self):
        near = {"vwap": {"position": "above"}, "bias": "bullish",
                "atr_distances": {"to_vwap": 0.5}}
        self.assertGreater(classify(near, "Long")["all"]["vwap_reclaim"]["score"], 0)

    def test_no_distance_measurement_means_no_match(self):
        """Unknown distance must not be read as "close"."""
        blind = {"vwap": {"position": "above"}, "bias": "bullish"}
        self.assertEqual(classify(blind, "Long")["all"]["vwap_reclaim"]["score"], 0.0)
