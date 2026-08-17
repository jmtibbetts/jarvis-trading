"""Documentation that describes an obsolete architecture is not neutral.

It teaches a fresh install the wrong model and sends a future reader
looking for code that was deliberately removed. Several claims in this
repository outlived the systems they described:

    "shorts and leverage go to paper, ordinary longs go live"
    "empty HELIUS_WATCH_WALLETS means the collector stays inert"
    "discovery costs roughly two Helius RPC calls per token"
    "HELIUS_BACKFILL_LIMIT=500"  against a collector that fetched one page

Each was true once. Each then misled somebody. These tests pin the
corrections so the documents cannot quietly drift back — a doc test is
cheap, and the alternative is discovering the drift through a bug.
"""
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def read(rel: str) -> str:
    p = ROOT / rel
    return p.read_text(encoding="utf-8") if p.exists() else ""


class ReadmeStatesTheCurrentPhaseTests(unittest.TestCase):
    def test_it_declares_virtual_only(self):
        r = read("README.md")
        self.assertIn("VIRTUAL_ONLY", r)
        self.assertIn("NOT A LIVE-TRADING PLATFORM", r.upper())

    def test_it_names_kraken_as_the_target_venue(self):
        r = read("README.md")
        self.assertIn("Kraken Pro is the primary real-world target venue", r)

    def test_it_positions_alpaca_as_a_data_source(self):
        self.assertIn("market-data source", read("README.md"))

    def test_it_carries_the_golden_principle(self):
        """The rule the whole build is judged against."""
        self.assertIn("must never make money because the simulator is wrong",
                      read("README.md"))

    def test_the_superseded_framing_is_marked_as_history_not_fact(self):
        """The old 'parallel paper engine for shorts and leverage' line may
        appear ONLY as a historical note explaining what changed."""
        r = read("README.md")
        if "parallel paper trading engine" in r:
            idx = r.index("parallel paper trading engine")
            context = r[max(0, idx - 400):idx]
            self.assertIn("Historical note", context)


class ArchitectureStatesTheBoundaryTests(unittest.TestCase):
    def test_it_documents_the_platform_mode(self):
        a = read("ARCHITECTURE.md")
        self.assertIn("VIRTUAL_ONLY", a)
        self.assertIn("platform_mode", a)

    def test_it_documents_the_risk_reducing_asymmetry(self):
        """The non-obvious design choice most likely to be 'fixed' by a
        future reader who has not thought it through."""
        a = read("ARCHITECTURE.md")
        self.assertIn("opening exposure is gated, closing it is", a)

    def test_it_documents_the_three_gates(self):
        """Whitespace-normalised: prose wraps, and a line break between
        two words must not read as a missing claim."""
        import re
        a = re.sub(r"\s+", " ", read("ARCHITECTURE.md").replace("*", ""))
        self.assertIn("execution_venue", a)
        for gate in ("platform mode", "venue capability", "plan-versus-risk"):
            self.assertIn(gate, a, gate)

    def test_it_records_that_capability_is_discovered(self):
        a = read("ARCHITECTURE.md")
        self.assertIn("discovered, never assumed", a)
        self.assertIn("UI_ONLY", a)


class PlanStatusExistsTests(unittest.TestCase):
    def test_the_status_document_is_present(self):
        self.assertTrue(read("docs/PLAN_STATUS.md"),
                        "docs/PLAN_STATUS.md is missing")

    def test_every_plan_carries_a_status(self):
        s = read("docs/PLAN_STATUS.md")
        for doc in ("HARDENING_PLAN.md", "UI_AUDIT.md", "UPGRADE_PLAN.md",
                    "DATA_PLATFORM_PLAN.md"):
            self.assertIn(doc, s, doc)

    def test_it_uses_the_declared_vocabulary(self):
        s = read("docs/PLAN_STATUS.md")
        for word in ("DONE", "PARTIAL", "OPEN", "SUPERSEDED"):
            self.assertIn(word, s, word)

    def test_it_carries_the_known_open_items_forward(self):
        """Open items in a commit message are lost; open items in a
        document are inherited."""
        s = read("docs/PLAN_STATUS.md")
        self.assertIn("6J=F", s)
        self.assertIn("LEGACY_UNATTRIBUTED", s)
        self.assertIn("commit SHAs", s)


class EnvExampleTeachesTheCurrentModelTests(unittest.TestCase):
    def test_it_declares_the_platform_mode(self):
        e = read(".env.example")
        self.assertIn("JARVIS_PLATFORM_MODE", e)
        self.assertIn("VIRTUAL_ONLY", e)

    def test_the_wallet_universe_is_the_database(self):
        e = read(".env.example")
        self.assertIn("SEED INPUT ONLY", e.upper())
        self.assertNotIn("Empty means the collector stays inert", e)

    def test_the_page_limit_states_the_api_maximum(self):
        """It allowed 1000 against an endpoint that serves 100."""
        e = read(".env.example")
        self.assertIn("at most 100 per page", e)

    def test_the_backfill_limit_is_described_as_a_budget(self):
        self.assertIn("a budget, not a page size", read(".env.example"))


class CiIsAReleaseGateTests(unittest.TestCase):
    def test_the_frontend_is_built_in_ci(self):
        """A green backend check while the UI fails to compile is a green
        check on half the product."""
        ci = read(".github/workflows/ci.yml")
        self.assertIn("npm run check", ci)
        self.assertIn("npm run build", ci)

    def test_windows_is_tested(self):
        """The operator's deployment IS Windows."""
        self.assertIn("windows-latest", read(".github/workflows/ci.yml"))

    def test_permissions_are_least_privilege(self):
        """Comments stripped before searching. `contents: write` appears in
        the header comment explaining what was REMOVED, and a naive search
        matches the documentation rather than the configuration — the same
        trap the learning-engine guard hit.

        Deliberately not using PyYAML: it is not in requirements.txt, so a
        yaml-based assertion passes locally on a transitive dependency and
        fails in CI, where only requirements.txt is installed. A test that
        only works on the author's machine is worse than no test.
        """
        lines = [ln for ln in read(".github/workflows/ci.yml").splitlines()
                 if not ln.lstrip().startswith("#")]
        config = chr(10).join(lines)
        self.assertIn("contents: read", config)
        self.assertNotIn("contents: write", config)

    def test_raw_test_output_is_not_published_publicly(self):
        """Test output can carry provider responses, request context,
        filesystem paths and environment fragments."""
        ci = read(".github/workflows/ci.yml")
        self.assertNotIn("createCommitComment", ci)
        self.assertIn("upload-artifact", ci)

    def test_the_bootstrap_path_is_exercised(self):
        ci = read(".github/workflows/ci.yml")
        self.assertIn("init_db", ci)
        self.assertIn("idempotent", ci)

    def test_ci_runs_in_virtual_only(self):
        """The platform boundary must hold in CI exactly as on the desk."""
        self.assertIn('JARVIS_PLATFORM_MODE: "VIRTUAL_ONLY"',
                      read(".github/workflows/ci.yml"))


if __name__ == "__main__":
    unittest.main()
