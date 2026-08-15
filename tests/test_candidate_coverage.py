"""Gate-verdict coverage — the 2026-08-16 audit found 3,347 active signals
with 9 joinable verdicts, so every card read UNMEASURED.

Two causes, both pinned here: the scanner never recorded candidates at
all, and a repeat sighting of a known setup returned early instead of
attaching the signal_id it now had. The fix must complete the row WITHOUT
rewriting any judgment — the gate experiment depends on those fields
being immutable.
"""
import re
import unittest
from pathlib import Path

from app.database import CandidateSignal, get_db, init_db
from lib.candidates import record_candidate

SCANNER_SRC = Path("jobs/scan_opportunities.py").read_text(encoding="utf-8")

SETUP = {
    "asset_symbol": "TEST-COV/USD", "timeframe": "4H", "direction": "Long",
    "entry_price": 100.0, "stop_loss": 95.0, "target_price": 115.0,
    "composite_score": 61.0, "asset_class": "Crypto",
}


class BothWritersRecordTests(unittest.TestCase):
    def test_scanner_records_candidates(self):
        """The scanner produces more than half the desk's signals; if it
        never calls record_candidate, those setups can never carry a gate
        verdict and the experiment silently covers half the desk."""
        self.assertIn("record_candidate", SCANNER_SRC,
                      "scan_opportunities.py must record candidates")
        self.assertRegex(
            SCANNER_SRC, r"source\s*=\s*[\"']scanner[\"']",
            "scanner candidates must be tagged so the running gate "
            "experiment can still be evaluated on its original population")


class UpsertTests(unittest.TestCase):
    def setUp(self):
        init_db()
        self._clean()

    def tearDown(self):
        self._clean()

    def _clean(self):
        from app.database import TradingSignal
        with get_db() as db:
            db.query(CandidateSignal).filter(
                CandidateSignal.symbol == "TEST-COV/USD").delete(
                synchronize_session=False)
            db.query(TradingSignal).filter(
                TradingSignal.asset_symbol == "TEST-COV/USD").delete(
                synchronize_session=False)
            db.commit()

    def _make_signal(self, status: str) -> str:
        """A REAL trading_signals row. The link rules depend on whether the
        signal a candidate points at still exists and is still live, so a
        fabricated id cannot exercise them."""
        import uuid

        from app.database import TradingSignal, now_iso
        sid = str(uuid.uuid4())
        with get_db() as db:
            db.add(TradingSignal(
                id=sid, asset_symbol="TEST-COV/USD", asset_name="TEST-COV/USD",
                asset_class="Crypto", direction="Long", confidence=65,
                timeframe="4H", entry_price=100.0, stop_loss=95.0,
                target_price=115.0, status=status, generated_at=now_iso(),
            ))
            db.commit()
        return sid

    def _rows(self, db):
        return db.query(CandidateSignal).filter(
            CandidateSignal.symbol == "TEST-COV/USD").all()

    def test_repeat_sighting_attaches_signal_id_without_duplicating(self):
        with get_db() as db:
            record_candidate(db, SETUP, "rejected",
                             rejection_reason="below_focus_bar")
            db.commit()
            record_candidate(db, SETUP, "persisted", signal_id="SIG-COV-1")
            db.commit()
            rows = self._rows(db)
            self.assertEqual(len(rows), 1, "must not create a second row")
            self.assertEqual(rows[0].signal_id, "SIG-COV-1",
                             "the link the signal card needs")
            self.assertEqual(rows[0].verdict, "persisted")
            # The first look's conclusion stays on the record.
            self.assertEqual(rows[0].rejection_reason, "below_focus_bar")

    def test_the_original_judgment_is_never_rewritten(self):
        with get_db() as db:
            first = record_candidate(db, SETUP, "rejected")
            db.commit()
            born_score = first.composite_score
            born_gate = first.gate_v8_decision
            born_created = first.created_at
            # A later sighting with a DIFFERENT score must not edit history.
            record_candidate(db, {**SETUP, "composite_score": 99.0},
                             "persisted", signal_id="SIG-COV-2")
            db.commit()
            row = self._rows(db)[0]
            self.assertEqual(row.composite_score, born_score)
            self.assertEqual(row.gate_v8_decision, born_gate)
            self.assertEqual(row.created_at, born_created)

    def test_existing_signal_id_is_not_stolen_by_a_LIVE_signal(self):
        """The original invariant, now tested with a signal that exists.

        This used the ids "SIG-FIRST"/"SIG-SECOND", which were never
        inserted into `trading_signals`. That reads as a link to a DELETED
        signal, not a live one — and `/api/signals/clear-expired` really
        does hard-delete rows, so the two cases are genuinely different and
        want opposite answers. Held apart here rather than conflated:
        a live link stands (below), a dangling one is replaced (next test).
        """
        live_id = self._make_signal("Active")
        with get_db() as db:
            record_candidate(db, SETUP, "persisted", signal_id=live_id)
            db.commit()
            record_candidate(db, SETUP, "persisted", signal_id="SIG-SECOND")
            db.commit()
            self.assertEqual(self._rows(db)[0].signal_id, live_id,
                             "the live link stands; a setup is one row")

    def test_a_link_to_a_deleted_signal_is_replaced(self):
        """Otherwise clearing expired signals would strand every candidate
        they were linked to, and the next signal for that same setup would
        read UNMEASURED forever — with its verdict sitting right there."""
        gone_id = self._make_signal("Expired")
        with get_db() as db:
            record_candidate(db, SETUP, "persisted", signal_id=gone_id)
            db.commit()
        with get_db() as db:
            from app.database import TradingSignal
            db.query(TradingSignal).filter(TradingSignal.id == gone_id).delete()
        live_id = self._make_signal("Active")
        with get_db() as db:
            record_candidate(db, SETUP, "persisted", signal_id=live_id)
            db.commit()
            self.assertEqual(self._rows(db)[0].signal_id, live_id)

    def test_new_rows_carry_their_source(self):
        with get_db() as db:
            row = record_candidate(db, SETUP, "persisted",
                                   signal_id="SIG-COV-3", source="scanner")
            db.commit()
            self.assertEqual(row.source, "scanner")


if __name__ == "__main__":
    unittest.main()
