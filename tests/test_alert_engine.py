import unittest
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Alert, Base
from lib import alert_engine


class AlertEngineTests(unittest.TestCase):
    def _session_factory(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine, expire_on_commit=False)
        session = Session()

        @contextmanager
        def closed_session():
            try:
                yield session
                session.commit()
            finally:
                pass

        return session, closed_session

    def test_unknown_severity_raises(self):
        with self.assertRaises(ValueError):
            alert_engine.raise_alert("test", "MADE_UP", "title")

    def test_creates_alert_and_broadcasts(self):
        session, closed_session = self._session_factory()
        with patch.object(alert_engine, "get_db", closed_session), \
             patch.object(alert_engine, "_broadcast") as mock_broadcast:
            result = alert_engine.raise_alert("insider", "ACTIONABLE", "Notable buy", detail="AAPL $1M")
        self.assertIsNotNone(result)
        self.assertEqual(result["severity"], "ACTIONABLE")
        mock_broadcast.assert_called_once()
        self.assertEqual(session.query(Alert).count(), 1)

    def test_dedup_within_cooldown_suppresses_second_alert(self):
        session, closed_session = self._session_factory()
        with patch.object(alert_engine, "get_db", closed_session), \
             patch.object(alert_engine, "_broadcast"):
            first = alert_engine.raise_alert(
                "crypto_derivatives", "WATCH", "Liquidation", dedup_key="liq-BTC-1", cooldown_minutes=60,
            )
            second = alert_engine.raise_alert(
                "crypto_derivatives", "WATCH", "Liquidation (dup)", dedup_key="liq-BTC-1", cooldown_minutes=60,
            )
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(session.query(Alert).count(), 1)

    def test_different_dedup_keys_both_created(self):
        session, closed_session = self._session_factory()
        with patch.object(alert_engine, "get_db", closed_session), \
             patch.object(alert_engine, "_broadcast"):
            alert_engine.raise_alert("crypto_derivatives", "WATCH", "A", dedup_key="key-a", cooldown_minutes=60)
            alert_engine.raise_alert("crypto_derivatives", "WATCH", "B", dedup_key="key-b", cooldown_minutes=60)
        self.assertEqual(session.query(Alert).count(), 2)

    def test_low_severity_does_not_push_telegram(self):
        session, closed_session = self._session_factory()
        with patch.object(alert_engine, "get_db", closed_session), \
             patch.object(alert_engine, "_broadcast"), \
             patch.object(alert_engine, "_push_telegram") as mock_push:
            alert_engine.raise_alert("insider", "ACTIONABLE", "Notable buy")
        mock_push.assert_not_called()

    def test_high_priority_does_not_push_telegram_by_default(self):
        """Telegram carries TRADE SIGNALS ONLY (user instruction) — alert
        pushes are gated behind TELEGRAM_ALERTS_ENABLED, default off."""
        session, closed_session = self._session_factory()
        with patch.object(alert_engine, "get_db", closed_session), \
             patch.object(alert_engine, "_broadcast"), \
             patch.object(alert_engine, "_push_telegram") as mock_push:
            alert_engine.raise_alert("crypto_derivatives", "HIGH_PRIORITY", "Big liquidation")
        mock_push.assert_not_called()

    def test_high_priority_pushes_telegram_when_reenabled(self):
        import os
        session, closed_session = self._session_factory()
        with patch.object(alert_engine, "get_db", closed_session), \
             patch.object(alert_engine, "_broadcast"), \
             patch.object(alert_engine, "_push_telegram") as mock_push, \
             patch.dict(os.environ, {"TELEGRAM_ALERTS_ENABLED": "true"}):
            alert_engine.raise_alert("crypto_derivatives", "HIGH_PRIORITY", "Big liquidation")
        mock_push.assert_called_once()

    def test_push_telegram_false_skips_even_at_critical(self):
        session, closed_session = self._session_factory()
        with patch.object(alert_engine, "get_db", closed_session), \
             patch.object(alert_engine, "_broadcast"), \
             patch.object(alert_engine, "_push_telegram") as mock_push:
            alert_engine.raise_alert("kill_switch", "CRITICAL", "Paused", push_telegram=False)
        mock_push.assert_not_called()

    def test_push_telegram_no_config_is_noop(self):
        """Real behavior when Telegram isn't set up: must not raise."""
        with patch("jobs.telegram_bot.get_cfg", return_value=("", "")):
            alert_engine._push_telegram({"id": "x", "severity": "CRITICAL", "title": "t", "detail": ""})

    def test_push_telegram_marks_delivered_on_success(self):
        session, closed_session = self._session_factory()
        row = Alert(id="alert-1", source="test", severity="CRITICAL", title="t")
        session.add(row)
        session.commit()
        with patch.object(alert_engine, "get_db", closed_session), \
             patch("jobs.telegram_bot.get_cfg", return_value=("tok", "chat")), \
             patch("jobs.telegram_bot.send", return_value={"message_id": 42}):
            alert_engine._push_telegram({"id": "alert-1", "severity": "CRITICAL", "title": "t", "detail": ""})
        session.refresh(row)
        self.assertTrue(row.delivered_telegram)

    def test_get_recent_alerts_filters_by_severity(self):
        session, closed_session = self._session_factory()
        with patch.object(alert_engine, "get_db", closed_session), patch.object(alert_engine, "_broadcast"):
            alert_engine.raise_alert("insider", "ACTIONABLE", "A")
            alert_engine.raise_alert("kill_switch", "CRITICAL", "B", push_telegram=False)
            results = alert_engine.get_recent_alerts(severity="CRITICAL")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "B")


if __name__ == "__main__":
    unittest.main()
