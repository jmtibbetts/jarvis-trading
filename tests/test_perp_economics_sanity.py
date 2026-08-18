"""The arithmetic that decides whether a cheap-coin scalp is worth taking.

Every case here is a DENOMINATOR question, and every historical bug in this
area was the same mistake wearing different clothes: comparing a cost
measured against one base with a move measured against another, and
concluding the trade was uneconomic.

A SMALL ABSOLUTE PRICE CHANGE IS NOT A SMALL ECONOMIC MOVE. A coin going
from $0.043241 to $0.044241 moves a tenth of a cent and 2.31%, and at 20x
that is a 46% return on committed margin. Reading "$0.001" and calling it
noise is how a profitable strategy gets filtered out by its own simulator.
"""
import unittest

from lib import fee_authority as FA
from lib import product_router as PR

# The operator's real scalp.
ENTRY = 0.043241
EXIT = 0.044241
MARGIN = 1_000.0
LEVERAGE = 20.0


class TheCheapCoinScalpIsAMaterialMoveTests(unittest.TestCase):

    def test_the_underlying_move_is_two_and_a_third_percent(self):
        move = (EXIT - ENTRY) / ENTRY
        self.assertAlmostEqual(move * 100.0, 2.31262, places=4)

    def test_the_absolute_change_is_a_tenth_of_a_cent_and_that_is_irrelevant(self):
        """Both facts are true. Only one of them is economically meaningful,
        and the model must reason with the percentage."""
        self.assertAlmostEqual(EXIT - ENTRY, 0.001, places=9)
        self.assertGreater((EXIT - ENTRY) / ENTRY, 0.02)

    def test_leverage_applies_once_through_exposure(self):
        notional = MARGIN * LEVERAGE
        self.assertAlmostEqual(notional, 20_000.0, places=6)
        gross = notional * ((EXIT - ENTRY) / ENTRY)
        self.assertAlmostEqual(gross, 462.52, places=2)

    def test_the_return_on_committed_margin_is_forty_six_percent(self):
        gross = (MARGIN * LEVERAGE) * ((EXIT - ENTRY) / ENTRY)
        self.assertAlmostEqual(gross / MARGIN * 100.0, 46.25, places=2)

    def test_the_inverse_short_earns_the_same_magnitude(self):
        """0.044241 -> 0.043241. A short is not a rounding error either."""
        move = (EXIT - ENTRY) / EXIT
        gross = (MARGIN * LEVERAGE) * move
        self.assertGreater(gross, 400.0)
        self.assertAlmostEqual(gross / MARGIN * 100.0, 45.21, places=2)

    def test_a_legitimate_round_trip_fee_does_not_kill_the_trade(self):
        """$20-$30 of product-correct cost against $462 of gross. The trade
        survives comfortably; a model that rejects it is measuring
        something other than this trade."""
        gross = (MARGIN * LEVERAGE) * ((EXIT - ENTRY) / ENTRY)
        for round_trip in (20.0, 30.0, 60.0):
            with self.subTest(fee=round_trip):
                self.assertGreater(gross - round_trip, 0.0)
                self.assertGreater((gross - round_trip) / MARGIN, 0.39)


class DenominatorsMustNotBeMixedTests(unittest.TestCase):
    """THE BUG CLASS. Comparing a fee as a percentage of MARGIN against an
    unleveraged move as a percentage of the UNDERLYING is not a comparison;
    the two numbers do not share a base."""

    NOTIONAL = MARGIN * LEVERAGE
    FEE = 20.0

    def test_the_same_fee_reads_twenty_times_worse_against_margin(self):
        vs_notional = self.FEE / self.NOTIONAL
        vs_margin = self.FEE / MARGIN
        self.assertAlmostEqual(vs_notional * 100.0, 0.10, places=6)
        self.assertAlmostEqual(vs_margin * 100.0, 2.00, places=6)
        self.assertAlmostEqual(vs_margin / vs_notional, LEVERAGE, places=6)

    def test_the_mixed_comparison_wrongly_kills_a_profitable_trade(self):
        """2% (fee/margin) against 2.31% (move/underlying) looks marginal.
        Correctly stated it is 0.10% against 2.31% — a 23x edge."""
        move = (EXIT - ENTRY) / ENTRY
        mixed_ratio = (self.FEE / MARGIN) / move
        correct_ratio = (self.FEE / self.NOTIONAL) / move
        self.assertGreater(mixed_ratio, 0.8, "the mixed reading looks marginal")
        self.assertLess(correct_ratio, 0.05, "correctly stated it is trivial")

    def test_both_sides_priced_on_notional_agree(self):
        gross_usd = self.NOTIONAL * ((EXIT - ENTRY) / ENTRY)
        net_usd = gross_usd - self.FEE
        self.assertAlmostEqual(net_usd / self.NOTIONAL * 100.0, 2.2126, places=3)


