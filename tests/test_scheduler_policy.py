"""What a job may DO is a different question from which stage it belongs to.

The capability axis (COLLECTION/ANALYSIS/ECONOMIC) says when a job runs. It
says nothing about its blast radius, and an audit conflated the two:

  * `telegram` sat under ANALYSIS and was read as read-only. It messages a
    human. An operator woken at 3am by a test run has been affected by it.
  * `guardian` and `positions` were reported as virtual-state-only. Both
    mutate a REAL broker account -- verified over the call graph, not the
    names: guardian closes positions and cancels orders, positions writes
    stop-loss, take-profit and trailing-stop exits.
  * `execute` opens real exposure, which is categorically different from
    those two even though all three reach the same broker.

THE ASYMMETRY WORTH READING TWICE: risk-REDUCING real actions stay
permitted under VIRTUAL_ONLY. Refusing to close a real position would trap
real capital behind a training-mode flag, making the guard the cause of the
loss it exists to prevent. Only INCREASING exposure is gated on the
platform.
"""
import unittest

from lib import job_capability as JC


class EveryJobIsClassifiedTests(unittest.TestCase):

    def test_no_job_is_unknown(self):
        unknown = [j for j in JC.REASONS
                   if JC.side_effect_class(j) == JC.UNKNOWN]
        self.assertEqual(unknown, [], f"unclassified jobs: {unknown}")

    def test_an_unregistered_job_is_unknown_not_assumed_safe(self):
        self.assertEqual(JC.side_effect_class("some_new_job_nobody_classified"),
                         JC.UNKNOWN)

    def test_unknown_is_blocked_in_every_mode(self):
        """UNKNOWN must never be treated as harmless."""
        for runtime in ("FULL_VIRTUAL", "EVIDENCE_ONLY"):
            for platform in ("VIRTUAL_ONLY", "LIVE_ENABLED"):
                d = JC.policy_for("never_registered", runtime_mode=runtime,
                                  platform_mode=platform)
                self.assertFalse(d["allowed"],
                                 f"UNKNOWN ran under {runtime}/{platform}")
                self.assertIn("UNKNOWN", d["blocked_reason"])


class TheCorrectedClassificationsTests(unittest.TestCase):
    """Each of these was previously reported wrongly."""

    def test_notifications_are_not_read_only(self):
        for job in ("telegram", "brief_push"):
            self.assertEqual(JC.side_effect_class(job),
                             JC.EXTERNAL_NOTIFICATION, job)

    def test_guardian_and_positions_touch_the_real_account(self):
        for job in ("guardian", "positions"):
            self.assertEqual(JC.side_effect_class(job),
                             JC.REAL_ACCOUNT_RISK_REDUCING, job)

    def test_execute_increases_real_exposure(self):
        self.assertEqual(JC.side_effect_class("execute"),
                         JC.REAL_ACCOUNT_RISK_INCREASING)

    def test_the_paper_economies_stay_virtual(self):
        for job in ("paper_trading", "auto_simulator", "dex_autotrade"):
            self.assertEqual(JC.side_effect_class(job),
                             JC.VIRTUAL_STATE_MUTATING, job)


