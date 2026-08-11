"""
Telegram bot v6.1 — proactive alerts + full command set.
New: alert_new_signals(), alert_position_update() for push notifications.
"""
import json, os, logging
from html import escape
from types import SimpleNamespace
from datetime import datetime, timezone, timedelta
import httpx
from app.database import (
    DEFAULT_USER_ID, TelegramDelivery, TradingSignal, ThreatEvent, Position,
    PlatformConfig, PortfolioSnapshot, TelegramCallback, UserPreference,
    PaperPosition, MarketAsset, get_db, now_iso,
)

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
    except Exception as e:
        logger.warning(f"[Telegram] Config lookup failed, falling back to env vars: {e}")
    return os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", "")

# ── Send helpers ────────────────────────────────────────────────────────────────
def send(token, chat_id, text, parse_mode="HTML", reply_markup=None):
    if not token or not chat_id:
        return
    try:
        payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        response = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json=payload,
            timeout=10
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            logger.error(f"[Telegram] Send rejected: {payload.get('description', 'unknown error')}")
            return None
        return payload.get("result") or {}
    except Exception as e:
        logger.error(f"[Telegram] Send error: {type(e).__name__}")
        return None


def edit_message(token, chat_id, message_id, text, parse_mode="HTML", reply_markup=None):
    if not token or not chat_id or not message_id:
        return None
    try:
        request = {
            "chat_id": chat_id,
            "message_id": int(message_id),
            "text": text,
            "parse_mode": parse_mode,
        }
        if reply_markup is not None:
            request["reply_markup"] = reply_markup
        response = httpx.post(
            f"https://api.telegram.org/bot{token}/editMessageText",
            json=request,
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            logger.error(f"[Telegram] Edit rejected: {payload.get('description', 'unknown error')}")
            return None
        return payload.get("result") or {}
    except Exception as e:
        logger.error(f"[Telegram] Edit error: {type(e).__name__}")
        return None


def edit_keyboard(token, chat_id, message_id, reply_markup):
    if not token or not chat_id or not message_id:
        return None
    try:
        response = httpx.post(
            f"https://api.telegram.org/bot{token}/editMessageReplyMarkup",
            json={
                "chat_id": chat_id,
                "message_id": int(message_id),
                "reply_markup": reply_markup,
            },
            timeout=10,
        )
        response.raise_for_status()
        return response.json().get("result")
    except Exception as exc:
        logger.error("[Telegram] Keyboard edit error: %s", type(exc).__name__)
        return None


def answer_callback(token, callback_id, text, show_alert=False):
    if not token or not callback_id:
        return
    try:
        httpx.post(
            f"https://api.telegram.org/bot{token}/answerCallbackQuery",
            json={
                "callback_query_id": callback_id,
                "text": str(text)[:190],
                "show_alert": bool(show_alert),
            },
            timeout=10,
        )
    except Exception as exc:
        logger.debug("[Telegram] Callback acknowledgement failed: %s", type(exc).__name__)


def _auto_trade_enabled():
    with get_db() as db:
        pref = db.query(UserPreference).filter(
            UserPreference.user_id == DEFAULT_USER_ID
        ).first()
        return bool(pref.paper_auto_trade_enabled if pref is not None else True)


def _signal_keyboard(signal_id, auto_enabled=None, live_eligible=False):
    """live_eligible: show the Approve LIVE button only for signals that can
    actually reach live execution (PendingApproval, not paper-gated). The
    button routes through the same approve_signal logic as the dashboard —
    kill switch and paper-mode guards included — so eligibility shown here is
    a convenience, not the enforcement."""
    rows = [[
        {"text": "Execute Paper", "callback_data": f"sig:paper:{signal_id}"},
        {"text": "Deny", "callback_data": f"sig:deny:{signal_id}"},
    ]]
    if live_eligible:
        rows.insert(0, [{"text": "🔴 Approve LIVE Trade", "callback_data": f"sig:live:{signal_id}"}])
    rows.append([
        {"text": "Auto On", "callback_data": "auto:on"},
        {"text": "Auto Off", "callback_data": "auto:off"},
    ])
    return {"inline_keyboard": rows}


def _position_keyboard(position_id, auto_enabled=None):
    return {"inline_keyboard": [
        [
            {"text": "Take Profit", "callback_data": f"pos:tp:{position_id}"},
            {"text": "Close", "callback_data": f"pos:close:{position_id}"},
        ],
        [
            {"text": "Auto On", "callback_data": "auto:on"},
            {"text": "Auto Off", "callback_data": "auto:off"},
        ],
    ]}


def _setup_key(signal):
    side = "short" if "short" in str(signal.direction or "").lower() else "long"
    return f"{str(signal.asset_symbol or '').upper().strip()}:{side}"


def _setup_state(signal):
    return {
        "direction": str(signal.direction or ""),
        "timeframe": str(getattr(signal, "timeframe", "") or ""),
        "entry": float(signal.entry_price or 0),
        "stop": float(signal.stop_loss or 0),
        "target": float(signal.target_price or 0),
    }


def _relative_change(old, new):
    old, new = float(old or 0), float(new or 0)
    if old <= 0 or new <= 0:
        return 1.0 if old != new else 0.0
    return abs(new - old) / old


def _material_setup_change(previous, current):
    if not previous:
        return False
    try:
        old = json.loads(previous) if isinstance(previous, str) else previous
    except (TypeError, ValueError):
        return False
    if old.get("direction") != current.get("direction") or old.get("timeframe") != current.get("timeframe"):
        return True
    entry_threshold = float(os.getenv("TELEGRAM_ENTRY_CHANGE_PCT", "0.25")) / 100
    level_threshold = float(os.getenv("TELEGRAM_LEVEL_CHANGE_PCT", "0.50")) / 100
    return (
        _relative_change(old.get("entry"), current.get("entry")) >= entry_threshold
        or _relative_change(old.get("stop"), current.get("stop")) >= level_threshold
        or _relative_change(old.get("target"), current.get("target")) >= level_threshold
    )


def _price(value):
    value = float(value or 0)
    if value < 0.01:
        return f"${value:.8f}"
    if value < 1:
        return f"${value:.6f}"
    return f"${value:,.2f}"


def _price_levels(entry, stop, target):
    """Format the three levels with SHARED adaptive precision so they are
    always visually distinguishable.

    Fixes a real user-reported bug: fixed 2-decimal formatting collapsed a
    tight setup on a ~$1 asset into "short from $1.00 to $1.00 stop loss
    $1.00" — entry, target, and stop all rendered identically because the
    level spacing was smaller than a cent. Precision is chosen from the
    smallest pairwise distance between the levels (2 significant digits of
    that distance), so the numbers that define the trade always differ on
    screen. Capped at 8 decimals."""
    import math
    values = [float(entry or 0), float(stop or 0), float(target or 0)]
    deltas = [abs(a - b) for i, a in enumerate(values) for b in values[i + 1:] if abs(a - b) > 0]
    decimals = 2
    if deltas:
        smallest = min(deltas)
        if smallest < 0.01:
            decimals = min(8, max(2, -int(math.floor(math.log10(smallest))) + 1))
    elif values and max(values) < 1:
        decimals = 6
    return tuple(f"${v:,.{decimals}f}" for v in values)


# Horizon labels mirror lib/signal_scorer._setup_type's timeframe mapping —
# the user-facing answer to "is this a scalp or a swing?"
_HORIZON_BY_TF = {
    "1m": "SCALP", "3m": "SCALP", "5m": "SCALP",
    "15m": "INTRADAY", "30m": "INTRADAY", "1H": "INTRADAY",
    "2H": "SWING", "4H": "SWING",
    "1D": "POSITION",
}


def _horizon(signal) -> str:
    setup = str(getattr(signal, "setup_type", "") or "").strip()
    if setup:
        return setup.upper()
    return _HORIZON_BY_TF.get(str(getattr(signal, "timeframe", "") or ""), "POSITION")


def _asset_name(symbol: str) -> str | None:
    try:
        with get_db() as db:
            row = db.query(MarketAsset.name).filter(MarketAsset.symbol == symbol).first()
            return row[0] if row and row[0] and row[0] != symbol else None
    except Exception:
        return None


def _format_trade_setup(signal, updated=False):
    is_short = "short" in str(signal.direction or "").lower()
    side = "SHORT" if is_short else "LONG"
    entry = float(signal.entry_price or 0)
    stop = float(signal.stop_loss or 0)
    target = float(signal.target_price or 0)
    risk = (stop - entry) if is_short else (entry - stop)
    reward = (entry - target) if is_short else (target - entry)
    rr = reward / risk if risk > 0 else 0
    risk_pct = abs(entry - stop) / entry * 100 if entry else 0
    reward_pct = abs(target - entry) / entry * 100 if entry else 0
    title = "TRADE SETUP UPDATED" if updated else "NEW TRADE SETUP"
    mode = "Paper only" if is_short or "lever" in str(signal.direction or "").lower() else "Pending approval"
    symbol = escape(str(signal.asset_symbol or ""))
    name = _asset_name(str(signal.asset_symbol or ""))
    name_line = f" — {escape(name)}" if name else ""
    horizon = escape(_horizon(signal))
    timeframe = escape(str(getattr(signal, "timeframe", "") or "N/A"))
    source = escape(str(getattr(signal, "signal_source", "") or "watchlist"))
    score = getattr(signal, "composite_score", None)
    score_txt = f"{float(score):.0f}" if score is not None else "—"
    reasoning = escape(str(signal.reasoning or "")[:240])
    entry_s, stop_s, target_s = _price_levels(entry, stop, target)
    earnings_line = "\n⚠ Earnings within days — elevated gap risk" if getattr(signal, "earnings_risk", False) else ""
    return (
        f"<b>{title}</b>\n\n"
        f"<b>{symbol}</b>{name_line}\n"
        f"<b>{side}</b> | <b>{horizon}</b> ({timeframe} chart)\n\n"
        f"Entry: <b>{entry_s}</b>\n"
        f"Stop Loss: <b>{stop_s}</b> ({risk_pct:.2f}% risk)\n"
        f"Take Profit: <b>{target_s}</b> ({reward_pct:.2f}% target)\n"
        f"R:R <b>{rr:.2f}</b> | Score <b>{score_txt}</b> | Conf {float(signal.confidence or 0):.0f}%\n"
        f"Source: {source} | {mode}{earnings_line}\n\n"
        f"{reasoning}"
    )

def get_updates(token, offset):
    try:
        r = httpx.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            params={"offset": offset, "timeout": 5},
            timeout=15
        )
        r.raise_for_status()
        return r.json().get("result", [])
    except Exception as e:
        logger.warning(f"[Telegram] getUpdates failed (polling may be stalled): {e}")
        return []

