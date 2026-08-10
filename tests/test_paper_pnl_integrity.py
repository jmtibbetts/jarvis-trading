"""Regression tests for the $148M phantom paper-P&L incident (2026-08-10):
a BEAT/USD crypto position (entry $0.000039) was marked-to-market and closed
against NASDAQ-listed BEAT's equity price ($2.87, a 74,000x move) after the
crypto quote briefly went missing and jobs/paper_trading.py's price-lookup
fallback fell through to the unrelated equity's bare ticker.

Two fixes under test:
  1. lib/paper_engine._price_move_is_plausible / the guard wired into
     close_paper_position, partial_close_paper_position, and
     mark_to_market — the last line of defense against any bad price,
     regardless of source.
  2. jobs/paper_trading._get_all_prices' canonical-first construction —
     the actual root-cause fix, so a crypto pair's bare-symbol alias can
     no longer collide with an unrelated equity's canonical price.
"""
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, MarketAsset, PaperPosition, PaperPortfolio
from lib import paper_engine
from jobs import paper_trading


class PriceMoveIsPlausibleTests(unittest.TestCase):
    def test_the_beat_incident_is_rejected(self):
        # entry $0.000039 (real crypto BEAT/USD) vs $2.8734 (unrelated
        # NASDAQ equity BEAT) — a ~74,000x move.
        self.assertFalse(paper_engine._price_move_is_plausible(3.8802e-05, 2.8734))

    def test_normal_price_moves_are_plausible(self):
        self.assertTrue(paper_engine._price_move_is_plausible(100.0, 105.0))
        self.assertTrue(paper_engine._price_move_is_plausible(100.0, 60.0))  # -40%, a hard stop/margin call
        self.assertTrue(paper_engine._price_move_is_plausible(0.000039, 0.000078))  # a real 2x crypto pump

    def test_boundary_multiple_is_plausible_just_past_is_not(self):
        entry = 10.0
        self.assertTrue(paper_engine._price_move_is_plausible(entry, entry * paper_engine.MAX_PLAUSIBLE_PRICE_MULTIPLE))
        self.assertFalse(paper_engine._price_move_is_plausible(entry, entry * paper_engine.MAX_PLAUSIBLE_PRICE_MULTIPLE * 1.01))
        self.assertTrue(paper_engine._price_move_is_plausible(entry, entry / paper_engine.MAX_PLAUSIBLE_PRICE_MULTIPLE))
        self.assertFalse(paper_engine._price_move_is_plausible(entry, entry / paper_engine.MAX_PLAUSIBLE_PRICE_MULTIPLE / 1.01))

    def test_non_positive_or_missing_prices_are_rejected(self):
        self.assertFalse(paper_engine._price_move_is_plausible(0, 5.0))
        self.assertFalse(paper_engine._price_move_is_plausible(5.0, 0))
        self.assertFalse(paper_engine._price_move_is_plausible(-1.0, 5.0))
        self.assertFalse(paper_engine._price_move_is_plausible(None, 5.0))
        self.assertFalse(paper_engine._price_move_is_plausible(5.0, None))


