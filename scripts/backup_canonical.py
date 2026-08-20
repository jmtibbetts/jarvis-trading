"""Back up the CURRENT canonical stores, online, without stopping anything.

WHY NOT `cp`. Every active store here runs in WAL mode, and a WAL database
is two files plus a shared-memory index. At the moment of writing this,
jarvis.db carried 12MB of committed transactions that lived ONLY in
jarvis.db-wal. Copying the .db alone would have produced a file that opens
cleanly, passes an integrity check, and is silently missing the most recent
work -- the worst possible failure, because nothing announces it.

SQLite's online backup API is used instead. It walks the database through
the engine rather than the filesystem, so it sees committed WAL content,
and it restarts itself if a writer changes pages underneath it. The source
is opened READ-ONLY and no checkpoint is forced: this must not alter the
runtime it is protecting.

THIS IS NOT A ROLLBACK POINT FOR THE LEGACY ECONOMY. It captures the
post-cutover canonical epoch as it exists now. The Aug-17 pre-WSL snapshots
remain separate, immutable, and are never a source here.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# The four ACTIVE stores, named the way lib/runtime_paths.py names them.
# Archives and cutover dry-runs are deliberately excluded: they are already
# immutable copies, and re-copying 1.2GB of them every backup buys nothing.
ROLES = (
    ("database", "jarvis.db", "canonical economy, decisions, collection"),
    ("evidence", "forward_evidence.db", "pre-cutover forward evidence"),
    ("events", "events.db", "event log"),
    ("ohlcv", "ohlcv_cache.db", "historical bar cache"),
)

# What the backup must be able to answer afterwards. Each entry is
# (label, sql). Anything that fails is recorded rather than skipped.
CANONICAL_CHECKS = (
    ("engine_epoch", "SELECT DISTINCT engine_epoch FROM decision_observations"),
    ("decision_observations", "SELECT count(*) FROM decision_observations"),
    ("outcomes", "SELECT count(*) FROM decision_observation_outcomes"),
    ("latest_observation", "SELECT max(decision_at) FROM decision_observations"),
    ("virtual_cash", "SELECT printf('%.2f', cash) FROM paper_portfolio LIMIT 1"),
    ("open_positions",
     "SELECT count(*) FROM paper_positions WHERE status='Open'"),
    ("paper_trades", "SELECT count(*) FROM paper_trades"),
    ("settlement_legs", "SELECT count(*) FROM paper_settlement_legs"),
    ("commitments", "SELECT count(*) FROM virtual_execution_commitments"),
    ("quote_samples", "SELECT count(*) FROM instrument_quote_samples"),
    ("latest_quote", "SELECT max(observed_at) FROM instrument_quote_samples"),
)

EVIDENCE_CHECKS = (
    ("trading_signals", "SELECT count(*) FROM trading_signals"),
    ("paper_positions", "SELECT count(*) FROM paper_positions"),
)


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _query(path: Path, checks) -> dict:
    """Read state back OUT of the finished backup, read-only."""
    out = {}
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        for label, sql in checks:
            try:
                row = conn.execute(sql).fetchone()
                out[label] = None if row is None else row[0]
            except sqlite3.Error as exc:
                out[label] = f"<unavailable: {exc}>"
    finally:
        conn.close()
    return out


def _integrity(path: Path) -> str:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return conn.execute("PRAGMA integrity_check;").fetchone()[0]
    finally:
        conn.close()


def backup_one(src: Path, dst: Path) -> dict:
    """One store, through the online backup API."""
    started = time.time()
    # READ-ONLY source: a backup must never be the thing that writes.
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    target = sqlite3.connect(str(dst))
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    elapsed = time.time() - started
    return {
        "seconds": round(elapsed, 1),
        "bytes": dst.stat().st_size,
        "sha256": _sha256(dst),
        "integrity_check": _integrity(dst),
        "completed_at": _utc(),
    }


def main(argv: list[str]) -> int:
    repo = Path(__file__).resolve().parent.parent
    data = repo / "data"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = Path(argv[1]) if len(argv) > 1 else Path.home() / "jarvis-backups"
    dest = root / f"post-cutover-{stamp}"

    missing = [f for _, f, _ in ROLES if not (data / f).exists()]
    if missing:
        print(f"FAIL  active store(s) absent: {missing}")
        return 1

    need = sum((data / f).stat().st_size for _, f, _ in ROLES)
    import shutil
    free = shutil.disk_usage(root.parent if root.exists() else Path.home()).free
    print(f"source total   {need / 1e9:.2f} GB")
    print(f"free space     {free / 1e9:.2f} GB")
    if free < need * 1.15:
        print("FAIL  not enough free space for a safe backup (need ~15% headroom)")
        return 1

    dest.mkdir(parents=True, exist_ok=False)
    print(f"destination    {dest}\n")

    manifest = {
        "kind": "post-cutover canonical baseline",
        "not_a_rollback_of": "the Aug-2026 pre-WSL snapshots, which remain separate",
        "created_at": _utc(),
        "source_repo": str(repo),
        "method": "sqlite3 online backup API (Connection.backup), source opened read-only",
        "stores": [],
    }

    ok = True
    for role, filename, description in ROLES:
        src = data / filename
        dst = dest / filename
        wal = src.with_name(filename + "-wal")
        wal_bytes = wal.stat().st_size if wal.exists() else 0
        print(f"  {filename} ... ", end="", flush=True)
        try:
            result = backup_one(src, dst)
        except Exception as exc:                        # noqa: BLE001
            print(f"FAILED: {exc}")
            manifest["stores"].append({"role": role, "source": str(src),
                                       "error": str(exc)})
            ok = False
            continue

        entry = {
            "role": role,
            "description": description,
            "source_path": str(src),
            "source_bytes": src.stat().st_size,
            # Recorded because it is the number that makes `cp` unsafe: this
            # much committed data was living only in the -wal sidecar.
            "source_wal_bytes_at_backup": wal_bytes,
            "backup_path": str(dst),
            **result,
        }
        if role == "database":
            entry["verified_state"] = _query(dst, CANONICAL_CHECKS)
        elif role == "evidence":
            entry["verified_state"] = _query(dst, EVIDENCE_CHECKS)
        manifest["stores"].append(entry)

        status = result["integrity_check"]
        if status != "ok":
            ok = False
        print(f"{result['bytes'] / 1e9:.2f} GB  integrity={status}  "
              f"{result['seconds']}s")

    (dest / "MANIFEST.json").write_text(json.dumps(manifest, indent=2),
                                        encoding="utf-8")
    print(f"\nmanifest       {dest / 'MANIFEST.json'}")

    print("\nverified FROM THE BACKUP, not from the source:")
    for entry in manifest["stores"]:
        for label, value in (entry.get("verified_state") or {}).items():
            print(f"  {entry['role']:10s} {label:24s} {value}")

    print("\nRESULT         " + ("OK" if ok else "FAIL - see integrity above"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
