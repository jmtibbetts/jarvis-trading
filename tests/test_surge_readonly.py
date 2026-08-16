"""How often someone LOOKS at a detector must not change what it measures.

`GET /onchain/surge` ran the surge pass with persistence ON. Every
dashboard refresh wrote a TokenActivitySnapshot, advanced the baseline and
updated surge state — so the operator's refresh rate became an input to the
model. Leaving the tab open on a fast poll would manufacture a dense
baseline; closing it would starve one. Two operators watching the same
desk would compute different surge scores.

Worse, it was the FAST writer. The only scheduled writer was
wallet_discovery every 20 minutes, which is three samples an hour against a
detector whose inputs include 5-minute volume, transactions and unique
wallets. Human refreshes were quietly supplying most of the history.

Ownership now: the scheduled `token_surge` sampler writes; the route reads.
"""
import unittest
from unittest.mock import patch


class RouteIsReadOnlyTests(unittest.TestCase):
    def test_the_route_asks_for_no_persistence(self):
        from app.routers import onchain
        with patch("lib.token_surge.scan_and_score",
                   return_value={"tokens": []}) as s:
            onchain.token_surge(limit=20)
        self.assertIs(s.call_args.kwargs.get("persist"), False,
                      "a dashboard refresh must not write surge history")

    def test_twenty_refreshes_write_nothing(self):
        """The audit's stated regression: N snapshots before, N after."""
        from app.routers import onchain
        with patch("lib.token_surge.scan_and_score",
                   return_value={"tokens": []}) as s:
            for _ in range(20):
                onchain.token_surge(limit=20)
        self.assertEqual(s.call_count, 20)
        for call in s.call_args_list:
            self.assertIs(call.kwargs.get("persist"), False)

    def test_the_response_declares_it_is_read_only(self):
        from app.routers import onchain
        with patch("lib.token_surge.scan_and_score",
                   return_value={"tokens": []}):
            out = onchain.token_surge(limit=5)
        self.assertTrue(out["read_only"])
        self.assertIn("scheduled", out["ingestion"])

    def test_scoring_is_unchanged_only_storage_is_skipped(self):
        """persist=False must not become a second, weaker definition of
        surge — the same function scores either way."""
        import inspect

        from lib.token_surge import scan_and_score
        src = inspect.getsource(scan_and_score)
        self.assertIn("persist", src)
        self.assertNotIn("def score_snapshot_readonly", src)


class SamplerOwnsIngestionTests(unittest.TestCase):
    def test_a_scheduled_sampler_exists_and_persists(self):
        import inspect

        from app import scheduler
        src = inspect.getsource(scheduler.create_scheduler)
        self.assertIn("token_surge_run", src)
        self.assertIn("persist=True", src,
                      "something scheduled must own the writes")

    def test_the_sampler_is_far_faster_than_wallet_discovery(self):
        """A 5-minute acceleration cannot be baselined at 3 samples/hour."""
        import os

        from app.scheduler import create_scheduler
        sched = create_scheduler()
        try:
            jobs = {j.id: j for j in sched.get_jobs()}
            self.assertIn("token_surge", jobs)
            surge = jobs["token_surge"].trigger.interval.total_seconds()
            discovery = jobs["wallet_discovery"].trigger.interval.total_seconds()
            self.assertLess(surge, discovery / 4,
                            "the sampler must be much cheaper and much faster")
            self.assertLessEqual(surge, 3600)
            self.assertGreaterEqual(surge, 30)
        finally:
            try:
                sched.shutdown(wait=False)
            except Exception:
                pass

    def test_the_cadence_is_configurable(self):
        import os

        old = os.environ.get("TOKEN_SURGE_SCAN_INTERVAL_SECONDS")
        try:
            os.environ["TOKEN_SURGE_SCAN_INTERVAL_SECONDS"] = "45"
            from app.scheduler import create_scheduler
            sched = create_scheduler()
            try:
                jobs = {j.id: j for j in sched.get_jobs()}
                self.assertEqual(
                    jobs["token_surge"].trigger.interval.total_seconds(), 45)
            finally:
                try:
                    sched.shutdown(wait=False)
                except Exception:
                    pass
        finally:
            if old is None:
                os.environ.pop("TOKEN_SURGE_SCAN_INTERVAL_SECONDS", None)
            else:
                os.environ["TOKEN_SURGE_SCAN_INTERVAL_SECONDS"] = old

    def test_the_sampler_reports_health(self):
        """A stalled sampler starves every baseline downstream, so its lag
        has to be visible rather than inferred from missing data."""
        from app.scheduler import job_status
        self.assertIn("token_surge", job_status)


if __name__ == "__main__":
    unittest.main()
