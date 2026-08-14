"""The operator database is structurally unreachable from tests.

Two incidents made this a guard instead of a convention: a test reset the
active paper book, and fixture rows leaked into the live candidate tables
despite per-test cleanup. Now the refusal lives at engine construction —
a test file that forgets every rule still cannot touch real state.
"""
import os
import unittest
from pathlib import Path


class HermeticDatabaseTests(unittest.TestCase):
    def test_this_very_process_is_on_a_throwaway_db(self):
        from app.database import DB_PATH
        self.assertIn("jarvis-test-db-", str(DB_PATH),
                      f"pytest is running against {DB_PATH}")

    def test_the_operator_db_is_refused_under_pytest(self):
        from app.database import _resolve_db_path
        old = os.environ.get("JARVIS_DB_PATH")
        try:
            os.environ["JARVIS_DB_PATH"] = str(
                Path(__file__).parent.parent / "data" / "jarvis.db")
            with self.assertRaises(RuntimeError) as ctx:
                _resolve_db_path()
            self.assertIn("Refusing", str(ctx.exception))
        finally:
            if old is None:
                os.environ.pop("JARVIS_DB_PATH", None)
            else:
                os.environ["JARVIS_DB_PATH"] = old

    def test_the_escape_hatch_is_explicit(self):
        from app.database import _resolve_db_path
        old_db = os.environ.get("JARVIS_DB_PATH")
        try:
            os.environ["JARVIS_DB_PATH"] = str(
                Path(__file__).parent.parent / "data" / "jarvis.db")
            os.environ["JARVIS_ALLOW_OPERATOR_DB"] = "1"
            p = _resolve_db_path()   # allowed, explicitly
            self.assertTrue(str(p).endswith("jarvis.db"))
        finally:
            os.environ.pop("JARVIS_ALLOW_OPERATOR_DB", None)
            if old_db is None:
                os.environ.pop("JARVIS_DB_PATH", None)
            else:
                os.environ["JARVIS_DB_PATH"] = old_db

    def test_the_scheduler_is_disabled_for_tests(self):
        self.assertEqual(os.getenv("JARVIS_DISABLE_SCHEDULER"), "1")


if __name__ == "__main__":
    unittest.main()
