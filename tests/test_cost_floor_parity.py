"""Every path that builds signal levels must apply the economics floor.

`clamp_stop_to_atr` carries three guards — the cost floor, the ATR band and
the per-contract viability refusal — and it was reachable from exactly ONE
of the two level-building jobs. jobs/scan_opportunities.py built stops from
raw 1.5x ATR and handed them straight to the gate.

Measured over two days of live candidates before the fix:

    7,106 of 7,895 gate rejections were on COST
    5,054 of those came from source='scanner'
    every absurd one was on that path — DAI/USD at 3,397R, USDT/USD at
    72R, NZDUSD=X at 37.9R

The gate was refusing them correctly. They should never have been built.

This test discovers level builders BY SHAPE rather than by a hardcoded
list, so a third one cannot land without either applying the floor or
failing here — the same discipline as test_concentration_parity.
"""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SEARCH_DIRS = ("jobs", "lib", "app")

# A module builds levels if it derives a stop from an ATR multiple.
BUILDS_LEVELS = re.compile(r"stop_loss\s*=\s*round\(.*atr", re.IGNORECASE)
APPLIES_FLOOR = re.compile(r"clamp_stop_to_atr")

# Modules that legitimately compute an ATR-derived stop without being a
# signal factory. Each needs a reason, not just an entry.
EXEMPT = {
    "lib/signal_levels.py": "defines the floor itself",
}


def _py_files():
    for d in SEARCH_DIRS:
        for p in (ROOT / d).rglob("*.py"):
            if "__pycache__" in p.parts:
                continue
            yield p


class CostFloorParityTests(unittest.TestCase):

    def test_every_level_builder_applies_the_floor(self):
        offenders = []
        for p in _py_files():
            rel = p.relative_to(ROOT).as_posix()
            if rel in EXEMPT:
                continue
            src = p.read_text(encoding="utf-8", errors="ignore")
            if BUILDS_LEVELS.search(src) and not APPLIES_FLOOR.search(src):
                offenders.append(rel)
        self.assertEqual(offenders, [], (
            "these modules build an ATR-derived stop but never call "
            "clamp_stop_to_atr, so their signals skip the cost floor, the "
            f"ATR band and the per-contract viability refusal: {offenders}"))

    def test_the_scanner_is_actually_covered_by_this_test(self):
        """Guard against the guard: if the scanner is refactored so the
        BUILDS_LEVELS pattern stops matching, this test would silently pass
        while covering nothing."""
        src = (ROOT / "jobs" / "scan_opportunities.py").read_text(
            encoding="utf-8", errors="ignore")
        self.assertTrue(BUILDS_LEVELS.search(src),
                        "scan_opportunities no longer matches the level-builder "
                        "shape — update BUILDS_LEVELS or this suite is inert")
        self.assertTrue(APPLIES_FLOOR.search(src))

    def test_scanner_refuses_pegged_assets_before_building_levels(self):
        src = (ROOT / "jobs" / "scan_opportunities.py").read_text(
            encoding="utf-8", errors="ignore")
        self.assertIn("is_stablecoin", src,
                      "a pegged asset has no ATR to build a stop from")


class ScannerLevelTests(unittest.TestCase):
    """The floor, exercised through the same call the scanner makes."""

    def test_a_sub_pip_fx_stop_is_widened_or_refused(self):
        from lib.signal_levels import clamp_stop_to_atr
        sig = {"asset_symbol": "NZDUSD=X", "direction": "Long",
               "entry_price": 0.589379, "stop_loss": 0.589306,
               "target_price": 0.589600, "order_type": "market"}
        out, clamped, reason = clamp_stop_to_atr(sig, atr_pct=0.05)
        if out.get("untradeable_reason"):
            return                                  # refused outright is fine
        widened = abs(out["entry_price"] - out["stop_loss"]) / out["entry_price"]
        from lib.transaction_costs import min_viable_stop_pct
        self.assertGreaterEqual(widened, min_viable_stop_pct("NZDUSD=X") * 0.999,
                                f"stop left below the cost floor: {reason}")

    def test_reward_to_risk_survives_the_widening(self):
        """Widening the stop alone would quietly convert a 3:1 into a 1:1."""
        from lib.signal_levels import clamp_stop_to_atr
        sig = {"asset_symbol": "AAPL", "direction": "Long",
               "entry_price": 200.0, "stop_loss": 199.6, "target_price": 201.2,
               "order_type": "market"}
        before = abs(sig["target_price"] - sig["entry_price"]) / abs(
            sig["entry_price"] - sig["stop_loss"])
        out, clamped, _ = clamp_stop_to_atr(sig, atr_pct=2.0)
        self.assertTrue(clamped)
        after = abs(out["target_price"] - out["entry_price"]) / abs(
            out["entry_price"] - out["stop_loss"])
        self.assertAlmostEqual(before, after, places=2)


if __name__ == "__main__":
    unittest.main()
