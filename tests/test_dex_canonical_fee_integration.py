"""Phase 6.2 — the fee authority is WIRED, not merely present.

WHAT THIS FILE EXISTS TO PROVE. Phase 6.1 built `lib.solana_fees` and
`lib.solana_fee_policy`, tested them thoroughly, and shipped them with NO
CANONICAL CALLER. Every DEX path priced its network fee from
`dex_swap_math.DEFAULT_PRIORITY_LAMPORTS` — a 100,000-lamport constant —
so the live fee market reached the simulator's economics nowhere at all.

A HELPER THAT EXISTS BUT HAS NO CANONICAL CALLER IS NOT IMPLEMENTED, and
"wired but inert" is the failure mode that has cost this project the most
time. Tests that call a helper directly cannot detect it: they pass whether
or not production uses the thing they are testing.

So the discrimination tests below POISON THE OLD PATH and then exercise the
REAL canonical caller. If canonical execution still depends on the static
constant, poisoning it makes the test fail. If it depends on the measured
authority, poisoning it changes nothing.

THREE FACTS, NEVER COLLAPSED:

    MEASURED    what the fee market indicated
    AUTHORIZED  what operator policy permitted us to bid
    ACTUAL      what the chain took — the ONLY one ever charged
"""
import os
import unittest
from unittest.mock import patch

from lib import dex_network_cost as NC
from lib import dex_wallet as DW
from lib import solana_fee_policy as POLICY
from lib import solana_fees as SF


# ── Injected estimators. A hermetic test never reaches real Helius. ─────
def fetch_at(micro_per_cu=1_000.0, *, helius=True, capture=None):
    """A live-shaped estimator. `capture` records the params it was sent."""
    def go(method, params):
        if capture is not None:
            capture.append((method, params))
        if method == "getPriorityFeeEstimate":
            if not helius:
                raise RuntimeError("helius down")
            return {"priorityFeeEstimate": micro_per_cu}
        return [{"prioritizationFee": micro_per_cu}]
    return go


def dead_fetch(method, params):
    raise RuntimeError("both authorities down")


class _Enabled:
    def __enter__(self):
        self.old = os.environ.get("DEX_AUTOTRADE_ENABLED")
        os.environ["DEX_AUTOTRADE_ENABLED"] = "1"
        return self

    def __exit__(self, *_):
        if self.old is None:
            os.environ.pop("DEX_AUTOTRADE_ENABLED", None)
        else:
            os.environ["DEX_AUTOTRADE_ENABLED"] = self.old


def _fund_sol(amount=5.0, usdc=10_000.0):
    from app.database import DexBalance, DexFundingEvent, get_db
    with get_db() as db:
        db.query(DexBalance).delete()
        db.query(DexFundingEvent).delete()
    DW.fund_wallet(DW.issue_test_fixture_grant(
        mint=DW.SOL_MINT, quantity=amount, reason="phase 6.2 fixture"))
    DW.fund_wallet(DW.issue_test_fixture_grant(
        mint=DW.USDC_MINT, quantity=usdc, reason="phase 6.2 fixture"))


def candidate(**kw):
    base = {"mint": "Mint111", "symbol": "TEST", "reserve_usd": 500_000.0,
            "price_usd": 1.0, "dex": "raydium", "pool_address": "Pool111",
            "depth_confidence": "VERIFIED", "gross_expected_r": 0.60}
    base.update(kw)
    return base


def _plan(notional=10.0):
    class _P:
        symbol = "TOK/SOL"; side = "long"; qty = float(notional)
        entry = 1.0
        quantity_unit = "TOKENS"; instrument_id = None
        order_type = "market"
    _P.notional = float(notional)
    return _P()


