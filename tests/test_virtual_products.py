"""Product mechanics — what differs once the order fills.

Prompt tests 6, 7, 8, 12 and 13. The one that matters most is 13: a stop
does NOT protect a leveraged position that reaches its liquidation price
first. The exchange closes it, at its own price, and the stop never fills.
A simulator that always honours the stop teaches the desk that leverage is
free downside protection — which is the most expensive lesson a training
platform could possibly teach.
"""
import unittest

from lib.virtual_products import (FORCED_LIQUIDATION, STOP_EXIT,
                                  BorrowUnavailable, PerpPosition,
                                  equity_borrow_cost, funding_payment,
                                  futures_size, fx_pip_value,
                                  liquidation_price, resolve_exit,
                                  spot_has_no_funding)


class EquityShortTests(unittest.TestCase):
    """Prompt test 8."""

    def test_an_unavailable_borrow_rejects_rather_than_shorting_free(self):
        with self.assertRaises(BorrowUnavailable):
            equity_borrow_cost(100_000, 24, available=False)

    def test_borrow_accrues_over_the_hold(self):
        a = equity_borrow_cost(100_000, hold_hours=24)
        b = equity_borrow_cost(100_000, hold_hours=24 * 30)
        self.assertGreater(b["borrow_cost_usd"], a["borrow_cost_usd"])

    def test_hard_to_borrow_costs_far_more(self):
        easy = equity_borrow_cost(100_000, 24 * 30)
        hard = equity_borrow_cost(100_000, 24 * 30, hard_to_borrow=True)
        self.assertGreater(hard["borrow_cost_usd"],
                           easy["borrow_cost_usd"] * 10)

    def test_borrow_is_never_zero_for_a_held_short(self):
        self.assertGreater(equity_borrow_cost(50_000, 24)["borrow_cost_usd"], 0)

    def test_provenance_distinguishes_measured_from_default(self):
        self.assertEqual(equity_borrow_cost(1000, 24)["provenance"],
                         "default_general_collateral")
        self.assertEqual(
            equity_borrow_cost(1000, 24, borrow_rate_annual=0.11)["provenance"],
            "measured")


