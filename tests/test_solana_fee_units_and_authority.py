"""Phase 6.1 — units are part of the type, and provenance is not authority.

THREE DEFECTS, THREE SECTIONS.

  1. A 1000x REPORTING ERROR. The live unsafeMax quote of 160,361,842,105
     micro-lamports/CU was written up as "~64,000 SOL on a 400k-CU
     transaction". It is ~64.14 SOL. The precise unit failure: the
     lamports->SOL step used the MICRO-LAMPORTS-PER-LAMPORT divisor (1e6) a
     second time instead of the LAMPORTS-PER-SOL divisor (1e9), and
     1e9/1e6 = 1000. Note that the intuitive guess is wrong — reading the
     lamport total as SOL outright, with no division at all, would be a 1e9x
     error giving 6.4e10 SOL. The mistake was reusing a right-looking
     constant in the wrong step. It lived only in a comment, but a comment
     is where the next reader gets their idea of the magnitude, and the
     tests below make both the magnitude and the conversion impossible to
     get wrong silently.

  2. A CEILING NOBODY ENFORCED. The policy table carried a
     `max_total_fee_lamports` per action that had no readers at all, so the
     operator's actual question — the most a transaction may COST — was
     unbounded while a priority-only bound looked like an answer.

  3. AN ENUM MISTAKEN FOR PERMISSION. fund_wallet funded on
     `authority in FUNDING_AUTHORITIES`, so any caller could mint virtual
     capital by typing TEST_FIXTURE.

The dimensional tests are deliberately written against an INDEPENDENT
oracle (Fraction arithmetic) rather than against the implementation's own
expression, so they can fail when the implementation is wrong instead of
agreeing with it.
"""
import math
import os
import unittest
from fractions import Fraction

from lib import dex_wallet as DW
from lib import solana_fee_policy as POLICY
from lib import solana_fees as SF

# The Phase 6 live observation, kept as a fixture so the arithmetic that
# was reported wrongly is now asserted rather than described.
UNSAFE_MAX_MICRO_LAMPORTS_PER_CU = 160_361_842_105
UNSAFE_MAX_PRIORITY_LAMPORTS = 64_144_736_842
UNSAFE_MAX_PRIORITY_SOL = 64.144736842


def _ceil_oracle(micro_per_cu, cu) -> int:
    """ceil(micro/CU * CU / 1e6), computed a different way on purpose."""
    return int(math.ceil(Fraction(str(micro_per_cu)) * Fraction(int(cu))
                         / Fraction(1_000_000)))


