"""Was the SIGNAL bad, or was the VENUE wrong?

A thesis can have real gross edge and still be untradeable on one venue
while remaining comfortably tradeable on another. The same BTC long is a
different economic proposition as a CEX perp with 4bp taker fees and as a
DEX swap with 3% price impact — and if the desk only records "this setup
lost money", it retires a strategy whose actual fault was routing.

    gross edge 0.40R
      - CEX cost 0.08R  ->  net +0.32R   TRADEABLE
      - DEX cost 0.55R  ->  net -0.15R   UNTRADEABLE_ON_DEX

Those are one signal and two conclusions. Reporting them as one verdict
throws away the more useful half.

HIERARCHICAL FALLBACK, NEVER A LEAP. When a specific bucket is too thin to
measure, widen one step at a time:

    strategy + BTC perp + 15m
        -> strategy + crypto perp + 15m
        -> crypto perp + 15m
        -> strategy + crypto perp
        -> compatible products

Never jump from three BTC perp trades to "all markets". A memecoin DEX
swap and a gold future do not inform each other, and pooling them produces
a number that describes nothing while looking like evidence.
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Below this a bucket cannot support a verdict of its own.
MIN_SAMPLE = 8
# Net R a venue must clear to be called tradeable.
MIN_NET_R = 0.05

TRADEABLE = "TRADEABLE"
UNTRADEABLE = "UNTRADEABLE"
UNKNOWN = "UNKNOWN"


@dataclass
class VenueVerdict:
    venue_type: str
    product: str | None = None
    gross_r: float | None = None
    cost_r: float | None = None
    net_r: float | None = None
    n: int = 0
    verdict: str = UNKNOWN
    level_used: str | None = None
    reason: str | None = None

    @property
    def edge_cost_ratio(self) -> float | None:
        """How much edge exists per unit of cost. A ratio near 1 means the
        venue eats the whole trade even when the thesis is right."""
        if self.gross_r is None or not self.cost_r:
            return None
        return self.gross_r / self.cost_r


def _levels(strategy, product, timeframe, asset_class) -> list:
    """Fallback ladder, most specific first. Each step widens ONE axis."""
    return [
        ("strategy+product+timeframe", (strategy, product, timeframe)),
        ("strategy+class+timeframe", (strategy, asset_class, timeframe)),
        ("class+timeframe", (None, asset_class, timeframe)),
        ("strategy+class", (strategy, asset_class, None)),
        ("class", (None, asset_class, None)),
    ]


def _matches(row, strategy, klass, timeframe) -> bool:
    if strategy is not None and row.get("strategy") != strategy:
        return False
    if klass is not None and row.get("asset_class") != klass \
            and row.get("product") != klass:
        return False
    if timeframe is not None and row.get("timeframe") != timeframe:
        return False
    return True


def venue_verdict(rows: list, *, venue_type: str, strategy: str | None = None,
                  product: str | None = None, timeframe: str | None = None,
                  asset_class: str | None = None,
                  min_sample: int = MIN_SAMPLE) -> VenueVerdict:
    """Net expectancy for one venue, using the narrowest bucket that has
    enough evidence to say anything.

    Returns UNKNOWN rather than borrowing an unrelated population. A
    verdict assembled from incompatible products is not a cautious
    estimate — it is a confident wrong answer.
    """
    pool = [r for r in rows or []
            if r is not None and r.get("venue_type") == venue_type]
    if not pool:
        return VenueVerdict(venue_type=venue_type, product=product,
                            verdict=UNKNOWN,
                            reason=f"no outcomes recorded on {venue_type}")

    for level, (s, k, tf) in _levels(strategy, product, timeframe, asset_class):
        subset = [r for r in pool if _matches(r, s, k, tf)]
        if len(subset) < min_sample:
            continue
        gross = statistics.fmean(
            [float(r.get("gross_r") or 0.0) for r in subset])
        cost = statistics.fmean(
            [abs(float(r.get("cost_r") or 0.0)) for r in subset])
        net = gross - cost
        return VenueVerdict(
            venue_type=venue_type, product=product,
            gross_r=round(gross, 4), cost_r=round(cost, 4), net_r=round(net, 4),
            n=len(subset), level_used=level,
            verdict=TRADEABLE if net >= MIN_NET_R else UNTRADEABLE,
            reason=(f"{len(subset)} outcomes at {level}: {gross:+.3f}R gross "
                    f"less {cost:.3f}R cost = {net:+.3f}R net"))

    return VenueVerdict(
        venue_type=venue_type, product=product, verdict=UNKNOWN,
        n=len(pool),
        reason=(f"only {len(pool)} {venue_type} outcomes, and no bucket "
                f"reached {min_sample} — widening further would pool "
                f"incompatible products"))


def compare_venues(rows: list, **kw) -> dict:
    """The question a single verdict cannot answer: bad signal or bad venue?

    When one venue is tradeable and another is not, the lesson is ROUTING
    — and a desk that only sees the blended result will retire the setup
    instead of moving it.
    """
    venues = {r.get("venue_type") for r in rows or [] if r}
    venues.discard(None)
    verdicts = {v: venue_verdict(rows, venue_type=v, **kw) for v in sorted(venues)}

    tradeable = [v for v, d in verdicts.items() if d.verdict == TRADEABLE]
    untradeable = [v for v, d in verdicts.items() if d.verdict == UNTRADEABLE]

    best = None
    scored = [(v, d) for v, d in verdicts.items() if d.net_r is not None]
    if scored:
        best = max(scored, key=lambda kv: kv[1].net_r)[0]

    if tradeable and untradeable:
        lesson = "BAD_VENUE"
        detail = (f"the thesis clears on {', '.join(tradeable)} and not on "
                  f"{', '.join(untradeable)} — this is a routing result, "
                  f"not a verdict on the setup")
    elif untradeable and not tradeable:
        lesson = "NO_EDGE_ANYWHERE"
        detail = ("no venue clears the bar — the cost model is not the "
                  "problem here")
    elif tradeable:
        lesson = "TRADEABLE"
        detail = f"clears on {', '.join(tradeable)}"
    else:
        lesson = "INSUFFICIENT_EVIDENCE"
        detail = "no venue has enough outcomes to judge"

    return {
        "verdicts": {v: d.__dict__ for v, d in verdicts.items()},
        "best_venue": best,
        "lesson": lesson,
        "detail": detail,
        "edge_cost_ratio": {v: d.edge_cost_ratio for v, d in verdicts.items()},
    }


# ── Edge decomposition (Phase 21) ────────────────────────────────────────

def decompose_edge(paired: list) -> dict:
    """SELECTION, EXECUTION and MANAGEMENT, from paired thesis outcomes.

    A single blended P&L answers none of these. Separating them is what
    turns "the portfolio went up" into "the AI's sizing helped and its
    entry review did not", which is the only form of feedback that can
    actually change what the system does next.
    """
    rows = [p for p in paired or [] if p]
    if not rows:
        return {"n": 0, "detail": "no paired outcomes yet"}

    deltas = [float(p["delta_net_r"]) for p in rows if p.get("delta_net_r") is not None]
    exec_d = [float(p["execution_delta_r"]) for p in rows
              if p.get("execution_delta_r") is not None]
    sel_d = [float(p["selection_delta_r"]) for p in rows
             if p.get("selection_delta_r") is not None]

    def _stat(vals):
        if not vals:
            return None
        return {"mean": round(statistics.fmean(vals), 4),
                "median": round(statistics.median(vals), 4),
                "n": len(vals),
                # Wins alone are not evidence of skill; consistency is.
                "positive_pct": round(
                    sum(1 for v in vals if v > 0) / len(vals) * 100.0, 1)}

    total = _stat(deltas)
    return {
        "n": len(rows),
        "market_samples": len({p.get("thesis_id") for p in rows}),
        "total_delta_r": total,
        "selection_delta_r": _stat(sel_d),
        "execution_delta_r": _stat(exec_d),
        # Whatever the total gain is not explained by selection or
        # execution came from what happened AFTER entry.
        "management_delta_r": _stat([
            d - (s or 0.0) - (e or 0.0)
            for d, s, e in zip(deltas,
                               sel_d + [0.0] * (len(deltas) - len(sel_d)),
                               exec_d + [0.0] * (len(deltas) - len(exec_d)))
        ]) if deltas else None,
        "verdict": (
            "AGENT_ADDS_VALUE" if total and total["mean"] > 0 else
            "AGENT_SUBTRACTS_VALUE" if total and total["mean"] < 0 else
            "NO_MEASURED_DIFFERENCE"),
        "note": ("market_samples counts distinct theses. Paired differences "
                 "remove the market's own variance, so what remains is "
                 "policy value."),
    }
