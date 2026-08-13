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



# ── Phase 6 strategies ──────────────────────────────────────────────────
# These need lib/structure.py: a level with a history, and a break with a
# verdict. Before that existed, "price is above resistance" was the whole
# vocabulary, and three of the strategies below are distinguished purely by
# what a break DID afterwards — held, failed, or swept. They were
# unbuildable, not merely unbuilt.


def _breaks(d: dict) -> list:
    return ((d.get("structure") or {}).get("breaks") or [])


def _break_of(d: dict, outcome: str, want_up: bool):
    """The most recent break with this outcome in the given direction."""
    for b in _breaks(d):
        if b.get("outcome") == outcome and (b.get("direction") == "up") == want_up:
            return b
    return None


def _breakout_retest(d: dict, is_short: bool) -> tuple[float, list]:
    """A level broke, held, and price has come back to it.

    The highest-quality entry in the set and the one most often missed: the
    breakout bar is where the chase happens, the retest is where the risk
    is defined.
    """
    hits, total = [], 4
    b = _break_of(d, "held", want_up=not is_short)
    if not b:
        return 0.0, []                      # NECESSARY: a break that held
    hits.append(f"broke {b['level_price']:g} and held")

    atrs = d.get("atr_distances") or {}
    back = _f(atrs.get("to_resistance") if is_short else atrs.get("to_support"))
    if back is not None and abs(back) <= 1.0:
        hits.append(f"price back within {abs(back):.2f} ATR of the level")
    if (b.get("break_volume_ratio") or 0) >= 1.2:
        hits.append(f"broke on {b['break_volume_ratio']:.1f}x volume")
    if (b.get("bars_ago") or 99) <= 8:
        hits.append(f"break is {b['bars_ago']} bars old — still live")
    return len(hits) / total, hits


def _failed_breakout(d: dict, is_short: bool) -> tuple[float, list]:
    """A break in the OPPOSITE direction that was reclaimed.

    The trade is against the break: whoever chased it is now offside, and
    their exits are the fuel. Trading WITH a failed break is the error this
    condition exists to prevent.
    """
    hits, total = [], 3
    b = _break_of(d, "failed", want_up=is_short)   # up-break failed -> short
    if not b:
        return 0.0, []                      # NECESSARY: a failed break the other way
    hits.append(f"{b['detail']} at {b['level_price']:g}")
    if (b.get("bars_ago") or 99) <= 5:
        hits.append(f"failure is {b['bars_ago']} bars old")
    if (b.get("distance_atr") or 0) >= 0.5:
        hits.append(f"travelled {b['distance_atr']:.1f} ATR before failing — trapped size")
    return len(hits) / total, hits


def _liquidity_sweep_reversal(d: dict, is_short: bool) -> tuple[float, list]:
    """A wick took a level and closed straight back inside.

    Stops were taken and nothing followed. Reads as a breakout on any rule
    that only asks whether price exceeded the level.
    """
    hits, total = [], 3
    b = _break_of(d, "sweep", want_up=is_short)    # swept the highs -> short
    if not b:
        return 0.0, []                      # NECESSARY: an actual sweep
    hits.append(f"swept {b['level_price']:g} — {b['detail']}")
    if (b.get("bars_ago") or 99) <= 3:
        hits.append(f"sweep is {b['bars_ago']} bars old")
    if (b.get("break_volume_ratio") or 0) >= 1.3:
        hits.append(f"{b['break_volume_ratio']:.1f}x volume into the sweep")
    return len(hits) / total, hits


