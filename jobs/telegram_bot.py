"""
Telegram bot v6.0 — added /regime /pnl /risk commands.
"""
import os, logging
from datetime import datetime, timezone
import httpx
from app.database import get_db, TradingSignal, ThreatEvent, Position, PlatformConfig

logger = logging.getLogger(__name__)

def get_cfg():
    try:
        with get_db() as db:
            cfg=db.query(PlatformConfig).filter(PlatformConfig.platform.like("telegram%"),PlatformConfig.is_active==True).first()
            if cfg: return cfg.api_key, cfg.extra_field_1
    except: pass
    return os.getenv("TELEGRAM_BOT_TOKEN",""),os.getenv("TELEGRAM_CHAT_ID","")

_last_uid=0

def send(token,chat_id,text):
    try: httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",json={"chat_id":chat_id,"text":text,"parse_mode":"HTML"},timeout=10)
    except Exception as e: logger.error(f"[Telegram] {e}")

def get_updates(token,offset):
    try:
        r=httpx.get(f"https://api.telegram.org/bot{token}/getUpdates",params={"offset":offset,"timeout":5},timeout=15)
        return r.json().get("result",[])
    except: return []

def handle(cmd,chat_id,token):
    base=cmd.strip().lower().split()[0]
    if base in ("/signals","/signal"):
        with get_db() as db:
            sigs=db.query(TradingSignal).filter(TradingSignal.status=="Active").order_by(TradingSignal.confidence.desc()).limit(10).all()
        if not sigs: send(token,chat_id,"No active signals."); return
        lines=[f"<b>🎯 Signals ({len(sigs)})</b>"]
        for s in sigs:
            sc=s.composite_score or s.confidence or 0; em="🟢" if s.direction=="Long" else "🔵"
            lines.append(f"{em} <b>{s.asset_symbol}</b> {s.direction} | Score:{sc:.0f}%\n   ${s.entry_price:.2f}→${s.target_price:.2f} SL${s.stop_loss:.2f}")
        send(token,chat_id,"\n".join(lines))
    elif base in ("/positions","/pos"):
        with get_db() as db: pos=db.query(Position).all()
        if not pos: send(token,chat_id,"No open positions."); return
        tpl=sum(p.unrealized_pl or 0 for p in pos); tmv=sum(p.market_value or 0 for p in pos)
        lines=[f"<b>📊 Positions ({len(pos)}) MV:${tmv:.0f} P&L:${tpl:+.2f}</b>"]
        for p in pos:
            em="🟢" if (p.unrealized_pl or 0)>=0 else "🔴"
            lines.append(f"{em} <b>{p.symbol}</b> x{p.qty:.4g} ${p.market_value:.0f} P&L:${p.unrealized_pl:.2f}({p.unrealized_plpc:.1f}%)")
        send(token,chat_id,"\n".join(lines))
    elif base in ("/threats","/threat"):
        with get_db() as db:
            threats=db.query(ThreatEvent).filter(ThreatEvent.status=="Active").order_by(ThreatEvent.published_at.desc()).limit(8).all()
        if not threats: send(token,chat_id,"No active threats."); return
        lines=[f"<b>⚠️ Threats ({len(threats)})</b>"]
        for t in threats:
            em={"Critical":"🔴","High":"🟠","Medium":"🟡","Low":"🟢"}.get(t.severity,"⚪")
            lines.append(f"{em} [{t.severity}] {t.country}: {t.title[:80]}")
        send(token,chat_id,"\n".join(lines))
    elif base in ("/regime","/market"):
        try:
            from lib.market_regime import get_regime; r=get_regime()
            em={"low":"🟢","medium":"🟡","medium-high":"🟠","high":"🔴"}.get(r.get("risk",""),"⚪")
            send(token,chat_id,f"<b>📈 Regime</b>\n{em} <b>{r.get('label')}</b>\nSPY ${r.get('spy_last')} RSI:{r.get('spy_rsi')} ADX:{r.get('spy_adx')}\nDrawdown:{r.get('spy_drawdown_pct')}%\n📋 {r.get('recommendation')}")
        except Exception as e: send(token,chat_id,f"Regime error: {e}")
    elif base in ("/pnl","/equity"):
        try:
            from lib.alpaca_client import get_account; a=get_account()
            send(token,chat_id,f"<b>💰 Portfolio</b>\nEquity: <b>${float(a.equity):,.2f}</b>\nCash: ${float(a.cash):,.2f}\nBuying Power: ${float(a.buying_power):,.2f}\nDay Trades: {a.daytrade_count or 0}")
        except Exception as e: send(token,chat_id,f"Error: {e}")
    elif base=="/risk":
        try:
            from lib.alpaca_client import get_account,get_positions; from lib.risk_manager import portfolio_heat
            a=get_account(); eq=float(a.equity); pos=get_positions()
            heat=portfolio_heat([{"market_value":float(p.market_value or 0),"unrealized_plpc":float(p.unrealized_plpc or 0)*100} for p in pos],eq)
            em={"ok":"🟢","warm":"🟡","hot":"🔴"}.get(heat.get("status",""),"⚪")
            send(token,chat_id,f"<b>🛡️ Risk</b>\n{em} <b>{heat.get('status','?').upper()}</b>\nHeat:{heat.get('heat',0):.1f}% Deployed:${heat.get('deployed',0):,.0f}({heat.get('deployed_pct',0):.1f}%) Positions:{len(pos)}")
        except Exception as e: send(token,chat_id,f"Error: {e}")
    elif base=="/status":
        from app.scheduler import job_status
        icons={"ok":"✅","running":"⏳","error":"❌","idle":"⏸"}
        lines=["🤖 <b>Jarvis v6.0</b>",f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",""]
        for name,info in job_status.items():
            last=info.get("last","Never")
            if last and last!="Never":
                try:
                    from datetime import datetime as dt
                    e=(datetime.now(timezone.utc)-dt.fromisoformat(last.replace("Z","+00:00"))).seconds//60
                    last=f"{e}m ago"
                except: pass
            err=f" ⚠️{info.get('error','')[:30]}" if info.get("error") else ""
            lines.append(f"{icons.get(info.get('status','idle'),'⚪')} {name}: {last}{err}")
        send(token,chat_id,"\n".join(lines))
    elif base=="/help":
        send(token,chat_id,"🤖 <b>Jarvis v6.0</b>\n/signals /positions /threats\n/regime /pnl /risk /status")

def run():
    global _last_uid
    token,chat_id=get_cfg()
    if not token: return
    updates=get_updates(token,_last_uid+1)
    for u in updates:
        _last_uid=u.get("update_id",_last_uid)
        msg=u.get("message",{}); text=msg.get("text",""); cid=str(msg.get("chat",{}).get("id",""))
        if text.startswith("/"): handle(text,cid or chat_id,token)
    return {"updates":len(updates)}
