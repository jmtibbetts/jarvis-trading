import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import (
    Base, DEFAULT_USER_ID, PaperPosition, TelegramCallback, TelegramDelivery,
    TradingSignal, UserPreference,
)
from jobs import telegram_bot
from lib.telegram_setup import detect_recent_chat, verify_bot_connection, validate_bot_token


TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdefghi"


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, json, timeout):
        self.calls.append((url, json, timeout))
        return FakeResponse(self.responses.pop(0))


class TelegramSetupTests(unittest.TestCase):
    def test_token_validation_rejects_incomplete_botfather_token(self):
        with self.assertRaisesRegex(ValueError, "format is invalid"):
            validate_bot_token("not-a-token")
        self.assertEqual(validate_bot_token(f"  {TOKEN}  "), TOKEN)

    def test_detect_uses_most_recent_chat(self):
        client = FakeClient([{"ok": True, "result": [
            {"update_id": 1, "message": {"chat": {"id": 10, "first_name": "Old"}}},
            {"update_id": 2, "message": {"chat": {"id": 20, "first_name": "Jarvis", "last_name": "User"}}},
        ]}])
        result = detect_recent_chat(TOKEN, client=client)
        self.assertEqual(result, {"chat_id": "20", "chat_name": "Jarvis User"})

    def test_connection_verifies_bot_and_sends_test_message(self):
        client = FakeClient([
            {"ok": True, "result": {"first_name": "Jarvis", "username": "jarvis_test_bot"}},
            {"ok": True, "result": {"message_id": 1}},
        ])
        result = verify_bot_connection(TOKEN, "1234", client=client)
        self.assertTrue(result["ok"])
        self.assertEqual(result["bot_username"], "jarvis_test_bot")
        self.assertEqual(client.calls[1][1]["chat_id"], "1234")

    def test_telegram_error_does_not_echo_token(self):
        client = FakeClient([{"ok": False, "description": "Unauthorized"}])
        with self.assertRaisesRegex(ValueError, "Unauthorized") as raised:
            detect_recent_chat(TOKEN, client=client)
        self.assertNotIn(TOKEN, str(raised.exception))

    def test_signal_alert_snapshots_rows_before_session_closes(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine, expire_on_commit=True)
        session = Session()
        session.add(TradingSignal(
            id="telegram-signal-1",
            asset_symbol="BTC/USD",
            direction="Short",
            confidence=82,
            composite_score=84,
            entry_price=65000,
            target_price=63000,
            stop_loss=66000,
            reasoning="Momentum and volume confirmation",
            signal_source="opportunistic",
            status="Active",
            generated_at=datetime.now(timezone.utc).isoformat(),
        ))
        session.commit()

        @contextmanager
        def closed_session():
            try:
                yield session
                session.commit()
            finally:
                session.close()

        telegram_bot._alerted_signals.clear()
        messages = []
        try:
            with patch.object(telegram_bot, "get_db", closed_session), patch.object(
                telegram_bot, "send",
                side_effect=lambda token, chat_id, text, **kwargs: messages.append(text)
            ):
                telegram_bot.alert_new_signals("token", "chat")
        finally:
            engine.dispose()

        self.assertEqual(len(messages), 1)
        self.assertIn("BTC/USD", messages[0])
        self.assertIn("Entry:", messages[0])
        self.assertIn("Stop Loss:", messages[0])
        self.assertIn("Take Profit:", messages[0])
        self.assertIn("SHORT", messages[0])

    def test_trade_setup_is_persistent_and_only_material_changes_edit_message(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)

        @contextmanager
        def session_scope():
            session = Session()
            try:
                yield session
                session.commit()
            finally:
                session.close()

        def add_signal(signal_id, entry=65000, target=63000, stop=66000):
            with session_scope() as session:
                session.add(TradingSignal(
                    id=signal_id,
                    asset_symbol="BTC/USD",
                    direction="Short",
                    timeframe="4H",
                    confidence=82,
                    entry_price=entry,
                    target_price=target,
                    stop_loss=stop,
                    reasoning="Momentum and volume confirmation",
                    status="Active",
                    generated_at=datetime.now(timezone.utc).isoformat(),
                ))

        sent = []
        edited = []
        try:
            add_signal("setup-v1")
            with patch.object(telegram_bot, "get_db", session_scope), patch.object(
                telegram_bot, "send",
                side_effect=lambda token, chat_id, text, **kwargs: sent.append(text) or {"message_id": 101},
            ), patch.object(
                telegram_bot, "edit_message",
                side_effect=lambda token, chat_id, message_id, text, **kwargs: edited.append(text) or {"message_id": 101},
            ):
                telegram_bot.alert_new_signals("token", "chat")
                add_signal("setup-v2")
                telegram_bot.alert_new_signals("token", "chat")
                add_signal("setup-v3", target=62000, stop=67000)
                telegram_bot.alert_new_signals("token", "chat")

            self.assertEqual(len(sent), 1)
            self.assertEqual(len(edited), 1)
            self.assertIn("TRADE SETUP UPDATED", edited[0])
            self.assertIn("$67,000.00", edited[0])
            with session_scope() as session:
                delivery = session.query(TelegramDelivery).one()
                self.assertEqual(delivery.signal_id, "setup-v3")
                self.assertEqual(delivery.message_id, "101")
                self.assertEqual(delivery.status, "interactive_v3")
        finally:
            engine.dispose()

    def test_signal_keyboard_is_explicitly_paper_only(self):
        keyboard = telegram_bot._signal_keyboard("signal-1", auto_enabled=True)
        buttons = [button for row in keyboard["inline_keyboard"] for button in row]
        self.assertEqual(buttons[0]["text"], "Execute Paper")
        self.assertEqual(buttons[0]["callback_data"], "sig:paper:signal-1")
        self.assertNotIn("live", str(keyboard).lower())

    def test_take_profit_refuses_a_losing_paper_position(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)

        @contextmanager
        def session_scope():
            session = Session()
            try:
                yield session
                session.commit()
            finally:
                session.close()

        with session_scope() as session:
            session.add(UserPreference(
                user_id=DEFAULT_USER_ID, paper_auto_trade_enabled=True
            ))
            session.add(PaperPosition(
                id="paper-loser", user_id=DEFAULT_USER_ID, symbol="BTC/USD",
                side="long", direction="Long", qty=1, entry_price=65000,
                current_price=64000, stop_loss=63000, target_price=68000,
                unrealized_pnl=-1000, unrealized_pct=-1.54, status="Open",
            ))

        callback = {
            "id": "callback-loss-tp",
            "data": "pos:tp:paper-loser",
            "message": {"message_id": 11, "chat": {"id": "chat"}},
        }
        answers = []
        try:
            with patch.object(telegram_bot, "get_db", session_scope), patch.object(
                telegram_bot, "_current_paper_price", return_value=64000
            ), patch.object(
                telegram_bot, "answer_callback",
                side_effect=lambda token, callback_id, text, **kwargs: answers.append(text),
            ), patch("lib.paper_engine.close_paper_position") as close_position:
                telegram_bot.handle_callback(callback, "chat", "token")
            close_position.assert_not_called()
            self.assertIn("not profitable", answers[-1])
            with session_scope() as session:
                self.assertEqual(session.query(TelegramCallback).count(), 1)
                self.assertEqual(
                    session.query(PaperPosition).filter_by(id="paper-loser").one().status,
                    "Open",
                )
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
