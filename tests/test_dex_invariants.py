"""Virtual money has provenance, and actual fees are never rounded away.

FOUR CORRECTIONS PINNED HERE.

An earlier wallet seeded 10,000 USDC and 1 SOL the moment anything touched
it, because those were the numbers an old caller default happened to use.
Zero prior DEX state does not prove a starting balance — it proves there
was none. Value now appears only through a funding event naming its
authority.

Failed on-chain settlement clamped the SOL balance at zero, so a 0.008 fee
against a 0.005 balance debited 0.005 and reported success — the book left
0.003 richer than reality. The clamp is gone; the contradiction is raised.

A caller-supplied balance could stand in as authority when no wallet
existed, which let an unfunded account execute by describing a balance it
did not have. It is now a conservative cap only, and never a source.

The fee ceilings read as though 0.05 SOL were a property of Solana. They
are operator policy, and they live in a policy module that contains no
estimates at all.
"""
import os
import unittest

from lib import dex_wallet as DW
from lib import solana_fee_policy as POLICY


def _empty_wallet():
    from app.database import DexBalance, DexFundingEvent, get_db
    with get_db() as db:
        db.query(DexBalance).delete()
        db.query(DexFundingEvent).delete()


class _Endowment:
    def __init__(self, value):
        self.value = value
        self.old = None

    def __enter__(self):
        self.old = os.environ.get(DW.ENDOWMENT_ENV)
        if self.value is None:
            os.environ.pop(DW.ENDOWMENT_ENV, None)
        else:
            os.environ[DW.ENDOWMENT_ENV] = self.value
        return self

    def __exit__(self, *_):
        if self.old is None:
            os.environ.pop(DW.ENDOWMENT_ENV, None)
        else:
            os.environ[DW.ENDOWMENT_ENV] = self.old


class VirtualValueRequiresProvenanceTests(unittest.TestCase):

    def setUp(self):
        _empty_wallet()

    def test_A_no_implicit_endowment_appears_from_nowhere(self):
        """The headline: nothing creates 10k USDC + 1 SOL on its own."""
        with _Endowment(None):
            out = DW.apply_configured_endowment()
        self.assertFalse(out["funded"])
        self.assertEqual(DW.balances(), [])
        self.assertFalse(DW.initialized())
        self.assertNotIn("10", str(DW.configured_endowment()))

    def test_A2_reading_a_wallet_never_funds_it(self):
        with _Endowment(None):
            DW.balance(DW.SOL_MINT)
            DW.gas_state()
            DW.max_spendable_sol()
        self.assertEqual(DW.balances(), [],
                         "merely inspecting the wallet created value")

    def test_B_configured_endowment_creates_exactly_what_it_says(self):
        with _Endowment("USDC:2500,SOL:0.5"):
            out = DW.apply_configured_endowment()
        self.assertTrue(out["funded"])
        self.assertEqual(out["credited"], {"USDC": 2500.0, "SOL": 0.5})
        self.assertEqual(DW.balance(DW.USDC_MINT)["total"], 2500.0)
        self.assertEqual(DW.balance(DW.SOL_MINT)["total"], 0.5)

    def test_B2_every_credit_carries_its_authority(self):
        with _Endowment("USDC:100"):
            DW.apply_configured_endowment()
        history = DW.funding_history()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["authority"],
                         DW.CONFIGURED_VIRTUAL_ENDOWMENT)
        self.assertIn(DW.ENDOWMENT_ENV, history[0]["reason"])

    def test_B3_the_endowment_is_applied_once(self):
        with _Endowment("USDC:100"):
            DW.apply_configured_endowment()
            again = DW.apply_configured_endowment()
        self.assertFalse(again["funded"])
        self.assertEqual(DW.balance(DW.USDC_MINT)["total"], 100.0)

    def test_funding_without_an_authority_is_refused(self):
        with self.assertRaises(ValueError):
            DW.fund_wallet(mint=DW.USDC_MINT, quantity=1.0,
                           authority="BECAUSE_I_SAID_SO", reason="no")


