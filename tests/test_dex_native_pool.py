"""Real reserves, and gas that leaves the SOL balance instead of the pool.

TWO DEFECTS.

The pool output was reduced by the network fee:

    received_usd = out_usd - network_fee_usd

That models a chain where the AMM hands you fewer tokens because the
validator got paid. It does not work that way — the pool gives you exactly
what the curve says and the fee is debited separately from the wallet's SOL
balance. Netting it out hides the one failure this simulator should teach
(a wallet holding tokens but no SOL cannot transact at all) and silently
scales a FLAT fee with trade size.

And depth was always `reserve_usd / 2.0` — an assumption that the pool is
balanced. That is roughly right for a balanced constant-product pool and
arbitrary for a concentrated one, where local depth around the current tick
may be nothing like half the total, in either direction. When real reserves
are available no assumption is needed at all.
"""
import unittest

from lib.dex_swap_math import quote_swap, quote_swap_native


class GasIsPaidSeparatelyTests(unittest.TestCase):
    """Prompt test 15."""

    def test_the_pool_output_is_not_reduced_by_gas(self):
        cheap = quote_swap(1_000, 500_000, fee_bps=25,
                           sol_price_usd=200.0, priority_lamports=0)
        pricey = quote_swap(1_000, 500_000, fee_bps=25,
                            sol_price_usd=200.0, priority_lamports=5_000_000)
        self.assertAlmostEqual(cheap["received_usd"], pricey["received_usd"],
                               msg="a priority fee changed the pool output")

    def test_the_fee_is_still_reported_and_in_sol(self):
        q = quote_swap(1_000, 500_000, fee_bps=25, sol_price_usd=200.0)
        self.assertGreater(q["network_fee_sol"], 0)
        self.assertGreater(q["network_fee_usd"], 0)
        self.assertTrue(q["gas_paid_separately"])

    def test_a_flat_fee_does_not_scale_with_trade_size(self):
        """Netting gas out of output made a flat Solana fee behave like a
        percentage."""
        small = quote_swap(100, 500_000, fee_bps=25, sol_price_usd=200.0)
        large = quote_swap(10_000, 500_000, fee_bps=25, sol_price_usd=200.0)
        self.assertAlmostEqual(small["network_fee_sol"], large["network_fee_sol"])

    def test_gas_is_still_counted_in_total_cost(self):
        """Paid separately is not paid never."""
        q = quote_swap(1_000, 500_000, fee_bps=25, sol_price_usd=200.0)
        self.assertGreater(q["total_cost_usd"],
                           q["pool_fee_usd"] + q["price_impact_usd"])


class NativeReserveTests(unittest.TestCase):
    """Prompt test 14 — exact x*y=k from real reserves."""

    def test_a_known_pool_produces_the_exact_curve_output(self):
        # 1,000 in against X=100,000 / Y=200,000, no fee.
        q = quote_swap_native(1_000, 100_000, 200_000, fee_bps=0)
        expected = (1_000 * 200_000) / (100_000 + 1_000)
        self.assertAlmostEqual(q["tokens_out"], expected, places=6)

    def test_the_fee_is_taken_from_the_input_leg(self):
        no_fee = quote_swap_native(1_000, 100_000, 200_000, fee_bps=0)
        fee = quote_swap_native(1_000, 100_000, 200_000, fee_bps=30)
        self.assertLess(fee["tokens_out"], no_fee["tokens_out"])
        self.assertAlmostEqual(fee["pool_fee_tokens_in"], 3.0)

    def test_an_unbalanced_pool_prices_differently_than_half_of_total(self):
        """THE reason native reserves matter. Same total value, very
        different curve."""
        balanced = quote_swap_native(1_000, 100_000, 100_000, fee_bps=0)
        skewed = quote_swap_native(1_000, 10_000, 190_000, fee_bps=0)
        self.assertNotAlmostEqual(balanced["price_impact_pct"],
                                  skewed["price_impact_pct"], places=2)
        self.assertGreater(skewed["price_impact_pct"],
                           balanced["price_impact_pct"])

    def test_impact_grows_with_size(self):
        small = quote_swap_native(100, 100_000, 200_000, fee_bps=0)
        large = quote_swap_native(20_000, 100_000, 200_000, fee_bps=0)
        self.assertGreater(large["price_impact_pct"], small["price_impact_pct"])

    def test_tokens_out_is_returned_so_nothing_divides_by_a_mid(self):
        q = quote_swap_native(1_000, 100_000, 200_000, fee_bps=25)
        self.assertGreater(q["tokens_out"], 0)
        self.assertAlmostEqual(q["effective_price"],
                               q["amount_in"] / q["tokens_out"])

    def test_the_effective_price_is_worse_than_spot(self):
        q = quote_swap_native(5_000, 100_000, 200_000, fee_bps=25)
        # spot_price is out-per-in; effective is in-per-out.
        self.assertLess(1.0 / q["effective_price"], q["spot_price"])

    def test_missing_reserves_refuse_rather_than_assume(self):
        self.assertFalse(quote_swap_native(100, 0, 200_000)["ok"])
        self.assertFalse(quote_swap_native(100, 100_000, 0)["ok"])

    def test_native_gas_also_leaves_the_output_alone(self):
        a = quote_swap_native(1_000, 100_000, 200_000, fee_bps=0,
                              priority_lamports=0)
        b = quote_swap_native(1_000, 100_000, 200_000, fee_bps=0,
                              priority_lamports=5_000_000)
        self.assertAlmostEqual(a["tokens_out"], b["tokens_out"])
        self.assertGreater(b["network_fee_sol"], a["network_fee_sol"])

    def test_native_reserves_are_labelled_verified(self):
        q = quote_swap_native(1_000, 100_000, 200_000)
        self.assertEqual(q["depth_confidence"], "VERIFIED")
        self.assertIn("no balanced-pool assumption", q["provenance"])


class DepthProvenanceTests(unittest.TestCase):
    def test_the_usd_path_declares_its_assumption(self):
        q = quote_swap(1_000, 500_000, fee_bps=25)
        self.assertEqual(q["depth_confidence"], "ASSUMED_BALANCED_POOL")
        self.assertIn("ASSUMING", q["provenance"])

    def test_concentrated_is_a_modelled_estimate_not_a_measurement(self):
        q = quote_swap(1_000, 500_000, fee_bps=25, concentrated=True)
        self.assertEqual(q["depth_confidence"], "MODELLED_ESTIMATE")
        self.assertIn("error direction is unknown", q["provenance"])

    def test_native_outranks_both(self):
        confidences = {
            quote_swap_native(1_000, 100_000, 200_000)["depth_confidence"],
            quote_swap(1_000, 500_000)["depth_confidence"],
            quote_swap(1_000, 500_000, concentrated=True)["depth_confidence"],
        }
        self.assertEqual(len(confidences), 3,
                         "three different certainties must not read alike")


if __name__ == "__main__":
    unittest.main()
