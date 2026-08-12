"""One realized outcome, counted once.

Historical performance was reaching the final decision through five
independent paths, none of which knew about the others:

    signal_scorer._calibrate_confidence   win rate -> calibrated confidence
    signal_scorer failure_penalty         postmortems -> composite penalty
    signal_fusion historical_adjustment   win rate -> +/-10 opportunity score
    leverage_policy history_factor        win rate -> leverage multiplier
    learning_engine confidence adjustment regime record -> confidence

The compounding is worse than a list of five suggests, because they nest:
calibrated confidence is a COMPONENT of the composite score, the composite
score is the BASE of the opportunity score, and then fusion added the same
win rate to that base a second time. A symbol with a good record had its
confidence raised, its composite raised through that confidence, its
opportunity score raised again on top, and its leverage multiplier raised
as well — four movements from one piece of evidence, as though four
independent studies had agreed.

The separation this module enforces, following the refactor spec:

    WIN RATE      is evidence about PROBABILITY. It belongs in exactly one
                  place: calibration. Nothing else may add it to a score.

    SAMPLE SIZE   is evidence about CERTAINTY, which is a different thing.
                  It may legitimately restrain risk (a setup with no track
                  record should not carry maximum leverage) without being a
                  second opinion on the win rate.

    FAILURE MEMORY is evidence about EXECUTION QUALITY — signals that
                  expired unfilled, degenerate levels, data errors. It is
                  not a win/loss statistic and does not double-count
                  against the win rate.

Consumers declare their purpose so the split stays visible in code rather
than living in a comment someone edits around.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Below this many decided outcomes a win rate is noise, not evidence.
MIN_SAMPLE_FOR_EDGE = 5

# Purposes a consumer may declare. Anything else is a programming error.
PURPOSE_CALIBRATION = "calibration"    # may use the win rate
PURPOSE_UNCERTAINTY = "uncertainty"    # may use ONLY the sample size
PURPOSE_DISPLAY = "display"            # may show anything, may change nothing

_VALID_PURPOSES = {PURPOSE_CALIBRATION, PURPOSE_UNCERTAINTY, PURPOSE_DISPLAY}


def get_edge(historical: dict | None, *, purpose: str) -> dict:
    """The one accessor for realized performance.

    Returns a dict whose CONTENTS depend on the declared purpose, so a
    consumer physically cannot use the win rate for something it should not
    influence:

      calibration  -> win_rate, sample, and a shrunk empirical estimate
      uncertainty  -> sample only; win_rate is deliberately withheld
      display      -> everything, clearly marked as presentational
    """
    if purpose not in _VALID_PURPOSES:
        raise ValueError(f"unknown purpose {purpose!r}; expected one of {sorted(_VALID_PURPOSES)}")

    historical = historical or {}
    total = int(historical.get("total_trades") or 0)
    wins = int(historical.get("wins") or 0)

    # signal_accuracy stores win_rate as a FRACTION; some callers pass a
    # percentage. Normalise defensively rather than trusting either.
    raw_rate = historical.get("win_rate")
    if raw_rate is None and total:
        raw_rate = wins / total
    if raw_rate is not None and raw_rate > 1.0:
        raw_rate = raw_rate / 100.0

    proven = total >= MIN_SAMPLE_FOR_EDGE

    if purpose == PURPOSE_UNCERTAINTY:
        # The win rate is WITHHELD on purpose. A consumer restraining risk
        # because a setup is unproven must not also re-litigate whether the
        # setup wins — calibration already did that.
        return {
            "purpose": purpose,
            "sample": total,
            "proven": proven,
            "note": ("win rate withheld: it is already counted once in "
                     "calibration and must not move a second number"),
        }

    # Laplace-style shrinkage toward 50%: 3 wins from 3 trades is not a 100%
    # edge, and this is the only place that judgement is made.
    empirical = ((wins + 2) / (total + 4)) if total else None

    return {
        "purpose": purpose,
        "sample": total,
        "wins": wins,
        "proven": proven,
        "win_rate": raw_rate,
        "shrunk_win_rate": empirical,
        "note": (f"{total} decided outcomes" if proven
                 else f"{total} outcomes — below the {MIN_SAMPLE_FOR_EDGE} needed to be evidence"),
    }


def describe_for_ui(historical: dict | None) -> str:
    """A human line for panels. Presentational only — changes no number."""
    edge = get_edge(historical, purpose=PURPOSE_DISPLAY)
    if not edge["proven"]:
        return edge["note"]
    rate = edge.get("win_rate")
    if rate is None:
        return edge["note"]
    return (f"{edge['sample']} trades, {rate * 100:.0f}% win rate "
            f"(shrunk to {edge['shrunk_win_rate'] * 100:.0f}% for sizing)")
