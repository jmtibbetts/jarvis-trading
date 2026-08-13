"""ATR as the unit of measurement, so assets can be compared at all.

A 2% move means completely different things on NVDA and on SHIB. Every
threshold in this system that is expressed as a raw percentage is therefore
either too tight for one asset or too loose for another — and this codebase
has paid for that repeatedly:

    a fixed $15 stop against $12,000 of notional  -> a 0.12% trigger
    a fee schedule in percent applied per contract -> $6,047 to trade $1,800
    "one contract = one token"                     -> count scaled by price

Each was a quantity measured in one unit and consumed in another. ATR
normalization is the same fix applied to the feature layer: express every
distance as "how many typical bars of movement away is this", and BTC,
SHIB, NVDA, EUR/USD and crude become directly comparable.

WHAT THIS IS NOT: a signal. Nothing here votes on direction. These are
measurements that strategies interpret — 3 ATR from VWAP is stretched for a
mean-reversion setup and unremarkable for a breakout that just fired.

MISSING IS NOT ZERO. An unavailable measurement returns None. Zero ATR
distance means "at the level"; it must never be the value returned because
the data was absent.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Multi-horizon ATR: short reacts to the last few bars, long describes the
# instrument's ordinary state. Their ratio is what says whether volatility
# is expanding or contracting right now.
ATR_PERIODS = (5, 20, 100)

# How far the short/long ratio must move before volatility is called
# expanding or contracting rather than merely noisy.
EXPANSION_RATIO = 1.25
CONTRACTION_RATIO = 0.80

# Percentile history. Long enough to place today's volatility in context,
# short enough to still be about the current market.
PERCENTILE_LOOKBACK = 250


def _series_atr(high, low, close, period: int):
    """Wilder's ATR as a SERIES, so percentiles and ratios are possible.

    The engine's existing _atr returns only the latest value, which cannot
    answer "is this high for this instrument".
    """
    try:
        import pandas as pd  # noqa: F401
        prev_close = close.shift(1)
        tr = (high - low).combine(
            (high - prev_close).abs(), max
        ).combine((low - prev_close).abs(), max)
        return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    except Exception as e:
        logger.debug(f"[ATR] series computation failed for period {period}: {e}")
        return None


def _last(series) -> float | None:
    try:
        v = float(series.iloc[-1])
        return v if v == v and v > 0 else None      # v == v rejects NaN
    except Exception:
        return None


def compute_atr_profile(df) -> dict | None:
    """Multi-horizon ATR plus where current volatility sits historically.

    Returns None when there is not enough history — not a fabricated value.
    """
    try:
        high, low, close = df["high"], df["low"], df["close"]
        last_price = float(close.iloc[-1])
    except Exception:
        return None
    if not last_price or last_price <= 0:
        return None

    out: dict = {"periods": {}}
    series_by_period = {}
    for period in ATR_PERIODS:
        if len(df) < period + 1:
            out["periods"][f"atr_{period}"] = None
            continue
        s = _series_atr(high, low, close, period)
        v = _last(s) if s is not None else None
        series_by_period[period] = s
        out["periods"][f"atr_{period}"] = (
            {"value": round(v, 8), "pct": round(v / last_price * 100, 4)} if v else None
        )

    short = (out["periods"].get("atr_5") or {}).get("value")
    long_ = (out["periods"].get("atr_20") or {}).get("value")
    if short and long_:
        ratio = short / long_
        out["ratio_short_long"] = round(ratio, 3)
        out["expanding"] = ratio >= EXPANSION_RATIO
        out["contracting"] = ratio <= CONTRACTION_RATIO
        out["state"] = ("EXPANSION" if out["expanding"]
                        else "CONTRACTION" if out["contracting"] else "NORMAL")
    else:
        out["ratio_short_long"] = None
        out["expanding"] = out["contracting"] = None
        out["state"] = None

    # Percentile of the CURRENT atr_20 against this instrument's own recent
    # history. The point of a percentile is that it is self-referential:
    # "high for BTC" and "high for NVDA" are different absolute numbers.
    s20 = series_by_period.get(20)
    out["percentile"] = None
    if s20 is not None:
        try:
            hist = s20.dropna().tail(PERCENTILE_LOOKBACK)
            if len(hist) >= 30:
                cur = float(hist.iloc[-1])
                out["percentile"] = round(float((hist <= cur).sum()) / len(hist) * 100, 1)
                out["percentile_sample"] = int(len(hist))
        except Exception as e:
            logger.debug(f"[ATR] percentile failed: {e}")

    out["reference"] = long_ or short
    return out


def atr_distance(price: float | None, level: float | None,
                 atr_value: float | None) -> float | None:
    """How many ATRs `level` is from `price`. Signed: positive means the
    level is ABOVE the price.

    None when any input is missing — see the module docstring on why this
    must not be 0.0.
    """
    try:
        p, lv, a = float(price), float(level), float(atr_value)
    except (TypeError, ValueError):
        return None
    if not a or a <= 0 or not p:
        return None
    return round((lv - p) / a, 3)


def normalized_distances(tf_result: dict, atr_reference: float | None) -> dict:
    """Every level the TA engine found, expressed in ATRs from the price.

    This is what makes a threshold portable. "2% from VWAP" is meaningless
    across assets; "1.4 ATR from VWAP" is the same statement about
    stretch whether the instrument is SHIB or crude oil.
    """
    out: dict = {}
    price = ((tf_result or {}).get("price") or {}).get("last")
    if price is None or not atr_reference:
        return out

    def put(name, level):
        d = atr_distance(price, level, atr_reference)
        if d is not None:
            out[name] = d

    put("to_vwap", ((tf_result.get("vwap") or {}).get("value")))
    emas = tf_result.get("emas") or {}
    put("to_ema9", emas.get("ema9"))
    put("to_ema21", emas.get("ema21"))
    put("to_ema50", emas.get("ema50"))
    put("to_ema200", emas.get("ema200"))
    sr = tf_result.get("support_resistance") or {}
    put("to_support", sr.get("support"))
    put("to_resistance", sr.get("resistance"))
    bb = tf_result.get("bollinger_bands") or {}
    put("to_bb_upper", bb.get("upper"))
    put("to_bb_lower", bb.get("lower"))
    kc = tf_result.get("keltner") or {}
    put("to_keltner_upper", kc.get("upper"))
    put("to_keltner_lower", kc.get("lower"))
    dc = tf_result.get("donchian") or {}
    put("to_donchian_upper", dc.get("upper"))
    put("to_donchian_lower", dc.get("lower"))
    st = tf_result.get("supertrend") or {}
    put("to_supertrend", st.get("level"))
    return out


def signal_distances(entry: float | None, stop: float | None,
                     target: float | None, atr_value: float | None) -> dict:
    """A signal's own levels in ATRs.

    Answers the question a percentage cannot: is this stop inside the noise?
    A 1.5% stop is generous on NVDA and inside a single bar on SHIB. In ATR
    terms the same number means the same thing everywhere.
    """
    out: dict = {}
    d_stop = atr_distance(entry, stop, atr_value)
    d_target = atr_distance(entry, target, atr_value)
    if d_stop is not None:
        out["stop_atr"] = abs(d_stop)
    if d_target is not None:
        out["target_atr"] = abs(d_target)
    if out.get("stop_atr") and out.get("target_atr"):
        out["rr_in_atr"] = round(out["target_atr"] / out["stop_atr"], 2)
    if out.get("stop_atr") is not None:
        # Below roughly one ATR a stop sits inside a single ordinary bar of
        # movement, which is the noise-triggered exit this system spent a
        # day removing from the paper book.
        out["stop_inside_noise"] = out["stop_atr"] < 1.0
    return out
