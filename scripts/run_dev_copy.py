"""Run a verification server against a COPY of the operator's database.

WHY THIS EXISTS, PLAINLY.

Twice in one session a second backend was started on an alternate port
against `data/jarvis.db` while the operator's own server was running. SQLite
allows exactly one writer; the two processes contended and the OPERATOR's
instance began returning

    sqlite3.OperationalError: database is locked

which surfaced to them as "API unreachable — the JARVIS server may be down".
Separately, an ad-hoc `python -c` probe that forgot to set `JARVIS_DB_PATH`
deleted and rewrote the live `dex_portfolios` row.

Both were the same mistake: reaching for the real database because it was
the default. Remembering not to is not a control. This is.

    python scripts/run_dev_copy.py                 # port 3010, DB copy
    python scripts/run_dev_copy.py --port 3020
    python scripts/run_dev_copy.py --keep          # keep the copy afterwards

The copy is made with SQLite's own backup API rather than a file copy, so a
snapshot taken while the operator's server is mid-write is still consistent.
Nothing this process does can reach the real file: `JARVIS_DB_PATH` and
`JARVIS_EVENTS_DB_PATH` are pointed at the copies before the app is
imported, which is the only moment that redirect can happen — `app.database`
builds its engine at import time.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_PORT = 3010


def _snapshot(src: Path, dst: Path) -> None:
    """Consistent copy of a live SQLite file, even mid-write.

    `shutil.copy` on a database with an active WAL can capture a torn state.
    The backup API takes a proper snapshot and is read-only against the
    source, so the operator's server is not disturbed.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not src.exists():
        print(f"  {src.name}: does not exist yet — starting from empty")
        return
    # Read-only URI: this process cannot write to the operator's file even
    # by accident.
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(str(dst))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    print(f"  {src.name}: {dst.stat().st_size / 1e6:,.0f} MB snapshot")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--keep", action="store_true",
                    help="keep the copies after exit (default: delete)")
    ap.add_argument("--dir", default=None,
                    help="where to put the copies (default: a temp dir)")
    args = ap.parse_args()

    if args.port == 3000:
        print("REFUSED: port 3000 is the operator's server. Pick another.",
              file=sys.stderr)
        return 2

    work = Path(args.dir) if args.dir else Path(
        tempfile.mkdtemp(prefix="jarvis-devcopy-"))
    print(f"Snapshotting operator data -> {work}")

    db_copy = work / "jarvis.db"
    events_copy = work / "events.db"
    _snapshot(REPO / "data" / "jarvis.db", db_copy)
    _snapshot(REPO / "data" / "events.db", events_copy)

    env = {
        **os.environ,
        "PORT": str(args.port),
        "JARVIS_DB_PATH": str(db_copy),
        "JARVIS_EVENTS_DB_PATH": str(events_copy),
        # A verification server must not run jobs: they would write trades,
        # spend provider quota and race the operator's real scheduler.
        "JARVIS_DISABLE_SCHEDULER": "1",
    }

    print(f"\nStarting verification server on http://localhost:{args.port}")
    print("  DB       :", db_copy)
    print("  scheduler: DISABLED")
    print("  operator DB is NOT reachable from this process\n")

    try:
        return subprocess.call([sys.executable, str(REPO / "main.py")],
                               cwd=str(REPO), env=env)
    except KeyboardInterrupt:
        return 0
    finally:
        if not args.keep and not args.dir:
            shutil.rmtree(work, ignore_errors=True)
            print(f"\nRemoved {work}")
        else:
            print(f"\nCopies kept at {work}")


if __name__ == "__main__":
    raise SystemExit(main())
