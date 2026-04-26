"""
Job: Manage Positions v6.0 — fixed tier logic + equity curve snapshots.
"""
import logging, uuid
from datetime import datetime, timezone
from app.database import get_db, TradingSignal, Position, PortfolioSnapshot
from lib.alpaca_client import get_positions, get_account, close_position

logger = logging.getLogger(__name__)

TIERS=[
    {"min_gain":15.0,"max_gain":None, "action":"close",        "label":"≥15% — close"},
    {"min_gain":10.0,"max_gain":15.0, "action":"trail_tight",  "label":"10-15% — trail 5%"},
    {"min_gain":5.0, "max_gain":10.0, "action":"trail_moderate","label":"5-10% — trail 8%"},
    {"min_gain":None,"max_gain":-8.0, "action":"close",        "label":"≤-8% — cut loss"},
]

def _tier(plpc):
    for t in TIERS:
        mg,xg=t["min_gain"],t["max_gain"]
        if mg is not None and xg is not None:
            if mg<=plpc<xg: return t
        elif mg is not None and xg is None:
            if plpc>=mg: return t
        elif mg is None and xg is not None:
            if plpc<=xg: return t
    return None

def run():
    logger.info("[Positions] Running position management...")
    try:
        positions=get_positions(); account=get_account()
        equity=float(account.equity); cash=float(account.cash)
    except Exception as e:
        logger.error(f"[Positions] Alpaca error: {e}"); return {"error":str(e)}
    now_iso=datetime.now(timezone.utc).isoformat()
    closed=trailing=0
    total_mv=sum(float(p.market_value or 0) for p in positions)
    total_pl=sum(float(p.unrealized_pl or 0) for p in positions)
    with get_db() as db:
        db.query(Position).delete()
        for pos in positions:
            sym=str(pos.symbol); qty=float(pos.qty or 0); avg=float(pos.avg_entry_price or 0)
            mv=float(pos.market_value or 0); pl=float(pos.unrealized_pl or 0)
            plpc=float(pos.unrealized_plpc or 0)*100
            db.add(Position(symbol=sym,qty=qty,avg_entry=avg,market_value=mv,unrealized_pl=pl,unrealized_plpc=plpc,side=str(pos.side),asset_class="Crypto" if "/" in sym else "Equity",updated_at=now_iso))
            tier=_tier(plpc)
            if tier is None: continue
            if tier["action"]=="close":
                try:
                    close_position(sym)
                    logger.info(f"[Positions] ✓ Closed {sym} @ {plpc:+.1f}% | {tier['label']}")
                    sig=db.query(TradingSignal).filter(TradingSignal.asset_symbol.in_([sym,sym.replace("/","")]),TradingSignal.status=="Executed").first()
                    if sig: sig.status="Closed"; sig.updated_date=now_iso
                    closed+=1
                except Exception as e:
                    logger.error(f"[Positions] Close {sym} failed: {e}")
            else:
                pct=5.0 if tier["action"]=="trail_tight" else 8.0
                logger.info(f"[Positions] ⟳ Trail {sym} @ {plpc:+.1f}% — trail {pct}% | {tier['label']}"); trailing+=1
        db.add(PortfolioSnapshot(id=str(uuid.uuid4()),equity=equity,cash=cash,market_value=total_mv,unrealized_pl=total_pl,position_count=len(positions),snapshot_at=now_iso))
    logger.info(f"[Positions] Done — {closed} closed, {trailing} trailing | equity=${equity:.2f}")
    return {"closed":closed,"trailing":trailing,"total":len(positions),"equity":equity}
