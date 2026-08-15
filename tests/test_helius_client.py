"""The centralized Helius client — auth, retries, and what 404 means.

No network. Every response is stubbed, because a client test that needs
mainnet is a client test nobody runs on a red build.
"""
import os
import unittest
from unittest.mock import patch

from lib import helius_client as hc


class _Resp:
    def __init__(self, status=200, body=None, text="", headers=None):
        self.status_code, self._body = status, body
        self.text = text or (str(body) if body is not None else "")
        self.headers = headers or {}

    def json(self):
        return self._body


class AuthTests(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("HELIUS_API_KEY")
        os.environ["HELIUS_API_KEY"] = "test-key-abcdef"
        hc.reset_metrics()

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("HELIUS_API_KEY", None)
        else:
            os.environ["HELIUS_API_KEY"] = self._prev

    def test_the_key_travels_as_a_header_never_in_the_url(self):
        """A key in a query string reaches every log, exception and metric
        label that ever prints a URL. A header cannot."""
        seen = {}

        def fake(method, url, **kw):
            seen["url"], seen["headers"] = url, kw.get("headers") or {}
            return _Resp(200, {"ok": True})

        with patch("httpx.request", side_effect=fake):
            hc.wallet_identity("SOMEADDR")
        self.assertEqual(seen["headers"].get("X-Api-Key"), "test-key-abcdef")
        self.assertNotIn("test-key-abcdef", seen["url"])
        self.assertNotIn("api-key", seen["url"])

    def test_no_key_raises_before_any_request(self):
        os.environ.pop("HELIUS_API_KEY", None)
        with patch("httpx.request") as req:
            with self.assertRaises(hc.HeliusNotConfigured):
                hc.wallet_identity("X")
            req.assert_not_called()

    def test_an_error_never_carries_the_key(self):
        with patch("httpx.request", return_value=_Resp(401, text="Unauthorized")):
            with self.assertRaises(hc.HeliusError) as ctx:
                hc.wallet_identity("X")
        self.assertNotIn("test-key-abcdef", str(ctx.exception))


class RetryTests(unittest.TestCase):
    def setUp(self):
        os.environ["HELIUS_API_KEY"] = "k"
        hc.reset_metrics()

    def test_429_is_retried_then_succeeds(self):
        responses = [_Resp(429, text="slow down", headers={"Retry-After": "0"}),
                     _Resp(200, {"ok": True})]
        with patch("httpx.request", side_effect=responses), \
             patch("time.sleep"):
            self.assertEqual(hc.wallet_identity("X"), {"ok": True})
        self.assertEqual(hc.metrics()["wallet/identity"]["rate_limited"], 1)

    def test_401_is_not_retried_because_repeating_it_cannot_help(self):
        with patch("httpx.request", return_value=_Resp(401, text="nope")) as req, \
             patch("time.sleep"):
            with self.assertRaises(hc.HeliusError):
                hc.wallet_identity("X")
        self.assertEqual(req.call_count, 1)

    def test_5xx_is_retried(self):
        with patch("httpx.request", return_value=_Resp(503, text="down")) as req, \
             patch("time.sleep"):
            with self.assertRaises(hc.HeliusError):
                hc.wallet_identity("X")
        self.assertGreater(req.call_count, 1)

    def test_metrics_count_calls_and_errors(self):
        with patch("httpx.request", return_value=_Resp(200, {"ok": 1})):
            hc.wallet_identity("A")
            hc.wallet_identity("B")
        m = hc.metrics()["wallet/identity"]
        self.assertEqual(m["calls"], 2)
        self.assertEqual(m["errors"], 0)
        self.assertIn("avg_ms", m)


class EndpointSemanticsTests(unittest.TestCase):
    def setUp(self):
        os.environ["HELIUS_API_KEY"] = "k"
        hc.reset_metrics()

    def test_funded_by_404_is_an_answer_not_a_failure(self):
        """Measured against the live API: an ordinary wallet with no
        indexed funder 404s, and raising would crash clustering on it."""
        with patch("httpx.request",
                   return_value=_Resp(404, text='{"error":"No funding transaction"}')):
            self.assertEqual(hc.funded_by("X"), {})

    def test_funded_by_still_raises_on_a_real_failure(self):
        with patch("httpx.request", return_value=_Resp(500, text="boom")), \
             patch("time.sleep"):
            with self.assertRaises(hc.HeliusError):
                hc.funded_by("X")

    def test_batch_identity_sends_addresses_not_wallets(self):
        """`wallets` returns HTTP 400 — measured, and not stated in the
        spec that prompted this client."""
        seen = {}

        def fake(method, url, **kw):
            seen.update(kw.get("json") or {})
            return _Resp(200, [{"address": "A", "type": "unknown"}])

        with patch("httpx.request", side_effect=fake):
            hc.batch_identity(["A"])
        self.assertIn("addresses", seen)
        self.assertNotIn("wallets", seen)

    def test_batch_identity_keys_by_address_and_dedupes(self):
        with patch("httpx.request", return_value=_Resp(
                200, [{"address": "A", "type": "exchange"}])) as req:
            out = hc.batch_identity(["A", "A", ""])
        self.assertEqual(list(out), ["A"])
        self.assertEqual(req.call_count, 1)

    def test_batch_identity_of_nothing_makes_no_call(self):
        with patch("httpx.request") as req:
            self.assertEqual(hc.batch_identity([]), {})
            req.assert_not_called()

    def test_balance_at_demands_exactly_one_selector(self):
        """The API rejects both; catching it here costs no call."""
        with self.assertRaises(ValueError):
            hc.balance_at("W", "MINT")
        with self.assertRaises(ValueError):
            hc.balance_at("W", "MINT", time_s=1, slot=2)

    def test_rpc_surfaces_a_jsonrpc_error_as_an_exception(self):
        body = {"jsonrpc": "2.0", "error": {"code": -32601,
                                            "message": "Method not found"}}
        with patch("httpx.request", return_value=_Resp(200, body)):
            with self.assertRaises(hc.HeliusError) as ctx:
                hc.rpc("nope")
        self.assertIn("Method not found", str(ctx.exception))

    def test_rpc_returns_the_result_field(self):
        with patch("httpx.request",
                   return_value=_Resp(200, {"jsonrpc": "2.0", "result": "ok"})):
            self.assertEqual(hc.rpc("getHealth"), "ok")


class HealthTests(unittest.TestCase):
    def test_health_never_raises(self):
        os.environ["HELIUS_API_KEY"] = "k"
        with patch("httpx.request", side_effect=RuntimeError("network gone")), \
             patch("time.sleep"):
            out = hc.health()
        self.assertFalse(out["rpc"]["ok"])
        self.assertFalse(out["wallet_api"]["ok"])

    def test_unconfigured_health_says_so_without_calling(self):
        prev = os.environ.pop("HELIUS_API_KEY", None)
        try:
            with patch("httpx.request") as req:
                out = hc.health()
                req.assert_not_called()
            self.assertFalse(out["configured"])
        finally:
            if prev is not None:
                os.environ["HELIUS_API_KEY"] = prev


if __name__ == "__main__":
    unittest.main()
