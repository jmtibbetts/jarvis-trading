"""
Telegram bot v6.1 — proactive alerts + full command set.
New: alert_new_signals(), alert_position_update() for push notifications.
"""
import os, logging
from datetime import datetime, timezone, timedelta
import httpx
from app.database import get_db, TradingSignal, ThreatEvent, Position, PlatformConfig, PortfolioSnapshot

logger = logging.getLogger(__name__)

_last_uid = 0
_alerted_signals = set()      # track which signal IDs we already pushed
_alerted_threats  = set()      # track which threat IDs we already pushed

# ── Config helpers ──────────────────────────────────────────────────────────────
def get_cfg():
    try:
        with get_db() as db:
            cfg = db.query(PlatformConfig).filter(
                PlatformConfig.platform.like("telegram%"),
                PlatformConfig.is_active == True
            ).first()
            if cfg:
                return cfg.api_key, cfg.extra_field_1
    except:
        pass
    return os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", "")

# ── Send helpers ────────────────────────────────────────────────────────────────
def send(token, chat_id, text, parse_mode="HTML"):
    if not token or not chat_id:
        return
    try:
        httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode},
            timeout=10
        )
    except Exception as e:
        logger.error(f"[Telegram] Send error: {e}")

def get_updates(token, offset):
    try:
        r = httpx.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            params={"offset": offset, "timeout": 5},
            timeout=15
        )
        return r.json().get("result", [])
    except:
        return []

# ── Proactive Alerts ────────────────────────────────────────────────────────────
def alert_new_signals(token, chat_id):
    """Push alerts for fresh high-confidence signals not yet alerted."""
    global _alerted_signals
    if not token or not chat_id:
        return
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=35)).isoformat()
    with get_db() as db:
        new_sigs = db.query(TradingSignal).filter(
            TradingSignal.status == "Active",
            TradingSignal.generated_at >= cutoff,
            TradingSignal.confidence >= 70
        ).order_by(TradingSignal.confidence.desc()).limit(8).all()

    to_alert = [s for s in new_sigs if s.id not in _alerted_signals]
    if not to_alert:
        return

    lines = [f"🎯 <b>New Signals ({len(to_alert)})</b>"]
    for s in to_alert:
        sc = s.composite_score or s.confidence or 0
        em = "🟢" if s.direction == "Long" else "🔵"
        rr_str = ""
        if s.entry_price and s.target_price and s.stop_loss and s.entry_price > s.stop_loss:
            rr = (s.target_price - s.entry_price) / (s.entry_price - s.stop_loss)
            rr_str = f" R:R {rr:.1f}"
        src = " 📰" if getattr(s, "signal_source", "watchlist") == "opportunistic" else ""
        earn = " 📅" if getattr(s, "earnings_risk", False) else ""
        lines.append(
            f"{em} <b>{s.asset_symbol}</b> {s.direction} | Score:{sc:.0f}%{rr_str}{src}{earn}\n"
            f"   ${s.entry_price:.2f} → ${s.target_price:.2f} | SL ${s.stop_loss:.2f}\n"
            f"   {(s.reasoning or '')[:80]}..."
        )
        _alerted_signals.add(s.id)

    # Prune old IDs to avoid unbounded growth
    if len(_alerted_signals) > 500:
        _alerted_signals = set(list(_alerted_signals)[-300:])

    send(token, chat_id, "\n".join(lines))


def alert_critical_threats(token, chat_id):
    """Push alerts for new Critical/High threats not yet alerted."""
    global _alerted_threats
    if not token or not chat_id:
        return
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
    with get_db() as db:
        threats = db.query(ThreatEvent).filter(
            ThreatEvent.status == "Active",
            ThreatEvent.severity.in_(["Critical", "High"]),
            ThreatEvent.published_at >= cutoff
        ).order_by(ThreatEvent.published_at.desc()).limit(5).all()

    to_alert = [t for t in threats if t.id not in _alerted_threats]
    if not to_alert:
        return

    lines = [f"⚠️ <b>Threat Alert ({len(to_alert)})</b>"]
    for t in to_alert:
        em = "🔴" if t.severity == "Critical" else "🟠"
        lines.append(f"{em} [{t.severity}] {t.country}: {t.title[:100]}")
        if t.source_url:
            lines.append(f"   🔗 {t.source_url[:80]}")
        _alerted_threats.add(t.id)

    if len(_alerted_threats) > 500:
        _alerted_threats = set(list(_alerted_threats)[-300:])

    send(token, chat_id, "\n".join(lines))


