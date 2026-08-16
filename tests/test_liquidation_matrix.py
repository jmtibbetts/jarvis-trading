"""health(t, sol_shock, depeg_shock) — three axes, kept separate.

A single "distance to liquidation" number hides that the boundary MOVES.
Measured on a bSOL-collateralised USDC loan: the SOL decline needed to
liquidate it goes from -15.47% today to -10.54% in 90 days under adverse
carry, with no price movement whatsoever. Borrow interest does that.

The axes are independent for a reason. "SOL -20%, LSTs hold their peg" and
"SOL flat, bSOL depegs 7%" are different events with different cascades,
and a position can survive one and not the other.
"""
import unittest

from lib.liquidation_matrix import (LST_PROFILES, SOL_DERIVED_MINTS, is_lst,
                                    liquidation_boundary,
                                    position_risk_report, profile_for,
                                    project_health, stress_matrix)
from lib.reserve_economics import (borrow_apr_at, borrow_curve, carry_rates)


def position(bsol=3_160_000, sol=1_610_000, usdc=2_490_000):
    deps = []
    if bsol:
        deps.append({"symbol": "bSOL", "asset": BSOL_MINT, "value_usd": bsol,
                     "liquidation_threshold_pct": 55})
    if sol:
        deps.append({"symbol": "SOL", "asset": WSOL_MINT, "value_usd": sol,
                     "liquidation_threshold_pct": 75})
    return {"obligation": "t", "assets": {
        "deposits": deps, "borrows": [{"symbol": "USDC", "value_usd": usdc}]}}


BSOL_MINT = "bSo13r4TkiE4KumL71LsHTPpL2euBYLFx6h9HP3piy1"
MSOL_MINT = "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So"
WSOL_MINT = "So11111111111111111111111111111111111111112"

CARRY_COL = [{"supply_apr_pct": 0.01}]
CARRY_DEBT = [{"borrow_apr_pct": 4.33, "stressed_borrow_apr_pct": 30.40}]


def carries():
    return {s: carry_rates(CARRY_COL, CARRY_DEBT, scenario=s,
                           collateral_is_lst=True)
            for s in ("optimistic", "current", "zero", "adverse")}


class ReserveParametersTests(unittest.TestCase):
    def test_each_leg_uses_its_own_liquidation_threshold(self):
        """Kamino gives SOL 75% and bSOL 55%. Averaging them into a family
        factor would discard the protocol's own risk judgement — it already
        prices the LST as the riskier collateral."""
        r = project_health(position())
        thresholds = {l["symbol"]: l["liquidation_threshold_pct"] for l in r["legs"]}
        self.assertEqual(thresholds["bSOL"], 55)
        self.assertEqual(thresholds["SOL"], 75)

    def test_swapping_sol_for_bsol_lowers_health_at_equal_value(self):
        all_sol = project_health(position(bsol=0, sol=4_770_000))
        all_bsol = project_health(position(bsol=4_770_000, sol=0))
        self.assertGreater(all_sol["health_factor"], all_bsol["health_factor"])


class ShockIndependenceTests(unittest.TestCase):
    def test_a_plain_sol_leg_takes_the_sol_shock_but_not_the_depeg(self):
        r = project_health(position(), sol_shock_pct=10, depeg_shock_pct=10)
        legs = {l["symbol"]: l for l in r["legs"]}
        self.assertTrue(legs["SOL"]["took_sol_shock"])
        self.assertFalse(legs["SOL"]["took_depeg"])
        self.assertTrue(legs["bSOL"]["took_depeg"])

    def test_depeg_alone_still_damages_an_lst_position(self):
        base = project_health(position())["health_factor"]
        depeg = project_health(position(), depeg_shock_pct=10)["health_factor"]
        self.assertLess(depeg, base)

    def test_depeg_can_flip_a_surviving_position_into_liquidation(self):
        """SOL -15% survives at 1.0055; the same shock with 1% bSOL depeg
        does not. That distinction is the whole point of the second axis."""
        held = project_health(position(), sol_shock_pct=15, depeg_shock_pct=0)
        depegged = project_health(position(), sol_shock_pct=15, depeg_shock_pct=1)
        self.assertFalse(held["liquidatable"])
        self.assertTrue(depegged["liquidatable"])

    def test_a_position_with_no_lst_is_untouched_by_depeg(self):
        p = position(bsol=0, sol=4_770_000)
        a = project_health(p, depeg_shock_pct=10)["health_factor"]
        b = project_health(p, depeg_shock_pct=0)["health_factor"]
        self.assertEqual(a, b)


