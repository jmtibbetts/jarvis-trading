"""
Minimal MCP (Model Context Protocol) client — streamable-HTTP transport.

Connects Jarvis DIRECTLY to the remote MCP servers the user configured in
LM Studio. That indirection is necessary: LM Studio's MCP integration only
applies to its own chat UI — tool use does not pass through the OpenAI-
compatible API Jarvis calls — so to give the analyst these abilities, Jarvis
must speak MCP itself and drive the tool loop.

Transport facts verified live against all four servers while building this:
  - JSON-RPC 2.0 over HTTP POST; responses arrive either as plain JSON or as
    SSE frames ("event: message\\ndata: {...}") — both must be parsed.
  - The server assigns a session via the Mcp-Session-Id response header on
    initialize; it must be echoed on every subsequent request.
  - Auth status per server (live-tested):
      exa        works keyless (initialize 200)
      firecrawl  works keyless with usage limits ("Search, Scrape, and
                 Parse"); a bearer key unlocks account tools
      massive    401 without Authorization
      tavily     401 without Authorization
    Keys are read from env (MASSIVE_API_KEY, TAVILY_API_KEY, EXA_API_KEY,
    FIRECRAWL_API_KEY) and sent as Authorization: Bearer. A 401 server is
    reported as unavailable, never retried in a loop.

Sessions and tool lists are cached in-process; a transport error drops the
session so the next call re-initializes.
"""
from __future__ import annotations

import json
import logging
import os

import httpx

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-03-26"
HTTP_TIMEOUT = 30.0

# Mirrors the user's LM Studio mcpServers config.
MCP_SERVERS: dict[str, dict] = {
    "massive": {"url": "https://mcp.massive.com/", "key_env": "MASSIVE_API_KEY"},
    "tavily": {"url": "https://mcp.tavily.com/mcp/", "key_env": "TAVILY_API_KEY"},
    "exa": {"url": "https://mcp.exa.ai", "key_env": "EXA_API_KEY"},
    "firecrawl": {"url": "https://mcp.firecrawl.dev/v2/mcp", "key_env": "FIRECRAWL_API_KEY"},
}

_sessions: dict[str, str] = {}          # server -> Mcp-Session-Id
_tool_cache: dict[str, list[dict]] = {}  # server -> tools


def _headers(server: str) -> dict:
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    key_env = MCP_SERVERS[server].get("key_env")
    key = os.getenv(key_env or "")
    if key:
        h["Authorization"] = f"Bearer {key}"
    if server in _sessions:
        h["Mcp-Session-Id"] = _sessions[server]
    return h


def _parse_body(text: str) -> dict | None:
    """Response may be plain JSON or SSE frames; take the last data: frame."""
    text = (text or "").strip()
    if not text:
        return None
    if text.startswith("{"):
        try:
            return json.loads(text)
        except ValueError:
            return None
    last = None
    for line in text.splitlines():
        if line.startswith("data:"):
            try:
                last = json.loads(line[5:].strip())
            except ValueError:
                continue
    return last


def _rpc(server: str, method: str, params: dict | None = None, rpc_id: int | None = 1) -> dict | None:
    """One JSON-RPC call. rpc_id=None sends a notification (no response)."""
    cfg = MCP_SERVERS[server]
    payload: dict = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        payload["params"] = params
    if rpc_id is not None:
        payload["id"] = rpc_id
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT) as c:
            resp = c.post(cfg["url"], json=payload, headers=_headers(server))
        if resp.status_code == 401:
            logger.info(f"[MCP:{server}] 401 — needs an API key ({cfg.get('key_env')})")
            return None
        session = resp.headers.get("mcp-session-id")
        if session:
            _sessions[server] = session
        if rpc_id is None:
            return {}
        resp.raise_for_status()
        return _parse_body(resp.text)
    except httpx.HTTPError as e:
        logger.warning(f"[MCP:{server}] {method} failed: {e}")
        _sessions.pop(server, None)  # force re-initialize next time
        return None


def _ensure_session(server: str) -> bool:
    if server in _sessions:
        return True
    result = _rpc(server, "initialize", {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "jarvis-trading-ai", "version": "1.0"},
    })
    if not result or "result" not in result:
        return False
    _rpc(server, "notifications/initialized", {}, rpc_id=None)
    return True


def list_tools(server: str, force_refresh: bool = False) -> list[dict]:
    """Tools a server offers: [{name, description, inputSchema}]. Empty list
    when the server is unavailable/unauthorized — callers simply see fewer
    abilities, never an error."""
    if server not in MCP_SERVERS:
        return []
    if not force_refresh and server in _tool_cache:
        return _tool_cache[server]
    if not _ensure_session(server):
        return []
    result = _rpc(server, "tools/list", {})
    tools = ((result or {}).get("result") or {}).get("tools") or []
    if tools:
        _tool_cache[server] = tools
    return tools


def call_tool(server: str, tool_name: str, arguments: dict) -> str | None:
    """Invoke one tool; returns the concatenated text content, or None on
    failure. Non-text content blocks are ignored (the analyst consumes
    text)."""
    if server not in MCP_SERVERS or not _ensure_session(server):
        return None
    result = _rpc(server, "tools/call", {"name": tool_name, "arguments": arguments}, rpc_id=2)
    payload = (result or {}).get("result")
    if payload is None:
        err = (result or {}).get("error")
        if err:
            logger.warning(f"[MCP:{server}] {tool_name} error: {err.get('message')}")
        return None
    if payload.get("isError"):
        texts = [b.get("text", "") for b in payload.get("content") or [] if b.get("type") == "text"]
        logger.warning(f"[MCP:{server}] {tool_name} tool-level error: {' '.join(texts)[:200]}")
        return None
    texts = [b.get("text", "") for b in payload.get("content") or [] if b.get("type") == "text"]
    return "\n".join(t for t in texts if t) or None


def available_servers() -> dict[str, dict]:
    """Connectivity/auth status of every configured server, with tool counts
    for the reachable ones. Used by the analyst to advertise real abilities
    and by the UI to show which integrations need keys."""
    out = {}
    for server, cfg in MCP_SERVERS.items():
        tools = list_tools(server)
        out[server] = {
            "url": cfg["url"],
            "connected": bool(tools),
            "tool_count": len(tools),
            "tools": [t.get("name") for t in tools],
            "needs_key_env": cfg.get("key_env") if not tools and not os.getenv(cfg.get("key_env") or "") else None,
        }
    return out