# ── Proactive Alerts ────────────────────────────────────────────────────────────
def alert_new_signals(token, chat_id):
    """Send one persistent Telegram message per symbol/side trade setup."""
    if not token or not chat_id:
        return
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=35)).isoformat()
    with get_db() as db:
        new_sigs = [
            SimpleNamespace(
                id=s.id,
                user_id=getattr(s, "user_id", None) or DEFAULT_USER_ID,
                asset_symbol=s.asset_symbol,
                direction=s.direction,
                confidence=s.confidence or 0,
                composite_score=s.composite_score,
                timeframe=s.timeframe or "",
                entry_price=s.entry_price or 0,
                target_price=s.target_price or 0,
                stop_loss=s.stop_loss or 0,
                reasoning=s.reasoning or "",
                signal_source=s.signal_source or "watchlist",
                earnings_risk=bool(s.earnings_risk),
                setup_type=getattr(s, "setup_type", None),
                status=s.status,
                paper_mode=bool(getattr(s, "paper_mode", False)),
            )
            for s in db.query(TradingSignal).filter(
                TradingSignal.status.in_(["Active", "PendingApproval"]),
                TradingSignal.generated_at >= cutoff,
                TradingSignal.confidence >= 70,
            ).order_by(TradingSignal.generated_at.desc(), TradingSignal.confidence.desc()).limit(
                max(1, int(os.getenv("TELEGRAM_SETUP_LIMIT", "8")))
            ).all()
        ]

        # Keep only the newest signal for each symbol and side.
        setups = {}
        for signal in new_sigs:
            setups.setdefault(_setup_key(signal), signal)

        sent = updated = unchanged = 0
        auto_enabled = _auto_trade_enabled()
        for key, signal in setups.items():
            state = _setup_state(signal)
            state_json = json.dumps(state, sort_keys=True, separators=(",", ":"))
            live_eligible = (
                signal.status == "PendingApproval" and not signal.paper_mode
                and "short" not in str(signal.direction or "").lower()
            )
            keyboard = _signal_keyboard(signal.id, auto_enabled, live_eligible=live_eligible)
            delivery = db.query(TelegramDelivery).filter(
                TelegramDelivery.user_id == signal.user_id,
                TelegramDelivery.chat_id == str(chat_id),
                TelegramDelivery.setup_key == key,
            ).order_by(TelegramDelivery.delivered_at.desc()).first()

            if delivery:
                # Legacy deliveries did not store a setup snapshot. Treat the current
                # setup as their baseline instead of sending it again after migration.
                if not delivery.setup_state:
                    delivery.setup_state = state_json
                    delivery.signal_id = signal.id
                    delivery.updated_at = now_iso()
                material_change = _material_setup_change(delivery.setup_state, state)
                needs_controls = delivery.status != "interactive_v3"
                if not material_change and not needs_controls:
                    delivery.signal_id = signal.id
                    delivery.updated_at = now_iso()
                    unchanged += 1
                    continue

                text = _format_trade_setup(signal, updated=material_change)
                result = edit_message(
                    token, chat_id, delivery.message_id, text,
                    reply_markup=keyboard,
                )
                if result is None:
                    result = send(token, chat_id, text, reply_markup=keyboard)
                if result is None:
                    continue
                delivery.signal_id = signal.id
                delivery.setup_state = state_json
                delivery.message_id = str(result.get("message_id") or delivery.message_id or "")
                delivery.updated_at = now_iso()
                delivery.status = "interactive_v3"
                updated += 1
                continue

            text = _format_trade_setup(signal, updated=False)
            result = send(token, chat_id, text, reply_markup=keyboard)
            if result is None:
                continue
            db.add(TelegramDelivery(
                user_id=signal.user_id,
                chat_id=str(chat_id),
                signal_id=signal.id,
                setup_key=key,
                setup_state=state_json,
                message_id=str(result.get("message_id") or ""),
                delivered_at=now_iso(),
                updated_at=now_iso(),
                status="interactive_v3",
            ))
            sent += 1

    if sent or updated:
        logger.info(
            "[Telegram] Trade setups: %s new, %s updated, %s unchanged",
            sent, updated, unchanged,
        )


