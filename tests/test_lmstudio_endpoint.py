"""An assigned address had been stored as if it were a chosen one.

`http://192.168.0.229:1234/v1` sat in PlatformConfig, and `get_llm_config`
read the DB before anything else — so a DHCP lease the operator never
reserved outranked a routing table that knew the right answer. It worked
until the lease moved, and then every LLM call failed with a connect error
that read like LM Studio being down.

Two rules replaced it, and these tests pin both.

An address is authoritative only when someone chose it. A chosen address is
then honoured all the way through failure: it is probed for the truth, and a
broken one is REPORTED broken, never quietly swapped for a working machine
nobody picked. Only automatic candidates — the shipped loopback default and
the WSL gateway — may be walked past.

And "down" is not "up with nothing loaded". Collapsing the two sends the
operator to check the network when the fix is to load a model.
"""
import unittest
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from lib import lmstudio as L


GATEWAY = "172.31.48.1"
STALE_DHCP = "http://192.168.0.229:1234/v1"

UP = L.ProbeResult(L.ST_AVAILABLE, ("qwen/qwen3-32b",), None)
NO_MODELS = L.ProbeResult(L.ST_NO_MODELS, (), "server is up with no model loaded")
INVALID = L.ProbeResult(L.ST_INVALID, (), "/v1/models did not return JSON")
DOWN = L.ProbeResult(L.ST_UNREACHABLE, (), "ConnectError: refused")


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

    def world(self, **by_url):
        """Patch the probe so each URL reports the ProbeResult given for it.
        Anything unnamed is unreachable."""
        probe = MagicMock(side_effect=lambda url, api_key="", timeout=3.0:
                          by_url.get(url, DOWN))
        p = patch.object(L, "_probe_endpoint", probe)
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

    def in_wsl(self, gateway=GATEWAY):
        p = patch.object(L, "_wsl_windows_host", return_value=gateway)
        p.start()
        self.addCleanup(p.stop)


class ExplicitOverridesAreObeyedTests(EndpointTestCase):
    """An override is a statement of intent, not a hint."""

    def test_an_explicit_env_address_is_used(self):
        import os
        os.environ["LM_STUDIO_URL"] = "http://10.0.0.5:9999/v1"
        self.world(**{"http://10.0.0.5:9999/v1": UP,
                      f"http://{GATEWAY}:1234/v1": UP})
        self.in_wsl()
        res = L.resolve_endpoint()
        self.assertEqual(res.url, "http://10.0.0.5:9999/v1")
        self.assertEqual(res.provenance, L.PROV_ENV)
        self.assertEqual(res.status, L.ST_AVAILABLE)

    def test_a_marked_operator_row_is_used(self):
        self.world(**{"http://10.0.0.7:1234/v1": UP,
                      f"http://{GATEWAY}:1234/v1": UP})
        self.in_wsl()
        res = L.resolve_endpoint(db_url="http://10.0.0.7:1234/v1",
                                 db_provenance=L.PROV_OPERATOR)
        self.assertEqual(res.url, "http://10.0.0.7:1234/v1")
        self.assertEqual(res.provenance, L.PROV_OPERATOR)

    def test_an_explicit_localhost_is_an_override_not_a_default(self):
        """The regression this replaced: loopback used to fall through on the
        theory that nobody means localhost. Someone who typed it does."""
        import os
        os.environ["LM_STUDIO_URL"] = "http://127.0.0.1:1234/v1"
        self.world(**{"http://127.0.0.1:1234/v1": UP,
                      f"http://{GATEWAY}:1234/v1": UP})
        self.in_wsl()
        res = L.resolve_endpoint()
        self.assertEqual(res.url, "http://127.0.0.1:1234/v1")
        self.assertEqual(res.provenance, L.PROV_ENV)


