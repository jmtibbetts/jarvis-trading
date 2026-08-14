"""The ablation harness — built before the data, so pinned on synthetic
candidates: bucketing math, direction splits, thin flags, and the
empty-corpus report all behave before a single real row matures.
"""
import json
import unittest
from datetime import datetime, timezone

from app.database import CandidateSignal, get_db, init_db
from lib.context_ablation import MIN_N, ablation_summary


def _cand(i, direction, pctile, pnl, resolved=True, ctx_extra=None):
    ctx = {"schema": "ctx_v1_2026-08-15", "cot_spec_pctile_3y": pctile}
    ctx.update(ctx_extra or {})
    return CandidateSignal(
        dedup_hash=f"TEST-ABL-{i}",
        symbol="TEST-ABL/USD", asset_class="Crypto", timeframe="4H",
        direction=direction, entry_price=100.0, stop_loss=95.0,
        target_price=110.0, verdict="persisted",
        market_context=json.dumps(ctx),
        resolved=resolved, outcome="TP" if pnl > 0 else "SL",
        pnl_pct=pnl, mfe_r=1.0 if pnl > 0 else 0.2,
        created_at=datetime.now(timezone.utc).isoformat(),
    )


class AblationTests(unittest.TestCase):
    def setUp(self):
        init_db()
        self._clean()

    def tearDown(self):
        self._clean()

    def _clean(self):
        with get_db() as db:
            db.query(CandidateSignal).filter(
                CandidateSignal.symbol == "TEST-ABL/USD").delete(
                synchronize_session=False)
            db.commit()

    def _get(self, summary, feature):
        return next(f for f in summary["features"]
                    if f["feature"] == feature)

    def test_empty_corpus_reports_itself_not_zeros(self):
        s = ablation_summary()
        self.assertIn("resolved_with_context", s["coverage"])
        for f in s["features"]:
            for b in f["buckets"]:
                self.assertGreater(b["n"], 0)   # no fabricated 0-rows

    def test_buckets_split_by_direction_and_flag_thin(self):
        with get_db() as db:
            # 30 crowded longs that lost, 30 crowded shorts that won,
            # 3 washed-out longs (thin).
            for i in range(30):
                db.add(_cand(f"cl{i}", "Long", 95.0, -1.0))
                db.add(_cand(f"cs{i}", "Short", 95.0, 2.0))
            for i in range(3):
                db.add(_cand(f"wl{i}", "Long", 5.0, 1.0))
            db.commit()
        f = self._get(ablation_summary(), "cot_spec_pctile_3y")
        rows = {(b["bucket"], b["direction"]): b for b in f["buckets"]
                if "TEST" not in str(b)}
        crowded_long = rows[("crowded(>80)", "Long")]
        crowded_short = rows[("crowded(>80)", "Short")]
        washed_long = rows[("washed_out(<20)", "Long")]
        self.assertGreaterEqual(crowded_long["n"], 30)
        self.assertEqual(crowded_long["win_rate"], 0.0)
        self.assertEqual(crowded_short["win_rate"], 100.0)
        self.assertFalse(crowded_long["thin"])
        self.assertTrue(washed_long["thin"])
        self.assertLess(washed_long["n"], MIN_N)
        # Pooling would have averaged the crowding effect away — the
        # direction split is what lets it show at all.
        self.assertNotEqual(crowded_long["win_rate"],
                            crowded_short["win_rate"])

    def test_unresolved_and_contextless_rows_stay_out(self):
        with get_db() as db:
            db.add(_cand("u1", "Long", 95.0, 1.0, resolved=False))
            row = _cand("nc1", "Long", 95.0, 1.0)
            row.market_context = None
            db.add(row)
            db.commit()
        f = self._get(ablation_summary(), "cot_spec_pctile_3y")
        n = sum(b["n"] for b in f["buckets"])
        s = ablation_summary()
        self.assertEqual(
            sum(b["n"] for b in self._get(s, "cot_spec_pctile_3y")["buckets"]),
            n)   # unresolved/contextless contributed nothing

    def test_missing_feature_key_is_skipped_not_bucketed(self):
        with get_db() as db:
            db.add(_cand("f1", "Long", 95.0, 1.0,
                         ctx_extra={"funding_rate": 0.0002}))
            db.commit()
        f = self._get(ablation_summary(), "funding_rate")
        self.assertTrue(any(b["bucket"] == "rich(>1bp)"
                            for b in f["buckets"]))
        # curve_structure was never in any context -> no phantom rows
        curve = self._get(ablation_summary(), "curve_structure")
        self.assertEqual(
            [b for b in curve["buckets"] if b["bucket"] is None], [])


if __name__ == "__main__":
    unittest.main()
