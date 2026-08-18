"""Pin the starvation defect and the campaign-identity defect.

Both were live bugs. The first ran for two hours behind a green service; the
second silently minted three epochs across three restarts. Tests now assert
the conditions that would have surfaced each one immediately.
"""
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from jobs import evidence_collector as EC
from lib import evidence_campaign as ECAMP


class StageHealthTests(unittest.TestCase):
    def setUp(self):
        for name in EC._stages:
            EC._stages[name] = EC._new_stage(EC._stages[name]["cadence_s"])
        EC._threads.clear()

    def test_a_stage_that_never_ran_is_not_healthy_once_past_grace(self):
        """THE EXACT BUG: service alive, signal stage never entered."""
        h = EC.stage_health(EC.STAGE_SIGNAL, uptime_s=7200)   # 2 hours up
        self.assertEqual(h["status"], EC.NEVER_RAN)
        self.assertEqual(h["successes"], 0)
        # and the aggregate must NOT be healthy just because others are
        stages = {EC.STAGE_MARKET: {"status": EC.HEALTHY},
                  EC.STAGE_SIGNAL: h}
        self.assertNotEqual(EC.aggregate_health(stages), EC.HEALTHY)

    def test_a_young_service_is_starting_not_stale(self):
        """A 30-minute stage is not stale 30 seconds after boot."""
        self.assertEqual(EC.stage_health(EC.STAGE_SIGNAL, uptime_s=30)["status"],
                         EC.STARTING)

    def test_failure_does_not_refresh_the_success_clock(self):
        t0 = EC._stage_begin(EC.STAGE_SIGNAL)
        EC._stage_end(EC.STAGE_SIGNAL, t0, False, "boom")
        st = EC._stages[EC.STAGE_SIGNAL]
        self.assertEqual(st["attempts"], 1)
        self.assertEqual(st["failures"], 1)
        self.assertEqual(st["successes"], 0)
        self.assertIsNone(st["last_success_at"])      # the load-bearing bit
        self.assertIsNotNone(st["last_failure_at"])
        self.assertEqual(st["last_error"], "boom")

    def test_success_updates_and_clears_the_failure_streak(self):
        t0 = EC._stage_begin(EC.STAGE_SIGNAL)
        EC._stage_end(EC.STAGE_SIGNAL, t0, False, "x")
        t1 = EC._stage_begin(EC.STAGE_SIGNAL)
        EC._stage_end(EC.STAGE_SIGNAL, t1, True)
        st = EC._stages[EC.STAGE_SIGNAL]
        self.assertEqual(st["successes"], 1)
        self.assertEqual(st["consecutive_failures"], 0)
        self.assertIsNotNone(st["last_success_at"])

    def test_a_long_running_stage_is_not_called_failed(self):
        """Market refresh legitimately takes many minutes."""
        EC._stage_begin(EC.STAGE_MARKET)
        h = EC.stage_health(EC.STAGE_MARKET, uptime_s=7200)
        self.assertTrue(h["in_progress"])
        self.assertEqual(h["status"], EC.RUNNING_LONG)
        self.assertNotEqual(h["status"], EC.FAILED)

    def test_a_dead_worker_thread_is_failed_not_healthy(self):
        t0 = EC._stage_begin(EC.STAGE_SIGNAL)
        EC._stage_end(EC.STAGE_SIGNAL, t0, True)
        dead = threading.Thread(target=lambda: None)
        dead.start()
        dead.join()
        EC._threads[EC.STAGE_SIGNAL] = dead
        self.assertEqual(EC.stage_health(EC.STAGE_SIGNAL, uptime_s=60)["status"],
                         EC.FAILED)

    def test_repeated_failures_degrade(self):
        for _ in range(3):
            t = EC._stage_begin(EC.STAGE_SCAN)
            EC._stage_end(EC.STAGE_SCAN, t, False, "e")
        t = EC._stage_begin(EC.STAGE_SCAN)
        EC._stage_end(EC.STAGE_SCAN, t, True)
        EC._stages[EC.STAGE_SCAN]["consecutive_failures"] = 3
        self.assertEqual(EC.stage_health(EC.STAGE_SCAN, uptime_s=60)["status"],
                         EC.DEGRADED)

    def test_all_stages_succeeding_is_healthy(self):
        for name in EC._stages:
            t = EC._stage_begin(name)
            EC._stage_end(name, t, True)
        stages = {n: EC.stage_health(n, uptime_s=60) for n in EC._stages}
        self.assertEqual(EC.aggregate_health(stages), EC.HEALTHY)


class CampaignIdentityTests(unittest.TestCase):
    """One campaign keeps ONE epoch across restarts. It did not, three times."""

    def setUp(self):
        self.db = str(Path(tempfile.mkdtemp(prefix="camp-")) / "e.db")
        sqlite3.connect(self.db).close()

    def test_restart_continues_the_same_campaign(self):
        first = ECAMP.get_or_create(self.db)
        second = ECAMP.get_or_create(self.db)      # a "restart"
        third = ECAMP.get_or_create(self.db)
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["epoch"], second["epoch"], "restart split the epoch")
        self.assertEqual(first["boundary_at"], third["boundary_at"])

    def test_the_boundary_never_moves_forward_on_restart(self):
        """A moving boundary would silently exclude freshly eligible signals."""
        a = ECAMP.get_or_create(self.db)
        b = ECAMP.get_or_create(self.db)
        self.assertEqual(a["boundary_at"], b["boundary_at"])

    def test_an_existing_identity_can_be_adopted_once(self):
        r = ECAMP.get_or_create(self.db, epoch="FORWARD_EVIDENCE_XYZ",
                                boundary_at="2026-08-18T07:53:21+00:00")
        self.assertEqual(r["epoch"], "FORWARD_EVIDENCE_XYZ")
        # and adoption arguments are ignored afterwards
        again = ECAMP.get_or_create(self.db, epoch="SOMETHING_ELSE")
        self.assertEqual(again["epoch"], "FORWARD_EVIDENCE_XYZ")

    def test_a_new_campaign_takes_a_deliberate_call(self):
        first = ECAMP.get_or_create(self.db)
        new = ECAMP.start_new_campaign(self.db, note="test")
        self.assertNotEqual(first["epoch"], new["epoch"])

    def test_current_returns_none_for_a_database_with_no_campaign(self):
        empty = str(Path(tempfile.mkdtemp(prefix="camp2-")) / "none.db")
        sqlite3.connect(empty).close()
        self.assertIsNone(ECAMP.current(empty))

    def test_the_collector_no_longer_mints_an_epoch_from_the_clock(self):
        """os.environ.setdefault(..., epoch_name(now)) was the defect."""
        import inspect
        src = inspect.getsource(EC)
        self.assertNotIn('os.environ.setdefault("JARVIS_EVIDENCE_EPOCH"', src)
        self.assertIn("evidence_campaign", src)


if __name__ == "__main__":
    unittest.main()