def _squeeze_expansion(d: dict, is_short: bool) -> tuple[float, list]:
    """Volatility compressed, and it is now expanding in this direction.

    Compression alone is not a trade — it says a move is coming, not which
    way. The expansion is what makes it directional.
    """
    hits, total = [], 4
    prof = d.get("atr_profile") or {}
    state = str(prof.get("state") or "").upper()
    pctile = _f(prof.get("percentile"))
    was_compressed = state == "CONTRACTION" or (pctile is not None and pctile <= 25)
    if not was_compressed:
        return 0.0, []                      # NECESSARY: it was actually coiled
    hits.append(f"volatility compressed ({state.lower() or 'low percentile'})")

    if prof.get("expanding"):
        hits.append("ATR now expanding")
    don = d.get("donchian") or {}
    if don.get("breakout_down" if is_short else "breakout_up"):
        hits.append("breaking the channel in this direction")
    st = _g(d, "supertrend", "direction")
    if st == ("down" if is_short else "up"):
        hits.append(f"Supertrend {st}")
    return len(hits) / total, hits


def _momentum_ignition(d: dict, is_short: bool) -> tuple[float, list]:
    """A sudden expansion in range AND participation together.

    Volume without range is absorption; range without volume is a gap
    nobody traded. Requiring both is what separates this from noise.
    """
    hits, total = [], 4
    vol = d.get("volume") or {}
    surge = _f(vol.get("surge_ratio"))
    if surge is None or surge < 1.5:
        return 0.0, []                      # NECESSARY: real participation
    hits.append(f"volume {surge:.1f}x average")

    prof = d.get("atr_profile") or {}
    if prof.get("expanding"):
        hits.append("range expanding")
    if _g(d, "supertrend", "flipped_this_bar"):
        hits.append("Supertrend flipped this bar")
    macd = _g(d, "macd", "crossover")
    if macd == ("bearish" if is_short else "bullish"):
        hits.append(f"MACD crossed {macd}")
    return len(hits) / total, hits


def _vwap_reclaim(d: dict, is_short: bool) -> tuple[float, list]:
    """Price crossing back through VWAP, the level most desks anchor to.

    Proximity is a NECESSARY condition, not a bonus. Position alone is a
    STATE, not a reclaim: a name that has sat 8 ATR above VWAP for three
    weeks satisfies "above" and has reclaimed nothing. Measured live, that
    version matched 10 of 36 chart/direction combinations — more than any
    other strategy — which would have made "vwap_reclaim" the label on
    every trending chart and diluted the attribution it exists to collect.
    """
    hits, total = [], 3
    vwap = d.get("vwap") or {}
    pos = vwap.get("position")
    want = "below" if is_short else "above"
    if pos != want:
        return 0.0, []                      # NECESSARY: on the right side of it

    atrs = d.get("atr_distances") or {}
    to_vwap = _f(atrs.get("to_vwap"))
    if to_vwap is None or abs(to_vwap) > VWAP_RECLAIM_ATR:
        return 0.0, []                      # NECESSARY: recently crossed, not merely above
    hits.append(f"reclaimed VWAP from {want}, {abs(to_vwap):.2f} ATR away")

    if abs(to_vwap) <= VWAP_RECLAIM_ATR / 2:
        hits.append("still hugging VWAP — entry and invalidation are tight")
    bias = str(d.get("bias") or "").lower()
    if bias == ("bearish" if is_short else "bullish"):
        hits.append(f"timeframe bias {bias}")
    return len(hits) / total, hits


def _relative_strength_breakout(d: dict, is_short: bool) -> tuple[float, list]:
    """Outperforming its benchmark AND breaking its own structure.

    Relative strength alone is a ranking, not a trade. The break is the
    trigger; the strength is why this name rather than another.
    """
    hits, total = [], 3
    rs = d.get("relative_strength") or {}
    state = str(rs.get("state") or "").lower()
    slope = _f(rs.get("rs_slope"))
    if is_short:
        leading = "underperform" in state or "laggard" in state
    else:
        leading = "outperform" in state or "leader" in state
    if not leading:
        return 0.0, []                      # NECESSARY: it is actually leading
    hits.append(f"relative strength: {state}")

    if slope is not None and abs(slope) > 1e-9 and ((slope < 0) == is_short):
        hits.append("relative strength still improving in this direction")
    don = d.get("donchian") or {}
    if don.get("breakout_down" if is_short else "breakout_up") or rs.get("rs_breakout"):
        hits.append("breaking out")
    return len(hits) / total, hits


