"""Empirical calibration: what a confidence number is actually worth.

Measured over 8,899 recorded outcomes, the model's self-reported confidence
was not merely imprecise — it was INVERTED at the extremes:

    stated confidence      n      actual win%
            90+           32          28.1%
            80-89        199          37.2%
            70-79        315          25.7%
            60-69         36          44.4%     <- the least confident won most

A number with that relationship to reality is worse than no number, because
every gate downstream — the focus floor, the execution criteria, the
leverage ladder — takes it as an input and treats it as a probability.

The old blend capped the evidence at weight 0.35, so the model's guess kept
65% of the vote no matter how much history contradicted it. That is why a
90%-confidence signal still displayed ~68% after "calibration". Evidence
now wins outright once there is enough of it.

WHAT PREDICTS, measured: timeframe is the strongest single dimension and
composite_score carries some signal; raw confidence carries almost none.

    timeframe   n      win%          score band    win%
        1H    635     66.5%              70-79    39.2%
        1D   4597     42.2%              60-69    30.0%
         5m   286     41.3%               <60     22.2%
        15m    66     36.4%
         4H  3222     27.8%
"""
from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

# Imported lazily-ish: used only to recognise a bucket label as a strategy.
try:
    from lib.strategies import STRATEGIES as _S
    STRATEGY_NAMES = set(_S)
except Exception:
    STRATEGY_NAMES = set()

# Below this many decided outcomes a bucket cannot speak for itself and the
# lookup falls back to a broader one. Chosen so a bucket's win rate has a
# meaningful confidence interval rather than being one lucky streak.
MIN_SAMPLE = 30

# Sample size at which measured evidence fully replaces the model's guess.
# Below it, the two are blended in proportion to how much evidence exists.
# The old cap of 0.35 meant evidence could never win; the whole point of
# recording 8,899 outcomes is that eventually it should.
FULL_TRUST_SAMPLE = 200

# Laplace smoothing, so a 3-win/3-loss bucket reads as ~50% rather than a
# hard number, and a 0/5 bucket does not read as literally impossible.
PRIOR_WINS = 2.0
PRIOR_TOTAL = 4.0

# Outcomes recorded before this epoch came from a materially different
# machine and are EXCLUDED from calibration. Not deleted — they remain for
# analysis — but they cannot be allowed to calibrate a system they never
# measured.
#
# Measured on 8,903 pre-epoch outcomes: 93.6% were closed by an exit rule
# that no longer exists (the $15 noise cap, closing when the ENTRY signal
# expired, un-scaled tier cuts, LLM exits without horizon context) and only
# 6.0% ever reached their target. Add the contract-multiplier error on
# futures and 6-decimal rounding on sub-cent assets, and the win/loss labels
# describe a machine that is gone.
CURRENT_EPOCH = "2026-08-13"

# Confidence a signal may claim when there is no evidence behind it.
# Without this cap, "insufficient history" silently returns the model's own
# number — and raw confidence was measured INVERTED: 90%+ signals won 28%.
# "We do not know" must not present as "90% sure".
NO_EVIDENCE_CEILING = 55.0

# Replayed outcomes are simulated against real bars under the current rules.
# The fills are perfect — no slippage, no partial fills, both the bar's high
# and low assumed reachable — so replay is systematically optimistic and
# counts for less than an observed result. It bootstraps calibration without
# pretending to BE live evidence.
REPLAY_WEIGHT = 0.5

_CACHE: dict = {"built_at": 0.0, "table": None}
_CACHE_TTL = 300.0
_LOCK = threading.Lock()


def _bucket_score(score) -> str:
    try:
        s = float(score)
    except (TypeError, ValueError):
        return "unknown"
    if s >= 80:
        return "80+"
    if s >= 70:
        return "70-79"
    if s >= 60:
        return "60-69"
    return "<60"


def _smoothed(wins: int, total: int) -> float:
    return (wins + PRIOR_WINS) / (total + PRIOR_TOTAL) * 100.0


