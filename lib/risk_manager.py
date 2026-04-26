"""
Risk Manager v6.2 — position sizing, Kelly criterion, correlation filter,
portfolio-level exposure limits.

v6.2: Crypto R:R floor lowered to 1.0 (was 1.5) — crypto signals have
      tighter moves; 24/7 market needs different thresholds than equities.
"""
import logging
import numpy as np
import pandas as pd
from typing import Optional
from dataclasses import dataclass, field
from lib.market_regime import get_regime

logger = logging.getLogger(__name__)

@dataclass
class SizedSignal:
    symbol: str
    direction: str
    confidence: float          # 0-100
    entry: float
    target: float
    stop: float
    kelly_fraction: float      # raw Kelly
    kelly_capped: float        # capped Kelly (max 25%)
    dollar_size: float         # $ to deploy
    shares: float              # qty
    risk_reward: float
    regime_adjusted: bool
    rejection_reason: Optional[str] = None

# ── Kelly Criterion ────────────────────────────────────────────────────────────

def kelly_fraction(win_rate: float, win_loss_ratio: float) -> float:
    """
    Classic Kelly formula: f* = (bp - q) / b
    where b = win/loss ratio, p = win rate, q = 1-p
    """
    p = win_rate / 100.0
    q = 1.0 - p
    b = win_loss_ratio
    if b <= 0 or p <= 0:
        return 0.0
    f = (b * p - q) / b
    return max(0.0, f)  # never negative

def calculate_position_size(signal: dict, equity: float, regime: dict,
                             max_risk_per_trade: float = 0.02) -> SizedSignal:
    """
    Calculate risk-adjusted position size using:
    1. Fixed fractional risk (2% of equity max loss per trade)
    2. Kelly Criterion (halved for conservatism = half-Kelly)
    3. Regime multiplier (reduce in bear/choppy markets)
    4. Confidence multiplier
    
    Returns the SMALLER of the two (fixed fractional vs half-Kelly).
    """
    sym     = signal['asset_symbol']
    entry   = float(signal['entry_price'] or 0)
    target  = float(signal['target_price'] or 0)
    stop    = float(signal['stop_loss'] or 0)
    conf    = float(signal['confidence'] or 65)
    
    if not entry or not target or not stop or stop >= entry or target <= entry:
        return SizedSignal(
            symbol=sym, direction=signal.get('direction','Long'),
            confidence=conf, entry=entry, target=target, stop=stop,
            kelly_fraction=0, kelly_capped=0, dollar_size=0, shares=0,
            risk_reward=0, regime_adjusted=False,
            rejection_reason='Invalid price levels'
        )
    
    # Risk/Reward
    risk_per_share   = entry - stop
    reward_per_share = target - entry
    rr_ratio         = reward_per_share / risk_per_share if risk_per_share > 0 else 0
    
    # Crypto gets a lower R:R floor (1.0) — tighter moves, 24/7 markets
    # Equity keeps the stricter 1.5 threshold
    is_crypto_sig = '/' in sym or sym.upper().endswith('USD')
    min_rr = 1.0 if is_crypto_sig else 1.5
    if rr_ratio < min_rr:
        return SizedSignal(
            symbol=sym, direction=signal.get('direction','Long'),
            confidence=conf, entry=entry, target=target, stop=stop,
            kelly_fraction=0, kelly_capped=0, dollar_size=0, shares=0,
            risk_reward=round(rr_ratio, 2), regime_adjusted=False,
            rejection_reason=f'R:R too low ({rr_ratio:.2f} < {min_rr})' 
        )
    
    # ── Fixed Fractional ──────────────────────────────────────────────────────
    # Max loss = 2% of equity
    max_loss_dollars = equity * max_risk_per_trade
    shares_by_risk   = max_loss_dollars / risk_per_share
    dollar_by_risk   = shares_by_risk * entry
    
    # ── Kelly ─────────────────────────────────────────────────────────────────
    # Estimate win rate from confidence (confidence = model's estimate of success)
    # We use half-Kelly to avoid overbetting
    win_rate = max(50.0, min(90.0, conf))  # clamp between 50-90%
    kf       = kelly_fraction(win_rate, rr_ratio)
    half_kelly_dollars = equity * kf * 0.5  # half-Kelly
    
    # ── Regime Multiplier ─────────────────────────────────────────────────────
    risk_level = regime.get('risk', 'medium')
    regime_mult = {
        'low':         1.0,
        'medium':      0.8,
        'medium-high': 0.6,
        'high':        0.4,
        'unknown':     0.7,
    }.get(risk_level, 0.7)
    
    # ── Confidence Multiplier ─────────────────────────────────────────────────
    conf_mult = 0.5 + (conf - 50) / 100.0  # 50% conf = 0.5x, 90% conf = 0.9x
    
    # ── Final Size ────────────────────────────────────────────────────────────
    # Take min of fixed-fractional and half-Kelly, then apply multipliers
    base_dollars = min(dollar_by_risk, half_kelly_dollars)
    final_dollars = base_dollars * regime_mult * conf_mult
    
    # Hard caps
    final_dollars = max(200.0, min(final_dollars, equity * 0.05))  # $200 min, 5% equity max
    
    shares = final_dollars / entry
    is_crypto = '/' in sym
    if not is_crypto:
        shares = max(1.0, round(shares))
        final_dollars = shares * entry
    else:
        shares = round(shares, 8)
    
    return SizedSignal(
        symbol=sym, direction=signal.get('direction','Long'),
        confidence=conf, entry=entry, target=target, stop=stop,
        kelly_fraction=round(kf, 4),
        kelly_capped=round(min(kf, 0.25), 4),
        dollar_size=round(final_dollars, 2),
        shares=shares,
        risk_reward=round(rr_ratio, 2),
        regime_adjusted=regime_mult < 1.0
    )


