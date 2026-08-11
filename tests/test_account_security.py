import json
import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.database import (
    Base, TelegramDelivery, TradingSignal, UserPreference, UserTelegramLink,
    backfill_legacy_user_ids,
)
from lib.account_security import (
    authorize_callback, consume_link_token, create_link_token, redact_secrets,
    requires_paper_only, signal_matches_preference, signals_for_user,
)
from lib.signal_identity import signal_identity


class AccountSecurityTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_link_tokens_expire_and_are_single_use(self):
        now = datetime(2026, 8, 9, tzinfo=timezone.utc)
        expired = create_link_token(self.db, "user-a", ttl_minutes=5, now=now)
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "expired"):
            consume_link_token(self.db, expired, "chat-a", now=now + timedelta(minutes=6))

        raw = create_link_token(self.db, "user-a", now=now)
        self.db.commit()
        self.assertEqual(consume_link_token(self.db, raw, "chat-a", now=now), "user-a")
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "already-used"):
            consume_link_token(self.db, raw, "chat-a", now=now)

    def test_callback_requires_delivery_and_rejects_replay(self):
        self.db.add(UserTelegramLink(user_id="user-a", chat_id="chat-a", is_active=True))
        self.db.add(TelegramDelivery(user_id="user-a", chat_id="chat-a", signal_id="sig-a"))
        self.db.commit()
        self.assertEqual(
            authorize_callback(self.db, "callback-1", "chat-a", "sig-a", "approve"),
            "user-a",
        )
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "duplicate"):
            authorize_callback(self.db, "callback-1", "chat-a", "sig-a", "approve")
        with self.assertRaises(PermissionError):
            authorize_callback(self.db, "callback-2", "chat-b", "sig-a", "approve")

    def test_signal_queries_are_scoped_by_user(self):
        self.db.add_all([
            TradingSignal(id="sig-a", user_id="user-a", asset_symbol="BTC/USD"),
            TradingSignal(id="sig-b", user_id="user-b", asset_symbol="ETH/USD"),
        ])
        self.db.commit()
        self.assertEqual([row.id for row in signals_for_user(self.db, "user-a").all()], ["sig-a"])

    def test_preferences_route_by_horizon_assets_direction_and_confidence(self):
        preference = UserPreference(
            user_id="user-a", trade_mode="scalp", min_confidence=70,
            asset_classes=json.dumps(["Crypto"]), directions=json.dumps(["short"]),
        )
        matching = {
            "timeframe": "5m", "asset_class": "Crypto", "direction": "Short",
            "calibrated_confidence": 78,
        }
        self.assertTrue(signal_matches_preference(matching, preference))
        self.assertFalse(signal_matches_preference({**matching, "timeframe": "4H"}, preference))
        self.assertFalse(signal_matches_preference({**matching, "direction": "Long"}, preference))

    def test_short_and_leveraged_directions_are_paper_only(self):
        self.assertTrue(requires_paper_only("Short"))
        self.assertTrue(requires_paper_only("Long_5x"))
        self.assertTrue(requires_paper_only("Long_Leveraged"))
        self.assertFalse(requires_paper_only("Long"))

    def test_secret_redaction_is_recursive(self):
        value = {"api_key": "abc", "nested": {"password": "xyz", "label": "keep"}}
        redacted = redact_secrets(value)
        self.assertEqual(redacted["api_key"], "[REDACTED]")
        self.assertEqual(redacted["nested"]["password"], "[REDACTED]")
        self.assertEqual(redacted["nested"]["label"], "keep")

    def test_same_side_replaces_identity_but_opposing_side_is_retained(self):
        self.assertEqual(signal_identity("BTC/USD", "Long"), signal_identity("btc/usd", "Long_5x"))
        self.assertNotEqual(signal_identity("BTC/USD", "Long"), signal_identity("BTC/USD", "Short"))

    def test_legacy_rows_are_assigned_to_local_user(self):
        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE legacy_signals (id TEXT PRIMARY KEY, user_id TEXT)"))
            conn.execute(text("INSERT INTO legacy_signals VALUES ('old', NULL), ('owned', 'user-b')"))
            backfill_legacy_user_ids(conn, tables=["legacy_signals"])
            rows = dict(conn.execute(text("SELECT id, user_id FROM legacy_signals")).fetchall())
        engine.dispose()
        self.assertEqual(rows, {"old": "local", "owned": "user-b"})


if __name__ == "__main__":
    unittest.main()

