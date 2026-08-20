"""Phase 6.3 — the DEX EXIT measures its own network cost.

THE ASYMMETRY THIS CLOSES. Phase 6.2 wired the live fee authority into DEX
ENTRY and left the exit pricing gas from
`dex_swap_math.DEFAULT_PRIORITY_LAMPORTS`. A simulator built that way learns
a market that does not exist: expensive to get in, cheap to get out. It
flatters exactly the positions a real desk finds hardest to close, and every
RealizedOutcome built on it is contaminated.

A SECOND, LARGER DEFECT WAS FOUND WHILE CLOSING THE FIRST. In this USD book
the network fee was measured, stored on the position, reported in
`total_costs_usd` — and never charged to anything. Cash fell by the notional
alone on entry and rose by the proceeds alone on exit, so `net_pnl` omitted
BOTH network legs. The fee existed as evidence and not as money. It is now
charged exactly once per leg, and the two legs stay separately answerable.

THE BOT MUST NEVER LEARN PROFIT BECAUSE THE SIMULATOR OMITTED EXIT COSTS.
"""
import unittest
from unittest.mock import patch

from lib import dex_paper as DP
from lib import dex_wallet as DW
from lib import solana_fee_policy as POLICY
from lib import solana_fees as SF


def fetch_at(micro_per_cu=1_000.0, *, helius=True, capture=None):
    """A live-shaped estimator. Hermetic: never reaches real Helius."""
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


def _open(mint="ExitMint1", pool="ExitPool1", size=1_000.0, price=1.0,
          fee_fetch=None):
    return DP.open_dex_position(
        mint=mint, symbol="EXIT", pool_address=pool, dex="raydium",
        reserve_usd=500_000.0, price_usd=price, size_usd=size,
        sol_price_usd=200.0, fee_fetch=fee_fetch or fetch_at())


class ExitCallsTheDynamicFeeAuthorityTests(unittest.TestCase):
    """A + B + C + D. The wiring, and proof it is load-bearing."""

    def setUp(self):
        _reset_book()

    def tearDown(self):
        # CLEAN UP AFTER ITSELF, not merely before. These tests create real
        # DexTrade rows, and a neighbouring contract test asserts on the
        # total row count — leaving rows behind made it fail depending on
        # execution order, which is a test-pollution bug and not a defect
        # in the code under test.
        _reset_book()

    def test_A_the_canonical_exit_invokes_the_dynamic_fee_authority(self):
        opened = _open()
        self.assertNotIn("error", opened, opened)
        calls = []
        closed = DP.close_dex_position(
            opened["position_id"], price_usd=1.2, sol_price_usd=200.0,
            fee_fetch=fetch_at(capture=calls))
        self.assertNotIn("error", closed, closed)
        self.assertTrue(any(m == "getPriorityFeeEstimate" for m, _ in calls),
                        "the canonical exit never asked the fee market")
        self.assertEqual(closed["network_cost"]["estimator"], SF.EST_HELIUS)
        self.assertEqual(closed["network_cost"]["estimate_quality"],
                         SF.MEASURED_HELIUS)

    def test_B_the_exit_quote_is_labelled_an_authorized_bid_not_a_default(self):
        opened = _open()
        closed = DP.close_dex_position(
            opened["position_id"], price_usd=1.2, sol_price_usd=200.0,
            fee_fetch=fetch_at())
        self.assertEqual(closed["network_fee_source"], "AUTHORIZED_BID")
        self.assertNotEqual(closed["network_fee_source"],
                            "STATIC_VALUATION_DEFAULT")

    def test_B2_valuation_is_labelled_a_static_default_and_says_so(self):
        """summary()/exit_quote() valuation is NOT a settlement price."""
        opened = _open()
        from app.database import DexPosition, get_db
        with get_db() as db:
            pos = db.query(DexPosition).filter(
                DexPosition.id == opened["position_id"]).first()
            q = DP.exit_quote(pos, price_usd=1.2, sol_price_usd=200.0)
        self.assertEqual(q["network_fee_source"], "STATIC_VALUATION_DEFAULT")

    def test_C_poisoning_the_static_path_does_not_break_the_canonical_exit(self):
        """THE DISCRIMINATION TEST for the exit."""
        class _Poison:
            def __int__(self):
                raise AssertionError(
                    "the canonical EXIT reached for the static "
                    "DEFAULT_PRIORITY_LAMPORTS instead of the measured fee")

            def __index__(self):
                return self.__int__()

        opened = _open()
        with patch("lib.dex_swap_math.DEFAULT_PRIORITY_LAMPORTS", _Poison()):
            closed = DP.close_dex_position(
                opened["position_id"], price_usd=1.2, sol_price_usd=200.0,
                fee_fetch=fetch_at())
        self.assertNotIn("error", closed, closed)
        self.assertGreater(closed["exit_network_fee_usd"], 0.0)

    def test_D_the_poison_does_fire_for_an_explicitly_legacy_caller(self):
        """Proves the poison is real rather than vacuously satisfied."""
        class _Poison:
            def __int__(self):
                raise AssertionError("legacy path used the static constant")

            def __index__(self):
                return self.__int__()

        opened = _open()
        from app.database import DexPosition, get_db
        with patch("lib.dex_swap_math.DEFAULT_PRIORITY_LAMPORTS", _Poison()):
            with get_db() as db:
                pos = db.query(DexPosition).filter(
                    DexPosition.id == opened["position_id"]).first()
                # The VALUATION path takes no authorized bid, so it is the
                # legacy consumer of the constant — and it must trip.
                with self.assertRaises(AssertionError):
                    DP.exit_quote(pos, price_usd=1.2, sol_price_usd=200.0)


