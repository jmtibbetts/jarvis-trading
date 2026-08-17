"""An assigned address had been stored as if it were a chosen one.

`http://192.168.0.229:1234/v1` sat in PlatformConfig, and `get_llm_config`
read the DB before anything else — so a DHCP lease the operator never
reserved outranked a routing table that knew the right answer. It worked
until the lease moved, and then every LLM call failed with a connect error
that read like LM Studio being down.

These tests pin the rule that replaced it: an address is authoritative only
when someone chose it. Everything else is probed, used for one process, and
never written back.
"""
import unittest
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from lib import lmstudio as L


GATEWAY = "172.31.48.1"
STALE_DHCP = "http://192.168.0.229:1234/v1"


class _Row:
    """A PlatformConfig row, without a database."""

    def __init__(self, **kw):
        self.platform = "lmstudio"
        self.api_url = ""
        self.api_key = ""
        self.extra_field_1 = "db-model"
        self.extra_field_2 = ""
        self.extra_field_3 = None
        self.is_active = True
        self.is_default = True
        for k, v in kw.items():
            setattr(self, k, v)


class EndpointTestCase(unittest.TestCase):
    """Every test starts from a cold process and a clean environment."""

    def setUp(self):
        import os
        L.reset_endpoint_cache()
        env = patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for var in ("LM_STUDIO_URL", "LM_STUDIO_PORT"):
            os.environ.pop(var, None)
        self.addCleanup(L.reset_endpoint_cache)

    def reachable(self, *urls):
        """Patch the probe so only `urls` answer like an LLM server."""
        allowed = set(urls)
        probe = MagicMock(side_effect=lambda url, api_key="", timeout=3.0: url in allowed)
        p = patch.object(L, "_probe_openai_compat", probe)
        p.start()
        self.addCleanup(p.stop)
        return probe

    def db_returns(self, row):
        """Patch get_db so get_llm_config sees exactly `row`. Returns the
        session mock, so a test can assert nothing was written to it."""
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = [row]

        @contextmanager
        def fake_get_db():
            yield db

        p = patch.object(L, "get_db", fake_get_db)
        p.start()
        self.addCleanup(p.stop)
        return db


class PrecedenceTests(EndpointTestCase):

    def test_an_explicit_env_address_is_honoured_exactly(self):
        """No probe, no rewrite. LM_STUDIO_URL is a statement of intent, and
        a resolver that substituted a reachable server for an unreachable
        chosen one would be answering from a machine nobody picked."""
        import os
        os.environ["LM_STUDIO_URL"] = "http://10.0.0.5:9999/v1"
        probe = self.reachable(f"http://{GATEWAY}:1234/v1")
        with patch.object(L, "_wsl_windows_host", return_value=GATEWAY):
            url, prov = L.resolve_endpoint()
        self.assertEqual(url, "http://10.0.0.5:9999/v1")
        self.assertEqual(prov, L.PROV_ENV)
        probe.assert_not_called()

    def test_a_marked_operator_row_is_honoured_exactly(self):
        probe = self.reachable(f"http://{GATEWAY}:1234/v1")
        with patch.object(L, "_wsl_windows_host", return_value=GATEWAY):
            url, prov = L.resolve_endpoint(db_url="http://10.0.0.7:1234/v1",
                                           db_provenance=L.PROV_OPERATOR)
        self.assertEqual(url, "http://10.0.0.7:1234/v1")
        self.assertEqual(prov, L.PROV_OPERATOR)
        probe.assert_not_called()

    def test_localhost_wins_when_it_actually_answers(self):
        """Mirrored networking and native hosts both land here."""
        self.reachable("http://127.0.0.1:1234/v1")
        with patch.object(L, "_wsl_windows_host", return_value=GATEWAY):
            url, prov = L.resolve_endpoint()
        self.assertEqual(url, "http://127.0.0.1:1234/v1")
        self.assertEqual(prov, L.PROV_LOCALHOST)

    def test_discovery_runs_when_loopback_is_dead(self):
        self.reachable(f"http://{GATEWAY}:1234/v1")
        with patch.object(L, "_wsl_windows_host", return_value=GATEWAY):
            url, prov = L.resolve_endpoint()
        self.assertEqual(url, f"http://{GATEWAY}:1234/v1")
        self.assertEqual(prov, L.PROV_WSL)

    def test_nothing_reachable_is_reported_as_unavailable(self):
        self.reachable()
        with patch.object(L, "_wsl_windows_host", return_value=GATEWAY):
            url, prov = L.resolve_endpoint()
        self.assertIsNone(url)
        self.assertEqual(prov, L.PROV_NONE)

    def test_an_explicit_loopback_url_still_falls_through_to_discovery(self):
        """localhost is the shipped default, not a chosen remote address —
        honouring it "exactly" under WSL2 NAT would honour it into failure."""
        import os
        os.environ["LM_STUDIO_URL"] = "http://localhost:1234/v1"
        self.reachable(f"http://{GATEWAY}:1234/v1")
        with patch.object(L, "_wsl_windows_host", return_value=GATEWAY):
            url, prov = L.resolve_endpoint()
        self.assertEqual(prov, L.PROV_WSL)


