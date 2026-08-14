"""Cost reconciliation — realized fees from the fill ledger, slippage
from recorded fills, and the fee-schedule sensitivity that shows what a
cell's verdict is made of.
"""
import unittest
from unittest.mock import patch

from app.database import KrakenTrade, TradingSignal, get_db, init_db
from lib.cost_reconciliation import (
    alpaca_realized_slippage,
    cell_fee_sensitivity,
    kraken_realized_fees,
)


class RealizedFeeTests(unittest.TestCase):
    def setUp(self):
        init_db()
        self._clean()

    def tearDown(self):
        self._clean()

    def _clean(self):
        with get_db() as db:
            db.query(KrakenTrade).filter(
                KrakenTrade.trade_id.like("TEST-CR-%")).delete(
                synchronize_session=False)
            db.query(TradingSignal).filter(
                TradingSignal.asset_symbol == "TEST-CR").delete(
                synchronize_session=False)
            db.commit()

    def test_fee_over_cost_is_the_realized_rate(self):
        with get_db() as db:
            for i, (cost, fee) in enumerate([(100.0, 0.25), (200.0, 0.50),
                                             (50.0, 0.20)]):
                db.add(KrakenTrade(trade_id=f"TEST-CR-{i}",
                                   pair="TESTUSD", side="buy",
                                   price=1.0, cost=cost, fee=fee,
                                   volume=cost))
            db.commit()
        out = kraken_realized_fees()
        self.assertGreaterEqual(out["n"], 3)
        pair = out["by_pair"]["TESTUSD"]
        # 0.25% and 0.40% fills -> median 0.25 for this pair.
        self.assertEqual(pair["median_pct"], 0.25)

    def test_slippage_reports_signed_and_absolute_separately(self):
        with get_db() as db:
            for i, slip in enumerate([-0.2, -0.1, 0.1]):
                db.add(TradingSignal(
                    id=f"TEST-CR-S{i}", asset_symbol="TEST-CR",
                    asset_class="TestClass", direction="Long",
                    entry_price=100.0, slippage_pct=slip))
            db.commit()
        out = alpaca_realized_slippage()
        row = out["testclass"]
        self.assertEqual(row["n"], 3)
        self.assertEqual(row["median_signed_pct"], -0.1)   # direction
        self.assertEqual(row["median_abs_pct"], 0.1)       # magnitude
        self.assertEqual(row["worst_pct"], -0.2)


class SensitivityTests(unittest.TestCase):
    def _fake_cell(self, gross_lower):
        # build_table cell accumulators that _summarise turns into a
        # gross_expected_r_lower near the requested value.
        return {"n": 1000.0, "wins": 500.0, "win_r": 500.0 * (1 + gross_lower),
                "loss_r": 500.0, "win_n": 500.0, "loss_n": 500.0,
                "raw": 1000, "raw_live": 100, "raw_replay": 900}

    def test_both_schedules_reported_with_gate_verdicts(self):
        cell = self._fake_cell(0.30)
        table = {(("asset_class", "timeframe"), ("crypto", "4H")): cell}
        with patch("lib.expectancy.build_table", return_value=table), \
             patch("lib.cost_reconciliation._median_stop_distance_pct",
                   return_value=0.025), \
             patch("lib.transaction_costs.estimate_costs",
                   side_effect=lambda *a, leveraged=False, **k:
                   {"total_cost_pct": 0.8 if not leveraged else 0.1,
                    "cost_r": (0.008 if not leveraged else 0.001) / 0.025}):
            out = cell_fee_sensitivity()
        self.assertEqual(out["cell"], "crypto/4H")
        schedules = {r["schedule"]: r for r in out["schedules"]}
        self.assertIn("spot", schedules)
        self.assertIn("perpetual", schedules)
        # Same gross, cheaper schedule -> net can only be >= spot's.
        s, p = schedules["spot"], schedules["perpetual"]
        if s["net_lower_r"] is not None and p["net_lower_r"] is not None:
            self.assertGreaterEqual(p["net_lower_r"], s["net_lower_r"])

    def test_missing_cell_reports_itself(self):
        with patch("lib.expectancy.build_table", return_value={}):
            out = cell_fee_sensitivity("crypto", "42H")
        self.assertIn("note", out)


if __name__ == "__main__":
    unittest.main()