class CarryTests(unittest.TestCase):
    def test_carry_can_be_negative_not_just_a_tailwind(self):
        """LST yield helps and borrow interest hurts; on a stablecoin loan
        at high utilization the borrow side is larger."""
        c = carries()
        self.assertGreater(c["current"]["net_carry_apy_pct"], 0)
        self.assertLess(c["zero"]["net_carry_apy_pct"], 0)
        self.assertLess(c["adverse"]["net_carry_apy_pct"], -20)

    def test_the_adverse_case_comes_from_kaminos_own_curve(self):
        c = carries()["adverse"]
        self.assertAlmostEqual(c["debt_growth_apy_pct"], 30.40, places=2)
        self.assertIn("VERIFIED", c["assumptions"]["borrow_apr"])

    def test_staking_yield_is_labelled_an_assumption(self):
        c = carries()["current"]
        self.assertIn("ASSUMED", c["assumptions"]["staking_apy"])

    def test_the_liquidation_boundary_moves_with_time(self):
        cs = carries()
        now = liquidation_boundary(position(), days=0, carry=cs["adverse"])
        later = liquidation_boundary(position(), days=90, carry=cs["adverse"])
        self.assertIsNotNone(now)
        self.assertLess(later, now, "adverse carry must bring liquidation closer")

    def test_good_carry_pushes_the_boundary_away(self):
        cs = carries()
        now = liquidation_boundary(position(), days=0, carry=cs["optimistic"])
        later = liquidation_boundary(position(), days=90, carry=cs["optimistic"])
        self.assertGreater(later, now)

    def test_carry_scenarios_order_as_expected(self):
        cs = carries()
        self.assertGreater(cs["optimistic"]["net_carry_apy_pct"],
                           cs["current"]["net_carry_apy_pct"])
        self.assertGreater(cs["current"]["net_carry_apy_pct"],
                           cs["zero"]["net_carry_apy_pct"])
        self.assertGreater(cs["zero"]["net_carry_apy_pct"],
                           cs["adverse"]["net_carry_apy_pct"])


class BorrowCurveTests(unittest.TestCase):
    def test_the_curve_origin_is_not_discarded_as_padding(self):
        """An earlier version dropped every (0,0) pair, deleting the curve's
        ORIGIN. USDC's curve then began at 95% utilization, 89% found no
        bracket, and the rate fell through to the terminal 30.40% instead of
        the real 4.33% — a 7x error from filtering a legitimate point."""
        raw = bytearray(4856 + 64 + 11 * 8 + 8)
        pts = [(0, 0), (9500, 461), (10000, 3040)] + [(10000, 3040)] * 8
        for i, (u, r) in enumerate(pts):
            off = 4856 + 64 + i * 8
            raw[off:off + 4] = u.to_bytes(4, "little")
            raw[off + 4:off + 8] = r.to_bytes(4, "little")
        curve = borrow_curve(bytes(raw))
        self.assertEqual(curve[0], (0.0, 0.0))
        self.assertAlmostEqual(borrow_apr_at(curve, 89.15), 4.33, places=1)
        self.assertAlmostEqual(borrow_apr_at(curve, 99.0), 30.4, delta=15)

    def test_rates_are_clamped_outside_the_curve(self):
        curve = [(0.0, 1.0), (100.0, 30.0)]
        self.assertEqual(borrow_apr_at(curve, -5), 1.0)
        self.assertEqual(borrow_apr_at(curve, 150), 30.0)
        self.assertEqual(borrow_apr_at([], 50), 0.0)


class ProfileTests(unittest.TestCase):
    def test_each_lst_carries_its_own_basis_assumptions(self):
        """mSOL, bSOL and JitoSOL differ in liquidity and mechanics, so one
        shared basis assumption would repeat the collateral-factor mistake."""
        self.assertNotEqual(profile_for({"asset": BSOL_MINT}).stress_depeg_pct,
                            profile_for({"asset": MSOL_MINT}).stress_depeg_pct)
        for mint, p in LST_PROFILES.items():
            self.assertIn("ASSUMED", p.as_dict()["basis"])

    def test_plain_sol_is_sol_derived_but_not_an_lst(self):
        self.assertIn(WSOL_MINT, SOL_DERIVED_MINTS)
        self.assertFalse(is_lst({"asset": WSOL_MINT, "symbol": "SOL"}))
        self.assertTrue(is_lst({"asset": BSOL_MINT, "symbol": "bSOL"}))

    def test_the_bsol_etf_ticker_cannot_borrow_the_lst_profile(self):
        """BSOL is also the Bitwise Solana Staking ETF, a US-listed equity,
        and JARVIS trades both asset classes. A ticker-keyed profile would
        eventually apply liquid-staking depeg assumptions to an ETF share."""
        etf = {"symbol": "BSOL", "asset": None}          # equity, no mint
        self.assertIsNone(profile_for(etf))
        self.assertFalse(is_lst(etf))
        # And the real LST is still found, by mint, whatever its ticker says.
        lst = {"symbol": "WHATEVER", "asset": BSOL_MINT}
        self.assertTrue(is_lst(lst))
        self.assertEqual(profile_for(lst).symbol, "bSOL")


class ReportTests(unittest.TestCase):
    def test_the_report_decomposes_rather_than_scoring(self):
        r = position_risk_report(position(), carry_by_scenario=carries())
        for key in ("current_health_factor", "static_sol_liquidation_pct",
                    "carry", "boundary_over_time", "matrix", "primary_risk"):
            self.assertIn(key, r)
        self.assertIn("VERIFIED", r["provenance"]["liquidation_thresholds"])
        self.assertIn("ASSUMED", r["provenance"]["staking_yield"])
        self.assertIn("MODELLED", r["provenance"]["projections"])

    def test_sol_beta_dominates_a_mostly_sol_position(self):
        r = position_risk_report(position(), carry_by_scenario=carries())
        self.assertEqual(r["primary_risk"], "SOL beta")

    def test_a_position_without_priced_legs_reports_unavailable(self):
        r = position_risk_report({"assets": {"deposits": [], "borrows": []}},
                                 carry_by_scenario={})
        self.assertFalse(r["available"])

    def test_the_matrix_covers_both_axes(self):
        m = stress_matrix(position())
        self.assertEqual(len(m["rows"]), len(m["sol_shocks"]))
        self.assertEqual(len(m["rows"][0]["cells"]), len(m["depeg_shocks"]))


if __name__ == "__main__":
    unittest.main()
