import unittest

from lib.signal_levels import clamp_stop_to_atr


class ClampStopToAtrTests(unittest.TestCase):
    def test_widens_a_stop_tighter_than_half_atr(self):
        # entry=100, stop=99.8 -> 0.2% distance, well inside a 2% ATR's 1% floor
        signal = {"entry_price": 100.0, "stop_loss": 99.8, "direction": "Long"}
        result, clamped, _ = clamp_stop_to_atr(signal, atr_pct=2.0)
        self.assertTrue(clamped)
        self.assertAlmostEqual(result["stop_loss"], 99.0, places=2)  # 100 * (1 - 1%)

    def test_tightens_a_stop_wider_than_five_atr(self):
        # entry=100, stop=80 -> 20% distance, way beyond a 2% ATR's 10% ceiling
        signal = {"entry_price": 100.0, "stop_loss": 80.0, "direction": "Long"}
        result, clamped, _ = clamp_stop_to_atr(signal, atr_pct=2.0)
        self.assertTrue(clamped)
        self.assertAlmostEqual(result["stop_loss"], 90.0, places=2)  # 100 * (1 - 10%)

    def test_leaves_a_stop_within_the_atr_band_untouched(self):
        # entry=100, stop=97 -> 3% distance, within [1%, 10%] for a 2% ATR
        signal = {"entry_price": 100.0, "stop_loss": 97.0, "direction": "Long"}
        result, clamped, _ = clamp_stop_to_atr(signal, atr_pct=2.0)
        self.assertFalse(clamped)
        self.assertEqual(result["stop_loss"], 97.0)

    def test_short_direction_widens_stop_above_entry(self):
        # entry=100, stop=100.2 -> 0.2% distance, inside a 2% ATR's 1% floor
        signal = {"entry_price": 100.0, "stop_loss": 100.2, "direction": "Short"}
        result, clamped, _ = clamp_stop_to_atr(signal, atr_pct=2.0)
        self.assertTrue(clamped)
        self.assertAlmostEqual(result["stop_loss"], 101.0, places=2)  # 100 * (1 + 1%)

    def test_missing_atr_skips_clamping(self):
        signal = {"entry_price": 100.0, "stop_loss": 99.9, "direction": "Long"}
        result, clamped, reason = clamp_stop_to_atr(signal, atr_pct=None)
        self.assertFalse(clamped)
        self.assertEqual(result["stop_loss"], 99.9)


if __name__ == "__main__":
    unittest.main()
