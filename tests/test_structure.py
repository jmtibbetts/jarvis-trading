"""A level with a history, and a break with a verdict.

Support and resistance were two floats. A ceiling touched four times over
three weeks and rejected hard each time was indistinguishable from one
printed once yesterday, and "price is above the level" was the whole of
break detection — so a wick through that closed straight back inside read
exactly like a breakout that held. Those are opposite trades.

The property these tests exist to protect is non-repainting. A swing is
confirmed only after `window` further bars print without exceeding it, so
the last `window` bars cannot contain one. Detecting a sweep off an
unconfirmed swing uses the very bars that prove the sweep — the readings
would look excellent in backtest and be unobtainable live.
"""
import unittest

import pandas as pd

from lib.structure import (BREAK_ATR, MAX_DIVERGENCE_AGE, RECLAIM_BARS,
                           SWING_WINDOW, analyze, build_levels, classify_break,
                           find_divergences, level_strength)


def frame(bars):
    """bars: list of (open, high, low, close, volume)."""
    idx = pd.date_range("2026-01-01", periods=len(bars), freq="h")
    return pd.DataFrame(
        {"open": [b[0] for b in bars], "high": [b[1] for b in bars],
         "low": [b[2] for b in bars], "close": [b[3] for b in bars],
         "volume": [b[4] for b in bars]},
        index=idx,
    )


def flat(n, price=100.0, spread=1.0, volume=1000):
    return [(price, price + spread, price - spread, price, volume)] * n


def spike(price, top, volume=1000):
    """A bar that reaches `top` and closes back at `price` — a pure wick."""
    return (price, top, price - 1.0, price, volume)


class LevelsCarryTheirHistoryTests(unittest.TestCase):

    def _with_ceiling(self, touches):
        """A ceiling at 120, approached and rejected `touches` times."""
        bars = flat(8)
        bars += [(100, 120.0, 99, 101, 1000)]          # the swing high
        bars += flat(SWING_WINDOW + 2)
        for _ in range(touches):
            bars += [spike(101, 119.6)]                 # returns and is turned away
            bars += flat(3)
        return frame(bars)

    def test_a_level_records_how_often_it_was_defended(self):
        one = build_levels(self._with_ceiling(1))
        three = build_levels(self._with_ceiling(3))
        top_one = max(one, key=lambda l: l["touches"])
        top_three = max(three, key=lambda l: l["touches"])
        self.assertGreater(top_three["touches"], top_one["touches"])

    def test_a_level_knows_its_age(self):
        levels = build_levels(self._with_ceiling(2))
        self.assertTrue(all("age_bars" in l for l in levels))
        self.assertTrue(any(l["age_bars"] > 0 for l in levels))

    def test_a_defended_level_outranks_a_fresh_one(self):
        defended = {"touches": 4, "rejection_atr": 2.0, "age_bars": 80}
        fresh = {"touches": 0, "rejection_atr": 0.0, "age_bars": 1}
        self.assertGreater(level_strength(defended), level_strength(fresh))

    def test_strength_is_bounded_and_saturates(self):
        absurd = {"touches": 99, "rejection_atr": 99, "age_bars": 9999}
        self.assertLessEqual(level_strength(absurd), 1.0)
        self.assertGreaterEqual(level_strength({}), 0.0)

    def test_touches_matter_more_than_age(self):
        """A level people keep defending beats one that is merely old."""
        touched = {"touches": 4, "rejection_atr": 0.0, "age_bars": 0}
        old = {"touches": 0, "rejection_atr": 0.0, "age_bars": 999}
        self.assertGreater(level_strength(touched), level_strength(old))

    def test_thin_data_yields_no_levels_rather_than_invented_ones(self):
        self.assertEqual(build_levels(frame(flat(4))), [])
        self.assertEqual(build_levels(None), [])


