"""
Historical conditional win-probability / expected-value model — pure
aggregation over already-evaluated signals (signal_evaluations joined with
their originating trading_signals). No prediction model, no LLM: these are
empirical frequencies of what actually happened to past signals, bucketed by
the conditions that existed when each was generated.

Non-negotiable rules, from the mega-prompt's own probability section and this
codebase's standing discipline:

  - A probability is NEVER shown without its sample size. Buckets under
    MIN_DECIDED decided outcomes report their counts but a null probability —
    3 wins out of 4 is an anecdote, not a 75% win rate.
  - Wins and losses are measured from the signal's own stored levels: a
    TARGET_HIT realized (target-entry)/entry, a STOP_HIT realized
    (stop-entry)/entry (sign-adjusted for shorts). No hindsight repricing.
  - OPEN / EXPIRED / AMBIGUOUS / INVALID_DATA outcomes are counted and shown
    but excluded from the win-rate denominator — an undecided signal is not
    evidence in either direction. AMBIGUOUS (target and stop struck within
    one bar) is deliberately not resolved by guessing intra-bar order.
  - The 95% interval is the Wilson score interval — deterministic arithmetic,
    chosen over the normal approximation because signal buckets are small.
"""
from __future__ import annotations

import math

MIN_DECIDED = 10
SCORE_BANDS = ((0, 50, "score_under_50"), (50, 70, "score_50_70"), (70, 101, "score_70_plus"))


def _score_band(score: float | None) -> str:
    if score is None:
        return "score_unknown"
    for low, high, name in SCORE_BANDS:
        if low <= score < high:
            return name
    return "score_unknown"


def bucket_key(row: dict) -> tuple:
    """Bucket by the conditions known at generation time: composite-score
    band, asset class, and direction."""
    direction = "short" if str(row.get("direction") or "").lower().startswith("short") else "long"
    return (
        _score_band(row.get("composite_score")),
        (row.get("asset_class") or "unknown").lower(),
        direction,
    )


def realized_move_pct(row: dict) -> float | None:
    """The signed % move the signal's own levels realized at its outcome."""
    entry = row.get("entry_price") or 0
    if entry <= 0:
        return None
    outcome = row.get("outcome")
    short = str(row.get("direction") or "").lower().startswith("short")
    if outcome == "TARGET_HIT":
        target = row.get("target_price") or 0
        if target <= 0:
            return None
        move = (entry - target) / entry if short else (target - entry) / entry
        return round(move * 100, 4)
    if outcome == "STOP_HIT":
        stop = row.get("stop_loss") or 0
        if stop <= 0:
            return None
        move = (entry - stop) / entry if short else (stop - entry) / entry
        return round(move * 100, 4)
    return None


def wilson_interval(wins: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    if n == 0:
        return None
    p = wins / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (round(max(0.0, center - half), 4), round(min(1.0, center + half), 4))


def compute_ev_buckets(rows: list[dict]) -> list[dict]:
    """Aggregate evaluated signals into buckets, each carrying its full
    accounting. Probability and EV appear only at MIN_DECIDED+ decided
    outcomes; below that the bucket still reports counts with an explicit
    insufficient_sample marker."""
    buckets: dict[tuple, dict] = {}
    for row in rows:
        key = bucket_key(row)
        b = buckets.setdefault(key, {
            "score_band": key[0], "asset_class": key[1], "direction": key[2],
            "total": 0, "decided": 0, "wins": 0, "losses": 0,
            "open": 0, "expired": 0, "ambiguous": 0, "invalid": 0,
            "win_moves": [], "loss_moves": [],
        })
        b["total"] += 1
        outcome = row.get("outcome")
        if outcome == "TARGET_HIT":
            b["decided"] += 1
            b["wins"] += 1
            move = realized_move_pct(row)
            if move is not None:
                b["win_moves"].append(move)
        elif outcome == "STOP_HIT":
            b["decided"] += 1
            b["losses"] += 1
            move = realized_move_pct(row)
            if move is not None:
                b["loss_moves"].append(move)
        elif outcome == "OPEN":
            b["open"] += 1
        elif outcome == "EXPIRED":
            b["expired"] += 1
        elif outcome == "AMBIGUOUS":
            b["ambiguous"] += 1
        else:
            b["invalid"] += 1

    results = []
    for b in buckets.values():
        win_moves, loss_moves = b.pop("win_moves"), b.pop("loss_moves")
        decided = b["decided"]
        if decided >= MIN_DECIDED:
            p = b["wins"] / decided
            avg_win = sum(win_moves) / len(win_moves) if win_moves else None
            avg_loss = sum(loss_moves) / len(loss_moves) if loss_moves else None
            ev = None
            if avg_win is not None and avg_loss is not None:
                ev = round(p * avg_win + (1 - p) * avg_loss, 4)
            b.update({
                "insufficient_sample": False,
                "win_probability": round(p, 4),
                "win_probability_ci95": wilson_interval(b["wins"], decided),
                "avg_win_pct": round(avg_win, 4) if avg_win is not None else None,
                "avg_loss_pct": round(avg_loss, 4) if avg_loss is not None else None,
                "expected_value_pct": ev,
            })
        else:
            b.update({
                "insufficient_sample": True,
                "win_probability": None,
                "win_probability_ci95": None,
                "avg_win_pct": None,
                "avg_loss_pct": None,
                "expected_value_pct": None,
                "note": f"{decided} decided outcome(s) — below the {MIN_DECIDED}-sample floor for a probability",
            })
        results.append(b)

    results.sort(key=lambda r: -r["decided"])
    return results