def build_table(force: bool = False) -> dict:
    """Measured win rate per bucket, from recorded outcomes.

    A trade counts as a win on realised P&L, not on the `outcome` column:
    that column stores 'WIN'/'LOSS'/'BREAKEVEN' in UPPERCASE, and any query
    comparing it to 'win' silently matches nothing — which is exactly how a
    0/8899 = 0.0% win rate got computed while 2,839 wins sat in the table.
    """
    with _LOCK:
        if not force and _CACHE["table"] is not None and (time.time() - _CACHE["built_at"]) < _CACHE_TTL:
            return _CACHE["table"]

    table = {"timeframe": {}, "tf_score": {}, "score": {}, "strategy": {},
             "strategy_tf": {}, "overall": None}
    try:
        from app.database import get_db, TradingSignal, TradeOutcome
        with get_db() as db:
            rows = db.query(
                TradeOutcome.timeframe, TradeOutcome.pnl_pct,
                TradingSignal.composite_score, TradingSignal.strategy,
                TradeOutcome.outcome_source,
            ).outerjoin(
                TradingSignal, TradingSignal.id == TradeOutcome.signal_id
            ).filter(
                TradeOutcome.engine_epoch == CURRENT_EPOCH
            ).all()
    except Exception as e:
        logger.warning(f"[Calibration] could not read outcomes: {e}")
        return table

    def _tally(store, key, won, w=1.0):
        """Fractional tallies, so replayed evidence counts for less than
        observed evidence without needing a separate table."""
        cell = store.setdefault(key, {"wins": 0.0, "total": 0.0, "raw": 0})
        cell["total"] += w
        cell["wins"] += w if won else 0.0
        cell["raw"] += 1

    total_all = wins_all = 0
    for tf, pnl, score, strat, src in rows:
        try:
            won = float(pnl or 0) > 0
        except (TypeError, ValueError):
            continue
        w = REPLAY_WEIGHT if src == "replay" else 1.0
        total_all += w
        wins_all += w if won else 0.0
        if tf:
            _tally(table["timeframe"], tf, won, w)
            _tally(table["tf_score"], (tf, _bucket_score(score)), won, w)
        _tally(table["score"], _bucket_score(score), won, w)
        # Strategy attribution. The point of naming strategies is to be able
        # to say "breakouts work here and range fades do not" instead of
        # staring at one pooled win rate with nothing to act on.
        if strat:
            _tally(table["strategy"], strat, won, w)
            if tf:
                _tally(table["strategy_tf"], (strat, tf), won, w)

    table["overall"] = {"wins": wins_all, "total": total_all}
    for store in ("timeframe", "tf_score", "score", "strategy", "strategy_tf"):
        for key, cell in table[store].items():
            cell["win_rate"] = round(_smoothed(cell["wins"], cell["total"]), 1)

    with _LOCK:
        _CACHE["table"] = table
        _CACHE["built_at"] = time.time()
    logger.info(f"[Calibration] built from {total_all} outcomes — "
                f"{len(table['timeframe'])} timeframes, overall "
                f"{wins_all / max(1, total_all) * 100:.1f}%")
    return table


def lookup(timeframe: str | None, composite_score=None,
           strategy: str | None = None) -> dict:
    """Measured win rate for this kind of setup, most specific bucket first.

    Returns the rate, the sample behind it, and WHICH bucket answered — a
    win rate without its sample size is not evidence, it is an assertion,
    and the caller needs to know how much to trust it.
    """
    table = build_table()
    band = _bucket_score(composite_score)

    # "unknown" is not a predictive band — it is the rows whose signal had no
    # composite_score recorded, which during live scoring is EVERY signal,
    # since the score is still being computed when calibration runs. Treating
    # it as a bucket calibrated every fresh signal against an artifact of the
    # join (8,329 rows at 39%) instead of against anything about the setup.
    candidates = []
    # Strategy + timeframe is the most specific aggregate available: "range
    # fades on 1H" says far more than either half alone.
    if strategy and timeframe:
        candidates.append(((strategy, timeframe), "strategy_tf", f"{strategy} on {timeframe}"))
    if strategy:
        candidates.append((strategy, "strategy", f"{strategy}"))
    if band != "unknown":
        candidates.append(((timeframe, band), "tf_score", f"{timeframe}/{band}"))
    candidates.append((timeframe, "timeframe", f"{timeframe}"))
    if band != "unknown":
        candidates.append((band, "score", f"score {band}"))

    for key, store, label in candidates:
        if key is None or (isinstance(key, tuple) and key[0] is None):
            continue
        cell = table[store].get(key)
        if cell and cell["total"] >= MIN_SAMPLE:
            return {"win_rate": cell["win_rate"], "sample": cell["total"],
                    "bucket": label, "source": "measured"}

    o = table.get("overall") or {}
    if o.get("total", 0) >= MIN_SAMPLE:
        return {"win_rate": round(_smoothed(o["wins"], o["total"]), 1),
                "sample": o["total"], "bucket": "overall", "source": "measured"}
    return {"win_rate": None, "sample": o.get("total", 0),
            "bucket": None, "source": "insufficient_history"}