def alert_critical_threats(token, chat_id):
    """Push alerts for new Critical/High threats not yet alerted."""
    global _alerted_threats
    if not token or not chat_id:
        return
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
    with get_db() as db:
        threats = [
            {"id": t.id, "severity": t.severity, "country": t.country or "", "title": t.title or "", "source_url": t.source_url or ""}
            for t in db.query(ThreatEvent).filter(
                ThreatEvent.status == "Active",
                ThreatEvent.severity.in_(["Critical", "High"]),
                ThreatEvent.published_at >= cutoff
            ).order_by(ThreatEvent.published_at.desc()).limit(5).all()
        ]

    to_alert = [t for t in threats if t["id"] not in _alerted_threats]
    if not to_alert:
        return

    lines = [f"⚠️ <b>Threat Alert ({len(to_alert)})</b>"]
    for t in to_alert:
        em = "🔴" if t["severity"] == "Critical" else "🟠"
        lines.append(f"{em} [{t['severity']}] {t['country']}: {t['title'][:100]}")
        if t["source_url"]:
            lines.append(f"   🔗 {t['source_url'][:80]}")
        _alerted_threats.add(t["id"])

    if len(_alerted_threats) > 500:
        _alerted_threats = set(list(_alerted_threats)[-300:])

    send(token, chat_id, "\n".join(lines))


