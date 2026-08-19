"""A committed virtual execution survives the death of the process.

THE CORRECTION THIS FILE MAKES. An earlier version of these tests asserted
that a crash between execution and settlement left the book untouched, and
concluded that "a crash cannot erase a losing stop". That conclusion was
wrong. The stop execution WAS erased — symmetrically with winners, which
rules out DIRECTIONAL bias and says nothing about PROCESS-TIMING bias.

A simulator whose history depends on whether Python happened to die is still
wrong. A stop that triggered at 61,000 and vanished on a crash, leaving a
position that later closed at 70,000, has had its market history rewritten
by an operating-system event.

THE BOUNDARY. A virtual fill is COMMITTED the moment the venue returns it.

    before commitment  ->  nothing economic happened; a crash is a clean no-op
    after  commitment  ->  the fill is immutable and settlement finishes it

These tests pin both halves.
"""
import unittest
from unittest.mock import patch

from tests.test_pass_b_corrections import _CanonicalHarness, _seed_book


class Boom(Exception):
    """A crash, injected exactly where we want one."""


class _Boundary(_CanonicalHarness):

    def _commitments(self, position_id=None):
        from app.database import VirtualExecutionCommitment, get_db
        with get_db() as db:
            q = db.query(VirtualExecutionCommitment)
            if position_id:
                q = q.filter(
                    VirtualExecutionCommitment.position_id == position_id)
            rows = q.all()
            return [{"execution_id": r.execution_id, "state": r.state,
                     "filled_qty": r.filled_qty, "fill_price": r.fill_price,
                     "intent": r.intent_kind} for r in rows]

    def _arm(self, pid, *, stop=None, target=None):
        """Trigger confirmation is exact: the book must really cross the
        level. These tests are about the CRASH WINDOW, so the level is set
        explicitly rather than relying on whatever the fixture chose."""
        kw = {}
        if stop is not None:
            kw["stop_loss"] = stop
        if target is not None:
            kw["target_price"] = target
        self._set_position(pid, **kw)

    def _crash_after_commit(self, pid, price, reason, source,
                            trigger_price=None):
        """Trigger an exit, let it COMMIT, then die before settlement.

        The fee authority is the first thing touched after the commit
        boundary, so poisoning it lands the crash in exactly the window
        that used to erase the execution.

        `trigger_price` is required for a threshold exit: the dispatcher
        confirms the level against the exact book rather than trusting a
        caller's word for it, so a stop with no named threshold is refused
        before execution — correctly.
        """
        from lib import exit_dispatch as ED
        from lib import fee_authority as FA
        with patch.object(FA, "leg_fee", side_effect=Boom("crash")):
            try:
                ED.request_position_exit(pid, caller_price=price,
                                         caller_reason=reason,
                                         caller_source=source,
                                         trigger_price=trigger_price)
            except Boom:
                return True
        return False


class ACommittedStopSurvivesACrashTests(_Boundary):

    def test_the_losing_stop_is_committed_before_the_crash(self):
        pos, header = self._enter()
        self._arm(pos.id, stop=62_000.0, target=999_999.0)
        _seed_book(bid=61_000.0, ask=61_100.0)
        self.assertTrue(self._crash_after_commit(
            pos.id, 61_000.0, "stop_loss", "MARK_TO_MARKET",
            trigger_price=62_000.0))

        rows = self._commitments(pos.id)
        self.assertEqual(len(rows), 1,
                         "the fill was never committed — a crash would erase "
                         "it")
        self.assertEqual(rows[0]["state"], "COMMITTED_PENDING_SETTLEMENT")
        # The committed price is the stop's own market, not a later one.
        self.assertLess(rows[0]["fill_price"], 62_000.0)

    def test_recovery_settles_at_the_ORIGINAL_price_not_the_new_market(self):
        """The headline. The market recovers to 70k after the crash; the
        settlement must still be the 61k stop."""
        from lib import execution_recovery as RECOV
        pos, header = self._enter()
        self._arm(pos.id, stop=62_000.0, target=999_999.0)
        _seed_book(bid=61_000.0, ask=61_100.0)
        self._crash_after_commit(pos.id, 61_000.0, "stop_loss",
                                 "MARK_TO_MARKET", trigger_price=62_000.0)
        committed = self._commitments(pos.id)[0]

        # The market recovers far above entry before JARVIS comes back.
        _seed_book(bid=70_000.0, ask=70_100.0)
        out = RECOV.recover_pending()
        self.assertEqual(out["settled"], 1, out)

        pos2, _, legs, outcomes = self._state(pos.id)
        self.assertEqual(pos2.status, "Closed")
        self.assertEqual(len(outcomes), 1)
        exit_leg = [l for l in legs if l.kind == "FINAL_EXIT"][0]
        self.assertAlmostEqual(float(exit_leg.fill_price),
                               committed["fill_price"], places=8,
                               msg="the stop settled at the RECOVERED market "
                                   "— a crash rewrote market history")
        self.assertAlmostEqual(float(exit_leg.filled_qty),
                               committed["filled_qty"], places=8)

    def test_the_committed_row_is_marked_settled_exactly_once(self):
        from lib import execution_recovery as RECOV
        pos, header = self._enter()
        self._arm(pos.id, stop=62_000.0, target=999_999.0)
        _seed_book(bid=61_000.0, ask=61_100.0)
        self._crash_after_commit(pos.id, 61_000.0, "stop_loss",
                                 "MARK_TO_MARKET", trigger_price=62_000.0)
        _seed_book(bid=70_000.0, ask=70_100.0)

        first = RECOV.recover_pending()
        second = RECOV.recover_pending()      # a second sweep must be a no-op
        self.assertEqual(first["settled"], 1)
        self.assertEqual(second["found"], 0, "the commitment settled twice")

        rows = self._commitments(pos.id)
        self.assertEqual([r["state"] for r in rows], ["SETTLED"])
        _, _, legs, outcomes = self._state(pos.id)
        self.assertEqual(len([l for l in legs if l.kind == "FINAL_EXIT"]), 1,
                         "a duplicate exit leg was created")
        self.assertEqual(len(outcomes), 1)


