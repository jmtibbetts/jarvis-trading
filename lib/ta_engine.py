"""
Technical Analysis Engine — pandas-ta based multi-timeframe analysis.
Replaces the custom JS taEngine with proper, battle-tested indicators.
"""
import pandas as pd
import pandas_ta as ta
import numpy as np
import logging
from typing import Optional

logger = logging.getLogger(__name__)

def compute_timeframe(df: pd.DataFrame, tf_label: str) -> dict:
    """
    Compute full TA suite on a single-timeframe OHLCV DataFrame.
    Returns a rich dict for use in LLM prompts and UI display.
    """
    if df is None or len(df) < 10:
        return {"error": "insufficient data", "tf": tf_label}

    df = df.copy()
    df.columns = [c.lower() for c in df.columns]
    required = {'open','high','low','close','volume'}
    if not required.issubset(df.columns):
        return {"error": f"missing columns: {required - set(df.columns)}", "tf": tf_label}

    # Ensure numeric
    for col in ['open','high','low','close','volume']:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    df.dropna(subset=['close'], inplace=True)
    if len(df) < 10:
        return {"error": "insufficient clean data", "tf": tf_label}

    close = df['close']
    high  = df['high']
    low   = df['low']
    vol   = df['volume']
    last  = float(close.iloc[-1])
    prev  = float(close.iloc[-2]) if len(close) > 1 else last

    result = {"tf": tf_label, "bars": len(df), "price": {}}

    # ── Price ─────────────────────────────────────────────────────────────────
    result["price"] = {
        "last": round(last, 6),
        "open": round(float(df['open'].iloc[-1]), 6),
        "high": round(float(high.iloc[-1]), 6),
        "low":  round(float(low.iloc[-1]), 6),
        "pct_change": round((last - prev) / prev * 100, 3) if prev else 0
    }

    # ── EMAs ──────────────────────────────────────────────────────────────────
    emas = {}
    for period in [9, 21, 50, 200]:
        if len(df) >= period:
            val = ta.ema(close, length=period)
            emas[f"ema{period}"] = round(float(val.iloc[-1]), 6) if val is not None and not val.empty else None
        else:
            emas[f"ema{period}"] = None
    result["emas"] = emas

    # EMA trend — count how many EMAs price is above
    ema_vals = [v for v in emas.values() if v is not None]
    above = sum(1 for e in ema_vals if last > e)
    result["trend"] = {
        "above_count": above,
        "total": len(ema_vals),
        "pct": round(above / len(ema_vals) * 100) if ema_vals else 0
    }

    # Simple bias
    e21 = emas.get("ema21")
    e50 = emas.get("ema50")
    if e21 and e50:
        result["bias"] = "bullish" if last > e21 > e50 else "bearish" if last < e21 < e50 else "neutral"
    else:
        result["bias"] = "neutral"

    # ── RSI ───────────────────────────────────────────────────────────────────
    rsi_series = ta.rsi(close, length=14)
    rsi_val = round(float(rsi_series.iloc[-1]), 2) if rsi_series is not None and not rsi_series.empty else None
    result["rsi"] = rsi_val
    if rsi_val is not None:
        result["rsi_signal"] = "oversold" if rsi_val < 35 else "overbought" if rsi_val > 70 else "neutral"
    else:
        result["rsi_signal"] = "N/A"

    # ── MACD ──────────────────────────────────────────────────────────────────
    try:
        macd_df = ta.macd(close, fast=12, slow=26, signal=9)
        if macd_df is not None and not macd_df.empty:
            mcol = [c for c in macd_df.columns if 'MACD_' in c and 'MACDs' not in c and 'MACDh' not in c]
            scol = [c for c in macd_df.columns if 'MACDs' in c]
            hcol = [c for c in macd_df.columns if 'MACDh' in c]
            macd_val  = round(float(macd_df[mcol[0]].iloc[-1]), 6) if mcol else None
            sig_val   = round(float(macd_df[scol[0]].iloc[-1]), 6) if scol else None
            hist_val  = round(float(macd_df[hcol[0]].iloc[-1]), 6) if hcol else None
            hist_prev = round(float(macd_df[hcol[0]].iloc[-2]), 6) if hcol and len(macd_df) > 1 else None
            crossover = "none"
            if hist_prev is not None and hist_val is not None:
                if hist_prev < 0 and hist_val >= 0:
                    crossover = "bullish"
                elif hist_prev > 0 and hist_val <= 0:
                    crossover = "bearish"
            result["macd"] = {
                "macd": macd_val, "signal": sig_val, "histogram": hist_val,
                "trend": "bullish" if (hist_val or 0) > 0 else "bearish",
                "crossover": crossover
            }
        else:
            result["macd"] = None
    except Exception as e:
        result["macd"] = None

    # ── Bollinger Bands ───────────────────────────────────────────────────────
    try:
        bb = ta.bbands(close, length=20, std=2)
        if bb is not None and not bb.empty:
            ucol = [c for c in bb.columns if 'BBU' in c][0]
            mcol = [c for c in bb.columns if 'BBM' in c][0]
            lcol = [c for c in bb.columns if 'BBL' in c][0]
            bwcol= [c for c in bb.columns if 'BBB' in c][0] if any('BBB' in c for c in bb.columns) else None
            upper = float(bb[ucol].iloc[-1])
            mid   = float(bb[mcol].iloc[-1])
            lower = float(bb[lcol].iloc[-1])
            bw    = float(bb[bwcol].iloc[-1]) if bwcol else (upper - lower) / mid
            pct_b = (last - lower) / (upper - lower) if (upper - lower) > 0 else 0.5
            # Squeeze: bandwidth < 20-period avg bandwidth * 0.8
            bw_mean = float(bb[bwcol].rolling(20).mean().iloc[-1]) if bwcol else bw
            result["bollinger_bands"] = {
                "upper": round(upper, 6), "mid": round(mid, 6), "lower": round(lower, 6),
                "pct_b": round(pct_b, 4),
                "bandwidth": round(bw, 4),
                "squeeze": bw < bw_mean * 0.8,
                "position": "above_upper" if last > upper else "below_lower" if last < lower else "inside"
            }
        else:
            result["bollinger_bands"] = None
    except:
        result["bollinger_bands"] = None

    # ── ATR ───────────────────────────────────────────────────────────────────
    try:
        atr_s = ta.atr(high, low, close, length=14)
        if atr_s is not None and not atr_s.empty:
            atr_val = float(atr_s.iloc[-1])
            result["atr"] = {
                "value": round(atr_val, 6),
                "pct": round(atr_val / last * 100, 3) if last else 0
            }
        else:
            result["atr"] = None
    except:
        result["atr"] = None

    # ── VWAP ──────────────────────────────────────────────────────────────────
    try:
        vwap_s = ta.vwap(high, low, close, vol)
        if vwap_s is not None and not vwap_s.empty:
            vwap_val = float(vwap_s.iloc[-1])
            pct_diff = (last - vwap_val) / vwap_val * 100 if vwap_val else 0
            result["vwap"] = {
                "value": round(vwap_val, 6),
                "pct_diff": round(pct_diff, 3),
                "position": "above" if last > vwap_val else "below"
            }
        else:
            result["vwap"] = None
    except:
        result["vwap"] = None

    # ── Volume Analysis ───────────────────────────────────────────────────────
    try:
        avg_vol = float(vol.rolling(20).mean().iloc[-1])
        cur_vol = float(vol.iloc[-1])
        surge_ratio = cur_vol / avg_vol if avg_vol else 1
        result["volume"] = {
            "current": int(cur_vol),
            "avg_20": int(avg_vol),
            "surge_ratio": round(surge_ratio, 2),
            "surge": surge_ratio > 1.5,
            "dry": surge_ratio < 0.5
        }
    except:
        result["volume"] = None

    # ── Support / Resistance (pivot-based) ────────────────────────────────────
    try:
        lookback = min(50, len(df))
        recent = df.tail(lookback)
        pivot_high = float(recent['high'].max())
        pivot_low  = float(recent['low'].min())
        # Nearest support/resistance from rolling window
        highs = recent['high'].values
        lows  = recent['low'].values
        # Simple: resistance = max of last 20 bars, support = min of last 20 bars
        resistance = round(float(recent['high'].tail(20).max()), 6)
        support    = round(float(recent['low'].tail(20).min()), 6)
        result["support_resistance"] = {
            "support": support, "resistance": resistance,
            "range_pct": round((resistance - support) / last * 100, 2) if last else 0,
            "position_in_range": round((last - support) / (resistance - support), 3) if (resistance - support) > 0 else 0.5
        }
    except:
        result["support_resistance"] = None

    # ── Stochastic ────────────────────────────────────────────────────────────
    try:
        stoch = ta.stoch(high, low, close)
        if stoch is not None and not stoch.empty:
            kcol = [c for c in stoch.columns if 'STOCHk' in c][0]
            dcol = [c for c in stoch.columns if 'STOCHd' in c][0]
            k = round(float(stoch[kcol].iloc[-1]), 2)
            d = round(float(stoch[dcol].iloc[-1]), 2)
            result["stochastic"] = {
                "k": k, "d": d,
                "signal": "oversold" if k < 20 else "overbought" if k > 80 else "neutral"
            }
        else:
            result["stochastic"] = None
    except:
        result["stochastic"] = None

    # ── OBV (On-Balance Volume) ───────────────────────────────────────────────
    try:
        obv_s = ta.obv(close, vol)
        if obv_s is not None and len(obv_s) > 5:
            obv_trend = "rising" if float(obv_s.iloc[-1]) > float(obv_s.iloc[-5]) else "falling"
            result["obv_trend"] = obv_trend
        else:
            result["obv_trend"] = None
    except:
        result["obv_trend"] = None

    return result


