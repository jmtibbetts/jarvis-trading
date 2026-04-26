"""
Technical Analysis Engine — uses `ta` library (pure Python, no numba, works on 3.13+).
All same indicators as pandas-ta version: EMA, RSI, MACD, BB, ATR, VWAP, Stoch, OBV, ADX.
"""
import pandas as pd
import numpy as np
import logging
import ta.trend as tat
import ta.momentum as tam
import ta.volatility as tav
import ta.volume as tavol

logger = logging.getLogger(__name__)


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
    result = {"tf": tf_label, "bars": len(df)}

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
    for period in [9, 21, 50, 200]:
        try:
            if len(df) >= period:
                v = float(tat.EMAIndicator(close=close, window=period).ema_indicator().iloc[-1])
                emas[f"ema{period}"] = round(v, 6)
            else:
                emas[f"ema{period}"] = None
        except Exception:
            emas[f"ema{period}"] = None
    result["emas"] = emas

    # RSI
    try:
        rsi_val = float(tam.RSIIndicator(close=close, window=14).rsi().iloc[-1])
        result["rsi"] = round(rsi_val, 2)
        result["rsi_signal"] = "oversold" if rsi_val < 30 else "overbought" if rsi_val > 70 else "neutral"
    except Exception:
        result["rsi"] = None
        result["rsi_signal"] = "unknown"

    # MACD
    try:
        macd_ind = tat.MACD(close=close, window_slow=26, window_fast=12, window_sign=9)
        mv  = float(macd_ind.macd().iloc[-1])
        sv  = float(macd_ind.macd_signal().iloc[-1])
        hv  = float(macd_ind.macd_diff().iloc[-1])
        pmv = float(macd_ind.macd().iloc[-2]) if len(df) > 2 else mv
        psv = float(macd_ind.macd_signal().iloc[-2]) if len(df) > 2 else sv
        crossover = "bullish" if pmv <= psv and mv > sv else "bearish" if pmv >= psv and mv < sv else "none"
        result["macd"] = {
            "macd": round(mv, 6), "signal": round(sv, 6), "histogram": round(hv, 6),
            "trend": "bullish" if hv > 0 else "bearish", "crossover": crossover
        }
    except Exception:
        result["macd"] = None

    # Trend bias
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
    except Exception:
        result["trend"] = {"pct": 50}
        result["bias"]  = "neutral"

    # ADX
    try:
        adx_val = float(tat.ADXIndicator(high=high, low=low, close=close, window=14).adx().iloc[-1])
        result["adx"] = {"value": round(adx_val, 2), "strong": adx_val > 25}
    except Exception:
        result["adx"] = None

    # Bollinger Bands
    try:
        bb = tav.BollingerBands(close=close, window=20, window_dev=2)
        upper = float(bb.bollinger_hband().iloc[-1])
        mid   = float(bb.bollinger_mavg().iloc[-1])
        lower = float(bb.bollinger_lband().iloc[-1])
        bw    = float(bb.bollinger_wband().iloc[-1])
        pct_b = float(bb.bollinger_pband().iloc[-1])
        bw_mean = float(bb.bollinger_wband().rolling(20).mean().iloc[-1])
        result["bollinger_bands"] = {
            "upper": round(upper, 6), "mid": round(mid, 6), "lower": round(lower, 6),
            "pct_b": round(pct_b, 4), "bandwidth": round(bw, 4),
            "squeeze": bw < bw_mean * 0.8,
            "position": "above_upper" if last > upper else "below_lower" if last < lower else "inside"
        }
    except Exception:
        result["bollinger_bands"] = None

    # ATR
    try:
        atr_val = float(tav.AverageTrueRange(high=high, low=low, close=close, window=14).average_true_range().iloc[-1])
        result["atr"] = {"value": round(atr_val, 6), "pct": round(atr_val / last * 100, 3) if last else 0}
    except Exception:
        result["atr"] = None

    # VWAP (manual — resets each fetch window)
    try:
        typical = (high + low + close) / 3
        vwap_val = float((typical * vol).cumsum().iloc[-1] / vol.cumsum().iloc[-1])
        pct_diff = (last - vwap_val) / vwap_val * 100 if vwap_val else 0
        result["vwap"] = {
            "value": round(vwap_val, 6), "pct_diff": round(pct_diff, 3),
            "position": "above" if last > vwap_val else "below"
        }
    except Exception:
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
    except Exception:
        result["volume"] = None

    # Support / Resistance
    try:
        recent     = df.tail(min(50, len(df)))
        resistance = round(float(recent["high"].tail(20).max()), 6)
        support    = round(float(recent["low"].tail(20).min()), 6)
        result["support_resistance"] = {
            "support": support, "resistance": resistance,
            "range_pct": round((resistance - support) / last * 100, 2) if last else 0,
            "position_in_range": round((last - support) / (resistance - support), 3)
                                  if (resistance - support) > 0 else 0.5
        }
    except Exception:
        result["support_resistance"] = None

    # Stochastic
    try:
        stoch = tam.StochasticOscillator(high=high, low=low, close=close, window=14, smooth_window=3)
        k = round(float(stoch.stoch().iloc[-1]), 2)
        d = round(float(stoch.stoch_signal().iloc[-1]), 2)
        result["stochastic"] = {"k": k, "d": d,
            "signal": "oversold" if k < 20 else "overbought" if k > 80 else "neutral"}
    except Exception:
        result["stochastic"] = None

    # OBV
    try:
        obv_s = tavol.OnBalanceVolumeIndicator(close=close, volume=vol).on_balance_volume()
        result["obv_trend"] = "rising" if float(obv_s.iloc[-1]) > float(obv_s.iloc[-5]) else "falling"
    except Exception:
        result["obv_trend"] = None

    return result


