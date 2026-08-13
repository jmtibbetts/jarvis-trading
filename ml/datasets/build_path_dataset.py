"""Build the path-model dataset, leakage-safe by construction.

The labels (MFE/MAE/first-touch in R) come from lib/signal_replay.py. The
FEATURES have to be reconstructed as of each signal's own moment, because
nothing stored the 33-dimensional vector at generation time.

Reconstruction is where a dataset like this normally goes wrong. Computing
RSI from the full bar history and attaching it to a signal from three
months ago gives the model an indicator that had not been observable yet.
The model then scores brilliantly in validation and is worthless live.

So: for each signal, bars are sliced to `index <= generated_at` and the TA
is computed from that slice alone. Every feature is therefore something
that existed when the decision was made. The slice is the whole discipline;
`assert_no_lookahead()` below re-checks it independently.

Output is a chronologically sorted .npz. Splitting happens at training
time, never here — a dataset that ships pre-shuffled cannot be split by
time afterwards.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.WARNING, format="%(message)s")
logger = logging.getLogger("build_path_dataset")

# Real, but they would dominate any gradient they touched. A 330R MFE comes
# from a near-zero risk distance, which is a level-placement artefact rather
# than a market move worth learning.
MFE_CLIP = 8.0
MAE_CLIP = 5.0

# Below this a timeframe cannot support its own model.
MIN_PER_TIMEFRAME = 150


def _rows(limit: int | None = None) -> list[dict]:
    from app.database import engine
    from sqlalchemy import text
    sql = """
        SELECT s.id, s.asset_symbol, s.asset_class, s.direction, s.timeframe,
               s.generated_at, s.entry_price, s.stop_loss, s.target_price,
               s.rr_ratio, s.strategy, s.strategy_score,
               o.mfe_r, o.mae_r, o.first_touch, o.outcome_source, o.pnl_pct
        FROM trade_outcomes o
        JOIN trading_signals s ON s.id = o.signal_id
        WHERE o.mfe_r IS NOT NULL AND o.mae_r IS NOT NULL
          AND s.generated_at IS NOT NULL AND s.entry_price > 0
        ORDER BY s.generated_at ASC
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    with engine.connect() as c:
        cols = ["id", "symbol", "asset_class", "direction", "timeframe",
                "generated_at", "entry_price", "stop_loss", "target_price",
                "rr_ratio", "strategy", "strategy_score",
                "mfe_r", "mae_r", "first_touch", "outcome_source", "pnl_pct"]
        return [dict(zip(cols, r)) for r in c.execute(text(sql))]


def _bars_for(symbol: str, timeframe: str):
    from lib.signal_replay import load_cached_bars
    try:
        return load_cached_bars(symbol, timeframe)
    except Exception:
        return None


def assert_no_lookahead(bars, cutoff) -> bool:
    """Independent re-check that the slice handed to the TA engine contains
    nothing after the decision moment."""
    if bars is None or len(bars) == 0:
        return True
    return bool(bars.index.max() <= cutoff)


def build(limit: int | None = None, out: Path | None = None) -> dict:
    from lib.evidence import gather
    from lib.predictive.features import build as build_features
    from lib.predictive.schemas import CURRENT_SCHEMA, feature_names, schema_hash
    from lib.signal_replay import _utc
    from lib.ta_engine import compute_timeframe

    rows = _rows(limit)
    logger.warning(f"{len(rows):,} labelled signals")

    # Bars load once per (symbol, timeframe); signals are grouped so the
    # cache is hit rather than re-read per row.
    cache: dict = {}
    X, MASK, Y_MFE, Y_MAE, Y_TOUCH, META = [], [], [], [], [], []
    skipped = {"no_bars": 0, "too_few_bars": 0, "ta_error": 0, "lookahead": 0}

    for i, r in enumerate(rows):
        if i % 1000 == 0 and i:
            logger.warning(f"  {i:,}/{len(rows):,} ...")
        key = (r["symbol"], r["timeframe"])
        if key not in cache:
            cache[key] = _bars_for(r["symbol"], r["timeframe"])
        bars = cache[key]
        if bars is None or len(bars) == 0:
            skipped["no_bars"] += 1
            continue

        cutoff = _utc(r["generated_at"])
        if cutoff is None:
            skipped["no_bars"] += 1
            continue

        # THE line that makes this honest.
        try:
            past = bars[bars.index <= cutoff]
        except Exception:
            skipped["no_bars"] += 1
            continue
        if len(past) < 60:
            skipped["too_few_bars"] += 1
            continue
        if not assert_no_lookahead(past, cutoff):
            skipped["lookahead"] += 1
            continue

        try:
            ta = compute_timeframe(past, r["timeframe"] or "4H")
            if ta.get("error"):
                skipped["ta_error"] += 1
                continue
            ev = gather(ta, r["direction"])
        except Exception:
            skipped["ta_error"] += 1
            continue

        sig = {
            "asset_symbol": r["symbol"], "asset_class": r["asset_class"],
            "direction": r["direction"], "timeframe": r["timeframe"],
            "entry_price": r["entry_price"], "stop_loss": r["stop_loss"],
            "rr_ratio": r["rr_ratio"], "strategy_score": r["strategy_score"],
        }
        fv = build_features(ta=ta, signal=sig, evidence=ev, regime=None)

        X.append(fv.values)
        MASK.append(fv.mask)
        Y_MFE.append(min(MFE_CLIP, float(r["mfe_r"])))
        Y_MAE.append(min(MAE_CLIP, float(r["mae_r"])))
        Y_TOUCH.append({"TARGET": 1, "STOP": 0}.get(r["first_touch"], -1))
        META.append((r["generated_at"], r["symbol"] or "", r["timeframe"] or "",
                     r["asset_class"] or "", r["outcome_source"] or "replay",
                     r["strategy"] or "unclassified"))

    if not X:
        raise SystemExit("no usable rows — check the OHLCV cache")

    data = {
        "X": np.asarray(X, dtype=np.float32),
        "mask": np.asarray(MASK, dtype=np.float32),
        "y_mfe": np.asarray(Y_MFE, dtype=np.float32),
        "y_mae": np.asarray(Y_MAE, dtype=np.float32),
        # 1 target-first, 0 stop-first, -1 unresolved or AMBIGUOUS. -1 is
        # EXCLUDED from first-touch training rather than resolved either
        # way: OHLC cannot say which came first inside one bar.
        "y_touch": np.asarray(Y_TOUCH, dtype=np.int64),
        "meta": np.asarray(META, dtype=object),
        "feature_names": np.asarray(feature_names(), dtype=object),
        "schema_version": CURRENT_SCHEMA,
        "schema_hash": schema_hash(),
    }

    out = out or (ROOT / "ml" / "datasets" / "path_dataset.npz")
    out.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out, **data)

    n = len(X)
    resolved = int((data["y_touch"] >= 0).sum())
    live = sum(1 for m in META if m[4] == "live")
    summary = {
        "rows": n, "path": str(out), "skipped": skipped,
        "first_touch_resolved": resolved,
        "live_rows": live, "replay_rows": n - live,
        "span": (META[0][0][:10], META[-1][0][:10]),
        "schema_hash": data["schema_hash"],
        "mean_missing": float(1.0 - data["mask"].mean()),
    }
    logger.warning(f"\nwrote {n:,} rows -> {out}")
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    import json
    print(json.dumps(build(args.limit), indent=1, default=str))
