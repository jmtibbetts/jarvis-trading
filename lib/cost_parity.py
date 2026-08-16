"""What the gate ASSUMED a trade would cost, against what it actually cost.

THE COST MODEL USED BY THE GATE MUST BE THE COST MODEL REALIZED BY THE
VIRTUAL EXCHANGE. It is invalid for the gate to refuse a trade on
spread + slippage + commission + funding while the book then records
commission only — the desk would be selecting against costs it never pays
and rejecting trades that were fine.

But parity is not the whole point. Keeping BOTH numbers lets JARVIS answer
a question a single number cannot:

    the signal was good and the cost estimate was poor
                        vs
    the signal had no edge

Those need opposite responses. The first says fix the cost model or the
venue; the second says stop taking the setup. A desk that only records
realized cost cannot tell them apart, and will happily retire a profitable
strategy because it was priced badly.

Every component is compared separately, because they fail for different
reasons: spread is the venue, slippage is your own size and urgency,
funding is the hold, and impact is the pool. A single aggregate error
hides which one is wrong.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

COST_MODEL_VERSION = "cost_parity_v1"

# Components compared. Named explicitly so a new cost cannot be added to
# one side of the comparison and silently skipped on the other.
COMPONENTS = ("spread", "slippage", "impact", "commission", "fees",
              "funding", "borrow", "network", "pool")

# Beyond this relative error the estimate is treated as materially wrong
# rather than noisy — reported, never auto-corrected.
MATERIAL_ERROR_PCT = 25.0


@dataclass
class CostComparison:
    """One trade's estimated vs realized costs, component by component."""
    thesis_id: str | None = None
    signal_id: str | None = None
    symbol: str | None = None
    product: str | None = None
    venue_type: str | None = None

    estimated: dict = field(default_factory=dict)
    realized: dict = field(default_factory=dict)

    estimated_total_usd: float = 0.0
    realized_total_usd: float = 0.0

    initial_risk_usd: float | None = None
    estimated_total_r: float | None = None
    realized_total_r: float | None = None

    cost_model_version: str = COST_MODEL_VERSION

    @property
    def error_usd(self) -> float:
        """Positive means the trade cost MORE than the gate assumed."""
        return self.realized_total_usd - self.estimated_total_usd

    @property
    def error_pct(self) -> float | None:
        if not self.estimated_total_usd:
            return None
        return self.error_usd / abs(self.estimated_total_usd) * 100.0

    @property
    def materially_wrong(self) -> bool:
        e = self.error_pct
        return e is not None and abs(e) > MATERIAL_ERROR_PCT

    @property
    def underestimated(self) -> bool:
        """The dangerous direction: the gate let trades through that could
        not pay for themselves."""
        return self.error_usd > 0

    def by_component(self) -> dict:
        """Per-component error, so the wrong one can be identified.

        An aggregate that is 30% off tells you nothing actionable. Knowing
        it is entirely funding on a multi-day hold, or entirely impact on
        one thin pool, tells you exactly what to fix.
        """
        out = {}
        for c in COMPONENTS:
            est = float(self.estimated.get(c) or 0.0)
            real = float(self.realized.get(c) or 0.0)
            if est == 0.0 and real == 0.0:
                continue
            out[c] = {
                "estimated_usd": est, "realized_usd": real,
                "error_usd": real - est,
                "error_pct": ((real - est) / abs(est) * 100.0) if est else None,
            }
        return out

    def as_dict(self) -> dict:
        return {
            "thesis_id": self.thesis_id, "signal_id": self.signal_id,
            "symbol": self.symbol, "product": self.product,
            "venue_type": self.venue_type,
            "estimated_total_usd": self.estimated_total_usd,
            "realized_total_usd": self.realized_total_usd,
            "estimated_total_r": self.estimated_total_r,
            "realized_total_r": self.realized_total_r,
            "error_usd": self.error_usd, "error_pct": self.error_pct,
            "underestimated": self.underestimated,
            "materially_wrong": self.materially_wrong,
            "components": self.by_component(),
            "cost_model_version": self.cost_model_version,
        }


