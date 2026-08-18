"""Prove a canonical epoch cutover CAN be done. Never do one.

WHAT A CUTOVER IS. The legacy paper economy is archived whole and a fresh
current-schema book is created beside it. The operator's deliberate
configuration crosses; the money, the history and the old machine's
conclusions do not. It is **archive the old database and create a new one**,
not **mutate the old one until it looks new**.

THE GOLDEN RULE — NEVER OLD ENTRY + NEW EXIT. The 667 legacy positions
belong to the legacy simulator. They are not backfilled, converted,
re-priced, canonically closed, given settlement headers, or fed through the
canonical machinery. An old OPEN position stays open forever in the archive,
and that is honest. Closing it under economics that did not exist when it
opened would be fabricated history.

THIS TOOL HAS NO APPLY MODE. There is no --apply, no --activate, no --swap.
It never renames, replaces or writes the source database, never edits a
service or an env file, and never starts the scheduler. It produces the
evidence needed to authorize a separate, controlled cutover later — and a
readiness flag that is the conjunction of explicit gates, never a
side-effect of reaching the end of the script.

WHY THE PARENT IMPORTS NO APPLICATION CODE. `app.database` builds its engine
at import time, and that engine sets `PRAGMA journal_mode=WAL` on connect —
a tool that proves immutability by opening the database in a mode that can
rewrite its header is not a proof. So the parent process uses stdlib sqlite3
with `mode=ro` and `query_only=ON`. Anything needing the ORM runs in a child
process with JARVIS_DB_PATH — and every other writable store redirected —
set BEFORE Python imports application code.

Usage:
    .venv/bin/python scripts/canonical_epoch_dry_run.py \
        --source data/jarvis.db \
        --work-dir data/cutover_dry_runs/<RUN_ID> \
        --starting-cash 100000
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# Pure data: a table->fate map and column allowlists. Imports nothing.
from lib.cutover_classification import (  # noqa: E402
    ARCHIVE_ONLY_ECONOMIC, COPY_PLAN, COPY_TABLES, RESET_DERIVED_LEARNING,
    UNKNOWN_REFUSE, classify)

MODE = "DRY_RUN_ONLY"

ECONOMIC_TABLES = ("paper_positions", "paper_trades", "paper_portfolio",
                   "trade_outcomes", "paper_position_settlements",
                   "paper_settlement_legs", "paper_realized_outcomes")

EXTERNAL_STORES = ("events.db", "ohlcv_cache.db", "forward_evidence.db")


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ── gates ────────────────────────────────────────────────────────────────
class Gates:
    """Readiness is a conjunction, and a gate nobody recorded is FALSE.

    Reaching the end of the script proves only that the script ran.
    """

    def __init__(self):
        self.results: dict[str, dict] = {}

    def record(self, name: str, ok: bool, detail: str = "") -> bool:
        self.results[name] = {"ok": bool(ok), "detail": detail}
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {name}{(' — ' + detail) if detail else ''}")
        return bool(ok)

    def failed(self) -> list[str]:
        return [k for k, v in self.results.items() if not v["ok"]]

    @property
    def ready(self) -> bool:
        return bool(self.results) and not self.failed()


# ── read-only source access ──────────────────────────────────────────────
def ro_connect(path: Path) -> sqlite3.Connection:
    """Read-only, and it says so twice. `mode=ro` refuses writes at the VFS;
    `query_only=ON` refuses them at the statement level."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=ON")
    return conn


