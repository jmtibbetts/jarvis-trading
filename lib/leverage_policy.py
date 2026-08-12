"""How much leverage a signal earns.

Conviction sets the ceiling; evidence takes it away. The base curve runs
from 1x at the execution floor to MAX_LEVERAGE at a perfect score, then
four independent factors — each of which can only REDUCE it — apply:

  regime      a high-risk regime halves leverage; nothing raises it
  history     realized win rate for that score band, with an explicit
              penalty for unproven buckets rather than optimism
  streak      consecutive losses cut size; the account is telling you
              something before any model does
  volatility  ATR far above normal shrinks the position

Design rules this module follows deliberately:

- The floor comes from the USER'S configured criteria, not a constant. If
  the operator raises the minimum score to 70, a 70 scores 1x and 100
  still scores the maximum — the curve always spans the range they chose,
  instead of a hardcoded 55 that silently disagrees with their settings.
- Every factor is multiplicative and capped at 1.0, so no combination of
  bullish inputs can inflate leverage past what conviction alone earned.
- decide() returns the full breakdown, never a bare number: a 25x that
  became 6x should say which factor did it.
- Unknown inputs are treated as UNPROVEN, not neutral. A bucket with no
  history is penalised, because "we have never seen this work" is
  information, not the absence of it.
"""
from __future__ import annotations

MIN_LEVERAGE = 1.0
MAX_LEVERAGE = 25.0

# Sample size below which a win rate is not yet evidence.
MIN_SAMPLE_FOR_TRUST = 20
UNPROVEN_FACTOR = 0.6

# Losses in a row before the brake bites, and how hard.
STREAK_THRESHOLDS = ((5, 0.25), (3, 0.5), (2, 0.75))


def conviction_leverage(score: float | None, floor: float,
                        max_leverage: float = MAX_LEVERAGE) -> float:
    """Linear from 1x at `floor` to max_leverage at 100."""
    try:
        sc = float(score or 0)
    except (TypeError, ValueError):
        sc = 0.0
    floor = max(0.0, min(99.0, float(floor)))
    if sc <= floor:
        return MIN_LEVERAGE
    frac = min(1.0, (sc - floor) / (100.0 - floor))
    return MIN_LEVERAGE + frac * (max_leverage - MIN_LEVERAGE)


def regime_factor(regime: dict | None) -> tuple[float, str]:
    risk = str((regime or {}).get("risk") or "").lower()
    if risk == "high":
        return 0.5, "high-risk regime — leverage halved"
    if risk == "medium":
        return 0.85, "medium-risk regime"
    if risk == "low":
        return 1.0, "low-risk regime"
    return 0.85, "regime unknown — treated as medium"


def history_factor(win_rate: float | None, sample: int | None) -> tuple[float, str]:
    """Realized win rate for this kind of setup, honestly sampled.

    A win rate is only trusted at MIN_SAMPLE_FOR_TRUST+ decided outcomes.
    Below that the setup is UNPROVEN and takes a penalty — betting big on
    something with no track record is the most expensive habit there is.
    """
    n = int(sample or 0)
    if win_rate is None or n < MIN_SAMPLE_FOR_TRUST:
        return UNPROVEN_FACTOR, f"unproven setup ({n} decided outcomes, need {MIN_SAMPLE_FOR_TRUST})"
    wr = max(0.0, min(1.0, float(win_rate)))
    if wr >= 0.60:
        return 1.0, f"{wr:.0%} historical win rate over {n}"
    if wr >= 0.50:
        return 0.85, f"{wr:.0%} win rate over {n} — modest edge"
    if wr >= 0.40:
        return 0.6, f"{wr:.0%} win rate over {n} — weak edge"
    return 0.35, f"{wr:.0%} win rate over {n} — negative edge, size cut hard"


def streak_factor(consecutive_losses: int | None) -> tuple[float, str]:
    n = int(consecutive_losses or 0)
    for threshold, factor in STREAK_THRESHOLDS:
        if n >= threshold:
            return factor, f"{n} losses in a row — size reduced"
    return 1.0, "no losing streak"


def volatility_factor(atr_pct: float | None, typical_atr_pct: float | None = None) -> tuple[float, str]:
    """Shrink into abnormal volatility. Compares the setup's ATR% against a
    typical reading for the instrument when one is known; falls back to an
    absolute scale otherwise."""
    if atr_pct is None or atr_pct <= 0:
        return 1.0, "volatility unknown"
    if typical_atr_pct and typical_atr_pct > 0:
        ratio = atr_pct / typical_atr_pct
        if ratio >= 2.0:
            return 0.5, f"ATR {ratio:.1f}x its normal level"
        if ratio >= 1.5:
            return 0.75, f"ATR {ratio:.1f}x its normal level"
        return 1.0, f"ATR near normal ({ratio:.1f}x)"
    if atr_pct >= 8.0:
        return 0.5, f"ATR {atr_pct:.1f}% — very volatile"
    if atr_pct >= 4.0:
        return 0.75, f"ATR {atr_pct:.1f}% — elevated"
    return 1.0, f"ATR {atr_pct:.1f}% — normal"


def decide(score: float | None, floor: float, *, regime: dict | None = None,
           win_rate: float | None = None, sample: int | None = None,
           consecutive_losses: int | None = None, atr_pct: float | None = None,
           typical_atr_pct: float | None = None,
           max_leverage: float = MAX_LEVERAGE) -> dict:
    """Final leverage plus the full accounting of how it got there."""
    base = conviction_leverage(score, floor, max_leverage)
    factors = []
    for name, (value, why) in (
        ("regime", regime_factor(regime)),
        ("history", history_factor(win_rate, sample)),
        ("streak", streak_factor(consecutive_losses)),
        ("volatility", volatility_factor(atr_pct, typical_atr_pct)),
    ):
        factors.append({"name": name, "factor": round(value, 3), "why": why})

    multiplier = 1.0
    for f in factors:
        multiplier *= f["factor"]

    final = max(MIN_LEVERAGE, min(max_leverage, base * multiplier))
    binding = min(factors, key=lambda f: f["factor"])
    return {
        "leverage": round(final, 1),
        "base_leverage": round(base, 1),
        "multiplier": round(multiplier, 3),
        "factors": factors,
        "binding_constraint": binding["name"] if binding["factor"] < 1.0 else None,
        "explanation": (
            f"score {float(score or 0):.0f} over a floor of {floor:.0f} earns "
            f"{base:.1f}x; " + ", ".join(f["why"] for f in factors if f["factor"] < 1.0)
            + f" -> {final:.1f}x"
        ) if multiplier < 1.0 else (
            f"score {float(score or 0):.0f} over a floor of {floor:.0f} earns {base:.1f}x; "
            f"no constraint applied"
        ),
    }
