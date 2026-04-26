"""
Telegram bot — polling-based (no webhook required).
Responds to /signals, /positions, /threats, /status commands.
"""
import os, logging
from datetime import datetime, timezone
import httpx
from app.database import get_db, TradingSignal, ThreatEvent, Position, PlatformConfig

logger = logging.getLogger(__name__)

def get_telegram_config():
    try:
        with get_db() as db:
            cfg = db.query(PlatformConfig).filter(
                PlatformConfig.platform.like('telegram%'),
                PlatformConfig.is_active == True
            ).first()
            if cfg:
                return cfg.api_key, cfg.extra_field_1
    except:
        pass
    return os.getenv('TELEGRAM_BOT_TOKEN', ''), os.getenv('TELEGRAM_CHAT_ID', '')

_last_update_id = 0

def send_message(token: str, chat_id: str, text: str):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        httpx.post(url, json={'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML'}, timeout=10)
    except Exception as e:
        logger.error(f"[Telegram] Send failed: {e}")

def get_updates(token: str, offset: int):
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    try:
        r = httpx.get(url, params={'offset': offset, 'timeout': 5}, timeout=15)
        return r.json().get('result', [])
    except:
        return []

def handle_command(cmd: str, chat_id: str, token: str):
    cmd = cmd.strip().lower().split()[0]
    
    if cmd in ('/signals', '/signal'):
        with get_db() as db:
            signals = db.query(TradingSignal).filter(
                TradingSignal.status == 'Active'
            ).order_by(TradingSignal.confidence.desc()).limit(10).all()
        if not signals:
            send_message(token, chat_id, "No active signals right now.")
            return
        lines = ["<b>🎯 Active Signals</b>"]
        for s in signals:
            emoji = "🟢" if s.direction == 'Long' else "🔵"
            lines.append(
                f"{emoji} <b>{s.asset_symbol}</b> {s.direction} | "
                f"Conf: {s.confidence}% | "
                f"Entry: ${s.entry_price:.2f} → Target: ${s.target_price:.2f} | "
                f"Stop: ${s.stop_loss:.2f}"
            )
        send_message(token, chat_id, '\n'.join(lines))
    
    elif cmd in ('/positions', '/pos'):
        with get_db() as db:
            positions = db.query(Position).all()
        if not positions:
            send_message(token, chat_id, "No open positions.")
            return
        lines = ["<b>📊 Open Positions</b>"]
        for p in positions:
            pl_emoji = "🟢" if p.unrealized_pl >= 0 else "🔴"
            lines.append(
                f"{pl_emoji} <b>{p.symbol}</b> x{p.qty:.4g} | "
                f"${p.market_value:.2f} | "
                f"P&L: ${p.unrealized_pl:.2f} ({p.unrealized_plpc:.1f}%)"
            )
        send_message(token, chat_id, '\n'.join(lines))
    
    elif cmd in ('/threats', '/threat'):
        with get_db() as db:
            threats = db.query(ThreatEvent).filter(
                ThreatEvent.status == 'Active'
            ).order_by(ThreatEvent.published_at.desc()).limit(8).all()
        if not threats:
            send_message(token, chat_id, "No active threats.")
            return
        lines = ["<b>⚠️ Active Threats</b>"]
        for t in threats:
            sev_emoji = {"Critical":"🔴","High":"🟠","Medium":"🟡","Low":"🟢"}.get(t.severity, "⚪")
            lines.append(f"{sev_emoji} [{t.severity}] {t.country}: {t.title[:80]}")
        send_message(token, chat_id, '\n'.join(lines))
    
    elif cmd == '/status':
        send_message(token, chat_id, 
            "🤖 <b>Jarvis Trading AI — Python Edition</b>\n"
            f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
            "Use /signals /positions /threats for data."
        )
    
    elif cmd == '/help':
        send_message(token, chat_id,
            "🤖 <b>Jarvis Commands</b>\n"
            "/signals — Active trading signals\n"
            "/positions — Open Alpaca positions\n"
            "/threats — Active geopolitical threats\n"
            "/status — System status\n"
        )

def run():
    global _last_update_id
    
    token, chat_id = get_telegram_config()
    if not token:
        logger.debug("[Telegram] No token configured, skipping")
        return
    
    updates = get_updates(token, _last_update_id + 1)
    for update in updates:
        _last_update_id = update.get('update_id', _last_update_id)
        msg = update.get('message', {})
        text = msg.get('text', '')
        cid  = str(msg.get('chat', {}).get('id', ''))
        
        if text.startswith('/'):
            logger.info(f"[Telegram] Command from {cid}: {text}")
            handle_command(text, cid or chat_id, token)
    
    return {'updates': len(updates)}
