"""A committed fill's economics must not depend on when the process returned.

Two seams, both economic rather than structural:

    CARRY STOPS AT committed_at. The quantity left the position when the
    fill happened. A 45-minute outage between the fill and the restart is an
    OPERATIONAL fact; charging carry for it would make the same trade cost
    different amounts depending on how long a restart took.

    A SETTLED FILL IS NOT "ABANDONED". There is a window between settlement
    committing and the bookkeeping flag being written. A crash there leaves
    a PENDING commitment whose economics are already in the ledger and whose
    position has moved on — which looks exactly like a lost race.
"""
import struct
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tests.test_pass_b_corrections import _CanonicalHarness, _seed_book


class Boom(Exception):
    pass


class _Base(_CanonicalHarness):

    def setUp(self):
        super().setUp()          # clears commitments; see _CanonicalHarness
        self.addCleanup(self._clear_commitments)
        self._starting_book = self._snapshot_book()

    BOOK_FIELDS = ("cash", "total_trades", "winning_trades", "realized_pnl")

    def _snapshot_book(self):
        from app.database import PaperPortfolio, get_db
        with get_db() as db:
            p = db.query(PaperPortfolio).first()
            return {f: getattr(p, f) for f in self.BOOK_FIELDS}

    def _restore_starting_conditions(self):
        """Put BOTH the book of accounts and the market back.

        Sizing reads portfolio equity and the realized track record, and it
        prices the stop against the live perp book — which the previous
        run's recovery left at 70,000. Restore only one of the two and the
        next entry is a different trade (or is refused outright for
        enlarging risk). The test asserts the sizes matched, so a miss here
        fails loudly rather than quietly weakening the comparison.
        """
        from app.database import PaperPortfolio, get_db
        with get_db() as db:
            db.query(PaperPortfolio).update(dict(self._starting_book))
            db.commit()
        _seed_book()

    def _clear_commitments(self):
        """Commitments outlive a position by design, so a leftover row from
        one test would be picked up by the next test's recovery sweep."""
        from app.database import VirtualExecutionCommitment, get_db
        with get_db() as db:
            db.query(VirtualExecutionCommitment).delete()
            db.commit()

    def _set_opened_at(self, pid, when):
        """Move the trade's start. The carry interval is measured from the
        CANONICAL header, not the paper projection, so that is the row that
        has to move."""
        from app.database import PaperPositionSettlement, get_db
        with get_db() as db:
            h = db.query(PaperPositionSettlement).filter(
                PaperPositionSettlement.position_id == pid).first()
            h.opened_at = when.isoformat()
            db.commit()

    def _arm(self, pid, *, stop=None, target=None):
        kw = {}
        if stop is not None:
            kw["stop_loss"] = stop
        if target is not None:
            kw["target_price"] = target
        self._set_position(pid, **kw)

    def _commit_a_stop(self, pid, bid=61_000.0, ask=61_100.0):
        from lib import exit_dispatch as ED
        from lib import fee_authority as FA
        self._arm(pid, stop=62_000.0, target=999_999.0)
        _seed_book(bid=bid, ask=ask)
        with patch.object(FA, "leg_fee", side_effect=Boom("crash")):
            try:
                ED.request_position_exit(pid, caller_price=bid,
                                         caller_reason="stop_loss",
                                         caller_source="MARK_TO_MARKET",
                                         trigger_price=62_000.0)
            except Boom:
                pass

    def _commitment(self, pid):
        from app.database import VirtualExecutionCommitment, get_db
        with get_db() as db:
            r = db.query(VirtualExecutionCommitment).filter(
                VirtualExecutionCommitment.position_id == pid).first()
            if r is None:
                return None
            return {"execution_id": r.execution_id, "state": r.state,
                    "committed_at": r.committed_at,
                    "filled_qty": r.filled_qty, "fill_price": r.fill_price}

    def _set_committed_at(self, execution_id, when_iso):
        from app.database import VirtualExecutionCommitment, get_db
        with get_db() as db:
            r = db.query(VirtualExecutionCommitment).filter(
                VirtualExecutionCommitment.execution_id == execution_id
            ).first()
            r.committed_at = when_iso
            db.commit()

    def _exit_leg(self, pid):
        _, _, legs, _ = self._state(pid)
        finals = [l for l in legs if l.kind in ("FINAL_EXIT", "PARTIAL_EXIT")]
        return finals[-1] if finals else None


