"""The debt side is a price too.

Before this axis existed, `project_health` summed borrows at face value and
grew them only by interest — borrowed USDC was debt that stayed exactly
$1.00 forever. The dangerous scenario is collateral falling WHILE the debt
gets more expensive, and the engine could not express it.
"""
import unittest

from lib.liquidation_matrix import (
    liquidation_boundary, project_health, stable_depeg_sensitivity,
    takes_stable_depeg,
)

BSOL_MINT = "bSo13r4TkiE4KumL71LsHTPpL2euBYLFx6h9HP3piy1"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def position(debt_symbol="USDC", debt_mint=USDC_MINT, debt=40_000):
    return {"assets": {
        "deposits": [{"symbol": "bSOL", "mint": BSOL_MINT,
                      "value_usd": 100_000, "liquidation_threshold_pct": 55}],
        "borrows": [{"symbol": debt_symbol, "mint": debt_mint,
                     "value_usd": debt}],
    }}


class DebtRepricesTests(unittest.TestCase):
    def test_debt_grows_when_the_stable_trades_above_par(self):
        par = project_health(position(), stable_depeg_pct=0.0)
        up = project_health(position(), stable_depeg_pct=2.0)
        self.assertAlmostEqual(par["debt_value_usd"], 40_000, places=2)
        self.assertAlmostEqual(up["debt_value_usd"], 40_800, places=2)
        self.assertLess(up["health_factor"], par["health_factor"])

    def test_debt_shrinks_when_the_stable_trades_below_par(self):
        down = project_health(position(), stable_depeg_pct=-1.0)
        self.assertAlmostEqual(down["debt_value_usd"], 39_600, places=2)
        self.assertGreater(down["health_factor"],
                           project_health(position())["health_factor"])

    def test_the_operators_two_scenarios_are_no_longer_the_same_number(self):
        """SOL -20% + bSOL/SOL -3% + USDC -1%  vs  the same with USDC +2%."""
        kw = dict(sol_shock_pct=20.0, depeg_shock_pct=3.0)
        good = project_health(position(), stable_depeg_pct=-1.0, **kw)
        bad = project_health(position(), stable_depeg_pct=+2.0, **kw)
        self.assertGreater(good["health_factor"], bad["health_factor"])

    def test_boundary_moves_forward_when_debt_gets_dearer(self):
        """The whole point: a dearer debt pulls liquidation closer."""
        at_par = liquidation_boundary(position(), depeg_shock_pct=3.0,
                                      stable_depeg_pct=0.0)
        dearer = liquidation_boundary(position(), depeg_shock_pct=3.0,
                                      stable_depeg_pct=2.0)
        self.assertIsNotNone(at_par)
        self.assertLess(dearer, at_par)
        # Materiality, not just direction — a 2% depeg is worth more than a
        # rounding error of SOL cushion.
        self.assertGreater(at_par - dearer, 1.0)


class BothSidesTests(unittest.TestCase):
    def test_pegged_collateral_takes_the_shock_too(self):
        pos = {"assets": {
            "deposits": [{"symbol": "USDC", "mint": USDC_MINT,
                          "value_usd": 100_000, "liquidation_threshold_pct": 80}],
            "borrows": [{"symbol": "SOL", "value_usd": 40_000}],
        }}
        up = project_health(pos, stable_depeg_pct=2.0)
        self.assertTrue(up["legs"][0]["took_stable_depeg"])
        self.assertAlmostEqual(up["collateral_value_usd"], 102_000, places=2)

    def test_stable_on_both_sides_nets_out(self):
        """A USDC-collateral/USDC-debt position is not directionally exposed
        to its own peg, and shocking one side only would invent risk."""
        pos = {"assets": {
            "deposits": [{"symbol": "USDC", "mint": USDC_MINT,
                          "value_usd": 100_000, "liquidation_threshold_pct": 80}],
            "borrows": [{"symbol": "USDT", "value_usd": 40_000}],
        }}
        par = project_health(pos, stable_depeg_pct=0.0)
        shocked = project_health(pos, stable_depeg_pct=3.0)
        self.assertAlmostEqual(par["health_factor"], shocked["health_factor"],
                               places=4)


class ScopeTests(unittest.TestCase):
    def test_gold_tokens_do_not_take_the_stable_axis(self):
        """XAUT is pegged, but to gold. Folding it into the stable axis
        would silently understate a gold position's real USD risk."""
        self.assertFalse(takes_stable_depeg({"symbol": "XAUT"}))
        self.assertFalse(takes_stable_depeg({"symbol": "PAXG"}))
        self.assertTrue(takes_stable_depeg({"symbol": "USDC"}))

    def test_unpegged_debt_is_untouched(self):
        pos = position(debt_symbol="SOL", debt_mint=None)
        par = project_health(pos, stable_depeg_pct=0.0)
        shocked = project_health(pos, stable_depeg_pct=5.0)
        self.assertEqual(par["debt_value_usd"], shocked["debt_value_usd"])
        self.assertFalse(shocked["debt_legs"][0]["took_stable_depeg"])

    def test_identity_is_by_mint_when_present(self):
        leg = {"symbol": "NOT-A-REAL-TICKER", "mint": USDC_MINT}
        self.assertTrue(takes_stable_depeg(leg))


class SensitivityTests(unittest.TestCase):
    def test_ladder_is_signed_in_both_directions(self):
        s = stable_depeg_sensitivity(position(), sol_shock_pct=10.0)
        shocks = [c["stable_depeg_pct"] for c in s["cells"]]
        self.assertTrue(any(x < 0 for x in shocks), "needs the favourable half")
        self.assertTrue(any(x > 0 for x in shocks), "needs the adverse half")

    def test_boundary_is_monotonic_in_the_stable_shock(self):
        s = stable_depeg_sensitivity(position(), depeg_shock_pct=2.0)
        bounds = [c["boundary_sol_pct"] for c in s["cells"]]
        self.assertTrue(all(b is not None for b in bounds))
        self.assertEqual(bounds, sorted(bounds, reverse=True),
                         "a dearer debt must never widen the cushion")

    def test_assumptions_are_declared(self):
        s = stable_depeg_sensitivity(position())
        self.assertIn("ASSUMED", s["basis"])
        self.assertIn("above par", s["sign_convention"].lower())


if __name__ == "__main__":
    unittest.main()
