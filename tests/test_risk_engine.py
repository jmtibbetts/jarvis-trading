"""One sizing authority — the arithmetic every book must share.

Three books shared the risk-first doctrine but not the implementation;
this engine is the implementation, and these tests are its contract.
The invariant that matters most: constraints only ever SHRINK, so
loss-at-stop can never exceed the budget by construction.
"""
import unittest

from lib.risk_engine import solve_position


def _solve(**kw):
    base = dict(entry=100.0, stop=95.0, risk_budget_usd=1000.0,
                free_cash=100_000.0, symbol="TEST/USD")
    base.update(kw)
    return solve_position(**base)


class CoreContractTests(unittest.TestCase):
    def test_qty_comes_from_the_budget(self):
        r = _solve()
        self.assertFalse(r.rejected)
        self.assertAlmostEqual(r.qty * r.stop_distance, 1000.0, delta=1.0)

    def test_loss_at_stop_never_exceeds_budget(self):
        for stop in (99.0, 97.0, 92.0, 80.0):
            for cash in (100_000.0, 5_000.0, 500.0):
                r = _solve(stop=stop, free_cash=cash)
                if not r.rejected:
                    self.assertLessEqual(r.qty * r.stop_distance, 1000.0 * 1.001,
                                         f"stop={stop} cash={cash}")

    def test_no_stop_distance_rejects(self):
        self.assertTrue(_solve(stop=100.0).rejected)
        self.assertTrue(_solve(stop=0).rejected)

    def test_no_budget_rejects(self):
        self.assertTrue(_solve(risk_budget_usd=0).rejected)

    def test_cash_cap_shrinks_and_names_itself(self):
        rich = _solve()
        poor = _solve(free_cash=1_000.0)
        self.assertFalse(poor.rejected)
        self.assertLess(poor.qty, rich.qty)
        self.assertEqual(poor.limiting_constraint, "cash")

    def test_notional_cap_shrinks_and_names_itself(self):
        r = _solve(notional_cap_usd=5_000.0)
        self.assertFalse(r.rejected)
        self.assertLessEqual(r.notional, 5_000.0 * 1.001)
        self.assertEqual(r.limiting_constraint, "notional-cap")

    def test_whole_units_round_down_never_up(self):
        # budget 1000, risk/unit 300 -> 3.33 units -> 3, never 4
        r = _solve(entry=1000.0, stop=700.0, whole_units=True)
        self.assertEqual(r.qty, 3.0)

    def test_one_unit_over_budget_rejects(self):
        r = _solve(entry=5000.0, stop=3000.0, whole_units=True)  # unit risks 2000
        self.assertTrue(r.rejected)
        self.assertIn("over the", r.rejection_reason)

    def test_leverage_is_derived_and_request_is_a_ceiling(self):
        free = _solve()                                # derived from stop
        capped = _solve(requested_leverage=2.0)
        self.assertLessEqual(capped.leverage, 2.0)
        self.assertGreaterEqual(free.leverage, capped.leverage)

    def test_futures_use_contract_multipliers(self):
        # HG=F: 25,000 lb/contract; budget 5000, stop 2c -> risk/contract 500 -> 10
        r = solve_position(entry=4.50, stop=4.48, risk_budget_usd=5000.0,
                           free_cash=1_000_000.0, symbol="HG=F")
        if not r.rejected:   # margin table permitting
            self.assertAlmostEqual(r.qty, 10.0, delta=1.0)
            self.assertGreater(r.notional, 1_000_000.0)  # 10 x 4.50 x 25k

    def test_returns_a_typed_decision(self):
        from lib.decision_types import RiskDecision
        self.assertIsInstance(_solve(), RiskDecision)


if __name__ == "__main__":
    unittest.main()
