"""Four questions, four answers — measured against the right market.

The single regime label bundled direction, volatility, liquidity and
positioning into one string, so a market rising calmly read the same as one
rising violently. Those want different position sizes. And it was computed
from SPY for everything: a BTC signal on a Saturday was graded on an equity
index that had been shut for two days.

The discipline these tests protect is abstention. An axis with no data must
say so, not report neutral — "we could not measure liquidity" and
"liquidity is normal" are different statements, and only one of them should
be allowed to affect a trade.
"""
import unittest

from lib.regime_axes import (AXES, BENCHMARKS, OUT_OF_REGIME_PENALTY,
                             asset_class_of, hierarchy_alignment, measure,
                             strategy_fit, timeframe_roles)


def bench(close=110, ema21=105, ema50=100, adx=30, atr_pct=1.5,
          atr_percentile=50, volume_ratio=1.0, symbol="SPY", secondary=None):
    out = {"primary": {"symbol": symbol, "close": close, "ema21": ema21,
                       "ema50": ema50, "adx": adx, "atr_pct": atr_pct,
                       "atr_percentile": atr_percentile,
                       "volume_ratio": volume_ratio}}
    if secondary:
        out["secondary"] = secondary
    return out


class AxesAreIndependentTests(unittest.TestCase):

    def test_all_four_are_reported(self):
        r = measure("equity", bench())
        self.assertEqual(set(r["axes"]), set(AXES))

    def test_a_calm_rise_and_a_violent_rise_read_differently(self):
        """The distinction the single label could not express."""
        calm = measure("equity", bench(atr_percentile=20))
        violent = measure("equity", bench(atr_percentile=95))
        self.assertEqual(calm["axes"]["trend"]["state"],
                         violent["axes"]["trend"]["state"])
        self.assertNotEqual(calm["axes"]["volatility"]["state"],
                            violent["axes"]["volatility"]["state"])

    def test_trend_direction_follows_the_moving_averages(self):
        up = measure("equity", bench(close=110, ema21=105, ema50=100))
        down = measure("equity", bench(close=90, ema21=95, ema50=100))
        self.assertEqual(up["axes"]["trend"]["state"], "uptrend")
        self.assertEqual(down["axes"]["trend"]["state"], "downtrend")

    def test_a_weak_adx_lowers_confidence_without_changing_direction(self):
        strong = measure("equity", bench(adx=40))["axes"]["trend"]
        weak = measure("equity", bench(adx=12))["axes"]["trend"]
        self.assertEqual(strong["state"], weak["state"])
        self.assertGreater(strong["confidence"], weak["confidence"])

    def test_disagreeing_benchmarks_lower_confidence(self):
        agree = measure("equity", bench(secondary={"symbol": "QQQ", "close": 110,
                                                   "ema50": 100}))
        disagree = measure("equity", bench(secondary={"symbol": "QQQ", "close": 90,
                                                      "ema50": 100}))
        self.assertLess(disagree["axes"]["trend"]["confidence"],
                        agree["axes"]["trend"]["confidence"])

    def test_thin_volume_is_a_liquidity_reading_not_a_trend_one(self):
        r = measure("equity", bench(volume_ratio=0.3))
        self.assertEqual(r["axes"]["liquidity"]["state"], "very_thin")
        self.assertEqual(r["axes"]["trend"]["state"], "uptrend")


class MissingDataAbstainsTests(unittest.TestCase):
    """A regime we failed to measure must not be scored as a neutral one."""

    def test_no_benchmark_abstains_on_every_price_axis(self):
        r = measure("equity", {})
        for axis in ("trend", "volatility", "liquidity"):
            self.assertTrue(r["axes"][axis]["abstained"], axis)

    def test_an_abstained_axis_carries_zero_confidence(self):
        r = measure("equity", {})
        self.assertEqual(r["axes"]["trend"]["confidence"], 0.0)

    def test_abstention_is_reported_not_hidden(self):
        r = measure("equity", {})
        self.assertEqual(r["measured_axes"], 0)
        self.assertIn("trend", r["abstained_axes"])

    def test_equities_abstain_on_flow_rather_than_inventing_it(self):
        """There is no free positioning feed for equities. Deriving one from
        price would just restate the trend axis under a second name."""
        r = measure("equity", bench())
        self.assertTrue(r["axes"]["flow"]["abstained"])

    def test_crypto_reads_flow_from_derivatives(self):
        r = measure("crypto", bench(symbol="BTC/USD"),
                    derivatives={"funding_rate": 0.002, "oi_change_pct": 12})
        self.assertFalse(r["axes"]["flow"]["abstained"])
        self.assertEqual(r["axes"]["flow"]["state"], "crowded_long")

    def test_crypto_without_a_snapshot_still_abstains(self):
        r = measure("crypto", bench(symbol="BTC/USD"), derivatives=None)
        self.assertTrue(r["axes"]["flow"]["abstained"])


