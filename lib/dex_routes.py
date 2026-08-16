"""Multi-hop routes — because one pool is rarely the whole trade.

A modern DEX trade is TOKEN A -> USDC -> SOL -> TOKEN B, and pricing it as
a single pool gets two things wrong at once. It understates cost, because
every hop charges its own LP fee and takes its own impact. And it overstates
availability, because a route is only as deep as its THINNEST hop — a
$2M pool in front of a $30k pool is a $30k route, and sizing against the
first number puts most of the stake into the second pool's slippage.

COMPOUNDING, NOT ADDITION. Impact across hops multiplies:

    surviving = (1 - i1) * (1 - i2) * (1 - i3)

Adding the percentages is close enough at 0.1% per hop and badly wrong at
5%, which is exactly where the decision actually matters. Three 5% hops are
not 15% — they are 14.26%, and more importantly the error grows with the
number of hops, so the model gets worse precisely as routes get more
exotic.

CERTAINTY IS THE WEAKEST LINK. A route through one VERIFIED pool and one
MODELLED pool is a MODELLED route. Averaging the confidences would let a
well-measured first hop launder an unmeasured second one, and the size
that comes out the other side would be justified by evidence the route
does not have.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

ROUTE_MODEL_VERSION = "route_v1"

# Ordered worst-to-best, so `min` over a route gives the weakest link.
_CONFIDENCE_RANK = {"MODELLED_ESTIMATE": 0, "ASSUMED_BALANCED_POOL": 1,
                    "VERIFIED": 2}
_RANK_TO_CONFIDENCE = {v: k for k, v in _CONFIDENCE_RANK.items()}

# Each additional hop is another program that can revert, another account
# that must exist, and more compute. Routes are not free to lengthen.
PER_HOP_FAILURE_RISK = 0.005


@dataclass
class Hop:
    """One pool crossing inside a route."""
    venue: str | None = None
    pool: str | None = None
    pool_type: str = "CONSTANT_PRODUCT_AMM"
    input_mint: str | None = None
    output_mint: str | None = None
    input_amount: float = 0.0
    output_amount: float = 0.0
    fee_bps: int = 25
    fee_paid: float = 0.0
    impact_pct: float = 0.0
    depth_confidence: str = "MODELLED_ESTIMATE"
    reserve_in: float | None = None
    reserve_out: float | None = None


@dataclass
class DexRouteQuote:
    input_asset: str | None = None
    output_asset: str | None = None
    input_amount: float = 0.0
    expected_output_amount: float = 0.0
    minimum_output_amount: float = 0.0

    hops: list = field(default_factory=list)

    aggregate_price_impact_pct: float = 0.0
    lp_fees_usd: float = 0.0
    protocol_fees_usd: float = 0.0
    network_fee_sol: float = 0.0
    network_fee_usd: float = 0.0

    route_quality: str | None = None
    depth_confidence: str = "MODELLED_ESTIMATE"
    thinnest_hop_usd: float | None = None
    failure_risk_pct: float = 0.0

    slippage_tolerance_pct: float = 1.0
    quote_timestamp: str | None = None
    route_model_version: str = ROUTE_MODEL_VERSION
    provenance: dict = field(default_factory=dict)
    ok: bool = True
    reason: str | None = None

    def as_dict(self) -> dict:
        from dataclasses import asdict
        d = asdict(self)
        d["hop_count"] = len(self.hops)
        return d


def route_confidence(hops: list) -> str:
    """The WEAKEST hop's certainty governs the whole route.

    Averaging would let a VERIFIED first hop launder a MODELLED second
    one, and the size permitted downstream would rest on evidence the
    route does not actually have.
    """
    if not hops:
        return "MODELLED_ESTIMATE"
    return _RANK_TO_CONFIDENCE[min(
        _CONFIDENCE_RANK.get(getattr(h, "depth_confidence", None), 0)
        for h in hops)]


def quote_route(hops: list, input_amount: float, *,
                slippage_tolerance_pct: float = 1.0,
                sol_price_usd: float = 0.0,
                priority_lamports: int = 0,
                quote_timestamp: str | None = None) -> DexRouteQuote:
    """Walk the hops in order, compounding impact and fees.

    Each hop's OUTPUT is the next hop's INPUT, so a thin pool in the middle
    degrades everything after it — which is the behaviour that makes a
    route quote worth having over a per-pool one.
    """
    from lib.dex_swap_math import (BASE_FEE_LAMPORTS, LAMPORTS_PER_SOL,
                                   quote_swap_native)

    if not hops:
        return DexRouteQuote(ok=False, reason="no hops in route")
    amount = float(input_amount or 0)
    if amount <= 0:
        return DexRouteQuote(ok=False, reason="amount must be positive")

    priced, surviving, lp_fees = [], 1.0, 0.0
    thinnest = None

    for h in hops:
        if not h.reserve_in or not h.reserve_out:
            return DexRouteQuote(
                ok=False,
                reason=(f"hop {h.venue or '?'} has no reserves — a route "
                        f"cannot be priced through a pool whose depth is "
                        f"unknown"))
        q = quote_swap_native(amount, h.reserve_in, h.reserve_out,
                              fee_bps=h.fee_bps, priority_lamports=0)
        if not q.get("ok"):
            return DexRouteQuote(ok=False,
                                 reason=f"hop {h.venue or '?'}: {q.get('reason')}")

        h.input_amount = amount
        h.output_amount = q["tokens_out"]
        h.fee_paid = q["pool_fee_tokens_in"]
        h.impact_pct = q["price_impact_pct"]
        lp_fees += q["pool_fee_tokens_in"]

        # COMPOUNDING. Adding percentages is close at 0.1% and wrong at 5%.
        surviving *= (1.0 - q["price_impact_pct"] / 100.0)

        # A route is only as deep as its thinnest hop.
        depth = float(h.reserve_in or 0)
        thinnest = depth if thinnest is None else min(thinnest, depth)

        amount = q["tokens_out"]
        priced.append(h)

    lamports = BASE_FEE_LAMPORTS + max(0, int(priority_lamports))
    net_sol = lamports / LAMPORTS_PER_SOL
    agg_impact = (1.0 - surviving) * 100.0

    # Each hop is another program that can revert.
    failure_risk = (1.0 - (1.0 - PER_HOP_FAILURE_RISK) ** len(priced)) * 100.0

    conf = route_confidence(priced)
    return DexRouteQuote(
        input_asset=priced[0].input_mint,
        output_asset=priced[-1].output_mint,
        input_amount=float(input_amount),
        expected_output_amount=amount,
        # What the trade is allowed to come back with. Below this it
        # reverts — and reverting still costs gas.
        minimum_output_amount=amount * (1.0 - slippage_tolerance_pct / 100.0),
        hops=priced,
        aggregate_price_impact_pct=round(agg_impact, 4),
        lp_fees_usd=lp_fees,
        network_fee_sol=round(net_sol, 9),
        network_fee_usd=round(net_sol * float(sol_price_usd or 0), 6),
        depth_confidence=conf,
        thinnest_hop_usd=thinnest,
        failure_risk_pct=round(failure_risk, 4),
        route_quality=_quality(agg_impact, len(priced), conf),
        slippage_tolerance_pct=slippage_tolerance_pct,
        quote_timestamp=quote_timestamp,
        provenance={"hops": len(priced), "compounded_impact": True,
                    "confidence_rule": "weakest hop governs"},
    )


def _quality(impact_pct: float, hops: int, confidence: str) -> str:
    if impact_pct >= 10.0 or confidence == "MODELLED_ESTIMATE" and impact_pct >= 5.0:
        return "POOR"
    if impact_pct >= 3.0 or hops >= 4:
        return "FAIR"
    if impact_pct >= 1.0 or hops >= 3:
        return "GOOD"
    return "EXCELLENT"


def best_route(routes: list) -> DexRouteQuote | None:
    """Most output wins — but a route whose depth is MODELLED must beat a
    measured one by more than the uncertainty it carries.

    Otherwise the router systematically prefers whichever pool it knows
    least about, because an unmeasured pool's optimistic depth estimate
    always quotes the best price. That is selection bias with a bias
    toward being wrong.
    """
    from lib.dex_swap_math import DEPTH_IMPACT_MULTIPLIER

    usable = [r for r in routes or [] if r is not None and r.ok
              and r.expected_output_amount > 0]
    if not usable:
        return None

    def _risk_adjusted(r: DexRouteQuote) -> float:
        mult = DEPTH_IMPACT_MULTIPLIER.get(r.depth_confidence, 2.5)
        # Charge the uncertainty as extra impact against the output.
        penalty = (r.aggregate_price_impact_pct / 100.0) * (mult - 1.0)
        return r.expected_output_amount * (1.0 - penalty) \
            * (1.0 - r.failure_risk_pct / 100.0)

    return max(usable, key=_risk_adjusted)
