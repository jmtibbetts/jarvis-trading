"""ATR as the unit, so instruments can be compared at all.

A 2% move means different things on NVDA and on SHIB. Every threshold
expressed as a raw percentage is therefore too tight for one asset or too
loose for another — the same class of error this codebase paid for
repeatedly in a single day:

    a fixed $15 stop against $12,000 of notional   -> a 0.12% trigger
    a percentage fee schedule applied per contract -> $6,047 to trade $1,800
    "one contract = one token"                     -> count scaled by price

Each was a quantity measured in one unit and consumed in another.
"""
import math
import unittest

import pandas as pd

from lib.atr_normalization import (ATR_PERIODS, atr_distance, compute_atr_profile,
                                   normalized_distances, signal_distances)


def _frame(prices, spread=0.01):
    """OHLCV where each bar has a predictable range."""
    return pd.DataFrame({
        "open": prices,
        "high": [p * (1 + spread) for p in prices],
        "low": [p * (1 - spread) for p in prices],
        "close": prices,
        "volume": [1000] * len(prices),
    })


class DistanceTests(unittest.TestCase):
    def test_distance_is_measured_in_atrs(self):
        self.assertEqual(atr_distance(100.0, 102.0, 2.0), 1.0)
        self.assertEqual(atr_distance(100.0, 96.0, 2.0), -2.0)

    def test_it_is_signed_so_direction_survives(self):
        self.assertGreater(atr_distance(100.0, 105.0, 1.0), 0)
        self.assertLess(atr_distance(100.0, 95.0, 1.0), 0)

    def test_missing_inputs_return_none_not_zero(self):
        """Zero ATR distance means AT the level. It must never be the value
        returned because data was absent — that is a measurement claiming to
        exist when it does not."""
        for args in ((None, 100.0, 1.0), (100.0, None, 1.0),
                     (100.0, 100.0, None), (100.0, 100.0, 0.0)):
            self.assertIsNone(atr_distance(*args), args)


class ComparabilityTests(unittest.TestCase):
    """The entire point: the same statement about stretch on any asset."""

    def test_identical_relative_moves_normalize_identically(self):
        cheap = atr_distance(0.00000449, 0.00000449 * 1.02, 0.00000449 * 0.01)
        dear = atr_distance(63546.0, 63546.0 * 1.02, 63546.0 * 0.01)
        self.assertAlmostEqual(cheap, dear, places=2)

    def test_the_same_percentage_stop_means_opposite_things(self):
        """A 1.5% stop, on two instruments, using each one's REAL measured
        ATR. On a quiet stock (ATR ~1% of price) it clears a bar and a
        half. On BEAT (ATR 12.2% of price, measured live) it sits at an
        eighth of one bar — the noise-triggered exit this system spent a
        day removing from the paper book."""
        quiet = signal_distances(100.0, 98.5, 104.0, atr_value=1.0)      # ATR 1% of price
        wild = signal_distances(1.198, 1.180, 1.25, atr_value=0.1465)    # ATR 12.2%, measured
        self.assertGreater(quiet["stop_atr"], wild["stop_atr"])
        self.assertTrue(wild["stop_inside_noise"], "a 1.5% stop on BEAT is inside one bar")
        self.assertFalse(quiet["stop_inside_noise"])
        # Same percentage, an order of magnitude apart once normalized.
        self.assertGreater(quiet["stop_atr"] / wild["stop_atr"], 8)


