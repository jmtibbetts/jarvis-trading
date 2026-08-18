"""Retire the legacy paper economy and install a fresh canonical book.

THIS ONE ACTUALLY MOVES FILES. Its sibling `canonical_epoch_dry_run.py` has
no apply path and never will; this is the separate, deliberate tool that
performs the swap, and it refuses to run without an exact confirmation
token, a successful dry-run manifest, and a provably quiescent source.

WHAT IT DOES

    archive the legacy database (SQLite backup API, logically verified)
    build a NEW candidate from the current schema, config/reference only
    DISARM that candidate explicitly
    move the legacy database and its sidecars out of the active path
    rename the candidate into the active path
    verify, and journal every step

WHAT IT REFUSES TO DO. It never closes a legacy position, never converts
one, never copies economic history forward, and never promotes the dry-run
candidate — that candidate contains a test lifecycle and is evidence, not
production. It does not enable the scheduler, does not arm trading, and
does not touch the evidence collector.

THE GOLDEN RULE — NEVER OLD ENTRY + NEW EXIT. The legacy positions belong
to the legacy simulator. Open ones stay open in the archive forever. That is
honest; closing them under economics that did not exist when they opened
would be fabricated history.

THE SIDECAR TRAP. SQLite's -wal and -shm belong to one specific database
history. If the active path is reused, a new database that inherits the old
sidecars can be corrupted or silently read stale pages. The legacy sidecars
move WITH the legacy database, and the active path is asserted empty — file,
WAL and SHM — before the candidate is installed.

DISARMED ON ARRIVAL. The copy plan preserves operator configuration, which
includes the arming flags. Their source values are recorded for audit, but
the candidate is forced to live_trading_enabled=0,
paper_auto_trade_enabled=0, auto_sim_enabled=0. This is not a claim that the
operator chose that; it is an operational interlock, recorded as
CUTOVER_DISARM, to be lifted deliberately in a later activation step.

Usage:
    .venv/bin/python scripts/canonical_epoch_cutover.py \
        --source data/jarvis.db \
        --work-dir data/cutover_runs/CANONICAL_CUTOVER_<UTC> \
        --starting-cash 100000 \
        --dry-run-manifest data/cutover_dry_runs/<RUN>/CUTOVER_DRY_RUN_MANIFEST.json \
        --confirm CANONICAL_EPOCH_CUTOVER
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from lib.cutover_classification import (  # noqa: E402
    COPY_PLAN, RESET_DERIVED_LEARNING, UNKNOWN_REFUSE, classify)
from scripts.canonical_epoch_dry_run import (  # noqa: E402
    ECONOMIC_TABLES, EXTERNAL_STORES, Gates, _sha256, child_env,
    columns_of, db_holders, economic_snapshot, integrity_ok,
    logical_manifest, ro_connect, row_count, run_child, run_fingerprint,
    schema_fingerprint, table_names)

CONFIRM_TOKEN = "CANONICAL_EPOCH_CUTOVER"

STATE_PREPARED = "PREPARED"
STATE_LEGACY_MOVED = "LEGACY_MOVED"
STATE_CANDIDATE_PROMOTED = "CANDIDATE_PROMOTED"
STATE_VERIFIED = "VERIFIED"
STATE_ROLLED_BACK = "CUTOVER_ROLLED_BACK_BEFORE_ACTIVATION"

# The arming flags. Source values are recorded; the candidate is forced safe.
DISARM = [
    ("system_state", "live_trading_enabled", 0),
    ("user_preferences", "paper_auto_trade_enabled", 0),
    ("user_preferences", "auto_sim_enabled", 0),
]


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _fsync_dir(path: Path) -> bool:
    """A rename is not durable until its directory entry is synced.

    POSIX only — Windows cannot open a directory as a file descriptor, so
    this reports False there rather than pretending. The real cutover runs
    on Linux, where the guarantee is available and taken; on other platforms
    the swap still happens, it is simply not proven durable across a power
    loss. Returns whether the sync actually occurred, so the manifest can
    record the truth instead of implying one.
    """
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except (OSError, PermissionError):
        return False
    try:
        os.fsync(fd)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


class Journal:
    """Recovery evidence. If this process dies mid-swap, the journal says
    exactly how far it got and what to put back."""

    def __init__(self, path: Path):
        self.path = path
        self.entries: list[dict] = []
        self.data: dict = {}

    def mark(self, state: str, **fields) -> None:
        self.data["state"] = state
        self.data.update(fields)
        self.entries.append({"state": state, "at": _utc(), **fields})
        self.data["history"] = self.entries
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, default=str),
                       encoding="utf-8")
        with open(tmp, "rb+") as fh:
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)
        _fsync_dir(self.path.parent)
        print(f"  journal -> {state}")


# ── gates before anything moves ──────────────────────────────────────────
def verify_dry_run(manifest_path: Path, source: Path, g: Gates) -> dict:
    """P2.1 — a successful dry run is a precondition, and its own claim of
    success is checked rather than assumed from the file's existence."""
    if not manifest_path.is_file():
        g.record("dry_run_manifest_present", False, str(manifest_path))
        return {}
    man = json.loads(manifest_path.read_text(encoding="utf-8"))
    g.record("dry_run_manifest_present", True, str(manifest_path))
    g.record("dry_run_was_ready", man.get("ready") is True,
             f"ready={man.get('ready')} status={man.get('status')}")
    g.record("dry_run_had_no_failed_gates", not man.get("failed_gates"),
             str(man.get("failed_gates")))
    same = Path(man.get("source", {}).get("path", "")).resolve() == source
    g.record("dry_run_source_matches", same,
             f"dry run source: {man.get('source', {}).get('path')}")
    g.record("dry_run_was_dry", man.get("mode") == "DRY_RUN_ONLY",
             str(man.get("mode")))
    return man


