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

# How coarsely a level is bucketed before it enters the thesis identity.
#
# EXACT FLOATS MANUFACTURE FAKE INDEPENDENCE. The scheduler regenerates the
# same setup every pass, and prices drift a few basis points between them.
# On raw floats, "BTC 15m breakout long, entry 100.01" and the same setup
# re-emitted at 100.03 become two different theses — so ONE market claim
# votes twice, which is the exact double-count thesis_id exists to prevent,
# arriving through a different door.
#
# 25bp buckets: wide enough to absorb regeneration drift, narrow enough
# that a genuinely different entry on the same instrument stays distinct.
LEVEL_BUCKET_BP = 25.0

# Time bucket for the same reason. A setup regenerated eight minutes later
# is the same claim; one found the next session is not.
BIRTH_BUCKET_MINUTES = 60

# Experiment arms. Same thesis, different policy.
AGENT = "AGENT"                  # what JARVIS actually chose
SHADOW = "SHADOW"                # standardized control policy
COUNTERFACTUAL = "COUNTERFACTUAL"  # what a declined thesis would have done

ARMS = frozenset({AGENT, SHADOW, COUNTERFACTUAL})

# Why an arm did not trade. DECLINED is evidence; the others are noise.
DECLINED = "DECLINED"            # the policy chose not to
UNFILLED = "UNFILLED"            # it tried and the market did not fill it
INELIGIBLE = "INELIGIBLE"        # it could not have traded this at all


def _bucket_level(price: float | None) -> str | None:
    """Round a level onto a log grid of LEVEL_BUCKET_BP-wide steps.

    A log grid keeps the tolerance PROPORTIONAL, so 25bp means the same
    thing on a $0.000004 memecoin and a $95,000 BTC print. A fixed
    absolute epsilon would merge every distinct memecoin level into one
    bucket while separating BTC entries that are effectively identical.
    """
    import math
    if price in (None, "") or float(price) <= 0:
        return None
    step = math.log(1.0 + LEVEL_BUCKET_BP / 10_000.0)
    return str(int(math.log(float(price)) / step))


def _bucket_time(created_at: str | None) -> str | None:
    """Round a birth time down to a BIRTH_BUCKET_MINUTES window."""
    if not created_at:
        return None
    try:
        from datetime import datetime, timezone
        t = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return str(int(t.timestamp() // (BIRTH_BUCKET_MINUTES * 60)))
    except (TypeError, ValueError):
        return str(created_at)


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
            # BUCKETED, not exact. See LEVEL_BUCKET_BP — raw floats let a
            # regenerated setup drift into a second "independent" thesis.
            _bucket_level(self.entry),
            _bucket_level(self.stop),
            _bucket_level(self.target),
            _bucket_time(self.created_at),
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


# ── Attribution levels ───────────────────────────────────────────────────
# FIVE IDS, FIVE DIFFERENT QUESTIONS. Collapsing any two of them loses a
# question the system needs to answer:
#
#   signal_id     which generated signal produced this
#   thesis_id     which MARKET CLAIM is being tested  <- the sample unit
#   arm_id        how that claim was acted upon (policy)
#   execution_id  which simulated execution
#   outcome_id    which final result
#
# Sample size is counted on thesis_id. Everything below it is an
# experimental variable, not a new observation.

# Outcomes that predate reliable linkage. THEY STAY THIS WAY.
LEGACY_UNATTRIBUTED = "LEGACY_UNATTRIBUTED"


def make_arm_id(thesis_id: str, arm: str, policy: str | None = None) -> str:
    """Identity of one POLICY acting on one claim.

    `policy` distinguishes arms that share a name but differ in what they
    are testing — two Agent arms at different sizing, say — so an A/B on
    position size does not silently overwrite itself.
    """
    parts = "|".join(str(x) for x in (thesis_id, arm, policy or "default"))
    return hashlib.sha256(parts.encode()).hexdigest()[:20]


def is_attributable(signal_id: str | None, thesis_id: str | None) -> bool:
    """Whether an outcome may enter attributed analysis at all.

    DO NOT FUZZY-MATCH. A historical win cannot be assigned to a strategy
    by resembling one — matching on symbol, direction and a nearby
    timestamp would attach real outcomes to claims that never produced
    them, and every per-strategy statistic downstream would inherit the
    guesswork while looking exactly as confident as measured evidence.
    A larger sample built that way is worse than a smaller honest one.
    """
    return bool(signal_id) and bool(thesis_id)


def attribution_state(signal_id: str | None, thesis_id: str | None) -> str:
    return ("ATTRIBUTED" if is_attributable(signal_id, thesis_id)
            else LEGACY_UNATTRIBUTED)


@dataclass
class ArmResult:
    """What ONE policy did with a thesis. A refusal is a result."""
    thesis_id: str
    arm: str
    traded: bool = False
    no_trade_reason: str | None = None

    # The rest of the attribution chain.
    signal_id: str | None = None
    arm_id: str | None = None
    execution_id: str | None = None
    outcome_id: str | None = None
    policy: str | None = None

    outcome = None                 # a RealizedOutcome when traded
    net_r: float | None = None
    net_pnl_usd: float | None = None
    entry_fill: float | None = None
    exit_reason: str | None = None
    venue_type: str | None = None

    def __post_init__(self):
        if not self.arm_id:
            self.arm_id = make_arm_id(self.thesis_id, self.arm, self.policy)

    @property
    def attribution(self) -> str:
        return attribution_state(self.signal_id, self.thesis_id)


def paired_deltas(results: list) -> list:
    """delta_net_r = agent_net_r - shadow_net_r, per shared thesis.

    500 unique theses give 500 PAIRED differences, which is a stronger
    analysis than pretending to 1,000 independent trades — the pairing
    removes the market's own variance, so what remains is policy value.
    """
    by_thesis: dict = {}
    for r in results or []:
        if r is not None:
            by_thesis.setdefault(r.thesis_id, {})[r.arm] = r

    out = []
    for t, arms in by_thesis.items():
        a, s = arms.get(AGENT), arms.get(SHADOW)
        if a and s and a.net_r is not None and s.net_r is not None:
            out.append({"thesis_id": t, "agent_net_r": a.net_r,
                        "shadow_net_r": s.net_r,
                        "delta_net_r": a.net_r - s.net_r})
    return out


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
