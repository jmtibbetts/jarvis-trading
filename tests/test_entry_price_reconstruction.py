"""An execution price is not a market price.

The wallet's entry comes from what IT actually paid — quote spent over base
received, from its own balance change — not from an external candle at the
same minute. Those are different numbers, and silently substituting the
second makes every reconstruction agree with the market by construction:
the alpha engine would then be measuring the market against itself.

External history remains available as an explicitly LABELLED fallback for
callers that ask for it, never as a quiet substitute.

This is also the path that upgrades a HOLDER_SNAPSHOT into a
VERIFIED_BUY_ENTRY: holder -> ledger lookup -> proven acquisition. The
upgrade only happens when the ledger actually proves the buy.
"""
import unittest
import uuid

from app.database import WalletTrade, get_db
from lib.wallet_swaps import (BUY, PRICE_EXECUTION, PRICE_UNAVAILABLE,
                              promote_holder_to_verified_entry,
                              verified_entry_for)

MINT = "MintUnderTest"


def _addr() -> str:
    return "W" + uuid.uuid4().hex[:30]


def add_buy(db, addr, *, qty, usd, when="2026-08-16T10:00:00+00:00",
            quality="MEASURED", mint=MINT):
    db.add(WalletTrade(
        address=addr, signature=uuid.uuid4().hex, mint=mint,
        direction=BUY, quantity=qty, value_usd=usd,
        price=(usd / qty if qty else None), price_quality=quality,
        opened_at=when, population="WALLET_ALPHA",
        ledger_version="swap_v1_balance_delta"))
    db.flush()


class ExecutionPriceTests(unittest.TestCase):
    def test_a_single_buy_gives_its_own_execution_price(self):
        with get_db() as db:
            a = _addr()
            add_buy(db, a, qty=1_000, usd=500)
            ev = verified_entry_for(db, a, MINT)
            self.assertAlmostEqual(ev["entry_price_usd"], 0.5)
            self.assertEqual(ev["entry_price_source"], PRICE_EXECUTION)
            db.rollback()

    def test_multiple_fills_average_by_weight_not_by_count(self):
        """What a follower scaling in would actually have paid."""
        with get_db() as db:
            a = _addr()
            add_buy(db, a, qty=1_000, usd=500)      # $0.50
            add_buy(db, a, qty=9_000, usd=9_000)    # $1.00
            ev = verified_entry_for(db, a, MINT)
            # 9,500 / 10,000 = 0.95, not the 0.75 a naive mean would give.
            self.assertAlmostEqual(ev["entry_price_usd"], 0.95)
            self.assertEqual(ev["priced_buys"], 2)
            db.rollback()

    def test_the_earliest_buy_anchors_the_entry_time(self):
        with get_db() as db:
            a = _addr()
            add_buy(db, a, qty=100, usd=100, when="2026-08-16T12:00:00+00:00")
            add_buy(db, a, qty=100, usd=100, when="2026-08-16T09:00:00+00:00")
            ev = verified_entry_for(db, a, MINT)
            self.assertIsNotNone(ev["entry_timestamp"])
            from datetime import datetime, timezone
            got = datetime.fromtimestamp(ev["entry_timestamp"], tz=timezone.utc)
            self.assertEqual(got.hour, 9)
            db.rollback()


class RefusalTests(unittest.TestCase):
    """UNKNOWN beats a fabricated number."""

    def test_no_buys_means_no_price(self):
        with get_db() as db:
            ev = verified_entry_for(db, _addr(), MINT)
            self.assertIsNone(ev["entry_price_usd"])
            self.assertEqual(ev["entry_price_source"], PRICE_UNAVAILABLE)
            self.assertIn("no verified buy", ev["reason"])
            db.rollback()

    def test_unvalued_buys_do_not_produce_a_price(self):
        """Buys exist but the quote leg had no price — a token-to-token
        entry. That is UNKNOWN, not zero and not a guess."""
        with get_db() as db:
            a = _addr()
            db.add(WalletTrade(address=a, signature=uuid.uuid4().hex,
                               mint=MINT, direction=BUY, quantity=1_000,
                               value_usd=None, opened_at="2026-08-16T10:00:00+00:00",
                               population="WALLET_ALPHA"))
            db.flush()
            ev = verified_entry_for(db, a, MINT)
            self.assertIsNone(ev["entry_price_usd"])
            self.assertEqual(ev["buys"], 1)
            self.assertIn("none could be valued", ev["reason"])
            db.rollback()

    def test_unvalued_buys_are_excluded_and_counted_not_silently_dropped(self):
        with get_db() as db:
            a = _addr()
            add_buy(db, a, qty=1_000, usd=500)
            db.add(WalletTrade(address=a, signature=uuid.uuid4().hex,
                               mint=MINT, direction=BUY, quantity=500,
                               value_usd=None, opened_at="2026-08-16T10:00:00+00:00",
                               population="WALLET_ALPHA"))
            db.flush()
            ev = verified_entry_for(db, a, MINT)
            self.assertAlmostEqual(ev["entry_price_usd"], 0.5)
            self.assertEqual(ev["unpriced_buys"], 1)
            self.assertIn("unvalued and excluded", ev["reason"])
            db.rollback()

    def test_a_market_fallback_is_off_by_default(self):
        import inspect
        src = inspect.signature(verified_entry_for)
        self.assertIs(src.parameters["allow_market_fallback"].default, False,
                      "a market candle must never silently stand in for a fill")