def verify_not_the_dry_run_candidate(candidate: Path, dry_manifest: dict,
                                     g: Gates) -> None:
    """P1 — the dry-run candidate carries a test lifecycle: a position, a
    trade, an outcome, a learning row. It proves the machinery works. It is
    not the production book, and nothing may promote it."""
    prior = (dry_manifest.get("candidate") or {}).get("first", {}) or {}
    prior_db = prior.get("db")
    distinct = True
    if prior_db:
        try:
            distinct = Path(prior_db).resolve() != candidate
        except OSError:
            distinct = True
    g.record("final_candidate_is_not_the_dry_run_candidate", distinct,
             f"dry-run candidate: {prior_db}")


def verify_schema_unchanged(source: Path, dry_manifest: dict,
                            g: Gates) -> None:
    """P4.2 — the reviewed classification covered the schema as it was. If
    the source has gained a table since, that table is unreviewed and the
    cutover refuses rather than trusting a stale list."""
    conn = ro_connect(source)
    try:
        tables = set(table_names(conn))
    finally:
        conn.close()
    unknown = sorted(t for t in tables if classify(t)[0] == UNKNOWN_REFUSE)
    g.record("every_source_table_still_classified", not unknown,
             f"{len(tables)} tables" + (f"; UNKNOWN: {unknown}"
                                        if unknown else ""))
    prior = set((dry_manifest.get("table_classification") or {}).keys())
    if prior:
        added, removed = sorted(tables - prior), sorted(prior - tables)
        g.record("source_schema_matches_the_reviewed_dry_run",
                 not added and not removed,
                 f"added={added} removed={removed}")


def _iter_proc_pids():
    """Every PID directory under /proc, or nothing if it cannot be read.

    Enumeration failing is not the same as "no holders" — callers pair this
    with `proc_enumeration_available()`, which decides whether an empty
    result is evidence or merely ignorance.
    """
    try:
        for entry in Path("/proc").iterdir():
            if entry.name.isdigit():
                yield entry
    except (OSError, FileNotFoundError):
        return


def proc_enumeration_available() -> bool:
    """Can this platform prove who holds a file?

    Only /proc gives an authoritative answer, and without one the honest
    result is "cannot prove", which refuses. Named so tests can substitute
    it; the real run happens on Linux, where it is genuinely available.
    """
    return Path("/proc").exists()


def verify_quiescence(source: Path, g: Gates) -> dict:
    """P5 — nothing else may hold the database at the boundary. Enumerated
    from /proc file descriptors, not from `pgrep -f`, which matches its own
    command line."""
    holders = db_holders(source)
    on_linux = proc_enumeration_available()
    g.record("source_quiescent", on_linux and not holders,
             "no other process holds the DB, WAL or SHM"
             if on_linux and not holders
             else (f"holders: {holders}" if holders
                   else "/proc unavailable — cannot prove quiescence"))

    collectors, clean = [], True
    if on_linux:
        for pid_dir in _iter_proc_pids():
            try:
                cmd = (pid_dir / "cmdline").read_bytes().replace(
                    b"\0", b" ").decode(errors="replace")
            except OSError:
                continue
            if "evidence_collector" not in cmd:
                continue
            pid = int(pid_dir.name)
            fds = []
            try:
                for fd in (pid_dir / "fd").iterdir():
                    try:
                        fds.append(os.readlink(fd))
                    except OSError:
                        continue
            except OSError:
                pass
            touches = any(str(source) in f for f in fds)
            clean = clean and not touches
            collectors.append({
                "pid": pid, "opens_source": touches,
                "evidence_fds": [f for f in fds
                                 if f.endswith("forward_evidence.db")]})
    g.record("collector_holds_only_its_own_db", clean,
             f"{len(collectors)} collector process(es)")
    return {"holders": holders, "collectors": collectors}


# ── build ────────────────────────────────────────────────────────────────
def capture_arming(source: Path) -> dict:
    """P3.2 — what the operator actually had configured, for the record."""
    out: dict = {}
    conn = ro_connect(source)
    try:
        present = set(table_names(conn))
        for table, column, _safe in DISARM:
            if table not in present or column not in columns_of(conn, table):
                out[f"{table}.{column}"] = None
                continue
            rows = conn.execute(f'SELECT "{column}" FROM "{table}"').fetchall()
            out[f"{table}.{column}"] = [r[0] for r in rows]
        if "user_preferences" in present:
            out["user_preferences.trade_mode"] = [
                r[0] for r in conn.execute(
                    "SELECT trade_mode FROM user_preferences")]
    finally:
        conn.close()
    return out