class CanonicalExecutionCallsTheFeeAuthorityTests(unittest.TestCase):
    """A + B + C. The wiring, and proof it is load-bearing."""

    def setUp(self):
        _fund_sol()

    def test_A_the_autotrade_gate_calls_the_dynamic_fee_authority(self):
        from lib.dex_autotrade import evaluate_candidate
        calls = []
        with _Enabled():
            out = evaluate_candidate(candidate(), cash_usd=100_000,
                                     sol_price_usd=200.0,
                                     fee_fetch=fetch_at(capture=calls))
        self.assertTrue(out["eligible"], out.get("detail"))
        self.assertTrue(any(m == "getPriorityFeeEstimate" for m, _ in calls),
                        "the canonical gate never asked the fee market")
        cost = out["network_cost"]
        self.assertEqual(cost["estimator"], SF.EST_HELIUS)
        self.assertEqual(cost["estimate_quality"], SF.MEASURED_HELIUS)
        self.assertIsNotNone(cost["measured_total_network_fee_lamports"])

    def test_A2_the_venue_adapter_calls_the_dynamic_fee_authority(self):
        from lib.execution_venue import VirtualDexAdapter
        calls = []
        sub = VirtualDexAdapter().submit(
            _plan(), reserve_usd=500_000.0, sol_price_usd=200.0,
            fee_fetch=fetch_at(capture=calls))
        self.assertTrue(sub.accepted, sub.detail)
        self.assertTrue(any(m == "getPriorityFeeEstimate" for m, _ in calls),
                        "the venue adapter never asked the fee market")
        self.assertEqual(sub.provenance["network_cost"]["estimator"],
                         SF.EST_HELIUS)

    def test_B_canonical_execution_cannot_use_STATIC_POLICY_ONLY_as_fee_truth(self):
        """The static reserve basis must never appear on a canonical path."""
        from lib.execution_venue import VirtualDexAdapter
        sub = VirtualDexAdapter().submit(
            _plan(), reserve_usd=500_000.0, sol_price_usd=200.0,
            fee_fetch=fetch_at())
        self.assertTrue(sub.accepted, sub.detail)
        gas = sub.provenance.get("gas") or {}
        # gas_state is reached through the AUTHORIZATION, so its basis is
        # the authorized bid rather than the operability floor.
        self.assertNotEqual(gas.get("reserve_basis"), "STATIC_POLICY_ONLY")

    def test_B2_the_reserve_is_the_authorized_bid(self):
        est = SF.estimate_network_fee(SF.NORMAL, fetch=fetch_at(5_000.0),
                                      record_health=False)
        auth = SF.authorize_fee(est, action=SF.NORMAL_ENTRY,
                                sol_price_usd=200.0)
        gas = DW.gas_state(fee_authorization=auth)
        self.assertEqual(gas["reserve_basis"], "AUTHORIZED_FEE_BID")
        self.assertEqual(gas["authorized_bid_lamports"],
                         auth.authorized_bid_lamports)

    def test_C_poisoning_the_static_fee_path_does_not_break_canonical_execution(self):
        """THE DISCRIMINATION TEST.

        `DEFAULT_PRIORITY_LAMPORTS` is replaced with a value that raises the
        moment anything tries to use it as a number. Canonical execution
        must be entirely unaffected — if it still reaches for the constant,
        this fails loudly instead of passing on a plausible-looking fee.
        """
        class _Poison:
            def __int__(self):
                raise AssertionError(
                    "canonical execution reached for the STATIC "
                    "DEFAULT_PRIORITY_LAMPORTS instead of the measured fee")

            def __index__(self):
                return self.__int__()

        from lib.dex_autotrade import evaluate_candidate
        from lib.execution_venue import VirtualDexAdapter

        with patch("lib.dex_swap_math.DEFAULT_PRIORITY_LAMPORTS", _Poison()):
            with _Enabled():
                out = evaluate_candidate(candidate(), cash_usd=100_000,
                                         sol_price_usd=200.0,
                                         fee_fetch=fetch_at())
            self.assertTrue(out["eligible"], out.get("detail"))

            sub = VirtualDexAdapter().submit(
                _plan(), reserve_usd=500_000.0, sol_price_usd=200.0,
                fee_fetch=fetch_at())
            self.assertTrue(sub.accepted, sub.detail)

    def test_C2_the_poison_would_actually_fire_on_the_legacy_path(self):
        """Proves the poison is real rather than vacuously satisfied."""
        class _Poison:
            def __int__(self):
                raise AssertionError("legacy path used the static constant")

            def __index__(self):
                return self.__int__()

        from lib.dex_swap_math import quote_swap
        with patch("lib.dex_swap_math.DEFAULT_PRIORITY_LAMPORTS", _Poison()):
            with self.assertRaises(AssertionError):
                quote_swap(1_000.0, 500_000.0, sol_price_usd=200.0)

    def test_C3_the_gate_fee_is_the_position_fee(self):
        """The booked entry must not silently re-price after the gate."""
        from lib.dex_autotrade import evaluate_candidate
        with _Enabled():
            ev = evaluate_candidate(candidate(), cash_usd=100_000,
                                    sol_price_usd=200.0,
                                    fee_fetch=fetch_at(7_000.0))
        self.assertTrue(ev["eligible"], ev.get("detail"))
        self.assertEqual(ev["priority_lamports"],
                         ev["quote"]["priority_lamports"]
                         - SF.PROTOCOL_BASE_FEE_LAMPORTS)


