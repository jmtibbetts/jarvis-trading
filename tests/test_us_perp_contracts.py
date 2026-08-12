"""US perpetuals: cost = contracts x per-contract all-in fee.

Kraken's US schedule (updated 2026-06-15) prices Bitnomial-listed perpetual
futures at $0.15 per contract per side, all-in -- $0.30 the round trip.

The RATE was never wrong. The CONTRACT COUNT was. It was derived from
Kraken's INTERNATIONAL flexible futures, where contractSize 1 means one
TOKEN, so the same correct rate produced opposite absurdities purely from
unit price:

    OP/USD   $0.089    20,157 "contracts" -> $6,047 on $1,800  (336%)
    BTC/USD  $95,000    0.0937 "contracts" ->   $0.03 on $8,900 (0.0003%)

Bitnomial PERPETUAL contracts are sized in units of the UNDERLYING
(PBTCUC = 0.01 BTC, PSHBUN = 1,000,000 SHIB), and futures trade in WHOLE
contracts. Counting them properly puts a $8,900 round trip at a few dollars
on every listed instrument, which is what the operator observes paying.
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
        small, _ = us_perp_contracts("BTC/USD", 950.0, 95_000.0)
        large, _ = us_perp_contracts("BTC/USD", 9_500.0, 95_000.0)
        self.assertEqual(small, 1)
        self.assertEqual(large, 10)

    def test_the_bitcoin_contract_is_the_perpetual_not_the_future(self):
        """PBTCUC is 0.01 BTC. BUI/BUS are Bitnomial's Bitcoin FUTURES, a
        different instrument -- using them put the contract out by 10x."""
        _, why = us_perp_contracts("BTC/USD", 9_500.0, 95_000.0)
        self.assertIn("PBTCUC", why)
        self.assertIn("0.01 BTC/contract", why)


class FeeFormulaTests(unittest.TestCase):
    """cost = contracts * per_contract_all_in_fee, both sides."""

    def test_the_formula_is_exactly_that(self):
        for notional in (1_000.0, 9_500.0, 95_000.0, 950_000.0):
            contracts, _ = us_perp_contracts("BTC/USD", notional, 95_000.0)
            fee, _ = us_perp_fee("BTC/USD", notional, 95_000.0)
            self.assertAlmostEqual(fee, contracts * US_PERP_FEE_PER_SIDE * 2.0)

    def test_a_round_trip_costs_thirty_cents_per_contract(self):
        fee, _ = us_perp_fee("BTC/USD", 950.0, 95_000.0)
        self.assertAlmostEqual(fee, 0.30)

    def test_cost_is_regressive_in_percentage_terms(self):
        """Per-contract pricing gets cheaper as a fraction of a larger
        position -- the opposite shape to a percentage schedule. At exact
        multiples of the contract size the rate is flat; the regressive
        step comes from the whole-contract MINIMUM, which a small position
        pays in full."""
        small, _ = us_perp_fee("BTC/USD", 50.0, 95_000.0)       # « 1 contract
        large, _ = us_perp_fee("BTC/USD", 950_000.0, 95_000.0)  # 1,000 contracts
        self.assertGreater(small / 50.0, large / 950_000.0)

    def test_a_sub_contract_position_still_pays_a_full_contract(self):
        """$50 of BTC cannot be expressed in less than one PBTCUC, so it
        pays the same $0.30 as a $950 position. That is a real cost floor,
        and it is what makes tiny leveraged positions inefficient."""
        tiny, _ = us_perp_fee("BTC/USD", 50.0, 95_000.0)
        full, _ = us_perp_fee("BTC/USD", 950.0, 95_000.0)
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
        for sym in ("OP/USD", "ISEK/USD", "VELVET/USD", "KAITO/USD"):
            n, why = us_perp_contracts(sym, 8_900.0, 1.0)
            self.assertIsNone(n, sym)
            self.assertIn("contract size not on file", why)

    def test_the_estimate_is_labelled_not_passed_off_as_exact(self):
        """A stand-in that looks like a measurement is worse than one that
        announces itself."""
        with mock.patch.dict(os.environ, {"VENUE_REGION": "us"}):
            _, why = venue_round_trip_fee("OP/USD", 8_900.0, 8.9, 0.0893)
            self.assertIn("ESTIMATED", why)

    def test_an_estimated_symbol_still_costs_something_sane(self):
        with mock.patch.dict(os.environ, {"VENUE_REGION": "us"}):
            fee, why = venue_round_trip_fee("OP/USD", 8_900.0, 8.9, 0.0893)
            self.assertGreater(fee, 0.0)
            self.assertLess(fee / 8_900.0, 0.05, why)

    def test_a_listed_symbol_is_priced_exactly_not_estimated(self):
        """SOL is on file now — it must use the real formula, not the
        stand-in."""
        with mock.patch.dict(os.environ, {"VENUE_REGION": "us"}):
            _, why = venue_round_trip_fee("SOL/USD", 8_900.0, 8.9, 180.0)
            self.assertNotIn("ESTIMATED", why)
            self.assertIn("PSOLUS", why)

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
        """0.0937 token-"contracts" x $0.15 gave $0.03. Ten real PBTCUC
        contracts give $3.00 -- two orders of magnitude, and correct."""
        fee, _ = venue_round_trip_fee("BTC/USD", 8_900.0, 8.9, 95_000.0)
        self.assertAlmostEqual(fee, 3.00)


class ContractSizePlausibilityTests(unittest.TestCase):
    """A configured contract size turns straight into a fee.

    Most Bitnomial contracts are sized so one is a few hundred to a few
    thousand dollars across a five-order-of-magnitude price range -- 0.01
    BTC and 5,000 ADA are both roughly $1,000. That property is what keeps
    the per-contract fee negligible, and losing it is what made "one
    contract = one token" so destructive.

    Where an instrument genuinely breaks the pattern, the model reports the
    real number rather than smoothing it: the decision about whether such a
    trade is worth taking belongs to the cost gate, not to a lookup.
    """

    def test_one_buy_of_a_million_shib_is_one_contract(self):
        """PSHBUN is 1,000,000 SHIB. Treating it as 100,000 turned a single
        1M buy into ten contracts and multiplied its fee by ten."""
        n, why = us_perp_contracts("SHIB/USD", 1_000_000 * 0.00000447, 0.00000447)
        self.assertEqual(n, 1, why)

    def test_a_rulebook_size_is_never_refused_by_a_heuristic(self):
        """These sizes are measurements. Refusing to price one would push
        the caller onto a percentage stand-in that is LESS accurate, and
        second-guessing a measurement with a guess is the error this whole
        model has been unwinding."""
        n, why = us_perp_contracts("SHIB/USD", 8_900.0, 0.00000447)
        self.assertIsNotNone(n, why)

    def test_a_costly_contract_reports_its_real_cost_uncapped(self):
        """SHIB's contract is $4.47 and costs $0.30 to trade -- 6.7%, above
        the 5% sanity ceiling. Capping it would UNDERSTATE cost, the one
        direction this model must never fail in. The gate at signal
        construction should refuse the trade on economics, not be handed a
        flattering number."""
        import os
        from unittest import mock as _m
        with _m.patch.dict(os.environ, {"VENUE_REGION": "us"}):
            fee, why = venue_round_trip_fee("SHIB/USD", 8_900.0, 8.9, 0.00000447)
        self.assertNotIn("CAPPED", why)
        self.assertGreater(fee / 8_900.0, 0.05)

    def test_per_contract_cost_does_not_scale_away(self):
        """Buying more contracts cannot dilute a per-contract fee -- the
        ratio is identical at every size. That is what makes an expensive
        contract expensive at ANY size."""
        rates = []
        for tokens in (1_000_000, 10_000_000, 100_000_000, 1_000_000_000):
            notional = tokens * 0.00000447
            fee, _ = us_perp_fee("SHIB/USD", notional, 0.00000447)
            rates.append(round(fee / notional, 6))
        self.assertEqual(len(set(rates)), 1, rates)

    def test_every_other_configured_size_is_plausible_at_a_real_price(self):
        prices = {"BTC": 95_000.0, "ETH": 3_200.0, "SOL": 180.0, "XRP": 2.1,
                  "AAVE": 260.0, "AVAX": 22.0, "BCH": 520.0, "ADA": 0.62,
                  "LINK": 18.0, "DOGE": 0.16, "HBAR": 0.19, "LTC": 95.0,
                  "DOT": 4.1, "XLM": 0.31, "XTZ": 0.75, "TRX": 0.29}
        for asset, px in prices.items():
            n, why = us_perp_contracts(f"{asset}/USD", 8_900.0, px)
            self.assertIsNotNone(n, f"{asset} refused: {why}")

    def test_a_round_trip_costs_single_digit_dollars_across_the_board(self):
        """The operator's actual observation, as a property."""
        prices = {"BTC": 95_000.0, "ETH": 3_200.0, "SOL": 180.0, "XRP": 2.1,
                  "ADA": 0.62, "DOGE": 0.16, "TRX": 0.29, "LTC": 95.0}
        for asset, px in prices.items():
            fee, why = us_perp_fee(f"{asset}/USD", 8_900.0, px)
            self.assertLess(fee, 15.0, f"{asset}: ${fee:,.2f} -- {why}")
            self.assertGreater(fee, 0.0, asset)


if __name__ == "__main__":
    unittest.main()
