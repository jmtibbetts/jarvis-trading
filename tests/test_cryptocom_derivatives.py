"""Second derivatives venue — and the semantics trap it introduces.

Crypto.com pays funding HOURLY on these perps; OKX pays 8-HOURLY. The same
number in the funding_rate column therefore means different things per
venue, and the cost model's contract is explicitly an 8-hour rate. These
tests pin the three defenses: rows carry their venue, the cost model reads
only OKX rows, and cross-venue comparison happens through one function
that normalizes with its assumptions stated.

Payload fixtures are copies of live responses captured 2026-08-13, not
hand-invented shapes.
"""
import unittest

from lib.crypto_derivatives import (CRYPTOCOM_FUNDING_INTERVAL_H,
                                    OKX_FUNDING_INTERVAL_H, funding_dispersion,
                                    parse_cryptocom_funding,
                                    parse_cryptocom_ticker, to_cryptocom_inst)

# Verbatim structure from the live probe.
TICKER = {"result": {"data": [{
    "a": "63645.2", "b": "63530.6", "k": "63530.7", "oi": "6059.1629",
    "t": 1786627145000, "v": "3962.7913", "vv": "251983201.81",
    "h": "64130.6", "l": "63244.0", "c": "-0.0069", "i": "BTCUSD-PERP",
}]}}

FUNDING = {"result": {"data": [
    {"v": "0.000011786", "t": 1786627145000},
    {"v": "0.000011786", "t": 1786627085000},
]}}


class InstrumentNameTests(unittest.TestCase):
    def test_both_symbol_forms_map(self):
        self.assertEqual(to_cryptocom_inst("BTC"), "BTCUSD-PERP")
        self.assertEqual(to_cryptocom_inst("eth/usd"), "ETHUSD-PERP")


class TickerParsingTests(unittest.TestCase):
    def test_oi_is_converted_from_base_units_to_usd(self):
        """The venue reports OI in BASE units (BTC), not dollars — the
        units-vs-contracts confusion has cost this codebase $440k on paper
        once already, so the conversion is the point of the parser."""
        t = parse_cryptocom_ticker(TICKER)
        self.assertAlmostEqual(t["open_interest_base"], 6059.1629)
        self.assertAlmostEqual(t["open_interest_usd"],
                               round(6059.1629 * 63645.2, 2))

    def test_empty_or_malformed_payloads_yield_none(self):
        for bad in ({}, {"result": {}}, {"result": {"data": []}},
                    {"result": {"data": [{"a": "0", "oi": "5"}]}},
                    {"result": {"data": [{"a": "junk"}]}}):
            self.assertIsNone(parse_cryptocom_ticker(bad), bad)


class FundingParsingTests(unittest.TestCase):
    def test_newest_row_wins(self):
        f = parse_cryptocom_funding(FUNDING)
        self.assertAlmostEqual(f["funding_rate"], 0.000011786)

    def test_no_rows_is_none(self):
        self.assertIsNone(parse_cryptocom_funding({"result": {"data": []}}))


class FundingSemanticsTests(unittest.TestCase):
    """The §49 trap: same column name, different meaning per venue."""

    def test_dispersion_normalizes_before_comparing(self):
        """Identical hourly economics must show ~zero spread even though
        the raw rates differ 8x."""
        okx_8h = 0.0008          # = 0.0001/hour
        cdc_1h = 0.0001          # = 0.0001/hour
        d = funding_dispersion(okx_8h, cdc_1h)
        self.assertAlmostEqual(d["spread_hourly"], 0.0, places=12)

    def test_dispersion_states_its_interval_assumptions(self):
        d = funding_dispersion(0.0008, 0.0001)
        self.assertEqual(d["intervals_h"]["okx"], OKX_FUNDING_INTERVAL_H)
        self.assertEqual(d["intervals_h"]["cryptocom"], CRYPTOCOM_FUNDING_INTERVAL_H)

    def test_missing_either_side_abstains(self):
        self.assertIsNone(funding_dispersion(None, 0.0001))
        self.assertIsNone(funding_dispersion(0.0008, None))


class ConsumersStayVenuePinnedTests(unittest.TestCase):
    """Static pins: the readers that predate the second venue must not
    silently start consuming it."""

    def test_the_cost_model_reads_only_okx_rows(self):
        """Its contract is an 8-hour rate. A cryptocom row (hourly) taken as
        'latest per symbol' would under-price funding 8x whenever it was
        the newer row."""
        import inspect

        from lib import transaction_costs
        src = inspect.getsource(transaction_costs)
        self.assertIn('venue == "okx"', src)

    def test_the_oi_change_baseline_is_venue_pinned(self):
        import inspect

        from jobs import fetch_crypto_derivatives
        src = inspect.getsource(fetch_crypto_derivatives.run)
        self.assertIn('venue == "okx"', src)


if __name__ == "__main__":
    unittest.main()
