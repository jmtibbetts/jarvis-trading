"""Deterministic closed-trade history, built in the temporary test database.

WHY THIS EXISTS. Twelve expectancy tests used to call `self.skipTest("no
outcome history in this environment")` whenever `lookup()` returned None.
That made coverage of the cost gate — the thing standing between the desk
and a setup carrying 13R of round-trip cost — conditional on whether the
machine running the suite happened to have traded. On a hermetic run it
never does, so those twelve assertions had never executed in CI at all: a
green check over a gate nobody had tested.

The history here is CONSTRUCTED, not sampled, so every number the tests
assert on is one this module chose:

  equity / long / 4H     a real, cost-survivable edge
  crypto / long / 15m    the same gross edge, which 15m crypto costs eat

Both buckets are seeded at MIN_SAMPLE so the hierarchy reports an exact
match rather than falling back, and both are `outcome_source="live"` so the
0.5 replay weight does not silently halve the sample.

USES THE SAME SESSION DATABASE AS THE REST OF THE SUITE. conftest.py has
already pointed JARVIS_DB_PATH at a temp file and asserts the operator DB is
not in play; this module deliberately does no redirecting of its own, so
there is exactly one place that decision is made.
"""
from __future__ import annotations

from lib.expectancy import MIN_SAMPLE

# Enough to clear MIN_SAMPLE with room to spare, so a future bump to the
# threshold surfaces as a failing assertion rather than a resurrected skip.
SAMPLE = MIN_SAMPLE + 15

# 60% winners at +2R against -1R losers => gross expectancy +0.8R.
# Comfortably positive on a 2.7% equity stop, comfortably negative once
# 15m crypto costs are charged against a 0.08% stop.
WIN_RATE = 0.6
WIN_R = 2.0
LOSS_R = -1.0

EQUITY_BUCKET = {"asset_class": "equity", "direction": "Long", "timeframe": "4H",
                 "symbol": "NVDA", "entry": 224.0, "stop": 218.0}
CRYPTO_BUCKET = {"asset_class": "crypto", "direction": "Long", "timeframe": "15m",
                 "symbol": "BTC/USD", "entry": 63800.0, "stop": 63750.0}


def _exit_for(entry: float, stop: float, r: float, direction: str) -> float:
    """The exit price that realises exactly `r` R on this entry/stop pair."""
    risk = abs(entry - stop)
    if str(direction).lower().startswith("short"):
        return entry - r * risk
    return entry + r * risk


def seed_outcomes(strategy: str = "unclassified", sample: int = SAMPLE) -> int:
    """Write a deterministic closed-trade history and rebuild the table.

    Returns the number of outcome rows written. Safe to call more than once
    in a session — rows accumulate, which only strengthens the sample.
    """
    from app.database import TradeOutcome, TradingSignal, get_db, new_id
    from lib.calibration import CURRENT_EPOCH

    written = 0
    with get_db() as db:
        for spec in (EQUITY_BUCKET, CRYPTO_BUCKET):
            wins = round(sample * WIN_RATE)
            for i in range(sample):
                r = WIN_R if i < wins else LOSS_R
                exit_price = _exit_for(spec["entry"], spec["stop"], r, spec["direction"])
                sig_id = new_id()
                # The signal carries the stop and the strategy; expectancy
                # reads both through an outer join, and computes R against
                # the stop rather than against a percentage.
                db.add(TradingSignal(
                    id=sig_id,
                    asset_symbol=spec["symbol"],
                    asset_class=spec["asset_class"],
                    direction=spec["direction"],
                    timeframe=spec["timeframe"],
                    entry_price=spec["entry"],
                    stop_loss=spec["stop"],
                    strategy=strategy,
                ))
                db.add(TradeOutcome(
                    id=new_id(),
                    signal_id=sig_id,
                    symbol=spec["symbol"],
                    asset_class=spec["asset_class"],
                    direction=spec["direction"],
                    timeframe=spec["timeframe"],
                    entry_price=spec["entry"],
                    exit_price=exit_price,
                    outcome="WIN" if r > 0 else "LOSS",
                    engine_epoch=CURRENT_EPOCH,
                    outcome_source="live",
                ))
                written += 1
        db.commit()

    # force=True because build_table caches for 300s and the tests need the
    # rows they just wrote, not whatever an earlier test left behind.
    from lib.expectancy import build_table
    build_table(force=True)
    return written


def has_history() -> bool:
    """True when a usable current-epoch history exists."""
    from lib.expectancy import lookup
    return lookup("unclassified", "equity", "long", "4H") is not None
