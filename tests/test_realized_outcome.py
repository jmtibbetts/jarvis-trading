"""The exchange decides what a trade made. Learning records it.

`learning_engine.record_trade_outcome` re-derived P&L from four loose
scalars and got it wrong in two independent ways:

    if direction.upper() in ("SELL", "SHORT", "SELL_SHORT"):
        pnl_pct = -pnl_pct

"Short_10x" is not in that tuple. Neither is "Short_5x",
"Short_Leveraged" or "Bearish". Every leveraged short kept a LONG sign, so
a winning short was recorded as a LOSS — and pattern memory, regime
performance and calibration all learned from those rows.

    pnl_usd = pnl_pct / 100.0 * entry_price * qty

No multiplier. One MES contract moving 10 points is $50, not $10; one gold
contract moving $10 is $1,000, not $10. Futures outcomes were understated
by the whole contract multiplier, so futures read as a low-impact asset
class and every comparison against equities was distorted.

Both defects share one cause: a second implementation of arithmetic that
already existed and was already correct elsewhere.
"""
import unittest

from lib.realized_outcome import (BREAKEVEN, LOSS, WIN, RealizedOutcome,
                                  attribute_execution, build, finalize)


class SideTests(unittest.TestCase):
    """Phase 3's bug, arriving through a different module."""

    def test_a_leveraged_short_profits_when_price_falls(self):
        for d in ("Short_10x", "Short_5x", "Short_Leveraged", "Bearish"):
            o = build(symbol="BTC/USD", direction=d, entry_fill=100.0,
                      exit_fill=90.0, quantity=1.0)
            self.assertGreater(o.gross_pnl_usd, 0, d)
            self.assertEqual(o.outcome, WIN, d)
            self.assertEqual(o.side, "short", d)

    def test_the_old_tuple_would_have_inverted_every_one(self):
        """Proves the fixture reproduces the defect."""
        for d in ("Short_10x", "Short_5x", "Short_Leveraged", "Bearish"):
            flipped = d.upper() in ("SELL", "SHORT", "SELL_SHORT")
            self.assertFalse(flipped, f"{d} kept a long sign")

    def test_a_long_is_unaffected(self):
        o = build(symbol="NVDA", direction="Long", entry_fill=100.0,
                  exit_fill=110.0, quantity=10.0)
        self.assertAlmostEqual(o.gross_pnl_usd, 100.0)
        self.assertEqual(o.outcome, WIN)

    def test_an_unreadable_direction_refuses_rather_than_assuming_long(self):
        for d in ("Aggressive_Moon_Mode", "", None, "LONGSHORT"):
            with self.assertRaises(ValueError, msg=repr(d)):
                build(symbol="NVDA", direction=d, entry_fill=100.0,
                      exit_fill=110.0, quantity=1.0)


class MultiplierTests(unittest.TestCase):
    def test_one_mes_contract_moving_ten_points_is_fifty_dollars(self):
        o = build(symbol="MES=F", direction="Long", entry_fill=5000.0,
                  exit_fill=5010.0, quantity=1.0)
        self.assertAlmostEqual(o.gross_pnl_usd, 50.0)
        self.assertEqual(o.multiplier, 5)
        self.assertEqual(o.quantity_unit, "CONTRACTS")

    def test_one_gold_contract_moving_ten_dollars_is_a_thousand(self):
        o = build(symbol="GC=F", direction="Long", entry_fill=2000.0,
                  exit_fill=2010.0, quantity=1.0)
        self.assertAlmostEqual(o.gross_pnl_usd, 1000.0)

    def test_the_old_arithmetic_understated_it_by_the_multiplier(self):
        old = (5010.0 - 5000.0) * 1.0            # no multiplier
        new = build(symbol="MES=F", direction="Long", entry_fill=5000.0,
                    exit_fill=5010.0, quantity=1.0).gross_pnl_usd
        self.assertEqual(old, 10.0)
        self.assertEqual(new, 50.0)
        self.assertAlmostEqual(new / old, 5.0)

    def test_equities_and_crypto_keep_multiplier_one(self):
        for s in ("NVDA", "BTC/USD"):
            o = build(symbol=s, direction="Long", entry_fill=100.0,
                      exit_fill=101.0, quantity=1.0)
            self.assertEqual(o.multiplier, 1.0, s)
            self.assertAlmostEqual(o.gross_pnl_usd, 1.0, msg=s)

    def test_a_short_future_still_carries_the_multiplier(self):
        o = build(symbol="MES=F", direction="Short_10x", entry_fill=5010.0,
                  exit_fill=5000.0, quantity=1.0)
        self.assertAlmostEqual(o.gross_pnl_usd, 50.0)


