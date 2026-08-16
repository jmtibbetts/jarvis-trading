"""Solana wallet activity — parsed from REAL captured payloads.

Every fixture below is a verbatim excerpt of a live Helius response
captured 2026-08-17, not a hand-written approximation of the docs. That
distinction has teeth: the predecessor parser was written from
documentation against a payload nobody had ever seen, and the endpoint it
assumed turned out not to be the one worth using.

The USDT row is kept exactly as it arrived because it is WRONG at the
source, and that wrongness is the most valuable thing in this file.
"""
import os
import tempfile
import unittest

# Verbatim from GET /v1/wallet/{addr}/transfers, trimmed to three rows.
LIVE_TRANSFERS = {
    "data": [
        {
            # amount=49.7 but amountRaw="50" and decimals=0, for a mint
            # (USDT) that actually has six. The three do not reconcile
            # under any exponent. v0 independently reports 49.7 for this
            # same signature.
            "signature": "426qsBP9nXrTcBptDEAvUo4bGWPX1swLnUvxiGcmsHzMpJ5UkpAWrEZf",
            "timestamp": 1786789506,
            "direction": "out",
            "counterparty": "Du1TJhM5x4k5a98Nc6H4GpcAL5dvRsuX4d64wkV7FHS8",
            "mint": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
            "symbol": None,
            "amount": 49.7,
            "amountRaw": "50",
            "decimals": 0,
        },
        # Two transfers sharing ONE signature, differing only by
        # counterparty — the case that makes signature alone an unusable
        # identity.
        {
            "signature": "5Bj12E1SbDdW6xxHFD9mUNYhZHi358z8h2jFxhgKAB3KyMPzTN1o",
            "timestamp": 1786789500,
            "direction": "out",
            "counterparty": "Hq9KM955F8Neq1ZV4sWCxkvyeLBS2Ss1DM3xLRspp6DT",
            "mint": "So11111111111111111111111111111111111111111",
            "symbol": "SOL",
            "amount": 3.63050357,
            "amountRaw": "3630503570",
            "decimals": 9,
        },
        {
            "signature": "5Bj12E1SbDdW6xxHFD9mUNYhZHi358z8h2jFxhgKAB3KyMPzTN1o",
            "timestamp": 1786789500,
            "direction": "out",
            "counterparty": "9gbrykp5JLiU6NEvvae4dcjj5fqPsHoXjet1CPCUoDJE",
            "mint": "So11111111111111111111111111111111111111111",
            "symbol": "SOL",
            "amount": 0.2864563,
            "amountRaw": "286456300",
            "decimals": 9,
        },
    ],
    "pagination": {"hasMore": True, "nextCursor": "3Urs7c34Ubcn"},
}

ADDR = "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9"

# A plain trader address for the collector tests. ADDR above is Binance 2
# and lives in KNOWN_INFRASTRUCTURE, so the registry correctly refuses to
# monitor it — using it here would test the exclusion, not the collector.
TRADER = "JDd3hy3gQn2V982mi1zqhNqUw1GfV2UL6g76STojCJPN"


