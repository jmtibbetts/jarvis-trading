"""The wallet universe is a table, not an environment variable.

The bug this locks down: `configured: false` whenever HELIUS_WATCH_WALLETS
was blank, which made an env var the database and left a working Helius
connection plus a 24KB scoring engine idle behind an empty string.

§30 names the specific case — empty watch list WITH discovery enabled must
report configured, discovery running, seed count 0.
"""
import os
import unittest
from unittest.mock import patch

from app.database import WalletRegistry, get_db, init_db
from lib.wallet_registry import (KNOWN_INFRASTRUCTURE, counts,
                                 import_seeds, intelligence_enabled,
                                 is_valid_address, load_seed_wallets,
                                 seed_known_infrastructure, upsert_wallet)

# Real, structurally valid Solana pubkeys used as fixtures.
A = "JDd3hy3gQn2V982mi1zqhNqUw1GfV2UL6g76STojCJPN"
B = "CyaE1VxvBrahnPWkqm5VsdCvyS2QmNht2UFrKJHga54o"
BINANCE = "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9"
JUPITER = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"


class AddressValidationTests(unittest.TestCase):
    def test_accepts_real_solana_pubkeys(self):
        for addr in (A, B, BINANCE, JUPITER):
            self.assertTrue(is_valid_address(addr), addr)

    def test_rejects_malformed(self):
        for bad in ("", "   ", "not-an-address", "0x1234567890abcdef",
                    "IOl0" * 11,                      # illegal base58 chars
                    "1" * 60):                         # too long
            self.assertFalse(is_valid_address(bad), repr(bad))

    def test_a_truncated_address_is_NOT_reliably_detectable(self):
        """Documenting a real limit rather than pretending it away.

        Dropping one base58 character divides the value by ~58, which often
        still decodes to 32 bytes — so length validation cannot catch every
        typo. The obvious hardening, checking the key lies on the ed25519
        curve, would be WRONG here: Program Derived Addresses are off-curve
        by design and are perfectly real accounts.

        So validation is a filter for junk, not a guarantee of existence.
        What actually catches a bad address is Helius returning nothing for
        it, which is why the registry records a discovery reason and lets
        the analysis stage archive addresses that never resolve.
        """
        truncated = A[:-1]
        self.assertTrue(is_valid_address(truncated),
                        "if this ever fails, length validation got stricter "
                        "and the comment above needs revisiting")
        self.assertNotEqual(truncated, A)

    def test_one_typo_does_not_drop_the_rest(self):
        """A malformed entry is skipped with a warning, never raised — one
        bad paste must not cost the other four wallets."""
        with patch.dict(os.environ, {"HELIUS_WATCH_WALLETS": f"{A},GARBAGE!!,{B}"}):
            self.assertEqual(load_seed_wallets(), [A, B])

    def test_tolerates_newlines_spaces_and_trailing_commas(self):
        with patch.dict(os.environ, {"HELIUS_WATCH_WALLETS": f"\n{A} ,\n {B},,\n"}):
            self.assertEqual(load_seed_wallets(), [A, B])

    def test_deduplicates_preserving_order(self):
        with patch.dict(os.environ, {"HELIUS_WATCH_WALLETS": f"{B},{A},{B}"}):
            self.assertEqual(load_seed_wallets(), [B, A])


class ConfigurationModelTests(unittest.TestCase):
    """§2: configured means 'Helius works and the feature is on', NOT
    'somebody hand-typed a wallet list'."""

    def test_empty_watchlist_is_still_configured_when_helius_works(self):
        with patch.dict(os.environ, {"HELIUS_WATCH_WALLETS": "",
                                     "HELIUS_WALLET_INTELLIGENCE_ENABLED": "true"}), \
             patch("lib.helius_client.configured", return_value=True):
            self.assertEqual(load_seed_wallets(), [])
            self.assertTrue(intelligence_enabled(),
                            "an empty seed list must not disable the subsystem")

    def test_not_configured_without_a_helius_key(self):
        with patch("lib.helius_client.configured", return_value=False):
            self.assertFalse(intelligence_enabled())

    def test_feature_flag_can_switch_it_off(self):
        with patch.dict(os.environ, {"HELIUS_WALLET_INTELLIGENCE_ENABLED": "false"}), \
             patch("lib.helius_client.configured", return_value=True):
            self.assertFalse(intelligence_enabled())


