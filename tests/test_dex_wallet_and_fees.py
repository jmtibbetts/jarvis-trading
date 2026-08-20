"""The DEX wallet is a ledger, and gas is a live price. Neither is an opinion.

TWO AUTHORITIES THIS FILE PINS.

The BALANCE authority: solvency used to be a function argument
(gas_balance_sol=1.0 was a default), so a caller could make an impossible
trade executable by typing a bigger number. Balances are rows now, every
settlement is one short transaction, and the three outcomes have three
different economics:

    rejected before submit  -> nothing moves, nothing is charged
    failed after submit     -> gas only; the chain charged for a
                               transaction that exchanged nothing
    success                 -> input debited, output credited, network fee
                               debited from SOL. Exactly once each.

The FEE authority: the operator has observed aggressive Solana inclusion
costing ~0.03 SOL. That is an observation of one moment in one fee market —
it calibrates a CEILING here, it is not the price. Fees come from live
estimation (Helius per-account fee market, standard RPC as fallback), and
when no trustworthy estimate exists the answer for a normal entry is
refusal, never a constant, never zero, never the caller's number.
"""
import threading
import unittest

from lib import dex_wallet as DW
from lib import solana_fees as SF


def _fresh_wallet(sol=1.0, usdc=10_000.0):
    """A clean wallet in the test DB (conftest routes it to a temp file)."""
    from app.database import (DEFAULT_USER_ID, DexBalance, DexFundingEvent,
                              get_db)
    with get_db() as db:
        db.query(DexBalance).delete()
        db.query(DexFundingEvent).delete()
        db.add(DexBalance(user_id=DEFAULT_USER_ID, mint=DW.SOL_MINT,
                          symbol="SOL", total_quantity=sol,
                          reserved_quantity=0.0))
        db.add(DexBalance(user_id=DEFAULT_USER_ID, mint=DW.USDC_MINT,
                          symbol="USDC", total_quantity=usdc,
                          reserved_quantity=0.0))


def _estimate(policy=SF.HIGH, micro_per_cu=5_000.0):
    return SF.estimate_network_fee(
        policy, fetch=lambda m, p: {"priorityFeeEstimate": micro_per_cu})


class LedgerBasicsTests(unittest.TestCase):

    def setUp(self):
        _fresh_wallet()

    def test_funding_is_explicit_and_applied_once(self):
        """The implicit 10k USDC + 1 SOL seed is gone. Value appears only
        through a configured endowment, and only once — see
        test_dex_invariants for the full provenance suite."""
        import os
        from app.database import DexBalance, DexFundingEvent, get_db
        with get_db() as db:
            db.query(DexBalance).delete()
            db.query(DexFundingEvent).delete()
        self.assertFalse(DW.initialized())
        old = os.environ.get(DW.ENDOWMENT_ENV)
        os.environ[DW.ENDOWMENT_ENV] = "USDC:10000,SOL:1"
        try:
            first = DW.apply_configured_endowment()
            second = DW.apply_configured_endowment()
        finally:
            if old is None:
                os.environ.pop(DW.ENDOWMENT_ENV, None)
            else:
                os.environ[DW.ENDOWMENT_ENV] = old
        self.assertTrue(first["funded"])
        self.assertFalse(second["funded"], "the endowment was applied twice")
        self.assertEqual(DW.balance(DW.USDC_MINT)["total"], 10_000.0)

    def test_available_is_derived_from_total_minus_reserved(self):
        from app.database import DexBalance, get_db
        with get_db() as db:
            row = db.query(DexBalance).filter(
                DexBalance.mint == DW.USDC_MINT).first()
            row.reserved_quantity = 400.0
        b = DW.balance(DW.USDC_MINT)
        self.assertEqual(b["total"], 10_000.0)
        self.assertEqual(b["available"], 9_600.0)


