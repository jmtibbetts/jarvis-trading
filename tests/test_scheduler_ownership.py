"""Only one process may run economic jobs against one book.

WHY THIS EXISTS. Two `main.py` processes pointed at the same database both
started APScheduler. Both would evaluate the same candidates, open entries,
manage the same positions and project learning. APScheduler's
`max_instances=1` does not help — it is a per-PROCESS guard and these are
two processes. Nothing recorded who was in charge, so the duplication was
silent: the book trades twice and the operator sees one set of logs.
"""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


class TheLeaseIsExclusiveTests(unittest.TestCase):

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(prefix="jarvis-lease-")
        self.addCleanup(self._dir.cleanup)
        self.db = Path(self._dir.name) / "book.db"
        self.db.write_bytes(b"")

    def test_the_first_caller_is_granted(self):
        from lib import scheduler_lease as SL
        lease = SL.acquire(self.db)
        self.addCleanup(lease.release)
        self.assertTrue(lease.granted, lease.detail)
        self.assertEqual(lease.state, SL.ACQUIRED)

    def test_a_second_caller_in_another_process_is_refused(self):
        """The real shape of the failure: a SECOND PROCESS. An in-process
        second call cannot prove this — POSIX advisory locks are held per
        open file description, and a same-process re-lock can succeed."""
        from lib import scheduler_lease as SL
        first = SL.acquire(self.db)
        self.addCleanup(first.release)
        self.assertTrue(first.granted)

        probe = textwrap.dedent(f"""
            import json, sys
            sys.path.insert(0, {str(REPO)!r})
            from lib import scheduler_lease as SL
            lease = SL.acquire({str(self.db)!r})
            print(json.dumps({{"state": lease.state,
                               "owner_pid": (lease.owner or {{}}).get("pid")}}))
        """)
        r = subprocess.run([sys.executable, "-c", probe],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(out["state"], "STANDBY",
                         "a second process was allowed to own the scheduler")
        self.assertEqual(out["owner_pid"], os.getpid(),
                         "the standby process cannot name the real owner")

    def test_releasing_lets_the_next_process_in(self):
        """A restart must not be locked out by its own predecessor."""
        from lib import scheduler_lease as SL
        first = SL.acquire(self.db)
        self.assertTrue(first.granted)
        first.release()
        second = SL.acquire(self.db)
        self.addCleanup(second.release)
        self.assertTrue(second.granted,
                        "the lease was not released — a restart would stand by "
                        "forever")

    def test_a_dead_holder_does_not_block_forever(self):
        """The reason this is a kernel lock and not a lease row: a process
        that dies — cleanly or not — releases it. A lease table would need a
        heartbeat and an expiry, and a crashed holder would look alive."""
        from lib import scheduler_lease as SL
        probe = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {str(REPO)!r})
            from lib import scheduler_lease as SL
            lease = SL.acquire({str(self.db)!r})
            assert lease.granted
            sys.exit(0)                 # exits holding it
        """)
        r = subprocess.run([sys.executable, "-c", probe],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        after = SL.acquire(self.db)
        self.addCleanup(after.release)
        self.assertTrue(after.granted,
                        "a dead holder's lease was never released")

    def test_two_different_books_do_not_contend(self):
        """The lock is scoped to the DATABASE, not the checkout. One process
        per book, not one process per machine."""
        from lib import scheduler_lease as SL
        other = Path(self._dir.name) / "other.db"
        other.write_bytes(b"")
        a = SL.acquire(self.db)
        b = SL.acquire(other)
        self.addCleanup(a.release)
        self.addCleanup(b.release)
        self.assertTrue(a.granted)
        self.assertTrue(b.granted, "two separate books were made to contend")

    def test_the_lock_sits_beside_the_database(self):
        from lib import scheduler_lease as SL
        lease = SL.acquire(self.db)
        self.addCleanup(lease.release)
        self.assertEqual(Path(lease.path).parent, self.db.parent)
        self.assertIn(self.db.name, Path(lease.path).name)


class StartupHonoursTheLeaseTests(unittest.TestCase):
    """Structural: the startup path must not start the scheduler unless the
    lease was actually granted."""

    def test_startup_starts_the_scheduler_only_when_granted(self):
        src = (REPO / "main.py").read_text(encoding="utf-8")
        self.assertIn("scheduler_lease", src)
        i = src.index("lease = SL.acquire")
        j = src.index("scheduler.start()")
        self.assertLess(i, j, "the scheduler starts before ownership is claimed")
        between = src[i:j]
        self.assertIn("lease.granted", between,
                      "the scheduler start is not conditioned on the lease")

    def test_an_unprovable_lease_does_not_start_jobs(self):
        """UNAVAILABLE is not ACQUIRED. A guard that cannot run is not
        permission to act."""
        src = (REPO / "main.py").read_text(encoding="utf-8")
        tail = src[src.index("lease = SL.acquire"):]
        tail = tail[:tail.index("# Crypto L2 order book streams")]
        self.assertIn("Refusing to run economic jobs", tail)


if __name__ == "__main__":
    unittest.main()
