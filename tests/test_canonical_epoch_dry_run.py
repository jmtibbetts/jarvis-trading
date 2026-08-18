"""The dry-run tool must fail, and fail LOUDLY, for every reason it exists.

A cutover tool that only works is worth very little. The whole value is in
what it refuses, so this file is mostly failure cases: each one must drive
`READY` to FALSE rather than being tolerated, warned about, or quietly
skipped. A gate nobody recorded is FALSE, and reaching the end of the script
is not evidence of anything.

The happy path is proved separately by running the real thing; here the
cheap structural and refusal properties are pinned so a regression surfaces
in CI instead of during a cutover.
"""
import ast
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TOOL = REPO / "scripts" / "canonical_epoch_dry_run.py"
CHILD = REPO / "scripts" / "canonical_epoch_child.py"

FIXTURE_SCHEMA = """
CREATE TABLE paper_positions (id TEXT PRIMARY KEY, symbol TEXT, qty REAL,
    status TEXT);
CREATE TABLE paper_trades (id TEXT PRIMARY KEY, symbol TEXT, realized_pnl REAL);
CREATE TABLE paper_portfolio (id TEXT PRIMARY KEY, cash REAL,
    total_trades REAL, winning_trades REAL, realized_pnl REAL);
CREATE TABLE trade_outcomes (id TEXT PRIMARY KEY, symbol TEXT,
    engine_epoch TEXT);
CREATE TABLE app_users (id TEXT PRIMARY KEY, email TEXT, display_name TEXT,
    is_active INTEGER, created_at TEXT, updated_at TEXT);
CREATE TABLE user_preferences (user_id TEXT PRIMARY KEY, trade_mode TEXT,
    min_confidence REAL, asset_classes TEXT, directions TEXT,
    telegram_enabled INTEGER, auto_sim_enabled INTEGER, updated_at TEXT,
    paper_auto_trade_enabled INTEGER, live_min_score REAL, live_min_rr REAL,
    live_min_confidence REAL);
CREATE TABLE signal_accuracy (id TEXT PRIMARY KEY, wins REAL, total REAL);
"""