class ABreakIsNotJustBeingAboveTheLevelTests(unittest.TestCase):
    """held / failed / sweep are three different trades that all satisfy
    "price exceeded the level"."""

    LEVEL = {"kind": "resistance", "price": 120.0}

    def _run(self, tail):
        return classify_break(frame(flat(30, 100.0) + tail), self.LEVEL)

    def test_a_wick_through_that_closes_back_inside_is_a_sweep(self):
        b = self._run([spike(101, 126.0, volume=3000)])
        self.assertIsNotNone(b)
        self.assertEqual(b["outcome"], "sweep")

    def test_a_break_that_holds_is_held(self):
        b = self._run([(121, 127, 120, 126, 3000)] + flat(3, 128.0))
        self.assertIsNotNone(b)
        self.assertEqual(b["outcome"], "held")

    def test_a_break_that_is_reclaimed_is_failed(self):
        b = self._run([(121, 127, 120, 126, 3000), (126, 127, 110, 112, 2000)])
        self.assertIsNotNone(b)
        self.assertEqual(b["outcome"], "failed")

    def test_price_merely_approaching_is_not_a_break(self):
        self.assertIsNone(self._run([spike(101, 119.0)]))

    def test_the_break_records_participation(self):
        b = self._run([(121, 127, 120, 126, 8000)] + flat(2, 128.0))
        self.assertIsNotNone(b["break_volume_ratio"])
        self.assertGreater(b["break_volume_ratio"], 2.0)

    def test_distance_is_measured_in_atr_not_percent(self):
        b = self._run([(121, 135, 120, 134, 3000)] + flat(2, 136.0))
        self.assertIn("distance_atr", b)
        self.assertGreater(b["distance_atr"], 0)

    def test_a_sweep_reads_at_least_as_strongly_as_a_plain_break(self):
        """The reversal IS the signal — it must not be the quiet case."""
        sweep = self._run([spike(101, 126.0, volume=3000)])
        held = self._run([(121, 127, 120, 126, 3000)] + flat(3, 128.0))
        self.assertGreaterEqual(sweep["conviction"], held["conviction"])

    def test_support_breaks_downward(self):
        df = frame(flat(30, 100.0) + [(99, 100, 88, 89, 3000)] + flat(3, 87.0))
        b = classify_break(df, {"kind": "support", "price": 95.0})
        self.assertIsNotNone(b)
        self.assertEqual(b["direction"], "down")


class DivergenceTests(unittest.TestCase):

    def _price_with_two_highs(self, second_high):
        bars = flat(6)
        bars += [(100, 120.0, 99, 101, 1000)]
        bars += flat(SWING_WINDOW + 4)
        bars += [(100, second_high, 99, 101, 1000)]
        bars += flat(SWING_WINDOW + 2)
        return frame(bars)

    def _series(self, df, first, second):
        """An indicator that reads `first` at the earlier swing and `second`
        at the later one."""
        vals = [50.0] * len(df)
        for i in range(len(df)):
            if abs(float(df["high"].iloc[i]) - 120.0) < 0.01:
                vals[i] = first
        # the later swing high is the max after the first
        highs = df["high"].tolist()
        later = max(range(len(highs)), key=lambda i: (highs[i], i)) if highs else 0
        vals[later] = second
        return pd.Series(vals, index=df.index)

    def test_price_higher_high_with_a_weaker_indicator_is_regular_bearish(self):
        df = self._price_with_two_highs(130.0)
        rsi = self._series(df, 80.0, 60.0)
        d = find_divergences(df, {"rsi": rsi})
        self.assertTrue(any(x["kind"] == "regular_bearish" for x in d), d)

    def test_agreement_is_not_divergence(self):
        df = self._price_with_two_highs(130.0)
        rsi = self._series(df, 60.0, 80.0)      # both rising
        self.assertEqual([x for x in find_divergences(df, {"rsi": rsi})
                          if x["indicator"] == "rsi"], [])

    def test_a_flat_indicator_is_not_divergence(self):
        df = self._price_with_two_highs(130.0)
        rsi = self._series(df, 70.0, 69.9)      # below MIN_DIVERGENCE_PCT
        self.assertEqual(find_divergences(df, {"rsi": rsi}), [])

    def test_only_recognised_series_are_read(self):
        df = self._price_with_two_highs(130.0)
        junk = self._series(df, 80.0, 60.0)
        self.assertEqual(find_divergences(df, {"astrology": junk}), [])

    def test_stale_divergence_is_dropped(self):
        """Thirty bars old is history, not a reason to trade now."""
        df = self._price_with_two_highs(130.0)
        rsi = self._series(df, 80.0, 60.0)
        padded = frame([tuple(r) for r in df[["open", "high", "low", "close", "volume"]].values]
                       + flat(MAX_DIVERGENCE_AGE + 12))
        padded_rsi = pd.Series(list(rsi) + [50.0] * (MAX_DIVERGENCE_AGE + 12),
                               index=padded.index)
        self.assertEqual(find_divergences(padded, {"rsi": padded_rsi}), [])

    def test_a_misaligned_series_is_skipped_not_guessed(self):
        df = self._price_with_two_highs(130.0)
        short = pd.Series([50.0] * 3)
        self.assertEqual(find_divergences(df, {"rsi": short}), [])


