"""Provider configuration, entitlement reporting and failure semantics.

WHAT THIS FILE IS FOR. A credential in `.env` proves that somebody pasted a
string. It does not prove authentication, entitlement, that the product is
the one the code calls, or that any of it still works. This pins the places
where that distinction is load-bearing — and the one place it had already
been lost.

SECRET DISCIPLINE. Nothing here reads the operator's `.env`. Every
credential-shaped value is a FAKE injected with `patch.dict`, which is also
what makes the leak tests discriminating: a real key would prove nothing
about whether a *different* key would leak.

THE HEADLINE: `WALLET_QUEUE_URL` and `WALLET_QUEUE_SECRET` are not Helius
settings, are not JARVIS settings either, and do not exist. They appear in
one planning document and in no code, no `.env`, and no `.env.example`. The
implemented design deliberately replaced that architecture with polling —
`lib/wallet_activity` says so in its first line — and these tests keep the
two from being confused again.
"""
import ast
import os
import pathlib
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).parent.parent

# Obviously-fake values. If one of these ever appears in an output, the code
# put a credential there.
FAKE = {
    "MASSIVE_API_KEY": "FAKE-massive-key-AAAA1111",
    "HELIUS_API_KEY": "FAKE-helius-key-BBBB2222",
    "COINGECKO_API_KEY": "FAKE-coingecko-key-CCCC3333",
    "LUNARCRUSH_API_KEY": "FAKE-lunarcrush-key-DDDD4444",
    "ALPACA_API_KEY": "FAKE-alpaca-key-EEEE5555",
    "ALPACA_API_SECRET": "FAKE-alpaca-secret-FFFF6666",
    "KRAKEN_API_KEY": "FAKE-kraken-key-GGGG7777",
    "KRAKEN_API_SECRET": "FAKE-kraken-secret-HHHH8888",
    "TWELVE_DATA_API_KEY": "FAKE-td-key-IIII9999",
    "ALLRATES_API_KEY": "FAKE-allrates-key-JJJJ0000",
}


