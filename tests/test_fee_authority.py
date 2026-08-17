"""A8. A US perpetual was silently billed at the SPOT schedule.

`transaction_costs.fee_pct(leveraged=True)` asked `venues.futures_fee_for()`
for a perpetual rate. Under VENUE_REGION=us that returns None — correctly,
because US perps list through Bitnomial and are priced PER CONTRACT, which a
percentage-shaped caller cannot express. Execution then continued into
`fee_for(asset_class="crypto")`, the spot path.

The two Nones meant different things and the code could not tell them apart:
"this symbol is not a listed future" (falling back to spot is fine) and "this
region prices per contract" (falling back to spot is nonsense).

A SPOT RATE IS NOT A CONSERVATIVE PERP ESTIMATE. It is a measurement of a
different instrument, and it is wrong in the direction that makes viable
setups look unaffordable — 0.80%/side against 0.05%/side, against a cost gate
that refuses above 0.50R.
"""
import os
import unittest
from unittest.mock import patch

from lib import fee_authority as FA
from lib import product_router as PR
from lib import venues as V
from lib.transaction_costs import fee_pct


def _us():
    return patch.dict(os.environ, {"VENUE_REGION": "us", "PAPER_VENUE": "kraken"})


def _intl():
    return patch.dict(os.environ, {"VENUE_REGION": "international",
                                   "PAPER_VENUE": "kraken"})


class ThePerpNeverFallsThroughToSpotTests(unittest.TestCase):

    def test_the_none_that_started_it_is_still_returned(self):
        """Not a regression in venues — the None is CORRECT. What was wrong
        was the caller treating it as 'no derivative schedule, use spot'."""
        with _us():
            rate, why = V.futures_fee_for("BTC/USD", maker=False)
        self.assertIsNone(rate)
        self.assertIn("PER CONTRACT", why)

    def test_a_us_perp_is_not_priced_at_the_spot_rate(self):
        with _us():
            perp = fee_pct("BTC/USD", product=PR.CRYPTO_PERP)
            spot = fee_pct("BTC/USD", product=PR.CRYPTO_SPOT)
        self.assertNotEqual(perp, spot,
                            "the perpetual is still being billed as spot")
        self.assertLess(perp, spot)

    def test_the_legacy_leveraged_flag_also_stops_falling_through(self):
        """Callers not yet threaded must not keep hitting the old path."""
        with _us():
            lev = fee_pct("BTC/USD", leveraged=True)
            spot = fee_pct("BTC/USD", leveraged=False)
        self.assertNotEqual(lev, spot)
        self.assertLess(lev, spot)

    def test_the_perp_rate_is_a_perp_rate_in_both_regions(self):
        with _us():
            us_rate = fee_pct("BTC/USD", product=PR.CRYPTO_PERP)
        with _intl():
            intl_rate = fee_pct("BTC/USD", product=PR.CRYPTO_PERP)
        for rate in (us_rate, intl_rate):
            # A perpetual schedule is basis points, not most of a percent.
            self.assertLess(rate, 0.005, "this is a spot-sized number")

    def test_even_an_unknown_symbol_gets_a_perp_rate_not_a_spot_one(self):
        """The failure path is where the fallthrough lived."""
        with _us():
            q = FA.leg_fee("NOTAREALCOIN/USD", notional=10_000.0, price=1.0,
                           product=PR.CRYPTO_PERP, venue="kraken")
        self.assertTrue(q.ok)
        self.assertEqual(q.quality, FA.ESTIMATED_PERP)
        self.assertLess(q.rate, 0.005)