class NonRepaintingTests(unittest.TestCase):
    """The property that decides whether any of this survives contact with
    live data. Readings must not change retroactively when later bars
    arrive, and must not depend on bars that had not printed yet."""

    def _base(self):
        return flat(20, 100.0) + [(100, 120.0, 99, 101, 2000)] + flat(12, 101.0)

    def test_past_levels_do_not_move_when_new_bars_arrive(self):
        bars = self._base()
        before = build_levels(frame(bars))
        after = build_levels(frame(bars + flat(5, 101.0)))
        prices_before = {round(l["price"], 6) for l in before}
        prices_after = {round(l["price"], 6) for l in after}
        self.assertTrue(prices_before <= prices_after,
                        "a level that existed stopped existing when bars were appended")

    def test_the_newest_bars_cannot_contain_a_confirmed_swing(self):
        df = frame(self._base() + [(101, 200.0, 100, 199, 5000)])   # huge final bar
        levels = build_levels(df)
        n = len(df)
        for l in levels:
            self.assertGreaterEqual(n - 1 - l["index"], SWING_WINDOW,
                                    "a swing was confirmed without its right-hand bars")

    def test_the_spike_becomes_a_level_only_after_it_is_confirmed(self):
        spiked = self._base() + [(101, 200.0, 100, 199, 5000)]
        immediate = build_levels(frame(spiked))
        self.assertFalse(any(abs(l["price"] - 200.0) < 0.01 for l in immediate))
        later = build_levels(frame(spiked + flat(SWING_WINDOW + 2, 150.0)))
        self.assertTrue(any(abs(l["price"] - 200.0) < 0.01 for l in later))


class AssemblyTests(unittest.TestCase):

    def test_thin_data_abstains_rather_than_claiming_no_levels(self):
        """None is the absence of a claim; an empty dict is the claim that
        nothing is there."""
        self.assertIsNone(analyze(frame(flat(5))))
        self.assertIsNone(analyze(None))

    def test_a_real_frame_produces_a_readable_summary(self):
        df = frame(flat(20, 100.0) + [(100, 120, 99, 101, 2000)] + flat(10, 101.0))
        out = analyze(df)
        self.assertIsNotNone(out)
        self.assertIn("levels", out)
        self.assertTrue(out["summary"])

    def test_a_sweep_is_flagged_at_the_top_level(self):
        df = frame(flat(20, 100.0) + [(100, 120, 99, 101, 2000)]
                   + flat(SWING_WINDOW + 3, 101.0) + [spike(101, 126.0, 4000)])
        out = analyze(df)
        self.assertIsNotNone(out)
        self.assertTrue(out["swept"] or out["failed_break"],
                        "a wick through a known level was not flagged")

    def test_junk_input_does_not_raise(self):
        for bad in (frame(flat(30, 0.0)), frame([(1, 1, 1, 1, 0)] * 40)):
            analyze(bad)


