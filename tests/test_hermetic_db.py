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

    def test_there_is_no_escape_hatch_any_more(self):
        """This test used to assert the OPPOSITE — that
        JARVIS_ALLOW_OPERATOR_DB=1 opened the real database "explicitly".

        The invariant is now unconditional. A test has already reset the
        live paper book, a probe has already deleted a dex_portfolios row,
        and fixture rows have already leaked into live candidate tables. In
        every one of those the mechanism was an environment value nobody
        meant to be in scope, so "explicit" was never a property of the
        variable — only of the intent someone imagined behind it."""
        from app.database import _resolve_db_path
        old_db = os.environ.get("JARVIS_DB_PATH")
        try:
            os.environ["JARVIS_DB_PATH"] = str(
                Path(__file__).parent.parent / "data" / "jarvis.db")
            os.environ["JARVIS_ALLOW_OPERATOR_DB"] = "1"
            with self.assertRaises(RuntimeError) as ctx:
                _resolve_db_path()
            self.assertIn("unconditional", str(ctx.exception))
        finally:
            os.environ.pop("JARVIS_ALLOW_OPERATOR_DB", None)
            if old_db is None:
                os.environ.pop("JARVIS_DB_PATH", None)
            else:
                os.environ["JARVIS_DB_PATH"] = old_db

    def test_an_inherited_llm_url_cannot_survive_the_pytest_bootstrap(self):
        """conftest used setdefault, which leaves an INHERITED value alone —
        so an operator shell exporting a real LM_STUDIO_URL handed it to
        pytest and the pin pinned nothing."""
        import conftest  # noqa: F401  - the bootstrap has already run
        self.assertEqual(os.environ.get("LM_STUDIO_URL"), "http://127.0.0.1:9/v1")

    def test_the_bootstrap_assigns_the_pin_rather_than_defaulting_it(self):
        """The distinction IS the bug: setdefault is a no-op precisely when
        the shell already exports the variable, which is the only case that
        matters.

        Checked by AST, so the prose above — which names setdefault in order
        to forbid it — can neither satisfy nor break the check. Matching the
        comment that describes a rule instead of the rule has cost this
        project five separate debugging cycles."""
        import ast
        src = (Path(__file__).parent.parent / "conftest.py").read_text(encoding="utf-8")
        tree = ast.parse(src)

        assigned = setdefaulted = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if (isinstance(tgt, ast.Subscript)
                            and isinstance(tgt.slice, ast.Constant)
                            and tgt.slice.value == "LM_STUDIO_URL"):
                        assigned = True
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "setdefault"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value == "LM_STUDIO_URL"):
                setdefaulted = True

        self.assertTrue(assigned, "conftest must ASSIGN the LM Studio pin")
        self.assertFalse(setdefaulted,
                         "setdefault leaves an inherited real endpoint in place")

    def test_the_scheduler_is_disabled_for_tests(self):
        self.assertEqual(os.getenv("JARVIS_DISABLE_SCHEDULER"), "1")


if __name__ == "__main__":
    unittest.main()
