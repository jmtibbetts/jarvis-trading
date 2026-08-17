"""Paper sizing: risk decides quantity, the stop decides leverage.

Three defects these pin (P0.6/7/8):
  - "conviction earns leverage": the composite score (measured inverted)
    chose 2-20x, then the STOP was tightened to fit that leverage's
    liquidation distance — the score literally moved the risk decision
  - a sizing rejection fell back to a flat-margin position and opened
    anyway, silently overriding "no free cash" and "exceeds risk budget"
  - non-futures sized margin-first (margin x leverage / price), so
    loss-at-stop was whatever fell out of the leverage choice instead of
    a budgeted number
"""
import inspect
import unittest

from lib.paper_engine import LIQ_STOP_BUFFER, max_safe_leverage, size_position


class LeverageFromTheStopTests(unittest.TestCase):
    def test_tight_stop_earns_more_leverage_than_wide(self):
        tight = max_safe_leverage(100.0, 99.0)     # 1% stop
        wide = max_safe_leverage(100.0, 90.0)      # 10% stop
        self.assertGreater(tight["cap"], wide["cap"])
        self.assertAlmostEqual(wide["liq_cap"], LIQ_STOP_BUFFER / 0.10, places=2)

    def test_explicit_request_is_a_ceiling_not_a_floor(self):
        g = max_safe_leverage(100.0, 90.0, requested=10.0)   # cap = 8 at 10% stop
        self.assertLessEqual(g["leverage"], g["cap"])
        self.assertLess(g["leverage"], 10.0)

    def test_no_risk_distance_means_1x(self):
        self.assertEqual(max_safe_leverage(100.0, 100.0)["leverage"], 1.0)
        self.assertEqual(max_safe_leverage(0, 0)["leverage"], 1.0)

    def test_the_stop_is_never_inside_the_liquidation_move(self):
        """At the returned leverage, the liquidation distance (1/L) must sit
        BEYOND the stop with the buffer to spare."""
        for stop in (99.5, 99.0, 97.0, 92.0):
            g = max_safe_leverage(100.0, stop)
            stop_frac = (100.0 - stop) / 100.0
            liq_frac = 1.0 / g["leverage"]
            self.assertLessEqual(stop_frac, liq_frac * LIQ_STOP_BUFFER + 1e-9,
                                 f"stop {stop}: stop_frac {stop_frac} vs liq {liq_frac}")


class RiskFirstSizingTests(unittest.TestCase):
    def test_loss_at_stop_equals_the_risk_budget(self):
        """Invariant #7 made structural: qty is solved FROM the budget, so
        a stop-out costs the budget — not whatever leverage produced."""
        out = size_position(equity=100_000, entry=100.0, stop=95.0,
                            leverage=1.0, free_cash=100_000, symbol="TEST/USD")
        self.assertTrue(out["ok"], out.get("reason"))
        expected_budget = 100_000 * 0.01   # TRADE_MARGIN_PCT slice
        self.assertAlmostEqual(out["loss_at_stop"], expected_budget,
                               delta=expected_budget * 0.05)

    def test_wide_and_tight_stops_risk_the_same_dollars(self):
        tight = size_position(100_000, 100.0, 99.0, 1.0, 100_000, symbol="TEST/USD")
        wide = size_position(100_000, 100.0, 92.0, 1.0, 100_000, symbol="TEST/USD")
        self.assertAlmostEqual(tight["loss_at_stop"], wide["loss_at_stop"],
                               delta=tight["loss_at_stop"] * 0.05)
        self.assertGreater(tight["qty"], wide["qty"])   # tighter stop, more units

    def test_no_stop_distance_is_a_rejection(self):
        out = size_position(100_000, 100.0, 100.0, 1.0, 100_000, symbol="TEST/USD")
        self.assertFalse(out["ok"])
        self.assertIn("stop distance", out["reason"])

    def test_cash_cap_scales_down_never_rejects_upward(self):
        """When margin busts the free-cash cap, quantity SHRINKS."""
        rich = size_position(100_000, 100.0, 99.0, 1.0, 100_000, symbol="TEST/USD")
        poor = size_position(100_000, 100.0, 99.0, 1.0, 500.0, symbol="TEST/USD")
        if poor["ok"]:
            self.assertLess(poor["qty"], rich["qty"])
            self.assertTrue(poor["capped_by_cash"])


class NoFlatFallbackTests(unittest.TestCase):
    """The open path is now PREPARE (authorize) then SETTLE (mutate), so the
    sizing guarantees are asserted over both halves together. Reading only
    the composed entry point would let the fallback reappear in whichever
    half the test stopped looking at."""

    def _open_path_src(self):
        import lib.paper_engine as pe
        return "\n".join(inspect.getsource(f) for f in
                         (pe.prepare_entry, pe.settle_position_entry,
                          pe.open_paper_position))

    def test_the_flat_margin_fallback_is_gone_from_the_open_path(self):
        src = self._open_path_src()
        self.assertNotIn("ASSET_CLASS_MARGIN.get", src,
                         "sizing rejection still falls back to a flat position")
        self.assertIn("paper sizing rejected", src)

    def test_conviction_leverage_is_out_of_the_open_path(self):
        self.assertNotIn("score_leverage(", self._open_path_src())

    def test_auto_sim_no_longer_scores_leverage(self):
        from lib import auto_simulator
        src = inspect.getsource(auto_simulator)
        self.assertNotIn("def score_leverage", src)
        self.assertIn("max_safe_leverage", src)


if __name__ == "__main__":
    unittest.main()