class NormalEntryRefusalsTests(unittest.TestCase):
    """D + E + F. What an autonomous entry must refuse, and why."""

    def setUp(self):
        _fund_sol()

    def test_D_unknown_network_fee_refuses_a_normal_entry(self):
        from lib.dex_autotrade import NETWORK_FEE_UNKNOWN, evaluate_candidate
        with _Enabled():
            out = evaluate_candidate(candidate(), cash_usd=100_000,
                                     sol_price_usd=200.0,
                                     fee_fetch=dead_fetch)
        self.assertFalse(out["eligible"])
        self.assertEqual(out["reason"], NETWORK_FEE_UNKNOWN)
        self.assertEqual(out["network_cost"]["fee_refusal_reason"],
                         SF.FEE_ESTIMATE_UNKNOWN)

    def test_D2_the_venue_adapter_refuses_an_unknown_fee(self):
        from lib.execution_venue import VirtualDexAdapter
        sub = VirtualDexAdapter().submit(
            _plan(), reserve_usd=500_000.0, sol_price_usd=200.0,
            fee_fetch=dead_fetch)
        self.assertFalse(sub.accepted)
        self.assertEqual(sub.reason, SF.FEE_ESTIMATE_UNKNOWN)

    def test_E_a_fee_over_the_entry_ceiling_refuses(self):
        """0.002 SOL is the authored NORMAL_ENTRY ceiling."""
        from lib.dex_autotrade import NETWORK_FEE_REFUSED, evaluate_candidate
        # 50M micro-lamports/CU on 400k CU is 0.02 SOL — 10x the ceiling.
        with _Enabled():
            out = evaluate_candidate(candidate(), cash_usd=100_000,
                                     sol_price_usd=200.0,
                                     fee_fetch=fetch_at(50_000_000.0))
        self.assertFalse(out["eligible"])
        self.assertEqual(out["reason"], NETWORK_FEE_REFUSED)
        self.assertEqual(out["network_cost"]["fee_refusal_reason"],
                         SF.FEE_EXCEEDS_AUTHORISED_POLICY)
        self.assertTrue(out["network_cost"]["bid_below_measured_requirement"])

    def test_E2_the_ceiling_is_not_raised_to_meet_the_market(self):
        before = POLICY.caps_for(POLICY.NORMAL_ENTRY)[
            "max_total_network_fee_lamports"]
        est = SF.estimate_network_fee(SF.NORMAL, fetch=fetch_at(50_000_000.0),
                                      record_health=False)
        SF.authorize_fee(est, action=SF.NORMAL_ENTRY, sol_price_usd=200.0)
        after = POLICY.caps_for(POLICY.NORMAL_ENTRY)[
            "max_total_network_fee_lamports"]
        self.assertEqual(before, after)
        self.assertEqual(after, 2_000_000)

    def test_F_a_fee_that_destroys_the_expected_edge_refuses(self):
        est = SF.estimate_network_fee(SF.NORMAL, fetch=fetch_at(4_000_000.0),
                                      record_health=False)
        auth = SF.authorize_fee(est, action=SF.NORMAL_ENTRY,
                                sol_price_usd=200.0,
                                expected_edge_usd=1.0, notional_usd=1_000.0)
        self.assertFalse(auth.allowed)
        self.assertEqual(auth.refusal_reason, SF.FEE_DESTROYS_EDGE)

    def test_F2_a_fee_over_the_notional_cap_refuses(self):
        est = SF.estimate_network_fee(SF.NORMAL, fetch=fetch_at(4_000_000.0),
                                      record_health=False)
        # $0.32 of fee against a $10 trade is 3.2%, past the 1% cap.
        auth = SF.authorize_fee(est, action=SF.NORMAL_ENTRY,
                                sol_price_usd=200.0, notional_usd=10.0)
        self.assertFalse(auth.allowed)
        self.assertEqual(auth.refusal_reason, SF.FEE_EXCEEDS_NOTIONAL_CAP)


