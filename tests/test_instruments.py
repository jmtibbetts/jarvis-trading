"""Contract specifications — the layer that makes simulated futures fills
transferable to a real broker.

Every assertion here corresponds to something that was wrong before this
module existed, when the whole system assumed notional = qty * price:

  - a 1-point ES move booked $1 instead of $50 (5x-1000x P&L error across
    the futures complex, silently corrupting the learning data)
  - positions of 0.37 contracts, which no venue can fill
  - percentage-of-notional fees charged $1,942 on an ES contract that
    really costs $4.50 round trip
  - margin derived from notional/leverage rather than the exchange's fixed
    dollar requirement
"""
import unittest

from lib.instruments import (ContractSpec, FUTURES_SPECS, get_spec, is_futures,
                             contract_notional, snap_to_tick, whole_contracts,
                             commission_for, margin_required,
                             max_affordable_contracts, suggest_micro)


class MultiplierTests(unittest.TestCase):
    def test_es_point_is_fifty_dollars(self):
        self.assertEqual(get_spec("ES=F").multiplier, 50)
        self.assertEqual(contract_notional("ES=F", 7766.75), 7766.75 * 50)

    def test_micro_is_one_tenth_of_its_parent(self):
        for micro, parent in (("MES=F", "ES=F"), ("MNQ=F", "NQ=F"), ("MYM=F", "YM=F")):
            self.assertAlmostEqual(get_spec(micro).multiplier,
                                   get_spec(parent).multiplier / 10, places=6)
            self.assertEqual(get_spec(micro).micro_of, parent)

    def test_shares_and_coins_have_multiplier_one(self):
        self.assertEqual(get_spec("NVDA").multiplier, 1.0)
        self.assertEqual(get_spec("SOL/USD").multiplier, 1.0)
        self.assertEqual(contract_notional("NVDA", 100.0, 10), 1000.0)

    def test_every_micro_points_at_a_real_parent(self):
        for sym, spec in FUTURES_SPECS.items():
            if spec.micro_of:
                self.assertIn(spec.micro_of, FUTURES_SPECS, f"{sym} orphaned")

    def test_suggest_micro_finds_the_retail_tier(self):
        self.assertEqual(suggest_micro("ES=F"), "MES=F")
        self.assertIsNone(suggest_micro("MES=F"))


class TickTests(unittest.TestCase):
    def test_off_tick_prices_are_snapped(self):
        self.assertEqual(snap_to_tick("ES=F", 7766.83), 7766.75)   # 0.25 tick
        self.assertEqual(snap_to_tick("YM=F", 53880.4), 53880.0)   # 1.00 tick

    def test_direction_lets_a_stop_move_to_the_safer_side(self):
        self.assertEqual(snap_to_tick("ES=F", 7766.60, "down"), 7766.50)
        self.assertEqual(snap_to_tick("ES=F", 7766.60, "up"), 7766.75)

    def test_tick_value_is_dollars_per_tick(self):
        self.assertAlmostEqual(get_spec("ES=F").tick_value, 12.50, places=2)

    def test_equities_and_crypto_pass_through_sanely(self):
        self.assertEqual(snap_to_tick("SOL/USD", 1.234567), 1.234567)  # no tick
        self.assertEqual(snap_to_tick("NVDA", 100.004), 100.0)         # penny tick


class WholeContractTests(unittest.TestCase):
    def test_fractional_futures_are_impossible(self):
        self.assertEqual(whole_contracts("ES=F", 0.37), 0.0)
        self.assertEqual(whole_contracts("ES=F", 3.9), 3.0)

    def test_crypto_stays_fractional(self):
        self.assertAlmostEqual(whole_contracts("SOL/USD", 0.37), 0.37)

    def test_equities_round_down(self):
        self.assertEqual(whole_contracts("NVDA", 3.9), 3.0)


class CostTests(unittest.TestCase):
    def test_futures_pay_per_contract_not_per_dollar(self):
        per_contract = commission_for("ES=F", 1)
        as_percentage = contract_notional("ES=F", 7766.75) * 0.0025 * 2
        self.assertAlmostEqual(per_contract, 4.50, places=2)
        self.assertLess(per_contract, as_percentage / 100)   # ~430x cheaper

    def test_cost_model_uses_the_per_contract_path(self):
        from lib.transaction_costs import estimate_costs
        c = estimate_costs("ES=F", 7766.75, 7766.75 * 0.98)
        naive = 0.0025 * 2
        self.assertLess(c["fees_pct"], naive / 100)


class MarginTests(unittest.TestCase):
    def test_futures_margin_is_fixed_dollars_per_contract(self):
        self.assertEqual(margin_required("ES=F", 2), get_spec("ES=F").initial_margin * 2)

    def test_margin_ignores_leverage_for_futures(self):
        self.assertEqual(margin_required("ES=F", 1, 7766.75, leverage=1),
                         margin_required("ES=F", 1, 7766.75, leverage=20))

    def test_equity_margin_still_uses_leverage(self):
        self.assertAlmostEqual(margin_required("NVDA", 10, 100.0, leverage=2), 500.0)

    def test_affordability_matches_the_account(self):
        self.assertGreater(max_affordable_contracts("MES=F", 100_000, 20), 0)
        self.assertEqual(max_affordable_contracts("ES=F", 5_000, 20), 0)


class SizingIntegrationTests(unittest.TestCase):
    """The end the operator actually sees."""

    def _size(self, sym, px):
        from lib.paper_engine import size_position
        return size_position(100_000, px, px * 0.98, 10, 100_000, symbol=sym)

    def test_full_size_contract_is_refused_and_points_at_the_micro(self):
        r = self._size("ES=F", 7766.75)
        self.assertFalse(r["ok"])
        self.assertIn("MES=F", r["reason"])

    def test_micro_contract_sizes_and_respects_the_risk_budget(self):
        r = self._size("MES=F", 7766.75)
        self.assertTrue(r["ok"], r.get("reason"))
        self.assertGreaterEqual(r["qty"], 1)
        self.assertEqual(r["qty"], int(r["qty"]))          # whole contracts
        self.assertLessEqual(r["loss_at_stop"], 1_000 * 1.001)   # the 1% budget

    def test_notional_reflects_the_multiplier(self):
        r = self._size("MES=F", 7766.75)
        self.assertAlmostEqual(r["notional"], 7766.75 * r["qty"] * 5, places=2)


class PnlTests(unittest.TestCase):
    def test_pnl_uses_the_multiplier(self):
        from lib.paper_engine import _calc_pnl
        for sym, expected in (("ES=F", 500.0), ("MES=F", 50.0), ("NVDA", 10.0)):
            pnl, _ = _calc_pnl(100.0, 110.0, 1, 1, 1, 1000, symbol=sym)
            self.assertAlmostEqual(pnl, expected, places=2, msg=sym)

    def test_shorts_invert_correctly_with_multipliers(self):
        from lib.paper_engine import _calc_pnl
        pnl, _ = _calc_pnl(100.0, 90.0, 1, -1, 1, 1000, symbol="ES=F")
        self.assertAlmostEqual(pnl, 500.0, places=2)


if __name__ == "__main__":
    unittest.main()