def _total(d: dict) -> float:
    return sum(float(d.get(c) or 0.0) for c in COMPONENTS)


def compare(*, estimated: dict, realized: dict,
            initial_risk_usd: float | None = None, **meta) -> CostComparison:
    """Build the comparison. Neither side is allowed to be implicit.

    A component missing from `realized` is treated as ZERO REALIZED, not as
    "same as estimated" — the whole purpose is to catch a cost the gate
    charged and the book then forgot to.
    """
    c = CostComparison(
        estimated={k: float(v or 0.0) for k, v in (estimated or {}).items()
                   if k in COMPONENTS},
        realized={k: float(v or 0.0) for k, v in (realized or {}).items()
                  if k in COMPONENTS},
        initial_risk_usd=initial_risk_usd,
        **{k: v for k, v in meta.items()
           if k in CostComparison.__dataclass_fields__},
    )
    c.estimated_total_usd = _total(c.estimated)
    c.realized_total_usd = _total(c.realized)
    if initial_risk_usd:
        c.estimated_total_r = c.estimated_total_usd / float(initial_risk_usd)
        c.realized_total_r = c.realized_total_usd / float(initial_risk_usd)
    return c


def realized_from_outcome(outcome) -> dict:
    """Pull realized costs out of a RealizedOutcome, in comparison shape.

    Attribution AND explicit charges are both included here, because the
    GATE estimated the full economic cost of trading — spread and slippage
    included. Comparing its estimate against ledger charges alone would
    make every estimate look wildly pessimistic and push the desk to
    loosen a gate that was right.
    """
    return {
        "spread": abs(float(getattr(outcome, "spread_attribution_usd", 0.0) or 0.0)),
        "slippage": abs(float(getattr(outcome, "slippage_attribution_usd", 0.0) or 0.0)),
        "impact": abs(float(getattr(outcome, "price_impact_attribution_usd", 0.0) or 0.0)),
        "commission": abs(float(getattr(outcome, "commission_usd", 0.0) or 0.0)),
        "fees": abs(float(getattr(outcome, "regulatory_fees_usd", 0.0) or 0.0)),
        # Funding is a TRANSFER and can be negative — a short receiving it
        # genuinely reduces the cost of the trade, so this one keeps its
        # sign where every other component takes an absolute value.
        "funding": float(getattr(outcome, "funding_usd", 0.0) or 0.0),
        "borrow": abs(float(getattr(outcome, "borrow_cost_usd", 0.0) or 0.0)),
        "network": abs(float(getattr(outcome, "network_fees_usd", 0.0) or 0.0)),
        "pool": abs(float(getattr(outcome, "pool_fees_usd", 0.0) or 0.0)),
    }


def summarize(comparisons: list) -> dict:
    """Aggregate parity across trades — is the gate systematically wrong?

    Reports the MEDIAN alongside the mean: a handful of gap fills or one
    thin pool will drag a mean far enough to look like a broken model when
    the typical trade is priced correctly.
    """
    import statistics

    rows = [c for c in comparisons if c is not None]
    if not rows:
        return {"n": 0, "detail": "no comparable trades yet"}

    errs = [c.error_usd for c in rows]
    under = sum(1 for c in rows if c.underestimated)
    material = sum(1 for c in rows if c.materially_wrong)

    per_component: dict = {}
    for c in rows:
        for name, d in c.by_component().items():
            per_component.setdefault(name, []).append(d["error_usd"])

    return {
        "n": len(rows),
        "mean_error_usd": statistics.fmean(errs),
        "median_error_usd": statistics.median(errs),
        "underestimated_trades": under,
        "underestimated_pct": under / len(rows) * 100.0,
        "materially_wrong_trades": material,
        "worst_component": (max(per_component,
                                key=lambda k: abs(statistics.fmean(per_component[k])))
                            if per_component else None),
        "component_median_error_usd": {
            k: statistics.median(v) for k, v in per_component.items()},
        "cost_model_version": COST_MODEL_VERSION,
    }