def analyze_symbol(bars_by_tf: dict) -> dict:
    return {tf: compute_timeframe(df, tf) for tf, df in bars_by_tf.items()}


def build_ta_prompt_block(symbol: str, ta_data: dict, asset_name: str = "") -> str:
    lines = [f"\n{'='*60}", f"  {symbol}  {asset_name}", f"{'='*60}"]

    def fmt(v, dec=2):
        return f"{float(v):.{dec}f}" if v is not None else "N/A"

    def pfmt(p):
        if p is None: return "N/A"
        p = float(p)
        return f"${p:,.0f}" if p > 1000 else f"${p:.4f}" if p < 1 else f"${p:.2f}"

    for tf in ["1H", "2H", "4H", "1D"]:
        d = ta_data.get(tf)
        if not d or d.get("error"):
            lines.append(f"  [{tf}] no data")
            continue
        p     = d.get("price", {})
        e     = d.get("emas", {})
        bb    = d.get("bollinger_bands") or {}
        macd  = d.get("macd") or {}
        sr    = d.get("support_resistance") or {}
        vol   = d.get("volume") or {}
        vwap  = d.get("vwap") or {}
        atr   = d.get("atr") or {}
        stoch = d.get("stochastic") or {}
        adx   = d.get("adx") or {}
        rsi   = d.get("rsi")
        bias  = d.get("bias", "neutral").upper()
        trend = d.get("trend", {})
        obv   = d.get("obv_trend", "N/A")
        last  = p.get("last", 0)
        chg   = p.get("pct_change", 0)
        sign  = "+" if chg >= 0 else ""
        adx_s = f"ADX={fmt(adx.get('value'))}{'!' if adx.get('strong') else ''}" if adx else ""
        lines.append(f"\n  [{tf}]  Price={pfmt(last)} ({sign}{fmt(chg)}%)  Bias={bias}  {adx_s}  Score={trend.get('pct',0)}%")
        ep = [f"EMA{p}={pfmt(e.get(f'ema{p}'))}({'>' if last > (e.get(f'ema{p}') or 0) else '<'})" for p in [9,21,50,200] if e.get(f'ema{p}')]
        if ep: lines.append(f"        EMAs: {' | '.join(ep)}")
        rsi_s  = f"RSI={fmt(rsi)} ({d.get('rsi_signal','N/A')})" if rsi else "RSI=N/A"
        macd_s = f"MACD={fmt(macd.get('macd'),4)} hist={fmt(macd.get('histogram'),4)} [{macd.get('trend','').upper()}{' ['+macd.get('crossover','').upper()+' X]' if macd.get('crossover','none')!='none' else ''}]" if macd.get('macd') is not None else "MACD=N/A"
        lines.append(f"        {rsi_s}  |  {macd_s}")
        if bb:
            lines.append(f"        BB: {pfmt(bb.get('lower'))} - {pfmt(bb.get('mid'))} - {pfmt(bb.get('upper'))}  %B={fmt(bb.get('pct_b'),3)}  {'[SQUEEZE] ' if bb.get('squeeze') else ''}Pos={bb.get('position','N/A')}")
        vwap_s = f"VWAP={pfmt(vwap.get('value'))} ({'+' if (vwap.get('pct_diff') or 0)>=0 else ''}{fmt(vwap.get('pct_diff'))}% {vwap.get('position','')})" if vwap else "VWAP=N/A"
        atr_s  = f"ATR={fmt(atr.get('value'),4)} ({fmt(atr.get('pct'))}%)" if atr else "ATR=N/A"
        vol_s  = f"Vol={vol.get('surge_ratio','?')}x {'SURGE' if vol.get('surge') else 'DRY' if vol.get('dry') else 'normal'}" if vol else "Vol=N/A"
        lines.append(f"        {vwap_s}  |  {atr_s}  |  {vol_s}")
        if sr:
            lines.append(f"        S/R: Sup={pfmt(sr.get('support'))}  Res={pfmt(sr.get('resistance'))}  Pos={fmt(sr.get('position_in_range'),2)}")
        if stoch or obv:
            lines.append(f"        {'Stoch K='+fmt(stoch.get('k'))+' D='+fmt(stoch.get('d'))+' ['+stoch.get('signal','')+']' if stoch else ''}  {'OBV='+obv if obv else ''}")
    return "\n".join(lines)
