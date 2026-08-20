"""Phantom borrowing must be impossible. LEVERAGE IS NOT BORROWING.

A 50x perpetual posts ~2% margin, and it is tempting to read the other 98%
as a loan and charge interest on it. Nobody lent anything: the exposure
lives in the contract. The audit found the planning path doing the
opposite-side version of this — `borrow_cost_pct` billed stock-borrow to
any short non-crypto symbol, so a short INDEX FUTURE was paying to borrow
shares nobody located, and `funding_cost_pct` billed perpetual funding to
any crypto symbol, so a SPOT holding paid a contract mechanism it does not
have.

These tests pin the whole boundary in both directions: derivatives never
inherit financing, and real financing products keep theirs — because a
simulator that invents a fee loses money that was never spent, and one
that waives a real fee earns money that was never earned.
"""
import unittest

from lib import product_cost_profile as PCP
from lib.holding_cost_authority import holding_cost
from lib.transaction_costs import borrow_cost_pct, funding_cost_pct


class LeverageIsNotBorrowingTests(unittest.TestCase):

    def test_A_leverage_alone_creates_no_borrow_cost(self):
        """The forbidden inference, pinned at the profile layer: resolution
        ignores leverage entirely."""
        lo = PCP.profile_for("CRYPTO_PERP", leverage=1.0)
        hi = PCP.profile_for("CRYPTO_PERP", leverage=50.0)
        self.assertEqual(lo, hi,
                         "leverage changed the cost profile — the "
                         "forbidden inference is live")
        self.assertFalse(hi.actual_borrowing_required)

    def test_B_a_50x_perpetual_has_no_generic_borrowing_interest(self):
        q = holding_cost("BTC/USD", product="CRYPTO_PERP",
                         notional_usd=50_000.0, hours_held=24.0,
                         is_short=False, funding_rate_8h=0.0001)
        self.assertTrue(q.ok)
        self.assertEqual(q.kind, "FUNDING",
                         "a perpetual's carry came from something other "
                         "than funding")

    def test_C_the_perpetual_still_pays_real_trading_fees(self):
        """No overcorrection: derivative != free."""
        from lib.fee_authority import leg_fee
        q = leg_fee("BTC/USD", notional=16_000.0, price=64_000.0,
                    product="CRYPTO_PERP", venue="kraken_derivatives_us",
                    side="sell", exact_contract_count=25.0)
        self.assertTrue(q.ok)
        self.assertGreater(q.fee_usd, 0.0,
                           "the perpetual stopped paying maker/taker fees")

    def test_D_funding_can_be_positive_or_negative(self):
        paid = holding_cost("BTC/USD", product="CRYPTO_PERP",
                            notional_usd=10_000.0, hours_held=8.0,
                            is_short=False, funding_rate_8h=0.0001)
        received = holding_cost("BTC/USD", product="CRYPTO_PERP",
                                notional_usd=10_000.0, hours_held=8.0,
                                is_short=True, funding_rate_8h=0.0001)
        self.assertGreater(paid.amount_usd, 0.0)
        self.assertLess(received.amount_usd, 0.0,
                        "a short with positive funding did not RECEIVE it "
                        "— funding was modelled as interest, which is "
                        "always a cost")

    def test_E_funding_is_not_borrow_interest(self):
        """Distinct kinds, distinct signs, distinct mechanics."""
        q = holding_cost("BTC/USD", product="CRYPTO_PERP",
                         notional_usd=10_000.0, hours_held=8.0,
                         is_short=True, funding_rate_8h=0.0001)
        self.assertEqual(q.kind, "FUNDING")
        self.assertNotEqual(q.kind, "BORROW")

    def test_F_a_fixed_future_has_no_perpetual_funding(self):
        prof = PCP.profile_for(PCP.CRYPTO_FIXED_FUTURE)
        self.assertEqual(prof.funding_model, PCP.NOT_APPLICABLE)
        self.assertFalse(PCP.funding_applies(PCP.CRYPTO_FIXED_FUTURE))

    def test_G_a_cme_style_future_has_exchange_fees_but_no_borrow(self):
        prof = PCP.profile_for("INDEX_FUTURE")
        self.assertEqual(prof.exchange_fee_model, PCP.EXCHANGE_NFA_CLEARING)
        self.assertEqual(prof.clearing_fee_model, PCP.EXCHANGE_NFA_CLEARING)
        self.assertFalse(prof.actual_borrowing_required)
        self.assertEqual(prof.borrow_fee_model, PCP.NOT_APPLICABLE)

    def test_H_a_short_crypto_perpetual_borrows_no_coin(self):
        pct, src = borrow_cost_pct("BTC/USD", 24.0, is_short=True,
                                   product="CRYPTO_PERP")
        self.assertEqual(pct, 0.0)
        self.assertEqual(src, "not_applicable_for_product")

    def test_I_a_short_equity_future_borrows_no_shares(self):
        """THE DEMONSTRATED DEFECT. Before the fix this charged 50bps GC
        annualised on a contract that borrows nothing."""
        pct, src = borrow_cost_pct("ES=F", 24.0, is_short=True,
                                   product="INDEX_FUTURE")
        self.assertEqual(pct, 0.0)
        # And the legacy no-product path is also cured:
        pct2, src2 = borrow_cost_pct("ES=F", 24.0, is_short=True)
        self.assertEqual(pct2, 0.0)
        self.assertEqual(src2, "not_applicable_derivative")

    def test_J_a_real_equity_short_still_pays_borrow(self):
        """No overcorrection: real shares really are borrowed."""
        pct, src = borrow_cost_pct("GME", 24.0, is_short=True,
                                   product="EQUITY_SHORT")
        self.assertGreater(pct, 0.0)
        q = holding_cost("GME", product="EQUITY_SHORT", notional_usd=10_000.0,
                         hours_held=24.0, is_short=True)
        self.assertTrue(q.ok)
        self.assertEqual(q.kind, "BORROW")

    def test_K_spot_margin_long_may_be_financed(self):
        prof = PCP.profile_for(PCP.SPOT_MARGIN_LONG)
        self.assertTrue(prof.actual_borrowing_required)
        self.assertEqual(prof.financing_model, PCP.MARGIN_OPEN_PLUS_ROLLOVER)

    def test_L_spot_margin_short_may_pay_borrow_and_rollover(self):
        prof = PCP.profile_for(PCP.SPOT_MARGIN_SHORT)
        self.assertTrue(prof.actual_borrowing_required)
        self.assertEqual(prof.borrow_fee_model, PCP.STOCK_BORROW_ACCRUAL)

    def test_M_an_unknown_product_gets_unknown_not_a_fee(self):
        prof = PCP.profile_for("SOMETHING_NOBODY_CHARACTERISED")
        self.assertEqual(prof.actual_borrowing_required, PCP.UNKNOWN)
        self.assertFalse(PCP.borrowing_applies("SOMETHING_NOBODY_CHARACTERISED"))
        # And the carry authority REFUSES rather than assuming free or paid:
        q = holding_cost("X", product="SOMETHING_NOBODY_CHARACTERISED",
                         notional_usd=1_000.0, hours_held=1.0,
                         is_short=False)
        self.assertFalse(q.ok)

    def test_N_leverage_changes_margin_not_loan_principal(self):
        """50x vs 5x: same product, same cost surface. What leverage moves
        is margin and liquidation risk — nothing here creates principal."""
        for product in ("CRYPTO_PERP", "INDEX_FUTURE"):
            with self.subTest(product=product):
                self.assertEqual(PCP.profile_for(product, leverage=5.0),
                                 PCP.profile_for(product, leverage=50.0))

    def test_O_spot_crypto_pays_no_perpetual_funding(self):
        pct, src = funding_cost_pct("BTC/USD", 24.0, 0.0001, False,
                                    product="CRYPTO_SPOT")
        self.assertEqual(pct, 0.0)
        self.assertEqual(src, "not_applicable_for_product")


