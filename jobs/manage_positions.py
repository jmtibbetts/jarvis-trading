"""
Job: Manage Open Positions — trailing stops, profit protection, displacement.
"""
import logging
from datetime import datetime, timezone
from app.database import get_db, TradingSignal, Position
from lib.alpaca_client import get_positions, get_account, close_position, normalize_symbol

logger = logging.getLogger(__name__)

# Profit protection tiers
TIERS = [
    {'gain_pct': 15.0, 'action': 'close',           'label': 'Target hit — close'},
    {'gain_pct': 10.0, 'action': 'trail_tight',     'label': '10% gain — tight trail (5%)'},
    {'gain_pct':  5.0, 'action': 'trail_moderate',  'label': '5% gain — moderate trail (8%)'},
    {'gain_pct': -8.0, 'action': 'close',            'label': 'Stop hit — close'},
]

def run():
    logger.info("[Positions] Running position management...")
    
    try:
        positions = get_positions()
        account   = get_account()
    except Exception as e:
        logger.error(f"[Positions] Alpaca error: {e}")
        return {'error': str(e)}
    
    if not positions:
        logger.info("[Positions] No open positions")
        return {'managed': 0}
    
    now_iso = datetime.now(timezone.utc).isoformat()
    managed = 0
    
    # Cache positions to DB
    with get_db() as db:
        # Clear old cache
        db.query(Position).delete()
        
        for pos in positions:
            sym = str(pos.symbol)
            qty = float(pos.qty or 0)
            avg = float(pos.avg_entry_price or 0)
            mv  = float(pos.market_value or 0)
            pl  = float(pos.unrealized_pl or 0)
            plpc= float(pos.unrealized_plpc or 0) * 100  # to percent
            side = str(pos.side)
            asset_class = 'Crypto' if '/' in sym else 'Equity'
            
            db.add(Position(
                symbol=sym, qty=qty, avg_entry=avg,
                market_value=mv, unrealized_pl=pl,
                unrealized_plpc=plpc, side=side,
                asset_class=asset_class, updated_at=now_iso
            ))
            
            # Apply profit protection
            for tier in TIERS:
                if plpc >= tier['gain_pct'] or plpc <= tier['gain_pct']:
                    action = tier['action']
                    if action == 'close':
                        try:
                            close_position(sym)
                            logger.info(f"[Positions] Closed {sym} at {plpc:.1f}% PnL — {tier['label']}")
                            # Mark signal as closed
                            sig = db.query(TradingSignal).filter(
                                TradingSignal.asset_symbol.in_([sym, sym.replace('/','')]),
                                TradingSignal.status == 'Executed'
                            ).first()
                            if sig:
                                sig.status = 'Closed'
                                sig.updated_date = now_iso
                            managed += 1
                        except Exception as e:
                            logger.error(f"[Positions] Close {sym} failed: {e}")
                    break
    
    logger.info(f"[Positions] Done — {managed} positions managed")
    return {'managed': managed, 'total': len(positions)}
