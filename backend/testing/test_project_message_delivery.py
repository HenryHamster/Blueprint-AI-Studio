"""Regression tests for ProjectMessage.delivered_at stamping via WebSocket message_ack.

Covers:
1. Ack stamps delivered_at end-to-end (real uvicorn server, real DB).
2. Idempotency: second ack does NOT change the already-set timestamp.
3. Cross-project isolation: unknown/cross-project message_id is a silent no-op.
4. project_message_base projection surfaces delivered_at correctly.

Uses AsyncSessionLocal (patched to the test engine by conftest) directly rather
than the db_session fixture, to avoid triggering the pre-existing projecttelemetry
teardown error (the table is named project_telemetry in migrations but the model
derives projecttelemetry, so conftest's cleanup DELETEs from the wrong name).
"""

import asyncio
import json
import socket
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest
import uvicorn
import websockets
from fastapi import FastAPI
from sqlalchemy import select

from app.auth import create_ws_ticket
from app.database import AsyncSessionLocal
from app.models import Project, ProjectMessage
from app.routers.projects import router as projects_router


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@asynccontextmanager
async def _noop_lifespan(app):
    yield


_ws_delivery_app = FastAPI(lifespan=_noop_lifespan)
_ws_delivery_app.include_router(projects_router)


async def _start_server() -> tuple:
    port = _find_free_port()
    config = uvicorn.Config(
        _ws_delivery_app,
        host="127.0.0.1",
        port=port,
        log_level="error",
        lifespan="on",
    )
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("uvicorn server did not start within 5 seconds")
    return server, serve_task, port


async def _stop_server(server, serve_task) -> None:
    server.should_exit = True
    await asyncio.wait_for(serve_task, timeout=10.0)


async def _create_project(name: str = "delivery-test") -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        project = Project(name=name, user_id="delivery-test-user")
        session.add(project)
        await session.commit()
        return project.id


async def _create_message(project_id: uuid.UUID, *, delivered_at=None) -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        msg = ProjectMessage(
            project_id=project_id,
            from_type="system",
            message_type="NOTE",
            status="open",
            content="Test strategist reply",
            context={},
            delivered_at=delivered_at,
        )
        session.add(msg)
        await session.commit()
        return msg.id


async def _get_message(msg_id: uuid.UUID) -> ProjectMessage | None:
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(ProjectMessage).where(ProjectMessage.id == msg_id)
            )
        ).scalars().first()
        return row


async def _ws_authenticate(ws, project_id: uuid.UUID) -> None:
    """Send ticket and drain until 'connected' event is received."""
    ticket = create_ws_ticket("test-user", str(project_id))
    await ws.send(json.dumps({"ticket": ticket}))
    for _ in range(10):
        raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
        msg = json.loads(raw)
        if msg.get("event") == "connected":
            return
    pytest.fail("Did not receive 'connected' event during WS authentication")


# ---------------------------------------------------------------------------
# Test 1: Ack stamps delivered_at end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_message_ack_stamps_delivered_at():
    """Sending message_ack over a real WS connection stamps delivered_at durably.

    This is the named acceptance test for the delivered_at stamping path
    (PR #16040, migration 0670): create a ProjectMessage with delivered_at=NULL,
    open a real WebSocket client, authenticate, send a message_ack frame, then
    re-query the row from the DB and assert delivered_at is now non-NULL.
    """
    project_id = await _create_project("ack-stamps-test")
    msg_id = await _create_message(project_id, delivered_at=None)

    server, serve_task, port = await _start_server()
    try:
        url = f"ws://127.0.0.1:{port}/api/projects/{str(project_id)}/ws"
        async with websockets.connect(url) as ws:
            await _ws_authenticate(ws, project_id)
            await ws.send(json.dumps({"type": "message_ack", "message_id": str(msg_id)}))

            # Poll until the handler commits the stamp (async, in a separate session).
            stamped = None
            for _ in range(20):
                await asyncio.sleep(0.05)
                row = await _get_message(msg_id)
                if row is not None and row.delivered_at is not None:
                    stamped = row.delivered_at
                    break

            assert stamped is not None, (
                "delivered_at was not stamped after message_ack — "
                "the WS handler may not have committed or the engine patch is broken"
            )
    finally:
        await _stop_server(server, serve_task)


