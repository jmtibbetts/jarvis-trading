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


class PerSymbolTests(unittest.TestCase):
    """One coin at a time.

    Asking "is BEAT ready?" should not spend an LLM call on every other
    watched coin, and two coins must be answerable at once — so both the
    pipeline and the scan state are per symbol.
    """

    def test_run_can_be_narrowed_to_specific_symbols(self):
        import inspect
        from jobs.generate_signals import run
        self.assertIn("only_symbols", inspect.signature(run).parameters)

    def test_only_symbols_defaults_to_everything(self):
        import inspect
        from jobs.generate_signals import run
        self.assertIsNone(inspect.signature(run).parameters["only_symbols"].default)

    def test_scan_state_is_keyed_by_symbol(self):
        from app import routes
        self.assertIsInstance(routes._FOCUS_SCANS, dict)

    def test_status_for_an_unscanned_symbol_is_idle_not_an_error(self):
        from app import routes
        st = routes.focus_scan_status("NEVER/SCANNED")
        self.assertFalse(st["running"])
        self.assertIsNone(st["result"])

    def test_the_endpoint_scans_only_the_requested_symbol(self):
        import inspect
        from app import routes
        src = inspect.getsource(routes.scan_focus_symbol)
        self.assertIn("only_symbols=[sym]", src)

    def test_an_unwatched_symbol_is_refused(self):
        from unittest import mock
        from fastapi import HTTPException
        from app import routes
        with mock.patch.object(routes, "get_db") as gd:
            gd.return_value.__enter__.return_value.query.return_value               .filter.return_value.first.return_value = None
            with self.assertRaises(HTTPException) as ctx:
                routes.scan_focus_symbol("NOTWATCHED/USD")
        self.assertEqual(ctx.exception.status_code, 404)


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
        src = inspect.getsource(routes.scan_focus_symbol)
        self.assertIn("focus_only=True", src)


class ScanEndpointTests(unittest.TestCase):
    def test_status_reports_the_shape_the_ui_polls(self):
        from app import routes
        st = routes.focus_scan_status("BEAT/USD")
        for k in ("symbol", "running", "started_at", "finished_at", "result", "error"):
            self.assertIn(k, st)

    def test_a_symbol_with_a_slash_survives_the_route(self):
        """Crypto symbols contain "/", so the path parameter must accept it
        or every coin on this list would 404."""
        import inspect
        from app import routes
        for fn in (routes.scan_focus_symbol, routes.focus_scan_status):
            self.assertIn("symbol", inspect.signature(fn).parameters)
        st = routes.focus_scan_status("BEAT/USD")
        self.assertEqual(st["symbol"], "BEAT/USD")


if __name__ == "__main__":
    unittest.main()
