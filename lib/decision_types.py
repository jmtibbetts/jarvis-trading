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

    def check(self, risk: RiskDecision, tol: float = 1e-6) -> "RiskGateVerdict":
        """THE LAST GATE. Prove this order stays inside what was approved.

        The previous version compared qty and notional and nothing else,
        which let four separate classes of over-risk through. Measured
        against the real types:

            NaN qty                      PASSED  (NaN > x is False)
            NaN qty on a REJECTED risk   PASSED
            50x leverage vs approved 2x  PASSED  (leverage unchecked)
            stop widened 1.0 -> 50.0     PASSED  (stop risk unchecked)

        The NaN cases are the sharpest: every comparison against NaN is
        False, so a quantity that is not a number satisfies every ceiling
        by construction — including on a decision that was refused outright.

        A venue may SHRINK risk. It may never ENLARGE it. Anything this
        cannot verify is refused rather than assumed safe.
        """
        import math

        fails: list[str] = []

        # 1. A refusal is a refusal. Checked FIRST and explicitly: relying
        #    on rejected decisions carrying qty=0 made the guarantee an
        #    accident of their constructor rather than a rule.
        if risk.rejected:
            return RiskGateVerdict(
                False, ("risk decision was REJECTED"
                        f"{f': {risk.rejection_reason}' if risk.rejection_reason else ''}"
                        " — a refusal can never authorize an order",))

        # 2. Finiteness before every comparison, because NaN defeats them
        #    all silently and infinity is not a size.
        for name, value in (("quantity", self.qty), ("entry", self.entry),
                            ("initial_stop", self.initial_stop),
                            ("leverage", self.leverage),
                            ("notional", self.notional)):
            if value is None:
                continue
            if not math.isfinite(float(value)):
                fails.append(f"{name} is not a finite number ({value!r}) — "
                             f"every ceiling comparison against it is False")
        if fails:
            return RiskGateVerdict(False, tuple(fails))

        if self.qty <= 0:
            fails.append(f"quantity {self.qty} is not positive")
        if self.notional is not None and self.notional < 0:
            fails.append(f"notional {self.notional} is negative")

        # 3. Identity must be known. An unparseable side or product means
        #    the arithmetic below is being applied to something nobody has
        #    established the units of.
        from lib.trade_side import parse_side_strict
        if parse_side_strict(self.side) is None:
            fails.append(f"side {self.side!r} is not a canonical long/short")

        # 4. The approved ceilings.
        if self.qty > risk.qty + tol:
            fails.append(f"quantity {self.qty} exceeds approved {risk.qty}")
        if risk.notional and self.notional and self.notional > risk.notional + tol:
            fails.append(f"notional {self.notional} exceeds approved {risk.notional}")
        if risk.leverage and self.leverage > risk.leverage + tol:
            fails.append(f"leverage {self.leverage}x exceeds approved "
                         f"{risk.leverage}x")

        # 5. THE ONE THAT MATTERS MOST. Quantity alone does not bound loss:
        #    the same size on a wider stop risks more money. Priced through
        #    the canonical multiplier, because a 10-point MES stop is $50 of
        #    risk per contract and not $10.
        stop_distance = abs(float(self.entry) - float(self.initial_stop))
        multiplier: float | None = 1.0
        try:
            from lib.instruments import resolve
            ident = resolve(self.symbol, product=self.product)
            # `resolve` does NOT raise for an unspecced contract — it returns
            # an identity with status UNSUPPORTED and multiplier 1.0. Reading
            # the multiplier without checking `executable` reintroduces the
            # exact fail-open the instrument layer exists to prevent: 6J=F
            # would be risked as though one point were one dollar.
            if not ident.executable:
                fails.append(
                    f"instrument {self.symbol!r} is {ident.status}"
                    f"{f' — {ident.reason}' if ident.reason else ''}; its units "
                    f"are unknown, so risk at stop is not computable")
                multiplier = None
            else:
                multiplier = float(ident.multiplier or 1.0)
        except Exception as e:
            fails.append(f"instrument {self.symbol!r} could not be resolved "
                         f"({e}) — risk at stop is not computable")
            multiplier = None

        if multiplier is not None:
            if risk.stop_distance and stop_distance > risk.stop_distance + tol:
                fails.append(
                    f"stop distance {stop_distance} is WIDER than the approved "
                    f"{risk.stop_distance} — same size, more money at risk")
            risk_at_stop = self.qty * stop_distance * multiplier
            if risk.allowed_risk_usd and risk_at_stop > risk.allowed_risk_usd + max(tol, risk.allowed_risk_usd * 1e-6):
                fails.append(
                    f"risk at stop ${risk_at_stop:,.2f} exceeds the approved "
                    f"${risk.allowed_risk_usd:,.2f}"
                    + (f" (x{multiplier:g} multiplier)" if multiplier != 1.0 else ""))

        return RiskGateVerdict(not fails, tuple(fails))

    def within(self, risk: RiskDecision, tol: float = 1e-6) -> bool:
        """The invariant, checkable at the last gate before submission."""
        return self.check(risk, tol=tol).ok


@dataclass(frozen=True)
class RiskGateVerdict:
    """Why an order was refused, not merely that it was.

    A boolean at the capital boundary tells the operator nothing about
    which ceiling was breached, and "the order was rejected" is the least
    actionable sentence a risk system can produce.
    """
    ok: bool
    failures: tuple[str, ...] = field(default_factory=tuple)

    @property
    def reason(self) -> str | None:
        return "; ".join(self.failures) if self.failures else None


def normalized_stays_within(original: OrderPlan, normalized: OrderPlan,
                            risk: RiskDecision,
                            tol: float = 1e-6) -> RiskGateVerdict:
    """Re-check AFTER a venue has normalized the order.

    Venues round to quantity steps, whole contracts and minimum sizes. That
    rounding has a direction, and only one of them is safe:

        approved BTC qty 0.006, venue minimum 0.010  ->  REFUSE

    Submitting the venue's minimum because it was "close" takes 67% more
    risk than was authorized. A venue may shrink an order; it may never
    enlarge one, and the check must run again on what will actually be
    placed rather than on what was planned.
    """
    verdict = normalized.check(risk, tol=tol)
    if not verdict.ok:
        return verdict

    fails: list[str] = []
    if normalized.qty > original.qty + tol:
        fails.append(f"venue normalization ENLARGED quantity "
                     f"{original.qty} -> {normalized.qty}")
    if (original.notional is not None and normalized.notional is not None
            and normalized.notional > original.notional + tol):
        fails.append(f"venue normalization ENLARGED notional "
                     f"{original.notional} -> {normalized.notional}")
    if normalized.leverage > original.leverage + tol:
        fails.append(f"venue normalization ENLARGED leverage "
                     f"{original.leverage}x -> {normalized.leverage}x")
    orig_stop = abs(float(original.entry) - float(original.initial_stop))
    norm_stop = abs(float(normalized.entry) - float(normalized.initial_stop))
    if norm_stop > orig_stop + tol:
        fails.append(f"venue normalization WIDENED the stop "
                     f"{orig_stop} -> {norm_stop}")
    return RiskGateVerdict(not fails, tuple(fails))


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
