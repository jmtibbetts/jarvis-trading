import unittest
from types import SimpleNamespace
from unittest.mock import patch

from jobs.telegram_bot import (
    _format_trade_setup, _horizon, _price_levels, _signal_keyboard,
)


class PriceLevelsTests(unittest.TestCase):
    def test_sub_cent_spacing_on_dollar_asset_is_distinguishable(self):
        """Regression for the user-reported bug: an asset near $1 with
        sub-cent level spacing rendered as "$1.00 / $1.00 / $1.00" — entry,
        stop, and target all identical on screen."""
        entry, stop, target = _price_levels(1.0034, 1.0051, 1.0018)
        self.assertEqual(len({entry, stop, target}), 3)
        self.assertEqual(entry, "$1.0034")

    def test_normal_spacing_keeps_two_decimals(self):
        self.assertEqual(_price_levels(82.04, 82.09, 81.97),
                         ("$82.04", "$82.09", "$81.97"))

    def test_large_prices_keep_thousands_separators(self):
        entry, _, _ = _price_levels(63955.7, 64100.0, 63700.0)
        self.assertEqual(entry, "$63,955.70")

    def test_micro_prices_resolve(self):
        levels = _price_levels(0.00001234, 0.00001250, 0.00001200)
        self.assertEqual(len(set(levels)), 3)

    def test_all_identical_levels_do_not_crash(self):
        # Degenerate input (no spacing at all) — format plainly, no exception.
        levels = _price_levels(1.0, 1.0, 1.0)
        self.assertEqual(levels, ("$1.00", "$1.00", "$1.00"))


class HorizonTests(unittest.TestCase):
    def _sig(self, setup_type=None, timeframe=""):
        return SimpleNamespace(setup_type=setup_type, timeframe=timeframe)

    def test_setup_type_wins_when_present(self):
        self.assertEqual(_horizon(self._sig(setup_type="swing", timeframe="5m")), "SWING")

    def test_derived_from_timeframe_when_absent(self):
        self.assertEqual(_horizon(self._sig(timeframe="5m")), "SCALP")
        self.assertEqual(_horizon(self._sig(timeframe="1H")), "INTRADAY")
        self.assertEqual(_horizon(self._sig(timeframe="4H")), "SWING")
        self.assertEqual(_horizon(self._sig(timeframe="1D")), "POSITION")

    def test_unknown_timeframe_defaults_to_position(self):
        self.assertEqual(_horizon(self._sig(timeframe="")), "POSITION")


class FormatTradeSetupTests(unittest.TestCase):
    def _sig(self, **over):
        base = dict(
            asset_symbol="ETH/USD", direction="Short", confidence=78,
            composite_score=71.2, timeframe="5m", entry_price=1873.59,
            target_price=1870.85, stop_loss=1875.23,
            reasoning="Rejection at VWAP.", signal_source="scanner_crypto",
            earnings_risk=False, setup_type="scalp", status="Active", paper_mode=True,
        )
        base.update(over)
        return SimpleNamespace(**base)

    @patch("jobs.telegram_bot._asset_name", return_value=None)
    def test_message_carries_horizon_and_timeframe(self, _):
        text = _format_trade_setup(self._sig())
        self.assertIn("SCALP", text)
        self.assertIn("5m chart", text)
        # every setup carries an expected hold duration for its timeframe
        self.assertIn("expect", text)
        self.assertIn("1 hr", text)

    @patch("jobs.telegram_bot._asset_name", return_value=None)
    def test_message_carries_risk_and_target_pct(self, _):
        text = _format_trade_setup(self._sig())
        plain = text.replace("<b>", "").replace("</b>", "")
        self.assertIn("% risk", plain)
        self.assertIn("% target", plain)
        self.assertIn("Score 71", plain)

    @patch("jobs.telegram_bot._asset_name", return_value="Ethereum")
    def test_asset_name_shown_when_known(self, _):
        self.assertIn("Ethereum", _format_trade_setup(self._sig()))

    @patch("jobs.telegram_bot._asset_name", return_value=None)
    def test_earnings_risk_warning_included(self, _):
        text = _format_trade_setup(self._sig(earnings_risk=True))
        self.assertIn("Earnings within days", text)


class SignalKeyboardTests(unittest.TestCase):
    def test_live_button_absent_by_default(self):
        kb = _signal_keyboard("abc")
        flat = [b["callback_data"] for row in kb["inline_keyboard"] for b in row]
        self.assertNotIn("sig:live:abc", flat)
        self.assertIn("sig:paper:abc", flat)
        self.assertIn("sig:deny:abc", flat)

    def test_live_button_first_when_eligible(self):
        kb = _signal_keyboard("abc", live_eligible=True)
        self.assertEqual(kb["inline_keyboard"][0][0]["callback_data"], "sig:live:abc")


if __name__ == "__main__":
    unittest.main()
