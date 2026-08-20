"""Every cost is charged exactly once, proven on a real round trip.

The claimed algebra (canonical_settlement's own header):

    net_pnl_usd = sum(leg gross - exit fees - holding) - entry fee

and the strongest measurable invariant: a COMPLETED position's total
contribution to portfolio.realized_pnl equals its one canonical net.

These tests do a genuine open -> close through the real engine and check
the arithmetic four different ways. Spread and slippage are ATTRIBUTION —
already inside the fill prices — so they must NOT appear in the
subtraction chain; if someone ever subtracts them again, the identity in
test 1 breaks by exactly their magnitude, which is the loudest possible
way for a double-charge to announce itself.
"""
import unittest

from tests.test_pass_b_corrections import _CanonicalHarness, _seed_book


class OneRoundTripChargesEachCostOnceTests(_CanonicalHarness):

    def _round_trip(self):
        from lib import exit_dispatch as ED
        pos, header = self._enter()
        _seed_book(bid=64_700.0, ask=64_800.0)
        res = ED.request_position_exit(pos.id, caller_price=64_700.0,
                                       caller_reason="manual",
                                       caller_source="API_MANUAL")
        self.assertTrue(res.get("ok"), res)
        pos2, header2, legs, outcomes = self._state(pos.id)
        self.assertEqual(len(outcomes), 1)
        return pos2, legs, outcomes[0]

    def test_the_outcome_algebra_is_internally_exact(self):
        """net = gross - commission - regulatory - funding - borrow,
        EXACTLY. Attribution fields are not in the chain — subtracting
        spread or slippage again would break this identity by their
        magnitude."""
        _, legs, out = self._round_trip()
        recomputed = (float(out.gross_pnl_usd)
                      - float(out.commission_usd or 0)
                      - float(out.regulatory_fees_usd or 0)
                      - float(out.funding_usd or 0)
                      - float(out.borrow_cost_usd or 0))
        self.assertAlmostEqual(float(out.net_pnl_usd), recomputed, places=9,
                               msg="the outcome's own cost chain does not "
                                   "reproduce its net — something is "
                                   "charged twice or not at all")

    def test_commission_is_the_sum_of_leg_fees_each_counted_once(self):
        _, legs, out = self._round_trip()
        leg_fees = sum(float(l.explicit_fee_usd or 0) for l in legs)
        self.assertAlmostEqual(float(out.commission_usd or 0), leg_fees,
                               places=9,
                               msg="outcome commission disagrees with the "
                                   "per-leg fee ledger")
        entry = [l for l in legs if l.kind == "ENTRY"]
        exits = [l for l in legs if l.kind == "FINAL_EXIT"]
        self.assertEqual(len(entry), 1)
        self.assertEqual(len(exits), 1)
        self.assertGreater(float(entry[0].explicit_fee_usd or 0), 0,
                           "the entry leg was never charged a fee")
        self.assertGreater(float(exits[0].explicit_fee_usd or 0), 0,
                           "the exit leg was never charged a fee")

    def test_carry_comes_from_the_exit_legs_only(self):
        """The entry leg is structurally zero-carry; holding cost accrues
        on what exits, over its own interval."""
        _, legs, out = self._round_trip()
        entry = [l for l in legs if l.kind == "ENTRY"][0]
        self.assertEqual(float(entry.holding_cost_usd or 0), 0.0)
        exit_carry = sum(float(l.holding_cost_usd or 0)
                         for l in legs if l.kind != "ENTRY")
        charged = float(out.funding_usd or 0) + float(out.borrow_cost_usd or 0)
        self.assertAlmostEqual(charged, exit_carry, places=9)

    def test_the_position_row_never_carries_a_second_fee(self):
        """A2's rule: PaperPosition.fees stays 0 so the close path cannot
        bill the same economics twice."""
        pos, legs, out = self._round_trip()
        self.assertEqual(float(pos.fees or 0), 0.0,
                         "a fee is parked on the position row where the "
                         "close path could charge it again")

    def test_portfolio_realized_equals_the_canonical_net(self):
        from app.database import PaperPortfolio, get_db
        with get_db() as db:
            before = float(db.query(PaperPortfolio).first().realized_pnl or 0)
        _, _, out = self._round_trip()
        with get_db() as db:
            after = float(db.query(PaperPortfolio).first().realized_pnl or 0)
        self.assertAlmostEqual(after - before, float(out.net_pnl_usd),
                               places=6,
                               msg="the portfolio moved by a different "
                                   "amount than the canonical net — a cost "
                                   "is being charged in one place and not "
                                   "the other")


if __name__ == "__main__":
    unittest.main()
