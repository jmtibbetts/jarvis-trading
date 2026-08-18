"""Forward outcome observer — schedule horizons, sample, resolve what is due.

Deliberately runnable on its own. The autonomous trading scheduler is OFF
and stays off; collecting evidence about decisions already made needs no
trading loop, and tying the two together would make the safe half of the
system depend on the half under review.

Nothing in this job can open a position, move cash or record a realized
outcome. It reads markets and writes evidence rows.
"""
import logging

logger = logging.getLogger(__name__)


def run(limit: int = 500) -> dict:
    from lib.decision_outcome import run_observer, schedule_pending_observations

    scheduled = schedule_pending_observations(limit=limit)
    observed = run_observer(limit=limit)
    out = {"scheduled": scheduled, **observed}
    logger.info("[ForwardOutcome] %s", out)
    return out


if __name__ == "__main__":       # pragma: no cover - operator entry point
    logging.basicConfig(level=logging.INFO)
    print(run())
