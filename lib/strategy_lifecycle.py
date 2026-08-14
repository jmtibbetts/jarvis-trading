"""Which strategies are allowed to trade, decided out-of-sample.

Phases 5 and 6 gave every strategy a measured expectancy. The obvious next
move — trade whichever bucket scores best — is also the classic way to lose
money with statistics: those numbers were computed on the same trades used
to pick them. With fourteen strategies across several timeframes there are
enough buckets that one will look excellent by chance, and it will be the
one selected precisely because it got lucky.

So the decision is made on data the ranking never saw. Outcomes are split
by TIME: the older portion trains, the newer portion validates, and a
strategy is judged on the newer portion alone. A strategy that was
profitable in-sample and is not out-of-sample was curve-fitted, and that is
the single most useful thing this module can detect.

The lifecycle:

    ACTIVE        out-of-sample expectancy positive, enough trades — trade it
    REDUCED       positive but thin or inconsistent — trade smaller
    EXPERIMENTAL  too few trades to judge — allowed, at minimum size
    SHADOW        negative out-of-sample but still measured — scored, never
                  traded, so it keeps generating evidence about itself
    DISABLED      negative out-of-sample with enough evidence to be sure

SHADOW is the state that matters. A strategy that is switched off stops
producing data, so nothing ever revises the judgement that switched it off,
and it can never come back even if the market changes. Shadow mode keeps
the measurement running with no money at risk.

The split point is never chosen by looking at the results. It is a fixed
fraction of the timeline, decided before anything is computed.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

ACTIVE = "ACTIVE"
REDUCED = "REDUCED"
EXPERIMENTAL = "EXPERIMENTAL"
SHADOW = "SHADOW"
DISABLED = "DISABLED"

LIFECYCLE = (ACTIVE, REDUCED, EXPERIMENTAL, SHADOW, DISABLED)

# Position-size multiplier per state. SHADOW and DISABLED are zero: they do
# not trade. EXPERIMENTAL trades at a size where being wrong is affordable.
SIZE_MULTIPLIER = {
    ACTIVE: 1.0,
    REDUCED: 0.5,
    EXPERIMENTAL: 0.25,
    SHADOW: 0.0,
    DISABLED: 0.0,
}

# Fraction of the timeline used for training. Fixed in advance and never
# tuned against the result — a split chosen after seeing the outcomes is
# just a slower way of fitting to them.
TRAIN_FRACTION = 0.6

# Out-of-sample trades needed before a verdict is anything other than
# EXPERIMENTAL.
MIN_OOS_TRADES = 30
# ...and before a negative verdict is confident enough to disable outright
# rather than shadow.
MIN_OOS_TO_DISABLE = 60

# Expectancy bars, in R, applied to the OUT-OF-SAMPLE window.
ACTIVE_R = 0.10
REDUCED_R = 0.0


def _f(v, default=0.0):
    try:
        if v is None or isinstance(v, bool):
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def split_by_time(rows: list, train_fraction: float = TRAIN_FRACTION) -> tuple[list, list]:
    """Oldest `train_fraction` trains, newest validates.

    By TIME, not at random. A random split leaks: trades from the same day
    on the same symbol land on both sides, so the validation set has
    already seen the conditions it is meant to be judging blind. Time is
    also the only split that answers the question actually being asked —
    does this still work NOW.
    """
    ordered = sorted(rows, key=lambda r: r.get("exited_at") or "")
    if len(ordered) < 2:
        return ordered, []
    cut = int(len(ordered) * train_fraction)
    cut = max(1, min(len(ordered) - 1, cut))
    return ordered[:cut], ordered[cut:]


def _expectancy(rows: list) -> dict:
    """P(win), average win/loss in R and expectancy for a set of trades.

    Replay evidence counts at REPLAY_WEIGHT (matching lib/expectancy and
    lib/calibration — P0.5's "also fix"): a strategy's LIVE-capital state
    was previously judged on replayed bars at full weight, so 500 replays
    could promote a strategy 3 live fills would have kept EXPERIMENTAL.
    The live/replay mix is surfaced so the UI can say what the verdict
    stands on."""
    from lib.expectancy import REPLAY_WEIGHT
    pairs = [(_f(r.get("r")), REPLAY_WEIGHT if r.get("replay") else 1.0)
             for r in rows if r.get("r") is not None]
    if not pairs:
        return {"trades": 0, "trades_live": 0, "trades_replay": 0,
                "p_win": 0.0, "avg_win_r": 0.0,
                "avg_loss_r": 0.0, "expected_r": 0.0}
    n = sum(w for _, w in pairs)
    wins = [(r, w) for r, w in pairs if r > 0]
    losses = [(abs(r), w) for r, w in pairs if r <= 0]
    win_w = sum(w for _, w in wins)
    loss_w = sum(w for _, w in losses)
    p = win_w / n if n else 0.0
    aw = sum(r * w for r, w in wins) / win_w if win_w else 0.0
    al = sum(r * w for r, w in losses) / loss_w if loss_w else 1.0
    n_replay = sum(1 for r in rows if r.get("r") is not None and r.get("replay"))
    n_live = sum(1 for r in rows if r.get("r") is not None) - n_replay
    return {"trades": round(n, 1), "trades_live": n_live,
            "trades_replay": n_replay, "p_win": round(p, 4),
            "avg_win_r": round(aw, 3), "avg_loss_r": round(al, 3),
            "expected_r": round(p * aw - (1 - p) * al, 4)}


def classify(in_sample: dict, out_of_sample: dict) -> dict:
    """The lifecycle state, from the OUT-OF-SAMPLE numbers.

    In-sample is reported only so the gap between them is visible: a
    strategy that looked good in training and does not hold up was
    curve-fitted, and saying so is more useful than the verdict alone.
    """
    oos_n = out_of_sample.get("trades", 0)
    oos_r = _f(out_of_sample.get("expected_r"))
    is_r = _f(in_sample.get("expected_r"))
    overfit_gap = round(is_r - oos_r, 4)

    if oos_n < MIN_OOS_TRADES:
        state = EXPERIMENTAL
        reason = (f"only {oos_n} out-of-sample trades — not enough to judge "
                  f"(need {MIN_OOS_TRADES})")
    elif oos_r >= ACTIVE_R:
        state = ACTIVE
        reason = f"{oos_r:+.3f}R out-of-sample over {oos_n} trades"
    elif oos_r > REDUCED_R:
        state = REDUCED
        reason = (f"{oos_r:+.3f}R out-of-sample — positive but under the "
                  f"{ACTIVE_R}R bar")
    elif oos_n >= MIN_OOS_TO_DISABLE:
        state = DISABLED
        reason = (f"{oos_r:+.3f}R out-of-sample over {oos_n} trades — "
                  f"enough evidence to stop")
    else:
        state = SHADOW
        reason = (f"{oos_r:+.3f}R out-of-sample over {oos_n} trades — "
                  f"negative, but keep measuring before disabling")

    return {
        "state": state,
        "reason": reason,
        "size_multiplier": SIZE_MULTIPLIER[state],
        "in_sample": in_sample,
        "out_of_sample": out_of_sample,
        # Positive gap means it did better in training than in validation,
        # which is the signature of fitting to the training set.
        "overfit_gap_r": overfit_gap,
        "overfitted": bool(oos_n >= MIN_OOS_TRADES and is_r > ACTIVE_R
                           and oos_r <= REDUCED_R),
    }


def _load_rows() -> list[dict]:
    """Closed trades with their strategy and result in R."""
    try:
        from app.database import get_db, TradeOutcome, TradingSignal
        from lib.calibration import CURRENT_EPOCH
        from lib.expectancy import _placed_stops, _r_of
        with get_db() as db:
            rows = db.query(
                TradeOutcome.entry_price, TradeOutcome.exit_price,
                TradeOutcome.direction, TradeOutcome.exited_at,
                TradeOutcome.outcome_source, TradeOutcome.timeframe,
                TradingSignal.stop_loss, TradingSignal.strategy,
                TradeOutcome.signal_id,
            ).outerjoin(
                TradingSignal, TradingSignal.id == TradeOutcome.signal_id
            ).filter(TradeOutcome.engine_epoch == CURRENT_EPOCH).all()
            placed = _placed_stops(db)
    except Exception as e:
        logger.warning(f"[Lifecycle] could not read outcomes: {e}")
        return []

    out = []
    for entry, exit_price, direction, exited_at, src, tf, stop, strategy, sig_id in rows:
        # R against the stop AS PLACED, matching expectancy (P0.12).
        r = _r_of(entry, placed.get(sig_id) or stop, exit_price, direction)
        if r is None:
            continue
        out.append({
            "strategy": strategy or "unclassified",
            "timeframe": tf, "exited_at": exited_at,
            "r": max(-3.0, min(10.0, r)),
            "replay": src == "replay",
        })
    return out


def evaluate_all(include_replay: bool = True) -> dict:
    """Lifecycle state for every strategy that has closed trades."""
    rows = _load_rows()
    if not include_replay:
        rows = [r for r in rows if not r["replay"]]
    by_strategy: dict[str, list] = {}
    for r in rows:
        by_strategy.setdefault(r["strategy"], []).append(r)

    out = {}
    for strategy, srows in by_strategy.items():
        train, validate = split_by_time(srows)
        out[strategy] = classify(_expectancy(train), _expectancy(validate))
    return {
        "train_fraction": TRAIN_FRACTION,
        "min_oos_trades": MIN_OOS_TRADES,
        "total_trades": len(rows),
        "strategies": dict(sorted(
            out.items(),
            key=lambda kv: -_f(kv[1]["out_of_sample"].get("expected_r")))),
    }


def state_of(strategy: str | None, cache: dict | None = None) -> dict:
    """One strategy's lifecycle state. `cache` is an evaluate_all() result,
    passed in when scoring many signals so the table is built once."""
    table = (cache or evaluate_all()).get("strategies", {})
    got = table.get(strategy or "unclassified")
    if got:
        return got
    # Never seen: allowed, at experimental size. Unknown is not the same as
    # bad, and refusing everything unmeasured is how the system stops
    # generating the evidence it needs.
    return {"state": EXPERIMENTAL, "reason": "no closed trades for this strategy yet",
            "size_multiplier": SIZE_MULTIPLIER[EXPERIMENTAL],
            "in_sample": None, "out_of_sample": None,
            "overfit_gap_r": None, "overfitted": False}


def may_trade(strategy: str | None, cache: dict | None = None) -> bool:
    return state_of(strategy, cache)["size_multiplier"] > 0
