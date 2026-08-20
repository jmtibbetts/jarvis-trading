"""Phase 6.4 — one economic event, one ledger authority, one charge.

WHAT WAS ACTUALLY WRONG. Two DEX books existed and neither was wrong on its
own terms:

    dex_wallet   DexBalance / DexFundingEvent — SOL and token ledger.
                 CONSULTED as an authority ("can you afford this?") by the
                 autotrade gate, the venue adapter and the exit, and then
                 NEVER DEBITED by any production path. `settle_swap_*` has
                 no production caller at all.
    dex_paper    DexPortfolio / DexPosition / DexTrade — the USD book, and
                 the only path that actually moves on entry and exit.

So the gate said "you hold 5 SOL, this costs 0.002, proceed" and the 5 SOL
never moved. The same wallet could authorise an unlimited number of
transactions forever, because a gate that never charges is not a ledger —
it is a permission slip that renews itself.

THE RULE NOW: the asset that pays a cost loses that asset. Gas is paid in
SOL, so a persisted wallet is debited exactly once per leg, atomically with
the position it pays for. USD net P&L ATTRIBUTES that cost — the value of
the SOL consumed — which is one expense expressed in the reporting unit, not
a second expense. Where no wallet exists there is no SOL to consume and the
USD book pays; `fee_settlement` names which happened, so the two are never
mistaken for one another.
"""
import unittest
from unittest.mock import patch

from lib import dex_paper as DP
from lib import dex_wallet as DW
from lib import provider_health as PH
from lib import solana_fees as SF


def fetch_at(micro_per_cu=1_000.0, *, capture=None):
    def go(method, params):
        if capture is not None:
            capture.append((method, params))
        if method == "getPriorityFeeEstimate":
            return {"priorityFeeEstimate": micro_per_cu}
        return [{"prioritizationFee": micro_per_cu}]
    return go


def _reset_book():
    from app.database import (DexBalance, DexFundingEvent, DexPortfolio,
                              DexPosition, DexTrade, get_db)
    with get_db() as db:
        db.query(DexTrade).delete()
        db.query(DexPosition).delete()
        db.query(DexPortfolio).delete()
        db.query(DexBalance).delete()
        db.query(DexFundingEvent).delete()
        db.commit()


def _fund(sol=5.0):
    DW.fund_wallet(DW.issue_test_fixture_grant(
        mint=DW.SOL_MINT, quantity=sol, reason="phase 6.4 fixture"))


def _open(mint="LedgerMint1", pool="LedgerPool1", size=1_000.0,
          fee_fetch=None):
    return DP.open_dex_position(
        mint=mint, symbol="LDG", pool_address=pool, dex="raydium",
        reserve_usd=500_000.0, price_usd=1.0, size_usd=size,
        sol_price_usd=200.0, fee_fetch=fee_fetch or fetch_at())


def _sol():
    return DW.balance(DW.SOL_MINT)["total"]


def _cash():
    from app.database import get_db
    with get_db() as db:
        return float(DP.get_portfolio(db).cash_usd or 0)


