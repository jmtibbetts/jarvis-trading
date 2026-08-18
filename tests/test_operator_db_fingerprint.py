"""The immutability probe must not be able to mutate what it audits."""
import ast
import pathlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

SRC = pathlib.Path("scripts/operator_db_fingerprint.py")


class ReadOnlyByConstructionTests(unittest.TestCase):
    def test_it_never_imports_the_orm_that_would_enable_wal(self):
        """app.database installs PRAGMA journal_mode=WAL on connect — a probe
        that proves immutability by opening in a mode that rewrites the
        header is not a probe."""
        tree = ast.parse(SRC.read_text())
        banned = {"app.database", "app", "sqlalchemy"}
        found = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and n.module:
                found |= {b for b in banned if n.module == b
                          or n.module.startswith(b + ".")}
            elif isinstance(n, ast.Import):
                for a in n.names:
                    found |= {b for b in banned if a.name == b
                              or a.name.startswith(b + ".")}
        self.assertEqual(found, set())

    def test_it_opens_read_only_and_asserts_query_only(self):
        src = SRC.read_text()
        self.assertIn("?mode=ro", src)
        self.assertIn("PRAGMA query_only=ON", src)

    def test_it_issues_no_write_statement(self):
        """AST over what is actually EXECUTED. Scanning raw text flags the
        module docstring, which names the operations precisely because it
        promises not to perform them."""
        tree = ast.parse(SRC.read_text())
        executed = []
        for n in ast.walk(tree):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr in ("execute", "executescript")
                    and n.args and isinstance(n.args[0], ast.Constant)
                    and isinstance(n.args[0].value, str)):
                executed.append(n.args[0].value.upper())
        self.assertTrue(executed, "no SQL found to audit")
        for sql in executed:
            for stmt in ("INSERT", "UPDATE", "DELETE", "REPLACE", "VACUUM",
                         "CHECKPOINT", "JOURNAL_MODE", "CREATE", "DROP",
                         "ALTER", "ANALYZE"):
                self.assertNotIn(stmt, sql, f"probe executes: {sql}")


class FingerprintSemanticsTests(unittest.TestCase):
    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("fp", SRC)
        self.fp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.fp)
        self.db = Path(tempfile.mkdtemp()) / "op.db"
        c = sqlite3.connect(self.db)
        c.execute("CREATE TABLE paper_portfolio (id INTEGER PRIMARY KEY, cash REAL)")
        c.execute("INSERT INTO paper_portfolio VALUES (1, 63550.8371643338)")
        c.commit()
        c.close()

    def test_counts_alone_cannot_hide_a_changed_row(self):
        """A table can keep its row count while every value inside changes."""
        a = self.fp.fingerprint(self.db)
        c = sqlite3.connect(self.db)
        c.execute("UPDATE paper_portfolio SET cash = 1.0")
        c.commit()
        c.close()
        b = self.fp.fingerprint(self.db)
        self.assertEqual(a["tables"]["paper_portfolio"]["rows"],
                         b["tables"]["paper_portfolio"]["rows"])   # count same
        self.assertNotEqual(a["tables"]["paper_portfolio"]["rows_sha256"],
                            b["tables"]["paper_portfolio"]["rows_sha256"])

    def test_float_encoding_is_exact_not_rounded(self):
        self.assertNotEqual(self.fp._canon(0.1), self.fp._canon(0.10000000000001))

    def test_typed_encoding_separates_one_from_the_string_one(self):
        self.assertNotEqual(self.fp._canon(1), self.fp._canon("1"))

    def test_null_is_distinct_from_empty_string(self):
        self.assertNotEqual(self.fp._canon(None), self.fp._canon(""))

    def test_the_probe_leaves_the_file_untouched(self):
        before = self.fp._file_state(self.db)["sha256"]
        self.fp.fingerprint(self.db)
        self.assertEqual(self.fp._file_state(self.db)["sha256"], before)


if __name__ == "__main__":
    unittest.main()