class RegistryTests(unittest.TestCase):
    def setUp(self):
        init_db()
        self._clean()

    def tearDown(self):
        self._clean()

    def _clean(self):
        with get_db() as db:
            db.query(WalletRegistry).filter(
                WalletRegistry.address.in_([A, B])).delete(
                synchronize_session=False)
            db.commit()

    def _row(self, db, addr):
        return db.query(WalletRegistry).filter(
            WalletRegistry.address == addr).first()

    def test_seeds_import_as_unproven_pinned_candidates(self):
        """A seed is a reason to investigate, not evidence of skill. It must
        arrive with NO scores — the operator's interest is not a measurement."""
        with patch.dict(os.environ, {"HELIUS_WATCH_WALLETS": f"{A},{B}"}):
            result = import_seeds()
        self.assertEqual(result["seeds_configured"], 2)
        with get_db() as db:
            for addr in (A, B):
                w = self._row(db, addr)
                self.assertEqual(w.status, "CANDIDATE")
                self.assertTrue(w.pinned)
                self.assertEqual(w.source, "manual_seed")
                self.assertIsNone(w.smart_money_score)
                self.assertIsNone(w.alpha_score)
                self.assertIsNone(w.copy_score)
                self.assertIsNone(w.win_rate)

    def test_reimport_does_not_demote_a_promoted_wallet(self):
        """THE regression risk: seeds are re-imported on every boot. A
        wallet that earned SMART_MONEY must not be knocked back to
        CANDIDATE, and its scores must survive."""
        with patch.dict(os.environ, {"HELIUS_WATCH_WALLETS": A}):
            import_seeds()
        with get_db() as db:
            w = self._row(db, A)
            w.status, w.smart_money_score, w.qualified_trades = "SMART_MONEY", 88.5, 64
            db.commit()

        with patch.dict(os.environ, {"HELIUS_WATCH_WALLETS": A}):
            import_seeds()

        with get_db() as db:
            w = self._row(db, A)
            self.assertEqual(w.status, "SMART_MONEY")
            self.assertEqual(w.smart_money_score, 88.5)
            self.assertEqual(w.qualified_trades, 64)

    def test_known_infrastructure_is_excluded_on_sight(self):
        """§13. The largest holder of the first token queried on live data
        was a Binance hot wallet; scoring it as a trader would promote an
        exchange moving customer funds to HIGH_CONVICTION."""
        seed_known_infrastructure()
        with get_db() as db:
            for addr in (BINANCE, JUPITER):
                w = self._row(db, addr) or db.query(WalletRegistry).filter(
                    WalletRegistry.address == addr).first()
                self.assertEqual(w.status, "EXCLUDED_ENTITY")
                self.assertTrue(w.is_protocol)
                self.assertFalse(w.is_trader)
                self.assertIsNotNone(w.entity_name)

    def test_discovering_infrastructure_excludes_it_even_when_asked_to_watch(self):
        """Discovery WILL surface exchanges — they are the largest holders
        of everything. Arriving through the normal path must not launder an
        exchange into the candidate pool."""
        with get_db() as db:
            upsert_wallet(db, BINANCE, source="token_holders",
                          discovery_reason="top holder of BONK",
                          status="CANDIDATE")
            db.commit()
            w = self._row(db, BINANCE) or db.query(WalletRegistry).filter(
                WalletRegistry.address == BINANCE).first()
            self.assertEqual(w.status, "EXCLUDED_ENTITY")
            self.assertFalse(w.is_trader)

    def test_pinned_survives_reimport_and_upsert(self):
        with get_db() as db:
            upsert_wallet(db, A, source="token_holders", pinned=True)
            db.commit()
            upsert_wallet(db, A, source="token_holders", pinned=False)
            db.commit()
            self.assertTrue(self._row(db, A).pinned,
                            "an operator pin is explicit and must not be "
                            "cleared by a later automatic sighting")

    def test_invalid_address_is_refused(self):
        with get_db() as db:
            with self.assertRaises(ValueError):
                upsert_wallet(db, "not-a-wallet")

    def test_counts_reports_every_lifecycle_state(self):
        seed_known_infrastructure()
        with patch.dict(os.environ, {"HELIUS_WATCH_WALLETS": f"{A},{B}"}):
            import_seeds()
        c = counts()
        self.assertGreaterEqual(c["candidates"], 2)
        self.assertGreaterEqual(c["excluded_entities"], len(KNOWN_INFRASTRUCTURE))
        self.assertEqual(c["seed"], c["seed"])  # key exists
        for key in ("discovered", "analyzing", "watching", "smart_money",
                    "high_conviction", "degraded", "archived", "total"):
            self.assertIn(key, c)

    def test_population_is_stamped_wallet_alpha(self):
        """§116: WALLET_ALPHA records must never reach CRYPTO_MAJORS
        expectancy, calibration, Gate or training. Stamped at birth so the
        guard can assert rather than infer."""
        with patch.dict(os.environ, {"HELIUS_WATCH_WALLETS": A}):
            import_seeds()
        with get_db() as db:
            self.assertEqual(self._row(db, A).population, "WALLET_ALPHA")


if __name__ == "__main__":
    unittest.main()