class MeasuredAuthorizedActualAreThreeFactsTests(unittest.TestCase):
    """G + H + I + J + K + Y."""

    def setUp(self):
        _fund_sol()

    def test_G_the_estimate_preserves_raw_measured_network_truth(self):
        raw = 12_345.678
        est = SF.estimate_network_fee(SF.NORMAL, fetch=fetch_at(raw),
                                      record_health=False)
        self.assertEqual(est.raw_provider_price, raw)
        self.assertEqual(est.provenance["micro_lamports_per_cu_raw"], raw)

    def test_H_authorization_does_not_mutate_the_estimate(self):
        """The headline separation."""
        est = SF.estimate_network_fee(SF.NORMAL, fetch=fetch_at(50_000_000.0),
                                      record_health=False)
        measured_before = est.measured_total_network_fee_lamports
        priority_before = est.measured_priority_fee_lamports
        auth = SF.authorize_fee(est, action=SF.NORMAL_EXIT,
                                sol_price_usd=200.0)
        self.assertEqual(est.measured_total_network_fee_lamports,
                         measured_before)
        self.assertEqual(est.measured_priority_fee_lamports, priority_before)
        self.assertLess(auth.authorized_bid_lamports, measured_before,
                        "the fixture should have been capped")

    def test_H2_the_estimate_has_no_capped_field_at_all(self):
        """Structural: there is nowhere for a policy number to hide."""
        est = SF.estimate_network_fee(SF.NORMAL, fetch=fetch_at(),
                                      record_health=False)
        self.assertFalse(hasattr(est, "capped"))
        self.assertFalse(hasattr(est, "cap_applied_lamports"))
        self.assertTrue(est.provenance["no_policy_applied"])

    def test_I_the_authorized_bid_is_represented_separately(self):
        est = SF.estimate_network_fee(SF.NORMAL, fetch=fetch_at(50_000_000.0),
                                      record_health=False)
        auth = SF.authorize_fee(est, action=SF.URGENT_EXIT,
                                sol_price_usd=200.0)
        self.assertTrue(auth.allowed)
        self.assertTrue(auth.bid_below_measured_requirement,
                        "a capped bid must say so")
        self.assertNotEqual(auth.authorized_bid_lamports,
                            auth.measured_total_network_fee_lamports)

    def test_J_the_actual_fee_is_a_third_separate_field(self):
        from lib.virtual_orders import ExecutionResult
        r = ExecutionResult(state="FILLED", symbol="TOK/SOL", side="long",
                            order_type="market",
                            measured_network_fee_lamports=3_000_000,
                            authorized_network_fee_lamports=2_000_000,
                            actual_network_fee_lamports=1_950_000)
        self.assertEqual(r.measured_network_fee_lamports, 3_000_000)
        self.assertEqual(r.authorized_network_fee_lamports, 2_000_000)
        self.assertEqual(r.actual_network_fee_lamports, 1_950_000)

    def test_J2_absent_evidence_stays_None_and_never_becomes_zero(self):
        from lib.virtual_orders import ExecutionResult
        r = ExecutionResult(state="FILLED", symbol="TOK/SOL", side="long",
                            order_type="market")
        for field in ("measured_network_fee_lamports",
                      "authorized_network_fee_lamports",
                      "actual_network_fee_lamports"):
            with self.subTest(field=field):
                self.assertIsNone(getattr(r, field),
                                  "a fee nobody measured is not a free "
                                  "transaction")

    def test_K_the_actual_network_fee_is_charged_exactly_once(self):
        _fund_sol(1.0)
        DW.settle_swap_success(
            input_mint=DW.USDC_MINT, input_qty=100.0,
            output_mint="TokenMint111111111111111111111111111111111",
            output_qty=42.0, network_fee_sol=0.001)
        self.assertAlmostEqual(DW.balance(DW.SOL_MINT)["total"],
                               1.0 - 0.001, places=9)

    def test_Y_estimate_and_authorization_are_never_added_to_the_charge(self):
        """Only ACTUAL is charged. Evidence is not money."""
        _fund_sol(1.0)
        est = SF.estimate_network_fee(SF.NORMAL, fetch=fetch_at(5_000.0),
                                      record_health=False)
        auth = SF.authorize_fee(est, action=SF.NORMAL_ENTRY,
                                sol_price_usd=200.0)
        actual_sol = 0.0004
        DW.settle_swap_success(
            input_mint=DW.USDC_MINT, input_qty=100.0,
            output_mint="TokenMint111111111111111111111111111111111",
            output_qty=1.0, network_fee_sol=actual_sol)
        charged = 1.0 - DW.balance(DW.SOL_MINT)["total"]
        self.assertAlmostEqual(charged, actual_sol, places=9)
        # The two evidence figures exist and are NOT part of the debit.
        self.assertGreater(auth.authorized_bid_lamports, 0)
        self.assertGreater(est.measured_total_network_fee_lamports, 0)

    def test_reconciliation_preserves_all_three(self):
        est = SF.estimate_network_fee(SF.NORMAL, fetch=fetch_at(5_000.0),
                                      record_health=False)
        auth = SF.authorize_fee(est, action=SF.NORMAL_ENTRY,
                                sol_price_usd=200.0)
        row = SF.reconcile_fee(est, actual_total_lamports=7_777,
                               actual_fee_source="MODELLED_SETTLEMENT",
                               authorization=auth)
        self.assertEqual(row["estimated_total_lamports"],
                         est.measured_total_network_fee_lamports)
        self.assertEqual(row["authorized_bid_lamports"],
                         auth.authorized_bid_lamports)
        self.assertEqual(row["actual_total_lamports"], 7_777)