class TheStaleRowLosesTests(EndpointTestCase):

    def test_an_unmarked_dhcp_address_does_not_outrank_discovery(self):
        """The bug, stated as a test: this row used to win outright."""
        row = _Row(api_url=STALE_DHCP, extra_field_3=None)
        self.db_returns(row)
        self.reachable(f"http://{GATEWAY}:1234/v1")
        with patch.object(L, "_wsl_windows_host", return_value=GATEWAY):
            cfg = L.get_llm_config()
        self.assertEqual(cfg["model"], "db-model", "the DB branch must be the one under test")
        self.assertEqual(cfg["url"], f"http://{GATEWAY}:1234/v1")
        self.assertEqual(cfg["provenance"], L.PROV_WSL)

    def test_the_port_survives_even_though_the_host_does_not(self):
        """Nothing assigns a port. It is the one real piece of configuration
        left in a row whose host has gone stale."""
        row = _Row(api_url="http://192.168.0.229:4321/v1")
        self.db_returns(row)
        self.reachable(f"http://{GATEWAY}:4321/v1")
        with patch.object(L, "_wsl_windows_host", return_value=GATEWAY):
            cfg = L.get_llm_config()
        self.assertEqual(cfg["url"], f"http://{GATEWAY}:4321/v1")

    def test_a_marked_row_still_wins_because_someone_chose_it(self):
        row = _Row(api_url="http://192.168.0.229:1234/v1",
                   extra_field_3=L.PROV_OPERATOR)
        self.db_returns(row)
        self.reachable(f"http://{GATEWAY}:1234/v1")
        with patch.object(L, "_wsl_windows_host", return_value=GATEWAY):
            cfg = L.get_llm_config()
        self.assertEqual(cfg["url"], "http://192.168.0.229:1234/v1")
        self.assertEqual(cfg["provenance"], L.PROV_OPERATOR)

    def test_a_hosted_provider_url_is_never_probed_away(self):
        """Only LM Studio's address is assigned. An OpenAI or Anthropic
        api_url is configuration and must pass through untouched."""
        row = _Row(platform="anthropic", api_url="https://api.anthropic.com")
        self.db_returns(row)
        probe = self.reachable()
        cfg = L.get_llm_config()
        self.assertEqual(cfg["url"], "https://api.anthropic.com")
        self.assertEqual(cfg["provenance"], L.PROV_OPERATOR)
        probe.assert_not_called()


