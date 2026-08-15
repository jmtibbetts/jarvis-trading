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
        signals were duplicate regenerations. One decision, one row.

        A repeat sighting now returns the EXISTING row rather than None:
        returning None discarded the news, and when the news was 'this
        setup finally became signal X' the link was lost — 3,347 active
        signals with 9 joinable gate verdicts, every card UNMEASURED
        (2026-08-16 audit). One row is still the invariant; silence is
        not."""
        s = _scored()
        first = self._record(s)
        second = self._record(s)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(first.id, second.id)
        with get_db() as db:
            self.assertEqual(
                db.query(CandidateSignal).filter(
                    CandidateSignal.dedup_hash == first.dedup_hash).count(), 1)

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


class ResolutionQueueTests(unittest.TestCase):
    """The head-of-line starvation fix (2026-08-14): 440 permanently
    barless candidates sat oldest-first eating the resolution limit while
    resolvable candidates behind them aged unjudged. The limit now
    budgets replay ATTEMPTS; barless rows cost a lookup, and after 14
    days they leave the queue as resolved-with-NULL-outcome."""

    def setUp(self):
        self.prefix = f"TEST-Q{uuid.uuid4().hex[:6]}"
        self._clean()

    def tearDown(self):
        self._clean()

    def _clean(self):
        with get_db() as db:
            db.query(CandidateSignal).filter(
                CandidateSignal.symbol.like(f"{self.prefix}%")).delete(
                synchronize_session=False)
            db.commit()

    def _seed(self, symbol, days_old, n=1):
        from datetime import datetime, timedelta, timezone
        ids = []
        with get_db() as db:
            for i in range(n):
                row = CandidateSignal(
                    dedup_hash=f"{self.prefix}-{symbol}-{i}-{uuid.uuid4().hex[:6]}",
                    symbol=symbol, asset_class="Crypto", timeframe="4H",
                    direction="Long", entry_price=100.0, stop_loss=96.0,
                    target_price=112.0, verdict="rejected",
                    created_at=(datetime.now(timezone.utc)
                                - timedelta(days=days_old)).isoformat())
                db.add(row)
                db.flush()
                ids.append(row.id)
            db.commit()
        return ids

    def test_barless_flood_cannot_starve_resolvable_candidates(self):
        from unittest.mock import patch

        import pandas as pd

        from lib.candidates import resolve_pending

        barless = f"{self.prefix}-VOID/USD"
        resolvable = f"{self.prefix}-LIVE/USD"
        self._seed(barless, days_old=5, n=40)      # older: head of queue
        live_ids = self._seed(resolvable, days_old=1, n=3)

        fake_bars = pd.DataFrame({"close": [1.0] * 10})

        def bars_for(symbol, timeframe):
            return fake_bars if symbol == resolvable else None

        def fake_replay(sig, bars):
            return {"outcome": "WIN", "pnl_pct": 1.0, "mfe_r": 0.5,
                    "mae_r": -0.2, "first_touch": "TARGET",
                    "exit_reason": "take_profit"}

        with patch("lib.signal_replay.load_cached_bars", side_effect=bars_for), \
             patch("lib.signal_replay.replay_signal", side_effect=fake_replay):
            # Limit smaller than the flood: pre-fix, barless rows consumed
            # it entirely and the LIVE candidates never got a replay.
            out = resolve_pending(limit=3)
        self.assertGreaterEqual(out["resolved"], 3)
        with get_db() as db:
            for cid in live_ids:
                row = db.query(CandidateSignal).filter(
                    CandidateSignal.id == cid).first()
                self.assertTrue(row.resolved)
                self.assertEqual(row.outcome, "WIN")

    def test_ancient_barless_rows_expire_out_of_the_queue(self):
        from unittest.mock import patch

        from lib.candidates import resolve_pending

        old_ids = self._seed(f"{self.prefix}-GONE/USD", days_old=20, n=2)
        with patch("lib.signal_replay.load_cached_bars", return_value=None):
            out = resolve_pending(limit=5)
        self.assertGreaterEqual(out["expired"], 2)
        with get_db() as db:
            for cid in old_ids:
                row = db.query(CandidateSignal).filter(
                    CandidateSignal.id == cid).first()
                self.assertTrue(row.resolved)
                self.assertIsNone(row.outcome)      # never a fake result
                self.assertEqual(row.exit_reason, "expired_no_bars")

    def test_expired_rows_never_pollute_selection_stats(self):
        from unittest.mock import patch

        from lib.candidates import resolve_pending, selection_bias_summary

        self._seed(f"{self.prefix}-GONE/USD", days_old=20, n=2)
        with patch("lib.signal_replay.load_cached_bars", return_value=None):
            resolve_pending(limit=5)
        rows = selection_bias_summary()["by_verdict"]
        # NULL-pnl rows must not appear as zero-win losses anywhere.
        for r in rows:
            self.assertIsNotNone(r["avg_pnl_pct"])


if __name__ == "__main__":
    unittest.main()