class TheRightBenchmarkTests(unittest.TestCase):
    """SPY's verdict was applied to BTC, to SOL, to gold. On a weekend it
    was applied while the equity market had been shut for two days."""

    def test_crypto_is_not_benchmarked_against_spy(self):
        self.assertNotIn("SPY", BENCHMARKS["crypto"].values())
        self.assertEqual(BENCHMARKS["crypto"]["primary"], "BTC/USD")

    def test_every_class_declares_a_primary(self):
        for cls, conf in BENCHMARKS.items():
            self.assertTrue(conf.get("primary"), cls)

    def test_symbols_route_to_their_own_class(self):
        self.assertEqual(asset_class_of("BTC/USD"), "crypto")
        self.assertEqual(asset_class_of("NVDA"), "equity")
        self.assertEqual(asset_class_of("HG=F"), "futures")
        self.assertEqual(asset_class_of("EURUSD=X"), "forex")

    def test_a_declared_class_wins_over_the_guess(self):
        self.assertEqual(asset_class_of("XBTC", "crypto"), "crypto")

    def test_the_measured_regime_names_the_benchmark_it_used(self):
        r = measure("crypto", bench(symbol="BTC/USD"))
        self.assertEqual(r["benchmark"], "BTC/USD")


class StrategyFitTests(unittest.TestCase):
    """Mean reversion in a strong trend is the classic way to lose money
    with a technically valid setup, and every strategy previously scored the
    same in every regime."""

    TRENDING = measure("equity", bench(close=120, ema21=110, ema50=100, adx=40,
                                       atr_percentile=50))
    RANGING = measure("equity", bench(close=100, ema21=100, ema50=100, adx=12,
                                      atr_percentile=30))

    def test_mean_reversion_is_marked_down_in_a_strong_trend(self):
        fit = strategy_fit("mean_reversion", self.TRENDING)
        self.assertFalse(fit["fits"])
        self.assertGreater(fit["penalty"], 0)

    def test_trend_continuation_is_fine_in_a_trend(self):
        self.assertTrue(strategy_fit("trend_continuation", self.TRENDING)["fits"])

    def test_range_fade_is_fine_in_a_range(self):
        self.assertTrue(strategy_fit("range_fade", self.RANGING)["fits"])

    def test_trend_continuation_is_marked_down_in_a_flat_market(self):
        self.assertFalse(strategy_fit("trend_continuation", self.RANGING)["fits"])

    def test_more_conflicting_axes_cost_more(self):
        one = strategy_fit("mean_reversion", self.TRENDING)
        self.assertGreaterEqual(one["penalty"], OUT_OF_REGIME_PENALTY)

    def test_the_reason_is_readable(self):
        self.assertIn("mean_reversion", strategy_fit("mean_reversion", self.TRENDING)["reason"])

    def test_an_abstained_axis_cannot_object(self):
        """Missing data must not become evidence against a trade."""
        blind = measure("equity", {})
        fit = strategy_fit("mean_reversion", blind)
        self.assertTrue(fit["fits"])
        self.assertEqual(fit["penalty"], 0.0)

    def test_an_unknown_strategy_is_not_penalised(self):
        self.assertTrue(strategy_fit("something_new", self.TRENDING)["fits"])
        self.assertTrue(strategy_fit(None, self.TRENDING)["fits"])


class TimeframeHierarchyTests(unittest.TestCase):
    """Requiring every timeframe to agree produces no trades; ignoring the
    higher one takes every countertrend setup. The hierarchy names which
    timeframe plays which role instead."""

    def test_each_timeframe_gets_a_higher_context_and_an_execution_frame(self):
        for tf in ("5m", "15m", "1H", "4H", "1D", "1W"):
            r = timeframe_roles(tf)
            self.assertTrue(r["higher_timeframe_context"], tf)
            self.assertEqual(r["setup_timeframe"], tf)
            self.assertTrue(r["execution_timeframe"], tf)

    def test_the_context_frame_is_never_shorter_than_the_setup(self):
        order = ["1m", "3m", "5m", "15m", "30m", "1H", "2H", "4H", "1D", "2D", "1W"]
        for tf in order:
            higher = timeframe_roles(tf)["higher_timeframe_context"]
            self.assertGreaterEqual(order.index(higher), order.index(tf), tf)

    def test_an_unknown_timeframe_does_not_invent_a_hierarchy(self):
        r = timeframe_roles("13h")
        self.assertEqual(r["higher_timeframe_context"], "13h")

    def test_trading_with_the_higher_timeframe_is_flagged_as_aligned(self):
        ta = {"1D": {"bias": "bullish"}}
        self.assertTrue(hierarchy_alignment(ta, "4H", "Long")["aligned"])

    def test_trading_against_it_is_allowed_but_named(self):
        """Against is not forbidden — it is sometimes the whole point. It
        must be visible rather than silent."""
        ta = {"1D": {"bias": "bearish"}}
        r = hierarchy_alignment(ta, "4H", "Long")
        self.assertFalse(r["aligned"])
        self.assertIn("against", r["detail"])

    def test_no_higher_timeframe_data_is_unknown_not_aligned(self):
        r = hierarchy_alignment({}, "4H", "Long")
        self.assertIsNone(r["aligned"])

    def test_a_neutral_higher_timeframe_does_not_count_as_agreement(self):
        r = hierarchy_alignment({"1D": {"bias": "neutral"}}, "4H", "Long")
        self.assertIsNone(r["aligned"])