# ── Correlation Filter ─────────────────────────────────────────────────────────

# Sector groupings — if you already hold one, be selective about adding more
SECTOR_MAP = {
    # Semis
    'NVDA':'semis','AMD':'semis','AVGO':'semis','TSM':'semis','INTC':'semis',
    'QCOM':'semis','SMCI':'semis','ARM':'semis','SOXX':'semis',
    # Big Tech
    'MSFT':'bigtech','GOOGL':'bigtech','AAPL':'bigtech','META':'bigtech',
    'AMZN':'bigtech','QQQ':'bigtech',
    # Defense
    'RTX':'defense','LMT':'defense','NOC':'defense','GD':'defense','BA':'defense',
    # Energy
    'XOM':'energy','CVX':'energy','COP':'energy','FANG':'energy','USO':'energy','UNG':'energy',
    # Gold/PM
    'GLD':'gold','SLV':'gold','GDX':'gold','GDXJ':'gold',
    # Crypto majors
    'BTC/USD':'btc','ETH/USD':'eth',
    # Crypto alts
    'SOL/USD':'altcoin','XRP/USD':'altcoin','BNB/USD':'altcoin','AVAX/USD':'altcoin',
    'LINK/USD':'altcoin','DOGE/USD':'altcoin','ADA/USD':'altcoin','AAVE/USD':'altcoin',
}

def filter_correlated(signals: list[dict], held_symbols: set[str],
                       max_per_sector: int = 2) -> list[dict]:
    """
    Remove signals where we'd be over-concentrating in one sector.
    Also removes signals for symbols already held.
    Returns filtered + annotated signal list.
    """
    sector_counts = {}
    
    # Count existing positions by sector
    for sym in held_symbols:
        sector = SECTOR_MAP.get(sym)
        if sector:
            sector_counts[sector] = sector_counts.get(sector, 0) + 1
    
    passed = []
    for sig in signals:
        sym = sig.get('asset_symbol', '')
        
        # Skip already held
        if sym in held_symbols:
            sig['filter_reason'] = 'already_held'
            continue
        
        sector = SECTOR_MAP.get(sym)
        count  = sector_counts.get(sector, 0) if sector else 0
        
        if sector and count >= max_per_sector:
            sig['filter_reason'] = f'sector_concentrated ({sector}: {count}/{max_per_sector})'
            continue
        
        if sector:
            sector_counts[sector] = count + 1
        
        passed.append(sig)
    
    return passed


# ── Portfolio Heat ─────────────────────────────────────────────────────────────

def portfolio_heat(positions: list, equity: float) -> dict:
    """
    Calculate current portfolio risk exposure.
    Returns metrics useful for deciding whether to add more positions.
    """
    if not positions or not equity:
        return {'heat': 0.0, 'total_value': 0.0, 'position_count': 0,
                'max_drawdown_est': 0.0, 'status': 'safe'}
    
    total_value = sum(float(p.get('market_value', 0)) for p in positions)
    deployment_pct = total_value / equity * 100
    
    # Estimate portfolio heat (weighted avg stop distance)
    heat_scores = []
    for p in positions:
        plpc = float(p.get('unrealized_plpc', 0))
        # If losing > 5%, this position contributes to heat
        if plpc < -5:
            heat_scores.append(abs(plpc))
    
    avg_heat = np.mean(heat_scores) if heat_scores else 0
    
    if deployment_pct > 80 or avg_heat > 10:
        status = 'hot'
    elif deployment_pct > 60 or avg_heat > 5:
        status = 'warm'
    else:
        status = 'safe'
    
    return {
        'heat': round(avg_heat, 2),
        'total_value': round(total_value, 2),
        'deployment_pct': round(deployment_pct, 2),
        'position_count': len(positions),
        'status': status
    }