class MicroLamportsPerComputeUnitToLamportsTests(unittest.TestCase):
    """A. The conversion the whole module exists to get right."""

    def test_A_the_documented_formula_holds_across_seven_magnitudes(self):
        for micro_per_cu in (0, 1, 100, 5_000, 1_000_000, 20_000_000,
                             UNSAFE_MAX_MICRO_LAMPORTS_PER_CU):
            for cu in (1, 200_000, 400_000, 1_400_000):
                with self.subTest(micro_per_cu=micro_per_cu, cu=cu):
                    self.assertEqual(
                        SF.priority_fee_lamports(
                            compute_unit_price_micro_lamports=micro_per_cu,
                            compute_unit_limit=cu),
                        _ceil_oracle(micro_per_cu, cu))

    def test_A2_the_divisor_is_a_million_not_a_billion(self):
        """The 1000x family of errors, caught by construction.

        1e6 micro-lamports/CU over 1 CU is exactly 1 lamport. Divide by 1e9
        instead and the answer is 0; multiply instead and it is 1e12. Only
        the correct divisor gives 1."""
        self.assertEqual(SF.MICRO_LAMPORTS_PER_LAMPORT, 1_000_000)
        self.assertEqual(
            SF.priority_fee_lamports(
                compute_unit_price_micro_lamports=1_000_000,
                compute_unit_limit=1), 1)

    def test_A3_the_arithmetic_is_exact_past_the_float64_integer_limit(self):
        """float64 stops representing consecutive integers above ~9.007e15.

        The unsafeMax product is 6.4e16 micro-lamports, comfortably past it.
        A float implementation can land a few lamports off; Decimal cannot."""
        product = UNSAFE_MAX_MICRO_LAMPORTS_PER_CU * 400_000
        self.assertGreater(product, 2 ** 53,
                           "fixture no longer exercises the precision limit")
        self.assertEqual(
            SF.priority_fee_lamports(
                compute_unit_price_micro_lamports=(
                    UNSAFE_MAX_MICRO_LAMPORTS_PER_CU),
                compute_unit_limit=400_000),
            product // 1_000_000)

    def test_A4_rounding_is_up_never_truncation(self):
        """A fee rounded down is a fee that was never offered to the chain."""
        # 1 micro-lamport over 1 CU is one MILLIONTH of a lamport.
        self.assertEqual(
            SF.priority_fee_lamports(compute_unit_price_micro_lamports=1,
                                     compute_unit_limit=1), 1)
        # 1.5 lamports' worth must become 2, not 1.
        self.assertEqual(
            SF.priority_fee_lamports(compute_unit_price_micro_lamports=1_500,
                                     compute_unit_limit=1_000), 2)

    def test_A5_a_negative_input_is_refused_not_silently_signed(self):
        with self.assertRaises(ValueError):
            SF.priority_fee_lamports(
                compute_unit_price_micro_lamports=-1, compute_unit_limit=400_000)

    def test_A7_a_fractional_price_is_refused_not_quietly_quantized(self):
        """Quantizing in two places is how the two numbers disagreed."""
        with self.assertRaises(ValueError) as ctx:
            SF.priority_fee_lamports(
                compute_unit_price_micro_lamports=10_383.242,
                compute_unit_limit=400_000)
        self.assertIn("executable integer", str(ctx.exception))

    def test_A6_only_one_module_speaks_micro_lamports(self):
        """The conversion is centralised, so there is one place to be wrong.

        If a second module starts handling micro-lamports it must either
        import this conversion or be caught here."""
        import pathlib
        root = pathlib.Path(SF.__file__).resolve().parent.parent
        offenders = []
        for folder in ("lib", "app"):
            for path in (root / folder).rglob("*.py"):
                if path.name == "solana_fees.py":
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore").lower()
                if "micro_lamport" in text or "microlamport" in text:
                    offenders.append(str(path.relative_to(root)))
        self.assertEqual(offenders, [],
                         "micro-lamport arithmetic escaped lib/solana_fees.py")


class FractionalProviderEstimateTests(unittest.TestCase):
    """Helius returns a FLOAT; SetComputeUnitPrice takes a u64.

    The version of this module that quantized in the wrong place derived the
    fee from the raw float and then persisted int(raw) — a TRUNCATED price
    that was no longer the price the stored fee came from. Recomputing the
    fee from the record produced a different, lower number.
    """

    RAW = 10_383.242            # a live-shaped fractional Helius estimate
    EXECUTABLE = 10_384         # ceiling, conservatively

    def test_the_quantizer_rounds_up_to_the_executable_u64(self):
        self.assertEqual(
            SF.executable_compute_unit_price_micro_lamports(self.RAW),
            self.EXECUTABLE)
        # An already-integral estimate is left exactly alone.
        for exact in (0, 1, 5_000, UNSAFE_MAX_MICRO_LAMPORTS_PER_CU):
            with self.subTest(exact=exact):
                self.assertEqual(
                    SF.executable_compute_unit_price_micro_lamports(exact),
                    exact)

    def test_the_persisted_price_is_the_price_the_fee_came_from(self):
        """The defect, stated as an equality that must hold."""
        est = SF.estimate_network_fee(SF.NORMAL, fetch=_fetch(self.RAW),
                                      record_health=False)
        self.assertTrue(est.ok)
        self.assertEqual(est.compute_unit_price_micro_lamports,
                         self.EXECUTABLE)
        # RECOMPUTE from the persisted record and get the same fee back.
        recomputed = SF.priority_fee_lamports(
            compute_unit_price_micro_lamports=(
                est.compute_unit_price_micro_lamports),
            compute_unit_limit=est.compute_unit_limit)
        self.assertEqual(recomputed, est.priority_fee_lamports)

    def test_the_truncated_price_is_explicitly_not_what_is_stored(self):
        est = SF.estimate_network_fee(SF.NORMAL, fetch=_fetch(self.RAW),
                                      record_health=False)
        self.assertNotEqual(est.compute_unit_price_micro_lamports,
                            int(self.RAW))

    def test_truncation_can_only_ever_under_bid(self):
        """The invariant, across budgets — and where it actually bites.

        At the 400k default the outer ceiling happens to absorb a 1
        micro-lamport/CU truncation (4153.2 and 4153.6 both round to 4154),
        which is exactly why the defect survived: at the ONE budget anyone
        looked at, the two numbers agreed. At a larger compute budget they
        do not."""
        for cu in (200_000, 400_000, 1_400_000):
            with self.subTest(cu=cu):
                self.assertLessEqual(
                    SF.priority_fee_lamports(
                        compute_unit_price_micro_lamports=int(self.RAW),
                        compute_unit_limit=cu),
                    SF.priority_fee_lamports(
                        compute_unit_price_micro_lamports=self.EXECUTABLE,
                        compute_unit_limit=cu))
        self.assertEqual(
            SF.priority_fee_lamports(
                compute_unit_price_micro_lamports=int(self.RAW),
                compute_unit_limit=1_400_000), 14_537)
        self.assertEqual(
            SF.priority_fee_lamports(
                compute_unit_price_micro_lamports=self.EXECUTABLE,
                compute_unit_limit=1_400_000), 14_538)

    def test_the_providers_exact_value_survives_as_evidence(self):
        """Quantized for arithmetic, preserved for reconciliation."""
        est = SF.estimate_network_fee(SF.NORMAL, fetch=_fetch(self.RAW),
                                      record_health=False)
        self.assertEqual(est.provenance["micro_lamports_per_cu_raw"], self.RAW)
        self.assertEqual(
            est.provenance["executable_compute_unit_price_micro_lamports"],
            self.EXECUTABLE)
        self.assertEqual(est.provenance["price_quantization"],
                         "CEILING_TO_EXECUTABLE_U64")

    def test_a_fractional_fallback_estimate_is_quantized_too(self):
        """getRecentPrioritizationFees goes through the same boundary."""
        def fallback_only(method, params):
            if method == "getPriorityFeeEstimate":
                raise RuntimeError("primary down")
            return _rpc_rows(self.RAW, self.RAW, self.RAW)
        est = SF.estimate_network_fee(SF.NORMAL, fetch=fallback_only,
                                      record_health=False)
        self.assertEqual(est.quality, SF.MEASURED_RPC_FALLBACK)
        self.assertEqual(est.compute_unit_price_micro_lamports,
                         self.EXECUTABLE)


class LamportsToSolTests(unittest.TestCase):
    """B. The other half of the 1000x family."""

    def test_B_one_sol_is_a_billion_lamports(self):
        self.assertEqual(SF.LAMPORTS_PER_SOL, 1_000_000_000)
        self.assertEqual(SF.lamports_to_sol(1_000_000_000), 1.0)
        self.assertEqual(SF.lamports_to_sol(5_000), 0.000005)
        self.assertEqual(SF.sol_to_lamports(0.002), 2_000_000)
        self.assertEqual(SF.sol_to_lamports(0.0035), 3_500_000)

    def test_B2_the_round_trip_is_stable_at_policy_magnitudes(self):
        for sol in (0.000005, 0.002, 0.0035, 1.0, 64.144736842):
            with self.subTest(sol=sol):
                self.assertAlmostEqual(
                    SF.lamports_to_sol(SF.sol_to_lamports(sol)), sol,
                    places=9)


class UnsafeMaxArithmeticTests(unittest.TestCase):
    """C + I. The number that was reported 1000x too large, and the level."""

    def test_C_the_phase_six_fixture_is_64_sol_not_64000(self):
        priority = SF.priority_fee_lamports(
            compute_unit_price_micro_lamports=UNSAFE_MAX_MICRO_LAMPORTS_PER_CU,
            compute_unit_limit=SF.DEFAULT_SWAP_COMPUTE_UNITS)
        self.assertEqual(priority, UNSAFE_MAX_PRIORITY_LAMPORTS)
        self.assertAlmostEqual(SF.lamports_to_sol(priority),
                               UNSAFE_MAX_PRIORITY_SOL, places=9)

    def test_C2_the_wrong_answer_is_explicitly_excluded(self):
        """Assert against the DEFECT, not only for the correct value.

        64,144.736842 is what you get by dividing by 1e6 twice instead of
        1e6 then 1e9. Naming it here means a regression that reuses the
        micro-lamport divisor in the SOL step fails loudly, instead of once
        again looking like a plausibly big scary number."""
        self.assertAlmostEqual(
            UNSAFE_MAX_PRIORITY_LAMPORTS / 1e6, 64_144.736842, places=3,
            msg="the fixture no longer reproduces the historical mistake")
        sol = SF.lamports_to_sol(SF.priority_fee_lamports(
            compute_unit_price_micro_lamports=UNSAFE_MAX_MICRO_LAMPORTS_PER_CU,
            compute_unit_limit=400_000))
        self.assertLess(sol, 100.0)
        self.assertGreater(sol, 60.0)
        self.assertNotAlmostEqual(sol, 64_144.736842, places=3)

    def test_C3_the_module_comment_no_longer_states_the_wrong_magnitude(self):
        import inspect
        src = inspect.getsource(SF)
        self.assertIn("64.144736842", src)
        self.assertIn("64,144,736,842 lamports", src)

    def test_I_unsafe_max_is_non_executable(self):
        with self.assertRaises(SF.NonExecutablePriorityLevel):
            SF.assert_executable_level("UnsafeMax")
        # Case-insensitively, because a case bug is what broke the primary.
        for spelling in ("unsafeMax", "unsafemax", "UNSAFEMAX", " UnsafeMax "):
            with self.subTest(spelling=spelling):
                with self.assertRaises(SF.NonExecutablePriorityLevel):
                    SF.assert_executable_level(spelling)

    def test_I2_no_policy_maps_to_unsafe_max(self):
        for policy in SF.POLICIES:
            with self.subTest(policy=policy):
                self.assertNotIn(SF._HELIUS_LEVEL[policy].lower(),
                                 SF.NON_EXECUTABLE_HELIUS_LEVELS)

    def test_I3_max_acceptance_maps_to_the_highest_SAFE_level(self):
        self.assertEqual(SF._HELIUS_LEVEL[SF.MAX_ACCEPTANCE], "VeryHigh")
        self.assertEqual(SF._HELIUS_LEVEL[SF.MAX_ACCEPTANCE],
                         SF._HELIUS_LEVEL[SF.VERY_HIGH])

    def test_I4_wiring_unsafe_max_into_the_table_still_refuses(self):
        """The guard is at the call, not merely in the table.

        A future edit that adds UnsafeMax to _HELIUS_LEVEL must not be able
        to quietly enable a 64 SOL bid."""
        original = dict(SF._HELIUS_LEVEL)
        SF._HELIUS_LEVEL[SF.MAX_ACCEPTANCE] = "UnsafeMax"
        try:
            est = SF.estimate_network_fee(
                SF.MAX_ACCEPTANCE, record_health=False,
                fetch=lambda m, p: _helius_ok(UNSAFE_MAX_MICRO_LAMPORTS_PER_CU)
                if m == "getPriorityFeeEstimate" else _rpc_rows(10))
            # It must NOT have been priced off unsafeMax: either the
            # fallback served it, or it refused outright.
            self.assertNotEqual(est.quality, SF.MEASURED_HELIUS)
            self.assertLess(est.total_network_fee_sol, 1.0)
        finally:
            SF._HELIUS_LEVEL.clear()
            SF._HELIUS_LEVEL.update(original)


def _helius_ok(micro_per_cu):
    return {"priorityFeeEstimate": micro_per_cu}


def _rpc_rows(*fees):
    return [{"prioritizationFee": f} for f in fees]


def _fetch(micro_per_cu):
    """A live-shaped Helius success."""
    def go(method, params):
        if method == "getPriorityFeeEstimate":
            return _helius_ok(micro_per_cu)
        return _rpc_rows(micro_per_cu)
    return go


class WorkedExamplesPreserveUnitMathTests(unittest.TestCase):
    """D + E. End-to-end unit math for a normal and a max-acceptance bid."""

    def test_D_a_normal_estimate_converts_correctly_end_to_end(self):
        micro_per_cu = 12_345.0
        est = SF.estimate_network_fee(SF.NORMAL, fetch=_fetch(micro_per_cu),
                                      record_health=False)
        self.assertTrue(est.ok)
        self.assertEqual(est.quality, SF.MEASURED_HELIUS)
        self.assertEqual(est.compute_unit_limit, 400_000)
        self.assertEqual(est.compute_unit_limit_source,
                         SF.DEFAULT_BUDGET_ASSUMPTION)
        expected_priority = _ceil_oracle(micro_per_cu, 400_000)   # 4,938
        self.assertEqual(est.priority_fee_lamports, expected_priority)
        self.assertFalse(est.capped, "the normal example must not be capped")
        self.assertEqual(est.total_network_fee_lamports,
                         expected_priority + SF.PROTOCOL_BASE_FEE_LAMPORTS)
        self.assertAlmostEqual(
            est.total_network_fee_sol,
            (expected_priority + SF.PROTOCOL_BASE_FEE_LAMPORTS) / 1e9,
            places=12)
        # Sanity of MAGNITUDE, which is what the 1000x error destroyed:
        # a normal bid is thousandths of a SOL, not tens of SOL.
        self.assertLess(est.total_network_fee_sol, 0.001)

    def test_E_max_acceptance_converts_correctly_and_stays_bounded(self):
        micro_per_cu = 5_000_000.0            # 2,000,000 lamports on 400k CU
        est = SF.estimate_network_fee(SF.MAX_ACCEPTANCE,
                                      fetch=_fetch(micro_per_cu),
                                      record_health=False)
        self.assertTrue(est.ok)
        self.assertEqual(est.provenance["measured_priority_fee_lamports"],
                         _ceil_oracle(micro_per_cu, 400_000))
        self.assertEqual(est.compute_unit_price_micro_lamports, 5_000_000)
        # Inside the 0.0035 SOL severe ceiling, so it is priced not capped.
        self.assertFalse(est.capped)
        self.assertAlmostEqual(est.total_network_fee_sol, 0.002005, places=9)

    def test_E2_max_acceptance_is_still_capped_by_operator_policy(self):
        est = SF.estimate_network_fee(SF.MAX_ACCEPTANCE,
                                      fetch=_fetch(200_000_000.0),
                                      record_health=False)
        self.assertTrue(est.capped)
        self.assertLessEqual(est.total_network_fee_lamports,
                             POLICY.caps_for(POLICY.SEVERE_RISK_EXIT)
                             ["max_total_network_fee_lamports"])
        self.assertAlmostEqual(est.total_network_fee_sol,
                               POLICY.EMERGENCY_TOTAL_CEILING_SOL, places=9)
        # The measurement survives next to the cap; capping does not erase
        # what the network said.
        self.assertGreater(est.provenance["measured_priority_fee_lamports"],
                           est.priority_fee_lamports)

    def test_E3_the_default_budget_stays_labelled_an_assumption(self):
        """simulateTransaction is unavailable; 400k CU must not harden."""
        est = SF.estimate_network_fee(SF.NORMAL, fetch=_fetch(100.0),
                                      record_health=False)
        self.assertEqual(est.compute_unit_limit_source,
                         "DEFAULT_BUDGET_ASSUMPTION")
        measured = SF.estimate_network_fee(SF.NORMAL, fetch=_fetch(100.0),
                                           compute_unit_limit=213_444,
                                           record_health=False)
        self.assertEqual(measured.compute_unit_limit_source,
                         "CALLER_SIMULATION")
        self.assertEqual(measured.compute_unit_limit, 213_444)


class OperatorFeePolicyDefaultsTests(unittest.TestCase):
    """F + G + H. Willingness to pay, and its independence from the market."""

    def test_F_normal_entry_and_exit_ceilings_are_two_thousandths_of_a_sol(self):
        for action in (POLICY.NORMAL_ENTRY, POLICY.NORMAL_EXIT):
            with self.subTest(action=action):
                caps = POLICY.caps_for(action)
                self.assertEqual(caps["max_total_network_fee_lamports"],
                                 2_000_000)
                self.assertEqual(caps["max_total_network_fee_sol"], 0.002)

    def test_G_urgent_and_severe_ceilings_are_thirty_five_ten_thousandths(self):
        for action in (POLICY.URGENT_EXIT, POLICY.SEVERE_RISK_EXIT):
            with self.subTest(action=action):
                caps = POLICY.caps_for(action)
                self.assertEqual(caps["max_total_network_fee_lamports"],
                                 3_500_000)
                self.assertEqual(caps["max_total_network_fee_sol"], 0.0035)

    def test_F_G2_risk_reduction_may_outbid_risk_creation(self):
        self.assertGreater(
            POLICY.caps_for(POLICY.SEVERE_RISK_EXIT)
            ["max_total_network_fee_lamports"],
            POLICY.caps_for(POLICY.NORMAL_ENTRY)
            ["max_total_network_fee_lamports"])

    def test_the_priority_ceiling_is_derived_from_the_total(self):
        """The two ceilings cannot drift apart, because one is computed."""
        for action in POLICY.ACTIONS:
            with self.subTest(action=action):
                caps = POLICY.caps_for(action)
                self.assertEqual(
                    caps["max_priority_fee_lamports"],
                    caps["max_total_network_fee_lamports"]
                    - SF.PROTOCOL_BASE_FEE_LAMPORTS)

    def test_the_base_fee_constant_has_not_drifted(self):
        """The policy module mirrors it rather than importing the estimator."""
        self.assertEqual(POLICY._PROTOCOL_BASE_FEE_LAMPORTS,
                         SF.PROTOCOL_BASE_FEE_LAMPORTS)

    def test_H_ceilings_are_configuration_not_estimator_outputs(self):
        """A ceiling must not move when the fee market moves."""
        before = POLICY.caps_for(POLICY.NORMAL_ENTRY)
        cold = SF.estimate_network_fee(SF.NORMAL, fetch=_fetch(1.0),
                                       record_health=False)
        hot = SF.estimate_network_fee(SF.NORMAL, fetch=_fetch(50_000_000.0),
                                      record_health=False)
        after = POLICY.caps_for(POLICY.NORMAL_ENTRY)
        self.assertNotEqual(cold.priority_fee_lamports,
                            hot.provenance["measured_priority_fee_lamports"])
        self.assertEqual(before["max_total_network_fee_lamports"],
                         after["max_total_network_fee_lamports"])

    def test_H2_the_policy_module_contains_no_estimator(self):
        import inspect
        src = inspect.getsource(POLICY)
        for forbidden in ("getPriorityFeeEstimate",
                          "getRecentPrioritizationFees", "httpx", "rpc("):
            self.assertNotIn(forbidden, src)

    def test_H3_an_operator_override_moves_the_total_ceiling(self):
        key = "JARVIS_SOL_FEE_NORMAL_ENTRY_MAX_TOTAL_NETWORK_FEE_LAMPORTS"
        old = os.environ.get(key)
        os.environ[key] = "1234567"
        try:
            caps = POLICY.caps_for(POLICY.NORMAL_ENTRY)
            self.assertEqual(caps["max_total_network_fee_lamports"], 1_234_567)
            self.assertEqual(
                caps["sources"]["max_total_network_fee_lamports"],
                "ENV_OVERRIDE")
        finally:
            os.environ.pop(key, None) if old is None else os.environ.__setitem__(key, old)

    def test_H4_the_emergency_fallback_cannot_exceed_the_emergency_ceiling(self):
        """v1 defaulted it to 0.02 SOL — 5.7x the ceiling it sat under."""
        fallback, source = POLICY.emergency_fallback_lamports()
        self.assertEqual(fallback, 3_500_000)
        self.assertIn("SEVERE_RISK_EXIT", source)

        key = POLICY.EMERGENCY_FALLBACK_ENV
        old = os.environ.get(key)
        os.environ[key] = "20000000"          # 0.02 SOL, the v1 default
        try:
            clamped, clamped_source = POLICY.emergency_fallback_lamports()
            self.assertEqual(clamped, 3_500_000)
            self.assertEqual(clamped_source,
                             "CLAMPED_TO_SEVERE_RISK_EXIT_CEILING")
        finally:
            os.environ.pop(key, None) if old is None else os.environ.__setitem__(key, old)

    def test_an_autonomous_entry_refuses_rather_than_raising_the_ceiling(self):
        """Live estimate above policy -> REFUSE. Never widen the policy."""
        est = SF.estimate_network_fee(SF.NORMAL, fetch=_fetch(50_000_000.0),
                                      record_health=False)
        self.assertTrue(est.capped)
        auth = SF.authorize_fee(est, action=SF.ENTRY, sol_price_usd=200.0)
        self.assertFalse(auth["ok"])
        self.assertEqual(auth["reason"], "FEE_EXCEEDS_AUTHORISED_POLICY")
        self.assertGreater(auth["measured_total_network_fee_lamports"],
                           auth["policy_total_cap_lamports"])
        # And the ceiling is exactly where it was.
        self.assertEqual(POLICY.caps_for(POLICY.NORMAL_ENTRY)
                         ["max_total_network_fee_lamports"], 2_000_000)

    def test_the_total_ceiling_binds_even_if_the_priority_override_is_loose(self):
        """Overriding one bound must not escape the other."""
        key = "JARVIS_SOL_FEE_NORMAL_ENTRY_MAX_PRIORITY_FEE_LAMPORTS"
        old = os.environ.get(key)
        os.environ[key] = str(500_000_000)     # 0.5 SOL of priority
        try:
            est = SF.estimate_network_fee(SF.NORMAL,
                                          fetch=_fetch(500_000_000.0),
                                          record_health=False)
            self.assertTrue(est.capped)
            self.assertEqual(est.provenance["binding_cap"],
                             "TOTAL_NETWORK_FEE_CEILING")
            self.assertLessEqual(est.total_network_fee_lamports, 2_000_000)
        finally:
            os.environ.pop(key, None) if old is None else os.environ.__setitem__(key, old)


class FundingAuthorityIsNotAnEnumTests(unittest.TestCase):
    """J + K. Virtual money must have an AUTHORIZED origin."""

    def test_J_test_fixture_cannot_fund_outside_a_test_process(self):
        """Canonical runtime — DEX, scheduler, API, CLI — is not pytest."""
        old = os.environ.get(DW.UNDER_PYTEST_ENV)
        os.environ.pop(DW.UNDER_PYTEST_ENV, None)
        try:
            with self.assertRaises(DW.FundingAuthorizationError) as ctx:
                DW.issue_test_fixture_grant(mint=DW.SOL_MINT, quantity=1.0)
            self.assertIn("outside a test process", str(ctx.exception))
        finally:
            if old is not None:
                os.environ[DW.UNDER_PYTEST_ENV] = old

    def test_J2_naming_the_authority_string_funds_nothing(self):
        """The whole defect, stated as a test."""
        for pretender in (DW.TEST_FIXTURE, DW.OPERATOR_GRANT,
                          DW.CONFIGURED_VIRTUAL_ENDOWMENT):
            with self.subTest(authority=pretender):
                with self.assertRaises(DW.FundingAuthorizationError):
                    DW.fund_wallet(pretender)

    def test_J3_a_hand_built_grant_is_refused(self):
        """A dataclass anyone can instantiate is not a capability."""
        with self.assertRaises(DW.FundingAuthorizationError):
            DW.FundingGrant(authority=DW.TEST_FIXTURE, mint=DW.SOL_MINT,
                            quantity=1.0, reason="mine now", actor="me",
                            issued_at="2026-01-01T00:00:00+00:00")

    def test_J4_no_canonical_runtime_module_can_reach_the_issuer(self):
        """Structural: only lib/dex_wallet.py knows how to make fixture money."""
        import pathlib
        root = pathlib.Path(DW.__file__).resolve().parent.parent
        offenders = []
        for folder in ("lib", "app"):
            for path in (root / folder).rglob("*.py"):
                if path.name == "dex_wallet.py":
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
                if ("issue_test_fixture_grant" in text
                        or "_GRANT_SEAL" in text):
                    offenders.append(str(path.relative_to(root)))
        self.assertEqual(offenders, [],
                         "a canonical runtime module can mint fixture money")

    def test_K_operator_grant_is_unavailable_without_a_workflow(self):
        """No workflow exists, so the authority is CLOSED, not friendly."""
        old = os.environ.get(DW.OPERATOR_GRANT_APPROVAL_ENV)
        os.environ.pop(DW.OPERATOR_GRANT_APPROVAL_ENV, None)
        try:
            self.assertFalse(DW.operator_grant_workflow_available())
            with self.assertRaises(DW.FundingAuthorizationError) as ctx:
                DW.issue_operator_grant(mint=DW.SOL_MINT, quantity=1.0,
                                        reason="because", actor="someone",
                                        approval="anything")
            self.assertIn("no operator-grant workflow is configured",
                          str(ctx.exception))
        finally:
            if old is not None:
                os.environ[DW.OPERATOR_GRANT_APPROVAL_ENV] = old

    def test_K2_a_wrong_approval_is_refused(self):
        key = DW.OPERATOR_GRANT_APPROVAL_ENV
        old = os.environ.get(key)
        os.environ[key] = "the-real-approval"
        try:
            with self.assertRaises(DW.FundingAuthorizationError):
                DW.issue_operator_grant(mint=DW.SOL_MINT, quantity=1.0,
                                        reason="because", actor="someone",
                                        approval="guessed")
            # And an authorised one carries the actor into provenance.
            grant = DW.issue_operator_grant(
                mint=DW.SOL_MINT, quantity=1.0, reason="manual top-up",
                actor="operator:jon", approval="the-real-approval")
            self.assertEqual(grant.authority, DW.OPERATOR_GRANT)
            self.assertEqual(grant.actor, "operator:jon")
            self.assertTrue(grant.issued_at)
        finally:
            os.environ.pop(key, None) if old is None else os.environ.__setitem__(key, old)

    def test_K3_an_operator_grant_must_name_its_actor(self):
        key = DW.OPERATOR_GRANT_APPROVAL_ENV
        old = os.environ.get(key)
        os.environ[key] = "approved"
        try:
            with self.assertRaises(DW.FundingAuthorizationError):
                DW.issue_operator_grant(mint=DW.SOL_MINT, quantity=1.0,
                                        reason="because", actor="  ",
                                        approval="approved")
        finally:
            os.environ.pop(key, None) if old is None else os.environ.__setitem__(key, old)

    def test_the_endowment_issuer_cannot_mint_an_arbitrary_amount(self):
        """Even the permitted authority is bound to the configured policy."""
        key = DW.ENDOWMENT_ENV
        old = os.environ.get(key)
        os.environ[key] = "SOL:1.5"
        try:
            good = DW.issue_endowment_grant(mint=DW.SOL_MINT, quantity=1.5)
            self.assertEqual(good.authority, DW.CONFIGURED_VIRTUAL_ENDOWMENT)
            with self.assertRaises(DW.FundingAuthorizationError):
                DW.issue_endowment_grant(mint=DW.SOL_MINT, quantity=1_000.0)
            with self.assertRaises(DW.FundingAuthorizationError):
                DW.issue_endowment_grant(mint=DW.USDC_MINT, quantity=1.5)
        finally:
            os.environ.pop(key, None) if old is None else os.environ.__setitem__(key, old)

    def test_an_unconfigured_endowment_is_unavailable_not_a_default(self):
        old = os.environ.get(DW.ENDOWMENT_ENV)
        os.environ.pop(DW.ENDOWMENT_ENV, None)
        try:
            with self.assertRaises(DW.FundingAuthorizationError):
                DW.issue_endowment_grant(mint=DW.SOL_MINT, quantity=1.0)
        finally:
            if old is not None:
                os.environ[DW.ENDOWMENT_ENV] = old


class _HeliusErrorLike(RuntimeError):
    """Shaped like lib.helius_client.HeliusError: it carries `.status`."""

    def __init__(self, status, detail):
        self.status = status
        super().__init__(f"rpc/getPriorityFeeEstimate: {status} {detail}")


def _clear_estimator_health():
    from app.database import ProviderHealth, get_db
    with get_db() as db:
        db.query(ProviderHealth).filter(
            ProviderHealth.provider.in_([SF.PRIMARY_PROVIDER,
                                         SF.FALLBACK_PROVIDER])).delete(
            synchronize_session=False)
        db.commit()


class FallbackMustNotHideABrokenPrimaryTests(unittest.TestCase):
    """L. The Phase 6 failure, made impossible to miss."""

    def setUp(self):
        _clear_estimator_health()

    def tearDown(self):
        _clear_estimator_health()

    def _malformed_primary(self, method, params):
        if method == "getPriorityFeeEstimate":
            raise _HeliusErrorLike(-32602, "Invalid params")
        return _rpc_rows(1_000, 2_000, 3_000)

    def test_L_a_permanently_malformed_primary_becomes_visible(self):
        """The estimates are fine. The primary has never once worked."""
        for _ in range(5):
            est = SF.estimate_network_fee(SF.NORMAL,
                                          fetch=self._malformed_primary)
            self.assertTrue(est.ok, "the fallback must still serve")
            self.assertEqual(est.quality, SF.MEASURED_RPC_FALLBACK)

        health = SF.fee_estimator_health()
        self.assertTrue(health["available"])
        self.assertEqual(health["primary"]["successes"], 0)
        self.assertEqual(health["primary"]["attempts"], 5)
        self.assertEqual(health["primary"]["last_error_class"],
                         SF.ERR_MALFORMED_REQUEST)
        self.assertTrue(health["primary_never_succeeded"])
        self.assertTrue(health["fallback_is_masking_a_dead_primary"])
        self.assertTrue(health["actionable"])
        self.assertEqual(health["last_successful_source"],
                         "rpc.getRecentPrioritizationFees")
        self.assertIn("NEVER succeeded", health["note"])

    def test_L2_the_primary_failure_lands_on_the_existing_health_surface(self):
        """No second monitoring architecture: it is a ProviderHealth row."""
        from lib import provider_health as PH
        SF.estimate_network_fee(SF.NORMAL, fetch=self._malformed_primary)
        rows = {(r["provider"], r["capability"]): r for r in PH.snapshot()}
        primary = rows.get((SF.PRIMARY_PROVIDER, SF.PRIMARY_CAPABILITY))
        self.assertIsNotNone(primary, "the primary attempt was not recorded")
        self.assertIn(SF.ERR_MALFORMED_REQUEST, primary["detail"])
        self.assertTrue(primary["actionable"])
        fallback = rows.get((SF.FALLBACK_PROVIDER, SF.FALLBACK_CAPABILITY))
        self.assertEqual(fallback["status"], PH.HEALTHY)

    def test_L3_a_malformed_request_is_classified_as_never_transient(self):
        """-32602 is our bug, not their outage. Retrying cannot fix it."""
        self.assertEqual(
            SF.classify_primary_error(_HeliusErrorLike(-32602, "Invalid params")),
            SF.ERR_MALFORMED_REQUEST)
        self.assertIn(SF.ERR_MALFORMED_REQUEST, SF.NEVER_TRANSIENT)
        SF.estimate_network_fee(SF.NORMAL, fetch=self._malformed_primary)
        health = SF.fee_estimator_health()
        self.assertTrue(health["primary_failure_is_non_transient"])
        self.assertTrue(health["actionable"],
                        "a malformed request must escalate immediately")

    def test_L4_one_transient_blip_does_not_create_noise(self):
        """A single 500 is a fact, not an alarm."""
        def flaky(method, params):
            if method == "getPriorityFeeEstimate":
                raise _HeliusErrorLike(503, "upstream unavailable")
            return _rpc_rows(1_000)
        SF.estimate_network_fee(SF.NORMAL, fetch=flaky)
        health = SF.fee_estimator_health()
        self.assertEqual(health["primary"]["consecutive_failures"], 1)
        self.assertFalse(health["primary_persistently_failing"])
        self.assertFalse(health["fallback_is_masking_a_dead_primary"])
        self.assertFalse(health["actionable"],
                         "one transient failure must not escalate")

    def test_L5_a_working_primary_reads_healthy(self):
        for _ in range(3):
            est = SF.estimate_network_fee(SF.NORMAL, fetch=_fetch(5_000.0))
            self.assertEqual(est.quality, SF.MEASURED_HELIUS)
        health = SF.fee_estimator_health()
        self.assertEqual(health["primary"]["successes"], 3)
        self.assertFalse(health["primary_never_succeeded"])
        self.assertFalse(health["actionable"])
        self.assertEqual(health["last_successful_source"],
                         "helius.getPriorityFeeEstimate")
        self.assertIsNone(health["note"])

    def test_L6_error_classes_cover_the_failures_that_matter(self):
        cases = {
            -32601: SF.ERR_MALFORMED_REQUEST,
            402: SF.ERR_PAYMENT_REQUIRED,
            429: SF.ERR_RATE_LIMITED,
            401: SF.ERR_AUTH,
            500: SF.ERR_TRANSPORT,
        }
        for status, expected in cases.items():
            with self.subTest(status=status):
                self.assertEqual(
                    SF.classify_primary_error(_HeliusErrorLike(status, "x")),
                    expected)
        self.assertEqual(
            SF.classify_primary_error(RuntimeError("HELIUS_API_KEY is not set")),
            SF.ERR_NOT_CONFIGURED)
        self.assertEqual(
            SF.classify_primary_error(
                ValueError("no priorityFeeEstimate in response: {}")),
            SF.ERR_EMPTY_RESPONSE)

    def test_L7_both_authorities_dead_is_unknown_not_a_number(self):
        def dead(method, params):
            raise _HeliusErrorLike(500, "down")
        est = SF.estimate_network_fee(SF.NORMAL, fetch=dead)
        self.assertFalse(est.ok)
        self.assertEqual(est.quality, SF.UNKNOWN)
        self.assertEqual(est.provenance["helius_error_class"],
                         SF.ERR_TRANSPORT)


if __name__ == "__main__":
    unittest.main()
