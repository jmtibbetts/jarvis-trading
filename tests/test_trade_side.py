"""Canonical side semantics, and proof that every module now agrees.

Before lib/trade_side.py, modules derived the side ad hoc and DISAGREED:
`startswith("short")` said LONG for "leveraged short" while `"short" in`
said SHORT. paper_engine._DIR_ALIASES maps exactly that string to
Short_Leveraged, so it is a real input — meaning such a signal executed as
a short while ev_model and signal_evaluation attributed its outcome to
longs, quietly corrupting both books' statistics.
"""
import unittest

from lib import trade_side


class NormalizationTests(unittest.TestCase):
    def test_the_string_that_split_the_codebase(self):
        for d in ("leveraged short", "leveraged_short", " short", "SHORT "):
            self.assertTrue(trade_side.is_short(d), d)

    def test_all_known_direction_variants(self):
        shorts = ["Short", "Short_Leveraged", "Short_5x", "Short_10x", "Short_20x",
                  "short-leveraged", "leveraged short", "SHORT"]
        longs = ["Long", "Bounce", "Long_Leveraged", "Long_5x", "Long_10x",
                 "Long_20x", "leveraged long", "LONG"]
        for d in shorts:
            self.assertEqual(trade_side.normalize_side(d), trade_side.SHORT, d)
        for d in longs:
            self.assertEqual(trade_side.normalize_side(d), trade_side.LONG, d)

    def test_unknown_defaults_to_long(self):
        for d in (None, "", "sideways", "???"):
            self.assertEqual(trade_side.normalize_side(d), trade_side.LONG)

    def test_every_module_agrees_with_the_canonical_answer(self):
        """The whole point of the audit: one input, one answer, everywhere."""
        from lib.signal_identity import direction_side as identity_side
        from lib.auto_simulator import _side as autosim_side
        for d in ("leveraged short", "Short_10x", "Long", "Bounce", "short"):
            expected = trade_side.normalize_side(d)
            self.assertEqual(identity_side(d), expected, f"signal_identity disagrees on {d!r}")
            self.assertEqual(autosim_side(d), expected, f"auto_simulator disagrees on {d!r}")


class LeverageParsingTests(unittest.TestCase):
    def test_explicit_multiplier_is_read(self):
        self.assertEqual(trade_side.leverage_from_direction("Long_10x"), 10.0)
        self.assertEqual(trade_side.leverage_from_direction("Short_5x"), 5.0)
        self.assertEqual(trade_side.leverage_from_direction("Short_Leveraged"), 2.0)

    def test_plain_direction_carries_no_instruction(self):
        self.assertIsNone(trade_side.leverage_from_direction("Long"))
        self.assertIsNone(trade_side.leverage_from_direction("Short"))


class GeometryTests(unittest.TestCase):
    def test_long_layout(self):
        ok, why = trade_side.validate_levels("Long", 100, 98, 106)
        self.assertTrue(ok, why)

    def test_short_layout(self):
        ok, why = trade_side.validate_levels("Short", 100, 102, 94)
        self.assertTrue(ok, why)

    def test_each_side_rejects_the_other_layout(self):
        self.assertFalse(trade_side.validate_levels("Long", 100, 102, 94)[0])
        self.assertFalse(trade_side.validate_levels("Short", 100, 98, 106)[0])

    def test_distances_are_positive_for_both_sides(self):
        self.assertEqual(trade_side.risk_distance(100, 98), 2.0)
        self.assertEqual(trade_side.risk_distance(100, 102), 2.0)
        self.assertEqual(trade_side.rr_ratio(100, 98, 106), 3.0)
        self.assertEqual(trade_side.rr_ratio(100, 102, 94), 3.0)

    def test_loss_at_stop_is_direction_independent(self):
        self.assertEqual(trade_side.loss_at_stop(10, 100, 98), 20.0)
        self.assertEqual(trade_side.loss_at_stop(10, 100, 102), 20.0)

    def test_missing_and_malformed_values_are_rejected(self):
        for args in (("Long", 0, 98, 106), ("Long", 100, 0, 106),
                     ("Long", 100, 98, 0), ("Long", -100, -98, -106),
                     ("Long", "x", 98, 106)):
            self.assertFalse(trade_side.validate_levels(*args)[0], args)


if __name__ == "__main__":
    unittest.main()