class AccountAwareContextTests(unittest.TestCase):
    """L + M + N + O. Use the best real context; label what it was."""

    def test_L_writable_keys_reach_the_helius_primary(self):
        calls = []
        SF.estimate_network_fee(
            SF.NORMAL, writable_account_keys=["PoolAAA", "MintBBB"],
            fetch=fetch_at(capture=calls), record_health=False)
        method, params = calls[0]
        self.assertEqual(method, "getPriorityFeeEstimate")
        self.assertEqual(params[0]["accountKeys"], ["PoolAAA", "MintBBB"])

    def test_M_writable_keys_reach_the_rpc_fallback(self):
        """The accountless fallback was measuring the wrong market."""
        calls = []
        est = SF.estimate_network_fee(
            SF.NORMAL, writable_account_keys=["PoolAAA", "MintBBB"],
            fetch=fetch_at(helius=False, capture=calls), record_health=False)
        fallback = [c for c in calls
                    if c[0] == "getRecentPrioritizationFees"]
        self.assertTrue(fallback, "the fallback was never called")
        self.assertEqual(fallback[0][1], [["PoolAAA", "MintBBB"]])
        self.assertEqual(est.quality, SF.MEASURED_RPC_FALLBACK)

    def test_N_the_local_fallback_is_labelled_account_aware(self):
        est = SF.estimate_network_fee(
            SF.NORMAL, writable_account_keys=["PoolAAA"],
            fetch=fetch_at(helius=False), record_health=False)
        self.assertEqual(est.estimator, SF.EST_RPC_LOCAL)
        self.assertEqual(est.context_quality, SF.CONTEXT_LOCAL_ACCOUNTS)

    def test_O_the_accountless_fallback_is_labelled_global_and_coarse(self):
        """A global zero is not proof that THIS transaction is free."""
        est = SF.estimate_network_fee(
            SF.NORMAL, fetch=fetch_at(0.0, helius=False),
            record_health=False)
        self.assertEqual(est.estimator, SF.EST_RPC_GLOBAL)
        self.assertEqual(est.context_quality, SF.CONTEXT_GLOBAL)
        self.assertEqual(est.measured_priority_fee_lamports, 0)
        self.assertNotEqual(est.context_quality, SF.CONTEXT_TRANSACTION)

    def test_O2_a_serialized_transaction_is_the_best_context(self):
        calls = []
        est = SF.estimate_network_fee(
            SF.NORMAL, transaction_b64="BASE64TX",
            writable_account_keys=["PoolAAA"],
            fetch=fetch_at(capture=calls), record_health=False)
        self.assertEqual(est.context_quality, SF.CONTEXT_TRANSACTION)
        self.assertEqual(calls[0][1][0]["transaction"], "BASE64TX")

    def test_the_canonical_path_supplies_the_pools_writable_accounts(self):
        keys = NC.writable_accounts_for_swap(mint="MintX", pool_address="PoolY")
        self.assertEqual(keys[0], "PoolY", "the contended account comes first")
        self.assertIn("MintX", keys)
        self.assertIn(NC.SOL_MINT, keys)


class PriorityAndActionAreOrthogonalTests(unittest.TestCase):
    """P + Q + R. The coupling that had to go."""

    def test_P_a_HIGH_priority_entry_uses_NORMAL_ENTRY_economics(self):
        est = SF.estimate_network_fee(SF.HIGH, fetch=fetch_at(1_000.0),
                                      record_health=False)
        auth = SF.authorize_fee(est, action=SF.NORMAL_ENTRY,
                                sol_price_usd=200.0, notional_usd=1_000.0)
        self.assertTrue(auth.allowed)
        self.assertEqual(auth.action_policy, POLICY.NORMAL_ENTRY)
        self.assertEqual(auth.priority_level, SF.HIGH)
        self.assertEqual(auth.operator_total_fee_limit_lamports,
                         POLICY.caps_for(POLICY.NORMAL_ENTRY)
                         ["max_total_network_fee_lamports"])

    def test_Q_changing_the_exit_ceiling_cannot_move_entry_economics(self):
        """THE PROOF THE COUPLING IS GONE.

        A table used to map HIGH -> NORMAL_EXIT, so a HIGH-priority ENTRY
        silently inherited exit caps. It looked coherent only because both
        ceilings were 0.002 SOL. Move the exit ceiling far away and the
        entry must not notice.
        """
        key = "JARVIS_SOL_FEE_NORMAL_EXIT_MAX_TOTAL_NETWORK_FEE_LAMPORTS"
        old = os.environ.get(key)
        est = SF.estimate_network_fee(SF.HIGH, fetch=fetch_at(1_000.0),
                                      record_health=False)
        before = SF.authorize_fee(est, action=SF.NORMAL_ENTRY,
                                  sol_price_usd=200.0, notional_usd=1_000.0)
        os.environ[key] = "999000000"          # ~1 SOL of exit headroom
        try:
            after = SF.authorize_fee(est, action=SF.NORMAL_ENTRY,
                                     sol_price_usd=200.0,
                                     notional_usd=1_000.0)
        finally:
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
        self.assertEqual(before.operator_total_fee_limit_lamports,
                         after.operator_total_fee_limit_lamports)
        self.assertEqual(after.operator_total_fee_limit_lamports, 2_000_000)

    def test_Q2_the_priority_to_action_mapping_table_is_gone(self):
        """Structural: the coupling cannot come back by accident."""
        import inspect
        src = inspect.getsource(SF)
        self.assertNotIn("_POLICY_ACTION_FOR", src,
                         "the priority->action coupling table is back")

    def test_Q3_an_action_still_constrains_how_hard_it_may_bid(self):
        """The correct direction of coupling is preserved."""
        est = SF.estimate_network_fee(SF.MAX_ACCEPTANCE, fetch=fetch_at(),
                                      record_health=False)
        entry = SF.authorize_fee(est, action=SF.NORMAL_ENTRY,
                                 sol_price_usd=200.0)
        self.assertFalse(entry.allowed)
        self.assertEqual(entry.refusal_reason,
                         SF.PRIORITY_NOT_PERMITTED_FOR_ACTION)
        severe = SF.authorize_fee(est, action=SF.SEVERE_RISK_EXIT,
                                  sol_price_usd=200.0)
        self.assertTrue(severe.allowed)

    def test_R_unsafe_max_remains_structurally_non_executable(self):
        with self.assertRaises(SF.NonExecutablePriorityLevel):
            SF.assert_executable_level("UnsafeMax")
        for level in SF.PRIORITY_LEVELS:
            with self.subTest(level=level):
                self.assertNotIn(SF._HELIUS_LEVEL[level].lower(),
                                 SF.NON_EXECUTABLE_HELIUS_LEVELS)


