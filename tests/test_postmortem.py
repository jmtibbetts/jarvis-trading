import unittest

from lib.postmortem import (
    MAX_PENALTY, MIN_SAMPLE, aggregate_reasons, classify, get_failure_adjustment,
)


def _sig(status="Expired", entry=100.0, target=110.0, stop=95.0, **over):
    base = dict(status=status, entry_price=entry, target_price=target,
                stop_loss=stop, paper_mode=False, notes="")
    base.update(over)
    return base


class ClassifyTests(unittest.TestCase):
    def test_stop_hit(self):
        code, detail = classify(_sig(status="Closed"), {"outcome": "STOP_HIT", "mae_pct": -5.2})
        self.assertEqual(code, "STOP_HIT")
        self.assertIn("-5.2", detail)

    def test_expired_never_executed(self):
        """Measured on real data: 226/226 expired signals had no broker order.
        Those are EXPIRED_UNEXECUTED (overproduction/gating), distinct from a
        submitted order that never filled."""
        code, _ = classify(_sig(status="Expired"), {"outcome": "OPEN"})
        self.assertEqual(code, "EXPIRED_UNEXECUTED")
        code2, _ = classify(_sig(status="Expired"), None)
        self.assertEqual(code2, "EXPIRED_UNEXECUTED")

    def test_expired_with_submitted_order_is_unfilled(self):
        code, detail = classify(_sig(status="Expired", alpaca_order_id="ord-123"), None)
        self.assertEqual(code, "EXPIRED_UNFILLED_ORDER")
        self.assertIn("never filled", detail)

    def test_ambiguous_bar(self):
        code, _ = classify(_sig(status="Closed"), {"outcome": "AMBIGUOUS"})
        self.assertEqual(code, "AMBIGUOUS_BAR")

    def test_data_error_carries_issue(self):
        code, detail = classify(_sig(status="Closed"),
                                {"outcome": "INVALID_DATA", "data_issue": "entry mismatch ratio 4.2"})
        self.assertEqual(code, "DATA_ERROR")
        self.assertIn("entry mismatch", detail)

    def test_rejected_by_user(self):
        code, _ = classify(_sig(status="Rejected"), None)
        self.assertEqual(code, "REJECTED_BY_USER")

    def test_degenerate_levels_outrank_everything(self):
        """The '1.00/1.00/1.00' class: spacing below 0.1% is the root cause
        regardless of how the signal eventually died."""
        code, detail = classify(_sig(status="Rejected", entry=1.0035, target=1.0038, stop=1.0031), None)
        self.assertEqual(code, "DEGENERATE_LEVELS")
        self.assertIn("spacing", detail)

    def test_win_returns_none(self):
        self.assertIsNone(classify(_sig(status="Closed"), {"outcome": "TARGET_HIT"}))

    def test_active_signal_returns_none(self):
        self.assertIsNone(classify(_sig(status="Active"), None))


class AggregateTests(unittest.TestCase):
    def test_counts_by_reason_and_bucket(self):
        pms = [
            {"reason_code": "STOP_HIT", "symbol": "AMD", "setup_type": "scalp"},
            {"reason_code": "STOP_HIT", "symbol": "AMD", "setup_type": "scalp"},
            {"reason_code": "EXPIRED_UNEXECUTED", "symbol": "TLT", "setup_type": "swing"},
        ]
        agg = aggregate_reasons(pms)
        self.assertEqual(agg["by_reason"]["STOP_HIT"], 2)
        self.assertEqual(agg["by_bucket"][("AMD", "scalp")]["STOP_HIT"], 2)
        self.assertEqual(agg["total"], 3)


class FailureAdjustmentTests(unittest.TestCase):
    def _pms(self, n, symbol="AMD", setup="scalp", code="STOP_HIT"):
        return [{"symbol": symbol, "setup_type": setup, "reason_code": code} for _ in range(n)]

    def test_below_sample_floor_is_none(self):
        """Sparse history is not evidence — the core guarantee."""
        self.assertIsNone(get_failure_adjustment("AMD", "scalp", self._pms(MIN_SAMPLE - 1)))

    def test_at_floor_activates_with_named_reason(self):
        adj = get_failure_adjustment("AMD", "scalp", self._pms(MIN_SAMPLE))
        self.assertIsNotNone(adj)
        self.assertEqual(adj["dominant_reason"], "STOP_HIT")
        self.assertEqual(adj["penalty"], 2.0)
        self.assertIn("mostly STOP_HIT", adj["note"])

    def test_penalty_scales_and_caps(self):
        big = get_failure_adjustment("AMD", "scalp", self._pms(50))
        self.assertEqual(big["penalty"], MAX_PENALTY)

    def test_other_symbols_failures_do_not_penalize(self):
        adj = get_failure_adjustment("NVDA", "scalp", self._pms(20, symbol="AMD"))
        self.assertIsNone(adj)

    def test_setup_type_scoping(self):
        pms = self._pms(10, setup="swing")
        self.assertIsNone(get_failure_adjustment("AMD", "scalp", pms))
        self.assertIsNotNone(get_failure_adjustment("AMD", "swing", pms))


class ScorerIntegrationTests(unittest.TestCase):
    def test_failure_penalty_lowers_composite_and_is_explained(self):
        from lib.signal_scorer import score_signal
        base_sig = {"asset_symbol": "AMD", "direction": "Long", "confidence": 70,
                    "entry_price": 100.0, "target_price": 110.0, "stop_loss": 95.0,
                    "timeframe": "4H"}
        plain = score_signal(dict(base_sig), {}, {"risk": "low"})
        adj = {"penalty": 8.0, "failures": 9, "dominant_reason": "STOP_HIT",
               "reasons": {"STOP_HIT": 9}, "note": "9 failed signals"}
        penalized = score_signal(dict(base_sig), {}, {"risk": "low"}, failure_adjustment=adj)
        self.assertAlmostEqual(plain["composite_score"] - penalized["composite_score"], 8.0, places=1)
        self.assertEqual(penalized["score_breakdown"]["failure_penalty"], -8.0)
        self.assertEqual(penalized["score_breakdown"]["failure_history"]["dominant_reason"], "STOP_HIT")


if __name__ == "__main__":
    unittest.main()
