import unittest

from lib.crypto_derivatives import (
    classify_oi_price_action, parse_funding_rate, parse_liquidations,
    parse_long_short_ratio, parse_open_interest, summarize_liquidations, to_okx_inst_id,
)


class ToOkxInstIdTests(unittest.TestCase):
    def test_plain_base(self):
        self.assertEqual(to_okx_inst_id("BTC"), "BTC-USDT-SWAP")

    def test_app_native_pair(self):
        self.assertEqual(to_okx_inst_id("ETH/USD"), "ETH-USDT-SWAP")

    def test_lowercase_input(self):
        self.assertEqual(to_okx_inst_id("sol"), "SOL-USDT-SWAP")


class ParseFundingRateTests(unittest.TestCase):
    def test_parses_real_okx_response(self):
        """Captured live from OKX while building this: BTC-USDT-SWAP funding rate."""
        data = {"code": "0", "data": [{
            "fundingRate": "0.0001000000000000", "fundingTime": "1786435200000",
            "nextFundingRate": "", "nextFundingTime": "1786464000000",
            "instId": "BTC-USDT-SWAP", "instType": "SWAP",
        }], "msg": ""}
        result = parse_funding_rate(data)
        self.assertEqual(result["funding_rate"], 0.0001)
        self.assertIsNotNone(result["next_funding_time"])

    def test_empty_data_returns_none(self):
        self.assertIsNone(parse_funding_rate({"code": "0", "data": [], "msg": ""}))


class ParseOpenInterestTests(unittest.TestCase):
    def test_parses_real_okx_response(self):
        """Captured live: BTC-USDT-SWAP open interest."""
        data = {"code": "0", "data": [{
            "instId": "BTC-USDT-SWAP", "instType": "SWAP",
            "oi": "3246494.10000001441", "oiCcy": "32464.9410000001441",
            "oiUsd": "2078892496.9350092274435", "ts": "1786413061228",
        }], "msg": ""}
        result = parse_open_interest(data)
        self.assertAlmostEqual(result["open_interest_usd"], 2078892496.94, places=1)

    def test_empty_data_returns_none(self):
        self.assertIsNone(parse_open_interest({"code": "0", "data": [], "msg": ""}))


class ParseLongShortRatioTests(unittest.TestCase):
    def test_parses_real_okx_response(self):
        """Captured live: [timestamp, ratio] pair format."""
        data = {"code": "0", "data": [["1786412700000", "1.6634118967452301"]], "msg": ""}
        result = parse_long_short_ratio(data)
        self.assertAlmostEqual(result["ratio"], 1.6634, places=3)

    def test_empty_data_returns_none(self):
        self.assertIsNone(parse_long_short_ratio({"code": "0", "data": [], "msg": ""}))


class ParseLiquidationsTests(unittest.TestCase):
    def test_parses_real_okx_shaped_response(self):
        """Shaped exactly like the real live OKX liquidation-orders response
        captured while building this — nested groups of "details"."""
        data = {"data": [{
            "details": [
                {"bkLoss": "0", "bkPx": "63955.7", "posSide": "long", "side": "sell", "sz": "3.62", "ts": "1786408771714"},
                {"bkLoss": "0", "bkPx": "64168.9", "posSide": "short", "side": "buy", "sz": "5.3", "ts": "1786394631624"},
            ],
            "instFamily": "BTC-USDT", "instId": "BTC-USDT-SWAP", "instType": "SWAP",
        }]}
        rows = parse_liquidations(data, symbol="BTC", inst_id="BTC-USDT-SWAP")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["pos_side"], "long")
        self.assertAlmostEqual(rows[0]["notional_usd"], 63955.7 * 3.62, places=1)

    def test_malformed_detail_skipped_not_crashed(self):
        data = {"data": [{"details": [{"bkPx": "not-a-number", "sz": "1", "posSide": "long", "side": "sell"}]}]}
        self.assertEqual(parse_liquidations(data, "BTC", "BTC-USDT-SWAP"), [])

    def test_no_groups_returns_empty_list(self):
        self.assertEqual(parse_liquidations({"data": []}, "BTC", "BTC-USDT-SWAP"), [])


class ClassifyOiPriceActionTests(unittest.TestCase):
    def test_long_buildup(self):
        self.assertEqual(classify_oi_price_action(5.0, 2.0), "long_buildup")

    def test_short_buildup(self):
        self.assertEqual(classify_oi_price_action(5.0, -2.0), "short_buildup")

    def test_short_covering(self):
        self.assertEqual(classify_oi_price_action(-5.0, 2.0), "short_covering")

    def test_long_unwinding(self):
        self.assertEqual(classify_oi_price_action(-5.0, -2.0), "long_unwinding")

    def test_missing_input_returns_none(self):
        self.assertIsNone(classify_oi_price_action(None, 2.0))
        self.assertIsNone(classify_oi_price_action(5.0, None))


class SummarizeLiquidationsTests(unittest.TestCase):
    def test_splits_by_position_side(self):
        liqs = [
            {"pos_side": "long", "notional_usd": 100_000},
            {"pos_side": "long", "notional_usd": 50_000},
            {"pos_side": "short", "notional_usd": 25_000},
        ]
        summary = summarize_liquidations(liqs)
        self.assertEqual(summary["count"], 3)
        self.assertEqual(summary["long_liquidated_usd"], 150_000)
        self.assertEqual(summary["short_liquidated_usd"], 25_000)
        self.assertAlmostEqual(summary["long_liquidation_share"], 150_000 / 175_000, places=4)

    def test_empty_list_returns_sane_defaults_not_division_error(self):
        summary = summarize_liquidations([])
        self.assertEqual(summary["count"], 0)
        self.assertIsNone(summary["long_liquidation_share"])


if __name__ == "__main__":
    unittest.main()