def alert_position_updates(token, chat_id):
    """Send persistent alerts only when a position crosses or materially moves beyond a threshold."""
    if not token or not chat_id:
        return
    try:
        with get_db() as db:
            positions = [
                {"symbol": p.symbol, "unrealized_plpc": p.unrealized_plpc or 0, "unrealized_pl": p.unrealized_pl or 0}
                for p in db.query(Position).all()
            ]
            pct_change_threshold = float(os.getenv("TELEGRAM_POSITION_CHANGE_PCT", "2.0"))
            dollar_change_threshold = float(os.getenv("TELEGRAM_POSITION_CHANGE_USD", "25"))
            min_profit_dollars = float(os.getenv("TELEGRAM_POSITION_MIN_PROFIT_USD", "10"))
            min_loss_dollars = float(os.getenv("TELEGRAM_POSITION_MIN_LOSS_USD", "10"))

            for position in positions:
                symbol = str(position["symbol"] or "").upper()
                plpc = float(position["unrealized_plpc"] or 0)
                pnl = float(position["unrealized_pl"] or 0)

                # Percentage and dollar P/L must agree. Never label a negative-dollar
                # position as a profit because of stale or inconsistent percentage data.
                if (plpc > 0 and pnl <= 0) or (plpc < 0 and pnl >= 0):
                    logger.warning(
                        "[Telegram] Suppressed inconsistent P/L alert for %s: pct=%+.2f dollars=%+.2f",
                        symbol, plpc, pnl,
                    )
                    continue

                if plpc >= 10 and pnl >= min_profit_dollars:
                    kind = "profit"
                    title = "PROFIT MILESTONE"
                    action = "Position is profitable. Review the stop loss and take-profit level; this is not an automatic close instruction."
                elif plpc <= -7 and pnl <= -min_loss_dollars:
                    kind = "loss"
                    title = "LOSS-RISK ALERT"
                    action = "Position is losing. Review the stop-loss or exit plan; this is not a take-profit recommendation."
                else:
                    continue

                key = f"position:{symbol}"
                state = {"kind": kind, "plpc": plpc, "pnl": pnl}
                state_json = json.dumps(state, sort_keys=True, separators=(",", ":"))
                delivery = db.query(TelegramDelivery).filter(
                    TelegramDelivery.user_id == DEFAULT_USER_ID,
                    TelegramDelivery.chat_id == str(chat_id),
                    TelegramDelivery.setup_key == key,
                ).order_by(TelegramDelivery.delivered_at.desc()).first()

                changed = True
                if delivery and delivery.setup_state:
                    try:
                        old = json.loads(delivery.setup_state)
                        changed = (
                            old.get("kind") != kind
                            or abs(plpc - float(old.get("plpc") or 0)) >= pct_change_threshold
                            or abs(pnl - float(old.get("pnl") or 0)) >= dollar_change_threshold
                        )
                    except (TypeError, ValueError):
                        changed = True
                if delivery and not changed:
                    continue

                text = (
                    f"<b>{title}</b>\n\n"
                    f"<b>{escape(symbol)}</b>\n"
                    f"Unrealized P/L: <b>${pnl:+,.2f} ({plpc:+.1f}%)</b>\n\n"
                    f"{action}"
                )
                result = edit_message(token, chat_id, delivery.message_id, text) if delivery else None
                if result is None:
                    result = send(token, chat_id, text)
                if result is None:
                    continue

                was_existing = delivery is not None
                if not delivery:
                    delivery = TelegramDelivery(
                        user_id=DEFAULT_USER_ID,
                        chat_id=str(chat_id),
                        signal_id=key,
                        setup_key=key,
                        delivered_at=now_iso(),
                    )
                    db.add(delivery)
                delivery.setup_state = state_json
                delivery.message_id = str(result.get("message_id") or delivery.message_id or "")
                delivery.updated_at = now_iso()
                delivery.status = "updated" if was_existing else "sent"
    except Exception as e:
        logger.debug(f"[Telegram] Position alert error: {e}")