class GuardedCloseTests(unittest.TestCase):
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

    def test_close_paper_position_rejects_the_beat_style_price(self):
        engine, session, closed_session = self._session_factory()
        try:
            session.add(PaperPosition(
                id="pos-beat", symbol="BEAT/USD", asset_class="Crypto",
                direction="Long", side="long", leverage=1.0, qty=51_543_734.859,
                entry_price=3.8802e-05, current_price=3.8802e-05,
                target_price=4.5e-05, stop_loss=3.5e-05,
                notional=2000.0, margin_used=2000.0,
                status="Open", opened_at=datetime.now(timezone.utc).isoformat(),
            ))
            session.commit()

            with patch.object(paper_engine, "get_db", closed_session), \
                 patch.object(paper_engine, "_record_outcome"):
                result = paper_engine.close_paper_position("pos-beat", 2.8734, reason="take_profit")

            self.assertIn("error", result)

            # Position must remain untouched — still Open, no phantom trade recorded.
            pos = session.query(PaperPosition).filter(PaperPosition.id == "pos-beat").first()
            self.assertEqual(pos.status, "Open")
            pf = session.query(PaperPortfolio).first()
            self.assertEqual(pf.realized_pnl, 0.0)
            self.assertEqual(pf.total_trades, 0)
            self.assertEqual(pf.cash, 100000.0)
        finally:
            engine.dispose()

    def test_close_paper_position_accepts_a_plausible_price(self):
        engine, session, closed_session = self._session_factory()
        try:
            session.add(PaperPosition(
                id="pos-normal", symbol="AAPL", asset_class="Equity",
                direction="Long", side="long", leverage=1.0, qty=10.0,
                entry_price=100.0, current_price=100.0,
                target_price=110.0, stop_loss=95.0,
                notional=1000.0, margin_used=1000.0,
                status="Open", opened_at=datetime.now(timezone.utc).isoformat(),
            ))
            session.commit()

            with patch.object(paper_engine, "get_db", closed_session), \
                 patch.object(paper_engine, "_record_outcome"):
                result = paper_engine.close_paper_position("pos-normal", 105.0, reason="take_profit")

            self.assertTrue(result["ok"])
            self.assertAlmostEqual(result["pnl"], 50.0, places=2)
            pf = session.query(PaperPortfolio).first()
            self.assertAlmostEqual(pf.realized_pnl, 50.0, places=2)
        finally:
            engine.dispose()

    def test_partial_close_rejects_implausible_price(self):
        engine, session, closed_session = self._session_factory()
        try:
            session.add(PaperPosition(
                id="pos-beat2", symbol="BEAT/USD", asset_class="Crypto",
                direction="Long", side="long", leverage=1.0, qty=1000.0,
                entry_price=3.8802e-05, target_price=4.5e-05, stop_loss=3.5e-05,
                notional=2000.0, margin_used=2000.0,
                status="Open", opened_at=datetime.now(timezone.utc).isoformat(),
            ))
            session.commit()

            with patch.object(paper_engine, "get_db", closed_session), \
                 patch.object(paper_engine, "_record_outcome"):
                result = paper_engine.partial_close_paper_position("pos-beat2", 0.5, 2.8734)

            self.assertIn("error", result)
            pos = session.query(PaperPosition).filter(PaperPosition.id == "pos-beat2").first()
            self.assertEqual(pos.qty, 1000.0)  # untouched
            self.assertFalse(pos.scaled_out)
        finally:
            engine.dispose()

    def test_mark_to_market_skips_implausible_price_and_leaves_position_open(self):
        engine, session, closed_session = self._session_factory()
        try:
            session.add(PaperPosition(
                id="pos-beat3", symbol="BEAT/USD", asset_class="Crypto",
                direction="Long", side="long", leverage=1.0, qty=1000.0,
                entry_price=3.8802e-05, current_price=3.8802e-05,
                target_price=4.5e-05, stop_loss=3.5e-05,
                notional=2000.0, margin_used=2000.0,
                status="Open", opened_at=datetime.now(timezone.utc).isoformat(),
            ))
            session.commit()

            with patch.object(paper_engine, "get_db", closed_session):
                result = paper_engine.mark_to_market({"BEAT/USD": 2.8734})

            self.assertEqual(result["closed"], [])
            self.assertEqual(result["updated"], 0)
            pos = session.query(PaperPosition).filter(PaperPosition.id == "pos-beat3").first()
            self.assertEqual(pos.status, "Open")
            self.assertEqual(pos.current_price, 3.8802e-05)  # unchanged — bad tick never applied
            pf = session.query(PaperPortfolio).first()
            self.assertEqual(pf.realized_pnl, 0.0)
        finally:
            engine.dispose()

    def test_mark_to_market_still_closes_on_a_plausible_take_profit(self):
        engine, session, closed_session = self._session_factory()
        try:
            session.add(PaperPosition(
                id="pos-tp", symbol="AAPL", asset_class="Equity",
                direction="Long", side="long", leverage=1.0, qty=10.0,
                entry_price=100.0, current_price=100.0,
                target_price=110.0, stop_loss=95.0,
                notional=1000.0, margin_used=1000.0,
                status="Open", opened_at=datetime.now(timezone.utc).isoformat(),
            ))
            session.commit()

            with patch.object(paper_engine, "get_db", closed_session), \
                 patch.object(paper_engine, "_record_outcome"):
                result = paper_engine.mark_to_market({"AAPL": 111.0})

            self.assertEqual(len(result["closed"]), 1)
            self.assertEqual(result["closed"][0]["reason"], "take_profit")
            pos = session.query(PaperPosition).filter(PaperPosition.id == "pos-tp").first()
            self.assertEqual(pos.status, "Closed")
        finally:
            engine.dispose()


class GetAllPricesCollisionTests(unittest.TestCase):
    """jobs/paper_trading._get_all_prices' canonical-first fix — the actual
    root cause. A crypto pair's bare-symbol alias must never shadow an
    unrelated equity's canonical price for the same bare ticker."""

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

    def test_equity_canonical_price_survives_a_colliding_crypto_alias(self):
        engine, session, closed_session = self._session_factory()
        try:
            # Real NASDAQ equity BEAT at $2.87, and a crypto pair BEAT/USD
            # at $0.000039 — both legitimately present in MarketAsset.
            session.add(MarketAsset(symbol="BEAT", name="Cardiff Oncology", asset_class="Equity", price=2.8734))
            session.add(MarketAsset(symbol="BEAT/USD", name="Beat Token", asset_class="Crypto", price=3.8802e-05))
            session.commit()

            with patch.object(paper_trading, "get_db", closed_session), \
                 patch.object(paper_trading, "FUTURES_UNIVERSE", {}):
                prices = paper_trading._get_all_prices()

            # Canonical entries: each row's own exact symbol must be correct.
            self.assertEqual(prices["BEAT"], 2.8734)
            self.assertEqual(prices["BEAT/USD"], 3.8802e-05)
            # The crypto alias for the bare symbol must NOT have overwritten
            # the equity's canonical entry — this is the exact collision
            # that caused the $148M incident.
            self.assertEqual(prices["BEAT"], 2.8734)
        finally:
            engine.dispose()

    def test_crypto_alias_still_fills_in_when_no_collision_exists(self):
        engine, session, closed_session = self._session_factory()
        try:
            session.add(MarketAsset(symbol="XRP/USD", name="XRP", asset_class="Crypto", price=1.02))
            session.commit()

            with patch.object(paper_trading, "get_db", closed_session), \
                 patch.object(paper_trading, "FUTURES_UNIVERSE", {}):
                prices = paper_trading._get_all_prices()

            self.assertEqual(prices["XRP/USD"], 1.02)
            self.assertEqual(prices["XRPUSD"], 1.02)  # alias fills a genuinely empty key
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