# Per-SYMBOL history earns a lower bar than the aggregate buckets, because
# it is far more specific: how this instrument has actually behaved beats
# how its timeframe behaves across every instrument.
MIN_SYMBOL_SAMPLE = 10

# Per-symbol evidence reaches full trust far sooner than an aggregate does,
# because it is answering a much narrower question. Forty trades on ONE
# instrument showing four wins is a confident statement about that
# instrument; forty scattered across every symbol and horizon is not. At the
# aggregate threshold that record only moved a 90% claim to 74.7.
FULL_TRUST_SYMBOL_SAMPLE = 50


def calibrate(raw_confidence: float, timeframe: str | None,
              composite_score=None, historical: dict | None = None,
              strategy: str | None = None) -> tuple[float, dict]:
    """Blend the model's stated confidence toward what actually happened.

    Evidence is taken from the MOST SPECIFIC source with enough of it:
    this symbol's own record first, then timeframe+score, then timeframe,
    then score band, then overall. A symbol that has lost 16 of 20 says
    more about the next trade on it than the aggregate behaviour of every
    1H setup ever taken.

    Weight rises with sample size and reaches 1.0 — evidence eventually
    wins outright. The previous implementation capped it at 0.35, so a
    signal claiming 90% still reported ~68% against a measured 28%, and
    every gate downstream inherited that optimism.
    """
    raw = max(0.0, min(100.0, float(raw_confidence or 0)))

    ev = None
    full_trust = FULL_TRUST_SAMPLE
    h = historical or {}
    h_total = int(h.get("total_trades") or 0)
    if h_total >= MIN_SYMBOL_SAMPLE:
        ev = {"win_rate": round(_smoothed(int(h.get("wins") or 0), h_total), 1),
              "sample": h_total, "bucket": "this symbol", "source": "measured"}
        full_trust = FULL_TRUST_SYMBOL_SAMPLE
    if ev is None:
        ev = lookup(timeframe, composite_score, strategy)
    if ev["win_rate"] is None:
        # No evidence. The model's own number cannot be trusted here — it was
        # measured inverted — so it is capped rather than passed through. A
        # signal with nothing behind it must not outrank one with a measured
        # edge simply because the model felt strongly about it.
        capped = min(raw, NO_EVIDENCE_CEILING)
        return capped, {**ev, "weight": 0.0, "raw": raw,
                        "capped_at": NO_EVIDENCE_CEILING if capped < raw else None}

    # Sample size says how RELIABLE the rate is; specificity says how much
    # it is about THIS setup. A base rate over 8,899 mixed trades is highly
    # reliable and barely relevant — trusting it at full weight would set
    # every signal to the same number and discard what little information
    # confidence carries. So the two are multiplied.
    weight = max(0.0, min(1.0, ev["sample"] / full_trust)) * _specificity(ev["bucket"])
    calibrated = raw * (1 - weight) + ev["win_rate"] * weight
    return round(max(1.0, min(99.0, calibrated)), 1), {
        **ev, "weight": round(weight, 3), "raw": raw,
    }


