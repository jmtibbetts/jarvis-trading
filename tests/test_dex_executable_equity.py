"""P0 — a DEX position is worth what the book can actually get out.

`summary()` valued open positions as `qty_tokens * current_price_usd` and
added that full mark to equity. That is a MID-PRICE MULTIPLICATION, and
on-chain it is frequently not recoverable: the same position can mark at
$10,000 and quote an executable exit of $7,800, or of nothing at all when
the route has gone.

Crediting the mark to equity is the simulator paying the book money it
could not have withdrawn — the precise failure the golden rule forbids:

    THE BOT MUST NEVER MAKE MONEY BECAUSE THE SIMULATOR IS WRONG.

The scenario that matters is the one a DEX trader actually fears, and it is
the one the old arithmetic scored best: price up, liquidity gone.
"""
import unittest

from app.database import DexPortfolio, DexPosition, get_db
from lib.dex_paper import EXIT_OK, EXIT_UNPRICEABLE, exit_quote, summary


class _Pos:
    """Minimal stand-in for a DexPosition row — exit_quote reads only these."""

    def __init__(self, qty, price, reserve, dex="raydium", pid="p1"):
        self.id = pid
        self.qty_tokens = qty
        self.current_price_usd = price
        self.pool_reserve_usd_at_entry = reserve
        self.dex = dex
        self.symbol = "TSTX"
        self.mint = "MintTSTX"
        self.notional_usd = 1000.0
        self.exit_state = None
        self.exit_blocked_reason = None


class ExecutableIsNotMark(unittest.TestCase):
    def test_a_deep_pool_still_costs_something_to_exit(self):
        q = exit_quote(_Pos(1000, 1.0, 5_000_000.0))
        self.assertEqual(q["status"], EXIT_OK)
        self.assertLess(q["executable_value_usd"], q["mark_value_usd"])
        self.assertGreater(q["exit_drag_usd"], 0)

    def test_a_thin_pool_costs_far_more_than_a_deep_one(self):
        """Impact is size against depth. Same position, same mark, two
        completely different amounts of recoverable money."""
        deep = exit_quote(_Pos(1000, 10.0, 10_000_000.0))
        thin = exit_quote(_Pos(1000, 10.0, 40_000.0))
        self.assertEqual(deep["mark_value_usd"], thin["mark_value_usd"])
        self.assertGreater(thin["exit_drag_usd"], deep["exit_drag_usd"])

    def test_an_unroutable_exit_is_unknown_not_the_mark(self):
        """No pool to price against. The value is UNKNOWN — and unknown
        must never be quietly replaced by the flattering number."""
        q = exit_quote(_Pos(1000, 10.0, 0.0))
        self.assertEqual(q["status"], EXIT_UNPRICEABLE)
        self.assertIsNone(q["executable_value_usd"])
        self.assertEqual(q["mark_value_usd"], 10_000.0)
        self.assertIn("liquidity", (q["reason"] or "").lower())

    def test_the_quote_carries_its_depth_provenance(self):
        q = exit_quote(_Pos(1000, 1.0, 500_000.0))
        self.assertIn(q["depth_confidence"],
                      ("VERIFIED", "ASSUMED_BALANCED_POOL", "MODELLED_ESTIMATE"))


class TheBookRefusesToCreditWhatItCannotRecover(unittest.TestCase):
    """The end-to-end scenario from the spec, against the real tables."""

    def setUp(self):
        with get_db() as db:
            db.query(DexPosition).filter(
                DexPosition.mint.like("EQTEST%")).delete(synchronize_session=False)
            db.query(DexPortfolio).delete(synchronize_session=False)
            db.add(DexPortfolio(starting_usd=10_000.0, cash_usd=5_000.0))
            db.commit()

    def tearDown(self):
        with get_db() as db:
            db.query(DexPosition).filter(
                DexPosition.mint.like("EQTEST%")).delete(synchronize_session=False)
            db.query(DexPortfolio).delete(synchronize_session=False)
            db.commit()

    def _open(self, *, price, reserve, mint="EQTEST1"):
        with get_db() as db:
            db.add(DexPosition(
                mint=mint, symbol="EQT", dex="raydium", status="Open",
                qty_tokens=1000.0, entry_price_usd=1.0,
                current_price_usd=price, notional_usd=1000.0,
                pool_reserve_usd_at_entry=reserve))
            db.commit()

    def test_price_up_and_liquidity_gone_is_not_recoverable_equity(self):
        # 1-2. Position opened at $1; mark rises to $10.
        # 3.   Exit liquidity is gone (no reserve to route against).
        self._open(price=10.0, reserve=0.0)
        s = summary()

        # 4. Mark value is positive and large.
        self.assertEqual(s["open_value_mark_usd"], 10_000.0)
        self.assertEqual(s["equity_mark_usd"], 15_000.0)

        # 5. Executable value is NOT the mark.
        self.assertEqual(s["open_value_executable_usd"], 0.0)
        self.assertEqual(s["unpriceable_positions"], 1)
        self.assertEqual(s["unpriceable_mark_value_usd"], 10_000.0)

        # 6. THE POINT. The book must not report the mark as recoverable.
        self.assertEqual(s["equity_executable_usd"], 5_000.0)
        self.assertLess(s["equity_executable_usd"], s["equity_mark_usd"])

    def test_the_legacy_equity_name_now_means_the_conservative_total(self):
        """Every caller reading `equity_usd` was reading a number that
        overstated the book. The conservative reading wins the name."""
        self._open(price=10.0, reserve=0.0)
        s = summary()
        self.assertEqual(s["equity_usd"], s["equity_executable_usd"])
        self.assertNotEqual(s["equity_usd"], s["equity_mark_usd"])

    def test_a_priceable_position_contributes_its_executable_value(self):
        self._open(price=10.0, reserve=5_000_000.0)
        s = summary()
        self.assertEqual(s["unpriceable_positions"], 0)
        self.assertGreater(s["open_value_executable_usd"], 0)
        self.assertLess(s["open_value_executable_usd"], s["open_value_mark_usd"])
        self.assertGreater(s["exit_drag_usd"], 0)

    def test_every_open_position_reports_its_own_exit_economics(self):
        self._open(price=10.0, reserve=5_000_000.0)
        row = summary()["positions_valuation"][0]
        for field in ("mark_value_usd", "executable_exit_value_usd",
                      "exit_drag_usd", "current_exit_impact_pct",
                      "current_exit_pool_fees_usd",
                      "current_exit_network_fee_usd", "current_route",
                      "current_depth_confidence", "exit_quote_at",
                      "exit_quote_status",
                      "executable_net_unrealized_pnl_usd"):
            self.assertIn(field, row)

    def test_unrealized_pnl_is_measured_on_executable_value(self):
        """Opened at $1,000 notional, marks at $10,000, but the pool is
        thin. The P&L the book may claim is the executable one."""
        self._open(price=10.0, reserve=30_000.0)
        row = summary()["positions_valuation"][0]
        executable_pnl = row["executable_net_unrealized_pnl_usd"]
        mark_pnl = row["mark_value_usd"] - 1000.0
        self.assertLess(executable_pnl, mark_pnl)

    def test_the_valuation_policy_is_stated_in_the_payload(self):
        self._open(price=10.0, reserve=0.0)
        self.assertIn("not recoverable capital", summary()["valuation_policy"])


if __name__ == "__main__":
    unittest.main()
