"""A rescanned setup must carry its gate verdict to the LIVE signal.

Fails against the code as it stood on 2026-08-15.

Measured on the running desk: 138 active signals, 95 with a gate verdict,
43 reading UNMEASURED — and the 43 shared their `generated_at` with
measured ones from the same batch, so the card's old claim that they
"predate the gate experiment" was false. 85 and 70 unlinked candidate rows
existed for the very symbols they named.

The mechanism: the scanner supersedes an unchanged setup's previous signal
and writes a NEW row with a new id every cycle. `record_candidate`
attached the link only `if signal_id and not existing.signal_id`, so the
candidate stayed bolted to the superseded signal and the new ACTIVE one
joined to nothing. Every rescan of a still-valid setup produced one more
orphan.
"""
import unittest
import uuid

from app.database import CandidateSignal, TradingSignal, get_db, now_iso
from lib.candidates import record_candidate


def _scored(symbol):
    return {
        "asset_symbol": symbol, "asset_class": "Crypto", "timeframe": "1H",
        "direction": "Long", "entry_price": 100.0, "stop_loss": 95.0,
        "target_price": 115.0, "composite_score": 61.0,
        "score_breakdown": {}, "strategy": "breakout",
    }


class CandidateRelinkTests(unittest.TestCase):
    def setUp(self):
        self.symbol = f"TST{uuid.uuid4().hex[:6].upper()}/USD"
        self.made = []

    def tearDown(self):
        with get_db() as db:
            db.query(CandidateSignal).filter(
                CandidateSignal.symbol == self.symbol).delete()
            for sid in self.made:
                db.query(TradingSignal).filter(TradingSignal.id == sid).delete()

    def _signal(self, status):
        sid = str(uuid.uuid4())
        self.made.append(sid)
        with get_db() as db:
            db.add(TradingSignal(
                id=sid, asset_symbol=self.symbol, asset_name=self.symbol,
                asset_class="Crypto", direction="Long", confidence=65,
                timeframe="1H", entry_price=100.0, stop_loss=95.0,
                target_price=115.0, status=status, generated_at=now_iso(),
            ))
        return sid

    def _linked_signal_id(self):
        with get_db() as db:
            row = db.query(CandidateSignal).filter(
                CandidateSignal.symbol == self.symbol).first()
            return row.signal_id if row else None

    def test_first_sighting_links(self):
        first = self._signal("Active")
        with get_db() as db:
            record_candidate(db, _scored(self.symbol), "persisted",
                             signal_id=first, source="scanner")
        self.assertEqual(self._linked_signal_id(), first)

    def test_rescan_repoints_to_the_new_signal_when_the_old_is_superseded(self):
        """THE regression. One candidate row, one live signal — always."""
        first = self._signal("Active")
        with get_db() as db:
            record_candidate(db, _scored(self.symbol), "persisted",
                             signal_id=first, source="scanner")

        # What the scanner does on the next cycle for an unchanged setup.
        with get_db() as db:
            db.query(TradingSignal).filter(
                TradingSignal.id == first).first().status = "Superseded"
        second = self._signal("Active")
        with get_db() as db:
            record_candidate(db, _scored(self.symbol), "persisted",
                             signal_id=second, source="scanner")

        self.assertEqual(self._linked_signal_id(), second,
                         "the live signal must carry the verdict, not the "
                         "superseded one")

    def test_a_still_live_link_is_not_stolen(self):
        """If the linked signal is STILL live, re-pointing would just move
        the orphan to the other signal. Leave it alone."""
        first = self._signal("Active")
        with get_db() as db:
            record_candidate(db, _scored(self.symbol), "persisted",
                             signal_id=first, source="scanner")
        second = self._signal("Active")
        with get_db() as db:
            record_candidate(db, _scored(self.symbol), "persisted",
                             signal_id=second, source="scanner")
        self.assertEqual(self._linked_signal_id(), first)

    def test_a_link_to_a_deleted_signal_is_replaced(self):
        first = self._signal("Active")
        with get_db() as db:
            record_candidate(db, _scored(self.symbol), "persisted",
                             signal_id=first, source="scanner")
        with get_db() as db:
            db.query(TradingSignal).filter(TradingSignal.id == first).delete()
        second = self._signal("Active")
        with get_db() as db:
            record_candidate(db, _scored(self.symbol), "persisted",
                             signal_id=second, source="scanner")
        self.assertEqual(self._linked_signal_id(), second)

    def test_the_judgment_never_moves_with_the_link(self):
        """Re-pointing carries the verdict to the new signal; it must not
        let the SECOND look rewrite what the first one concluded."""
        first = self._signal("Active")
        with get_db() as db:
            record_candidate(db, _scored(self.symbol), "rejected",
                             rejection_reason="score below floor",
                             signal_id=first, source="scanner")
        with get_db() as db:
            row = db.query(CandidateSignal).filter(
                CandidateSignal.symbol == self.symbol).first()
            original_score = row.composite_score
            original_reason = row.rejection_reason
            db.query(TradingSignal).filter(
                TradingSignal.id == first).first().status = "Superseded"

        second = self._signal("Active")
        changed = {**_scored(self.symbol), "composite_score": 99.0}
        with get_db() as db:
            record_candidate(db, changed, "persisted",
                             signal_id=second, source="scanner")

        with get_db() as db:
            row = db.query(CandidateSignal).filter(
                CandidateSignal.symbol == self.symbol).first()
            self.assertEqual(row.signal_id, second)
            self.assertEqual(row.composite_score, original_score,
                             "hindsight must not edit the recorded score")
            self.assertEqual(row.rejection_reason, original_reason)


if __name__ == "__main__":
    unittest.main()
