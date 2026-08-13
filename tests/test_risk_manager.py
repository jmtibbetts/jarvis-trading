"""P0 correctness tests for direction-aware sizing, the risk invariant,
NO_TRADE, and cost-aware rejection.

Every test here corresponds to a bug that was live in production:

  - shorts were rejected at validation by long-only geometry
    (`stop >= entry or target <= entry`), so no short could ever be sized
  - risk_per_share = entry - stop went NEGATIVE for shorts, collapsing R:R
    to zero via the `if risk > 0 else 0` guard
  - max(200.0, ...) forced a $200 floor that overrode the risk budget
  - nothing checked that loss-at-stop stayed inside the budget after the
    regime/confidence multipliers and share rounding
  - no cost model existed, so a scalp whose spread+fees exceed the risk
    taken was sized as though it were free
"""
import unittest

from lib import trade_side
from lib.risk_manager import calculate_position_size, MAX_COST_R
from lib.transaction_costs import estimate_costs, net_expected_r

REGIME = {"risk": "low"}
EQUITY = 100_000.0


def size(**kw):
    sig = {"asset_symbol": "NVDA", "direction": "Long", "entry_price": 100.0,
           "stop_loss": 98.0, "target_price": 106.0, "confidence": 75}
    sig.update(kw)
    return calculate_position_size(sig, EQUITY, REGIME)


class DirectionValidationTests(unittest.TestCase):
    def test_valid_long_is_sized(self):
        r = size(direction="Long", entry_price=100, stop_loss=97, target_price=110)
        self.assertEqual(r.decision, "TRADE")
        self.assertEqual(r.side, "long")
        self.assertGreater(r.shares, 0)

    def test_valid_short_is_sized(self):
        """The headline bug: this returned 'Invalid price levels'."""
        r = size(direction="Short", entry_price=100, stop_loss=103, target_price=90)
        self.assertEqual(r.decision, "TRADE")
        self.assertEqual(r.side, "short")
        self.assertGreater(r.shares, 0)

    def test_invalid_long_rejected_with_reason(self):
        r = size(direction="Long", entry_price=100, stop_loss=103, target_price=110)
        self.assertEqual(r.decision, "NO_TRADE")
        self.assertIn("must sit BELOW", r.rejection_reason)

    def test_invalid_short_rejected_with_reason(self):
        r = size(direction="Short", entry_price=100, stop_loss=97, target_price=90)
        self.assertEqual(r.decision, "NO_TRADE")
        self.assertIn("must sit ABOVE", r.rejection_reason)

    def test_malformed_signal_is_never_silently_reinterpreted(self):
        """A 'short' with long geometry is rejected, not flipped to a long."""
        r = size(direction="Short", entry_price=100, stop_loss=97, target_price=110)
        self.assertEqual(r.decision, "NO_TRADE")
        self.assertEqual(r.shares, 0)

    def test_leveraged_direction_variants_resolve_to_a_side(self):
        for d in ("Short_5x", "Short_10x", "Short_Leveraged", "leveraged short"):
            self.assertTrue(trade_side.is_short(d), d)
        for d in ("Long", "Bounce", "Long_20x", "Long_Leveraged"):
            self.assertFalse(trade_side.is_short(d), d)


class RiskRewardTests(unittest.TestCase):
    def test_long_and_short_rr_are_symmetric(self):
        long_r = size(direction="Long", entry_price=100, stop_loss=98, target_price=106)
        short_r = size(direction="Short", entry_price=100, stop_loss=102, target_price=94)
        self.assertEqual(long_r.risk_reward, short_r.risk_reward)
        self.assertEqual(long_r.risk_reward, 3.0)

    def test_rr_is_never_negative_for_a_short(self):
        r = size(direction="Short", entry_price=100, stop_loss=101, target_price=95)
        self.assertGreater(r.risk_reward, 0)

    def test_degenerate_levels_give_zero_rr_not_a_crash(self):
        self.assertEqual(trade_side.rr_ratio(100, 100, 110), 0.0)


