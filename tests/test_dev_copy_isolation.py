"""The dev-copy helper must be incapable of reaching the operator's DB.

Two incidents in one session, same root cause: the real database was the
default, so forgetting to redirect meant hitting it. A second server on an
alternate port contended for the SQLite write lock and took the operator's
own instance down with `database is locked`; an ad-hoc probe that forgot
`JARVIS_DB_PATH` deleted and rewrote the live `dex_portfolios` row.

Remembering is not a control. These tests pin the control.
"""
import ast
import pathlib
import unittest

SCRIPT = pathlib.Path("scripts/run_dev_copy.py")


class TheHelperIsolatesTheOperatorDatabase(unittest.TestCase):
    def setUp(self):
        self.src = SCRIPT.read_text(encoding="utf-8")
        self.tree = ast.parse(self.src)

    def test_it_exists_and_parses(self):
        self.assertTrue(SCRIPT.exists())

    def test_it_redirects_both_database_paths(self):
        """Redirecting only the main DB would still let the raw event store
        be written to the operator's file."""
        self.assertIn("JARVIS_DB_PATH", self.src)
        self.assertIn("JARVIS_EVENTS_DB_PATH", self.src)

    def test_it_disables_the_scheduler(self):
        """A verification server that runs jobs would write trades, spend
        provider quota and race the operator's real scheduler."""
        self.assertIn("JARVIS_DISABLE_SCHEDULER", self.src)

    def test_it_refuses_the_operator_port(self):
        self.assertIn("3000", self.src)
        self.assertIn("REFUSED", self.src)

    def test_the_snapshot_opens_the_source_read_only(self):
        """A file copy of a live SQLite database with an active WAL can
        capture a torn state, and a writable handle could corrupt the
        operator's file outright. The source must be opened mode=ro."""
        self.assertIn("mode=ro", self.src)
        self.assertIn("uri=True", self.src)

    def test_it_uses_the_backup_api_rather_than_a_file_copy(self):
        self.assertIn(".backup(", self.src)

    def test_nothing_in_the_script_writes_to_the_data_directory(self):
        """Every path it constructs under data/ must be a READ source."""
        writes = [n for n in ast.walk(self.tree)
                  if isinstance(n, ast.Call)
                  and isinstance(n.func, ast.Attribute)
                  and n.func.attr in ("unlink", "write_text", "write_bytes")]
        self.assertEqual(writes, [],
                         "dev-copy helper must never write files itself")


class PytestCannotTouchTheOperatorDatabase(unittest.TestCase):
    """The other half of the guard, which was already in place — asserted
    here so a future conftest edit cannot quietly remove it."""

    def test_the_running_suite_is_on_a_throwaway_database(self):
        from app.database import DB_PATH
        self.assertIn("jarvis-test-db-", str(DB_PATH),
                      f"the suite is pointed at {DB_PATH} — it must never be "
                      f"the operator's database")

    def test_the_events_store_is_also_redirected(self):
        import os
        self.assertIn("jarvis-test-events-",
                      os.getenv("JARVIS_EVENTS_DB_PATH", ""))


if __name__ == "__main__":
    unittest.main()