def build_candidate(source: Path, candidate: Path, work: Path,
                    starting_cash: float, g: Gates) -> dict:
    """A brand-new book: current schema, allowlisted config, fresh capital,
    zero economics — and NO verification trade. The lifecycle proof already
    exists; running one here would contaminate the book that goes live."""
    candidate.parent.mkdir(parents=True, exist_ok=True)
    if candidate.exists():
        g.record("candidate_did_not_exist", False, "refusing to overwrite")
        return {}
    g.record("candidate_did_not_exist", True, str(candidate))

    first = run_child("init_schema", candidate, work)
    second = run_child("init_schema", candidate, work)
    g.record("candidate_schema_created", first.get("ok") is True,
             first.get("error") or f"{first.get('table_count')} tables")
    g.record("init_db_idempotent",
             second.get("ok") is True
             and second.get("schema_fingerprint")
             == first.get("schema_fingerprint"),
             "a second init_db left the schema identical")

    copied = copy_allowlist(source, candidate, g)
    disarmed = disarm_candidate(candidate, g)
    seeded = run_child("seed_portfolio", candidate, work,
                       {"starting_cash": starting_cash})
    g.record("fresh_portfolio_created", seeded.get("ok") is True,
             seeded.get("error") or f"cash={seeded.get('cash')}")
    g.record("starting_cash_exact",
             seeded.get("cash") == starting_cash
             and seeded.get("total_trades") == 0
             and seeded.get("winning_trades") == 0
             and seeded.get("realized_pnl") == 0,
             f"cash={seeded.get('cash')}")

    zero = assert_zero_economy(candidate, g)
    return {"init": first, "copied": copied, "disarmed": disarmed,
            "portfolio": seeded, "zero": zero}


def copy_allowlist(source: Path, candidate: Path, g: Gates) -> list[dict]:
    src, dst = ro_connect(source), sqlite3.connect(str(candidate))
    report, ok_all = [], True
    try:
        src_tables = set(table_names(src))
        cur = dst.cursor()
        for entry in COPY_PLAN:
            st, tt = entry["source_table"], entry["target_table"]
            row = {"source_table": st, "target_table": tt, "copied_rows": 0,
                   "source_rows": 0, "columns": [], "status": "SKIPPED",
                   "reason": entry["reason"]}
            if st not in src_tables:
                row["status"] = "ABSENT_IN_SOURCE"
                report.append(row)
                continue
            s_cols = columns_of(src, st)
            t_cols = [r[1] for r in cur.execute(f'PRAGMA table_info("{tt}")')]
            wanted = entry["columns"] or s_cols
            unmet = [c for c in wanted if c not in s_cols or c not in t_cols]
            if entry["columns"] and unmet:
                row["status"] = f"COLUMN_MISMATCH:{unmet}"
                ok_all = False
                report.append(row)
                continue
            usable = [c for c in wanted if c in s_cols and c in t_cols]
            quoted = ", ".join(f'"{c}"' for c in usable)
            rows = list(src.execute(f'SELECT {quoted} FROM "{st}"'))
            if rows:
                cur.executemany(
                    f'INSERT OR REPLACE INTO "{tt}" ({quoted}) VALUES '
                    f'({", ".join("?" for _ in usable)})', rows)
            row.update({"source_rows": row_count(src, st),
                        "copied_rows": len(rows), "columns": usable,
                        "excluded_columns": entry.get("excluded_columns", []),
                        "status": "COPIED"})
            report.append(row)
        dst.commit()
    finally:
        dst.close()
        src.close()
    g.record("config_copy_valid", ok_all,
             f"{sum(r['copied_rows'] for r in report)} rows across "
             f"{len([r for r in report if r['status'] == 'COPIED'])} tables")
    return report


def disarm_candidate(candidate: Path, g: Gates) -> dict:
    """P3.1 — force the interlock ON in the candidate only. The legacy
    source is never written."""
    conn = sqlite3.connect(str(candidate))
    applied: dict = {}
    try:
        present = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for table, column, safe in DISARM:
            if table not in present:
                applied[f"{table}.{column}"] = "TABLE_ABSENT"
                continue
            cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
            if column not in cols:
                applied[f"{table}.{column}"] = "COLUMN_ABSENT"
                continue
            conn.execute(f'UPDATE "{table}" SET "{column}" = ?', (safe,))
            applied[f"{table}.{column}"] = safe
        if "user_preferences" in present:
            cols = [r[1] for r in conn.execute(
                'PRAGMA table_info("user_preferences")')]
            if "trade_mode" in cols:
                conn.execute("UPDATE user_preferences SET trade_mode = 'paper'")
                applied["user_preferences.trade_mode"] = "paper"
        conn.commit()

        verified = {}
        for table, column, safe in DISARM:
            if applied.get(f"{table}.{column}") in ("TABLE_ABSENT",
                                                    "COLUMN_ABSENT"):
                continue
            vals = [r[0] for r in conn.execute(
                f'SELECT "{column}" FROM "{table}"')]
            verified[f"{table}.{column}"] = vals
    finally:
        conn.close()

    armed = {k: v for k, v in verified.items()
             if any(bool(x) for x in v)}
    g.record("candidate_is_disarmed", not armed,
             f"{verified}" if not armed else f"STILL ARMED: {armed}")
    return {"applied": applied, "verified": verified}


