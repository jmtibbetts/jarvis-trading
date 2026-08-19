"""No module may reference a name that does not exist.

WHY THIS FILE EXISTS. The paper-trading job — the only thing the scheduler
calls to trade — raised `NameError` at its summary line on EVERY run, after
entries had already been executed. 3,900 tests were green. None of them
called `run()`, because every test exercised the pieces underneath it.

Three more of the same class were sitting in the API layer, invisible to the
linter because the routers use `from app.routers.common import *`:

    intel.py     get_portfolio_risk  — inside `except Exception: pass`, so
                                       the analyst silently never received
                                       the portfolio it was asked about
    intel.py     sym                 — /analyze?generate_signal=true → 500
    trading.py   normalize_symbol    — /signals/{id}/reverse → 500

A unit test proves a function works. It cannot prove the function is
reachable, or that its caller can finish. This check is cheap, total, and
catches the failure the expensive tests structurally cannot.
"""
import ast
import pathlib
import subprocess
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
PACKAGES = ("app", "lib", "jobs", "scripts")
WILDCARD = "from app.routers.common import *"


def _pyflakes(paths):
    r = subprocess.run([sys.executable, "-m", "pyflakes", *[str(p) for p in paths]],
                       capture_output=True, text=True)
    # "unable to detect undefined names" is pyflakes REPORTING that a
    # wildcard import blinded it — a caveat, not a finding. The second test
    # below is what actually covers those files.
    return [ln for ln in (r.stdout or "").splitlines()
            if "undefined name" in ln and "unable to detect" not in ln]


def _wildcard_exports():
    """What `import *` actually pulls in from the routers' shared module."""
    tree = ast.parse((REPO / "app" / "routers" / "common.py").read_text(encoding="utf-8"))
    out = set()
    for n in tree.body:
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and not t.id.startswith("_"):
                    out.add(t.id)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not n.name.startswith("_"):
                out.add(n.name)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                nm = (a.asname or a.name).split(".")[0]
                if not nm.startswith("_"):
                    out.add(nm)
    return out


class NoUndefinedNamesTests(unittest.TestCase):

    def test_no_module_references_an_undefined_name(self):
        try:
            import pyflakes           # noqa: F401
        except ImportError:
            self.skipTest("pyflakes not installed")
        files = [p for pkg in PACKAGES
                 for p in (REPO / pkg).rglob("*.py")] + [REPO / "main.py"]
        findings = _pyflakes(files)
        self.assertEqual(findings, [], "\n".join(findings))

    def test_the_routers_are_checked_despite_the_wildcard_import(self):
        """`import *` blinds the linter across the whole API surface — about
        7,000 lines where an undefined name only shows up at request time.
        Resolving the wildcard into explicit names restores the check."""
        try:
            import pyflakes           # noqa: F401
        except ImportError:
            self.skipTest("pyflakes not installed")
        exports = _wildcard_exports()
        self.assertGreater(len(exports), 20, "the wildcard resolved to almost "
                                             "nothing — this check is vacuous")
        findings = []
        with tempfile.TemporaryDirectory(prefix="jarvis-flake-") as tmpdir:
            tmp = pathlib.Path(tmpdir)
            (tmp / "_wildcard_shim.py").write_text(
                "\n".join(f"{n} = None" for n in sorted(exports)),
                encoding="utf-8")
            for router in sorted((REPO / "app" / "routers").glob("*.py")):
                if router.name == "common.py":
                    continue
                src = router.read_text(encoding="utf-8")
                if WILDCARD not in src:
                    continue
                patched = src.replace(
                    WILDCARD,
                    "from _wildcard_shim import " + ", ".join(sorted(exports)))
                out = tmp / router.name
                out.write_text(patched, encoding="utf-8")
                for line in _pyflakes([out]):
                    findings.append(f"{router.name}: {line.split(':', 2)[-1]}")
        self.assertEqual(findings, [], "\n".join(findings))

    def test_the_control_a_planted_undefined_name_is_caught(self):
        """Without this, a broken pyflakes invocation would report a clean
        codebase forever."""
        try:
            import pyflakes           # noqa: F401
        except ImportError:
            self.skipTest("pyflakes not installed")
        with tempfile.TemporaryDirectory(prefix="jarvis-flake-ctl-") as tmpdir:
            bad = pathlib.Path(tmpdir) / "planted.py"
            bad.write_text("def f():\n    return a_name_that_does_not_exist\n",
                           encoding="utf-8")
            self.assertTrue(_pyflakes([bad]), "the checker is not working")


if __name__ == "__main__":
    unittest.main()
