"""Account isolation and Telegram authorization primitives."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime, timedelta, timezone

from app.database import (
    TelegramCallback, TelegramDelivery, TelegramLinkToken, UserPreference,
    UserTelegramLink, now_iso,
)
from lib.trading_preferences import timeframe_allowed

SECRET_KEY_RE = re.compile(r"(?:secret|token|password|private[_-]?key|api[_-]?key)", re.I)


def _utc(value: datetime | None = None) -> datetime:
    value = value or datetime.now(timezone.utc)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def create_link_token(db, user_id: str, ttl_minutes: int = 10,
                      now: datetime | None = None) -> str:
    created = _utc(now)
    raw = secrets.token_urlsafe(24)
    db.add(TelegramLinkToken(
        user_id=user_id,
        token_hash=_hash_token(raw),
        expires_at=(created + timedelta(minutes=max(1, ttl_minutes))).isoformat(),
        created_at=created.isoformat(),
    ))
    return raw


def consume_link_token(db, raw_token: str, chat_id: str,
                       now: datetime | None = None) -> str:
    current = _utc(now)
    digest = _hash_token(raw_token)
    candidates = db.query(TelegramLinkToken).filter(TelegramLinkToken.used_at.is_(None)).all()
    row = next((item for item in candidates if hmac.compare_digest(item.token_hash, digest)), None)
    if not row:
        raise ValueError("invalid or already-used link token")
    expiry = datetime.fromisoformat(row.expires_at.replace("Z", "+00:00"))
    if _utc(expiry) < current:
        raise ValueError("link token expired")
    existing_chat = db.query(UserTelegramLink).filter(UserTelegramLink.chat_id == str(chat_id)).first()
    if existing_chat and existing_chat.user_id != row.user_id:
        raise PermissionError("Telegram chat is already linked to another user")
    link = db.query(UserTelegramLink).filter(UserTelegramLink.user_id == row.user_id).first()
    if not link:
        link = UserTelegramLink(user_id=row.user_id, chat_id=str(chat_id))
        db.add(link)
    else:
        link.chat_id = str(chat_id)
        link.is_active = True
        link.linked_at = current.isoformat()
    row.used_at = current.isoformat()
    preference = db.query(UserPreference).filter(UserPreference.user_id == row.user_id).first()
    if preference:
        preference.telegram_enabled = True
        preference.updated_at = current.isoformat()
    return row.user_id


def record_delivery(db, user_id: str, chat_id: str, signal_id: str) -> TelegramDelivery:
    existing = db.query(TelegramDelivery).filter(
        TelegramDelivery.user_id == user_id,
        TelegramDelivery.signal_id == signal_id,
    ).first()
    if existing:
        return existing
    row = TelegramDelivery(user_id=user_id, chat_id=str(chat_id), signal_id=signal_id)
    db.add(row)
    return row


def authorize_callback(db, callback_id: str, chat_id: str, signal_id: str,
                       action: str) -> str:
    action = str(action or "").lower()
    if action not in {"approve", "deny"}:
        raise ValueError("unsupported callback action")
    if db.query(TelegramCallback).filter(TelegramCallback.callback_id == callback_id).first():
        raise ValueError("duplicate callback")
    link = db.query(UserTelegramLink).filter(
        UserTelegramLink.chat_id == str(chat_id), UserTelegramLink.is_active == True,
    ).first()
    if not link:
        raise PermissionError("Telegram chat is not linked")
    delivery = db.query(TelegramDelivery).filter(
        TelegramDelivery.user_id == link.user_id,
        TelegramDelivery.chat_id == str(chat_id),
        TelegramDelivery.signal_id == signal_id,
    ).first()
    if not delivery:
        raise PermissionError("signal was not delivered to this user")
    db.add(TelegramCallback(
        callback_id=callback_id, user_id=link.user_id, chat_id=str(chat_id),
        signal_id=signal_id, action=action, processed_at=now_iso(),
    ))
    return link.user_id


def signal_matches_preference(signal: dict, preference) -> bool:
    if float(signal.get("calibrated_confidence") or signal.get("confidence") or 0) < float(preference.min_confidence or 0):
        return False
    if not timeframe_allowed(signal.get("timeframe"), preference.trade_mode):
        return False
    try:
        classes = set(json.loads(preference.asset_classes or "[]"))
        directions = set(json.loads(preference.directions or "[]"))
    except (TypeError, ValueError):
        classes, directions = set(), set()
    if classes and signal.get("asset_class") not in classes:
        return False
    side = "short" if "short" in str(signal.get("direction") or "").lower() else "long"
    return not directions or side in {str(value).lower() for value in directions}


def redact_secrets(value):
    if isinstance(value, dict):
        return {
            key: ("[REDACTED]" if SECRET_KEY_RE.search(str(key)) and item else redact_secrets(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    return value


def requires_paper_only(direction: str | None) -> bool:
    value = str(direction or "").lower()
    return "short" in value or "leverag" in value or re.search(r"(?:^|_)\d+x(?:_|$)", value) is not None


def signals_for_user(db, user_id: str):
    """Ownership-scoped signal query used by account and messaging surfaces."""
    from app.database import TradingSignal
    return db.query(TradingSignal).filter(TradingSignal.user_id == user_id)
