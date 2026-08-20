"""WHICH EVIDENCE MAY A GIVEN LEARNING CONSUMER POOL?

THE DEFECT THIS CLOSES, BEFORE IT HAPPENS. `trade_outcomes.outcome_source`
had exactly two values, `live` and `replay`, and every consumer expressed its
policy as a DENYLIST:

    w = REPLAY_WEIGHT if src == "replay" else 1.0

That is safe only while `replay` is the only thing worth excluding. The
moment a THIRD population exists — a trade a HUMAN executed at a real venue —
every one of those lines silently admits it at FULL WEIGHT, as though JARVIS
had produced the fill itself. Six sites read that way:

    calibration.build_table          weight 1.0
    expectancy.build_table           weight 1.0, and counted as `raw_live`
    edge_cost_matrix                 weight 1.0, and counted into `n_live`
    strategy_lifecycle               `replay=False`, i.e. treated as live
    jobs/paper_trading               `!= "replay"` — the BOOTSTRAP gate
    learning_engine.signal_accuracy  NO SOURCE FILTER AT ALL

None of them would have raised, logged, or failed a test. JARVIS's own
measured win rate would simply have started including trades JARVIS did not
place, and the number would have looked better because a person picked the
entries. `jobs/paper_trading` is the sharpest edge: its comment already says
"a simulation cannot certify that the system is ready to stop bootstrapping",
and a human's trades cannot certify that either.

SO ADMISSION IS AN ALLOWLIST, AND UNKNOWN FAILS CLOSED. A source nobody
characterised gets weight None — excluded — rather than 1.0. Adding a fourth
population later therefore breaks nothing silently: it is simply absent from
every profile until someone decides where it belongs.

THE POPULATIONS

    live              JARVIS executed it, virtually, forward, under the
                      current engine. The reference population.
    replay            simulated under current rules against real historical
                      bars. Perfect fills, both extremes of a bar assumed
                      reachable — systematically optimistic, so it is
                      weighted below live rather than pooled with it.
    manual_operator   a HUMAN executed it, at a real venue, with real money.
                      Real-world truth about the MARKET and about that
                      ACCOUNT'S economics. It is NOT evidence about JARVIS's
                      execution, because JARVIS did not execute it.
    NULL              pre-source rows. Treated as `live`, which is what they
                      were, and never backfilled.

WHY manual_operator IS EXCLUDED FROM EVERY JARVIS-EXECUTION PROFILE HERE.
Not because it is untrustworthy — it is the most real evidence in the system.
Because it answers a different question. Calibration asks "when JARVIS says
score 80 on a 4H setup, how often does that win?" and an outcome shaped by a
person's entry timing, venue choice and exit discipline cannot answer it.
Pooling the two produces one headline number that describes neither, and
afterwards nobody can separate them again.

It is admitted to `OPERATOR_EXECUTION`, which is its own population and has
its own reader. Separable, kept, and counted — just not counted as JARVIS.
"""
from __future__ import annotations

import logging

# THE SAME STRINGS the replay writer already uses, imported rather than
# retyped so a rename cannot fork the vocabulary.
from lib.signal_replay import SOURCE_LIVE, SOURCE_REPLAY

logger = logging.getLogger(__name__)

LEARNING_POPULATION_VERSION = "learning_population_v1"

LIVE = SOURCE_LIVE                      # "live"
REPLAY = SOURCE_REPLAY                  # "replay"
MANUAL_OPERATOR = "manual_operator"

POPULATIONS = (LIVE, REPLAY, MANUAL_OPERATOR)

# Pre-source rows. NULL is not a fourth population; it is `live` before the
# column existed, and it is never backfilled.
LEGACY_UNLABELLED = None

# Replayed fills assumed perfect execution. Kept identical to the constant
# the consumers already used, imported by them from here going forward so
# there is one number rather than two copies that can drift.
REPLAY_WEIGHT = 0.5

# ── Consumer profiles ────────────────────────────────────────────────────
# What is this statistic ABOUT? That question, not convenience, decides
# which populations may enter it.

#: Anything measuring JARVIS'S OWN execution and selection: calibration,
#: expectancy, the edge/cost matrix, strategy lifecycle, signal accuracy.
JARVIS_EXECUTION = "JARVIS_EXECUTION"

