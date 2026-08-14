"""Hermetic test root — every pytest run gets its own throwaway database.

This file runs before any test imports application code, which is the only
moment the redirect can happen: app/database.py builds its engine at import
time. Two incidents made this structural rather than advisory — a test
reset the active paper book, and fixture rows later leaked into the live
candidate tables despite per-test cleanup. The guard in
app.database._resolve_db_path refuses the operator DB outright while
JARVIS_UNDER_PYTEST=1, so a test file that forgets everything below still
cannot touch real state.

The scheduler is disabled for the same reason: a test importing main must
never start background jobs against anything.
"""
import os
import tempfile
from pathlib import Path

os.environ["JARVIS_UNDER_PYTEST"] = "1"
os.environ.setdefault("JARVIS_DISABLE_SCHEDULER", "1")

if not os.getenv("JARVIS_DB_PATH", "").strip():
    _tmp = Path(tempfile.mkdtemp(prefix="jarvis-test-db-"))
    os.environ["JARVIS_DB_PATH"] = str(_tmp / "test.db")


def pytest_configure(config):
    """Create the full schema once per session, exactly as the app would."""
    from app.database import DB_PATH, init_db

    assert "jarvis-test-db-" in str(DB_PATH) or os.getenv("JARVIS_ALLOW_OPERATOR_DB") == "1", (
        f"test session resolved to unexpected DB: {DB_PATH}"
    )
    init_db()