def _specificity(bucket: str | None) -> float:
    """How much a bucket's rate is about the setup in front of us."""
    if not bucket:
        return 0.0
    if bucket == "this symbol":
        return 1.0
    if " on " in bucket:          # strategy on a timeframe
        return 1.0
    if bucket in STRATEGY_NAMES:  # strategy across horizons
        return 0.95
    if bucket == "overall":
        return 0.35          # the base rate, not evidence about this setup
    if bucket.startswith("score "):
        return 0.6           # a score band across every instrument and horizon
    if "/" in bucket:
        return 1.0           # timeframe + score band — the most specific aggregate
    return 0.9               # timeframe alone


def timeframe_edge(timeframe: str | None) -> dict:
    """How this timeframe has actually performed, relative to all of them.

    Used to stop selection treating every horizon as equally viable. The
    generator picks 4H 60% of the time; 4H wins 27.8% while 1H wins 66.5%
    on 635 trades. Nothing in scoring reflected that, so a 4H signal
    claiming 85% outranked a 1H signal claiming 70% — and the measured
    ordering is the reverse.
    """
    table = build_table()
    cell = table["timeframe"].get(timeframe)
    o = table.get("overall") or {}
    base = _smoothed(o.get("wins", 0), o.get("total", 0)) if o.get("total") else 50.0
    if not cell or cell["total"] < MIN_SAMPLE:
        return {"edge": 0.0, "win_rate": None, "sample": cell["total"] if cell else 0,
                "baseline": round(base, 1), "source": "insufficient_history"}
    return {"edge": round(cell["win_rate"] - base, 1), "win_rate": cell["win_rate"],
            "sample": cell["total"], "baseline": round(base, 1), "source": "measured"}


def strategy_edge(strategy: str | None) -> dict:
    """How a named strategy has actually performed, against the baseline.

    This is what naming strategies was for: turning "the bot is 32%
    accurate" into "breakouts work here and range fades do not", which is
    something a person can act on.
    """
    table = build_table()
    cell = table["strategy"].get(strategy)
    o = table.get("overall") or {}
    base = _smoothed(o.get("wins", 0), o.get("total", 0)) if o.get("total") else 50.0
    if not cell or cell["total"] < MIN_SAMPLE:
        return {"edge": 0.0, "win_rate": None, "sample": cell["total"] if cell else 0,
                "baseline": round(base, 1), "source": "insufficient_history"}
    return {"edge": round(cell["win_rate"] - base, 1), "win_rate": cell["win_rate"],
            "sample": cell["total"], "baseline": round(base, 1), "source": "measured"}


def summary() -> dict:
    """Everything measured, for the UI and for answering "says who?"."""
    table = build_table()
    o = table.get("overall") or {}
    return {
        "overall_win_rate": round(_smoothed(o.get("wins", 0), o.get("total", 0)), 1) if o.get("total") else None,
        "sample": o.get("total", 0),
        "min_sample": MIN_SAMPLE,
        "full_trust_sample": FULL_TRUST_SAMPLE,
        "by_timeframe": sorted(
            ({"timeframe": tf, "win_rate": c["win_rate"], "sample": c["total"]}
             for tf, c in table["timeframe"].items() if c["total"] >= MIN_SAMPLE),
            key=lambda r: -r["win_rate"],
        ),
        "by_strategy": sorted(
            ({"strategy": st, "win_rate": c["win_rate"], "sample": c["total"]}
             for st, c in table["strategy"].items() if c["total"] >= MIN_SAMPLE),
            key=lambda r: -r["win_rate"],
        ),
        "by_strategy_timeframe": sorted(
            ({"strategy": k[0], "timeframe": k[1], "win_rate": c["win_rate"], "sample": c["total"]}
             for k, c in table["strategy_tf"].items() if c["total"] >= MIN_SAMPLE),
            key=lambda r: -r["win_rate"],
        ),
        "by_score": sorted(
            ({"band": b, "win_rate": c["win_rate"], "sample": c["total"]}
             for b, c in table["score"].items() if c["total"] >= MIN_SAMPLE),
            key=lambda r: -r["win_rate"],
        ),
    }
