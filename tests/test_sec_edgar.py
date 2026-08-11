import os
import unittest
from unittest.mock import patch

from lib import sec_edgar

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")

# A trimmed real atom feed snippet (structure verified live against
# https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4&...&output=atom
# while building this) — two entries for the SAME filing (issuer + reporting-owner
# party, as SEC's feed emits), plus one entry for a second filing.
SAMPLE_ATOM = """<?xml version="1.0" encoding="ISO-8859-1" ?>
<feed xmlns="http://www.w3.org/2005/Atom">
<title>Latest Filings</title>
<entry>
<title>4 - Example Biotherapeutics, Inc. (0002056611) (Issuer)</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/2056611/000152367226000007/0001523672-26-000007-index.htm"/>
<updated>2026-08-10T20:00:10-04:00</updated>
<category scheme="https://www.sec.gov/" label="form type" term="4"/>
<id>urn:tag:sec.gov,2008:accession-number=0001523672-26-000007</id>
</entry>
<entry>
<title>4 - SMITH TODD N (0001523672) (Reporting)</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/1523672/000152367226000007/0001523672-26-000007-index.htm"/>
<updated>2026-08-10T20:00:10-04:00</updated>
<category scheme="https://www.sec.gov/" label="form type" term="4"/>
<id>urn:tag:sec.gov,2008:accession-number=0001523672-26-000007</id>
</entry>
<entry>
<title>4 - TENET HEALTHCARE CORP (0000070318) (Issuer)</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/70318/000119312526342970/0001193125-26-342970-index.htm"/>
<updated>2026-08-10T20:00:03-04:00</updated>
<category scheme="https://www.sec.gov/" label="form type" term="4"/>
<id>urn:tag:sec.gov,2008:accession-number=0001193125-26-342970</id>
</entry>
</feed>"""


def _fixture(name: str) -> str:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


class FetchRecentForm4FilingsTests(unittest.TestCase):
    def test_dedupes_by_accession_and_extracts_correct_cik(self):
        """Regression test: an earlier version of this parser read parts[-2]
        of the index URL and returned the accession-number directory segment
        (18 digits) instead of the actual CIK (parts[-3], <=10 digits) —
        this silently produced garbage CIKs that broke every downstream
        document lookup. Caught by comparing against SEC's real URL shape."""
        mock_response = type("R", (), {"text": SAMPLE_ATOM, "raise_for_status": lambda self: None})()
        with patch.object(sec_edgar, "_get", return_value=mock_response):
            filings = sec_edgar.fetch_recent_form4_filings(count=10)

        # 3 raw entries, 2 sharing one accession -> 2 unique filings
        self.assertEqual(len(filings), 2)
        by_accession = {f["accession"]: f for f in filings}

        first = by_accession["0001523672-26-000007"]
        self.assertEqual(first["cik"], "2056611")  # NOT "000152367226000007"
        self.assertLessEqual(len(first["cik"]), 10)

        second = by_accession["0001193125-26-342970"]
        self.assertEqual(second["cik"], "70318")

    def test_empty_feed_returns_empty_list(self):
        mock_response = type("R", (), {
            "text": '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>',
            "raise_for_status": lambda self: None,
        })()
        with patch.object(sec_edgar, "_get", return_value=mock_response):
            self.assertEqual(sec_edgar.fetch_recent_form4_filings(), [])

    def test_network_failure_returns_empty_list_not_exception(self):
        import httpx
        with patch.object(sec_edgar, "_get", side_effect=httpx.ConnectError("boom")):
            self.assertEqual(sec_edgar.fetch_recent_form4_filings(), [])


class ParseForm4XmlTests(unittest.TestCase):
    def test_parses_real_non_derivative_sale(self):
        """Fixture is a real Form 4 (Tenet Healthcare, director open-market
        sales) fetched live from SEC EDGAR while building this integration."""
        result = sec_edgar.parse_form4_xml(_fixture("form4_non_derivative.xml"))
        self.assertIsNotNone(result)
        self.assertEqual(result["ticker"], "THC")
        self.assertEqual(result["issuer_name"], "TENET HEALTHCARE CORP")
        self.assertEqual(result["owner_name"], "Romo Tammy")
        self.assertTrue(result["is_director"])
        self.assertFalse(result["is_officer"])
        self.assertEqual(len(result["transactions"]), 2)

        tx = result["transactions"][0]
        self.assertEqual(tx["table"], "non_derivative")
        self.assertEqual(tx["transaction_code"], "S")
        self.assertEqual(tx["transaction_label"], "Open Market Sale")
        self.assertEqual(tx["acquired_disposed"], "D")
        self.assertEqual(tx["shares"], 4922.0)
        self.assertEqual(tx["price_per_share"], 264.15)
        self.assertAlmostEqual(tx["total_value"], 4922.0 * 264.15, places=2)

    def test_parses_real_derivative_grant(self):
        """Fixture: a director stock option grant (code 'A'), no non-derivative
        table at all — makes sure the derivative-only path works."""
        result = sec_edgar.parse_form4_xml(_fixture("form4_derivative_only.xml"))
        self.assertIsNotNone(result)
        self.assertEqual(result["ticker"], "LTGO")
        self.assertEqual(len(result["transactions"]), 1)
        tx = result["transactions"][0]
        self.assertEqual(tx["table"], "derivative")
        self.assertEqual(tx["transaction_code"], "A")
        self.assertEqual(tx["transaction_label"], "Grant/Award")

    def test_malformed_xml_returns_none(self):
        self.assertIsNone(sec_edgar.parse_form4_xml("<not><valid"))

    def test_wrong_root_tag_returns_none(self):
        self.assertIsNone(sec_edgar.parse_form4_xml("<somethingElse></somethingElse>"))

    def test_no_transactions_returns_none(self):
        empty = """<?xml version="1.0"?><ownershipDocument>
        <issuer><issuerCik>1</issuerCik><issuerName>X</issuerName><issuerTradingSymbol>X</issuerTradingSymbol></issuer>
        <reportingOwner><reportingOwnerId><rptOwnerCik>2</rptOwnerCik><rptOwnerName>Y</rptOwnerName></reportingOwnerId></reportingOwner>
        <nonDerivativeTable></nonDerivativeTable>
        </ownershipDocument>"""
        self.assertIsNone(sec_edgar.parse_form4_xml(empty))


if __name__ == "__main__":
    unittest.main()
