"""P4 — clock snapshots and independent-horizon labels.

The properties pinned here: snapshots dedup per bar (a stalled feed must
not mint identical vectors under fresh timestamps), labels resolve each
horizon on its own evidence, thin coverage abstains after grace rather
than fabricating a return, and a degraded vector is flagged, not dropped.
"""
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import numpy as np
import pandas as pd

from app.database import FeatureLabel, FeatureSnapshot, get_db, init_db
from lib.feature_snapshots import (
    HORIZONS_MIN,
    resolve_due_labels,
    snapshot_summary,
    take_clock_snapshot,
)


def _bars(n=300, start=None, freq_min=15, price=100.0):
    start = start or (datetime.now(timezone.utc) - timedelta(minutes=freq_min * n))
    idx = pd.date_range(start=start, periods=n, freq=f"{freq_min}min", tz="UTC")
    drift = np.linspace(0, 5.0, n)
    close = price + drift
    return pd.DataFrame({
        "open": close - 0.1, "high": close + 1.0,
        "low": close - 1.0, "close": close,
        "volume": np.full(n, 1000.0),
    }, index=idx)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        init_db()
        self._clean()

    def tearDown(self):
        self._clean()

    def _clean(self):
        with get_db() as db:
            for snap_id, in db.query(FeatureSnapshot.id).filter(
                    FeatureSnapshot.symbol.like("TEST-FS%")).all():
                db.query(FeatureLabel).filter(
                    FeatureLabel.snapshot_id == snap_id).delete(
                    synchronize_session=False)
            db.query(FeatureSnapshot).filter(
                FeatureSnapshot.symbol.like("TEST-FS%")).delete(
                synchronize_session=False)
            db.commit()

    def _take(self, bars):
        with patch("lib.signal_replay.load_cached_bars", return_value=bars):
            return take_clock_snapshot("TEST-FS/USD")

    def test_snapshot_stores_vector_under_schema_hash_with_labels(self):
        r = self._take(_bars())
        self.assertIn("snapshot_id", r)
        self.assertEqual(r["labels_scheduled"], len(HORIZONS_MIN))
        with get_db() as db:
            snap = db.query(FeatureSnapshot).get(r["snapshot_id"])
            self.assertTrue(snap.schema_hash)
            values = json.loads(snap.values_json)
            mask = json.loads(snap.mask_json)
            self.assertEqual(len(values), len(mask))
            self.assertIsNotNone(snap.anchor_price)
            labels = db.query(FeatureLabel).filter(
                FeatureLabel.snapshot_id == snap.id).all()
            self.assertEqual(sorted(l.horizon_min for l in labels),
                             sorted(HORIZONS_MIN))
            self.assertTrue(all(l.status == "pending" for l in labels))

    def test_same_bar_is_never_snapshotted_twice(self):
        bars = _bars()
        first = self._take(bars)
        second = self._take(bars)
        self.assertIn("snapshot_id", first)
        self.assertEqual(second.get("skipped"), "bar_already_snapshotted")

    def test_no_bars_skips_never_fabricates(self):
        r = self._take(None)
        self.assertEqual(r.get("skipped"), "no_bars")


class LabelResolutionTests(SnapshotTests):
    def _make_due(self, snapshot_id, hours_ago=30):
        """Backdate the snapshot + labels so every horizon is due."""
        past = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
        with get_db() as db:
            snap = db.query(FeatureSnapshot).get(snapshot_id)
            snap.created_at = past.isoformat()
            for l in db.query(FeatureLabel).filter(
                    FeatureLabel.snapshot_id == snapshot_id).all():
                l.due_at = (past + timedelta(minutes=l.horizon_min)).isoformat()
            db.commit()

    def test_each_horizon_resolves_on_its_own_evidence(self):
        # Anchor 30h ago with bars covering the FULL day after it: every
        # horizon (1h/4h/1d) has complete forward coverage and resolves.
        anchor_time = datetime.now(timezone.utc) - timedelta(hours=30)
        bars = _bars(n=200, start=anchor_time - timedelta(minutes=15 * 60))
        r = self._take(bars)
        with get_db() as db:
            snap = db.query(FeatureSnapshot).get(r["snapshot_id"])
            # Re-anchor the snapshot on a bar 30h back so forward bars exist.
            anchor_bar = bars.index[60]
            snap.bar_time = anchor_bar.isoformat()
            snap.anchor_price = float(bars["close"].iloc[60])
            db.commit()
        self._make_due(r["snapshot_id"])
        with patch("lib.signal_replay.load_cached_bars", return_value=bars):
            out = resolve_due_labels()
        self.assertGreaterEqual(out["resolved"], 3)
        with get_db() as db:
            labels = {l.horizon_min: l for l in db.query(FeatureLabel).filter(
                FeatureLabel.snapshot_id == r["snapshot_id"]).all()}
            for h in HORIZONS_MIN:
                self.assertEqual(labels[h].status, "resolved", f"horizon {h}")
                self.assertIsNotNone(labels[h].forward_ret_pct)
            # Monotone drift upward: the 1d forward return must exceed 1h.
            self.assertGreater(labels[1440].forward_ret_pct,
                               labels[60].forward_ret_pct)

    def test_thin_coverage_abstains_after_grace_instead_of_fabricating(self):
        # Anchor on the LAST bar: zero forward bars exist. Labels far past
        # due + grace must abstain with the coverage reason, not resolve.
        bars = _bars(n=200)
        r = self._take(bars)
        self._make_due(r["snapshot_id"], hours_ago=80)
        with patch("lib.signal_replay.load_cached_bars", return_value=bars):
            out = resolve_due_labels()
        self.assertGreaterEqual(out["abstained"], 3)
        with get_db() as db:
            for l in db.query(FeatureLabel).filter(
                    FeatureLabel.snapshot_id == r["snapshot_id"]).all():
                self.assertEqual(l.status, "abstained")
                self.assertIn("coverage", l.abstain_reason)
                self.assertIsNone(l.forward_ret_pct)

    def test_pending_inside_grace_stays_pending(self):
        # Due, but the horizon's grace window hasn't elapsed — the honest
        # state is "still waiting for bars", not a premature abstention.
        bars = _bars(n=200)
        r = self._take(bars)
        past = datetime.now(timezone.utc) - timedelta(minutes=30)
        with get_db() as db:
            for l in db.query(FeatureLabel).filter(
                    FeatureLabel.snapshot_id == r["snapshot_id"],
                    FeatureLabel.horizon_min == 60).all():
                l.due_at = past.isoformat()
            db.commit()
        with patch("lib.signal_replay.load_cached_bars", return_value=bars):
            resolve_due_labels()
        with get_db() as db:
            l = db.query(FeatureLabel).filter(
                FeatureLabel.snapshot_id == r["snapshot_id"],
                FeatureLabel.horizon_min == 60).first()
            self.assertEqual(l.status, "pending")

    def test_summary_reports_corpus_and_label_mix(self):
        r = self._take(_bars())
        s = snapshot_summary()
        self.assertTrue(any("clock" in k for k in s["snapshots"]))
        self.assertTrue(any(row["status"] == "pending" for row in s["labels"]))


if __name__ == "__main__":
    unittest.main()
