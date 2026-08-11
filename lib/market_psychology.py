"""
JARVIS Market Psychology Index — a fear/greed composite built ONLY from data
this system already collects and verifies, rather than scraping a third-party
fear & greed number.

Every component is a pure function over already-fetched data (no I/O here), so
the scoring is unit-testable in isolation.

Scale convention, applied uniformly: 0 = extreme fear, 100 = extreme greed,
50 = neutral. A component returns None to ABSTAIN when its input is missing.
Abstaining matters: scoring an absent input as a neutral 50 would silently drag
the composite toward neutral and misrepresent thin data as balanced sentiment.
The composite reports how many components actually contributed.

On honesty of the mappings: the thresholds below are conventions, not
discovered constants, and each is documented where it is applied. The one
component with a genuinely empirical basis is VIX, which is ranked against its
own trailing history rather than against invented cutoffs. Nothing here is an
LLM judgment — it is all arithmetic over measured values.

What this index is NOT: a prediction. It describes current positioning and
volatility conditions. Extreme readings have historically coincided with
turning points, but this module does not claim to time them.
"""
from __future__ import annotations

import math

LABELS = (
    (20, "EXTREME_FEAR"),
    (40, "FEAR"),
    (60, "NEUTRAL"),
    (80, "GREED"),
    (101, "EXTREME_GREED"),
)


def _clamp(v: float) -> float:
    return max(0.0, min(100.0, v))


def label_for(score: float | None) -> str | None:
    if score is None:
        return None
    for ceiling, name in LABELS:
        if score < ceiling:
            return name
    return "EXTREME_GREED"


def vix_component(current: float | None, history: list[float] | None) -> dict | None:
    """Percentile rank of the current VIX within its own trailing history,
    inverted so that a LOW VIX (complacency) reads as greed.

    Ranking against actual history — rather than fixed "VIX below 15 is calm"
    cutoffs — keeps this meaningful across volatility regimes, where the same
    absolute level can be unusually high or unusually low."""
    if current is None or not history or len(history) < 30:
        return None
    below = sum(1 for h in history if h < current)
    percentile = below / len(history) * 100
    return {
        "score": round(_clamp(100 - percentile), 1),
        "vix": round(current, 2),
        "percentile": round(percentile, 1),
        "sample_size": len(history),
        "detail": f"VIX {current:.1f} sits at the {percentile:.0f}th percentile of the last {len(history)} sessions",
    }


def breadth_component(change_percents: list[float] | None) -> dict | None:
    """Share of tracked assets trading up on the day. This one needs no
    arbitrary scaling: the percentage advancing IS the 0-100 reading, and 50%
    advancing is genuinely neutral."""
    values = [c for c in (change_percents or []) if c is not None]
    if len(values) < 5:
        return None
    advancing = sum(1 for c in values if c > 0)
    pct = advancing / len(values) * 100
    return {
        "score": round(_clamp(pct), 1),
        "advancing": advancing,
        "declining": len(values) - advancing,
        "universe_size": len(values),
        "detail": f"{advancing} of {len(values)} tracked assets advancing ({pct:.0f}%)",
    }


# Perpetual funding is quoted per settlement period and normally sits within a
# few basis points of zero. 0.05% (0.0005) either way is treated as the edge of
# the normal band — a convention chosen to keep routine funding near neutral
# rather than saturating the score.
FUNDING_FULL_SCALE = 0.0005


def funding_component(funding_rates: list[float] | None) -> dict | None:
    """Positive funding means longs are paying shorts to hold the position —
    crowded long, read as greed. Negative funding is the inverse."""
    rates = [r for r in (funding_rates or []) if r is not None]
    if not rates:
        return None
    avg = sum(rates) / len(rates)
    return {
        "score": round(_clamp(50 + avg / FUNDING_FULL_SCALE * 50), 1),
        "avg_funding_rate": round(avg, 8),
        "symbols": len(rates),
        "detail": (
            f"Average perp funding {avg * 100:.4f}% across {len(rates)} symbol(s) — "
            f"{'longs paying shorts' if avg > 0 else 'shorts paying longs' if avg < 0 else 'balanced'}"
        ),
    }


