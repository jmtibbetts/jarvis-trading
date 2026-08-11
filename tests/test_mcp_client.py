import unittest
from unittest.mock import MagicMock, patch

import lib.mcp_client as mcp
from lib.mcp_client import _parse_body


class ParseBodyTests(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(_parse_body('{"jsonrpc":"2.0","id":1,"result":{}}')["id"], 1)

    def test_sse_frames_take_last_data_line(self):
        """Real observed shape from exa/firecrawl: SSE 'event: message' with a
        data: line carrying the JSON-RPC payload."""
        body = 'event: message\ndata: {"result":{"ok":1},"jsonrpc":"2.0","id":1}\n\n'
        self.assertEqual(_parse_body(body)["result"], {"ok": 1})

    def test_sse_with_id_line(self):
        # firecrawl includes an SSE id: line before data:
        body = 'event: message\nid: abc_123\ndata: {"result":{"x":2},"jsonrpc":"2.0","id":2}\n'
        self.assertEqual(_parse_body(body)["result"], {"x": 2})

    def test_multiple_data_frames_last_wins(self):
        body = 'data: {"id":1}\ndata: {"id":2}\n'
        self.assertEqual(_parse_body(body)["id"], 2)

    def test_empty_and_garbage(self):
        self.assertIsNone(_parse_body(""))
        self.assertIsNone(_parse_body("not json at all"))
        self.assertIsNone(_parse_body("{broken"))


class RpcBehaviorTests(unittest.TestCase):
    def setUp(self):
        mcp._sessions.clear()
        mcp._tool_cache.clear()

    def _response(self, status=200, text="", headers=None):
        resp = MagicMock()
        resp.status_code = status
        resp.text = text
        resp.headers = headers or {}
        resp.raise_for_status.return_value = None
        return resp

    @patch("lib.mcp_client.httpx.Client")
    def test_401_returns_none_and_no_session(self, client_cls):
        client_cls.return_value.__enter__.return_value.post.return_value = self._response(status=401)
        self.assertIsNone(mcp._rpc("tavily", "initialize", {}))
        self.assertNotIn("tavily", mcp._sessions)

    @patch("lib.mcp_client.httpx.Client")
    def test_session_header_captured_and_echoed(self, client_cls):
        post = client_cls.return_value.__enter__.return_value.post
        post.return_value = self._response(
            text='{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-03-26"}}',
            headers={"mcp-session-id": "sess-42"},
        )
        mcp._rpc("exa", "initialize", {})
        self.assertEqual(mcp._sessions["exa"], "sess-42")
        # next call must echo it
        mcp._rpc("exa", "tools/list", {})
        sent_headers = post.call_args.kwargs["headers"]
        self.assertEqual(sent_headers.get("Mcp-Session-Id"), "sess-42")

    @patch("lib.mcp_client.httpx.Client")
    def test_transport_error_drops_session_for_reinit(self, client_cls):
        import httpx
        mcp._sessions["exa"] = "stale"
        client_cls.return_value.__enter__.return_value.post.side_effect = httpx.ConnectError("down")
        self.assertIsNone(mcp._rpc("exa", "tools/list", {}))
        self.assertNotIn("exa", mcp._sessions)

    @patch("lib.mcp_client._rpc")
    def test_call_tool_concatenates_text_content(self, rpc):
        mcp._sessions["exa"] = "s"
        rpc.return_value = {"result": {"content": [
            {"type": "text", "text": "part one"},
            {"type": "image", "data": "..."},
            {"type": "text", "text": "part two"},
        ]}}
        self.assertEqual(mcp.call_tool("exa", "web_search_exa", {"query": "x"}), "part one\npart two")

    @patch("lib.mcp_client._rpc")
    def test_tool_level_error_returns_none(self, rpc):
        mcp._sessions["exa"] = "s"
        rpc.return_value = {"result": {"isError": True, "content": [{"type": "text", "text": "quota exceeded"}]}}
        self.assertIsNone(mcp.call_tool("exa", "web_search_exa", {"query": "x"}))

    def test_unknown_server_rejected(self):
        self.assertEqual(mcp.list_tools("nonsense"), [])
        self.assertIsNone(mcp.call_tool("nonsense", "t", {}))

    @patch.dict("os.environ", {"TAVILY_API_KEY": "tvly-secret"})
    def test_key_env_becomes_bearer_header(self):
        h = mcp._headers("tavily")
        self.assertEqual(h["Authorization"], "Bearer tvly-secret")

    def test_no_key_no_auth_header(self):
        import os
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("EXA_API_KEY", None)
            self.assertNotIn("Authorization", mcp._headers("exa"))


if __name__ == "__main__":
    unittest.main()
