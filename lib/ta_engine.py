"""
Technical Analysis Engine — pandas-ta multi-timeframe analysis.
Requires Python <=3.13 (numba constraint).
"""
import pandas as pd
import pandas_ta as ta
import numpy as np
import logging

logger = logging.getLogger(__name__)


def compute_timeframe(df: pd.DataFrame, tf_label: str) -> dict:
    if df is None or len(df) < 10:
        return {"error": "insufficient data", "tf": tf_label}

    df = df.copy()
    df.columns = [c.lower() for c in df.columns]
    required = {"open", "high", "low", "close", "volume"}
    if not required.issubset(df.columns):
        return {"error": f"missing columns: {required - set(df.columns)}", "tf": tf_label}

    for col in ["open", "high", "low", "close", "volume"]:
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
        if len(df) >= period:
            s = ta.ema(close, length=period)
            emas[f"ema{period}"] = round(float(s.iloc[-1]), 6) if s is not None and not s.empty else None
        else:
            emas[f"ema{period}"] = None
    result["emas"] = emas

    # RSI
    try:
        rsi_s = ta.rsi(close, length=14)
        rsi_val = float(rsi_s.iloc[-1]) if rsi_s is not None and not rsi_s.empty else None
        result["rsi"] = round(rsi_val, 2) if rsi_val else None
        result["rsi_signal"] = "oversold" if (rsi_val or 50) < 30 else "overbought" if (rsi_val or 50) > 70 else "neutral"
    except Exception:
        result["rsi"] = None
        result["rsi_signal"] = "unknown"

    # MACD
    try:
        macd_df = ta.macd(close)
        if macd_df is not None and not macd_df.empty:
            mcol  = [c for c in macd_df.columns if "MACD_" in c and "MACDs_" not in c and "MACDh_" not in c][0]
            scol  = [c for c in macd_df.columns if "MACDs_" in c][0]
            hcol  = [c for c in macd_df.columns if "MACDh_" in c][0]
            mv, sv, hv = float(macd_df[mcol].iloc[-1]), float(macd_df[scol].iloc[-1]), float(macd_df[hcol].iloc[-1])
            prev_mv = float(macd_df[mcol].iloc[-2]) if len(macd_df) > 2 else mv
            prev_sv = float(macd_df[scol].iloc[-2]) if len(macd_df) > 2 else sv
            crossover = "none"
            if prev_mv <= prev_sv and mv > sv: crossover = "bullish"
            elif prev_mv >= prev_sv and mv < sv: crossover = "bearish"
            result["macd"] = {"macd": round(mv,6), "signal": round(sv,6), "histogram": round(hv,6),
                              "trend": "bullish" if hv > 0 else "bearish", "crossover": crossover}
        else:
            result["macd"] = None
    except Exception:
        result["macd"] = None

    # Trend bias
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

    # ADX
    try:
        adx_df = ta.adx(high, low, close, length=14)
        if adx_df is not None and not adx_df.empty:
            acol = [c for c in adx_df.columns if c.startswith("ADX_")][0]
            adx_val = float(adx_df[acol].iloc[-1])
            result["adx"] = {"value": round(adx_val, 2), "strong": adx_val > 25}
        else:
            result["adx"] = None
    except Exception:
        result["adx"] = None

    # Bollinger Bands
    try:
        bb_df = ta.bbands(close, length=20, std=2)
        if bb_df is not None and not bb_df.empty:
            ucol = [c for c in bb_df.columns if "BBU_" in c][0]
            mcol = [c for c in bb_df.columns if "BBM_" in c][0]
            lcol = [c for c in bb_df.columns if "BBL_" in c][0]
            bwcol= [c for c in bb_df.columns if "BBB_" in c][0]
            pcol = [c for c in bb_df.columns if "BBP_" in c][0]
            upper, mid, lower = float(bb_df[ucol].iloc[-1]), float(bb_df[mcol].iloc[-1]), float(bb_df[lcol].iloc[-1])
            bw    = float(bb_df[bwcol].iloc[-1])
            pct_b = float(bb_df[pcol].iloc[-1])
            bw_mean = float(bb_df[bwcol].rolling(20).mean().iloc[-1])
            result["bollinger_bands"] = {
                "upper": round(upper,6), "mid": round(mid,6), "lower": round(lower,6),
                "pct_b": round(pct_b,4), "bandwidth": round(bw,4),
                "squeeze": bw < bw_mean * 0.8,
                "position": "above_upper" if last > upper else "below_lower" if last < lower else "inside"
            }
        else:
            result["bollinger_bands"] = None
    except Exception:
        result["bollinger_bands"] = None

    # ATR
    try:
        atr_s = ta.atr(high, low, close, length=14)
        if atr_s is not None and not atr_s.empty:
            atr_val = float(atr_s.iloc[-1])
            result["atr"] = {"value": round(atr_val,6), "pct": round(atr_val/last*100,3) if last else 0}
        else:
            result["atr"] = None
    except Exception:
        result["atr"] = None

    # VWAP
    try:
        vwap_s = ta.vwap(high, low, close, vol)
        if vwap_s is not None and not vwap_s.empty:
            vwap_val = float(vwap_s.iloc[-1])
            pct_diff = (last - vwap_val) / vwap_val * 100 if vwap_val else 0
            result["vwap"] = {"value": round(vwap_val,6), "pct_diff": round(pct_diff,3),
                              "position": "above" if last > vwap_val else "below"}
        else:
            result["vwap"] = None
    except Exception:
        result["vwap"] = None

    # Volume
    try:
        avg_vol = float(vol.rolling(20).mean().iloc[-1])
        cur_vol = float(vol.iloc[-1])
        surge_ratio = cur_vol / avg_vol if avg_vol else 1
        result["volume"] = {"current": int(cur_vol), "avg_20": int(avg_vol),
                            "surge_ratio": round(surge_ratio,2),
                            "surge": surge_ratio > 1.5, "dry": surge_ratio < 0.5}
    except Exception:
        result["volume"] = None

    # Support / Resistance
    try:
        recent = df.tail(min(50, len(df)))
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
        stoch_df = ta.stoch(high, low, close)
        if stoch_df is not None and not stoch_df.empty:
            kcol = [c for c in stoch_df.columns if "STOCHk" in c][0]
            dcol = [c for c in stoch_df.columns if "STOCHd" in c][0]
            k = round(float(stoch_df[kcol].iloc[-1]), 2)
            d = round(float(stoch_df[dcol].iloc[-1]), 2)
            result["stochastic"] = {"k": k, "d": d,
                                    "signal": "oversold" if k < 20 else "overbought" if k > 80 else "neutral"}
        else:
            result["stochastic"] = None
    except Exception:
        result["stochastic"] = None

    # OBV
    try:
        obv_s = ta.obv(close, vol)
        if obv_s is not None and len(obv_s) > 5:
            result["obv_trend"] = "rising" if float(obv_s.iloc[-1]) > float(obv_s.iloc[-5]) else "falling"
        else:
            result["obv_trend"] = None
    except Exception:
        result["obv_trend"] = None

    return result


