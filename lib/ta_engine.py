"""
Technical Analysis Engine.
Auto-detects available library: TA-Lib (preferred, C-backed) → ta (pure Python fallback).
Same output format regardless of which library is active.
"""
import pandas as pd
import numpy as np
import logging

logger = logging.getLogger(__name__)

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

    # Price
    result["price"] = {
        "last":       round(last, 6),
        "open":       round(float(df["open"].iloc[-1]), 6),
        "high":       round(float(high.iloc[-1]), 6),
        "low":        round(float(low.iloc[-1]), 6),
        "pct_change": round((last - prev) / prev * 100, 3) if prev else 0,
    }

    # EMAs
    emas = {}
    for p in [9, 21, 50, 200]:
        try:
            if len(df) >= p:
                emas[f"ema{p}"] = round(_ema(close, p), 6)
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
            "macd": round(mv, 6), "signal": round(sv, 6), "histogram": round(hv, 6),
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
            "upper": round(upper, 6), "mid": round(mid, 6), "lower": round(lower, 6),
            "pct_b": round((last - lower) / (upper - lower), 4) if (upper - lower) > 0 else 0.5,
            "bandwidth": round(bw, 4),
            "position": "above_upper" if last > upper else "below_lower" if last < lower else "inside"
        }
    except:
        result["bollinger_bands"] = None

    # ATR
    try:
        atr_val = _atr(high, low, close)
        result["atr"] = {"value": round(atr_val, 6), "pct": round(atr_val / last * 100, 3) if last else 0} if atr_val else None
    except:
        result["atr"] = None

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
            "value": round(vwap_val, 6), "pct_diff": round(pct_diff, 3),
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
        resistance = round(float(recent["high"].tail(20).max()), 6)
        support    = round(float(recent["low"].tail(20).min()), 6)
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

    return result


def analyze_symbol(bars_by_tf: dict) -> dict:
    return {tf: compute_timeframe(df, tf) for tf, df in bars_by_tf.items()}


def build_ta_prompt_block(symbol: str, ta_data: dict, asset_name: str = "") -> str:
    """Compact TA block — ~3 lines per symbol instead of ~30. Keeps signal quality, cuts tokens 80%."""
    def fmt(v, dec=2):
        return f"{float(v):.{dec}f}" if v is not None else "?"
    def pfmt(p):
        if p is None: return "?"
        p = float(p)
        return f"${p:,.0f}" if p > 1000 else f"${p:.4f}" if p < 1 else f"${p:.2f}"

    lines = [f"[{symbol}]" + (f" {asset_name}" if asset_name else "")]
    for tf in ["1H", "4H", "1D"]:   # dropped 2H — redundant
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
        mc   = "X" if macd.get("crossover") else ""
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
    return "\n".join(lines) + "\n"

