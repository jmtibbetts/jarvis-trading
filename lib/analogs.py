"""Analog retrieval — "when did this market last look like NOW, and what
happened next?"

The five-year backfill made the question answerable: every cached
symbol/timeframe holds tens of thousands of historical moments. Each
moment is described by a compact SHAPE vector — multi-horizon returns
normalized by local volatility, vol regime, range position, volume
tilt — deliberately computed from bars alone and deliberately NOT the
TA engine's indicators: analogs answer "does this pattern rhyme?",
which is a different question from "what does RSI say?", and keeping
the representations separate means neither can silently contaminate
the other.

Retrieval is display/shadow-only. Forward stats over the top analogs
(median forward return, up-rate at 1h/4h/1d equivalents) are HISTORY,
not prediction — the panel says what followed similar moments, with
sample counts, and lets the operator think. Nothing here feeds a gate.

Honesty constraints:
  - analogs must be temporally NON-OVERLAPPING (else "15 matches" is
    one market episode counted 15 times)
  - forward windows must fit inside the data (an anchor too close to
    the present has no forward truth and is excluded from stats)
  - the current moment's own recent past is excluded from candidates
    (matching yourself is not evidence)
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

WINDOW = 96                 # bars of context that define "a moment"
MIN_SEPARATION = 24         # bars between chosen analogs — one episode, one vote
SELF_EXCLUSION = 192        # bars before "now" that cannot be analogs
TOP_K = 12
# Forward horizons in BARS — on 15m these are 1h / 4h / 1d; on 1H they
# are 4h / 16h / 4d. Reported with bar units so the UI can label truthfully.
FORWARD_BARS = (4, 16, 96)

RET_LOOKBACKS = (1, 2, 4, 8, 16, 32, 64, 96)


def shape_matrix(close: np.ndarray, volume: np.ndarray) -> np.ndarray:
    """Vector per bar: vol-normalized returns at 8 lookbacks, vol regime,
    range position, volume tilt. NaN rows (warmup) stay NaN."""
    n = len(close)
    logc = np.log(np.clip(close, 1e-12, None))
    r1 = np.diff(logc, prepend=logc[0])
    # Local vol: rolling std of 1-bar returns over the window.
    vol = pd.Series(r1).rolling(WINDOW).std().to_numpy()
    vol_safe = np.where(vol > 1e-12, vol, np.nan)

    feats = []
    for lb in RET_LOOKBACKS:
        ret = logc - np.roll(logc, lb)
        ret[:lb] = np.nan
        # Normalize an lb-bar move by its expected scale (vol * sqrt(lb)).
        feats.append(ret / (vol_safe * np.sqrt(lb)))
    # Vol regime: short vol vs long vol.
    vol24 = pd.Series(r1).rolling(24).std().to_numpy()
    feats.append(vol24 / vol_safe)
    # Position in the window's range.
    s = pd.Series(close)
    lo = s.rolling(WINDOW).min().to_numpy()
    hi = s.rolling(WINDOW).max().to_numpy()
    rng = np.where((hi - lo) > 1e-12, hi - lo, np.nan)
    feats.append((close - lo) / rng)
    # Volume tilt: recent vs window average.
    v = pd.Series(volume.astype(float))
    v16 = v.rolling(16).mean().to_numpy()
    v96 = v.rolling(WINDOW).mean().to_numpy()
    feats.append(v16 / np.where(v96 > 1e-12, v96, np.nan))

    return np.column_stack(feats)


def find_analogs(bars: pd.DataFrame, top_k: int = TOP_K) -> dict | None:
    """Top-k non-overlapping historical analogs of the LATEST bar's shape,
    with forward outcomes. Returns None when history is too thin to say
    anything — a thin corpus produces anecdotes, not analogs."""
    if bars is None or len(bars) < WINDOW * 4:
        return None
    close = bars["close"].astype(float).to_numpy()
    volume = (bars["volume"] if "volume" in bars else
              pd.Series(np.zeros(len(bars)))).astype(float).to_numpy()

    M = shape_matrix(close, volume)
    now_vec = M[-1]
    if np.isnan(now_vec).any():
        return None

    max_fwd = max(FORWARD_BARS)
    # Candidates: warmup done, forward truth available, not the recent past.
    lo_idx, hi_idx = WINDOW, len(bars) - max_fwd - 1
    hi_idx = min(hi_idx, len(bars) - SELF_EXCLUSION)
    if hi_idx - lo_idx < 500:
        return None
    cand = M[lo_idx:hi_idx]
    valid = ~np.isnan(cand).any(axis=1)
    dists = np.full(len(cand), np.inf)
    diffs = cand[valid] - now_vec
    dists[valid] = np.sqrt((diffs * diffs).sum(axis=1))

    # Greedy top-k with temporal separation: one episode, one vote.
    order = np.argsort(dists)
    chosen: list[int] = []
    for idx in order:
        if not np.isfinite(dists[idx]):
            break
        absolute = idx + lo_idx
        if all(abs(absolute - c) >= MIN_SEPARATION for c in chosen):
            chosen.append(absolute)
        if len(chosen) >= top_k:
            break
    if len(chosen) < 5:
        return None

    logc = np.log(np.clip(close, 1e-12, None))
    analogs = []
    fwd_by_h: dict[int, list[float]] = {h: [] for h in FORWARD_BARS}
    for a in chosen:
        fwd = {}
        for h in FORWARD_BARS:
            ret_pct = float((np.exp(logc[a + h] - logc[a]) - 1) * 100)
            fwd[f"fwd_{h}b_pct"] = round(ret_pct, 3)
            fwd_by_h[h].append(ret_pct)
        analogs.append({
            "time": str(bars.index[a]),
            "distance": round(float(dists[a - lo_idx]), 3),
            **fwd,
        })

    summary = {}
    for h in FORWARD_BARS:
        v = np.array(fwd_by_h[h])
        summary[f"fwd_{h}b"] = {
            "median_pct": round(float(np.median(v)), 3),
            "iqr_pct": [round(float(np.percentile(v, 25)), 3),
                        round(float(np.percentile(v, 75)), 3)],
            "up_rate": round(float((v > 0).mean() * 100), 1),
            "n": len(v),
        }

    return {
        "window_bars": WINDOW,
        "forward_bars": list(FORWARD_BARS),
        "candidates_searched": int(valid.sum()),
        "analogs": analogs,
        "forward_summary": summary,
        "note": ("history, not prediction: what followed the most similar "
                 "non-overlapping past moments; shape features are "
                 "returns-based and independent of the TA engine"),
    }


def analogs_for(symbol: str, timeframe: str = "15m",
                top_k: int = TOP_K) -> dict | None:
    from lib.signal_replay import load_cached_bars

    bars = load_cached_bars(symbol, timeframe)
    out = find_analogs(bars, top_k=top_k)
    if out is not None:
        out["symbol"] = symbol
        out["timeframe"] = timeframe
    return out