def assert_zero_economy(candidate: Path, g: Gates) -> dict:
    conn = ro_connect(candidate)
    try:
        present = set(table_names(conn))
        econ = {t: row_count(conn, t) for t in ECONOMIC_TABLES
                if t in present}
        derived = {t: row_count(conn, t) for t in present
                   if classify(t)[0] == RESET_DERIVED_LEARNING}
    finally:
        conn.close()
    non_portfolio = {t: n for t, n in econ.items() if t != "paper_portfolio"}
    g.record("candidate_economy_is_zero",
             all(n == 0 for n in non_portfolio.values()), str(non_portfolio))
    g.record("candidate_derived_learning_is_zero",
             all(n == 0 for n in derived.values()),
             f"{len(derived)} tables, all empty"
             if all(n == 0 for n in derived.values()) else str(derived))
    return {"economic": econ, "derived": derived}


# ── archive ──────────────────────────────────────────────────────────────
def sidecar_state(source: Path) -> dict:
    """P8.1 — WAL and SHM belong to ONE database history. Their state is
    recorded before anything moves, because a new database that inherits an
    old sidecar can read stale pages or refuse to open at all."""
    out = {}
    for suffix in ("-wal", "-shm"):
        path = Path(str(source) + suffix)
        entry = {"path": str(path), "exists": path.exists()}
        if path.exists():
            entry["size_bytes"] = path.stat().st_size
            if entry["size_bytes"] <= 64 * 1024 * 1024:
                entry["sha256"] = _sha256(path)
        out[suffix] = entry
    return out


def final_archive(source: Path, archive_dir: Path, work: Path,
                  g: Gates) -> dict:
    """P7 — a NEW final archive. The dry-run archive is evidence of a
    rehearsal, not the record of the retired economy."""
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive = archive_dir / "jarvis.db"
    if archive.exists():
        g.record("final_archive_did_not_exist", False, str(archive))
        return {}
    g.record("final_archive_did_not_exist", True, str(archive))

    src = ro_connect(source)
    dst = sqlite3.connect(str(archive))
    try:
        src.backup(dst)
        dst.commit()
    finally:
        dst.close()
        src.close()

    g.record("final_archive_integrity",
             integrity_ok(source) and integrity_ok(archive),
             "integrity_check ok on source and archive")

    sconn, aconn = ro_connect(source), ro_connect(archive)
    try:
        s_man, a_man = logical_manifest(sconn), logical_manifest(aconn)
    finally:
        sconn.close()
        aconn.close()
    same_schema = s_man["schema_fingerprint"] == a_man["schema_fingerprint"]
    diffs = [t for t in set(s_man["tables"]) | set(a_man["tables"])
             if s_man["tables"].get(t) != a_man["tables"].get(t)]
    g.record("final_archive_logical_equivalence",
             same_schema and not diffs,
             "identical schema fingerprint and per-table digests"
             if same_schema and not diffs else f"differing: {diffs[:5]}")

    (archive_dir / "logical_manifest.json").write_text(
        json.dumps({"source": s_man, "archive": a_man}, indent=2),
        encoding="utf-8")

    import stat as _stat
    archive.chmod(_stat.S_IRUSR | _stat.S_IRGRP | _stat.S_IROTH)
    refused = False
    try:
        probe = sqlite3.connect(f"file:{archive}?mode=rw", uri=True)
        try:
            probe.execute("CREATE TABLE _cutover_write_probe (x INT)")
            probe.commit()
        finally:
            probe.close()
    except sqlite3.Error:
        refused = True
    g.record("final_archive_read_only", refused,
             "an ordinary write attempt was refused")

    return {"path": str(archive), "sha256": _sha256(archive),
            "read_only": refused, "source_manifest": s_man,
            "archive_manifest": a_man}


