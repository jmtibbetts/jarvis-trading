"""A name used and never imported is a NameError waiting for a code path.

This exists because of a live failure:

    wallet_discovery job failed: name 'WalletRegistry' is not defined

`discover_from_tokens` queried WalletRegistry directly to detect a REPEAT
SIGHTING — an address already known, whose new appearance is still evidence
— and the query was added without its import. The branch only runs when
discovery meets an already-known wallet, which is the COMMON case, so the
scheduled job died on essentially every pass.

Nothing caught it. The suite had 2,700 tests and none of them drove that
branch with a populated registry, the module imported fine, and the
function only raised once it reached the wallet.

That is the whole shape of the problem: a missing import is invisible until
the exact line executes, and the lines most likely to be missed are the
ones in rarely-tested branches. A static check does not care how rare the
branch is.

Deliberately conservative — it only reports a name when it is confident.
False positives here would be worse than the bug, because a noisy guard
gets skipped and then deleted.
"""
import ast
import builtins
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCAN_DIRS = ("lib", "jobs", "app")

_BUILTINS = set(dir(builtins)) | {
    "__file__", "__name__", "__doc__", "__package__", "self", "cls",
}


def _bound_names(node) -> set:
    """Everything a scope binds: imports, assignments, defs, args, loops."""
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Import):
            for a in n.names:
                out.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, ast.ImportFrom):
            for a in n.names:
                out.add(a.asname or a.name)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            out.add(n.id)
        elif isinstance(n, ast.arg):
            out.add(n.arg)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            out.add(n.name)
        elif isinstance(n, ast.Global):
            out.update(n.names)
        elif isinstance(n, (ast.withitem,)):
            pass
    return out


def undefined_in(path: pathlib.Path) -> list:
    """Names read in a function that no enclosing scope binds."""
    tree = ast.parse(path.read_text(encoding="utf-8"))

    # A star import makes the bound set statically unknowable — the router
    # modules do `from app.routers.common import *`, and those names DO
    # exist at runtime. Reporting them would be a false positive, and a
    # guard that cries wolf gets skipped and then deleted.
    if any(isinstance(n, ast.ImportFrom) and any(a.name == "*" for a in n.names)
           for n in ast.walk(tree)):
        return []

    parents = {}
    for n in ast.walk(tree):
        for c in ast.iter_child_nodes(n):
            parents[c] = n

    module_names = _bound_names(tree)
    problems = []

    for fn in [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        # Everything visible from inside this function.
        visible = set(module_names)
        cur = fn
        while cur is not None:
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visible |= _bound_names(cur)
            cur = parents.get(cur)

        for n in ast.walk(fn):
            if not (isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)):
                continue
            name = n.id
            if name in visible or name in _BUILTINS or name.startswith("_"):
                continue
            # Only flag Capitalised names — classes and models, which is
            # where this class of bug actually lands. Lowercase misses are
            # far more often comprehension or walrus scoping that this
            # simple walker would misread, and a false positive is worse
            # than the bug.
            if name[:1].isupper():
                problems.append((path.name, n.lineno, fn.name, name))
    return problems


class NoUndefinedNamesTests(unittest.TestCase):
    def test_no_capitalised_name_is_used_without_being_bound(self):
        found = []
        for d in SCAN_DIRS:
            for p in sorted((ROOT / d).rglob("*.py")):
                if "__pycache__" in str(p):
                    continue
                found.extend(undefined_in(p))
        self.assertEqual(
            found, [],
            "names used but never imported or defined:\n" + "\n".join(
                f"  {f}:{ln} in {fn}() -> {nm}" for f, ln, fn, nm in found))

    def test_the_checker_catches_the_bug_it_was_written_for(self):
        """Proof the guard works, using the exact shape that shipped."""
        import tempfile
        src = (
            "def discover(db):\n"
            "    from app.database import get_db\n"
            "    return db.query(WalletRegistry).first()\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                         encoding="utf-8") as f:
            f.write(src)
            tmp = pathlib.Path(f.name)
        try:
            found = undefined_in(tmp)
            self.assertTrue(any(n == "WalletRegistry" for *_, n in found),
                            f"the checker missed it: {found}")
        finally:
            tmp.unlink(missing_ok=True)

    def test_the_checker_does_not_flag_a_closure_import(self):
        """The common legitimate pattern: an inner _run() using a name the
        enclosing function imported. Flagging these would make the guard
        noise, and a noisy guard gets deleted."""
        import tempfile
        src = (
            "def outer(db):\n"
            "    from app.database import WalletRegistry\n"
            "    def _run(session):\n"
            "        return session.query(WalletRegistry).first()\n"
            "    return _run(db)\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                         encoding="utf-8") as f:
            f.write(src)
            tmp = pathlib.Path(f.name)
        try:
            self.assertEqual(undefined_in(tmp), [])
        finally:
            tmp.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
