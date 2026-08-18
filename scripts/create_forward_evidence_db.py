"""Seed the SHADOW RESEARCH database for prospective evidence collection.

WHY A SEED RATHER THAN AN EMPTY DB. The decision pipeline reads reference
and config substrate — watchlists, provider state, strategy config, signal
history — that an empty schema does not have. Seeding from a point-in-time
copy keeps the evaluator running against the same substrate as production
without the operator's economic book ever being opened for writing.

WHY THE BACKUP API AND NOT `cp`. The operator DB runs in WAL mode. Copying
the main file while a writer holds uncommitted pages in the -wal gives a
torn database that opens fine and is quietly wrong. `sqlite3.Connection
.backup()` takes a consistent snapshot under SQLite's own locking.

WHAT THIS DATABASE IS NOT. It is not the future clean canonical trading
epoch. It carries copied legacy rows — 667 positions, old outcomes, old
signals — as COMPATIBILITY SUBSTRATE ONLY. Those rows are not prospective
research observations, and prospective queries must filter on the evidence
epoch and the activation boundary rather than assuming the table is empty.
Do not silently promote this file to the active economic database.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REPO = Path(__file__).resolve().parent.parent
SOURCE = REPO / "data" / "jarvis.db"
TARGET = REPO / "data" / "forward_evidence.db"


def _now():
    return datetime.now(timezone.utc)


def _sha256(p: Path, limit_mb: int = 512) -> str:
    """Checksum of the file. Bounded read so a 400MB DB stays quick."""
    h = hashlib.sha256()
    with open(p, "rb") as f:
        read = 0
        while chunk := f.read(1 << 20):
            h.update(chunk)
            read += len(chunk)
            if read >= limit_mb * (1 << 20):
                h.update(b"<truncated>")
                break
    return h.hexdigest()


def _counts(path: Path) -> dict:
    uri = f"file:{path}?mode=ro"
    with sqlite3.connect(uri, uri=True) as c:
        def one(q, d=None):
            try:
                return c.execute(q).fetchone()[0]
            except sqlite3.Error:
                return d
        return {
            "trade_outcomes": one("SELECT COUNT(*) FROM trade_outcomes"),
            "paper_positions": one("SELECT COUNT(*) FROM paper_positions"),
            "paper_trades": one("SELECT COUNT(*) FROM paper_trades"),
            "cash": one("SELECT cash FROM paper_portfolio LIMIT 1"),
            "total_trades": one("SELECT total_trades FROM paper_portfolio LIMIT 1"),
        }


def main() -> dict:
    if not SOURCE.exists():
        raise SystemExit(f"operator DB not found: {SOURCE}")

    before = {"path": str(SOURCE), "size": SOURCE.stat().st_size,
              "sha256": _sha256(SOURCE), "counts": _counts(SOURCE)}
    print("SOURCE BEFORE:", json.dumps(before["counts"]))
    print("  sha256:", before["sha256"][:16], "size:", before["size"])

    if TARGET.exists():
        raise SystemExit(f"{TARGET} already exists — refusing to overwrite an "
                         f"evidence database that may already hold research rows")

    # ── consistent snapshot, source opened READ-ONLY ─────────────────────
    started = _now()
    src = sqlite3.connect(f"file:{SOURCE}?mode=ro", uri=True)
    dst = sqlite3.connect(str(TARGET))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    print(f"backup complete -> {TARGET} ({TARGET.stat().st_size:,} bytes)")

    # ── current schema onto the COPY only ────────────────────────────────
    os.environ["JARVIS_DB_PATH"] = str(TARGET)
    os.environ["JARVIS_DISABLE_MARKET_DATA"] = "1"
    from app.database import init_db
    init_db()
    init_db()          # idempotent rerun

    with sqlite3.connect(str(TARGET)) as c:
        integrity = c.execute("PRAGMA integrity_check").fetchone()[0]
        have = {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        edge_cols = {r[1] for r in c.execute(
            "PRAGMA table_info(decision_observations)")}

    required = {"decision_observations", "decision_observation_outcomes",
                "instrument_quote_samples"}
    missing = required - have
    edge_required = {"net_expected_r_lower", "robust", "edge_gate_role",
                     "robust_distance_to_threshold_r", "expectancy_bucket"}
    edge_missing = edge_required - edge_cols

    after = {"path": str(SOURCE), "size": SOURCE.stat().st_size,
             "sha256": _sha256(SOURCE), "counts": _counts(SOURCE)}
    unchanged = (before["sha256"] == after["sha256"]
                 and before["counts"] == after["counts"])
    print("SOURCE AFTER :", json.dumps(after["counts"]))
    print("OPERATOR DB UNCHANGED:", unchanged)

    result = {
        "created_at": started.isoformat(),
        "source_path": str(SOURCE),
        "source_sha256": before["sha256"],
        "source_size": before["size"],
        "source_counts_before": before["counts"],
        "source_counts_after": after["counts"],
        "operator_db_unchanged": unchanged,
        "target_path": str(TARGET),
        "target_size": TARGET.stat().st_size,
        "integrity_check": integrity,
        "evidence_tables_present": sorted(required - missing),
        "missing_tables": sorted(missing),
        "missing_edge_columns": sorted(edge_missing),
        "role": "FORWARD_EVIDENCE / SHADOW RESEARCH — NOT the canonical epoch",
        "legacy_substrate": {
            "note": "copied legacy rows are compatibility substrate only, "
                    "never prospective observations",
            **_counts(TARGET),
        },
    }
    if missing or edge_missing or integrity != "ok" or not unchanged:
        raise SystemExit(f"REFUSING: {json.dumps(result, indent=2)}")
    return result


if __name__ == "__main__":
    r = main()
    out = REPO / "data" / "reports"
    out.mkdir(parents=True, exist_ok=True)
    ts = _now().strftime("%Y%m%dT%H%M%SZ")
    (out / f"forward_evidence_db_created_{ts}.json").write_text(
        json.dumps(r, indent=2))
    print(json.dumps(r, indent=2))
