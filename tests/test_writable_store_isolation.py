"""Every writable store the application owns must be redirectable.

A disposable process — a dry run, a dev copy, a test — is only disposable if
EVERY file it can write is under its own directory. One store with a
hard-coded path is enough to make an otherwise-isolated process mutate live
operator data, and that store will be found the hard way.

`app.database` honours JARVIS_DB_PATH. `lib.event_store` honours
JARVIS_EVENTS_DB_PATH. `lib.ohlcv_cache` computed its path at import time
from __file__ with no override at all.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _child(env_extra: dict, code: str):
    env = dict(os.environ)
    env.pop("JARVIS_UNDER_PYTEST", None)
    env.update(env_extra)
    return subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=180)


class OhlcvCacheHonoursAnOverrideTests(unittest.TestCase):

    def test_the_cache_path_follows_the_environment(self):
        with tempfile.TemporaryDirectory(prefix="jarvis-store-") as d:
            target = Path(d) / "ohlcv.db"
            r = _child({"JARVIS_OHLCV_DB_PATH": str(target),
                        "JARVIS_DB_PATH": str(Path(d) / "jarvis.db")},
                       "from lib.ohlcv_cache import CACHE_DB; print(CACHE_DB)")
            self.assertEqual(r.returncode, 0, r.stderr[-2000:])
            self.assertEqual(Path(r.stdout.strip()).resolve(),
                             target.resolve(),
                             "the OHLCV cache ignored its override and would "
                             "have opened the live cache")

    def test_without_an_override_it_still_uses_the_default(self):
        r = _child({}, "from lib.ohlcv_cache import CACHE_DB; print(CACHE_DB)")
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        self.assertEqual(Path(r.stdout.strip()).name, "ohlcv_cache.db")

    def test_importing_it_under_an_override_creates_nothing_live(self):
        """Import alone must not touch the real data directory."""
        live = REPO / "data" / "ohlcv_cache.db"
        before = live.stat().st_mtime if live.exists() else None
        with tempfile.TemporaryDirectory(prefix="jarvis-store-") as d:
            r = _child({"JARVIS_OHLCV_DB_PATH": str(Path(d) / "o.db"),
                        "JARVIS_DB_PATH": str(Path(d) / "jarvis.db")},
                       "import lib.ohlcv_cache")
            self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        after = live.stat().st_mtime if live.exists() else None
        self.assertEqual(before, after, "the live OHLCV cache was touched")


class EveryWritableStoreIsEnumeratedTests(unittest.TestCase):
    """A guard against the next one. If a module gains a module-level
    sqlite path built from __file__, it must also gain an override."""

    def test_no_module_level_sqlite_path_without_an_override(self):
        import re
        offenders = []
        pat = re.compile(r"""^\s*[A-Z_]+\s*=\s*.*["'][\w.]+\.db["']""")
        for path in (REPO / "lib").rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            for n, line in enumerate(text.splitlines(), 1):
                if not pat.match(line):
                    continue
                if "os.environ" in text or "os.getenv" in text:
                    continue
                offenders.append(f"{path.name}:{n}: {line.strip()}")
        self.assertEqual(offenders, [], f"unredirectable store: {offenders}")


if __name__ == "__main__":
    unittest.main()