class CarryStopsWhenTheFillHappenedTests(_Base):
    """THE EXPERIMENT. Hold the real trade constant — opened 24h before the
    fill — and vary only how late recovery runs. The carry is a property of
    the TRADE, so it must not move.

    Getting this fixture right matters: an earlier version pinned the open
    to `now` instead of to the fill, which made longer downtime mean a
    SHORTER trade and would have "passed" nothing.
    """
    HELD_HOURS = 24.0

    def _recover_after_downtime(self, minutes):
        """Same 24h trade, recovered `minutes` after the fill."""
        from lib import execution_recovery as RECOV
        self._restore_starting_conditions()
        pos, header = self._enter()
        self._commit_a_stop(pos.id)
        c = self._commitment(pos.id)
        self.assertIsNotNone(c, "nothing was committed")

        committed = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        self._set_committed_at(c["execution_id"], committed.isoformat())
        self._set_opened_at(pos.id,
                            committed - timedelta(hours=self.HELD_HOURS))

        _seed_book(bid=70_000.0, ask=70_100.0)      # the market moved on
        out = RECOV.recover_pending()
        self.assertEqual(out["settled"], 1, out)
        leg = self._exit_leg(pos.id)
        return (float(leg.hours_held), float(leg.holding_cost_usd or 0.0),
                float(leg.filled_qty))

    def test_downtime_length_does_not_change_the_carry(self):
        """1 minute, 1 hour and 24 hours of downtime must all produce the
        SAME economics, because the fill happened before any of them."""
        seen = {m: self._recover_after_downtime(m)
                for m in (1, 60, 60 * 24)}
        first = seen[1]
        for minutes, (hours, carry, qty) in seen.items():
            self.assertAlmostEqual(
                qty, first[2], places=8,
                msg=f"the runs traded different sizes, so the carry "
                    f"comparison would prove nothing: {seen}")
            self.assertAlmostEqual(
                hours, first[0], places=8,
                msg=f"downtime changed the holding interval: {seen}")
            self.assertAlmostEqual(
                carry, first[1], places=8,
                msg=f"downtime changed the carry charged: {seen}")

    def test_the_interval_ends_at_the_fill_not_at_the_recovery(self):
        """The absolute check, so the test above cannot pass by being
        uniformly wrong. A 24h trade recovered 3h late is 24h of carry.
        Billing to the restart would make it 27."""
        hours, carry, _ = self._recover_after_downtime(180)
        self.assertAlmostEqual(hours, self.HELD_HOURS, places=6,
                               msg="the carry was billed to the restart, not "
                                   "to the fill")
        self.assertGreater(carry, 0.0, "a 24h perp carry of zero proves "
                                       "nothing about the cutoff")

    def test_downtime_would_have_changed_the_carry_before_the_fix(self):
        """The control. Point the cutoff back at wall-clock and the same
        fixture must FAIL — otherwise these tests pass regardless."""
        from lib import canonical_exit as CX
        real = CX._now_iso

        with patch.object(CX, "_now_iso", real):
            ok_hours, _, _ = self._recover_after_downtime(180)
        # Poison: settle at 'now' the way the pre-fix code did.
        with patch.object(CX, "settle_committed_exit",
                          _settle_at_wall_clock(CX)):
            bad_hours, _, _ = self._recover_after_downtime(180)
        self.assertAlmostEqual(ok_hours, self.HELD_HOURS, places=6)
        self.assertGreater(bad_hours, ok_hours + 2.9,
                           "the poisoned build produced the same answer, so "
                           "these tests are not measuring the cutoff")