def long_short_component(ratios: list[float] | None) -> dict | None:
    """Account long/short ratio. Scored on a log scale because the ratio is
    multiplicative: 2.0 (twice as many longs) and 0.5 (twice as many shorts)
    are symmetric extremes and must score symmetrically about 50."""
    values = [r for r in (ratios or []) if r is not None and r > 0]
    if not values:
        return None
    avg = sum(values) / len(values)
    return {
        "score": round(_clamp(50 + math.log2(avg) * 35), 1),
        "avg_long_short_ratio": round(avg, 3),
        "symbols": len(values),
        "detail": f"Average long/short account ratio {avg:.2f} across {len(values)} symbol(s)",
    }


def liquidation_component(long_liquidated_usd: float | None, short_liquidated_usd: float | None) -> dict | None:
    """Which side is being forcibly closed. Longs blowing up means price is
    falling through leveraged positions (fear); shorts blowing up means the
    opposite (greed). Uses the skew between the two, so total market size
    doesn't distort the reading."""
    long_usd = long_liquidated_usd or 0.0
    short_usd = short_liquidated_usd or 0.0
    total = long_usd + short_usd
    if total <= 0:
        return None
    skew = (short_usd - long_usd) / total  # -1 (all longs liquidated) .. +1 (all shorts)
    return {
        "score": round(_clamp(50 + skew * 50), 1),
        "long_liquidated_usd": round(long_usd, 2),
        "short_liquidated_usd": round(short_usd, 2),
        "skew": round(skew, 3),
        "detail": (
            f"${long_usd:,.0f} longs vs ${short_usd:,.0f} shorts liquidated — "
            f"{'longs' if skew < 0 else 'shorts'} taking the pain"
        ),
    }


# Weights are a stated editorial judgment, not an optimized fit. VIX and breadth
# carry the most because they cover the broad equity market; the three crypto
# components together are capped near the weight of the two equity ones so a
# quiet equity tape isn't overwhelmed by crypto leverage churn.
COMPONENT_WEIGHTS = {
    "vix": 0.30,
    "breadth": 0.25,
    "funding": 0.15,
    "long_short": 0.15,
    "liquidations": 0.15,
}


def compute_psychology_index(components: dict) -> dict:
    """Weighted composite over whichever components have data.

    Weights are renormalized across only the contributing components, so an
    absent input doesn't silently pull the result toward zero. Returns the
    composite alongside every component, per the rule applied throughout this
    codebase: never hide the evidence behind a single number."""
    present = {k: v for k, v in components.items() if v and v.get("score") is not None}
    if not present:
        return {
            "score": None, "label": None,
            "components": components,
            "components_available": 0,
            "components_possible": len(COMPONENT_WEIGHTS),
            "note": "No component inputs available — index cannot be computed.",
        }

    total_weight = sum(COMPONENT_WEIGHTS.get(k, 0) for k in present)
    if total_weight <= 0:
        return {
            "score": None, "label": None, "components": components,
            "components_available": 0, "components_possible": len(COMPONENT_WEIGHTS),
            "note": "Available components carry no weight.",
        }

    score = sum(v["score"] * COMPONENT_WEIGHTS.get(k, 0) for k, v in present.items()) / total_weight
    score = round(_clamp(score), 1)
    return {
        "score": score,
        "label": label_for(score),
        "components": components,
        "components_available": len(present),
        "components_possible": len(COMPONENT_WEIGHTS),
        "weight_coverage": round(total_weight, 3),
        "note": (
            f"Composite of {len(present)} of {len(COMPONENT_WEIGHTS)} components "
            f"({total_weight * 100:.0f}% of total weight). Absent components abstain "
            f"rather than scoring neutral."
        ),
    }


def compute_rate_of_change(current: float | None, prior: float | None, hours: float | None) -> dict | None:
    """Change in the index since an earlier snapshot. The mega-prompt asks for
    both absolute level and rate of change — a fast move from greed toward fear
    is different information from simply sitting at neutral."""
    if current is None or prior is None or not hours or hours <= 0:
        return None
    delta = current - prior
    return {
        "delta": round(delta, 1),
        "hours": round(hours, 2),
        "per_day": round(delta / hours * 24, 2),
        "direction": "toward_greed" if delta > 0 else "toward_fear" if delta < 0 else "flat",
    }