def analyze_symbol(bars_by_tf: dict) -> dict:
    """
    bars_by_tf: { '1H': DataFrame, '4H': DataFrame, '1D': DataFrame, ... }
    Returns full multi-timeframe analysis dict.
    """
    result = {}
    for tf, df in bars_by_tf.items():
        result[tf] = compute_timeframe(df, tf)
    return result


def build_ta_prompt_block(symbol: str, ta_data: dict, asset_name: str = "") -> str:
    """
    Build the rich TA summary string for LLM prompts.
    """
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

        p   = d.get('price', {})
        e   = d.get('emas', {})
        bb  = d.get('bollinger_bands', {}) or {}
        macd= d.get('macd') or {}
        sr  = d.get('support_resistance', {}) or {}
        vol = d.get('volume', {}) or {}
        vwap= d.get('vwap', {}) or {}
        atr = d.get('atr', {}) or {}
        stoch=d.get('stochastic', {}) or {}
        rsi = d.get('rsi')
        bias= d.get('bias','neutral').upper()
        trend= d.get('trend', {})
        obv = d.get('obv_trend', 'N/A')

        last = p.get('last', 0)
        change = p.get('pct_change', 0)
        sign = '+' if change >= 0 else ''
        lines.append(f"\n  [{tf}]  Price={pfmt(last)} ({sign}{fmt(change)}%)  Bias={bias}  TrendScore={trend.get('pct',0)}%")

        # EMAs
        ema_parts = []
        for period in [9, 21, 50, 200]:
            v = e.get(f"ema{period}")
            if v:
                arrow = "↑" if last > v else "↓"
                ema_parts.append(f"EMA{period}={pfmt(v)}{arrow}")
        if ema_parts:
            lines.append(f"        EMAs: {' | '.join(ema_parts)}")

        # RSI + MACD
        rsi_str = f"RSI={fmt(rsi)} ({d.get('rsi_signal','N/A')})" if rsi else "RSI=N/A"
        if macd and macd.get('macd') is not None:
            cross = f" [{macd.get('crossover','none').upper()} CROSS]" if macd.get('crossover','none') != 'none' else ""
            macd_str = f"MACD={fmt(macd.get('macd'),4)} hist={fmt(macd.get('histogram'),4)} [{macd.get('trend','').upper()}{cross}]"
        else:
            macd_str = "MACD=N/A"
        lines.append(f"        {rsi_str}  |  {macd_str}")

        # Bollinger Bands
        if bb:
            sq = "⚡SQUEEZE " if bb.get('squeeze') else ""
            lines.append(f"        BB: {pfmt(bb.get('lower'))} — {pfmt(bb.get('mid'))} — {pfmt(bb.get('upper'))}  %B={fmt(bb.get('pct_b'),3)}  {sq}Pos={bb.get('position','N/A')}")

        # VWAP + ATR + Volume
        vwap_str = f"VWAP={pfmt(vwap.get('value'))} ({'+' if (vwap.get('pct_diff') or 0) >= 0 else ''}{fmt(vwap.get('pct_diff'))}% {vwap.get('position','')})" if vwap else "VWAP=N/A"
        atr_str  = f"ATR={fmt(atr.get('value'),4)} ({fmt(atr.get('pct'))}%)" if atr else "ATR=N/A"
        vol_str  = f"Vol={vol.get('surge_ratio','?')}x {'🔥SURGE' if vol.get('surge') else '🔕DRY' if vol.get('dry') else 'normal'}" if vol else "Vol=N/A"
        lines.append(f"        {vwap_str}  |  {atr_str}  |  {vol_str}")

        # S/R + Stoch + OBV
        if sr:
            lines.append(f"        S/R: Support={pfmt(sr.get('support'))}  Resistance={pfmt(sr.get('resistance'))}  Pos={fmt(sr.get('position_in_range'),2)}")
        stoch_str = f"Stoch K={fmt(stoch.get('k'))} D={fmt(stoch.get('d'))} [{stoch.get('signal','')}]" if stoch else ""
        obv_str = f"OBV={obv}" if obv else ""
        if stoch_str or obv_str:
            lines.append(f"        {stoch_str}  {obv_str}")

    return "\n".join(lines)