class TheFeeApiIsNotPercentageShapedTests(unittest.TestCase):
    """Percentage cannot represent a flat per-contract fee without knowing
    the size — which is the information the old signature threw away."""

    def test_a_quote_is_denominated_in_dollars(self):
        with _us():
            q = FA.leg_fee("BTC/USD", notional=95_000.0, price=95_000.0,
                           product=PR.CRYPTO_PERP, venue="kraken")
        self.assertTrue(q.ok)
        self.assertIsNotNone(q.fee_usd)

    def test_a_us_perp_with_a_known_contract_size_is_priced_per_contract(self):
        with _us():
            q = FA.leg_fee("BTC/USD", notional=95_000.0, price=95_000.0,
                           product=PR.CRYPTO_PERP, venue="kraken")
        self.assertEqual(q.fee_basis, FA.PER_CONTRACT)
        self.assertIsNotNone(q.contract_count)
        self.assertIsNone(q.rate, "a per-contract fee has no rate")
        self.assertAlmostEqual(q.fee_usd,
                               q.contract_count * V.US_PERPETUAL_FEE_PER_SIDE,
                               places=6)

    def test_a_percentage_product_is_notional_times_rate(self):
        with _intl():
            q = FA.leg_fee("BTC/USD", notional=20_000.0, price=95_000.0,
                           product=PR.CRYPTO_SPOT, venue="kraken")
        self.assertEqual(q.fee_basis, FA.PERCENT_OF_NOTIONAL)
        self.assertAlmostEqual(q.fee_usd, 20_000.0 * q.rate, places=9)

    def test_the_display_percentage_is_derived_and_never_an_input(self):
        """A flat per-contract fee expressed as a percentage changes with
        position size, so it must not be fed back into a percentage model.

        BTC's contract is 0.01 BTC = $950 at $95,000, so $1,425 is 1.5
        contracts: the rounding penalty is a larger share of a small
        position than of a large one.
        """
        with _us():
            small = FA.leg_fee("BTC/USD", notional=1_425.0, price=95_000.0,
                               product=PR.CRYPTO_PERP, venue="kraken")
            large = FA.leg_fee("BTC/USD", notional=950_000.0, price=95_000.0,
                               product=PR.CRYPTO_PERP, venue="kraken")
        self.assertGreater(small.pct_of_notional, large.pct_of_notional,
                           "a flat per-contract fee is REGRESSIVE with size")

    def test_a_quote_carries_the_full_provenance_the_plan_requires(self):
        with _us():
            q = FA.leg_fee("BTC/USD", notional=95_000.0, price=95_000.0,
                           product=PR.CRYPTO_PERP, venue="kraken", maker=False)
        for field in ("fee_usd", "fee_basis", "contract_count", "venue",
                      "product", "region", "maker", "source", "quality"):
            with self.subTest(field=field):
                self.assertIsNotNone(getattr(q, field), field)
        self.assertEqual(q.product, PR.CRYPTO_PERP)
        self.assertEqual(q.region, "us")


class AnEstimateIsLabelledAsOneTests(unittest.TestCase):

    def test_a_rulebook_contract_figure_is_a_measurement(self):
        with _us():
            q = FA.leg_fee("BTC/USD", notional=95_000.0, price=95_000.0,
                           product=PR.CRYPTO_PERP, venue="kraken")
        self.assertEqual(q.quality, FA.EXCHANGE_SCHEDULE)
        self.assertTrue(q.is_measured)

    def test_a_stand_in_perp_rate_is_never_reported_as_measured(self):
        """It must not pool into calibration as though it were observed."""
        with _us():
            q = FA.leg_fee("NOTAREALCOIN/USD", notional=10_000.0, price=1.0,
                           product=PR.CRYPTO_PERP, venue="kraken")
        self.assertEqual(q.quality, FA.ESTIMATED_PERP)
        self.assertFalse(q.is_measured)
        self.assertIn("perpetual", q.detail.lower())

    def test_an_unestablished_product_is_unavailable_not_defaulted(self):
        q = FA.leg_fee("BTC/USD", notional=10_000.0, price=1.0, product="")
        self.assertFalse(q.ok)
        self.assertEqual(q.reason, FA.FEE_AUTHORITY_UNAVAILABLE)
        self.assertIsNone(q.fee_usd)

    def test_an_uncharacterised_product_is_refused_rather_than_invented(self):
        q = FA.leg_fee("ES=F", notional=10_000.0, price=5_000.0,
                       product=PR.INDEX_FUTURE)
        self.assertFalse(q.ok)
        self.assertEqual(q.reason, FA.FEE_AUTHORITY_UNAVAILABLE)