# ---------------------------------------------------------------------------
# Test 2: Idempotency — second ack must not change delivered_at
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_message_ack_is_idempotent():
    """A second message_ack for the same message must not overwrite delivered_at.

    The handler only stamps when delivered_at IS NULL; once set, subsequent acks
    are silent no-ops so the original delivery timestamp is preserved.
    """
    project_id = await _create_project("ack-idempotent-test")
    msg_id = await _create_message(project_id, delivered_at=None)

    server, serve_task, port = await _start_server()
    try:
        url = f"ws://127.0.0.1:{port}/api/projects/{str(project_id)}/ws"
        async with websockets.connect(url) as ws:
            await _ws_authenticate(ws, project_id)

            # First ack — must stamp delivered_at.
            await ws.send(json.dumps({"type": "message_ack", "message_id": str(msg_id)}))

            first_stamp = None
            for _ in range(20):
                await asyncio.sleep(0.05)
                row = await _get_message(msg_id)
                if row is not None and row.delivered_at is not None:
                    first_stamp = row.delivered_at
                    break

            assert first_stamp is not None, "First ack did not stamp delivered_at"

            # Second ack — handler must skip because delivered_at is already set.
            await ws.send(json.dumps({"type": "message_ack", "message_id": str(msg_id)}))
            await asyncio.sleep(0.2)

            row2 = await _get_message(msg_id)
            assert row2 is not None
            assert row2.delivered_at == first_stamp, (
                f"Second ack overwrote delivered_at: "
                f"{first_stamp!r} → {row2.delivered_at!r}; "
                "handler must only stamp when delivered_at IS NULL"
            )
    finally:
        await _stop_server(server, serve_task)


# ---------------------------------------------------------------------------
# Test 3: Cross-project isolation — unknown message_id is a safe no-op
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_message_ack_unknown_id_is_silent():
    """An ack with a non-existent or cross-project message_id must not raise
    and must not stamp any row in the DB.
    """
    project_id = await _create_project("ack-isolation-test")

    # A real message belonging to a DIFFERENT project (cross-project attempt).
    other_project_id = await _create_project("ack-isolation-other")
    other_msg_id = await _create_message(other_project_id, delivered_at=None)

    server, serve_task, port = await _start_server()
    try:
        url = f"ws://127.0.0.1:{port}/api/projects/{str(project_id)}/ws"
        async with websockets.connect(url) as ws:
            await _ws_authenticate(ws, project_id)

            # Ack for a completely random UUID (row does not exist).
            await ws.send(json.dumps({"type": "message_ack", "message_id": str(uuid.uuid4())}))

            # Ack for a message that exists but belongs to a DIFFERENT project.
            await ws.send(json.dumps({"type": "message_ack", "message_id": str(other_msg_id)}))

            # Verify WS is still alive (pong works) — no crash from bad acks.
            await ws.send(json.dumps({"type": "ping"}))
            pong_received = False
            for _ in range(10):
                raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                msg = json.loads(raw)
                if msg.get("event") == "pong":
                    pong_received = True
                    break
            assert pong_received, "WS connection broken after unknown message_ack"

            # Brief pause to let any handler commit land, then verify no stamp.
            await asyncio.sleep(0.2)
            other_row = await _get_message(other_msg_id)
            assert other_row is not None
            assert other_row.delivered_at is None, (
                "Cross-project message_ack stamped another project's message — "
                "the project_id filter in the WS handler is broken"
            )
    finally:
        await _stop_server(server, serve_task)


# ---------------------------------------------------------------------------
# Test 4: project_message_base projection surfaces delivered_at
# ---------------------------------------------------------------------------


def test_project_message_base_includes_delivered_at_none():
    """project_message_base returns delivered_at=None when the column is NULL."""
    from app.project_message_projection import project_message_base

    msg = ProjectMessage(
        project_id=uuid.uuid4(),
        from_type="system",
        message_type="NOTE",
        status="open",
        content="Test",
        context={},
        delivered_at=None,
    )
    result = project_message_base(msg)
    assert "delivered_at" in result, "project_message_base must include 'delivered_at' key"
    assert result["delivered_at"] is None


def test_project_message_base_includes_delivered_at_iso_string():
    """project_message_base returns an ISO string when delivered_at is set."""
    from app.project_message_projection import project_message_base

    stamp = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    msg = ProjectMessage(
        project_id=uuid.uuid4(),
        from_type="system",
        message_type="NOTE",
        status="open",
        content="Test",
        context={},
        delivered_at=stamp,
    )
    result = project_message_base(msg)
    assert "delivered_at" in result
    assert result["delivered_at"] is not None
    assert isinstance(result["delivered_at"], str), (
        f"delivered_at should be an ISO string, got {type(result['delivered_at'])}"
    )
    assert "2024" in result["delivered_at"], (
        f"delivered_at ISO string should contain the year: {result['delivered_at']!r}"
    )
