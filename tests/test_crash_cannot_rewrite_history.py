"""A process crash must not select which trades happened.

THE QUESTION. A long position's stop triggers at 95. Virtual execution
determines the economics. The process dies before settlement. The market
then recovers to 110 and JARVIS restarts.

    Did the crash just delete a losing trade?

If a crash can erase a loser but not a winner — or vice versa — then process
timing, not the market, decides the book's history. That is the sharpest
possible form of "the bot makes money because the simulator is wrong",
because it is invisible: every surviving trade is individually correct.

WHAT IS ACTUALLY AT STAKE HERE, stated precisely. Virtual execution leaves
nothing resting at a venue, so an abandoned attempt is a true no-op and a
later cycle re-deciding at a fresh book is a NEW decision. The danger is not
double-execution; it is ASYMMETRY — that the re-decision systematically
favours one direction because the trigger is re-evaluated against a market
that has moved.
"""
import unittest
from unittest.mock import patch

from tests.test_pass_b_corrections import _CanonicalHarness, _seed_book


class Boom(Exception):
    """A crash, injected exactly where we want one."""


class ACrashMustNotSelectWhichTradesHappenedTests(_CanonicalHarness):

    def _economics(self, pid):
        pos, header, legs, outcomes = self._state(pid)
        return {"status": pos.status if pos else None,
                "legs": [l.kind for l in legs],
                "outcomes": len(outcomes),
                "cash": self._portfolio()["cash"]}

    def _crash_during_exit(self, pid, price):
        """Trigger an exit and die after execution, before settlement."""
        from lib import exit_dispatch as ED
        from lib import fee_authority as FA
        with patch.object(FA, "leg_fee", side_effect=Boom("crash")):
            try:
                ED.request_position_exit(pid, caller_price=price,
                                         caller_reason="stop_loss",
                                         caller_source="MARK_TO_MARKET")
            except Boom:
                return True
        return False

    def test_a_crash_during_a_LOSING_stop_does_not_delete_the_position(self):
        """The headline case. If the position silently vanished, or came
        back flat, the loss would have been erased by a crash."""
        pos, header = self._enter()
        before = self._economics(pos.id)

        _seed_book(bid=61_000.0, ask=61_100.0)          # deeply underwater
        self._crash_during_exit(pos.id, 61_000.0)

        after = self._economics(pos.id)
        self.assertEqual(after, before,
                         "an abandoned losing exit changed the book")
        self.assertEqual(after["status"], "Open",
                         "the losing position disappeared")
        self.assertEqual(after["outcomes"], 0)

    def test_the_loss_is_still_realisable_after_the_market_recovers(self):
        """The position remains OPEN and still carries its loss exposure.
        A later exit settles at the market that exists THEN — which is a new
        decision, not an erased one — and it is still one outcome."""
        pos, header = self._enter()
        entry_fill = float(header.actual_entry_fill)

        _seed_book(bid=61_000.0, ask=61_100.0)
        self._crash_during_exit(pos.id, 61_000.0)
        self.assertEqual(self._economics(pos.id)["status"], "Open")

        # Market recovers well above entry.
        _seed_book(bid=entry_fill + 3_000.0, ask=entry_fill + 3_100.0)
        from lib import exit_dispatch as ED
        res = ED.request_position_exit(pos.id, caller_price=entry_fill + 3_000.0,
                                       caller_reason="manual",
                                       caller_source="API_MANUAL")
        self.assertTrue(res.get("ok"), res)
        after = self._economics(pos.id)
        self.assertEqual(after["status"], "Closed")
        self.assertEqual(after["outcomes"], 1, "exactly one outcome")
        self.assertEqual(after["legs"], ["ENTRY", "FINAL_EXIT"])

    def test_a_crash_during_a_WINNING_target_behaves_identically(self):
        """The mirror. If a crash erased losers but preserved winners the
        book would drift upward through process failure alone; the two
        directions must be indistinguishable."""
        pos, header = self._enter()
        before = self._economics(pos.id)

        _seed_book(bid=70_000.0, ask=70_100.0)          # well in profit
        self._crash_during_exit(pos.id, 70_000.0)

        after = self._economics(pos.id)
        self.assertEqual(after, before,
                         "an abandoned winning exit changed the book")
        self.assertEqual(after["status"], "Open")
        self.assertEqual(after["outcomes"], 0)

    def test_the_two_directions_are_symmetric(self):
        """Asymmetry is the actual defect. Run both and require the same
        shape of outcome — not the same numbers, the same BEHAVIOUR."""
        shapes = {}
        for label, bid, ask in (("loss", 61_000.0, 61_100.0),
                                ("win", 70_000.0, 70_100.0)):
            # Each iteration opens against a NORMAL book — otherwise the
            # second entry is (correctly) refused by risk revalidation for
            # repricing against the moved market from the first.
            _seed_book()
            pos, header = self._enter()
            _seed_book(bid=bid, ask=ask)
            crashed = self._crash_during_exit(pos.id, bid)
            e = self._economics(pos.id)
            shapes[label] = (crashed, e["status"], e["legs"], e["outcomes"])
            # close it so the next iteration can open a fresh one
            _seed_book(bid=bid, ask=ask)
            from lib import exit_dispatch as ED
            ED.request_position_exit(pos.id, caller_price=bid,
                                     caller_reason="manual",
                                     caller_source="API_MANUAL")
        self.assertEqual(shapes["loss"], shapes["win"],
                         f"a crash treats winners and losers differently: "
                         f"{shapes}")


class ACrashDuringENTRYLeavesNoPositionTests(_CanonicalHarness):
    """Where intention becomes committed economic fact."""

    def test_a_crash_before_entry_settlement_creates_nothing(self):
        from lib import canonical_entry as CE
        from lib import fee_authority as FA
        from app.database import PaperPosition, get_db

        cash_before = self._portfolio()["cash"]
        with get_db() as db:
            n_before = db.query(PaperPosition).count()

        _seed_book(bid=64_700.0, ask=64_800.0)
        sig = {"asset_symbol": "BTC/USD", "asset_class": "Crypto",
               "paper_direction": "Long", "direction": "Long",
               "entry_price": 64_750.0, "stop_loss": 61_000.0,
               "target_price": 70_000.0, "timeframe": "4H",
               "id": "crash-entry-test"}
        with patch.object(FA, "leg_fee", side_effect=Boom("crash at entry")):
            try:
                CE.open_canonical_position(sig, decision_price=64_750.0)
            except Boom:
                pass

        with get_db() as db:
            n_after = db.query(PaperPosition).count()
        self.assertEqual(n_after, n_before,
                         "a crash before settlement left a position behind")
        self.assertEqual(self._portfolio()["cash"], cash_before,
                         "a crash before settlement moved cash")


if __name__ == "__main__":
    unittest.main()