class VirtualOnlyBlocksRealRiskIncreasingWorkTests(unittest.TestCase):

    def test_execute_is_blocked_under_virtual_only(self):
        d = JC.policy_for("execute", runtime_mode="FULL_VIRTUAL",
                          platform_mode="VIRTUAL_ONLY")
        self.assertFalse(d["allowed"])
        self.assertIn("VIRTUAL_ONLY", d["blocked_reason"])

    def test_execute_is_blocked_in_every_non_live_platform(self):
        for platform in ("VIRTUAL_ONLY", "LIVE_SHADOW"):
            d = JC.policy_for("execute", runtime_mode="FULL_VIRTUAL",
                              platform_mode=platform)
            self.assertFalse(d["allowed"], platform)

    def test_execute_is_permitted_only_on_an_explicitly_live_platform(self):
        for platform in ("LIVE_LIMITED", "LIVE_ENABLED"):
            d = JC.policy_for("execute", runtime_mode="FULL_VIRTUAL",
                              platform_mode=platform)
            self.assertTrue(d["allowed"], platform)

    def test_the_platform_is_not_what_blocks_risk_reduction(self):
        """The asymmetry, stated at the right level.

        Trapping real capital behind a training flag would make the guard
        the cause of the loss, so PLATFORM MODE never blocks reduction.
        What gates these jobs is a separate capability -- whether the
        operator has activated external account management at all -- and
        the refusal must say so rather than blaming the platform.
        """
        import os
        for job in ("guardian", "positions"):
            d = JC.policy_for(job, runtime_mode="FULL_VIRTUAL",
                              platform_mode="VIRTUAL_ONLY")
            if not d["allowed"]:
                self.assertNotIn("platform", (d["blocked_reason"] or "").lower(),
                                 f"{job} was blocked by the PLATFORM, which "
                                 f"would re-create the trapped-capital trap")
                self.assertIn("management", d["blocked_reason"])

    def test_activation_of_account_management_is_what_permits_them(self):
        """With the capability explicitly on, the same jobs are permitted
        under exactly the same VIRTUAL_ONLY platform."""
        import os
        old = {k: os.environ.get(k) for k in
               ("JARVIS_ENABLE_EXTERNAL_BROKER_CONNECTOR",
                "JARVIS_ENABLE_EXTERNAL_ACCOUNT_MANAGEMENT")}
        try:
            for k in old:
                os.environ[k] = "1"
            for job in ("guardian", "positions"):
                d = JC.policy_for(job, runtime_mode="FULL_VIRTUAL",
                                  platform_mode="VIRTUAL_ONLY")
                self.assertTrue(d["allowed"],
                                f"{job} stayed blocked with the capability on")
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


class NotificationsRequireTheirOwnSwitchTests(unittest.TestCase):

    def test_a_mode_change_alone_does_not_start_messaging_people(self):
        for job in ("telegram", "brief_push"):
            d = JC.policy_for(job, runtime_mode="FULL_VIRTUAL",
                              platform_mode="VIRTUAL_ONLY",
                              notifications_enabled=False)
            self.assertFalse(d["allowed"], job)

    def test_explicitly_enabling_delivery_permits_them(self):
        for job in ("telegram", "brief_push"):
            d = JC.policy_for(job, runtime_mode="FULL_VIRTUAL",
                              platform_mode="VIRTUAL_ONLY",
                              notifications_enabled=True)
            self.assertTrue(d["allowed"], job)


class TheTwoRuntimeModesAreNotTheSameTests(unittest.TestCase):
    """If both permitted identical job sets the mode would be decorative."""

    def test_evidence_only_does_not_move_the_virtual_book(self):
        for job in ("paper_trading", "auto_simulator", "dex_autotrade"):
            d = JC.policy_for(job, runtime_mode="EVIDENCE_ONLY",
                              platform_mode="VIRTUAL_ONLY")
            self.assertFalse(d["allowed"],
                             f"{job} mutated virtual state in EVIDENCE_ONLY")

    def test_full_virtual_permits_strictly_more_than_evidence_only(self):
        full = {r["job"] for r in JC.policy_matrix(
            runtime_mode="FULL_VIRTUAL", platform_mode="VIRTUAL_ONLY")
            if r["allowed"]}
        eco = {r["job"] for r in JC.policy_matrix(
            runtime_mode="EVIDENCE_ONLY", platform_mode="VIRTUAL_ONLY")
            if r["allowed"]}
        self.assertTrue(eco < full,
                        "the two runtime modes permit the same job set")

    def test_an_unrecognised_runtime_mode_permits_nothing(self):
        d = JC.policy_for("market", runtime_mode="NONSENSE",
                          platform_mode="VIRTUAL_ONLY")
        self.assertFalse(d["allowed"])


class TheMatrixIsRenderableTests(unittest.TestCase):

    def test_every_job_appears_with_a_reason_when_blocked(self):
        rows = JC.policy_matrix(runtime_mode="FULL_VIRTUAL",
                                platform_mode="VIRTUAL_ONLY")
        self.assertEqual(len(rows), len(JC.REASONS))
        for r in rows:
            self.assertIn("side_effect_class", r)
            if not r["allowed"]:
                self.assertTrue(r["blocked_reason"],
                                f"{r['job']} blocked without a reason")


if __name__ == "__main__":
    unittest.main()