def analyze_symbol(bars_by_tf: dict) -> dict:
    return {tf: compute_timeframe(df, tf) for tf, df in bars_by_tf.items()}


def build_ta_prompt_block(symbol: str, ta_data: dict, asset_name: str = "") -> str:
    lines = [f"\n{chr(9552)*60}", f"  {symbol}  {asset_name}", f"{chr(9552)*60}"]

    def fmt(v, dec=2):
        if v is None: return "N/A"
        return f"{float(v):.{dec}f}"

    def pfmt(p):
        if p is None: return "N/A"
        p = float(p)
        return f"${p:,.0f}" if p > 1000 else f"${p:.4f}" if p < 1 else f"${p:.2f}"

    for tf in ["1H", "2H", "4H", "1D"]:
        d = ta_data.get(tf)
        if not d or d.get("error"):
            lines.append(f"  [{tf}] no data")
            continue

        p    = d.get("price", {})
        e    = d.get("emas", {})
        bb   = d.get("bollinger_bands", {}) or {}
        macd = d.get("macd") or {}
        sr   = d.get("support_resistance", {}) or {}
        vol  = d.get("volume", {}) or {}
        vwap = d.get("vwap", {}) or {}
        atr  = d.get("atr", {}) or {}
        stoch= d.get("stochastic", {}) or {}
        adx  = d.get("adx", {}) or {}
        rsi  = d.get("rsi")
        bias = d.get("bias", "neutral").upper()
        trend= d.get("trend", {})
        obv  = d.get("obv_trend", "N/A")

        last   = p.get("last", 0)
        change = p.get("pct_change", 0)
        sign   = "+" if change >= 0 else ""
        adx_str = f"ADX={fmt(adx.get('value'))}{'!' if adx.get('strong') else ''}" if adx else ""
        lines.append(f"\n  [{tf}]  Price={pfmt(last)} ({sign}{fmt(change)}%)  Bias={bias}  {adx_str}  TrendScore={trend.get('pct',0)}%")

        ema_parts = []
        for period in [9, 21, 50, 200]:
            v = e.get(f"ema{period}")
            if v:
                ema_parts.append(f"EMA{period}={pfmt(v)}{'>' if last > v else '<'}")
        if ema_parts:
            lines.append(f"        EMAs: {' | '.join(ema_parts)}")

        rsi_str = f"RSI={fmt(rsi)} ({d.get('rsi_signal','N/A')})" if rsi else "RSI=N/A"
        if macd and macd.get("macd") is not None:
            cross = f" [{macd.get('crossover','none').upper()} CROSS]" if macd.get("crossover","none") != "none" else ""
            macd_str = f"MACD={fmt(macd.get('macd'),4)} hist={fmt(macd.get('histogram'),4)} [{macd.get('trend','').upper()}{cross}]"
        else:
            macd_str = "MACD=N/A"
        lines.append(f"        {rsi_str}  |  {macd_str}")

        if bb:
            sq = "[SQUEEZE] " if bb.get("squeeze") else ""
            lines.append(f"        BB: {pfmt(bb.get('lower'))} - {pfmt(bb.get('mid'))} - {pfmt(bb.get('upper'))}  %B={fmt(bb.get('pct_b'),3)}  {sq}Pos={bb.get('position','N/A')}")

        vwap_str = f"VWAP={pfmt(vwap.get('value'))} ({'+' if (vwap.get('pct_diff') or 0) >= 0 else ''}{fmt(vwap.get('pct_diff'))}% {vwap.get('position','')})" if vwap else "VWAP=N/A"
        atr_str  = f"ATR={fmt(atr.get('value'),4)} ({fmt(atr.get('pct'))}%)" if atr else "ATR=N/A"
        vol_str  = f"Vol={vol.get('surge_ratio','?')}x {'SURGE' if vol.get('surge') else 'DRY' if vol.get('dry') else 'normal'}" if vol else "Vol=N/A"
        lines.append(f"        {vwap_str}  |  {atr_str}  |  {vol_str}")

        if sr:
            lines.append(f"        S/R: Sup={pfmt(sr.get('support'))}  Res={pfmt(sr.get('resistance'))}  Pos={fmt(sr.get('position_in_range'),2)}")

        stoch_str = f"Stoch K={fmt(stoch.get('k'))} D={fmt(stoch.get('d'))} [{stoch.get('signal','')}]" if stoch else ""
        obv_str   = f"OBV={obv}" if obv else ""
        if stoch_str or obv_str:
            lines.append(f"        {stoch_str}  {obv_str}")

    return "\n".join(lines)
