"""A paper position cannot lose more than the capital committed to it.

That is what liquidation means. The broker closes you out when margin is
exhausted; it does not hand you a bill for 440x the position.

This is the backstop for a units/contracts mismatch. `size_position` returns
CONTRACTS for futures and `_calc_pnl` multiplies by the contract multiplier,
so the two paths must agree about what `qty` means. When they disagree the
error is silent and enormous:

    HG=F  entry 6.6705  exit 6.6470   (-0.35% on copper)
    qty   749.57 = notional/price     <- units, the wrong convention
    pnl   -$440,371                   <- 749.57 x 0.0235 x 25,000

    correct: 0.03 contracts -> -$17.61

That single trade was 104% of the paper book's entire -$422,504 deficit and
drove cash to -$341,681, at which point `size_position` began refusing to
size every signal card in the UI for lack of equity.
"""
import unittest

from lib.paper_engine import _calc_pnl, LOSS_MISMATCH_FACTOR


class LossBoundTests(unittest.TestCase):
    def test_a_loss_never_exceeds_the_margin(self):
        """A contract-sized position that gaps far through its stop still
        cannot cost more than the capital committed."""
        pnl, _ = _calc_pnl(5000.0, 3000.0, 2.0, 1, 1.0, 1000.0, symbol="ES=F")
        self.assertEqual(pnl, -1000.0)

    def test_the_regression_number_specifically(self):
        """-$440,371 on a -0.35% move must not be representable."""
        pnl, _ = _calc_pnl(6.6705, 6.6470, 749.56902, 1, 5.0, 1000.0, symbol="HG=F")
        self.assertGreater(pnl, -1001.0)

    def test_pnl_pct_is_bounded_too(self):
        """-44,037% would poison every average in the learning data."""
        _, pct = _calc_pnl(5000.0, 3000.0, 2.0, 1, 1.0, 1000.0, symbol="ES=F")
        self.assertAlmostEqual(pct, -100.0)

    def test_a_short_is_bounded_the_same_way(self):
        pnl, _ = _calc_pnl(3000.0, 5000.0, 2.0, -1, 1.0, 1000.0, symbol="ES=F")
        self.assertEqual(pnl, -1000.0)


class CorrectSizingIsUntouchedTests(unittest.TestCase):
    """The bound is a backstop, not a substitute for sizing correctly."""

    def test_contract_sized_futures_price_normally(self):
        pnl, _ = _calc_pnl(6.6705, 6.6470, 0.03, 1, 5.0, 150.0, symbol="HG=F")
        self.assertAlmostEqual(pnl, -17.62, places=1)

    def test_the_multiplier_still_applies(self):
        """One point of ES is $50 per contract. Neither the bound nor the
        units detection may silently undo the multiplier for legitimate
        contract quantities."""
        pnl, _ = _calc_pnl(5000.0, 5010.0, 2.0, 1, 1.0, 500_000.0, symbol="ES=F")
        self.assertAlmostEqual(pnl, 10.0 * 2 * 50)

    def test_equities_are_unaffected(self):
        pnl, _ = _calc_pnl(100.0, 95.0, 10.0, 1, 1.0, 1000.0, symbol="AAPL")
        self.assertAlmostEqual(pnl, -50.0)

    def test_crypto_is_unaffected(self):
        pnl, _ = _calc_pnl(95_000.0, 94_000.0, 0.05, 1, 10.0, 1000.0, symbol="BTC/USD")
        self.assertAlmostEqual(pnl, -50.0)

    def test_profits_are_not_bounded(self):
        """Only losses are capped by margin — a winner has no such ceiling."""
        pnl, _ = _calc_pnl(100.0, 200.0, 100.0, 1, 1.0, 1000.0, symbol="AAPL")
        self.assertAlmostEqual(pnl, 10_000.0)