class ExplicitFailuresAreReportedNotRoutedAroundTests(EndpointTestCase):
    """A working machine nobody chose is the wrong answer to a broken one
    somebody did."""

    def test_a_dead_env_localhost_does_not_fall_through_to_the_gateway(self):
        import os
        os.environ["LM_STUDIO_URL"] = "http://127.0.0.1:1234/v1"
        self.world(**{f"http://{GATEWAY}:1234/v1": UP})     # gateway is fine
        self.in_wsl()
        res = L.resolve_endpoint()
        self.assertEqual(res.url, "http://127.0.0.1:1234/v1")
        self.assertEqual(res.provenance, L.PROV_ENV)
        self.assertEqual(res.status, L.ST_UNREACHABLE)
        self.assertNotIn(GATEWAY, str(res.candidates))

    def test_a_dead_env_remote_does_not_fall_through_either(self):
        import os
        os.environ["LM_STUDIO_URL"] = "http://10.0.0.5:9999/v1"
        self.world(**{f"http://{GATEWAY}:1234/v1": UP,
                      "http://127.0.0.1:1234/v1": UP})
        self.in_wsl()
        res = L.resolve_endpoint()
        self.assertEqual(res.url, "http://10.0.0.5:9999/v1")
        self.assertEqual(res.status, L.ST_UNREACHABLE)

    def test_a_dead_marked_db_override_does_not_fall_through(self):
        self.world(**{f"http://{GATEWAY}:1234/v1": UP})
        self.in_wsl()
        res = L.resolve_endpoint(db_url="http://10.0.0.7:1234/v1",
                                 db_provenance=L.PROV_OPERATOR)
        self.assertEqual(res.url, "http://10.0.0.7:1234/v1")
        self.assertEqual(res.provenance, L.PROV_OPERATOR)
        self.assertEqual(res.status, L.ST_UNREACHABLE)

    def test_an_override_with_no_model_loaded_says_so(self):
        """Not unreachable. The address is right and the fix is elsewhere."""
        import os
        os.environ["LM_STUDIO_URL"] = "http://10.0.0.5:1234/v1"
        self.world(**{"http://10.0.0.5:1234/v1": NO_MODELS,
                      f"http://{GATEWAY}:1234/v1": UP})
        self.in_wsl()
        res = L.resolve_endpoint()
        self.assertEqual(res.url, "http://10.0.0.5:1234/v1")
        self.assertEqual(res.status, L.ST_NO_MODELS)
        problem = L.endpoint_problem(res)
        self.assertIn("10.0.0.5", problem)
        self.assertIn("Load a model", problem)
        self.assertNotIn(GATEWAY, problem)

    def test_the_override_is_probed_exactly_once_and_nothing_else_is(self):
        import os
        os.environ["LM_STUDIO_URL"] = "http://10.0.0.5:1234/v1"
        probe = self.world()
        self.in_wsl()
        L.resolve_endpoint()
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(probe.call_args[0][0], "http://10.0.0.5:1234/v1")

    def test_the_env_override_wins_and_the_db_override_is_not_a_backup(self):
        """Two explicit addresses is a configuration mistake, not a failover
        pair. Substituting the second is the same silent swap."""
        import os
        os.environ["LM_STUDIO_URL"] = "http://10.0.0.5:1234/v1"
        self.world(**{"http://10.0.0.7:1234/v1": UP})
        res = L.resolve_endpoint(db_url="http://10.0.0.7:1234/v1",
                                 db_provenance=L.PROV_OPERATOR)
        self.assertEqual(res.url, "http://10.0.0.5:1234/v1")
        self.assertEqual(res.status, L.ST_UNREACHABLE)


