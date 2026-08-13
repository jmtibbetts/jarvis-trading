"""Historical path dataset — the training corpus Phase 4 never had.

The path model was rejected on 9,246 labels spanning ONE usable day, which
made its chronological split meaningless. This walks the deep Twelve Data
history (3+ years of 15m/1H bars) forward, computes the full TA state at
each anchor from PAST bars only, poses one hypothetical position per
direction with the book's standard geometry, and labels what the future
actually did — the same stop-first-conservative, AMBIGUOUS-aware walk the
replay engine uses on real signals, so historical and live labels mean the
same thing.

Leakage rules, enforced structurally rather than by care:
  - features come from df.iloc[i-window : i+1]  (bars <= anchor, closed)
  - labels   come from df.iloc[i+1 : i+1+horizon]  (bars strictly after)
  - no normalization here at all — normalizers are fit at TRAINING time on
    the training split only, never on the whole dataset.

Output: one immutable Parquet + manifest per run (never overwritten), per
the platform doc's reproducibility rules.

Usage:
    python -m ml.datasets.build_path_dataset --timeframe 15m
    python -m ml.datasets.build_path_dataset --timeframe 1H --stride 4
"""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

FEATURE_SCHEMA = "path_features_v1_2026-08-13"
LABEL_SCHEMA = "path_labels_v1_2026-08-13"

# The book's standard geometry: ~1.5R. Held fixed across the dataset so the
# label distribution reflects MARKET behaviour, not geometry choices.
STOP_ATR = 1.5
TARGET_ATR = 2.25

OUT_DIR = Path("ml/datasets/out")


def _f(v):
    try:
        f = float(v)
        return f if f == f else None      # NaN -> None
    except (TypeError, ValueError):
        return None


def _g(d, *path):
    cur = d
    for p in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


_TREND_ENC = {"strong_down": -2, "down": -1, "bearish": -1, "neutral": 0,
              "sideways": 0, "up": 1, "bullish": 1, "strong_up": 2}


def _enc(value, table=_TREND_ENC):
    if value is None:
        return None
    return table.get(str(value).lower())


def flatten_features(ta: dict) -> dict:
    """The numeric feature vector, schema path_features_v1.

    Distances are expressed relative to price or ATR so the same feature
    means the same thing on a $0.000004 SHIB bar and a $63,000 BTC bar.
    Missing values stay None (NaN in the frame) — a zero would be a claim.
    """
    price = _f(_g(ta, "price", "last"))
    atr = _f(_g(ta, "atr", "value"))
    out = {
        "rsi": _f(ta.get("rsi")),
        "adx": _f(_g(ta, "adx", "value") if isinstance(ta.get("adx"), dict) else ta.get("adx")),
        "atr_pct": (atr / price * 100) if (atr and price) else None,
        "pct_change": _f(_g(ta, "price", "pct_change")),
        "preceding_return_5": _f(ta.get("preceding_return_5")),
        "cci": _f(ta.get("cci")),
        "mfi": _f(ta.get("mfi")),
        "williams_r": _f(ta.get("williams_r")),
        "stoch_k": _f(_g(ta, "stochastic", "k")),
        "stoch_d": _f(_g(ta, "stochastic", "d")),
        "macd_hist": _f(_g(ta, "macd", "histogram")),
        # pct_b IS the position (0 = lower band, 1 = upper); "position" in
        # this dict is a WORD ("inside"), verified against live output.
        "bb_pct_b": _f(_g(ta, "bollinger_bands", "pct_b")),
        "bb_bandwidth": _f(_g(ta, "bollinger_bands", "bandwidth")),
        "volume_surge_ratio": _f(_g(ta, "volume", "surge_ratio")),
        "range_position": _f(_g(ta, "support_resistance", "position_in_range")),
        # trend is {"pct": 0-100}, a bullish-alignment percentage —
        # verified live, not the word the name suggests.
        "trend_pct": _f(_g(ta, "trend", "pct")),
        "bias": _enc(ta.get("bias")),
        "obv_trend": _enc(ta.get("obv_trend"),
                          {"rising": 1, "flat": 0, "falling": -1}),
        "supertrend_dir": _enc(_g(ta, "supertrend", "direction")),
        "atr_percentile": _f(_g(ta, "atr_profile", "percentile")),
    }
    # Price vs each EMA, in percent — the trend-location features. Keys are
    # 'ema9' style, verified against live output.
    emas = ta.get("emas") or {}
    for p in (9, 21, 50, 200):
        e = _f(emas.get(f"ema{p}"))
        out[f"ema{p}_dist_pct"] = ((price - e) / e * 100) if (e and price) else None
    # ATR-normalized distances to structure — 'to_*' keys.
    dist = ta.get("atr_distances") or {}
    for k in ("support", "resistance", "vwap"):
        out[f"dist_{k}_atr"] = _f(dist.get(f"to_{k}"))
    return out


