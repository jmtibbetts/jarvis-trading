"""Refilling the wallet must not burn the books.

MUTATING INTEGRATION TESTS: these exercise the real reset functions against
the application database — running them RESETS THE LIVE PAPER BOOK. That is
not hypothetical: the first run of this file soft-reset the live portfolio
to $50k and closed 47 real open positions mid-experiment. They are gated
behind RUN_DB_MUTATING_TESTS=1 and excluded from the default suite.

Both original resets deleted every trade row — the learning data that
outcomes, calibration and the failure postmortems read. A reset exists to
restart the EXPERIMENT, not to destroy its measurements. These tests pin
the contract: history survives, open positions become real closed trades,
and only the portfolio counters restart.
"""
import os
import unittest
import uuid

MUTATING_OK = os.getenv("RUN_DB_MUTATING_TESTS") == "1"

from app.database import (AutoSimPortfolio, AutoSimPosition, AutoSimTrade,
                          PaperPortfolio, PaperTrade, get_db, new_id, now_iso)


@unittest.skipUnless(MUTATING_OK, "mutates the live paper book — set RUN_DB_MUTATING_TESTS=1")
class PaperSoftResetTests(unittest.TestCase):
    def setUp(self):
        self.marker = f"RESET-TEST-{uuid.uuid4().hex[:8]}"
        with get_db() as db:
            db.add(PaperTrade(id=new_id(), symbol=self.marker, side="buy",
                              qty=1.0, entry_price=100.0, exit_price=110.0,
                              realized_pnl=10.0, close_reason="take_profit",
                              opened_at=now_iso(), closed_at=now_iso()))
            db.commit()

    def tearDown(self):
        with get_db() as db:
            db.query(PaperTrade).filter(PaperTrade.symbol == self.marker).delete()
            db.commit()

    def test_soft_reset_keeps_trade_history(self):
        from lib.paper_engine import soft_reset_paper_portfolio
        r = soft_reset_paper_portfolio(starting_cash=50_000)
        self.assertTrue(r["ok"])
        self.assertTrue(r["history_preserved"])
        with get_db() as db:
            survived = db.query(PaperTrade).filter(
                PaperTrade.symbol == self.marker).count()
            self.assertEqual(survived, 1, "soft reset deleted trade history")
            p = db.query(PaperPortfolio).first()
            self.assertEqual(p.cash, 50_000)
            self.assertEqual(p.total_trades, 0)


@unittest.skipUnless(MUTATING_OK, "mutates the live auto-sim book — set RUN_DB_MUTATING_TESTS=1")
class AutoSimSoftResetTests(unittest.TestCase):
    USER = "soft-reset-test-user"

    def setUp(self):
        with get_db() as db:
            db.add(AutoSimTrade(id=new_id(), user_id=self.USER, symbol="T/USD",
                                direction="Long", side="buy", qty=1.0,
                                entry_price=100.0, exit_price=90.0,
                                gross_pnl=-10.0, fees=0.1, realized_pnl=-10.1,
                                pnl_pct=-1.0, close_reason="stop_loss",
                                opened_at=now_iso(), closed_at=now_iso()))
            db.add(AutoSimPosition(id=new_id(), user_id=self.USER, symbol="T/USD",
                                   signal_id=f"test-{uuid.uuid4().hex[:8]}",
                                   direction="Long", side="buy", qty=2.0,
                                   entry_price=100.0, current_price=105.0,
                                   leverage=1.0, margin_used=200.0,
                                   opened_at=now_iso()))
            db.commit()

    def tearDown(self):
        with get_db() as db:
            for model in (AutoSimTrade, AutoSimPosition, AutoSimPortfolio):
                db.query(model).filter(model.user_id == self.USER).delete()
            db.commit()

    def test_history_survives_and_open_position_becomes_a_trade(self):
        from lib.auto_simulator import soft_reset_auto_simulator
        r = soft_reset_auto_simulator(user_id=self.USER, starting_cash=25_000)
        self.assertTrue(r["ok"])
        self.assertEqual(r["positions_closed"], 1)
        with get_db() as db:
            trades = db.query(AutoSimTrade).filter(
                AutoSimTrade.user_id == self.USER).all()
            # 1 pre-existing + 1 from closing the open position
            self.assertEqual(len(trades), 2)
            reset_closes = [t for t in trades if t.close_reason == "reset"]
            self.assertEqual(len(reset_closes), 1,
                             "the open position must land in history")
            self.assertEqual(db.query(AutoSimPosition).filter(
                AutoSimPosition.user_id == self.USER).count(), 0)
            p = db.query(AutoSimPortfolio).filter(
                AutoSimPortfolio.user_id == self.USER).first()
            self.assertEqual(p.starting_cash, 25_000)
            self.assertEqual(p.total_trades, 0)


class HardResetStaysExplicitTests(unittest.TestCase):
    def test_the_destructive_paths_still_exist_but_are_labelled(self):
        """Hard resets remain for corrupt data, and their docstrings must
        say what they destroy — a rename or a silent alias would let the
        old behaviour hide behind the new name."""
        from lib.auto_simulator import reset_auto_simulator
        from lib.paper_engine import reset_paper_portfolio
        self.assertIn("DESTRUCTIVE", reset_paper_portfolio.__doc__)
        self.assertIn("DESTRUCTIVE", reset_auto_simulator.__doc__)


if __name__ == "__main__":
    unittest.main()
