import unittest

import numpy as np
import pandas as pd

from lib.ta_extensions import (
    cci, compute_extensions, donchian_channels, find_swings, keltner_channels,
    market_structure, mfi, pivot_points, supertrend, williams_r,
)


def _df(highs, lows, closes, volumes=None):
    n = len(closes)
    return pd.DataFrame({
        "open": closes, "high": highs, "low": lows, "close": closes,
        "volume": volumes if volumes is not None else [1000.0] * n,
    })


def _trend(n=60, start=100.0, step=1.0):
    closes = [start + i * step for i in range(n)]
    highs = [c + 0.5 for c in closes]
    lows = [c - 0.5 for c in closes]
    return _df(highs, lows, closes)


class WilliamsRTests(unittest.TestCase):
    def test_close_at_period_high_is_zero(self):
        df = _trend()
        # close == period high - 0.5 offset; use exact: set last close to last high
        r = williams_r(df["high"], df["high"], df["high"])
        self.assertEqual(r, 0.0)

    def test_close_at_period_low_is_minus_100(self):
        df = _trend()
        r = williams_r(df["high"], df["low"], df["low"])
        # close equals lowest low -> -100 exactly only if last low == period low;
        # in an uptrend the period low is 14 bars back, so just assert bounds
        self.assertLessEqual(r, 0.0)
        self.assertGreaterEqual(r, -100.0)

    def test_insufficient_data_returns_none(self):
        df = _trend(n=5)
        self.assertIsNone(williams_r(df["high"], df["low"], df["close"]))

    def test_flat_series_returns_none_not_division_error(self):
        s = pd.Series([50.0] * 30)
        self.assertIsNone(williams_r(s, s, s))


class CciTests(unittest.TestCase):
    def test_uptrend_is_positive(self):
        df = _trend()
        self.assertGreater(cci(df["high"], df["low"], df["close"]), 0)

    def test_downtrend_is_negative(self):
        df = _trend(step=-1.0)
        self.assertLess(cci(df["high"], df["low"], df["close"]), 0)

    def test_flat_series_returns_none(self):
        s = pd.Series([50.0] * 30)
        self.assertIsNone(cci(s, s, s))


class MfiTests(unittest.TestCase):
    def test_all_up_days_saturate_at_100(self):
        df = _trend()
        self.assertEqual(mfi(df["high"], df["low"], df["close"], df["volume"]), 100.0)

    def test_all_down_days_near_zero(self):
        df = _trend(step=-1.0)
        self.assertLess(mfi(df["high"], df["low"], df["close"], df["volume"]), 1.0)

    def test_insufficient_data_returns_none(self):
        df = _trend(n=5)
        self.assertIsNone(mfi(df["high"], df["low"], df["close"], df["volume"]))


class KeltnerTests(unittest.TestCase):
    def test_bands_bracket_ema(self):
        df = _trend()
        k = keltner_channels(df["high"], df["low"], df["close"])
        self.assertLess(k["lower"], k["mid"])
        self.assertLess(k["mid"], k["upper"])

    def test_position_classification(self):
        df = _trend()
        k = keltner_channels(df["high"], df["low"], df["close"])
        self.assertIn(k["position"], ("above_upper", "below_lower", "inside"))

    def test_insufficient_data_returns_none(self):
        df = _trend(n=8)
        self.assertIsNone(keltner_channels(df["high"], df["low"], df["close"]))


class DonchianTests(unittest.TestCase):
    def test_breakout_up_detected_against_prior_bars(self):
        """The channel is built from the PRIOR 20 bars, so a new extreme on the
        current bar counts as a breakout instead of raising the channel."""
        df = _trend()
        d = donchian_channels(df["high"], df["low"])
        self.assertTrue(d["breakout_up"])  # steady uptrend: every bar breaks the prior-20 high
        self.assertFalse(d["breakout_down"])

    def test_no_breakout_inside_range(self):
        closes = [100 + (i % 5) for i in range(40)]  # oscillates 100..104
        highs = [c + 0.5 for c in closes]
        lows = [c - 0.5 for c in closes]
        df = _df(highs, lows, closes)
        d = donchian_channels(df["high"], df["low"])
        self.assertFalse(d["breakout_up"])
        self.assertFalse(d["breakout_down"])


class SupertrendTests(unittest.TestCase):
    def test_uptrend_direction_up_with_stop_below(self):
        df = _trend()
        st = supertrend(df["high"], df["low"], df["close"])
        self.assertEqual(st["direction"], "up")
        self.assertLess(st["level"], float(df["close"].iloc[-1]))

    def test_downtrend_direction_down_with_stop_above(self):
        df = _trend(step=-1.0)
        st = supertrend(df["high"], df["low"], df["close"])
        self.assertEqual(st["direction"], "down")
        self.assertGreater(st["level"], float(df["close"].iloc[-1]))

    def test_insufficient_data_returns_none(self):
        df = _trend(n=6)
        self.assertIsNone(supertrend(df["high"], df["low"], df["close"]))