class NothingDiscoveredIsWrittenBackTests(EndpointTestCase):

    def test_resolution_never_writes_to_the_database(self):
        row = _Row(api_url=STALE_DHCP)
        db = self.db_returns(row)
        self.reachable(f"http://{GATEWAY}:1234/v1")
        with patch.object(L, "_wsl_windows_host", return_value=GATEWAY):
            L.get_llm_config()
        self.assertEqual(row.api_url, STALE_DHCP, "the row was mutated in place")
        db.commit.assert_not_called()
        db.add.assert_not_called()

    def test_only_an_operator_choice_is_allowed_to_persist(self):
        self.assertEqual(set(L.PERSISTABLE_PROVENANCE), {L.PROV_OPERATOR})

    def test_the_next_process_rediscovers_rather_than_remembering(self):
        """The cache is per-process by construction: a gateway is assigned
        per boot, so carrying one across a restart is carrying a stale fact."""
        probe = self.reachable(f"http://{GATEWAY}:1234/v1")
        with patch.object(L, "_wsl_windows_host", return_value=GATEWAY):
            L.resolve_endpoint()
            first = probe.call_count
            L.resolve_endpoint()
            self.assertEqual(probe.call_count, first, "cached within the process")

            L.reset_endpoint_cache()          # stands in for a fresh process
            L.resolve_endpoint()
        self.assertGreater(probe.call_count, first)

    def test_an_unavailable_result_is_retried_rather_than_cached_forever(self):
        self.reachable()
        with patch.object(L, "_wsl_windows_host", return_value=GATEWAY), \
             patch.object(L, "ENDPOINT_RETRY_S", 0.0):
            self.assertEqual(L.resolve_endpoint()[1], L.PROV_NONE)
            probe = self.reachable(f"http://{GATEWAY}:1234/v1")
            url, prov = L.resolve_endpoint()
        self.assertEqual(prov, L.PROV_WSL)
        self.assertTrue(probe.called)


class TheProbeDecidesTests(EndpointTestCase):
    """Reaching *an* endpoint is not reaching *the* endpoint."""

    def _response(self, status=200, payload=None, bad_json=False):
        r = MagicMock()
        r.status_code = status
        if bad_json:
            r.json.side_effect = ValueError("not json")
        else:
            r.json.return_value = payload
        return r

    def test_a_real_model_list_is_accepted(self):
        with patch.object(L.httpx, "get", return_value=self._response(
                payload={"data": [{"id": "qwen/qwen3-32b"}]})):
            self.assertTrue(L._probe_openai_compat("http://h:1234/v1"))

    def test_a_200_that_is_not_json_is_rejected(self):
        """A router admin page answers 200 all day."""
        with patch.object(L.httpx, "get", return_value=self._response(bad_json=True)):
            self.assertFalse(L._probe_openai_compat("http://h:1234/v1"))

    def test_a_200_with_no_models_loaded_is_rejected(self):
        with patch.object(L.httpx, "get", return_value=self._response(payload={"data": []})):
            self.assertFalse(L._probe_openai_compat("http://h:1234/v1"))

    def test_a_json_list_without_ids_is_rejected(self):
        with patch.object(L.httpx, "get", return_value=self._response(
                payload={"data": [{"object": "model"}]})):
            self.assertFalse(L._probe_openai_compat("http://h:1234/v1"))

    def test_a_connection_error_is_not_an_exception_to_the_caller(self):
        import httpx as _httpx
        with patch.object(L.httpx, "get", side_effect=_httpx.ConnectError("refused")):
            self.assertFalse(L._probe_openai_compat("http://h:1234/v1"))

    def test_the_discovered_address_is_probed_before_it_is_believed(self):
        """Under mirrored networking the default route is the LAN router,
        not the Windows host. Without this, that router becomes the LLM."""
        probe = self.reachable()          # nothing answers
        with patch.object(L, "_wsl_windows_host", return_value=GATEWAY):
            url, prov = L.resolve_endpoint()
        probe.assert_any_call(f"http://{GATEWAY}:1234/v1", "")
        self.assertIsNone(url)