class UnitsVersusContractsTests(unittest.TestCase):
    """Reading the convention off the position beats bounding the result.

    Bounding losses alone was not enough: the same mismatch inflated the
    WINNERS, and a cap on losses leaves those untouched. Every futures
    position in the paper book was unit-sized while P&L multiplied anyway.

        SI=F  +0.07% move  ->  recorded +$17,028   true +$3.41
        HG=F  -0.35% move  ->  recorded -$440,371  true -$17.61

    Which convention a stored position used is recoverable from its own
    numbers, because notional is margin x leverage.
    """

    def test_a_unit_sized_loser_prices_correctly(self):
        pnl, _ = _calc_pnl(6.6705, 6.647, 749.56902, 1, 5.0, 1000.0, symbol="HG=F")
        self.assertAlmostEqual(pnl, -17.61, places=1)

    def test_a_unit_sized_winner_prices_correctly(self):
        """The direction a loss-only bound could never have fixed."""
        pnl, _ = _calc_pnl(66.06500244, 66.11000061, 75.6830, 1, 5.0, 1000.0, symbol="SI=F")
        self.assertAlmostEqual(pnl, 3.41, places=1)

    def test_a_stop_loss_cannot_produce_a_1700_percent_gain(self):
        _, pct = _calc_pnl(66.06500244, 66.11000061, 75.6830, 1, 5.0, 1000.0, symbol="SI=F")
        self.assertLess(abs(pct), 100.0)

    def test_contract_sized_futures_still_get_the_multiplier(self):
        """The detection must not disarm the multiplier for correct sizing."""
        pnl, _ = _calc_pnl(5000.0, 5010.0, 2.0, 1, 1.0, 500_000.0, symbol="ES=F")
        self.assertAlmostEqual(pnl, 1000.0)

    def test_ambiguous_data_keeps_the_declared_convention(self):
        """Only an EXACT units match disarms the multiplier. A heuristic
        that fires on a maybe would corrupt correctly-sized positions in
        order to fix incorrectly-sized ones."""
        pnl, _ = _calc_pnl(100.0, 90.0, 1, -1, 1, 1000, symbol="ES=F")
        self.assertAlmostEqual(pnl, 500.0)

    def test_non_futures_are_never_reinterpreted(self):
        """multiplier 1 means there is no ambiguity to resolve."""
        for sym, qty in (("AAPL", 10.0), ("BTC/USD", 0.05), ("SPY", 3.0)):
            pnl, _ = _calc_pnl(100.0, 95.0, qty, 1, 1.0, 1000.0, symbol=sym)
            self.assertAlmostEqual(pnl, -5.0 * qty, msg=sym)


class OrdinaryLiquidationTests(unittest.TestCase):
    """Losing exactly the margin is normal; the alarm is for the absurd."""

    def test_a_small_overshoot_still_bounds_without_being_a_unit_bug(self):
        pnl, _ = _calc_pnl(100.0, 95.0, 10.0, 1, 1.0, 45.0, symbol="AAPL")
        self.assertEqual(pnl, -45.0)

    def test_the_mismatch_threshold_is_above_one(self):
        """A position that gapped slightly through its stop is ordinary and
        must not be reported as a bug."""
        self.assertGreater(LOSS_MISMATCH_FACTOR, 1.0)


if __name__ == "__main__":
    unittest.main()


class PaperChargesCostsTests(unittest.TestCase):
    """The paper book must pay to trade, same as Auto Sim.

    `size_position` computed the venue round trip and returned it, but the
    close path was `cash += margin + pnl` — the fee was displayed and never
    charged. Two books on different cost bases cannot be compared, which is
    the entire reason both exist.
    """

    def test_the_position_model_carries_a_fee_reserve(self):
        from app.database import PaperPosition
        self.assertTrue(hasattr(PaperPosition, "fees"))
        self.assertTrue(hasattr(PaperPosition, "fee_basis"))

    def test_the_trade_model_separates_gross_from_net(self):
        """Costs are reported, not merely netted away — the gap between the
        two is what the book used to keep for free."""
        from app.database import PaperTrade
        for field in ("gross_pnl", "fees", "fee_basis", "realized_pnl"):
            self.assertTrue(hasattr(PaperTrade, field), field)

    def test_sizing_still_reports_the_fee_it_now_charges(self):
        from lib.paper_engine import size_position
        r = size_position(100_000.0, 95_000.0, 92_000.0, 10.0, 100_000.0,
                          symbol="BTC/USD")
        self.assertTrue(r.get("ok"), r.get("reason"))
        self.assertGreater(r["round_trip_fees"], 0.0)
        self.assertTrue(r["fee_basis"])