class ACommittedTargetSurvivesTooTests(_Boundary):
    """The mirror: process failure must not erase a winner either."""

    def test_a_winning_target_settles_at_its_own_price(self):
        from lib import execution_recovery as RECOV
        pos, header = self._enter()
        self._arm(pos.id, stop=1.0, target=69_000.0)
        _seed_book(bid=70_000.0, ask=70_100.0)
        self._crash_after_commit(pos.id, 70_000.0, "take_profit",
                                 "MARK_TO_MARKET", trigger_price=69_000.0)
        committed = self._commitments(pos.id)[0]
        self.assertGreater(committed["fill_price"], 69_000.0)

        # Market collapses before recovery runs.
        _seed_book(bid=60_000.0, ask=60_100.0)
        out = RECOV.recover_pending()
        self.assertEqual(out["settled"], 1, out)

        _, _, legs, _ = self._state(pos.id)
        exit_leg = [l for l in legs if l.kind == "FINAL_EXIT"][0]
        self.assertAlmostEqual(float(exit_leg.fill_price),
                               committed["fill_price"], places=8,
                               msg="the winning exit settled at the collapsed "
                                   "market — a crash rewrote history")


class BeforeTheBoundaryNothingHappenedTests(_Boundary):
    """The other half of the rule, and the reason the boundary is clean."""

    def test_a_crash_before_the_venue_returns_leaves_no_commitment(self):
        from lib import execution_venue as EV
        from lib import exit_dispatch as ED
        pos, header = self._enter()
        _seed_book(bid=64_700.0, ask=64_800.0)

        with patch.object(EV, "submit", side_effect=Boom("crash pre-fill")):
            try:
                ED.request_position_exit(pos.id, caller_price=64_700.0,
                                         caller_reason="stop_loss",
                                         caller_source="MARK_TO_MARKET")
            except Boom:
                pass

        self.assertEqual(self._commitments(pos.id), [],
                         "a commitment exists for a fill that never happened")
        pos2, _, legs, outcomes = self._state(pos.id)
        self.assertEqual(pos2.status, "Open")
        self.assertEqual([l.kind for l in legs], ["ENTRY"])
        self.assertEqual(outcomes, [])

    def test_nothing_pending_means_recovery_is_a_no_op(self):
        from lib import execution_recovery as RECOV
        out = RECOV.recover_pending()
        self.assertEqual(out["found"], 0)
        self.assertEqual(out["settled"], 0)


class ANormalExitCommitsAndSettlesTests(_Boundary):
    """The happy path still leaves a clean, SETTLED trail."""

    def test_an_uninterrupted_exit_leaves_a_settled_commitment(self):
        from lib import exit_dispatch as ED
        pos, header = self._enter()
        _seed_book(bid=64_700.0, ask=64_800.0)
        res = ED.request_position_exit(pos.id, caller_price=64_700.0,
                                       caller_reason="manual",
                                       caller_source="API_MANUAL")
        self.assertTrue(res.get("ok"), res)
        rows = self._commitments(pos.id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "SETTLED")
        self.assertEqual(rows[0]["intent"], "EXIT")


if __name__ == "__main__":
    unittest.main()