class RiskInvariantTests(unittest.TestCase):
    """loss_at_stop <= allowed account risk, always."""

    def test_loss_at_stop_never_exceeds_budget(self):
        for stop in (99.9, 99.0, 98.0, 95.0, 90.0):
            r = size(entry_price=100, stop_loss=stop, target_price=100 + (100 - stop) * 3)
            if r.decision == "TRADE":
                self.assertLessEqual(r.loss_at_stop, r.max_allowed_loss * 1.001,
                                     f"stop {stop} breached the budget")

    def test_invariant_holds_for_shorts_too(self):
        for stop in (100.1, 101.0, 102.0, 105.0):
            r = size(direction="Short", entry_price=100, stop_loss=stop,
                     target_price=100 - (stop - 100) * 3)
            if r.decision == "TRADE":
                self.assertLessEqual(r.loss_at_stop, r.max_allowed_loss * 1.001)

    def test_no_forced_minimum_position(self):
        """max(200.0, ...) used to deploy $200 even when risk said less."""
        r = size(entry_price=100, stop_loss=98, target_price=106)
        if r.decision == "TRADE":
            self.assertLessEqual(r.loss_at_stop, r.max_allowed_loss * 1.001)

    def test_equity_shares_round_down_not_up(self):
        r = size(asset_symbol="BRKA", entry_price=700_000, stop_loss=690_000,
                 target_price=730_000)
        self.assertEqual(r.decision, "NO_TRADE")
        self.assertEqual(r.shares, 0)


class NoTradeTests(unittest.TestCase):
    def test_zero_size_is_a_valid_answer(self):
        r = size(asset_symbol="BRKA", entry_price=700_000, stop_loss=690_000,
                 target_price=730_000)
        self.assertEqual(r.decision, "NO_TRADE")
        self.assertEqual(r.dollar_size, 0)
        self.assertTrue(r.rejection_reason)

    def test_missing_prices_reject(self):
        for kw in ({"entry_price": 0}, {"stop_loss": 0}, {"target_price": 0}):
            r = size(**kw)
            self.assertEqual(r.decision, "NO_TRADE")

    def test_negative_prices_reject(self):
        r = size(entry_price=-100, stop_loss=-98, target_price=-106)
        self.assertEqual(r.decision, "NO_TRADE")


class TransactionCostTests(unittest.TestCase):
    def test_same_fee_costs_more_R_on_a_tighter_stop(self):
        wide = estimate_costs("SOL/USD", 100.0, 95.0)
        tight = estimate_costs("SOL/USD", 100.0, 99.7)
        self.assertEqual(wide["total_pct"], tight["total_pct"])   # identical % cost
        self.assertGreater(tight["total_r"], wide["total_r"] * 10)  # wildly different R

    def test_scalp_with_unpayable_costs_is_rejected(self):
        r = size(asset_symbol="SOL/USD", entry_price=100, stop_loss=99.7, target_price=101)
        self.assertEqual(r.decision, "NO_TRADE")
        self.assertIn("Transaction costs", r.rejection_reason)

    def test_wide_stop_survives_the_cost_gate(self):
        r = size(asset_symbol="SOL/USD", entry_price=100, stop_loss=95, target_price=115)
        self.assertEqual(r.decision, "TRADE")

    def test_higher_cost_never_improves_net_ev(self):
        cheap = estimate_costs("NVDA", 100.0, 98.0)
        dear = estimate_costs("SOL/USD", 100.0, 98.0)
        self.assertGreaterEqual(dear["total_r"], cheap["total_r"])
        self.assertGreaterEqual(net_expected_r(0.4, cheap)["net_expected_r"],
                                net_expected_r(0.4, dear)["net_expected_r"])

    def test_limit_order_does_not_pay_the_spread(self):
        mkt = estimate_costs("SOL/USD", 100.0, 98.0, order_type="market")
        lim = estimate_costs("SOL/USD", 100.0, 98.0, order_type="limit", maker=True)
        self.assertLess(lim["total_r"], mkt["total_r"])

    def test_short_receives_positive_funding(self):
        long_c = estimate_costs("SOL/USD", 100.0, 98.0, hold_hours=24,
                                funding_rate_8h=0.0001, is_short=False)
        short_c = estimate_costs("SOL/USD", 100.0, 98.0, hold_hours=24,
                                 funding_rate_8h=0.0001, is_short=True)
        self.assertGreater(long_c["funding_pct"], 0)
        self.assertLess(short_c["funding_pct"], 0)

    def test_an_unquoted_funding_rate_is_looked_up_not_priced_at_zero(self):
        """Changed deliberately from asserting "unknown_rate_excluded".

        That name promised the cost was "not assumed zero" while the function
        returned exactly zero — labelled honestly, but still the number that
        flatters the trade. Excluding a cost IS pricing it at zero, and it
        flatters precisely the positions that hold longest, where funding
        matters most: a 1D setup estimates a 1-4 week hold, and four weeks at
        the standard 0.01%/8h is 0.84% of notional before leverage.

        The rate was being collected all along in
        crypto_derivatives_snapshots and simply never handed to this
        function. It is now read from there, falling back to the published
        baseline — never to zero, which is the one direction this model must
        not fail in.
        """
        c = estimate_costs("SOL/USD", 100.0, 98.0, hold_hours=24, funding_rate_8h=None)
        self.assertIn(c["funding_source"], ("measured", "default_baseline"))
        self.assertNotEqual(c["funding_pct"], 0.0)

    def test_a_non_crypto_symbol_still_has_no_funding(self):
        """Perpetual funding is not a thing on equities — the fallback must
        not invent one."""
        c = estimate_costs("AAPL", 100.0, 98.0, hold_hours=24, funding_rate_8h=None)
        self.assertEqual(c["funding_pct"], 0.0)

    def test_missing_risk_distance_blocks_the_verdict(self):
        c = estimate_costs("SOL/USD", 100.0, 100.0)
        self.assertIsNone(c["total_r"])
        self.assertFalse(net_expected_r(0.5, c)["tradeable"])