class FuturesSizingTests(unittest.TestCase):
    """Prompt test 6 — whole contracts, multiplier-aware risk."""

    def test_risk_per_contract_includes_the_multiplier(self):
        s = futures_size("MES=F", 1_000.0, entry=5000.0, stop=4990.0)
        self.assertEqual(s["risk_per_contract_usd"], 10.0 * 5)

    def test_the_old_arithmetic_would_have_oversized_by_the_multiplier(self):
        """Sizing on price distance alone makes a 1% risk position a 5%
        one on MES, and a 100% one on gold."""
        naive = int(1_000.0 // 10.0)                 # distance only
        real = futures_size("MES=F", 1_000.0, 5000.0, 4990.0)["contracts"]
        self.assertEqual(naive, 100)
        self.assertEqual(real, 20)

    def test_only_whole_contracts_are_returned(self):
        s = futures_size("GC=F", 5_000.0, entry=2000.0, stop=1990.0)
        self.assertEqual(s["contracts"], int(s["contracts"]))

    def test_a_budget_below_one_contract_returns_zero_not_one(self):
        s = futures_size("GC=F", 100.0, entry=2000.0, stop=1990.0)
        self.assertEqual(s["contracts"], 0)
        self.assertIn("below", s["reason"])

    def test_notional_carries_the_multiplier(self):
        s = futures_size("MES=F", 1_000.0, entry=5000.0, stop=4990.0)
        self.assertAlmostEqual(s["notional_usd"], 20 * 5000.0 * 5)

    def test_an_unsupported_future_refuses_to_size(self):
        from lib.instruments import UnsupportedInstrument
        with self.assertRaises(UnsupportedInstrument):
            futures_size("UNKNOWN=F", 10_000.0, 100.0, 99.0)


class ForexTests(unittest.TestCase):
    """Prompt test 7."""

    def test_a_major_pips_at_four_decimals(self):
        v = fx_pip_value("EUR/USD", 100_000)
        self.assertEqual(v["pip_size"], 0.0001)
        self.assertAlmostEqual(v["pip_value_usd"], 10.0)

    def test_a_jpy_cross_pips_at_two_decimals(self):
        v = fx_pip_value("USD/JPY", 100_000, quote_to_usd=1 / 150.0)
        self.assertEqual(v["pip_size"], 0.01)

    def test_using_the_wrong_pip_overstates_a_jpy_pip_by_100x(self):
        right = fx_pip_value("USD/JPY", 100_000)["pip_value_usd"]
        wrong = 100_000 * 0.0001
        self.assertAlmostEqual(right / wrong, 100.0)

    def test_lots_are_derived_from_units(self):
        self.assertAlmostEqual(fx_pip_value("EUR/USD", 100_000)["lots"], 1.0)
        self.assertAlmostEqual(fx_pip_value("EUR/USD", 10_000)["lots"], 0.1)

    def test_a_crypto_pair_is_not_given_fx_semantics(self):
        self.assertIsNone(fx_pip_value("BTC/USD", 1)["pip_value_usd"])


class PerpLiquidationTests(unittest.TestCase):
    """Prompt test 13 — THE one."""

    def _pos(self, side="Long", lev=10.0, entry=100.0):
        return PerpPosition("BTC/USD", side, 1.0, entry, leverage=lev)

    def test_higher_leverage_liquidates_closer_to_entry(self):
        near = liquidation_price(self._pos(lev=50.0))
        far = liquidation_price(self._pos(lev=2.0))
        self.assertGreater(near, far)

    def test_a_long_liquidates_below_entry(self):
        self.assertLess(liquidation_price(self._pos("Long")), 100.0)

    def test_a_short_liquidates_above_entry(self):
        self.assertGreater(liquidation_price(self._pos("Short_10x")), 100.0)

    def test_liquidation_before_the_stop_is_a_liquidation(self):
        """A stop below the liquidation price never fills — the exchange
        closed the position first."""
        pos = self._pos("Long", lev=20.0)          # liquidates ~-4.5%
        out = resolve_exit(pos, stop_price=90.0, bar_high=100.0, bar_low=88.0)
        self.assertEqual(out["exit_reason"], FORCED_LIQUIDATION)
        self.assertGreater(out["exit_price"], 90.0)
        self.assertIn("never filled", out["detail"])

    def test_a_stop_inside_the_liquidation_boundary_still_wins(self):
        pos = self._pos("Long", lev=5.0)           # liquidates ~-19.5%
        out = resolve_exit(pos, stop_price=98.0, bar_high=100.0, bar_low=97.0)
        self.assertEqual(out["exit_reason"], STOP_EXIT)
        self.assertEqual(out["exit_price"], 98.0)

    def test_the_stop_that_would_not_have_saved_it_is_recorded(self):
        pos = self._pos("Long", lev=20.0)
        out = resolve_exit(pos, stop_price=90.0, bar_high=100.0, bar_low=88.0)
        self.assertTrue(out["stop_would_have_filled"])
        self.assertEqual(out["exit_reason"], FORCED_LIQUIDATION)

    def test_an_untouched_position_exits_neither_way(self):
        pos = self._pos("Long", lev=10.0)
        out = resolve_exit(pos, stop_price=90.0, bar_high=101.0, bar_low=99.0)
        self.assertIsNone(out["exit_reason"])

    def test_a_short_liquidates_on_the_way_up(self):
        pos = self._pos("Short_20x", lev=20.0)
        out = resolve_exit(pos, stop_price=110.0, bar_high=112.0, bar_low=100.0)
        self.assertEqual(out["exit_reason"], FORCED_LIQUIDATION)


class FundingTests(unittest.TestCase):
    """Prompt test 12 — funding is a transfer, and it has a sign."""

    def _pos(self, side):
        return PerpPosition("BTC/USD", side, 1.0, 100.0, leverage=10.0)

    def test_a_long_pays_positive_funding(self):
        f = funding_payment(self._pos("Long"), 24, rate_8h=0.0001)
        self.assertGreater(f["funding_usd"], 0)

    def test_a_short_RECEIVES_positive_funding(self):
        """A model that always charges funding understates every short."""
        f = funding_payment(self._pos("Short_10x"), 24, rate_8h=0.0001)
        self.assertLess(f["funding_usd"], 0)
        self.assertTrue(f["receives"])

    def test_a_negative_rate_flips_both_sides(self):
        long_f = funding_payment(self._pos("Long"), 24, rate_8h=-0.0001)
        short_f = funding_payment(self._pos("Short_10x"), 24, rate_8h=-0.0001)
        self.assertLess(long_f["funding_usd"], 0)
        self.assertGreater(short_f["funding_usd"], 0)

    def test_funding_scales_with_the_hold(self):
        a = funding_payment(self._pos("Long"), 8, rate_8h=0.0001)
        b = funding_payment(self._pos("Long"), 80, rate_8h=0.0001)
        self.assertAlmostEqual(b["funding_usd"] / a["funding_usd"], 10.0)

    def test_spot_has_no_funding(self):
        self.assertTrue(spot_has_no_funding("BTC/USD"))


if __name__ == "__main__":
    unittest.main()
