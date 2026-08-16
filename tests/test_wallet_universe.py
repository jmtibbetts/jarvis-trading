"""W1 — there is exactly ONE wallet universe.

The defect: `wallet_registry` was documented as the source of truth while
`wallet_activity` and `/wallet/intel` both read `HELIUS_WATCH_WALLETS`
directly and skipped when it was blank — which it is on this deployment.
Discovery filled a table no runtime loop read.

The regression the audit asked for by name:

    HELIUS_WATCH_WALLETS=""
    registry contains WATCH wallet A
    EXPECTED: wallet_activity polls A, /wallet/intel analyses A
    NOT:      "no wallets to analyse"
"""
import os
import unittest
from unittest.mock import patch

from app.database import WalletRegistry, get_db
from lib import wallet_registry as reg

A = "JDd3hy3gQn2V982mi1zqhNqUw1GfV2UL6g76STojCJPN"
B = "CyaE1VxvBrahnPWkqm5VsdCvyS2QmNht2UFrKJHga54o"
C = "Bi4rd5FH5bYEN8scZ7wevxNZyNmKHdaBcvewdPFxYdLt"
BINANCE = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"

_ONE_TRANSFER = {"data": [{
    "signature": "sig-w1-regression",
    "timestamp": 1786789506,
    "direction": "out",
    "counterparty": "Du1TJhM5x4k5a98Nc6H4GpcAL5dvRsuX4d64wkV7FHS8",
    "mint": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    "symbol": None,
    "amount": 250_000.0,
}]}


def _clear(session):
    session.query(WalletRegistry).delete()
    session.flush()


class MonitorableSelectorTests(unittest.TestCase):

    def test_watch_wallet_is_monitored_with_empty_env(self):
        """The exact scenario from the audit."""
        with get_db() as db:
            _clear(db)
            reg.upsert_wallet(db, A, status=reg.WATCH)
            db.flush()
            with patch.dict(os.environ, {"HELIUS_WATCH_WALLETS": ""}):
                self.assertIn(A, reg.get_monitorable_wallets(db))

    def test_promoted_states_are_monitored(self):
        with get_db() as db:
            _clear(db)
            for addr, st in ((A, reg.WATCH), (B, reg.SMART_MONEY),
                             (C, reg.HIGH_CONVICTION)):
                reg.upsert_wallet(db, addr, status=st)
            db.flush()
            got = reg.get_monitorable_wallets(db)
            self.assertEqual(set(got), {A, B, C})

    def test_unproven_states_are_not_monitored(self):
        """A discovered wallet is not yet worth spending budget on."""
        with get_db() as db:
            _clear(db)
            reg.upsert_wallet(db, A, status=reg.DISCOVERED)
            reg.upsert_wallet(db, B, status=reg.CANDIDATE)
            db.flush()
            self.assertEqual(reg.get_monitorable_wallets(db), [])

    def test_pinned_candidate_is_monitored(self):
        """This is how imported seeds keep being watched: a seed lands as a
        PINNED CANDIDATE, so addresses that used to be polled straight from
        the env var still are — through the registry."""
        with get_db() as db:
            _clear(db)
            reg.upsert_wallet(db, A, status=reg.CANDIDATE, pinned=True)
            db.flush()
            self.assertIn(A, reg.get_monitorable_wallets(db))

    def test_excluded_entity_is_never_monitored_even_if_pinned(self):
        """A mistakenly pinned exchange must not buy its way back in."""
        with get_db() as db:
            _clear(db)
            reg.upsert_wallet(db, BINANCE, pinned=True)
            db.flush()
            row = db.query(WalletRegistry).filter(
                WalletRegistry.address == BINANCE).first()
            self.assertEqual(row.status, reg.EXCLUDED_ENTITY)
            self.assertNotIn(BINANCE, reg.get_monitorable_wallets(db))

    def test_archived_is_never_monitored(self):
        with get_db() as db:
            _clear(db)
            reg.upsert_wallet(db, A, status=reg.ARCHIVED)
            db.flush()
            self.assertEqual(reg.get_monitorable_wallets(db), [])

    def test_most_proven_survive_truncation(self):
        """A caller bounded at N spends them on HIGH_CONVICTION first."""
        with get_db() as db:
            _clear(db)
            reg.upsert_wallet(db, A, status=reg.WATCH)
            reg.upsert_wallet(db, B, status=reg.HIGH_CONVICTION)
            db.flush()
            self.assertEqual(reg.get_monitorable_wallets(db, limit=1), [B])

    def test_empty_population_says_which_emptiness(self):
        with get_db() as db:
            _clear(db)
            db.flush()
            self.assertIn("empty", reg.monitorable_breakdown(db)["reason"])
            reg.upsert_wallet(db, A, status=reg.CANDIDATE)
            db.flush()
            r = reg.monitorable_breakdown(db)["reason"]
            self.assertIn("promoted", r)


class ActiveAnalysisCapTests(unittest.TestCase):
    """Item 13 — registry growth must not disable discovery."""

    def test_cap_counts_only_the_analysis_queue(self):
        with get_db() as db:
            _clear(db)
            reg.upsert_wallet(db, A, status=reg.CANDIDATE)      # counts
            reg.upsert_wallet(db, B, status=reg.SMART_MONEY)    # does not
            reg.upsert_wallet(db, C, status=reg.ARCHIVED)       # does not
            reg.upsert_wallet(db, BINANCE)                      # does not
            db.flush()
            self.assertEqual(reg.active_analysis_count(db), 1)
            self.assertEqual(db.query(WalletRegistry).count(), 4)

    def test_permanent_rows_do_not_fill_the_queue(self):
        """The whole point: the registry may accumulate learned identities
        forever without ever throttling discovery."""
        with get_db() as db:
            _clear(db)
            for st in (reg.SMART_MONEY, reg.HIGH_CONVICTION, reg.ARCHIVED,
                       reg.WATCH, reg.DEGRADED):
                pass
            reg.upsert_wallet(db, A, status=reg.SMART_MONEY)
            reg.upsert_wallet(db, B, status=reg.HIGH_CONVICTION)
            reg.upsert_wallet(db, C, status=reg.DEGRADED)
            db.flush()
            self.assertEqual(reg.active_analysis_count(db), 0)


