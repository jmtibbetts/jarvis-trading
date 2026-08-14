"""Typed decisions: a field can never mean three things again.

The costliest defects this codebase produced were semantic — one dict key
("confidence") carrying three meanings across modules, ending with an
inverted score bet as a Kelly probability. These types make the four
concerns structurally distinct, and the OrderPlan.within check turns
invariant #10 (execution never enlarges an approval) into code.
"""
import unittest
from unittest.mock import patch

from lib.decision_types import (MeasuredEdge, ObservedEvidence, OrderPlan,
                                RiskDecision, TradeDecision)


class EvidenceTests(unittest.TestCase):
    def test_builds_from_a_signal_row_with_strict_side(self):
        ev = ObservedEvidence.from_signal({
            "asset_symbol": "BTC/USD", "direction": "Long", "timeframe": "4H",
            "entry_price": 60000, "stop_loss": 58000, "target_price": 64000,
            "composite_score": 72.0, "confidence": 88.0})
        self.assertEqual(ev.side, "long")
        self.assertEqual(ev.evidence_composite, 72.0)
        self.assertEqual(ev.llm_stated_confidence, 88.0)

    def test_unparseable_direction_is_none_not_long(self):
        ev = ObservedEvidence.from_signal({"asset_symbol": "X", "direction": "sideways"})
        self.assertIsNone(ev.side)

    def test_frozen(self):
        ev = ObservedEvidence.from_signal({"asset_symbol": "X", "direction": "Long"})
        with self.assertRaises(Exception):
            ev.symbol = "Y"


class EdgeTests(unittest.TestCase):
    def test_builds_from_expectancy_output(self):
        edge = MeasuredEdge.from_expectancy({
            "verdict": "TRADE", "reason": "r", "robust": True,
            "expectancy": {"bucket": "b", "sample": 100.0, "raw_sample": 120,
                           "p_win": 0.55, "p_win_ci": [0.50, 0.60],
                           "avg_win_r": 1.4, "avg_loss_r": 0.9,
                           "gross_expected_r": 0.36},
            "net": {"net_expected_r": 0.2, "expected_cost_r": 0.16},
            "net_lower": {"net_expected_r": 0.07}})
        self.assertEqual(edge.p_win_lower, 0.50)
        self.assertEqual(edge.net_expected_r_lower, 0.07)
        self.assertTrue(edge.robust)


class OrderPlanInvariantTests(unittest.TestCase):
    RISK = RiskDecision(allowed_risk_usd=200.0, stop_distance=2.0, qty=10.0,
                        notional=1000.0, margin=1000.0, leverage=1.0)

    def _plan(self, qty, notional):
        return OrderPlan(symbol="X", venue="alpaca", side="long",
                         order_type="market", qty=qty, entry=100.0,
                         initial_stop=98.0, notional=notional)

    def test_within_passes_at_or_below_approval(self):
        self.assertTrue(self._plan(10.0, 1000.0).within(self.RISK))
        self.assertTrue(self._plan(7.0, 700.0).within(self.RISK))

    def test_within_fails_on_any_enlargement(self):
        self.assertFalse(self._plan(10.5, 1050.0).within(self.RISK))
        self.assertFalse(self._plan(10.0, 1100.0).within(self.RISK))

    def test_the_one_share_roundup_is_gone(self):
        import inspect

        from jobs import execute_signals
        src = inspect.getsource(execute_signals)
        self.assertNotIn("qty = 1\n", src.replace("    ", ""),
                         "the round-up-to-1-share enlargement survived")
        self.assertIn("INVARIANT VIOLATION", src)


class GateReturnsTypedTests(unittest.TestCase):
    def test_decide_returns_a_trade_decision(self):
        from lib.gate import decide
        with patch("lib.expectancy.evaluate",
                   return_value={"verdict": "NO_TRADE", "reason": "bad",
                                 "net": {"net_expected_r": -0.2},
                                 "net_lower": {"net_expected_r": -0.4},
                                 "robust": False, "expectancy": {}}):
            d = decide({"asset_symbol": "BTC/USD", "direction": "Long",
                        "entry_price": 60000, "stop_loss": 58000,
                        "target_price": 64000, "timeframe": "4H"})
        self.assertIsInstance(d, TradeDecision)
        self.assertEqual(d.decision, "NO_TRADE")
        self.assertFalse(d.take)
        self.assertIsNotNone(d.edge)
        self.assertEqual(d.edge.net_expected_r, -0.2)


if __name__ == "__main__":
    unittest.main()
