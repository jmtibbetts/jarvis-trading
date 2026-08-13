"""
TA engine extensions — the indicators and market-structure detection the
original engine (lib/ta_engine.py) lacked: Williams %R, CCI, MFI, Keltner
Channels, Donchian Channels, Supertrend, classic pivot points, and swing-based
market structure (HH/HL/LH/LL, BOS, CHoCH).

Deliberately pure pandas/numpy — no TA-Lib/ta dependency — so results are
identical under either backend the main engine happens to detect, and every
function is unit-testable against hand-computable fixtures. All formulas are
the standard textbook definitions (noted per function); nothing here is an
invented indicator.

Market-structure vocabulary (standard usage, not this module's invention):
  swing high/low  local extremum confirmed by `window` bars on each side
  HH/HL/LH/LL     higher-high / higher-low / lower-high / lower-low, from the
                  last two confirmed swings of each kind
  BOS             break of structure — close beyond the last confirmed swing
                  in the direction of the prevailing structure
  CHoCH           change of character — close beyond the last confirmed swing
                  AGAINST the prevailing structure (earliest reversal hint)

Confirmation lag is inherent and honest: a swing needs `window` bars after it
to be confirmed, so the most recent `window` bars can never contain a
confirmed swing. Detecting swings "instantly" would be repainting.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _r(value, sig: int = 8):
    """Round a PRICE to significant figures, never to fixed decimals.

    _r(x) silently destroys sub-cent assets: SHIB at $0.00000449
    becomes $0.000004, a 10.9% error, and every level derived from it
    inherits that error before any strategy sees it. Significant figures
    keep the same relative precision at $0.0000045 and at $63,000.
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return value
    if v == 0 or v != v or v in (float("inf"), float("-inf")):
        return v
    import math
    digits = sig - 1 - math.floor(math.log10(abs(v)))
    return round(v, max(0, digits)) if digits > 0 else round(v, 0)