def alert_position_updates(token, chat_id):
    """Alert when positions hit notable thresholds."""
    if not token or not chat_id:
        return
    try:
        with get_db() as db:
            positions = db.query(Position).all()
        notable = []
        for p in positions:
            plpc = p.unrealized_plpc or 0
            if plpc >= 10:
                notable.append(f"🚀 <b>{p.symbol}</b> +{plpc:.1f}% (${p.unrealized_pl:.2f}) — consider taking profit")
            elif plpc <= -7:
                notable.append(f"🛑 <b>{p.symbol}</b> {plpc:.1f}% (${p.unrealized_pl:.2f}) — approaching stop-loss")
        if notable:
            send(token, chat_id, "📊 <b>Position Alert</b>\n" + "\n".join(notable))
    except Exception as e:
        logger.debug(f"[Telegram] Position alert error: {e}")


# ── Command handlers ─────────────────────────────────────────────────────────────
def handle(cmd, chat_id, token):
    base = cmd.strip().lower().split()[0]

    if base in ("/signals", "/signal"):
        with get_db() as db:
            sigs = db.query(TradingSignal).filter(TradingSignal.status == "Active").order_by(
                TradingSignal.confidence.desc()
            ).limit(10).all()
        if not sigs:
            send(token, chat_id, "No active signals."); return
        lines = [f"<b>🎯 Signals ({len(sigs)})</b>"]
        for s in sigs:
            sc = s.composite_score or s.confidence or 0
            em = "🟢" if s.direction == "Long" else "🔵"
            lines.append(f"{em} <b>{s.asset_symbol}</b> {s.direction} | Score:{sc:.0f}%\n   ${s.entry_price:.2f}→${s.target_price:.2f} SL${s.stop_loss:.2f}")
        send(token, chat_id, "\n".join(lines))

    elif base in ("/positions", "/pos"):
        with get_db() as db:
            pos = db.query(Position).all()
        if not pos:
            send(token, chat_id, "No open positions."); return
        tpl = sum(p.unrealized_pl or 0 for p in pos)
        tmv = sum(p.market_value or 0 for p in pos)
        lines = [f"<b>📊 Positions ({len(pos)}) MV:${tmv:.0f} P&L:${tpl:+.2f}</b>"]
        for p in pos:
            em = "🟢" if (p.unrealized_pl or 0) >= 0 else "🔴"
            lines.append(f"{em} <b>{p.symbol}</b> x{p.qty:.4g} ${p.market_value:.0f} P&L:${p.unrealized_pl:.2f}({p.unrealized_plpc:.1f}%)")
        send(token, chat_id, "\n".join(lines))

    elif base in ("/threats", "/threat"):
        with get_db() as db:
            threats = db.query(ThreatEvent).filter(ThreatEvent.status == "Active").order_by(
                ThreatEvent.published_at.desc()
            ).limit(8).all()
        if not threats:
            send(token, chat_id, "No active threats."); return
        lines = [f"<b>⚠️ Threats ({len(threats)})</b>"]
        for t in threats:
            em = {"Critical": "🔴", "High": "🟠", "Medium": "🟡", "Low": "🟢"}.get(t.severity, "⚪")
            lines.append(f"{em} [{t.severity}] {t.country}: {t.title[:80]}")
        send(token, chat_id, "\n".join(lines))

    elif base in ("/regime", "/market"):
        try:
            from lib.market_regime import get_regime
            r = get_regime()
            em = {"low": "🟢", "medium": "🟡", "medium-high": "🟠", "high": "🔴"}.get(r.get("risk", ""), "⚪")
            send(token, chat_id,
                 f"<b>📈 Regime</b>\n{em} <b>{r.get('label')}</b>\n"
                 f"SPY ${r.get('spy_last')} RSI:{r.get('spy_rsi')} ADX:{r.get('spy_adx')}\n"
                 f"Drawdown:{r.get('spy_drawdown_pct')}%\n📋 {r.get('recommendation')}")
        except Exception as e:
            send(token, chat_id, f"Regime error: {e}")

    elif base in ("/pnl", "/equity"):
        try:
            from lib.alpaca_client import get_account
            a = get_account()
            send(token, chat_id,
                 f"<b>💰 Portfolio</b>\nEquity: <b>${float(a.equity):,.2f}</b>\n"
                 f"Cash: ${float(a.cash):,.2f}\nBuying Power: ${float(a.buying_power):,.2f}\n"
                 f"Day Trades: {a.daytrade_count or 0}")
        except Exception as e:
            send(token, chat_id, f"Error: {e}")

    elif base == "/risk":
        try:
            from lib.alpaca_client import get_account, get_positions
            from lib.risk_manager import portfolio_heat
            a = get_account(); eq = float(a.equity); pos = get_positions()
            heat = portfolio_heat(
                [{"market_value": float(p.market_value or 0), "unrealized_plpc": float(p.unrealized_plpc or 0) * 100}
                 for p in pos], eq
            )
            em = {"ok": "🟢", "warm": "🟡", "hot": "🔴"}.get(heat.get("status", ""), "⚪")
            send(token, chat_id,
                 f"<b>🛡️ Risk</b>\n{em} <b>{heat.get('status', '?').upper()}</b>\n"
                 f"Heat:{heat.get('heat', 0):.1f}% Deployed:${heat.get('deployed', 0):,.0f}"
                 f"({heat.get('deployed_pct', 0):.1f}%) Positions:{len(pos)}")
        except Exception as e:
            send(token, chat_id, f"Error: {e}")

    elif base == "/perf":
        try:
            with get_db() as db:
                closed = db.query(TradingSignal).filter(
                    TradingSignal.status.in_(["Closed", "Executed"])
                ).order_by(TradingSignal.updated_date.desc()).limit(20).all()
            if not closed:
                send(token, chat_id, "No closed trades yet."); return
            wins = losses = total_rr = 0
            for s in closed:
                if s.entry_price and s.target_price and s.stop_loss and s.entry_price > s.stop_loss:
                    rr = (s.target_price - s.entry_price) / (s.entry_price - s.stop_loss)
                    total_rr += rr
                    wins += 1 if rr >= 1 else 0
                    losses += 1 if rr < 1 else 0
            wr = f"{wins/(wins+losses)*100:.0f}%" if (wins + losses) > 0 else "N/A"
            avg_rr = f"{total_rr/len(closed):.2f}" if closed else "N/A"
            send(token, chat_id,
                 f"<b>📈 Performance ({len(closed)} trades)</b>\n"
                 f"Win Rate: {wr} | Avg R:R: {avg_rr}\n"
                 f"Wins: {wins} | Losses: {losses}")
        except Exception as e:
            send(token, chat_id, f"Error: {e}")

    elif base == "/status":
        from app.scheduler import job_status
        icons = {"ok": "✅", "running": "⏳", "error": "❌", "idle": "⏸"}
        lines = ["🤖 <b>Jarvis v6.1</b>", f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}", ""]
        for name, info in job_status.items():
            last = info.get("last", "Never")
            if last and last != "Never":
                try:
                    from datetime import datetime as dt
                    e = (datetime.now(timezone.utc) - dt.fromisoformat(last.replace("Z", "+00:00"))).seconds // 60
                    last = f"{e}m ago"
                except:
                    pass
            err = f" ⚠️{info.get('error', '')[:30]}" if info.get("error") else ""
            lines.append(f"{icons.get(info.get('status', 'idle'), '⚪')} {name}: {last}{err}")
        send(token, chat_id, "\n".join(lines))

    elif base == "/help":
        send(token, chat_id,
             "🤖 <b>Jarvis v6.1</b>\n\n"
             "/signals — active trade signals\n"
             "/positions — open positions with P&L\n"
             "/threats — active geopolitical threats\n"
             "/regime — market regime analysis\n"
             "/pnl — portfolio equity & cash\n"
             "/risk — portfolio heat & risk status\n"
             "/perf — trade performance summary\n"
             "/status — job scheduler status\n"
             "\n💡 Proactive alerts enabled for new signals ≥70% and Critical/High threats.")


# ── Main run() ───────────────────────────────────────────────────────────────────
def run():
    global _last_uid
    token, chat_id = get_cfg()
    if not token:
        return {"skipped": True, "reason": "no_token"}

    # Handle incoming commands
    updates = get_updates(token, _last_uid + 1)
    for u in updates:
        _last_uid = u.get("update_id", _last_uid)
        msg = u.get("message", {})
        text = msg.get("text", "")
        cid = str(msg.get("chat", {}).get("id", ""))
        if text.startswith("/"):
            handle(text, cid or chat_id, token)

    # Proactive alerts (every run = every 1min)
    alert_new_signals(token, chat_id)
    alert_critical_threats(token, chat_id)
    alert_position_updates(token, chat_id)

    return {"updates": len(updates)}