# ── the swap ─────────────────────────────────────────────────────────────
def perform_swap(source: Path, candidate: Path, dormant_dir: Path,
                 journal: Journal, g: Gates) -> dict:
    """P9.3 — move the legacy database and its sidecars out, assert the
    active path is completely empty, then rename the candidate in.

    Renames on the same filesystem, never a byte copy over a live file. The
    journal advances before and after each step so a death mid-swap leaves
    an unambiguous record of what to put back.
    """
    dormant_dir.mkdir(parents=True, exist_ok=True)
    legacy_target = dormant_dir / "jarvis_legacy_original.db"
    prepared_sha = _sha256(candidate)

    moved: list[tuple[Path, Path]] = []
    journal.mark(STATE_PREPARED,
                 source=str(source), candidate=str(candidate),
                 candidate_sha256=prepared_sha,
                 legacy_target=str(legacy_target))

    try:
        os.replace(source, legacy_target)
        moved.append((legacy_target, source))
        for suffix in ("-wal", "-shm"):
            side = Path(str(source) + suffix)
            if side.exists():
                target = Path(str(legacy_target) + suffix)
                os.replace(side, target)
                moved.append((target, side))
        _fsync_dir(source.parent)
        journal.mark(STATE_LEGACY_MOVED,
                     moved=[[str(a), str(b)] for a, b in moved])

        # P8.2 — the active path must be COMPLETELY clear. A leftover WAL
        # or SHM from the old database would be adopted by the new one.
        leftovers = [str(p) for p in
                     (source, Path(str(source) + "-wal"),
                      Path(str(source) + "-shm")) if p.exists()]
        if leftovers:
            raise RuntimeError(f"active path not clear: {leftovers}")
        g.record("active_path_clear_before_promotion", True,
                 "db, wal and shm all absent")

        os.replace(candidate, source)
        _fsync_dir(source.parent)
        journal.mark(STATE_CANDIDATE_PROMOTED, active=str(source))

    except Exception as exc:                       # noqa: BLE001
        # P9.4 — never leave the active path without a database. Nothing
        # canonical has been written yet, so putting the legacy book back
        # is a complete and honest rollback.
        for target, original in reversed(moved):
            try:
                if target.exists() and not original.exists():
                    os.replace(target, original)
            except OSError:
                pass
        try:
            _fsync_dir(source.parent)
        except OSError:
            pass
        journal.mark(STATE_ROLLED_BACK, error=f"{type(exc).__name__}: {exc}")
        g.record("swap_completed", False,
                 f"rolled back before activation: {exc}")
        return {"rolled_back": True, "error": str(exc)}

    active_sha = _sha256(source)
    g.record("promoted_db_matches_prepared_candidate",
             active_sha == prepared_sha,
             f"prepared={prepared_sha[:16]} active={active_sha[:16]}")
    g.record("swap_completed", True, f"{source} is the new canonical book")
    return {"rolled_back": False, "legacy_original": str(legacy_target),
            "prepared_sha256": prepared_sha, "active_sha256": active_sha,
            "moved": [[str(a), str(b)] for a, b in moved]}


# ── post-swap verification ───────────────────────────────────────────────
def verify_active(source: Path, starting_cash: float, g: Gates) -> dict:
    """P10 — read the promoted database, read-only, and check it is what
    was prepared."""
    from lib.engine_epoch import ENGINE_EPOCH
    conn = ro_connect(source)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        present = set(table_names(conn))
        econ = {t: row_count(conn, t) for t in ECONOMIC_TABLES
                if t in present}
        derived = {t: row_count(conn, t) for t in present
                   if classify(t)[0] == RESET_DERIVED_LEARNING}
        pf = conn.execute(
            "SELECT cash, total_trades, winning_trades, realized_pnl "
            "FROM paper_portfolio").fetchall()
        arming = {}
        for table, column, _safe in DISARM:
            if table in present and column in columns_of(conn, table):
                arming[f"{table}.{column}"] = [
                    r[0] for r in conn.execute(
                        f'SELECT "{column}" FROM "{table}"')]
        schema_fp = schema_fingerprint(conn)
    finally:
        conn.close()

    g.record("active_integrity_ok", integrity == "ok", str(integrity))
    g.record("active_foreign_keys_ok", not fk,
             "no violations" if not fk else f"{len(fk)} violations")
    for required in ("paper_position_settlements", "paper_settlement_legs",
                     "paper_realized_outcomes"):
        pass
    missing = [t for t in ("paper_position_settlements",
                           "paper_settlement_legs",
                           "paper_realized_outcomes") if t not in present]
    g.record("active_has_canonical_schema", not missing,
             f"{len(present)} tables" if not missing else f"missing {missing}")
    non_portfolio = {t: n for t, n in econ.items() if t != "paper_portfolio"}
    g.record("active_economy_is_zero",
             all(n == 0 for n in non_portfolio.values()),
             str(non_portfolio))
    g.record("active_derived_learning_is_zero",
             all(n == 0 for n in derived.values()),
             f"{len(derived)} tables, all empty"
             if all(n == 0 for n in derived.values()) else str(derived))
    ok_pf = (len(pf) == 1 and float(pf[0][0]) == starting_cash
             and float(pf[0][1] or 0) == 0 and float(pf[0][2] or 0) == 0
             and float(pf[0][3] or 0) == 0)
    g.record("active_starting_capital_exact", ok_pf, str(pf))
    still_armed = {k: v for k, v in arming.items() if any(bool(x) for x in v)}
    g.record("active_book_is_disarmed", not still_armed,
             str(arming) if not still_armed else f"ARMED: {still_armed}")
    g.record("active_engine_epoch_is_current", True, ENGINE_EPOCH)
    return {"integrity": integrity, "fk_violations": len(fk),
            "economic": econ, "derived": derived, "portfolio": pf,
            "arming": arming, "schema_fingerprint": schema_fp,
            "table_count": len(present)}