class ExitActionPolicyTests(unittest.TestCase):
    """E + F + G + H + I + J + K. Which economics apply, and why."""

    def setUp(self):
        _reset_book()

    def tearDown(self):
        # CLEAN UP AFTER ITSELF, not merely before. These tests create real
        # DexTrade rows, and a neighbouring contract test asserts on the
        # total row count — leaving rows behind made it fail depending on
        # execution order, which is a test-pollution bug and not a defect
        # in the code under test.
        _reset_book()

    def _close_with(self, action, fetch=None, price=1.2):
        opened = _open()
        return DP.close_dex_position(
            opened["position_id"], price_usd=price, sol_price_usd=200.0,
            exit_action=action, fee_fetch=fetch or fetch_at())

    def test_E_a_normal_exit_uses_NORMAL_EXIT_policy(self):
        closed = self._close_with("NORMAL_EXIT")
        self.assertEqual(closed["exit_action"], POLICY.NORMAL_EXIT)
        self.assertEqual(closed["network_cost"]["action_policy"],
                         POLICY.NORMAL_EXIT)

    def test_F_an_urgent_exit_uses_URGENT_EXIT_policy(self):
        closed = self._close_with("URGENT_EXIT")
        self.assertEqual(closed["exit_action"], POLICY.URGENT_EXIT)
        # The alias spelled the other way resolves to the same policy.
        _reset_book()
        aliased = self._close_with("URGENT_RISK_EXIT")
        self.assertEqual(aliased["exit_action"], POLICY.URGENT_EXIT)

    def test_G_a_severe_exit_uses_SEVERE_RISK_EXIT_policy(self):
        closed = self._close_with("SEVERE_RISK_EXIT")
        self.assertEqual(closed["exit_action"], POLICY.SEVERE_RISK_EXIT)

    def test_H_the_priority_level_does_not_choose_the_economics(self):
        """A NORMAL_EXIT bidding HIGH keeps NORMAL_EXIT ceilings."""
        closed = self._close_with("NORMAL_EXIT")
        self.assertEqual(closed["exit_priority_level"], "HIGH")
        self.assertEqual(closed["exit_action"], POLICY.NORMAL_EXIT)
        est = SF.estimate_network_fee(SF.HIGH, fetch=fetch_at(),
                                      record_health=False)
        auth = SF.authorize_fee(est, action=POLICY.NORMAL_EXIT,
                                sol_price_usd=200.0)
        self.assertEqual(auth.operator_total_fee_limit_lamports,
                         POLICY.caps_for(POLICY.NORMAL_EXIT)
                         ["max_total_network_fee_lamports"])

    def test_H2_an_entry_and_an_exit_at_the_same_priority_differ_in_policy(self):
        est = SF.estimate_network_fee(SF.HIGH, fetch=fetch_at(),
                                      record_health=False)
        entry = SF.authorize_fee(est, action=POLICY.NORMAL_ENTRY,
                                 sol_price_usd=200.0)
        urgent = SF.authorize_fee(est, action=POLICY.URGENT_EXIT,
                                  sol_price_usd=200.0)
        self.assertEqual(entry.priority_level, urgent.priority_level)
        self.assertNotEqual(entry.action_policy, urgent.action_policy)
        self.assertGreater(urgent.operator_total_fee_limit_lamports,
                           entry.operator_total_fee_limit_lamports)

    def test_I_an_urgent_exit_over_policy_bids_the_ceiling_and_says_so(self):
        """Risk reduction may bid a bounded ceiling; it must not pretend."""
        # 50M micro-lamports/CU on 400k CU is 0.02 SOL, far past 0.0035.
        est = SF.estimate_network_fee(SF.VERY_HIGH, fetch=fetch_at(50_000_000.0),
                                      record_health=False)
        auth = SF.authorize_fee(est, action=POLICY.URGENT_EXIT,
                                sol_price_usd=200.0)
        self.assertTrue(auth.allowed)
        self.assertTrue(auth.bid_below_measured_requirement)
        self.assertLess(auth.authorized_bid_lamports,
                        est.measured_total_network_fee_lamports)
        self.assertEqual(auth.authorized_bid_lamports,
                         POLICY.caps_for(POLICY.URGENT_EXIT)
                         ["max_total_network_fee_lamports"])

    def test_I2_there_is_no_pay_whatever_it_takes_mode(self):
        for action in (POLICY.URGENT_EXIT, POLICY.SEVERE_RISK_EXIT):
            with self.subTest(action=action):
                est = SF.estimate_network_fee(
                    SF.MAX_ACCEPTANCE, fetch=fetch_at(900_000_000.0),
                    record_health=False)
                auth = SF.authorize_fee(est, action=action,
                                        sol_price_usd=200.0)
                self.assertLessEqual(
                    auth.authorized_bid_lamports,
                    POLICY.caps_for(action)["max_total_network_fee_lamports"])

    def test_J_a_normal_exit_fee_refusal_is_explicit(self):
        opened = _open()
        closed = DP.close_dex_position(
            opened["position_id"], price_usd=1.2, sol_price_usd=200.0,
            exit_action="NORMAL_EXIT", fee_fetch=dead_fetch)
        self.assertEqual(closed["error"], "exit_network_fee_refused")
        self.assertEqual(closed["state"], "EXIT_PENDING_FEE_REFUSED")
        self.assertEqual(closed["reason"], SF.FEE_ESTIMATE_UNKNOWN)

    def test_J2_a_refused_exit_leaves_the_position_open_and_the_book_intact(self):
        from app.database import DexPosition, get_db
        opened = _open()
        with get_db() as db:
            cash_before = float(DP.get_portfolio(db).cash_usd or 0)
        DP.close_dex_position(
            opened["position_id"], price_usd=1.2, sol_price_usd=200.0,
            fee_fetch=dead_fetch)
        with get_db() as db:
            pos = db.query(DexPosition).filter(
                DexPosition.id == opened["position_id"]).first()
            self.assertEqual(pos.status, "Open")
            self.assertAlmostEqual(float(DP.get_portfolio(db).cash_usd or 0),
                                   cash_before, places=9)

    def test_K_unknown_on_a_risk_exit_uses_only_the_bounded_fallback(self):
        est = SF.estimate_network_fee(SF.MAX_ACCEPTANCE, fetch=dead_fetch,
                                      record_health=False)
        self.assertEqual(est.quality, SF.UNKNOWN)
        auth = SF.authorize_fee(est, action=POLICY.SEVERE_RISK_EXIT,
                                sol_price_usd=200.0)
        self.assertTrue(auth.allowed)
        self.assertEqual(auth.binding_constraint, "EMERGENCY_FALLBACK")
        expected, _src = POLICY.emergency_fallback_lamports()
        self.assertEqual(auth.authorized_bid_lamports, expected)
        # UNKNOWN STAYS UNKNOWN: a bounded fallback is not a measurement.
        self.assertEqual(auth.quality, SF.UNKNOWN)
        self.assertIsNone(auth.measured_total_network_fee_lamports)

    def test_an_unrecognised_exit_action_is_refused(self):
        opened = _open()
        closed = DP.close_dex_position(
            opened["position_id"], price_usd=1.2, sol_price_usd=200.0,
            exit_action="PAY_WHATEVER_IT_TAKES", fee_fetch=fetch_at())
        self.assertIn("error", closed)
        self.assertIn("unknown exit action", closed["error"])

    def test_an_exit_cannot_borrow_entry_economics(self):
        opened = _open()
        closed = DP.close_dex_position(
            opened["position_id"], price_usd=1.2, sol_price_usd=200.0,
            exit_action="NORMAL_ENTRY", fee_fetch=fetch_at())
        self.assertIn("error", closed)


