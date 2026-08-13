"""
Technical Analysis Engine.
Auto-detects available library: TA-Lib (preferred, C-backed) → ta (pure Python fallback).
Same output format regardless of which library is active.
"""
import pandas as pd
import numpy as np
import logging

logger = logging.getLogger(__name__)


def _r(value, sig: int = 8):
    """Round a PRICE to significant figures, never to fixed decimals.

    _r(x) silently destroys sub-cent assets: SHIB at $0.00000449
    becomes $0.000004, an 10.91% error, and every level derived from it —
    VWAP, support, resistance, bands, Supertrend — inherits that error
    before any strategy sees it. The same class of bug as pricing a
    contract by token count: a fixed unit applied across a five-order-of-
    magnitude price range.

    Significant figures keep the same relative precision whether the
    instrument trades at $0.0000045 or $63,000.
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

TIMEFRAME_LADDER = ["1m", "3m", "5m", "15m", "30m", "1H", "2H", "4H", "1D"]

# Detect which TA library is available
try:
    import talib as _talib
    _BACKEND = "talib"
    logger.info("[TA Engine] Using TA-Lib (C-backed, fast)")
except ImportError:
    _BACKEND = "ta"
    logger.info("[TA Engine] Using ta (pure Python fallback)")


def _ema(close: pd.Series, period: int) -> float:
    if _BACKEND == "talib":
        v = _talib.EMA(close, timeperiod=period)
    else:
        import ta.trend as tat
        v = tat.EMAIndicator(close=close, window=period).ema_indicator()
    return float(v.iloc[-1]) if not v.empty else None


def _rsi(close: pd.Series, period: int = 14) -> float:
    if _BACKEND == "talib":
        v = _talib.RSI(close, timeperiod=period)
    else:
        import ta.momentum as tam
        v = tam.RSIIndicator(close=close, window=period).rsi()
    return float(v.iloc[-1]) if not v.empty else None


def _macd(close: pd.Series):
    if _BACKEND == "talib":
        m, s, h = _talib.MACD(close, fastperiod=12, slowperiod=26, signalperiod=9)
        return float(m.iloc[-1]), float(s.iloc[-1]), float(h.iloc[-1]), float(m.iloc[-2]), float(s.iloc[-2])
    else:
        import ta.trend as tat
        ind = tat.MACD(close=close, window_slow=26, window_fast=12, window_sign=9)
        m, s, h = ind.macd(), ind.macd_signal(), ind.macd_diff()
        return float(m.iloc[-1]), float(s.iloc[-1]), float(h.iloc[-1]), float(m.iloc[-2]), float(s.iloc[-2])


def _adx(high, low, close, period=14) -> float:
    if _BACKEND == "talib":
        v = _talib.ADX(high, low, close, timeperiod=period)
    else:
        import ta.trend as tat
        v = tat.ADXIndicator(high=high, low=low, close=close, window=period).adx()
    return float(v.iloc[-1]) if not v.empty else None


def _bbands(close: pd.Series):
    if _BACKEND == "talib":
        upper, mid, lower = _talib.BBANDS(close, timeperiod=20, nbdevup=2, nbdevdn=2)
        return float(upper.iloc[-1]), float(mid.iloc[-1]), float(lower.iloc[-1])
    else:
        import ta.volatility as tav
        bb = tav.BollingerBands(close=close, window=20, window_dev=2)
        return float(bb.bollinger_hband().iloc[-1]), float(bb.bollinger_mavg().iloc[-1]), float(bb.bollinger_lband().iloc[-1])


def _atr(high, low, close, period=14) -> float:
    if _BACKEND == "talib":
        v = _talib.ATR(high, low, close, timeperiod=period)
    else:
        import ta.volatility as tav
        v = tav.AverageTrueRange(high=high, low=low, close=close, window=period).average_true_range()
    return float(v.iloc[-1]) if not v.empty else None


def _stoch(high, low, close):
    if _BACKEND == "talib":
        k, d = _talib.STOCH(high, low, close, fastk_period=14, slowk_period=3, slowd_period=3)
        return float(k.iloc[-1]), float(d.iloc[-1])
    else:
        import ta.momentum as tam
        ind = tam.StochasticOscillator(high=high, low=low, close=close, window=14, smooth_window=3)
        return float(ind.stoch().iloc[-1]), float(ind.stoch_signal().iloc[-1])


def _obv(close, volume):
    if _BACKEND == "talib":
        v = _talib.OBV(close, volume)
    else:
        import ta.volume as tavol
        v = tavol.OnBalanceVolumeIndicator(close=close, volume=volume).on_balance_volume()
    return float(v.iloc[-1]), float(v.iloc[-5]) if len(v) >= 5 else float(v.iloc[0])


# ── MACD crossover semantics ─────────────────────────────────────────────
# compute_timeframe emits crossover as one of "bullish" / "bearish" / "none".
# "none" is a NON-EMPTY string and therefore TRUTHY, so `if macd["crossover"]`
# reports a crossover on every bar that has none. That exact bug marked a
# crossover for every symbol in every LLM batch prompt (ta_engine) and in
# every learning pattern signature (learning_engine) — the model and the
# pattern memory were both told a signal existed that did not.
# Use these helpers instead of testing the raw value for truthiness.
CROSSOVER_NONE = "none"


def has_crossover(macd: dict | None) -> bool:
    """True only for a REAL crossover ("bullish" or "bearish")."""
    return str((macd or {}).get("crossover") or CROSSOVER_NONE) in ("bullish", "bearish")


def crossover_direction(macd: dict | None) -> str | None:
    """"bullish" / "bearish", or None when there is no crossover."""
    value = str((macd or {}).get("crossover") or CROSSOVER_NONE)
    return value if value in ("bullish", "bearish") else None


def compute_timeframe(df: pd.DataFrame, tf_label: str) -> dict:
    if df is None or len(df) < 10:
        return {"error": "insufficient data", "tf": tf_label}

    df = df.copy()
    df.columns = [c.lower() for c in df.columns]
    for col in ["open", "high", "low", "close", "volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df.dropna(subset=["close"], inplace=True)
    if len(df) < 10:
        return {"error": "insufficient clean data", "tf": tf_label}

    close = df["close"]
    high  = df["high"]
    low   = df["low"]
    vol   = df["volume"]
    last  = float(close.iloc[-1])
    prev  = float(close.iloc[-2]) if len(close) > 1 else last
    result = {"tf": tf_label, "bars": len(df), "backend": _BACKEND}
    try:
        last_bar = pd.Timestamp(df.index[-1])
        last_bar = last_bar.tz_localize("UTC") if last_bar.tzinfo is None else last_bar.tz_convert("UTC")
        now = pd.Timestamp.now(tz="UTC")
        result["bar_time"] = last_bar.isoformat()
        result["bar_age_seconds"] = max(0, round((now - last_bar).total_seconds()))
        result["data_source"] = df.attrs.get("source", "unknown")
    except Exception:
        result["bar_time"] = None
        result["bar_age_seconds"] = None
        result["data_source"] = df.attrs.get("source", "unknown")

    # Price
    result["price"] = {
        "last":       _r(last),
        "open":       _r(float(df["open"].iloc[-1])),
        "high":       _r(float(high.iloc[-1])),
        "low":        _r(float(low.iloc[-1])),
        "pct_change": round((last - prev) / prev * 100, 3) if prev else 0,
    }

    # EMAs
    emas = {}
    for p in [9, 21, 50, 200]:
        try:
            if len(df) >= p:
                emas[f"ema{p}"] = _r(_ema(close, p))
            else:
                emas[f"ema{p}"] = None
        except:
            emas[f"ema{p}"] = None
    result["emas"] = emas

    # RSI
    try:
        rsi_val = _rsi(close)
        result["rsi"] = round(rsi_val, 2) if rsi_val else None
        result["rsi_signal"] = "oversold" if rsi_val < 30 else "overbought" if rsi_val > 70 else "neutral"
    except:
        result["rsi"] = None
        result["rsi_signal"] = "unknown"

    # MACD
    try:
        mv, sv, hv, pmv, psv = _macd(close)
        crossover = "bullish" if pmv <= psv and mv > sv else "bearish" if pmv >= psv and mv < sv else "none"
        result["macd"] = {
            "macd": _r(mv), "signal": _r(sv), "histogram": _r(hv),
            "trend": "bullish" if hv > 0 else "bearish", "crossover": crossover
        }
    except:
        result["macd"] = None

    # Bias
    try:
        bulls = sum([
            last > (emas.get("ema21") or 0) if emas.get("ema21") else False,
            last > (emas.get("ema50") or 0) if emas.get("ema50") else False,
            last > (emas.get("ema200") or 0) if emas.get("ema200") else False,
            (result.get("rsi") or 50) > 50,
            (result.get("macd") or {}).get("trend") == "bullish",
        ])
        pct = round(bulls / 5 * 100)
        result["trend"] = {"pct": pct}
        result["bias"]  = "bullish" if pct >= 60 else "bearish" if pct <= 40 else "neutral"
    except:
        result["trend"] = {"pct": 50}
        result["bias"]  = "neutral"

    # ADX
    try:
        adx_val = _adx(high, low, close)
        result["adx"] = {"value": round(adx_val, 2), "strong": adx_val > 25} if adx_val else None
    except:
        result["adx"] = None

    # Bollinger Bands
    try:
        upper, mid, lower = _bbands(close)
        bb_series = close.rolling(20).std() * 2
        bw = (upper - lower) / mid if mid else 0
        bw_mean = float(pd.Series(
            [(close.iloc[i] * 2 * 2) for i in range(len(close))]
        ).rolling(20).mean().iloc[-1]) if False else bw  # simplified
        result["bollinger_bands"] = {
            "upper": _r(upper), "mid": _r(mid), "lower": _r(lower),
            "pct_b": round((last - lower) / (upper - lower), 4) if (upper - lower) > 0 else 0.5,
            "bandwidth": round(bw, 4),
            "position": "above_upper" if last > upper else "below_lower" if last < lower else "inside"
        }
    except:
        result["bollinger_bands"] = None

    # ATR
    try:
        atr_val = _atr(high, low, close)
        result["atr"] = {"value": _r(atr_val), "pct": round(atr_val / last * 100, 3) if last else 0} if atr_val else None
    except:
        result["atr"] = None

    # ATR profile — multi-horizon, plus where this volatility sits in the
    # instrument's OWN history. The single-value ATR above cannot answer
    # "is this high for this asset", which is what makes any threshold
    # portable across BTC, SHIB and NVDA.
    try:
        from lib.atr_normalization import compute_atr_profile
        result["atr_profile"] = compute_atr_profile(df)
    except Exception as e:
        logger.debug(f"[TA] ATR profile unavailable: {e}")
        result["atr_profile"] = None

    # VWAP
    try:
        typical = (high + low + close) / 3
        vol_sum = vol.cumsum().iloc[-1]
        vwap_val = float((typical * vol).cumsum().iloc[-1] / vol_sum) if vol_sum > 0 else None
        if vwap_val is None:
            result["vwap"] = None
            raise ValueError("zero volume — no VWAP")
        pct_diff = (last - vwap_val) / vwap_val * 100 if vwap_val else 0
        result["vwap"] = {
            "value": _r(vwap_val), "pct_diff": round(pct_diff, 3),
            "position": "above" if last > vwap_val else "below"
        }
    except:
        result["vwap"] = None

    # Volume
    try:
        avg_vol = float(vol.rolling(20).mean().iloc[-1])
        cur_vol = float(vol.iloc[-1])
        ratio   = cur_vol / avg_vol if avg_vol else 1
        result["volume"] = {
            "current": int(cur_vol), "avg_20": int(avg_vol),
            "surge_ratio": round(ratio, 2), "surge": ratio > 1.5, "dry": ratio < 0.5
        }
    except:
        result["volume"] = None

    # Support / Resistance
    try:
        recent     = df.tail(50)
        resistance = _r(float(recent["high"].tail(20).max()))
        support    = _r(float(recent["low"].tail(20).min()))
        result["support_resistance"] = {
            "support": support, "resistance": resistance,
            "range_pct": round((resistance - support) / last * 100, 2) if last else 0,
            "position_in_range": round((last - support) / (resistance - support), 3)
                                  if (resistance - support) > 0 else 0.5
        }
    except:
        result["support_resistance"] = None

    # Stochastic
    try:
        k, d = _stoch(high, low, close)
        result["stochastic"] = {"k": round(k, 2), "d": round(d, 2),
            "signal": "oversold" if k < 20 else "overbought" if k > 80 else "neutral"}
    except:
        result["stochastic"] = None

    # OBV
    try:
        obv_last, obv_prev = _obv(close, vol)
        result["obv_trend"] = "rising" if obv_last > obv_prev else "falling"
    except:
        result["obv_trend"] = None

    # Extension indicators (Williams %R, CCI, MFI, Keltner, Donchian,
    # Supertrend, pivots, market structure) — pure-pandas module, identical
    # under either backend; each key independently None on failure.
    try:
        from lib.ta_extensions import compute_extensions
        result.update(compute_extensions(df))
    except Exception:
        pass

    # Every level found above, restated in ATRs from the current price.
    # LAST, because it needs all of them to exist first. This is what makes
    # a threshold portable: "2% from VWAP" says nothing across assets,
    # "1.4 ATR from VWAP" says the same thing on SHIB and on crude.
    try:
        from lib.atr_normalization import normalized_distances
        prof = result.get("atr_profile") or {}
        ref = prof.get("reference") or ((result.get("atr") or {}).get("value"))
        dist = normalized_distances(result, ref)
        result["atr_distances"] = dist or None
    except Exception as e:
        logger.debug(f"[TA] ATR distances unavailable: {e}")
        result["atr_distances"] = None

    return result


def analyze_symbol(bars_by_tf: dict) -> dict:
    return {tf: compute_timeframe(df, tf) for tf, df in bars_by_tf.items()}


def build_ta_prompt_block(symbol: str, ta_data: dict, asset_name: str = "") -> str:
    """Compact TA block — ~3 lines per symbol instead of ~30. Keeps signal quality, cuts tokens 80%."""
    def fmt(v, dec=2):
        return f"{float(v):.{dec}f}" if v is not None else "?"
    def pfmt(p):
        """Price with SIGNIFICANT figures, not fixed decimals.

        `f"${p:.4f}"` for anything under $1 printed SHIB, its support and
        its resistance all as "$0.0000" — three different levels rendered
        identically, in the text the model actually reads. The same class of
        error as round(x, 6) destroying the levels themselves.
        """
        if p is None: return "?"
        p = float(p)
        if p == 0: return "$0"
        if p >= 1000: return f"${p:,.0f}"
        if p >= 1: return f"${p:.2f}"
        import math
        decimals = min(12, max(4, 4 - int(math.floor(math.log10(abs(p))))))
        return f"${p:.{decimals}f}".rstrip("0")

    lines = [f"[{symbol}]" + (f" {asset_name}" if asset_name else "")]
    for tf in TIMEFRAME_LADDER:
        d = ta_data.get(tf)
        if not d or d.get("error"):
            continue
        p    = d.get("price", {})
        last = float(p.get("last") or 0)
        chg  = float(p.get("pct_change") or 0)
        rsi  = d.get("rsi")
        bias = d.get("bias", "?")[:1].upper()   # B/S/N one char
        macd = d.get("macd") or {}
        mt   = (macd.get("trend") or "?")[:1].upper()   # U/D one char
        mc   = "X" if has_crossover(macd) else ""
        bb   = d.get("bollinger_bands") or {}
        bbp  = (bb.get("position") or "?")[:3]          # low/mid/up
        vol  = d.get("volume") or {}
        vs   = "SRG" if vol.get("surge") else ("DRY" if vol.get("dry") else "nrm")
        vwap = d.get("vwap") or {}
        vp   = (vwap.get("position") or "?")[:3]        # abo/bel
        atr  = d.get("atr") or {}
        atrp = fmt(atr.get("pct"), 1)
        sr   = d.get("support_resistance") or {}
        sup  = pfmt(sr.get("support"))
        res  = pfmt(sr.get("resistance"))
        sign = "+" if chg >= 0 else ""
        lines.append(
            f"  {tf}: {pfmt(last)}({sign}{fmt(chg,1)}%) "
            f"RSI={fmt(rsi,0)} Bias={bias} MACD={mt}{mc} "
            f"BB={bbp} VWAP={vp} ATR={atrp}% Vol={vs} "
            f"S={sup} R={res}"
        )
        # Distances in ATRs, so the model can judge stretch without knowing
        # what a normal move looks like on this instrument. "3.2 ATR from
        # VWAP" is the same statement on SHIB and on crude; "2% from VWAP"
        # is not a statement at all.
        dist = d.get("atr_distances") or {}
        prof = d.get("atr_profile") or {}
        parts = [f"{name}={value:+.1f}" for name, value in (
            ("vwap", dist.get("to_vwap")),
            ("sup", dist.get("to_support")),
            ("res", dist.get("to_resistance")),
        ) if value is not None]
        extra = []
        if prof.get("state"):
            extra.append(prof["state"].lower())
        if prof.get("percentile") is not None:
            extra.append(f"pctile={prof['percentile']:.0f}")
        if parts or extra:
            lines.append(
                f"       ATRs: {' '.join(parts)}"
                + (f"  [{' '.join(extra)}]" if extra else "")
            )
    return "\n".join(lines) + "\n"

