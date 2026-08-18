"""Pin the confirmed graceful-shutdown defect.

Observed twice against the live service: `systemctl --user stop` returned
Result=timeout and systemd escalated to SIGKILL, because the market refresh
walked ~157 symbols x 6 timeframes with no cancellation check and an
uninterruptible `time.sleep` between every item.

Fully hermetic — no provider is contacted, no socket is opened.
"""
import threading
import time
import unittest
from unittest import mock

from jobs import evidence_collector as EC
from jobs import fetch_market_data as FMD


class CooperativeCancellationTests(unittest.TestCase):
    """The warm loop must stop between work items, not run to completion."""

    def _run_warm(self, symbols, cancel_event, fetch_side_effect=None):
        seen = []

        def fake_call(fn, timeout, sym, tf, *a, **kw):
            seen.append((sym, tf))
            if fetch_side_effect:
                fetch_side_effect(sym, tf)
            return FMD._SENTINEL          # "nothing usable", no storage path

        with mock.patch.object(FMD, "_call_with_timeout", fake_call), \
             mock.patch.object(FMD, "init_cache_db", create=True), \
             mock.patch("lib.ohlcv_cache.init_cache_db"), \
             mock.patch.dict("os.environ", {"OHLCV_WARM_TIMEFRAMES": "1H,4H"}):
            FMD._warm_ohlcv_cache(symbols, None, None, cancel_event=cancel_event)
        return seen

    def test_cancellation_stops_the_loop_before_the_next_symbol(self):
        ev = threading.Event()
        symbols = [f"SYM{i}" for i in range(50)]

        def after_three(sym, tf):
            if len(sym) and sym == "SYM2":
                ev.set()

        seen = self._run_warm(symbols, ev, after_three)
        touched = {s for s, _ in seen}
        self.assertLess(len(touched), len(symbols),
                        "cancellation did not stop the sweep")
        self.assertNotIn("SYM49", touched)

    def test_an_already_cancelled_event_does_no_work_at_all(self):
        ev = threading.Event()
        ev.set()
        seen = self._run_warm([f"S{i}" for i in range(20)], ev)
        self.assertEqual(seen, [])

    def test_the_rate_limit_pause_is_interruptible(self):
        """time.sleep() cannot be woken; Event.wait() can."""
        ev = threading.Event()
        symbols = [f"P{i}" for i in range(40)]

        with mock.patch.object(FMD, "RATE_LIMIT_DELAY", 5.0, create=True):
            threading.Timer(0.2, ev.set).start()
            t0 = time.monotonic()
            self._run_warm(symbols, ev)
            elapsed = time.monotonic() - t0

        # Uninterruptible sleeps would need minutes for 40 symbols.
        self.assertLess(elapsed, 10.0,
                        f"cancellation waited out the sleeps ({elapsed:.1f}s)")

    def test_no_cancel_event_preserves_existing_behaviour(self):
        """Every existing caller, the scheduler included, is unaffected."""
        with mock.patch.object(FMD, "RATE_LIMIT_DELAY", 0.0, create=True):
            seen = self._run_warm(["A", "B"], None)
        self.assertEqual({s for s, _ in seen}, {"A", "B"})

    def test_run_accepts_cancellation_and_defaults_to_none(self):
        import inspect
        sig = inspect.signature(FMD.run)
        self.assertIn("cancel_event", sig.parameters)
        self.assertIsNone(sig.parameters["cancel_event"].default)

    def test_provider_calls_remain_bounded_by_a_finite_timeout(self):
        """Cancellation cannot interrupt a blocking socket; timeouts must."""
        self.assertIsInstance(FMD.ALPACA_READ_TIMEOUT, (int, float))
        self.assertGreater(FMD.ALPACA_READ_TIMEOUT, 0)
        self.assertLess(FMD.ALPACA_READ_TIMEOUT, 60)


class CollectorShutdownContractTests(unittest.TestCase):
    def test_the_collector_passes_its_own_stop_event(self):
        """One cancellation authority, not a second that could disagree."""
        import inspect
        src = inspect.getsource(EC._refresh_market)
        self.assertIn("cancel_event=_stop", src)

    def test_worker_joins_are_bounded(self):
        self.assertGreater(EC.WORKER_JOIN_TIMEOUT_S, 0)
        # must land inside systemd TimeoutStopSec=30
        self.assertLess(EC.WORKER_JOIN_TIMEOUT_S, 30)

    def test_shutdown_joins_producers_before_tearing_down_runtimes(self):
        """Stopping the runtimes under a live worker is the wrong order."""
        import inspect
        src = inspect.getsource(EC.main)
        join_at = src.index("t.join(timeout=WORKER_JOIN_TIMEOUT_S)")
        er_at = src.index("ER.stop()")
        mdr_at = src.index("MDR.stop()")
        self.assertLess(join_at, er_at, "runtimes torn down before workers exit")
        self.assertLess(er_at, mdr_at, "feed stopped before its consumer")

    def test_a_stuck_worker_is_reported_not_silently_called_clean(self):
        import inspect
        src = inspect.getsource(EC.main)
        self.assertIn("did not exit within", src)
        self.assertIn("DEGRADED", src)

    def test_shutdown_makes_no_economic_mutation(self):
        import ast
        import pathlib
        banned = {"open_paper_position", "prepare_entry", "close_paper_position",
                  "partial_close_paper_position", "settle_position_entry"}
        tree = ast.parse(pathlib.Path("jobs/evidence_collector.py").read_text())
        called = {n.func.attr for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        called |= {n.func.id for n in ast.walk(tree)
                   if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        self.assertEqual(called & banned, set())


if __name__ == "__main__":
    unittest.main()