class WalletAuthorityOnExitTests(unittest.TestCase):
    """L + M + U. The fee payer must survive its own transaction."""

    def setUp(self):
        _reset_book()

    def tearDown(self):
        # CLEAN UP AFTER ITSELF, not merely before. These tests create real
        # DexTrade rows, and a neighbouring contract test asserts on the
        # total row count — leaving rows behind made it fail depending on
        # execution order, which is a test-pollution bug and not a defect
        # in the code under test.
        _reset_book()

    def test_L_a_funded_wallet_that_cannot_pay_the_bid_refuses_the_exit(self):
        opened = _open()
        # A real ledger holding far too little SOL to pay for a swap.
        DW.fund_wallet(DW.issue_test_fixture_grant(
            mint=DW.SOL_MINT, quantity=0.0000001, reason="exit gas fixture"))
        closed = DP.close_dex_position(
            opened["position_id"], price_usd=1.2, sol_price_usd=200.0,
            fee_fetch=fetch_at())
        self.assertEqual(closed["error"], "exit_insufficient_gas")
        self.assertEqual(closed["state"], "EXIT_PENDING_INSUFFICIENT_GAS")

    def test_U_the_exit_reserves_the_bid_plus_operability(self):
        """Selling the last SOL needed to execute the sale is not an exit."""
        est = SF.estimate_network_fee(SF.HIGH, fetch=fetch_at(5_000.0),
                                      record_health=False)
        auth = SF.authorize_fee(est, action=POLICY.NORMAL_EXIT,
                                sol_price_usd=200.0)
        DW.fund_wallet(DW.issue_test_fixture_grant(
            mint=DW.SOL_MINT, quantity=1.0, reason="exit reserve fixture"))
        gas = DW.gas_state(fee_authorization=auth)
        self.assertEqual(gas["reserve_basis"], "AUTHORIZED_FEE_BID")
        self.assertGreater(gas["future_operability_reserve_sol"], 0.0)
        self.assertAlmostEqual(
            gas["execution_reserve_sol"],
            gas["immediate_transaction_reserve_sol"]
            + gas["future_operability_reserve_sol"], places=12)
        self.assertLess(gas["max_spendable_sol"], gas["balance_sol"])

    def test_M_caller_gas_semantics_None_and_zero_stay_distinct(self):
        """Unchanged from Phase 6.2, asserted here so it cannot drift."""
        from lib.execution_venue import VirtualDexAdapter

        class _Plan:
            symbol = "TOK/SOL"; side = "long"; qty = 1.0
            entry = 1.0; notional = 1.0
            quantity_unit = "TOKENS"; instrument_id = None
            order_type = "market"

        DW.fund_wallet(DW.issue_test_fixture_grant(
            mint=DW.SOL_MINT, quantity=5.0, reason="caller cap fixture"))
        absent = VirtualDexAdapter().submit(
            _Plan(), reserve_usd=500_000.0, sol_price_usd=200.0,
            gas_balance_sol=None, fee_fetch=fetch_at())
        zero = VirtualDexAdapter().submit(
            _Plan(), reserve_usd=500_000.0, sol_price_usd=200.0,
            gas_balance_sol=0.0, fee_fetch=fetch_at())
        self.assertTrue(absent.accepted, absent.detail)
        self.assertFalse(zero.accepted)
        self.assertEqual(zero.reason, "INSUFFICIENT_GAS")