class TheAssetThatPaysLosesItTests(unittest.TestCase):
    """A + B + G + I. Gas is paid in SOL, so SOL must actually leave."""

    def setUp(self):
        _reset_book()
        _fund()

    def tearDown(self):
        _reset_book()

    def test_A_entry_consumes_the_actual_SOL_fee_exactly_once(self):
        before = _sol()
        opened = _open()
        self.assertNotIn("error", opened, opened)
        self.assertEqual(opened["fee_settlement"], DP.SOL_WALLET_PAYS)
        fee_sol = float(opened["network_fee_sol"])
        self.assertGreater(fee_sol, 0.0)
        self.assertAlmostEqual(before - _sol(), fee_sol, places=12)

    def test_B_exit_consumes_the_actual_SOL_fee_exactly_once(self):
        opened = _open()
        after_open = _sol()
        closed = DP.close_dex_position(
            opened["position_id"], price_usd=1.2, sol_price_usd=200.0,
            fee_fetch=fetch_at())
        self.assertNotIn("error", closed, closed)
        self.assertEqual(closed["fee_settlement"], DP.SOL_WALLET_PAYS)
        exit_fee_sol = float(closed["exit_network_fee_sol"])
        self.assertGreater(exit_fee_sol, 0.0)
        self.assertAlmostEqual(after_open - _sol(), exit_fee_sol, places=12)

    def test_G_the_two_legs_are_distinct_and_both_land(self):
        start = _sol()
        opened = _open()
        closed = DP.close_dex_position(
            opened["position_id"], price_usd=1.2, sol_price_usd=200.0,
            fee_fetch=fetch_at())
        entry_sol = float(opened["network_fee_sol"])
        exit_sol = float(closed["exit_network_fee_sol"])
        self.assertAlmostEqual(start - _sol(), entry_sol + exit_sol, places=12)
        # And they are separately answerable in USD too.
        self.assertGreater(closed["entry_network_fee_usd"], 0.0)
        self.assertGreater(closed["exit_network_fee_usd"], 0.0)

    def test_I_SOL_conservation_holds_across_a_round_trip(self):
        """Nothing appears, nothing vanishes twice."""
        start = _sol()
        opened = _open()
        closed = DP.close_dex_position(
            opened["position_id"], price_usd=1.2, sol_price_usd=200.0,
            fee_fetch=fetch_at())
        spent = start - _sol()
        accounted = (float(opened["network_fee_sol"])
                     + float(closed["exit_network_fee_sol"]))
        self.assertAlmostEqual(spent, accounted, places=12)

    def test_the_gate_can_no_longer_authorise_forever(self):
        """THE DEFECT, stated directly: a gate that never charges.

        With a tiny wallet, repeated entries must eventually be refused
        because the SOL is really being consumed. Before this phase the
        same balance authorised an unlimited number of transactions.
        """
        _reset_book()
        # 0.000015 SOL operability floor plus 0.0000054 per entry leg, so
        # this funds a handful of entries and no more. If the gate were
        # still not charging, every attempt would succeed.
        _fund(sol=0.00005)
        opened_count = 0
        for i in range(30):
            out = _open(mint=f"Drain{i}", pool=f"DrainPool{i}")
            if "error" in out:
                break
            opened_count += 1
        self.assertGreater(opened_count, 0, "the fixture funded nothing")
        self.assertLess(opened_count, 30,
                        "the wallet authorised every attempt — the gate is "
                        "still not charging")
        self.assertEqual(out["error"], "entry_insufficient_gas",
                         "the wallet ran dry for some other reason")


class UsdAttributionIsNotASecondFeeTests(unittest.TestCase):
    """C + D. One expense, expressed in the reporting unit."""

    def setUp(self):
        _reset_book()
        _fund()

    def tearDown(self):
        _reset_book()

    def test_C_usd_pnl_reflects_the_value_of_the_SOL_fee_exactly_once(self):
        opened = _open()
        closed = DP.close_dex_position(
            opened["position_id"], price_usd=1.2, sol_price_usd=200.0,
            fee_fetch=fetch_at())
        rebuilt = (closed["proceeds_usd"] - opened["notional_usd"]
                   - closed["entry_network_fee_usd"]
                   - closed["exit_network_fee_usd"])
        self.assertAlmostEqual(closed["net_pnl_usd"], rebuilt, places=6)
        self.assertLess(closed["net_pnl_usd"], closed["gross_pnl_usd"])

    def test_D_usd_cash_is_not_charged_a_second_duplicate_fee(self):
        """The SOL wallet paid. Cash must not pay for the same lamports."""
        before = _cash()
        opened = _open()
        after_open = _cash()
        # Cash fell by the NOTIONAL only — the gas left the SOL wallet.
        self.assertAlmostEqual(before - after_open, opened["notional_usd"],
                               places=6)

        closed = DP.close_dex_position(
            opened["position_id"], price_usd=1.2, sol_price_usd=200.0,
            fee_fetch=fetch_at())
        # Cash rose by the PROCEEDS only, for the same reason.
        self.assertAlmostEqual(_cash() - after_open, closed["proceeds_usd"],
                               places=6)

    def test_D2_without_a_wallet_the_usd_book_pays_and_says_so(self):
        """No SOL to consume, so the cost still lands — on the only pool."""
        _reset_book()                      # no wallet funded
        before = _cash()
        opened = _open()
        self.assertEqual(opened["fee_settlement"], DP.USD_BOOK_PAYS)
        self.assertAlmostEqual(
            before - _cash(),
            opened["notional_usd"] + float(opened["network_fee_usd"]),
            places=6)

    def test_D3_the_two_settlement_modes_are_never_confused(self):
        self.assertNotEqual(DP.SOL_WALLET_PAYS, DP.USD_BOOK_PAYS)
        opened = _open()
        self.assertEqual(opened["fee_settlement"], DP.SOL_WALLET_PAYS)


