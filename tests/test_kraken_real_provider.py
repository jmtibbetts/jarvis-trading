"""REAL_PROVIDER_READ_ONLY — Kraken's public surfaces, live.

CLASSIFIED AND SKIPPED BY DEFAULT. The release gate is the deterministic
twin in tests/kraken_twin.py; this exists to catch the twin going stale —
a captured payload that no longer resembles the service is a test that
passes while describing a venue that no longer exists.

    JARVIS_REAL_PROVIDER_TESTS=1 .venv/bin/python -m pytest \
        tests/test_kraken_real_provider.py -v

Setting that variable also disables the hermetic twin (see conftest), so
these reach the real endpoints. READ-ONLY: public GET only, no credentials,
no order or account surface.
"""
import os
import unittest

RUN = os.getenv("JARVIS_REAL_PROVIDER_TESTS") == "1"


@unittest.skipUnless(RUN, "REAL_PROVIDER_READ_ONLY: set "
                          "JARVIS_REAL_PROVIDER_TESTS=1 to reach the network")
class KrakenPublicSurfacesTests(unittest.TestCase):
    """Shape checks, not value checks. Prices move; field names must not."""

    def test_asset_pairs_still_carries_the_fields_the_parser_reads(self):
        import httpx
        r = httpx.get("https://api.kraken.com/0/public/AssetPairs", timeout=30)
        self.assertEqual(r.status_code, 200)
        result = r.json().get("result") or {}
        btc = next(v for v in result.values() if v.get("altname") == "XBTUSD")
        for field in ("altname", "ordermin", "pair_decimals", "lot_decimals"):
            with self.subTest(field=field):
                self.assertIn(field, btc)

    def test_the_captured_pair_payload_still_matches_the_live_shape(self):
        """The twin's fields must remain a subset of the real ones."""
        import httpx

        from kraken_twin import PAYLOADS
        r = httpx.get("https://api.kraken.com/0/public/AssetPairs", timeout=30)
        live = r.json()["result"]
        captured = PAYLOADS["AssetPairs"]["result"]
        for key, row in captured.items():
            with self.subTest(pair=key):
                self.assertIn(key, live, "captured pair was delisted")
                self.assertLessEqual(set(row) - set(live[key]), set(),
                                     "captured payload has fields the live "
                                     "service no longer returns")

    def test_the_spread_endpoint_responds_for_a_major(self):
        import httpx
        r = httpx.get("https://api.kraken.com/0/public/Spread",
                      params={"pair": "XBTUSD"}, timeout=30)
        self.assertEqual(r.status_code, 200)
        result = r.json().get("result") or {}
        quotes = next((v for k, v in result.items() if k != "last"), [])
        self.assertTrue(quotes, "no spread quotes returned")

    def test_the_futures_instrument_endpoint_responds(self):
        import httpx
        r = httpx.get(
            "https://futures.kraken.com/derivatives/api/v3/instruments",
            timeout=30)
        self.assertEqual(r.status_code, 200)
        syms = {i["symbol"] for i in r.json().get("instruments", [])}
        self.assertIn("PF_XBTUSD", syms)

    def test_the_futures_fee_schedule_endpoint_responds(self):
        import httpx
        r = httpx.get(
            "https://futures.kraken.com/derivatives/api/v3/feeschedules",
            timeout=30)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json().get("feeSchedules"))

    def test_the_captured_fee_schedule_uids_are_still_published(self):
        import httpx

        from kraken_twin import PAYLOADS
        r = httpx.get(
            "https://futures.kraken.com/derivatives/api/v3/feeschedules",
            timeout=30)
        live = {s.get("uid") for s in r.json().get("feeSchedules", [])}
        for s in PAYLOADS["feeschedules"]["feeSchedules"]:
            with self.subTest(uid=s.get("uid")):
                self.assertIn(s.get("uid"), live,
                              "captured fee schedule no longer exists — "
                              "refresh tests/fixtures/kraken_public_payloads.json")


if __name__ == "__main__":
    unittest.main()
