"""Prove the operator's economic state has not moved — without touching it.

WHY THIS EXISTS. A byte-level SHA of the operator database was observed to
differ between two readings and later return to its original value. Counts and
cash matched throughout, but "the counts match" is a weak claim: a table can
keep its row count while every row inside it changes. Two different questions
were being answered with one number, so they are separated here:

    file SHA          are these bytes identical?
    row fingerprint   did any state JARVIS economically cares about change?

WHY IT REFUSES TO USE app.database. That module's engine installs
`PRAGMA journal_mode=WAL` on connect. A verification tool that proves
immutability by opening the database in a mode that can rewrite its header is
not a verification tool. So this uses stdlib sqlite3 only, opens with
`mode=ro`, sets `query_only=ON`, and imports no ORM, no engine and no
migration path.

WHAT IT DELIBERATELY DOES NOT DO: no checkpoint, no VACUUM, no ANALYZE, no
journal-mode change, no temp table, no write of any kind. Reports go to
data/reports/; the database is only ever read.

Usage:  .venv/bin/python scripts/operator_db_fingerprint.py [--db PATH]
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
DEFAULT_DB = REPO / "data" / "jarvis.db"

# Tables whose contents ARE the economic claim. Deliberately not "every table":
# market data and caches change constantly and calling that an economic
# mutation would make the alarm useless.
#
# The B1 settlement ledger tables are ECONOMIC_MUTATION_SENSITIVE and listed
# here from the day the schema exists — but the OPERATOR database is
# deliberately not migrated yet, so on that DB they are reported truthfully
# as NOT_PRESENT_IN_THIS_SCHEMA rather than created, failed on, or padded to
# a fictitious row count of zero. Absence of a table is a fact about the
# schema; pretending otherwise would make the fingerprint a liar in exactly
# the tool whose only job is not lying.
ECONOMIC_TABLES = ("paper_positions", "paper_trades", "paper_portfolio",
                   "trade_outcomes",
                   "paper_position_settlements", "paper_settlement_legs")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canon(v) -> str:
    """One deterministic encoding per value, typed so 1 and '1' differ.

    Floats go through .hex() because decimal formatting is lossy and locale
    dependent — a fingerprint that rounds is a fingerprint that misses.
    """
    if v is None:
        return "\x00NULL"
    if isinstance(v, bool):
        return "\x00B" + ("1" if v else "0")
    if isinstance(v, int):
        return "\x00I" + str(v)
    if isinstance(v, float):
        return "\x00F" + float(v).hex()
    if isinstance(v, bytes):
        return "\x00X" + hashlib.sha256(v).hexdigest()
    return "\x00S" + str(v)


def _file_state(p: Path) -> dict:
    if not p.exists():
        return {"exists": False}
    st = p.stat()
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    st2 = p.stat()
    return {"exists": True, "size": st.st_size,
            "mtime": st.st_mtime, "sha256": h.hexdigest(),
            # If the file changed WHILE being hashed, the digest describes no
            # single state and must not be presented as though it did.
            "stable_during_hash": (st.st_size == st2.st_size
                                   and st.st_mtime == st2.st_mtime)}


def fingerprint(db_path: Path) -> dict:
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.execute("PRAGMA query_only=ON")
        # One consistent read snapshot for every table, so a concurrent writer
        # cannot make two tables describe different instants.
        conn.execute("BEGIN")
        present = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        out: dict = {}
        for table in ECONOMIC_TABLES:
            if table not in present:
                out[table] = {"present": False,
                              "status": "NOT_PRESENT_IN_THIS_SCHEMA"}
                continue
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
            pk = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")
                  if r[5]] or cols[:1]
            order = ", ".join(f'"{c}"' for c in pk)
            sel = ", ".join(f'"{c}"' for c in cols)
            h = hashlib.sha256()
            n = 0
            for row in conn.execute(f"SELECT {sel} FROM {table} ORDER BY {order}"):
                h.update("".join(_canon(v) for v in row).encode("utf-8"))
                h.update(b"\x01")
                n += 1
            out[table] = {
                "present": True, "rows": n,
                "columns": cols,
                "schema_sha256": hashlib.sha256(
                    "|".join(cols).encode()).hexdigest()[:16],
                "rows_sha256": h.hexdigest(),
            }
        cash = counters = None
        if "paper_portfolio" in present:
            pcols = [r[1] for r in conn.execute("PRAGMA table_info(paper_portfolio)")]
            want = [c for c in ("cash", "total_trades", "winning_trades",
                                "realized_pnl") if c in pcols]
            if want:
                row = conn.execute(
                    f"SELECT {', '.join(want)} FROM paper_portfolio LIMIT 1"
                ).fetchone()
                counters = dict(zip(want, row)) if row else None
                cash = counters.get("cash") if counters else None
        version = conn.execute("PRAGMA data_version").fetchone()[0]
        conn.rollback()
    finally:
        conn.close()
    return {"tables": out, "portfolio": counters, "cash": cash,
            "data_version": version,
            "sqlite_version": sqlite3.sqlite_version}


def snapshot(db_path: Path) -> dict:
    before = _file_state(db_path)
    logical = fingerprint(db_path)
    after = _file_state(db_path)
    return {
        "at": _now(),
        "db_path": str(db_path),
        "file_before": before,
        "file_after": after,
        "file_unchanged_by_probe": before.get("sha256") == after.get("sha256"),
        "wal": _file_state(Path(str(db_path) + "-wal")),
        "shm": _file_state(Path(str(db_path) + "-shm")),
        **logical,
    }


def compare(a: dict, b: dict) -> dict:
    changed = [t for t in ECONOMIC_TABLES
               if a["tables"].get(t, {}).get("rows_sha256")
               != b["tables"].get(t, {}).get("rows_sha256")]
    return {
        "economic_tables_changed": changed,
        "economic_state_unchanged": not changed,
        "cash_before": a.get("cash"), "cash_after": b.get("cash"),
        "file_sha_before": a["file_before"].get("sha256"),
        "file_sha_after": b["file_after"].get("sha256"),
        "file_bytes_identical": (a["file_before"].get("sha256")
                                 == b["file_after"].get("sha256")),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--compare-to", help="a previous fingerprint JSON")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"no such database: {db}", file=sys.stderr)
        return 2

    snap = snapshot(db)
    if args.compare_to:
        prior = json.loads(Path(args.compare_to).read_text())
        snap["comparison"] = compare(prior, snap)

    out = Path(args.out) if args.out else (
        REPO / "data" / "reports"
        / f"operator_db_fingerprint_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snap, indent=2, default=str))

    print(f"db          : {snap['db_path']}")
    print(f"file sha256 : {snap['file_before'].get('sha256', '')[:32]}")
    print(f"stable      : {snap['file_before'].get('stable_during_hash')}")
    print(f"probe wrote : {not snap['file_unchanged_by_probe'] and 'YES (BAD)' or 'no'}")
    for t, d in snap["tables"].items():
        if d.get("present"):
            print(f"  {t:<18} rows={d['rows']:<8} {d['rows_sha256'][:32]}")
        else:
            print(f"  {t:<18} ABSENT")
    print(f"cash        : {snap.get('cash')}")
    if "comparison" in snap:
        c = snap["comparison"]
        print(f"UNCHANGED   : {c['economic_state_unchanged']}  "
              f"changed={c['economic_tables_changed']}")
        print(f"bytes same  : {c['file_bytes_identical']}")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