class ParserTests(unittest.TestCase):
    def test_every_live_row_parses(self):
        from lib.wallet_activity import parse_transfers
        obs = parse_transfers(LIVE_TRANSFERS, ADDR)
        self.assertEqual(len(obs), 3, "a real payload must parse completely")

    def test_amount_is_taken_verbatim_never_derived(self):
        """The whole reason this endpoint's raw fields are ignored.

        int(amountRaw) / 10**decimals gives 50 for the USDT row. The
        correct answer is 49.7, confirmed independently by the v0
        endpoint. A parser that derives is silently 0.6% wrong here and
        arbitrarily wrong on the next token whose decimals are misreported.
        """
        from lib.wallet_activity import parse_transfers
        usdt = next(o for o in parse_transfers(LIVE_TRANSFERS, ADDR)
                    if o["mint"].startswith("Es9vMF"))
        self.assertEqual(usdt["amount"], 49.7)
        raw = LIVE_TRANSFERS["data"][0]
        derived = int(raw["amountRaw"]) / 10 ** raw["decimals"]
        self.assertNotEqual(usdt["amount"], derived,
                            "the fixture no longer captures the source defect")

    def test_direction_is_read_not_inferred(self):
        from lib.wallet_activity import parse_transfers
        for o in parse_transfers(LIVE_TRANSFERS, ADDR):
            self.assertIn(o["direction"], ("in", "out"))

    def test_null_symbol_falls_back_to_the_mint(self):
        """`symbol` is routinely null for SPL tokens; the mint is the only
        always-present identity, and an observation with no instrument is
        useless."""
        from lib.wallet_activity import parse_transfers
        usdt = next(o for o in parse_transfers(LIVE_TRANSFERS, ADDR)
                    if o["mint"].startswith("Es9vMF"))
        self.assertEqual(usdt["symbol"], usdt["mint"])

    def test_a_row_without_an_amount_is_dropped_not_reconstructed(self):
        from lib.wallet_activity import parse_transfers
        payload = {"data": [{**LIVE_TRANSFERS["data"][1], "amount": None}]}
        self.assertEqual(parse_transfers(payload, ADDR), [])

    def test_an_unknown_direction_is_dropped(self):
        from lib.wallet_activity import parse_transfers
        payload = {"data": [{**LIVE_TRANSFERS["data"][1], "direction": "?"}]}
        self.assertEqual(parse_transfers(payload, ADDR), [])

    def test_garbage_shapes_do_not_raise(self):
        from lib.wallet_activity import parse_transfers
        for junk in ({}, {"data": None}, {"data": "nope"}, {"data": [None, 7]},
                     None):
            self.assertEqual(parse_transfers(junk, ADDR), [])


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("JARVIS_EVENTS_DB_PATH")
        d = tempfile.mkdtemp(prefix="jarvis-test-events-")
        os.environ["JARVIS_EVENTS_DB_PATH"] = os.path.join(d, "ev.db")

    def tearDown(self):
        if self._prev is not None:
            os.environ["JARVIS_EVENTS_DB_PATH"] = self._prev
        else:
            os.environ.pop("JARVIS_EVENTS_DB_PATH", None)

    def test_one_signature_two_counterparties_stays_two_events(self):
        """Keying on signature alone would silently merge these into one,
        losing half a wallet's activity with no error anywhere."""
        from lib.wallet_activity import _store, parse_transfers
        obs = parse_transfers(LIVE_TRANSFERS, ADDR)
        shared = [o for o in obs if o["signature"].startswith("5Bj12E")]
        self.assertEqual(len(shared), 2, "fixture must retain the collision")
        self.assertEqual(_store(shared), 2)

    def test_repolling_the_same_window_stores_nothing_twice(self):
        """Why this keeps no cursor: an overlapping re-poll is free."""
        from lib.wallet_activity import _store, parse_transfers
        obs = parse_transfers(LIVE_TRANSFERS, ADDR)
        self.assertEqual(_store(obs), 3)
        self.assertEqual(_store(obs), 0, "a replay must be idempotent")


class StoredCountIsActuallyCountedTests(unittest.TestCase):
    """The status panel must see what the collector stored.

    EventStore.read() filters on an exact symbol (`WHERE symbol = ?`), so
    read(None, kind, ...) returns [] no matter how much is stored — and an
    empty list reads as "this feed has collected nothing". A panel built
    to detect an inert collector would itself have been permanently inert.
    Caught end-to-end: 201 events appended, the same call reported 0.
    """

    def setUp(self):
        self._prev = os.environ.get("JARVIS_EVENTS_DB_PATH")
        d = tempfile.mkdtemp(prefix="jarvis-test-events-")
        os.environ["JARVIS_EVENTS_DB_PATH"] = os.path.join(d, "ev.db")

    def tearDown(self):
        if self._prev is not None:
            os.environ["JARVIS_EVENTS_DB_PATH"] = self._prev
        else:
            os.environ.pop("JARVIS_EVENTS_DB_PATH", None)

    def test_the_endpoint_counts_what_was_stored(self):
        from app.routers.intel import wallet_activity_status
        from lib.wallet_activity import _store, parse_transfers

        self.assertEqual(wallet_activity_status()["events_stored"], 0)
        n = _store(parse_transfers(LIVE_TRANSFERS, ADDR))
        self.assertEqual(n, 3)
        out = wallet_activity_status()
        self.assertEqual(out["events_stored"], n,
                         "the panel disagrees with the collector")
        self.assertTrue(out["top_symbols"])

    def test_read_with_a_null_symbol_is_the_trap_it_looks_like(self):
        """Pinned so nobody 'simplifies' kind_summary back into read()."""
        from lib.event_store import get_store
        from lib.wallet_activity import _store, parse_transfers

        _store(parse_transfers(LIVE_TRANSFERS, ADDR))
        self.assertEqual(get_store().read(None, "onchain", since_ts=0), [])
        self.assertEqual(get_store().kind_summary("onchain")["events"], 3)

    def test_kind_summary_filters_by_source(self):
        from lib.event_store import get_store
        from lib.wallet_activity import _store, parse_transfers

        _store(parse_transfers(LIVE_TRANSFERS, ADDR))
        self.assertEqual(get_store().kind_summary("onchain", "helius")["events"], 3)
        self.assertEqual(get_store().kind_summary("onchain", "nobody")["events"], 0)


