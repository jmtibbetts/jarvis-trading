"""The loaded build identity must be stamped at LOAD, not at first request.

WHAT WENT WRONG. `lib/build_identity` captures the commit in a module-level
constant and documents it as "captured ONCE, at import ... the moment the
process's code was read from disk". That is true of the CONSTANT and was
false of the PROCESS: the only consumer imported the module INSIDE the
`/system/version` handler, so nothing imported it until a request arrived.
The constant was therefore stamped at first request, and any commit made in
between became the reported "loaded" build.

Measured 2026-08-20: a server started at 11:04:51 reported a commit authored
at 11:08:48 as the code it was running. A deploy check asking "are you
running the new code?" was answered "yes" by a process that had loaded the
working tree from before either commit existed — the identical failure the
per-request `git rev-parse` was replaced to stop, arriving through a lazy
import instead.

WHY THIS TEST IS STRUCTURAL, NOT BEHAVIOURAL. Reproducing the bug end to end
needs a running server and a commit made underneath it. What actually has to
hold is much simpler and is checked directly: SOMETHING ON THE STARTUP PATH
IMPORTS THE MODULE. A future edit that drops the import compiles, serves,
and passes every other test, so nothing but this would notice.
"""
import ast
import pathlib
import unittest

ROOT = pathlib.Path(__file__).parent.parent


def _module_level_imports(path: pathlib.Path) -> set:
    """Only imports at MODULE scope — one inside a function is the bug."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for a in node.names:
                found.add(a.name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            found.add(mod)
            for a in node.names:
                found.add(f"{mod}.{a.name}")
    return found


class BuildIdentityIsStampedAtStartupTests(unittest.TestCase):

    def test_the_entrypoint_imports_build_identity_at_module_scope(self):
        imports = _module_level_imports(ROOT / "main.py")
        self.assertTrue(
            "lib.build_identity" in imports
            or "lib.build_identity.LOADED_BACKEND_COMMIT" in imports,
            "main.py must import lib.build_identity at MODULE scope so the "
            "loaded commit is stamped when the process reads its code. "
            "Without it the constant is captured on the first "
            "/system/version request, and a commit made in between is "
            "reported as the running build.")

    def test_importing_the_entrypoint_stamps_the_identity(self):
        """The property itself, measured IN A SUBPROCESS.

        `import main` must not happen in this process: main.py calls
        `load_dotenv()`, which injects the OPERATOR'S REAL .env into the
        whole pytest session. Measured while writing this file — a bare
        `import main` here turned 42 unrelated tests red, because
        `VENUE_30D_VOLUME_USD` and friends leaked into tests that assume a
        clean environment. The suite is hermetic by construction and one
        import can undo that for everything scheduled after it.

        So the entrypoint is loaded in a child, inheriting this process's
        pytest environment (including the redirected database path), and
        only the answer comes back.
        """
        import os
        import subprocess
        import sys

        probe = ("import sys, main; "
                 "print('YES' if 'lib.build_identity' in sys.modules "
                 "else 'NO')")
        out = subprocess.run(
            [sys.executable, "-c", probe], cwd=str(ROOT),
            env={**os.environ, "PYTHONPATH": str(ROOT)},
            capture_output=True, text=True, timeout=120)
        self.assertEqual(
            out.returncode, 0,
            f"loading the entrypoint failed:\n{out.stderr[-2000:]}")
        self.assertIn(
            "YES", out.stdout,
            "loading the entrypoint did not stamp the build identity, so "
            "the loaded commit will be captured on the first request "
            f"instead:\n{out.stdout}\n{out.stderr[-2000:]}")

    def test_the_constant_is_never_recomputed(self):
        """`repository_head_commit()` is live BY DESIGN; the loaded one is not.

        Asserted on the AST: `loaded_backend_commit` must return the
        module constant and must not call out to git, or the immutability
        the whole module promises is gone.
        """
        tree = ast.parse((ROOT / "lib" / "build_identity.py")
                         .read_text(encoding="utf-8"))
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "loaded_backend_commit")
        calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
        self.assertEqual(
            calls, [],
            "loaded_backend_commit() calls something; it must return the "
            "constant captured at import and nothing else")

    def test_the_identity_survives_a_repository_that_moves_underneath_it(self):
        """Re-reading must not change what this process reports."""
        from lib import build_identity as BI

        first = BI.loaded_backend_commit()
        for _ in range(3):
            BI.repository_head_commit()
            self.assertEqual(BI.loaded_backend_commit(), first)


if __name__ == "__main__":
    unittest.main()
