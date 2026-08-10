import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, PaperPosition, PaperPortfolio
from lib import paper_engine
from jobs import paper_trading


class PartialClosePaperPositionTests(unittest.TestCase):
    def _session_factory(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine, expire_on_commit=False)
        session = Session()
        session.add(PaperPortfolio(id="pf-1", cash=100000.0, total_trades=0, winning_trades=0, realized_pnl=0.0))
        session.commit()

        @contextmanager
        def closed_session():
            try:
                yield session
                session.commit()
            finally:
                pass

        return engine, session, closed_session

    def test_partial_close_realizes_pnl_and_keeps_position_open(self):
        engine, session, closed_session = self._session_factory()
        try:
            session.add(PaperPosition(
                id="pos-1", symbol="BTC/USD", asset_class="Crypto",
                direction="Long", side="long", leverage=1.0, qty=1.0,
                entry_price=50000.0, current_price=50000.0,
                target_price=55000.0, stop_loss=48000.0,
                notional=50000.0, margin_used=50000.0,
                status="Open", opened_at=datetime.now(timezone.utc).isoformat(),
            ))
            session.commit()

            with patch.object(paper_engine, "get_db", closed_session), \
                 patch.object(paper_engine, "_record_outcome"):
                result = paper_engine.partial_close_paper_position("pos-1", 0.5, 52500.0, reason="scale_out_tp1")

            self.assertTrue(result["ok"])
            self.assertAlmostEqual(result["pnl"], 1250.0, places=2)  # (52500-50000)*0.5
            self.assertEqual(result["closed_qty"], 0.5)
            self.assertEqual(result["remaining_qty"], 0.5)

            pos = session.query(PaperPosition).filter(PaperPosition.id == "pos-1").first()
            self.assertEqual(pos.status, "Open")
            self.assertEqual(pos.qty, 0.5)
            self.assertTrue(pos.scaled_out)
            self.assertEqual(pos.scaled_out_qty, 0.5)
            self.assertEqual(pos.margin_used, 25000.0)

            pf = session.query(PaperPortfolio).first()
            self.assertAlmostEqual(pf.realized_pnl, 1250.0, places=2)
            self.assertEqual(pf.total_trades, 1)
        finally:
            engine.dispose()

    def test_cannot_scale_out_twice(self):
        engine, session, closed_session = self._session_factory()
        try:
            session.add(PaperPosition(
                id="pos-2", symbol="BTC/USD", asset_class="Crypto",
                direction="Long", side="long", leverage=1.0, qty=1.0,
                entry_price=50000.0, target_price=55000.0, stop_loss=48000.0,
                notional=50000.0, margin_used=50000.0, scaled_out=True, scaled_out_qty=0.5,
                status="Open", opened_at=datetime.now(timezone.utc).isoformat(),
            ))
            session.commit()

            with patch.object(paper_engine, "get_db", closed_session), \
                 patch.object(paper_engine, "_record_outcome"):
                result = paper_engine.partial_close_paper_position("pos-2", 0.5, 52500.0)

            self.assertIn("error", result)
        finally:
            engine.dispose()


class MaybeScaleOutPaperTests(unittest.TestCase):
    def test_no_action_before_tp1(self):
        pos = {"id": "pos-1", "symbol": "AAPL", "direction": "Long", "entry_price": 100.0,
               "target_price": 110.0, "qty": 10.0, "scaled_out": False}
        with patch("lib.paper_engine.partial_close_paper_position") as mock_partial:
            result = paper_trading._maybe_scale_out_paper(pos, 104.0)
        self.assertIsNone(result)
        mock_partial.assert_not_called()

    def test_scales_out_long_at_tp1(self):
        pos = {"id": "pos-1", "symbol": "AAPL", "direction": "Long", "entry_price": 100.0,
               "target_price": 110.0, "qty": 10.0, "scaled_out": False}
        with patch("lib.paper_engine.partial_close_paper_position",
                    return_value={"ok": True, "closed_qty": 5.0, "remaining_qty": 5.0, "pnl": 30.0}) as mock_partial, \
             patch.object(paper_trading, "get_db") as mock_get_db:
            mock_get_db.return_value.__enter__.return_value.query.return_value.filter.return_value.first.return_value = None
            result = paper_trading._maybe_scale_out_paper(pos, 106.0)
        self.assertIsNotNone(result)
        mock_partial.assert_called_once_with("pos-1", 0.5, 106.0, reason="scale_out_tp1")

    def test_scales_out_short_at_tp1(self):
        # short: entry=100, target=90 -> TP1 = 100 - (100-90)*0.5 = 95; price 94 has reached it
        pos = {"id": "pos-2", "symbol": "AAPL", "direction": "Short", "entry_price": 100.0,
               "target_price": 90.0, "qty": 10.0, "scaled_out": False}
        with patch("lib.paper_engine.partial_close_paper_position",
                    return_value={"ok": True, "closed_qty": 5.0, "remaining_qty": 5.0, "pnl": 30.0}) as mock_partial, \
             patch.object(paper_trading, "get_db") as mock_get_db:
            mock_get_db.return_value.__enter__.return_value.query.return_value.filter.return_value.first.return_value = None
            result = paper_trading._maybe_scale_out_paper(pos, 94.0)
        self.assertIsNotNone(result)
        mock_partial.assert_called_once()

    def test_already_scaled_out_is_a_no_op(self):
        pos = {"id": "pos-1", "symbol": "AAPL", "direction": "Long", "entry_price": 100.0,
               "target_price": 110.0, "qty": 10.0, "scaled_out": True}
        with patch("lib.paper_engine.partial_close_paper_position") as mock_partial:
            result = paper_trading._maybe_scale_out_paper(pos, 108.0)
        self.assertIsNone(result)
        mock_partial.assert_not_called()


if __name__ == "__main__":
    unittest.main()