def _settle_at_wall_clock(CX):
    """The pre-fix behaviour: date the settlement to the recovery."""
    real = CX.settle_committed_exit

    def poisoned(commitment, snap):
        c = dict(commitment)
        c["committed_at"] = None        # forces the `or _now_iso()` fallback
        return real(c, snap)
    return poisoned


class ASettledFillIsNeverAbandonedTests(_Base):
    """P0.6/P0.7 — the window between settlement and the bookkeeping flag."""

    def test_a_crash_after_settlement_converges_to_SETTLED(self):
        from lib import execution_commitment as EC
        from lib import execution_recovery as RECOV
        from lib import exit_dispatch as ED

        pos, header = self._enter()
        _seed_book(bid=64_700.0, ask=64_800.0)

        # Settle for real, but lose the flag write — exactly what a crash
        # between the two would leave behind.
        with patch.object(EC, "mark_settled", return_value=True):
            res = ED.request_position_exit(pos.id, caller_price=64_700.0,
                                           caller_reason="manual",
                                           caller_source="API_MANUAL")
        self.assertTrue(res.get("ok"), res)
        c = self._commitment(pos.id)
        self.assertEqual(c["state"], "COMMITTED_PENDING_SETTLEMENT",
                         "the fixture did not reproduce the seam")

        before = self._portfolio()["cash"]
        out = RECOV.recover_pending()

        self.assertEqual(out["abandoned"], 0,
                         "a SETTLED fill was labelled ABANDONED because "
                         "Python died before a status flag changed")
        self.assertEqual(out["settled"], 1, out)
        self.assertEqual(self._commitment(pos.id)["state"], "SETTLED")
        self.assertEqual(self._portfolio()["cash"], before,
                         "recovery mutated economics that already existed")
        _, _, legs, outcomes = self._state(pos.id)
        self.assertEqual(len([l for l in legs if l.kind == "FINAL_EXIT"]), 1,
                         "recovery created a second exit leg")
        self.assertEqual(len(outcomes), 1)

    def test_mismatched_facts_fail_loudly_rather_than_being_blessed(self):
        """An execution id that maps to different economics is corruption,
        not a recovery case."""
        from lib import execution_recovery as RECOV
        self.assertIsNotNone(RECOV._facts_disagree(
            {"position_id": "p1", "filled_qty": 10.0, "fill_price": 100.0},
            {"position_id": "p1", "filled_qty": 11.0, "fill_price": 100.0}))
        self.assertIsNone(RECOV._facts_disagree(
            {"position_id": "p1", "filled_qty": 10.0, "fill_price": 100.0},
            {"position_id": "p1", "filled_qty": 10.0, "fill_price": 100.0}))


class CommittedNumbersRoundTripExactlyTests(unittest.TestCase):
    """Durable economic facts must survive storage bit-for-bit.

    MEASURED, not assumed: SQLite REAL is an 8-byte IEEE-754 double and so
    is a Python float, so the value stored is the value returned. Compared
    on the raw bytes rather than with `==`, which would hide a near-miss.

    The settlement ledger this feeds is itself float-based with explicit
    representation tolerances, so introducing a Decimal column here would
    add a SECOND numeric framework and an inconsistency, not remove one.
    """
    AWKWARD = (0.01, 0.1, 0.3, 0.33333333, 64_812.25, 0.5, 5.0, 1e-8,
               123_456.789012345, 2 / 3)

    def test_every_awkward_value_survives_a_round_trip(self):
        from lib import execution_commitment as EC
        for i, v in enumerate(self.AWKWARD):
            eid = f"roundtrip-{i}"
            EC.record_commitment(
                execution_id=eid, intent_kind=EC.EXIT, symbol="BTC/USD",
                product="CRYPTO_PERP", venue="kraken_derivatives_us",
                instrument_id="PBTCUCZ50", side="sell",
                requested_qty=v, filled_qty=v, fill_price=v,
                quantity_unit="CONTRACTS", multiplier=v,
                fill_model="TEST", fill_model_version="v1",
                position_id="p", expected_revision=0)
            got = EC.get(eid)
            for field in ("requested_qty", "filled_qty", "fill_price",
                          "multiplier"):
                self.assertEqual(
                    struct.pack("<d", got[field]), struct.pack("<d", v),
                    f"{field} drifted for {v!r}: got {got[field]!r}")


