"""Only one process may run the economic scheduler against a book.

THE FAILURE THIS PREVENTS. Two `main.py` processes pointed at the same
database both start APScheduler. Both run the paper job. Both evaluate the
same candidates, both open entries, both manage the same positions, both
project learning. APScheduler's `max_instances=1` does not help — it is a
per-process guard, and these are two processes. Nothing in the database says
who is in charge, so the duplication is silent: the book simply trades twice
and the operator sees one set of logs.

WHY A FILE LOCK AND NOT A LEASE TABLE. An advisory lock held on an open file
descriptor is released BY THE KERNEL when the holder exits, however it
exits — clean shutdown, SIGKILL, or a power-off followed by a reboot. A
lease row in the database cannot do that: it needs a heartbeat, an expiry,
and a takeover rule, and every one of those is a new way to be wrong. A
crashed process would leave a lease that looks alive until it times out, and
a paused one (SIGSTOP, a long GC, a suspended laptop) would let a second
process take over while the first is still holding open transactions.

The lock file sits BESIDE THE DATABASE, not in the repository, because the
database is the resource being protected. Two checkouts pointed at one book
contend correctly; one checkout serving two different books does not
contend at all, which is also correct.

SCOPE. This is a single-machine guard, which matches the thing it guards:
SQLite on a local filesystem. It is not a distributed lock and does not
pretend to be one — on a network filesystem the guarantee weakens exactly
as SQLite's own does.

WHAT A REFUSAL MEANS. The second process does not die. It serves the API and
the UI read-only-ish, with the scheduler in STANDBY, and says so. A desk
that cannot start a second window is worse than one that can look but not
act.
"""
from __future__ import annotations

import json
import logging
import os
import socket
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

ACQUIRED = "ACQUIRED"
STANDBY = "STANDBY"
UNAVAILABLE = "UNAVAILABLE"

LOCK_SUFFIX = ".scheduler.lock"


@dataclass
class Lease:
    """The result of asking to be the economic scheduler."""
    state: str
    path: str
    owner: dict | None = None
    detail: str = ""
    _handle: object = None

    @property
    def granted(self) -> bool:
        return self.state == ACQUIRED

    def release(self) -> None:
        """Explicit release. The kernel does this anyway when the process
        exits; this exists so a clean shutdown does not leave the next start
        racing against file-descriptor teardown."""
        if self._handle is None:
            return
        try:
            _unlock(self._handle)
        except OSError:
            pass
        finally:
            try:
                self._handle.close()
            except Exception:                       # noqa: BLE001
                pass
            self._handle = None


def _lock_path(db_path: str | os.PathLike | None = None) -> Path:
    """Beside the database it protects."""
    if db_path is None:
        from app.database import DB_PATH
        db_path = DB_PATH
    return Path(str(db_path) + LOCK_SUFFIX)


# Windows locks a BYTE RANGE from the current file position, so both
# processes must agree on which byte. It is also a MANDATORY lock there, so
# that byte is placed far past any data this file will ever hold — locking
# byte 0 would make the owner's own diagnostic write fail.
_WINDOWS_LOCK_OFFSET = 1 << 30


def _try_lock(fh) -> bool:
    """Non-blocking exclusive lock. True if we got it."""
    if os.name == "nt":
        import msvcrt
        try:
            fh.seek(_WINDOWS_LOCK_OFFSET)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fh) -> None:
    if os.name == "nt":
        import msvcrt
        try:
            fh.seek(_WINDOWS_LOCK_OFFSET)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    import fcntl
    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _read_owner(path: Path) -> dict | None:
    """Who says they hold it. Diagnostics only — the LOCK is the authority,
    never this file's contents, which may be stale or truncated."""
    try:
        raw = (path.read_text(encoding="utf-8", errors="ignore") or "").strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        return json.loads(raw) or None
    except ValueError:
        # The payload is padded, and on Windows sits in a file whose lock
        # byte lives far beyond it. Read the first JSON object and ignore
        # whatever follows.
        try:
            obj, _ = json.JSONDecoder().raw_decode(raw)
            return obj or None
        except ValueError:
            return None


def acquire(db_path: str | os.PathLike | None = None,
            *, now_iso: str | None = None) -> Lease:
    """Claim the right to run economic jobs against this database.

    Never raises: a filesystem that cannot support the lock returns
    UNAVAILABLE and the caller decides. Refusing to start the desk because
    the guard itself is unhappy would be a worse failure than the one it
    prevents — but UNAVAILABLE is reported honestly rather than being
    quietly treated as ACQUIRED.
    """
    path = _lock_path(db_path)
    # An advisory lock on Windows-backed storage is not a lock you can rely
    # on — which would silently defeat the single-scheduler guarantee.
    try:
        from lib.runtime_paths import assert_linux_native_runtime_path
        assert_linux_native_runtime_path(path, purpose="the scheduler lease")
    except ImportError:
        pass
    except RuntimeError as exc:
        return Lease(state=UNAVAILABLE, path=str(path), detail=str(exc))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(path, "a+", encoding="utf-8")
    except OSError as exc:
        return Lease(state=UNAVAILABLE, path=str(path),
                     detail=f"cannot open lock file: {exc}")

    if not _try_lock(fh):
        owner = _read_owner(path)
        try:
            fh.close()
        except Exception:                           # noqa: BLE001
            pass
        return Lease(state=STANDBY, path=str(path), owner=owner,
                     detail=("another process already runs the economic "
                             "scheduler for this database"))

    # Held. Record who we are, for the other process to report.
    me = {"pid": os.getpid(), "host": socket.gethostname(),
          "db": str(db_path or ""), "acquired_at": now_iso}
    try:
        fh.seek(0)
        if os.name != "nt":
            # Never truncate on Windows: the lock lives past the data.
            fh.truncate()
        payload = json.dumps(me)
        fh.write(payload + " " * 8)                 # pad over any longer prior
        fh.flush()
        os.fsync(fh.fileno())
    except OSError:
        pass                                        # the LOCK is what counts
    return Lease(state=ACQUIRED, path=str(path), owner=me, _handle=fh)


def current_owner(db_path: str | os.PathLike | None = None) -> dict | None:
    """For the API/UI: who claims the scheduler right now."""
    return _read_owner(_lock_path(db_path))