class RejectedBeforeSubmitCostsNothingTests(unittest.TestCase):
    """A refusal is free. Nothing reached the chain."""

    def setUp(self):
        _fresh_wallet()

    def _totals(self):
        return {m: DW.balance(m)["total"]
                for m in (DW.SOL_MINT, DW.USDC_MINT)}

    def test_insufficient_input_asset_rejects_and_charges_nothing(self):
        before = self._totals()
        with self.assertRaises(DW.SwapRejected) as cm:
            DW.check_swap(input_mint=DW.USDC_MINT, input_qty=999_999.0)
        self.assertEqual(cm.exception.reason, "INSUFFICIENT_BALANCE")
        self.assertEqual(self._totals(), before,
                         "a rejected swap moved a balance")

    def test_insufficient_gas_rejects_and_charges_nothing(self):
        _fresh_wallet(sol=0.0)
        before = self._totals()
        with self.assertRaises(DW.SwapRejected) as cm:
            DW.check_swap(input_mint=DW.USDC_MINT, input_qty=100.0)
        self.assertEqual(cm.exception.reason, "INSUFFICIENT_GAS")
        self.assertEqual(self._totals(), before)

    def test_invalid_quantity_rejects(self):
        for bad in (0, -5, float("nan"), float("inf")):
            with self.subTest(qty=bad):
                with self.assertRaises(DW.SwapRejected):
                    DW.check_swap(input_mint=DW.USDC_MINT, input_qty=bad)

    def test_failure_before_chain_charges_no_gas(self):
        before = DW.balance(DW.SOL_MINT)["total"]
        out = DW.settle_swap_failure(network_fee_sol=0.001,
                                     reached_chain=False,
                                     reason="route vanished pre-submit")
        self.assertEqual(out["status"], DW.FAILED_BEFORE_CHAIN)
        self.assertEqual(out["network_fee_sol"], 0.0)
        self.assertEqual(DW.balance(DW.SOL_MINT)["total"], before)


class FailedAfterSubmitCostsGasOnlyTests(unittest.TestCase):
    """The state the simulator most needs to reach honestly."""

    def setUp(self):
        _fresh_wallet(sol=0.5)

    def test_gas_is_consumed_but_no_assets_exchange(self):
        usdc_before = DW.balance(DW.USDC_MINT)["total"]
        out = DW.settle_swap_failure(network_fee_sol=0.0009,
                                     reached_chain=True,
                                     reason="slippage exceeded on-chain")
        self.assertEqual(out["status"], DW.FAILED_ON_CHAIN)
        self.assertAlmostEqual(DW.balance(DW.SOL_MINT)["total"],
                               0.5 - 0.0009, places=9)
        self.assertEqual(DW.balance(DW.USDC_MINT)["total"], usdc_before,
                         "a FAILED swap exchanged assets")


class SuccessSettlesExactlyOnceTests(unittest.TestCase):

    def setUp(self):
        _fresh_wallet()

    def test_success_debits_input_credits_output_debits_gas(self):
        out = DW.settle_swap_success(
            input_mint=DW.USDC_MINT, input_qty=500.0,
            output_mint="TokenMint111111111111111111111111111111111",
            output_qty=1234.5, network_fee_sol=0.0007,
            output_symbol="TOK")
        self.assertEqual(out["status"], DW.SETTLED)
        self.assertEqual(DW.balance(DW.USDC_MINT)["total"], 9_500.0)
        self.assertAlmostEqual(DW.balance(DW.SOL_MINT)["total"],
                               1.0 - 0.0007, places=9)
        tok = DW.balance("TokenMint111111111111111111111111111111111")
        self.assertEqual(tok["total"], 1234.5)

    def test_network_fee_is_charged_exactly_once(self):
        """The fee lives in the SOL debit and NOWHERE else — output is
        credited in full, not shaved a second time."""
        DW.settle_swap_success(
            input_mint=DW.USDC_MINT, input_qty=100.0,
            output_mint="TokenMint111111111111111111111111111111111",
            output_qty=42.0, network_fee_sol=0.001)
        tok = DW.balance("TokenMint111111111111111111111111111111111")
        self.assertEqual(tok["total"], 42.0,
                         "output was shaved — the fee was charged twice")
        self.assertAlmostEqual(DW.balance(DW.SOL_MINT)["total"], 0.999,
                               places=9)

    def test_swapping_sol_itself_debits_qty_plus_gas_from_one_row(self):
        _fresh_wallet(sol=1.0)
        DW.settle_swap_success(
            input_mint=DW.SOL_MINT, input_qty=0.4,
            output_mint=DW.USDC_MINT, output_qty=80.0,
            network_fee_sol=0.0005)
        self.assertAlmostEqual(DW.balance(DW.SOL_MINT)["total"],
                               1.0 - 0.4 - 0.0005, places=9)
        self.assertEqual(DW.balance(DW.USDC_MINT)["total"], 10_080.0)


