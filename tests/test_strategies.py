"""Named strategies exist so losses can be attributed.

There was one strategy: "the LLM reads the indicators and decides". When
that loses you cannot tell whether breakouts are failing, mean reversion is
failing, or the model is guessing — so there is nothing to fix, only a 32%
win rate to stare at.

Tagging each setup with a strategy lets lib/calibration.py score strategies
the way it already scores timeframes, turning "the bot doesn't work" into
"breakouts work here and range fades do not".
"""
import unittest

from lib.strategies import MIN_MATCH, STRATEGIES, classify, classify_signal


def _ta(**over):
    """A neutral TA frame; tests turn on only what they are testing."""
    base = {
        "rsi": 50.0, "bias": "neutral",
        "macd": {"macd": 0.0, "signal": 0.0, "histogram": 0.0, "trend": "flat", "crossover": "none"},
        "emas": {"ema9": 100.0, "ema21": 100.0, "ema50": 100.0},
        "adx": {"value": 15.0, "strong": False},
        "bollinger_bands": {"upper": 105.0, "mid": 100.0, "lower": 95.0, "pct_b": 0.5, "position": "middle"},
        "atr": {"value": 1.0, "pct": 1.0},
        "volume": {"surge_ratio": 1.0, "surge": False, "dry": False},
        "support_resistance": {"support": 95.0, "resistance": 105.0, "range_pct": 10.0, "position_in_range": 0.5},
        "donchian": {"breakout_up": False, "breakout_down": False},
        "supertrend": {"direction": "flat", "flipped_this_bar": False},
        "vwap": {"position": "at"},
        "obv_trend": "flat", "mfi": 50.0, "williams_r": -50.0,
        "stochastic": {"k": 50.0, "d": 50.0, "signal": "neutral"},
        "market_structure": {"structure": "range"},
    }
    base.update(over)
    return base


class DirectionAwarenessTests(unittest.TestCase):
    """A bullish breakout and a bearish breakdown are the same strategy; a
    long entered on bearish structure is neither."""

    def test_a_breakout_matches_the_direction_it_broke(self):
        up = _ta(donchian={"breakout_up": True, "breakout_down": False},
                 volume={"surge_ratio": 2.4, "surge": True, "dry": False},
                 adx={"value": 34.0, "strong": True},
                 bollinger_bands={"pct_b": 1.1, "position": "above_upper"})
        self.assertEqual(classify(up, "Long")["strategy"], "breakout")
        self.assertNotEqual(classify(up, "Short")["strategy"], "breakout")

    def test_oversold_is_a_long_not_a_short(self):
        d = _ta(rsi=22.0, williams_r=-92.0, mfi=15.0,
                bollinger_bands={"pct_b": -0.1, "position": "below_lower"},
                stochastic={"k": 8.0, "d": 10.0, "signal": "oversold"})
        self.assertEqual(classify(d, "Long")["strategy"], "mean_reversion")
        self.assertNotEqual(classify(d, "Short")["strategy"], "mean_reversion")


class StrategiesAreDistinguishableTests(unittest.TestCase):
    """If every setup classified the same way the tagging would be useless."""

    def test_a_trending_breakout_is_not_a_range_fade(self):
        d = _ta(donchian={"breakout_up": True, "breakout_down": False},
                volume={"surge_ratio": 3.0, "surge": True, "dry": False},
                adx={"value": 38.0, "strong": True},
                bollinger_bands={"pct_b": 1.2, "position": "above_upper"})
        r = classify(d, "Long")
        self.assertEqual(r["strategy"], "breakout")
        self.assertGreater(r["all"]["breakout"]["score"], r["all"]["range_fade"]["score"])

    def test_a_quiet_range_top_is_a_fade_not_a_breakout(self):
        d = _ta(adx={"value": 14.0, "strong": False},
                support_resistance={"support": 95.0, "resistance": 105.0,
                                    "range_pct": 10.0, "position_in_range": 0.95})
        r = classify(d, "Short")
        self.assertEqual(r["strategy"], "range_fade")

    def test_stacked_emas_with_volume_read_as_trend_continuation(self):
        d = _ta(emas={"ema9": 110.0, "ema21": 105.0, "ema50": 100.0},
                supertrend={"direction": "up", "flipped_this_bar": False},
                macd={"trend": "bullish", "crossover": "none", "histogram": 0.4},
                market_structure={"structure": "higher_highs_higher_lows"},
                obv_trend="up")
        self.assertEqual(classify(d, "Long")["strategy"], "trend_continuation")