if __name__ == "__main__":
    unittest.main()


class ATouchIsAVisitNotABarTests(unittest.TestCase):
    """Counting every bar inside the zone made one long pause at a level
    read as dozens of separate defences — NVDA daily reported a level
    "held 54x", which was one consolidation described as 54 rejections.
    Touch count feeds level_strength, so the inflation propagated."""

    def _consolidation(self):
        """Price parks at the level for many bars without leaving."""
        bars = flat(8, 100.0)
        bars += [(100, 120.0, 99, 101, 1000)]
        bars += flat(SWING_WINDOW + 2, 101.0)
        bars += [(119, 119.8, 118.5, 119, 1000)] * 20     # parked at the ceiling
        return frame(bars)

    def _two_real_visits(self):
        bars = flat(8, 100.0)
        bars += [(100, 120.0, 99, 101, 1000)]
        bars += flat(SWING_WINDOW + 2, 101.0)
        for _ in range(2):
            bars += [(119, 119.8, 118.5, 119, 1000)]      # visit
            bars += flat(6, 100.0)                        # and leaves
        return frame(bars)

    def test_parking_at_a_level_is_one_touch_not_twenty(self):
        levels = build_levels(self._consolidation())
        ceiling = max(levels, key=lambda l: l["touches"])
        self.assertLessEqual(ceiling["touches"], 3,
                             "a single consolidation was counted as many defences")

    def test_leaving_and_returning_counts_again(self):
        two = build_levels(self._two_real_visits())
        ceiling = max(two, key=lambda l: l["touches"])
        self.assertGreaterEqual(ceiling["touches"], 2)

    def test_a_parked_level_does_not_outrank_a_genuinely_defended_one(self):
        parked = max(build_levels(self._consolidation()), key=lambda l: l["touches"])
        visited = max(build_levels(self._two_real_visits()), key=lambda l: l["touches"])
        self.assertGreaterEqual(visited["touches"], parked["touches"])


class BoundedWorkTests(unittest.TestCase):
    """Level building is O(swings x bars) and the daily series carries 520
    bars — 164ms per symbol per timeframe, which is minutes of CPU across
    the universe. The bound is also the more honest model: a ceiling from
    450 bars ago is not one current participants are trading against."""

    def test_it_stays_fast_on_a_full_daily_series(self):
        import time
        bars = []
        for i in range(600):
            base = 100 + (i % 37)
            bars.append((base, base + 2, base - 2, base + 1, 1000 + i))
        df = frame(bars)
        start = time.perf_counter()
        build_levels(df)
        elapsed = (time.perf_counter() - start) * 1000
        self.assertLess(elapsed, 90, f"level build took {elapsed:.0f}ms on 600 bars")

    def test_levels_come_from_recent_history_not_the_whole_series(self):
        from lib.structure import MAX_LEVEL_LOOKBACK
        bars = [(50, 500.0, 49, 51, 1000)]              # ancient spike
        bars += flat(MAX_LEVEL_LOOKBACK + 60, 100.0)
        levels = build_levels(frame(bars))
        self.assertFalse(any(abs(l["price"] - 500.0) < 0.01 for l in levels),
                         "a level far outside the lookback was still reported")

    def test_age_is_measured_within_the_window(self):
        from lib.structure import MAX_LEVEL_LOOKBACK
        df = frame(flat(20, 100.0) + [(100, 130.0, 99, 101, 2000)]
                   + flat(MAX_LEVEL_LOOKBACK, 101.0))
        for l in build_levels(df):
            self.assertLessEqual(l["age_bars"], MAX_LEVEL_LOOKBACK)