class AutomaticCandidatesMayBeWalkedPastTests(EndpointTestCase):
    """With no override, everything is a guess held only until a better one."""

    def test_loopback_wins_when_it_actually_answers(self):
        self.world(**{"http://127.0.0.1:1234/v1": UP,
                      f"http://{GATEWAY}:1234/v1": UP})
        self.in_wsl()
        res = L.resolve_endpoint()
        self.assertEqual(res.url, "http://127.0.0.1:1234/v1")
        self.assertEqual(res.provenance, L.PROV_LOCALHOST)

    def test_a_dead_loopback_falls_through_to_the_gateway(self):
        self.world(**{f"http://{GATEWAY}:1234/v1": UP})
        self.in_wsl()
        res = L.resolve_endpoint()
        self.assertEqual(res.url, f"http://{GATEWAY}:1234/v1")
        self.assertEqual(res.provenance, L.PROV_WSL)
        self.assertEqual(res.status, L.ST_AVAILABLE)

    def test_a_loopback_with_no_models_also_falls_through(self):
        """The 5-model desk instance versus the 26-model server, generalised:
        an endpoint with nothing loaded cannot serve inference, so automatic
        discovery keeps looking."""
        self.world(**{"http://127.0.0.1:1234/v1": NO_MODELS,
                      f"http://{GATEWAY}:1234/v1": UP})
        self.in_wsl()
        res = L.resolve_endpoint()
        self.assertEqual(res.provenance, L.PROV_WSL)

    def test_but_the_skipped_candidate_keeps_its_real_status(self):
        """Walked past is not the same as unreachable, and the diagnostics
        have to still say which."""
        self.world(**{"http://127.0.0.1:1234/v1": NO_MODELS,
                      f"http://{GATEWAY}:1234/v1": UP})
        self.in_wsl()
        res = L.resolve_endpoint()
        skipped = [c for c in res.candidates if c.provenance == L.PROV_LOCALHOST]
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0].status, L.ST_NO_MODELS)

    def test_nothing_usable_reports_the_furthest_any_candidate_got(self):
        """If one was up with no model loaded, that is the headline — it names
        the fix. "Nothing answered" would not."""
        self.world(**{"http://127.0.0.1:1234/v1": NO_MODELS})
        self.in_wsl()
        res = L.resolve_endpoint()
        self.assertIsNone(res.url)
        self.assertEqual(res.provenance, L.PROV_NONE)
        self.assertEqual(res.status, L.ST_NO_MODELS)
        self.assertIn("Load a model", L.endpoint_problem(res))

    def test_nothing_answering_at_all_is_unreachable(self):
        self.world()
        self.in_wsl()
        res = L.resolve_endpoint()
        self.assertIsNone(res.url)
        self.assertEqual(res.status, L.ST_UNREACHABLE)

    def test_a_junk_responder_ranks_above_silence_but_below_no_models(self):
        self.world(**{"http://127.0.0.1:1234/v1": INVALID})
        self.in_wsl()
        self.assertEqual(L.resolve_endpoint().status, L.ST_INVALID)

    def test_both_candidates_are_recorded_even_when_neither_works(self):
        self.world()
        self.in_wsl()
        res = L.resolve_endpoint()
        self.assertEqual([c.provenance for c in res.candidates],
                         [L.PROV_LOCALHOST, L.PROV_WSL])

    def test_off_wsl_only_loopback_is_a_candidate(self):
        self.world()
        self.in_wsl(gateway=None)
        res = L.resolve_endpoint()
        self.assertEqual([c.provenance for c in res.candidates], [L.PROV_LOCALHOST])


class TheStaleRowLosesTests(EndpointTestCase):

    def test_an_unmarked_dhcp_address_does_not_outrank_discovery(self):
        """The original bug, stated as a test: this row used to win outright."""
        self.db_returns(_Row(api_url=STALE_DHCP, extra_field_3=None))
        self.world(**{f"http://{GATEWAY}:1234/v1": UP})
        self.in_wsl()
        cfg = L.get_llm_config()
        self.assertEqual(cfg["model"], "db-model", "the DB branch must be under test")
        self.assertEqual(cfg["url"], f"http://{GATEWAY}:1234/v1")
        self.assertEqual(cfg["provenance"], L.PROV_WSL)

    def test_an_unmarked_row_is_not_even_probed(self):
        """It is not a weak candidate. It is not a candidate."""
        self.db_returns(_Row(api_url=STALE_DHCP))
        probe = self.world(**{f"http://{GATEWAY}:1234/v1": UP})
        self.in_wsl()
        L.get_llm_config()
        self.assertNotIn("192.168.0.229", str(probe.call_args_list))

    def test_the_port_survives_even_though_the_host_does_not(self):
        """Nothing assigns a port. It is the one real piece of configuration
        left in a row whose host has gone stale."""
        self.db_returns(_Row(api_url="http://192.168.0.229:4321/v1"))
        self.world(**{f"http://{GATEWAY}:4321/v1": UP})
        self.in_wsl()
        self.assertEqual(L.get_llm_config()["url"], f"http://{GATEWAY}:4321/v1")

    def test_a_marked_row_still_wins_because_someone_chose_it(self):
        self.db_returns(_Row(api_url=STALE_DHCP, extra_field_3=L.PROV_OPERATOR))
        self.world(**{STALE_DHCP: UP, f"http://{GATEWAY}:1234/v1": UP})
        self.in_wsl()
        cfg = L.get_llm_config()
        self.assertEqual(cfg["url"], STALE_DHCP)
        self.assertEqual(cfg["provenance"], L.PROV_OPERATOR)

    def test_a_hosted_provider_url_is_never_probed_away(self):
        """Only LM Studio's address is assigned. An OpenAI or Anthropic
        api_url is configuration and must pass through untouched."""
        self.db_returns(_Row(platform="anthropic", api_url="https://api.anthropic.com"))
        probe = self.world()
        cfg = L.get_llm_config()
        self.assertEqual(cfg["url"], "https://api.anthropic.com")
        self.assertEqual(cfg["provenance"], L.PROV_OPERATOR)
        probe.assert_not_called()


