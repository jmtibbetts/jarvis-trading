"""VIRTUAL_ONLY is a boundary, not a preference.

JARVIS is currently a virtual training laboratory. Routing used to be
decided by a `paper_mode` boolean whose real meaning was "this direction is
not a plain long" — shorts, leverage and futures went to the simulator and
everything else went to a real broker. Under that architecture an ordinary
long equity signal reached Alpaca BY DEFAULT.

For a training platform that is backwards. The default must be that
nothing reaches a broker, and reaching one must be an explicit, audited
exception.

THE DELIBERATE ASYMMETRY, which is the part worth reading twice: opening
exposure is gated, closing it is not. If the platform is switched to
VIRTUAL_ONLY while real positions are open, refusing to close them would
trap real capital behind a training-mode flag — the guard would become the
cause of the loss it exists to prevent.
"""
import os
import unittest
from unittest.mock import patch

from lib.platform_mode import (LIVE_ENABLED, LIVE_LIMITED, LIVE_SHADOW,
                               MODES, VIRTUAL_ONLY, LiveExecutionDisabled,
                               assert_may_increase_exposure, current_mode,
                               is_virtual_only, live_execution_allowed,
                               status)


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


class DefaultsTests(unittest.TestCase):
    def test_the_default_is_virtual_only(self):
        with _Mode(None):
            self.assertEqual(current_mode(), VIRTUAL_ONLY)
            self.assertTrue(is_virtual_only())
            self.assertFalse(live_execution_allowed())

    def test_an_unrecognised_mode_fails_closed(self):
        """A typo in an environment variable must never enable live trading."""
        for bad in ("live", "LIVE", "yes", "true", "1", "ENABLED", "  "):
            with _Mode(bad):
                self.assertEqual(current_mode(), VIRTUAL_ONLY, bad)
                self.assertFalse(live_execution_allowed(), bad)

    def test_live_shadow_still_refuses_real_orders(self):
        with _Mode(LIVE_SHADOW):
            self.assertFalse(live_execution_allowed())

    def test_only_the_explicitly_live_modes_permit_submission(self):
        for m in MODES:
            with _Mode(m):
                self.assertEqual(live_execution_allowed(),
                                 m in (LIVE_LIMITED, LIVE_ENABLED), m)


class HardGuardTests(unittest.TestCase):
    """The prompt's test 30."""

    def test_the_guard_raises_rather_than_returning_false(self):
        """A caller that ignores a return value would submit anyway."""
        with _Mode(VIRTUAL_ONLY):
            with self.assertRaises(LiveExecutionDisabled):
                assert_may_increase_exposure("test order")

    def test_a_real_order_submission_is_refused_in_virtual_only(self):
        from lib.alpaca_client import submit_bracket_order
        with _Mode(VIRTUAL_ONLY):
            with self.assertRaises(LiveExecutionDisabled):
                submit_bracket_order("AAPL", 1, 100.0, 110.0, 95.0)

    def test_the_guard_fires_before_any_broker_call(self):
        """Proof it is a boundary and not a post-hoc check."""
        from lib import alpaca_client
        with _Mode(VIRTUAL_ONLY), \
             patch.object(alpaca_client, "get_trading_client") as gc:
            with self.assertRaises(LiveExecutionDisabled):
                alpaca_client.submit_bracket_order("AAPL", 1, 100.0, 110.0, 95.0)
            gc.assert_not_called()

    def test_the_refusal_names_the_mode_and_says_it_is_not_a_fault(self):
        with _Mode(VIRTUAL_ONLY):
            try:
                assert_may_increase_exposure("bracket order submission")
                self.fail("should have raised")
            except LiveExecutionDisabled as e:
                msg = str(e)
                self.assertIn(VIRTUAL_ONLY, msg)
                self.assertIn("not a fault", msg)

    def test_a_live_mode_lets_the_guard_pass(self):
        with _Mode(LIVE_ENABLED):
            assert_may_increase_exposure("test order")   # must not raise


class RiskReducingAsymmetryTests(unittest.TestCase):
    """Closing is never blocked — a mode flag must not strand real capital."""

    def test_reducing_risk_is_permitted_in_virtual_only(self):
        from lib.platform_mode import note_risk_reducing_action
        with _Mode(VIRTUAL_ONLY):
            note_risk_reducing_action("close_position", "AAPL")  # no raise

    def test_only_the_opening_path_carries_the_guard(self):
        """close/partial_close/cancel must NOT be gated, or a VIRTUAL_ONLY
        switch would trap an open real position."""
        import inspect

        from lib import alpaca_client
        for fn in ("close_position", "partial_close_position",
                   "cancel_open_orders_for_symbol"):
            src = inspect.getsource(getattr(alpaca_client, fn))
            self.assertNotIn("assert_may_increase_exposure", src, fn)
            self.assertNotIn("assert_live_execution_allowed", src, fn)

    def test_the_opening_path_does_carry_it(self):
        import inspect

        from lib import alpaca_client
        src = inspect.getsource(alpaca_client.submit_bracket_order)
        self.assertIn("assert_may_increase_exposure", src)


class StatusTests(unittest.TestCase):
    def test_disabled_execution_is_a_state_not_an_error(self):
        """The UI must present this as configuration, or an operator will
        try to repair it."""
        with _Mode(VIRTUAL_ONLY):
            s = status()
            self.assertTrue(s["virtual_only"])
            self.assertFalse(s["live_execution_allowed"])
            self.assertIn("TRAINING MODE", s["detail"])

    def test_data_access_is_never_blocked_by_the_mode(self):
        """Reading a broker is not trading."""
        with _Mode(VIRTUAL_ONLY):
            s = status()
            self.assertTrue(s["market_data_allowed"])
            self.assertTrue(s["account_data_allowed"])


if __name__ == "__main__":
    unittest.main()
