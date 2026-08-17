"""Reconcile the live databases against a pre-migration baseline.

WHY THIS EXISTS. The migration snapshot recorded size, SHA-256 and
`PRAGMA integrity_check` for every store, and its own manifest says
"integrity checks and row counts are the verification that matters" - then
records no row counts at all. Integrity proves a file is a well-formed
SQLite database. It says nothing about whether it still holds your data. A
copy that lost half a table passes integrity_check without complaint.

So this compares CONTENT: every table, every column, every row count,
baseline against live.

WHAT IS EXPECTED. Growth. The desk has been running since the migration, so
signals, outcomes and events all increase, and tables created by a newer
schema appear on the live side only.

WHAT IS NOT. Any table that existed in the baseline and is gone. Any column
that existed and is gone. Any row count that went DOWN - a table cannot
shrink by being used, only by being truncated, pruned or lost, and a
migration is precisely when "lost" becomes possible.

READ-ONLY, both sides. Every connection is opened with mode=ro. A probe
that opened the operator database read-write destroyed dex_portfolios once
already, and a verification tool that can damage what it verifies is worse
than no verification.

    python scripts/reconcile_db.py --baseline /mnt/c/jarvis-backups/pre-wsl-migration
    python scripts/reconcile_db.py --baseline <dir> --skip-cache
    python scripts/reconcile_db.py --baseline <dir> --json report.json

Exit code is 0 only when nothing was lost.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "data"

STORES = [
    ("jarvis.db", "operator state - signals, outcomes, books, wallets", False),
    ("events.db", "raw event store", False),
    ("ohlcv_cache.db", "OHLCV cache (rebuildable)", True),
]


def ro(path: Path) -> sqlite3.Connection:
    """A connection that cannot write, even by accident."""
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
    return [r[0] for r in rows]


def columns(conn: sqlite3.Connection, table: str) -> set[str]:
    # PRAGMA rather than parsing the DDL: a column added by ALTER TABLE does
    # not necessarily appear the way the original CREATE spelled it.
    return {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}


def count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]


def integrity(conn: sqlite3.Connection) -> str:
    return conn.execute("PRAGMA integrity_check").fetchone()[0]


def reconcile(name: str, live_path: Path, base_path: Path) -> dict:
    result = {
        "name": name,
        "live_path": str(live_path),
        "baseline_path": str(base_path),
        "live_integrity": None,
        "baseline_integrity": None,
        "tables_lost": [],
        "tables_added": [],
        "columns_lost": {},
        "shrunk": [],
        "grew": [],
        "unchanged": [],
        "errors": [],
    }
    live, base = ro(live_path), ro(base_path)
    try:
        result["live_integrity"] = integrity(live)
        result["baseline_integrity"] = integrity(base)

        live_tables, base_tables = set(tables(live)), set(tables(base))
        result["tables_lost"] = sorted(base_tables - live_tables)
        result["tables_added"] = sorted(live_tables - base_tables)

        for table in sorted(base_tables & live_tables):
            lost_cols = sorted(columns(base, table) - columns(live, table))
            if lost_cols:
                result["columns_lost"][table] = lost_cols
            try:
                b, l = count(base, table), count(live, table)
            except sqlite3.DatabaseError as e:
                result["errors"].append(f"{table}: {e}")
                continue
            entry = {"table": table, "baseline": b, "live": l, "delta": l - b}
            if l < b:
                result["shrunk"].append(entry)
            elif l > b:
                result["grew"].append(entry)
            else:
                result["unchanged"].append(entry)
    finally:
        live.close()
        base.close()
    return result


def lost_anything(r: dict) -> bool:
    return bool(r["tables_lost"] or r["columns_lost"] or r["shrunk"]
                or r["errors"]
                or r["live_integrity"] != "ok"
                or r["baseline_integrity"] != "ok")


def report(r: dict) -> None:
    print(f"\n=== {r['name']} ===")
    print(f"  integrity        live={r['live_integrity']}  baseline={r['baseline_integrity']}")
    print(f"  tables           {len(r['unchanged']) + len(r['grew']) + len(r['shrunk'])} in common, "
          f"{len(r['tables_added'])} new, {len(r['tables_lost'])} lost")

    if r["tables_lost"]:
        print("  TABLES LOST      " + ", ".join(r["tables_lost"]))
    if r["columns_lost"]:
        for t, cols in sorted(r["columns_lost"].items()):
            print(f"  COLUMNS LOST     {t}: {', '.join(cols)}")
    if r["shrunk"]:
        print("  ROWS LOST")
        for e in sorted(r["shrunk"], key=lambda e: e["delta"]):
            print(f"    {e['table']:<38} {e['baseline']:>12,} -> {e['live']:>12,}  ({e['delta']:+,})")
    if r["errors"]:
        for e in r["errors"]:
            print(f"  ERROR            {e}")

    grew = sorted(r["grew"], key=lambda e: -e["delta"])
    if grew:
        print(f"  grew             {len(grew)} table(s); largest:")
        for e in grew[:8]:
            print(f"    {e['table']:<38} {e['baseline']:>12,} -> {e['live']:>12,}  ({e['delta']:+,})")
    if r["tables_added"]:
        print(f"  new tables       {', '.join(r['tables_added'][:12])}"
              + (" ..." if len(r["tables_added"]) > 12 else ""))
    print(f"  unchanged        {len(r['unchanged'])} table(s)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--baseline", required=True, type=Path,
                    help="directory holding the pre-migration copies")
    ap.add_argument("--skip-cache", action="store_true",
                    help="skip ohlcv_cache.db (large, and rebuildable)")
    ap.add_argument("--json", type=Path, help="also write the full report here")
    args = ap.parse_args()

    if not args.baseline.is_dir():
        print(f"baseline directory not found: {args.baseline}", file=sys.stderr)
        return 2

    results, missing = [], []
    for name, _what, rebuildable in STORES:
        if rebuildable and args.skip_cache:
            print(f"skipping {name} (--skip-cache)")
            continue
        live, base = DATA / name, args.baseline / name
        if not live.exists():
            missing.append(f"live {live} is absent")
            continue
        if not base.exists():
            missing.append(f"baseline {base} is absent")
            continue
        results.append(reconcile(name, live, base))

    for r in results:
        report(r)

    if args.json:
        args.json.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"\nfull report: {args.json}")

    print("\n=== verdict ===")
    for m in missing:
        print(f"  MISSING  {m}")
    bad = [r for r in results if lost_anything(r)]
    for r in bad:
        print(f"  LOSS     {r['name']} - see above")
    if not bad and not missing:
        print("  Nothing was lost. Every baseline table and column is present, "
              "and no row count went down.")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
