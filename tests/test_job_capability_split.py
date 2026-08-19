"""Looking and acting are different permissions.

Turning the scheduler off was the only way to stop JARVIS trading, and it
also stopped every paid data feed. Helius, LunarCrush, TwelveData, Massive,
Tavily, FRED, EIA, CoinGecko — all driven by scheduler jobs, all idle for
days while their subscriptions were being paid for, during exactly the
period we most wanted market evidence.
"""
import ast
import pathlib
import unittest
from unittest.mock import patch

REPO = pathlib.Path(__file__).resolve().parent.parent


def _registered_job_ids():
    """Every id app/scheduler.py registers, read from the source."""
    tree = ast.parse((REPO / "app" / "scheduler.py").read_text(encoding="utf-8"))
    ids = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "attr", None) != "add_job":
            continue
        for kw in node.keywords:
            if kw.arg == "id" and isinstance(kw.value, ast.Constant):
                ids.add(kw.value.value)
    return ids


class EveryJobIsClassifiedTests(unittest.TestCase):

    def test_every_registered_job_has_a_capability(self):
        from lib import job_capability as JC
        unclassified = sorted(j for j in _registered_job_ids()
                              if not JC.is_classified(j))
        self.assertEqual(unclassified, [],
                         f"unclassified scheduler jobs: {unclassified}")

    def test_the_classification_covers_nothing_that_does_not_exist(self):
        """A stale entry for a deleted job hides the fact that it is gone."""
        from lib import job_capability as JC
        registered = _registered_job_ids()
        stale = sorted(j for j in JC.describe_flat() if j not in registered)
        self.assertEqual(stale, [], f"classified but never registered: {stale}")

    def test_an_unknown_job_is_treated_as_economic(self):
        """FAIL CLOSED. Withholding a data job costs a gap in a dataset;
        running an unauthorised economic one costs a trade nobody asked
        for. Those are not comparable."""
        from lib import job_capability as JC
        self.assertEqual(JC.capability_of("a_job_invented_just_now"),
                         JC.ECONOMIC)
        self.assertNotIn(JC.capability_of("a_job_invented_just_now"),
                         JC.allowed_capabilities(economic=False))

    def test_anything_that_moves_the_book_is_economic(self):
        from lib import job_capability as JC
        for job in ("paper_trading", "execute", "positions", "guardian",
                    "auto_simulator", "dex_autotrade"):
            self.assertEqual(JC.capability_of(job), JC.ECONOMIC, job)

    def test_the_paid_feeds_are_collection(self):
        """The whole point: these must run while trading is off."""
        from lib import job_capability as JC
        for job in ("market", "threats", "insider", "inst13f", "congress",
                    "crypto_derivatives", "official_data", "onchain",
                    "wallet_discovery", "wallet_activity", "futures_curve"):
            self.assertEqual(JC.capability_of(job), JC.COLLECTION, job)
            self.assertIn(JC.capability_of(job),
                          JC.allowed_capabilities(economic=False), job)


class TheSchedulerHonoursTheSplitTests(unittest.TestCase):

    def _build(self, economic):
        from app import scheduler as S
        started = {}

        class FakeSched:
            def __init__(self, *a, **k):
                pass

            def add_job(self, *a, **k):
                started.setdefault("ids", []).append(k.get("id"))
                return None

        with patch.object(S, "BackgroundScheduler", FakeSched), \
             patch.object(S, "load_persisted_job_status", lambda: None):
            S.create_scheduler(economic=economic)
        return set(started.get("ids") or [])

    def test_economic_jobs_are_withheld_when_the_runtime_may_not_act(self):
        from lib import job_capability as JC
        ids = self._build(economic=False)
        for job in JC.describe()[JC.ECONOMIC]:
            self.assertNotIn(job, ids,
                             f"{job} would run in a non-economic runtime")

    def test_collection_still_runs_when_the_runtime_may_not_act(self):
        """The defect this whole file exists for."""
        ids = self._build(economic=False)
        for job in ("market", "threats", "crypto_derivatives", "insider",
                    "official_data", "wallet_activity"):
            self.assertIn(job, ids,
                          f"{job} was withheld — the paid feed stays idle")
        self.assertGreaterEqual(len(ids), 25,
                                f"only {len(ids)} jobs would collect")

    def test_everything_runs_when_the_runtime_may_act(self):
        ids = self._build(economic=True)
        self.assertIn("paper_trading", ids)
        self.assertIn("market", ids)

    def test_startup_picks_the_group_from_the_runtime_mode(self):
        src = (REPO / "main.py").read_text(encoding="utf-8")
        self.assertIn("create_scheduler(economic=economic)", src)
        self.assertIn("EVIDENCE_ONLY", src)


if __name__ == "__main__":
    unittest.main()