class GatewayReadingTests(EndpointTestCase):

    ROUTE_HEADER = ("Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\t"
                    "Mask\t\tMTU\tWindow\tIRTT\n")

    @contextmanager
    def _route(self, body, wsl=True):
        import os
        from unittest.mock import mock_open
        real_open = open

        def fake_open(path, *a, **kw):
            if str(path) == "/proc/net/route":
                return mock_open(read_data=self.ROUTE_HEADER + body)(path, *a, **kw)
            if str(path) == "/proc/version":
                data = "Linux version 6.6 microsoft-standard-WSL2" if wsl else "Linux version 6.6 generic"
                return mock_open(read_data=data)(path, *a, **kw)
            return real_open(path, *a, **kw)

        env = {"WSL_DISTRO_NAME": "Ubuntu-24.04"} if wsl else {}
        with patch.dict(os.environ, env, clear=False):
            if not wsl:
                os.environ.pop("WSL_DISTRO_NAME", None)
            with patch("builtins.open", fake_open):
                yield

    def test_a_default_route_is_read_little_endian(self):
        with self._route("eth0\t00000000\t01301FAC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"):
            self.assertEqual(L._wsl_windows_host(), GATEWAY)

    def test_a_malformed_gateway_fails_safely(self):
        with self._route("eth0\t00000000\tZZZZZZZZ\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"):
            self.assertIsNone(L._wsl_windows_host())

    def test_a_truncated_table_fails_safely(self):
        with self._route("eth0\n"):
            self.assertIsNone(L._wsl_windows_host())

    def test_a_zero_gateway_is_not_reported_as_0_0_0_0(self):
        """It is a route with no next hop. Formatted, it is accepted
        everywhere and fails much later as a connect error."""
        with self._route("eth0\t00000000\t00000000\t0001\t0\t0\t0\t00000000\t0\t0\t0\n"):
            self.assertIsNone(L._wsl_windows_host())

    def test_a_table_with_no_default_route_yields_nothing(self):
        with self._route("eth0\t00301FAC\t00000000\t0001\t0\t0\t0\t00F0FFFF\t0\t0\t0\n"):
            self.assertIsNone(L._wsl_windows_host())

    def test_a_native_linux_host_never_reads_the_route_table(self):
        with self._route("eth0\t00000000\t01301FAC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n",
                         wsl=False):
            self.assertIsNone(L._wsl_windows_host())


class UnavailableIsSaidOutLoudTests(EndpointTestCase):

    def test_a_call_fails_with_the_reason_not_a_connect_error(self):
        with patch.object(L, "get_llm_config", return_value={
                "url": L.DEFAULT_URL, "model": "m", "api_key": "", "max_tokens": 10,
                "platform": "lmstudio", "provider": "openai_compat",
                "provenance": L.PROV_NONE}):
            with self.assertRaises(RuntimeError) as ctx:
                L.call_lm_studio("hello")
        self.assertIn("UNAVAILABLE", str(ctx.exception))

    def test_the_health_check_reports_provenance(self):
        self.reachable(f"http://{GATEWAY}:1234/v1")
        with patch.object(L, "_wsl_windows_host", return_value=GATEWAY), \
             patch.object(L, "get_db", side_effect=RuntimeError("no db")), \
             patch.object(L, "_resolve_model", lambda cfg: "qwen/qwen3-32b"), \
             patch.object(L.httpx, "get") as get:
            get.return_value.status_code = 200
            get.return_value.json.return_value = {"data": [{"id": "qwen/qwen3-32b"}]}
            health = L.check_health()
        self.assertTrue(health["ok"])
        self.assertEqual(health["provenance"], L.PROV_WSL)

    def test_the_health_check_says_unavailable_rather_than_ok_on_nothing(self):
        self.reachable()
        with patch.object(L, "_wsl_windows_host", return_value=None), \
             patch.object(L, "get_db", side_effect=RuntimeError("no db")):
            health = L.check_health()
        self.assertFalse(health["ok"])
        self.assertEqual(health["provenance"], L.PROV_NONE)


if __name__ == "__main__":
    unittest.main()
