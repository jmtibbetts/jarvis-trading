"""The holding-cost authority — signed transfers, honest qualities.

Zero is six different facts wearing one number. These tests pin the
vocabulary that keeps them apart, and the sign convention that makes funding
a TRANSFER rather than a fee: positive funding is paid by longs and received
by shorts, and nothing here ever calls abs().
"""
import unittest
from unittest.mock import patch

from lib import holding_cost_authority as HC

NOTIONAL, HOURS = 10_000.0, 16.0     # two 8h funding periods


class PerpFundingIsASignedTransferTests(unittest.TestCase):

    def _q(self, is_short, rate=0.0001):
        return HC.holding_cost("BTC/USD", product="CRYPTO_PERP",
                               notional_usd=NOTIONAL, hours_held=HOURS,
                               is_short=is_short, funding_rate_8h=rate)

    def test_positive_funding_long_pays(self):
        q = self._q(is_short=False)
        self.assertTrue(q.ok)
        self.assertAlmostEqual(q.amount_usd, NOTIONAL * 0.0001 * 2)
        self.assertEqual(q.kind, HC.KIND_FUNDING)

    def test_positive_funding_short_receives(self):
        q = self._q(is_short=True)
        self.assertTrue(q.ok)
        self.assertAlmostEqual(q.amount_usd, -NOTIONAL * 0.0001 * 2)

    def test_negative_funding_reverses_both(self):
        self.assertLess(self._q(False, rate=-0.0002).amount_usd, 0,
                        "negative funding: the long RECEIVES")
        self.assertGreater(self._q(True, rate=-0.0002).amount_usd, 0,
                           "negative funding: the short PAYS")

    def test_a_caller_rate_is_extrapolated_not_measured(self):
        """Even a measured RATE extrapolated over an interval is not a
        measurement of the funding this position paid."""
        q = self._q(False)
        self.assertEqual(q.quality, HC.LATEST_RATE_EXTRAPOLATED)

    def test_a_snapshot_rate_is_extrapolated(self):
        with patch("lib.transaction_costs._latest_funding_rate",
                   return_value=(0.0003, "measured")):
            q = HC.holding_cost("BTC/USD", product="CRYPTO_PERP",
                                notional_usd=NOTIONAL, hours_held=8.0,
                                is_short=False)
        self.assertEqual(q.quality, HC.LATEST_RATE_EXTRAPOLATED)
        self.assertAlmostEqual(q.amount_usd, NOTIONAL * 0.0003)

    def test_the_published_baseline_is_labelled_a_default(self):
        with patch("lib.transaction_costs._latest_funding_rate",
                   return_value=(0.0001, "default_baseline")):
            q = HC.holding_cost("BTC/USD", product="CRYPTO_PERP",
                                notional_usd=NOTIONAL, hours_held=8.0,
                                is_short=False)
        self.assertEqual(q.quality, HC.DEFAULT_BASELINE)

    def test_a_failed_lookup_is_unavailable_not_zero(self):
        with patch("lib.transaction_costs._latest_funding_rate",
                   side_effect=RuntimeError("provider down")):
            q = HC.holding_cost("BTC/USD", product="CRYPTO_PERP",
                                notional_usd=NOTIONAL, hours_held=8.0,
                                is_short=False)
        self.assertFalse(q.ok)
        self.assertIsNone(q.amount_usd, "an unavailable carry became a number")
        self.assertEqual(q.quality, HC.UNAVAILABLE)


class NonCarryProductsTests(unittest.TestCase):

    def test_spot_is_an_established_zero(self):
        for prod in ("CRYPTO_SPOT", "EQUITY_SPOT", "ETF_SPOT"):
            q = HC.holding_cost("X", product=prod, notional_usd=NOTIONAL,
                                hours_held=100.0, is_short=False)
            self.assertTrue(q.ok, prod)
            self.assertEqual(q.amount_usd, 0.0)
            self.assertEqual(q.kind, HC.KIND_NOT_APPLICABLE)
            self.assertEqual(q.quality, HC.NOT_APPLICABLE)

    def test_an_unknown_product_is_unavailable_not_free(self):
        q = HC.holding_cost("X", product="MYSTERY_SWAP",
                            notional_usd=NOTIONAL, hours_held=10.0,
                            is_short=False)
        self.assertFalse(q.ok)
        self.assertEqual(q.quality, HC.UNAVAILABLE)


class EquityBorrowTests(unittest.TestCase):

    def _q(self, **kw):
        base = dict(product="EQUITY_SHORT", notional_usd=NOTIONAL,
                    hours_held=48.0, is_short=True)
        base.update(kw)
        return HC.holding_cost("GME", **base)

    def test_a_measured_rate_is_labelled_measured(self):
        q = self._q(borrow_rate_annual=0.12)
        self.assertTrue(q.ok)
        self.assertEqual(q.quality, HC.MEASURED_BORROW_RATE)
        self.assertAlmostEqual(q.amount_usd,
                               NOTIONAL * 0.12 * (48.0 / 24.0) / 365.0)

    def test_general_collateral_default_is_labelled_a_default(self):
        q = self._q()
        self.assertEqual(q.quality, HC.DEFAULT_GENERAL_COLLATERAL)
        self.assertEqual(q.kind, HC.KIND_BORROW)
        self.assertGreater(q.amount_usd, 0)

    def test_hard_to_borrow_is_its_own_default(self):
        q = self._q(hard_to_borrow=True)
        self.assertEqual(q.quality, HC.DEFAULT_HARD_TO_BORROW)
        self.assertGreater(q.amount_usd, self._q().amount_usd,
                           "a tight name must cost more than GC")

    def test_a_long_pays_no_borrow(self):
        q = self._q(is_short=False)
        self.assertTrue(q.ok)
        self.assertEqual(q.amount_usd, 0.0)
        self.assertEqual(q.kind, HC.KIND_NOT_APPLICABLE)


if __name__ == "__main__":
    unittest.main()
