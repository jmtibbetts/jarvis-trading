"""
Market Regime Detection — things Python/pandas-ta makes easy that were
skipped entirely in the Node.js version.

Regime = { 'label': str, 'risk': str, 'vix_level': float, 'spy_trend': str,
           'breadth': str, 'recommendation': str }
"""
import logging
import pandas as pd
import pandas_ta as ta
import numpy as np
from lib.ohlcv import fetch_multi_timeframe

logger = logging.getLogger(__name__)

def get_regime() -> dict:
    """
    Compute current market regime using SPY + VIX.
    Returns a regime dict that gets injected into every LLM signal prompt.
    """
    regime = {
        'label': 'Unknown',
        'risk': 'medium',
        'spy_trend': 'unknown',
        'vix_level': None,
        'breadth': 'unknown',
        'recommendation': 'Standard position sizing'
    }
    
    try:
        # SPY multi-timeframe
        spy_bars = fetch_multi_timeframe('SPY', ['1D'])
        spy_1d = spy_bars.get('1D')
        
        if spy_1d is not None and len(spy_1d) >= 50:
            close = spy_1d['close']
            
            ema21  = ta.ema(close, 21).iloc[-1]
            ema50  = ta.ema(close, 50).iloc[-1]
            ema200 = ta.ema(close, 200).iloc[-1]
            rsi    = ta.rsi(close, 14).iloc[-1]
            last   = close.iloc[-1]
            
            # ADX for trend strength
            adx_df = ta.adx(spy_1d['high'], spy_1d['low'], close, 14)
            adx_col = [c for c in adx_df.columns if c.startswith('ADX_')]
            adx = float(adx_df[adx_col[0]].iloc[-1]) if adx_col else 20
            
            # Drawdown from 52-week high
            high_52w = close.tail(252).max()
            drawdown_pct = (last - high_52w) / high_52w * 100
            
            # Determine trend
            if last > ema21 > ema50 > ema200:
                spy_trend = 'strong_uptrend'
            elif last > ema50 > ema200:
                spy_trend = 'uptrend'
            elif last < ema21 < ema50:
                spy_trend = 'downtrend'
            else:
                spy_trend = 'choppy'
            
            regime['spy_trend'] = spy_trend
            regime['spy_last']  = round(float(last), 2)
            regime['spy_ema21'] = round(float(ema21), 2)
            regime['spy_ema50'] = round(float(ema50), 2)
            regime['spy_ema200']= round(float(ema200), 2)
            regime['spy_rsi']   = round(float(rsi), 1)
            regime['spy_adx']   = round(float(adx), 1)
            regime['spy_drawdown_pct'] = round(float(drawdown_pct), 1)
            
            # Regime classification
            if spy_trend in ('strong_uptrend', 'uptrend') and rsi < 75 and adx > 20:
                regime['label'] = 'Risk-On Bull'
                regime['risk']  = 'low'
                regime['recommendation'] = 'Full position sizing. Favor momentum longs. Trend-following > mean reversion.'
            elif spy_trend == 'choppy' and adx < 20:
                regime['label'] = 'Range-Bound'
                regime['risk']  = 'medium'
                regime['recommendation'] = 'Reduce size 30%. Favor mean-reversion Bounce signals. Tighter stops.'
            elif spy_trend == 'downtrend' or drawdown_pct < -15:
                regime['label'] = 'Bear / Risk-Off'
                regime['risk']  = 'high'
                regime['recommendation'] = 'Reduce size 50-70%. Only highest confidence bounces. Wider stops, smaller targets. Defensive assets (GLD, TLT) preferred.'
            elif rsi > 72:
                regime['label'] = 'Overbought Bull'
                regime['risk']  = 'medium-high'
                regime['recommendation'] = 'Reduce size 20%. Be selective — wait for pullbacks before entering. Risk of sharp reversal.'
            else:
                regime['label'] = 'Neutral'
                regime['risk']  = 'medium'
                regime['recommendation'] = 'Standard sizing. Mix of Long and Bounce signals appropriate.'
    
    except Exception as e:
        logger.error(f"[Regime] SPY analysis failed: {e}")
    
    return regime


def regime_to_prompt_block(regime: dict) -> str:
    """Format regime dict as a prompt block for LLM."""
    lines = [
        "=== CURRENT MARKET REGIME ===",
        f"Regime:       {regime.get('label','Unknown')}",
        f"Risk Level:   {regime.get('risk','unknown').upper()}",
        f"SPY Trend:    {regime.get('spy_trend','unknown')}",
    ]
    if regime.get('spy_last'):
        lines.append(f"SPY Price:    ${regime['spy_last']} (EMA21={regime.get('spy_ema21')} EMA50={regime.get('spy_ema50')} EMA200={regime.get('spy_ema200')})")
        lines.append(f"SPY RSI:      {regime.get('spy_rsi')}  ADX: {regime.get('spy_adx')}  Drawdown: {regime.get('spy_drawdown_pct')}%")
    lines.append(f"Sizing Rule:  {regime.get('recommendation','Standard position sizing')}")
    return '\n'.join(lines)
