"""Clock-driven feature snapshots and the independent-horizon label
scheduler — P4 of the data platform plan.

Every dataset this system has trained on so far was born from moments the
system itself chose to look at — signals, scans, candidates. That corpus
answers "what happens after we get interested?", which is not the same
question as "what happens?". Clock-driven snapshots fix the frame: the
same feature vector, taken on a fixed cadence for the Tier-1 symbols,
interesting or not. Selection bias is removed at CAPTURE time, the only
time it can be.

Labels are scheduled at snapshot birth, one row per horizon, and resolve
INDEPENDENTLY (§57): the 1-hour label resolves an hour later from the
bars that exist then; the 1-day label waits for its own due time. A
horizon that cannot see enough forward bars abstains with a reason (§43)
— partial coverage silently averaged into a "forward return" would be a
label lying about its own horizon.

Nothing here scores, gates, or trades. This is the corpus the outcome
model (platform doc P6) will be trained on, accumulating from today.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# Tier-1 only, matching every other raw-capture gate. 15m bars resolve the
# 1h horizon with 4 bars and the 1d horizon with 96.
CLOCK_SYMBOLS = ("BTC/USD", "ETH/USD")
CLOCK_TIMEFRAME = "15m"
HORIZONS_MIN = (60, 240, 1440)

# §43: above this missing fraction the vector is flagged degraded. Flagged,
# not discarded — the flag travels with the row and training decides.
DEGRADED_MISSING_FRACTION = 0.30


def _now():
    return datetime.now(timezone.utc)


def take_clock_snapshot(symbol: str, timeframe: str = CLOCK_TIMEFRAME) -> dict:
    """One snapshot for one symbol, deduped per bar.

    The dedup matters: the job fires every 15 minutes, but if the feed is
    stalled the same bar would be snapshotted repeatedly — identical
    vectors wearing different timestamps, which a chronological split
    would then leak across.
    """
    from app.database import FeatureSnapshot, get_db
    from lib.predictive.features import build
    from lib.signal_replay import load_cached_bars
    from lib.ta_engine import compute_timeframe

    bars = load_cached_bars(symbol, timeframe)
    if bars is None or len(bars) < 30:
        return {"symbol": symbol, "skipped": "no_bars"}

    ta = compute_timeframe(bars, timeframe)
    if ta.get("error"):
        return {"symbol": symbol, "skipped": ta["error"]}

    bar_time = ta.get("bar_time")
    with get_db() as db:
        if bar_time and db.query(FeatureSnapshot.id).filter(
                FeatureSnapshot.symbol == symbol,
                FeatureSnapshot.timeframe == timeframe,
                FeatureSnapshot.trigger == "clock",
                FeatureSnapshot.bar_time == bar_time).first():
            return {"symbol": symbol, "skipped": "bar_already_snapshotted"}

        vec = build(ta=ta, signal={"asset_symbol": symbol,
                                   "timeframe": timeframe},
                    max_source_age_s=float(ta.get("bar_age_seconds") or 0))
        anchor = ta.get("price") or {}
        row = FeatureSnapshot(
            symbol=symbol, timeframe=timeframe, trigger="clock",
            schema_version=vec.schema_version, schema_hash=vec.schema_hash,
            values_json=json.dumps(vec.values), mask_json=json.dumps(vec.mask),
            missing_fraction=round(vec.missing_fraction, 4),
            quality=("degraded" if vec.missing_fraction >
                     DEGRADED_MISSING_FRACTION else "ok"),
            bar_time=bar_time,
            anchor_price=anchor.get("last"),
        )
        db.add(row)
        db.flush()
        n_labels = _schedule_labels(db, row)
        db.commit()
        return {"symbol": symbol, "snapshot_id": row.id,
                "quality": row.quality, "labels_scheduled": n_labels}


def _schedule_labels(db, snapshot) -> int:
    """One pending label per horizon, due on its own clock."""
    from app.database import FeatureLabel

    created = datetime.fromisoformat(str(snapshot.created_at))
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    n = 0
    for h in HORIZONS_MIN:
        db.add(FeatureLabel(
            snapshot_id=snapshot.id, horizon_min=h,
            due_at=(created + timedelta(minutes=h)).isoformat()))
        n += 1
    return n


def run_clock_snapshots() -> dict:
    """The scheduled job body: every Tier-1 symbol, one pass."""
    out = {"taken": 0, "skipped": 0, "results": []}
    for sym in CLOCK_SYMBOLS:
        try:
            r = take_clock_snapshot(sym)
        except Exception as e:
            logger.warning(f"[FeatureSnapshots] {sym} failed: {e}")
            r = {"symbol": sym, "skipped": f"error: {e}"}
        out["results"].append(r)
        out["taken" if "snapshot_id" in r else "skipped"] += 1
    logger.info(f"[FeatureSnapshots] clock pass: {out['taken']} taken, "
                f"{out['skipped']} skipped")
    return out


# ── Label resolution (§57: each horizon on its own evidence) ─────────────────

# A horizon must be covered to at least this fraction by actual forward
# bars, else the label abstains. 1.0 would be brittle against a single
# missing bar; anything much lower starts averaging half-horizons.
MIN_COVERAGE = 0.9


def resolve_due_labels(limit: int = 500) -> dict:
    """Resolve every pending label whose due time has passed.

    forward_ret_pct is close-at-horizon vs anchor close; max_up/max_down
    are the extremes within the horizon window — the raw material for
    outcome-model targets at each horizon independently.
    """
    import pandas as pd

    from app.database import FeatureLabel, FeatureSnapshot, get_db
    from lib.signal_replay import load_cached_bars

    now = _now()
    checked = resolved = abstained = 0
    bars_cache: dict = {}

    with get_db() as db:
        due = (db.query(FeatureLabel, FeatureSnapshot)
                 .join(FeatureSnapshot,
                       FeatureLabel.snapshot_id == FeatureSnapshot.id)
                 .filter(FeatureLabel.status == "pending")
                 .filter(FeatureLabel.due_at < now.isoformat())
                 .order_by(FeatureLabel.due_at.asc())
                 .limit(limit).all())
        for label, snap in due:
            checked += 1

            def _abstain(reason: str):
                label.status = "abstained"
                label.abstain_reason = reason
                label.resolved_at = now.isoformat()

            if not snap.anchor_price or not snap.bar_time:
                _abstain("no_anchor")
                abstained += 1
                continue

            key = (snap.symbol, snap.timeframe)
            if key not in bars_cache:
                bars_cache[key] = load_cached_bars(snap.symbol, snap.timeframe)
            bars = bars_cache[key]
            if bars is None or len(bars) == 0:
                _abstain("no_bars")
                abstained += 1
                continue

            try:
                anchor_ts = pd.Timestamp(snap.bar_time)
                anchor_ts = (anchor_ts.tz_localize("UTC")
                             if anchor_ts.tzinfo is None
                             else anchor_ts.tz_convert("UTC"))
                idx = bars.index
                idx = (idx.tz_localize("UTC") if idx.tzinfo is None
                       else idx.tz_convert("UTC"))
                horizon_end = anchor_ts + pd.Timedelta(minutes=label.horizon_min)
                window = bars.loc[(idx > anchor_ts) & (idx <= horizon_end)]
            except Exception as e:
                _abstain(f"bar_index_error: {e}")
                abstained += 1
                continue

            tf_min = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1H": 60,
                      "4H": 240, "1D": 1440}.get(snap.timeframe, 15)
            expected = max(1, label.horizon_min // tf_min)
            if len(window) < expected * MIN_COVERAGE:
                # Not abstained forever — the bars may still arrive. Only
                # a due label with a FULL horizon behind it and still-thin
                # coverage is a genuine gap; give the feed one extra
                # horizon of grace before giving up.
                grace_end = datetime.fromisoformat(str(label.due_at))
                if grace_end.tzinfo is None:
                    grace_end = grace_end.replace(tzinfo=timezone.utc)
                if now > grace_end + timedelta(minutes=label.horizon_min):
                    _abstain(f"coverage {len(window)}/{expected}")
                    abstained += 1
                continue

            anchor = float(snap.anchor_price)
            closes = window["close"].astype(float)
            highs = window["high"].astype(float)
            lows = window["low"].astype(float)
            label.forward_ret_pct = round(
                (float(closes.iloc[-1]) - anchor) / anchor * 100.0, 4)
            label.max_up_pct = round(
                (float(highs.max()) - anchor) / anchor * 100.0, 4)
            label.max_down_pct = round(
                (float(lows.min()) - anchor) / anchor * 100.0, 4)
            label.status = "resolved"
            label.resolved_at = now.isoformat()
            resolved += 1
        db.commit()

    out = {"checked": checked, "resolved": resolved, "abstained": abstained}
    if checked:
        logger.info(f"[FeatureLabels] resolution pass: {out}")
    return out


def snapshot_summary() -> dict:
    """Ops view: corpus size, quality mix, label states per horizon."""
    from sqlalchemy import text

    from app.database import engine

    out: dict = {"snapshots": {}, "labels": []}
    with engine.connect() as c:
        for trigger, quality, n in c.execute(text("""
            SELECT trigger, quality, COUNT(*) FROM feature_snapshots
            GROUP BY trigger, quality""")):
            out["snapshots"][f"{trigger}/{quality}"] = n
        for horizon, status, n, avg_ret in c.execute(text("""
            SELECT horizon_min, status, COUNT(*),
                   ROUND(AVG(forward_ret_pct), 4)
            FROM feature_labels GROUP BY horizon_min, status
            ORDER BY horizon_min""")):
            out["labels"].append({"horizon_min": horizon, "status": status,
                                  "n": n, "avg_forward_ret_pct": avg_ret})
    return out