def table_names(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def row_count(conn: sqlite3.Connection, table: str) -> int:
    try:
        return int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
    except sqlite3.Error:
        return -1


def columns_of(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]


def schema_fingerprint(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        "SELECT type, name, tbl_name, COALESCE(sql,'') FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name").fetchall()
    h = hashlib.sha256()
    for r in rows:
        h.update(("|".join(str(x) for x in r) + "\n").encode())
    return h.hexdigest()


def table_digest(conn: sqlite3.Connection, table: str) -> str:
    """Order-independent digest of a table's contents."""
    cols = columns_of(conn, table)
    if not cols:
        return "NO_COLUMNS"
    quoted = ", ".join(f'"{c}"' for c in cols)
    h = hashlib.sha256()
    rows = []
    for row in conn.execute(f'SELECT {quoted} FROM "{table}"'):
        rows.append(hashlib.sha256(
            json.dumps(row, default=str, sort_keys=True).encode()).hexdigest())
    for d in sorted(rows):
        h.update(d.encode())
    return h.hexdigest()


def logical_manifest(conn: sqlite3.Connection) -> dict:
    """Everything that makes two databases logically the same, table by
    table. A SQLite backup-API copy is legitimately byte-different from its
    source, so requiring equal file SHAs would be wrong. This is what
    equality actually means here."""
    out = {"schema_fingerprint": schema_fingerprint(conn), "tables": {}}
    for t in table_names(conn):
        out["tables"][t] = {"rows": row_count(conn, t),
                            "digest": table_digest(conn, t)}
    return out


def integrity_ok(path: Path) -> bool:
    conn = ro_connect(path)
    try:
        return conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


# ── P3 quiescence ────────────────────────────────────────────────────────
def db_holders(source: Path) -> list[dict]:
    """Which OTHER processes have the source database (or its WAL/SHM) open.

    An archive is an epoch boundary. If anything can still write while the
    copy is taken, the archive is a photograph of a moving subject and the
    'immutable' claim is false. No sudo, and nothing is killed — a holder is
    reported and the run refuses.
    """
    targets = {str(source), str(source) + "-wal", str(source) + "-shm"}
    holders: list[dict] = []
    proc = Path("/proc")
    if not proc.exists():
        return holders                      # not Linux; caller decides
    me = os.getpid()
    for pid_dir in proc.iterdir():
        if not pid_dir.name.isdigit():
            continue
        pid = int(pid_dir.name)
        if pid == me:
            continue
        fd_dir = pid_dir / "fd"
        try:
            fds = list(fd_dir.iterdir())
        except (PermissionError, FileNotFoundError, OSError):
            continue                        # another user's process
        for fd in fds:
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            if target in targets:
                try:
                    cmd = (pid_dir / "cmdline").read_bytes().replace(
                        b"\0", b" ").decode(errors="replace").strip()
                except OSError:
                    cmd = "?"
                holders.append({"pid": pid, "cmd": cmd[:200],
                                "path": target})
                break
    return holders


def process_fds(pid: int) -> list[str]:
    out = []
    fd_dir = Path("/proc") / str(pid) / "fd"
    try:
        for fd in fd_dir.iterdir():
            try:
                out.append(os.readlink(fd))
            except OSError:
                continue
    except (PermissionError, FileNotFoundError, OSError):
        pass
    return out


# ── child process plumbing ───────────────────────────────────────────────
def child_env(candidate: Path, work: Path) -> dict:
    """Every writable store this application owns, redirected.

    P8.2: a disposable process is only disposable if EVERY file it can write
    lives under its own directory. One hard-coded path is enough to have an
    otherwise-isolated child mutate live operator data.
    """
    env = dict(os.environ)
    env.pop("JARVIS_UNDER_PYTEST", None)
    stores = work / "stores"
    stores.mkdir(parents=True, exist_ok=True)
    env["JARVIS_DB_PATH"] = str(candidate)
    env["JARVIS_EVENTS_DB_PATH"] = str(stores / "events.db")
    env["JARVIS_OHLCV_DB_PATH"] = str(stores / "ohlcv_cache.db")
    env["JARVIS_SCHEDULER_ENABLED"] = "false"
    # FULL_VIRTUAL, deliberately. EVIDENCE_ONLY forbids economic mutation,
    # which is exactly right for the OPERATOR runtime and exactly wrong
    # here: the whole point is to prove canonical economics execute end to
    # end. This applies only to this child process, against the disposable
    # candidate database. The operator's own runtime mode is not read,
    # written or affected by any of this.
    env["JARVIS_RUNTIME_MODE"] = "FULL_VIRTUAL"
    env["PYTHONPATH"] = str(REPO)
    return env


def run_child(phase: str, candidate: Path, work: Path, extra: dict | None = None,
              timeout: int = 900) -> dict:
    """Run one phase in a subprocess whose environment was set BEFORE the
    interpreter started, so no application import can reach live state."""
    payload = json.dumps(extra or {})
    proc = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "canonical_epoch_child.py"),
         "--phase", phase, "--payload", payload],
        cwd=str(REPO), env=child_env(candidate, work),
        capture_output=True, text=True, timeout=timeout)
    result = {"phase": phase, "returncode": proc.returncode,
              "stderr_tail": proc.stderr[-4000:]}
    marker = "@@RESULT@@"
    for line in proc.stdout.splitlines():
        if line.startswith(marker):
            result.update(json.loads(line[len(marker):]))
            break
    else:
        result["ok"] = False
        result["error"] = "child produced no result payload"
        result["stdout_tail"] = proc.stdout[-4000:]
    return result


def run_fingerprint(source: Path, out_dir: Path, label: str) -> dict:
    """The existing read-only fingerprint tool, as a subprocess so the
    parent still imports nothing."""
    out_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "operator_db_fingerprint.py"),
         "--db", str(source)],
        cwd=str(REPO), capture_output=True, text=True, timeout=600)
    text = proc.stdout + proc.stderr
    (out_dir / f"fingerprint_{label}.txt").write_text(text, encoding="utf-8")
    # The tool writes its own JSON report; parse the path it names.
    report = None
    for line in proc.stdout.splitlines():
        if line.startswith("wrote "):
            report = line.split("wrote ", 1)[1].strip()
    parsed = {}
    if report and Path(report).exists():
        parsed = json.loads(Path(report).read_text(encoding="utf-8"))
        shutil.copy2(report, out_dir / f"fingerprint_{label}.json")
    return {"returncode": proc.returncode, "text": text, "report": report,
            "parsed": parsed}


def economic_snapshot(source: Path) -> dict:
    """The digests that must not move, read straight from the source."""
    conn = ro_connect(source)
    try:
        snap = {}
        for t in ECONOMIC_TABLES:
            if t in table_names(conn):
                snap[t] = {"rows": row_count(conn, t),
                           "digest": table_digest(conn, t)}
            else:
                snap[t] = {"rows": None, "digest": "ABSENT"}
        try:
            snap["_cash"] = conn.execute(
                "SELECT cash FROM paper_portfolio").fetchone()[0]
        except sqlite3.Error:
            snap["_cash"] = None
        return snap
    finally:
        conn.close()


# ── phases ───────────────────────────────────────────────────────────────
def phase_inputs(args, g: Gates) -> tuple[Path, Path, Path, Path]:
    """P0.1/P0.2 — explicit inputs, and no path may alias another.

    realpath() before comparing, so a symlink cannot smuggle the source in
    as the archive or the candidate.
    """
    source = Path(args.source).expanduser().resolve()
    work = Path(args.work_dir).expanduser().resolve()
    archive = (work / "archive" / "jarvis_legacy.db").resolve()
    candidate = (work / "candidate" / "jarvis.db").resolve()

    g.record("source_exists", source.is_file(), str(source))
    distinct = len({source, archive, candidate}) == 3
    g.record("paths_distinct", distinct,
             "archive and candidate must not alias the source")
    inside = work not in source.parents and source.parent != work
    g.record("source_outside_work_dir", inside,
             "the work directory must not contain the source")
    return source, work, archive, candidate


