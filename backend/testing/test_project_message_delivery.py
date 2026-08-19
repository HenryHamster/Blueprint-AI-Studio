"""Regression tests for ProjectMessage.delivered_at stamping on both delivery paths.

Covers:
1. WS ack stamps delivered_at end-to-end (real uvicorn server, real DB).
2. Idempotency: second ack does NOT change the already-set timestamp.
3. Cross-project isolation: unknown/cross-project message_id is a silent no-op.
4. project_message_base projection surfaces delivered_at correctly.
5. REST GET /strategic-conversation stamps delivered_at on strategist/system replies.
6. REST GET /strategic-conversation does NOT stamp user messages.
7. REST GET /strategic-session (via _build_strategic_session_response) stamps delivered_at.
8. REST stamp idempotency: repeated GET does not overwrite an existing delivered_at.

Tests 1–4 use AsyncSessionLocal (patched to the test engine by conftest) directly
rather than the db_session fixture, to avoid triggering the pre-existing
projecttelemetry teardown error (the table is named project_telemetry in migrations
but the model derives projecttelemetry, so conftest's cleanup DELETEs from the wrong
name). Tests 5–8 use the client/db_session conftest fixtures.
"""

import asyncio
import json
import socket
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
import uvicorn
import websockets
from fastapi import FastAPI
from sqlalchemy import select

from app.auth import create_ws_ticket
from app.database import AsyncSessionLocal
from app.models import MessageSurface, MessageType, Project, ProjectMember, ProjectMessage
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


# ---------------------------------------------------------------------------
# Tests 5–8: REST conversation poll stamps delivered_at
# ---------------------------------------------------------------------------
# These tests exercise both REST paths that load the conversation transcript:
# - GET /strategic-conversation (the lightweight poll endpoint)
# - GET /strategic-session     (the full session endpoint, via
#                               _build_strategic_session_response)
# Both must stamp delivered_at on strategist/system messages so that the
# durable delivery record reflects operator reads regardless of transport.
# ---------------------------------------------------------------------------

_REST_TEST_USER = "test-user-123"
_REST_BASE_URL = "/api/admin/projects"


async def _make_rest_project(db_session) -> Project:
    project = Project(name="delivery-rest-test", user_id=_REST_TEST_USER)
    db_session.add(project)
    await db_session.flush()
    member = ProjectMember(
        project_id=project.id, user_id=_REST_TEST_USER, role="owner"
    )
    db_session.add(member)
    await db_session.commit()
    await db_session.refresh(project)
    return project


async def _add_conversation_message(
    db_session,
    project_id: uuid.UUID,
    *,
    from_type: str,
    sequence: int,
    delivered_at=None,
) -> ProjectMessage:
    # The conversation visibility predicate is fail-closed for machine-authored
    # rows (strategic/system): they are only transcript-visible with a
    # registered operator-conversation event_type (or reply provenance). Stamp
    # a registered type so these rows model real strategist/system replies;
    # human rows (user/operator) are visible unconditionally.
    context = None
    if from_type == "strategic":
        context = {"event_type": "question_answer"}
    elif from_type == "system":
        context = {"event_type": "failure_alert"}
    msg = ProjectMessage(
        project_id=project_id,
        sequence=sequence,
        surface=MessageSurface.CONVERSATION,
        message_type=MessageType.NOTE,
        from_type=from_type,
        content=f"{from_type} message {sequence}",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        delivered_at=delivered_at,
        context=context,
    )
    db_session.add(msg)
    await db_session.commit()
    await db_session.refresh(msg)
    return msg


