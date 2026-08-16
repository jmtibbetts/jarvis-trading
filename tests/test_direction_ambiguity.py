"""Recognisable directional text is not unambiguous directional evidence.

`LONGSHORT` contains "short". `BUYSELL` contains "buy". Substring matching
resolves both to a confident side, and WHICH side depends only on which
test happens to run first — so reordering the checks changes the answer
without changing the input.

That is why the fix is conflict DETECTION, not reordering. The order test
below is the point of the whole file: it permutes the marker vocabularies
and asserts the ambiguous cases stay ambiguous under every permutation.

The near-miss that motivated this: `BUYSELL` resolved LONG, because "sell"
was in neither the long nor the short vocabulary — so "buy" matched with
nothing to contradict it. `requires_paper_only()` would then have routed
it to the LIVE long-only path.
"""
import itertools
import unittest

from lib import trade_side
from lib.trade_side import (LONG, SHORT, SRC_AMBIGUOUS, SRC_UNRECOGNISED,
                            is_ambiguous_direction, parse_side_detailed,
                            parse_side_strict)

AMBIGUOUS = ["LONGSHORT", "SHORTLONG", "BUYSELL", "SELLBUY",
             "long_short", "short_long", "long/short", "buy/sell",
             "Long Short", "BUY_SELL"]

UNAMBIGUOUS_LONG = ["buy_maybe", "buy", "long", "Long", "LONG", "bull",
                    "bullish", "call", "Bounce", "Long_10x",
                    "Long_Leveraged", "breakout_up"]

UNAMBIGUOUS_SHORT = ["sell", "short", "Short", "SHORT", "bear", "bearish",
                     "put", "Short_20x", "Short_Leveraged", "sell_short"]

UNRECOGNISED = ["Aggressive_Moon_Mode", "sideways", "???", "n/a", "", None]


class AmbiguityTests(unittest.TestCase):
    def test_contradictory_values_are_unknown(self):
        for d in AMBIGUOUS:
            info = parse_side_detailed(d)
            self.assertIsNone(info["side"], f"{d!r} resolved to {info['side']}")
            self.assertTrue(info["ambiguous"], repr(d))
            self.assertEqual(info["source"], SRC_AMBIGUOUS, repr(d))

    def test_ambiguous_is_distinct_from_unrecognised(self):
        """Two sides stated and no side stated need different handling: the
        first is a data-quality defect worth surfacing."""
        self.assertTrue(is_ambiguous_direction("LONGSHORT"))
        self.assertFalse(is_ambiguous_direction("Aggressive_Moon_Mode"))
        self.assertEqual(parse_side_detailed("Aggressive_Moon_Mode")["source"],
                         SRC_UNRECOGNISED)

    def test_ambiguous_carries_both_sides_as_evidence(self):
        ev = parse_side_detailed("BUYSELL")["evidence"]
        self.assertIn("buy", ev)
        self.assertIn("sell", ev)

    def test_raw_value_is_preserved_for_provenance(self):
        self.assertEqual(parse_side_detailed("LONGSHORT")["raw"], "LONGSHORT")


class UnambiguousStillWorksTests(unittest.TestCase):
    def test_long_vocabulary(self):
        for d in UNAMBIGUOUS_LONG:
            self.assertEqual(parse_side_strict(d), LONG, repr(d))
            self.assertFalse(is_ambiguous_direction(d), repr(d))

    def test_short_vocabulary(self):
        for d in UNAMBIGUOUS_SHORT:
            self.assertEqual(parse_side_strict(d), SHORT, repr(d))
            self.assertFalse(is_ambiguous_direction(d), repr(d))

    def test_buy_maybe_stays_long(self):
        """Named explicitly: `buy` is an intentionally supported long alias,
        and `maybe` is not directional. This must NOT become UNKNOWN as a
        side effect of tightening the parser."""
        info = parse_side_detailed("buy_maybe")
        self.assertEqual(info["side"], LONG)
        self.assertFalse(info["ambiguous"])
        self.assertEqual(info["evidence"], ["buy"])

    def test_agreeing_tokens_are_not_a_conflict(self):
        """`sell_short` names one side twice, not two sides once."""
        self.assertEqual(parse_side_strict("sell_short"), SHORT)
        self.assertFalse(is_ambiguous_direction("sell_short"))

    def test_unrecognised_is_none_but_not_ambiguous(self):
        for d in UNRECOGNISED:
            self.assertIsNone(parse_side_strict(d), repr(d))
            self.assertFalse(is_ambiguous_direction(d), repr(d))


