"""
Signal Scorer — scores signals 0-100 using multiple factors beyond just LLM confidence.
This replaces the "just use LLM confidence" approach with a proper scoring system.

Factors:
  1. LLM confidence (base)
  2. TA confluence (how many timeframes agree)
  3. R:R ratio quality
  4. Volume confirmation
  5. Regime alignment
  6. Signal freshness
  7. Earnings risk penalty
"""
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

def score_signal(signal: dict, ta_data: dict, regime: dict,
                  earnings_risk: bool = False) -> dict:
    """
    Score a signal and return enriched signal dict with composite_score.
    
    ta_data: { '1H': {...}, '4H': {...}, '1D': {...} }
    regime: from market_regime.get_regime()
    """
    scores = {}
    
    # 1. LLM Confidence (0-100) → weight 30%
    llm_conf = float(signal.get('confidence', 65))
    scores['llm'] = llm_conf
    
    # 2. TA Confluence — how many timeframes show bullish bias
    bullish_tfs = 0
    total_tfs   = 0
    for tf, td in ta_data.items():
        if not td or td.get('error'):
            continue
        total_tfs += 1
        bias = (td.get('bias') or '').lower()
        rsi  = td.get('rsi') or 50
        macd = td.get('macd') or {}
        if bias == 'bullish':
            bullish_tfs += 1
        elif rsi < 40:  # oversold = bullish for bounce
            bullish_tfs += 0.5
        if macd.get('crossover') == 'bullish':
            bullish_tfs += 0.5
    
    conf_score = (bullish_tfs / total_tfs * 100) if total_tfs else 50
    scores['ta_confluence'] = conf_score
    
    # 3. R:R Ratio (target: >= 2.0 = 100, 1.5 = 50, < 1.5 = 0)
    entry  = float(signal.get('entry_price') or 0)
    target = float(signal.get('target_price') or 0)
    stop   = float(signal.get('stop_loss') or 0)
    rr = (target - entry) / (entry - stop) if (entry > stop > 0 and target > entry) else 0
    rr_score = min(100, rr / 3.0 * 100)  # 3.0 R:R = 100 points
    scores['rr'] = rr_score
    
    # 4. Volume confirmation (surge = +20 pts, dry = -20 pts)
    vol_score = 50
    for tf in ['4H', '1D']:
        td = ta_data.get(tf, {}) or {}
        vol = td.get('volume', {}) or {}
        if vol.get('surge'):
            vol_score = 80
            break
        elif vol.get('dry'):
            vol_score = 30
    scores['volume'] = vol_score
    
    # 5. Regime alignment
    risk = regime.get('risk', 'medium')
    direction = (signal.get('direction') or 'Long').lower()
    regime_score = {
        'low':         85 if direction == 'long' else 70,
        'medium':      65,
        'medium-high': 45,
        'high':        30 if direction == 'bounce' else 20,
        'unknown':     50,
    }.get(risk, 50)
    scores['regime'] = regime_score
    
    # 6. Earnings risk penalty
    earnings_penalty = -25 if earnings_risk else 0
    
    # 7. BB Squeeze bonus (explosive move incoming)
    squeeze_bonus = 0
    for tf in ['4H', '1H']:
        td = ta_data.get(tf, {}) or {}
        bb = td.get('bollinger_bands', {}) or {}
        if bb.get('squeeze'):
            squeeze_bonus = 10
            break
    
    # ── Weighted composite ──────────────────────────────────────────────────────
    composite = (
        scores['llm']           * 0.30 +
        scores['ta_confluence'] * 0.25 +
        scores['rr']            * 0.20 +
        scores['volume']        * 0.10 +
        scores['regime']        * 0.15 +
        earnings_penalty        +
        squeeze_bonus
    )
    composite = max(0, min(100, composite))
    
    signal['composite_score'] = round(composite, 1)
    signal['score_breakdown'] = {
        'llm': round(scores['llm'], 1),
        'ta_confluence': round(scores['ta_confluence'], 1),
        'rr': round(rr_score, 1),
        'volume': round(scores['volume'], 1),
        'regime': round(scores['regime'], 1),
        'earnings_penalty': earnings_penalty,
        'squeeze_bonus': squeeze_bonus
    }
    signal['earnings_risk'] = earnings_risk
    signal['rr_ratio'] = round(rr, 2) if rr else None
    
    return signal
