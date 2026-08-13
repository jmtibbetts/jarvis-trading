"""Evidence-based signal scoring with freshness and calibration metadata."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


SIGNAL_VERSION = "v7.2"
TF_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1H": 3600, "2H": 7200, "4H": 14400, "1D": 86400,
}
EXPIRY_MINUTES = {
    "1m": 20, "3m": 30, "5m": 45, "15m": 120, "30m": 240,
    "1H": 480, "2H": 720, "4H": 1440, "1D": 4320,
}


def _valid_timeframes(ta_data: dict) -> list[tuple[str, dict]]:
    return [
        (timeframe, data) for timeframe, data in (ta_data or {}).items()
        if data and not data.get("error")
    ]


def _freshness_score(timeframe: str, data: dict) -> float:
    age = data.get("bar_age_seconds")
    expected = TF_SECONDS.get(timeframe, 14400)
    if age is None:
        # Missing/unknown freshness data must fail toward not-trading, not be
        # treated as fresh — 55.0 used to sit above MIN_SIGNAL_FRESHNESS (20),
        # silently letting stale/unverifiable bars through.
        return 0.0
    ratio = max(0.0, float(age)) / max(expected, 1)
    if ratio <= 2:
        return 100.0
    if ratio <= 4:
        return 80.0
    if ratio <= 8:
        return 55.0
    if ratio <= 24:
        return 25.0
    return 0.0


def _data_quality(valid: list[tuple[str, dict]]) -> float:
    if not valid:
        return 0.0
    coverage = min(100.0, len(valid) / 6 * 100)
    completeness = []
    for _, data in valid:
        fields = ("price", "rsi", "macd", "emas", "atr", "volume", "support_resistance")
        completeness.append(sum(data.get(field) is not None for field in fields) / len(fields) * 100)
    return round(coverage * 0.55 + sum(completeness) / len(completeness) * 0.45, 1)


# How much of the R:R component survives when Jarvis invented both legs.
# Not zero: a synthesised stop still encodes real volatility (ATR) and a
# real cost floor, so the geometry is not meaningless — but it is a far
# weaker claim than a level the market itself drew.
SELF_REFERENTIAL_RR_WEIGHT = 0.25

# How much of a timeframe's measured edge enters the composite score. A
# third: enough that 1H's +27.9 becomes a visible +9.3 and 4H's -10.7 a
# real -3.6, without letting the horizon outweigh the evidence for the
# individual setup.
TIMEFRAME_EDGE_WEIGHT = 1.0 / 3.0


def _calibrate_confidence(raw: float, historical: dict | None,
                          timeframe: str | None = None,
                          composite_score=None) -> tuple[float, dict]:
    """Blend the model's stated confidence toward what actually happened.

    Measured over 8,899 outcomes, raw confidence was INVERTED at the
    extremes — 90%+ signals won 28% while 60-69% signals won 44% — so the
    number the model reports carries essentially no information about
    whether the trade works.

    The previous implementation capped the evidence weight at 0.35, which
    meant the model's guess kept 65% of the vote however much history
    contradicted it: a 90% signal still reported ~68% against a measured
    28%. Evidence now wins outright once there is enough of it, and the
    bucket that answered travels with the number so "says who?" is always
    answerable. See lib/calibration.py.
    """
    try:
        from lib.calibration import calibrate as _cal
        calibrated, why = _cal(raw, timeframe, composite_score, historical=historical)
        return calibrated, {
            "sample_size": why.get("sample"),
            "empirical_win_rate": why.get("win_rate"),
            "weight": why.get("weight"),
            "bucket": why.get("bucket"),
            "source": why.get("source"),
        }
    except Exception as e:
        logger.debug(f"[Scorer] calibration unavailable ({e}) — falling back to per-symbol history")

    historical = historical or {}
    total = int(historical.get("total_trades") or 0)
    wins = int(historical.get("wins") or 0)
    if total < 5:
        return raw, {"sample_size": total, "empirical_win_rate": None, "weight": 0.0}
    empirical = (wins + 2) / (total + 4) * 100
    weight = min(0.35, total / 100)
    calibrated = raw * (1 - weight) + empirical * weight
    return round(max(5, min(95, calibrated)), 1), {
        "sample_size": total,
        "empirical_win_rate": round(empirical, 1),
        "weight": round(weight, 3),
        "source": "per_symbol_fallback",
    }


def _setup_type(signal: dict) -> str:
    if signal.get("setup_type"):
        return str(signal["setup_type"])
    timeframe = signal.get("timeframe") or "4H"
    if timeframe in {"1m", "3m", "5m"}:
        return "scalp"
    if timeframe in {"15m", "30m", "1H"}:
        return "intraday"
    if timeframe in {"2H", "4H"}:
        return "swing"
    return "position"


def _expiry(generated_at: datetime, timeframe: str) -> str:
    return (generated_at + timedelta(minutes=EXPIRY_MINUTES.get(timeframe, 1440))).isoformat()


def score_signal(signal: dict, ta_data: dict, regime: dict,
                 earnings_risk: bool = False, historical: dict | None = None,
                 news_confidence: float | None = None,
                 failure_adjustment: dict | None = None,
                 now: datetime | None = None) -> dict:
    """Return the signal enriched with an auditable 0-100 evidence score."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    valid = _valid_timeframes(ta_data)
    raw_confidence = max(0.0, min(100.0, float(signal.get("confidence") or 65)))
    # Calibrated against outcomes for THIS timeframe, which is the strongest
    # measured predictor: 1H wins 66.4% over 635 trades while 4H wins 27.8%
    # over 3,222. Passing it was the difference between calibrating on
    # something that predicts and calibrating on the symbol alone.
    calibrated, calibration = _calibrate_confidence(
        raw_confidence, historical, timeframe=signal.get("timeframe"),
        composite_score=signal.get("composite_score"),
    )

    direction = (signal.get("direction") or "Long").lower()
    is_short = direction.startswith("short")
    expected_bias = "bearish" if is_short else "bullish"
    opposing_bias = "bullish" if is_short else "bearish"

    aligned = 0.0
    opposing = 0
    for _, data in valid:
        bias = (data.get("bias") or "").lower()
        rsi = data.get("rsi") or 50
        macd = data.get("macd") or {}
        if bias == expected_bias:
            aligned += 1
        elif (is_short and rsi > 60) or (not is_short and rsi < 40):
            aligned += 0.5
        if macd.get("crossover") == expected_bias:
            aligned += 0.5
        if bias == opposing_bias:
            opposing += 1
    ta_confluence = min(100.0, aligned / len(valid) * 100) if valid else 50.0
    conflict_ratio = opposing / len(valid) if valid else 0.0

    entry = float(signal.get("entry_price") or 0)
    target = float(signal.get("target_price") or 0)
    stop = float(signal.get("stop_loss") or 0)
    if is_short:
        rr = (entry - target) / (stop - entry) if stop > entry > target > 0 else 0.0
    else:
        rr = (target - entry) / (entry - stop) if entry > stop > 0 and target > entry else 0.0
    rr_score = min(100.0, rr / 3 * 100)

    # A ratio Jarvis computed is not evidence Jarvis found. When BOTH the
    # stop and the target were synthesised from a fixed ATR multiple, their
    # ratio is a constant the system chose — scoring it as confluence lets
    # the model reward itself for its own defaults, and does so most loudly
    # on exactly the signals where the LLM gave no usable levels.
    from lib.signal_levels import rr_is_self_referential
    rr_self_referential = rr_is_self_referential(signal)
    if rr_self_referential:
        rr_score *= SELF_REFERENTIAL_RR_WEIGHT

    volume_ratios = [
        float((data.get("volume") or {}).get("surge_ratio") or 1.0)
        for _, data in valid if data.get("volume")
    ]
    best_volume = max(volume_ratios, default=float(signal.get("vol_ratio") or 1.0))
    volume_score = min(100.0, max(20.0, 45 + (best_volume - 1) * 35))
    liquidity_score = 80.0 if best_volume >= 1.0 else 55.0 if best_volume >= 0.6 else 25.0
    spread_pct = signal.get("spread_pct")
    if spread_pct is not None:
        liquidity_score = min(liquidity_score, max(0.0, 100 - float(spread_pct) * 40))

    atr_values = [
        float((data.get("atr") or {}).get("pct") or 0)
        for _, data in valid if (data.get("atr") or {}).get("pct") is not None
    ]
    atr_pct = next((value for value in atr_values if value > 0), 0.0)
    if not atr_pct:
        volatility_score = 50.0
    elif 0.4 <= atr_pct <= 6:
        volatility_score = 85.0
    elif atr_pct <= 10:
        volatility_score = 55.0
    else:
        volatility_score = 20.0

    regime_risk = (regime or {}).get("risk", "unknown")
    regime_score = {
        "low": 60 if is_short else 85,
        "medium": 65,
        "medium-high": 65 if is_short else 45,
        "high": 75 if is_short else 25,
        "unknown": 50,
    }.get(regime_risk, 50)

    selected_tf = signal.get("timeframe") or "4H"
    selected_data = (ta_data or {}).get(selected_tf) or {}
    freshness = _freshness_score(selected_tf, selected_data)
    if not selected_data and valid:
        freshness = max(_freshness_score(tf, data) for tf, data in valid)
    quality = _data_quality(valid)
    news_score = max(0.0, min(100.0, float(news_confidence if news_confidence is not None else 50)))

    conflict_penalty = -12 if conflict_ratio >= 0.5 else -6 if conflict_ratio >= 0.34 else 0
    stale_penalty = -15 if freshness < 25 else -7 if freshness < 50 else 0
    earnings_penalty = -25 if earnings_risk else 0
    # Failure-memory penalty (lib/postmortem.get_failure_adjustment): this
    # symbol/setup bucket has a recurring, classified failure history. The
    # adjustment dict carries its own reason breakdown, surfaced in the
    # score_breakdown below so the penalty is explainable, not a vibe.
    failure_penalty = -float((failure_adjustment or {}).get("penalty") or 0)

    values = {
        "calibrated_confidence": calibrated,
        "ta_confluence": ta_confluence,
        "rr": rr_score,
        "rr_self_referential": rr_self_referential,
        "volume": volume_score,
        "regime": regime_score,
        "data_quality": quality,
        "freshness": freshness,
        "liquidity": liquidity_score,
        "volatility": volatility_score,
        "news": news_score,
    }
    weights = {
        "calibrated_confidence": 0.18, "ta_confluence": 0.20, "rr": 0.15,
        "volume": 0.08, "regime": 0.08, "data_quality": 0.10,
        "freshness": 0.07, "liquidity": 0.05, "volatility": 0.04, "news": 0.05,
    }
    composite = sum(values[key] * weights[key] for key in weights)
    composite += conflict_penalty + stale_penalty + earnings_penalty + failure_penalty

    # ── Timeframe edge ──────────────────────────────────────────────────
    # Nothing in scoring reflected that horizons perform differently, so a
    # 4H signal claiming 85% outranked a 1H signal claiming 70% — while the
    # measured ordering is the reverse:
    #
    #     1H   66.4% over   635 trades   edge +27.9
    #     1D   42.2% over 4,597 trades   edge  +3.7
    #     4H   27.8% over 3,222 trades   edge -10.7
    #
    # The generator picks 4H 60% of the time. This is what makes the 1H
    # bucket visible next to it. Damped to a third of the raw edge so a
    # timeframe cannot dominate the setup's own evidence, and applied only
    # where the sample is large enough to mean something.
    tf_edge_applied = 0.0
    tf_edge_meta = None
    try:
        from lib.calibration import timeframe_edge
        te = timeframe_edge(signal.get("timeframe"))
        if te.get("win_rate") is not None:
            tf_edge_applied = round(te["edge"] * TIMEFRAME_EDGE_WEIGHT, 2)
            composite += tf_edge_applied
            tf_edge_meta = te
    except Exception as e:
        logger.debug(f"[Scorer] timeframe edge unavailable: {e}")

    bar_times = [data.get("bar_time") for _, data in valid if data.get("bar_time")]
    market_data_at = max(bar_times) if bar_times else None
    setup_type = _setup_type(signal)
    if is_short:
        invalidation = f"Invalid above {stop:g}; also invalidate if higher-timeframe bias turns bullish."
    else:
        invalidation = f"Invalid below {stop:g}; also invalidate if higher-timeframe bias turns bearish."

    signal.update({
        "composite_score": round(max(0, min(100, composite)), 1),
        "calibrated_confidence": calibrated,
        "score_breakdown": {
            **{key: round(value, 1) for key, value in values.items()},
            "raw_llm_confidence": round(raw_confidence, 1),
            "calibration": calibration,
            "timeframe_edge": tf_edge_applied,
            "timeframe_evidence": tf_edge_meta,
            "conflict_ratio": round(conflict_ratio, 3),
            "conflict_penalty": conflict_penalty,
            "stale_penalty": stale_penalty,
            "earnings_penalty": earnings_penalty,
            "failure_penalty": failure_penalty,
            "failure_history": failure_adjustment,
        },
        "data_quality_score": round(quality, 1),
        "freshness_score": round(freshness, 1),
        "news_confidence": round(news_score, 1),
        "earnings_risk": earnings_risk,
        "rr_ratio": round(rr, 2) if rr else None,
        "setup_type": setup_type,
        "invalidation": signal.get("invalidation") or invalidation,
        "signal_version": SIGNAL_VERSION,
        "market_data_at": market_data_at,
        "expires_at": _expiry(now, selected_tf),
    })
    return signal
