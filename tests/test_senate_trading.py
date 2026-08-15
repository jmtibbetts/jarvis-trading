"""Senate eFD parsing, against real markup captured from the live site.

The Senate half of STOCK Act coverage. Fixtures are trimmed from actual
eFD report pages so a layout change breaks a test rather than silently
producing zero rows — the failure mode that matters here is a parser that
returns [] and lets the job log "0 trades saved" as success.
"""
import unittest

from lib.senate_trading import (TRANSACTION_CODES, _to_iso, delay_days,
                                parse_amount_range, parse_ptr_html)

# Real shape: 9 columns, bonds carry coupon/maturity inside the asset cell,
# ticker is "--" when the instrument has none.
REPORT_HTML = """
<table class="table">
<thead><tr><th>&#35;</th><th>Transaction Date</th><th>Owner</th><th>Ticker</th>
<th>Asset Name</th><th>Asset Type</th><th>Type</th><th>Amount</th>
<th>Comment</th></tr></thead>
<tbody>
<tr><td>1</td><td>07/02/2026</td><td>Self</td><td>AAPL</td>
<td>Apple Inc</td><td>Stock</td><td>Purchase</td>
<td>$15,001 - $50,000</td><td>--</td></tr>
<tr><td>2</td><td>07/03/2026</td><td>Spouse</td><td>--</td>
<td>Virginia Beach VA GO Public Improvement Bonds
    Rate/Coupon: 5.0% Matures: 2031-02-01</td>
<td>Municipal Security</td><td>Sale (Partial)</td>
<td>$100,001 - $250,000</td><td>--</td></tr>
<tr><td>3</td><td>07/04/2026</td><td>Self</td><td>NVDA</td>
<td>NVIDIA Corp</td><td>Stock</td><td>Sale (Full)</td>
<td>$1,001 - $15,000</td><td>--</td></tr>
</tbody></table>
"""


class ParseReportTests(unittest.TestCase):
    def setUp(self):
        self.out = parse_ptr_html(REPORT_HTML)
        self.rows = self.out["rows"]

    def test_every_row_parses(self):
        self.assertEqual(len(self.rows), 3)
        self.assertEqual(self.out["rows_seen"], 3)
        self.assertEqual(self.out["rows_unparsed"], 0)

    def test_dates_are_iso(self):
        self.assertEqual([r["transaction_date"] for r in self.rows],
                         ["2026-07-02", "2026-07-03", "2026-07-04"])

    def test_transaction_types_map_to_house_codes(self):
        """Both chambers land in ONE column, so the codes must agree — a
        Senate 'Sale (Full)' and a House 'S' are the same fact."""
        self.assertEqual([r["transaction_code"] for r in self.rows],
                         ["P", "S", "S"])
        self.assertEqual(self.rows[1]["transaction_label"], "Partial Sale")

    def test_missing_ticker_is_none_not_a_dash(self):
        """'--' is the site's placeholder. Storing it would make a municipal
        bond look like a ticker called '--' in every ticker aggregation."""
        self.assertIsNone(self.rows[1]["ticker"])
        self.assertEqual(self.rows[0]["ticker"], "AAPL")

    def test_amount_ranges(self):
        self.assertEqual((self.rows[0]["amount_low"], self.rows[0]["amount_high"]),
                         (15001.0, 50000.0))
        self.assertEqual(self.rows[2]["amount_low"], 1001.0)

    def test_owner_is_kept(self):
        self.assertEqual([r["owner"] for r in self.rows],
                         ["Self", "Spouse", "Self"])

    def test_a_layout_change_does_not_silently_return_nothing(self):
        """A table with too few columns yields no rows AND no false
        confidence — rows_seen stays 0, so the job cannot report success."""
        out = parse_ptr_html("<table><tr><td>a</td><td>b</td></tr></table>")
        self.assertEqual(out["rows"], [])
        self.assertEqual(out["rows_seen"], 0)


class HelperTests(unittest.TestCase):
    def test_amount_range_single_bound(self):
        self.assertEqual(parse_amount_range("$1,000,001 -"), (1000001.0, None))
        self.assertEqual(parse_amount_range(None), (None, None))
        self.assertEqual(parse_amount_range("--"), (None, None))

    def test_iso_dates(self):
        self.assertEqual(_to_iso("08/14/2026"), "2026-08-14")
        self.assertIsNone(_to_iso("not a date"))
        self.assertIsNone(_to_iso(None))

    def test_delay_days(self):
        """The gap between trading and disclosing is the single most
        informative number in the filing."""
        self.assertEqual(delay_days("2026-07-02", "2026-08-14"), 43)
        self.assertIsNone(delay_days(None, "2026-08-14"))

    def test_every_mapped_type_has_a_code_and_a_label(self):
        for raw, (code, label) in TRANSACTION_CODES.items():
            self.assertTrue(code and label, raw)


if __name__ == "__main__":
    unittest.main()