class NothingDiscoveredIsWrittenBackTests(EndpointTestCase):

    def test_resolution_never_writes_to_the_database(self):
        row = _Row(api_url=STALE_DHCP)
        db = self.db_returns(row)
        self.world(**{f"http://{GATEWAY}:1234/v1": UP})
        self.in_wsl()
        L.get_llm_config()
        self.assertEqual(row.api_url, STALE_DHCP, "the row was mutated in place")
        db.commit.assert_not_called()
        db.add.assert_not_called()

    def test_only_an_operator_choice_is_allowed_to_persist(self):
        self.assertEqual(set(L.PERSISTABLE_PROVENANCE), {L.PROV_OPERATOR})

    def test_the_next_process_rediscovers_rather_than_remembering(self):
        """The cache is per-process by construction: a gateway is assigned
        per boot, so carrying one across a restart is carrying a stale fact."""
        probe = self.world(**{f"http://{GATEWAY}:1234/v1": UP})
        self.in_wsl()
        L.resolve_endpoint()
        first = probe.call_count
        L.resolve_endpoint()
        self.assertEqual(probe.call_count, first, "cached within the process")

        L.reset_endpoint_cache()          # stands in for a fresh process
        L.resolve_endpoint()
        self.assertGreater(probe.call_count, first)

    def test_an_unusable_result_is_retried_rather_than_cached_forever(self):
        self.world()
        self.in_wsl()
        with patch.object(L, "ENDPOINT_RETRY_S", 0.0):
            self.assertEqual(L.resolve_endpoint().status, L.ST_UNREACHABLE)
            self.world(**{f"http://{GATEWAY}:1234/v1": UP})
            self.assertEqual(L.resolve_endpoint().status, L.ST_AVAILABLE)


class TheProbeGradesRatherThanJudgesTests(EndpointTestCase):
    """Reaching *an* endpoint is not reaching *the* endpoint — but the
    grades in between have to survive."""

    def _response(self, status=200, payload=None, bad_json=False):
        r = MagicMock()
        r.status_code = status
        if bad_json:
            r.json.side_effect = ValueError("not json")
        else:
            r.json.return_value = payload
        return r

    def _probe(self, **kw):
        with patch.object(L.httpx, "get", **kw):
            return L._probe_endpoint("http://h:1234/v1")

    def test_a_real_model_list_is_available(self):
        got = self._probe(return_value=self._response(
            payload={"data": [{"id": "qwen/qwen3-32b"}, {"id": "gemma"}]}))
        self.assertEqual(got.status, L.ST_AVAILABLE)
        self.assertEqual(got.models, ("qwen/qwen3-32b", "gemma"))

    def test_an_empty_model_list_is_reachable_with_no_models(self):
        """Up, correct, and nothing loaded. Not a network fault."""
        got = self._probe(return_value=self._response(payload={"data": []}))
        self.assertEqual(got.status, L.ST_NO_MODELS)

    def test_a_200_that_is_not_json_is_invalid(self):
        """A router admin page answers 200 all day."""
        self.assertEqual(self._probe(return_value=self._response(bad_json=True)).status,
                         L.ST_INVALID)

    def test_a_bare_200_with_no_model_list_is_invalid(self):
        self.assertEqual(self._probe(return_value=self._response(payload={"ok": True})).status,
                         L.ST_INVALID)

    def test_entries_without_ids_are_invalid_not_merely_empty(self):
        """Bogus entries are a wrong server, not an idle one."""
        self.assertEqual(self._probe(return_value=self._response(
            payload={"data": [{"object": "model"}]})).status, L.ST_INVALID)

    def test_a_non_200_is_invalid(self):
        self.assertEqual(self._probe(return_value=self._response(status=404)).status,
                         L.ST_INVALID)

    def test_a_refused_connection_is_unreachable(self):
        import httpx as _httpx
        got = self._probe(side_effect=_httpx.ConnectError("refused"))
        self.assertEqual(got.status, L.ST_UNREACHABLE)
        self.assertIn("ConnectError", got.detail)

    def test_a_timeout_is_unreachable_rather_than_an_exception(self):
        import httpx as _httpx
        self.assertEqual(self._probe(side_effect=_httpx.ReadTimeout("slow")).status,
                         L.ST_UNREACHABLE)

    def test_the_discovered_address_is_probed_before_it_is_believed(self):
        """Under mirrored networking the default route is the LAN router,
        not the Windows host. Without this, that router becomes the LLM."""
        probe = self.world()          # nothing answers
        self.in_wsl()
        res = L.resolve_endpoint()
        probe.assert_any_call(f"http://{GATEWAY}:1234/v1", "")
        self.assertIsNone(res.url)


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


