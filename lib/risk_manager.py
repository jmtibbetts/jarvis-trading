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
from lib import trade_side

logger = logging.getLogger(__name__)

# Round-trip cost ceiling as a multiple of the risk taken. Above this the
# trade is structurally unprofitable: costs alone eat half the risk budget.
MAX_COST_R = 0.50

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
    # ── Added by the profitability refactor (P0) ─────────────────────────
    decision: str = "TRADE"            # TRADE | NO_TRADE
    loss_at_stop: float = 0.0          # dollars lost if the stop fills
    max_allowed_loss: float = 0.0      # the risk budget it was checked against
    side: str = "long"

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
                             max_risk_per_trade: float = 0.02,
                             lifecycle_multiplier: float = 1.0) -> SizedSignal:
    """
    Risk-adjusted position size:
    1. Fixed fractional risk (2% of equity max loss, scaled by the
       strategy's lifecycle multiplier BEFORE quantity is solved)
    2. Quarter-Kelly from MEASURED evidence only — lower Wilson bound of
       the observed win rate, measured payoff ratio, 25-trade sample
       floor; no valid statistics means no Kelly contribution at all
    3. Regime multiplier (reduce in bear/choppy markets)

    Model confidence has NO sizing effect. The field survives on
    SizedSignal as a diagnostic only.
    """
    sym     = signal['asset_symbol']
    entry   = float(signal['entry_price'] or 0)
    target  = float(signal['target_price'] or 0)
    stop    = float(signal['stop_loss'] or 0)
    conf    = float(signal['confidence'] or 65)
    
    direction = signal.get('direction', 'Long')
    side = trade_side.normalize_side(direction)

    # Direction-aware validation. The previous test (`stop >= entry or
    # target <= entry`) encodes LONG geometry, so every short was rejected
    # here as "Invalid price levels" before its R:R was ever computed —
    # shorts could not be sized at all.
    ok, why = trade_side.validate_levels(direction, entry, stop, target)
    if not ok:
        return SizedSignal(
            symbol=sym, direction=direction,
            confidence=conf, entry=entry, target=target, stop=stop,
            kelly_fraction=0, kelly_capped=0, dollar_size=0, shares=0,
            risk_reward=0, regime_adjusted=False,
            rejection_reason=f'Invalid price levels: {why}',
            decision="NO_TRADE", side=side,
        )

    # Distances are absolute — correct for both sides once the layout above
    # has been validated.
    risk_per_share   = trade_side.risk_distance(entry, stop)
    reward_per_share = trade_side.reward_distance(entry, target)
    rr_ratio         = trade_side.rr_ratio(entry, stop, target)
    
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
            rejection_reason=f'R:R too low ({rr_ratio:.2f} < {min_rr})',
            decision="NO_TRADE", side=side,
        )
    
    # ── Transaction-cost gate ─────────────────────────────────────────────────
    # Costs are paid in R, and R is set by the STOP DISTANCE — so the same
    # 0.25% crypto fee is 0.20R on a 5% stop and 3.40R on a 0.3% scalp. A
    # setup whose round-trip cost consumes more than MAX_COST_R of the risk
    # taken cannot pay for itself no matter how good the signal is, so it is
    # rejected here rather than sized. This is deliberately a structural
    # check (cost vs risk), not an expectancy check — calibrated P(win)
    # arrives in P1 and will refine it into full net-EV rejection.
    try:
        from lib.transaction_costs import estimate_costs
        costs = estimate_costs(
            sym, entry, stop,
            order_type=signal.get("order_type", "market"),
            is_short=(side == trade_side.SHORT),
            hold_hours=float(signal.get("expected_hold_hours") or 0),
            funding_rate_8h=signal.get("funding_rate_8h"),
        )
        cost_r = costs.get("total_r")
        if cost_r is not None and cost_r > MAX_COST_R:
            return SizedSignal(
                symbol=sym, direction=direction, confidence=conf,
                entry=entry, target=target, stop=stop,
                kelly_fraction=0, kelly_capped=0, dollar_size=0, shares=0,
                risk_reward=round(rr_ratio, 2), regime_adjusted=False,
                rejection_reason=(f'Transaction costs {cost_r:.2f}R exceed the '
                                  f'{MAX_COST_R:.2f}R ceiling — the stop is too tight '
                                  f'to pay for spread, fees and slippage'),
                decision="NO_TRADE", side=side,
            )
    except Exception as e:
        logger.debug(f"[Risk] Cost gate skipped for {sym}: {e}")

    # ── Fixed Fractional ──────────────────────────────────────────────────────
    # Max loss = 2% of equity, scaled by the strategy's lifecycle state
    # (REDUCED 0.50 / EXPERIMENTAL 0.25) — applied to the RISK BUDGET so
    # quantity is solved from reduced risk, never multiplied onto an
    # already-rounded order afterward (P0.5).
    max_loss_dollars = equity * max_risk_per_trade * max(0.0, min(1.0, lifecycle_multiplier))
    shares_by_risk   = max_loss_dollars / risk_per_share
    dollar_by_risk   = shares_by_risk * entry

    # ── Kelly, from MEASURED evidence only (P0.2) ─────────────────────────────
    # The old path clamped the signal's "confidence" to 50-90% and called
    # it a win probability — and the executor was writing the COMPOSITE
    # SCORE into that field, so a number measured inverted against
    # outcomes was being bet as if it were p(win). Kelly now feeds
    # exclusively from the expectancy table: the LOWER Wilson bound of the
    # measured win rate (uncertainty shrinks size), measured payoff ratio,
    # a 25-trade sample floor, and a quarter-Kelly cap. No statistically
    # valid probability -> Kelly contributes NOTHING (fixed-fractional
    # alone), never a flattering default.
    kelly_dollars = None
    kf = 0.0
    try:
        from lib.expectancy import MIN_SAMPLE as _EV_MIN
        from lib.expectancy import lookup as _ev_lookup
        stats = _ev_lookup(signal.get("strategy"), signal.get("asset_class"),
                           direction, signal.get("timeframe"))
        if stats and stats.get("raw_sample", 0) >= _EV_MIN:
            p_lower = float((stats.get("p_win_ci") or [0, 0])[0]) * 100.0
            avg_win = float(stats.get("avg_win_r") or 0)
            avg_loss = abs(float(stats.get("avg_loss_r") or 0))
            if avg_win > 0 and avg_loss > 0:
                kf = kelly_fraction(p_lower, avg_win / avg_loss)
                kelly_dollars = equity * kf * 0.25 * max(0.0, min(1.0, lifecycle_multiplier))
    except Exception as e:
        logger.debug(f"[Risk] measured-Kelly unavailable for {sym}: {e}")

    # ── Regime Multiplier ─────────────────────────────────────────────────────
    risk_level = regime.get('risk', 'medium')
    regime_mult = {
        'low':         1.0,
        'medium':      0.8,
        'medium-high': 0.6,
        'high':        0.4,
        'unknown':     0.7,
    }.get(risk_level, 0.7)

    # ── Final Size ────────────────────────────────────────────────────────────
    # Fixed-fractional bounded by measured Kelly when one exists. The old
    # confidence multiplier (0.5-0.9x from the same poisoned field) is
    # deleted: model self-belief has no sizing effect (invariant #1).
    base_dollars = min(dollar_by_risk, kelly_dollars) if kelly_dollars is not None else dollar_by_risk
    final_dollars = base_dollars * regime_mult
    
    # Cap by equity share. The old line also applied max(200.0, ...), a FLOOR
    # that overrode the risk budget: when the risk math said $50, it deployed
    # $200 anyway — four times the intended risk. A minimum position size is
    # not a risk control, it is a violation of one. The correct size for a
    # trade too small to express is zero (Phase 3: NO_TRADE).
    # The notional cap scales with the lifecycle multiplier too — with
    # tight stops this cap binds before the risk budget does, and a
    # REDUCED strategy that sizes identically to an ACTIVE one whenever
    # the cap binds would make invariant #11 true on paper only.
    final_dollars = min(final_dollars,
                        equity * 0.05 * max(0.0, min(1.0, lifecycle_multiplier)))

    shares = final_dollars / entry if entry > 0 else 0.0
    is_crypto = '/' in sym
    if not is_crypto:
        # Whole shares only; rounding DOWN so the risk invariant cannot be
        # breached by rounding up into a bigger position than budgeted.
        shares = float(int(shares))
        final_dollars = shares * entry
    else:
        shares = round(shares, 8)
        final_dollars = shares * entry

    # ── The invariant: loss at the stop must not exceed the risk budget ──
    realized_loss_at_stop = trade_side.loss_at_stop(shares, entry, stop)
    if shares <= 0 or final_dollars <= 0:
        return SizedSignal(
            symbol=sym, direction=direction, confidence=conf,
            entry=entry, target=target, stop=stop,
            kelly_fraction=round(kf, 4), kelly_capped=0, dollar_size=0, shares=0,
            risk_reward=round(rr_ratio, 2), regime_adjusted=True,
            rejection_reason='Position rounds to zero at this risk budget',
            decision="NO_TRADE", side=side,
            loss_at_stop=0.0, max_allowed_loss=round(max_loss_dollars, 2),
        )
    if realized_loss_at_stop > max_loss_dollars * 1.001:   # tolerance for float noise
        return SizedSignal(
            symbol=sym, direction=direction, confidence=conf,
            entry=entry, target=target, stop=stop,
            kelly_fraction=round(kf, 4), kelly_capped=0, dollar_size=0, shares=0,
            risk_reward=round(rr_ratio, 2), regime_adjusted=True,
            rejection_reason=(f'Loss at stop ${realized_loss_at_stop:,.2f} exceeds '
                              f'risk budget ${max_loss_dollars:,.2f}'),
            decision="NO_TRADE", side=side,
            loss_at_stop=round(realized_loss_at_stop, 2),
            max_allowed_loss=round(max_loss_dollars, 2),
        )
    
    return SizedSignal(
        symbol=sym, direction=signal.get('direction','Long'),
        confidence=conf, entry=entry, target=target, stop=stop,
        kelly_fraction=round(kf, 4),
        kelly_capped=round(min(kf, 0.25), 4),
        dollar_size=round(final_dollars, 2),
        shares=shares,
        risk_reward=round(rr_ratio, 2),
        regime_adjusted=regime_mult < 1.0,
        decision="TRADE", side=side,
        loss_at_stop=round(realized_loss_at_stop, 2),
        max_allowed_loss=round(max_loss_dollars, 2),
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
        # Real max-drawdown (computed from equity-snapshot history) lives in
        # lib/performance_analytics.py / GET /api/performance/analytics —
        # this function only assesses current point-in-time exposure.
        return {'heat': 0.0, 'total_value': 0.0, 'position_count': 0, 'status': 'safe'}
    
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
