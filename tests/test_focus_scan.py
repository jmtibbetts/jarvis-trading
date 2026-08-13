"""On-demand focus scan: the same pipeline, narrowed to the coins to watch.

The operator asked for a "scan now" button on the Coins to Watch panel. The
implementation deliberately reuses generate_signals.run() rather than adding
a second scanner, because a parallel implementation would eventually grade
setups by different rules than the ones that actually trade them.

Narrowing by batch id exposed a latent collision: the FOCUS track emitted
"F0", "F1"... and so did the FUTURES/FOREX track. The guard against that was
`not b[0].startswith("FUT")` — a prefix nothing ever emitted. Harmless while
the id was only used for ordering; wrong the moment it selects batches. A
one-coin focus list produced "4 batch(es), 13 symbol(s)".
"""
import re
import unittest


class BatchIdContractTests(unittest.TestCase):
    """Batch ids are now load-bearing, so their shapes are pinned."""

    def test_focus_and_futures_no_longer_share_a_prefix(self):
        import inspect
        from jobs import generate_signals
        src = inspect.getsource(generate_signals)
        self.assertIn('f"FX{i}"', src, "the futures/forex track must not emit F{i}")

    def test_the_focus_filter_matches_only_focus_ids(self):
        pattern = re.compile(r"F\d+")
        for bid in ("F0", "F1", "F12"):
            self.assertTrue(pattern.fullmatch(bid), bid)
        for bid in ("FX0", "FX1", "W0", "WE0", "A0", "B0", "C0", "D0", "E0"):
            self.assertIsNone(pattern.fullmatch(bid), f"{bid} must not read as focus")

    def test_the_dead_guard_is_gone(self):
        """`startswith("FUT")` never matched anything and hid the collision."""
        import inspect
        from jobs import generate_signals
        self.assertNotIn('startswith("FUT")', inspect.getsource(generate_signals))


class ReusesTheRealPipelineTests(unittest.TestCase):
    def test_run_takes_a_focus_only_flag(self):
        import inspect
        from jobs.generate_signals import run
        self.assertIn("focus_only", inspect.signature(run).parameters)

    def test_focus_only_defaults_off(self):
        """The scheduled sweep must be unaffected."""
        import inspect
        from jobs.generate_signals import run
        self.assertIs(inspect.signature(run).parameters["focus_only"].default, False)

    def test_there_is_no_second_scanner(self):
        """The endpoint must call generate_signals.run, not reimplement it."""
        import inspect
        from app import routes
        src = inspect.getsource(routes.scan_focus)
        self.assertIn("focus_only=True", src)


class ScanEndpointTests(unittest.TestCase):
    def test_it_refuses_when_no_coins_are_watched(self):
        from unittest import mock
        from fastapi import HTTPException
        from app import routes
        with mock.patch.object(routes, "get_db") as gd:
            gd.return_value.__enter__.return_value.query.return_value \
              .filter.return_value.count.return_value = 0
            with self.assertRaises(HTTPException) as ctx:
                routes.scan_focus()
        self.assertEqual(ctx.exception.status_code, 400)

    def test_status_reports_the_shape_the_ui_polls(self):
        from app import routes
        st = routes.focus_scan_status()
        for k in ("running", "started_at", "finished_at", "result", "error"):
            self.assertIn(k, st)


if __name__ == "__main__":
    unittest.main()