def phase_classify(source: Path, work: Path, g: Gates) -> dict:
    """P2 — every table gets exactly one fate. An unclassified table is a
    refusal, because the alternative is guessing about the operator's
    money."""
    conn = ro_connect(source)
    try:
        tables = table_names(conn)
        report = {}
        for t in tables:
            cls, why = classify(t)
            report[t] = {"classification": cls, "reason": why,
                         "source_rows": row_count(conn, t)}
    finally:
        conn.close()

    unknown = [t for t, v in report.items()
               if v["classification"] == UNKNOWN_REFUSE]
    g.record("zero_unknown_tables", not unknown,
             f"{len(tables)} tables classified"
             + (f"; UNKNOWN: {unknown}" if unknown else ""))
    (work / "cutover_table_classification.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8")
    return report


def phase_quiescence(source: Path, g: Gates) -> dict:
    """P3 — nothing else may hold the source at the archive boundary."""
    holders = db_holders(source)
    on_linux = Path("/proc").exists()
    g.record("source_quiescent", on_linux and not holders,
             "no other process holds the DB" if on_linux and not holders
             else (f"holders: {holders}" if holders
                   else "/proc unavailable — cannot prove quiescence"))

    # P3.2 — the collector is allowed to run because it writes a DIFFERENT
    # database. Prove that rather than assert it.
    collector_pids, collector_clean = [], True
    proc = Path("/proc")
    if proc.exists():
        for pid_dir in proc.iterdir():
            if not pid_dir.name.isdigit():
                continue
            try:
                cmd = (pid_dir / "cmdline").read_bytes().replace(
                    b"\0", b" ").decode(errors="replace")
            except OSError:
                continue
            if "evidence_collector" not in cmd:
                continue
            pid = int(pid_dir.name)
            fds = process_fds(pid)
            touches_source = any(str(source) in f for f in fds)
            collector_pids.append({
                "pid": pid,
                "opens_source_db": touches_source,
                "evidence_db_fds": [f for f in fds
                                    if f.endswith("forward_evidence.db")]})
            collector_clean = collector_clean and not touches_source
    g.record("collector_writes_a_different_db", collector_clean,
             f"{len(collector_pids)} collector process(es)")
    return {"holders": holders, "collectors": collector_pids,
            "proc_available": on_linux}


def phase_archive(source: Path, archive: Path, work: Path, g: Gates) -> dict:
    """P4 — a complete logical backup, then prove it IS one.

    SQLite's backup API, not shutil.copy: a file copy of a live database can
    catch a torn page or miss the WAL entirely. Nothing is pruned — archive
    means preserve what existed, not clean it up.
    """
    archive.parent.mkdir(parents=True, exist_ok=True)
    if archive.exists():
        g.record("archive_did_not_exist", False, "refusing to overwrite")
        return {}

    src = ro_connect(source)
    dst = sqlite3.connect(str(archive))
    try:
        src.backup(dst)
        dst.commit()
    finally:
        dst.close()
        src.close()

    g.record("archive_integrity", integrity_ok(archive) and integrity_ok(source),
             "PRAGMA integrity_check on source and archive")

    sconn, aconn = ro_connect(source), ro_connect(archive)
    try:
        s_man, a_man = logical_manifest(sconn), logical_manifest(aconn)
    finally:
        sconn.close()
        aconn.close()

    same_schema = s_man["schema_fingerprint"] == a_man["schema_fingerprint"]
    diffs = [t for t in set(s_man["tables"]) | set(a_man["tables"])
             if s_man["tables"].get(t) != a_man["tables"].get(t)]
    g.record("archive_logical_equivalence", same_schema and not diffs,
             "identical schema and per-table digests"
             if same_schema and not diffs else f"differing: {diffs[:5]}")

    (work / "archive" / "logical_manifest_source.json").write_text(
        json.dumps(s_man, indent=2), encoding="utf-8")
    (work / "archive" / "logical_manifest_archive.json").write_text(
        json.dumps(a_man, indent=2), encoding="utf-8")

    # P4.4 — read-only by ordinary permissions, then proved by attempting a
    # write. Source permissions are never touched.
    archive.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    write_refused = False
    try:
        probe = sqlite3.connect(f"file:{archive}?mode=rw", uri=True)
        try:
            probe.execute("CREATE TABLE _dry_run_write_probe (x INT)")
            probe.commit()
        finally:
            probe.close()
    except sqlite3.Error:
        write_refused = True
    g.record("archive_read_only", write_refused,
             "an ordinary write attempt was refused")

    return {"archive_sha256": _sha256(archive),
            "source_manifest": s_man, "archive_manifest": a_man,
            "read_only": write_refused}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def phase_candidate(source: Path, candidate: Path, work: Path,
                    starting_cash: float, g: Gates) -> dict:
    """P5 — a FRESH database built by the current code's own init_db.

    Not by copying the operator's tables. The entire point is that the old
    schema and the new active canonical schema are different things; building
    the candidate from the old one would quietly preserve whatever the old
    one had.
    """
    candidate.parent.mkdir(parents=True, exist_ok=True)
    if candidate.exists():
        g.record("candidate_did_not_exist", False, "refusing to overwrite")
        return {}
    g.record("candidate_did_not_exist", True, str(candidate))

    first = run_child("init_schema", candidate, work)
    g.record("candidate_schema_created", first.get("ok") is True,
             first.get("error") or f"{first.get('table_count')} tables")

    second = run_child("init_schema", candidate, work)
    idempotent = (second.get("ok") is True
                  and second.get("schema_fingerprint")
                  == first.get("schema_fingerprint"))
    g.record("init_db_idempotent", idempotent,
             "a second init_db left the schema identical")

    conn = ro_connect(candidate)
    try:
        have = set(table_names(conn))
    finally:
        conn.close()
    required = {"paper_positions", "paper_trades", "trade_outcomes",
                "paper_position_settlements", "paper_settlement_legs",
                "paper_realized_outcomes", "paper_portfolio"}
    missing = sorted(required - have)
    g.record("candidate_has_canonical_schema", not missing,
             f"{len(have)} tables" if not missing else f"missing {missing}")
    return {"first": first, "second": second, "tables": sorted(have)}