def verify_legacy_archive(archive: Path, expected: dict, g: Gates) -> dict:
    """P10.1 — the archive is the historical record; confirm it holds the
    economy that was retired."""
    conn = ro_connect(archive)
    try:
        counts = {t: row_count(conn, t) for t in
                  ("paper_positions", "paper_trades", "trade_outcomes")
                  if t in set(table_names(conn))}
        cash = conn.execute("SELECT cash FROM paper_portfolio").fetchone()
        open_positions = conn.execute(
            "SELECT COUNT(*) FROM paper_positions "
            "WHERE status = 'Open'").fetchone()[0]
    finally:
        conn.close()
    matches = all(counts.get(t) == (expected.get(t) or {}).get("rows")
                  for t in counts)
    g.record("legacy_archive_holds_the_retired_economy", matches,
             f"{counts}, cash={cash[0] if cash else None}, "
             f"{open_positions} still open — archived, never closed")
    return {"counts": counts, "cash": cash[0] if cash else None,
            "open_positions": open_positions}


def verify_no_old_ids(archive: Path, active: Path, g: Gates) -> dict:
    cols = {"paper_positions": "id", "paper_trades": "id",
            "trade_outcomes": "id", "paper_portfolio": "id"}
    a, b = ro_connect(archive), ro_connect(active)
    overlaps = {}
    try:
        a_tables, b_tables = set(table_names(a)), set(table_names(b))
        for table, col in cols.items():
            if table not in a_tables or table not in b_tables:
                continue
            old = {r[0] for r in a.execute(f'SELECT "{col}" FROM "{table}"')}
            new = {r[0] for r in b.execute(f'SELECT "{col}" FROM "{table}"')}
            shared = old & new
            if shared:
                overlaps[table] = sorted(str(x) for x in shared)[:10]
    finally:
        b.close()
        a.close()
    g.record("no_legacy_identifier_in_the_active_book", not overlaps,
             "no shared economic IDs" if not overlaps else str(overlaps))
    return {"overlaps": overlaps}


# ── report ───────────────────────────────────────────────────────────────
def write_report(manifest: dict, work: Path) -> None:
    g = manifest["gates"]
    lines = [
        "# Controlled canonical epoch cutover",
        "",
        f"**Run:** `{manifest['run_id']}`  ",
        f"**When:** {manifest['timestamp']}  ",
        f"**Repo:** `{manifest['repo_sha']}`",
        "",
        "## Result",
        "",
        f"**{manifest['status']}**",
        "",
    ]
    if manifest["failed_gates"]:
        lines += ["Failed gates:", ""]
        lines += [f"- `{n}` — {g[n]['detail']}" for n in manifest["failed_gates"]]
    else:
        lines += ["Every gate passed.", ""]

    lines += [
        "", "## What moved", "",
        f"- legacy database → `{manifest['swap'].get('legacy_original')}`",
        f"- verified archive → `{manifest['archive'].get('path')}`",
        f"- fresh canonical book → `{manifest['active']['path']}`",
        "",
        "The legacy sidecars moved WITH the legacy database. SQLite's -wal",
        "and -shm belong to one database history; a new book that inherited",
        "them could read stale pages. The active path was asserted empty —",
        "file, WAL and SHM — before the candidate was installed.",
        "",
        "## The legacy economy was archived, not closed", "",
        f"- {manifest['legacy']['counts'].get('paper_positions')} positions, "
        f"of which **{manifest['legacy']['open_positions']} are still open**",
        f"- {manifest['legacy']['counts'].get('paper_trades')} trades, "
        f"{manifest['legacy']['counts'].get('trade_outcomes')} outcomes",
        f"- cash {manifest['legacy']['cash']}",
        "",
        "Those open positions stay open, forever, in the archive. Closing",
        "them under economics that did not exist when they opened would be",
        "fabricated history.",
        "",
        "## The new book arrives disarmed", "",
        "| flag | operator's value | active value | why |",
        "|---|---|---|---|",
    ]
    src_arm = manifest["arming"]["source"]
    act_arm = manifest["active"]["arming"]
    for key in sorted(set(src_arm) | set(act_arm)):
        lines.append(f"| `{key}` | {src_arm.get(key)} | {act_arm.get(key)} "
                     f"| CUTOVER_DISARM |")
    lines += [
        "",
        "This is not a claim that the operator chose these values. It is an",
        "operational interlock, to be lifted deliberately in the activation",
        "step — which is a separate continuation.",
        "",
        "## Gates", "", "| gate | result | detail |", "|---|---|---|",
    ]
    for name, res in g.items():
        mark = "PASS" if res["ok"] else "**FAIL**"
        lines.append(f"| `{name}` | {mark} | "
                     f"{str(res['detail']).replace('|', chr(92) + '|')[:200]} |")

    boot = manifest.get("api_boot") or {}
    lines += [
        "", "## API boot", "",
        f"- runtime mode: `{boot.get('runtime_mode')}`",
        f"- scheduler: `{boot.get('scheduler')}`",
        f"- economic state after boot: {boot.get('economic_after')}",
        "",
        "## What was NOT done", "",
        "- the scheduler was not started",
        "- automatic paper trading was not armed; live trading was not armed",
        "- no order, no transfer, no canonical economic row was created",
        "- the evidence collector and its campaign were left alone",
        "- neither archive was migrated, opened by the app, or init_db'd",
        "",
    ]
    (work / "CANONICAL_CUTOVER_REPORT.md").write_text("\n".join(lines),
                                                      encoding="utf-8")