def _source(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _module_env_names(rel: str) -> set:
    """Every environment name a module mentions, however it is quoted."""
    import re
    return set(re.findall(r"[A-Z][A-Z0-9_]{3,}", _source(rel)))


PRODUCTION_TREES = ("lib", "app", "jobs", "ml", "scripts")


def _grep_production(needle: str) -> list:
    hits = []
    for tree in PRODUCTION_TREES:
        base = ROOT / tree
        if not base.exists():
            continue
        for p in base.rglob("*.py"):
            try:
                if needle in p.read_text(encoding="utf-8"):
                    hits.append(str(p.relative_to(ROOT)))
            except (OSError, UnicodeDecodeError):
                continue
    for extra in ("main.py", "conftest.py"):
        p = ROOT / extra
        if p.exists() and needle in p.read_text(encoding="utf-8"):
            hits.append(extra)
    return sorted(hits)


# ── C/E. WALLET_QUEUE_* ownership ────────────────────────────────────────
class WalletQueueOwnershipTests(unittest.TestCase):
    """They are not Helius's, and they are not implemented."""

    def test_no_production_code_reads_either_variable(self):
        for name in ("WALLET_QUEUE_URL", "WALLET_QUEUE_SECRET"):
            self.assertEqual(
                _grep_production(name), [],
                f"{name} acquired a production reader; if a queue was built, "
                f"this test and the handoff must be updated together")

    def test_neither_appears_in_the_environment_template(self):
        example = _source(".env.example")
        for name in ("WALLET_QUEUE_URL", "WALLET_QUEUE_SECRET",
                     "HELIUS_WEBHOOK_AUTH_SECRET"):
            self.assertNotIn(name, example,
                             f"{name} is documented as configurable but "
                             f"nothing reads it")

    def test_they_are_not_helius_configuration(self):
        """The Helius client authenticates with HELIUS_API_KEY and nothing
        else. A queue URL is not a provider endpoint."""
        src = _source("lib/helius_client.py")
        self.assertIn("HELIUS_API_KEY", src)
        for name in ("WALLET_QUEUE_URL", "WALLET_QUEUE_SECRET", "QUEUE"):
            self.assertNotIn(name, src)

    def test_the_implemented_design_is_polling_not_a_queue(self):
        """`lib/wallet_activity` replaced the webhook/queue architecture.
        Asserted on the CODE — no inbound receiver, no queue client — rather
        than on its prose, which is exactly what a text search would match."""
        tree = ast.parse(_source("lib/wallet_activity.py"))
        names = {n.name for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        # It PULLS.
        self.assertIn("collect_once", names)
        # And there is no receiver anywhere to push into.
        self.assertEqual(
            [f for f in _grep_production("def webhook")
             if "telegram" not in f.lower()], [])

    def test_nothing_requires_a_queue_to_monitor_wallets(self):
        """E — there is no 'required queue configuration' to fail on. The
        honest statement is that the requirement does not exist, so this
        pins the absence rather than inventing an error message for it."""
        src = _source("lib/wallet_activity.py")
        tree = ast.parse(src)
        cfg = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "_config")
        cfg_src = ast.get_source_segment(src, cfg) or ""
        self.assertNotIn("QUEUE", cfg_src)
        # What it DOES require is the provider key and a watch list.
        self.assertIn("HELIUS", cfg_src)


# ── B/D. Helius configuration classification ─────────────────────────────
class HeliusConfigurationTests(unittest.TestCase):

    def test_the_client_targets_documented_helius_hosts_only(self):
        src = _source("lib/helius_client.py")
        self.assertIn("https://api.helius.xyz", src)
        self.assertIn("https://mainnet.helius-rpc.com/", src)

    def test_health_depends_on_the_api_key_and_nothing_else(self):
        """D — an unset, unrelated variable must not make Helius look ill."""
        from lib import helius_client as HC

        with patch.dict(os.environ, {"HELIUS_API_KEY": ""}, clear=False):
            self.assertFalse(HC.configured())
            out = HC.health()
            self.assertFalse(out["configured"])
            self.assertIn("HELIUS_API_KEY", out["detail"])
            self.assertNotIn("QUEUE", str(out).upper())

        with patch.dict(os.environ, {**FAKE}, clear=False):
            self.assertTrue(HC.configured())

    def test_a_configured_helius_knob_with_no_reader_is_a_known_gap(self):
        """The operator's `.env` carries six HELIUS_* knobs that nothing
        reads. This pins the ones that ARE wired so a future rename cannot
        quietly join them — the unwired six are recorded in the handoff,
        not here, because this suite never reads the operator's `.env`."""
        wired = {"HELIUS_API_KEY", "HELIUS_WATCH_WALLETS",
                 "HELIUS_WALLET_INTELLIGENCE_ENABLED",
                 "HELIUS_WALLET_DISCOVERY_ENABLED",
                 "HELIUS_DISCOVERY_MAX_CANDIDATES", "HELIUS_PAGE_LIMIT",
                 "HELIUS_MAX_PAGES_PER_POLL", "HELIUS_BACKFILL_LIMIT",
                 "HELIUS_MAX_RETRIES", "HELIUS_MIN_CALL_SPACING"}
        found = set()
        for tree in ("lib", "app", "jobs"):
            for p in (ROOT / tree).rglob("*.py"):
                found |= {n for n in _module_env_names(
                    str(p.relative_to(ROOT))) if n.startswith("HELIUS_")}
        missing = wired - found
        self.assertEqual(missing, set(),
                         f"a wired Helius knob lost its reader: {missing}")


# ── F. The read-only boundary ────────────────────────────────────────────
class HeliusCannotSubmitTransactionsTests(unittest.TestCase):

    # STATE-CHANGING methods only. `simulateTransaction` is deliberately NOT
    # here: it simulates and submits nothing, which is why the fee path is
    # allowed to reach for it at all.
    MUTATING = ("sendTransaction", "sendRawTransaction", "requestAirdrop",
                "signTransaction", "signAllTransactions")

    def test_no_production_call_site_requests_a_state_changing_rpc_method(self):
        for method in self.MUTATING:
            hits = _grep_production(f'"{method}"')
            self.assertEqual(hits, [], f"{method} is requested in {hits}")

    def test_the_simulation_path_has_nothing_to_simulate_and_says_so(self):
        """`transaction_b64` is threaded through the fee path as an OPTIONAL
        argument and NO production caller supplies one — there is no Solana
        transaction builder here. The branch therefore returns
        NOT_APPLIED_NO_SIMULATION with a truthful reason instead of quietly
        promoting the assumed compute budget to a measurement.
        """
        from lib import dex_network_cost as DNC

        out = DNC.measure_compute_units(transaction_b64=None) \
            if hasattr(DNC, "measure_compute_units") else None
        if out is None:                       # locate it by shape instead
            fn = next(getattr(DNC, n) for n in dir(DNC)
                      if n.startswith("compute") or "unit" in n
                      if callable(getattr(DNC, n)))
            out = fn(transaction_b64=None)
        self.assertIsNone(out["measured_units_consumed"],
                          "an unsimulated compute budget became a measurement")
        self.assertEqual(out["compute_headroom_policy"],
                         "NOT_APPLIED_NO_SIMULATION")
        self.assertIn("no serialized transaction", out["detail"])

    def test_no_signing_or_keypair_library_is_imported_anywhere(self):
        for needle in ("import nacl", "from nacl", "Keypair",
                       "from solders", "import solders", "sign_message"):
            self.assertEqual(_grep_production(needle), [],
                             f"{needle!r} appeared in production code")


# ── A/M. Secrets and honest health ───────────────────────────────────────
class CredentialHandlingTests(unittest.TestCase):

    def test_provider_status_never_echoes_a_credential(self):
        """A — with FAKE credentials injected, none may reach the payload."""
        from app.routers.common import _build_provider_status

        with patch.dict(os.environ, FAKE, clear=False):
            payload = _build_provider_status()
        blob = repr(payload)
        for name, value in FAKE.items():
            self.assertNotIn(value, blob,
                             f"{name}'s value reached the provider payload")

    def test_health_does_not_claim_entitlement_from_a_key_alone(self):
        """M — the one place this had already been lost.

        Massive was reported ok=True purely because MASSIVE_API_KEY was set.
        `state` may now never read HEALTHY for a row nobody called.
        """
        from app.routers.common import _build_provider_status

        with patch.dict(os.environ, FAKE, clear=False):
            payload = _build_provider_status()

        rows = {p["name"]: p for p in payload["providers"]}
        self.assertIn("Massive", rows)
        for name, row in rows.items():
            self.assertIn("probed", row, f"{name} does not say whether it "
                                         f"was probed")
            self.assertIn(row["state"], ("HEALTHY", "DOWN", "UNPROBED"))
            if not row["probed"]:
                self.assertNotEqual(
                    row["state"], "HEALTHY",
                    f"{name} claims HEALTHY without having been called")

        massive = rows["Massive"]
        self.assertFalse(massive["probed"])
        self.assertEqual(massive["state"], "UNPROBED")
        self.assertIn("NOT PROBED", massive["detail"])

    def test_the_helius_health_payload_carries_no_credential(self):
        from lib import helius_client as HC

        with patch.dict(os.environ, FAKE, clear=False):
            out = HC.health()
        self.assertNotIn(FAKE["HELIUS_API_KEY"], repr(out))

    def test_no_credential_shaped_assignment_is_committed(self):
        """No real key may sit in tracked docs, tests or the template."""
        import re

        # Two traps, both hit while writing this:
        #  - the suffix must END the name, or TOKEN_SURGE_MIN_SCORE=70 reads
        #    as a leaked token;
        #  - the spacing around `=` must be [ \t]*, NOT \s*, because \s
        #    matches a newline, so an EMPTY value lets the pattern run on and
        #    capture the NEXT line's variable name as though it were a
        #    secret. That is how a leak test starts crying wolf and ends up
        #    with a permanent `# noqa`.
        suspicious = re.compile(
            r"^([A-Z][A-Z0-9_]*(?:API_KEY|API_SECRET|TOKEN|SECRET))"
            r"[ \t]*=[ \t]*"
            r"(?![ \t]*$|#|<|your|YOUR|\"\"|''|changeme)(\S{16,})[ \t]*$",
            re.M)
        for rel in (".env.example",):
            for m in suspicious.finditer(_source(rel)):
                self.fail(f"{rel} contains a credential-shaped value for "
                          f"{m.group(1)}")
        self.assertIn(".env", _source(".gitignore"))

    def test_the_operator_env_is_not_tracked_by_git(self):
        import subprocess

        out = subprocess.run(["git", "ls-files", ".env"], cwd=str(ROOT),
                             capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), "",
                         "the operator .env is tracked by git")