class OrderIndependenceTests(unittest.TestCase):
    """THE test. Ordering cannot fix ambiguity — it only changes which
    wrong answer wins. Permute both vocabularies and prove the outcome
    for contradictory input never moves."""

    def test_marker_order_cannot_change_an_ambiguous_outcome(self):
        orig_short = trade_side._SHORT_MARKERS
        orig_long = trade_side._LONG_MARKERS
        try:
            for sp in itertools.permutations(orig_short):
                for lp in itertools.permutations(orig_long):
                    trade_side._SHORT_MARKERS = sp
                    trade_side._LONG_MARKERS = lp
                    for d in AMBIGUOUS:
                        self.assertIsNone(
                            trade_side.parse_side_strict(d),
                            f"{d!r} resolved under short={sp} long={lp}")
        finally:
            trade_side._SHORT_MARKERS = orig_short
            trade_side._LONG_MARKERS = orig_long

    def test_marker_order_cannot_change_an_unambiguous_outcome(self):
        orig_short = trade_side._SHORT_MARKERS
        orig_long = trade_side._LONG_MARKERS
        try:
            for sp in itertools.permutations(orig_short):
                trade_side._SHORT_MARKERS = sp
                for d in UNAMBIGUOUS_LONG:
                    self.assertEqual(trade_side.parse_side_strict(d), LONG, repr(d))
                for d in UNAMBIGUOUS_SHORT:
                    self.assertEqual(trade_side.parse_side_strict(d), SHORT, repr(d))
        finally:
            trade_side._SHORT_MARKERS = orig_short
            trade_side._LONG_MARKERS = orig_long


class AmbiguousIsExcludedFromDecisionsTests(unittest.TestCase):
    """Ambiguous rows must not reach P&L, counts, scoring or execution."""

    def test_gate_refuses(self):
        from lib.gate import NO_TRADE, decide
        for d in AMBIGUOUS:
            self.assertEqual(
                decide({"direction": d, "entry_price": 100, "stop_loss": 98,
                        "target_price": 104, "asset_symbol": "AAPL"}).decision,
                NO_TRADE, repr(d))

    def test_validate_levels_refuses(self):
        for d in AMBIGUOUS:
            ok, _ = trade_side.validate_levels(d, 100, 98, 104)
            self.assertFalse(ok, repr(d))

    def test_ambiguous_never_routes_to_the_live_long_only_path(self):
        """The near-miss. BUYSELL resolved LONG, so this returned False and
        sent a contradictory direction to LIVE."""
        from lib.account_security import requires_paper_only
        for d in AMBIGUOUS:
            self.assertTrue(requires_paper_only(d),
                            f"{d!r} was allowed onto the live path")

    def test_unreadable_direction_never_routes_to_live_either(self):
        from lib.account_security import requires_paper_only
        for d in ("Aggressive_Moon_Mode", "", None):
            self.assertTrue(requires_paper_only(d), repr(d))

    def test_plain_long_still_reaches_live(self):
        from lib.account_security import requires_paper_only
        self.assertFalse(requires_paper_only("Long"))
        self.assertFalse(requires_paper_only("Bounce"))

    def test_signal_generation_drops_ambiguous(self):
        from jobs.generate_signals import normalize_signal
        for d in AMBIGUOUS:
            self.assertIsNone(
                normalize_signal({"asset_symbol": "AAPL", "direction": d,
                                  "entry_price": 100, "stop_loss": 98,
                                  "target_price": 104}, {}, {}), repr(d))


if __name__ == "__main__":
    unittest.main()