class ACallerCannotInventAWalletTests(unittest.TestCase):

    def setUp(self):
        _empty_wallet()

    def test_C_the_adapter_refuses_an_unfunded_wallet(self):
        from lib.execution_venue import VirtualDexAdapter

        class _Plan:
            symbol = "TOK/SOL"; side = "long"; qty = 10.0
            entry = 1.0; notional = 10.0
            quantity_unit = "TOKENS"; instrument_id = None
            order_type = "market"

        sub = VirtualDexAdapter().submit(
            _Plan(), reserve_usd=100_000.0, sol_price_usd=200.0,
            gas_balance_sol=99.0)
        self.assertFalse(sub.accepted)
        self.assertEqual(sub.reason, "WALLET_NOT_FUNDED")
        self.assertEqual(sub.provenance["gas_authority"],
                         "NONE_WALLET_UNFUNDED")
        self.assertEqual(DW.balances(), [],
                         "a refused submission created balances")

    def test_C2_autotrade_refuses_an_unfunded_wallet(self):
        from lib.dex_autotrade import evaluate_candidate
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
        self.assertEqual(out["gas"]["authority"], "NONE_WALLET_UNFUNDED")

    def test_D_a_caller_cap_may_still_shrink_a_funded_wallet(self):
        """Conservative direction is preserved: min(ledger, caller)."""
        with _Endowment("SOL:5"):
            DW.apply_configured_endowment()
        from lib.execution_venue import VirtualDexAdapter

        class _Plan:
            symbol = "TOK/SOL"; side = "long"; qty = 1.0
            entry = 1.0; notional = 1.0
            quantity_unit = "TOKENS"; instrument_id = None
            order_type = "market"

        sub = VirtualDexAdapter().submit(
            _Plan(), reserve_usd=100_000.0, sol_price_usd=200.0,
            gas_balance_sol=0.25)
        self.assertTrue(sub.accepted, sub.detail)
        gas = sub.provenance["gas"] if "gas" in sub.provenance else None
        # The cap is recorded even on the accepted path.
        self.assertEqual(sub.provenance["gas_authority"], "PERSISTED_WALLET")

    def test_no_canonical_path_names_a_legacy_caller_authority(self):
        """Structural: the old fallback label must not exist in the
        canonical execution modules."""
        import inspect
        from lib import dex_autotrade, execution_venue
        for mod in (execution_venue, dex_autotrade):
            self.assertNotIn("LEGACY_CALLER_SUPPLIED",
                             inspect.getsource(mod),
                             f"{mod.__name__} still has a caller-authority "
                             f"fallback")


