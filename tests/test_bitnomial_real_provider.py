"""REAL_PROVIDER_READ_ONLY — the public Bitnomial surfaces, live.

CLASSIFIED AND SKIPPED BY DEFAULT. Hermetic CI must not depend on a third
party being up, and a test that silently reaches the internet is the same
category of mistake as a "hermetic" test that reached the operator's LM
Studio. The deterministic twin lives in test_bitnomial_market_data.py and
runs everywhere; this proves the twin still resembles reality.

    JARVIS_REAL_PROVIDER_TESTS=1 .venv/bin/python -m pytest \
        tests/test_bitnomial_real_provider.py -v

READ-ONLY BY CONSTRUCTION: one public GET and one public WebSocket
subscribe. No credential is sent, none exists, and there is no account,
order or transfer surface anywhere in the adapter.
"""
import json
import os
import unittest

RUN = os.getenv("JARVIS_REAL_PROVIDER_TESTS") == "1"


@unittest.skipUnless(RUN, "REAL_PROVIDER_READ_ONLY: set "
                          "JARVIS_REAL_PROVIDER_TESTS=1 to reach the network")
class BitnomialPublicSurfacesTests(unittest.TestCase):

    def test_the_public_product_spec_endpoint_responds(self):
        import urllib.request
        from lib.bitnomial_products import REST_BASE
        with urllib.request.urlopen(f"{REST_BASE}/product/specs/", timeout=30) as f:
            rows = json.load(f)
        self.assertIsInstance(rows, list)
        self.assertTrue(rows)

    def test_the_active_perpetuals_still_match_the_captured_snapshot(self):
        """If the exchange lists or retires a contract, the offline snapshot
        is stale and discovery must be refreshed rather than trusted."""
        import urllib.request
        from lib import bitnomial_products as BP
        with urllib.request.urlopen(f"{BP.REST_BASE}/product/specs/", timeout=30) as f:
            rows = json.load(f)
        live = {r["base_symbol"] for r in rows
                if r.get("product_status") == "active" and r.get("type") == "perpetual"}
        self.assertEqual(live, set(BP.all_discovered()),
                         "the active perpetual set has changed; refresh "
                         "data/reference/bitnomial_perp_specs.json")

    def test_a_canonical_symbol_maps_to_a_live_product(self):
        from lib import bitnomial_products as BP
        p = BP.resolve("BTC/USD")
        self.assertTrue(p.ok, p.detail)
        self.assertEqual(p.symbol, "PBTCUCZ50")
        self.assertEqual(p.contract_size, 0.01)

    def test_the_websocket_accepts_the_subscribe_and_returns_a_book(self):
        """Handshake, subscription and a decodable snapshot — no auth."""
        import asyncio

        import websockets

        from lib import bitnomial_market_data as MD
        from lib import bitnomial_products as BP

        async def go():
            async with websockets.connect(BP.WS_URL, open_timeout=25) as ws:
                await ws.send(json.dumps(MD.subscribe_message(["PBTCUCZ50"])))
                for _ in range(25):
                    m = json.loads(await asyncio.wait_for(ws.recv(), timeout=25))
                    if m.get("type") == "book":
                        return m
            return None

        book = asyncio.run(go())
        self.assertIsNotNone(book, "no book snapshot arrived")
        self.assertTrue(book["bids"] and book["asks"])
        # Ordering is what makes index 0 the best level.
        self.assertEqual([p for p, _ in book["asks"]],
                         sorted(p for p, _ in book["asks"]))

    def test_the_live_book_prices_are_economically_sane(self):
        """THE UNIT CHECK. A tick-conversion error is invisible in structure
        and enormous in dollars, so this asserts the converted price lands in
        a plausible band rather than merely being a number."""
        import asyncio

        import websockets

        from lib import bitnomial_market_data as MD
        from lib import bitnomial_products as BP

        prod = BP.resolve("BTC/USD")

        async def go():
            async with websockets.connect(BP.WS_URL, open_timeout=25) as ws:
                await ws.send(json.dumps(MD.subscribe_message([prod.symbol])))
                for _ in range(25):
                    m = json.loads(await asyncio.wait_for(ws.recv(), timeout=25))
                    if m.get("type") == "book" and m.get("bids"):
                        return m
            return None

        book = asyncio.run(go())
        bid_usd = prod.price_usd(max(p for p, _ in book["bids"]))
        self.assertGreater(bid_usd, 1_000.0, "BTC priced under $1k — scale is wrong")
        self.assertLess(bid_usd, 10_000_000.0, "BTC priced over $10M — scale is wrong")

    def test_the_adapter_sends_no_credential(self):
        from lib import bitnomial_market_data as MD
        frame = MD.subscribe_message(["PBTCUCZ50"])
        blob = json.dumps(frame).lower()
        for token in ("token", "key", "secret", "sign", "auth"):
            with self.subTest(token=token):
                self.assertNotIn(token, blob)


if __name__ == "__main__":
    unittest.main()
