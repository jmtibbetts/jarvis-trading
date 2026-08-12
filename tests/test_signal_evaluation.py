import unittest
from datetime import datetime, timezone

import pandas as pd

from lib.signal_evaluation import evaluate_signal_path, summarize_evaluations
from lib.signal_scorer import score_signal


def frame(rows):
    return pd.DataFrame(
        rows,
        columns=["time", "open", "high", "low", "close", "volume"],
    ).set_index(pd.to_datetime([row[0] for row in rows], utc=True)).drop(columns=["time"])


class ForwardSignalEvaluationTests(unittest.TestCase):
    def test_mismatched_entry_and_market_price_is_quarantined(self):
        signal = {
            "generated_at": "2026-01-01T00:00:00Z", "direction": "Long",
            "entry_price": 0.001, "target_price": 0.0012, "stop_loss": 0.0008,
        }
        bars = frame([
            ["2026-01-01T00:05:00Z", 1.0, 1.1, 0.9, 1.0, 1],
        ])
        result = evaluate_signal_path(signal, bars)
        self.assertEqual(result["outcome"], "INVALID_DATA")
        self.assertIn("symbol and exchange mapping", result["data_issue"])

    def test_pre_signal_bar_is_never_used(self):
        signal = {
            "generated_at": "2026-08-09T12:00:00Z", "direction": "Long",
            "entry_price": 100, "target_price": 110, "stop_loss": 95,
        }
        bars = frame([
            ["2026-08-09T11:59:00Z", 100, 120, 99, 110, 1],
            ["2026-08-09T12:01:00Z", 100, 104, 99, 103, 1],
        ])
        result = evaluate_signal_path(signal, bars)
        self.assertEqual(result["outcome"], "OPEN")
        self.assertEqual(result["bars_observed"], 1)
        self.assertEqual(result["mfe_pct"], 4.0)

    def test_target_hit_and_short_excursions(self):
        signal = {
            "generated_at": "2026-08-09T12:00:00Z", "direction": "Short",
            "entry_price": 100, "target_price": 90, "stop_loss": 106,
        }
        bars = frame([
            ["2026-08-09T12:01:00Z", 100, 102, 97, 98, 1],
            ["2026-08-09T12:02:00Z", 98, 99, 89, 90, 1],
        ])
        result = evaluate_signal_path(signal, bars)
        self.assertEqual(result["outcome"], "TARGET_HIT")
        self.assertEqual(result["mfe_pct"], 11.0)
        self.assertEqual(result["mae_pct"], -2.0)

    def test_same_bar_stop_and_target_is_ambiguous(self):
        signal = {
            "generated_at": "2026-08-09T12:00:00Z", "direction": "Long",
            "entry_price": 100, "target_price": 105, "stop_loss": 95,
        }
        bars = frame([["2026-08-09T12:01:00Z", 100, 106, 94, 101, 1]])
        self.assertEqual(evaluate_signal_path(signal, bars)["outcome"], "AMBIGUOUS")

    def test_summary_reports_hit_rate_and_excursions(self):
        summary = summarize_evaluations([
            {"outcome": "TARGET_HIT", "mfe_pct": 5, "mae_pct": -1},
            {"outcome": "STOP_HIT", "mfe_pct": 1, "mae_pct": -3},
        ])
        self.assertEqual(summary["hit_rate"], 50.0)
        self.assertEqual(summary["avg_mfe_pct"], 3.0)


class EvidenceScoringTests(unittest.TestCase):
    def test_history_calibrates_overconfident_signal(self):
        signal = {
            "direction": "Long", "confidence": 90, "timeframe": "1H",
            "entry_price": 100, "target_price": 110, "stop_loss": 95,
        }
        ta = {"1H": {"bias": "bullish", "rsi": 55, "macd": {"crossover": "bullish"}}}
        scored = score_signal(
            signal, ta, {"risk": "medium"},
            historical={"total_trades": 20, "wins": 4},
            now=datetime(2026, 8, 9, tzinfo=timezone.utc),
        )
        self.assertLess(scored["calibrated_confidence"], 90)
        self.assertEqual(scored["score_breakdown"]["calibration"]["sample_size"], 20)

    def test_stale_selected_timeframe_gets_penalty(self):
        signal = {
            "direction": "Long", "confidence": 75, "timeframe": "5m",
            "entry_price": 100, "target_price": 110, "stop_loss": 95,
        }
        ta = {"5m": {
            "bias": "bullish", "rsi": 55, "macd": {"crossover": "bullish"},
            "bar_age_seconds": 86400,
        }}
        scored = score_signal(signal, ta, {"risk": "medium"})
        self.assertEqual(scored["freshness_score"], 0)
        self.assertEqual(scored["score_breakdown"]["stale_penalty"], -15)


if __name__ == "__main__":
    unittest.main()


class TestReversalProposal:
    """A confidently-rejected thesis becomes the opposite-side setup, with
    levels computed from ATR — never invented by the model."""

    SIG = {"direction": "Long", "timeframe": "15m", "asset_symbol": "OP/USD"}
    TA = {"1H": {"atr": {"value": 0.000861}}}

    def _agree(self, **kw):
        base = {"assessment": "AGREE", "confidence": 95}
        base.update(kw)
        return base

    def test_no_proposal_when_ai_agrees(self):
        from lib.signal_verification import propose_reversal
        assert propose_reversal(self.SIG, 0.09144, self.TA, self._agree()) is None

    def test_no_proposal_on_low_confidence_disagree(self):
        from lib.signal_verification import propose_reversal
        weak = {"assessment": "DISAGREE", "confidence": 55}
        assert propose_reversal(self.SIG, 0.09144, self.TA, weak) is None

    def test_no_proposal_without_atr(self):
        from lib.signal_verification import propose_reversal
        strong = {"assessment": "DISAGREE", "confidence": 90}
        assert propose_reversal(self.SIG, 0.09144, {}, strong) is None

    def test_long_flips_to_short_with_valid_geometry(self):
        from lib.signal_verification import propose_reversal
        strong = {"assessment": "DISAGREE", "confidence": 90}
        p = propose_reversal(self.SIG, 0.09144, self.TA, strong)
        assert p["direction"] == "Short"
        # short geometry: stop ABOVE entry, target BELOW
        assert p["stop_loss"] > p["entry_price"] > p["target_price"]
        risk = p["stop_loss"] - p["entry_price"]
        reward = p["entry_price"] - p["target_price"]
        assert round(reward / risk, 1) == 2.0

    def test_short_flips_to_long(self):
        from lib.signal_verification import propose_reversal
        sig = dict(self.SIG, direction="Short_Leveraged")
        p = propose_reversal(sig, 100.0, {"1H": {"atr": {"value": 1.0}}},
                             {"assessment": "DISAGREE", "confidence": 80})
        assert p["direction"] == "Long"
        assert p["target_price"] > p["entry_price"] > p["stop_loss"]

    def test_scalp_stop_never_exceeds_three_percent(self):
        from lib.signal_verification import propose_reversal
        # ATR is huge (20% of price) — the scalp cap must clamp the risk
        p = propose_reversal(self.SIG, 100.0, {"1H": {"atr": {"value": 20.0}}},
                             {"assessment": "DISAGREE", "confidence": 90})
        assert p["risk_per_unit"] == 3.0   # 3% of 100, not the 20 ATR
