"""
Portfolio risk analytics — returns-based correlation, historical VaR, hidden
concentration, and market breadth. Pure functions over already-fetched price
series; the route layer assembles data (cache-only) and these compute.

Methods are the standard ones, named explicitly in every output so a reading
is never mistaken for something fancier than it is:

  VaR      historical simulation (no distributional assumption): the loss at
           the chosen percentile of the portfolio's own realized daily
           returns. Reported with sample_days and method label. Abstains
           below MIN_VAR_DAYS overlapping days — a percentile of 20
           observations is noise.
  corr     Pearson on daily returns with pairwise-complete overlap; a pair
           with under MIN_OVERLAP common days is reported as null, not 0 —
           zero is a claim, null is an absence.
  breadth  share of symbols above their own N-day simple moving averages,
           with explicit coverage (how many symbols actually had enough
           cached history) — 5 of 40 symbols is a very different claim from
           40 of 40, and the number is never hidden.

The hidden-concentration idea (mega-prompt: "eight positions may actually be
one leveraged risk-on bet") is made concrete as: pairs of held positions with
|corr| >= HIGH_CORR plus the portfolio's average pairwise correlation.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

MIN_VAR_DAYS = 60
MIN_OVERLAP = 30
HIGH_CORR = 0.7


def returns_frame(closes_by_symbol: dict[str, pd.Series]) -> pd.DataFrame:
    """Daily % returns, outer-joined on CALENDAR DATE.

    Timestamps are floored to the day before joining because different asset
    classes stamp their daily bars at different hours (crypto at 00:00 UTC,
    equities at exchange hours) — joining on raw timestamps produced ZERO
    common rows across a mixed equity+crypto book (observed live), silently
    killing the joint VaR. Duplicate same-day bars keep the last."""
    cols = {}
    for sym, closes in closes_by_symbol.items():
        if closes is None or len(closes) < 2:
            continue
        s = closes.astype(float) if isinstance(closes, pd.Series) else pd.Series(closes).astype(float)
        if isinstance(s.index, pd.DatetimeIndex):
            s = s.copy()
            s.index = s.index.tz_localize(None) if s.index.tz is not None else s.index
            s.index = s.index.normalize()
            s = s[~s.index.duplicated(keep="last")]
        cols[sym] = s.pct_change().dropna()
    if not cols:
        return pd.DataFrame()
    return pd.DataFrame(cols)


def correlation_matrix(returns: pd.DataFrame, min_overlap: int = MIN_OVERLAP) -> dict:
    """Pairwise Pearson correlation. Pairs with insufficient overlap are null."""
    symbols = list(returns.columns)
    matrix: dict[str, dict[str, float | None]] = {}
    for a in symbols:
        matrix[a] = {}
        for b in symbols:
            if a == b:
                matrix[a][b] = 1.0
                continue
            pair = returns[[a, b]].dropna()
            if len(pair) < min_overlap:
                matrix[a][b] = None
                continue
            c = float(pair[a].corr(pair[b]))
            matrix[a][b] = round(c, 3) if not np.isnan(c) else None
    return matrix


def concentration_summary(matrix: dict, weights: dict[str, float]) -> dict:
    """Highly-correlated pairs among held positions + average pairwise corr.
    Weights are position market values (sign-agnostic shares of gross)."""
    symbols = [s for s in matrix if s in weights]
    pairs = []
    values = []
    for i, a in enumerate(symbols):
        for b in symbols[i + 1:]:
            c = matrix.get(a, {}).get(b)
            if c is None:
                continue
            values.append(c)
            if abs(c) >= HIGH_CORR:
                pairs.append({"a": a, "b": b, "correlation": c})
    pairs.sort(key=lambda p: -abs(p["correlation"]))
    avg = round(float(np.mean(values)), 3) if values else None
    return {
        "avg_pairwise_correlation": avg,
        "high_correlation_pairs": pairs,
        "pairs_measured": len(values),
        "interpretation": (
            None if avg is None else
            "positions are moving largely as one trade" if avg >= HIGH_CORR else
            "meaningful co-movement across the book" if avg >= 0.4 else
            "reasonable diversification by realized correlation"
        ),
    }


def historical_var(returns: pd.DataFrame, weights: dict[str, float],
                   confidence: float = 0.95, gross_value: float | None = None) -> dict | None:
    """One-day historical-simulation VaR of the CURRENT portfolio weights
    applied to realized joint daily returns.

    Abstains (None) with fewer than MIN_VAR_DAYS days where every held symbol
    has a return — the joint distribution is the whole point, so partial days
    are dropped rather than filled with zeros (a zero return is a claim)."""
    held = [s for s in returns.columns if weights.get(s)]
    if not held:
        return None

    # Histories can individually be long yet jointly thin: observed live, a
    # 7-symbol book whose windows overlapped only ~38 days because one symbol's
    # history started late and three others' ended early. Rather than abstain
    # entirely, iteratively drop the symbol whose absence grows the joint
    # sample most, until the floor is met — every exclusion is reported, and
    # var_usd is later scaled only to the gross actually included.
    excluded: list[str] = []
    joint = returns[held].dropna()
    while len(joint) < MIN_VAR_DAYS and len(held) > 1:
        best_sym, best_len = None, len(joint)
        for s in held:
            trial = returns[[x for x in held if x != s]].dropna()
            if len(trial) > best_len:
                best_sym, best_len = s, len(trial)
        if best_sym is None:
            break  # no single removal helps — genuinely insufficient data
        held.remove(best_sym)
        excluded.append(best_sym)
        joint = returns[held].dropna()

    if len(joint) < MIN_VAR_DAYS:
        return None
    total = sum(abs(weights[s]) for s in held)
    if total <= 0:
        return None
    w = np.array([weights[s] / total for s in held])
    port_returns = joint.values @ w
    var_pct = float(-np.percentile(port_returns, (1 - confidence) * 100))
    es_tail = port_returns[port_returns <= -var_pct]
    es_pct = float(-es_tail.mean()) if len(es_tail) else var_pct
    out = {
        "method": "historical_simulation_1d",
        "confidence": confidence,
        "var_pct": round(var_pct * 100, 3),
        "expected_shortfall_pct": round(es_pct * 100, 3),
        "sample_days": int(len(joint)),
        "symbols_included": held,
        "symbols_excluded_short_history": excluded,
    }
    if gross_value:
        # Scale only to the symbols actually in the measurement.
        included_gross = sum(abs(weights[s]) for s in held)
        out["var_usd"] = round(var_pct * included_gross, 2)
        out["expected_shortfall_usd"] = round(es_pct * included_gross, 2)
        out["included_gross_usd"] = round(included_gross, 2)
    return out


def breadth_above_smas(closes_by_symbol: dict[str, pd.Series],
                       windows: tuple[int, ...] = (20, 50, 200)) -> dict:
    """Share of symbols trading above their own N-day SMAs, with coverage
    reported per window (a symbol needs N+ closes to count for window N)."""
    result: dict = {"universe_size": len(closes_by_symbol), "windows": {}}
    for w in windows:
        above = eligible = 0
        for closes in closes_by_symbol.values():
            s = pd.Series(closes).astype(float) if not isinstance(closes, pd.Series) else closes.astype(float)
            if len(s) < w:
                continue
            eligible += 1
            if float(s.iloc[-1]) > float(s.tail(w).mean()):
                above += 1
        result["windows"][f"sma{w}"] = {
            "above": above,
            "eligible": eligible,
            "pct_above": round(above / eligible * 100, 1) if eligible else None,
        }
    return result
