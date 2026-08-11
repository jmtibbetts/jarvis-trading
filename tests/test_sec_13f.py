import unittest
from unittest.mock import MagicMock, patch

from lib.sec_13f import (
    _normalize_period, parse_infotable, parse_primary_doc, resolve_cusips,
)

# Shaped exactly like the real live filing captured while building this
# (Lisanti Capital Growth, LLC — accession 0001424467-26-000003).
PRIMARY_DOC = """<?xml version="1.0" encoding="UTF-8"?>
<edgarSubmission xmlns:ns1="http://www.sec.gov/edgar/common" xmlns="http://www.sec.gov/edgar/thirteenffiler">
  <headerData>
    <submissionType>13F-HR</submissionType>
    <filerInfo><periodOfReport>06-30-2026</periodOfReport></filerInfo>
  </headerData>
  <formData>
    <coverPage>
      <reportCalendarOrQuarter>06-30-2026</reportCalendarOrQuarter>
      <isAmendment>false</isAmendment>
      <filingManager>
        <name>Lisanti Capital Growth, LLC</name>
        <address><ns1:city>NEW YORK</ns1:city></address>
      </filingManager>
    </coverPage>
  </formData>
</edgarSubmission>"""

INFOTABLE = """<?xml version="1.0" encoding="utf-8"?>
<ns1:informationTable xmlns:ns1="http://www.sec.gov/edgar/document/thirteenf/informationtable">
  <ns1:infoTable>
    <ns1:nameOfIssuer>ACADIA HEALTHCARE COMPANY IN</ns1:nameOfIssuer>
    <ns1:titleOfClass>COM</ns1:titleOfClass>
    <ns1:cusip>00404A109</ns1:cusip>
    <ns1:figi>BBG001SNNWL7</ns1:figi>
    <ns1:value>4501701</ns1:value>
    <ns1:shrsOrPrnAmt><ns1:sshPrnamt>152445</ns1:sshPrnamt><ns1:sshPrnamtType>SH</ns1:sshPrnamtType></ns1:shrsOrPrnAmt>
    <ns1:investmentDiscretion>SOLE</ns1:investmentDiscretion>
  </ns1:infoTable>
  <ns1:infoTable>
    <ns1:nameOfIssuer>ADAPTIVE BIOTECHNOLOGIES COR</ns1:nameOfIssuer>
    <ns1:titleOfClass>COM</ns1:titleOfClass>
    <ns1:cusip>00650F109</ns1:cusip>
    <ns1:value>6974575</ns1:value>
    <ns1:shrsOrPrnAmt><ns1:sshPrnamt>325155</ns1:sshPrnamt><ns1:sshPrnamtType>SH</ns1:sshPrnamtType></ns1:shrsOrPrnAmt>
  </ns1:infoTable>
</ns1:informationTable>"""

# Some filers use a default namespace with no prefix — must parse identically.
INFOTABLE_NO_PREFIX = """<?xml version="1.0"?>
<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
  <infoTable>
    <nameOfIssuer>NVIDIA CORP</nameOfIssuer>
    <cusip>67066G104</cusip>
    <value>12345678</value>
    <shrsOrPrnAmt><sshPrnamt>1000</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
  </infoTable>
</informationTable>"""


class ParsePrimaryDocTests(unittest.TestCase):
    def test_parses_real_cover_page(self):
        result = parse_primary_doc(PRIMARY_DOC)
        self.assertEqual(result["filer_name"], "Lisanti Capital Growth, LLC")
        self.assertEqual(result["period_of_report"], "2026-06-30")
        self.assertFalse(result["is_amendment"])

    def test_malformed_xml_returns_none(self):
        self.assertIsNone(parse_primary_doc("<not valid"))


class NormalizePeriodTests(unittest.TestCase):
    def test_converts_sec_mmddyyyy_to_iso(self):
        """SEC writes MM-DD-YYYY; stored ISO so string sorting is chronological."""
        self.assertEqual(_normalize_period("06-30-2026"), "2026-06-30")

    def test_already_iso_passes_through(self):
        self.assertEqual(_normalize_period("2026-06-30"), "2026-06-30")

    def test_none_returns_none(self):
        self.assertIsNone(_normalize_period(None))


