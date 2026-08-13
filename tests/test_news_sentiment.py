"""News alignment is recorded cargo — measured, never hand-scored.

The 2026-08-13 pilot: alignment split outcomes 52.5% vs 26.0% win rate,
but across ~4 falling days, so it may be one regime's artifact. These
tests pin the feature's contract, not the pilot's conclusion.
"""
import unittest
from unittest.mock import patch

from lib.news_sentiment import MIXED_BAND, alignment, net_sentiment


def _with_map(m):
    return patch("lib.news_sentiment._cached_map", return_value=m)


class AlignmentTests(unittest.TestCase):
    def test_long_with_positive_mood_is_with(self):
        with _with_map({("BTC", "2026-08-13"): 0.5}):
            self.assertEqual(alignment("BTC/USD", "Long", "2026-08-13"), "with")

    def test_short_with_positive_mood_is_against(self):
        with _with_map({("BTC", "2026-08-13"): 0.5}):
            self.assertEqual(alignment("BTC/USD", "Short", "2026-08-13"), "against")

    def test_short_with_negative_mood_is_with(self):
        with _with_map({("SOL", "2026-08-13"): -0.4}):
            self.assertEqual(alignment("SOL/USD", "Short", "2026-08-13"), "with")

    def test_a_mixed_day_claims_nothing(self):
        with _with_map({("BTC", "2026-08-13"): MIXED_BAND / 2}):
            self.assertEqual(alignment("BTC/USD", "Long", "2026-08-13"), "mixed")

    def test_no_measured_mood_is_none_not_neutral(self):
        with _with_map({}):
            self.assertIsNone(alignment("BTC/USD", "Long", "2026-08-13"))
            self.assertIsNone(net_sentiment("BTC/USD", "2026-08-13"))

    def test_symbol_forms_normalize_to_base(self):
        with _with_map({("ETH", "2026-08-13"): 0.6}):
            self.assertEqual(alignment("eth/usd", "Long", "2026-08-13"), "with")
            self.assertEqual(alignment("ETH", "Long", "2026-08-13"), "with")


class RecordedNotScoredTests(unittest.TestCase):
    def test_the_alignment_cannot_move_the_composite(self):
        from lib.signal_scorer import score_signal
        base = {"asset_symbol": "T/USD", "direction": "Long", "timeframe": "4H",
                "entry_price": 100, "target_price": 110, "stop_loss": 95,
                "confidence": 60}
        ta = {"4H": {"bias": "bullish", "rsi": 60, "macd": {}}}
        with _with_map({("T", __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc).strftime("%Y-%m-%d")): 0.9}):
            a = score_signal(dict(base), ta, {}, set())
        with _with_map({}):
            b = score_signal(dict(base), ta, {}, set())
        self.assertEqual(a["composite_score"], b["composite_score"])
        self.assertEqual(a["score_breakdown"]["news_alignment"], "with")
        self.assertIsNone(b["score_breakdown"]["news_alignment"])


if __name__ == "__main__":
    unittest.main()
