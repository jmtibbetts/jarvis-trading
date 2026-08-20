"""The Helius wallet polling loop — what starts it, and what it may touch.

WHAT THIS LOOP IS ALLOWED TO BE. One timer calling one existing read-only
function. The legacy scheduler that used to own this job also executes
signals, opens paper positions and manages the book, which is why it stays
disabled — and why wallet intelligence needed its own switch rather than
that one.

THE TWO PROPERTIES THAT MATTER MOST, both pinned below:

  it is OFF unless someone said ON, in a way a typo cannot fake —
  `bool("false")` is True, so a truthiness check would have started a paid
  provider loop on the string "false";

  a FAILED pass is not an EMPTY pass. Zeroing the counts on failure would
  make "we could not reach Helius" indistinguishable from "we looked and
  the chain was quiet", and only one of those is about the market.
"""
import ast
import os
import pathlib
import threading
import time
import unittest
from unittest.mock import patch

from lib import wallet_poller as WP

ROOT = pathlib.Path(__file__).parent.parent

ON = {WP.POLLING_ENABLED_ENV: "true"}
OFF = {WP.POLLING_ENABLED_ENV: ""}


def _collect_result(**kw):
    """The real shape `wallet_activity.collect_once` returns."""
    base = {"wallets": 5, "observations": 0, "stored": 0, "duplicates": 0,
            "errors": [], "page_limit": 100, "truncated_wallets": [],
            "parser": "helius_v1_transfers_v1", "pages_fetched": 5,
            "wallets_fully_drained": 5, "wallets_truncated": 0,
            "credits_estimated": 5, "max_pages_per_poll": 5,
            "backfill_limit": 500}
    base.update(kw)
    return base


class PollerCase(unittest.TestCase):
    def setUp(self):
        WP.stop(timeout=2.0)
        WP._reset_for_tests()

    def tearDown(self):
        WP.stop(timeout=2.0)
        WP._reset_for_tests()


# ── Gating ───────────────────────────────────────────────────────────────
class GatingTests(PollerCase):

    def test_polling_defaults_off_when_the_variable_is_absent(self):
        env = {k: v for k, v in os.environ.items()
               if k != WP.POLLING_ENABLED_ENV}
        with patch.dict(os.environ, env, clear=True):
            self.assertFalse(WP.polling_enabled())
            res = WP.start()
            self.assertFalse(res["started"])
            self.assertEqual(res["reason"], WP.POLL_DISABLED)
            self.assertFalse(WP.is_running())

    def test_malformed_configuration_stays_off(self):
        """`bool("false")` is True. An allowlist is the only safe reading."""
        for bad in ("false", "no", "0", "off", "disabled", "maybe", "TRUEISH",
                    " ", "yes please", "2", "None", "null", "enabled=1"):
            with patch.dict(os.environ,
                            {WP.POLLING_ENABLED_ENV: bad}, clear=False):
                self.assertFalse(WP.polling_enabled(),
                                 f"{bad!r} enabled a provider loop")
                self.assertFalse(WP.start()["started"], bad)
                self.assertFalse(WP.is_running(), bad)

    def test_only_explicit_true_tokens_enable_it(self):
        for good in ("1", "true", "TRUE", "True", "yes", "on", "enabled",
                     "  true  "):
            with patch.dict(os.environ,
                            {WP.POLLING_ENABLED_ENV: good}, clear=False):
                self.assertTrue(WP.polling_enabled(), f"{good!r} refused")

    def test_the_interval_is_bounded_and_survives_a_malformed_value(self):
        cases = {
            "": WP.DEFAULT_INTERVAL_S, "abc": WP.DEFAULT_INTERVAL_S,
            "  ": WP.DEFAULT_INTERVAL_S, "nan": WP.DEFAULT_INTERVAL_S,
            "1": WP.MIN_INTERVAL_S, "0": WP.MIN_INTERVAL_S,
            "-5": WP.MIN_INTERVAL_S,
            "999999999": WP.MAX_INTERVAL_S,
            "300": 300, "900": 900, "120.7": 120,
        }
        for raw, expected in cases.items():
            with patch.dict(os.environ,
                            {WP.POLL_INTERVAL_ENV: raw}, clear=False):
                self.assertEqual(WP.poll_interval_seconds(), expected,
                                 f"{raw!r}")

    def test_the_default_matches_the_cadence_this_job_already_had(self):
        self.assertEqual(WP.DEFAULT_INTERVAL_S, 900)