@pytest.mark.asyncio
async def test_rest_conversation_poll_stamps_delivered_at(client, db_session):
    """GET /strategic-conversation stamps delivered_at on strategist and system messages.

    This is the primary REST conversation poll path.  Strategist and system
    replies with delivered_at=NULL must be stamped on read; user messages must
    remain un-stamped (they are outbound, not inbound strategist replies).
    """
    project = await _make_rest_project(db_session)
    strategic_msg = await _add_conversation_message(
        db_session, project.id, from_type="strategic", sequence=1
    )
    system_msg = await _add_conversation_message(
        db_session, project.id, from_type="system", sequence=2
    )
    user_msg = await _add_conversation_message(
        db_session, project.id, from_type="user", sequence=3
    )

    resp = await client.get(f"{_REST_BASE_URL}/{project.id}/strategic-conversation")
    assert resp.status_code == 200, resp.text

    items = {item["id"]: item for item in resp.json()["items"]}

    # strategist and system replies must be stamped in the response payload
    assert items[str(strategic_msg.id)]["delivered_at"] is not None, (
        "GET /strategic-conversation must stamp delivered_at on returned strategic messages"
    )
    assert items[str(system_msg.id)]["delivered_at"] is not None, (
        "GET /strategic-conversation must stamp delivered_at on returned system messages"
    )
    # user outbound messages must NOT be stamped
    assert items[str(user_msg.id)]["delivered_at"] is None, (
        "GET /strategic-conversation must not stamp delivered_at on user messages"
    )

    # verify the stamps are durable in the DB (not just in-memory)
    await db_session.refresh(strategic_msg)
    await db_session.refresh(system_msg)
    await db_session.refresh(user_msg)
    assert strategic_msg.delivered_at is not None, "strategic_msg.delivered_at must be durable"
    assert system_msg.delivered_at is not None, "system_msg.delivered_at must be durable"
    assert user_msg.delivered_at is None, "user_msg.delivered_at must remain NULL"


@pytest.mark.asyncio
async def test_rest_session_endpoint_stamps_delivered_at(client, db_session):
    """GET /strategic-session stamps delivered_at via _build_strategic_session_response.

    The full-session endpoint loads the conversation via _build_strategic_session_response.
    Stamping must happen there too so that the initial page load (before the lighter poll
    starts) also records delivery.
    """
    project = await _make_rest_project(db_session)
    msg = await _add_conversation_message(
        db_session, project.id, from_type="strategic", sequence=1
    )

    resp = await client.get(f"{_REST_BASE_URL}/{project.id}/strategic-session")
    assert resp.status_code == 200, resp.text

    # the message must appear in the conversation window
    conversation = resp.json().get("conversation") or resp.json().get("session", {}).get(
        "conversation", {}
    )
    items = {item["id"]: item for item in (conversation.get("items") or [])}
    assert str(msg.id) in items, "strategic message must appear in strategic-session conversation"
    assert items[str(msg.id)]["delivered_at"] is not None, (
        "GET /strategic-session must stamp delivered_at on returned strategic messages"
    )

    # verify the stamp is durable in the DB
    await db_session.refresh(msg)
    assert msg.delivered_at is not None, (
        "delivered_at stamp from GET /strategic-session must be committed to the DB"
    )


@pytest.mark.asyncio
async def test_rest_conversation_stamp_is_idempotent(client, db_session):
    """Repeated REST GET calls do not overwrite an existing delivered_at timestamp."""
    project = await _make_rest_project(db_session)
    msg = await _add_conversation_message(
        db_session, project.id, from_type="strategic", sequence=1
    )

    resp1 = await client.get(f"{_REST_BASE_URL}/{project.id}/strategic-conversation")
    assert resp1.status_code == 200
    await db_session.refresh(msg)
    first_stamp = msg.delivered_at
    assert first_stamp is not None, "first GET must stamp delivered_at"

    resp2 = await client.get(f"{_REST_BASE_URL}/{project.id}/strategic-conversation")
    assert resp2.status_code == 200
    await db_session.refresh(msg)
    assert msg.delivered_at == first_stamp, (
        f"Second GET overwrote delivered_at: {first_stamp!r} → {msg.delivered_at!r}; "
        "stamp must be a one-time idempotent write"
    )