class ParseInfotableTests(unittest.TestCase):
    def test_parses_real_holdings(self):
        rows = parse_infotable(INFOTABLE)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["cusip"], "00404A109")
        self.assertEqual(rows[0]["issuer_name"], "ACADIA HEALTHCARE COMPANY IN")
        self.assertEqual(rows[0]["value_usd"], 4501701.0)
        self.assertEqual(rows[0]["shares"], 152445.0)
        self.assertEqual(rows[0]["shares_type"], "SH")

    def test_missing_optional_figi_handled(self):
        rows = parse_infotable(INFOTABLE)
        self.assertEqual(rows[0]["figi"], "BBG001SNNWL7")
        self.assertIsNone(rows[1]["figi"])

    def test_default_namespace_without_prefix_parses_identically(self):
        """Real filers vary in namespace prefix usage; parsing must key off
        local tag names, not a hardcoded ns1: prefix."""
        rows = parse_infotable(INFOTABLE_NO_PREFIX)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["cusip"], "67066G104")
        self.assertEqual(rows[0]["shares"], 1000.0)

    def test_holding_without_cusip_skipped(self):
        xml = INFOTABLE.replace("<ns1:cusip>00404A109</ns1:cusip>", "")
        self.assertEqual(len(parse_infotable(xml)), 1)

    def test_malformed_xml_returns_empty_not_exception(self):
        self.assertEqual(parse_infotable("<broken"), [])


class ResolveCusipsTests(unittest.TestCase):
    def _response(self, payload, status=200):
        resp = MagicMock()
        resp.status_code = status
        resp.json.return_value = payload
        resp.raise_for_status.return_value = None
        return resp

    def test_empty_input_short_circuits(self):
        self.assertEqual(resolve_cusips([]), ({}, set()))

    def test_prefers_us_composite_listing(self):
        """OpenFIGI returns one row per venue; the US composite is the right
        one for 13F, which reports the security not a venue line."""
        client = MagicMock()
        client.post.return_value = self._response([{"data": [
            {"ticker": "ACHC", "exchCode": "UA"},
            {"ticker": "ACHC", "exchCode": "US"},
        ]}])
        resolved, attempted = resolve_cusips(["00404A109"], client=client)
        self.assertEqual(resolved, {"00404A109": "ACHC"})
        self.assertEqual(attempted, {"00404A109"})

    def test_no_match_is_attempted_but_unresolved(self):
        """Critical distinction: a genuine no-match must be reported as
        attempted so it gets cached and not retried forever."""
        client = MagicMock()
        client.post.return_value = self._response([{"warning": "No identifier found."}])
        resolved, attempted = resolve_cusips(["999999999"], client=client)
        self.assertEqual(resolved, {})
        self.assertEqual(attempted, {"999999999"})

    def test_rate_limit_stops_and_does_not_mark_remainder_attempted(self):
        """The inverse guarantee: CUSIPs never asked about (because we hit
        429 and stopped) must NOT be reported attempted, or they'd be cached
        as permanent no-matches and never looked up again."""
        client = MagicMock()
        client.post.return_value = self._response(None, status=429)
        resolved, attempted = resolve_cusips([f"{i:09d}" for i in range(25)], client=client)
        self.assertEqual(resolved, {})
        self.assertEqual(attempted, set())

    def test_http_error_stops_cleanly(self):
        import httpx
        client = MagicMock()
        client.post.side_effect = httpx.ConnectError("boom")
        resolved, attempted = resolve_cusips(["00404A109"], client=client)
        self.assertEqual(resolved, {})
        self.assertEqual(attempted, set())

    def test_api_key_passed_as_header_when_provided(self):
        client = MagicMock()
        client.post.return_value = self._response([{"data": [{"ticker": "X", "exchCode": "US"}]}])
        resolve_cusips(["00404A109"], api_key="secret", client=client)
        _, kwargs = client.post.call_args
        self.assertEqual(kwargs["headers"]["X-OPENFIGI-APIKEY"], "secret")


if __name__ == "__main__":
    unittest.main()
