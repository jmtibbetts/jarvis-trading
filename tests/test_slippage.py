import unittest
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from unittest.mock import patch

from app.database import Base, TradingSignal
from jobs import manage_positions


class SlippageRecordingTests(unittest.TestCase):
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

        return engine, session, closed_session

    def test_records_slippage_from_intended_to_actual_fill(self):
        engine, session, closed_session = self._session_factory()
        try:
            session.add(TradingSignal(
                id="sig-1", asset_symbol="AAPL", direction="Long",
                entry_price=100.0, status="Executed",
                # A fill only exists if the signal actually reached the broker;
                # without this the record is a phantom (see _record_slippage).
                alpaca_order_id="order-abc",
                generated_at=datetime.now(timezone.utc).isoformat(),
            ))
            session.commit()

            with patch.object(manage_positions, "get_db", closed_session):
                manage_positions._record_slippage("sig-1", 100.0, 100.25, "2026-01-01T00:00:00+00:00")

            sig = session.query(TradingSignal).filter(TradingSignal.id == "sig-1").first()
            self.assertEqual(sig.actual_fill_price, 100.25)
            self.assertAlmostEqual(sig.slippage_pct, 0.25, places=4)
            self.assertEqual(sig.fill_recorded_at, "2026-01-01T00:00:00+00:00")
        finally:
            engine.dispose()

    def test_does_not_overwrite_an_already_recorded_fill(self):
        engine, session, closed_session = self._session_factory()
        try:
            session.add(TradingSignal(
                id="sig-2", asset_symbol="AAPL", direction="Long",
                entry_price=100.0, status="Executed",
                actual_fill_price=100.25, slippage_pct=0.25,
                fill_recorded_at="2026-01-01T00:00:00+00:00",
                generated_at=datetime.now(timezone.utc).isoformat(),
            ))
            session.commit()

            with patch.object(manage_positions, "get_db", closed_session):
                # A later cycle observes a different avg_entry_price (e.g. a scale-in) —
                # the first-recorded fill price must not be silently replaced.
                manage_positions._record_slippage("sig-2", 100.0, 101.0, "2026-01-02T00:00:00+00:00")

            sig = session.query(TradingSignal).filter(TradingSignal.id == "sig-2").first()
            self.assertEqual(sig.actual_fill_price, 100.25)
            self.assertEqual(sig.fill_recorded_at, "2026-01-01T00:00:00+00:00")
        finally:
            engine.dispose()

    def test_skips_when_inputs_missing(self):
        engine, session, closed_session = self._session_factory()
        try:
            session.add(TradingSignal(
                id="sig-3", asset_symbol="AAPL", direction="Long",
                entry_price=100.0, status="Executed",
                generated_at=datetime.now(timezone.utc).isoformat(),
            ))
            session.commit()

            with patch.object(manage_positions, "get_db", closed_session):
                manage_positions._record_slippage("sig-3", 0, 100.25, "2026-01-01T00:00:00+00:00")
                manage_positions._record_slippage("sig-3", 100.0, 0, "2026-01-01T00:00:00+00:00")
                manage_positions._record_slippage("", 100.0, 100.25, "2026-01-01T00:00:00+00:00")

            sig = session.query(TradingSignal).filter(TradingSignal.id == "sig-3").first()
            self.assertIsNone(sig.actual_fill_price)
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