if __name__ == "__main__":
    unittest.main()


class PartialExitCarryAlgebraTests(_Base):
    """P0.3 — quantity that has LEFT stops accruing; quantity that stayed
    keeps accruing, and a crash changes neither.

    The position opens at T0. Part of it exits at T1 and the rest at T3.
    The carry owed is therefore

        leg 1:  exited_qty_1  x  (T1 - T0)
        leg 2:  exited_qty_2  x  (T3 - T0)

    NOT the whole position over the whole life, and not the remainder
    measured from the partial. Both legs here are settled through the
    recovery path, which is what makes T1 and T3 exactly controllable —
    and simultaneously proves a crash between the partial fill and its
    settlement does not disturb the algebra.
    """
    OPENED_HOURS_AGO = 10.0
    T1_HOURS_AGO = 6.0          # the partial fill  -> 4h held
    T3_HOURS_AGO = 1.0          # the final fill    -> 9h held

    def _crash_after_partial_commit(self, pid, qty, bid=64_700.0,
                                    ask=64_800.0):
        from lib import exit_dispatch as ED
        from lib import fee_authority as FA
        _seed_book(bid=bid, ask=ask)
        with patch.object(FA, "leg_fee", side_effect=Boom("crash")):
            try:
                ED.request_position_partial_exit(
                    pid, requested_qty=qty, caller_price=bid,
                    caller_reason="scale_out_tp1", caller_source="PAPER_TP1")
            except Boom:
                pass

    def _pending_commitment(self, pid):
        """The UNFINISHED one. A split exit leaves several commitments on
        one position, so `first()` would return an already-settled row."""
        from app.database import VirtualExecutionCommitment, get_db
        with get_db() as db:
            r = db.query(VirtualExecutionCommitment).filter(
                VirtualExecutionCommitment.position_id == pid,
                VirtualExecutionCommitment.state
                == "COMMITTED_PENDING_SETTLEMENT").first()
            return None if r is None else {"execution_id": r.execution_id,
                                           "state": r.state}

    def _recover_at(self, pid, when):
        """Settle the pending commitment as though it had been filled at
        `when`. Every anchor in this test derives from ONE captured instant,
        so the intervals are exact rather than a few milliseconds off."""
        from lib import execution_recovery as RECOV
        c = self._pending_commitment(pid)
        self.assertIsNotNone(c, "nothing was committed")
        self._set_committed_at(c["execution_id"], when.isoformat())
        out = RECOV.recover_pending()
        self.assertEqual(out["settled"], 1, out)
        return c["execution_id"]

    def _leg_by_execution(self, execution_id):
        from app.database import PaperSettlementLeg, get_db
        with get_db() as db:
            leg = db.query(PaperSettlementLeg).filter(
                PaperSettlementLeg.execution_id == execution_id).first()
            db.expunge_all()
            return leg

    def _run_split_exit(self):
        pos, header = self._enter()
        now = datetime.now(timezone.utc)
        t0 = now - timedelta(hours=self.OPENED_HOURS_AGO)
        t1 = now - timedelta(hours=self.T1_HOURS_AGO)
        t3 = now - timedelta(hours=self.T3_HOURS_AGO)
        self._set_opened_at(pos.id, t0)
        original = float(pos.qty)
        first = float(int(original // 2))
        self.assertGreater(first, 0.0, "the fixture opened too small a "
                                       "position to split")

        self._crash_after_partial_commit(pos.id, first)
        leg1 = self._leg_by_execution(self._recover_at(pos.id, t1))

        pos_mid, _, _, _ = self._state(pos.id)
        self.assertEqual(pos_mid.status, "Open",
                         "a partial exit closed the position")
        remaining = float(pos_mid.qty)

        self._crash_a_final_exit(pos.id)
        leg2 = self._leg_by_execution(self._recover_at(pos.id, t3))
        return leg1, leg2, original, first, remaining

    def _crash_a_final_exit(self, pid, bid=64_700.0, ask=64_800.0):
        from lib import exit_dispatch as ED
        from lib import fee_authority as FA
        _seed_book(bid=bid, ask=ask)
        with patch.object(FA, "leg_fee", side_effect=Boom("crash")):
            try:
                ED.request_position_exit(pid, caller_price=bid,
                                         caller_reason="manual",
                                         caller_source="API_MANUAL")
            except Boom:
                pass

    def test_each_leg_is_charged_for_its_own_interval(self):
        leg1, leg2, original, first, remaining = self._run_split_exit()
        self.assertAlmostEqual(
            leg1.hours_held, self.OPENED_HOURS_AGO - self.T1_HOURS_AGO,
            places=6, msg="the partial was not charged T0 -> T1")
        self.assertAlmostEqual(
            leg2.hours_held, self.OPENED_HOURS_AGO - self.T3_HOURS_AGO,
            places=6,
            msg="the remainder was charged from the partial, not from the "
                "open — it was held the whole time")

    def test_the_quantity_that_left_stops_accruing(self):
        """The economic claim. Carry is proportional to qty x hours at one
        rate, so the two legs must agree on that rate — which they can only
        do if leg 2 is charged on the REMAINDER, not on the whole position.
        """
        leg1, leg2, original, first, remaining = self._run_split_exit()
        self.assertAlmostEqual(first + remaining, original, places=8,
                               msg="the split lost or invented quantity")
        rate1 = leg1.holding_cost_usd / (leg1.filled_qty * leg1.hours_held)
        rate2 = leg2.holding_cost_usd / (leg2.filled_qty * leg2.hours_held)
        self.assertAlmostEqual(
            rate1, rate2, places=10,
            msg=f"the legs disagree on the carry rate, so one was charged "
                f"on the wrong quantity: leg1={leg1.filled_qty}x"
                f"{leg1.hours_held}h={leg1.holding_cost_usd}, "
                f"leg2={leg2.filled_qty}x{leg2.hours_held}h="
                f"{leg2.holding_cost_usd}")
        # And state it absolutely, so a uniformly wrong pair cannot pass.
        self.assertAlmostEqual(float(leg1.filled_qty), first, places=8)
        self.assertAlmostEqual(float(leg2.filled_qty), remaining, places=8)

    def test_the_partial_did_not_move_the_positions_start(self):
        """If a partial reset opened_at, the remainder's carry would be
        silently forgiven for the first stretch of its life."""
        leg1, leg2, original, first, remaining = self._run_split_exit()
        self.assertGreater(leg2.hours_held, leg1.hours_held,
                           "the remainder was held longer than the partial, "
                           "so its interval cannot be the shorter one")


class FeeReproducibilityAcrossRestartTests(_Base):
    """P0.9/P0.10 — a recovered fill is priced under the schedule that was
    in force when it filled, or it is not priced at all.

    The fill's own facts survive a crash. The SCHEDULE does not: the fee
    authority's version and its region are process state, and the region
    comes from an environment variable. Recomputing under whatever the new
    process happens to load would charge yesterday's execution a price it
    never faced.

    The fix is NOT to delay the commit until the fee is known. The boundary
    belongs at the fill, because that is when the economic fact came into
    existence -- the last test here pins that, so a future change cannot
    "solve" reproducibility by moving it.

    MEASURED: of the two context components, the VERSION is the one that
    protects money -- revise the authority's rates and every unsettled fill
    would otherwise be repriced at the new ones. The REGION currently
    changes no product's fee at all ($3.90/contract on the perp in every
    region), so the region test below pins that the guard fires on a change
    of schedule IDENTITY, not that regions price differently today.
    """

    def _commit_then_recover(self, env=None):
        from lib import execution_recovery as RECOV
        pos, header = self._enter()
        self._commit_a_stop(pos.id)
        _seed_book(bid=70_000.0, ask=70_100.0)
        with patch.dict("os.environ", env or {}):
            return pos, RECOV.recover_pending()

    def test_the_schedule_in_force_at_the_fill_is_persisted(self):
        from lib import fee_authority as FA
        pos, header = self._enter()
        self._commit_a_stop(pos.id)
        c = self._commitment(pos.id)
        stored = self._fee_context(c["execution_id"])
        self.assertEqual(stored, FA.pricing_context(),
                         "the fill was committed without recording what "
                         "schedule would price it")
        self.assertIn("fee_authority_version", stored)
        self.assertIn("region", stored)

    def _fee_context(self, execution_id):
        from lib import execution_commitment as EC
        return (EC.get(execution_id) or {}).get("fee_context")

    def test_an_unchanged_schedule_settles_normally(self):
        pos, out = self._commit_then_recover()
        self.assertEqual(out["settled"], 1, out)

    def test_a_changed_region_refuses_rather_than_repricing(self):
        """VENUE_REGION selects the schedule. If a restart comes back with a
        different one, the owed settlement must not be priced under it."""
        pos, out = self._commit_then_recover({"VENUE_REGION": "somewhere-else"})
        self.assertEqual(out["settled"], 0, out)
        self.assertEqual(out["abandoned"], 0,
                         "a real fill was discarded because the process "
                         "environment changed")
        self.assertEqual(out["failed"], 1, out)
        self.assertEqual(out["details"][0]["error"],
                         "EXIT_FEE_SCHEDULE_CHANGED")
        # Still owed, so a corrected process can finish it.
        self.assertEqual(self._commitment(pos.id)["state"],
                         "COMMITTED_PENDING_SETTLEMENT")

    def test_the_refusal_is_recoverable_once_the_schedule_is_restored(self):
        from lib import execution_recovery as RECOV
        pos, out = self._commit_then_recover({"VENUE_REGION": "somewhere-else"})
        self.assertEqual(out["failed"], 1, out)
        again = RECOV.recover_pending()          # same process, right schedule
        self.assertEqual(again["settled"], 1, again)

    def test_the_boundary_is_at_the_fill_not_after_the_fee(self):
        """P0.10. The fee lookup is the first thing after the boundary, so
        killing it proves the commit already landed. If a later change moved
        the commit past the fee to make pricing easy, this goes red."""
        pos, header = self._enter()
        self._commit_a_stop(pos.id)
        c = self._commitment(pos.id)
        self.assertIsNotNone(
            c, "the fill was NOT committed before the fee was priced -- the "
               "boundary has moved, and a crash can once again erase an "
               "execution that really happened")
        self.assertEqual(c["state"], "COMMITTED_PENDING_SETTLEMENT")

    def test_a_revised_fee_authority_refuses_to_reprice_an_owed_fill(self):
        """THE CASE THAT PROTECTS MONEY. The rates are revised while a
        committed fill is still owed a settlement. That fill faced the old
        schedule and must not be charged the new one."""
        from lib import execution_recovery as RECOV
        from lib import fee_authority as FA
        pos, header = self._enter()
        self._commit_a_stop(pos.id)
        _seed_book(bid=70_000.0, ask=70_100.0)

        with patch.object(FA, "FEE_AUTHORITY_VERSION", "fee_authority_v2"):
            out = RECOV.recover_pending()
        self.assertEqual(out["settled"], 0, out)
        self.assertEqual(out["details"][0]["error"],
                         "EXIT_FEE_SCHEDULE_CHANGED")
        self.assertEqual(self._commitment(pos.id)["state"],
                         "COMMITTED_PENDING_SETTLEMENT",
                         "the owed fill was discarded instead of held")


class TheEntryBoundaryMatchesTheExitBoundaryTests(_Base):
    """P0.11-P0.14 — one rule for both sides of a trade.

    The exit path committed at the fill; the entry path committed at
    settlement, and recovery ABANDONED every entry it found. Those two
    cannot both be the universal rule, and the asymmetry was not harmless:
    a crash between the entry fill and its settlement erased the entry, and
    the next cycle re-decided against whatever the market had become. If it
    had moved down, JARVIS obtained a better entry by crashing -- the same
    process-timing bias the exit boundary removes, wearing the opposite
    sign.
    """

    def _crash_after_entry_commit(self):
        """Fill an entry, then die before it settles.

        The fee authority is the first thing touched after the entry commit
        boundary, so poisoning it lands the crash in exactly the window that
        used to erase the entry.
        """
        from lib import canonical_entry as CE
        from lib import fee_authority as FA
        from tests.test_pass_b_corrections import _signal, _spot_feed
        _seed_book()
        with patch.object(FA, "leg_fee", side_effect=Boom("crash")):
            with _spot_feed():
                try:
                    CE.open_canonical_position(_signal(),
                                               decision_price=64_400.0)
                except Boom:
                    pass

    def _entry_commitments(self):
        from app.database import VirtualExecutionCommitment, get_db
        with get_db() as db:
            rows = db.query(VirtualExecutionCommitment).filter(
                VirtualExecutionCommitment.intent_kind == "ENTRY").all()
            return [{"execution_id": r.execution_id, "state": r.state,
                     "filled_qty": r.filled_qty, "fill_price": r.fill_price,
                     "position_id": r.position_id} for r in rows]

    def _open_positions(self):
        from app.database import PaperPosition, get_db
        with get_db() as db:
            return [p.id for p in db.query(PaperPosition).filter(
                PaperPosition.status == "Open").all()]

    # ── Case A: the fill is committed before anything derives from it ────
    def test_the_entry_fill_is_committed_before_the_crash(self):
        self._crash_after_entry_commit()
        rows = self._entry_commitments()
        self.assertEqual(len(rows), 1,
                         "the entry fill was never committed, so a crash "
                         "erases an execution that really happened")
        self.assertEqual(rows[0]["state"], "COMMITTED_PENDING_SETTLEMENT")
        self.assertGreater(rows[0]["fill_price"], 0.0)
        # Nothing economic yet: the boundary is the fill, not the position.
        self.assertEqual(self._open_positions(), [],
                         "a position exists for an unsettled entry")

    # ── Case B: recovery opens it at the ORIGINAL fill ───────────────────
    def test_recovery_opens_the_position_at_the_committed_fill(self):
        from lib import execution_recovery as RECOV
        self._crash_after_entry_commit()
        committed = self._entry_commitments()[0]

        # The market runs away before JARVIS comes back.
        _seed_book(bid=70_000.0, ask=70_100.0)
        out = RECOV.recover_pending()
        self.assertEqual(out["abandoned"], 0,
                         "a real entry fill was discarded, which is what the "
                         "old ENTRY_RECOVERY_NOT_IMPLEMENTED branch did")
        self.assertEqual(out["settled"], 1, out)

        open_ids = self._open_positions()
        self.assertEqual(len(open_ids), 1, "recovery opened no position")
        pos, header, legs, _ = self._state(open_ids[0])
        self.assertAlmostEqual(float(header.actual_entry_fill),
                               committed["fill_price"], places=8,
                               msg="the entry settled at the RECOVERED "
                                   "market -- a crash bought at a better "
                                   "price than the order actually paid")
        self.assertAlmostEqual(float(pos.qty), committed["filled_qty"],
                               places=8)
        self.assertEqual([l.kind for l in legs], ["ENTRY"])

    def test_the_recovered_position_is_canonical_and_exitable(self):
        """A recovered entry that is not canonical is worse than none: the
        fail-closed exit guard would route it down the legacy path."""
        from lib import canonical_entry as CE
        from lib import execution_recovery as RECOV
        from app.database import PaperPosition, get_db
        self._crash_after_entry_commit()
        _seed_book(bid=70_000.0, ask=70_100.0)
        RECOV.recover_pending()

        pid = self._open_positions()[0]
        with get_db() as db:
            pos = db.query(PaperPosition).filter(
                PaperPosition.id == pid).first()
            db.expunge_all()
        self.assertTrue(CE.is_canonical(pos),
                        "the recovered position is not canonical, so the "
                        "exit guard would send it down the legacy path")

    # ── Case C: settle exactly once, however many sweeps run ─────────────
    def test_recovery_is_idempotent_and_opens_one_position(self):
        from lib import execution_recovery as RECOV
        self._crash_after_entry_commit()
        _seed_book(bid=70_000.0, ask=70_100.0)
        first = RECOV.recover_pending()
        second = RECOV.recover_pending()
        self.assertEqual(first["settled"], 1, first)
        self.assertEqual(second["found"], 0, "the entry settled twice")
        self.assertEqual(len(self._open_positions()), 1,
                         "recovery opened a second position for one fill")
        self.assertEqual([r["state"] for r in self._entry_commitments()],
                         ["SETTLED"])

    def test_an_uninterrupted_entry_leaves_a_settled_commitment(self):
        pos, header = self._enter()
        rows = self._entry_commitments()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "SETTLED",
                         "the happy path left an entry commitment dangling")

    def test_an_owed_entry_is_never_abandoned_for_a_transient_reason(self):
        """A fill that really happened is owed a settlement. 'We could not
        price it today' is not the same fact as 'it must never settle'."""
        from lib import execution_recovery as RECOV
        self._crash_after_entry_commit()
        _seed_book(bid=70_000.0, ask=70_100.0)
        with patch.dict("os.environ", {"VENUE_REGION": "somewhere-else"}):
            out = RECOV.recover_pending()
        self.assertEqual(out["abandoned"], 0, out)
        self.assertEqual(out["failed"], 1, out)
        self.assertEqual(self._entry_commitments()[0]["state"],
                         "COMMITTED_PENDING_SETTLEMENT")
        # And a corrected process still finishes it.
        self.assertEqual(RECOV.recover_pending()["settled"], 1)


class OnlyACrashMayLeaveAFillPendingTests(unittest.TestCase):
    """The invariant that makes the entry boundary meaningful.

    A path that RETURNS has told its caller whether the position opened. If
    it returns a refusal while leaving the commitment PENDING, recovery will
    later open a position the decision path already declined -- and the
    caller, having been told "not opened", may have re-decided the same
    signal in the meantime. Two positions, one intention.

    So: every return between the commit boundary and settlement resolves the
    commitment. Only a crash -- which returns nothing to anyone -- may leave
    a fill owed. This is checked structurally because it is an invariant
    about EVERY path, including ones no test happens to exercise.
    """

    def test_every_returning_path_after_the_commit_resolves_it(self):
        import re
        from pathlib import Path
        src = Path("lib/canonical_entry.py").read_text(encoding="utf-8")
        lines = src.splitlines()
        start = next(i for i, l in enumerate(lines)
                     if "entry_execution_id = _execution_id()" in l)
        end = next(i for i, l in enumerate(lines)
                   if "result = settle_position_entry(" in l)
        self.assertLess(start, end, "the commit boundary moved after "
                                    "settlement, which inverts the rule")

        unresolved = []
        for i in range(start, end):
            if not re.match(r"\s*return\b", lines[i]):
                continue
            window = "\n".join(lines[max(start, i - 12):i])
            if "EC.mark_abandoned(entry_execution_id" not in window:
                unresolved.append(f"L{i + 1}: {lines[i].strip()[:70]}")
        self.assertEqual(
            unresolved, [],
            "these paths tell the caller the entry did not open but leave "
            "the commitment PENDING, so recovery would open it anyway:\n  "
            + "\n  ".join(unresolved))
