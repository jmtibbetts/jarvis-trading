"""Where JARVIS is allowed to keep state.

THE BOUNDARY. One machine hosts Windows and Linux; that does not give JARVIS
two runtimes. The repository, the Python, the scheduler, the collectors, the
databases and the economy live on Linux-native storage. Windows hosts the
GPU and LM Studio, and talks to JARVIS over HTTP. Nothing about that
exception gives Windows authority over persistent state.

WHY THIS IS ENFORCED IN CODE. SQLite's correctness depends on filesystem
semantics — advisory locking, fsync, atomic rename. Through WSL's 9p/drvfs
translation to NTFS those are slower and weaker, and the failure mode is not
a clean error: it is a corrupted book, or a lock that silently does not
lock. An operator who one day exports `JARVIS_DB_PATH=/mnt/c/...` because it
is convenient should be refused loudly, not quietly served.

FILESYSTEM, NOT STRING PREFIX. Checking for "/mnt/c" catches the obvious
case and misses the one that matters: a symlink sitting innocently at
`~/jarvis-trading/data` pointing at `/mnt/c/...`. So the path is resolved
through symlinks first, then matched against the longest mount point in
`/proc/self/mountinfo`, and the verdict comes from the MOUNT's filesystem
type. A symlink cannot hide the storage underneath it.

WINDOWS REMAINS TESTABLE. JARVIS may be run under pytest on Windows — that
is useful and is not production. On a platform with no `/proc/self/mountinfo`
this reports UNKNOWN and declines to refuse, because refusing there would
say something false: it cannot see the filesystem, so it does not claim to.
The refusal is for Linux, where the answer is knowable and where the
operator actually runs.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

WINDOWS_BACKED_PERSISTENCE_FORBIDDEN = "WINDOWS_BACKED_PERSISTENCE_FORBIDDEN"

# Filesystem types that mean "this is really Windows storage, reached
# through a translation layer". 9p and drvfs are WSL's two mechanisms;
# ntfs/ntfs3/fuseblk appear when NTFS is mounted directly; cifs is a
# Windows network share.
WINDOWS_BACKED_FSTYPES = frozenset({
    "9p", "drvfs", "cifs", "smbfs", "smb3", "ntfs", "ntfs3", "fuseblk",
    "prjfs", "v9fs",
})

MOUNTINFO = "/proc/self/mountinfo"


@dataclass(frozen=True)
class PathVerdict:
    """What this path actually sits on."""
    path: str
    resolved: str
    fstype: str | None
    mount_point: str | None
    windows_backed: bool
    knowable: bool          # False when the platform cannot tell us

    @property
    def safe(self) -> bool:
        """Unknowable is not unsafe — see the module docstring."""
        return not self.windows_backed


def _mounts() -> list[tuple[str, str]]:
    """(mount_point, fstype), longest mount point first.

    Read from mountinfo rather than /etc/mtab because mountinfo is the
    kernel's own view and includes bind mounts.
    """
    try:
        raw = Path(MOUNTINFO).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[tuple[str, str]] = []
    for line in raw.splitlines():
        # ... <mount point> ... - <fstype> <source> <opts>
        parts = line.split()
        if "-" not in parts:
            continue
        sep = parts.index("-")
        if sep + 1 >= len(parts) or sep < 5:
            continue
        out.append((parts[4], parts[sep + 1]))
    out.sort(key=lambda kv: len(kv[0]), reverse=True)
    return out


def _posix(text: str) -> str:
    """Compare mount points in one spelling. Windows pathlib renders a
    POSIX-looking path with backslashes, and the mount table is POSIX."""
    return str(text).replace("\\", "/")


def _fstype_of(resolved: str) -> tuple[str | None, str | None]:
    mounts = _mounts()          # already longest-first
    if not mounts:
        return None, None
    target = _posix(resolved)
    best = None
    for point, fstype in mounts:
        point_p = _posix(point)
        if target == point_p or target.startswith(point_p.rstrip("/") + "/"):
            return fstype, point
        if point_p == "/":
            best = (fstype, point)          # root matches last, never first
    return best if best else (None, None)


def _resolve(path: str | os.PathLike) -> str:
    """Follow symlinks where they exist; normalise lexically where they do
    not. A database is judged before it is created, and the directory that
    will hold it is what decides — but the path must keep its own spelling
    so a substituted mount table can be reasoned about on any platform."""
    p = Path(path)
    if os.name == "nt":
        # Symlink resolution is a LINUX concern — that is where the guard
        # operates and where the mount table exists. On Windows, resolving
        # a POSIX-looking path prepends a drive letter and destroys the very
        # spelling a substituted mount table needs to reason about, so the
        # path is normalised lexically and left alone.
        return _posix(str(p))
    probe = p
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if probe == probe.parent and not probe.exists():
        return os.path.normpath(str(p))     # nothing exists: lexical only
    try:
        real_ancestor = str(probe.resolve())
    except OSError:
        return os.path.normpath(str(p))
    if real_ancestor == str(probe):
        return os.path.normpath(str(p))     # no symlink in play
    tail = str(p)[len(str(probe)):]
    return os.path.normpath(real_ancestor + tail)


def inspect(path: str | os.PathLike) -> PathVerdict:
    """What filesystem is under this path, following symlinks."""
    resolved = _resolve(path)
    fstype, point = _fstype_of(resolved)
    return PathVerdict(
        path=str(path), resolved=resolved, fstype=fstype,
        mount_point=point,
        windows_backed=bool(fstype and fstype.lower() in WINDOWS_BACKED_FSTYPES),
        knowable=fstype is not None)


def assert_linux_native_runtime_path(path: str | os.PathLike,
                                     *, purpose: str) -> PathVerdict:
    """Refuse Windows-backed storage for a persistent runtime path.

    Raises RuntimeError naming the purpose and the resolved location, so the
    message says what to change rather than only that something is wrong.
    """
    verdict = inspect(path)
    if verdict.windows_backed:
        raise RuntimeError(
            f"{WINDOWS_BACKED_PERSISTENCE_FORBIDDEN}: {purpose} would live on "
            f"{verdict.fstype!r} storage at {verdict.resolved!r} "
            f"(mount {verdict.mount_point!r}). JARVIS keeps all active state "
            f"on Linux-native storage — SQLite's locking and fsync semantics "
            f"are weaker through the Windows translation layer, and the "
            f"failure mode is a corrupted book rather than a clean error. "
            f"Point it at a path under the Linux home instead.")
    return verdict


def runtime_report() -> dict:
    """One place to answer 'where is this actually running?'.

    Used by the startup self-check and the API so an operator never has to
    infer the answer from a log line.
    """
    import platform
    import sys

    repo = Path(__file__).resolve().parent.parent
    try:
        from app.database import DB_PATH
        db_path = str(DB_PATH)
    except Exception:                                # noqa: BLE001
        db_path = os.getenv("JARVIS_DB_PATH", "")

    stores = {"repo": str(repo), "database": db_path}
    for name, env, default in (
            ("evidence", "JARVIS_EVIDENCE_DB_PATH", repo / "data" / "forward_evidence.db"),
            ("events", "JARVIS_EVENTS_DB_PATH", repo / "data" / "events.db"),
            ("ohlcv", "JARVIS_OHLCV_DB_PATH", repo / "data" / "ohlcv_cache.db")):
        stores[name] = os.getenv(env) or str(default)

    verdicts = {k: inspect(v) for k, v in stores.items() if v}
    unsafe = {k: v for k, v in verdicts.items() if v.windows_backed}
    return {
        "os": platform.system(),
        "platform": platform.platform(),
        "python": sys.executable,
        "venv": sys.prefix,
        "repo_root": str(repo),
        "stores": {k: {"path": v.path, "resolved": v.resolved,
                       "fstype": v.fstype, "mount": v.mount_point,
                       "windows_backed": v.windows_backed,
                       "knowable": v.knowable}
                   for k, v in verdicts.items()},
        "canonical_runtime_linux": platform.system() == "Linux",
        "active_db_windows_backed": bool(unsafe),
        "unsafe_stores": sorted(unsafe),
    }
