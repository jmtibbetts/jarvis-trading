"""Autonomous execution is VIRTUAL. The broker connector is opt-in.

Three scheduled jobs predated the VIRTUAL_ONLY architecture and still
described the desk as live-first:

    execute    submitted orders to Alpaca, every 30 minutes
    positions  managed Alpaca positions, every 5 minutes
    guardian   read the Alpaca ACCOUNT and called itself THE portfolio
               guardian, every 5 minutes

and `event_driven_signals` fired `jobs.execute_signals` fifteen seconds
after generating a thesis — so an event-driven thesis went straight at an
external broker path on a platform whose stated mode forbids live entry.

An external account may be DATA, REFERENCE or GROUND TRUTH. It must not
silently define current training positions, current training risk or
current autonomous execution. The code is retained for the day live
execution returns; only its automatic scheduling is withdrawn.
"""
import ast
import pathlib
import unittest

SCHED = pathlib.Path("app/scheduler.py")


def _source() -> str:
    return SCHED.read_text(encoding="utf-8")


class TheConnectorIsOffByDefault(unittest.TestCase):
    def test_the_flag_defaults_to_disabled(self):
        import app.scheduler as sched
        self.assertFalse(sched.external_connector_enabled())

    def test_enabling_requires_an_explicit_opt_in(self):
        import os
        import app.scheduler as sched
        prior = os.environ.get("JARVIS_ENABLE_EXTERNAL_BROKER_CONNECTOR")
        try:
            for truthy in ("1", "true", "YES"):
                os.environ["JARVIS_ENABLE_EXTERNAL_BROKER_CONNECTOR"] = truthy
                self.assertTrue(sched.external_connector_enabled(), truthy)
            for falsy in ("", "0", "no", "off"):
                os.environ["JARVIS_ENABLE_EXTERNAL_BROKER_CONNECTOR"] = falsy
                self.assertFalse(sched.external_connector_enabled(), falsy)
        finally:
            os.environ.pop("JARVIS_ENABLE_EXTERNAL_BROKER_CONNECTOR", None)
            if prior is not None:
                os.environ["JARVIS_ENABLE_EXTERNAL_BROKER_CONNECTOR"] = prior


class TheAlpacaJobsAreNotScheduledByDefault(unittest.TestCase):
    """Asserted structurally: every add_job for a broker job must sit
    inside the connector guard."""

    def setUp(self):
        self.tree = ast.parse(_source())

    def _guarded_job_ids(self) -> set[str]:
        """Job ids whose add_job call is inside an `if external_connector_
        enabled()` block."""
        guarded: set[str] = set()
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.If):
                continue
            if "external_connector_enabled" not in ast.dump(node.test):
                continue
            for call in ast.walk(node):
                if (isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Attribute)
                        and call.func.attr == "add_job"):
                    for kw in call.keywords:
                        if kw.arg == "id" and isinstance(kw.value, ast.Constant):
                            guarded.add(kw.value.value)
        return guarded

    def test_execute_guardian_and_positions_are_all_behind_the_flag(self):
        guarded = self._guarded_job_ids()
        for job in ("execute", "guardian", "positions"):
            self.assertIn(job, guarded,
                          f"'{job}' is scheduled unconditionally — it submits "
                          f"to or reads an external broker and is not the "
                          f"desk's virtual training path")

    def test_the_virtual_books_are_still_scheduled_unconditionally(self):
        """Quarantining the broker path must not have taken the actual
        autonomous execution with it."""
        guarded = self._guarded_job_ids()
        src = _source()
        for job in ("paper_trading", "autosim", "dex_autotrade"):
            self.assertIn(job, src)
            self.assertNotIn(job, guarded)


class EventDrivenThesesRouteToVirtualExecution(unittest.TestCase):
    def test_the_event_path_no_longer_fires_the_alpaca_executor(self):
        src = ast.parse(_source())
        fn = next(n for n in ast.walk(src)
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "event_driven_signals")
        body = ast.dump(fn)
        self.assertNotIn("execute_signals", body,
                         "an event-driven thesis must not be routed at the "
                         "external broker submitter")

    def test_the_event_path_fires_the_virtual_cex(self):
        src = ast.parse(_source())
        fn = next(n for n in ast.walk(src)
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "event_driven_signals")
        self.assertIn("paper_trading", ast.dump(fn))


class TheGuardianSaysWhoseRiskItGuards(unittest.TestCase):
    def test_the_external_guardian_is_named_for_what_it_reads(self):
        """`portfolio_guardian` reading the Alpaca account presented an
        external reference book as the desk's own risk view."""
        import app.scheduler as sched
        self.assertTrue(hasattr(sched, "external_alpaca_guardian"))
        self.assertFalse(hasattr(sched, "portfolio_guardian"))

    def test_the_manual_trigger_map_points_at_the_renamed_function(self):
        """A rename that left the job map behind would 404 the Ops button."""
        import importlib
        from app.routers.platform import trigger_job  # noqa: F401
        src = pathlib.Path("app/routers/platform.py").read_text(encoding="utf-8")
        self.assertIn("external_alpaca_guardian", src)
        self.assertNotIn('"portfolio_guardian"', src)
        mod = importlib.import_module("app.scheduler")
        self.assertTrue(callable(getattr(mod, "external_alpaca_guardian")))


if __name__ == "__main__":
    unittest.main()