# ── Starting only this loop ──────────────────────────────────────────────
class StartsOnlyTheWalletLoopTests(PollerCase):

    def test_enabling_it_starts_exactly_one_named_thread(self):
        before = {t.name for t in threading.enumerate()}
        with patch.dict(os.environ, {**ON, WP.POLL_INTERVAL_ENV: "3600"},
                        clear=False):
            with patch("lib.wallet_activity.collect_once",
                       return_value=_collect_result()):
                res = WP.start()
                self.assertTrue(res["started"])
                time.sleep(0.4)
                after = {t.name for t in threading.enumerate()}
                new = after - before
                self.assertEqual(new, {"helius-wallet-poller"},
                                 f"unexpected threads started: {new}")
                self.assertTrue(WP.is_running())

    def test_starting_twice_does_not_start_a_second_loop(self):
        with patch.dict(os.environ, {**ON, WP.POLL_INTERVAL_ENV: "3600"},
                        clear=False):
            with patch("lib.wallet_activity.collect_once",
                       return_value=_collect_result()):
                self.assertTrue(WP.start()["started"])
                time.sleep(0.2)
                again = WP.start()
                self.assertFalse(again["started"])
                self.assertEqual(again["reason"], "ALREADY_RUNNING")
                names = [t.name for t in threading.enumerate()
                         if t.name == "helius-wallet-poller"]
                self.assertEqual(len(names), 1)

    def test_the_legacy_scheduler_is_untouched_by_this_flag(self):
        """This loop neither reads JARVIS_DISABLE_SCHEDULER nor is read by
        it. Enabling wallet polling must not enable the trading scheduler."""
        src = (ROOT / "lib" / "wallet_poller.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        consts = {n.value for n in ast.walk(tree)
                  if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        self.assertNotIn("JARVIS_DISABLE_SCHEDULER", consts)
        self.assertEqual(os.getenv("JARVIS_DISABLE_SCHEDULER"), "1")

        # And in main.py the poller starts OUTSIDE the scheduler branch.
        #
        # Checked on the AST, not by comparing string offsets: the first
        # textual occurrence of JARVIS_DISABLE_SCHEDULER is a LOG line, so a
        # positional test compares against the wrong thing and fails on
        # correct code. What matters is nesting, which only the tree knows.
        main_tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        lifespan = next(n for n in ast.walk(main_tree)
                        if isinstance(n, (ast.FunctionDef,
                                          ast.AsyncFunctionDef))
                        and n.name == "lifespan")

        def _starts_poller(node):
            return [c for c in ast.walk(node)
                    if isinstance(c, ast.Call)
                    and isinstance(c.func, ast.Attribute)
                    and c.func.attr == "start"
                    and isinstance(c.func.value, ast.Name)
                    and c.func.value.id == "wallet_poller"]

        self.assertTrue(_starts_poller(lifespan),
                        "the poller is never started at startup")

        gates = [n for n in ast.walk(lifespan) if isinstance(n, ast.If)
                 and "JARVIS_DISABLE_SCHEDULER" in ast.dump(n.test)]
        self.assertTrue(gates, "the scheduler gate disappeared from lifespan")
        for gate in gates:
            for branch in (gate.body, gate.orelse):
                for stmt in branch:
                    self.assertEqual(
                        _starts_poller(stmt), [],
                        "the poller start moved INSIDE the scheduler gate — "
                        "wallet intelligence would then require the trading "
                        "scheduler")

    def test_shutdown_stops_the_loop_cleanly(self):
        with patch.dict(os.environ, {**ON, WP.POLL_INTERVAL_ENV: "3600"},
                        clear=False):
            with patch("lib.wallet_activity.collect_once",
                       return_value=_collect_result()):
                WP.start()
                time.sleep(0.3)
                self.assertTrue(WP.is_running())
                # The sleep is interruptible: stopping must not wait out the
                # hour-long interval.
                t0 = time.time()
                out = WP.stop(timeout=5.0)
                elapsed = time.time() - t0
        self.assertTrue(out["stopped"], out)
        self.assertLess(elapsed, 3.0, "shutdown waited for the interval")
        self.assertFalse(WP.is_running())
        self.assertNotIn("helius-wallet-poller",
                         [t.name for t in threading.enumerate()])


# ── Overlap ──────────────────────────────────────────────────────────────
class OverlapTests(PollerCase):

    def test_a_second_pass_is_refused_while_one_is_in_flight(self):
        entered = threading.Event()
        release = threading.Event()

        def slow(*_a, **_k):
            entered.set()
            release.wait(5.0)
            return _collect_result()

        with patch("lib.wallet_activity.collect_once", side_effect=slow):
            t = threading.Thread(target=WP.poll_once, daemon=True)
            t.start()
            self.assertTrue(entered.wait(5.0), "the slow pass never started")

            refused = WP.poll_once()
            self.assertFalse(refused["ok"])
            self.assertEqual(refused["result"], WP.POLL_ALREADY_RUNNING)

            release.set()
            t.join(5.0)

        self.assertEqual(WP.status()["polls_refused_overlapping"], 1)
        self.assertEqual(WP.status()["polls_completed"], 1)

    def test_a_refused_pass_does_not_call_the_provider(self):
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def slow(*_a, **_k):
            calls.append(1)
            entered.set()
            release.wait(5.0)
            return _collect_result()

        with patch("lib.wallet_activity.collect_once", side_effect=slow):
            t = threading.Thread(target=WP.poll_once, daemon=True)
            t.start()
            entered.wait(5.0)
            for _ in range(4):
                WP.poll_once()
            release.set()
            t.join(5.0)
        self.assertEqual(len(calls), 1, "a refused pass still called Helius")


# ── Failure is not an empty collection ───────────────────────────────────
class FailureSemanticsTests(PollerCase):

    def test_a_provider_failure_does_not_become_a_successful_empty_poll(self):
        with patch("lib.wallet_activity.collect_once",
                   side_effect=RuntimeError("helius unreachable")):
            out = WP.poll_once()
        self.assertFalse(out["ok"])
        self.assertEqual(out["result"], WP.POLL_FAILED)

        st = WP.status()
        self.assertEqual(st["last_result"], WP.POLL_FAILED)
        # MISSING IS NOT ZERO — the whole point.
        for field in ("observed", "inserted", "deduplicated",
                      "provider_calls"):
            self.assertIsNone(st[field],
                              f"{field} became {st[field]!r} on failure")
        self.assertIn("unreachable", st["last_error"])
        self.assertEqual(st["polls_failed"], 1)

    def test_a_skipped_pass_reports_null_counts_not_zero(self):
        with patch("lib.wallet_activity.collect_once",
                   return_value={"skipped": "no monitorable wallets"}):
            out = WP.poll_once()
        self.assertEqual(out["result"], WP.POLL_SKIPPED)
        st = WP.status()
        for field in ("observed", "inserted", "deduplicated",
                      "provider_calls"):
            self.assertIsNone(st[field])

    def test_a_genuinely_quiet_chain_reports_zero_not_null(self):
        """The other side of the same rule: looking and finding nothing IS
        a measurement, and must be reported as one."""
        with patch("lib.wallet_activity.collect_once",
                   return_value=_collect_result(observations=0, stored=0,
                                                duplicates=0,
                                                pages_fetched=5)):
            WP.poll_once()
        st = WP.status()
        self.assertEqual(st["observed"], 0)
        self.assertEqual(st["inserted"], 0)
        self.assertEqual(st["provider_calls"], 5)
        self.assertEqual(st["last_result"], WP.POLL_OK)


# ── Deduplication and restart ────────────────────────────────────────────
class DeduplicationTests(PollerCase):

    def test_repeated_polling_reports_duplicates_rather_than_new_rows(self):
        """The dedup key lives in the existing store; this only surfaces it.
        No second cursor and no second persistence model is introduced."""
        first = _collect_result(observations=12, stored=12, duplicates=0)
        repeat = _collect_result(observations=12, stored=0, duplicates=12)
        with patch("lib.wallet_activity.collect_once",
                   side_effect=[first, repeat, repeat]):
            WP.poll_once()
            self.assertEqual(WP.status()["inserted"], 12)
            WP.poll_once()
            WP.poll_once()
        st = WP.status()
        self.assertEqual(st["inserted"], 0)
        self.assertEqual(st["deduplicated"], 12)
        self.assertEqual(st["polls_completed"], 3)

    def test_the_poller_introduces_no_cursor_or_persistence_of_its_own(self):
        src = (ROOT / "lib" / "wallet_poller.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
                for a in node.names:
                    imported.add(f"{node.module}.{a.name}")
            elif isinstance(node, ast.Import):
                for a in node.names:
                    imported.add(a.name)
        # It may reach the collector and nothing else that stores anything.
        for forbidden in ("app.database", "lib.event_store",
                          "lib.wallet_registry", "sqlite3",
                          "lib.api_cache"):
            self.assertNotIn(forbidden, imported,
                             f"the poller imports {forbidden}")
        self.assertIn("lib.wallet_activity", imported)

    def test_restarting_the_loop_does_not_re_collect_from_scratch(self):
        """Restart safety comes from dedup_key, not from poller state — so
        a fresh process re-polling an overlapping window inserts nothing."""
        repeat = _collect_result(observations=9, stored=0, duplicates=9)
        with patch("lib.wallet_activity.collect_once", return_value=repeat):
            WP.poll_once()
            WP._reset_for_tests()          # simulate a fresh process
            WP.poll_once()
        st = WP.status()
        self.assertEqual(st["inserted"], 0)
        self.assertEqual(st["deduplicated"], 9)


# ── The read-only boundary ───────────────────────────────────────────────
class ReadOnlyBoundaryTests(PollerCase):

    FORBIDDEN_MODULES = (
        "lib.paper_engine", "lib.dex_paper", "lib.dex_wallet",
        "lib.canonical_entry", "lib.canonical_exit", "lib.execution_venue",
        "lib.virtual_orders", "lib.canonical_settlement",
        "lib.settlement_ledger", "lib.alpaca_client", "lib.kraken_account",
        "lib.external_account", "jobs.execute_signals", "jobs.paper_trading",
        "lib.manual_trade_store", "lib.helius_client",
    )
    FORBIDDEN_NAMES = (
        "submit_order", "place_order", "close_position", "open_paper_position",
        "sendTransaction", "signTransaction", "Keypair", "execute_market",
        "fund_wallet", "settle_", "close_paper_position",
    )

    def test_the_poller_reaches_no_execution_or_account_surface(self):
        src = (ROOT / "lib" / "wallet_poller.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
            elif isinstance(node, ast.Import):
                for a in node.names:
                    imported.add(a.name)
        for mod in self.FORBIDDEN_MODULES:
            self.assertNotIn(mod, imported, f"the poller imports {mod}")
        for name in self.FORBIDDEN_NAMES:
            self.assertNotIn(name, src, f"the poller mentions {name}")

    def test_a_poll_moves_no_virtual_money(self):
        from app.database import (DexBalance, DexFundingEvent, DexPortfolio,
                                  DexPosition, DexTrade, PaperPortfolio,
                                  PaperPosition, PaperTrade, get_db)

        def snap():
            with get_db() as db:
                pf = db.query(PaperPortfolio).first()
                return {"cash": pf.cash if pf else None,
                        "realized_pnl": pf.realized_pnl if pf else None,
                        "paper_positions": db.query(PaperPosition).count(),
                        "paper_trades": db.query(PaperTrade).count(),
                        "dex_balances": db.query(DexBalance).count(),
                        "dex_funding": db.query(DexFundingEvent).count(),
                        "dex_portfolio": db.query(DexPortfolio).count(),
                        "dex_positions": db.query(DexPosition).count(),
                        "dex_trades": db.query(DexTrade).count()}

        before = snap()
        with patch("lib.wallet_activity.collect_once",
                   return_value=_collect_result(observations=7, stored=7)):
            out = WP.poll_once()
        self.assertTrue(out["ok"], out)
        self.assertEqual(snap(), before,
                         "a wallet poll moved the virtual economy")

    def test_the_loop_calls_only_the_existing_collector(self):
        src = (ROOT / "lib" / "wallet_poller.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        attr_calls = {
            f"{n.func.value.id}.{n.func.attr}"
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and isinstance(n.func.value, ast.Name)}
        provider_calls = {c for c in attr_calls
                          if c.startswith("wallet_activity.")}
        self.assertEqual(provider_calls,
                         {"wallet_activity.collect_once",
                          "wallet_activity.status"},
                         f"the poller calls more than the collector: "
                         f"{provider_calls}")


# ── Truthful, secret-free status ─────────────────────────────────────────
class StatusTests(PollerCase):

    def test_status_reports_every_required_field(self):
        for field in ("enabled", "running", "last_started_at",
                      "last_completed_at", "last_result", "next_run_at",
                      "observed", "inserted", "deduplicated",
                      "provider_calls", "last_error"):
            self.assertIn(field, WP.status(), field)

    def test_no_wallet_address_reaches_the_status_payload(self):
        """The collector prefixes its own errors with eight characters of a
        real wallet, so even a failure message leaks without redaction."""
        addr = "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9"
        result = _collect_result(
            errors=[f"{addr[:8]}…: 429 rate limited"],
            truncated_wallets=[f"{addr[:8]}…: max_pages_per_poll"])
        with patch("lib.wallet_activity.collect_once", return_value=result):
            WP.poll_once()
        blob = repr(WP.status())
        self.assertNotIn(addr, blob)
        self.assertNotIn(addr[:8], blob, "an address prefix leaked")
        # The useful half survives — the reason, never the account.
        self.assertIn("429", blob)
        self.assertIn("max_pages_per_poll", blob)

    def test_an_address_anywhere_in_an_error_is_scrubbed_not_just_the_prefix(self):
        """Removing the producer's own prefix is not enough on its own: a
        provider message can name the account mid-sentence."""
        addr = "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9"
        result = _collect_result(
            errors=[f"{addr[:8]}…: account {addr} was rate limited"])
        with patch("lib.wallet_activity.collect_once", return_value=result):
            WP.poll_once()
        blob = repr(WP.status())
        self.assertNotIn(addr, blob)
        self.assertNotIn(addr[:8], blob)
        self.assertIn("<wallet>", blob, "the mid-message address survived")
        self.assertIn("rate limited", blob)

    def test_no_credential_reaches_the_status_payload(self):
        fake = "FAKE-helius-key-ZZZZ9999"
        with patch.dict(os.environ, {"HELIUS_API_KEY": fake}, clear=False):
            blob = repr(WP.status())
        self.assertNotIn(fake, blob)

    def test_next_run_is_null_while_the_loop_is_not_running(self):
        self.assertFalse(WP.is_running())
        self.assertIsNone(WP.status()["next_run_at"])

    def test_status_reports_enabled_independently_of_running(self):
        with patch.dict(os.environ, OFF, clear=False):
            st = WP.status()
        self.assertFalse(st["enabled"])
        self.assertFalse(st["running"])


if __name__ == "__main__":
    unittest.main()