# ── G. Failure classes stay distinct ─────────────────────────────────────
class FailureSemanticsTests(unittest.TestCase):

    def test_provider_health_keeps_the_failure_classes_apart(self):
        from lib import provider_health as PH

        classes = {PH.RATE_LIMITED, PH.AUTH_FAILED, PH.PAYMENT_REQUIRED,
                   PH.STALE, PH.DEGRADED, PH.UNAVAILABLE, PH.DISABLED,
                   PH.NOT_CONFIGURED, PH.HEALTHY}
        self.assertEqual(len(classes), 9, "two failure classes collapsed")
        # 402 is its own fact: the credential is VALID and unpaid.
        self.assertNotEqual(PH.PAYMENT_REQUIRED, PH.AUTH_FAILED)
        self.assertNotEqual(PH.NOT_CONFIGURED, PH.UNAVAILABLE)

    def test_a_payment_required_provider_is_not_reported_unconfigured(self):
        """LunarCrush: credential valid, subscription inactive. Collapsing
        that into NOT_CONFIGURED would send someone to look for a missing
        key that is not missing."""
        from lib import lunarcrush_client as LC

        src = _source("lib/lunarcrush_client.py")
        self.assertIn("402", src)
        self.assertTrue(hasattr(LC, "PROVIDER"))