class ExitFeeIsChargedExactlyOnceTests(unittest.TestCase):
    """N + O + P + Q + R + S. The money, not just the evidence."""

    def setUp(self):
        _reset_book()

    def tearDown(self):
        # CLEAN UP AFTER ITSELF, not merely before. These tests create real
        # DexTrade rows, and a neighbouring contract test asserts on the
        # total row count — leaving rows behind made it fail depending on
        # execution order, which is a test-pollution bug and not a defect
        # in the code under test.
        _reset_book()

    def test_N_the_exit_network_fee_is_charged_exactly_once(self):
        from app.database import get_db
        opened = _open()
        with get_db() as db:
            cash_after_open = float(DP.get_portfolio(db).cash_usd or 0)
        closed = DP.close_dex_position(
            opened["position_id"], price_usd=1.2, sol_price_usd=200.0,
            fee_fetch=fetch_at())
        with get_db() as db:
            cash_after_close = float(DP.get_portfolio(db).cash_usd or 0)
        exit_fee = closed["exit_network_fee_usd"]
        self.assertGreater(exit_fee, 0.0)
        # Cash moved by proceeds MINUS exactly one exit fee.
        self.assertAlmostEqual(
            cash_after_close - cash_after_open,
            closed["proceeds_usd"] - exit_fee, places=6)

    def test_N2_the_entry_network_fee_is_charged_exactly_once(self):
        """The fee used to be stored and never paid by anything."""
        from app.database import DexPosition, get_db
        _reset_book()
        with get_db() as db:
            cash_before = float(DP.get_portfolio(db).cash_usd or 0)
        opened = _open(size=1_000.0)
        with get_db() as db:
            cash_after = float(DP.get_portfolio(db).cash_usd or 0)
            pos = db.query(DexPosition).filter(
                DexPosition.id == opened["position_id"]).first()
            entry_fee = float(pos.entry_network_fee_usd or 0)
        self.assertGreater(entry_fee, 0.0)
        self.assertAlmostEqual(cash_before - cash_after,
                               opened["notional_usd"] + entry_fee, places=6)

    def test_N3_net_pnl_pays_for_both_network_legs(self):
        """`proceeds - notional` omitted the chain entirely."""
        opened = _open()
        closed = DP.close_dex_position(
            opened["position_id"], price_usd=1.2, sol_price_usd=200.0,
            fee_fetch=fetch_at())
        rebuilt = (closed["proceeds_usd"] - opened["notional_usd"]
                   - closed["entry_network_fee_usd"]
                   - closed["exit_network_fee_usd"])
        self.assertAlmostEqual(closed["net_pnl_usd"], rebuilt, places=6)
        self.assertLess(closed["net_pnl_usd"], closed["gross_pnl_usd"])

    def test_O_a_pre_submit_refusal_charges_no_network_fee(self):
        from app.database import get_db
        opened = _open()
        with get_db() as db:
            before = float(DP.get_portfolio(db).cash_usd or 0)
        DP.close_dex_position(
            opened["position_id"], price_usd=1.2, sol_price_usd=200.0,
            fee_fetch=dead_fetch)
        with get_db() as db:
            self.assertAlmostEqual(float(DP.get_portfolio(db).cash_usd or 0),
                                   before, places=9)

    def test_O2_an_unpriceable_exit_still_charges_nothing(self):
        from app.database import get_db
        opened = _open()
        with get_db() as db:
            before = float(DP.get_portfolio(db).cash_usd or 0)
        with patch("lib.dex_swap_math.quote_swap",
                   return_value={"ok": False, "reason": "no route"}):
            out = DP.close_dex_position(
                opened["position_id"], price_usd=1.2, sol_price_usd=200.0,
                fee_fetch=fetch_at())
        self.assertEqual(out["error"], "exit_unpriceable")
        with get_db() as db:
            self.assertAlmostEqual(float(DP.get_portfolio(db).cash_usd or 0),
                                   before, places=9)

    def test_P_a_post_chain_failure_may_consume_the_actual_fee(self):
        """The wallet ledger's three outcomes are unchanged."""
        DW.fund_wallet(DW.issue_test_fixture_grant(
            mint=DW.SOL_MINT, quantity=0.5, reason="failure fixture"))
        out = DW.settle_swap_failure(network_fee_sol=0.0009,
                                     reached_chain=True,
                                     reason="slippage exceeded on-chain")
        self.assertEqual(out["status"], DW.FAILED_ON_CHAIN)
        self.assertAlmostEqual(DW.balance(DW.SOL_MINT)["total"],
                               0.5 - 0.0009, places=9)

    def test_P2_a_failure_before_the_chain_consumes_nothing(self):
        DW.fund_wallet(DW.issue_test_fixture_grant(
            mint=DW.SOL_MINT, quantity=0.5, reason="failure fixture"))
        out = DW.settle_swap_failure(network_fee_sol=0.0009,
                                     reached_chain=False, reason="pre-submit")
        self.assertEqual(out["status"], DW.FAILED_BEFORE_CHAIN)
        self.assertEqual(out["network_fee_sol"], 0.0)
        self.assertAlmostEqual(DW.balance(DW.SOL_MINT)["total"], 0.5, places=9)

    def test_Q_measured_authorized_and_actual_stay_separate_on_exit(self):
        opened = _open()
        closed = DP.close_dex_position(
            opened["position_id"], price_usd=1.2, sol_price_usd=200.0,
            fee_fetch=fetch_at())
        cost = closed["network_cost"]
        self.assertIsNotNone(cost["measured_total_network_fee_lamports"])
        self.assertIsNotNone(cost["authorized_network_fee_lamports"])
        # ACTUAL is the USD amount the book was charged, a third figure.
        self.assertGreater(closed["exit_network_fee_usd"], 0.0)
        self.assertIn("bid_below_measured_requirement", cost)

    def test_R_entry_and_exit_network_evidence_stay_separate(self):
        from app.database import DexTrade, get_db
        opened = _open()
        closed = DP.close_dex_position(
            opened["position_id"], price_usd=1.2, sol_price_usd=200.0,
            fee_fetch=fetch_at())
        self.assertIn("entry_network_fee_usd", closed)
        self.assertIn("exit_network_fee_usd", closed)
        with get_db() as db:
            trade = db.query(DexTrade).filter(
                DexTrade.position_id == opened["position_id"]).first()
            # The trade row keeps the entry leg and the combined total, so
            # the two halves stay answerable rather than merged beyond
            # recovery. Read inside the session that owns the instance.
            self.assertIsNotNone(trade)
            self.assertAlmostEqual(
                float(trade.network_fees_usd),
                closed["entry_network_fee_usd"]
                + closed["exit_network_fee_usd"], places=6)
            self.assertIsNotNone(trade.entry_impact_pct)
            self.assertIsNotNone(trade.exit_impact_pct)

    def test_S_the_closed_trade_carries_correct_net_economics(self):
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
            self.assertLess(float(trade.net_pnl_usd),
                            float(trade.gross_pnl_usd))
            self.assertGreater(float(trade.total_costs_usd), 0.0)


