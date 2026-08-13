"""Levels with a history, breaks with a verdict, and divergence on confirmed swings.

A price level is not just a number. A ceiling touched four times over three
weeks and rejected hard each time is a different object from one printed
once yesterday, and a break of the first means something the break of the
second does not. Nothing in the engine could tell them apart: support and
resistance were two floats with no age, no touch count, and no record of
how price behaved when it got there.

The other half is knowing what a break DID. Most breaks fail. A level taken
out and immediately reclaimed is the opposite signal to one taken out and
held — the first is a liquidity sweep that trapped whoever chased it, the
second is a genuine break — and both look identical to a rule that only
asks "is price above the level".

**Everything here is built on confirmed swings only.** A swing high at bar
i is confirmed only once `window` further bars have printed without
exceeding it; the last `window` bars therefore cannot contain a confirmed
swing. That lag is not a limitation to be optimised away, it is what makes
these readings non-repainting. A sweep detected off an unconfirmed swing is
lookahead: it uses the very bars that prove the sweep happened.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Bars either side that must not exceed a swing for it to count. 3 is the
# same window market_structure already uses, kept identical so the two
# cannot disagree about where the swings are.
SWING_WINDOW = 3

# How close a bar must come to a level, in ATR, to count as touching it.
# In ATR rather than percent because a 0.5% approach is a direct hit on a
# quiet equity and a near miss on a volatile alt.
TOUCH_ATR = 0.35

# A level must be exceeded by this much ATR to count as broken rather than
# merely tested. Without it, a wick one cent through a level reads as a
# breakout on every bar it happens.
BREAK_ATR = 0.25

# A break that reverses back through the level within this many bars is a
# failed break — and, if it took out a swing first, a liquidity sweep.
RECLAIM_BARS = 3

# How far back to look for levels. Level building is O(swings x bars) and
# the daily series now carries 520 bars, which measured at 164ms per symbol
# per timeframe — multiplied across the universe that is minutes of CPU per
# scan. Bounding it is also the more honest model: a ceiling from 450 bars
# ago is not a level current participants are trading against, and scoring
# it alongside one from last week overstates it.
MAX_LEVEL_LOOKBACK = 250


def _f(v, default=0.0):
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _atr_of(df, period: int = 14) -> float:
    """Plain ATR. Local so this module never depends on the caller having
    already computed one — a level engine that silently divides by a
    missing ATR would report every level as a direct hit."""
    try:
        h, l, c = df["high"], df["low"], df["close"]
        pc = c.shift(1)
        tr = (h - l).combine((h - pc).abs(), max).combine((l - pc).abs(), max)
        v = float(tr.tail(period).mean())
        return v if v > 0 else 0.0
    except Exception:
        return 0.0


# ── Levels ───────────────────────────────────────────────────────────────────

def build_levels(df, window: int = SWING_WINDOW, max_levels: int = 6) -> list[dict]:
    """Confirmed swing levels, each with the history that makes it matter.

    - `age_bars`     how long it has stood. An old level is a level many
                     participants can see.
    - `touches`      how often price returned and was turned away. One touch
                     is a coincidence.
    - `rejection`    how hard, in ATR, price was pushed back on its best
                     rejection. A level that produced a 2-ATR reversal is
                     defended; one that produced a 0.1-ATR pause is not.
    - `broken`       whether price has since closed decisively through it.
    """
    if df is None or len(df) < window * 2 + 5:
        return []
    try:
        from lib.ta_extensions import find_swings
        if len(df) > MAX_LEVEL_LOOKBACK:
            df = df.iloc[-MAX_LEVEL_LOOKBACK:]
        high, low, close = df["high"], df["low"], df["close"]
        atr = _atr_of(df)
        if atr <= 0:
            return []
        swings = find_swings(high, low, window)
        n = len(df)
        levels = []
        for kind, points, series in (("resistance", swings["swing_highs"], high),
                                     ("support", swings["swing_lows"], low)):
            for p in points:
                i, price = p["index"], p["price"]
                after = range(i + window + 1, n)
                touches, best_rejection, broken_at = 0, 0.0, None
                # A touch is a VISIT, not a bar. Counting every bar inside
                # the zone made a multi-bar consolidation at a level read as
                # dozens of separate defences — NVDA daily reported one
                # level "held 54x", which is one long pause described as 54
                # rejections. Price has to leave the zone before returning
                # can count again.
                inside = False
                for j in after:
                    hi, lo = float(high.iloc[j]), float(low.iloc[j])
                    cl = float(close.iloc[j])
                    if kind == "resistance":
                        near = abs(hi - price) <= TOUCH_ATR * atr
                        through = cl > price + BREAK_ATR * atr
                        rejection = (hi - cl) / atr if near else 0.0
                        left = hi < price - TOUCH_ATR * atr * 2
                    else:
                        near = abs(lo - price) <= TOUCH_ATR * atr
                        through = cl < price - BREAK_ATR * atr
                        rejection = (cl - lo) / atr if near else 0.0
                        left = lo > price + TOUCH_ATR * atr * 2
                    if near and not through:
                        if not inside:
                            touches += 1
                            inside = True
                        best_rejection = max(best_rejection, rejection)
                    elif left:
                        inside = False
                    if through and broken_at is None:
                        broken_at = j
                levels.append({
                    "kind": kind,
                    "price": round(price, 8),
                    "index": i,
                    "age_bars": n - 1 - i,
                    "touches": touches,
                    "rejection_atr": round(best_rejection, 2),
                    "broken": broken_at is not None,
                    "broken_bars_ago": (n - 1 - broken_at) if broken_at is not None else None,
                })
        # Rank by how much attention a level has earned, not by proximity:
        # touch count first, then how hard it was defended, then age.
        levels.sort(key=lambda x: (-x["touches"], -x["rejection_atr"], -x["age_bars"]))
        return levels[:max_levels]
    except Exception as e:
        logger.debug(f"[Structure] level build failed: {e}")
        return []


def level_strength(level: dict) -> float:
    """0-1. How much weight this level has earned.

    Touches dominate: a level defended repeatedly is the one other
    participants are also watching. Age and rejection contribute, both
    saturating, so nothing is scored as unbreakable.
    """
    if not level:
        return 0.0
    touches = min(1.0, _f(level.get("touches")) / 4.0)
    rejection = min(1.0, _f(level.get("rejection_atr")) / 2.0)
    age = min(1.0, _f(level.get("age_bars")) / 60.0)
    return round(0.55 * touches + 0.30 * rejection + 0.15 * age, 3)


# ── What a break actually did ────────────────────────────────────────────────

def classify_break(df, level: dict, atr: float | None = None) -> dict | None:
    """Whether a break held, failed, or swept.

    The distinction the engine could not previously draw:

      held    price broke and stayed through — a real break
      failed  price broke and came back within RECLAIM_BARS — the break was
              the trap, and the trade is the other way
      sweep   price took the level out only on a WICK and closed back
              inside on the same bar — stops were taken and nothing else
              happened, which is the strongest of the three signals and the
              one most often read as a breakout
    """
    if df is None or not level or len(df) < 3:
        return None
    try:
        atr = atr or _atr_of(df)
        if atr <= 0:
            return None
        price = _f(level.get("price"))
        kind = level.get("kind")
        high, low, close = df["high"], df["low"], df["close"]
        n = len(df)
        start = max(1, n - 1 - RECLAIM_BARS)

        for j in range(start, n):
            hi, lo, cl = float(high.iloc[j]), float(low.iloc[j]), float(close.iloc[j])
            if kind == "resistance":
                wicked = hi > price + BREAK_ATR * atr
                closed_through = cl > price + BREAK_ATR * atr
                direction = "up"
            else:
                wicked = lo < price - BREAK_ATR * atr
                closed_through = cl < price - BREAK_ATR * atr
                direction = "down"
            if not wicked:
                continue

            distance_atr = round(abs((hi if kind == "resistance" else lo) - price) / atr, 2)
            vol_ratio = _break_volume_ratio(df, j)

            if not closed_through:
                return _break(level, "sweep", direction, j, n, distance_atr, vol_ratio,
                              "took the level on a wick and closed back inside — "
                              "stops taken, no follow-through")
            # Closed through: did it hold?
            for k in range(j + 1, n):
                back = (float(close.iloc[k]) < price) if kind == "resistance" \
                    else (float(close.iloc[k]) > price)
                if back:
                    return _break(level, "failed", direction, j, n, distance_atr, vol_ratio,
                                  f"broke and was reclaimed {k - j} bar(s) later")
            return _break(level, "held", direction, j, n, distance_atr, vol_ratio,
                          "broke and held through")
        return None
    except Exception as e:
        logger.debug(f"[Structure] break classification failed: {e}")
        return None


def _break(level, outcome, direction, j, n, distance_atr, vol_ratio, detail):
    return {
        "level_price": level.get("price"),
        "level_kind": level.get("kind"),
        "outcome": outcome,
        "direction": direction,
        "bars_ago": n - 1 - j,
        "distance_atr": distance_atr,
        # Volume on the breaking bar against its recent average. A break on
        # no volume is a break nobody participated in.
        "break_volume_ratio": vol_ratio,
        "conviction": _conviction(outcome, distance_atr, vol_ratio),
        "detail": detail,
    }


def _break_volume_ratio(df, j: int, lookback: int = 20) -> float | None:
    try:
        vol = df["volume"]
        base = float(vol.iloc[max(0, j - lookback): j].mean())
        if base <= 0:
            return None
        return round(float(vol.iloc[j]) / base, 2)
    except Exception:
        return None


def _conviction(outcome: str, distance_atr: float, vol_ratio: float | None) -> float:
    """0-1, how strongly to read the break. A held break on heavy volume
    that travelled is conviction; a sweep's conviction is in the REVERSAL,
    so it is scored high too — the number says "this reading is strong",
    not "this is bullish"."""
    v = min(1.0, (vol_ratio or 1.0) / 2.0)
    d = min(1.0, distance_atr / 1.5)
    base = {"held": 0.6, "failed": 0.75, "sweep": 0.85}.get(outcome, 0.5)
    return round(min(1.0, base * (0.5 + 0.3 * v + 0.2 * d) / 0.75), 3)


# ── Divergence ───────────────────────────────────────────────────────────────

# Which oscillators are worth comparing against price. Each is a genuinely
# different question, unlike the momentum family in lib/evidence.py: RSI is
# rate of change, MACD histogram is acceleration, OBV and MFI are
# participation.
DIVERGENCE_SERIES = ("rsi", "macd_hist", "obv", "mfi")

# A divergence has to actually diverge. Below this the two series are
# effectively flat against each other and any label is noise.
MIN_DIVERGENCE_PCT = 0.5

# Divergence goes stale. One printed thirty bars ago is history, not a
# reason to trade now.
MAX_DIVERGENCE_AGE = 12


def find_divergences(df, indicators: dict, window: int = SWING_WINDOW) -> list[dict]:
    """Regular and hidden divergence, on confirmed swings only.

    regular bearish  price higher high, indicator lower high  → reversal down
    regular bullish  price lower low,   indicator higher low  → reversal up
    hidden bearish   price lower high,  indicator higher high → continuation down
    hidden bullish   price higher low,  indicator lower low   → continuation up

    `indicators` maps a name from DIVERGENCE_SERIES to a series aligned with
    df. Anything missing is skipped rather than approximated.
    """
    out = []
    if df is None or len(df) < window * 2 + 6 or not indicators:
        return out
    try:
        from lib.ta_extensions import find_swings
        swings = find_swings(df["high"], df["low"], window)
        sh, sl = swings["swing_highs"], swings["swing_lows"]
        n = len(df)
        for name, series in indicators.items():
            if name not in DIVERGENCE_SERIES or series is None:
                continue
            try:
                if len(series) != n:
                    continue
                out.extend(_divergence_pair(sh, series, n, name, "high"))
                out.extend(_divergence_pair(sl, series, n, name, "low"))
            except Exception as e:
                logger.debug(f"[Structure] divergence on {name} failed: {e}")
        out = [d for d in out if d["age_bars"] <= MAX_DIVERGENCE_AGE]
        out.sort(key=lambda d: (d["age_bars"], -d["strength"]))
        return out
    except Exception as e:
        logger.debug(f"[Structure] divergence scan failed: {e}")
        return []


def _pct_change(a: float, b: float) -> float:
    if b == 0:
        return 0.0
    return (a - b) / abs(b) * 100.0


def _divergence_pair(points, series, n, name, side) -> list[dict]:
    if len(points) < 2:
        return []
    p2, p1 = points[-1], points[-2]        # p1 older, p2 newer
    i1, i2 = p1["index"], p2["index"]
    try:
        v1, v2 = float(series.iloc[i1]), float(series.iloc[i2])
    except Exception:
        return []
    price_delta = _pct_change(p2["price"], p1["price"])
    ind_delta = _pct_change(v2, v1)
    if abs(price_delta) < MIN_DIVERGENCE_PCT or abs(ind_delta) < MIN_DIVERGENCE_PCT:
        return []
    # Same sign means they agree; there is no divergence to report.
    if (price_delta > 0) == (ind_delta > 0):
        return []

    if side == "high":
        kind = "regular_bearish" if price_delta > 0 else "hidden_bearish"
        bias = "bearish"
    else:
        kind = "regular_bullish" if price_delta < 0 else "hidden_bullish"
        bias = "bullish"

    return [{
        "indicator": name,
        "kind": kind,
        "bias": bias,
        "regular": kind.startswith("regular"),
        "price_change_pct": round(price_delta, 2),
        "indicator_change_pct": round(ind_delta, 2),
        # Strength is the size of the disagreement, saturating: a 40%
        # divergence is not twice as meaningful as a 20% one.
        "strength": round(min(1.0, (abs(price_delta) + abs(ind_delta)) / 20.0), 3),
        "age_bars": n - 1 - i2,
        "from_index": i1,
        "to_index": i2,
    }]


# ── Assembly ─────────────────────────────────────────────────────────────────

def analyze(df, indicators: dict | None = None) -> dict | None:
    """Everything this module knows about one timeframe.

    Returns None rather than a hollow dict when there is not enough data —
    an empty structure block reads as "no levels here", which is a claim,
    where None is the absence of one.
    """
    if df is None or len(df) < SWING_WINDOW * 2 + 6:
        return None
    try:
        atr = _atr_of(df)
        levels = build_levels(df)
        if not levels and not indicators:
            return None
        for lv in levels:
            lv["strength"] = level_strength(lv)
        breaks = []
        for lv in levels[:4]:
            b = classify_break(df, lv, atr)
            if b:
                breaks.append(b)
        divergences = find_divergences(df, indicators or {})
        sweeps = [b for b in breaks if b["outcome"] == "sweep"]
        failed = [b for b in breaks if b["outcome"] == "failed"]
        return {
            "levels": levels,
            "breaks": breaks,
            "divergences": divergences,
            "swept": bool(sweeps),
            "failed_break": bool(failed),
            "summary": _summary(levels, breaks, divergences),
        }
    except Exception as e:
        logger.debug(f"[Structure] analyze failed: {e}")
        return None


def _summary(levels, breaks, divergences) -> str:
    bits = []
    if levels:
        best = levels[0]
        bits.append(f"{best['kind']} {best['price']:g} held {best['touches']}x "
                    f"over {best['age_bars']} bars")
    for b in breaks[:2]:
        bits.append(f"{b['level_kind']} {b['level_price']:g} {b['outcome']} "
                    f"({b['bars_ago']} bars ago)")
    for d in divergences[:2]:
        bits.append(f"{d['kind'].replace('_', ' ')} on {d['indicator']}")
    return "; ".join(bits) or "no notable structure"


def divergence_series(df) -> dict:
    """The four series worth comparing against price, built here.

    The TA engine returns scalars — the latest RSI, the latest MACD
    histogram — because that is all the rest of scoring needs. Divergence
    needs the value AT each swing, which means the whole series. Rather
    than reshape every indicator in ta_engine for one consumer, this
    rebuilds the four from the frame using the same backend.

    Anything that fails to compute is omitted, not defaulted: a divergence
    read against a zero-filled series would be a confident reading of
    nothing.
    """
    out = {}
    if df is None or len(df) < 30:
        return out
    close, high, low = df["close"], df["high"], df["low"]
    vol = df["volume"] if "volume" in df.columns else None

    def _safe(name, fn):
        try:
            s = fn()
            if s is not None and len(s) == len(df):
                out[name] = s.astype(float)
        except Exception as e:
            logger.debug(f"[Structure] {name} series unavailable: {e}")

    try:
        from lib.ta_engine import _BACKEND, _talib
    except Exception:
        _BACKEND, _talib = "ta", None

    if _BACKEND == "talib" and _talib is not None:
        _safe("rsi", lambda: _talib.RSI(close, timeperiod=14))
        _safe("macd_hist", lambda: _talib.MACD(close, fastperiod=12, slowperiod=26,
                                               signalperiod=9)[2])
        if vol is not None:
            _safe("obv", lambda: _talib.OBV(close, vol))
            _safe("mfi", lambda: _talib.MFI(high, low, close, vol, timeperiod=14))
    else:
        import ta.momentum as tam
        import ta.trend as tat
        _safe("rsi", lambda: tam.RSIIndicator(close=close, window=14).rsi())
        _safe("macd_hist", lambda: tat.MACD(close=close, window_slow=26, window_fast=12,
                                            window_sign=9).macd_diff())
        if vol is not None:
            import ta.volume as tav
            _safe("obv", lambda: tav.OnBalanceVolumeIndicator(
                close=close, volume=vol).on_balance_volume())
            _safe("mfi", lambda: tav.MFIIndicator(
                high=high, low=low, close=close, volume=vol, window=14).money_flow_index())
    return out