class CallerCapSemanticsTests(unittest.TestCase):
    """S + T + U. A caller may shrink; it may never create."""

    def setUp(self):
        _fund_sol(5.0)

    def test_S_a_caller_cap_of_zero_is_an_explicit_zero(self):
        from lib.execution_venue import VirtualDexAdapter
        sub = VirtualDexAdapter().submit(
            _plan(), reserve_usd=500_000.0, sol_price_usd=200.0,
            gas_balance_sol=0.0, fee_fetch=fetch_at())
        self.assertFalse(sub.accepted)
        self.assertEqual(sub.reason, "INSUFFICIENT_GAS")

    def test_T_a_caller_cap_of_None_is_absence_not_zero(self):
        from lib.execution_venue import VirtualDexAdapter
        sub = VirtualDexAdapter().submit(
            _plan(), reserve_usd=500_000.0, sol_price_usd=200.0,
            gas_balance_sol=None, fee_fetch=fetch_at())
        self.assertTrue(sub.accepted, sub.detail)

    def test_U_a_caller_balance_cannot_become_wallet_authority(self):
        from app.database import DexBalance, DexFundingEvent, get_db
        from lib.execution_venue import VirtualDexAdapter
        with get_db() as db:
            db.query(DexBalance).delete()
            db.query(DexFundingEvent).delete()
        sub = VirtualDexAdapter().submit(
            _plan(), reserve_usd=500_000.0, sol_price_usd=200.0,
            gas_balance_sol=99.0, fee_fetch=fetch_at())
        self.assertFalse(sub.accepted)
        self.assertEqual(sub.reason, "WALLET_NOT_FUNDED")
        self.assertEqual(DW.balances(), [])


class FundingAuthorityDoesNotRegressTests(unittest.TestCase):
    """V. Phase 6.1's protections survive Phase 6.2."""

    def test_V_test_fixture_cannot_reach_the_canonical_operator_db(self):
        from app.database import DB_PATH
        self.assertIn("jarvis-test-db", str(DB_PATH),
                      "pytest is not running against a throwaway database")
        old = os.environ.get(DW.UNDER_PYTEST_ENV)
        os.environ.pop(DW.UNDER_PYTEST_ENV, None)
        try:
            with self.assertRaises(DW.FundingAuthorizationError):
                DW.issue_test_fixture_grant(mint=DW.SOL_MINT, quantity=1.0)
        finally:
            if old is not None:
                os.environ[DW.UNDER_PYTEST_ENV] = old

    def test_V2_a_bare_authority_string_still_mints_nothing(self):
        for pretender in (DW.TEST_FIXTURE, DW.OPERATOR_GRANT,
                          DW.CONFIGURED_VIRTUAL_ENDOWMENT):
            with self.subTest(authority=pretender):
                with self.assertRaises(DW.FundingAuthorizationError):
                    DW.fund_wallet(pretender)

    def test_V3_operator_grant_is_still_unavailable(self):
        old = os.environ.get(DW.OPERATOR_GRANT_APPROVAL_ENV)
        os.environ.pop(DW.OPERATOR_GRANT_APPROVAL_ENV, None)
        try:
            self.assertFalse(DW.operator_grant_workflow_available())
        finally:
            if old is not None:
                os.environ[DW.OPERATOR_GRANT_APPROVAL_ENV] = old