#: Certification that enough FORWARD-OBSERVED evidence exists to stop
#: bootstrapping. Neither a simulation nor a human's trades can certify
#: that the program is ready, so both are excluded.
FORWARD_OBSERVED_CERTIFICATION = "FORWARD_OBSERVED_CERTIFICATION"

#: The operator's own executed trades, on their own. Real venues, real
#: money, real costs — and never mixed into the three above.
OPERATOR_EXECUTION = "OPERATOR_EXECUTION"

PROFILES = (JARVIS_EXECUTION, FORWARD_OBSERVED_CERTIFICATION,
            OPERATOR_EXECUTION)

# source -> weight. ABSENT MEANS EXCLUDED. There is deliberately no
# fall-through default: a population that nobody placed in a profile does
# not belong to it.
_ADMISSION: dict[str, dict] = {
    JARVIS_EXECUTION: {
        LIVE: 1.0,
        LEGACY_UNLABELLED: 1.0,
        REPLAY: REPLAY_WEIGHT,
    },
    FORWARD_OBSERVED_CERTIFICATION: {
        LIVE: 1.0,
        LEGACY_UNLABELLED: 1.0,
    },
    OPERATOR_EXECUTION: {
        MANUAL_OPERATOR: 1.0,
    },
}


class LearningPopulationError(ValueError):
    """An unknown profile. Unknown SOURCES are excluded, not raised on —
    a stray value in one row must not take down a whole aggregate."""


def _normalise(source) -> str | None:
    if source is None:
        return None
    s = str(source).strip()
    return s if s else None


def weight(source, *, profile: str) -> float | None:
    """How much this row counts for this consumer. None means EXCLUDED.

    UNKNOWN SOURCES ARE EXCLUDED, NOT DEFAULTED TO 1.0. That is the whole
    inversion: the previous denylist gave full weight to everything it had
    not been told to distrust, which is the wrong direction for a number
    that decides position sizing.
    """
    if profile not in _ADMISSION:
        raise LearningPopulationError(
            f"{profile!r} is not a learning consumer profile. Known: "
            f"{', '.join(PROFILES)}")
    table = _ADMISSION[profile]
    key = _normalise(source)
    if key not in table:
        return None
    return table[key]


def admits(source, *, profile: str) -> bool:
    """Whether this row may enter this consumer at all."""
    return weight(source, profile=profile) is not None


def admitted_sources(profile: str) -> tuple:
    """The sources a profile accepts, for building a SQL IN-list.

    Returned as a tuple that MAY CONTAIN None, because NULL is a real
    admitted value and `IN (...)` does not match it — a caller building SQL
    must handle the NULL arm explicitly. `sql_filter` does that for them.
    """
    if profile not in _ADMISSION:
        raise LearningPopulationError(
            f"{profile!r} is not a learning consumer profile")
    return tuple(_ADMISSION[profile].keys())


def sql_filter(column: str, profile: str) -> tuple:
    """A SQL fragment and its parameters for admitting `profile`.

    Handles the NULL arm explicitly: `outcome_source IN ('live')` does NOT
    match a legacy NULL row, and silently dropping every pre-source outcome
    from calibration would be a large, quiet behaviour change.
    """
    sources = admitted_sources(profile)
    named = [s for s in sources if s is not None]
    parts, params = [], {}
    if named:
        keys = []
        for i, s in enumerate(named):
            k = f"lp_{profile.lower()}_{i}"
            keys.append(f":{k}")
            params[k] = s
        parts.append(f"{column} IN ({', '.join(keys)})")
    if None in sources:
        parts.append(f"{column} IS NULL")
    if not parts:
        # A profile that admits nothing must select nothing, not everything.
        return "1 = 0", {}
    return "(" + " OR ".join(parts) + ")", params


def describe() -> dict:
    """The whole admission matrix, for the API and for the handoff."""
    return {
        "version": LEARNING_POPULATION_VERSION,
        "populations": list(POPULATIONS),
        "legacy_unlabelled_is_treated_as": LIVE,
        "profiles": {
            p: {("NULL" if k is None else k): v
                for k, v in _ADMISSION[p].items()}
            for p in PROFILES
        },
        "note": ("admission is an ALLOWLIST: a source absent from a profile "
                 "is EXCLUDED, never given a default weight. manual_operator "
                 "is real-world truth and is deliberately absent from every "
                 "JARVIS-execution profile, because JARVIS did not execute "
                 "it and a pooled number would describe neither population"),
    }