if __name__ == "__main__":
    unittest.main()


class WiredIntoTheScoreTests(unittest.TestCase):
    """Reported but unused would leave every strategy scoring identically in
    every regime, which is the state this phase exists to end."""

    def _score(self, strategy_ta, symbol="NVDA"):
        from lib.signal_scorer import score_signal
        return score_signal(
            {"asset_symbol": symbol, "direction": "Long", "confidence": 70,
             "timeframe": "4H", "entry_price": 100.0, "target_price": 106.0,
             "stop_loss": 98.0},
            {"4H": strategy_ta}, {"risk": "medium"})

    def test_the_breakdown_records_which_benchmark_was_used(self):
        out = self._score({"bias": "bullish", "rsi": 55})
        bd = out["score_breakdown"]
        self.assertIn("regime_benchmark", bd)
        self.assertIn("regime_axes", bd)

    def test_the_timeframe_hierarchy_is_recorded(self):
        out = self._score({"bias": "bullish"})
        h = out["score_breakdown"]["timeframe_hierarchy"]
        self.assertIsNotNone(h)
        self.assertEqual(h["setup_timeframe"], "4H")
        self.assertEqual(h["higher_timeframe_context"], "1D")

    def test_the_regime_penalty_is_itemised(self):
        out = self._score({"bias": "bullish"})
        self.assertIn("regime_penalty", out["score_breakdown"])
        self.assertLessEqual(out["score_breakdown"]["regime_penalty"], 0)

    def test_a_crypto_symbol_is_not_graded_against_spy(self):
        out = self._score({"bias": "bullish"}, symbol="BTC/USD")
        bench = out["score_breakdown"].get("regime_benchmark")
        self.assertNotEqual(bench, "SPY")


class FieldNamesMatchTheTaEngineTests(unittest.TestCase):
    """Both bugs here were silent: the code read a key that does not exist
    and got {}, so the axis abstained on every symbol and looked like
    missing data rather than a typo. Live output is what exposed them."""

    def test_the_percentile_scale_is_zero_to_one_hundred(self):
        """atr_profile reports 62.4, not 0.624. Read as a fraction, every
        symbol above the 1st percentile read "expanding"."""
        self.assertEqual(measure("equity", bench(atr_percentile=62.4)
                                 )["axes"]["volatility"]["state"], "elevated")
        self.assertEqual(measure("equity", bench(atr_percentile=0.4)
                                 )["axes"]["volatility"]["state"], "compressed")
        self.assertEqual(measure("equity", bench(atr_percentile=92)
                                 )["axes"]["volatility"]["state"], "expanding")

    def test_the_detail_string_reads_as_a_percentile(self):
        d = measure("equity", bench(atr_percentile=62.4))["axes"]["volatility"]["detail"]
        self.assertIn("62th percentile", d)
        self.assertNotIn("6240", d)

    def test_the_snapshot_reads_the_key_the_ta_engine_actually_writes(self):
        """compute_timeframe emits "emas" (plural). The singular abstained
        the trend axis everywhere."""
        import inspect
        from lib import regime_axes
        src = inspect.getsource(regime_axes.benchmark_snapshot)
        self.assertIn('"emas"', src)

    def test_the_evidence_engine_reads_it_too(self):
        """lib/evidence._trend had the same typo, so its EMA vote never
        fired and the trend category ran on supertrend and VWAP alone."""
        from lib.evidence import gather
        ev = gather({"emas": {"ema9": 11, "ema21": 10}}, "Long")
        cats = [c["category"] for c in ev["supporting"]]
        self.assertIn("trend", cats)

    def test_a_live_benchmark_measures_its_trend(self):
        """The end-to-end check the unit tests could not make: real fields,
        real shapes, trend actually measured rather than abstained."""
        from lib.regime_axes import benchmark_snapshot
        snap = benchmark_snapshot("equity")
        if not snap.get("primary"):
            self.skipTest("no market data available")
        r = measure("equity", snap)
        self.assertFalse(r["axes"]["trend"]["abstained"],
                         "trend abstained against a live benchmark")
