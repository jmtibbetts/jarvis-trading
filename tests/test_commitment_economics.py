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
        super().setUp()
        self._clear_commitments()
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
