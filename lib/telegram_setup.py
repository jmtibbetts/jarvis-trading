"""Telegram Bot API helpers used by the local Settings setup flow."""
import re

import httpx


TOKEN_PATTERN = re.compile(r"^\d+:[A-Za-z0-9_-]{20,}$")


def validate_bot_token(token: str) -> str:
    value = str(token or "").strip()
    if not TOKEN_PATTERN.fullmatch(value):
        raise ValueError("The bot token format is invalid. Copy the complete token from @BotFather.")
    return value


def _call(token: str, method: str, payload: dict | None = None, client=httpx) -> dict:
    token = validate_bot_token(token)
    try:
        response = client.post(
            f"https://api.telegram.org/bot{token}/{method}",
            json=payload or {},
            timeout=12,
        )
        data = response.json()
    except Exception:
        raise RuntimeError("Could not reach Telegram. Check this computer's internet connection.")
    if not data.get("ok"):
        description = str(data.get("description") or "Telegram rejected the request")
        raise ValueError(description[:240])
    return data.get("result") or {}


def detect_recent_chat(token: str, client=httpx) -> dict:
    updates = _call(token, "getUpdates", {"limit": 20, "timeout": 0}, client=client)
    for update in reversed(updates if isinstance(updates, list) else []):
        message = update.get("message") or update.get("channel_post") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is not None:
            name = chat.get("title") or " ".join(
                part for part in (chat.get("first_name"), chat.get("last_name")) if part
            )
            return {"chat_id": str(chat_id), "chat_name": name or chat.get("username") or "Telegram chat"}
    raise ValueError("No recent chat found. Open your bot in Telegram, tap Start, send /start, then try again.")


def verify_bot_connection(token: str, chat_id: str, client=httpx) -> dict:
    chat_id = str(chat_id or "").strip()
    if not chat_id:
        raise ValueError("A Chat ID is required.")
    bot = _call(token, "getMe", client=client)
    _call(token, "sendMessage", {
        "chat_id": chat_id,
        "text": "Jarvis Trading AI is connected. Signal alerts are enabled for this chat.",
    }, client=client)
    return {
        "ok": True,
        "bot_name": bot.get("first_name") or "Telegram bot",
        "bot_username": bot.get("username") or "",
        "chat_id": chat_id,
    }