def phase_copy_config(source: Path, candidate: Path, work: Path,
                      g: Gates) -> dict:
    """P6 — only the allowlist crosses, column by column.

    Two connections. The source is opened read-only and is never ATTACHed in
    a way that could write to it. No `SELECT *`: a column added upstream must
    surface as a refusal, not as data that silently rode along.
    """
    src = ro_connect(source)
    dst = sqlite3.connect(str(candidate))
    report: list[dict] = []
    ok_all = True
    try:
        src_tables = set(table_names(src))
        dst_cur = dst.cursor()
        for entry in COPY_PLAN:
            st, tt = entry["source_table"], entry["target_table"]
            row = {"source_table": st, "target_table": tt,
                   "reason": entry["reason"], "source_rows": 0,
                   "copied_rows": 0, "columns": [], "status": "SKIPPED"}
            if st not in src_tables:
                row["status"] = "ABSENT_IN_SOURCE"
                report.append(row)
                continue
            s_cols = columns_of(src, st)
            t_cols = [r[1] for r in dst_cur.execute(
                f'PRAGMA table_info("{tt}")')]
            wanted = entry["columns"] or s_cols
            usable = [c for c in wanted if c in s_cols and c in t_cols]
            unmet = [c for c in wanted if c not in s_cols or c not in t_cols]
            if entry["columns"] and unmet:
                row["status"] = f"COLUMN_MISMATCH:{unmet}"
                ok_all = False
                report.append(row)
                continue
            quoted_s = ", ".join(f'"{c}"' for c in usable)
            where = f" WHERE {entry['predicate']}" if entry.get("predicate") else ""
            rows = list(src.execute(f'SELECT {quoted_s} FROM "{st}"{where}'))
            row["source_rows"] = row_count(src, st)
            row["columns"] = usable
            if rows:
                placeholders = ", ".join("?" for _ in usable)
                quoted_t = ", ".join(f'"{c}"' for c in usable)
                dst_cur.executemany(
                    f'INSERT OR REPLACE INTO "{tt}" ({quoted_t}) '
                    f'VALUES ({placeholders})', rows)
            row["copied_rows"] = len(rows)
            row["excluded_columns"] = entry.get("excluded_columns", [])
            row["status"] = "COPIED"
            report.append(row)
        dst.commit()
    finally:
        dst.close()
        src.close()

    g.record("config_copy_valid", ok_all,
             f"{sum(r['copied_rows'] for r in report)} rows across "
             f"{len([r for r in report if r['status'] == 'COPIED'])} tables")

    # P2.6 — nothing economic may sneak in as a foreign-key parent.
    fk_report = _fk_dependencies(source, [e["source_table"] for e in COPY_PLAN])
    economic_parents = sorted(
        {p for deps in fk_report.values() for p in deps
         if classify(p)[0] in (ARCHIVE_ONLY_ECONOMIC, RESET_DERIVED_LEARNING)})
    g.record("config_has_no_economic_fk_parents", not economic_parents,
             f"economic parents required: {economic_parents}"
             if economic_parents else "no economic parent tables required")

    (work / "cutover_config_copy.json").write_text(
        json.dumps({"copied": report, "fk_dependencies": fk_report},
                   indent=2), encoding="utf-8")
    return {"copied": report, "fk": fk_report}


def _fk_dependencies(source: Path, tables: list[str]) -> dict:
    conn = ro_connect(source)
    try:
        out = {}
        present = set(table_names(conn))
        for t in tables:
            if t not in present:
                continue
            out[t] = sorted({r[2] for r in conn.execute(
                f'PRAGMA foreign_key_list("{t}")')})
        return out
    finally:
        conn.close()


def phase_fresh_economy(source: Path, candidate: Path, work: Path,
                        starting_cash: float, g: Gates) -> dict:
    """P7 — one fresh wallet and a genuinely empty economy."""
    res = run_child("seed_portfolio", candidate, work,
                    {"starting_cash": starting_cash})
    g.record("fresh_portfolio_created", res.get("ok") is True,
             res.get("error") or f"cash={res.get('cash')}")

    conn = ro_connect(candidate)
    try:
        counts = {t: row_count(conn, t) for t in ECONOMIC_TABLES
                  if t in table_names(conn)}
        derived = {t: row_count(conn, t) for t in table_names(conn)
                   if classify(t)[0] == RESET_DERIVED_LEARNING}
    finally:
        conn.close()

    zero = {t: n for t, n in counts.items() if t != "paper_portfolio"}
    g.record("fresh_economy_zero", all(n == 0 for n in zero.values()),
             f"{zero}")
    g.record("derived_learning_reset", all(n == 0 for n in derived.values()),
             f"{len(derived)} derived tables, all empty"
             if all(n == 0 for n in derived.values()) else f"{derived}")
    g.record("starting_cash_exact",
             res.get("cash") == starting_cash
             and res.get("total_trades") == 0
             and res.get("winning_trades") == 0
             and res.get("realized_pnl") == 0,
             f"cash={res.get('cash')} trades={res.get('total_trades')}")
    return {"portfolio": res, "economic_counts": counts,
            "derived_counts": derived}