class RefusalAndFailureAccountingTests(unittest.TestCase):
    """E + F + M. A refusal is free; a chain failure is not."""

    def setUp(self):
        _reset_book()
        _fund()

    def tearDown(self):
        _reset_book()

    def test_E_a_rejection_before_the_chain_consumes_no_SOL(self):
        before = _sol()
        cash_before = _cash()
        # Impact cap refuses this outright, after the fee was measured.
        out = DP.open_dex_position(
            mint="ThinPool", symbol="THIN", pool_address="P", dex="raydium",
            reserve_usd=5_000.0, price_usd=1.0, size_usd=1_000.0,
            sol_price_usd=200.0, fee_fetch=fetch_at())
        self.assertIn("error", out)
        self.assertIn("pool", out["error"].lower())
        self.assertAlmostEqual(_sol(), before, places=12)
        self.assertAlmostEqual(_cash(), cash_before, places=6)

    def test_E2_an_unpriceable_exit_consumes_no_SOL(self):
        opened = _open()
        before = _sol()
        with patch("lib.dex_swap_math.quote_swap",
                   return_value={"ok": False, "reason": "no route"}):
            out = DP.close_dex_position(
                opened["position_id"], price_usd=1.2, sol_price_usd=200.0,
                fee_fetch=fetch_at())
        self.assertEqual(out["error"], "exit_unpriceable")
        self.assertAlmostEqual(_sol(), before, places=12)

    def test_F_a_failure_after_reaching_the_chain_may_consume_SOL(self):
        before = _sol()
        out = DW.settle_swap_failure(network_fee_sol=0.0009,
                                     reached_chain=True, reason="on-chain")
        self.assertEqual(out["status"], DW.FAILED_ON_CHAIN)
        self.assertAlmostEqual(before - _sol(), 0.0009, places=12)

    def test_M_insufficient_persisted_SOL_blocks_settlement(self):
        _reset_book()
        _fund(sol=0.0000001)
        opened = _open()
        self.assertIn("error", opened,
                      "an entry settled with a wallet that cannot pay gas")
        self.assertEqual(opened["error"], "entry_insufficient_gas")
        self.assertEqual(opened["state"], "ENTRY_REFUSED_INSUFFICIENT_GAS")

    def test_a_fee_larger_than_the_wallet_raises_rather_than_clamping(self):
        _reset_book()
        _fund(sol=0.001)
        with self.assertRaises(DW.FeeAccountingInvariant) as ctx:
            DW.charge_network_fee(network_fee_sol=5.0, leg="ENTRY")
        detail = ctx.exception.detail
        self.assertEqual(detail["actual_network_fee_sol"], 5.0)
        self.assertGreater(detail["shortfall_sol"], 0.0)
        self.assertAlmostEqual(_sol(), 0.001, places=12,
                               msg="a refused fee still moved the balance")


