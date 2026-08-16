"""One market observation, however many policies act on it.

THE SAMPLE-INFLATION BUG THIS PREVENTS. The Agent book and the Shadow
control book see the same signals. If both take a trade and both record an
outcome, naive counting says two independent observations — so a market
that moved once votes twice, confidence intervals shrink by root two for
free, and a strategy looks twice as validated as its evidence supports.

They are not two observations. They are ONE market event with TWO POLICY
RESULTS, and that is precisely what makes the comparison valuable: same
instrument, same moment, same evidence, different decisions. Binding them
to a shared `thesis_id` is what turns a duplicated sample into a
controlled experiment.

WHAT A THESIS IS. It is the claim, not the trade: this instrument, this
side, this strategy, on this timeframe, with these levels, given this
evidence at this moment. Two arms may size it differently, manage it
differently or decline it entirely — and a DECLINE is still an outcome
worth recording, because a policy that avoids losers has selection edge
that never appears in its own trade log.

WHAT DECOMPOSES. With arms bound to one thesis, the difference between
them is attributable rather than mysterious:

    SELECTION   did this policy take the right theses?
    EXECUTION   given the same thesis, did it get filled better?
    MANAGEMENT  given the same entry, did it exit better?
    VENUE       would the other venue have been cheaper?

A single blended P&L answers none of those.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

THESIS_VERSION = "thesis_v1"

# Experiment arms. Same thesis, different policy.
AGENT = "AGENT"                  # what JARVIS actually chose
SHADOW = "SHADOW"                # standardized control policy
COUNTERFACTUAL = "COUNTERFACTUAL"  # what a declined thesis would have done

ARMS = frozenset({AGENT, SHADOW, COUNTERFACTUAL})

# Why an arm did not trade. DECLINED is evidence; the others are noise.
DECLINED = "DECLINED"            # the policy chose not to
UNFILLED = "UNFILLED"            # it tried and the market did not fill it
INELIGIBLE = "INELIGIBLE"        # it could not have traded this at all


@dataclass
class TradeThesis:
    """The claim. Stable across every arm that acts on it."""
    instrument_id: str
    side: str
    strategy: str | None = None
    timeframe: str | None = None

    entry: float | None = None
    stop: float | None = None
    target: float | None = None

    created_at: str | None = None
    signal_id: str | None = None
    source: str | None = None

    # Frozen at decision time. Rebuilding a decision from today's mutable
    # tables produces the decision you would make NOW, which is not the one
    # being judged.
    evidence: dict = field(default_factory=dict)
    measured_edge: dict = field(default_factory=dict)
    market_snapshot: dict = field(default_factory=dict)

    model_version: str | None = None
    engine_epoch: str | None = None
    thesis_version: str = THESIS_VERSION

    _thesis_id: str | None = None

    @property
    def thesis_id(self) -> str:
        """Deterministic, so the same claim produced twice binds to itself.

        Derived from the CLAIM only — instrument, side, strategy, timeframe,
        levels and the moment. Deliberately excludes size, leverage, venue
        and anything else a POLICY decides, because those are exactly what
        the arms are allowed to differ on. Including size would give the
        Agent and the Shadow different ids for the same market event and
        reinstate the double-count this exists to prevent.
        """
        if self._thesis_id:
            return self._thesis_id
        parts = "|".join(str(x) for x in (
            self.instrument_id, self.side, self.strategy, self.timeframe,
            self.entry, self.stop, self.target, self.created_at,
            self.thesis_version))
        self._thesis_id = hashlib.sha256(parts.encode()).hexdigest()[:24]
        return self._thesis_id

    def as_dict(self) -> dict:
        from dataclasses import asdict
        d = asdict(self)
        d.pop("_thesis_id", None)
        return {**d, "thesis_id": self.thesis_id}


def build(*, symbol: str, side: str, **kw) -> TradeThesis:
    """Create a thesis with a canonical instrument and a strict side.

    Refuses an unreadable side rather than defaulting: a thesis is a CLAIM,
    and a claim that does not state a direction is not one.
    """
    from lib.instruments import resolve
    from lib.trade_side import parse_side_strict

    parsed = parse_side_strict(side)
    if parsed is None:
        raise ValueError(
            f"cannot form a thesis for {symbol}: direction {side!r} states "
            f"no side, and a thesis without a direction is not a claim")

    inst = resolve(symbol)
    return TradeThesis(
        instrument_id=inst.instrument_id, side=parsed,
        **{k: v for k, v in kw.items()
           if k in TradeThesis.__dataclass_fields__ and not k.startswith("_")})


@dataclass
class ArmResult:
    """What ONE policy did with a thesis. A refusal is a result."""
    thesis_id: str
    arm: str
    traded: bool = False
    no_trade_reason: str | None = None

    outcome = None                 # a RealizedOutcome when traded
    net_r: float | None = None
    net_pnl_usd: float | None = None
    entry_fill: float | None = None
    exit_reason: str | None = None
    venue_type: str | None = None


def sample_count(results: list) -> int:
    """DISTINCT THESES — never the number of arm results.

    This is the whole guard. `len(results)` over a two-arm experiment
    double-counts every market event, and the error is invisible because
    the number looks bigger and better.
    """
    return len({r.thesis_id for r in results if r is not None})


def decompose(results: list) -> dict:
    """Split Agent-vs-Shadow into the edges that actually differ.

    Only theses BOTH arms saw are compared. A thesis one arm never
    evaluated says nothing about their relative skill, and including it
    would measure coverage while claiming to measure edge.
    """
    import statistics

    by_thesis: dict = {}
    for r in results or []:
        if r is None:
            continue
        by_thesis.setdefault(r.thesis_id, {})[r.arm] = r

    paired = {t: arms for t, arms in by_thesis.items()
              if AGENT in arms and SHADOW in arms}
    if not paired:
        return {"theses": len(by_thesis), "paired": 0,
                "detail": "no thesis was evaluated by both arms yet"}

    both_traded, agent_only, shadow_only, neither = [], [], [], []
    for t, arms in paired.items():
        a, s = arms[AGENT], arms[SHADOW]
        if a.traded and s.traded:
            both_traded.append((a, s))
        elif a.traded:
            agent_only.append(a)
        elif s.traded:
            shadow_only.append(s)
        else:
            neither.append(t)

    def _mean_r(rows):
        vals = [x.net_r for x in rows if x.net_r is not None]
        return statistics.fmean(vals) if vals else None

    # MANAGEMENT edge: both entered the same thesis, so any difference in
    # result came from what happened AFTER entry.
    mgmt = [a.net_r - s.net_r for a, s in both_traded
            if a.net_r is not None and s.net_r is not None]

    return {
        "theses": len(by_thesis),
        "paired": len(paired),
        # THE number. Two arms over one market event is one sample.
        "market_samples": len(paired),
        "arm_results": len([r for r in results if r is not None]),
        "both_traded": len(both_traded),
        "agent_only": len(agent_only),
        "shadow_only": len(shadow_only),
        "neither_traded": len(neither),
        # SELECTION edge: what the Agent took that the control did not,
        # against what the control took and the Agent declined.
        "selection_agent_only_mean_r": _mean_r(agent_only),
        "selection_shadow_only_mean_r": _mean_r(shadow_only),
        "management_delta_r_mean": statistics.fmean(mgmt) if mgmt else None,
        "management_delta_r_median": statistics.median(mgmt) if mgmt else None,
        "agent_mean_r": _mean_r([a for a, _ in both_traded]),
        "shadow_mean_r": _mean_r([s for _, s in both_traded]),
        "note": ("market_samples counts DISTINCT THESES. Two arms acting on "
                 "one market event is one observation with two policy "
                 "results, not two observations."),
    }
