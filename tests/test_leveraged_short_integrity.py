"""Short_10x must stay short through every arithmetic path.

THE BUG, in the paper mark-to-market loop:

    direction = pos["direction"].lower()
    side = -1 if direction == "short" else 1

`pos["direction"]` is the FULL direction string — "Short_10x", "Short_5x",
"Short_Leveraged". Comparing it to the literal "short" matched only the
plain case, so EVERY leveraged short fell to the else branch and was marked
to market as a LONG.

That inverts the sign of the P&L. A winning short reported a loss and a
losing short reported a gain — and every learning row derived from those
positions taught the system the opposite of what actually happened. It is
not a display defect; it is training corruption, and it gets worse the
more the desk uses leverage.

The second half: auto_simulator normalised the side of a position it was
about to OPEN with the permissive reader, whose documented behaviour is
"unknown becomes LONG". The `position.side == "short"` comparisons
downstream of it are correct precisely because the value is canonicalised
once at that boundary — which is exactly why that boundary must be strict.
"""
import unittest

from lib.trade_side import LONG, SHORT, parse_side_strict

LEVERAGED_SHORTS = ["Short_10x", "Short_5x", "Short_20x", "Short_2x",
                    "Short_Leveraged", "short_10x", "SHORT_10X"]
LEVERAGED_LONGS = ["Long_10x", "Long_5x", "Long_Leveraged"]


class SideParsingTests(unittest.TestCase):
    def test_every_leveraged_short_parses_short(self):
        for d in LEVERAGED_SHORTS:
            self.assertEqual(parse_side_strict(d), SHORT, d)

    def test_every_leveraged_long_parses_long(self):
        for d in LEVERAGED_LONGS:
            self.assertEqual(parse_side_strict(d), LONG, d)

    def test_the_old_comparison_would_have_failed(self):
        """Proves the fixture reproduces the original defect — without
        this, the assertions above could pass against broken code."""
        for d in LEVERAGED_SHORTS:
            old_side = -1 if d.lower() == "short" else 1
            self.assertEqual(old_side, 1,
                             f"{d} was marked to market as a LONG")


class MarkToMarketSignTests(unittest.TestCase):
    """The economics, not just the parse."""

    ENTRY = 100.0
    QTY = 10.0

    def _side_multiplier(self, direction):
        parsed = parse_side_strict(direction)
        return None if parsed is None else (-1 if parsed == SHORT else 1)

    def test_a_leveraged_short_profits_when_price_falls(self):
        for d in LEVERAGED_SHORTS:
            side = self._side_multiplier(d)
            pl = (90.0 - self.ENTRY) * self.QTY * side
            self.assertGreater(pl, 0, f"{d} lost money on a falling market")

    def test_a_leveraged_short_loses_when_price_rises(self):
        for d in LEVERAGED_SHORTS:
            side = self._side_multiplier(d)
            pl = (110.0 - self.ENTRY) * self.QTY * side
            self.assertLess(pl, 0, f"{d} profited from a rising market")

    def test_the_old_arithmetic_inverted_the_result(self):
        """A winning short reported a loss. This is what reached learning."""
        old_side = -1 if "short_10x" == "short" else 1        # noqa: PLR0133
        old_pl = (90.0 - self.ENTRY) * self.QTY * old_side
        new_pl = (90.0 - self.ENTRY) * self.QTY * self._side_multiplier("Short_10x")
        self.assertLess(old_pl, 0)
        self.assertGreater(new_pl, 0)
        self.assertEqual(old_pl, -new_pl, "the sign was exactly inverted")

    def test_percentage_pl_carries_the_same_sign(self):
        side = self._side_multiplier("Short_10x")
        plpc = ((90.0 - self.ENTRY) / self.ENTRY) * 100 * side
        self.assertAlmostEqual(plpc, 10.0)


class UnreadableDirectionTests(unittest.TestCase):
    def test_an_unreadable_direction_is_refused_not_marked_long(self):
        for d in ("Aggressive_Moon_Mode", "", None, "LONGSHORT"):
            self.assertIsNone(parse_side_strict(d), repr(d))

    def test_the_paper_loop_refuses_rather_than_assuming(self):
        import inspect

        from jobs import paper_trading
        src = inspect.getsource(paper_trading)
        self.assertNotIn('side = -1 if direction == "short" else 1', src,
                         "the inverting comparison is back")
        self.assertIn("parse_side_strict", src)


class AutoSimBoundaryTests(unittest.TestCase):
    """The side stored on a position must be strict at the boundary."""

    def test_auto_sim_side_is_strict(self):
        from lib.auto_simulator import _side
        self.assertEqual(_side("Short_10x"), SHORT)
        self.assertEqual(_side("Long_5x"), LONG)
        self.assertIsNone(_side("Aggressive_Moon_Mode"),
                          "an unopenable direction must not become a long")

    def test_auto_sim_does_not_use_the_permissive_reader(self):
        import ast
        import inspect

        from lib import auto_simulator
        src = inspect.getsource(auto_simulator._side)
        called = {getattr(n.func, "attr", None)
                  for n in ast.walk(ast.parse(src.lstrip()))
                  if isinstance(n, ast.Call)}
        self.assertIn("parse_side_strict", called)
        self.assertNotIn("normalize_side", called)

    def test_downstream_side_comparisons_stay_valid(self):
        """`position.side == "short"` is correct ONLY because the value is
        canonicalised once, at open. This pins that contract."""
        from lib.auto_simulator import _side
        for d in LEVERAGED_SHORTS:
            self.assertEqual(_side(d), "short", d)
        for d in LEVERAGED_LONGS:
            self.assertEqual(_side(d), "long", d)


if __name__ == "__main__":
    unittest.main()
