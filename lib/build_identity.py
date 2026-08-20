"""WHICH CODE IS THIS PROCESS ACTUALLY RUNNING?

THE DEFECT THIS CLOSES. `/api/system/version` answered `backend_commit` by
shelling out to `git rev-parse HEAD` AT REQUEST TIME, in the repository
directory. That reports what the REPOSITORY is on, which is not what the
running Python process loaded — the two diverge the instant anyone commits,
pulls or checks out while the server keeps running.

It was not hypothetical. On 2026-08-20 a server that had loaded `ae5bab9`
reported `ac77450`, because the repository had advanced after the process
started. A deploy check that asks "are you running the new code?" got back
"yes" from a server running the old code, and only the process start time
and a behavioural probe disproved it. A build identity that changes without
the build changing is not an identity.

THE INVARIANT: PROCESS IDENTITY IS IMMUTABLE FOR THE LIFE OF THE PROCESS.

    loaded_backend_commit        captured ONCE, at import. Never re-read.
    repository_head_commit       live, re-read per call. May legitimately
                                 differ, and says so.
    code_matches_repository_head comparison, or None when either is unknown

The two are deliberately named for what they are. A field called
`backend_commit` that means "whatever the repo is on right now" was worse
than having no field at all, because it was trusted.

A CONTAINER WITHOUT .git IS A NORMAL DEPLOYMENT, NOT A FAULT. When git
cannot answer, `JARVIS_BUILD_COMMIT` is consulted, then the answer is
UNKNOWN — never a fabricated or empty SHA.
"""
from __future__ import annotations

import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

UNKNOWN = "unknown"
BUILD_COMMIT_ENV = "JARVIS_BUILD_COMMIT"

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _git_head(cwd: Path) -> str:
    """Current HEAD, or UNKNOWN. Never raises, never invents."""
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=cwd,
            stderr=subprocess.DEVNULL, timeout=5).decode().strip()
        return out or UNKNOWN
    except Exception:                                        # noqa: BLE001
        return UNKNOWN


def _capture_loaded_commit() -> str:
    """Resolve the loaded commit ONCE, at import time.

    An explicitly built artifact names itself through the environment; a
    working tree is identified by its HEAD at the moment this module was
    imported, which is the moment the process's code was read from disk.
    """
    declared = (os.getenv(BUILD_COMMIT_ENV) or "").strip()
    if declared:
        return declared
    return _git_head(_REPO_ROOT)


# CAPTURED AT IMPORT. This module-level binding is the whole mechanism:
# nothing below ever recomputes it, and no later git operation can move it.
LOADED_BACKEND_COMMIT = _capture_loaded_commit()
LOADED_AT = datetime.now(timezone.utc).isoformat()


def loaded_backend_commit() -> str:
    """The commit whose code THIS PROCESS loaded. Immutable for its life."""
    return LOADED_BACKEND_COMMIT


def repository_head_commit() -> str:
    """What the repository is on RIGHT NOW. May differ; that is the point."""
    return _git_head(_REPO_ROOT)


def describe() -> dict:
    """Loaded identity, live repository state, and whether they agree."""
    head = repository_head_commit()
    loaded = LOADED_BACKEND_COMMIT
    known = UNKNOWN not in (head, loaded)
    return {
        "loaded_backend_commit": loaded,
        "loaded_at": LOADED_AT,
        "repository_head_commit": head,
        "code_matches_repository_head": (loaded == head) if known else None,
        "identity_source": (BUILD_COMMIT_ENV
                            if (os.getenv(BUILD_COMMIT_ENV) or "").strip()
                            else "GIT_HEAD_AT_IMPORT"),
        "note": ("loaded_backend_commit is captured once at import and never "
                 "re-read; repository_head_commit is live. A difference means "
                 "the repository advanced while this process kept running — "
                 "which is normal for a documentation commit and a genuine "
                 "mismatch for a code-bearing one."),
    }
