"""Unknown direction is a refusal, never a long.

normalize_side() maps anything without a short-marker to LONG — fine for
reading legacy display rows, lethal on an order path: a typo'd or novel
LLM direction string silently BOUGHT things. Worse, the live executor's
own inline check was `startswith(("long", "b"))`, which read "Bearish"
as a buy. Order creation now requires an affirmative parse.
"""
import inspect
import unittest

from lib.trade_side import LONG, SHORT, normalize_side, parse_side_strict


class StrictParseTests(unittest.TestCase):
    def test_affirmative_longs(self):
        for d in ("Long", "long_10x", "Bounce", "BUY", "bullish", "Long Leveraged"):
            self.assertEqual(parse_side_strict(d), LONG, d)

    def test_affirmative_shorts(self):
        for d in ("Short", "short_5x", "BEARISH", "put spread", "Short_Leveraged"):
            self.assertEqual(parse_side_strict(d), SHORT, d)

    def test_the_bearish_buy_bug_is_dead(self):
        """'Bearish' startswith 'b' — the old live-executor check submitted
        it as a BUY. Strict parse reads it as SHORT."""
        self.assertEqual(parse_side_strict("Bearish"), SHORT)

    def test_unknown_is_none_not_long(self):
        for d in (None, "", "  ", "sideways", "hold", "neutral", "5x", "???"):
            self.assertIsNone(parse_side_strict(d), d)

    def test_permissive_still_exists_for_legacy_reads(self):
        self.assertEqual(normalize_side("???"), LONG)   # documented, display-only


class OrderPathsUseStrictTests(unittest.TestCase):
    """The refusal must live in every order path, not just the library."""

    def test_live_executor_gates_on_strict_long(self):
        from jobs import execute_signals
        src = inspect.getsource(execute_signals)
        self.assertIn("parse_side_strict", src)
        self.assertNotIn('startswith(("long", "b"))', src)

    def test_paper_engine_refuses_unparseable(self):
        from lib.paper_engine import open_paper_position
        out = open_paper_position({"asset_symbol": "BTC/USD",
                                   "direction": "totally novel phrasing",
                                   "entry_price": 60000}, current_price=60000)
        self.assertIn("error", out)
        self.assertIn("refusing", out["error"])

    def test_auto_sim_skips_unparseable(self):
        from lib import auto_simulator
        self.assertIn("parse_side_strict", inspect.getsource(auto_simulator))


if __name__ == "__main__":
    unittest.main()
