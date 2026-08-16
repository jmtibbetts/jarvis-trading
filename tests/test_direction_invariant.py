"""P0 — unknown side never buys anything.

The audit's invariant, verbatim: for every decision-capable path, an
unknown direction must result in REFUSAL, never Long.

Three defects behind it, all in code whose job was to prevent exactly this:

  validate_levels()   called the PERMISSIVE is_short(), so a validator whose
                      whole purpose is refusing malformed input would check
                      "Aggressive_Moon_Mode" against long-side geometry and
                      pass it
  stop_side_ok()      same
  generate_signals    repaired unknown directions to "Long" in three places,
                      and on the long-only live path rewrote ANY other
                      direction to "Long" — including an explicit "Short".
                      That does not default a missing value, it INVERTS a
                      stated one: the model says sell, the desk buys.
"""
import unittest

from lib import trade_side
from lib.trade_side import (LONG, SHORT, parse_side_strict, stop_side_ok,
                            validate_levels)

GARBAGE = ["Aggressive_Moon_Mode", "", None, "sideways", "???", "n/a",
           # Names BOTH sides, so it has stated neither.
           "LONGSHORT"]


class StrictParseTests(unittest.TestCase):
    def test_garbage_is_none_never_long(self):
        for d in GARBAGE:
            self.assertIsNone(parse_side_strict(d), repr(d))

    def test_real_directions_still_parse(self):
        for d in ("Long", "long", "Bounce", "Long_10x", "Long_Leveraged"):
            self.assertEqual(parse_side_strict(d), LONG, d)
        for d in ("Short", "short", "Short_20x", "Short_Leveraged", "Bearish"):
            self.assertEqual(parse_side_strict(d), SHORT, d)

    def test_permissive_normalize_still_defaults_for_display(self):
        """normalize_side is a READING rule and keeps its default — the
        point is that no decision path uses it."""
        self.assertEqual(trade_side.normalize_side("garbage"), LONG)


class ValidateLevelsTests(unittest.TestCase):
    """The validator must not contain the repair it exists to prevent."""

    def test_unknown_direction_is_refused(self):
        for d in GARBAGE:
            ok, why = validate_levels(d, entry=100, stop=98, target=104)
            self.assertFalse(ok, f"{d!r} validated as a long")
            self.assertIn("direction", (why or "").lower())

    def test_a_valid_long_layout_still_passes(self):
        ok, why = validate_levels("Long", 100, 98, 104)
        self.assertTrue(ok, why)

    def test_a_valid_short_layout_still_passes(self):
        ok, why = validate_levels("Short", 100, 102, 96)
        self.assertTrue(ok, why)

    def test_a_short_layout_under_an_unknown_direction_is_still_refused(self):
        """Geometry must not be allowed to imply the side."""
        ok, _ = validate_levels("moon", 100, 102, 96)
        self.assertFalse(ok)

    def test_layout_errors_are_still_caught_for_known_sides(self):
        self.assertFalse(validate_levels("Long", 100, 102, 104)[0])
        self.assertFalse(validate_levels("Short", 100, 98, 96)[0])


class StopSideTests(unittest.TestCase):
    def test_unknown_direction_has_no_correct_stop_side(self):
        for d in GARBAGE:
            self.assertFalse(stop_side_ok(d, 100, 98), repr(d))
            self.assertFalse(stop_side_ok(d, 100, 102), repr(d))

    def test_known_sides_unchanged(self):
        self.assertTrue(stop_side_ok("Long", 100, 98))
        self.assertFalse(stop_side_ok("Long", 100, 102))
        self.assertTrue(stop_side_ok("Short", 100, 102))
        self.assertFalse(stop_side_ok("Short", 100, 98))


class GateRefusalTests(unittest.TestCase):
    """The capital boundary itself."""

    def test_gate_refuses_an_unknown_direction(self):
        from lib.gate import NO_TRADE, decide
        for d in GARBAGE:
            v = decide({"direction": d, "entry_price": 100,
                        "stop_loss": 98, "target_price": 104,
                        "asset_symbol": "AAPL"})
            self.assertEqual(v.decision, NO_TRADE, repr(d))

    def test_gate_v8_never_takes_on_an_unknown_direction(self):
        from lib.gate import gate_v8
        for d in GARBAGE:
            self.assertFalse(gate_v8({"direction": d, "entry_price": 100,
                                      "stop_loss": 98, "target_price": 104,
                                      "asset_symbol": "AAPL"})["take"], repr(d))


class SignalGenerationTests(unittest.TestCase):
    """normalize_signal must drop, not repair."""

    def _norm(self, direction, is_paper=False):
        from jobs.generate_signals import normalize_signal
        return normalize_signal(
            {"asset_symbol": "AAPL", "direction": direction,
             "entry_price": 100, "stop_loss": 98, "target_price": 104},
            {}, {}, is_paper=is_paper)

    def test_garbage_produces_no_signal(self):
        for d in GARBAGE:
            self.assertIsNone(self._norm(d), f"{d!r} became a signal")
            self.assertIsNone(self._norm(d, is_paper=True), f"{d!r} (paper)")

    def test_an_explicit_short_is_never_flipped_to_long(self):
        """THE inversion. The live path is long-only and used to rewrite
        'Short' to 'Long' — the model says sell and the desk buys."""
        out = self._norm("Short")
        if out is not None:
            self.assertNotEqual(out.get("direction"), "Long",
                                "a Short was rewritten into a Long")
            self.assertEqual(parse_side_strict(out.get("direction")), SHORT)
            self.assertTrue(out.get("paper_mode"),
                            "a short on the long-only path belongs in paper")

    def test_a_plain_long_still_works(self):
        out = self._norm("Long")
        self.assertIsNotNone(out)
        self.assertEqual(out["direction"], "Long")


class NoPermissiveParsingInValidatorsTests(unittest.TestCase):
    """Guard the guards: these two must not regress to is_short()."""

    def test_validators_use_strict_parsing(self):
        import ast
        import inspect
        for fn in (validate_levels, stop_side_ok):
            src = inspect.getsource(fn)
            called = {getattr(n.func, "attr", None) or getattr(n.func, "id", None)
                      for n in ast.walk(ast.parse(src.lstrip()))
                      if isinstance(n, ast.Call)}
            self.assertIn("parse_side_strict", called, fn.__name__)
            self.assertNotIn("is_short", called, fn.__name__)


if __name__ == "__main__":
    unittest.main()