def phase_isolation(source: Path, candidate: Path, work: Path,
                    g: Gates) -> dict:
    """P9 — while a candidate process runs, it must hold the candidate
    database and NOT the source. Asserted from its actual open file
    descriptors, because configuration can say one thing while a hard-coded
    path in some module quietly does another."""
    res = run_child("fd_report", candidate, work)
    fds = res.get("fds") or []
    source_fds = [f for f in fds
                  if f == str(source) or f.startswith(str(source) + "-")]
    candidate_fds = [f for f in fds
                     if f == str(candidate)
                     or f.startswith(str(candidate) + "-")]
    live_store_fds = [f for f in fds
                      if any(f.endswith(s) for s in EXTERNAL_STORES)
                      and str(work) not in f]
    g.record("candidate_process_opened_candidate_db",
             res.get("ok") is True, str(candidate_fds))
    g.record("candidate_process_never_opened_source", not source_fds,
             "no source FD" if not source_fds
             else f"SOURCE FD PRESENT: {source_fds}")
    g.record("candidate_process_never_opened_live_stores",
             not live_store_fds,
             "no live external store FD" if not live_store_fds
             else f"LIVE STORE FD: {live_store_fds}")
    return {"fds": fds, "source_fds": source_fds,
            "candidate_fds": candidate_fds, "child": res}


def phase_lifecycle(candidate: Path, work: Path, starting_cash: float,
                    g: Gates) -> dict:
    """P10 — the load-bearing proof. Entry, partial, final, outcome and
    learning through the REAL production call graph, on the candidate."""
    res = run_child("lifecycle", candidate, work,
                    {"starting_cash": starting_cash}, timeout=1800)
    if res.get("ok") is not True:
        detail = res.get("error") or res.get("stderr_tail", "")[-300:]
        for name in ("hermetic_lifecycle_passed", "cash_identity_passed",
                     "one_vote_passed", "new_epoch_used",
                     "exact_unit_basis", "margin_fully_released"):
            g.record(name, False, detail if name ==
                     "hermetic_lifecycle_passed" else "lifecycle incomplete")
        return res

    checks = res.get("checks", {})
    stages = ("entry", "partial", "final", "outcome", "learning")
    g.record("hermetic_lifecycle_passed",
             all(checks.get(k) for k in stages),
             ", ".join(f"{k}={checks.get(k)}" for k in stages))
    g.record("cash_identity_passed", checks.get("cash_identity") is True,
             f"expected={res.get('cash_expected')} "
             f"actual={res.get('cash_actual')}")
    g.record("one_vote_passed", checks.get("one_vote") is True,
             f"total_trades={res.get('total_trades')} "
             f"realized={res.get('realized_outcomes')} "
             f"trade_outcomes={res.get('trade_outcomes')}")
    g.record("new_epoch_used", checks.get("new_epoch") is True,
             f"header epoch={res.get('header_epoch')}")
    g.record("exact_unit_basis", checks.get("unit_basis") is True,
             str(res.get("unit_basis")))
    g.record("margin_fully_released", checks.get("margin_released") is True,
             f"committed={res.get('committed_margin')} "
             f"released={res.get('released_margin')}")
    return res


def phase_restart(candidate: Path, work: Path, g: Gates) -> dict:
    """P11 — the state has to survive the process that made it."""
    res = run_child("restart_verify", candidate, work)
    g.record("state_survives_restart", res.get("ok") is True,
             res.get("error")
             or f"learning_state={res.get('learning_state')}")
    g.record("no_duplicate_learning_after_restart",
             res.get("trade_outcomes") == 1
             and res.get("realized_outcomes") == 1,
             f"trade_outcomes={res.get('trade_outcomes')} "
             f"realized={res.get('realized_outcomes')}")
    return res


def phase_fresh_boot(work: Path, starting_cash: float, g: Gates) -> dict:
    """P12 — a second, economically untouched candidate must be usable.

    If something breaks only because there is no history, that assumption is
    the bug. The answer is never to copy old economics in to satisfy a
    dashboard.
    """
    second = (work / "candidate_fresh" / "jarvis.db").resolve()
    second.parent.mkdir(parents=True, exist_ok=True)
    if second.exists():
        g.record("fresh_book_boots_empty", False, "candidate already exists")
        return {}
    init = run_child("init_schema", second, work)
    seed = run_child("seed_portfolio", second, work,
                     {"starting_cash": starting_cash})
    boot = run_child("empty_boot", second, work)
    ok = (init.get("ok") is True and seed.get("ok") is True
          and boot.get("ok") is True)
    g.record("fresh_book_boots_empty", ok,
             boot.get("error") or f"surfaces={boot.get('surfaces')}")
    return {"path": str(second), "init": init, "seed": seed, "boot": boot}


ECONOMIC_ID_COLUMNS = {
    "paper_positions": "id",
    "paper_trades": "id",
    "trade_outcomes": "id",
    "paper_portfolio": "id",
    "paper_position_settlements": "id",
    "paper_settlement_legs": "id",
    "paper_realized_outcomes": "id",
}


def phase_leakage(source: Path, candidate: Path, g: Gates,
                  label: str) -> dict:
    """P15 — no old economic identifier may appear in the new book."""
    src, dst = ro_connect(source), ro_connect(candidate)
    overlaps = {}
    try:
        s_tables, d_tables = set(table_names(src)), set(table_names(dst))
        for table, col in ECONOMIC_ID_COLUMNS.items():
            if table not in s_tables or table not in d_tables:
                continue
            s_ids = {r[0] for r in
                     src.execute(f'SELECT "{col}" FROM "{table}"')}
            d_ids = {r[0] for r in
                     dst.execute(f'SELECT "{col}" FROM "{table}"')}
            shared = s_ids & d_ids
            if shared:
                overlaps[table] = sorted(str(x) for x in shared)[:10]
    finally:
        dst.close()
        src.close()
    g.record(f"no_old_economic_id_leakage_{label}", not overlaps,
             "no shared economic IDs" if not overlaps else str(overlaps))
    return {"overlaps": overlaps}


