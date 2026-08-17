"""Prove what the scheduler WILL do, before letting it do anything.

WHY NOT JUST READ app/scheduler.py. Because reading source tells you what
someone wrote, not what the process will register. The quarantine depends on
an environment variable evaluated at build time, and an env var is exactly
the kind of thing that is set in one shell and absent in another. This
builds the real scheduler object with the real environment and asks it for
its job list — then throws it away without ever calling start().

WHAT MUST BE TRUE UNDER VIRTUAL_ONLY:

  * JARVIS_ENABLE_EXTERNAL_BROKER_CONNECTOR is off
  * `execute`  is NOT scheduled — it submits orders to Alpaca
  * `guardian` is NOT scheduled — it reads the external Alpaca account and
    is not the desk's own risk view
  * `positions` is NOT scheduled — it manages Alpaca positions
  * live execution is refused at the boundary, not merely unscheduled

NEVER TOUCHES THE OPERATOR DATABASE. Both stores are redirected to copies
before app.database is imported, because that module builds its engine at
import time.

    python scripts/verify_scheduler_safety.py

Exit 0 only if every invariant holds. A non-zero exit means DO NOT START
THE SCHEDULER.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# Jobs that reach a real broker. None of these may be scheduled while the
# platform is virtual.
LIVE_MONEY_JOBS = {
    "execute":   "submits orders to Alpaca",
    "guardian":  "reads the external Alpaca account",
    "positions": "manages Alpaca positions",
}


def copy_databases() -> str:
    tmp = tempfile.mkdtemp(prefix="jarvis-sched-verify-")
    first = ""
    for name in ("jarvis.db", "events.db"):
        src = REPO / "data" / name
        if not src.exists():
            continue
        dst = Path(tmp) / name
        s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        d = sqlite3.connect(dst)
        with d:
            s.backup(d)
        s.close(); d.close()
        if name == "jarvis.db":
            os.environ["JARVIS_DB_PATH"] = str(dst)
            first = str(dst)
        else:
            os.environ["JARVIS_EVENTS_DB_PATH"] = str(dst)
    return first


def main() -> int:
    db = copy_databases()
    os.environ.setdefault("JARVIS_PLATFORM_MODE", "VIRTUAL_ONLY")
    print(f"database copy : {db or '(none)'}")

    failures: list[str] = []

    from lib import platform_mode as PM
    from app import scheduler as S

    mode = PM.current_mode()
    connector = S.external_connector_enabled()
    print(f"platform mode : {mode}")
    print(f"external broker connector : {'ENABLED' if connector else 'off'}")

    if mode != PM.VIRTUAL_ONLY:
        failures.append(f"platform mode is {mode}, expected VIRTUAL_ONLY")
    if connector:
        failures.append("JARVIS_ENABLE_EXTERNAL_BROKER_CONNECTOR is enabled")

    # Build the real thing. Never started; the object is discarded below.
    sched = S.create_scheduler()
    jobs = {j.id: j for j in sched.get_jobs()}
    print(f"\njobs the scheduler would register ({len(jobs)}):")
    for jid in sorted(jobs):
        print(f"  {jid:<24} next={getattr(jobs[jid], 'next_run_time', None)}")

    print("\nlive-money jobs:")
    for jid, what in sorted(LIVE_MONEY_JOBS.items()):
        present = jid in jobs
        print(f"  {jid:<24} {'SCHEDULED' if present else 'absent':<10} ({what})")
        if present:
            failures.append(f"{jid} is scheduled and {what}")

    # The boundary itself, independent of scheduling. A job that is merely
    # unscheduled today is one config edit from running; this is the guard
    # that refuses regardless.
    try:
        PM.assert_live_execution_allowed("scheduler safety probe")
        failures.append("assert_live_execution_allowed did NOT raise under VIRTUAL_ONLY")
        print("\nlive execution boundary : DID NOT REFUSE  <-- serious")
    except Exception as e:
        print(f"\nlive execution boundary : refused ({type(e).__name__})")

    # Discard without starting. APScheduler builds jobs at construction, so
    # reaching this line means nothing has fired.
    try:
        sched.shutdown(wait=False)
    except Exception:
        pass

    print("\n" + "=" * 66)
    if failures:
        print("DO NOT START THE SCHEDULER:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("SAFE TO START: virtual-only, no live-money job scheduled, and the")
    print("execution boundary refuses independently of what is scheduled.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