def _claim_callback(callback, configured_chat_id, entity_id, action, require_delivery=False):
    callback_id = str(callback.get("id") or "")
    message = callback.get("message") or {}
    chat_id = str((message.get("chat") or {}).get("id") or "")
    if not callback_id or not chat_id:
        return False, chat_id, "Invalid callback"
    # Fail closed: if no chat id has been configured yet, no one is authorized to
    # trigger a state-changing action (open/close a paper position, deny a signal,
    # toggle auto-trading) — previously an unset TELEGRAM_CHAT_ID let ANY chat that
    # messaged the bot control it during the window before setup completes.
    if not configured_chat_id:
        logger.warning("[Telegram] Ignored callback — no chat id configured yet (complete Telegram setup)")
        return False, chat_id, "Telegram setup is not complete yet — link a chat in Settings first"
    if chat_id != str(configured_chat_id):
        logger.warning("[Telegram] Ignored callback from unauthorized chat %s", chat_id)
        return False, chat_id, "This chat is not authorized"

    with get_db() as db:
        if db.query(TelegramCallback).filter(
            TelegramCallback.callback_id == callback_id
        ).first():
            return False, chat_id, "This action was already processed"
        if require_delivery:
            delivered = db.query(TelegramDelivery).filter(
                TelegramDelivery.user_id == DEFAULT_USER_ID,
                TelegramDelivery.chat_id == chat_id,
                TelegramDelivery.signal_id == entity_id,
            ).first()
            if not delivered:
                return False, chat_id, "Signal delivery could not be verified"
        db.add(TelegramCallback(
            callback_id=callback_id,
            user_id=DEFAULT_USER_ID,
            chat_id=chat_id,
            signal_id=entity_id or "settings",
            action=action,
            processed_at=now_iso(),
        ))
    return True, chat_id, ""


def _current_paper_price(position):
    symbol = str(position.symbol or "")
    variants = [
        symbol,
        symbol.replace("/", ""),
        symbol.replace("/USD", ""),
        f"{symbol}/USD" if "/" not in symbol else symbol,
    ]
    with get_db() as db:
        for variant in dict.fromkeys(variants):
            asset = db.query(MarketAsset).filter(MarketAsset.symbol == variant).first()
            if asset and float(asset.price or 0) > 0:
                return float(asset.price)
    return float(position.current_price or position.entry_price or 0)


def _format_paper_position(position):
    pnl = float(position.unrealized_pnl or 0)
    pnl_pct = float(position.unrealized_pct or 0)
    side = str(position.side or position.direction or "").upper()
    return (
        "<b>PAPER POSITION</b>\n\n"
        f"<b>{escape(str(position.symbol or ''))}</b> | <b>{escape(side)}</b>\n"
        f"Entry: <b>{_price(position.entry_price)}</b>\n"
        f"Current: <b>{_price(position.current_price)}</b>\n"
        f"Stop Loss: <b>{_price(position.stop_loss)}</b>\n"
        f"Take Profit: <b>{_price(position.target_price)}</b>\n"
        f"P/L: <b>${pnl:+,.2f} ({pnl_pct:+.2f}%)</b>"
    )


def send_paper_positions(token, chat_id):
    with get_db() as db:
        positions = [
            SimpleNamespace(
                id=p.id, symbol=p.symbol, side=p.side, direction=p.direction,
                entry_price=p.entry_price, current_price=p.current_price,
                stop_loss=p.stop_loss, target_price=p.target_price,
                unrealized_pnl=p.unrealized_pnl, unrealized_pct=p.unrealized_pct,
            )
            for p in db.query(PaperPosition).filter(
                PaperPosition.user_id == DEFAULT_USER_ID,
                PaperPosition.status == "Open",
            ).order_by(PaperPosition.opened_at.desc()).limit(20).all()
        ]
    if not positions:
        send(token, chat_id, "No open paper positions.")
        return
    auto_enabled = _auto_trade_enabled()
    for position in positions:
        send(
            token, chat_id, _format_paper_position(position),
            reply_markup=_position_keyboard(position.id, auto_enabled),
        )


def _set_auto_trade(enabled):
    with get_db() as db:
        pref = db.query(UserPreference).filter(
            UserPreference.user_id == DEFAULT_USER_ID
        ).first()
        if pref is None:
            pref = UserPreference(user_id=DEFAULT_USER_ID)
            db.add(pref)
            db.flush()
        pref.paper_auto_trade_enabled = bool(enabled)
        pref.updated_at = now_iso()
        return bool(pref.paper_auto_trade_enabled)


