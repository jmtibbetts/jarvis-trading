"""
Technical Analysis Engine — uses the `ta` library (pure Python, no numba).
Compatible with Python 3.14+. Covers all the same indicators as before.
"""
import pandas as pd
import numpy as np
import logging
import ta
import ta.trend, ta.momentum, ta.volatility, ta.volume
from typing import Optional

logger = logging.getLogger(__name__)


def _safe(fn, *args, **kwargs):
    """Call a ta function, return None on any error."""
    try:
        return fn(*args, **kwargs)
    except Exception:
        return None


def compute_timeframe(df: pd.DataFrame, tf_label: str) -> dict:
    if df is None or len(df) < 10:
        return {"error": "insufficient data", "tf": tf_label}

    df = df.copy()
    df.columns = [c.lower() for c in df.columns]
    required = {'open', 'high', 'low', 'close', 'volume'}
    if not required.issubset(df.columns):
        return {"error": f"missing columns: {required - set(df.columns)}", "tf": tf_label}

    for col in ['open', 'high', 'low', 'close', 'volume']:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    df.dropna(subset=['close'], inplace=True)
    if len(df) < 10:
        return {"error": "insufficient clean data", "tf": tf_label}

    close  = df['close']
    high   = df['high']
    low    = df['low']
    vol    = df['volume']
    last   = float(close.iloc[-1])
    prev   = float(close.iloc[-2]) if len(close) > 1 else last
    result = {"tf": tf_label, "bars": len(df)}

    # ── Price ──────────────────────────────────────────────────────────────────
    result["price"] = {
        "last":       round(last, 6),
        "open":       round(float(df['open'].iloc[-1]), 6),
        "high":       round(float(high.iloc[-1]), 6),
        "low":        round(float(low.iloc[-1]), 6),
        "pct_change": round((last - prev) / prev * 100, 3) if prev else 0
    }

    # ── EMAs ───────────────────────────────────────────────────────────────────
    emas = {}
    for period in [9, 21, 50, 200]:
        if len(df) >= period:
            s = _safe(ta.trend.EMAIndicator, close=close, window=period)
            emas[f"ema{period}"] = round(float(s.ema_indicator().iloc[-1]), 6) if s else None
        else:
            emas[f"ema{period}"] = None
    result["emas"] = emas

    # ── RSI ────────────────────────────────────────────────────────────────────
    try:
        rsi_ind = ta.momentum.RSIIndicator(close=close, window=14)
        rsi_val = float(rsi_ind.rsi().iloc[-1])
        result["rsi"] = round(rsi_val, 2)
        result["rsi_signal"] = "oversold" if rsi_val < 30 else "overbought" if rsi_val > 70 else "neutral"
    except Exception:
        result["rsi"] = None
        result["rsi_signal"] = "unknown"

    # ── MACD ───────────────────────────────────────────────────────────────────
    try:
        macd_ind  = ta.trend.MACD(close=close, window_slow=26, window_fast=12, window_sign=9)
        macd_val  = float(macd_ind.macd().iloc[-1])
        signal_val= float(macd_ind.macd_signal().iloc[-1])
        hist_val  = float(macd_ind.macd_diff().iloc[-1])
        prev_hist = float(macd_ind.macd_diff().iloc[-2]) if len(df) > 2 else hist_val

        # Detect crossover
        crossover = "none"
        prev_macd   = float(macd_ind.macd().iloc[-2])   if len(df) > 2 else macd_val
        prev_signal = float(macd_ind.macd_signal().iloc[-2]) if len(df) > 2 else signal_val
        if prev_macd <= prev_signal and macd_val > signal_val:
            crossover = "bullish"
        elif prev_macd >= prev_signal and macd_val < signal_val:
            crossover = "bearish"

        result["macd"] = {
            "macd":      round(macd_val, 6),
            "signal":    round(signal_val, 6),
            "histogram": round(hist_val, 6),
            "trend":     "bullish" if hist_val > 0 else "bearish",
            "crossover": crossover
        }
    except Exception:
        result["macd"] = None

    # ── Trend bias ─────────────────────────────────────────────────────────────
    try:
        ema21  = emas.get("ema21")
        ema50  = emas.get("ema50")
        ema200 = emas.get("ema200")
        bullish_count = sum([
            last > ema21  if ema21  else False,
            last > ema50  if ema50  else False,
            last > ema200 if ema200 else False,
            (result.get("rsi") or 50) > 50,
            (result.get("macd") or {}).get("trend") == "bullish",
        ])
        pct = round(bullish_count / 5 * 100)
        result["trend"] = {"pct": pct}
        result["bias"]  = "bullish" if pct >= 60 else "bearish" if pct <= 40 else "neutral"
    except Exception:
        result["trend"] = {"pct": 50}
        result["bias"]  = "neutral"

    # ── ADX (trend strength) ────────────────────────────────────────────────────
    try:
        adx_ind = ta.trend.ADXIndicator(high=high, low=low, close=close, window=14)
        adx_val = float(adx_ind.adx().iloc[-1])
        result["adx"] = {
            "value":  round(adx_val, 2),
            "strong": adx_val > 25
        }
    except Exception:
        result["adx"] = None

    # ── Bollinger Bands ────────────────────────────────────────────────────────
    try:
        bb_ind = ta.volatility.BollingerBands(close=close, window=20, window_dev=2)
        upper  = float(bb_ind.bollinger_hband().iloc[-1])
        mid    = float(bb_ind.bollinger_mavg().iloc[-1])
        lower  = float(bb_ind.bollinger_lband().iloc[-1])
        bw     = float(bb_ind.bollinger_wband().iloc[-1])
        pct_b  = float(bb_ind.bollinger_pband().iloc[-1])
        bw_mean = float(bb_ind.bollinger_wband().rolling(20).mean().iloc[-1])
        result["bollinger_bands"] = {
            "upper": round(upper, 6), "mid": round(mid, 6), "lower": round(lower, 6),
            "pct_b": round(pct_b, 4), "bandwidth": round(bw, 4),
            "squeeze": bw < bw_mean * 0.8,
            "position": "above_upper" if last > upper else "below_lower" if last < lower else "inside"
        }
    except Exception:
        result["bollinger_bands"] = None

    # ── ATR ────────────────────────────────────────────────────────────────────
    try:
        atr_ind = ta.volatility.AverageTrueRange(high=high, low=low, close=close, window=14)
        atr_val = float(atr_ind.average_true_range().iloc[-1])
        result["atr"] = {
            "value": round(atr_val, 6),
            "pct":   round(atr_val / last * 100, 3) if last else 0
        }
    except Exception:
        result["atr"] = None

    # ── VWAP (manual — ta library doesn't have intraday VWAP) ─────────────────
    try:
        typical = (high + low + close) / 3
        cum_tp_vol = (typical * vol).cumsum()
        cum_vol    = vol.cumsum()
        vwap_series = cum_tp_vol / cum_vol
        vwap_val = float(vwap_series.iloc[-1])
        pct_diff = (last - vwap_val) / vwap_val * 100 if vwap_val else 0
        result["vwap"] = {
            "value":    round(vwap_val, 6),
            "pct_diff": round(pct_diff, 3),
            "position": "above" if last > vwap_val else "below"
        }
    except Exception:
        result["vwap"] = None

    # ── Volume ─────────────────────────────────────────────────────────────────
    try:
        avg_vol    = float(vol.rolling(20).mean().iloc[-1])
        cur_vol    = float(vol.iloc[-1])
        surge_ratio= cur_vol / avg_vol if avg_vol else 1
        result["volume"] = {
            "current":    int(cur_vol),
            "avg_20":     int(avg_vol),
            "surge_ratio": round(surge_ratio, 2),
            "surge": surge_ratio > 1.5,
            "dry":   surge_ratio < 0.5
        }
    except Exception:
        result["volume"] = None

    # ── Support / Resistance ───────────────────────────────────────────────────
    try:
        recent     = df.tail(min(50, len(df)))
        resistance = round(float(recent['high'].tail(20).max()), 6)
        support    = round(float(recent['low'].tail(20).min()), 6)
        result["support_resistance"] = {
            "support":    support,
            "resistance": resistance,
            "range_pct":  round((resistance - support) / last * 100, 2) if last else 0,
            "position_in_range": round((last - support) / (resistance - support), 3)
                                  if (resistance - support) > 0 else 0.5
        }
    except Exception:
        result["support_resistance"] = None

    # ── Stochastic ────────────────────────────────────────────────────────────
    try:
        stoch_ind = ta.momentum.StochasticOscillator(high=high, low=low, close=close, window=14, smooth_window=3)
        k = round(float(stoch_ind.stoch().iloc[-1]), 2)
        d = round(float(stoch_ind.stoch_signal().iloc[-1]), 2)
        result["stochastic"] = {
            "k": k, "d": d,
            "signal": "oversold" if k < 20 else "overbought" if k > 80 else "neutral"
        }
    except Exception:
        result["stochastic"] = None

    # ── OBV ───────────────────────────────────────────────────────────────────
    try:
        obv_ind = ta.volume.OnBalanceVolumeIndicator(close=close, volume=vol)
        obv_s   = obv_ind.on_balance_volume()
        obv_trend = "rising" if float(obv_s.iloc[-1]) > float(obv_s.iloc[-5]) else "falling"
        result["obv_trend"] = obv_trend
    except Exception:
        result["obv_trend"] = None

    return result