class ProfileTests(unittest.TestCase):
    def test_it_computes_every_horizon_it_has_data_for(self):
        prof = compute_atr_profile(_frame([100 + i * 0.1 for i in range(150)]))
        self.assertIsNotNone(prof)
        for p in ATR_PERIODS:
            key = f"atr_{p}"
            self.assertIn(key, prof["periods"])
        self.assertIsNotNone(prof["periods"]["atr_20"])

    def test_a_horizon_without_enough_history_is_none_not_guessed(self):
        prof = compute_atr_profile(_frame([100.0] * 30))
        self.assertIsNone(prof["periods"]["atr_100"])

    def test_too_little_data_returns_none_entirely(self):
        self.assertIsNone(compute_atr_profile(pd.DataFrame()))

    def test_widening_bars_read_as_expansion(self):
        calm = _frame([100.0] * 60, spread=0.002)
        wild = pd.concat([calm, _frame([100.0] * 10, spread=0.05)], ignore_index=True)
        prof = compute_atr_profile(wild)
        self.assertEqual(prof["state"], "EXPANSION")
        self.assertTrue(prof["expanding"])

    def test_percentile_is_self_referential(self):
        """"High for BTC" and "high for NVDA" are different absolute
        numbers; a percentile is the only portable form."""
        prof = compute_atr_profile(_frame([100 + math.sin(i / 5) for i in range(200)]))
        if prof.get("percentile") is not None:
            self.assertGreaterEqual(prof["percentile"], 0)
            self.assertLessEqual(prof["percentile"], 100)


class NormalizedDistanceTests(unittest.TestCase):
    def test_every_level_found_is_restated_in_atrs(self):
        tf = {
            "price": {"last": 100.0},
            "vwap": {"value": 102.0},
            "emas": {"ema9": 101.0, "ema21": 99.0, "ema50": 95.0},
            "support_resistance": {"support": 96.0, "resistance": 108.0},
            "bollinger_bands": {"upper": 104.0, "lower": 96.0},
        }
        d = normalized_distances(tf, atr_reference=2.0)
        self.assertEqual(d["to_vwap"], 1.0)
        self.assertEqual(d["to_support"], -2.0)
        self.assertEqual(d["to_resistance"], 4.0)

    def test_no_atr_means_no_distances_rather_than_wrong_ones(self):
        tf = {"price": {"last": 100.0}, "vwap": {"value": 102.0}}
        self.assertEqual(normalized_distances(tf, None), {})

    def test_absent_levels_are_simply_omitted(self):
        d = normalized_distances({"price": {"last": 100.0}}, atr_reference=2.0)
        self.assertNotIn("to_vwap", d)


class PricePrecisionTests(unittest.TestCase):
    """round(x, 6) silently destroyed sub-cent assets.

    SHIB at $0.00000449 became $0.000004 — a 10.9% error — and every level
    derived from it inherited that error before any strategy saw it. There
    were 28 such calls across the TA engine and its extensions.
    """

    def test_significant_figures_survive_a_sub_cent_price(self):
        from lib.ta_engine import _r
        self.assertAlmostEqual(_r(4.49e-06), 4.49e-06, places=12)

    def test_the_old_behaviour_really_was_that_lossy(self):
        raw = 4.49e-06
        self.assertGreater(abs(round(raw, 6) - raw) / raw * 100, 10.0)

    def test_large_prices_are_unharmed(self):
        from lib.ta_engine import _r
        self.assertAlmostEqual(_r(63546.3), 63546.3, places=2)

    def test_no_fixed_six_decimal_rounding_remains(self):
        """Code only — the first version of this test matched the phrase
        inside a docstring explaining the bug, which is the same trap the
        strategy tests hit earlier."""
        import ast
        import io as _io
        for path in ("lib/ta_engine.py", "lib/ta_extensions.py"):
            tree = ast.parse(_io.open(path, encoding="utf-8").read())
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call)
                        and getattr(node.func, "id", None) == "round"
                        and len(node.args) == 2
                        and isinstance(node.args[1], ast.Constant)
                        and node.args[1].value == 6):
                    self.fail(f"{path}: round(..., 6) on line {node.lineno}")

    def test_zero_and_nonsense_do_not_explode(self):
        from lib.ta_engine import _r
        self.assertEqual(_r(0), 0)
        self.assertEqual(_r(None), None)
        self.assertEqual(_r("abc"), "abc")


if __name__ == "__main__":
    unittest.main()
