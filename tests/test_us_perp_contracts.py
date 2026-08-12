"""US perpetuals: cost = contracts x per-contract all-in fee.

Kraken's US schedule (updated 2026-06-15) prices Bitnomial-listed perpetual
futures at $0.15 per contract per side, all-in -- $0.30 the round trip.

The RATE was never wrong. The CONTRACT COUNT was. It was derived from
Kraken's INTERNATIONAL flexible futures, where contractSize 1 means one
TOKEN, so the same correct rate produced opposite absurdities purely from
unit price:

    OP/USD   $0.089    20,157 "contracts" -> $6,047 on $1,800  (336%)
    BTC/USD  $95,000    0.0937 "contracts" ->   $0.03 on $8,900 (0.0003%)

Bitnomial contracts are sized in units of the UNDERLYING (BUI = 0.1 BTC,
BUS = 1 BTC), and futures trade in WHOLE contracts. Counting them properly
gives $0.30 on a $8,900 position, which is what the operator observes.
"""
import os
import unittest
from unittest import mock

from lib.paper_engine import venue_round_trip_fee
from lib.venues import (us_perp_contracts, us_perp_fee, US_PERP_CONTRACTS,
                        US_PERP_FEE_PER_SIDE)


class ContractCountTests(unittest.TestCase):
    def test_contracts_are_whole(self):
        """You cannot buy 0.0937 of a futures contract."""
        n, _ = us_perp_contracts("BTC/USD", 8_900.0, 95_000.0)
        self.assertEqual(n, int(n))
        self.assertGreaterEqual(n, 1)

    def test_a_partial_contract_rounds_up_not_down(self):
        """Rounding down would let a position trade for free."""
        n, _ = us_perp_contracts("BTC/USD", 1.0, 95_000.0)
        self.assertEqual(n, 1)

    def test_count_scales_with_notional(self):
        small, _ = us_perp_contracts("BTC/USD", 9_500.0, 95_000.0)
        large, _ = us_perp_contracts("BTC/USD", 95_000.0, 95_000.0)
        self.assertEqual(small, 1)
        self.assertEqual(large, 10)

    def test_the_smallest_listed_contract_is_preferred(self):
        """BUI (0.1 BTC) expresses a small position; BUS (1 BTC) would force
        a 10x larger minimum."""
        _, why = us_perp_contracts("BTC/USD", 9_500.0, 95_000.0)
        self.assertIn("BUI", why)


class FeeFormulaTests(unittest.TestCase):
    """cost = contracts * per_contract_all_in_fee, both sides."""

    def test_the_formula_is_exactly_that(self):
        for notional in (1_000.0, 9_500.0, 95_000.0, 950_000.0):
            contracts, _ = us_perp_contracts("BTC/USD", notional, 95_000.0)
            fee, _ = us_perp_fee("BTC/USD", notional, 95_000.0)
            self.assertAlmostEqual(fee, contracts * US_PERP_FEE_PER_SIDE * 2.0)

    def test_a_round_trip_costs_thirty_cents_per_contract(self):
        fee, _ = us_perp_fee("BTC/USD", 9_500.0, 95_000.0)
        self.assertAlmostEqual(fee, 0.30)

    def test_cost_is_regressive_in_percentage_terms(self):
        """Per-contract pricing gets cheaper as a fraction of a larger
        position -- the opposite shape to a percentage schedule. At exact
        multiples of the contract size the rate is flat; the regressive
        step comes from the whole-contract MINIMUM, which a small position
        pays in full."""
        small, _ = us_perp_fee("BTC/USD", 500.0, 95_000.0)      # « 1 contract
        large, _ = us_perp_fee("BTC/USD", 950_000.0, 95_000.0)  # 100 contracts
        self.assertGreater(small / 500.0, large / 950_000.0)

    def test_a_sub_contract_position_still_pays_a_full_contract(self):
        """$500 of BTC cannot be expressed in less than one BUI, so it pays
        the same $0.30 as a $9,500 position. That is a real cost floor, and
        it is what makes tiny leveraged positions inefficient."""
        tiny, _ = us_perp_fee("BTC/USD", 500.0, 95_000.0)
        full, _ = us_perp_fee("BTC/USD", 9_500.0, 95_000.0)
        self.assertAlmostEqual(tiny, full)

    def test_the_explanation_names_the_contract_and_the_vintage(self):
        _, why = us_perp_fee("BTC/USD", 9_500.0, 95_000.0)
        self.assertIn("BTC/contract", why)
        self.assertIn("2026-06-15", why)


class UnknownContractSizeTests(unittest.TestCase):
    """Everything on the Kraken US exchange IS a US perpetual, priced per
    contract. What varies per instrument is the CONTRACT SIZE, and that is
    the one input the formula cannot proceed without.

    A symbol missing from the table is one whose size is unknown, NOT one
    that cannot be traded. Inventing a size is what produced every absurd
    number this model has emitted, so the lookup declines instead.
    """

    def test_an_unknown_size_returns_none_rather_than_guessing(self):
        for sym in ("OP/USD", "SOL/USD", "DOGE/USD", "ISEK/USD"):
            n, why = us_perp_contracts(sym, 8_900.0, 1.0)
            self.assertIsNone(n, sym)
            self.assertIn("contract size not on file", why)

    def test_the_estimate_is_labelled_not_passed_off_as_exact(self):
        """A stand-in that looks like a measurement is worse than one that
        announces itself."""
        with mock.patch.dict(os.environ, {"VENUE_REGION": "us"}):
            _, why = venue_round_trip_fee("SOL/USD", 8_900.0, 8.9, 180.0)
            self.assertIn("ESTIMATED", why)

    def test_an_estimated_symbol_still_costs_something_sane(self):
        with mock.patch.dict(os.environ, {"VENUE_REGION": "us"}):
            fee, why = venue_round_trip_fee("SOL/USD", 8_900.0, 8.9, 180.0)
            self.assertGreater(fee, 0.0)
            self.assertLess(fee / 8_900.0, 0.05, why)

    def test_bitcoin_is_listed(self):
        self.assertIn("BTC", US_PERP_CONTRACTS)

    def test_xbt_resolves_to_btc(self):
        """Kraken calls it XBT in places."""
        n, _ = us_perp_contracts("XBT/USD", 9_500.0, 95_000.0)
        self.assertIsNotNone(n)


class RegressionTests(unittest.TestCase):
    """The two numbers that started this."""

    def setUp(self):
        self._p = mock.patch.dict(os.environ, {"VENUE_REGION": "us"})
        self._p.start()
        self.addCleanup(self._p.stop)

    def test_a_cheap_coin_no_longer_costs_thousands(self):
        fee, _ = venue_round_trip_fee("OP/USD", 1_800.0, 5.0, 0.0893)
        self.assertLess(fee, 25.0)

    def test_bitcoin_no_longer_costs_three_cents_on_nine_thousand(self):
        """0.0937 token-"contracts" x $0.15 gave $0.03. One real BUI
        contract gives $0.30 -- an order of magnitude, and correct."""
        fee, _ = venue_round_trip_fee("BTC/USD", 8_900.0, 8.9, 95_000.0)
        self.assertAlmostEqual(fee, 0.30)


if __name__ == "__main__":
    unittest.main()