class LearningDoesNotRecomputeExitCostsTests(unittest.TestCase):
    """T. RealizedOutcome remains the learning authority."""

    def test_T_learning_never_recomputes_network_economics(self):
        import inspect

        from lib import canonical_learning, realized_outcome
        for mod in (canonical_learning, realized_outcome):
            src = inspect.getsource(mod)
            for forbidden in ("estimate_network_fee", "authorize_fee",
                              "price_transaction", "quote_swap",
                              "exit_quote", "close_dex_position"):
                with self.subTest(module=mod.__name__, forbidden=forbidden):
                    self.assertNotIn(forbidden, src,
                                     "learning recomputed an execution cost")


class ProviderIoStaysOutsideWriteTransactionsTests(unittest.TestCase):
    """V. Proven behaviourally, not by reading the source."""

    def setUp(self):
        _reset_book()

    def tearDown(self):
        # CLEAN UP AFTER ITSELF, not merely before. These tests create real
        # DexTrade rows, and a neighbouring contract test asserts on the
        # total row count — leaving rows behind made it fail depending on
        # execution order, which is a test-pollution bug and not a defect
        # in the code under test.
        _reset_book()

    def test_V_no_write_lock_is_held_while_the_fee_is_measured(self):
        """A SECOND connection must be able to write DURING measurement.

        This is the real invariant, and it is what makes the difference
        visible: if the exit held SQLite's write lock across the RPC, this
        independent write would block until the busy timeout instead of
        succeeding immediately.
        """
        from app.database import ProviderHealth, get_db

        observed = {}
        probe_seq = {"n": 0}

        def probing_fetch(method, params):
            if "wrote_during_measurement" not in observed:
                probe_seq["n"] += 1
                try:
                    with get_db() as other:
                        other.add(ProviderHealth(
                            provider="phase63_probe",
                            capability=f"write_probe_{probe_seq['n']}",
                            status="HEALTHY", success_count=0,
                            failure_count=0, consecutive_failures=0))
                        other.commit()
                    observed["wrote_during_measurement"] = True
                except Exception as exc:            # noqa: BLE001
                    observed["wrote_during_measurement"] = False
                    observed["error"] = str(exc)
            if method == "getPriorityFeeEstimate":
                return {"priorityFeeEstimate": 1_000.0}
            return [{"prioritizationFee": 1_000.0}]

        opened = _open(fee_fetch=probing_fetch)
        self.assertNotIn("error", opened, opened)
        self.assertTrue(observed.get("wrote_during_measurement"),
                        f"a write lock was held during ENTRY measurement: "
                        f"{observed.get('error')}")

        observed.clear()
        closed = DP.close_dex_position(
            opened["position_id"], price_usd=1.2, sol_price_usd=200.0,
            fee_fetch=probing_fetch)
        self.assertNotIn("error", closed, closed)
        self.assertTrue(observed.get("wrote_during_measurement"),
                        f"a write lock was held during EXIT measurement: "
                        f"{observed.get('error')}")

        with get_db() as db:
            db.query(ProviderHealth).filter(
                ProviderHealth.provider == "phase63_probe").delete()
            db.commit()


