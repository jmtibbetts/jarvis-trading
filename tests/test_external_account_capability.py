"""Being ALLOWED to reduce risk is not the same as being switched on to
manage a brokerage account.

platform_mode lets risk REDUCTION through in every mode, deliberately: a
desk switched to VIRTUAL_ONLY with real positions still open must be able
to close them, or the guard becomes the cause of the loss it prevents.

That was being read as something stronger. "Closing is permitted" is a
statement about the ACTION. It is not authorisation for a scheduled job to
start reaching into an account on its own initiative the moment normal
FULL_VIRTUAL operation begins. An operator running the virtual desk with
credentials still in .env and stale positions at the broker has not asked
JARVIS to manage that account.

So there are two authorities and these tests keep them apart. The asymmetry
is PRESERVED, not reversed: risk reduction still never depends on
permission to open new risk.
"""
import os
import unittest
from unittest.mock import MagicMock, patch

from lib.external_account import (INCREASES, REDUCES, UNKNOWN,
                                  ExternalAccountManagementDisabled,
                                  assert_may_manage_external_account,
                                  connector_enabled, management_enabled,
                                  status)


class _Caps:
    """Set the two capability switches for a block."""

    def __init__(self, connector=None, management=None):
        self.want = {"JARVIS_ENABLE_EXTERNAL_BROKER_CONNECTOR": connector,
                     "JARVIS_ENABLE_EXTERNAL_ACCOUNT_MANAGEMENT": management}
        self.old = {}

    def __enter__(self):
        for k, v in self.want.items():
            self.old[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = "1" if v else "0"
        return self

    def __exit__(self, *_):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _close(symbol="AAPL"):
    """Drive the REAL risk-reducing entry point with a fake broker."""
    from lib import alpaca_client as AC
    client = MagicMock()
    with patch.object(AC, "get_trading_client", return_value=client):
        try:
            AC.close_position(symbol)
            return client, None
        except ExternalAccountManagementDisabled as e:
            return client, e
        except Exception as e:                                # noqa: BLE001
            return client, e


class TheDefaultIsObserveOnlyTests(unittest.TestCase):

    def test_both_switches_default_off(self):
        with _Caps(None, None):
            self.assertFalse(connector_enabled())
            self.assertFalse(management_enabled())

    def test_status_says_observe_only_by_default(self):
        with _Caps(None, None):
            self.assertIn("observe only", status()["effective"])


class CaseA_NothingEnabledTests(unittest.TestCase):
    """VIRTUAL_ONLY, connector OFF, management OFF -> no broker mutation."""

    def test_no_broker_mutation(self):
        with _Caps(False, False):
            client, err = _close()
        self.assertIsInstance(err, ExternalAccountManagementDisabled)
        client.close_position.assert_not_called()


class CaseB_ConnectorOnlyTests(unittest.TestCase):
    """connector ON, management OFF -> still no broker mutation.

    This is the case the old design got wrong: a connector switched on for
    READING was enough to make a risk-reducing mutation look authorised.
    """

    def test_a_connector_alone_is_not_authorisation(self):
        with _Caps(True, False):
            client, err = _close()
        self.assertIsInstance(err, ExternalAccountManagementDisabled)
        self.assertIn("management is not enabled", str(err))
        client.close_position.assert_not_called()

    def test_management_alone_is_also_not_enough(self):
        with _Caps(False, True):
            client, err = _close()
        self.assertIsInstance(err, ExternalAccountManagementDisabled)
        client.close_position.assert_not_called()


class CaseC_BothEnabledReduceOnlyTests(unittest.TestCase):
    """connector ON, management ON, proven reduce-only -> permitted."""

    def test_the_policy_permits_the_action(self):
        with _Caps(True, True):
            assert_may_manage_external_account(
                "close_position", exposure_effect=REDUCES, identity="AAPL")

    def test_the_real_path_reaches_the_broker(self):
        with _Caps(True, True):
            client, err = _close()
        self.assertIsNone(err, "unexpected refusal: %s" % err)
        client.close_position.assert_called_once()

    def test_the_scheduler_policy_agrees(self):
        from lib import job_capability as JC
        with _Caps(True, True):
            for job in ("guardian", "positions"):
                d = JC.policy_for(job, runtime_mode="FULL_VIRTUAL",
                                  platform_mode="VIRTUAL_ONLY")
                self.assertTrue(d["allowed"], job)
        with _Caps(True, False):
            for job in ("guardian", "positions"):
                d = JC.policy_for(job, runtime_mode="FULL_VIRTUAL",
                                  platform_mode="VIRTUAL_ONLY")
                self.assertFalse(d["allowed"], job)
                self.assertIn("not enabled", d["blocked_reason"])


class CaseD_ExposureIncreasingIsAlwaysRejectedTests(unittest.TestCase):
    """Even fully enabled, this path never opens exposure."""

    def test_rejected_in_every_capability_state(self):
        for conn, mgmt in ((False, False), (True, False), (True, True)):
            with self.subTest(connector=conn, management=mgmt):
                with _Caps(conn, mgmt):
                    with self.assertRaises(ExternalAccountManagementDisabled):
                        assert_may_manage_external_account(
                            "open", exposure_effect=INCREASES, identity="AAPL")

    def test_the_refusal_names_the_reason(self):
        with _Caps(True, True):
            with self.assertRaises(ExternalAccountManagementDisabled) as cm:
                assert_may_manage_external_account(
                    "open", exposure_effect=INCREASES, identity="AAPL")
        self.assertIn("INCREASE", str(cm.exception))


class CaseE_UnknownIsRejectedTests(unittest.TestCase):
    """An action nobody can prove reduce-only is not a risk reduction."""

    def test_unknown_exposure_effect_is_refused_even_fully_enabled(self):
        with _Caps(True, True):
            with self.assertRaises(ExternalAccountManagementDisabled) as cm:
                assert_may_manage_external_account(
                    "mystery", exposure_effect=UNKNOWN, identity="AAPL")
        self.assertIn("PROVEN", str(cm.exception))

    def test_an_unnamed_target_is_refused(self):
        """A mutation with no identity cannot be shown to be reduce-only."""
        with _Caps(True, True):
            with self.assertRaises(ExternalAccountManagementDisabled):
                assert_may_manage_external_account(
                    "close_everything", exposure_effect=REDUCES, identity=None)


class TheAsymmetryIsPreservedTests(unittest.TestCase):
    """Risk reduction must never require permission to open new risk."""

    def test_reduction_does_not_consult_platform_live_permission(self):
        import inspect
        from lib import external_account as EA
        src = inspect.getsource(EA.assert_may_manage_external_account)
        for forbidden in ("assert_may_increase_exposure",
                          "assert_live_execution_allowed",
                          "live_execution_allowed"):
            self.assertNotIn(
                forbidden, src,
                "risk reduction was made to depend on permission to open "
                "risk, which re-creates the trap the asymmetry prevents")

    def test_reduction_is_permitted_while_the_platform_is_virtual_only(self):
        from lib.platform_mode import VIRTUAL_ONLY
        old = os.environ.get("JARVIS_PLATFORM_MODE")
        os.environ["JARVIS_PLATFORM_MODE"] = VIRTUAL_ONLY
        try:
            with _Caps(True, True):
                assert_may_manage_external_account(
                    "close_position", exposure_effect=REDUCES, identity="AAPL")
        finally:
            if old is None:
                os.environ.pop("JARVIS_PLATFORM_MODE", None)
            else:
                os.environ["JARVIS_PLATFORM_MODE"] = old


if __name__ == "__main__":
    unittest.main()