def williams_r(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> float | None:
    """Williams %R: (highest_high - close) / (highest_high - lowest_low) * -100.
    Range -100..0; below -80 oversold, above -20 overbought (convention)."""
    if len(close) < period:
        return None
    hh = float(high.tail(period).max())
    ll = float(low.tail(period).min())
    if hh == ll:
        return None
    return round((hh - float(close.iloc[-1])) / (hh - ll) * -100, 2)


def cci(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 20) -> float | None:
    """Commodity Channel Index, Lambert's original: (TP - SMA(TP)) / (0.015 * mean deviation)."""
    if len(close) < period:
        return None
    tp = (high + low + close) / 3
    sma = tp.rolling(period).mean()
    mad = tp.rolling(period).apply(lambda x: np.mean(np.abs(x - x.mean())), raw=True)
    denom = 0.015 * float(mad.iloc[-1])
    if not denom:
        return None
    return round((float(tp.iloc[-1]) - float(sma.iloc[-1])) / denom, 2)


def mfi(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series, period: int = 14) -> float | None:
    """Money Flow Index — volume-weighted RSI over typical price."""
    if len(close) < period + 1:
        return None
    tp = (high + low + close) / 3
    raw_flow = tp * volume
    delta = tp.diff()
    pos = raw_flow.where(delta > 0, 0.0).tail(period).sum()
    neg = raw_flow.where(delta < 0, 0.0).tail(period).sum()
    if neg == 0:
        return 100.0 if pos > 0 else None
    ratio = float(pos) / float(neg)
    return round(100 - 100 / (1 + ratio), 2)


def keltner_channels(high: pd.Series, low: pd.Series, close: pd.Series,
                     ema_period: int = 20, atr_period: int = 10, mult: float = 2.0) -> dict | None:
    """Keltner Channels: EMA(close) ± mult * ATR."""
    if len(close) < max(ema_period, atr_period) + 1:
        return None
    mid = float(close.ewm(span=ema_period, adjust=False).mean().iloc[-1])
    atr = _atr_series(high, low, close, atr_period)
    if atr is None:
        return None
    a = float(atr.iloc[-1])
    upper, lower = mid + mult * a, mid - mult * a
    last = float(close.iloc[-1])
    return {
        "upper": _r(upper), "mid": _r(mid), "lower": _r(lower),
        "position": "above_upper" if last > upper else "below_lower" if last < lower else "inside",
    }


def donchian_channels(high: pd.Series, low: pd.Series, period: int = 20) -> dict | None:
    """Donchian Channels: highest high / lowest low of the prior `period` bars
    (excluding the current bar, so a breakout is measurable against them)."""
    if len(high) < period + 1:
        return None
    upper = float(high.iloc[-(period + 1):-1].max())
    lower = float(low.iloc[-(period + 1):-1].min())
    return {
        "upper": _r(upper),
        "lower": _r(lower),
        "mid": _r((upper + lower) / 2),
        "breakout_up": float(high.iloc[-1]) > upper,
        "breakout_down": float(low.iloc[-1]) < lower,
    }


def _atr_series(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series | None:
    if len(close) < period + 1:
        return None
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def supertrend(high: pd.Series, low: pd.Series, close: pd.Series,
               period: int = 10, mult: float = 3.0) -> dict | None:
    """Supertrend (Olivier Seban's formulation): ATR bands around the median
    price with ratcheting, flipping direction when close crosses the active
    band. Returns the current direction and the active stop level."""
    if len(close) < period + 2:
        return None
    atr = _atr_series(high, low, close, period)
    if atr is None:
        return None
    hl2 = (high + low) / 2
    upper_basic = hl2 + mult * atr
    lower_basic = hl2 - mult * atr

    n = len(close)
    upper = upper_basic.copy()
    lower = lower_basic.copy()
    trend = np.ones(n, dtype=int)  # 1 = up, -1 = down

    for i in range(1, n):
        # Band ratcheting: bands only tighten while the trend holds.
        if close.iloc[i - 1] <= upper.iloc[i - 1]:
            upper.iloc[i] = min(upper_basic.iloc[i], upper.iloc[i - 1])
        if close.iloc[i - 1] >= lower.iloc[i - 1]:
            lower.iloc[i] = max(lower_basic.iloc[i], lower.iloc[i - 1])

        if close.iloc[i] > upper.iloc[i]:
            trend[i] = 1
        elif close.iloc[i] < lower.iloc[i]:
            trend[i] = -1
        else:
            trend[i] = trend[i - 1]

    direction = "up" if trend[-1] == 1 else "down"
    level = float(lower.iloc[-1]) if trend[-1] == 1 else float(upper.iloc[-1])
    flipped = bool(trend[-1] != trend[-2])
    return {"direction": direction, "level": _r(level), "flipped_this_bar": flipped}


def pivot_points(prev_high: float, prev_low: float, prev_close: float) -> dict | None:
    """Classic floor-trader pivots from the PRIOR completed bar."""
    try:
        p = (prev_high + prev_low + prev_close) / 3
    except TypeError:
        return None
    return {
        "pivot": _r(p),
        "r1": _r(2 * p - prev_low),
        "s1": _r(2 * p - prev_high),
        "r2": _r(p + (prev_high - prev_low)),
        "s2": _r(p - (prev_high - prev_low)),
    }


def find_swings(high: pd.Series, low: pd.Series, window: int = 3) -> dict:
    """Confirmed swing highs/lows: a bar whose high (low) is the strict
    maximum (minimum) of the `window` bars on each side. The trailing
    `window` bars can't contain a confirmed swing — that lag is what makes
    the swings non-repainting."""
    highs, lows = [], []
    n = len(high)
    for i in range(window, n - window):
        seg_h = high.iloc[i - window: i + window + 1]
        if float(high.iloc[i]) == float(seg_h.max()) and (seg_h == seg_h.max()).sum() == 1:
            highs.append({"index": i, "price": float(high.iloc[i])})
        seg_l = low.iloc[i - window: i + window + 1]
        if float(low.iloc[i]) == float(seg_l.min()) and (seg_l == seg_l.min()).sum() == 1:
            lows.append({"index": i, "price": float(low.iloc[i])})
    return {"swing_highs": highs, "swing_lows": lows}


def market_structure(df: pd.DataFrame, window: int = 3) -> dict | None:
    """Swing-based structure: HH/HL/LH/LL labels from the last two confirmed
    swings of each kind, plus BOS/CHoCH off the latest close.

    Returns None (abstains) with fewer than two swings of either kind —
    labeling structure off a single swing would be noise presented as
    analysis."""
    if df is None or len(df) < window * 2 + 4:
        return None
    high, low, close = df["high"], df["low"], df["close"]
    swings = find_swings(high, low, window)
    sh, sl = swings["swing_highs"], swings["swing_lows"]
    if len(sh) < 2 or len(sl) < 2:
        return None

    hh = sh[-1]["price"] > sh[-2]["price"]
    hl = sl[-1]["price"] > sl[-2]["price"]
    labels = [
        "HH" if hh else "LH",
        "HL" if hl else "LL",
    ]
    if hh and hl:
        structure = "uptrend"
    elif not hh and not hl:
        structure = "downtrend"
    else:
        structure = "range"

    last = float(close.iloc[-1])
    last_swing_high = sh[-1]["price"]
    last_swing_low = sl[-1]["price"]

    event = None
    if structure == "uptrend":
        if last > last_swing_high:
            event = "BOS_UP"          # continuation break
        elif last < last_swing_low:
            event = "CHOCH_DOWN"      # break against prevailing structure
    elif structure == "downtrend":
        if last < last_swing_low:
            event = "BOS_DOWN"
        elif last > last_swing_high:
            event = "CHOCH_UP"
    else:
        if last > last_swing_high:
            event = "RANGE_BREAK_UP"
        elif last < last_swing_low:
            event = "RANGE_BREAK_DOWN"

    return {
        "structure": structure,
        "labels": labels,
        "last_swing_high": _r(last_swing_high),
        "last_swing_low": _r(last_swing_low),
        "event": event,
        "swing_count": {"highs": len(sh), "lows": len(sl)},
        "confirmation_window": window,
    }


def compute_extensions(df: pd.DataFrame) -> dict:
    """All extension indicators for one already-cleaned OHLCV frame. Each is
    independently None on failure/insufficient data — one indicator's gap
    never blanks the others."""
    high, low, close = df["high"], df["low"], df["close"]
    vol = df["volume"] if "volume" in df.columns else None
    out = {}

    def _safe(name, fn):
        try:
            out[name] = fn()
        except Exception:
            out[name] = None

    _safe("williams_r", lambda: williams_r(high, low, close))
    _safe("cci", lambda: cci(high, low, close))
    _safe("mfi", lambda: mfi(high, low, close, vol) if vol is not None else None)
    _safe("keltner", lambda: keltner_channels(high, low, close))
    _safe("donchian", lambda: donchian_channels(high, low))
    _safe("supertrend", lambda: supertrend(high, low, close))
    _safe("pivots", lambda: pivot_points(
        float(high.iloc[-2]), float(low.iloc[-2]), float(close.iloc[-2])
    ) if len(close) >= 2 else None)
    _safe("market_structure", lambda: market_structure(df))
    return out