class TokenConservationAndBookAgreementTests(unittest.TestCase):
    """H + J + K + L."""

    def setUp(self):
        _reset_book()
        _fund()

    def tearDown(self):
        _reset_book()

    def test_H_the_wallet_conserves_tokens_across_a_settled_swap(self):
        DW.fund_wallet(DW.issue_test_fixture_grant(
            mint=DW.USDC_MINT, quantity=1_000.0, reason="conservation"))
        usdc_before = DW.balance(DW.USDC_MINT)["total"]
        sol_before = _sol()
        out = DW.settle_swap_success(
            input_mint=DW.USDC_MINT, input_qty=250.0,
            output_mint="TokenMintZZZ11111111111111111111111111111",
            output_qty=99.0, network_fee_sol=0.0004)
        self.assertEqual(out["status"], DW.SETTLED)
        self.assertAlmostEqual(usdc_before - DW.balance(DW.USDC_MINT)["total"],
                               250.0, places=9)
        self.assertAlmostEqual(
            DW.balance("TokenMintZZZ11111111111111111111111111111")["total"],
            99.0, places=9)
        self.assertAlmostEqual(sol_before - _sol(), 0.0004, places=12)

    def test_J_the_trade_row_agrees_with_the_settlement(self):
        from app.database import DexTrade, get_db
        opened = _open()
        closed = DP.close_dex_position(
            opened["position_id"], price_usd=1.2, sol_price_usd=200.0,
            fee_fetch=fetch_at())
        with get_db() as db:
            trade = db.query(DexTrade).filter(
                DexTrade.position_id == opened["position_id"]).first()
            self.assertAlmostEqual(float(trade.net_pnl_usd),
                                   closed["net_pnl_usd"], places=6)
            self.assertAlmostEqual(
                float(trade.network_fees_usd),
                closed["entry_network_fee_usd"]
                + closed["exit_network_fee_usd"], places=6)

    def test_K_the_two_books_cannot_disagree_about_the_same_fee(self):
        """One event: the SOL that left equals the USD that was attributed."""
        sol_before = _sol()
        opened = _open()
        closed = DP.close_dex_position(
            opened["position_id"], price_usd=1.2, sol_price_usd=200.0,
            fee_fetch=fetch_at())
        sol_spent = sol_before - _sol()
        usd_attributed = (closed["entry_network_fee_usd"]
                          + closed["exit_network_fee_usd"])
        # The USD figure is the SOL consumed, valued at the same price the
        # quote used. Same event, two units — never two events.
        self.assertAlmostEqual(usd_attributed, sol_spent * 200.0, places=6)

    def test_L_caller_balances_cannot_create_wallet_authority(self):
        from lib.execution_venue import VirtualDexAdapter

        class _Plan:
            symbol = "TOK/SOL"; side = "long"; qty = 1.0
            entry = 1.0; notional = 1.0
            quantity_unit = "TOKENS"; instrument_id = None
            order_type = "market"

        _reset_book()                      # no wallet at all
        sub = VirtualDexAdapter().submit(
            _Plan(), reserve_usd=500_000.0, sol_price_usd=200.0,
            gas_balance_sol=99.0, fee_fetch=fetch_at())
        self.assertFalse(sub.accepted)
        self.assertEqual(sub.reason, "WALLET_NOT_FUNDED")
        self.assertEqual(DW.balances(), [])


