"""
Job: Execute Trading Signals → Alpaca Bracket Orders
"""
import logging, uuid
from datetime import datetime, timezone, timedelta
from app.database import get_db, TradingSignal
from lib.alpaca_client import (
    get_account, get_positions, submit_bracket_order,
    normalize_symbol, is_crypto
)

logger = logging.getLogger(__name__)

BUDGET_PER_TRADE  = 1000.0
EQUITY_CAP_PCT    = 0.50
MIN_POSITIONS_CAP = 8
SIGNAL_MAX_AGE_H  = 4
MIN_CONFIDENCE    = 55
MIN_SHARES        = 1

def run():
    logger.info("[Execute] Starting signal execution job...")
    
    try:
        account   = get_account()
        equity    = float(account.equity)
        cash      = float(account.cash)
        positions = get_positions()
    except Exception as e:
        logger.error(f"[Execute] Alpaca connection failed: {e}")
        return {'error': str(e)}
    
    # Current positions
    held_symbols = {p.symbol for p in positions}
    market_value_held = sum(float(p.market_value or 0) for p in positions)
    
    # Max positions cap
    max_positions = max(MIN_POSITIONS_CAP, int(equity * EQUITY_CAP_PCT / 1000))
    available_slots = max_positions - len(positions)
    
    if available_slots <= 0:
        logger.info(f"[Execute] At max positions ({max_positions}), skipping")
        return {'executed': 0, 'reason': 'at_max_positions'}
    
    # Budget remaining
    max_deployed = equity * EQUITY_CAP_PCT
    remaining_budget = max(0, max_deployed - market_value_held)
    
    if remaining_budget < BUDGET_PER_TRADE:
        logger.info(f"[Execute] Insufficient budget: ${remaining_budget:.2f}")
        return {'executed': 0, 'reason': 'insufficient_budget'}
    
    # Fetch active signals
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=SIGNAL_MAX_AGE_H)).isoformat()
    with get_db() as db:
        signals = db.query(TradingSignal).filter(
            TradingSignal.status == 'Active',
            TradingSignal.generated_at >= cutoff,
            TradingSignal.confidence >= MIN_CONFIDENCE
        ).order_by(TradingSignal.confidence.desc()).all()
        
        executed = 0
        
        for sig in signals:
            if executed >= available_slots:
                break
            if remaining_budget < BUDGET_PER_TRADE * 0.5:
                break
            
            sym_raw = sig.asset_symbol
            sym, crypto = normalize_symbol(sym_raw)
            
            # Skip if already held
            if sym in held_symbols or sym_raw in held_symbols:
                continue
            
            # Skip if no price data
            entry = float(sig.entry_price or 0)
            target = float(sig.target_price or 0)
            stop = float(sig.stop_loss or 0)
            
            if not entry or not target or not stop:
                continue
            if stop >= entry or target <= entry:
                continue
            
            # Calculate quantity
            budget = min(BUDGET_PER_TRADE, remaining_budget)
            if crypto:
                qty = round(budget / entry, 8)
                if qty * entry < 1.0:
                    logger.debug(f"[Execute] {sym}: qty too small (${qty*entry:.4f})")
                    continue
            else:
                qty = max(MIN_SHARES, int(budget / entry))
                if qty * entry > budget * 1.1:
                    qty = max(1, qty - 1)
            
            try:
                result = submit_bracket_order(
                    symbol=sym,
                    qty=qty,
                    entry_price=entry,
                    take_profit=target,
                    stop_loss=stop
                )
                sig.status = 'Executed'
                sig.updated_date = datetime.now(timezone.utc).isoformat()
                held_symbols.add(sym)
                remaining_budget -= qty * entry
                executed += 1
                logger.info(f"[Execute] ✓ {sym} qty={qty} entry=${entry:.2f} target=${target:.2f} stop=${stop:.2f}")
            except Exception as e:
                sig.status = 'Rejected'
                sig.updated_date = datetime.now(timezone.utc).isoformat()
                logger.error(f"[Execute] ✗ {sym}: {e}")
    
    logger.info(f"[Execute] Done — {executed} orders submitted")
    return {'executed': executed}

# Integration hook — call this from run() after getting signals
def size_and_filter_signals(signals: list, positions: list, equity: float) -> list:
    """Apply Kelly sizing + correlation filter to signals before execution."""
    try:
        from lib.market_regime import get_regime
        from lib.risk_manager import calculate_position_size, filter_correlated, portfolio_heat
        
        regime = get_regime()
        heat   = portfolio_heat([{'market_value': float(p.market_value or 0),
                                   'unrealized_plpc': float(p.unrealized_plpc or 0)*100}
                                  for p in positions], equity)
        
        if heat['status'] == 'hot':
            logger.warning(f"[Execute] Portfolio hot ({heat['heat']:.1f}% avg loss) — skipping new entries")
            return []
        
        held = {str(p.symbol) for p in positions}
        signal_dicts = [{'asset_symbol': s.asset_symbol, 'direction': s.direction,
                         'confidence': s.composite_score or s.confidence,
                         'entry_price': s.entry_price, 'target_price': s.target_price,
                         'stop_loss': s.stop_loss} for s in signals]
        
        filtered = filter_correlated(signal_dicts, held, max_per_sector=2)
        
        sized = []
        for sig in filtered:
            sz = calculate_position_size(sig, equity, regime)
            if sz.rejection_reason:
                logger.debug(f"[Execute] {sz.symbol} rejected: {sz.rejection_reason}")
                continue
            sized.append((sig, sz))
        
        return sized
    except Exception as e:
        logger.error(f"[Execute] Sizing/filtering failed: {e}")
        return []
