"""Refilling the wallet must not burn the books.

These exercise the REAL reset functions, and they now run in the normal
suite. That was not always safe: the first run of this file soft-reset the
live portfolio to $50k and closed 47 real open positions mid-experiment,
and the response at the time was an opt-in flag, RUN_DB_MUTATING_TESTS.

The flag has since been guarding a danger that cannot happen. Protection
became STRUCTURAL and layered: conftest.py sets JARVIS_UNDER_PYTEST before
any app import, app.database._resolve_db_path then REFUSES to open the
operator database outright, the session is redirected to a temp file, and
pytest_configure asserts the resolved path is a test path. A test that
forgets all of this still cannot reach real state.

So the flag was no longer protecting anything — it was only withholding
coverage of the reset contract, which is precisely the contract that
incident was about. `assert_disposable_database()` below re-checks the
resolved path at run time, so if the structural guard is ever weakened
these tests refuse rather than proceed.

Both original resets deleted every trade row — the learning data that
outcomes, calibration and the failure postmortems read. A reset exists to
restart the EXPERIMENT, not to destroy its measurements. These tests pin
the contract: history survives, open positions become real closed trades,
and only the portfolio counters restart.
"""
import os
import unittest
import uuid

from app.database import (AutoSimPortfolio, AutoSimPosition, AutoSimTrade,
                          PaperPortfolio, PaperTrade, get_db, new_id, now_iso)


def assert_disposable_database():
    """Defence in depth. app.database refuses the operator DB under pytest;
    this refuses anything that is not visibly a throwaway, so a future change
    to that guard turns these tests red instead of destructive."""
    from app.database import DB_PATH
    resolved = str(DB_PATH)
    if "jarvis-test-db-" not in resolved:
        raise AssertionError(
            f"refusing to run a mutating reset against {resolved} - "
            "expected a temporary pytest database")
    return resolved


class PaperSoftResetTests(unittest.TestCase):
    def setUp(self):
        assert_disposable_database()
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


class AutoSimSoftResetTests(unittest.TestCase):
    USER = "soft-reset-test-user"

    def setUp(self):
        assert_disposable_database()
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


class ResetAllCoversEveryBookTests(unittest.TestCase):
    """One reset, one clean slate.

    Reset was the only Danger Zone action without an EVERYTHING scope —
    flatten had one, reset had a button per book. Resetting the paper book
    alone left Auto Sim's positions standing in the combined equity the
    Positions tab displays, which reads exactly like "the reset refilled
    cash and left the orders open". These tests are non-mutating: the reset
    functions themselves are patched, because what is under test is that
    the endpoint calls BOTH of them.
    """

    def _call(self, paper, autosim):
        from unittest.mock import patch
        from app.routers.trading import reset_all_virtual_books
        with patch("lib.paper_engine.soft_reset_paper_portfolio", paper), \
             patch("lib.auto_simulator.soft_reset_auto_simulator", autosim):
            return reset_all_virtual_books(starting_cash=100_000.0)

    def test_both_books_are_reset(self):
        from unittest.mock import MagicMock
        paper = MagicMock(return_value={"ok": True, "positions_closed": 3})
        autosim = MagicMock(return_value={"ok": True, "positions_closed": 4})
        out = self._call(paper, autosim)
        self.assertTrue(out["ok"], out)
        paper.assert_called_once()
        autosim.assert_called_once()
        self.assertEqual(set(out["books"]), {"paper", "auto_sim"},
                         "a virtual book was left out of the combined reset")
        self.assertEqual(out["positions_closed"], 7,
                         "the count must span both books")

    def test_every_registered_position_book_is_covered(self):
        """The combined reset must not drift behind the book registry."""
        from unittest.mock import MagicMock
        from lib.concentration import POSITION_BOOKS
        out = self._call(MagicMock(return_value={}), MagicMock(return_value={}))
        self.assertEqual(
            set(out["books"]), set(POSITION_BOOKS),
            "a book exists that /reset/all does not reset — one reset must "
            "mean one clean slate across every book")

    def test_one_book_failing_still_resets_the_other_and_says_so(self):
        from unittest.mock import MagicMock
        paper = MagicMock(side_effect=RuntimeError("paper db locked"))
        autosim = MagicMock(return_value={"ok": True, "positions_closed": 2})
        out = self._call(paper, autosim)
        autosim.assert_called_once()
        self.assertFalse(out["ok"])
        self.assertTrue(any("paper" in e for e in out["errors"]), out)
        self.assertIn("auto_sim", out["books"],
                      "one book's failure must not abort the other")

    def test_the_starting_cash_reaches_both_books(self):
        from unittest.mock import MagicMock
        from unittest.mock import patch
        from app.routers.trading import reset_all_virtual_books
        paper, autosim = MagicMock(return_value={}), MagicMock(return_value={})
        with patch("lib.paper_engine.soft_reset_paper_portfolio", paper), \
             patch("lib.auto_simulator.soft_reset_auto_simulator", autosim):
            reset_all_virtual_books(starting_cash=25_000.0)
        self.assertEqual(paper.call_args.kwargs["starting_cash"], 25_000.0)
        self.assertEqual(autosim.call_args.kwargs["starting_cash"], 25_000.0)


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