class TheFailureIsSaidOutLoudTests(EndpointTestCase):

    def _cfg(self, status, provenance=L.PROV_NONE, url=None, candidates=()):
        return {"url": url or L.DEFAULT_URL, "model": "m", "api_key": "",
                "max_tokens": 10, "platform": "lmstudio",
                "provider": "openai_compat", "provenance": provenance,
                "endpoint_status": status, "endpoint_candidates": candidates}

    def test_a_call_fails_with_the_reason_not_a_connect_error(self):
        with patch.object(L, "get_llm_config",
                          return_value=self._cfg(L.ST_UNREACHABLE)):
            with self.assertRaises(RuntimeError) as ctx:
                L.call_lm_studio("hello")
        self.assertIn(L.ST_UNREACHABLE, str(ctx.exception))

    def test_a_call_against_an_idle_server_says_load_a_model(self):
        with patch.object(L, "get_llm_config",
                          return_value=self._cfg(L.ST_NO_MODELS)):
            with self.assertRaises(RuntimeError) as ctx:
                L.call_lm_studio("hello")
        self.assertIn("Load a model", str(ctx.exception))

    def test_a_config_without_endpoint_status_is_left_alone(self):
        """Hosted providers and older call sites never went through the
        resolver and must not start failing because of it."""
        captured = {}

        def fake_compat(prompt, system, max_tokens, temperature, cfg, **kw):
            captured["called"] = True
            return "ok"

        cfg = {"url": "http://x", "model": "m", "api_key": "", "max_tokens": 10,
               "platform": "lmstudio", "provider": "openai_compat"}
        with patch.object(L, "get_llm_config", return_value=cfg), \
             patch.object(L, "_resolve_model", lambda c: "m"), \
             patch.object(L, "_call_openai_compat", fake_compat):
            L.call_lm_studio("hello", thinking=False)
        self.assertTrue(captured.get("called"))

    def test_the_health_check_reports_provenance_and_status(self):
        self.world(**{f"http://{GATEWAY}:1234/v1": UP})
        self.in_wsl()
        with patch.object(L, "get_db", side_effect=RuntimeError("no db")), \
             patch.object(L, "_resolve_model", lambda cfg: "qwen/qwen3-32b"), \
             patch.object(L.httpx, "get") as get:
            get.return_value.status_code = 200
            get.return_value.json.return_value = {"data": [{"id": "qwen/qwen3-32b"}]}
            health = L.check_health()
        self.assertTrue(health["ok"])
        self.assertEqual(health["provenance"], L.PROV_WSL)
        self.assertEqual(health["status"], L.ST_AVAILABLE)

    def test_the_health_check_keeps_a_skipped_candidates_real_status(self):
        """localhost was up with nothing loaded. The operator must be able to
        see that, not a flat "unreachable"."""
        self.world(**{"http://127.0.0.1:1234/v1": NO_MODELS,
                      f"http://{GATEWAY}:1234/v1": UP})
        self.in_wsl()
        with patch.object(L, "get_db", side_effect=RuntimeError("no db")), \
             patch.object(L, "_resolve_model", lambda cfg: "qwen/qwen3-32b"), \
             patch.object(L.httpx, "get") as get:
            get.return_value.status_code = 200
            get.return_value.json.return_value = {"data": [{"id": "qwen/qwen3-32b"}]}
            health = L.check_health()
        local = [t for t in health["tried"] if t["provenance"] == L.PROV_LOCALHOST]
        self.assertEqual(local[0]["status"], L.ST_NO_MODELS)

    def test_the_health_check_distinguishes_idle_from_down(self):
        self.world(**{"http://127.0.0.1:1234/v1": NO_MODELS})
        self.in_wsl(gateway=None)
        with patch.object(L, "get_db", side_effect=RuntimeError("no db")):
            health = L.check_health()
        self.assertFalse(health["ok"])
        self.assertEqual(health["status"], L.ST_NO_MODELS)
        self.assertIn("Load a model", health["error"])

    def test_the_health_check_names_an_explicit_endpoint_that_failed(self):
        import os
        os.environ["LM_STUDIO_URL"] = "http://10.0.0.5:1234/v1"
        self.world(**{f"http://{GATEWAY}:1234/v1": UP})
        self.in_wsl()
        with patch.object(L, "get_db", side_effect=RuntimeError("no db")):
            health = L.check_health()
        self.assertFalse(health["ok"])
        self.assertEqual(health["url"], "http://10.0.0.5:1234/v1")
        self.assertEqual(health["provenance"], L.PROV_ENV)
        self.assertIn("no other endpoint was tried", health["error"])


if __name__ == "__main__":
    unittest.main()
