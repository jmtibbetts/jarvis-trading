"""The decision pipeline's types — so a field can never mean three things.

The costliest bugs this codebase has produced were not algorithmic; they
were SEMANTIC. One dict field named "confidence" carried the LLM's
self-report in one module, the composite score in another, and was clamped
into a Kelly win probability in a third — a number measured INVERTED
against outcomes, bet as p(win). A module should never infer probability
from an arbitrary score merely because both happen to be 0-100.

These types make the four concerns structurally distinct (implementation
plan §1/§3):

    ObservedEvidence   what is true NOW (market state; no history)
    MeasuredEdge       what HISTORY says about comparable setups
    RiskDecision       what the ACCOUNT can afford (no opinions)
    OrderPlan          how the trade is actually placed (venue facts)
    TradeDecision      the verdict that binds them, with reasons

All frozen: a decision object is a record of what was decided, not a
scratchpad. Constructors accept the existing dict shapes so migration can
proceed joint by joint — the dicts die at the edges, not everywhere at
once.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


def _f(v, default=None):
    try:
        f = float(v)
        return f if f == f else default
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class ObservedEvidence:
    """Point-in-time market state. NO outcome history belongs here —
    mixing history into evidence is how double counting starts (§4.2)."""
    symbol: str
    asset_class: str | None = None
    side: str | None = None                 # strict-parsed; None = unparseable
    timeframe: str | None = None
    entry: float | None = None
    stop: float | None = None
    target: float | None = None
    data_quality: float | None = None
    freshness: float | None = None
    evidence_composite: float | None = None  # diagnostic ONLY; measured inverted
    llm_stated_confidence: float | None = None  # the model's self-report; no authority
    strategy: str | None = None

    @classmethod
    def from_signal(cls, sig: dict) -> "ObservedEvidence":
        from lib.trade_side import parse_side_strict
        return cls(
            symbol=str(sig.get("asset_symbol") or sig.get("symbol") or ""),
            asset_class=sig.get("asset_class"),
            side=parse_side_strict(sig.get("direction")),
            timeframe=sig.get("timeframe"),
            entry=_f(sig.get("entry_price")),
            stop=_f(sig.get("stop_loss")),
            target=_f(sig.get("target_price")),
            data_quality=_f(sig.get("data_quality_score")),
            freshness=_f(sig.get("freshness_score")),
            evidence_composite=_f(sig.get("composite_score")),
            llm_stated_confidence=_f(sig.get("confidence")),
            strategy=sig.get("strategy"),
        )


@dataclass(frozen=True)
class MeasuredEdge:
    """What outcome history says. Every number here is measured, sampled,
    and interval-bounded — nothing is a model's self-belief."""
    bucket: str | None = None
    sample: float | None = None             # replay-weighted
    raw_sample: int | None = None
    p_win: float | None = None
    p_win_lower: float | None = None        # Wilson lower bound
    p_win_upper: float | None = None
    avg_win_r: float | None = None
    avg_loss_r: float | None = None
    gross_expected_r: float | None = None
    expected_cost_r: float | None = None
    net_expected_r: float | None = None
    net_expected_r_lower: float | None = None
    robust: bool = False                     # lower bound clears the bar too
    verdict: str = "UNKNOWN"                 # TRADE | NO_TRADE | UNKNOWN
    reason: str | None = None

    @classmethod
    def from_expectancy(cls, ev: dict) -> "MeasuredEdge":
        stats = ev.get("expectancy") or {}
        net = ev.get("net") or {}
        net_lower = ev.get("net_lower") or {}
        ci = stats.get("p_win_ci") or [None, None]
        return cls(
            bucket=stats.get("bucket"),
            sample=_f(stats.get("sample")),
            raw_sample=int(stats["raw_sample"]) if stats.get("raw_sample") is not None else None,
            p_win=_f(stats.get("p_win")),
            p_win_lower=_f(ci[0]),
            p_win_upper=_f(ci[1]),
            avg_win_r=_f(stats.get("avg_win_r")),
            avg_loss_r=_f(stats.get("avg_loss_r")),
            gross_expected_r=_f(stats.get("gross_expected_r")),
            expected_cost_r=_f(net.get("expected_cost_r")),
            net_expected_r=_f(net.get("net_expected_r")),
            net_expected_r_lower=_f(net_lower.get("net_expected_r")),
            robust=bool(ev.get("robust")),
            verdict=str(ev.get("verdict") or "UNKNOWN"),
            reason=ev.get("reason"),
        )


@dataclass(frozen=True)
class RiskDecision:
    """What the account can afford. Inputs are account state and the
    frozen stop; opinions (scores, confidence) are structurally absent."""
    allowed_risk_usd: float
    stop_distance: float
    qty: float
    notional: float
    margin: float
    leverage: float
    lifecycle_multiplier: float = 1.0
    limiting_constraint: str | None = None   # risk | cash | venue | liquidation | notional-cap
    rejected: bool = False
    rejection_reason: str | None = None

    @classmethod
    def rejection(cls, reason: str) -> "RiskDecision":
        return cls(allowed_risk_usd=0.0, stop_distance=0.0, qty=0.0,
                   notional=0.0, margin=0.0, leverage=1.0,
                   rejected=True, rejection_reason=reason)


@dataclass(frozen=True)
class OrderPlan:
    """The trade as it will actually be placed — venue facts only.
    Execution may shrink a plan against the RiskDecision; nothing may
    enlarge one (invariant #10)."""
    symbol: str
    venue: str
    side: str                                # long | short
    order_type: str                          # market | limit
    qty: float
    entry: float
    initial_stop: float                      # AS PLACED — the immutable R basis
    target: float | None = None
    notional: float | None = None
    leverage: float = 1.0
    product: str | None = None               # spot | perp | equity | futures
    estimated_fees: float | None = None
    estimated_spread_pct: float | None = None

    def within(self, risk: RiskDecision, tol: float = 1e-6) -> bool:
        """The invariant, checkable at the last gate before submission."""
        if self.qty > risk.qty + tol:
            return False
        if risk.notional and self.notional and self.notional > risk.notional + tol:
            return False
        return True


@dataclass(frozen=True)
class TradeDecision:
    """The binding verdict. `decision` is the only field execution reads;
    everything else is the evidence trail that justifies it."""
    decision: str                            # TRADE | TENTATIVE | NO_TRADE | UNKNOWN
    reasons: tuple[str, ...] = field(default_factory=tuple)
    evidence: ObservedEvidence | None = None
    edge: MeasuredEdge | None = None
    risk: RiskDecision | None = None
    plan: OrderPlan | None = None

    @property
    def take(self) -> bool:
        return self.decision == "TRADE" and (self.risk is None or not self.risk.rejected)

    def to_dict(self) -> dict:
        return asdict(self)
