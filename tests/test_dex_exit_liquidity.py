"""A liquidity failure must never resolve as a perfect exit.

The close path did this:

    if not q.get("ok"):
        # Never strand a position because the exit could not be priced.
        proceeds, exit_impact, pool_fee, net_fee = gross_out, 0.0, 0.0, 0.0

So when no route could price the exit, the position closed at the GROSS
MID with zero impact, zero pool fee and zero network fee. The single
scenario a DEX trader actually fears — liquidity vanishing underneath an
open position — became the best possible outcome in the simulator: a
perfect fill at an untouched price, for free.

The comment was honest about its intent and wrong about its consequence.
Not stranding a position is a good goal; achieving it by inventing a
costless escape teaches the desk that illiquidity is harmless, and a model
that rewards illiquidity will go looking for it.

The position now stays OPEN as EXIT_PENDING_NO_LIQUIDITY, which is what it
actually is: capital that cannot currently be recovered.
"""
import unittest
from unittest.mock import patch


class UnpriceableExitTests(unittest.TestCase):
    def _open_then_fail_exit(self, session):
        """Open a position, then make every exit quote fail."""
        from lib import dex_paper

        opened = dex_paper.open_dex_position(
            mint="TestMint111", symbol="TEST", pool_address="Pool111",
            dex="raydium", reserve_usd=500_000.0, price_usd=1.0,
            size_usd=1_000.0, db=session)
        self.assertNotIn("error", opened, opened)

        with patch("lib.dex_swap_math.quote_swap",
                   return_value={"ok": False, "reason": "no route"}):
            return opened, dex_paper.close_dex_position(
                position_id=opened["position_id"], price_usd=1.0, db=session)

    def test_an_unpriceable_exit_does_not_close_the_position(self):
        from app.database import DexPosition, get_db
        with get_db() as db:
            opened, closed = self._open_then_fail_exit(db)
            self.assertEqual(closed.get("error"), "exit_unpriceable")
            pos = db.query(DexPosition).filter(
                DexPosition.id == opened["position_id"]).one()
            self.assertEqual(pos.status, "Open")
            db.rollback()

    def test_it_is_marked_exit_pending_no_liquidity(self):
        from app.database import DexPosition, get_db
        with get_db() as db:
            opened, closed = self._open_then_fail_exit(db)
            self.assertEqual(closed["state"], "EXIT_PENDING_NO_LIQUIDITY")
            pos = db.query(DexPosition).filter(
                DexPosition.id == opened["position_id"]).one()
            self.assertEqual(pos.exit_state, "EXIT_PENDING_NO_LIQUIDITY")
            self.assertTrue(pos.exit_blocked_reason)
            self.assertTrue(pos.exit_last_attempt_at)
            db.rollback()

    def test_no_costless_proceeds_are_booked(self):
        """The old path returned realized P&L computed from the gross mid."""
        from app.database import get_db
        with get_db() as db:
            _, closed = self._open_then_fail_exit(db)
            self.assertNotIn("net_pnl_usd", closed)
            self.assertNotIn("gross_pnl_usd", closed)
            self.assertIsNone(closed["executable_value_usd"])
            db.rollback()

    def test_mark_value_is_reported_but_not_treated_as_recoverable(self):
        """Mark and EXECUTABLE are different numbers, and conflating them
        is what made the failure look profitable."""
        from app.database import get_db
        with get_db() as db:
            _, closed = self._open_then_fail_exit(db)
            self.assertGreater(closed["mark_value_usd"], 0)
            self.assertIsNone(closed["executable_value_usd"])
            db.rollback()

    def test_the_reason_is_carried_not_swallowed(self):
        from app.database import get_db
        with get_db() as db:
            _, closed = self._open_then_fail_exit(db)
            self.assertIn("no route", closed["reason"])
            db.rollback()

    def test_cash_is_not_credited_for_an_exit_that_did_not_happen(self):
        from app.database import get_db
        from lib.dex_paper import get_portfolio
        with get_db() as db:
            opened, _ = self._open_then_fail_exit(db)
            before = float(get_portfolio(db).cash_usd or 0)
            # Attempt it again; cash must not move on a failed exit.
            with patch("lib.dex_swap_math.quote_swap",
                       return_value={"ok": False, "reason": "no route"}):
                from lib import dex_paper
                dex_paper.close_dex_position(
                    position_id=opened["position_id"], price_usd=1.0, db=db)
            self.assertAlmostEqual(float(get_portfolio(db).cash_usd or 0), before)
            db.rollback()


class PriceableExitStillWorksTests(unittest.TestCase):
    def test_a_normal_exit_closes_and_charges_its_costs(self):
        from app.database import get_db
        from lib import dex_paper
        with get_db() as db:
            opened = dex_paper.open_dex_position(
                mint="TestMint222", symbol="TEST2", pool_address="Pool222",
                dex="raydium", reserve_usd=500_000.0, price_usd=1.0,
                size_usd=1_000.0, db=db)
            self.assertNotIn("error", opened, opened)
            closed = dex_paper.close_dex_position(
                position_id=opened["position_id"], price_usd=1.2, db=db)
            self.assertNotIn("error", closed, closed)
            self.assertIn("net_pnl_usd", closed)
            # Costs were charged: net is below the gross price move.
            self.assertLess(closed["net_pnl_usd"], closed["gross_pnl_usd"])
            db.rollback()


class NoFreeExitGuardTests(unittest.TestCase):
    def test_the_costless_fallback_is_gone(self):
        import inspect

        from lib import dex_paper
        src = inspect.getsource(dex_paper)
        self.assertNotIn(
            "proceeds, exit_impact, pool_fee, net_fee = gross_out, 0.0, 0.0, 0.0",
            src, "the costless-exit fallback is back")


if __name__ == "__main__":
    unittest.main()