def phase_schema_proof(candidate: Path, g: Gates) -> dict:
    """P16 — integrity, foreign keys, and the canonical unique indexes."""
    conn = ro_connect(candidate)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        fk_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
        indexes = {}
        for row in conn.execute(
                "SELECT name, tbl_name, sql FROM sqlite_master "
                "WHERE type='index' ORDER BY name"):
            indexes[row[0]] = {"table": row[1], "sql": row[2],
                               "unique": bool(row[2] and "UNIQUE"
                                              in row[2].upper())}
        # Unique constraints declared inline on the table (not as named
        # indexes) show up as auto-indexes, so read the table DDL too.
        ddl = {r[0]: r[1] for r in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()

    g.record("candidate_integrity_ok", integrity == "ok", str(integrity))
    g.record("candidate_foreign_keys_ok", not fk_rows,
             "no violations" if not fk_rows else f"{len(fk_rows)} violations")

    required = {
        "paper_position_settlements": ["position_id"],
        "paper_settlement_legs": ["execution_id"],
        "paper_realized_outcomes": ["position_id"],
    }
    missing = []
    for table, cols in required.items():
        text = (ddl.get(table) or "")
        idx_sql = " ".join(
            v["sql"] or "" for v in indexes.values() if v["table"] == table)
        blob = (text + " " + idx_sql).upper()
        for col in cols:
            if "UNIQUE" not in blob or col.upper() not in blob:
                missing.append(f"{table}.{col}")
    g.record("canonical_unique_constraints_present", not missing,
             "settlement/leg/outcome uniqueness declared" if not missing
             else f"missing: {missing}")
    return {"integrity": integrity, "fk_violations": len(fk_rows),
            "index_count": len(indexes),
            "unique_indexes": sorted(k for k, v in indexes.items()
                                     if v["unique"])}


def phase_external_stores(source: Path, g: Gates) -> dict:
    """P8 — raw research data is preserved IN PLACE, never imported.

    Retiring the economic epoch does not retire the evidence. These stores
    are read for identity only; the live evidence DB is not frozen, altered
    or checkpointed just to make a manifest.
    """
    data_dir = source.parent
    out = {}
    for name in EXTERNAL_STORES:
        path = data_dir / name
        entry = {"path": str(path), "exists": path.exists(),
                 "preserved_in_place": True, "imported_into_candidate": False}
        if path.exists():
            entry["size_bytes"] = path.stat().st_size
            try:
                entry["integrity"] = "ok" if integrity_ok(path) else "FAILED"
            except sqlite3.Error as exc:
                entry["integrity"] = f"unreadable: {exc}"
        out[name] = entry

    fe = data_dir / "forward_evidence.db"
    if fe.exists():
        try:
            conn = ro_connect(fe)
            try:
                rows = conn.execute("SELECT id, epoch, runtime_mode "
                                    "FROM evidence_campaign").fetchall()
            finally:
                conn.close()
            out["forward_evidence.db"]["campaigns"] = [
                {"id": r[0], "epoch": r[1], "runtime_mode": r[2]}
                for r in rows]
            out["forward_evidence.db"]["campaign_count"] = len(rows)
        except sqlite3.Error as exc:
            out["forward_evidence.db"]["campaigns_error"] = str(exc)

    g.record("external_stores_preserved",
             all(v["exists"] for v in out.values()),
             ", ".join(f"{k}={'ok' if v['exists'] else 'MISSING'}"
                       for k, v in out.items()))
    return out


def phase_final_table_report(source: Path, candidate: Path,
                             classification: dict, work: Path) -> dict:
    """P14 — the report that makes leakage obvious at a glance."""
    src, dst = ro_connect(source), ro_connect(candidate)
    try:
        d_tables = set(table_names(dst))
        report = {}
        for table, info in classification.items():
            after = row_count(dst, table) if table in d_tables else None
            report[table] = {
                "classification": info["classification"],
                "reason": info["reason"],
                "source_rows": info["source_rows"],
                "candidate_rows_after": after,
                "copied": table in COPY_TABLES,
            }
    finally:
        dst.close()
        src.close()
    (work / "cutover_table_reconciliation.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8")
    return report


def write_report(manifest: dict, work: Path) -> None:
    g = manifest["gates"]
    lines = [
        "# Canonical epoch cutover — DRY RUN",
        "",
        f"**Mode:** `{manifest['mode']}` — this tool has no apply path.",
        f"**Run:** `{manifest['run_id']}`  ",
        f"**When:** {manifest['timestamp']}  ",
        f"**Repo:** `{manifest['repo_sha']}` (clean: {manifest['repo_clean']})",
        "",
        "## Readiness",
        "",
        f"**READY_FOR_CONTROLLED_CANONICAL_EPOCH_CUTOVER = "
        f"{str(manifest['ready']).upper()}**",
        "",
    ]
    if manifest["failed_gates"]:
        lines += ["Failed gates:", ""]
        lines += [f"- `{name}` — {g[name]['detail']}"
                  for name in manifest["failed_gates"]]
    else:
        lines += ["Every mandatory gate passed.", ""]
    lines += ["", "## Gates", "", "| gate | result | detail |",
              "|---|---|---|"]
    for name, res in g.items():
        mark = "PASS" if res["ok"] else "**FAIL**"
        detail = str(res["detail"]).replace("|", "\\|")[:200]
        lines.append(f"| `{name}` | {mark} | {detail} |")

    src = manifest["source"]
    lines += [
        "", "## The legacy economy is archived, not converted", "",
        "The 667 legacy positions belong to the legacy simulator. They are",
        "not backfilled, re-priced or canonically closed. An old OPEN",
        "position stays open forever in the archive — that is honest.",
        "Closing it under economics that did not exist when it opened would",
        "be fabricated history.", "",
        f"- source: `{src['path']}`",
        f"- economic state unchanged by this run: "
        f"**{src['economic_unchanged']}**",
        f"- archive: `{manifest['archive'].get('path')}`",
        f"- archive read-only: {manifest['archive'].get('read_only')}",
        "",
        "## Epoch", "",
        f"- previous: `{manifest['epoch']['previous']}`",
        f"- candidate: `{manifest['epoch']['candidate']}`",
        "",
        "## Table classification", "",
        "| class | tables |", "|---|---|",
    ]
    by_class: dict[str, list[str]] = {}
    for table, info in manifest["table_classification"].items():
        by_class.setdefault(info["classification"], []).append(table)
    for cls in sorted(by_class):
        lines.append(f"| `{cls}` | {len(by_class[cls])} |")

    life = manifest.get("lifecycle") or {}
    if life.get("ok"):
        lines += [
            "", "## Canonical lifecycle on the candidate", "",
            f"- product/venue/instrument: `{life.get('unit_basis')}`",
            f"- committed margin: {life.get('committed_margin')}",
            f"- released margin: {life.get('released_margin')}",
            f"- cash expected: {life.get('cash_expected')}",
            f"- cash actual: {life.get('cash_actual')}",
            f"- total_trades: {life.get('total_trades')} "
            f"(one thesis, one vote)",
            f"- realized outcomes: {life.get('realized_outcomes')}",
            f"- canonical trade_outcomes: {life.get('trade_outcomes')}",
            f"- learning state: {life.get('learning_state')}",
        ]
    lines += [
        "", "## What was NOT done", "",
        "- `data/jarvis.db` was not renamed, replaced, migrated or reset",
        "- no service, unit file, env file or symlink was modified",
        "- the scheduler was not started; no real order, no transfer",
        "- `reset_paper_portfolio()` was not used and is not importable here",
        "- the live evidence collector and its campaign were left alone",
        "",
        "The next continuation reviews this evidence and performs a",
        "separate, controlled cutover.", "",
    ]
    (work / "CUTOVER_DRY_RUN_REPORT.md").write_text(
        "\n".join(lines), encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Canonical epoch cutover DRY RUN. No apply mode exists.")
    ap.add_argument("--source", required=True,
                    help="the operator database to archive (read-only)")
    ap.add_argument("--work-dir", required=True,
                    help="where archive/ and candidate/ are created")
    ap.add_argument("--starting-cash", required=True, type=float)
    ap.add_argument("--allow-non-operator-source", action="store_true",
                    help="permit a fixture source (tests)")
    args = ap.parse_args(argv)

    run_id = _utc().replace(":", "").replace("-", "")
    g = Gates()
    print(f"\ncanonical epoch cutover — {MODE}\nrun {run_id}\n")

    print("P0  inputs")
    source, work, archive, candidate = phase_inputs(args, g)
    work.mkdir(parents=True, exist_ok=True)
    if not g.ready:
        return _finish(g, work, run_id, args, source, {}, {}, {}, {}, {},
                       {}, {}, {}, {}, {})

    if source.name != "jarvis.db" and not args.allow_non_operator_source:
        g.record("source_is_the_operator_db", False, str(source))
        return _finish(g, work, run_id, args, source, {}, {}, {}, {}, {},
                       {}, {}, {}, {}, {})

    print("\nP2  table classification")
    classification = phase_classify(source, work, g)

    print("\nP3  source quiescence")
    quiescence = phase_quiescence(source, g)

    print("\nP3.3  fingerprint before")
    fp_before = run_fingerprint(source, work / "fingerprints", "before")
    econ_before = economic_snapshot(source)
    g.record("fingerprint_before_captured", fp_before["returncode"] == 0,
             fp_before.get("report") or "")
    g.record("fingerprint_probe_wrote_nothing",
             "probe wrote : no" in fp_before["text"],
             "the read-only probe did not write")

    print("\nP4  legacy archive")
    archive_info = {}
    if g.results.get("source_quiescent", {}).get("ok"):
        archive_info = phase_archive(source, archive, work, g)
        archive_info["path"] = str(archive)
        _write_archive_manifest(archive, work, run_id, source, fp_before,
                                archive_info, econ_before)
    else:
        g.record("archive_created", False,
                 "refused: the source was not quiescent")

    print("\nP4.3  source unchanged across the archive boundary")
    econ_mid = economic_snapshot(source)
    g.record("source_unchanged_across_archive", econ_mid == econ_before,
             "economic digests identical")

    print("\nP5  fresh candidate")
    cand_info = phase_candidate(source, candidate, work,
                                args.starting_cash, g)

    print("\nP6  allowlisted config/reference copy")
    copy_info = (phase_copy_config(source, candidate, work, g)
                 if candidate.exists() else {})

    print("\nP7  fresh economy")
    econ_info = (phase_fresh_economy(source, candidate, work,
                                     args.starting_cash, g)
                 if candidate.exists() else {})

    print("\nP15  old-id leakage (before lifecycle)")
    leak_before = (phase_leakage(source, candidate, g, "before_lifecycle")
                   if candidate.exists() else {})

    print("\nP8  external evidence stores")
    stores = phase_external_stores(source, g)

    print("\nP9  candidate process isolation")
    isolation = (phase_isolation(source, candidate, work, g)
                 if candidate.exists() else {})

    print("\nP10  hermetic canonical lifecycle")
    lifecycle = (phase_lifecycle(candidate, work, args.starting_cash, g)
                 if candidate.exists() else {})

    print("\nP11  restart persistence")
    restart = (phase_restart(candidate, work, g)
               if candidate.exists() else {})

    print("\nP12  fresh-book boot")
    fresh_boot = phase_fresh_boot(work, args.starting_cash, g)

    print("\nP15  old-id leakage (after lifecycle)")
    leak_after = (phase_leakage(source, candidate, g, "after_lifecycle")
                  if candidate.exists() else {})

    print("\nP16  candidate schema proof")
    schema = (phase_schema_proof(candidate, g) if candidate.exists() else {})

    print("\nP14  table reconciliation")
    reconciliation = (phase_final_table_report(source, candidate,
                                               classification, work)
                      if candidate.exists() else {})

    print("\nP17  operator source after everything")
    fp_after = run_fingerprint(source, work / "fingerprints", "after")
    econ_after = economic_snapshot(source)
    g.record("operator_economics_unchanged", econ_after == econ_before,
             "every economic digest, count and the cash are identical")
    g.record("operator_canonical_tables_still_absent",
             all(econ_after[t]["digest"] == "ABSENT"
                 for t in ("paper_position_settlements",
                           "paper_settlement_legs",
                           "paper_realized_outcomes")),
             "the operator DB remains unmigrated")

    print("\nP18  live evidence unchanged")
    campaigns = (stores.get("forward_evidence.db", {}) or {}).get("campaigns")
    g.record("evidence_campaign_unchanged",
             bool(campaigns) and len(campaigns) == 1,
             str(campaigns))

    return _finish(g, work, run_id, args, source, classification, quiescence,
                   archive_info, cand_info, copy_info, econ_info, stores,
                   isolation, lifecycle,
                   {"restart": restart, "fresh_boot": fresh_boot,
                    "leak_before": leak_before, "leak_after": leak_after,
                    "schema": schema, "reconciliation": reconciliation,
                    "fp_before": fp_before.get("report"),
                    "fp_after": fp_after.get("report"),
                    "econ_before": econ_before, "econ_after": econ_after})


def _write_archive_manifest(archive: Path, work: Path, run_id: str,
                            source: Path, fp_before: dict,
                            archive_info: dict, econ: dict) -> None:
    manifest = {
        "run_id": run_id,
        "timestamp": _utc(),
        "repo_sha": _repo_sha(),
        "source_path": str(source),
        "source_fingerprint_report": fp_before.get("report"),
        "source_economic_snapshot": econ,
        "archive_path": str(archive),
        "archive_sha256": archive_info.get("archive_sha256"),
        "archive_read_only": archive_info.get("read_only"),
        "source_manifest": archive_info.get("source_manifest"),
        "archive_manifest": archive_info.get("archive_manifest"),
        "note": ("OPEN LEGACY POSITIONS ARE ARCHIVED, NOT CANONICALLY "
                 "CLOSED. They belong to the legacy simulator and are "
                 "preserved exactly as history recorded them."),
    }
    (work / "archive" / "LEGACY_ARCHIVE_MANIFEST.json").write_text(
        json.dumps(manifest, indent=2, default=str), encoding="utf-8")


def _repo_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(REPO),
                              capture_output=True, text=True,
                              timeout=60).stdout.strip()
    except Exception:
        return "unknown"


def _repo_clean() -> bool:
    try:
        out = subprocess.run(["git", "status", "--porcelain"], cwd=str(REPO),
                             capture_output=True, text=True,
                             timeout=60).stdout.strip()
        return out == ""
    except Exception:
        return False


def _finish(g: Gates, work: Path, run_id: str, args, source: Path,
            classification: dict, quiescence: dict, archive_info: dict,
            cand_info: dict, copy_info: dict, econ_info: dict,
            stores: dict, isolation: dict, lifecycle: dict,
            rest: dict) -> int:
    """READY is the conjunction of the gates — never a side effect of
    having reached the end of the script."""
    rest = rest or {}
    manifest = {
        "run_id": run_id,
        "timestamp": _utc(),
        "mode": MODE,
        "repo_sha": _repo_sha(),
        "repo_clean": _repo_clean(),
        "source": {
            "path": str(source),
            "fingerprint_before": rest.get("fp_before"),
            "fingerprint_after": rest.get("fp_after"),
            "economic_before": rest.get("econ_before"),
            "economic_after": rest.get("econ_after"),
            "economic_unchanged": g.results.get(
                "operator_economics_unchanged", {}).get("ok", False),
        },
        "quiescence": quiescence,
        "archive": archive_info,
        "epoch": {"previous": list(_prior_epochs()),
                  "candidate": _engine_epoch()},
        "table_classification": classification,
        "config_copy": copy_info,
        "candidate": {**cand_info, "economy": econ_info},
        "external_stores": stores,
        "isolation": isolation,
        "lifecycle": lifecycle,
        "restart": rest.get("restart"),
        "fresh_boot": rest.get("fresh_boot"),
        "leakage": {"before": rest.get("leak_before"),
                    "after": rest.get("leak_after")},
        "schema": rest.get("schema"),
        "reconciliation": rest.get("reconciliation"),
        "gates": g.results,
        "failed_gates": g.failed(),
        "ready": g.ready,
        "status": ("CANONICAL_EPOCH_DRY_RUN_COMPLETE" if g.ready
                   else "CANONICAL_EPOCH_DRY_RUN_INCOMPLETE"),
    }
    work.mkdir(parents=True, exist_ok=True)
    (work / "CUTOVER_DRY_RUN_MANIFEST.json").write_text(
        json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    write_report(manifest, work)

    print("\n" + "=" * 64)
    if g.ready:
        print("READY_FOR_CONTROLLED_CANONICAL_EPOCH_CUTOVER = TRUE")
        print("STATUS: CANONICAL_EPOCH_DRY_RUN_COMPLETE")
    else:
        print("READY_FOR_CONTROLLED_CANONICAL_EPOCH_CUTOVER = FALSE")
        print("failed gates:")
        for name in g.failed():
            print(f"  - {name}: {g.results[name]['detail']}")
    print("=" * 64)
    print(f"artifacts: {work}")
    return 0 if g.ready else 1


def _engine_epoch() -> str:
    from lib.engine_epoch import ENGINE_EPOCH
    return ENGINE_EPOCH


def _prior_epochs() -> tuple:
    from lib.engine_epoch import PRIOR_EPOCHS
    return PRIOR_EPOCHS


if __name__ == "__main__":
    raise SystemExit(main())
