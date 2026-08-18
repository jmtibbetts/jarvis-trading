"""The cutover moves the operator's real database. Prove it before it runs.

This is the one tool in the repository that performs a destructive
filesystem operation on live economic state, so nearly everything here is a
refusal or a recovery property. The happy path is exercised end to end
against fixtures; the real run is a separate, deliberate act.

The two failures that would matter most, and are pinned hardest:

    a database promoted into the active path while a legacy -wal or -shm is
    still sitting there — SQLite sidecars belong to one database history and
    a new book can adopt them

    a swap that dies after the legacy move and leaves NO database at the
    active path
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
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
TOOL = REPO / "scripts" / "canonical_epoch_cutover.py"
DRY = REPO / "scripts" / "canonical_epoch_dry_run.py"

from tests.test_canonical_epoch_dry_run import make_fixture  # noqa: E402


def dry_manifest(source: Path, run_id: str = "DRYRUN1",
                 ready: bool = True, **over) -> dict:
    man = {
        "run_id": run_id,
        "mode": "DRY_RUN_ONLY",
        "ready": ready,
        "failed_gates": [],
        "status": "CANONICAL_EPOCH_DRY_RUN_COMPLETE",
        "source": {"path": str(source)},
        "table_classification": {},
        "candidate": {"first": {"db": "/nonexistent/dryrun/candidate.db"}},
    }
    man.update(over)
    return man


class _Fixture(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(prefix="jarvis-cut-")
        self.addCleanup(self._cleanup)
        self.root = Path(self._dir.name)
        self.data = self.root / "data"
        self.source = self.data / "jarvis.db"
        make_fixture(self.source)
        self.work = self.root / "run"
        self.work.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.root / "dry.json"
        self.manifest_path.write_text(json.dumps(dry_manifest(self.source)),
                                      encoding="utf-8")

    def _cleanup(self):
        for path in self.root.rglob("*"):
            try:
                path.chmod(0o700)
            except OSError:
                pass
        self._dir.cleanup()

    def quiet(self):
        """Windows has no /proc, so the tool cannot prove quiescence there
        and correctly refuses. The refusal itself is tested separately; here
        the platform capability is substituted so the rest of the logic is
        exercised on every OS."""
        from scripts import canonical_epoch_cutover as CUT
        return patch.multiple(CUT,
                              db_holders=lambda _s: [],
                              proc_enumeration_available=lambda: True)

    def args(self, **over):
        base = {
            "source": str(self.source), "work_dir": str(self.work),
            "starting_cash": 100000.0,
            "dry_run_manifest": str(self.manifest_path),
            "confirm": "CANONICAL_EPOCH_CUTOVER",
            "confirm_run_id": "DRYRUN1",
            "legacy_dir": str(self.root / "legacy"),
            "prepare_only": False,
        }
        base.update(over)
        return [
            "--source", base["source"], "--work-dir", base["work_dir"],
            "--starting-cash", str(base["starting_cash"]),
            "--dry-run-manifest", base["dry_run_manifest"],
            "--confirm", base["confirm"],
            "--confirm-run-id", base["confirm_run_id"],
            "--legacy-dir", base["legacy_dir"],
        ] + (["--prepare-only"] if base["prepare_only"] else [])

    def manifest(self):
        path = self.work / "CANONICAL_CUTOVER_MANIFEST.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def journal(self):
        path = self.work / "CUTOVER_STATE.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


# ── 1, 2 — refusals before anything moves ────────────────────────────────
class ItRefusesBeforeTouchingAnythingTests(_Fixture):

    def _run(self, argv):
        from scripts import canonical_epoch_cutover as CUT
        with self.quiet():
            return CUT.main(argv)

    def test_a_wrong_confirmation_token_refuses(self):
        rc = self._run(self.args(confirm="canonical_epoch_cutover"))
        self.assertEqual(rc, 1)
        self.assertTrue(self.source.exists(), "the source was touched")
        gates = self.manifest()["gates"]
        self.assertFalse(gates["confirmation_token_exact"]["ok"])

    def test_a_dry_run_that_was_not_ready_refuses(self):
        self.manifest_path.write_text(
            json.dumps(dry_manifest(self.source, ready=False,
                                    failed_gates=["something"])),
            encoding="utf-8")
        rc = self._run(self.args())
        self.assertEqual(rc, 1)
        self.assertTrue(self.source.exists())
        gates = self.manifest()["gates"]
        self.assertFalse(gates["dry_run_was_ready"]["ok"])
        self.assertFalse(gates["dry_run_had_no_failed_gates"]["ok"])

    def test_a_dry_run_for_a_different_source_refuses(self):
        other = self.root / "elsewhere.db"
        make_fixture(other)
        self.manifest_path.write_text(
            json.dumps(dry_manifest(other)), encoding="utf-8")
        rc = self._run(self.args())
        self.assertEqual(rc, 1)
        self.assertFalse(self.manifest()["gates"]["dry_run_source_matches"]["ok"])

    def test_a_mismatched_run_id_refuses(self):
        rc = self._run(self.args(confirm_run_id="SOMETHING_ELSE"))
        self.assertEqual(rc, 1)
        self.assertFalse(self.manifest()["gates"]["confirm_run_id_matches"]["ok"])

    def test_an_unclassified_source_table_refuses(self):
        conn = sqlite3.connect(self.source)
        conn.execute("CREATE TABLE a_brand_new_table (id TEXT)")
        conn.commit()
        conn.close()
        rc = self._run(self.args())
        self.assertEqual(rc, 1)
        self.assertTrue(self.source.exists())
        gate = self.manifest()["gates"]["every_source_table_still_classified"]
        self.assertFalse(gate["ok"])
        self.assertIn("a_brand_new_table", gate["detail"])

    def test_a_held_source_refuses(self):
        """P5 — quiescence is proved from open file descriptors, and a
        holder stops the run rather than being killed."""
        from scripts import canonical_epoch_cutover as CUT
        holder = {"pid": 999, "cmd": "some-writer", "path": str(self.source)}
        with patch.object(CUT, "db_holders", return_value=[holder]):
            rc = CUT.main(self.args())
        self.assertEqual(rc, 1)
        self.assertTrue(self.source.exists())
        self.assertFalse(self.manifest()["gates"]["source_quiescent"]["ok"])


# ── 3, 4, 5 — the candidate arrives disarmed ─────────────────────────────
class TheCandidateIsBuiltDisarmedTests(_Fixture):

    def _prepare(self):
        from scripts import canonical_epoch_cutover as CUT
        with self.quiet():
            rc = CUT.main(self.args(prepare_only=True))
        return rc, self.manifest()

    def test_prepare_only_builds_a_candidate_and_moves_nothing(self):
        rc, man = self._prepare()
        self.assertEqual(rc, 1, "prepare-only is not a completed cutover")
        self.assertEqual(man["status"], "CUTOVER_REFUSED_NOTHING_MOVED")
        self.assertTrue(self.source.exists(), "the source moved")
        self.assertTrue((self.work / "candidate" / "jarvis.db").exists())

    def test_the_candidate_forces_every_arming_flag_off(self):
        rc, man = self._prepare()
        candidate = self.work / "candidate" / "jarvis.db"
        conn = sqlite3.connect(f"file:{candidate}?mode=ro", uri=True)
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "system_state" in tables:
                for (v,) in conn.execute(
                        "SELECT live_trading_enabled FROM system_state"):
                    self.assertFalse(bool(v), "live trading is armed")
            if "user_preferences" in tables:
                for v, a in conn.execute(
                        "SELECT paper_auto_trade_enabled, auto_sim_enabled "
                        "FROM user_preferences"):
                    self.assertFalse(bool(v), "paper auto trading is armed")
                    self.assertFalse(bool(a), "auto sim is armed")
        finally:
            conn.close()
        self.assertTrue(man["gates"]["candidate_is_disarmed"]["ok"])

    def test_the_source_arming_values_are_preserved_in_the_manifest(self):
        """P3.2 — the interlock is recorded as an interlock. Nothing here
        pretends the operator chose the safe values."""
        rc, man = self._prepare()
        self.assertEqual(man["arming"]["reason"], "CUTOVER_DISARM")
        source_arm = man["arming"]["source"]
        self.assertIn("user_preferences.paper_auto_trade_enabled", source_arm)
        # The fixture ships it ARMED, so this proves the record is of the
        # real prior value and not a copy of the forced one.
        self.assertEqual(source_arm["user_preferences.paper_auto_trade_enabled"],
                         [1])

    def test_the_source_is_never_disarmed(self):
        self._prepare()
        conn = sqlite3.connect(f"file:{self.source}?mode=ro", uri=True)
        try:
            (v,) = conn.execute(
                "SELECT paper_auto_trade_enabled FROM user_preferences"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(v, 1, "the legacy source was modified")

    def test_the_candidate_has_no_economic_history(self):
        rc, man = self._prepare()
        self.assertTrue(man["gates"]["candidate_economy_is_zero"]["ok"])
        self.assertTrue(man["gates"]["candidate_derived_learning_is_zero"]["ok"])
        self.assertTrue(man["gates"]["starting_cash_exact"]["ok"])

    def test_no_lifecycle_trade_is_run_against_the_final_candidate(self):
        """P4.4/P16 — the lifecycle proof already exists from the dry run.
        Running one here would contaminate the book that goes live."""
        source = TOOL.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                rendered = ast.unparse(node)
                self.assertNotIn('"lifecycle"', rendered)
                self.assertNotIn("'lifecycle'", rendered)


# ── 6, 14, 19, 20 — archives and leakage ─────────────────────────────────
class TheLegacyEconomySurvivesTests(_Fixture):

    def test_a_completed_cutover_archives_the_full_legacy_economy(self):
        from scripts import canonical_epoch_cutover as CUT
        with self.quiet():
            rc = CUT.main(self.args())
        man = self.manifest()
        self.assertEqual(rc, 0, man.get("failed_gates"))
        archive = Path(man["archive"]["path"])
        conn = sqlite3.connect(f"file:{archive}?mode=ro", uri=True)
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM paper_positions"
                             ).fetchone()[0], 1)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM trade_outcomes"
                             ).fetchone()[0], 1)
            still_open = conn.execute(
                "SELECT COUNT(*) FROM paper_positions "
                "WHERE status='Open'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(still_open, 1,
                         "an open legacy position was closed — it must be "
                         "archived open, not settled under new economics")

    def test_the_active_book_shares_no_identifier_with_the_archive(self):
        from scripts import canonical_epoch_cutover as CUT
        with self.quiet():
            rc = CUT.main(self.args())
        self.assertEqual(rc, 0)
        man = self.manifest()
        self.assertTrue(
            man["gates"]["no_legacy_identifier_in_the_active_book"]["ok"])

    def test_the_active_book_has_no_lifecycle_test_trade(self):
        from scripts import canonical_epoch_cutover as CUT
        with self.quiet():
            self.assertEqual(CUT.main(self.args()), 0)
        conn = sqlite3.connect(f"file:{self.source}?mode=ro", uri=True)
        try:
            for table in ("paper_positions", "paper_trades",
                          "trade_outcomes"):
                self.assertEqual(
                    conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                    0, f"{table} is not empty in the active book")
        finally:
            conn.close()

    def test_the_dormant_original_is_kept_too(self):
        """P7.3 — two recovery artifacts: the verified logical backup AND
        the original file itself."""
        from scripts import canonical_epoch_cutover as CUT
        with self.quiet():
            self.assertEqual(CUT.main(self.args()), 0)
        man = self.manifest()
        dormant = Path(man["swap"]["legacy_original"])
        self.assertTrue(dormant.exists(), "the original was not preserved")
        self.assertTrue(Path(man["archive"]["path"]).exists())


# ── 7, 8, 9, 10 — the swap itself ────────────────────────────────────────
class TheSwapIsSidecarSafeTests(_Fixture):

    def _with_sidecars(self):
        """Force real WAL/SHM files to exist beside the source."""
        conn = sqlite3.connect(self.source)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("INSERT INTO paper_trades VALUES ('t2','ETH/USD',1.0)")
        conn.commit()
        return conn          # left OPEN so the sidecars persist

    def test_legacy_sidecars_do_not_remain_at_the_active_path(self):
        conn = self._with_sidecars()
        conn.close()
        wal = Path(str(self.source) + "-wal")
        # SQLite may checkpoint on close; only assert when one exists.
        from scripts import canonical_epoch_cutover as CUT
        with self.quiet():
            rc = CUT.main(self.args())
        self.assertEqual(rc, 0, self.manifest().get("failed_gates"))
        man = self.manifest()
        self.assertTrue(
            man["gates"]["active_path_clear_before_promotion"]["ok"])
        # Whatever sidecars existed moved WITH the legacy database.
        dormant = Path(man["swap"]["legacy_original"])
        for suffix in ("-wal", "-shm"):
            stale = Path(str(self.source) + suffix)
            if stale.exists():
                # It must belong to the NEW database, not the old one.
                self.assertGreaterEqual(
                    stale.stat().st_mtime, dormant.stat().st_mtime - 5)

    def test_the_promoted_database_is_the_prepared_candidate(self):
        from scripts import canonical_epoch_cutover as CUT
        with self.quiet():
            self.assertEqual(CUT.main(self.args()), 0)
        man = self.manifest()
        self.assertEqual(man["swap"]["prepared_sha256"],
                         man["swap"]["active_sha256"])
        self.assertTrue(
            man["gates"]["promoted_db_matches_prepared_candidate"]["ok"])

    def test_the_journal_records_the_full_progression(self):
        from scripts import canonical_epoch_cutover as CUT
        with self.quiet():
            self.assertEqual(CUT.main(self.args()), 0)
        states = [e["state"] for e in self.journal()["history"]]
        self.assertEqual(states[:3], ["PREPARED", "LEGACY_MOVED",
                                      "CANDIDATE_PROMOTED"])
        self.assertEqual(states[-1], "VERIFIED")

    def test_a_failure_after_the_legacy_move_restores_the_old_database(self):
        """P9.4 — the active path must never be left without a database.
        Nothing canonical has been written yet, so restoring the legacy book
        is a complete and honest rollback."""
        from scripts import canonical_epoch_cutover as CUT
        real_replace = os.replace
        calls = {"n": 0}

        # The tool resolves its paths, and on Windows a temp dir arrives as
        # an 8.3 short name, so compare RESOLVED paths or this never fires.
        active = self.source.resolve()

        def exploding_replace(a, b, *rest, **kw):
            # Let the legacy move and its sidecars through, then fail on
            # the candidate promotion.
            if Path(b).resolve() == active and "candidate" in str(a):
                raise OSError("injected: promotion failed")
            return real_replace(a, b, *rest, **kw)

        with self.quiet(), patch.object(CUT.os, "replace",
                                        exploding_replace):
            rc = CUT.main(self.args())

        self.assertEqual(rc, 1)
        self.assertTrue(self.source.exists(),
                        "the active path was left with NO database")
        conn = sqlite3.connect(f"file:{self.source}?mode=ro", uri=True)
        try:
            positions = conn.execute(
                "SELECT COUNT(*) FROM paper_positions").fetchone()[0]
            cash = conn.execute("SELECT cash FROM paper_portfolio").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(positions, 1, "the legacy book was not restored")
        self.assertAlmostEqual(float(cash), 63550.83, places=2)
        man = self.manifest()
        self.assertEqual(man["status"],
                         "CUTOVER_ROLLED_BACK_BEFORE_ACTIVATION")
        self.assertEqual(self.journal()["state"],
                         "CUTOVER_ROLLED_BACK_BEFORE_ACTIVATION")


# ── 12, 13 — the new active book ─────────────────────────────────────────
class TheActiveBookIsFreshAndDisarmedTests(_Fixture):

    def test_the_active_book_starts_at_exactly_the_stated_capital(self):
        from scripts import canonical_epoch_cutover as CUT
        with self.quiet():
            self.assertEqual(CUT.main(self.args()), 0)
        conn = sqlite3.connect(f"file:{self.source}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT cash, total_trades, winning_trades, realized_pnl "
                "FROM paper_portfolio").fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(float(rows[0][0]), 100000.0)
        self.assertEqual(float(rows[0][1] or 0), 0.0)
        self.assertEqual(float(rows[0][3] or 0), 0.0)

    def test_the_active_book_is_disarmed(self):
        from scripts import canonical_epoch_cutover as CUT
        with self.quiet():
            self.assertEqual(CUT.main(self.args()), 0)
        self.assertTrue(self.manifest()["gates"]["active_book_is_disarmed"]["ok"])


# ── 6, 17 — structural ───────────────────────────────────────────────────
class StructuralGuardsTests(unittest.TestCase):

    def test_the_dry_run_tool_still_has_no_apply_path(self):
        """The cutover capability lives in ONE file. Adding it to the dry
        run would remove the property that makes the dry run safe to run
        casually."""
        tree = ast.parse(DRY.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                qualified = ast.unparse(node.func)
                self.assertNotIn(qualified,
                                 {"os.replace", "os.rename", "shutil.move"})

    def test_the_cutover_never_calls_init_db_on_an_archive(self):
        """P14 — archives are archives. Nothing may migrate them."""
        text = TOOL.read_text(encoding="utf-8")
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = (getattr(node.func, "id", None)
                      or getattr(node.func, "attr", None))
                self.assertNotEqual(fn, "init_db",
                                    "the cutover calls init_db directly")

    def test_it_does_not_use_either_reset(self):
        for path in (TOOL, DRY):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        self.assertNotIn(alias.name,
                                         ("reset_paper_portfolio",
                                          "soft_reset_paper_portfolio"))
                if isinstance(node, ast.Call):
                    fn = (getattr(node.func, "id", None)
                          or getattr(node.func, "attr", None))
                    self.assertNotIn(fn, ("reset_paper_portfolio",
                                          "soft_reset_paper_portfolio"))

    def test_the_parent_never_imports_app_database(self):
        tree = ast.parse(TOOL.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self.assertFalse((node.module or "").startswith("app"),
                                 f"imports {node.module}")

    def test_the_disarm_list_covers_every_arming_flag(self):
        from scripts.canonical_epoch_cutover import DISARM
        flags = {(t, c) for t, c, _ in DISARM}
        self.assertIn(("system_state", "live_trading_enabled"), flags)
        self.assertIn(("user_preferences", "paper_auto_trade_enabled"), flags)
        self.assertIn(("user_preferences", "auto_sim_enabled"), flags)
        for _table, _col, safe in DISARM:
            self.assertFalse(bool(safe), "a disarm value is truthy")


# ── 15, 18 — operator tooling defaults ───────────────────────────────────
class OperatorToolsStillTargetTheActivePathTests(unittest.TestCase):
    """P15 — the canonical book now occupies the standard path, so the
    tools' defaults stay correct. Archive inspection must be explicit; no
    tool may pick the archive because it is bigger or older."""

    def test_the_fingerprint_tool_defaults_to_the_active_path(self):
        text = (REPO / "scripts" / "operator_db_fingerprint.py").read_text(
            encoding="utf-8")
        self.assertIn('"jarvis.db"', text)
        for wrong in ("legacy_epochs", "jarvis_legacy_original",
                      "cutover_runs"):
            self.assertNotIn(wrong, text,
                             f"the fingerprint tool references {wrong}")

    def test_no_operator_tool_defaults_to_an_archive(self):
        for name in ("operator_db_fingerprint.py", "snapshot_operator_db.py",
                     "run_dev_copy.py"):
            path = REPO / "scripts" / name
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            for wrong in ("legacy_epochs", "jarvis_legacy_original"):
                self.assertNotIn(wrong, text, f"{name} references {wrong}")


if __name__ == "__main__":
    unittest.main()
