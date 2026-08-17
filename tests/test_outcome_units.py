"""R IS NOT PERCENT.

`record_trade_outcome(realized=...)` computed the percentage column as

    pnl_pct = realized.net_r * 100

which is a different unit wearing the same name. $50 of profit on $100 of
initial risk is +0.5R; recording that as "+50%" asserts a denominator nobody
supplied. Beneath it a fallback guessed NOTIONAL, while the paper book's own
documented contract is ROI on COMMITTED MARGIN (lib/paper_engine: raw_pnl /
margin * 100). So one column could hold any of three incompatible numbers
depending on which branch ran, and every one of them fed pattern memory's
running average.

Three units, kept apart:

    dollars     net_pnl_usd
    R           net_r, against initial risk
    percent     net_return_pct, against a STATED basis

An unknown percentage is NULL. MISSING IS NOT ZERO.
"""
import unittest

from lib.realized_outcome import RealizedOutcome


class TheThreeUnitsAreDistinctTests(unittest.TestCase):

    # NOT _outcome: unittest.TestCase uses self._outcome internally for
    # result collection, and shadowing it fails with the memorable
    # "'_Outcome' object is not callable".
    def _make(self, **kw):
        base = dict(symbol="BTC/USD", side="long", quantity=1.0,
                    net_pnl_usd=50.0, initial_risk_usd=100.0, net_r=0.5,
                    outcome="WIN")
        base.update(kw)
        return RealizedOutcome(**base)

    def test_realized_outcome_carries_a_percentage_and_its_basis(self):
        """A percentage without a denominator is not a percentage."""
        o = self._make(net_return_pct=4.39, return_pct_basis="MARGIN")
        self.assertEqual(o.net_r, 0.5)
        self.assertEqual(o.net_return_pct, 4.39)
        self.assertEqual(o.return_pct_basis, "MARGIN")

    def test_the_percentage_defaults_to_unknown_rather_than_zero(self):
        o = self._make()
        self.assertIsNone(o.net_return_pct)
        self.assertIsNone(o.return_pct_basis)

    def test_half_an_r_is_not_fifty_percent(self):
        """The arithmetic identity that made the bug invisible: R and percent
        coincide only when the denominator happens to equal initial risk."""
        o = self._make(net_pnl_usd=50.0, initial_risk_usd=100.0, net_r=0.5,
                          net_return_pct=4.39, return_pct_basis="MARGIN")
        self.assertNotEqual(o.net_return_pct, o.net_r * 100)


class LearningRecordsRatherThanDerivesTests(unittest.TestCase):
    """What actually reaches the trade_outcomes row."""

    def _record(self, realized):
        from unittest.mock import patch
        captured = {}

        class _Conn:
            def execute(self, *a, **k):
                # The INSERT carries a bound-parameter dict as its 2nd arg.
                if len(a) > 1 and isinstance(a[1], dict) and "pnl_pct" in a[1]:
                    captured.update(a[1])
                return self
            def fetchone(self):
                return None

        class _Engine:
            def begin(self):
                from contextlib import contextmanager

                @contextmanager
                def _cm():
                    yield _Conn()
                return _cm()

        import lib.learning_engine as LE
        with patch.object(LE, "_lazy_ensure", lambda: None), \
             patch.object(LE, "_ensure_tables", lambda conn: None), \
             patch.object(LE, "_refresh_signal_accuracy", lambda *a, **k: None), \
             patch("app.database.engine", _Engine()):
            LE.record_trade_outcome(
                symbol="BTC/USD", asset_class="crypto", direction="Long",
                entry_price=100.0, exit_price=105.0, qty=1.0,
                exit_reason="take_profit", realized=realized, paper_mode=True)
        return captured

    def test_a_stated_percentage_is_recorded_verbatim(self):
        o = RealizedOutcome(symbol="BTC/USD", side="long", quantity=1.0,
                            net_pnl_usd=50.0, net_r=0.5,
                            net_return_pct=4.39, return_pct_basis="MARGIN",
                            outcome="WIN")
        row = self._record(o)
        self.assertEqual(row.get("pnl_usd"), 50.0)
        self.assertEqual(row.get("pnl_pct"), 4.39,
                         "learning must record the stated percentage, not derive one")

    def test_an_absent_percentage_is_recorded_as_null_not_as_r_times_100(self):
        """THE BUG. net_r=0.5 with no stated basis used to become 50.0."""
        o = RealizedOutcome(symbol="BTC/USD", side="long", quantity=1.0,
                            net_pnl_usd=50.0, net_r=0.5, outcome="WIN")
        row = self._record(o)
        self.assertIsNone(row.get("pnl_pct"),
                          "an unknown percentage must be NULL, not R*100 and not 0")
        self.assertEqual(row.get("pnl_usd"), 50.0, "dollars are still known")

    def test_learning_does_not_recompute_the_dollar_result(self):
        """THE EXCHANGE DECIDES, LEARNING RECORDS. The entry/exit prices
        passed alongside would imply a different number; the canonical
        net_pnl_usd must win."""
        o = RealizedOutcome(symbol="BTC/USD", side="long", quantity=1.0,
                            net_pnl_usd=7.25, net_r=0.1, outcome="WIN")
        row = self._record(o)
        self.assertEqual(row.get("pnl_usd"), 7.25)


class AnUnreadableSideIsNotALongTests(unittest.TestCase):
    """MISSING IS NOT LONG.

    `startswith("short")` turned a missing or unparseable direction into
    False, indistinguishable from a known long. An outcome whose side nobody
    recorded was then priced and signed as a long, so a winning short could
    enter the ledger as a loss."""

    def test_r_is_not_computable_without_a_readable_side(self):
        from lib.expectancy import _r_of
        self.assertIsNone(_r_of(100, 98, 105, None))
        self.assertIsNone(_r_of(100, 98, 105, ""))
        self.assertIsNone(_r_of(100, 98, 105, "sideways"))

    def test_a_readable_side_still_computes(self):
        from lib.expectancy import _r_of
        self.assertAlmostEqual(_r_of(100, 98, 106, "Long"), 3.0, places=6)
        self.assertAlmostEqual(_r_of(100, 102, 94, "Short"), 3.0, places=6)

    def test_leveraged_short_spellings_are_read_as_short(self):
        """"Short_10x" is the spelling that a membership test missed."""
        from lib.expectancy import _r_of
        for spelling in ("Short", "short", "Short_10x", "SHORT_5X"):
            with self.subTest(direction=spelling):
                self.assertAlmostEqual(_r_of(100, 102, 94, spelling), 3.0, places=6)

    def test_the_cost_model_refuses_an_unknown_side(self):
        """Costs are asymmetric between long and short, so pricing an unknown
        side as a long can only be right by luck."""
        from lib.expectancy import evaluate
        r = evaluate({"asset_symbol": "NVDA", "asset_class": "equity",
                      "direction": None, "timeframe": "4H",
                      "entry_price": 224.0, "stop_loss": 218.0})
        self.assertEqual(r["verdict"], "UNKNOWN")
        self.assertIn("unreadable", r["reason"])


if __name__ == "__main__":
    unittest.main()