class VolumeMattersTests(unittest.TestCase):
    """A breakout without volume is a fake the range reclaims — measured
    live on BEAT/USD, which scored exactly at the threshold on channel
    break + ADX alone."""

    def test_volume_raises_a_breakouts_score(self):
        dry = _ta(donchian={"breakout_up": True, "breakout_down": False},
                  adx={"value": 34.0, "strong": True})
        wet = _ta(donchian={"breakout_up": True, "breakout_down": False},
                  adx={"value": 34.0, "strong": True},
                  volume={"surge_ratio": 2.6, "surge": True, "dry": False})
        self.assertGreater(classify(wet, "Long")["all"]["breakout"]["score"],
                           classify(dry, "Long")["all"]["breakout"]["score"])

    def test_the_surge_appears_in_the_stated_conditions(self):
        d = _ta(donchian={"breakout_up": True, "breakout_down": False},
                volume={"surge_ratio": 2.6, "surge": True, "dry": False})
        conds = " ".join(classify(d, "Long")["conditions"])
        self.assertIn("volume surge", conds)


class UnclassifiedIsAnAnswerTests(unittest.TestCase):
    """Forcing a weak match into the nearest bucket would poison the very
    statistics the tagging exists to collect."""

    def test_a_featureless_chart_matches_nothing(self):
        r = classify(_ta(), "Long")
        self.assertIsNone(r["strategy"])
        self.assertIn("no strategy met", r["reason"])

    def test_the_threshold_is_a_real_bar(self):
        self.assertGreaterEqual(MIN_MATCH, 0.5)

    def test_missing_ta_does_not_invent_a_strategy(self):
        self.assertIsNone(classify({}, "Long")["strategy"])
        self.assertIsNone(classify(None, "Short")["strategy"])


class EvidenceTravelsTests(unittest.TestCase):
    """A tag without its evidence is an assertion."""

    def test_a_match_lists_the_conditions_that_fired(self):
        d = _ta(rsi=18.0, williams_r=-95.0, mfi=12.0,
                bollinger_bands={"pct_b": -0.2, "position": "below_lower"})
        r = classify(d, "Long")
        self.assertTrue(r["conditions"])
        self.assertIn("RSI", " ".join(r["conditions"]))

    def test_every_strategy_is_scored_so_near_misses_are_visible(self):
        r = classify(_ta(), "Long")
        self.assertEqual(set(r["all"]), set(STRATEGIES))


class SignalLevelTests(unittest.TestCase):
    def test_it_classifies_on_the_signals_own_timeframe(self):
        prof = {"1H": _ta(rsi=20.0, williams_r=-90.0, mfi=15.0,
                          bollinger_bands={"pct_b": -0.1, "position": "below_lower"}),
                "4H": _ta()}
        r = classify_signal({"timeframe": "1H", "direction": "Long"}, prof)
        self.assertEqual(r["timeframe_used"], "1H")
        self.assertEqual(r["strategy"], "mean_reversion")

    def test_falling_back_to_another_timeframe_says_so(self):
        prof = {"4H": _ta(rsi=20.0, williams_r=-90.0, mfi=15.0,
                          bollinger_bands={"pct_b": -0.1, "position": "below_lower"})}
        r = classify_signal({"timeframe": "1H", "direction": "Long"}, prof)
        self.assertEqual(r["timeframe_used"], "4H")
        self.assertEqual(r["timeframe_requested"], "1H")


class ScoringIntegrationTests(unittest.TestCase):
    def test_a_scored_signal_carries_its_strategy(self):
        from lib.signal_scorer import score_signal
        ta = {"1H": _ta(rsi=20.0, williams_r=-92.0, mfi=14.0,
                        bollinger_bands={"pct_b": -0.1, "position": "below_lower"},
                        stochastic={"k": 6.0, "d": 9.0, "signal": "oversold"},
                        bar_age_seconds=60)}
        out = score_signal(
            {"asset_symbol": "X/USD", "direction": "Long", "confidence": 80,
             "timeframe": "1H", "entry_price": 100, "target_price": 110, "stop_loss": 95},
            ta, {"risk": "medium"},
        )
        self.assertEqual(out["strategy"], "mean_reversion")
        self.assertGreater(out["strategy_score"], 0)
        self.assertIn("strategy_match", out["score_breakdown"])

    def test_an_unclassified_setup_stores_no_strategy(self):
        from lib.signal_scorer import score_signal
        out = score_signal(
            {"asset_symbol": "X/USD", "direction": "Long", "confidence": 70,
             "timeframe": "1H", "entry_price": 100, "target_price": 110, "stop_loss": 95},
            {"1H": _ta(bar_age_seconds=60)}, {"risk": "medium"},
        )
        self.assertIsNone(out["strategy"])


if __name__ == "__main__":
    unittest.main()
