"""Real uvicorn WebSocket integration test.

All other WebSocket tests use Starlette TestClient or direct coroutine
invocations, which bypass uvicorn's WSProtocol and the websockets library
entirely.  This test exercises the full handshake-auth-message-close cycle
through a live uvicorn server so that a uvicorn<->websockets incompatibility
fails here instead of silently passing CI and only surfacing in Cloud Run.
"""

import asyncio
import json
import socket
import uuid
from contextlib import asynccontextmanager

import pytest
import uvicorn
import websockets
from fastapi import FastAPI

from app.auth import create_ws_ticket
from app.routers.projects import router as projects_router


def _find_free_port() -> int:
    """Bind to port 0 so the OS assigns an available port, then return it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@asynccontextmanager
async def _noop_lifespan(app):
    yield


# Minimal ASGI app containing only the projects router (where the WS endpoint
# lives).  Using the full app would trigger the lifespan which starts background
# workers and expects a real Postgres schema; the WS endpoint itself only needs
# the projects router and the module-level broadcast manager singleton.
_ws_test_app = FastAPI(lifespan=_noop_lifespan)
_ws_test_app.include_router(projects_router)


async def test_real_uvicorn_websocket_roundtrip():
    """WebSocket round-trip (handshake -> ticket auth -> message -> close) through
    a live uvicorn server with the real websockets==12.0 stack.

    Unlike TestClient-based tests this exercises uvicorn.WSProtocol and the
    websockets library, so a post-bump incompatibility surfaces here rather than
    reaching Cloud Run.
    """
    project_id = str(uuid.uuid4())
    port = _find_free_port()

    config = uvicorn.Config(
        _ws_test_app,
        host="127.0.0.1",
        port=port,
        log_level="error",
        lifespan="on",
    )
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        else:
            pytest.fail("uvicorn server did not start within 5 seconds")

        ticket = create_ws_ticket("test-user", project_id)
        url = f"ws://127.0.0.1:{port}/api/projects/{project_id}/ws"

        async with websockets.connect(url) as ws:
            # Authenticate: first message must be the ws_ticket
            await ws.send(json.dumps({"ticket": ticket}))

            # Receive "connected" event; deploy_state may also arrive (skipped)
            connected = False
            for _ in range(5):
                raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                msg = json.loads(raw)
                if msg.get("event") == "connected":
                    assert msg["data"]["project_id"] == project_id
                    connected = True
                    break
            assert connected, "Did not receive 'connected' event from server"

            # Ping -> pong; drain any buffered events (e.g. deploy_state) first
            await ws.send(json.dumps({"type": "ping"}))
            pong_received = False
            for _ in range(5):
                raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                msg = json.loads(raw)
                if msg.get("event") == "pong":
                    pong_received = True
                    break
            assert pong_received, "Did not receive 'pong' event after sending ping"

    finally:
        server.should_exit = True
        await asyncio.wait_for(serve_task, timeout=10.0)