class ValuationCannotBecomeTruthTests(unittest.TestCase):
    """N + O + P + Q. Display valuation is not learning truth."""

    def setUp(self):
        _reset_book()
        _fund()

    def tearDown(self):
        _reset_book()

    def test_O_the_static_valuation_fee_cannot_move_executable_equity(self):
        """THE DISPOSITION OF STATIC_VALUATION_DEFAULT, proven.

        Gas is paid in SOL from the wallet and is NOT deducted from the
        pool's output, so the static per-row network fee is display only.
        Move the static constant by four orders of magnitude and the
        economic/risk/learning authority must not budge.
        """
        _open()
        base = DP.summary(sol_price_usd=200.0)
        with patch("lib.dex_swap_math.DEFAULT_PRIORITY_LAMPORTS", 900_000_000):
            moved = DP.summary(sol_price_usd=200.0)
        self.assertEqual(base["equity_executable_usd"],
                         moved["equity_executable_usd"])
        self.assertEqual(base["open_value_executable_usd"],
                         moved["open_value_executable_usd"])
        # The DISPLAY figure does move, which is what makes it a display
        # figure rather than a constant nobody reads.
        self.assertNotEqual(
            base["positions_valuation"][0]["current_exit_network_fee_usd"],
            moved["positions_valuation"][0]["current_exit_network_fee_usd"])

    def test_O2_valuation_labels_itself_as_a_static_default(self):
        from app.database import DexPosition, get_db
        _open()
        with get_db() as db:
            pos = db.query(DexPosition).filter(
                DexPosition.status == "Open").first()
            q = DP.exit_quote(pos, price_usd=1.2, sol_price_usd=200.0)
        self.assertEqual(q["network_fee_source"], "STATIC_VALUATION_DEFAULT")

    def test_P_valuation_triggers_no_provider_requests(self):
        """A UI refresh must not become a provider storm."""
        _open()
        calls = []

        def tripwire(method, params):
            calls.append(method)
            raise AssertionError("summary() reached a provider")

        with patch.object(SF, "_default_fetch", tripwire):
            body = DP.summary(sol_price_usd=200.0)
        self.assertEqual(calls, [], "valuation called a provider")
        self.assertGreater(body["equity_executable_usd"], 0.0)

    def test_N_pool_and_gas_executability_are_separate_states(self):
        _open()
        body = DP.summary(sol_price_usd=200.0)
        self.assertIn("gas", body)
        self.assertIn("open_value_executable_usd", body)
        self.assertEqual(body["gas"]["basis"], "STATIC_POLICY_ONLY")
        self.assertTrue(body["gas"]["wallet_present"])
        self.assertFalse(body["gas_blocked"])

    def test_N2_a_gas_blocked_book_keeps_its_pool_value_visible(self):
        """UNKNOWN and BLOCKED are not zero."""
        _open()
        from app.database import DexBalance, get_db
        with get_db() as db:
            db.query(DexBalance).filter(
                DexBalance.mint == DW.SOL_MINT).update(
                {"total_quantity": 0.0})
            db.commit()
        body = DP.summary(sol_price_usd=200.0)
        self.assertTrue(body["gas_blocked"])
        # The pool value is still REPORTED, not zeroed...
        self.assertGreater(body["open_value_executable_usd"], 0.0)
        # ...but it is not claimed as reachable today.
        self.assertLess(body["executable_after_all_constraints_usd"],
                        body["equity_executable_usd"])
        self.assertGreater(body["gas_blocked_pool_value_usd"], 0.0)

    def test_Q_measured_fee_provenance_is_preserved_on_settlement(self):
        opened = _open()
        closed = DP.close_dex_position(
            opened["position_id"], price_usd=1.2, sol_price_usd=200.0,
            fee_fetch=fetch_at())
        cost = closed["network_cost"]
        self.assertEqual(cost["estimate_quality"], SF.MEASURED_HELIUS)
        self.assertEqual(cost["estimate_context_quality"],
                         SF.CONTEXT_LOCAL_ACCOUNTS)
        self.assertEqual(closed["network_fee_source"], "AUTHORIZED_BID")


class TelemetryMayBeLostButNotInvisibleTests(unittest.TestCase):
    """R + S + T."""

    def setUp(self):
        PH.reset_dropped_writes()

    def tearDown(self):
        PH.reset_dropped_writes()

    def test_R_a_health_write_cannot_block_execution_for_thirty_seconds(self):
        """The 250ms ceiling is asserted on the code that sets it."""
        import inspect
        src = inspect.getsource(PH.record)
        self.assertIn("busy_timeout = 250", src)

    def test_S_a_dropped_health_write_is_counted_not_swallowed(self):
        self.assertEqual(PH.dropped_writes()["count"], 0)
        import app.database as DBMOD
        with patch.object(DBMOD, "get_db",
                          side_effect=RuntimeError("db busy")):
            PH.record("probe_provider", "probe_capability", status="HEALTHY")
        drops = PH.dropped_writes()
        self.assertEqual(drops["count"], 1)
        self.assertTrue(drops["any_dropped"])
        self.assertIsNotNone(drops["last_at"])
        self.assertIn("probe_provider/probe_capability", drops["by_provider"])

    def test_S2_the_drop_counter_reaches_the_existing_ops_surface(self):
        from app.routers.platform import providers_health
        body = providers_health()
        self.assertIn("dropped_health_writes", body)
        self.assertIn("count", body["dropped_health_writes"])
        # Not a second monitoring architecture: it rides the existing one.
        self.assertIn("solana_fee_estimator", body)

    def test_S3_a_dropped_write_never_raises_into_the_caller(self):
        import app.database as DBMOD
        with patch.object(DBMOD, "get_db",
                          side_effect=RuntimeError("db busy")):
            out = PH.record("probe2", "cap2", status="HEALTHY")
        self.assertIsInstance(out, dict)

    def test_T_fallback_success_still_cannot_hide_primary_failure(self):
        from app.database import ProviderHealth, get_db
        with get_db() as db:
            db.query(ProviderHealth).filter(
                ProviderHealth.provider.in_(
                    [SF.PRIMARY_PROVIDER, SF.FALLBACK_PROVIDER])).delete(
                synchronize_session=False)
            db.commit()

        def primary_down(method, params):
            if method == "getPriorityFeeEstimate":
                raise RuntimeError("helius down")
            return [{"prioritizationFee": 1_000.0}]

        for _ in range(4):
            est = SF.estimate_network_fee(SF.NORMAL, fetch=primary_down)
            self.assertTrue(est.ok, "the fallback should still serve")
        health = SF.fee_estimator_health()
        self.assertTrue(health["primary_never_succeeded"])
        self.assertTrue(health["fallback_is_masking_a_dead_primary"])
        self.assertTrue(health["actionable"])

        with get_db() as db:
            db.query(ProviderHealth).filter(
                ProviderHealth.provider.in_(
                    [SF.PRIMARY_PROVIDER, SF.FALLBACK_PROVIDER])).delete(
                synchronize_session=False)
            db.commit()


