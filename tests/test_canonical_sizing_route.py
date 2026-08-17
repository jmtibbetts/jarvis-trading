"""The UI renders sizing; it never derives it.

The frontend calculator computed:

    qty      = riskDollars / |entry - stop|
    notional = qty * entry

That is share arithmetic — correct for equities and crypto, wrong for every
contract product, because risk per unit is price distance TIMES the
multiplier.

THE PART THAT EXPLAINS WHY NOBODY NOTICED: the NOTIONAL was accidentally
right. Notional is qty x entry x multiplier and qty is budget/(distance x
multiplier), so the multiplier cancels. The dollar exposure on screen
matched reality exactly.

The QUANTITY did not, and quantity is what goes on an order ticket. With a
$1,000 risk budget the old calculator showed "100" for gold. One GC
contract is $100 of risk per $1 move; 100 of them against a $10 stop is
$100,000 of risk — a hundred times what was authorised, displayed beside a
notional figure that was completely correct.
"""
import unittest

from app.routers.platform import platform_size


class ContractSizingTests(unittest.TestCase):
    def test_gold_sizes_in_whole_contracts(self):
        r = platform_size(symbol="GC=F", entry=2000.0, stop=1990.0,
                          equity=100_000.0, risk_pct=1.0)
        self.assertTrue(r["ok"])
        self.assertEqual(r["quantity_unit"], "CONTRACTS")
        self.assertEqual(r["quantity"], float(int(r["quantity"])))
        self.assertEqual(r["multiplier"], 100)

    def test_the_old_js_quantity_would_have_been_100x_the_risk(self):
        """The measured divergence, not a hypothetical one."""
        entry, stop, budget = 2000.0, 1990.0, 1000.0
        r = platform_size(symbol="GC=F", entry=entry, stop=stop,
                          equity=100_000.0, risk_pct=1.0)
        js_qty = budget / abs(entry - stop)          # the old formula
        real_risk = js_qty * abs(entry - stop) * r["multiplier"]
        self.assertEqual(js_qty, 100.0)
        self.assertEqual(r["quantity"], 1.0)
        self.assertAlmostEqual(real_risk / budget, 100.0)

    def test_mes_is_five_times_the_risk_the_old_formula_believed(self):
        entry, stop, budget = 5000.0, 4990.0, 1000.0
        r = platform_size(symbol="MES=F", entry=entry, stop=stop,
                          equity=100_000.0, risk_pct=1.0)
        js_qty = budget / abs(entry - stop)
        self.assertEqual(js_qty, 100.0)
        self.assertEqual(r["quantity"], 20.0)
        self.assertAlmostEqual(js_qty / r["quantity"], r["multiplier"])

    def test_the_notional_was_accidentally_right(self):
        """Documented so nobody 'fixes' it back — the multiplier cancels
        between qty and notional, which is exactly why the bug survived."""
        entry, stop = 5000.0, 4990.0
        r = platform_size(symbol="MES=F", entry=entry, stop=stop,
                          equity=100_000.0, risk_pct=1.0)
        js_notional = (1000.0 / abs(entry - stop)) * entry
        self.assertAlmostEqual(r["notional_usd"], js_notional, places=2)

    def test_risk_matches_the_budget_that_was_authorised(self):
        r = platform_size(symbol="MES=F", entry=5000.0, stop=4990.0,
                          equity=100_000.0, risk_pct=1.0)
        self.assertLessEqual(r["risk_usd"], 1000.0 + 1e-6)


class UnitSizingTests(unittest.TestCase):
    def test_equities_size_in_shares_and_are_unchanged(self):
        r = platform_size(symbol="NVDA", entry=100.0, stop=98.0,
                          equity=100_000.0, risk_pct=1.0)
        self.assertEqual(r["quantity_unit"], "SHARES")
        self.assertEqual(r["multiplier"], 1.0)
        self.assertAlmostEqual(r["quantity"], 500.0)

    def test_crypto_may_be_fractional(self):
        r = platform_size(symbol="BTC/USD", entry=100_000.0, stop=97_000.0,
                          equity=100_000.0, risk_pct=1.0)
        self.assertEqual(r["quantity_unit"], "COINS")
        self.assertNotEqual(r["quantity"], float(int(r["quantity"])))

    def test_fx_sizes_in_fx_units(self):
        r = platform_size(symbol="EUR/USD", entry=1.08, stop=1.075,
                          equity=100_000.0, risk_pct=1.0)
        self.assertEqual(r["quantity_unit"], "FX_UNITS")


class RefusalTests(unittest.TestCase):
    def test_an_unspecified_future_is_refused_not_sized(self):
        """A contract whose units are unknown cannot be sized. The old
        calculator would have returned a confident number."""
        r = platform_size(symbol="UNKNOWN=F", entry=100.0, stop=99.0,
                          equity=100_000.0, risk_pct=1.0)
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "UNSUPPORTED_INSTRUMENT")
        self.assertNotIn("quantity", r)

    def test_a_budget_below_one_contract_refuses_rather_than_rounding_up(self):
        r = platform_size(symbol="GC=F", entry=2000.0, stop=1990.0,
                          equity=1_000.0, risk_pct=1.0)
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "BELOW_MIN_SIZE")
        self.assertIn("more risk than authorised", r["detail"])

    def test_no_risk_distance_is_refused(self):
        r = platform_size(symbol="NVDA", entry=100.0, stop=100.0,
                          equity=100_000.0, risk_pct=1.0)
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "NO_RISK_DISTANCE")


class ContractShapeTests(unittest.TestCase):
    def test_the_unit_is_always_returned(self):
        """"20" means nothing without knowing shares from contracts."""
        for sym in ("NVDA", "BTC/USD", "MES=F", "EUR/USD"):
            r = platform_size(symbol=sym, entry=100.0, stop=98.0,
                              equity=1_000_000.0, risk_pct=1.0)
            if r["ok"]:
                self.assertTrue(r["quantity_unit"], sym)

    def test_provenance_is_stated(self):
        r = platform_size(symbol="NVDA", entry=100.0, stop=98.0,
                          equity=100_000.0, risk_pct=1.0)
        self.assertIn("CALCULATED", r["provenance"])

    def test_an_explicit_risk_usd_overrides_the_percentage(self):
        r = platform_size(symbol="NVDA", entry=100.0, stop=98.0,
                          equity=100_000.0, risk_pct=1.0, risk_usd=250.0)
        self.assertAlmostEqual(r["risk_budget_usd"], 250.0)
        self.assertAlmostEqual(r["quantity"], 125.0)


class FrontendNoLongerDerivesTests(unittest.TestCase):
    def test_the_svelte_calculator_no_longer_does_the_arithmetic(self):
        import pathlib
        src = pathlib.Path(
            "frontend/src/lib/sections/PositionsPaper.svelte").read_text(
            encoding="utf-8")
        self.assertNotIn("const qty = riskDollars / perUnitRisk", src)
        self.assertNotIn("const notional = qty * entry", src)
        self.assertIn("api.sizePosition", src)


if __name__ == "__main__":
    unittest.main()
