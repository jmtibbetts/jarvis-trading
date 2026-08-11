"""
WebSocket push channel for the new Svelte dashboard (frontend/, served at
/next). Replaces the old dashboard's 60s full-page poll for panels that
should feel live — positions, signals, job status, the kill switch.

Threading note: APScheduler jobs run on BackgroundScheduler's own daemon
thread pool (app/scheduler.py), not on the asyncio event loop FastAPI/uvicorn
runs on. A job thread can't just `await manager.broadcast(...)` — there's no
running loop on that thread. broadcast_from_thread() bridges this with
asyncio.run_coroutine_threadsafe() against the loop captured at startup.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone

from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)


class ConnectionManager:
    def __init__(self):
        self._connections: set[WebSocket] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop):
        """Called once from main.py's lifespan startup, on the loop uvicorn runs."""
        self._loop = loop

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._connections.add(ws)
        logger.debug(f"[WS] Client connected ({len(self._connections)} total)")

    def disconnect(self, ws: WebSocket):
        self._connections.discard(ws)
        logger.debug(f"[WS] Client disconnected ({len(self._connections)} total)")

    async def broadcast(self, msg_type: str, data):
        if not self._connections:
            return
        envelope = json.dumps({
            "type": msg_type,
            "ts": datetime.now(timezone.utc).isoformat(),
            "data": data,
        }, default=str)
        dead = []
        # Iterate a SNAPSHOT: send_text awaits, and during that await another
        # coroutine on the same loop can connect/disconnect a client, mutating
        # the live set mid-iteration. Hit 53 times in production logs as
        # "RuntimeError: Set changed size during iteration", which the
        # orderbook streams' reconnect handler then misread as a connection
        # failure — churning all four exchange WebSockets simultaneously.
        for ws in list(self._connections):
            try:
                await ws.send_text(envelope)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._connections.discard(ws)

    def broadcast_from_thread(self, msg_type: str, data):
        """Fire-and-forget broadcast callable from any APScheduler job thread.
        Safe to call even when no clients are connected or the app is still
        starting up (self._loop is None) — jobs shouldn't need a try/except
        around every call site just to push a UI event."""
        if self._loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self.broadcast(msg_type, data), self._loop)
        except Exception as e:
            logger.debug(f"[WS] broadcast_from_thread failed: {e}")


manager = ConnectionManager()


async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        while True:
            # Clients don't need to send anything — this just detects disconnects.
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug(f"[WS] Connection error: {e}")
    finally:
        manager.disconnect(ws)