class TheCatastrophicGateUsesNotionalNotMarginTests(unittest.TestCase):
    """The gate answers "is this PRODUCT structurally absurd?" — never "is
    this trade good?". Those are different questions and collapsing them
    turns a product check into an expectancy check with the wrong units."""

    def test_a_real_fee_on_real_notional_passes(self):
        from lib import venues as V
        notional, fee = 20_000.0, 20.0
        round_trip_pct = (2.0 * fee / notional) * 100.0
        self.assertAlmostEqual(round_trip_pct, 0.20, places=6)
        self.assertLess(round_trip_pct, V.MAX_VIABLE_FEE_PCT_OF_NOTIONAL)

    def test_the_same_fee_measured_against_margin_would_falsely_fail(self):
        """$20 on $1,000 of margin reads as 4% round trip and would be
        refused as catastrophic — for a trade costing 0.20% of what it
        actually controls."""
        from lib import venues as V
        against_margin = (2.0 * 20.0 / MARGIN) * 100.0
        self.assertGreater(against_margin, V.MAX_VIABLE_FEE_PCT_OF_NOTIONAL)

    def test_the_gate_in_the_entry_path_divides_by_notional(self):
        """By source, so the denominator cannot drift back to margin."""
        import inspect

        from lib import canonical_entry as CE
        src = inspect.getsource(CE.open_canonical_position)
        gate = src[src.index("CATASTROPHIC-PRODUCT GATE"):
                   src.index("FEE_EXCEEDS_VIABLE_SHARE_OF_NOTIONAL,")]
        self.assertIn("final.notional", gate)
        self.assertNotIn("final.margin", gate)

    def test_a_genuinely_absurd_product_still_fails(self):
        """SHIB's US contract was $4.47 and cost $0.30 to trade — 6.7% at
        any size, because a per-contract cost does not dilute."""
        from lib import venues as V
        contract_value, round_trip = 4.47, 0.30
        pct = round_trip / contract_value * 100.0
        self.assertGreater(pct, V.MAX_VIABLE_FEE_PCT_OF_NOTIONAL)


class ThePerpFeeStaysPerContractTests(unittest.TestCase):

    def test_a_us_perp_leg_is_priced_per_contract(self):
        q = FA.leg_fee("BTC/USD", notional=20_000.0, price=64_400.0,
                       product=PR.CRYPTO_PERP, venue="kraken_derivatives_us")
        self.assertTrue(q.ok, q.detail)
        self.assertEqual(q.fee_basis, FA.PER_CONTRACT)
        self.assertIsNone(q.rate)
        self.assertEqual(q.quality, FA.EXCHANGE_SCHEDULE)

    def test_the_contract_count_drives_the_fee(self):
        q = FA.leg_fee("BTC/USD", notional=20_000.0, price=64_400.0,
                       product=PR.CRYPTO_PERP, venue="kraken_derivatives_us")
        from lib.venues import US_PERPETUAL_FEE_PER_SIDE
        self.assertAlmostEqual(q.fee_usd,
                               q.contract_count * US_PERPETUAL_FEE_PER_SIDE,
                               places=9)

    def test_the_perp_is_never_billed_at_the_spot_rate(self):
        perp = FA.leg_fee("BTC/USD", notional=20_000.0, price=64_400.0,
                          product=PR.CRYPTO_PERP, venue="kraken_derivatives_us")
        spot = FA.leg_fee("BTC/USD", notional=20_000.0, price=64_400.0,
                          product=PR.CRYPTO_SPOT, venue="kraken")
        self.assertLess(perp.fee_usd, spot.fee_usd)
        self.assertNotEqual(perp.fee_basis, spot.fee_basis)


if __name__ == "__main__":
    unittest.main()