class SolMaxLeavesTheReserveTests(unittest.TestCase):

    def setUp(self):
        _fresh_wallet(sol=1.0)

    def test_static_max_leaves_the_operability_reserve(self):
        out = DW.max_spendable_sol()
        self.assertTrue(out["ok"])
        self.assertLess(out["max_sol"], 1.0)
        self.assertGreater(out["reserve_sol"], 0.0)

    def test_dynamic_max_reserves_this_transactions_estimated_fee(self):
        """§M: with a live estimate, MAX excludes what THIS transaction is
        estimated to cost plus the operability floor."""
        est = _estimate(SF.VERY_HIGH, micro_per_cu=50_000.0)
        out = DW.max_spendable_sol(fee_estimate=est)
        self.assertTrue(out["ok"])
        static = DW.max_spendable_sol()["max_sol"]
        self.assertLess(out["max_sol"], static,
                        "a live fee estimate did not tighten MAX")
        gas = out["gas"]
        self.assertEqual(gas["reserve_basis"], "DYNAMIC_FEE_ESTIMATE")
        self.assertGreater(gas["immediate_transaction_reserve_sol"], 0.0)
        self.assertGreater(gas["future_operability_reserve_sol"], 0.0)

    def test_spending_max_then_reserve_prevents_overdraw(self):
        out = DW.max_spendable_sol()
        with self.assertRaises(DW.SwapRejected):
            DW.check_swap(input_mint=DW.SOL_MINT,
                          input_qty=out["max_sol"] + out["reserve_sol"])


