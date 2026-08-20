"""Prove the commitment table can appear on a real book without disturbing it.

WHAT THIS CHECKS. `init_db()` creates missing tables and adds missing
columns. For a NEW table that is additive by construction — but "by
construction" is the kind of claim that has been wrong before, and the
database it runs against holds the canonical economy. So this measures it:

    1. fingerprint every table in the copy
    2. run the real init_db() against the copy
    3. fingerprint again

and then asserts that the ONLY difference is the new table and its indexes,
that no pre-existing table's contents moved, and that the economy is exactly
where it was. Finally it runs init_db() a second time to show the migration
is idempotent rather than merely survivable.

IT NEVER RUNS AGAINST THE OPERATOR DATABASE. Take a copy first:

    sqlite3 "file:$HOME/jarvis-trading/data/jarvis.db?mode=ro" ".backup /tmp/copy.db"
    python scripts/verify_commitment_migration.py /tmp/copy.db

The backup API is used rather than `cp` because the live file is being
written by collection and a plain copy can be torn.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
from pathlib import Path

# Tables larger than this are fingerprinted by count and boundary rows
# rather than by full content. Reported explicitly — a coverage limit that
# is not stated reads as coverage.
FULL_HASH_ROW_LIMIT = 200_000

# Tables this migration is EXPECTED to add. Additive-only migrations keep
# appending here; anything added that is NOT listed is reported as a
# surprise, because an unexpected new table is exactly what a review needs
# to see rather than a check that quietly widens to accommodate it.
EXPECTED_NEW_TABLES = {
    "virtual_execution_commitments",     # P0 durable commit boundary
    "dex_balances",                      # P0-3 virtual DEX wallet
    "dex_funding_events",                # P0-3 virtual-value provenance
}

# Columns that boot-time repair passes re-stamp even when they recompute
# the identical value. Excluded from the CONTENT fingerprint only, and any
# table that moves solely in these is reported rather than passed silently.
HEARTBEAT_COLUMNS = {"updated_at"}


def _tables(conn) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def _indexes(conn) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def _fingerprint(conn, table: str, sampled: list[str]) -> tuple[str, str]:
    """Two fingerprints: everything, and everything EXCEPT heartbeat stamps.

    init_db runs repair passes that stamp `updated_at` on every boot even
    when they recompute the same values. That is a heartbeat, not a change
    of economic content -- but the two must not be conflated, so they are
    measured separately and both reported. A difference in the second
    fingerprint is a real mutation.
    """
    count = conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
    cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
    beats = {i for i, c in enumerate(cols) if c in HEARTBEAT_COLUMNS}

    full = hashlib.sha256(f"{table}:{count}:".encode())
    content = hashlib.sha256(f"{table}:{count}:".encode())

    def absorb(rows):
        for row in rows:
            full.update(repr(row).encode("utf-8", "replace"))
            if beats:
                row = tuple(None if i in beats else v
                            for i, v in enumerate(row))
            content.update(repr(row).encode("utf-8", "replace"))

    if count <= FULL_HASH_ROW_LIMIT:
        absorb(conn.execute(f'SELECT * FROM "{table}"'))
    else:
        sampled.append(f"{table} ({count:,} rows)")
        for sql in (f'SELECT * FROM "{table}" LIMIT 1000',
                    f'SELECT * FROM "{table}" ORDER BY rowid DESC LIMIT 1000'):
            absorb(conn.execute(sql))
    return (f"{count}:{full.hexdigest()[:16]}",
            f"{count}:{content.hexdigest()[:16]}")


def _snapshot(path: Path) -> tuple[dict, list[str], list[str]]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        sampled: list[str] = []
        tables = _tables(conn)
        fps = {t: _fingerprint(conn, t, sampled) for t in tables}
        return fps, _indexes(conn), sampled
    finally:
        conn.close()


def _economy(path: Path) -> dict:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        cash = conn.execute("SELECT cash FROM paper_portfolio "
                            "LIMIT 1").fetchone()
        return {
            "cash": None if cash is None else float(cash[0]),
            "positions": conn.execute(
                "SELECT count(*) FROM paper_positions").fetchone()[0],
            "trades": conn.execute(
                "SELECT count(*) FROM paper_trades").fetchone()[0],
            "settlement_legs": conn.execute(
                "SELECT count(*) FROM paper_settlement_legs").fetchone()[0],
        }
    finally:
        conn.close()


def _run_init_db(path: Path) -> None:
    """Import the real app against this copy and run the real migration."""
    os.environ["JARVIS_DB_PATH"] = str(path)
    os.environ.pop("JARVIS_UNDER_PYTEST", None)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    for mod in [m for m in sys.modules if m.startswith("app.")]:
        del sys.modules[mod]
    from app.database import init_db
    init_db()


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    path = Path(argv[1]).resolve()
    if not path.exists():
        print(f"FAIL  no such database: {path}")
        return 1

    operator = Path.home() / "jarvis-trading" / "data" / "jarvis.db"
    if operator.exists() and path == operator.resolve():
        print("FAIL  refusing to run against the operator database; "
              "take a .backup copy first")
        return 1

    before, before_idx, sampled = _snapshot(path)
    econ_before = _economy(path)

    _run_init_db(path)

    after, after_idx, _ = _snapshot(path)
    econ_after = _economy(path)

    problems: list[str] = []

    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    unexpected = [t for t in added if t not in EXPECTED_NEW_TABLES]
    if unexpected:
        problems.append(f"UNEXPECTED tables added: {unexpected} "
                        f"(expected only {sorted(EXPECTED_NEW_TABLES)})")
    if removed:
        problems.append(f"tables REMOVED: {removed}")

    heartbeats: list[str] = []
    for table, (full_fp, content_fp) in before.items():
        if table not in after:
            continue
        after_full, after_content = after[table]
        if after_content != content_fp:
            problems.append(f"{table} CONTENT changed: {content_fp} -> "
                            f"{after_content}")
        elif after_full != full_fp:
            heartbeats.append(table)

    new_idx = sorted(set(after_idx) - set(before_idx))
    # Only assert indexes for tables that actually appeared in THIS run —
    # a database that already had the commitment table adds none.
    if "virtual_execution_commitments" in added and not any(
            i.startswith("ix_commitment") for i in new_idx):
        problems.append(f"the commitment table's indexes were not "
                        f"created: {new_idx}")

    if econ_before != econ_after:
        problems.append(f"the economy moved: {econ_before} -> {econ_after}")

    # Idempotency: a second run must be a complete no-op.
    _run_init_db(path)
    again, again_idx, _ = _snapshot(path)
    moved = [t for t in again
             if t in after and again[t][1] != after[t][1]]
    if moved:
        problems.append(f"re-running the migration changed content: {moved}")
    if sorted(again_idx) != sorted(after_idx):
        problems.append("re-running the migration changed the indexes")

    print(f"DATABASE          {path}")
    print(f"TABLES            {len(before)} -> {len(after)}")
    print(f"TABLES ADDED      {added or 'none'}")
    print(f"INDEXES ADDED     {new_idx or 'none'}")
    print(f"ECONOMY BEFORE    {econ_before}")
    print(f"ECONOMY AFTER     {econ_after}")
    print(f"IDEMPOTENT        {'yes' if not moved else 'NO'}")
    if heartbeats:
        print(f"HEARTBEAT ONLY    {', '.join(heartbeats)}: updated_at was "
              f"re-stamped by a boot repair pass; every other column is "
              f"byte-identical")
    if sampled:
        print(f"SAMPLED NOT HASHED IN FULL ({FULL_HASH_ROW_LIMIT:,}+ rows): "
              f"{', '.join(sampled)}")
    if problems:
        print("\nRESULT            FAIL")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("\nRESULT            OK - the table appeared and nothing else moved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
