"""Shadow scoring variants — measured alternatives to the live composite.

The live composite is monotonically INVERTED against outcomes (measured
2026-08-13, 8,932 labelled 4H outcomes): the <60 band wins 53.9% at +0.516%
avg P&L while 80+ wins 30.5% at -0.253%, and MFE falls from 1.145R to
0.703R as the score rises. Decomposition located the mechanism:

    ta_confluence   weight 0.20 (largest)   measured -1.035%/trade  INVERTED
    conflict_ratio  penalty -12/-6          measured +0.758%/trade  healthy
    volatility      weight 0.04             measured +1.051%/trade  positive
    regime 0.08, news 0.05, freshness 0.07  all measured inverted

"Every timeframe agrees" is what the END of a move looks like: full
confluence arrives after the move has happened, so the composite pays 20
points for exhaustion and fines the disagreement that marks an early entry.

These variants exist to be RUN IN SHADOW — logged beside every new signal,
influencing nothing — until resolved outcomes say which selects better.
Per the review that motivated this: a high live score currently means
"high output from the existing formula", not "strong setup", and no
variant here graduates without out-of-sample evidence.

Nothing in this module touches execution. The live composite in
lib/signal_scorer.py is untouched and remains the control (variant A).
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Pinned per the feature-versioning rule (§58): a variant's meaning may
# never silently change. New weights = new version string, never an edit
# to an existing one. v2 ADDS variant MS; B and C are byte-identical to v1.
VARIANT_SCHEMA_VERSION = "shadow_v2_2026-08-14"

# When each variant's definition was frozen. The promotion framework uses
# these as leakage cutoffs: a variant may only be judged on candidates
# created AFTER its definition existed — C was calibrated on data through
# 2026-08-13, so any candidate from before that date is in-sample for C
# and counting it would let the variant grade its own homework.
VARIANT_DEFINED = {
    "B": "2026-08-13T00:00:00+00:00",
    "C": "2026-08-13T00:00:00+00:00",
    "MS": "2026-08-14T00:00:00+00:00",
}

# Variant C weights, derived from the 2026-08-13 quintile decomposition.
# Components measured INVERTED enter as (100 - value); the conflict ratio —
# measured as the strongest positive predictor and currently penalized —
# enters positively. All inputs are 0-100 after normalization.
#
# This is IN-SAMPLE, REPLAY-DERIVED calibration. Its job is to be a
# falsifiable candidate in shadow, not to be believed.
C_WEIGHTS = {
    "volatility":       (0.20, False),   # strongest measured positive (+1.051)
    "conflict_pct":     (0.15, False),   # +0.758 measured, live formula fines it
    "ta_confluence":    (0.15, True),    # -1.035 measured, live's largest weight
    "calibrated_confidence": (0.12, False),  # flat but calibration-backed
    "rr":               (0.10, False),
    "regime":           (0.08, True),    # -0.459 measured
    "news":             (0.05, True),    # -0.429 measured
    "freshness":        (0.05, True),    # -0.244 measured
    "data_quality":     (0.05, False),
    "liquidity":        (0.05, False),
}
assert abs(sum(w for w, _ in C_WEIGHTS.values()) - 1.0) < 1e-9


def _f(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _components(breakdown: dict) -> dict | None:
    """Normalize a stored score_breakdown into 0-100 component values.

    Returns None when the breakdown is unusable — a variant computed from
    defaults would be noise wearing a version tag.
    """
    if not isinstance(breakdown, dict) or not breakdown:
        return None
    out = {}
    for key in ("ta_confluence", "regime", "volatility", "volume", "news",
                "freshness", "data_quality", "liquidity", "rr",
                "calibrated_confidence"):
        v = _f(breakdown.get(key))
        if v is not None:
            out[key] = max(0.0, min(100.0, v))
    cr = _f(breakdown.get("conflict_ratio"))
    if cr is not None:
        out["conflict_pct"] = max(0.0, min(100.0, cr * 100.0))
    # Enough signal to be meaningful: the three components the decomposition
    # turned on must all be present, else this row can't test the thesis.
    if not {"ta_confluence", "volatility", "conflict_pct"} <= set(out):
        return None
    return out


def variant_b(composite: float | None) -> float | None:
    """100 - live score. A diagnostic, not a proposal: if B selects better
    than A at the same gate, the inversion is confirmed end-to-end. If both
    select badly, the score is uninformative rather than inverted — a
    different conclusion demanding a different fix."""
    c = _f(composite)
    return round(100.0 - c, 2) if c is not None else None


def variant_c(breakdown: dict) -> float | None:
    """Component-calibrated: measured-inverted components flipped, the
    penalized-but-healthy conflict ratio promoted, weights from evidence."""
    comps = _components(breakdown)
    if comps is None:
        return None
    score, used = 0.0, 0.0
    for key, (weight, flip) in C_WEIGHTS.items():
        v = comps.get(key)
        if v is None:
            continue
        score += ((100.0 - v) if flip else v) * weight
        used += weight
    if used < 0.6:
        # More than 40% of the weight mass missing — refuse rather than
        # renormalize a fragment into a confident-looking number.
        return None
    return round(score / used, 2)


# Variant MS — the §4.2 decomposition made falsifiable: a point-in-time
# evidence score built from CURRENT MARKET STATE ONLY. Two exclusions
# define it:
#   calibrated_confidence  outcome history — belongs to the statistical-
#                          edge layer (expectancy), and feeding it here AND
#                          into EV is the double-count §4.2 forbids
#   rr                     the proposal's level geometry — the same market
#                          must produce the same evidence score regardless
#                          of where someone drew entry/stop/target
# Orientation follows the same 2026-08-13 measurements as C (inverted
# components flipped); weights renormalized over the market-state subset.
MS_WEIGHTS = {
    "volatility":    (0.25, False),
    "conflict_pct":  (0.20, False),
    "ta_confluence": (0.20, True),
    "regime":        (0.10, True),
    "news":          (0.075, True),
    "freshness":     (0.075, True),
    "data_quality":  (0.05, False),
    "liquidity":     (0.05, False),
}
assert abs(sum(w for w, _ in MS_WEIGHTS.values()) - 1.0) < 1e-9


def variant_ms(breakdown: dict) -> float | None:
    """Market-state-only evidence score (§4.2 separation, run in shadow)."""
    comps = _components(breakdown)
    if comps is None:
        return None
    score, used = 0.0, 0.0
    for key, (weight, flip) in MS_WEIGHTS.items():
        v = comps.get(key)
        if v is None:
            continue
        score += ((100.0 - v) if flip else v) * weight
        used += weight
    if used < 0.6:
        return None
    return round(score / used, 2)


def compute_variants(composite: float | None, breakdown: dict) -> dict:
    """All shadow variants for one signal. A (the control) is the live
    composite itself and is stored by the signal row already."""
    return {
        "schema": VARIANT_SCHEMA_VERSION,
        "B": variant_b(composite),
        "C": variant_c(breakdown or {}),
        "MS": variant_ms(breakdown or {}),
    }


# ── Retrospective evaluation ─────────────────────────────────────────────────

def evaluate_variants(gate: float = 55.0, timeframe: str | None = None) -> dict:
    """How each variant WOULD have selected, over resolved outcomes.

    For every labelled outcome with a stored breakdown, recompute each
    variant and ask: had the execution gate used this variant at `gate`,
    what would the selected set's win rate, avg P&L and avg MFE have been?

    Everything here is in-sample and replay-weighted — it ranks variants
    for shadowing, it does not promote them. Promotion needs the shadow
    rows accumulating from today plus resolved outcomes that postdate the
    variant's definition.
    """
    import json as _json

    from sqlalchemy import text as _text

    from app.database import engine

    rows = []
    q = """
        SELECT s.composite_score, s.score_breakdown, o.pnl_pct, o.mfe_r,
               o.first_touch
        FROM trade_outcomes o
        JOIN trading_signals s ON s.id = o.signal_id
        WHERE o.mfe_r IS NOT NULL
          AND s.score_breakdown IS NOT NULL AND s.score_breakdown != ''
    """
    args = {}
    if timeframe:
        q += " AND o.timeframe = :tf"
        args["tf"] = timeframe
    with engine.connect() as c:
        for comp, bd, pnl, mfe, ft in c.execute(_text(q), args):
            try:
                bd = _json.loads(bd)
            except Exception:
                continue
            rows.append((_f(comp), bd, _f(pnl, 0.0), _f(mfe, 0.0), ft))

    def _score_set(selected):
        n = len(selected)
        if n == 0:
            return {"n": 0}
        wins = sum(1 for p, _m, _f_ in selected if p > 0)
        return {
            "n": n,
            "win_rate": round(100.0 * wins / n, 1),
            "avg_pnl_pct": round(sum(p for p, _m, _f_ in selected) / n, 3),
            "avg_mfe_r": round(sum(m for _p, m, _f_ in selected) / n, 3),
            "stop_first_pct": round(100.0 * sum(
                1 for _p, _m, f in selected if f == "STOP") / n, 1),
        }

    out = {"gate": gate, "timeframe": timeframe or "all",
           "total_outcomes": len(rows),
           "schema": VARIANT_SCHEMA_VERSION, "variants": {}}
    for name in ("A", "B", "C", "MS"):
        selected = []
        for comp, bd, pnl, mfe, ft in rows:
            if name == "A":
                v = comp
            elif name == "B":
                v = variant_b(comp)
            elif name == "C":
                v = variant_c(bd)
            else:
                v = variant_ms(bd)
            if v is not None and v >= gate:
                selected.append((pnl, mfe, ft))
        out["variants"][name] = _score_set(selected)
    return out