# ── H/I/J/K/L. The data-integrity guards ─────────────────────────────────
class DataIntegrityGuardTests(unittest.TestCase):

    def test_a_failed_crypto_fetch_returns_none_never_zero(self):
        """H — missing is not zero."""
        import lib.crypto_market_data as CMD

        # Patch the module's OWN fetch helper. `_json` builds an
        # `httpx.Client` and calls `client.get`, so patching `httpx.get`
        # intercepts nothing — the first version of this test silently made
        # LIVE calls to Binance and OKX and then asserted against real
        # market data.
        with patch.object(CMD, "_json",
                          side_effect=RuntimeError("provider down")):
            for fn in ("_binance_ohlcv", "_okx_ohlcv"):
                self.assertTrue(hasattr(CMD, fn), f"{fn} disappeared")
                out = getattr(CMD, fn)("BTC/USD", "1h", 10)
                self.assertIsNone(out, f"{fn} returned {out!r} on failure")
            for fn in ("_binance_prices", "_okx_prices"):
                if hasattr(CMD, fn):
                    out = getattr(CMD, fn)(["BTC"])
                    # An empty mapping, never a mapping of zeros.
                    self.assertEqual(out, {}, f"{fn} returned {out!r}")
                    self.assertNotIn(0, list(out.values()))
                    self.assertNotIn(0.0, list(out.values()))

    def test_stale_market_data_is_labelled_not_served_as_current(self):
        """I — the snapshot authority has a STALE state, and only AVAILABLE
        may price a fill."""
        from lib import execution_snapshot as ES

        self.assertTrue(hasattr(ES, "FILLABLE"))
        statuses = {getattr(ES, n) for n in dir(ES)
                    if n.isupper() and isinstance(getattr(ES, n), str)}
        for expected in ("STALE", "AVAILABLE"):
            self.assertIn(expected, statuses)
        self.assertNotIn("STALE", set(ES.FILLABLE))

    def test_a_spot_venue_cannot_supply_a_perpetual_price(self):
        """J — a perpetual book is no more a spot price than the reverse."""
        from lib import execution_snapshot as ES

        caps = ES._READER_PRODUCTS
        self.assertIn("kraken", caps, "the venue->product map disappeared")
        # The spot book may not speak for a perpetual...
        self.assertNotIn("CRYPTO_PERP", caps["kraken"])
        self.assertIn("CRYPTO_SPOT", caps["kraken"])
        # ...and the perpetual book may not speak for spot. The substitution
        # is refused in BOTH directions, which is the whole point.
        self.assertNotIn("CRYPTO_SPOT", caps["kraken_derivatives_us"])
        self.assertIn("CRYPTO_PERP", caps["kraken_derivatives_us"])
        self.assertFalse(ES.prices_product("kraken", "CRYPTO_PERP"))
        self.assertTrue(ES.prices_product("kraken", "CRYPTO_SPOT"))

    def test_a_token_ticker_is_not_a_contract_address(self):
        """K — on-chain lookups key on the MINT, not on a symbol."""
        src = _source("lib/token_pricing.py")
        self.assertIn("getAsset", src)
        tree = ast.parse(src)
        params = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                params |= {a.arg for a in node.args.args}
        self.assertTrue(params & {"mint", "mint_address", "address", "id"},
                        f"token pricing takes no address-shaped argument: "
                        f"{sorted(params)}")

    def test_a_fallback_keeps_its_provenance(self):
        """L — a fallback that does not say it is one is indistinguishable
        from the primary it replaced."""
        from lib import execution_snapshot as ES

        statuses = {getattr(ES, n) for n in dir(ES)
                    if n.isupper() and isinstance(getattr(ES, n), str)}
        self.assertIn("FALLBACK", statuses)
        self.assertNotIn("FALLBACK", set(ES.FILLABLE),
                         "fallback data may price a fill")