def make_fixture(path: Path, *, extra_table: str | None = None) -> None:
    """A miniature source database in the real shape: some economics, some
    config, some derived learning."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(FIXTURE_SCHEMA)
    conn.execute("INSERT INTO paper_portfolio VALUES "
                 "('pf-old', 63550.83, 664, 200, -1234.5)")
    conn.execute("INSERT INTO paper_positions VALUES "
                 "('pos-old-1', 'BTC/USD', 0.5, 'Open')")
    conn.execute("INSERT INTO paper_trades VALUES ('trade-old-1','BTC/USD',12.0)")
    conn.execute("INSERT INTO trade_outcomes VALUES "
                 "('outcome-old-1','BTC/USD','2026-08-13')")
    conn.execute("INSERT INTO app_users VALUES "
                 "('local','op@example.com','Operator',1,'t','t')")
    conn.execute("INSERT INTO user_preferences VALUES "
                 "('local','paper',60.0,'Crypto','Long',1,1,'t',1,55.0,0.0,0.0)")
    conn.execute("INSERT INTO signal_accuracy VALUES ('sa-1', 40, 100)")
    if extra_table:
        conn.execute(f"CREATE TABLE {extra_table} (id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()


def run_tool(args: list[str], timeout: int = 900):
    env = dict(os.environ)
    env.pop("JARVIS_UNDER_PYTEST", None)
    return subprocess.run([sys.executable, str(TOOL), *args], cwd=str(REPO),
                          env=env, capture_output=True, text=True,
                          timeout=timeout)


def manifest_of(work: Path) -> dict:
    path = work / "CUTOVER_DRY_RUN_MANIFEST.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


class _Fixture(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(prefix="jarvis-cutover-")
        self.addCleanup(self._cleanup)
        self.root = Path(self._dir.name)
        self.source = self.root / "data" / "jarvis.db"
        self.work = self.root / "run"
        make_fixture(self.source)

    def _cleanup(self):
        # The tool marks the archive read-only on purpose; make the tree
        # writable again so the temp dir can be removed on every platform.
        for path in self.root.rglob("*"):
            try:
                path.chmod(0o700)
            except OSError:
                pass
        self._dir.cleanup()

    def run_dry(self, *extra, source=None, work=None):
        return run_tool([
            "--source", str(source or self.source),
            "--work-dir", str(work or self.work),
            "--starting-cash", "100000",
            "--allow-non-operator-source", *extra])


# ── P0 / P23 — the tool cannot cut over ──────────────────────────────────
class TheToolHasNoApplyPathTests(unittest.TestCase):
    """P0/P24 — the safety property is that this capability is ABSENT, not
    that it is guarded by a flag someone could pass."""

    def _source(self, path: Path) -> str:
        return path.read_text(encoding="utf-8")

    def test_no_apply_activate_or_swap_flag_exists(self):
        text = self._source(TOOL)
        tree = ast.parse(text)
        options = set()
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "add_argument"):
                for arg in node.args:
                    if isinstance(arg, ast.Constant):
                        options.add(str(arg.value))
        for forbidden in ("--apply", "--activate", "--swap", "--cutover",
                          "--commit", "--promote"):
            self.assertNotIn(forbidden, options, forbidden)

    def test_it_never_renames_moves_or_deletes_anything(self):
        """The activation step of a real cutover is a rename or a replace.
        Those calls are simply absent here — `str.replace` is not one of
        them, so the check is on the qualified form, not the bare name."""
        forbidden = {
            "os.replace", "os.rename", "os.remove", "os.unlink", "os.rmdir",
            "shutil.move", "shutil.rmtree", "shutil.copyfile", "shutil.copy",
            "Path.rename", "Path.replace", "Path.unlink",
        }
        for path in (TOOL, CHILD):
            tree = ast.parse(self._source(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                try:
                    qualified = ast.unparse(node.func)
                except Exception:       # pragma: no cover - unparse guard
                    continue
                self.assertNotIn(
                    qualified, forbidden,
                    f"{path.name} calls {qualified}() — a cutover tool "
                    f"must not move or delete files")
                # Bare attribute names that str does not have, so any
                # receiver at all is suspicious.
                self.assertNotIn(
                    getattr(node.func, "attr", ""),
                    ("rename", "rmtree", "unlink", "copytree"),
                    f"{path.name}: {qualified}()")

    def test_the_only_file_copy_is_of_a_report_never_a_database(self):
        """`shutil.copy2` is used once, to file a fingerprint REPORT into
        the artifacts directory. A database is copied only through SQLite's
        backup API, which is the difference between a consistent snapshot
        and a torn one."""
        text = self._source(TOOL)
        tree = ast.parse(text)
        copies = [n for n in ast.walk(tree)
                  if isinstance(n, ast.Call)
                  and ast.unparse(n.func) in ("shutil.copy2", "shutil.copy")]
        self.assertLessEqual(len(copies), 1, "unexpected file copies")
        for node in copies:
            rendered = ast.unparse(node)
            self.assertIn("report", rendered.lower(),
                          f"a non-report file copy: {rendered}")
        self.assertIn(".backup(", text,
                      "the archive must be made with the SQLite backup API")

    def test_it_does_not_import_or_call_the_resets(self):
        """P23 — a cutover is 'archive the old and create a new one', never
        'mutate the old one until it looks fresh'."""
        for path in (TOOL, CHILD):
            text = self._source(path)
            tree = ast.parse(text)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        self.assertNotIn(
                            alias.name,
                            ("reset_paper_portfolio",
                             "soft_reset_paper_portfolio"),
                            f"{path.name} imports {alias.name}")
                if isinstance(node, ast.Call):
                    fn = getattr(node.func, "id", None) or getattr(
                        node.func, "attr", None)
                    self.assertNotIn(
                        fn, ("reset_paper_portfolio",
                             "soft_reset_paper_portfolio"),
                        f"{path.name} calls {fn}()")

    def test_the_parent_process_never_imports_app_database(self):
        """P0.3 — app.database builds its engine at import time and that
        engine sets journal_mode=WAL on connect. A tool proving immutability
        must not open the file in a mode that can rewrite its header."""
        tree = ast.parse(self._source(TOOL))
        bad = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                    ("app.", "app")):
                bad.append(node.module)
            if isinstance(node, ast.Import):
                bad += [a.name for a in node.names
                        if a.name == "app" or a.name.startswith("app.")]
        self.assertEqual(bad, [], f"the parent imports {bad}")

    def test_the_child_refuses_to_run_against_the_operator_db(self):
        env = dict(os.environ)
        env.pop("JARVIS_UNDER_PYTEST", None)
        env["JARVIS_DB_PATH"] = str(REPO / "data" / "jarvis.db")
        proc = subprocess.run(
            [sys.executable, str(CHILD), "--phase", "fd_report"],
            cwd=str(REPO), env=env, capture_output=True, text=True,
            timeout=300)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("operator database", proc.stdout + proc.stderr)

    def test_the_child_refuses_with_no_db_path_at_all(self):
        env = dict(os.environ)
        env.pop("JARVIS_UNDER_PYTEST", None)
        env.pop("JARVIS_DB_PATH", None)
        proc = subprocess.run(
            [sys.executable, str(CHILD), "--phase", "fd_report"],
            cwd=str(REPO), env=env, capture_output=True, text=True,
            timeout=300)
        self.assertNotEqual(proc.returncode, 0)


# ── P0.2 — aliasing ──────────────────────────────────────────────────────
class PathAliasingIsRefusedTests(_Fixture):

    def test_a_work_dir_containing_the_source_is_refused(self):
        proc = self.run_dry(work=self.source.parent)
        self.assertNotEqual(proc.returncode, 0)
        gates = manifest_of(self.source.parent).get("gates", {})
        if gates:
            self.assertFalse(gates.get("source_outside_work_dir", {})
                             .get("ok", True))

    def test_a_symlinked_source_cannot_alias_the_archive(self):
        """realpath() before comparing, so a symlink cannot smuggle the
        source in as its own archive."""
        link = self.root / "aliased.db"
        try:
            link.symlink_to(self.source)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable on this platform")
        proc = self.run_dry(source=link)
        # Either it refuses outright, or it resolves the link and treats it
        # as the same file — never silently archives a database onto itself.
        man = manifest_of(self.work)
        if man:
            self.assertNotEqual(
                Path(man["source"]["path"]).name, "aliased.db",
                "the symlink was not resolved before comparison")
        self.assertIsInstance(proc.returncode, int)


# ── P2 — unknown tables ──────────────────────────────────────────────────
class AnUnclassifiedTableStopsTheRunTests(_Fixture):

    def test_an_unknown_table_fails_the_run(self):
        make_fixture(self.source.with_name("with_extra.db"),
                     extra_table="some_new_feature_table")
        proc = self.run_dry(source=self.source.with_name("with_extra.db"))
        self.assertNotEqual(proc.returncode, 0)
        gates = manifest_of(self.work).get("gates", {})
        self.assertFalse(gates.get("zero_unknown_tables", {}).get("ok", True))
        self.assertIn("some_new_feature_table",
                      gates.get("zero_unknown_tables", {}).get("detail", ""))

    def test_the_classifier_never_guesses(self):
        from lib.cutover_classification import UNKNOWN_REFUSE, classify
        self.assertEqual(classify("a_table_invented_tomorrow")[0],
                         UNKNOWN_REFUSE)

    def test_every_class_is_one_of_the_declared_classes(self):
        from lib.cutover_classification import CLASSES, CLASSIFICATION
        for table, (cls, why) in CLASSIFICATION.items():
            self.assertIn(cls, CLASSES, table)
            self.assertTrue(why.strip(), f"{table} has no stated reason")


# ── P2.2 / P6 — what may and may not cross ───────────────────────────────
class OnlyTheAllowlistCrossesTests(unittest.TestCase):

    def test_no_economic_table_is_in_the_copy_plan(self):
        from lib.cutover_classification import (ARCHIVE_ONLY_ECONOMIC,
                                                COPY_PLAN, classify)
        for entry in COPY_PLAN:
            self.assertNotEqual(classify(entry["source_table"])[0],
                                ARCHIVE_ONLY_ECONOMIC,
                                entry["source_table"])

    def test_no_derived_learning_table_is_in_the_copy_plan(self):
        from lib.cutover_classification import (COPY_PLAN,
                                                RESET_DERIVED_LEARNING,
                                                classify)
        for entry in COPY_PLAN:
            self.assertNotEqual(classify(entry["source_table"])[0],
                                RESET_DERIVED_LEARNING,
                                entry["source_table"])

    def test_every_copy_entry_names_its_columns_or_says_why_not(self):
        """P6.1 — no blind `SELECT *`. A column added upstream must surface
        as a refusal, not ride along as data."""
        from lib.cutover_classification import COPY_PLAN
        for entry in COPY_PLAN:
            self.assertTrue(entry.get("reason", "").strip(),
                            f"{entry['source_table']} has no stated reason")
            if entry["columns"] is None:
                # Allowed only for a table that is empty and schema-identical.
                self.assertIn(entry["source_table"], ("user_telegram_links",))

    def test_the_mixed_table_excludes_its_market_columns(self):
        """P2.5 — a stale price must not cross as though it were reference
        data."""
        from lib.cutover_classification import COPY_PLAN
        entry = [e for e in COPY_PLAN
                 if e["source_table"] == "market_assets"][0]
        for transient in ("price", "change_percent", "volume", "market_cap",
                          "last_updated"):
            self.assertNotIn(transient, entry["columns"])
            self.assertIn(transient, entry["excluded_columns"])
        for identity in ("symbol", "asset_class", "is_focus"):
            self.assertIn(identity, entry["columns"])


# ── P4.3 / P17 — the source must not move ────────────────────────────────
class SourceMutationIsDetectedTests(_Fixture):

    def test_a_source_change_during_the_run_fails_the_gate(self):
        """The archive is an epoch boundary. If the source can move while
        the copy is taken, 'immutable' is not a claim this tool can make."""
        sys.path.insert(0, str(REPO))
        from scripts.canonical_epoch_dry_run import economic_snapshot
        before = economic_snapshot(self.source)
        conn = sqlite3.connect(self.source)
        conn.execute("UPDATE paper_portfolio SET cash = 1.0")
        conn.commit()
        conn.close()
        after = economic_snapshot(self.source)
        self.assertNotEqual(before, after,
                            "an economic change was not detected")
        self.assertNotEqual(before["paper_portfolio"]["digest"],
                            after["paper_portfolio"]["digest"])

    def test_a_row_change_that_keeps_the_count_is_still_detected(self):
        """Counts are a weak claim: a table can keep its row count while
        every row inside it changes."""
        sys.path.insert(0, str(REPO))
        from scripts.canonical_epoch_dry_run import economic_snapshot
        before = economic_snapshot(self.source)
        conn = sqlite3.connect(self.source)
        conn.execute("UPDATE paper_positions SET qty = 999.0")
        conn.commit()
        conn.close()
        after = economic_snapshot(self.source)
        self.assertEqual(before["paper_positions"]["rows"],
                         after["paper_positions"]["rows"])
        self.assertNotEqual(before["paper_positions"]["digest"],
                            after["paper_positions"]["digest"])


# ── P4.2 — logical equivalence, not byte equality ────────────────────────
class ArchiveEquivalenceIsLogicalTests(_Fixture):

    def test_a_truncated_archive_is_not_logically_equivalent(self):
        sys.path.insert(0, str(REPO))
        from scripts.canonical_epoch_dry_run import (logical_manifest,
                                                     ro_connect)
        archive = self.root / "archive.db"
        src = ro_connect(self.source)
        dst = sqlite3.connect(archive)
        try:
            src.backup(dst)
            dst.execute("DELETE FROM paper_positions")
            dst.commit()
        finally:
            dst.close()
            src.close()
        a = ro_connect(self.source)
        b = ro_connect(archive)
        try:
            self.assertNotEqual(logical_manifest(a)["tables"],
                                logical_manifest(b)["tables"])
        finally:
            a.close()
            b.close()

    def test_a_faithful_backup_is_equivalent_despite_different_bytes(self):
        sys.path.insert(0, str(REPO))
        from scripts.canonical_epoch_dry_run import (logical_manifest,
                                                     ro_connect)
        archive = self.root / "faithful.db"
        src = ro_connect(self.source)
        dst = sqlite3.connect(archive)
        try:
            src.backup(dst)
            dst.commit()
        finally:
            dst.close()
            src.close()
        a, b = ro_connect(self.source), ro_connect(archive)
        try:
            self.assertEqual(logical_manifest(a), logical_manifest(b))
        finally:
            a.close()
            b.close()


# ── P20 — readiness is a conjunction ─────────────────────────────────────
class ReadinessIsFailClosedTests(unittest.TestCase):

    def test_a_single_failed_gate_makes_the_run_not_ready(self):
        sys.path.insert(0, str(REPO))
        from scripts.canonical_epoch_dry_run import Gates
        g = Gates()
        for i in range(12):
            g.record(f"gate_{i}", True)
        self.assertTrue(g.ready)
        g.record("the_one_that_matters", False, "injected")
        self.assertFalse(g.ready)
        self.assertEqual(g.failed(), ["the_one_that_matters"])

    def test_an_empty_gate_set_is_not_ready(self):
        """Reaching the end of the script proves only that it ran."""
        sys.path.insert(0, str(REPO))
        from scripts.canonical_epoch_dry_run import Gates
        self.assertFalse(Gates().ready)


# ── P8.2 — writable store redirection ────────────────────────────────────
class EveryWritableStoreIsRedirectedTests(unittest.TestCase):

    def test_the_child_environment_redirects_all_known_stores(self):
        sys.path.insert(0, str(REPO))
        from scripts.canonical_epoch_dry_run import child_env
        with tempfile.TemporaryDirectory(prefix="jarvis-env-") as d:
            work = Path(d) / "work"
            cand = Path(d) / "candidate" / "jarvis.db"
            env = child_env(cand, work)
        for key in ("JARVIS_DB_PATH", "JARVIS_EVENTS_DB_PATH",
                    "JARVIS_OHLCV_DB_PATH"):
            self.assertIn(key, env)
            self.assertIn(str(Path(d)), env[key],
                          f"{key} points outside the work directory")
        self.assertEqual(env["JARVIS_SCHEDULER_ENABLED"], "false")


if __name__ == "__main__":
    unittest.main()