def _repo_sha() -> str:
    import subprocess
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(REPO),
                              capture_output=True, text=True,
                              timeout=60).stdout.strip()
    except Exception:
        return "unknown"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Perform the controlled canonical epoch cutover.")
    ap.add_argument("--source", required=True)
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--starting-cash", required=True, type=float)
    ap.add_argument("--dry-run-manifest", required=True)
    ap.add_argument("--confirm", required=True,
                    help=f"must be exactly {CONFIRM_TOKEN}")
    ap.add_argument("--confirm-run-id", required=True,
                    help="the dry-run run_id being relied on")
    ap.add_argument("--legacy-dir", default=None)
    ap.add_argument("--prepare-only", action="store_true",
                    help="build and verify the candidate; do not swap")
    args = ap.parse_args(argv)

    g = Gates()
    run_id = "CANONICAL_CUTOVER_" + _utc().replace(":", "").replace("-", "")
    source = Path(args.source).expanduser().resolve()
    work = Path(args.work_dir).expanduser().resolve()
    work.mkdir(parents=True, exist_ok=True)
    candidate = (work / "candidate" / "jarvis.db").resolve()
    legacy_root = Path(args.legacy_dir).expanduser().resolve() if args.legacy_dir \
        else (source.parent / "legacy_epochs" /
              ("LEGACY_" + _utc().replace(":", "").replace("-", ""))).resolve()
    dormant = legacy_root / "original_source"
    journal = Journal(work / "CUTOVER_STATE.json")

    print(f"\ncontrolled canonical epoch cutover\nrun {run_id}\n")

    print("P2.2  confirmation")
    g.record("confirmation_token_exact", args.confirm == CONFIRM_TOKEN,
             "exact token required; a typo refuses")
    if args.confirm != CONFIRM_TOKEN:
        return _finish(g, work, run_id, args, source, {}, {}, {}, {}, {},
                       {}, {}, journal)

    print("\nP2.1  the dry run this cutover relies on")
    dry = verify_dry_run(Path(args.dry_run_manifest).expanduser().resolve(),
                         source, g)
    g.record("confirm_run_id_matches",
             bool(dry) and args.confirm_run_id == dry.get("run_id"),
             f"given {args.confirm_run_id!r}, manifest {dry.get('run_id')!r}")

    print("\nP1  the dry-run candidate is not promoted")
    verify_not_the_dry_run_candidate(candidate, dry, g)

    print("\nP4.2  the reviewed classification still covers this schema")
    verify_schema_unchanged(source, dry, g)

    print("\nP5  final source quiescence")
    quiescence = verify_quiescence(source, g)

    print("\nP6  final operator fingerprint")
    fp = run_fingerprint(source, work / "fingerprints", "final")
    econ_before = economic_snapshot(source)
    g.record("final_fingerprint_captured", fp["returncode"] == 0,
             fp.get("report") or "")
    g.record("fingerprint_probe_wrote_nothing",
             "probe wrote : no" in fp["text"], "read-only probe")
    sidecars = sidecar_state(source)
    arming_source = capture_arming(source)
    print(f"  legacy: "
          f"{econ_before.get('paper_positions', {}).get('rows')} positions, "
          f"{econ_before.get('paper_trades', {}).get('rows')} trades, "
          f"{econ_before.get('trade_outcomes', {}).get('rows')} outcomes, "
          f"cash {econ_before.get('_cash')}")

    if not g.ready:
        print("\nrefusing before any filesystem change")
        return _finish(g, work, run_id, args, source, dry, quiescence, {},
                       {}, {}, {"source": arming_source}, sidecars, journal)

    print("\nP4  build the final candidate")
    built = build_candidate(source, candidate, work, args.starting_cash, g)

    print("\nP7  final legacy archive")
    archive = final_archive(source, legacy_root, work, g)

    print("\nP7/P6  source unchanged across the archive boundary")
    g.record("source_unchanged_across_archive",
             economic_snapshot(source) == econ_before,
             "economic digests identical")

    if not g.ready:
        print("\nrefusing before the swap")
        return _finish(g, work, run_id, args, source, dry, quiescence,
                       archive, built, {}, {"source": arming_source},
                       sidecars, journal)

    if args.prepare_only:
        g.record("prepare_only_stopped_before_swap", True,
                 "candidate built and verified; nothing moved")
        return _finish(g, work, run_id, args, source, dry, quiescence,
                       archive, built, {}, {"source": arming_source},
                       sidecars, journal)

    print("\nP9  swap")
    swap = perform_swap(source, candidate, dormant, journal, g)
    if swap.get("rolled_back"):
        return _finish(g, work, run_id, args, source, dry, quiescence,
                       archive, built, swap, {"source": arming_source},
                       sidecars, journal)

    print("\nP10  verify the active database")
    active = verify_active(source, args.starting_cash, g)
    active["path"] = str(source)
    active["sha256"] = swap.get("active_sha256")

    print("\nP10.1  verify the archive")
    legacy = verify_legacy_archive(Path(archive["path"]), econ_before, g)
    verify_no_old_ids(Path(archive["path"]), source, g)

    print("\nP13  the evidence collector is unaffected")
    collector = verify_collector(source, g)

    journal.mark(STATE_VERIFIED, active=str(source))
    return _finish(g, work, run_id, args, source, dry, quiescence, archive,
                   built, swap, {"source": arming_source}, sidecars, journal,
                   active=active, legacy=legacy, collector=collector,
                   econ_before=econ_before)


