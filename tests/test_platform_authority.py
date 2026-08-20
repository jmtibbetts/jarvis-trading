"""PLATFORM MODE IS THE HARD AUTHORITY. Nothing else gets a vote.

test_platform_mode already proves the guard exists, fires before any broker
call, and lets the closing path through. What it does NOT prove is
PRECEDENCE: that when some other switch says "yes, trade", the platform
mode still wins.

That matters because the other switches are exactly the kind that go stale.
`live_trading_enabled` is a boolean in a SQLite row that predates this
architecture. A row can be edited by hand, restored from an old backup,
carried forward by a migration, or set by a UI that nobody remembered was
still wired. If any of those could re-enable real execution while the
platform is VIRTUAL_ONLY, then the boundary is decorative.

So each test here turns some OTHER authority to its most permissive setting
and asserts the order is still refused. The failure these prevent is not
"the guard is missing" -- it is "the guard is present and something
downstream overrode it".
"""
import os
import unittest
from unittest.mock import MagicMock, patch

from lib.platform_mode import (LIVE_ENABLED, VIRTUAL_ONLY,
                               LiveExecutionDisabled,
                               assert_may_increase_exposure)


class _Mode:
    """Set JARVIS_PLATFORM_MODE for the duration of a block."""

    def __init__(self, value):
        self.value = value
        self.old = None

    def __enter__(self):
        self.old = os.environ.get("JARVIS_PLATFORM_MODE")
        if self.value is None:
            os.environ.pop("JARVIS_PLATFORM_MODE", None)
        else:
            os.environ["JARVIS_PLATFORM_MODE"] = self.value
        return self

    def __exit__(self, *_):
        if self.old is None:
            os.environ.pop("JARVIS_PLATFORM_MODE", None)
        else:
            os.environ["JARVIS_PLATFORM_MODE"] = self.old


class NoOtherFlagCanOverridePlatformModeTests(unittest.TestCase):
    """Each case sets a DIFFERENT authority to permissive and still expects
    a refusal."""

    def _submit(self):
        """Call the real risk-increasing entry point with a broker client
        that would record any order it received."""
        from lib import alpaca_client as AC
        client = MagicMock()
        with patch.object(AC, "get_trading_client", return_value=client):
            try:
                AC.submit_bracket_order("AAPL", 1, 100.0, 90.0, 110.0)
            except LiveExecutionDisabled:
                return client, True
            except Exception:
                # Any other failure still must not have produced an order.
                return client, False
        return client, False

    def test_a_stale_live_trading_enabled_row_cannot_re_enable_real_orders(self):
        """THE HEADLINE. A legacy boolean in the database says trade; the
        platform says virtual. The platform wins."""
        from lib import kill_switch as KS
        with _Mode(VIRTUAL_ONLY):
            with patch.object(KS, "get_kill_switch_state",
                              return_value={"live_trading_enabled": 1,
                                            "paused_reason": None}):
                client, refused = self._submit()
        self.assertTrue(refused,
                        "VIRTUAL_ONLY did not refuse while a stale "
                        "live_trading_enabled=1 row was present")
        client.submit_order.assert_not_called()

    def test_an_enabled_broker_connector_cannot_re_enable_real_orders(self):
        """A configured, reachable, authenticated broker is not permission."""
        with _Mode(VIRTUAL_ONLY):
            with patch.dict(os.environ, {"ALPACA_API_KEY": "present",
                                         "ALPACA_API_SECRET": "present",
                                         "ALPACA_MODE": "live"}):
                client, refused = self._submit()
        self.assertTrue(refused, "credentials were treated as authorisation")
        client.submit_order.assert_not_called()

    def test_caller_arguments_cannot_opt_out_of_the_guard(self):
        """There must be no force/override parameter reachable from a
        caller. If one is added, this fails and asks why."""
        import inspect
        from lib import alpaca_client as AC
        params = set(inspect.signature(AC.submit_bracket_order).parameters)
        smells = {p for p in params
                  if any(w in p.lower() for w in
                         ("force", "override", "bypass", "skip_guard",
                          "allow_live", "ignore"))}
        self.assertEqual(smells, set(),
                         f"submit_bracket_order exposes an override "
                         f"parameter: {smells}")

    def test_the_guard_is_not_reachable_only_through_the_job(self):
        """A direct caller that skips jobs/execute_signals.py must still be
        refused -- the gate lives at the broker boundary, not in the job."""
        with _Mode(VIRTUAL_ONLY):
            client, refused = self._submit()
        self.assertTrue(refused,
                        "calling the adapter directly bypassed the platform "
                        "gate; the gate is in the job, not at the boundary")
        client.submit_order.assert_not_called()

    def test_the_scheduler_being_enabled_is_not_permission(self):
        with _Mode(VIRTUAL_ONLY):
            with patch.dict(os.environ, {"JARVIS_DISABLE_SCHEDULER": "0"}):
                client, refused = self._submit()
        self.assertTrue(refused)
        client.submit_order.assert_not_called()


class TheGuardStillPermitsWhatItShouldTests(unittest.TestCase):
    """A boundary that refuses everything is not a boundary, it is an
    outage. These pin that the permissive direction still works."""

    def test_an_explicitly_live_platform_permits_the_guard(self):
        with _Mode(LIVE_ENABLED):
            assert_may_increase_exposure("test")      # must not raise

    def test_virtual_only_refuses_with_a_named_exception(self):
        with _Mode(VIRTUAL_ONLY):
            with self.assertRaises(LiveExecutionDisabled):
                assert_may_increase_exposure("test")


class TheRefusalHappensBeforeAnyNetworkCallTests(unittest.TestCase):
    """Refusing after the order is on the wire is not refusing."""

    def test_no_trading_client_is_even_constructed(self):
        from lib import alpaca_client as AC
        with _Mode(VIRTUAL_ONLY):
            with patch.object(AC, "get_trading_client") as get_client:
                try:
                    AC.submit_bracket_order("AAPL", 1, 100.0, 90.0, 110.0)
                except LiveExecutionDisabled:
                    pass
        self.assertFalse(
            get_client.called,
            "a broker client was constructed before the platform guard ran")


if __name__ == "__main__":
    unittest.main()
