"""
Market Regime Detection — uses `ta` library (pure Python, no numba).
"""
import logging
import pandas as pd
import ta.trend as tat
import ta.momentum as tam
from lib.ohlcv import fetch_multi_timeframe

logger = logging.getLogger(__name__)


def get_regime() -> dict:
    regime = {
        "label": "Unknown", "risk": "medium",
        "spy_trend": "unknown", "recommendation": "Standard position sizing"
    }
    try:
        spy_bars = fetch_multi_timeframe("SPY", ["1D"])
        spy_1d   = spy_bars.get("1D")
        if spy_1d is None or len(spy_1d) < 50:
            return regime

        close = spy_1d["close"]
        high  = spy_1d["high"]
        low   = spy_1d["low"]

        ema21  = float(tat.EMAIndicator(close=close, window=21).ema_indicator().iloc[-1])
        ema50  = float(tat.EMAIndicator(close=close, window=50).ema_indicator().iloc[-1])
        ema200 = float(tat.EMAIndicator(close=close, window=200).ema_indicator().iloc[-1])
        rsi    = float(tam.RSIIndicator(close=close, window=14).rsi().iloc[-1])
        adx    = float(tat.ADXIndicator(high=high, low=low, close=close, window=14).adx().iloc[-1])
        last   = float(close.iloc[-1])

        high_52w     = float(close.tail(252).max())
        drawdown_pct = (last - high_52w) / high_52w * 100

        if last > ema21 > ema50 > ema200:   spy_trend = "strong_uptrend"
        elif last > ema50 > ema200:          spy_trend = "uptrend"
        elif last < ema21 < ema50:           spy_trend = "downtrend"
        else:                                spy_trend = "choppy"

        regime.update({
            "spy_trend": spy_trend, "spy_last": round(last, 2),
            "spy_ema21": round(ema21, 2), "spy_ema50": round(ema50, 2), "spy_ema200": round(ema200, 2),
            "spy_rsi": round(rsi, 1), "spy_adx": round(adx, 1), "spy_drawdown_pct": round(drawdown_pct, 1),
        })

        if spy_trend in ("strong_uptrend", "uptrend") and rsi < 75 and adx > 20:
            regime.update({"label": "Risk-On Bull", "risk": "low",
                           "recommendation": "Full position sizing. Favor momentum longs."})
        elif spy_trend == "choppy" and adx < 20:
            regime.update({"label": "Range-Bound", "risk": "medium",
                           "recommendation": "Reduce size 30%. Favor mean-reversion Bounce signals."})
        elif spy_trend == "downtrend" or drawdown_pct < -15:
            regime.update({"label": "Bear / Risk-Off", "risk": "high",
                           "recommendation": "Reduce size 50-70%. Only highest confidence bounces."})
        elif rsi > 72:
            regime.update({"label": "Overbought Bull", "risk": "medium-high",
                           "recommendation": "Reduce size 20%. Wait for pullbacks."})
        else:
            regime.update({"label": "Neutral", "risk": "medium", "recommendation": "Standard sizing."})

    except Exception as e:
        logger.error(f"[Regime] SPY analysis failed: {e}")

    return regime


def regime_to_prompt_block(regime: dict) -> str:
    lines = [
        "=== CURRENT MARKET REGIME ===",
        f"Regime:      {regime.get('label', 'Unknown')}",
        f"Risk Level:  {regime.get('risk', 'unknown').upper()}",
        f"SPY Trend:   {regime.get('spy_trend', 'unknown')}",
    ]
    if regime.get("spy_last"):
        lines.append(f"SPY Price:   ${regime['spy_last']} (EMA21={regime.get('spy_ema21')} EMA50={regime.get('spy_ema50')} EMA200={regime.get('spy_ema200')})")
        lines.append(f"SPY RSI:     {regime.get('spy_rsi')}  ADX: {regime.get('spy_adx')}  Drawdown: {regime.get('spy_drawdown_pct')}%")
    lines.append(f"Sizing Rule: {regime.get('recommendation', 'Standard')}")
    return "\n".join(lines)