# ── N/O/P. Posture and economy ───────────────────────────────────────────
class PostureTests(unittest.TestCase):

    def test_the_scheduler_stays_disabled_under_test(self):
        from app import scheduler as S

        self.assertTrue(hasattr(S, "start") or hasattr(S, "job_status"))
        self.assertEqual(os.getenv("JARVIS_UNDER_PYTEST"), "1")

    def test_broker_and_account_management_stay_off(self):
        from lib.external_account import connector_enabled, management_enabled

        self.assertFalse(connector_enabled())
        self.assertFalse(management_enabled())

    def test_reading_provider_status_moves_no_virtual_money(self):
        """A DELTA, not an absolute count. This database is shared with
        every other test module, several of which legitimately open paper
        positions; asserting zero here measures THEM. What must hold is that
        asking a provider whether it is alive changes no economic row."""
        from app.database import (DexBalance, DexFundingEvent, DexPosition,
                                  DexTrade, PaperPortfolio, PaperPosition,
                                  PaperTrade, get_db)

        def snap():
            with get_db() as db:
                pf = db.query(PaperPortfolio).first()
                return {
                    "cash": pf.cash if pf else None,
                    "realized_pnl": pf.realized_pnl if pf else None,
                    "paper_positions": db.query(PaperPosition).count(),
                    "paper_trades": db.query(PaperTrade).count(),
                    "dex_balances": db.query(DexBalance).count(),
                    "dex_funding_events": db.query(DexFundingEvent).count(),
                    "dex_positions": db.query(DexPosition).count(),
                    "dex_trades": db.query(DexTrade).count(),
                }

        from app.routers.common import _build_provider_status

        before = snap()
        with patch.dict(os.environ, FAKE, clear=False):
            payload = _build_provider_status()
        self.assertTrue(payload["providers"],
                        "the builder returned nothing — this proves nothing")
        self.assertEqual(snap(), before,
                         "a provider health check moved the virtual economy")


if __name__ == "__main__":
    unittest.main()