def analyze_symbol(bars_by_tf: dict) -> dict:
    result = {}
    for tf, df in bars_by_tf.items():
        result[tf] = compute_timeframe(df, tf)
    return result


def build_ta_prompt_block(symbol: str, ta_data: dict, asset_name: str = "") -> str:
    lines = [f"\n{'═'*60}", f"  {symbol}  {asset_name}", f"{'═'*60}"]

    def fmt(v, dec=2):
        if v is None: return "N/A"
        return f"{float(v):.{dec}f}"

    def pfmt(p):
        if p is None: return "N/A"
        p = float(p)
        return f"${p:,.0f}" if p > 1000 else f"${p:.4f}" if p < 1 else f"${p:.2f}"

    for tf in ['1H', '2H', '4H', '1D']:
        d = ta_data.get(tf)
        if not d or d.get('error'):
            lines.append(f"  [{tf}] no data")
            continue

        p    = d.get('price', {})
        e    = d.get('emas', {})
        bb   = d.get('bollinger_bands', {}) or {}
        macd = d.get('macd') or {}
        sr   = d.get('support_resistance', {}) or {}
        vol  = d.get('volume', {}) or {}
        vwap = d.get('vwap', {}) or {}
        atr  = d.get('atr', {}) or {}
        stoch= d.get('stochastic', {}) or {}
        adx  = d.get('adx', {}) or {}
        rsi  = d.get('rsi')
        bias = d.get('bias', 'neutral').upper()
        trend= d.get('trend', {})
        obv  = d.get('obv_trend', 'N/A')

        last   = p.get('last', 0)
        change = p.get('pct_change', 0)
        sign   = '+' if change >= 0 else ''
        adx_str = f"ADX={fmt(adx.get('value'))}{'⚡' if adx.get('strong') else ''}" if adx else ""
        lines.append(f"\n  [{tf}]  Price={pfmt(last)} ({sign}{fmt(change)}%)  Bias={bias}  {adx_str}  TrendScore={trend.get('pct', 0)}%")

        ema_parts = []
        for period in [9, 21, 50, 200]:
            v = e.get(f"ema{period}")
            if v:
                arrow = "↑" if last > v else "↓"
                ema_parts.append(f"EMA{period}={pfmt(v)}{arrow}")
        if ema_parts:
            lines.append(f"        EMAs: {' | '.join(ema_parts)}")

        rsi_str = f"RSI={fmt(rsi)} ({d.get('rsi_signal', 'N/A')})" if rsi else "RSI=N/A"
        if macd and macd.get('macd') is not None:
            cross = f" [{macd.get('crossover','none').upper()} CROSS]" if macd.get('crossover', 'none') != 'none' else ""
            macd_str = f"MACD={fmt(macd.get('macd'), 4)} hist={fmt(macd.get('histogram'), 4)} [{macd.get('trend', '').upper()}{cross}]"
        else:
            macd_str = "MACD=N/A"
        lines.append(f"        {rsi_str}  |  {macd_str}")

        if bb:
            sq = "⚡SQUEEZE " if bb.get('squeeze') else ""
            lines.append(f"        BB: {pfmt(bb.get('lower'))} — {pfmt(bb.get('mid'))} — {pfmt(bb.get('upper'))}  %B={fmt(bb.get('pct_b'), 3)}  {sq}Pos={bb.get('position', 'N/A')}")

        vwap_str = f"VWAP={pfmt(vwap.get('value'))} ({('+' if (vwap.get('pct_diff') or 0) >= 0 else '')}{fmt(vwap.get('pct_diff'))}% {vwap.get('position', '')})" if vwap else "VWAP=N/A"
        atr_str  = f"ATR={fmt(atr.get('value'), 4)} ({fmt(atr.get('pct'))}%)" if atr else "ATR=N/A"
        vol_str  = f"Vol={vol.get('surge_ratio', '?')}x {'🔥SURGE' if vol.get('surge') else '🔕DRY' if vol.get('dry') else 'normal'}" if vol else "Vol=N/A"
        lines.append(f"        {vwap_str}  |  {atr_str}  |  {vol_str}")

        if sr:
            lines.append(f"        S/R: Support={pfmt(sr.get('support'))}  Resistance={pfmt(sr.get('resistance'))}  Pos={fmt(sr.get('position_in_range'), 2)}")

        stoch_str = f"Stoch K={fmt(stoch.get('k'))} D={fmt(stoch.get('d'))} [{stoch.get('signal', '')}]" if stoch else ""
        obv_str   = f"OBV={obv}" if obv else ""
        if stoch_str or obv_str:
            lines.append(f"        {stoch_str}  {obv_str}")

    return "\n".join(lines)
