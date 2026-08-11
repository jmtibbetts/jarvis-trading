import unittest

from lib.congress_trading import (
    _delay_days, _to_iso, _trailing_amount, extract_ticker, parse_amount_range,
    parse_transaction_line,
)


class TrailingAmountTests(unittest.TestCase):
    def test_extracts_wrapped_upper_bound(self):
        """Real continuation line from a live filing: the transaction row ends
        '$15,001 -' and its upper bound wraps to the next line."""
        self.assertEqual(_trailing_amount("Stock (FERG) [ST] $50,000"), 50000.0)

    def test_takes_last_amount_when_several(self):
        self.assertEqual(_trailing_amount("foo $1,000 bar $250,000"), 250000.0)

    def test_no_amount_returns_none(self):
        self.assertIsNone(_trailing_amount("Stock (FERG) [ST]"))
        self.assertIsNone(_trailing_amount(""))
        self.assertIsNone(_trailing_amount(None))


class ToIsoTests(unittest.TestCase):
    def test_converts_house_mdy_to_iso(self):
        self.assertEqual(_to_iso("3/31/2026"), "2026-03-31")
        self.assertEqual(_to_iso("12/12/2025"), "2025-12-12")

    def test_invalid_returns_none(self):
        self.assertIsNone(_to_iso("not a date"))
        self.assertIsNone(_to_iso(None))


class ParseAmountRangeTests(unittest.TestCase):
    def test_parses_standard_range(self):
        self.assertEqual(parse_amount_range("$1,001 - $15,000"), (1001.0, 15000.0))

    def test_parses_larger_range(self):
        self.assertEqual(parse_amount_range("$15,001 - $50,000"), (15001.0, 50000.0))

    def test_truncated_upper_bound_returns_none_not_a_guess(self):
        """Wrapped cells lose the upper bound ('$15,001 -'). The low bound is
        real; the high must stay None rather than be invented."""
        low, high = parse_amount_range("$15,001 -")
        self.assertEqual(low, 15001.0)
        self.assertIsNone(high)

    def test_no_amount_returns_nones(self):
        self.assertEqual(parse_amount_range(""), (None, None))
        self.assertEqual(parse_amount_range(None), (None, None))


class ExtractTickerTests(unittest.TestCase):
    def test_extracts_ticker_and_asset_type(self):
        self.assertEqual(extract_ticker("Apple Inc. - Common Stock (AAPL) [ST]"), ("AAPL", "ST"))

    def test_extracts_dotted_ticker(self):
        """Real case from a live filing: Berkshire class B is printed BRK.B."""
        self.assertEqual(extract_ticker("Berkshire Hathaway Inc. New Common Stock (BRK.B) [ST]")[0], "BRK.B")

    def test_asset_without_parenthesized_ticker_returns_none(self):
        """Bonds, treasuries, and many funds are disclosed with no ticker.
        Inferring one from the company name would be fabrication."""
        self.assertEqual(extract_ticker("US Treasury Note 4% DUE 7/31/29"), (None, None))
        self.assertEqual(extract_ticker("Invesco QQQ [OT]"), (None, None))

    def test_empty_input(self):
        self.assertEqual(extract_ticker(None), (None, None))


class ParseTransactionLineTests(unittest.TestCase):
    def test_parses_real_reconstructed_line(self):
        """Exactly the line shape recovered from a live 2026 filing."""
        line = "SP Netflix, Inc. - Common Stock (NFLX) S 12/12/2025 01/06/2026 $1,001 - $15,000"
        r = parse_transaction_line(line)
        self.assertEqual(r["ticker"], "NFLX")
        self.assertEqual(r["owner"], "SP")
        self.assertEqual(r["transaction_code"], "S")
        self.assertEqual(r["transaction_label"], "Sale")
        self.assertEqual(r["transaction_date"], "2025-12-12")
        self.assertEqual(r["notification_date"], "2026-01-06")
        self.assertEqual(r["amount_low"], 1001.0)
        self.assertEqual(r["filing_delay_days"], 25)

    def test_parses_purchase(self):
        line = "SP Ferguson Enterprises Inc. Common P 12/12/2025 01/06/2026 $15,001 -"
        r = parse_transaction_line(line)
        self.assertEqual(r["transaction_code"], "P")
        self.assertEqual(r["transaction_label"], "Purchase")
        self.assertEqual(r["amount_low"], 15001.0)
        self.assertIsNone(r["amount_high"])

    def test_parses_partial_sale(self):
        line = "Apple Inc. - Common Stock (AAPL) [ST] S (partial) 03/16/2026 03/16/2026 $1,001 - $15,000"
        r = parse_transaction_line(line)
        self.assertEqual(r["transaction_code"], "S (partial)")
        self.assertEqual(r["transaction_label"], "Partial Sale")

    def test_line_without_two_dates_is_not_a_transaction(self):
        self.assertIsNone(parse_transaction_line("Digitally Signed: Hon. Richard W. Allen , 01/15/2026"))
        self.assertIsNone(parse_transaction_line("Some heading text"))

    def test_asset_with_no_ticker_still_parses_transaction(self):
        """The trade is still a real disclosed fact even when the security
        carries no ticker — it must not be dropped."""
        line = "JT US Treasury Note 4% DUE 7/31/29 P 02/18/2026 03/05/2026 $50,001 - $100,000"
        r = parse_transaction_line(line)
        self.assertIsNotNone(r)
        self.assertIsNone(r["ticker"])
        self.assertEqual(r["owner"], "JT")
        self.assertEqual(r["amount_low"], 50001.0)


class DelayDaysTests(unittest.TestCase):
    def test_computes_disclosure_lag(self):
        self.assertEqual(_delay_days("2025-12-12", "2026-01-06"), 25)

    def test_same_day_disclosure_is_zero_not_none(self):
        self.assertEqual(_delay_days("2026-03-16", "2026-03-16"), 0)

    def test_missing_date_returns_none(self):
        self.assertIsNone(_delay_days(None, "2026-01-06"))
        self.assertIsNone(_delay_days("2025-12-12", None))


if __name__ == "__main__":
    unittest.main()
