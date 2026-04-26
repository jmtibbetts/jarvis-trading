"""
Job: Execute Signals v6.0 — Kelly sizing + regime check + correlation filter.
"""
import logging
from datetime import datetime, timezone, timedelta
from app.database import get_db, TradingSignal
from lib.alpaca_client import get_account, get_positions, submit_bracket_order, normalize_symbol

logger = logging.getLogger(__name__)

def run():
    logger.info("[Execute] Starting execution job...")
    try:
        account=get_account(); equity=float(account.equity); positions=get_positions()
    except Exception as e:
        logger.error(f"[Execute] Alpaca failed: {e}"); return {"error":str(e)}
    held={p.symbol for p in positions}
    mv_held=sum(float(p.market_value or 0) for p in positions)
    max_pos=max(8,int(equity*0.5/1000)); slots=max_pos-len(positions)
    if slots<=0: logger.info(f"[Execute] Max positions ({max_pos})"); return {"executed":0,"reason":"at_max_positions"}
    budget=max(0,equity*0.5-mv_held)
    if budget<200: logger.info(f"[Execute] Low budget ${budget:.0f}"); return {"executed":0,"reason":"insufficient_budget"}
    regime={"label":"Unknown","risk":"medium"}
    try:
        from lib.market_regime import get_regime; regime=get_regime()
        logger.info(f"[Execute] Regime: {regime['label']} | Risk: {regime['risk']}")
    except: pass
    min_conf=75 if regime.get("risk")=="high" else 55
    try:
        from lib.risk_manager import portfolio_heat
        heat=portfolio_heat([{"market_value":float(p.market_value or 0),"unrealized_plpc":float(p.unrealized_plpc or 0)*100} for p in positions],equity)
        if heat.get("status")=="hot":
            logger.warning(f"[Execute] Portfolio hot — skipping"); return {"executed":0,"reason":"portfolio_hot"}
    except: pass
    cutoff=(datetime.now(timezone.utc)-timedelta(hours=4)).isoformat()
    with get_db() as db:
        sigs=db.query(TradingSignal).filter(TradingSignal.status=="Active",TradingSignal.generated_at>=cutoff,TradingSignal.confidence>=min_conf).order_by(TradingSignal.confidence.desc()).limit(50).all()
        sig_dicts=[{"id":s.id,"asset_symbol":s.asset_symbol,"asset_class":s.asset_class or "Equity","direction":s.direction or "Long","confidence":s.composite_score or s.confidence or 65,"entry_price":s.entry_price,"target_price":s.target_price,"stop_loss":s.stop_loss} for s in sigs]
    candidates=sig_dicts
    try:
        from lib.risk_manager import filter_correlated
        candidates=filter_correlated(sig_dicts,held,max_per_sector=2)
    except: pass
    executed=0
    with get_db() as db:
        for sig in candidates:
            if executed>=slots or budget<100: break
            sym_raw=sig["asset_symbol"]; sym,crypto=normalize_symbol(sym_raw)
            if sym in held or sym_raw in held: continue
            entry=float(sig.get("entry_price") or 0); target=float(sig.get("target_price") or 0); stop=float(sig.get("stop_loss") or 0)
            if not entry or not target or not stop or stop>=entry or target<=entry: continue
            sz=None
            try:
                from lib.risk_manager import calculate_position_size
                sz=calculate_position_size(sig,equity,regime)
                if sz.rejection_reason: continue
                trade_budget=min(sz.dollar_size,budget)
            except:
                conf=float(sig.get("confidence",65)); trade_budget=min(500+(conf-55)/45*1000,budget); trade_budget=max(100,min(1500,trade_budget))
            qty=round(trade_budget/entry,8) if crypto else max(1,int(trade_budget/entry))
            try:
                submit_bracket_order(symbol=sym,qty=qty,entry_price=entry,take_profit=target,stop_loss=stop)
                rec=db.query(TradingSignal).filter(TradingSignal.id==sig["id"]).first()
                if rec: rec.status="Executed"; rec.updated_date=datetime.now(timezone.utc).isoformat()
                held.add(sym); budget-=qty*entry; executed+=1
                logger.info(f"[Execute] ✓ {sym} x{qty} @ ${entry:.2f} TP=${target:.2f} SL=${stop:.2f}")
            except Exception as e:
                rec=db.query(TradingSignal).filter(TradingSignal.id==sig["id"]).first()
                if rec: rec.status="Rejected"; rec.updated_date=datetime.now(timezone.utc).isoformat()
                logger.error(f"[Execute] ✗ {sym}: {e}")
    logger.info(f"[Execute] Done — {executed} orders | budget=${budget:.0f}")
    return {"executed":executed,"regime":regime.get("label"),"budget_remaining":round(budget,2)}