def path_labels(future, entry: float, stop: float, target: float,
                is_short: bool) -> dict:
    """Same rules as the live replay: stop-first inside a bar, AMBIGUOUS
    when both levels are inside one bar, MFE/MAE in R."""
    risk = abs(entry - stop)
    if risk <= 0 or future is None or len(future) == 0:
        return {}
    max_fav = max_adv = 0.0
    first_touch = None
    touch_bar = None
    for i, (_, bar) in enumerate(future.iterrows(), start=1):
        hi, lo = float(bar["high"]), float(bar["low"])
        fav = (entry - lo) if is_short else (hi - entry)
        adv = (hi - entry) if is_short else (entry - lo)
        max_fav = max(max_fav, fav)
        max_adv = max(max_adv, adv)
        if first_touch is None:
            stop_hit = (hi >= stop) if is_short else (lo <= stop)
            tgt_hit = (lo <= target) if is_short else (hi >= target)
            if stop_hit and tgt_hit:
                first_touch, touch_bar = "AMBIGUOUS", i
            elif stop_hit:
                first_touch, touch_bar = "STOP", i
            elif tgt_hit:
                first_touch, touch_bar = "TARGET", i

    closes = future["close"].astype(float)
    entry_signed = -1.0 if is_short else 1.0

    def fwd(n):
        if len(closes) < n:
            return None
        return (float(closes.iloc[n - 1]) / entry - 1) * 100 * entry_signed

    return {
        "mfe_r": round(max_fav / risk, 4),
        "mae_r": round(max_adv / risk, 4),
        "first_touch": first_touch or "NONE",
        "bars_to_touch": touch_bar,
        "fwd_ret_4": fwd(4),
        "fwd_ret_16": fwd(16),
        "fwd_ret_64": fwd(64),
    }


def cached_symbols(timeframe: str, min_bars: int) -> list[str]:
    import sqlite3
    con = sqlite3.connect("data/ohlcv_cache.db")
    rows = con.execute(
        "SELECT symbol, COUNT(*) n FROM ohlcv_bars WHERE timeframe=? "
        "GROUP BY symbol HAVING n >= ? ORDER BY n DESC",
        (timeframe, min_bars)).fetchall()
    con.close()
    return [r[0] for r in rows]


def build(timeframe: str, symbols: list[str], stride: int, window: int,
          horizon: int, tag: str) -> Path:
    import pandas as pd

    from lib.signal_replay import load_cached_bars
    from lib.ta_engine import compute_timeframe

    rows = []
    for si, sym in enumerate(symbols, 1):
        bars = load_cached_bars(sym, timeframe)
        if bars is None or len(bars) < window + horizon + stride:
            logger.info(f"  [{si}/{len(symbols)}] {sym}: too thin, skipped")
            continue
        n_anchors = 0
        for i in range(window, len(bars) - horizon, stride):
            past = bars.iloc[i - window: i + 1]
            future = bars.iloc[i + 1: i + 1 + horizon]
            ta = compute_timeframe(past, timeframe)
            if ta.get("error"):
                continue
            feats = flatten_features(ta)
            atr = _f(_g(ta, "atr", "value"))
            entry = _f(_g(ta, "price", "last"))
            if not atr or not entry:
                continue
            anchor_ts = str(bars.index[i])
            for direction in ("Long", "Short"):
                is_short = direction == "Short"
                sgn = -1.0 if is_short else 1.0
                stop = entry - sgn * STOP_ATR * atr
                target = entry + sgn * TARGET_ATR * atr
                labels = path_labels(future, entry, stop, target, is_short)
                if not labels:
                    continue
                rows.append({
                    "symbol": sym,
                    "asset_class": "crypto" if "/" in sym else "equity",
                    "timeframe": timeframe,
                    "anchor_ts": anchor_ts,
                    "direction": direction,
                    "entry": entry, "stop": stop, "target": target,
                    **feats, **labels,
                })
            n_anchors += 1
        logger.info(f"  [{si}/{len(symbols)}] {sym}: {n_anchors} anchors "
                    f"({len(bars)} bars)")

    df = pd.DataFrame(rows)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    dataset_id = f"path_{timeframe}_{stamp}{('_' + tag) if tag else ''}"
    parquet_path = OUT_DIR / f"{dataset_id}.parquet"
    if parquet_path.exists():
        raise FileExistsError(f"{parquet_path} exists — datasets are immutable, "
                              "use a tag for a new run")
    df.to_parquet(parquet_path, index=False)

    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                                text=True).stdout.strip()
    except Exception:
        commit = None
    manifest = {
        "dataset_id": dataset_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "feature_schema": FEATURE_SCHEMA,
        "label_schema": LABEL_SCHEMA,
        "timeframe": timeframe,
        "stride_bars": stride, "window_bars": window, "horizon_bars": horizon,
        "geometry": {"stop_atr": STOP_ATR, "target_atr": TARGET_ATR},
        "symbols": symbols,
        "rows": len(df),
        "time_start": str(df["anchor_ts"].min()) if len(df) else None,
        "time_end": str(df["anchor_ts"].max()) if len(df) else None,
        "git_commit": commit,
        "leakage_rules": "features from bars <= anchor; labels from bars > anchor; "
                         "no normalization at build time",
    }
    (OUT_DIR / f"{dataset_id}.manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")
    logger.info(f"\ndataset: {parquet_path}  rows={len(df):,}")
    return parquet_path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--timeframe", default="15m")
    ap.add_argument("--stride", type=int, default=8)
    ap.add_argument("--window", type=int, default=240)
    ap.add_argument("--horizon", type=int, default=96)
    ap.add_argument("--symbols", help="comma-separated; default: every cached "
                                      "symbol deep enough to use")
    ap.add_argument("--tag", default="")
    args = ap.parse_args()

    symbols = ([s.strip() for s in args.symbols.split(",") if s.strip()]
               if args.symbols
               else cached_symbols(args.timeframe,
                                   args.window + args.horizon + args.stride))
    logger.info(f"building {args.timeframe} path dataset over {len(symbols)} symbols "
                f"(stride {args.stride}, window {args.window}, horizon {args.horizon})")
    build(args.timeframe, symbols, args.stride, args.window, args.horizon, args.tag)
    return 0


if __name__ == "__main__":
    sys.exit(main())