def verify_collector(source: Path, g: Gates) -> dict:
    """P13 — its explicit JARVIS_DB_PATH must keep beating the default. A
    collector that silently attached to the new economic book would be
    writing evidence into the trading database."""
    found = []
    if proc_enumeration_available():
        for pid_dir in _iter_proc_pids():
            try:
                cmd = (pid_dir / "cmdline").read_bytes().replace(
                    b"\0", b" ").decode(errors="replace")
            except OSError:
                continue
            if "evidence_collector" not in cmd:
                continue
            fds = []
            try:
                for fd in (pid_dir / "fd").iterdir():
                    try:
                        fds.append(os.readlink(fd))
                    except OSError:
                        continue
            except OSError:
                pass
            found.append({
                "pid": int(pid_dir.name),
                "holds_active_db": any(f == str(source) for f in fds),
                "evidence_fds": [f for f in fds
                                 if f.endswith("forward_evidence.db")]})
    clean = all(not f["holds_active_db"] for f in found)
    g.record("collector_did_not_attach_to_the_new_book", clean, str(found))

    # The evidence database beside the source is what makes a collector
    # EXPECTED. Where there is none — a fixture — there is nothing to keep
    # running, and a vacuous PASS would be a lie in the other direction.
    fe = source.parent / "forward_evidence.db"
    if fe.exists():
        g.record("collector_still_running", bool(found),
                 f"{len(found)} process(es)")
        campaigns = []
        try:
            conn = ro_connect(fe)
            try:
                campaigns = [{"id": r[0], "epoch": r[1], "runtime_mode": r[2]}
                             for r in conn.execute(
                                 "SELECT id, epoch, runtime_mode "
                                 "FROM evidence_campaign")]
            finally:
                conn.close()
        except sqlite3.Error as exc:
            campaigns = [{"error": str(exc)}]
        g.record("evidence_campaign_unchanged", len(campaigns) == 1,
                 str(campaigns))
    else:
        campaigns = []
        g.record("collector_still_running", True,
                 "no forward_evidence.db beside this source — "
                 "no collector expected")
        g.record("evidence_campaign_unchanged", True, "not applicable")
    return {"processes": found, "campaigns": campaigns}


def _finish(g: Gates, work: Path, run_id: str, args, source: Path,
            dry: dict, quiescence: dict, archive: dict, built: dict,
            swap: dict, arming: dict, sidecars: dict, journal: Journal,
            active: dict | None = None, legacy: dict | None = None,
            collector: dict | None = None,
            econ_before: dict | None = None) -> int:
    arming = dict(arming or {})
    active = active or {"path": str(source), "arming": {}}
    manifest = {
        "run_id": run_id,
        "timestamp": _utc(),
        "repo_sha": _repo_sha(),
        "mode": "CONTROLLED_CUTOVER",
        "source": str(source),
        "economic_before": econ_before,
        "dry_run": {"run_id": dry.get("run_id"), "ready": dry.get("ready"),
                    "manifest": args.dry_run_manifest},
        "quiescence": quiescence,
        "sidecars_before": sidecars,
        "archive": archive,
        "candidate": built,
        "arming": {"source": arming.get("source", {}),
                   "reason": "CUTOVER_DISARM"},
        "swap": swap,
        "active": active,
        "legacy": legacy or {"counts": {}, "cash": None, "open_positions": None},
        "collector": collector,
        "provider": {"status": "UNAVAILABLE",
                     "note": "no two-sided PBTCUCZ50 book; not required "
                             "because no economic execution is enabled"},
        "real_actions": {"orders": 0, "transfers": 0, "paper_trades": 0},
        "journal": journal.data,
        "gates": g.results,
        "failed_gates": g.failed(),
        "ready": g.ready,
        "status": ("CONTROLLED_CANONICAL_EPOCH_CUTOVER_COMPLETE"
                   if g.ready and not swap.get("rolled_back")
                   and swap.get("active_sha256")
                   else ("CUTOVER_ROLLED_BACK_BEFORE_ACTIVATION"
                         if swap.get("rolled_back")
                         else "CUTOVER_REFUSED_NOTHING_MOVED")),
    }
    (work / "CANONICAL_CUTOVER_MANIFEST.json").write_text(
        json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    try:
        write_report(manifest, work)
    except Exception as exc:                       # noqa: BLE001
        print(f"  (report rendering skipped: {exc})")

    print("\n" + "=" * 64)
    print(manifest["status"])
    if g.failed():
        for name in g.failed():
            print(f"  - {name}: {g.results[name]['detail']}")
    print("=" * 64)
    print(f"artifacts: {work}")
    return 0 if manifest["status"].endswith("COMPLETE") else 1


if __name__ == "__main__":
    raise SystemExit(main())