class AssumptionsStayLabelledTests(unittest.TestCase):
    """W + X. UNKNOWN stays UNKNOWN."""

    def test_W_the_base_fee_is_labelled_an_assumption_by_default(self):
        est = SF.estimate_network_fee(SF.NORMAL, fetch=fetch_at(),
                                      record_health=False)
        self.assertEqual(est.base_fee_source, SF.BASE_FEE_ASSUMED)
        self.assertEqual(est.measured_base_fee_lamports,
                         SF.PROTOCOL_BASE_FEE_LAMPORTS)

    def test_W2_no_compiled_message_means_no_measured_base_fee(self):
        out = NC.base_fee_authority(message_b64=None)
        self.assertIsNone(out["base_fee_lamports"])
        self.assertEqual(out["base_fee_source"], SF.BASE_FEE_ASSUMED)

    def test_W3_a_compiled_message_would_be_measured(self):
        """The path exists and is labelled; nothing today can reach it."""
        def fetch(method, params):
            self.assertEqual(method, "getFeeForMessage")
            return {"value": 10_000}
        out = NC.base_fee_authority(message_b64="MSG", fetch=fetch)
        self.assertEqual(out["base_fee_lamports"], 10_000)
        self.assertEqual(out["base_fee_source"], SF.BASE_FEE_MEASURED)

    def test_X_the_compute_budget_is_labelled_an_assumption(self):
        out = NC.compute_unit_authority(transaction_b64=None)
        self.assertEqual(out["compute_unit_limit"],
                         SF.DEFAULT_SWAP_COMPUTE_UNITS)
        self.assertEqual(out["compute_unit_limit_source"],
                         SF.DEFAULT_BUDGET_ASSUMPTION)
        self.assertIsNone(out["measured_units_consumed"])

    def test_X2_a_simulated_transaction_would_be_measured_plus_headroom(self):
        def fetch(method, params):
            self.assertEqual(method, "simulateTransaction")
            return {"value": {"unitsConsumed": 100_000}}
        out = NC.compute_unit_authority(transaction_b64="TX", fetch=fetch)
        self.assertEqual(out["measured_units_consumed"], 100_000)
        self.assertEqual(out["compute_unit_limit_source"],
                         SF.MEASURED_UNITS_CONSUMED)
        # Headroom is POLICY, applied to a measurement, and both survive.
        self.assertEqual(out["compute_headroom_pct"],
                         POLICY.compute_headroom_pct())
        self.assertEqual(out["compute_unit_limit"], 120_000)

    def test_X3_the_canonical_path_carries_the_labels_through(self):
        priced = NC.price_transaction(
            action=SF.NORMAL_ENTRY, mint="M", pool_address="P",
            sol_price_usd=200.0, fetch=fetch_at(), record_health=False)
        prov = NC.fee_provenance(priced)
        self.assertEqual(prov["compute_unit_limit_source"],
                         SF.DEFAULT_BUDGET_ASSUMPTION)
        self.assertEqual(prov["base_fee_source"], SF.BASE_FEE_ASSUMED)
        self.assertIsNone(prov["measured_units_consumed"])


class LearningDoesNotRecomputeTheChargeTests(unittest.TestCase):
    """Z. RealizedOutcome stays the canonical learning truth."""

    def test_Z_learning_reads_the_realized_outcome_it_was_given(self):
        import inspect

        from lib import canonical_learning
        src = inspect.getsource(canonical_learning)
        for forbidden in ("estimate_network_fee", "authorize_fee",
                          "price_transaction", "quote_swap"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, src,
                                 "learning recomputed an execution cost")

    def test_Z2_realized_outcome_totals_are_not_re_derived_from_fees(self):
        import inspect

        from lib import realized_outcome
        src = inspect.getsource(realized_outcome)
        for forbidden in ("estimate_network_fee", "authorize_fee",
                          "price_transaction"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, src)


