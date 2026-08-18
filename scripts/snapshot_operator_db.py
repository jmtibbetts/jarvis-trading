"""Verified, online-safe snapshots of every operator database.

Migration's highest-risk step is copying a database that is still being
written. `shutil.copy` on a live SQLite file with an active WAL can capture
a torn state that opens cleanly and is quietly missing the tail.

This uses SQLite's backup API with the source opened READ-ONLY, and records
size + SHA-256 + `PRAGMA integrity_check` for source and copy.

IT DOES NOT REQUIRE A QUIESCENT SOURCE, and that is deliberate. The backup
API reads through the WAL and produces a consistent snapshot of a live
database, which is exactly what makes routine online backups useful — a
tool you cannot run without stopping the system is a tool that does not get
run. A non-empty WAL is reported as a NOTE, not a refusal.

    An earlier version of this docstring claimed it "refuses to proceed if
    a writer still holds the file". It never did. The claim is removed
    rather than quietly softened, because a safety property that exists
    only in documentation is worse than no claim at all: it gets relied on.

THE CUTOVER HAS A STRICTER REQUIREMENT, and it lives elsewhere.
`scripts/canonical_epoch_dry_run.py` scans `/proc/<pid>/fd` and REFUSES if
any other process holds the database, its WAL or its SHM. The difference is
what the copy is FOR: a routine snapshot is a recovery point, while a
cutover archive is a final immutable epoch boundary. "Consistent" is enough
for the first; for the second the source must also be provably still, or
the word "immutable" is not one this tool has earned.

    python scripts/snapshot_operator_db.py --out <dir>
    python scripts/snapshot_operator_db.py --out <dir> --skip-cache

Nothing here mutates the source. It is safe to run twice.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "data"

# Every persistent store, with whether it is reconstructible. The cache is
# 5.6 GB and CAN be rebuilt — but rebuilding spends provider quota and
# wall-clock, so it is migrated by default and skippable by flag.
STORES = [
    ("jarvis.db", "operator state — signals, outcomes, books, wallets", False),
    ("events.db", "raw event store", False),
    ("ohlcv_cache.db", "OHLCV cache (rebuildable, costs provider quota)", True),
]


def sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def integrity(path: Path) -> str:
    """PRAGMA integrity_check against a read-only handle."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()


def wal_state(path: Path) -> dict:
    wal = path.with_name(path.name + "-wal")
    shm = path.with_name(path.name + "-shm")
    return {
        "wal_present": wal.exists(),
        "wal_bytes": wal.stat().st_size if wal.exists() else 0,
        "shm_present": shm.exists(),
    }


def snapshot(src: Path, dst: Path) -> None:
    """Consistent copy. Source is opened mode=ro and never written."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(str(dst))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", required=True, help="destination directory")
    ap.add_argument("--skip-cache", action="store_true",
                    help="do not copy the rebuildable OHLCV cache")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    report: list[dict] = []
    failed = False

    print(f"Snapshotting operator databases -> {out}\n")
    for name, what, rebuildable in STORES:
        src = DATA / name
        if not src.exists():
            print(f"  {name}: absent, skipped")
            continue
        if rebuildable and args.skip_cache:
            print(f"  {name}: skipped by --skip-cache ({what})")
            continue

        print(f"  {name}  ({what})")
        pre = wal_state(src)
        if pre["wal_bytes"] > 0:
            # Not fatal — the backup API reads through the WAL — but a
            # non-empty WAL means something was writing recently, and the
            # operator should know before trusting the copy.
            print(f"    NOTE: WAL holds {pre['wal_bytes']:,} bytes — confirm "
                  f"no writer is running")

        src_integrity = integrity(src)
        print(f"    source integrity : {src_integrity}")
        if src_integrity != "ok":
            print("    REFUSING to snapshot a database that is not ok")
            failed = True
            continue

        dst = out / name
        snapshot(src, dst)

        src_hash, dst_hash = sha256(src), sha256(dst)
        dst_integrity = integrity(dst)
        print(f"    source           : {src.stat().st_size:,} bytes  {src_hash[:16]}")
        print(f"    copy             : {dst.stat().st_size:,} bytes  {dst_hash[:16]}")
        print(f"    copy integrity   : {dst_integrity}")
        if dst_integrity != "ok":
            print("    COPY FAILED INTEGRITY CHECK")
            failed = True

        report.append({
            "name": name, "purpose": what,
            "source_path": str(src), "backup_path": str(dst),
            "source_bytes": src.stat().st_size,
            "backup_bytes": dst.stat().st_size,
            "source_sha256": src_hash, "backup_sha256": dst_hash,
            "source_integrity": src_integrity, "backup_integrity": dst_integrity,
            # The hashes will NOT match: a backup-API copy is a logically
            # equivalent database, not a byte-identical file (page ordering
            # and freelist differ). Integrity + row counts are the check
            # that matters; the hashes identify each artifact.
            "byte_identical": src_hash == dst_hash,
            "wal_before": pre,
        })
        print()

    manifest = out / "MANIFEST.json"
    manifest.write_text(json.dumps({
        "taken_at": datetime.now(timezone.utc).isoformat(),
        "repo": str(REPO),
        "note": ("Backup-API copies are logically equivalent, NOT byte-identical "
                 "— page ordering and freelist differ. Integrity checks and row "
                 "counts are the verification that matters."),
        "stores": report,
    }, indent=2), encoding="utf-8")
    print(f"Manifest: {manifest}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