class QualityTests(unittest.TestCase):
    def test_one_estimated_input_demotes_the_aggregate(self):
        """A weighted average is only as good as its weakest input."""
        with get_db() as db:
            a = _addr()
            add_buy(db, a, qty=1_000, usd=500, quality="MEASURED")
            add_buy(db, a, qty=1_000, usd=600, quality="ESTIMATED")
            ev = verified_entry_for(db, a, MINT)
            self.assertEqual(ev["price_quality"], "ESTIMATED")
            db.rollback()

    def test_all_measured_stays_measured(self):
        with get_db() as db:
            a = _addr()
            add_buy(db, a, qty=1_000, usd=500, quality="MEASURED")
            add_buy(db, a, qty=1_000, usd=600, quality="MEASURED")
            self.assertEqual(verified_entry_for(db, a, MINT)["price_quality"],
                             "MEASURED")
            db.rollback()


class HolderPromotionTests(unittest.TestCase):
    """holder -> ledger lookup -> VERIFIED_BUY_ENTRY, only on proof."""

    class _Obs:
        def __init__(self, addr, mint, surge=None):
            self.wallet_address = addr
            self.mint = mint
            self.evidence_class = "HOLDER_SNAPSHOT"
            self.alpha_eligible = 0
            self.entry_price_usd = None
            self.entry_amount = None
            self.entry_notional_usd = None
            self.entry_timestamp = None
            self.price_source = None
            self.price_quality = None
            self.surge_started_at = surge
            self.seconds_before_surge = None

    def test_a_holder_with_a_ledger_buy_is_promoted(self):
        with get_db() as db:
            a = _addr()
            add_buy(db, a, qty=1_000, usd=500)
            obs = self._Obs(a, MINT)
            out = promote_holder_to_verified_entry(db, obs)
            self.assertTrue(out["promoted"])
            self.assertEqual(obs.evidence_class, "VERIFIED_BUY_ENTRY")
            self.assertTrue(obs.alpha_eligible)
            self.assertAlmostEqual(obs.entry_price_usd, 0.5)
            self.assertEqual(obs.price_source, PRICE_EXECUTION)
            db.rollback()

    def test_a_holder_with_no_ledger_evidence_is_not_promoted(self):
        with get_db() as db:
            obs = self._Obs(_addr(), MINT)
            out = promote_holder_to_verified_entry(db, obs)
            self.assertFalse(out["promoted"])
            self.assertEqual(obs.evidence_class, "HOLDER_SNAPSHOT")
            self.assertFalse(obs.alpha_eligible)
            db.rollback()

    def test_promotion_recomputes_the_surge_offset(self):
        """The old offset described a different entry time, so leaving it
        would attach a real price to a fabricated earliness."""
        with get_db() as db:
            a = _addr()
            add_buy(db, a, qty=1_000, usd=500,
                    when="2026-08-16T11:48:00+00:00")
            obs = self._Obs(a, MINT, surge="2026-08-16T12:00:00+00:00")
            promote_holder_to_verified_entry(db, obs)
            self.assertEqual(obs.seconds_before_surge, -720.0)
            db.rollback()

    def test_an_already_verified_entry_is_left_alone(self):
        with get_db() as db:
            obs = self._Obs(_addr(), MINT)
            obs.evidence_class = "VERIFIED_BUY_ENTRY"
            out = promote_holder_to_verified_entry(db, obs)
            self.assertFalse(out["promoted"])
            self.assertIn("already", out["reason"])
            db.rollback()


if __name__ == "__main__":
    unittest.main()
