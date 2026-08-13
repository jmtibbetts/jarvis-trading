"""Named, testable trading strategies — so performance can be attributed.

There was one strategy here: "the LLM reads the indicators and decides".
That is unattributable. When it loses you cannot tell whether breakouts are
failing, mean reversion is failing, or the model is simply guessing — so
there is nothing to fix, only a win rate to stare at.

Each strategy below states its own entry conditions in code against the
indicators the TA engine already computes. A signal is then TAGGED with the
strategy it matches, and lib/calibration.py scores strategies exactly as it
now scores timeframes:

    1H   66.4% over   635 trades   edge +27.9
    4H   27.8% over 3,222 trades   edge -10.7

The point is to be able to say "breakouts work here and range fades do not"
instead of "the bot is 32% accurate".

DESIGN RULES, learned the hard way in this codebase:

  - Classification is DETERMINISTIC. No LLM decides what strategy a setup
    is, or the label would drift and the attribution would be worthless.

  - Every match reports the conditions that fired. A tag without its
    evidence is an assertion; the whole point is to be able to check it.

  - A setup matching nothing is UNCLASSIFIED, not forced into the closest
    bucket. Forcing it would poison the very statistics being collected —
    the same error as calibrating against an "unknown" score band.

  - Conditions are checked against the SIGNAL'S OWN DIRECTION. A bullish
    breakout and a bearish breakdown are the same strategy; a long entered
    on bearish structure is neither.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _f(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _g(data: dict, *path, default=None):
    cur = data or {}
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


# ── Strategy definitions ────────────────────────────────────────────────
# Each returns (score 0-1, [conditions that fired]). Score is the fraction
# of the strategy's own conditions met — NOT a probability of profit. What
# a match is worth is decided by measured outcomes, never asserted here.

def _breakout(d: dict, is_short: bool) -> tuple[float, list]:
    """Price leaving a range on expanding volume.

    REQUIRES the channel to be broken IN THIS DIRECTION. Without that it is
    not a breakout at all, and scoring it on volume and ADX alone matched a
    short against an upward break.

    The distinguishing feature beyond the break is VOLUME: a breakout
    without it is a fake that the range will reclaim.
    """
    if _g(d, "donchian", "breakout_down" if is_short else "breakout_up") is not True:
        return 0.0, []
    hits, total = [], 4
    hits.append("donchian channel broken")
    surge = _f(_g(d, "volume", "surge_ratio"), 1.0)
    if _g(d, "volume", "surge") is True or surge >= 1.5:
        hits.append(f"volume surge {surge:.1f}x")
    pos = _g(d, "bollinger_bands", "position") or ""
    if (is_short and "lower" in str(pos).lower()) or (not is_short and "upper" in str(pos).lower()):
        hits.append(f"outside bollinger band ({pos})")
    if _g(d, "adx", "strong") is True:
        hits.append(f"ADX {_f(_g(d, 'adx', 'value'), 0):.0f} — trending")
    return len(hits) / total, hits


def _trend_continuation(d: dict, is_short: bool) -> tuple[float, list]:
    """An established trend resuming after a pause. Structure and EMAs
    aligned, momentum agreeing, no exhaustion."""
    # REQUIRES a trend to actually exist in this direction — either the
    # supertrend or the EMA stack. "Continuation" of nothing is not a setup.
    st = str(_g(d, "supertrend", "direction") or "").lower()
    st_ok = (is_short and st in ("down", "bearish")) or (not is_short and st in ("up", "bullish"))
    e9_, e21_, e50_ = (_f(_g(d, "emas", k)) for k in ("ema9", "ema21", "ema50"))
    ema_ok = None not in (e9_, e21_, e50_) and (
        (e9_ < e21_ < e50_) if is_short else (e9_ > e21_ > e50_))
    if not (st_ok or ema_ok):
        return 0.0, []
    hits, total = [], 5
    if st_ok:
        hits.append(f"supertrend {st}")
    e9, e21, e50 = (_f(_g(d, "emas", k)) for k in ("ema9", "ema21", "ema50"))
    if None not in (e9, e21, e50):
        if (is_short and e9 < e21 < e50) or (not is_short and e9 > e21 > e50):
            hits.append("EMAs stacked in trend order")
    if str(_g(d, "macd", "trend") or "").lower() == ("bearish" if is_short else "bullish"):
        hits.append("MACD trending with the move")
    struct = str(_g(d, "market_structure", "structure") or "").lower()
    if ("lower" in struct if is_short else "higher" in struct):
        hits.append(f"structure: {struct}")
    obv = str(_g(d, "obv_trend") or "").lower()
    if (is_short and "down" in obv) or (not is_short and "up" in obv):
        hits.append(f"OBV {obv} — volume confirms")
    return len(hits) / total, hits


def _mean_reversion(d: dict, is_short: bool) -> tuple[float, list]:
    """Stretched far enough from value to snap back. The opposite posture
    to breakout — here an extreme is the ENTRY, not a warning."""
    # REQUIRES a genuine extreme somewhere. Mean reversion without one is
    # just a guess about direction.
    rsi = _f(_g(d, "rsi"))
    pct_b0 = _f(_g(d, "bollinger_bands", "pct_b"))
    stretched = (
        (rsi is not None and ((is_short and rsi >= 70) or (not is_short and rsi <= 30)))
        or (pct_b0 is not None and ((is_short and pct_b0 >= 1.0) or (not is_short and pct_b0 <= 0.0)))
    )
    if not stretched:
        return 0.0, []
    hits, total = [], 5
    if rsi is not None and ((is_short and rsi >= 70) or (not is_short and rsi <= 30)):
        hits.append(f"RSI {rsi:.0f} — {'overbought' if is_short else 'oversold'}")
    pct_b = _f(_g(d, "bollinger_bands", "pct_b"))
    if pct_b is not None and ((is_short and pct_b >= 1.0) or (not is_short and pct_b <= 0.0)):
        hits.append(f"outside band (%B {pct_b:.2f})")
    wr = _f(_g(d, "williams_r"))
    if wr is not None and ((is_short and wr >= -20) or (not is_short and wr <= -80)):
        hits.append(f"Williams %R {wr:.0f}")
    mfi = _f(_g(d, "mfi"))
    if mfi is not None and ((is_short and mfi >= 80) or (not is_short and mfi <= 20)):
        hits.append(f"MFI {mfi:.0f} — money flow extreme")
    stoch = str(_g(d, "stochastic", "signal") or "").lower()
    if (is_short and "overbought" in stoch) or (not is_short and "oversold" in stoch):
        hits.append(f"stochastic {stoch}")
    return len(hits) / total, hits


def _range_fade(d: dict, is_short: bool) -> tuple[float, list]:
    """Selling the top / buying the bottom of a range that is HOLDING.
    Distinct from mean reversion: this needs a defined range and a weak
    trend, and it is the strategy a breakout invalidates."""
    # REQUIRES price to be at the edge it is being faded from. Without this
    # the strategy scored on ABSENCE — no trend, no breakout, a range exists
    # — which is true of every featureless chart, so a flat market with no
    # setup at all classified as a range fade.
    pos = _f(_g(d, "support_resistance", "position_in_range"))
    at_edge = pos is not None and ((is_short and pos >= 0.8) or (not is_short and pos <= 0.2))
    if not at_edge:
        return 0.0, []
    hits, total = [], 4
    hits.append(f"at range {'top' if is_short else 'bottom'} ({pos:.0%})")
    if _g(d, "adx", "strong") is False or (_f(_g(d, "adx", "value"), 100) < 20):
        hits.append(f"ADX {_f(_g(d, 'adx', 'value'), 0):.0f} — no trend")
    rng = _f(_g(d, "support_resistance", "range_pct"))
    if rng is not None and rng > 0:
        hits.append(f"defined range {rng:.1f}% wide")
    if _g(d, "donchian", "breakout_up") is not True and _g(d, "donchian", "breakout_down") is not True:
        hits.append("range intact — no channel break")
    return len(hits) / total, hits


def _momentum(d: dict, is_short: bool) -> tuple[float, list]:
    """A fresh impulse: the turn just happened, and volume came with it."""
    # REQUIRES a fresh turn — a flip or a crossover this bar. Momentum
    # without an impulse is just trend continuation wearing its coat.
    flipped = _g(d, "supertrend", "flipped_this_bar") is True
    crossed = str(_g(d, "macd", "crossover") or "").lower() == ("bearish" if is_short else "bullish")
    if not (flipped or crossed):
        return 0.0, []
    hits, total = [], 4
    if flipped:
        hits.append("supertrend flipped this bar")
    if crossed:
        hits.append("MACD crossover")
    hist = _f(_g(d, "macd", "histogram"))
    if hist is not None and ((is_short and hist < 0) or (not is_short and hist > 0)):
        hits.append(f"MACD histogram {hist:+.4f}")
    vwap_pos = str(_g(d, "vwap", "position") or "").lower()
    if (is_short and "below" in vwap_pos) or (not is_short and "above" in vwap_pos):
        hits.append(f"price {vwap_pos} VWAP")
    return len(hits) / total, hits


STRATEGIES = {
    "breakout": _breakout,
    "trend_continuation": _trend_continuation,
    "mean_reversion": _mean_reversion,
    "range_fade": _range_fade,
    "momentum": _momentum,
}

# A setup must meet at least this fraction of a strategy's conditions to be
# tagged with it. Below the bar it is UNCLASSIFIED — deliberately, because
# forcing a weak match into the nearest bucket would poison the statistics
# the tagging exists to collect.
MIN_MATCH = 0.5


def classify(ta_timeframe_data: dict, direction: str | None) -> dict:
    """Which strategy this setup is, judged against its own direction.

    Returns the best match with the conditions that fired, plus every
    strategy's score so a near-miss is visible rather than hidden.
    """
    is_short = str(direction or "Long").lower().startswith("short")
    d = ta_timeframe_data or {}

    scored = {}
    for name, fn in STRATEGIES.items():
        try:
            score, hits = fn(d, is_short)
        except Exception as e:
            logger.debug(f"[Strategies] {name} failed to evaluate: {e}")
            score, hits = 0.0, []
        scored[name] = {"score": round(score, 3), "conditions": hits}

    best = max(scored.items(), key=lambda kv: kv[1]["score"])
    name, detail = best
    if detail["score"] < MIN_MATCH:
        return {
            "strategy": None, "score": detail["score"], "conditions": [],
            "reason": (f"no strategy met {MIN_MATCH:.0%} of its conditions "
                       f"(best: {name} at {detail['score']:.0%})"),
            "all": scored,
        }
    return {
        "strategy": name, "score": detail["score"],
        "conditions": detail["conditions"],
        "reason": f"{name}: {', '.join(detail['conditions'])}",
        "all": scored,
    }


def classify_signal(signal: dict, ta_profile: dict) -> dict:
    """Classify using the timeframe the signal was actually taken on."""
    tf = signal.get("timeframe")
    data = (ta_profile or {}).get(tf)
    if not data or data.get("error"):
        # Fall back to any usable timeframe rather than guessing blind, but
        # say which was used — a strategy read off a different horizon than
        # the trade is a weaker claim.
        for alt, alt_data in (ta_profile or {}).items():
            if alt_data and not alt_data.get("error"):
                out = classify(alt_data, signal.get("direction"))
                out["timeframe_used"] = alt
                out["timeframe_requested"] = tf
                return out
        return {"strategy": None, "score": 0.0, "conditions": [],
                "reason": "no usable TA to classify from", "all": {}}
    out = classify(data, signal.get("direction"))
    out["timeframe_used"] = tf
    return out