class ConcurrencyTests(unittest.TestCase):
    """Two spenders, one balance. The second must see the first's debit."""

    def setUp(self):
        _fresh_wallet(usdc=100.0)

    def test_concurrent_spends_cannot_exceed_available(self):
        results = []

        def spend():
            try:
                DW.settle_swap_success(
                    input_mint=DW.USDC_MINT, input_qty=80.0,
                    output_mint="TokenMint111111111111111111111111111111111",
                    output_qty=1.0, network_fee_sol=0.0001)
                results.append("ok")
            except DW.SwapRejected as e:
                results.append(e.reason)

        threads = [threading.Thread(target=spend) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(sorted(results)[1], "ok")
        self.assertEqual(results.count("ok"), 1,
                         f"both spends of 80 from 100 succeeded: {results}")
        self.assertGreaterEqual(DW.balance(DW.USDC_MINT)["total"], 0.0)


class CallerFictionCannotOverrideTheLedgerTests(unittest.TestCase):
    """§N — the original defect, pinned shut."""

    def setUp(self):
        # A FUNDED but broke wallet: the ledger exists and says zero SOL.
        # (An entirely unfunded wallet is refused earlier, with
        # WALLET_NOT_FUNDED — covered in test_dex_invariants.)
        _fresh_wallet(sol=0.0)

    def test_the_adapter_believes_the_wallet_not_the_caller(self):
        from lib.execution_venue import VirtualDexAdapter

        class _Plan:
            symbol = "TOK/SOL"; side = "long"; qty = 10.0
            entry = 1.0; notional = 10.0
            quantity_unit = "TOKENS"; instrument_id = None
            order_type = "market"

        sub = VirtualDexAdapter().submit(
            _Plan(), reserve_usd=100_000.0, sol_price_usd=200.0,
            gas_balance_sol=99.0)          # the fiction
        self.assertFalse(sub.accepted,
                         "a caller-typed 99 SOL overrode an empty wallet")
        self.assertEqual(sub.reason, "INSUFFICIENT_GAS")
        self.assertEqual(sub.provenance["gas_authority"], "PERSISTED_WALLET")
        # The caller value is recorded as a CAP, never as a source.
        self.assertEqual(sub.provenance["gas"]["caller_supplied_limit"],
                         99.0)

    def test_autotrade_evaluation_shrinks_to_the_wallet(self):
        from lib.dex_autotrade import evaluate_candidate
        import os
        old = os.environ.get("DEX_AUTOTRADE_ENABLED")
        os.environ["DEX_AUTOTRADE_ENABLED"] = "1"
        try:
            out = evaluate_candidate(
                {"mint": "M1111111111111111111111111111111111111111",
                 "symbol": "TOK", "reserve_usd": 100_000.0,
                 "price_usd": 1.0},
                gas_balance_sol=99.0, cash_usd=1_000.0)
        finally:
            if old is None:
                os.environ.pop("DEX_AUTOTRADE_ENABLED", None)
            else:
                os.environ["DEX_AUTOTRADE_ENABLED"] = old
        self.assertFalse(out.get("eligible"))
        self.assertEqual(out.get("reason"), "INSUFFICIENT_GAS")
        self.assertEqual(out["gas"]["authority"], "PERSISTED_WALLET")
        self.assertEqual(out["gas"]["caller_exceeded_wallet"], 99.0)


class DynamicFeeAuthorityTests(unittest.TestCase):
    """§A/B/K/L — gas is a live price with a real authority hierarchy."""

    def test_no_hardcoded_canonical_fee(self):
        """§A: 0.03 SOL appears ONLY as a policy CAP, never as the fee."""
        import inspect
        from lib import solana_fees
        src = inspect.getsource(solana_fees)
        # The estimate itself must come from the fetch path.
        est = _estimate(SF.HIGH, micro_per_cu=1_000.0)
        self.assertEqual(est.priority_fee_lamports,
                         int(1_000.0 * SF.DEFAULT_SWAP_COMPUTE_UNITS / 1e6),
                         "the estimate is not driven by the live fetch")
        # The CEILING now lives in policy, not in the estimator — see
        # test_dex_invariants for the full separation suite.
        self.assertIn("priority_cap_lamports", src)

    def test_the_estimate_drives_the_required_balance(self):
        """§B: a hotter fee market raises what the wallet must hold."""
        _fresh_wallet(sol=1.0)
        cold = DW.gas_state(fee_estimate=_estimate(SF.NORMAL, 100.0))
        hot = DW.gas_state(fee_estimate=_estimate(SF.VERY_HIGH, 60_000.0))
        self.assertGreater(hot["execution_reserve_sol"],
                           cold["execution_reserve_sol"])

    def test_helius_down_falls_back_to_rpc(self):
        """§K, with provenance showing the fallback."""
        def fetch(method, params):
            if method == "getPriorityFeeEstimate":
                raise RuntimeError("helius 500")
            return [{"prioritizationFee": f} for f in (10, 20, 30, 40)]
        est = SF.estimate_network_fee(SF.NORMAL, fetch=fetch)
        self.assertTrue(est.ok)
        self.assertEqual(est.quality, SF.MEASURED_RPC_FALLBACK)
        self.assertIn("helius estimate unavailable", est.reason)

    def test_no_trustworthy_estimate_is_unknown_not_a_number(self):
        """§L: both authorities dead -> UNKNOWN, and a normal entry
        refuses. Not zero, not 0.03, not the caller's number."""
        def dead(method, params):
            raise RuntimeError("down")
        est = SF.estimate_network_fee(SF.NORMAL, fetch=dead)
        self.assertFalse(est.ok)
        self.assertEqual(est.quality, SF.UNKNOWN)
        auth = SF.authorize_fee(est, action=SF.ENTRY, sol_price_usd=200.0)
        self.assertFalse(auth["ok"])
        self.assertEqual(auth["reason"], "FEE_ESTIMATE_UNKNOWN")


class FeePolicyEconomicsTests(unittest.TestCase):
    """§C/D/E/F — inclusion priority is economically aware and bounded."""

    def test_a_high_fee_destroys_a_thin_edge(self):
        """§C/D: the bot must not pay $30 of priority to capture $5.

        Magnitude check on the fixture itself: 20M micro-lamports/CU on a
        400k-CU swap is 0.008 SOL of priority — inside the HIGH cap, so the
        refusal below is the EDGE test firing, not the cap."""
        est = _estimate(SF.HIGH, micro_per_cu=20_000_000.0)
        self.assertFalse(est.capped, "fixture accidentally hit the cap")
        auth = SF.authorize_fee(est, action=SF.ENTRY, sol_price_usd=200.0,
                                expected_edge_usd=5.0, notional_usd=1_000.0)
        self.assertFalse(auth["ok"])
        self.assertEqual(auth["reason"], "FEE_DESTROYS_EDGE")

    def test_a_cheap_fee_passes_the_same_edge(self):
        est = _estimate(SF.NORMAL, micro_per_cu=100.0)
        auth = SF.authorize_fee(est, action=SF.ENTRY, sol_price_usd=200.0,
                                expected_edge_usd=5.0, notional_usd=1_000.0)
        self.assertTrue(auth["ok"], auth)

    def test_entry_may_not_select_max_acceptance(self):
        """§E's flip side: aggression is for risk REDUCTION."""
        est = _estimate(SF.MAX_ACCEPTANCE, micro_per_cu=1_000.0)
        auth = SF.authorize_fee(est, action=SF.ENTRY, sol_price_usd=200.0)
        self.assertFalse(auth["ok"])
        self.assertEqual(auth["reason"], "POLICY_NOT_PERMITTED_FOR_ACTION")

    def test_urgent_risk_reduction_may_select_max_acceptance(self):
        est = _estimate(SF.MAX_ACCEPTANCE, micro_per_cu=1_000.0)
        auth = SF.authorize_fee(est, action=SF.URGENT_RISK_REDUCTION,
                                sol_price_usd=200.0)
        self.assertTrue(auth["ok"])

    def test_max_acceptance_still_obeys_the_hard_cap(self):
        """§F: an estimator screaming an absurd number is capped, and the
        cap is recorded rather than silently applied."""
        # 200M u/CU * 400k CU = 0.08 SOL of priority — well past the
        # 0.05 SOL MAX_ACCEPTANCE ceiling.
        est = _estimate(SF.MAX_ACCEPTANCE, micro_per_cu=200_000_000.0)
        self.assertTrue(est.capped)
        cap, _source = SF.priority_cap_lamports(SF.MAX_ACCEPTANCE)
        self.assertEqual(est.priority_fee_lamports, cap)
        self.assertLessEqual(est.total_sol, 0.06,
                             "MAX_ACCEPTANCE exceeded its own ceiling")

    def test_the_emergency_fallback_is_bounded(self):
        """Even with a dead estimator, an urgent exit pays the named cap,
        not 'whatever it takes'."""
        def dead(m, p):
            raise RuntimeError("down")
        est = SF.estimate_network_fee(SF.MAX_ACCEPTANCE, fetch=dead)
        auth = SF.authorize_fee(est, action=SF.URGENT_RISK_REDUCTION,
                                sol_price_usd=200.0)
        self.assertTrue(auth["ok"])
        from lib.solana_fee_policy import emergency_fallback_lamports
        expected, _src = emergency_fallback_lamports()
        self.assertEqual(auth["fee_lamports"], expected)
        self.assertEqual(auth["policy"], "EMERGENCY_FALLBACK")


class EstimateVsActualReconciliationTests(unittest.TestCase):
    """§J — the estimate survives next to the actual, or nothing is learned."""

    def test_both_numbers_are_preserved(self):
        est = _estimate(SF.HIGH, micro_per_cu=5_000.0)
        row = SF.reconcile_fee(est, actual_total_lamports=est.total_lamports
                               + 1_500, actual_fee_source="MODELED_SETTLEMENT")
        self.assertEqual(row["estimated_total_lamports"], est.total_lamports)
        self.assertEqual(row["actual_total_lamports"],
                         est.total_lamports + 1_500)
        self.assertEqual(row["estimation_error_lamports"], 1_500)
        self.assertEqual(row["priority_policy"], SF.HIGH)
        self.assertIsNotNone(row["estimation_error_pct"])

    def test_the_summary_rolls_up_by_policy_and_estimator(self):
        est = _estimate(SF.NORMAL, micro_per_cu=2_000.0)
        SF.reconcile_fee(est, actual_total_lamports=est.total_lamports,
                         actual_fee_source="MODELED_SETTLEMENT")
        summary = SF.reconciliation_summary()
        key = f"{SF.NORMAL}/helius.getPriorityFeeEstimate"
        self.assertIn(key, summary["by_policy_estimator"])


class MigrationDoesNotRestoreLegacyTests(unittest.TestCase):
    """The endowment is a declaration, never a recovery."""

    def test_an_unconfigured_wallet_stays_empty(self):
        """No configuration, no value. The old implicit seed would have
        created 10k USDC + 1 SOL here."""
        import os
        from app.database import DexBalance, DexFundingEvent, get_db
        with get_db() as db:
            db.query(DexBalance).delete()
            db.query(DexFundingEvent).delete()
        old = os.environ.get(DW.ENDOWMENT_ENV)
        os.environ.pop(DW.ENDOWMENT_ENV, None)
        try:
            out = DW.apply_configured_endowment()
        finally:
            if old is not None:
                os.environ[DW.ENDOWMENT_ENV] = old
        self.assertFalse(out["funded"])
        self.assertEqual(DW.balances(), [])

    def test_no_code_path_reads_the_legacy_dex_portfolio_for_balances(self):
        """The legacy pre-cutover dex economy (cash 10,720.77) must never
        seed the wallet. The claim is about CODE PATHS, so the assertion is
        about references: dex_wallet must not touch the DexPortfolio model
        or its table, and must not carry the legacy cash figure."""
        import inspect
        from lib import dex_wallet
        src = inspect.getsource(dex_wallet)
        self.assertNotIn("DexPortfolio", src)
        self.assertNotIn("dex_portfolio", src)
        self.assertNotIn("10720", src)
        self.assertNotIn("10_720", src)


if __name__ == "__main__":
    unittest.main()