def _funding_squeeze(d: dict, is_short: bool) -> tuple[float, list]:
    """Crowded positioning against the trade direction.

    Crypto only, and only where a real funding number exists. Contrarian by
    construction: crowded longs paying to stay long are fuel for a move
    down, not confirmation of one up.
    """
    hits, total = [], 3
    dv = d.get("derivatives") or {}
    funding = _f(dv.get("funding_rate"))
    if funding is None:
        return 0.0, []                      # NECESSARY: real positioning data
    crowded_against = funding > 0.0005 if is_short else funding < -0.0005
    if not crowded_against:
        return 0.0, []
    hits.append(f"funding {funding * 100:.3f}% — the crowd is on the other side")

    oi = _f(dv.get("oi_change_pct"))
    if oi is not None and oi >= 5:
        hits.append(f"open interest +{oi:.1f}% — the crowded side is still adding")
    bias = str(d.get("bias") or "").lower()
    if bias == ("bearish" if is_short else "bullish"):
        hits.append(f"price bias {bias} agrees")
    return len(hits) / total, hits


def _divergence_reversal(d: dict, is_short: bool) -> tuple[float, list]:
    """Price made a new extreme that momentum did not confirm.

    Built on confirmed swings only (lib/structure.py), so it cannot be read
    off the in-progress bar — which is where every repainting divergence
    indicator gets its impressive backtest.
    """
    hits, total = [], 3
    want = "bearish" if is_short else "bullish"
    divs = [x for x in ((d.get("structure") or {}).get("divergences") or [])
            if x.get("bias") == want and x.get("regular")]
    if not divs:
        return 0.0, []                      # NECESSARY: a regular divergence
    best = max(divs, key=lambda x: x.get("strength", 0))
    hits.append(f"{best['kind'].replace('_', ' ')} on {best['indicator']}")
    if len(divs) >= 2:
        hits.append(f"confirmed across {len(divs)} indicators")
    if (best.get("age_bars") or 99) <= 5:
        hits.append(f"{best['age_bars']} bars old — still current")
    return len(hits) / total, hits


STRATEGIES = {
    "breakout": _breakout,
    "trend_continuation": _trend_continuation,
    "mean_reversion": _mean_reversion,
    "range_fade": _range_fade,
    "momentum": _momentum,
    # Phase 6. Each needs the structure engine, relative strength, or the
    # derivatives feed — all of which now exist.
    "breakout_retest": _breakout_retest,
    "failed_breakout": _failed_breakout,
    "liquidity_sweep_reversal": _liquidity_sweep_reversal,
    "squeeze_expansion": _squeeze_expansion,
    "momentum_ignition": _momentum_ignition,
    "vwap_reclaim": _vwap_reclaim,
    "relative_strength_breakout": _relative_strength_breakout,
    "funding_squeeze": _funding_squeeze,
    "divergence_reversal": _divergence_reversal,
}

# Deliberately NOT built: absorption reversal, and liquidation cascade as a
# per-bar strategy. Both need aggressor-side data — who crossed the spread,
# and into what resting size. Free OHLCV cannot distinguish absorption from
# ordinary two-way volume, and a detector built from bar volume alone would
# produce confident labels from a measurement that was never taken. The gap
# is documented rather than approximated; the upgrade docs make the same
# call about full-universe CVD.
UNBUILDABLE_WITHOUT_ORDER_FLOW = ("absorption_reversal", "liquidation_cascade")

# A setup must meet at least this fraction of a strategy's conditions to be
# tagged with it. Below the bar it is UNCLASSIFIED — deliberately, because
# forcing a weak match into the nearest bucket would poison the statistics
# the tagging exists to collect.
MIN_MATCH = 0.5

# How close to VWAP still counts as a reclaim rather than a position. Beyond
# this the level has been left behind and is no longer the reference the
# trade is being taken against.
VWAP_RECLAIM_ATR = 1.5


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
