"""Which economic machine produced a result — one string, one authority.

WHY THIS FILE EXISTS. Two epoch constants lived in this codebase and they
had drifted apart:

    lib.canonical_entry.CANONICAL_ENGINE_EPOCH = "2026-08-17-venue-book"
    lib.calibration.CURRENT_EPOCH              = "2026-08-13"

`canonical_learning` correctly stamps its `trade_outcomes` row with the
PERSISTED epoch — the one that actually produced the economics. Calibration,
expectancy, the edge-cost matrix and the paper job's historical-edge query
then all filter `engine_epoch == CURRENT_EPOCH`.

While those two strings differ, a canonical trade settles exactly, projects
its outcome exactly, records `learning_state = APPLIED` — and is invisible
to every consumer of outcomes. **The book would learn nothing while
reporting that it had.** That is a silent failure of the worst kind: every
individual component is correct and the system as a whole does nothing.

They are ONE concept. Both answer "which version of the economic machine is
this?" — the calibration constant was introduced with exactly that
reasoning, that pre-epoch outcomes "describe a machine that is gone". So
there is one string here and both names now alias it.

IT IS A MODEL VERSION, NOT A SESSION ID. Never generate it at process
startup, never derive it from a clock. Two processes running the same code
must agree, and a result stamped last week must still say which machine
produced it. Bump it deliberately, in a commit, when the economics change.

WHAT A BUMP MEANS. Outcomes stamped with a prior epoch are not deleted, not
relabelled and not migrated — they remain honest history of the machine that
made them. They simply stop being counted as evidence about THIS machine.
Expect the learners to report no evidence immediately after a bump; that is
the correct answer, and `NO_EVIDENCE_CEILING` exists to keep "we do not
know" from presenting as confidence.
"""
from __future__ import annotations

# ── THE AUTHORITY ────────────────────────────────────────────────────────
# Post-Pass-B: exact venue-book fills, contract-native sizing and mark
# economics, persisted entry ledger, exact exit settlement with holding
# cost, canonical realized outcome, idempotent learning projection, and one
# routed door for every exit.
ENGINE_EPOCH = "2026-08-18-canonical-lifecycle-v1"

# Historical epochs, for reading old rows. Never filtered on by live code —
# recorded so nobody has to guess what an old label meant.
PRIOR_EPOCHS = (
    # The mark-as-fill simulator. Its outcomes carry the defects Pass B
    # removed: caller mark treated as fill, unit-blind contract sizing
    # (26 CONTRACTS priced as 26 BTC), scale-outs voting twice, and no
    # holding cost. Archived, never counted.
    "2026-08-13",
    # Venue-book ENTRY landed, but exit was still legacy. A real entry fill
    # with a mark-priced exit is a half-canonical trade, so its results
    # describe neither machine cleanly.
    "2026-08-17-venue-book",
)


def is_current(epoch: str | None) -> bool:
    """Was this result produced by the machine now running?"""
    return epoch == ENGINE_EPOCH