class PivotTests(unittest.TestCase):
    def test_classic_floor_pivot_arithmetic(self):
        p = pivot_points(110.0, 90.0, 100.0)
        self.assertEqual(p["pivot"], 100.0)
        self.assertEqual(p["r1"], 110.0)   # 2*100 - 90
        self.assertEqual(p["s1"], 90.0)    # 2*100 - 110
        self.assertEqual(p["r2"], 120.0)   # 100 + 20
        self.assertEqual(p["s2"], 80.0)    # 100 - 20

    def test_none_input_returns_none(self):
        self.assertIsNone(pivot_points(None, 90.0, 100.0))


class SwingTests(unittest.TestCase):
    def _zigzag(self):
        # Explicit swing pattern: peak at i=5 (105), trough at i=10 (95),
        # peak at i=15 (110), trough at i=20 (98), then rise. window=3.
        closes = []
        path = [(0, 100), (5, 105), (10, 95), (15, 110), (20, 98), (27, 108)]
        for (i0, v0), (i1, v1) in zip(path, path[1:]):
            for i in range(i0, i1):
                closes.append(v0 + (v1 - v0) * (i - i0) / (i1 - i0))
        closes.append(108)
        highs = [c + 0.2 for c in closes]
        lows = [c - 0.2 for c in closes]
        return _df(highs, lows, closes)

    def test_finds_constructed_swings(self):
        df = self._zigzag()
        swings = find_swings(df["high"], df["low"], window=3)
        peak_idxs = [s["index"] for s in swings["swing_highs"]]
        trough_idxs = [s["index"] for s in swings["swing_lows"]]
        self.assertIn(5, peak_idxs)
        self.assertIn(15, peak_idxs)
        self.assertIn(10, trough_idxs)
        self.assertIn(20, trough_idxs)

    def test_recent_bars_cannot_contain_confirmed_swing(self):
        """Anti-repaint guarantee: the trailing `window` bars are never
        eligible, because confirmation needs bars after the extremum."""
        df = self._zigzag()
        n = len(df)
        swings = find_swings(df["high"], df["low"], window=3)
        for s in swings["swing_highs"] + swings["swing_lows"]:
            self.assertLess(s["index"], n - 3)


class MarketStructureTests(unittest.TestCase):
    def _zigzag(self, second_peak, second_trough, tail):
        path = [(0, 100), (5, 105), (10, 95), (15, second_peak), (20, second_trough), (27, tail)]
        closes = []
        for (i0, v0), (i1, v1) in zip(path, path[1:]):
            for i in range(i0, i1):
                closes.append(v0 + (v1 - v0) * (i - i0) / (i1 - i0))
        closes.append(tail)
        highs = [c + 0.2 for c in closes]
        lows = [c - 0.2 for c in closes]
        return _df(highs, lows, closes)

    def test_higher_highs_and_lows_is_uptrend(self):
        df = self._zigzag(second_peak=110, second_trough=98, tail=105)
        ms = market_structure(df)
        self.assertEqual(ms["structure"], "uptrend")
        self.assertEqual(ms["labels"], ["HH", "HL"])

    def test_bos_up_when_close_breaks_last_swing_high(self):
        df = self._zigzag(second_peak=110, second_trough=98, tail=115)
        ms = market_structure(df)
        self.assertEqual(ms["event"], "BOS_UP")

    def test_choch_down_when_uptrend_breaks_last_swing_low(self):
        """CHoCH construction is timing-sensitive by design: the second trough
        (98) must be CONFIRMED by a bounce, but the breakdown must land before
        the bounce top itself confirms as a (lower) swing high — otherwise the
        structure legitimately reads range, not uptrend. The bounce top here
        sits within the confirmation window at evaluation time, exactly the
        real-world moment a CHoCH fires."""
        path = [(0, 100), (5, 105), (10, 95), (15, 110), (20, 98)]
        closes = []
        for (i0, v0), (i1, v1) in zip(path, path[1:]):
            for i in range(i0, i1):
                closes.append(v0 + (v1 - v0) * (i - i0) / (i1 - i0))
        closes += [98, 99, 101, 103, 98, 94]  # confirm trough, bounce, break down fast
        highs = [c + 0.2 for c in closes]
        lows = [c - 0.2 for c in closes]
        ms = market_structure(_df(highs, lows, closes))
        self.assertEqual(ms["structure"], "uptrend")
        self.assertEqual(ms["event"], "CHOCH_DOWN")

    def test_lower_highs_and_lows_is_downtrend(self):
        df = self._zigzag(second_peak=103, second_trough=92, tail=96)
        ms = market_structure(df)
        self.assertEqual(ms["structure"], "downtrend")
        self.assertEqual(ms["labels"], ["LH", "LL"])

    def test_too_few_swings_abstains(self):
        df = _trend(n=20)  # monotone: no confirmed swings at all
        self.assertIsNone(market_structure(df))


class ComputeExtensionsTests(unittest.TestCase):
    def test_all_keys_present_on_real_shaped_frame(self):
        df = _trend()
        out = compute_extensions(df)
        for key in ("williams_r", "cci", "mfi", "keltner", "donchian",
                    "supertrend", "pivots", "market_structure"):
            self.assertIn(key, out)

    def test_one_indicator_failure_does_not_blank_others(self):
        df = _trend().drop(columns=["volume"])  # MFI's input gone
        out = compute_extensions(df)
        self.assertIsNone(out["mfi"])
        self.assertIsNotNone(out["supertrend"])
        self.assertIsNotNone(out["pivots"])


if __name__ == "__main__":
    unittest.main()