class WholeContractsFloorForExecutionTests(unittest.TestCase):
    """The plan's audit: `us_perp_contracts` uses ceil(). That is CORRECT
    where it is used — it prices a fee, and overstating a cost estimate is
    the safe direction. It would be wrong as an executable quantity, because
    an authorization is a MAXIMUM."""

    # BTC's US perpetual contract is 0.01 BTC — $950 of notional at $95,000.
    PRICE = 95_000.0
    ONE_CONTRACT = 950.0

    def test_the_fee_estimator_still_rounds_up(self):
        """Unchanged on purpose. Flooring here would understate cost."""
        with _us():
            n, _ = V.us_perp_contracts("BTC/USD", self.ONE_CONTRACT * 1.5,
                                       self.PRICE)
        self.assertEqual(n, 2.0, "1.5 contracts ceils to 2 for a cost estimate")

    def test_the_executable_count_floors_instead(self):
        with _us():
            n, why = FA.executable_contracts("BTC/USD", self.ONE_CONTRACT * 1.5,
                                             self.PRICE)
        self.assertEqual(n, 1.0, "authorization is a maximum, so 1.5 -> 1")
        self.assertIn("floored", why)

    def test_below_one_contract_refuses_rather_than_rounding_up_to_one(self):
        """`us_perp_contracts` does max(1.0, ceil(...)), so a request for
        half a contract becomes a whole one — size nobody authorized."""
        with _us():
            planned, _ = V.us_perp_contracts("BTC/USD", self.ONE_CONTRACT * 0.5,
                                             self.PRICE)
            n, why = FA.executable_contracts("BTC/USD", self.ONE_CONTRACT * 0.5,
                                             self.PRICE)
        self.assertEqual(planned, 1.0, "the planning count still rounds up")
        self.assertIsNone(n)
        self.assertIn("below one whole", why)

    def test_the_executable_count_never_exceeds_the_planning_count(self):
        with _us():
            for mult in (0.5, 1.0, 1.5, 2.7, 9.0, 137.4):
                notional = self.ONE_CONTRACT * mult
                plan, _ = V.us_perp_contracts("BTC/USD", notional, self.PRICE)
                real, _ = FA.executable_contracts("BTC/USD", notional, self.PRICE)
                with self.subTest(contracts=mult):
                    if real is not None:
                        self.assertLessEqual(real, plan)

    def test_an_unknown_contract_size_is_refused_not_guessed(self):
        with _us():
            n, why = FA.executable_contracts("NOTAREALCOIN/USD", 10_000.0, 1.0)
        self.assertIsNone(n)
        self.assertIn("not on file", why)


class NothingUsesThePlanningCountAsAQuantityTests(unittest.TestCase):

    def test_us_perp_contracts_is_only_consumed_by_fee_code(self):
        """By AST over the runtime tree. A ceil()ed count reaching an order
        ticket would take more size than was authorized."""
        import ast
        import pathlib

        root = pathlib.Path(__file__).parent.parent
        offenders = []
        for path in list((root / "lib").rglob("*.py")) + \
                    list((root / "jobs").rglob("*.py")) + \
                    list((root / "app").rglob("*.py")):
            if path.name in ("venues.py", "fee_authority.py"):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    name = (node.func.attr if isinstance(node.func, ast.Attribute)
                            else getattr(node.func, "id", None))
                    if name == "us_perp_contracts":
                        offenders.append(str(path.relative_to(root)))
        self.assertEqual(offenders, [],
                         f"the ceil()ed planning count is being called outside "
                         f"the fee model: {offenders}")


if __name__ == "__main__":
    unittest.main()
