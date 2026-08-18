"""One research campaign keeps ONE epoch, across every restart.

THE DEFECT THIS FIXES, WHICH ALREADY HAPPENED THREE TIMES.

The collector minted its epoch with

    os.environ.setdefault("JARVIS_EVIDENCE_EPOCH", epoch_name(now))

inside `main()`. Nothing pinned it, so every process start generated a fresh
epoch from the wall clock. Three service restarts produced three epochs and
three different activation boundaries:

    FORWARD_EVIDENCE_20260818T072950Z
    FORWARD_EVIDENCE_20260818T073121Z
    FORWARD_EVIDENCE_20260818T075321Z

Only the last accumulated observations, so nothing was lost this time. Left
alone it would have shattered a long-running prospective dataset into a new
fragment on every systemd restart, host reboot or WSL restart — and the
damage would have been silent, because each fragment looks perfectly healthy
on its own. A campaign that changes identity whenever the process bounces is
not a campaign.

WHERE IDENTITY LIVES NOW. In the evidence database, beside the evidence it
describes. A state file next to the repo would drift if the DB were moved or
a second DB were pointed at; storing it in the dataset means the campaign and
its rows cannot disagree about which campaign they belong to.

CREATED ONCE, THEN READ. `get_or_create` writes exactly one row. Later starts
read it. Minting a NEW campaign is a deliberate operator act
(`start_new_campaign`), never a side effect of a restart.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

TABLE = "evidence_campaign"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def epoch_name(started: datetime) -> str:
    return "FORWARD_EVIDENCE_" + started.strftime("%Y%m%dT%H%M%SZ")


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            id            INTEGER PRIMARY KEY CHECK (id = 1),
            epoch         TEXT NOT NULL,
            boundary_at   TEXT NOT NULL,
            created_at    TEXT NOT NULL,
            runtime_mode  TEXT,
            note          TEXT
        )""")


def get_or_create(db_path: str, *, started: datetime | None = None,
                  runtime_mode: str = "EVIDENCE_ONLY",
                  epoch: str | None = None,
                  boundary_at: str | None = None) -> dict:
    """The campaign identity for this evidence database.

    Returns the EXISTING row when there is one — which is the entire point.
    `epoch`/`boundary_at` are honoured only when creating the very first row,
    so an operator can adopt an identity that predates this table.
    """
    started = started or _now()
    with sqlite3.connect(db_path) as conn:
        _ensure_table(conn)
        row = conn.execute(
            f"SELECT epoch, boundary_at, created_at, runtime_mode "
            f"FROM {TABLE} WHERE id = 1").fetchone()
        if row:
            out = {"epoch": row[0], "boundary_at": row[1],
                   "created_at": row[2], "runtime_mode": row[3],
                   "created": False}
            logger.info("[EvidenceCampaign] continuing %s (boundary %s)",
                        out["epoch"], out["boundary_at"])
            return out

        ep = epoch or epoch_name(started)
        bd = boundary_at or started.isoformat()
        conn.execute(
            f"INSERT INTO {TABLE} (id, epoch, boundary_at, created_at, "
            f"runtime_mode, note) VALUES (1, ?, ?, ?, ?, ?)",
            (ep, bd, started.isoformat(), runtime_mode,
             "one campaign, one epoch — survives restarts by design"))
        logger.info("[EvidenceCampaign] created %s (boundary %s)", ep, bd)
        return {"epoch": ep, "boundary_at": bd,
                "created_at": started.isoformat(),
                "runtime_mode": runtime_mode, "created": True}


def current(db_path: str) -> dict | None:
    """The campaign, or None if this database has never run one."""
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            row = conn.execute(
                f"SELECT epoch, boundary_at, created_at, runtime_mode "
                f"FROM {TABLE} WHERE id = 1").fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    return {"epoch": row[0], "boundary_at": row[1], "created_at": row[2],
            "runtime_mode": row[3]}


def start_new_campaign(db_path: str, *, note: str = "") -> dict:
    """Deliberately begin a NEW campaign. Never called by a restart.

    Splitting a prospective dataset is a research decision with consequences
    for every comparison drawn across the boundary, so it takes an explicit
    call rather than happening because a process bounced.
    """
    started = _now()
    prev = current(db_path)
    ep = epoch_name(started)
    # Epoch names carry second resolution, so a new campaign started within
    # the same second as the previous one would reuse its name and silently
    # MERGE two campaigns that were meant to be separate. Distinctness is
    # the entire purpose of the identity, so it is enforced rather than
    # assumed.
    if prev and prev.get("epoch") == ep:
        n = 2
        while f"{ep}_{n}" == prev.get("epoch"):
            n += 1
        ep = f"{ep}_{n}"
    with sqlite3.connect(db_path) as conn:
        _ensure_table(conn)
        conn.execute(f"DELETE FROM {TABLE} WHERE id = 1")
        conn.execute(
            f"INSERT INTO {TABLE} (id, epoch, boundary_at, created_at, "
            f"runtime_mode, note) VALUES (1, ?, ?, ?, ?, ?)",
            (ep, started.isoformat(), started.isoformat(),
             "EVIDENCE_ONLY", note or "operator started a new campaign"))
    return current(db_path)