class StructuralGuaranteesTests(unittest.TestCase):
    """U + V + W + X."""

    def setUp(self):
        _reset_book()
        _fund()

    def tearDown(self):
        _reset_book()

    def test_U_no_provider_io_happens_inside_an_economic_write(self):
        """An independent write must succeed DURING measurement."""
        from app.database import ProviderHealth, get_db
        seen = {}
        seq = {"n": 0}

        def probing_fetch(method, params):
            if "ok" not in seen:
                seq["n"] += 1
                try:
                    with get_db() as other:
                        other.add(ProviderHealth(
                            provider="p64_probe",
                            capability=f"probe_{seq['n']}",
                            status="HEALTHY", success_count=0,
                            failure_count=0, consecutive_failures=0))
                        other.commit()
                    seen["ok"] = True
                except Exception as exc:              # noqa: BLE001
                    seen["ok"] = False
                    seen["err"] = str(exc)
            if method == "getPriorityFeeEstimate":
                return {"priorityFeeEstimate": 1_000.0}
            return [{"prioritizationFee": 1_000.0}]

        opened = _open(fee_fetch=probing_fetch)
        self.assertTrue(seen.get("ok"),
                        f"write lock held during ENTRY: {seen.get('err')}")
        seen.clear()
        DP.close_dex_position(opened["position_id"], price_usd=1.2,
                              sol_price_usd=200.0, fee_fetch=probing_fetch)
        self.assertTrue(seen.get("ok"),
                        f"write lock held during EXIT: {seen.get('err')}")
        with get_db() as db:
            db.query(ProviderHealth).filter(
                ProviderHealth.provider == "p64_probe").delete()
            db.commit()

    def test_V_ordinary_pytest_remains_hermetic(self):
        import os
        self.assertEqual(os.getenv("JARVIS_UNDER_PYTEST"), "1")
        with self.assertRaises(RuntimeError) as ctx:
            SF._default_fetch("getPriorityFeeEstimate", [{}])
        self.assertIn("hermetic test", str(ctx.exception))

    def test_W_learning_never_recomputes_execution_economics(self):
        import inspect

        from lib import canonical_learning, realized_outcome
        for mod in (canonical_learning, realized_outcome):
            src = inspect.getsource(mod)
            for forbidden in ("estimate_network_fee", "authorize_fee",
                              "price_transaction", "quote_swap",
                              "charge_network_fee", "exit_quote"):
                with self.subTest(module=mod.__name__, forbidden=forbidden):
                    self.assertNotIn(forbidden, src)

    def test_X_the_canonical_tests_run_against_a_throwaway_database(self):
        from app.database import DB_PATH
        self.assertIn("jarvis-test-db", str(DB_PATH),
                      "pytest is not isolated from the operator database")


if __name__ == "__main__":
    unittest.main()
