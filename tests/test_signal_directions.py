import unittest

from jobs.generate_signals import (
    build_ta_fallback_signals,
    direction_requires_paper,
    normalize_signal,
    select_llm_batches,
)
from lib.signal_scorer import score_signal


class SignalDirectionTests(unittest.TestCase):
    def test_short_direction_is_routed_to_paper_with_short_price_levels(self):
        raw = {
            "asset_symbol": "NVDA",
            "direction": "Short",
            "confidence": 75,
            "entry_price": 100,
            "target_price": 110,
            "stop_loss": 90,
        }

        self.assertTrue(direction_requires_paper(raw["direction"]))
        normalized = normalize_signal(raw, {}, {}, is_paper=True)
        self.assertTrue(normalized["paper_mode"])
        self.assertEqual(normalized["paper_direction"], "Short")
        self.assertGreater(normalized["stop_loss"], normalized["entry_price"])
        self.assertLess(normalized["target_price"], normalized["entry_price"])

    def test_bearish_confluence_and_short_risk_reward_are_scored(self):
        signal = {
            "asset_symbol": "NVDA",
            "direction": "Short",
            "confidence": 80,
            "entry_price": 100,
            "target_price": 90,
            "stop_loss": 105,
        }
        ta_data = {
            "1H": {"bias": "bearish", "rsi": 55, "macd": {"crossover": "bearish"}, "bar_age_seconds": 60},
            "4H": {"bias": "bearish", "rsi": 58, "macd": {"crossover": "bearish"}, "bar_age_seconds": 60},
            "1D": {"bias": "bearish", "rsi": 45, "macd": {"crossover": "none"}, "bar_age_seconds": 60},
        }

        scored = score_signal(signal, ta_data, {"risk": "high"})
        self.assertEqual(scored["rr_ratio"], 2.0)
        self.assertEqual(scored["score_breakdown"]["ta_confluence"], 100)
        self.assertGreater(scored["composite_score"], 70)

    def test_plain_long_does_not_require_paper(self):
        self.assertFalse(direction_requires_paper("Long"))
        self.assertTrue(direction_requires_paper("Long_Leveraged"))
        self.assertTrue(direction_requires_paper("Short_5x"))

    def test_ta_fallback_creates_valid_short_without_llm_output(self):
        bearish = {
            "bias": "bearish",
            "trend": {"pct": 20},
            "price": {"last": 100},
            "rsi": 42,
            "macd": {"trend": "bearish"},
            "atr": {"pct": 2.0},
            "volume": {"surge": True},
        }
        signals = build_ta_fallback_signals(
            ["NVDA"],
            {"NVDA": {"4H": bearish, "1D": bearish}},
            {"NVDA": {"name": "NVIDIA", "price": 100}},
            trade_mode="longer",
        )

        self.assertEqual(len(signals), 1)
        signal = signals[0]
        self.assertEqual(signal["direction"], "Short")
        self.assertEqual(signal["signal_source"], "ta_fallback")
        self.assertLess(signal["target_price"], signal["entry_price"])
        self.assertGreater(signal["stop_loss"], signal["entry_price"])

    def test_local_llm_batch_budget_is_balanced_across_tracks(self):
        batches = [
            ("A0", [], "", False),
            ("A1", [], "", False),
            ("B0", [], "", False),
            ("B1", [], "", False),
            ("C0", [], "", False),
            ("E0", [], "", True),
            ("F0", [], "", True),
            ("D0", [], "", False),
        ]

        selected = select_llm_batches(batches, 6)

        self.assertEqual([batch[0] for batch in selected], ["A0", "B0", "C0", "E0", "F0", "D0"])
        expected_all = ["A0", "B0", "C0", "E0", "F0", "D0", "A1", "B1"]
        self.assertEqual([batch[0] for batch in select_llm_batches(batches, 0)], expected_all)
        self.assertEqual([batch[0] for batch in select_llm_batches(batches, 99)], expected_all)


if __name__ == "__main__":
    unittest.main()
