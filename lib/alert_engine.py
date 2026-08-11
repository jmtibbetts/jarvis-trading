"""
Generic cross-module alert engine — the single place any intelligence
source raises a notification through, instead of hand-rolling its own
Telegram push + dedup logic (as jobs/telegram_bot.py's three alert_*
functions each currently do independently).

Severities, from least to most attention-worthy: INFO, WATCH, ACTIONABLE,
HIGH_PRIORITY, CRITICAL. Only HIGH_PRIORITY/CRITICAL page Telegram — INFO/
WATCH/ACTIONABLE surface in the UI (WS broadcast + NotificationCenter) only,
so a $500 insider buy doesn't compete for phone-notification attention with
a kill-switch trip.

Scope note: this is the canonical path for new emitters (insider notable
buys, crypto liquidations, kill-switch state changes). The existing
trade-setup/threat Telegram flows in jobs/telegram_bot.py are NOT rerouted
through this — they have their own mature interactive-message-edit and
per-chat dedup logic (TelegramDelivery table) that already works correctly,
and forcing them through a new generic path here is not worth the regression
risk. Dark-pool-anomaly and data-staleness emitters are deferred until
Phase C (anomaly scoring) and a dedicated staleness monitor exist.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from app.database import Alert, get_db

logger = logging.getLogger(__name__)

SEVERITIES = ("INFO", "WATCH", "ACTIONABLE", "HIGH_PRIORITY", "CRITICAL")
TELEGRAM_SEVERITIES = {"HIGH_PRIORITY", "CRITICAL"}

_SEVERITY_EMOJI = {
    "INFO": "ℹ️", "WATCH": "👀", "ACTIONABLE": "🔶", "HIGH_PRIORITY": "🟠", "CRITICAL": "🔴",
}


def raise_alert(
    source: str, severity: str, title: str, detail: str = "",
    dedup_key: str | None = None, cooldown_minutes: int = 60,
    extra: dict | None = None, push_telegram: bool = True,
) -> dict | None:
    """Create an alert unless dedup_key was already raised within
    cooldown_minutes. Returns the created alert dict, or None if suppressed
    as a duplicate. Broadcasts over WS; pushes to Telegram only for
    HIGH_PRIORITY/CRITICAL (and only if push_telegram=True, so a caller that
    already sent its own Telegram message — e.g. the existing insider WS
    push — doesn't double-send)."""
    if severity not in SEVERITIES:
        raise ValueError(f"Unknown severity {severity!r}, must be one of {SEVERITIES}")

    with get_db() as db:
        if dedup_key:
            cutoff = (datetime.now(timezone.utc) - timedelta(minutes=max(1, cooldown_minutes))).isoformat()
            existing = (
                db.query(Alert)
                .filter(Alert.dedup_key == dedup_key, Alert.created_at >= cutoff)
                .first()
            )
            if existing:
                return None

        row = Alert(
            source=source, severity=severity, title=title, detail=detail,
            dedup_key=dedup_key, extra_json=json.dumps(extra) if extra else None,
        )
        db.add(row)
        db.flush()
        alert = _to_dict(row)

    _broadcast(alert)
    if push_telegram and severity in TELEGRAM_SEVERITIES:
        _push_telegram(alert)
    return alert


def _to_dict(row: Alert) -> dict:
    return {
        "id": row.id, "source": row.source, "severity": row.severity,
        "title": row.title, "detail": row.detail,
        "extra": json.loads(row.extra_json) if row.extra_json else None,
        "created_at": row.created_at,
    }


def _broadcast(alert: dict) -> None:
    try:
        from app.ws import manager as ws_manager
        ws_manager.broadcast_from_thread("alert", alert)
    except Exception:
        pass


def _push_telegram(alert: dict) -> None:
    try:
        from jobs.telegram_bot import get_cfg, send
        token, chat_id = get_cfg()
        if not token or not chat_id:
            return
        emoji = _SEVERITY_EMOJI.get(alert["severity"], "")
        text = f"{emoji} <b>{alert['title']}</b>"
        if alert.get("detail"):
            text += f"\n{alert['detail']}"
        result = send(token, chat_id, text)
        if result is not None:
            with get_db() as db:
                row = db.query(Alert).filter(Alert.id == alert["id"]).first()
                if row:
                    row.delivered_telegram = True
    except Exception as e:
        logger.debug(f"[AlertEngine] Telegram push failed: {e}")


def get_recent_alerts(hours: int = 24, severity: str | None = None, limit: int = 200) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(1, hours))).isoformat()
    with get_db() as db:
        query = db.query(Alert).filter(Alert.created_at >= cutoff)
        if severity:
            query = query.filter(Alert.severity == severity.upper())
        rows = query.order_by(Alert.created_at.desc()).limit(min(max(limit, 1), 500)).all()
        return [_to_dict(r) for r in rows]