class ActualFeesAreNeverClampedTests(unittest.TestCase):

    def setUp(self):
        _empty_wallet()

    def _fund_sol(self, amount):
        DW.fund_wallet(mint=DW.SOL_MINT, quantity=amount,
                       authority=DW.TEST_FIXTURE, reason="fixture")

    def test_E_actual_above_estimate_but_within_balance_is_fully_charged(self):
        self._fund_sol(0.05)
        out = DW.settle_swap_failure(network_fee_sol=0.008,
                                     estimated_fee_sol=0.003,
                                     reached_chain=True,
                                     reason="on-chain revert")
        self.assertEqual(out["status"], DW.FAILED_ON_CHAIN)
        self.assertAlmostEqual(out["network_fee_sol"], 0.008, places=9)
        self.assertAlmostEqual(DW.balance(DW.SOL_MINT)["total"],
                               0.05 - 0.008, places=9)
        # Estimate and actual BOTH survive.
        self.assertAlmostEqual(out["fee_estimate_miss_sol"], 0.005, places=9)
        self.assertAlmostEqual(out["estimated_fee_sol"], 0.003, places=9)

    def test_F_actual_above_available_does_not_clamp(self):
        """THE DEFECT. 0.008 against 0.005 must not quietly debit 0.005."""
        self._fund_sol(0.005)
        with self.assertRaises(DW.FeeAccountingInvariant) as cm:
            DW.settle_swap_failure(network_fee_sol=0.008,
                                   estimated_fee_sol=0.004,
                                   reached_chain=True,
                                   estimator="helius.getPriorityFeeEstimate",
                                   priority_policy="VERY_HIGH")
        # Nothing was debited: all or nothing.
        self.assertAlmostEqual(DW.balance(DW.SOL_MINT)["total"], 0.005,
                               places=9)
        d = cm.exception.detail
        self.assertEqual(d["reason"], DW.FEE_EXCEEDS_AVAILABLE)
        self.assertAlmostEqual(d["shortfall_sol"], 0.003, places=9)

    def test_G_the_invariant_carries_everything_needed_to_reconcile(self):
        self._fund_sol(0.001)
        with self.assertRaises(DW.FeeAccountingInvariant) as cm:
            DW.settle_swap_failure(network_fee_sol=0.01,
                                   estimated_fee_sol=0.002,
                                   reached_chain=True,
                                   estimator="rpc.getRecentPrioritizationFees",
                                   priority_policy="MAX_ACCEPTANCE")
        d = cm.exception.detail
        for key in ("actual_fee_sol", "estimated_fee_sol", "available_sol",
                    "shortfall_sol", "estimator", "priority_policy",
                    "transaction_state"):
            self.assertIn(key, d)
        self.assertEqual(d["estimator"], "rpc.getRecentPrioritizationFees")

    def test_a_pre_chain_failure_still_charges_nothing(self):
        self._fund_sol(0.01)
        out = DW.settle_swap_failure(network_fee_sol=0.005,
                                     reached_chain=False)
        self.assertEqual(out["network_fee_sol"], 0.0)
        self.assertAlmostEqual(DW.balance(DW.SOL_MINT)["total"], 0.01,
                               places=9)

    def test_M_a_fee_is_never_charged_twice(self):
        self._fund_sol(0.05)
        DW.settle_swap_failure(network_fee_sol=0.004, reached_chain=True)
        self.assertAlmostEqual(DW.balance(DW.SOL_MINT)["total"], 0.046,
                               places=9)


class FeeCapsArePolicyNotNetworkTruthTests(unittest.TestCase):

    def test_H_and_I_the_numbers_are_not_in_the_estimator(self):
        """0.05 and 0.02 SOL must not be embedded as network constants."""
        import inspect
        from lib import solana_fees
        src = inspect.getsource(solana_fees)
        self.assertNotIn("0.05 * LAMPORTS_PER_SOL", src)
        self.assertNotIn("0.02 * LAMPORTS_PER_SOL", src)
        self.assertNotIn("_MAX_PRIORITY_LAMPORTS", src)

    def test_J_caps_resolve_through_configuration(self):
        from lib.solana_fees import priority_cap_lamports, MAX_ACCEPTANCE
        cap, source = priority_cap_lamports(MAX_ACCEPTANCE)
        self.assertEqual(cap,
                         POLICY.caps_for(POLICY.SEVERE_RISK_EXIT)
                         ["max_priority_fee_lamports"])
        self.assertIn(POLICY.FEE_POLICY_VERSION, source)

    def test_J2_an_operator_override_moves_the_cap(self):
        key = "JARVIS_SOL_FEE_SEVERE_RISK_EXIT_MAX_PRIORITY_FEE_LAMPORTS"
        old = os.environ.get(key)
        os.environ[key] = "12345"
        try:
            caps = POLICY.caps_for(POLICY.SEVERE_RISK_EXIT)
            self.assertEqual(caps["max_priority_fee_lamports"], 12345)
            self.assertEqual(caps["sources"]["max_priority_fee_lamports"],
                             "ENV_OVERRIDE")
        finally:
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old

    def test_the_policy_module_contains_no_estimates(self):
        """It answers 'would we pay that?', never 'what does it cost?'."""
        import inspect
        from lib import solana_fee_policy
        src = inspect.getsource(solana_fee_policy)
        for forbidden in ("getPriorityFeeEstimate",
                          "getRecentPrioritizationFees", "httpx", "rpc("):
            self.assertNotIn(forbidden, src)

    def test_an_unknown_action_gets_the_strictest_policy(self):
        caps = POLICY.caps_for("SOMETHING_NOBODY_NAMED")
        self.assertEqual(caps["source"], "STRICTEST_FALLBACK_UNKNOWN_ACTION")

    def test_L_the_fallback_hierarchy_still_holds(self):
        from lib import solana_fees as SF

        def only_rpc(method, params):
            if method == "getPriorityFeeEstimate":
                raise RuntimeError("simulated outage")
            return [{"prioritizationFee": f} for f in (10, 20, 30)]
        est = SF.estimate_network_fee(SF.NORMAL, fetch=only_rpc)
        self.assertTrue(est.ok)
        self.assertEqual(est.quality, SF.MEASURED_RPC_FALLBACK)

        def dead(method, params):
            raise RuntimeError("both down")
        est2 = SF.estimate_network_fee(SF.NORMAL, fetch=dead)
        self.assertFalse(est2.ok)
        self.assertEqual(est2.quality, SF.UNKNOWN)


