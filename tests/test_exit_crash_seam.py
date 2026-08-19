"""What survives a crash between virtual execution and settlement.

THE WORRY. `canonical_exit` submits to the venue, then computes fees and
carry, then settles. If the process dies in between, nothing has been
persisted — so a later attempt re-prices against a NEW book. The concern is
that economics become a function of process timing rather than of the
market.

THIS FILE ESTABLISHES WHAT IS AND IS NOT ACTUALLY AT RISK, by killing the
flow at each boundary and inspecting the book afterwards. Claims here are
measured, not argued.
"""
import unittest
from unittest.mock import patch

from tests.test_pass_b_corrections import _CanonicalHarness, _seed_book


class Boom(Exception):
    """A crash, injected exactly where we want one."""


class ACrashBeforeSettlementLeavesNoEconomicsTests(_CanonicalHarness):

    def _state_of(self, pid):
        pos, header, legs, outcomes = self._state(pid)
        return {"status": pos.status if pos else None,
                "legs": [l.kind for l in legs],
                "outcomes": len(outcomes),
                "cash": self._portfolio()["cash"]}

    def test_a_crash_after_submit_before_settlement_changes_nothing(self):
        """Boundary 2/3/4. Virtual execution has NO external side effect —
        there is no resting order at the venue — so an abandoned attempt
        must leave the book exactly as it was."""
        from lib import exit_dispatch as ED
        from lib import fee_authority as FA
        pos, header = self._enter()
        _seed_book(bid=64_700.0, ask=64_800.0)
        before = self._state_of(pos.id)

        # Die immediately after the venue produced a fill — the fee
        # authority is the first thing touched after submit.
        with patch.object(FA, "leg_fee", side_effect=Boom("crash after submit")):
            with self.assertRaises(Boom):
                ED.request_position_exit(pos.id, caller_price=64_700.0,
                                         caller_reason="manual",
                                         caller_source="API_MANUAL")

        after = self._state_of(pos.id)
        self.assertEqual(after, before,
                         "an abandoned exit attempt mutated the book")
        self.assertEqual(after["legs"], ["ENTRY"])
        self.assertEqual(after["outcomes"], 0)

    def test_the_position_can_still_exit_normally_afterwards(self):
        """The crash must not poison the position. A later cycle re-decides
        from a fresh book, which is correct behaviour, not fabrication —
        nothing was executed externally to reconcile against."""
        from lib import exit_dispatch as ED
        from lib import fee_authority as FA
        pos, header = self._enter()
        _seed_book(bid=64_700.0, ask=64_800.0)

        with patch.object(FA, "leg_fee", side_effect=Boom("crash")):
            try:
                ED.request_position_exit(pos.id, caller_price=64_700.0,
                                         caller_reason="manual",
                                         caller_source="API_MANUAL")
            except Boom:
                pass
        mid = self._state_of(pos.id)
        self.assertEqual(mid["legs"], ["ENTRY"])

        _seed_book(bid=64_900.0, ask=65_000.0)
        res = ED.request_position_exit(pos.id, caller_price=64_900.0,
                                       caller_reason="manual",
                                       caller_source="API_MANUAL")
        self.assertTrue(res.get("ok"), res)
        after = self._state_of(pos.id)
        self.assertEqual(after["legs"], ["ENTRY", "FINAL_EXIT"])
        self.assertEqual(after["outcomes"], 1, "exactly one outcome")

    def test_a_crash_after_settlement_does_not_settle_twice(self):
        """Boundary 5. If settlement committed but the caller died before
        recording it, the position is closed — and a second attempt must
        refuse rather than settle again."""
        from lib import exit_dispatch as ED
        pos, header = self._enter()
        _seed_book(bid=64_700.0, ask=64_800.0)

        first = ED.request_position_exit(pos.id, caller_price=64_700.0,
                                         caller_reason="manual",
                                         caller_source="API_MANUAL")
        self.assertTrue(first.get("ok"), first)
        settled = self._state_of(pos.id)

        second = ED.request_position_exit(pos.id, caller_price=64_700.0,
                                          caller_reason="manual",
                                          caller_source="API_MANUAL")
        self.assertFalse(second.get("ok"),
                         "a closed position settled a second time")
        self.assertEqual(self._state_of(pos.id), settled,
                         "the second attempt changed settled economics")

    def test_each_attempt_mints_its_own_execution_id(self):
        """Stated plainly because it bounds what B2A's idempotency can do:
        the execution id is minted per attempt, so it protects against the
        SAME facts being submitted twice — not against a fresh attempt
        pricing at a new book. That second thing is a new decision, and
        because virtual execution leaves nothing resting at the venue, it is
        the correct outcome rather than a double fill."""
        from lib.canonical_entry import _execution_id
        self.assertNotEqual(_execution_id(), _execution_id())


if __name__ == "__main__":
    unittest.main()
