"""Context ablation — does the macro context a candidate was born under
predict how it resolved?

The instrument is built BEFORE the data matures, like the promotion
framework and the parity gate before it: it runs continuously, reports
honestly-empty today, and fills itself as counterfactually-resolved
candidates with stored context accumulate. Nothing here influences
trading; a context feature earns its way into a gate only by surviving
this table with sample sizes a skeptic would accept.

Design rules:

  joined at birth   reads ONLY candidate_signals.market_context, stored
                    at judgment time — recomputing context for old rows
                    would inject hindsight and is not implemented
  direction split   crowded-long positioning plausibly hurts longs and
                    helps shorts; pooling directions would average the
                    effect away, so every bucket reports per side
  thin is thin      buckets under MIN_N carry thin=True and a skeptic
                    should read them as anecdotes, not evidence
  hypotheses named  every feature states what would confirm it, so the
                    table can KILL ideas, not just flatter them
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

MIN_N = 25

# (feature, json path, hypothesis, bucket CASE expression)
# Buckets are coarse on purpose: three cells fill three ways faster than
# ten, and the hypotheses are directional, not curve-fitting exercises.
FEATURES = [
    ("cot_spec_pctile_3y",
     "speculator crowding: setups born at positioning extremes (>80th "
     "pctile crowded-long, <20th washed-out) resolve differently than "
     "mid-range",
     "CASE WHEN v < 20 THEN 'washed_out(<20)' "
     "WHEN v > 80 THEN 'crowded(>80)' ELSE 'mid(20-80)' END"),
    ("funding_rate",
     "perp carry: positive funding means longs pay to hold — longs born "
     "into expensive carry should underperform",
     "CASE WHEN v < 0 THEN 'negative' "
     "WHEN v > 0.0001 THEN 'rich(>1bp)' ELSE 'near_zero' END"),
    ("long_short_ratio",
     "retail account skew: extreme long-side account ratios mark "
     "crowd-consensus entries",
     "CASE WHEN v < 1.0 THEN 'net_short(<1)' "
     "WHEN v > 2.0 THEN 'heavy_long(>2)' ELSE 'balanced(1-2)' END"),
    ("curve_structure",
     "term structure: backwardation pays longs to hold futures, contango "
     "charges them",
     "v"),
    ("finra_short_ratio",
     "short-sale pressure: elevated daily short ratio marks supply that "
     "either caps rallies or fuels squeezes",
     "CASE WHEN v < 0.4 THEN 'light(<40%)' "
     "WHEN v > 0.6 THEN 'heavy(>60%)' ELSE 'normal(40-60%)' END"),
    ("eia_change_z",
     "inventory surprise: multi-sigma builds/draws reprice energy — "
     "setups born right after them live in a different regime",
     "CASE WHEN v > 1.0 THEN 'big_build(z>1)' "
     "WHEN v < -1.0 THEN 'big_draw(z<-1)' ELSE 'normal' END"),
]


def ablation_summary() -> dict:
    """Per-feature, per-bucket, per-direction resolution stats over
    counterfactually resolved candidates that carry stored context."""
    from sqlalchemy import text

    from app.database import engine

    out: dict = {"min_n": MIN_N, "features": [], "coverage": {}}
    with engine.connect() as c:
        cov = c.execute(text("""
            SELECT
              SUM(CASE WHEN market_context IS NOT NULL THEN 1 ELSE 0 END),
              COUNT(*)
            FROM candidate_signals WHERE resolved = 1
        """)).fetchone()
        out["coverage"] = {"resolved_with_context": cov[0] or 0,
                           "resolved_total": cov[1] or 0}

        for feature, hypothesis, bucket_case in FEATURES:
            rows = c.execute(text(f"""
                WITH ctx AS (
                    SELECT direction,
                           json_extract(market_context, '$.{feature}') AS v,
                           pnl_pct, mfe_r
                    FROM candidate_signals
                    WHERE resolved = 1 AND market_context IS NOT NULL
                      AND json_extract(market_context, '$.{feature}')
                          IS NOT NULL
                )
                SELECT {bucket_case} AS bucket, direction, COUNT(*),
                       ROUND(AVG(CASE WHEN pnl_pct > 0 THEN 100.0
                                      ELSE 0 END), 1),
                       ROUND(AVG(pnl_pct), 3), ROUND(AVG(mfe_r), 3)
                FROM ctx
                GROUP BY bucket, direction
                ORDER BY bucket, direction
            """)).fetchall()
            buckets = [{"bucket": b, "direction": d, "n": n,
                        "win_rate": wr, "avg_pnl_pct": pnl,
                        "avg_mfe_r": mfe, "thin": n < MIN_N}
                       for b, d, n, wr, pnl, mfe in rows]
            out["features"].append({
                "feature": feature,
                "hypothesis": " ".join(hypothesis.split()),
                "buckets": buckets,
            })

    out["note"] = ("shadow-only; a feature earns gate influence by "
                   "surviving this table at real sample sizes, and thin "
                   "buckets are anecdotes, not evidence")
    return out
