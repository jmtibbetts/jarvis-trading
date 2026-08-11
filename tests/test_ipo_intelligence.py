import unittest

from lib.ipo_intelligence import (
    _TITLE_RE, FORM_STAGES, parse_cover_page,
)

# Condensed from the real Latigo Biotherapeutics 424B4 cover verified live.
IPO_COVER = """<html><body>
<p>19,200,000&nbsp;Shares</p>
<p>Latigo Biotherapeutics, Inc. Common Stock</p>
<p>This is our initial public offering. The initial public offering price is
$18.00 per share.</p>
<p>Our common stock has been approved for listing on the Nasdaq Global Select
Market under the symbol &#8220;LTGO&#8221;.</p>
</body></html>"""

# Condensed from the real iSpecimen 424B4 — an already-listed company's
# follow-on that uses the same form. The only "$X per share" mentions are the
# PAR VALUE and the prior close, neither of which is an offer price.
FOLLOWON_COVER = """<html><body>
<p>996,231 SHARES of our common stock, par value $0.0001 per share, on a best
efforts basis.</p>
<p>Our common stock is listed on the Nasdaq Capital Market. On August 5, 2026,
the reported closing price of our common stock was $1.92 per share.</p>
</body></html>"""

# Real observed variation: lowercase "shares" (BlossomHill).
LOWERCASE_SHARES_COVER = """<html><body>
<p>We are offering 9,375,000 shares of our common stock in our initial public
offering. The initial public offering price is $16.00 per share.</p>
<p>listing on the Nasdaq Global Select Market</p>
</body></html>"""


class ParseCoverPageTests(unittest.TestCase):
    def test_extracts_real_ipo_cover(self):
        r = parse_cover_page(IPO_COVER)
        self.assertEqual(r["ticker"], "LTGO")
        self.assertEqual(r["exchange"], "Nasdaq Global Select Market")
        self.assertEqual(r["offer_price"], 18.0)
        self.assertEqual(r["shares_offered"], 19_200_000.0)
        self.assertEqual(r["total_offering_usd"], 345_600_000.0)
        self.assertTrue(r["cover_mentions_ipo"])

    def test_par_value_is_never_extracted_as_offer_price(self):
        """Regression for a real bug: a generic '$X per share' fallback
        extracted $0.0001 — the PAR VALUE — from a live filing. The price
        must come only from the explicit IPO-price phrasing, so this cover
        yields no price at all."""
        r = parse_cover_page(FOLLOWON_COVER)
        self.assertIsNone(r["offer_price"])
        self.assertIsNone(r["total_offering_usd"])

    def test_followon_prospectus_flagged_as_not_ipo(self):
        """424(b)(4) also covers follow-ons by listed companies (observed
        live) — those must be distinguishable from true IPO pricings."""
        r = parse_cover_page(FOLLOWON_COVER)
        self.assertFalse(r["cover_mentions_ipo"])
        # the share count is still real data for the offering
        self.assertEqual(r["shares_offered"], 996_231.0)

    def test_lowercase_shares_extracted(self):
        """Regression: covers write 'Shares' and 'shares' interchangeably
        (both observed in live filings)."""
        r = parse_cover_page(LOWERCASE_SHARES_COVER)
        self.assertEqual(r["shares_offered"], 9_375_000.0)
        self.assertEqual(r["offer_price"], 16.0)
        self.assertEqual(r["total_offering_usd"], 150_000_000.0)

    def test_nbsp_between_number_and_shares(self):
        """The real Latigo cover separates '19,200,000' and 'Shares' with a
        non-breaking space."""
        r = parse_cover_page(IPO_COVER)
        self.assertEqual(r["shares_offered"], 19_200_000.0)

    def test_empty_document_yields_all_nulls(self):
        r = parse_cover_page("<html></html>")
        self.assertIsNone(r["ticker"])
        self.assertIsNone(r["offer_price"])
        self.assertIsNone(r["shares_offered"])
        self.assertFalse(r["cover_mentions_ipo"])


class TitleParsingTests(unittest.TestCase):
    def test_parses_real_feed_title(self):
        m = _TITLE_RE.match("S-1 - CURIS INC (0001108205) (Filer)")
        self.assertEqual(m.group("form"), "S-1")
        self.assertEqual(m.group("name"), "CURIS INC")
        self.assertEqual(m.group("cik"), "0001108205")

    def test_amendment_form_parsed(self):
        m = _TITLE_RE.match("S-1/A - Silexion Therapeutics Corp (0002022416) (Filer)")
        self.assertEqual(m.group("form"), "S-1/A")

    def test_s11_is_a_different_form(self):
        """The feed's type filter is a prefix match, so S-11 (real-estate
        registrations) appears in an S-1 query — exact comparison against the
        parsed form is what keeps them out."""
        m = _TITLE_RE.match("S-11 - Janus Living, Inc. (0002100805) (Filer)")
        self.assertEqual(m.group("form"), "S-11")
        self.assertNotIn(m.group("form"), FORM_STAGES)


if __name__ == "__main__":
    unittest.main()
