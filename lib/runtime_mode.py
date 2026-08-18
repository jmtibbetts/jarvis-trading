"""What this process is ALLOWED to do to the book, enforced at the mutation.

THE MODE THIS EXISTS FOR.

    EVIDENCE_ONLY   decide for real; touch the book not at all
    FULL_VIRTUAL    decide for real; execute against the virtual book

EVIDENCE_ONLY is how JARVIS starts producing genuine forward data — real
candidates, real gates, real AI verdicts, real T0 market state, real forward
MFE/MAE — BEFORE canonical exits exist. The dataset we are missing is
decisions, not fills, and decisions can be collected honestly long before the
economics are trustworthy enough to simulate.

WHY THE GUARD IS HERE AND NOT AT THE CALLER.

    if evidence_only:
        skip_open = True

at one call site is not a safety property. It is a promise that every present
and future caller remembers to ask, and the caller that forgets is exactly
the one that opens a position in a mode that forbade it. So the refusal lives
at the ECONOMIC MUTATION itself: `prepare_entry`, `settle_position_entry`,
`close_paper_position` and `partial_close_paper_position` each call
`forbid_economic_mutation()`, and in EVIDENCE_ONLY they raise. A new caller
inherits the protection by construction, and a caller that tries to bypass it
has to delete a line rather than merely forget one.

A DECISION IS NOT AN ECONOMIC MUTATION. Deciding TRADE in EVIDENCE_ONLY is
legitimate and is recorded as such — `FORWARD_EVIDENCE_ONLY` with lifecycle
`EXECUTION_SUPPRESSED`. Nothing failed; execution was not permitted. Those
rows carry forward outcomes and inform decision research, and they are barred
from fill calibration and portfolio P&L by the calibration predicate, which
requires a forward EXECUTED source they deliberately are not.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

MODE_ENV = "JARVIS_RUNTIME_MODE"

EVIDENCE_ONLY = "EVIDENCE_ONLY"
FULL_VIRTUAL = "FULL_VIRTUAL"

VALID_MODES = frozenset({EVIDENCE_ONLY, FULL_VIRTUAL})

# FULL_VIRTUAL is the default because it is what every existing caller and
# every existing test already assumes; making EVIDENCE_ONLY the default would
# silently change the meaning of the whole suite. Evidence-only is opted into
# explicitly, which is also the honest way to record that a collection run
# was a deliberate act.
DEFAULT_MODE = FULL_VIRTUAL


class EconomicMutationForbidden(RuntimeError):
    """An economic mutation was attempted in a mode that forbids it."""


def current_mode() -> str:
    raw = (os.getenv(MODE_ENV) or "").strip().upper()
    return raw if raw in VALID_MODES else DEFAULT_MODE


def is_evidence_only() -> bool:
    return current_mode() == EVIDENCE_ONLY


def forbid_economic_mutation(operation: str) -> None:
    """Refuse anything that would move the book in EVIDENCE_ONLY.

    Raises rather than returning a flag: a caller that ignores a False is
    the failure mode this is meant to remove, and an exception cannot be
    ignored by omission.
    """
    if is_evidence_only():
        raise EconomicMutationForbidden(
            f"{operation} is an economic mutation and the runtime is in "
            f"{EVIDENCE_ONLY}. The decision is recorded as evidence with "
            f"execution suppressed; nothing failed and nothing is retried.")


def describe() -> dict:
    m = current_mode()
    return {
        "mode": m,
        "economic_mutation_allowed": m != EVIDENCE_ONLY,
        "decisions_recorded": True,
        "forward_outcomes_collected": True,
    }