class CollectorTests(unittest.TestCase):
    """The population comes from the REGISTRY, not from the environment.

    These tests used to establish a watchlist by setting
    HELIUS_WATCH_WALLETS. That env var is now seed input only, so they seed
    the registry instead — which is the architecture under test.
    """

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in
                       ("HELIUS_API_KEY", "HELIUS_WATCH_WALLETS")}
        self._clear_registry()

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._clear_registry()

    @staticmethod
    def _clear_registry():
        from app.database import WalletRegistry, get_db
        with get_db() as db:
            db.query(WalletRegistry).delete()

    @staticmethod
    def _watch(address):
        """Put one wallet into the monitored population."""
        from app.database import get_db
        from lib import wallet_registry as reg
        with get_db() as db:
            reg.upsert_wallet(db, address, status=reg.WATCH)

    def test_no_key_is_inert_not_an_error(self):
        from lib.wallet_activity import collect_once
        os.environ.pop("HELIUS_API_KEY", None)
        self.assertIn("skipped", collect_once())

    def test_empty_registry_does_not_invent_a_population(self):
        from lib.wallet_activity import collect_once
        os.environ["HELIUS_API_KEY"] = "x" * 36
        os.environ["HELIUS_WATCH_WALLETS"] = ""
        out = collect_once()
        self.assertIn("skipped", out)
        self.assertIn("monitorable", out["skipped"])

    def test_registry_wallet_is_polled_with_an_empty_env_var(self):
        """The W1 regression, at the collector: an empty HELIUS_WATCH_WALLETS
        must NOT stop a promoted registry wallet from being polled."""
        from unittest.mock import patch

        from lib import wallet_activity
        os.environ["HELIUS_API_KEY"] = "x" * 36
        os.environ["HELIUS_WATCH_WALLETS"] = ""
        self._watch(TRADER)
        with patch.object(wallet_activity, "_fetch",
                          return_value=(LIVE_TRANSFERS, None)) as fetch:
            out = wallet_activity.collect_once()
        self.assertNotIn("skipped", out)
        self.assertEqual(out["wallets"], 1)
        fetch.assert_called_once_with(TRADER)

    def test_status_reports_configuration_without_leaking_the_key(self):
        from lib.wallet_activity import status
        os.environ["HELIUS_API_KEY"] = "sekrit" + "x" * 30
        st = status()
        self.assertTrue(st["has_key"])
        self.assertNotIn("sekrit", repr(st))

    def test_rows_that_all_fail_to_parse_are_reported_not_silent(self):
        """An empty result must never be indistinguishable from a quiet
        chain — the failure shape that has bitten this codebase repeatedly.
        """
        from unittest.mock import patch

        from lib import wallet_activity
        os.environ["HELIUS_API_KEY"] = "x" * 36
        self._watch(TRADER)
        unparseable = {"data": [{"nope": 1}, {"nope": 2}], "pagination": {}}
        with patch.object(wallet_activity, "_fetch",
                          return_value=(unparseable, None)):
            out = wallet_activity.collect_once()
        self.assertEqual(out["observations"], 0)
        self.assertTrue(out["errors"], "0 parsed from 2 rows must be reported")
        self.assertIn("0 parsed", out["errors"][0])

    def test_a_budget_truncated_wallet_names_the_bound(self):
        """`hasMore` no longer means "we gave up" — the collector follows
        the cursor. Truncation now means a BUDGET stopped the walk, and the
        report says which one rather than leaving a gap that reads as calm.
        """
        from unittest.mock import patch

        from lib import wallet_activity
        os.environ["HELIUS_API_KEY"] = "x" * 36
        self._watch(TRADER)
        budget_stopped = {**LIVE_TRANSFERS, "pages_fetched": 5,
                          "fully_drained": False,
                          "truncated_due_budget": True,
                          "truncation_reason": "max_pages (5)"}
        with patch.object(wallet_activity, "_fetch",
                          return_value=(budget_stopped, None)):
            out = wallet_activity.collect_once()
        self.assertEqual(out["wallets_truncated"], 1)
        self.assertEqual(out["wallets_fully_drained"], 0)
        self.assertIn("max_pages", out["truncated_wallets"][0])

    def test_a_fully_drained_wallet_is_not_reported_as_truncated(self):
        from unittest.mock import patch

        from lib import wallet_activity
        os.environ["HELIUS_API_KEY"] = "x" * 36
        self._watch(TRADER)
        drained = {**LIVE_TRANSFERS, "pages_fetched": 2,
                   "fully_drained": True, "truncated_due_budget": False,
                   "truncation_reason": None}
        with patch.object(wallet_activity, "_fetch",
                          return_value=(drained, None)):
            out = wallet_activity.collect_once()
        self.assertEqual(out["truncated_wallets"], [])
        self.assertEqual(out["wallets_fully_drained"], 1)
        self.assertEqual(out["pages_fetched"], 2)

    def test_a_fetch_error_never_raises(self):
        from unittest.mock import patch

        from lib import wallet_activity
        os.environ["HELIUS_API_KEY"] = "x" * 36
        self._watch(TRADER)
        with patch.object(wallet_activity, "_fetch",
                          return_value=(None, "HTTPError: boom")):
            out = wallet_activity.collect_once()
        self.assertTrue(out["errors"])
        self.assertEqual(out["observations"], 0)


if __name__ == "__main__":
    unittest.main()