class CostAccountingTests(unittest.TestCase):
    """Each economic cost hits net P&L EXACTLY ONCE."""

    def test_explicit_charges_are_subtracted(self):
        o = build(symbol="NVDA", direction="Long", entry_fill=100.0,
                  exit_fill=110.0, quantity=10.0,
                  commission_usd=2.0, regulatory_fees_usd=0.5,
                  funding_usd=1.5)
        self.assertAlmostEqual(o.gross_pnl_usd, 100.0)
        self.assertAlmostEqual(o.explicit_fees_usd, 4.0)
        self.assertAlmostEqual(o.net_pnl_usd, 96.0)

    def test_attribution_is_NOT_subtracted_again(self):
        """Spread and slippage are already inside the fill price. Charging
        them again is the double-count the accounting rule forbids."""
        o = build(symbol="NVDA", direction="Long", entry_fill=100.0,
                  exit_fill=110.0, quantity=10.0,
                  spread_attribution_usd=5.0,
                  slippage_attribution_usd=7.0,
                  price_impact_attribution_usd=3.0)
        self.assertAlmostEqual(o.net_pnl_usd, 100.0,
                               msg="attribution was charged to cash")

    def test_a_gross_win_lost_to_costs_is_recorded_as_a_loss(self):
        o = build(symbol="NVDA", direction="Long", entry_fill=100.0,
                  exit_fill=100.5, quantity=10.0, commission_usd=8.0)
        self.assertGreater(o.gross_pnl_usd, 0)
        self.assertLess(o.net_pnl_usd, 0)
        self.assertEqual(o.outcome, LOSS)

    def test_dex_fees_are_explicit_charges(self):
        o = build(symbol="SOL/USDC", direction="Long", entry_fill=100.0,
                  exit_fill=110.0, quantity=1.0,
                  pool_fees_usd=0.3, network_fees_usd=0.02)
        self.assertAlmostEqual(o.explicit_fees_usd, 0.32)
        self.assertAlmostEqual(o.net_pnl_usd, 10.0 - 0.32)


class RMultipleTests(unittest.TestCase):
    def test_r_is_derived_from_the_initial_risk(self):
        o = build(symbol="NVDA", direction="Long", entry_fill=100.0,
                  exit_fill=110.0, quantity=10.0, initial_risk_usd=50.0)
        self.assertAlmostEqual(o.gross_r, 2.0)
        self.assertAlmostEqual(o.net_r, 2.0)

    def test_net_r_reflects_costs(self):
        o = build(symbol="NVDA", direction="Long", entry_fill=100.0,
                  exit_fill=110.0, quantity=10.0, initial_risk_usd=50.0,
                  commission_usd=25.0)
        self.assertAlmostEqual(o.gross_r, 2.0)
        self.assertAlmostEqual(o.net_r, 1.5)

    def test_no_initial_risk_leaves_r_unknown_not_zero(self):
        o = build(symbol="NVDA", direction="Long", entry_fill=100.0,
                  exit_fill=110.0, quantity=10.0)
        self.assertIsNone(o.gross_r)
        self.assertIsNone(o.net_r)


class MfeIsNotRealizedTests(unittest.TestCase):
    def test_mfe_never_becomes_the_realized_result(self):
        """Exit at +2R while the path later reached +5R."""
        o = build(symbol="NVDA", direction="Long", entry_fill=100.0,
                  exit_fill=104.0, quantity=10.0, initial_risk_usd=20.0)
        o.mfe_r = 5.0
        self.assertAlmostEqual(o.net_r, 2.0)
        self.assertNotEqual(o.net_r, o.mfe_r)


class LearningRecordsRatherThanRecomputesTests(unittest.TestCase):
    def test_the_recorder_accepts_a_canonical_outcome(self):
        import inspect

        from lib import learning_engine
        sig = inspect.signature(learning_engine.record_trade_outcome)
        self.assertIn("realized", sig.parameters)

    def test_the_legacy_path_is_now_strict_and_multiplier_aware(self):
        """Checked against the AST, not the source text — the old tuple is
        quoted in a comment explaining the defect, and a naive string
        search would match the documentation rather than the code."""
        import ast
        import inspect
        import textwrap

        from lib import learning_engine
        tree = ast.parse(textwrap.dedent(
            inspect.getsource(learning_engine.record_trade_outcome)))

        # No LIVE membership test against the side literals that missed
        # every leveraged short.
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare):
                for comp in node.comparators:
                    if isinstance(comp, (ast.Tuple, ast.List, ast.Set)):
                        vals = {e.value for e in comp.elts
                                if isinstance(e, ast.Constant)}
                        self.assertNotIn(
                            "SELL_SHORT", vals,
                            "the tuple that missed every leveraged short is back")

        called = {getattr(n.func, "attr", None) or getattr(n.func, "id", None)
                  for n in ast.walk(tree) if isinstance(n, ast.Call)}
        self.assertIn("parse_side_strict", called)
        self.assertIn("resolve", called, "the multiplier must come from the "
                                         "instrument, not be assumed 1")


class AttributionTests(unittest.TestCase):
    def test_decision_versus_fill_is_recorded_not_charged(self):
        o = build(symbol="NVDA", direction="Long", entry_fill=100.5,
                  exit_fill=110.0, quantity=10.0)
        before = o.net_pnl_usd
        attribute_execution(o, decision_entry=100.0)
        finalize(o)
        self.assertNotEqual(o.slippage_attribution_usd, 0.0)
        self.assertAlmostEqual(o.net_pnl_usd, before,
                               msg="attribution changed cash")


if __name__ == "__main__":
    unittest.main()
