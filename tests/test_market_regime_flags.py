import unittest

import pandas as pd

from lib.market_regime import compute_regime_flags


def _atr(values):
    return pd.Series(values, dtype=float)


class RegimeFlagTests(unittest.TestCase):
    def test_no_inputs_no_flags(self):
        self.assertEqual(compute_regime_flags(), [])

    def test_volatility_expansion(self):
        # 50 bars around 1.0, last bar 2.0 -> well above 1.25x median
        series = _atr([1.0] * 59 + [2.0])
        self.assertIn("VOLATILITY_EXPANSION", compute_regime_flags(atr_series=series))

    def test_volatility_compression(self):
        series = _atr([1.0] * 59 + [0.5])
        self.assertIn("VOLATILITY_COMPRESSION", compute_regime_flags(atr_series=series))

    def test_normal_volatility_no_flag(self):
        series = _atr([1.0] * 60)
        flags = compute_regime_flags(atr_series=series)
        self.assertNotIn("VOLATILITY_EXPANSION", flags)
        self.assertNotIn("VOLATILITY_COMPRESSION", flags)

    def test_short_atr_history_abstains(self):
        series = _atr([1.0] * 10 + [3.0])
        self.assertEqual(compute_regime_flags(atr_series=series), [])

    def test_panic_requires_both_high_vix_and_weak_breadth(self):
        history = list(range(10, 30))  # VIX 10..29
        flags = compute_regime_flags(vix_current=35.0, vix_history=history * 5,
                                     breadth_pct_advancing=15.0)
        self.assertIn("PANIC", flags)
        # High VIX alone is elevated volatility, not panic:
        flags2 = compute_regime_flags(vix_current=35.0, vix_history=history * 5,
                                      breadth_pct_advancing=55.0)
        self.assertNotIn("PANIC", flags2)

    def test_euphoria_requires_both_low_vix_and_broad_advance(self):
        history = list(range(10, 30))
        flags = compute_regime_flags(vix_current=9.0, vix_history=history * 5,
                                     breadth_pct_advancing=85.0)
        self.assertIn("EUPHORIA", flags)
        flags2 = compute_regime_flags(vix_current=9.0, vix_history=history * 5,
                                      breadth_pct_advancing=50.0)
        self.assertNotIn("EUPHORIA", flags2)

    def test_risk_on_off_from_breadth(self):
        self.assertIn("RISK_ON", compute_regime_flags(breadth_pct_advancing=70.0))
        self.assertIn("RISK_OFF", compute_regime_flags(breadth_pct_advancing=30.0))
        mid = compute_regime_flags(breadth_pct_advancing=50.0)
        self.assertNotIn("RISK_ON", mid)
        self.assertNotIn("RISK_OFF", mid)

    def test_short_vix_history_abstains_from_panic(self):
        flags = compute_regime_flags(vix_current=35.0, vix_history=[10.0] * 20,
                                     breadth_pct_advancing=15.0)
        self.assertNotIn("PANIC", flags)
        # breadth flags still fire independently
        self.assertIn("RISK_OFF", flags)


if __name__ == "__main__":
    unittest.main()
