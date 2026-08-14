"""Rejected setups must leave a record the filter cannot erase.

Until this table existed, anything under MIN_PERSIST_SCORE vanished, so
"are the filters discarding winners?" was unanswerable — and with the
composite score measured inverted, the rejected pile is exactly where the
winners are likely to be. These tests pin the properties that make the
record trustworthy: every verdict recorded, duplicates collapsed, the
original judgment immutable, and bookkeeping that can never take signal
generation down with it.
"""
import unittest
import uuid

from app.database import CandidateSignal, get_db
from lib.candidates import dedup_hash, record_candidate

def _scored(**over):
    base = {
        "asset_symbol": f"TEST-{uuid.uuid4().hex[:8]}/USD",
        "asset_class": "Crypto",
        "timeframe": "4H",
        "direction": "Long",
        "strategy": "breakout",
        "entry_price": 100.0,
        "stop_loss": 96.0,
        "target_price": 112.0,
        "composite_score": 52.0,
        "score_breakdown": {
            "ta_confluence": 80.0, "conflict_ratio": 0.2, "volatility": 60.0,
            "regime": 70.0, "news": 50.0, "freshness": 90.0,
            "data_quality": 95.0, "liquidity": 60.0, "rr": 40.0,
            "calibrated_confidence": 55.0,
        },
    }
    base.update(over)
    return base


class RecordingTests(unittest.TestCase):
    def setUp(self):
        self.cleanup_ids = []

    def tearDown(self):
        with get_db() as db:
            for cid in self.cleanup_ids:
                db.query(CandidateSignal).filter(
                    CandidateSignal.id == cid).delete()
            db.commit()

    def _record(self, scored, verdict="rejected", **kw):
        with get_db() as db:
            row = record_candidate(db, scored, verdict, **kw)
            db.commit()
            if row is not None:
                self.cleanup_ids.append(row.id)
                db.refresh(row)
                db.expunge(row)
            return row

    def test_a_rejected_candidate_is_recorded_with_its_reason(self):
        row = self._record(_scored(), "rejected",
                           rejection_reason="below_min_persist")
        self.assertIsNotNone(row)
        self.assertEqual(row.verdict, "rejected")
        self.assertEqual(row.rejection_reason, "below_min_persist")
        self.assertFalse(row.resolved)

    def test_the_judgment_travels_with_the_candidate(self):
        """Score, breakdown and shadow variants at the moment of decision —
        recomputing them in hindsight would let a formula change rewrite
        history."""
        row = self._record(_scored(composite_score=61.5))
        self.assertEqual(row.composite_score, 61.5)
        self.assertIn("ta_confluence", row.score_breakdown)
        # Pinned to the exact current schema on purpose: adding a variant
        # (v1 -> v2 added MS) must arrive here as a conscious edit, never
        # as a silent redefinition of stored rows.
        self.assertIn("shadow_v2_2026-08-14", row.shadow_variants)

    def test_regenerated_setups_collapse_to_one_row(self):
        """Generators re-emit the same setup every cycle — 12,845 of 39,235
        signals were duplicate regenerations. One decision, one row."""
        s = _scored()
        first = self._record(s)
        second = self._record(s)
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_a_different_level_is_a_different_candidate(self):
        s = _scored()
        first = self._record(s)
        second = self._record({**s, "entry_price": 101.0})
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)

    def test_recording_never_raises_into_signal_generation(self):
        """A malformed candidate must be dropped silently — bookkeeping
        that can crash the batch is worse than no bookkeeping."""
        with get_db() as db:
            row = record_candidate(db, {"asset_symbol": None,
                                        "entry_price": "junk"}, "rejected")
            db.commit()
            if row is not None:
                self.cleanup_ids.append(row.id)
        # reaching here without an exception IS the assertion


class DedupHashTests(unittest.TestCase):
    def test_identity_is_symbol_tf_direction_and_levels(self):
        a = dedup_hash("BTC/USD", "4H", "Long", 100.0, 96.0, 112.0)
        self.assertEqual(a, dedup_hash("BTC/USD", "4H", "Long", 100.0, 96.0, 112.0))
        self.assertNotEqual(a, dedup_hash("BTC/USD", "4H", "Short", 100.0, 96.0, 112.0))
        self.assertNotEqual(a, dedup_hash("BTC/USD", "1H", "Long", 100.0, 96.0, 112.0))
        self.assertNotEqual(a, dedup_hash("BTC/USD", "4H", "Long", 100.0, 96.0, 113.0))


class ResolutionContractTests(unittest.TestCase):
    """resolve_pending reuses replay_signal, so the heavy lifting is tested
    in test_signal_replay. What matters here is the update discipline."""

    def test_resolution_only_touches_resolution_fields(self):
        """The original judgment is immutable; resolution fills in outcome
        fields and nothing else."""
        import inspect

        from lib import candidates
        src = inspect.getsource(candidates.resolve_pending)
        for field in ("composite_score", "score_breakdown", "verdict",
                      "rejection_reason", "shadow_variants"):
            self.assertNotIn(f"cand.{field} =", src,
                             f"resolution must never rewrite {field}")
        for field in ("resolved", "outcome", "pnl_pct", "mfe_r"):
            self.assertIn(f"cand.{field} =", src)


if __name__ == "__main__":
    unittest.main()