class HermeticAndNonExecutableTests(unittest.TestCase):
    """W + X + Y + Z."""

    def setUp(self):
        _reset_book()

    def tearDown(self):
        # CLEAN UP AFTER ITSELF, not merely before. These tests create real
        # DexTrade rows, and a neighbouring contract test asserts on the
        # total row count — leaving rows behind made it fail depending on
        # execution order, which is a test-pollution bug and not a defect
        # in the code under test.
        _reset_book()

    def test_W_an_ordinary_test_cannot_contact_live_helius(self):
        import os
        self.assertEqual(os.getenv("JARVIS_UNDER_PYTEST"), "1")
        self.assertNotEqual(os.getenv("JARVIS_REAL_PROVIDER_TESTS"), "1")
        with self.assertRaises(RuntimeError) as ctx:
            SF._default_fetch("getPriorityFeeEstimate", [{}])
        self.assertIn("hermetic test", str(ctx.exception))

    def test_X_unsafe_max_remains_non_executable(self):
        with self.assertRaises(SF.NonExecutablePriorityLevel):
            SF.assert_executable_level("UnsafeMax")
        # And the severe exit, which bids hardest, still does not select it.
        self.assertEqual(SF._HELIUS_LEVEL[SF.MAX_ACCEPTANCE], "VeryHigh")

    def test_Y_the_positions_real_accounts_reach_the_exit_estimator(self):
        calls = []
        opened = _open(mint="RealMintAAA", pool="RealPoolBBB")
        calls.clear()
        DP.close_dex_position(
            opened["position_id"], price_usd=1.2, sol_price_usd=200.0,
            fee_fetch=fetch_at(capture=calls))
        primary = [c for c in calls if c[0] == "getPriorityFeeEstimate"]
        self.assertTrue(primary)
        keys = primary[0][1][0]["accountKeys"]
        self.assertEqual(keys[0], "RealPoolBBB",
                         "the contended pool account comes first")
        self.assertIn("RealMintAAA", keys)

    def test_Y2_no_account_address_is_invented(self):
        opened = _open(mint="OnlyMint", pool=None)
        calls = []
        DP.close_dex_position(
            opened["position_id"], price_usd=1.2, sol_price_usd=200.0,
            fee_fetch=fetch_at(capture=calls))
        keys = [c for c in calls
                if c[0] == "getPriorityFeeEstimate"][0][1][0]["accountKeys"]
        self.assertNotIn(None, keys)
        self.assertIn("OnlyMint", keys)

    def test_Z_exit_context_provenance_is_truthful(self):
        opened = _open()
        closed = DP.close_dex_position(
            opened["position_id"], price_usd=1.2, sol_price_usd=200.0,
            fee_fetch=fetch_at())
        self.assertEqual(closed["network_cost"]["estimate_context_quality"],
                         SF.CONTEXT_LOCAL_ACCOUNTS)

    def test_Z2_a_local_fallback_is_never_labelled_global(self):
        est = SF.estimate_network_fee(
            SF.HIGH, writable_account_keys=["PoolX", "MintY"],
            fetch=fetch_at(helius=False), record_health=False)
        self.assertEqual(est.estimator, SF.EST_RPC_LOCAL)
        self.assertEqual(est.context_quality, SF.CONTEXT_LOCAL_ACCOUNTS)
        self.assertNotEqual(est.context_quality, SF.CONTEXT_GLOBAL)


if __name__ == "__main__":
    unittest.main()