class ReconciliationNamesTheWrongCostModelTests(unittest.TestCase):
    """§16 — a model that charges borrow where the venue realized zero is
    WRONG_PRODUCT_COST_MODEL, not slippage, not unexplained noise."""

    def test_phantom_borrow_is_classified_not_absorbed(self):
        from lib.venue_reconciliation import classify_cost_mismatch
        out = classify_cost_mismatch(
            component="borrow_fees", model_usd=3.20, venue_usd=0.0,
            product="CRYPTO_PERPETUAL")
        self.assertEqual(out["classification"], "WRONG_PRODUCT_COST_MODEL")
        self.assertIn("does not exist for this product", out["detail"])

    def test_a_missing_real_fee_is_named_missing(self):
        from lib.venue_reconciliation import classify_cost_mismatch
        out = classify_cost_mismatch(
            component="exchange_fees", model_usd=0.0, venue_usd=1.55,
            product="EQUITY_INDEX_FUTURE")
        self.assertEqual(out["classification"], "MISSING_COST_CATEGORY")

    def test_an_ordinary_estimation_gap_stays_an_estimation_gap(self):
        from lib.venue_reconciliation import classify_cost_mismatch
        out = classify_cost_mismatch(
            component="maker_taker_fees", model_usd=0.24, venue_usd=0.26,
            product="CRYPTO_PERPETUAL")
        self.assertEqual(out["classification"], "ESTIMATION_GAP")


if __name__ == "__main__":
    unittest.main()