class LoadedRuntimeIdentityTests(unittest.TestCase):
    """AA + AB + AC. Process identity is immutable for the process's life."""

    def test_AA_the_loaded_commit_is_captured_once_at_import(self):
        from lib import build_identity as BI
        first = BI.loaded_backend_commit()
        second = BI.loaded_backend_commit()
        self.assertEqual(first, second)
        self.assertEqual(first, BI.LOADED_BACKEND_COMMIT)

    def test_AB_repository_head_may_advance_without_moving_loaded_identity(self):
        """THE EXACT PHASE 6.1 FAILURE, reproduced and pinned.

        A server that had loaded ae5bab9 reported ac77450 because
        backend_commit re-ran `git rev-parse HEAD` per request. Here the
        repository is made to answer a DIFFERENT sha; the loaded identity
        must not move.
        """
        from lib import build_identity as BI
        loaded_before = BI.loaded_backend_commit()
        with patch.object(BI, "_git_head", return_value="b" * 40):
            self.assertEqual(BI.repository_head_commit(), "b" * 40)
            self.assertEqual(BI.loaded_backend_commit(), loaded_before)
            described = BI.describe()
        self.assertEqual(described["loaded_backend_commit"], loaded_before)
        self.assertEqual(described["repository_head_commit"], "b" * 40)
        self.assertFalse(described["code_matches_repository_head"])
        self.assertEqual(BI.loaded_backend_commit(), loaded_before)

    def test_AB2_agreement_is_reported_when_they_genuinely_agree(self):
        from lib import build_identity as BI
        with patch.object(BI, "_git_head",
                          return_value=BI.LOADED_BACKEND_COMMIT):
            described = BI.describe()
        if BI.LOADED_BACKEND_COMMIT != BI.UNKNOWN:
            self.assertTrue(described["code_matches_repository_head"])

    def test_AB3_an_unknown_sha_is_not_reported_as_a_match(self):
        from lib import build_identity as BI
        with patch.object(BI, "_git_head", return_value=BI.UNKNOWN):
            described = BI.describe()
        self.assertIsNone(described["code_matches_repository_head"],
                          "unknown must not masquerade as agreement")

    def test_AC_the_version_endpoint_exposes_loaded_identity_honestly(self):
        from app.routers.platform import system_version
        from lib import build_identity as BI
        with patch.object(BI, "_git_head", return_value="c" * 40):
            body = system_version()
        self.assertEqual(body["backend_commit"], BI.LOADED_BACKEND_COMMIT)
        self.assertEqual(body["loaded_backend_commit"],
                         BI.LOADED_BACKEND_COMMIT)
        self.assertEqual(body["repository_head_commit"], "c" * 40)
        self.assertIn("code_matches_repository_head", body)

    def test_AC2_the_endpoint_no_longer_shells_out_per_request(self):
        """Asserted on the AST, not on the text.

        A substring scan for "rev-parse" matches the COMMENT that explains
        why the shell-out was removed — this repository has been bitten
        by text-scanning its own prose four separate times. The structural
        claim is that the handler references no subprocess machinery at all.
        """
        import ast
        import inspect
        import textwrap

        from app.routers import platform
        tree = ast.parse(textwrap.dedent(
            inspect.getsource(platform.system_version)))
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        names |= {n.attr for n in ast.walk(tree)
                  if isinstance(n, ast.Attribute)}
        for banned in ("subprocess", "check_output", "Popen"):
            with self.subTest(banned=banned):
                self.assertNotIn(banned, names,
                                 "the version handler is shelling out again "
                                 "— that reports the REPOSITORY head, "
                                 "not the loaded code")


class ApiVerificationNeedsJsonNotHtmlTests(unittest.TestCase):
    """AD. HTTP 200 is not endpoint verification."""

    def test_AD_an_html_fallback_cannot_satisfy_a_json_assertion(self):
        """The SPA answers unmatched paths with index.html and HTTP 200.

        A verification helper that checks only the status code passes
        against a route the server does not have. This pins the shape of
        the check itself: JSON-parseable, and carrying the expected key.
        """
        import json

        spa_html = ("<!doctype html><html><head><title>Jarvis</title>"
                    "</head><body><div id=\"app\"></div></body></html>")

        def looks_like_json_api(body: str, expected_key: str) -> bool:
            try:
                parsed = json.loads(body)
            except (ValueError, TypeError):
                return False
            return isinstance(parsed, dict) and expected_key in parsed

        self.assertFalse(looks_like_json_api(spa_html, "status"),
                         "an HTML fallback satisfied an API assertion")
        self.assertTrue(looks_like_json_api('{"status": "ok"}', "status"))
        # And the real payload shape passes.
        from app.routers.platform import system_version
        body = json.dumps(system_version())
        self.assertTrue(looks_like_json_api(body, "loaded_backend_commit"))


class ProviderHealthReflectsRealCallsTests(unittest.TestCase):
    """17. Health describes actual calls, and startup fabricates nothing."""

    def _clear(self):
        from app.database import ProviderHealth, get_db
        with get_db() as db:
            db.query(ProviderHealth).filter(
                ProviderHealth.provider.in_(
                    [SF.PRIMARY_PROVIDER, SF.FALLBACK_PROVIDER])).delete(
                synchronize_session=False)
            db.commit()

    def setUp(self):
        self._clear()

    def tearDown(self):
        self._clear()

    def test_a_canonical_execution_populates_provider_health(self):
        _fund_sol()
        from lib.dex_autotrade import evaluate_candidate
        with _Enabled():
            evaluate_candidate(candidate(), cash_usd=100_000,
                               sol_price_usd=200.0, fee_fetch=fetch_at())
        health = SF.fee_estimator_health()
        self.assertEqual(health["primary"]["successes"], 1,
                         "a real canonical call left no health record")
        self.assertFalse(health["actionable"])

    def test_no_rows_are_fabricated_without_a_call(self):
        health = SF.fee_estimator_health()
        self.assertEqual(health["primary"]["attempts"], 0)
        self.assertIsNone(health["last_successful_source"])
        self.assertFalse(health["actionable"])

    def test_a_persistently_broken_primary_stays_visible(self):
        for _ in range(4):
            SF.estimate_network_fee(SF.NORMAL, fetch=fetch_at(helius=False))
        health = SF.fee_estimator_health()
        self.assertTrue(health["primary_never_succeeded"])
        self.assertTrue(health["fallback_is_masking_a_dead_primary"])
        self.assertTrue(health["actionable"])


if __name__ == "__main__":
    unittest.main()