def handle_callback(callback, configured_chat_id, token):
    callback_id = str(callback.get("id") or "")
    data = str(callback.get("data") or "")
    message = callback.get("message") or {}
    message_id = message.get("message_id")

    try:
        if data in {"auto:on", "auto:off"}:
            requested_state = data == "auto:on"
            ok, chat_id, error = _claim_callback(
                callback, configured_chat_id, "settings",
                "auto_on" if requested_state else "auto_off",
            )
            if not ok:
                answer_callback(token, callback_id, error, show_alert=True)
                return
            enabled = _set_auto_trade(requested_state)
            state = "ON" if enabled else "OFF"
            answer_callback(
                token, callback_id,
                f"Automatic paper entries and TA management: {state}. Hard stops and targets remain active.",
                show_alert=True,
            )
            return

        parts = data.split(":", 2)
        if len(parts) != 3 or parts[0] not in {"sig", "pos"}:
            answer_callback(token, callback_id, "Unknown action", show_alert=True)
            return
        kind, action, entity_id = parts
        allowed = {"paper", "deny", "live"} if kind == "sig" else {"tp", "close"}
        if action not in allowed:
            answer_callback(token, callback_id, "Unknown action", show_alert=True)
            return

        ok, chat_id, error = _claim_callback(
            callback, configured_chat_id, entity_id, f"{kind}_{action}",
            require_delivery=(kind == "sig"),
        )
        if not ok:
            answer_callback(token, callback_id, error, show_alert=True)
            return

        if kind == "sig" and action == "live":
            # Reuses the dashboard's approve_signal end-to-end: kill-switch
            # check, PendingApproval-only, paper-mode guard, bracket-order
            # submission, and stuck-status handling all live there. The
            # Telegram button is another door to the same room, not new logic.
            try:
                from app.routes import approve_signal
                result = approve_signal(entity_id)
                filled = result.get("qty") if isinstance(result, dict) else None
                answer_callback(
                    token, callback_id,
                    f"LIVE order submitted{f' (qty {filled})' if filled else ''}.",
                    show_alert=True,
                )
                edit_keyboard(token, chat_id, message_id, {"inline_keyboard": []})
            except Exception as approve_err:
                detail = getattr(approve_err, "detail", None) or str(approve_err)
                answer_callback(token, callback_id, f"Live approval failed: {detail}"[:190], show_alert=True)
            return

        if kind == "sig" and action == "deny":
            with get_db() as db:
                signal = db.query(TradingSignal).filter(TradingSignal.id == entity_id).first()
                if not signal:
                    answer_callback(token, callback_id, "Signal no longer exists", show_alert=True)
                    return
                signal.status = "Rejected"
                signal.updated_date = now_iso()
            edit_keyboard(token, chat_id, message_id, {"inline_keyboard": []})
            answer_callback(token, callback_id, "Signal denied", show_alert=True)
            return

        if kind == "sig" and action == "paper":
            with get_db() as db:
                signal = db.query(TradingSignal).filter(TradingSignal.id == entity_id).first()
                if not signal or signal.status not in {"Active", "PendingApproval"}:
                    answer_callback(token, callback_id, "Signal is no longer eligible", show_alert=True)
                    return
                original_status = signal.status
                signal.status = "PaperOpening"
                signal.updated_date = now_iso()
                signal_data = {
                    "id": signal.id,
                    "asset_symbol": signal.asset_symbol,
                    "asset_class": signal.asset_class,
                    "direction": signal.direction,
                    "paper_direction": signal.paper_direction or signal.direction,
                    "entry_price": signal.entry_price,
                    "target_price": signal.target_price,
                    "stop_loss": signal.stop_loss,
                }
            try:
                from lib.paper_engine import open_paper_position
                result = open_paper_position(
                    signal_data, current_price=float(signal_data["entry_price"] or 0)
                )
            except Exception as open_err:
                # Guarantee the status revert below runs even on a hard failure
                # (network hiccup, bad price, DB lock) — otherwise the signal is
                # left at "PaperOpening" forever and can never be approved/denied
                # again since eligibility requires Active/PendingApproval.
                logger.exception("[Telegram] open_paper_position failed for signal %s", entity_id)
                result = {"ok": False, "error": f"{type(open_err).__name__}: {open_err}"}
            with get_db() as db:
                signal = db.query(TradingSignal).filter(TradingSignal.id == entity_id).first()
                if signal:
                    signal.status = "PaperExecuted" if result.get("ok") else original_status
                    signal.paper_mode = True
                    signal.updated_date = now_iso()
            if not result.get("ok"):
                answer_callback(token, callback_id, result.get("error", "Paper trade failed"), show_alert=True)
                return
            edit_keyboard(token, chat_id, message_id, {"inline_keyboard": []})
            position = result.get("position") or {}
            answer_callback(token, callback_id, "Paper trade opened", show_alert=True)
            send(
                token, chat_id,
                f"<b>PAPER TRADE OPENED</b>\n\n"
                f"<b>{escape(str(position.get('symbol') or ''))}</b> | "
                f"{escape(str(position.get('direction') or ''))}\n"
                f"Entry: <b>{_price(position.get('entry_price'))}</b>\n"
                f"Stop Loss: <b>{_price(position.get('stop'))}</b>\n"
                f"Take Profit: <b>{_price(position.get('target'))}</b>",
                reply_markup=_position_keyboard(position.get("id")),
            )
            return

        with get_db() as db:
            position = db.query(PaperPosition).filter(
                PaperPosition.id == entity_id,
                PaperPosition.user_id == DEFAULT_USER_ID,
                PaperPosition.status == "Open",
            ).first()
            if not position:
                answer_callback(token, callback_id, "Position is already closed", show_alert=True)
                return
            snapshot = SimpleNamespace(
                id=position.id, symbol=position.symbol, side=position.side,
                entry_price=position.entry_price, current_price=position.current_price,
                qty=position.qty, unrealized_pnl=position.unrealized_pnl,
            )
        close_price = _current_paper_price(snapshot)
        side = 1 if str(snapshot.side).lower() == "long" else -1
        pnl = (close_price - float(snapshot.entry_price or 0)) * float(snapshot.qty or 0) * side
        if action == "tp" and pnl <= 0:
            answer_callback(
                token, callback_id,
                f"{snapshot.symbol} is not profitable (${pnl:+.2f}). Use Close if you still want to exit.",
                show_alert=True,
            )
            return
        from lib.paper_engine import close_paper_position
        result = close_paper_position(
            entity_id, close_price,
            reason="telegram_take_profit" if action == "tp" else "telegram_close",
        )
        if not result.get("ok"):
            answer_callback(token, callback_id, result.get("error", "Close failed"), show_alert=True)
            return
        edit_keyboard(token, chat_id, message_id, {"inline_keyboard": []})
        answer_callback(
            token, callback_id,
            f"Paper position closed: {result['symbol']} P/L ${result['pnl']:+.2f}",
            show_alert=True,
        )
    except Exception as exc:
        logger.exception("[Telegram] Callback action failed")
        answer_callback(token, callback_id, f"Action failed: {type(exc).__name__}", show_alert=True)


