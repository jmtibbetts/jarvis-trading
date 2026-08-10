import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, TradingSignal
from jobs import manage_positions


class ScaleOutTests(unittest.TestCase):
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

    def _signal(self, **overrides):
        base = dict(
            id="sig-1", asset_symbol="AAPL", direction="Long",
            entry_price=100.0, target_price=110.0, status="Executed",
            generated_at=datetime.now(timezone.utc).isoformat(),
        )
        base.update(overrides)
        return base

    def test_no_action_before_tp1_is_reached(self):
        # TP1 = 100 + (110-100)*0.5 = 105; price at 104 hasn't reached it yet
        original_signal = {"id": "sig-1", "entry_price": 100.0, "target_price": 110.0}
        with patch("lib.alpaca_client.partial_close_position") as mock_close:
            fired = manage_positions._maybe_scale_out(
                "AAPL", "AAPL", 10.0, 100.0, 104.0, False, original_signal, "Unknown", "2026-01-01T00:00:00+00:00"
            )
        self.assertFalse(fired)
        mock_close.assert_not_called()

    def test_scales_out_half_at_tp1_and_marks_signal(self):
        engine, session, closed_session = self._session_factory()
        try:
            session.add(TradingSignal(**self._signal()))
            session.commit()
            original_signal = {"id": "sig-1", "entry_price": 100.0, "target_price": 110.0,
                                "timeframe": "4H", "confidence": 70, "reasoning": "test"}

            with patch.object(manage_positions, "get_db", closed_session), \
                 patch("lib.alpaca_client.partial_close_position") as mock_close, \
                 patch.object(manage_positions, "_sync_exit_orders") as mock_sync, \
                 patch.object(manage_positions, "record_trade_outcome") as mock_record, \
                 patch.object(manage_positions, "log_decision"):
                fired = manage_positions._maybe_scale_out(
                    "AAPL", "AAPL", 10.0, 100.0, 106.0, False, original_signal, "Unknown",
                    "2026-01-01T00:00:00+00:00",
                )

            self.assertTrue(fired)
            mock_close.assert_called_once_with("AAPL", 5)  # 50% of 10 shares
            mock_sync.assert_called_once()
            # Remaining runner's stop moves to breakeven (entry price)
            sync_args = mock_sync.call_args[0]
            self.assertEqual(sync_args[3], 100.0)  # stop_price arg
            self.assertEqual(sync_args[4], 110.0)  # target_price arg unchanged
            mock_record.assert_called_once()
            self.assertEqual(mock_record.call_args.kwargs["exit_reason"], "SCALE_OUT_TP1")

            sig = session.query(TradingSignal).filter(TradingSignal.id == "sig-1").first()
            self.assertTrue(sig.scaled_out)
            self.assertEqual(sig.scaled_out_qty, 5)
        finally:
            engine.dispose()

    def test_does_not_fire_twice_for_the_same_position(self):
        original_signal = {"id": "sig-1", "entry_price": 100.0, "target_price": 110.0, "scaled_out": True}
        with patch("lib.alpaca_client.partial_close_position") as mock_close:
            fired = manage_positions._maybe_scale_out(
                "AAPL", "AAPL", 5.0, 100.0, 108.0, False, original_signal, "Unknown", "2026-01-01T00:00:00+00:00"
            )
        self.assertFalse(fired)
        mock_close.assert_not_called()

    def test_skips_positions_too_small_to_split(self):
        # qty=1 share: 50% closes to 0 shares (equity is whole-share only) — nothing to split
        original_signal = {"id": "sig-1", "entry_price": 100.0, "target_price": 110.0}
        with patch("lib.alpaca_client.partial_close_position") as mock_close:
            fired = manage_positions._maybe_scale_out(
                "AAPL", "AAPL", 1.0, 100.0, 106.0, False, original_signal, "Unknown", "2026-01-01T00:00:00+00:00"
            )
        self.assertFalse(fired)
        mock_close.assert_not_called()


if __name__ == "__main__":
    unittest.main()