class BootstrapTests(unittest.TestCase):

    def test_exclusions_land_before_seeds(self):
        with get_db() as db:
            _clear(db)
            db.flush()
            with patch.dict(os.environ, {"HELIUS_WATCH_WALLETS": BINANCE}):
                reg.bootstrap(db)
                db.flush()
            row = db.query(WalletRegistry).filter(
                WalletRegistry.address == BINANCE).first()
            self.assertEqual(row.status, reg.EXCLUDED_ENTITY,
                             "an address in both lists must end excluded")

    def test_seeds_arrive_pinned_and_unproven(self):
        with get_db() as db:
            _clear(db)
            db.flush()
            with patch.dict(os.environ, {"HELIUS_WATCH_WALLETS": A}):
                reg.bootstrap(db)
                db.flush()
            row = db.query(WalletRegistry).filter(
                WalletRegistry.address == A).first()
            self.assertEqual(row.status, reg.CANDIDATE)
            self.assertTrue(row.pinned)
            self.assertIsNone(row.smart_money_score)

    def test_bootstrap_never_downgrades_a_promotion(self):
        """Idempotent on every boot: a wallet promoted since the last start
        must not be knocked back to CANDIDATE by re-seeding."""
        with get_db() as db:
            _clear(db)
            reg.upsert_wallet(db, A, status=reg.SMART_MONEY)
            db.flush()
            with patch.dict(os.environ, {"HELIUS_WATCH_WALLETS": A}):
                reg.bootstrap(db)
                db.flush()
            row = db.query(WalletRegistry).filter(
                WalletRegistry.address == A).first()
            self.assertEqual(row.status, reg.SMART_MONEY)


class DuplicateInsertTests(unittest.TestCase):
    """Found while wiring bootstrap, and it was a live defect.

    `upsert_wallet` added without flushing, so a second upsert of the same
    address inside ONE session could not see the pending row and inserted a
    duplicate. Discovery hits this whenever a wallet is both a token holder
    and a pool trader in the same pass.
    """

    def test_same_address_twice_in_one_session(self):
        with get_db() as db:
            _clear(db)
            reg.upsert_wallet(db, A, discovery_reason="token_holders")
            reg.upsert_wallet(db, A, discovery_reason="pool_traders")
            db.flush()
            self.assertEqual(
                db.query(WalletRegistry).filter(
                    WalletRegistry.address == A).count(), 1)


class IntelRouteUniverseTests(unittest.TestCase):
    """The /wallet/intel half of the audit's named regression."""

    def test_route_analyses_a_registry_wallet_with_empty_env(self):
        from app.routers import intel as intel_routes

        with get_db() as db:
            _clear(db)
            reg.upsert_wallet(db, A, status=reg.WATCH)

        with patch.dict(os.environ, {"HELIUS_WATCH_WALLETS": "",
                                     "HELIUS_API_KEY": "k" * 36}), \
             patch("lib.helius_client.configured", return_value=True), \
             patch("lib.helius_client.batch_identity", return_value={}), \
             patch("lib.helius_client.funded_by", return_value={}), \
             patch("lib.token_pricing.resolve_prices", return_value={}), \
             patch("lib.helius_client.transfers",
                   return_value=_ONE_TRANSFER) as tr:
            out = intel_routes.wallet_intel_report(limit=10)

        self.assertTrue(out["configured"])
        # THE assertion: the address analysed came from the registry while
        # HELIUS_WATCH_WALLETS was empty.
        tr.assert_called_once()
        self.assertEqual(tr.call_args[0][0], A)
        self.assertGreater(out.get("transfers", 0), 0)
        self._clear()

    @staticmethod
    def _clear():
        with get_db() as db:
            _clear(db)


class StartupWiringTests(unittest.TestCase):
    """The gap the existing suite could not see.

    tests/test_wallet_registry.py asserts seeds are re-imported on every
    boot, but calls `import_seeds()` directly — so it passed against a
    startup path that never invoked it. This drives the real lifespan.
    """

    def test_lifespan_bootstraps_the_registry(self):
        import asyncio

        import main as main_mod

        called = {"n": 0}

        def _spy(*a, **kw):
            called["n"] += 1
            return {"infrastructure_excluded": 0, "seeds_configured": 0,
                    "imported": 0, "already_present": 0}

        async def _drive():
            with patch.dict(os.environ, {"JARVIS_DISABLE_SCHEDULER": "1"}), \
                 patch("lib.wallet_registry.bootstrap", _spy), \
                 patch("lib.orderbook_stream.start_orderbook_streams",
                       lambda **kw: []):
                async with main_mod.lifespan(main_mod.app):
                    pass

        try:
            asyncio.run(_drive())
        except Exception:
            # Startup touches streams and threads that may not be available
            # in a test process. The assertion below is what matters: the
            # bootstrap call must happen, and it happens before any of that.
            pass
        self.assertEqual(called["n"], 1,
                         "lifespan must call wallet_registry.bootstrap() exactly once")


if __name__ == "__main__":
    unittest.main()