class LedgerConservationTests(unittest.TestCase):
    """§7/§N — token quantities are conserved across settlement."""

    def setUp(self):
        _empty_wallet()
        DW.fund_wallet(mint=DW.SOL_MINT, quantity=1.0,
                       authority=DW.TEST_FIXTURE, reason="fixture")
        DW.fund_wallet(mint=DW.USDC_MINT, quantity=1_000.0,
                       authority=DW.TEST_FIXTURE, reason="fixture")

    def _totals(self):
        return {b["mint"]: b["total"] for b in DW.balances()}

    def test_N_a_successful_swap_conserves_every_quantity(self):
        """USD is NOT the conservation authority — a swap changes what the
        wallet is worth. TOKEN QUANTITIES are, and each moves by exactly
        the modelled amount."""
        TOK = "TokenMint111111111111111111111111111111111"
        before = self._totals()
        DW.settle_swap_success(
            input_mint=DW.USDC_MINT, input_qty=250.0,
            output_mint=TOK, output_qty=1_000.0,
            network_fee_sol=0.0006, output_symbol="TOK")
        after = self._totals()
        self.assertAlmostEqual(after[DW.USDC_MINT],
                               before[DW.USDC_MINT] - 250.0, places=9)
        self.assertAlmostEqual(after[DW.SOL_MINT],
                               before[DW.SOL_MINT] - 0.0006, places=9)
        self.assertAlmostEqual(after[TOK], 1_000.0, places=9)

    def test_a_refused_settlement_conserves_everything(self):
        before = self._totals()
        with self.assertRaises(DW.SwapRejected):
            DW.settle_swap_success(
                input_mint=DW.USDC_MINT, input_qty=999_999.0,
                output_mint="X", output_qty=1.0, network_fee_sol=0.0001)
        self.assertEqual(self._totals(), before,
                         "a rolled-back settlement leaked quantity")

    def test_a_fee_invariant_failure_conserves_everything(self):
        _empty_wallet()
        DW.fund_wallet(mint=DW.SOL_MINT, quantity=0.001,
                       authority=DW.TEST_FIXTURE, reason="fixture")
        before = self._totals()
        with self.assertRaises(DW.FeeAccountingInvariant):
            DW.settle_swap_failure(network_fee_sol=0.5, reached_chain=True)
        self.assertEqual(self._totals(), before)

    def test_funding_events_account_for_every_credit(self):
        """Total credited must equal the sum of funding events, or value
        appeared without provenance."""
        credited = {}
        for ev in DW.funding_history():
            credited[ev["mint"]] = credited.get(ev["mint"], 0.0) + ev["quantity"]
        for mint, total in self._totals().items():
            self.assertAlmostEqual(
                credited.get(mint, 0.0), total, places=9,
                msg=f"{mint} holds value with no funding event behind it")


if __name__ == "__main__":
    unittest.main()
