"""Every scheduled job must be runnable — registration is not enough.

The 'candidates' job was registered with add_job but had no row in the
job_status seed dict, so make_job_runner's first line raised KeyError on
every firing. From the outside this looked like "not yet fired" — the
job silently never ran across two server processes, and only reading the
APScheduler traceback in the logs revealed it. Two lessons pinned here:
the runner must self-seed, and every name passed to make_job_runner in
the scheduler source must resolve to a seeded status row.
"""
import re
import unittest
from pathlib import Path

from app.scheduler import job_status, make_job_runner

SCHEDULER_SRC = Path("app/scheduler.py").read_text(encoding="utf-8")


class RunnerSelfSeedsTests(unittest.TestCase):
    def test_an_unseeded_name_is_seeded_not_crashed(self):
        name = "never_seeded_test_job"
        job_status.pop(name, None)
        ran = []
        runner = make_job_runner(name, lambda: ran.append(True))
        runner()
        self.assertEqual(ran, [True])
        self.assertEqual(job_status[name]["status"], "ok")
        job_status.pop(name, None)

    def test_a_failing_job_records_its_error(self):
        name = "failing_test_job"
        job_status.pop(name, None)

        def boom():
            raise RuntimeError("deliberate")

        make_job_runner(name, boom)()
        self.assertEqual(job_status[name]["status"], "error")
        self.assertIn("deliberate", job_status[name]["error"])
        job_status.pop(name, None)


class EveryRegisteredJobIsSeededTests(unittest.TestCase):
    def test_every_make_job_runner_name_has_a_status_row(self):
        """Static check over the scheduler source: any literal name handed
        to make_job_runner must exist in the seed dict, so the UI shows it
        as idle BEFORE its first run rather than omitting it entirely."""
        names = set(re.findall(r"make_job_runner\(\s*['\"]([\w]+)['\"]", SCHEDULER_SRC))
        self.assertGreater(len(names), 15, "regex found implausibly few jobs")
        missing = names - set(job_status)
        self.assertEqual(missing, set(),
                         f"registered but not seeded in job_status: {missing}")


if __name__ == "__main__":
    unittest.main()