class CostCeilingTests(unittest.TestCase):
    def test_ceiling_is_a_fraction_of_risk_not_of_price(self):
        self.assertGreater(MAX_COST_R, 0)
        self.assertLessEqual(MAX_COST_R, 1.0)


if __name__ == "__main__":
    unittest.main()


class MinimumViableStopTests(unittest.TestCase):
    """A stop must be wide enough to pay for the round trip, or the trade
    is unprofitable by construction no matter how good the signal is."""

    def test_min_viable_stop_is_higher_for_crypto_than_equity(self):
        from lib.transaction_costs import min_viable_stop_pct
        self.assertGreater(min_viable_stop_pct("SOL/USD"), min_viable_stop_pct("NVDA"))

    def test_maker_orders_need_a_less_wide_stop(self):
        from lib.transaction_costs import min_viable_stop_pct
        taker = min_viable_stop_pct("SOL/USD", order_type="market")
        maker = min_viable_stop_pct("SOL/USD", order_type="limit", maker=True)
        self.assertLess(maker, taker)

    def test_a_stop_at_the_floor_lands_exactly_on_the_ceiling(self):
        from lib.transaction_costs import min_viable_stop_pct, estimate_costs
        floor = min_viable_stop_pct("SOL/USD", max_cost_r=MAX_COST_R)
        costs = estimate_costs("SOL/USD", 100.0, 100.0 * (1 - floor))
        self.assertAlmostEqual(costs["total_r"], MAX_COST_R, places=2)

    def test_widening_preserves_reward_to_risk(self):
        from lib.signal_levels import clamp_stop_to_atr
        sig = {"asset_symbol": "SOL/USD", "direction": "Long", "entry_price": 100.0,
               "stop_loss": 99.7, "target_price": 100.9, "timeframe": "1H"}
        before = abs(sig["target_price"] - 100.0) / abs(100.0 - sig["stop_loss"])
        out, clamped, _ = clamp_stop_to_atr(sig, atr_pct=2.0)
        after = abs(out["target_price"] - 100.0) / abs(100.0 - out["stop_loss"])
        self.assertTrue(clamped)
        self.assertAlmostEqual(before, after, places=2)

    def test_widening_works_for_shorts(self):
        from lib.signal_levels import clamp_stop_to_atr
        sig = {"asset_symbol": "SOL/USD", "direction": "Short", "entry_price": 100.0,
               "stop_loss": 100.3, "target_price": 99.1, "timeframe": "1H"}
        out, clamped, _ = clamp_stop_to_atr(sig, atr_pct=2.0)
        self.assertTrue(clamped)
        self.assertGreater(out["stop_loss"], 100.0)      # short stop stays ABOVE
        self.assertLess(out["target_price"], 100.0)      # target stays BELOW

    def test_impossible_combination_is_refused_not_stretched(self):
        """Cost floor above the ATR ceiling means no viable stop exists."""
        from lib.signal_levels import clamp_stop_to_atr
        sig = {"asset_symbol": "SOL/USD", "direction": "Long", "entry_price": 100.0,
               "stop_loss": 99.7, "target_price": 100.9, "timeframe": "5m"}
        out, clamped, reason = clamp_stop_to_atr(sig, atr_pct=0.4)
        self.assertFalse(clamped)
        self.assertTrue(out.get("untradeable_reason"))
        self.assertIn("no viable stop", reason)
