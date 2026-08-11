import unittest
from unittest.mock import patch

from lib import crypto_market_data as market_data


class ExchangeOhlcvTests(unittest.TestCase):
    def test_bybit_ohlcv_payload(self):
        rows = [
            ["1720000120000", "0.041", "0.044", "0.040", "0.043", "1200", "51"],
            ["1720000060000", "0.040", "0.043", "0.039", "0.041", "900", "37"],
            ["1720000000000", "0.039", "0.042", "0.038", "0.040", "800", "32"],
        ]
        payload = {"result": {"list": rows}}
        with patch.object(market_data, "_bybit_pair", return_value="BANKUSDT"), \
                patch.object(market_data, "_json", return_value=payload):
            frame = market_data._bybit_ohlcv("BANK/USDT", "1m", 3)

        self.assertIsNotNone(frame)
        self.assertEqual(frame.attrs["source"], "bybit")
        self.assertAlmostEqual(float(frame.iloc[-1]["close"]), 0.043)

    def test_kucoin_ohlcv_payload(self):
        rows = [
            ["1720000120", "0.041", "0.044", "0.040", "0.043", "1200", "51"],
            ["1720000060", "0.040", "0.043", "0.039", "0.041", "900", "37"],
            ["1720000000", "0.039", "0.042", "0.038", "0.040", "800", "32"],
        ]
        with patch.object(market_data, "_kucoin_pair", return_value="BANK-USDT"), \
                patch.object(market_data, "_json", return_value={"data": {"list": rows}}):
            frame = market_data._kucoin_ohlcv("BANK/USDT", "1m", 3)

        self.assertIsNotNone(frame)
        self.assertEqual(frame.attrs["source"], "kucoin")
        self.assertAlmostEqual(float(frame.iloc[-1]["close"]), 0.043)

    def test_mexc_resamples_one_minute_bars_to_three_minutes(self):
        start = 1720000020000
        rows = []
        for index in range(6):
            price = 0.040 + index * 0.001
            rows.append([
                start + index * 60000,
                str(price), str(price + 0.002), str(price - 0.001),
                str(price + 0.001), "100", start + (index + 1) * 60000, "4.2",
            ])

        with patch.object(market_data, "_mexc_pair", return_value="BANKUSDT"), \
                patch.object(market_data, "_json", return_value=rows):
            frame = market_data._mexc_ohlcv("BANK/USDT", "3m", 3)

        self.assertIsNotNone(frame)
        self.assertEqual(frame.attrs["source"], "mexc")
        self.assertGreaterEqual(len(frame), 2)
        self.assertAlmostEqual(float(frame["volume"].sum()), 600.0)


if __name__ == "__main__":
    unittest.main()