# ── Command handlers ─────────────────────────────────────────────────────────────
def handle(cmd, chat_id, token):
    base = cmd.strip().lower().split()[0]

    if base == "/chatid":
        send(token, chat_id, f"<b>Your Jarvis Chat ID</b>\n<code>{chat_id}</code>")

    elif base in ("/signals", "/signal"):
        with get_db() as db:
            sigs = [
                {"asset_symbol": s.asset_symbol, "direction": s.direction,
                 "confidence": s.confidence or 0, "composite_score": s.composite_score,
                 "entry_price": s.entry_price or 0, "target_price": s.target_price or 0, "stop_loss": s.stop_loss or 0}
                for s in db.query(TradingSignal).filter(TradingSignal.status == "Active").order_by(
                    TradingSignal.confidence.desc()
                ).limit(10).all()
            ]
        if not sigs:
            send(token, chat_id, "No active signals."); return
        lines = [f"<b>🎯 Signals ({len(sigs)})</b>"]
        for s in sigs:
            sc = s["composite_score"] or s["confidence"]
            em = "🟢" if s["direction"] == "Long" else "🔵"
            lines.append(f"{em} <b>{s['asset_symbol']}</b> {s['direction']} | Score:{sc:.0f}%\n   ${s['entry_price']:.2f}→${s['target_price']:.2f} SL${s['stop_loss']:.2f}")
        send(token, chat_id, "\n".join(lines))

    elif base in ("/paper", "/paperpositions"):
        send_paper_positions(token, chat_id)

    elif base in ("/positions", "/pos"):
        with get_db() as db:
            pos = [
                {"symbol": p.symbol, "qty": p.qty or 0, "market_value": p.market_value or 0,
                 "unrealized_pl": p.unrealized_pl or 0, "unrealized_plpc": p.unrealized_plpc or 0}
                for p in db.query(Position).all()
            ]
        if not pos:
            send(token, chat_id, "No open positions."); return
        tpl = sum(p["unrealized_pl"] for p in pos)
        tmv = sum(p["market_value"] for p in pos)
        lines = [f"<b>📊 Positions ({len(pos)}) MV:${tmv:.0f} P&L:${tpl:+.2f}</b>"]
        for p in pos:
            em = "🟢" if p["unrealized_pl"] >= 0 else "🔴"
            lines.append(f"{em} <b>{p['symbol']}</b> x{p['qty']:.4g} ${p['market_value']:.0f} P&L:${p['unrealized_pl']:.2f}({p['unrealized_plpc']:.1f}%)")
        send(token, chat_id, "\n".join(lines))

    elif base in ("/threats", "/threat"):
        with get_db() as db:
            threats = [
                {"severity": t.severity, "country": t.country or "", "title": t.title or ""}
                for t in db.query(ThreatEvent).filter(ThreatEvent.status == "Active").order_by(
                    ThreatEvent.published_at.desc()
                ).limit(8).all()
            ]
        if not threats:
            send(token, chat_id, "No active threats."); return
        lines = [f"<b>⚠️ Threats ({len(threats)})</b>"]
        for t in threats:
            em = {"Critical": "🔴", "High": "🟠", "Medium": "🟡", "Low": "🟢"}.get(t["severity"], "⚪")
            lines.append(f"{em} [{t['severity']}] {t['country']}: {t['title'][:80]}")
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
                closed = [
                    {"entry_price": s.entry_price, "target_price": s.target_price, "stop_loss": s.stop_loss}
                    for s in db.query(TradingSignal).filter(
                        TradingSignal.status.in_(["Closed", "Executed"])
                    ).order_by(TradingSignal.updated_date.desc()).limit(20).all()
                ]
            if not closed:
                send(token, chat_id, "No closed trades yet."); return
            wins = losses = total_rr = 0
            for s in closed:
                ep = s["entry_price"]; tp = s["target_price"]; sl = s["stop_loss"]
                if ep and tp and sl and ep > sl:
                    rr = (tp - ep) / (ep - sl)
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

    elif base in ("/pause", "/stop"):
        from lib.kill_switch import set_live_trading_enabled
        state = set_live_trading_enabled(False, reason="Paused via Telegram")
        send(token, chat_id,
             "⏸ <b>Live trading PAUSED</b>\n"
             "No new live orders will be submitted. Existing positions keep their "
             "hard stop-loss/take-profit protection. Send /resume to re-enable.")

    elif base in ("/resume", "/start_trading"):
        from lib.kill_switch import set_live_trading_enabled
        set_live_trading_enabled(True)
        send(token, chat_id, "▶️ <b>Live trading RESUMED</b>\nNew live orders can be submitted again.")

    elif base == "/status":
        from app.scheduler import job_status
        from lib.kill_switch import get_kill_switch_state
        kill_state = get_kill_switch_state()
        icons = {"ok": "✅", "running": "⏳", "error": "❌", "idle": "⏸"}
        trading_line = (
            "🟢 Live trading: ENABLED" if kill_state["live_trading_enabled"]
            else f"⏸ Live trading: PAUSED ({kill_state.get('paused_reason') or 'manual'})"
        )
        lines = ["🤖 <b>Jarvis v6.1</b>", f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}", trading_line, ""]
        for name, info in job_status.items():
            last = info.get("last", "Never")
            if last and last != "Never":
                try:
                    from datetime import datetime as dt
                    # .seconds drops the .days component of a timedelta, so a job
                    # dead for 2 days was showing as e.g. "180m ago" — total_seconds()
                    # captures the full elapsed span.
                    total_min = int((datetime.now(timezone.utc) - dt.fromisoformat(last.replace("Z", "+00:00"))).total_seconds() // 60)
                    if total_min < 60:
                        last = f"{total_min}m ago"
                    elif total_min < 1440:
                        last = f"{total_min // 60}h ago"
                    else:
                        last = f"{total_min // 1440}d ago"
                except Exception:
                    pass
            err = f" ⚠️{info.get('error', '')[:30]}" if info.get("error") else ""
            lines.append(f"{icons.get(info.get('status', 'idle'), '⚪')} {name}: {last}{err}")
        send(token, chat_id, "\n".join(lines))

    elif base == "/help":
        send(token, chat_id,
             "🤖 <b>Jarvis v6.1</b>\n\n"
             "/signals — active trade signals\n"
             "/paper - paper positions with Take Profit and Close buttons\n"
             "/positions — open positions with P&L\n"
             "/threats — active geopolitical threats\n"
             "/regime — market regime analysis\n"
             "/pnl — portfolio equity & cash\n"
             "/risk — portfolio heat & risk status\n"
             "/perf — trade performance summary\n"
             "/status — job scheduler + live trading status\n"
             "/pause — stop all new live orders (existing stops/targets stay active)\n"
             "/resume — re-enable live trading\n"
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
        callback = u.get("callback_query")
        if callback:
            handle_callback(callback, chat_id, token)
            continue
        msg = u.get("message", {})
        text = msg.get("text", "")
        cid = str(msg.get("chat", {}).get("id", ""))
        if text.startswith("/"):
            base_cmd = text.strip().lower().split()[0] if text.strip() else ""
            if chat_id:
                if cid and cid != str(chat_id):
                    logger.warning(f"[Telegram] Ignored command from unauthorized chat {cid}")
                    continue
            elif base_cmd != "/chatid":
                # No chat id configured yet: only allow the read-only /chatid
                # bootstrap command (needed to discover the id to configure) —
                # every other command is state-changing or exposes account data
                # and must wait until setup links a specific chat.
                logger.warning(f"[Telegram] Ignored '{base_cmd}' from {cid} — no chat id configured yet")
                continue
            handle(text, cid or chat_id, token)

    # Proactive alerts (every run = every 1min)
    alert_new_signals(token, chat_id)
    alert_critical_threats(token, chat_id)
    alert_position_updates(token, chat_id)

    return {"updates": len(updates)}
