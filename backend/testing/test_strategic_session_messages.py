"""Tests for the inbound strategic-message poll endpoint
(app.routers.agent_strategic_messages).

The strategic agent polls
``GET /api/agent/projects/{project_id}/messages`` with a ``since`` cursor to
drain new operator messages. These cover: the exact response shape, ascending
order, the ``since`` cursor, the ``from_type`` / ``status`` filters, the limit
cap, cross-project 404, invalid-cursor 400, and server_time round-tripping as
the next cursor.
"""

from datetime import datetime, timedelta, timezone
import uuid

import pytest
from httpx import AsyncClient
from starlette.requests import Request

from app.auth import create_agent_token
from app.conversation_coordinator import renew_conversation_claim
from app.main import app
from app.models import (
    MessageHandlingState,
    MessageSurface,
    MessageType,
    Project,
    ProjectMessage,
    Task,
    TaskStatus,
)
from app.routers.agent_strategic_messages import (
    _RESIDENT_POLL_EXPECTED_PER_MINUTE,
    _RESIDENT_POLL_RATE_LIMIT_PER_MINUTE,
    _RESIDENT_POLL_REPLACEMENT_OVERLAP,
    _resident_poll_rate_limit_key,
    agent_poll_messages,
    router as _messages_router,
)
from app.shared_state import limiter

# The inbound-poll router is intentionally NOT wired into app.main here — that
# registration line is reported to the operator, not committed by the plumbing
# builder (see task instructions / CLAUDE.md: do not edit main.py). Mount it onto
# the app the `client` fixture serves so the real route + auth + rate-limit are
# exercised end-to-end. Guarded so a re-import can't double-register the path.
_POLL_PATH = "/api/agent/projects/{project_id}/messages"
if not any(getattr(r, "path", None) == _POLL_PATH for r in app.routes):
    app.include_router(_messages_router)


async def _make_project_task(db_session, *, vm="agent-vm-msgpoll"):
    project = Project(name="Msg Poll", user_id="test-user-msgpoll")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = Task(project_id=project.id, payloads=[{}], vm_instance_id=vm)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return project, task


@pytest.mark.asyncio
async def test_resident_worker_can_fence_and_release_call_after_operator_disconnect(
    client: AsyncClient, db_session
):
    project, task = await _make_project_task(db_session)
    task.context = {"_resident_session": True}
    project.vision = {
        "_resident_strategic_session": {
            "status": "active",
            "call": {
                "call_id": "call-empty-room",
                "generation": 3,
                "status": "active",
                "worker_task_id": str(task.id),
                "actor_user_id": "test-user-msgpoll",
                "input_revision": 1,
                "expires_at": (
                    datetime.now(timezone.utc) + timedelta(minutes=5)
                ).isoformat(),
            },
        }
    }
    db_session.add(task)
    db_session.add(project)
    await db_session.commit()

    stale = await client.post(
        f"/api/agent/projects/{project.id}/strategic-call/end",
        headers=_headers(task),
        json={
            "task_id": str(task.id),
            "call_id": "call-empty-room",
            "call_generation": 2,
            "reason": "operator_disconnected",
        },
    )
    assert stale.status_code == 409

    ended = await client.post(
        f"/api/agent/projects/{project.id}/strategic-call/end",
        headers=_headers(task),
        json={
            "task_id": str(task.id),
            "call_id": "call-empty-room",
            "call_generation": 3,
            "reason": "operator_disconnected",
        },
    )
    assert ended.status_code == 200, ended.text
    assert ended.json()["status"] == "ended"
    assert ended.json()["call"]["status"] == "expired"
    assert ended.json()["call"]["failure"] == "operator_disconnected"


async def _add_message(db_session, project, *, created_at=None, **kwargs):
    msg = ProjectMessage(project_id=project.id, created_at=created_at, **kwargs)
    db_session.add(msg)
    await db_session.commit()
    await db_session.refresh(msg)
    return msg


def _headers(task, vm="agent-vm-msgpoll"):
    return {"x-agent-token": create_agent_token(str(task.id), vm)}


# Fixed base clock (naive UTC — the column stores naive-UTC values).
_BASE = datetime(2026, 7, 2, 12, 0, 0)


def _rate_limit_request(
    project_id: uuid.UUID,
    *,
    remote: str = "203.0.113.10",
    token: str = "token-a",
) -> Request:
    path = f"/api/agent/projects/{project_id}/messages"
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "https",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [(b"x-agent-token", token.encode())],
            "client": (remote, 4242),
            "server": ("blueprint.test", 443),
            "path_params": {"project_id": str(project_id)},
        }
    )


def test_resident_poll_route_limit_supports_cadence_and_project_isolation():
    endpoint_key = (
        f"{agent_poll_messages.__module__}.{agent_poll_messages.__name__}"
    )
    registered = [
        route_limit
        for route_limit in limiter._route_limits[endpoint_key]
        if route_limit.key_func is _resident_poll_rate_limit_key
    ]
    assert len(registered) == 1
    assert registered[0].limit.amount == _RESIDENT_POLL_RATE_LIMIT_PER_MINUTE
    assert registered[0].limit.get_expiry() == 60

    replacement_capacity = (
        _RESIDENT_POLL_EXPECTED_PER_MINUTE
        * _RESIDENT_POLL_REPLACEMENT_OVERLAP
    )
    assert _RESIDENT_POLL_RATE_LIMIT_PER_MINUTE >= replacement_capacity
    assert (
        _RESIDENT_POLL_RATE_LIMIT_PER_MINUTE - replacement_capacity
        >= _RESIDENT_POLL_EXPECTED_PER_MINUTE
    )

    project_a = uuid.uuid4()
    project_b = uuid.uuid4()
    key_a = _resident_poll_rate_limit_key(_rate_limit_request(project_a))
    key_b = _resident_poll_rate_limit_key(_rate_limit_request(project_b))
    assert key_a != key_b
    assert key_a != _resident_poll_rate_limit_key(
        _rate_limit_request(project_a, remote="203.0.113.11")
    )
    assert key_a == _resident_poll_rate_limit_key(
        _rate_limit_request(project_a, token="rotated-token")
    )


@pytest.mark.asyncio
async def test_poll_returns_open_user_messages_ascending_with_exact_shape(
    client: AsyncClient, db_session
):
    project, task = await _make_project_task(db_session)
    # Insert out of chronological order to prove the endpoint sorts, not the insert.
    m_mid = await _add_message(
        db_session,
        project,
        from_type="user",
        status="open",
        message_type=MessageType.NOTE,
        content="second",
        context={"k": "v"},
        created_at=_BASE + timedelta(minutes=1),
    )
    m_old = await _add_message(
        db_session,
        project,
        from_type="user",
        status="open",
        message_type=MessageType.QUESTION,
        content="first",
        created_at=_BASE,
    )
    m_new = await _add_message(
        db_session,
        project,
        from_type="user",
        status="open",
        message_type=MessageType.NOTE,
        content="third",
        created_at=_BASE + timedelta(minutes=2),
    )

    res = await client.get(
        f"/api/agent/projects/{project.id}/messages",
        headers=_headers(task),
    )
    assert res.status_code == 200, res.text
    body = res.json()
    ids = [m["id"] for m in body["messages"]]
    assert ids == [str(m_old.id), str(m_mid.id), str(m_new.id)]

    # Exact contract shape: the seven fixed keys, nothing else (no project_id,
    # from_task_id, resolution, ...).
    sample = next(m for m in body["messages"] if m["id"] == str(m_mid.id))
    assert set(sample.keys()) == {
        "id",
        "created_at",
        "from_type",
        "status",
        "message_type",
        "content",
        "context",
    }
    assert sample["from_type"] == "user"
    assert sample["status"] == "open"
    assert sample["message_type"] == "NOTE"
    assert sample["content"] == "second"
    assert sample["context"] == {"k": "v"}

    # server_time is an ISO-8601 UTC timestamp.
    parsed = datetime.fromisoformat(body["server_time"])
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)


@pytest.mark.asyncio
async def test_poll_since_cursor_returns_only_newer(
    client: AsyncClient, db_session
):
    project, task = await _make_project_task(db_session)
    await _add_message(
        db_session, project, from_type="user", status="open",
        content="old", created_at=_BASE,
    )
    newer = await _add_message(
        db_session, project, from_type="user", status="open",
        content="new", created_at=_BASE + timedelta(minutes=5),
    )
    # Cursor between the two → only the newer one comes back.
    since = (_BASE + timedelta(minutes=1)).isoformat()
    res = await client.get(
        f"/api/agent/projects/{project.id}/messages",
        params={"since": since},
        headers=_headers(task),
    )
    assert res.status_code == 200, res.text
    ids = [m["id"] for m in res.json()["messages"]]
    assert ids == [str(newer.id)]


@pytest.mark.asyncio
async def test_poll_since_accepts_tz_aware_cursor(
    client: AsyncClient, db_session
):
    """A tz-aware (Z-suffixed) cursor is normalized to UTC before comparison."""
    project, task = await _make_project_task(db_session)
    await _add_message(
        db_session, project, from_type="user", status="open",
        content="old", created_at=_BASE,
    )
    newer = await _add_message(
        db_session, project, from_type="user", status="open",
        content="new", created_at=_BASE + timedelta(minutes=5),
    )
    since = (_BASE + timedelta(minutes=1)).replace(tzinfo=timezone.utc).isoformat()
    assert since.endswith("+00:00")
    res = await client.get(
        f"/api/agent/projects/{project.id}/messages",
        params={"since": since},
        headers=_headers(task),
    )
    assert res.status_code == 200, res.text
    ids = [m["id"] for m in res.json()["messages"]]
    assert ids == [str(newer.id)]


@pytest.mark.asyncio
async def test_call_inputs_filter_before_limit_and_include_sequence(
    client: AsyncClient, db_session
):
    """Ordinary chat cannot crowd a canonical call input out of a 20-row page."""
    project, task = await _make_project_task(db_session)
    call_id = "call-limit-fence"
    call_generation = 7
    project.vision = {
        "_resident_strategic_session": {
            "session_id": "call-input-session",
            "task_id": str(task.id),
            "worker_generation": 1,
            "status": "active",
            "call": {
                "call_id": call_id,
                "generation": call_generation,
                "status": "active",
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
                "worker_task_id": str(task.id),
                "worker_generation": 1,
            }
        }
    }
    task.context = {"_resident_session": True}
    db_session.add_all([project, task])
    await db_session.commit()

    # These are all newer than the canonical input.  A filter after LIMIT would
    # return this page and then discard every row, starving the call forever.
    for sequence in range(1, 22):
        await _add_message(
            db_session,
            project,
            from_type="user",
            status="open",
            surface=MessageSurface.CONVERSATION,
            sequence=sequence,
            content=f"ordinary-{sequence}",
            context={"call_id": "other-call", "call_generation": 1},
        )
    canonical = await _add_message(
        db_session,
        project,
        from_type="user",
        status="open",
        surface=MessageSurface.CONVERSATION,
        sequence=22,
        content="canonical input",
        context={"call_id": call_id, "call_generation": call_generation},
    )

    res = await client.get(
        f"/api/agent/projects/{project.id}/strategic-call/inputs",
        params={"call_id": call_id, "call_generation": call_generation},
        headers=_headers(task),
    )
    assert res.status_code == 200, res.text
    messages = res.json()["messages"]
    assert [message["id"] for message in messages] == [str(canonical.id)]
    assert messages[0]["sequence"] == 22
    assert isinstance(messages[0]["sequence"], int)


@pytest.mark.asyncio
async def test_poll_filters_from_type_and_status(
    client: AsyncClient, db_session
):
    project, task = await _make_project_task(db_session)
    user_open = await _add_message(
        db_session, project, from_type="user", status="open",
        content="user-open", created_at=_BASE,
    )
    await _add_message(
        db_session, project, from_type="agent", status="open",
        content="agent-open", created_at=_BASE + timedelta(minutes=1),
    )
    resolved = await _add_message(
        db_session, project, from_type="user", status="resolved",
        content="user-resolved", created_at=_BASE + timedelta(minutes=2),
    )

    # Defaults: from_type=user, status=open → only the user/open message.
    res = await client.get(
        f"/api/agent/projects/{project.id}/messages",
        headers=_headers(task),
    )
    assert res.status_code == 200, res.text
    ids = [m["id"] for m in res.json()["messages"]]
    assert ids == [str(user_open.id)]

    # Explicit override reaches the resolved user message.
    res2 = await client.get(
        f"/api/agent/projects/{project.id}/messages",
        params={"status": "resolved"},
        headers=_headers(task),
    )
    assert res2.status_code == 200, res2.text
    ids2 = [m["id"] for m in res2.json()["messages"]]
    assert ids2 == [str(resolved.id)]


@pytest.mark.asyncio
async def test_poll_limit_caps_and_keeps_oldest_first(
    client: AsyncClient, db_session
):
    project, task = await _make_project_task(db_session)
    created = []
    for i in range(5):
        m = await _add_message(
            db_session, project, from_type="user", status="open",
            content=f"m{i}", created_at=_BASE + timedelta(minutes=i),
        )
        created.append(m)

    res = await client.get(
        f"/api/agent/projects/{project.id}/messages",
        params={"limit": 2},
        headers=_headers(task),
    )
    assert res.status_code == 200, res.text
    ids = [m["id"] for m in res.json()["messages"]]
    # Ascending order + limit → the two OLDEST, so the cursor advances forward
    # and nothing is skipped across successive polls.
    assert ids == [str(created[0].id), str(created[1].id)]


@pytest.mark.asyncio
async def test_poll_limit_out_of_range_rejected(client: AsyncClient, db_session):
    project, task = await _make_project_task(db_session)
    res = await client.get(
        f"/api/agent/projects/{project.id}/messages",
        params={"limit": 0},
        headers=_headers(task),
    )
    assert res.status_code == 422
    res_big = await client.get(
        f"/api/agent/projects/{project.id}/messages",
        params={"limit": 10_000},
        headers=_headers(task),
    )
    assert res_big.status_code == 422


@pytest.mark.asyncio
async def test_poll_invalid_since_returns_400(client: AsyncClient, db_session):
    project, task = await _make_project_task(db_session)
    res = await client.get(
        f"/api/agent/projects/{project.id}/messages",
        params={"since": "not-a-timestamp"},
        headers=_headers(task),
    )
    assert res.status_code == 400, res.text


@pytest.mark.asyncio
async def test_poll_rejects_cross_project(client: AsyncClient, db_session):
    """A token scoped to project A cannot poll project B (404, not 403)."""
    _project_a, task_a = await _make_project_task(db_session, vm="agent-vm-a")
    project_b = Project(name="B", user_id="test-user-b")
    db_session.add(project_b)
    await db_session.commit()
    await db_session.refresh(project_b)
    await _add_message(
        db_session, project_b, from_type="user", status="open",
        content="secret", created_at=_BASE,
    )

    res = await client.get(
        f"/api/agent/projects/{project_b.id}/messages",
        headers={"x-agent-token": create_agent_token(str(task_a.id), "agent-vm-a")},
    )
    assert res.status_code == 404, res.text


@pytest.mark.asyncio
async def test_poll_requires_agent_token(client: AsyncClient, db_session):
    project, _ = await _make_project_task(db_session)
    res = await client.get(f"/api/agent/projects/{project.id}/messages")
    # No token → rejected. The x-agent-token header is a required dependency, so
    # FastAPI answers a missing header with 422 before the handler runs (an
    # invalid token would be 401/403). Any of these is a hard rejection, never 200.
    assert res.status_code in (401, 403, 422), res.text


@pytest.mark.asyncio
async def test_poll_server_time_round_trips_as_next_cursor(
    client: AsyncClient, db_session
):
    """Feeding server_time back as `since` drains the thread (nothing re-served)."""
    project, task = await _make_project_task(db_session)
    await _add_message(
        db_session, project, from_type="user", status="open",
        content="only", created_at=_BASE,
    )
    first = await client.get(
        f"/api/agent/projects/{project.id}/messages",
        headers=_headers(task),
    )
    assert first.status_code == 200, first.text
    assert len(first.json()["messages"]) == 1
    cursor = first.json()["server_time"]

    second = await client.get(
        f"/api/agent/projects/{project.id}/messages",
        params={"since": cursor},
        headers=_headers(task),
    )
    assert second.status_code == 200, second.text
    assert second.json()["messages"] == []


@pytest.mark.asyncio
async def test_poll_since_is_inclusive_of_boundary(
    client: AsyncClient, db_session
):
    """The cursor filter is INCLUSIVE (created_at >= since): a message sitting
    exactly on the cursor is re-delivered (the agent dedupes by id) rather than
    skipped — this is what closes the >limit-truncation and commit-race skips a
    strict `>` opens."""
    project, task = await _make_project_task(db_session)
    boundary = await _add_message(
        db_session, project, from_type="user", status="open",
        content="on-the-boundary", created_at=_BASE + timedelta(minutes=1),
    )
    # Cursor == the message's own created_at.
    since = (_BASE + timedelta(minutes=1)).isoformat()
    res = await client.get(
        f"/api/agent/projects/{project.id}/messages",
        params={"since": since},
        headers=_headers(task),
    )
    assert res.status_code == 200, res.text
    ids = [m["id"] for m in res.json()["messages"]]
    # Inclusive: the boundary message is returned, not skipped.
    assert ids == [str(boundary.id)]


@pytest.mark.asyncio
async def test_poll_max_created_at_cursor_redelivers_boundary(
    client: AsyncClient, db_session
):
    """Advancing the cursor to the MAX returned created_at (the agent's cursor
    rule) and re-polling re-delivers that boundary message under the inclusive
    filter — proving the un-returned tail at that instant is reachable, not
    skipped. The agent dedupes the already-seen id."""
    project, task = await _make_project_task(db_session)
    older = await _add_message(
        db_session, project, from_type="user", status="open",
        content="older", created_at=_BASE,
    )
    newest = await _add_message(
        db_session, project, from_type="user", status="open",
        content="newest", created_at=_BASE + timedelta(minutes=2),
    )
    first = await client.get(
        f"/api/agent/projects/{project.id}/messages",
        headers=_headers(task),
    )
    assert first.status_code == 200, first.text
    msgs = first.json()["messages"]
    assert [m["id"] for m in msgs] == [str(older.id), str(newest.id)]
    max_created_at = msgs[-1]["created_at"]

    second = await client.get(
        f"/api/agent/projects/{project.id}/messages",
        params={"since": max_created_at},
        headers=_headers(task),
    )
    assert second.status_code == 200, second.text
    # Inclusive re-scan of the boundary instant returns the newest again.
    assert [m["id"] for m in second.json()["messages"]] == [str(newest.id)]


@pytest.mark.asyncio
async def test_poll_session_id_scopes_to_session_plus_legacy_rows(
    client: AsyncClient, db_session
):
    """Session A sees its rows and legacy NULL rows, never session B's rows."""
    project, task = await _make_project_task(db_session)
    own = await _add_message(
        db_session, project, from_type="user", status="open",
        message_type=MessageType.NOTE, content="session A",
        resident_session_id="session-a",
        created_at=_BASE,
    )
    legacy = await _add_message(
        db_session, project, from_type="user", status="open",
        message_type=MessageType.NOTE, content="legacy",
        created_at=_BASE + timedelta(minutes=1),
    )
    other = await _add_message(
        db_session, project, from_type="user", status="open",
        message_type=MessageType.NOTE, content="session B",
        resident_session_id="session-b",
        created_at=_BASE + timedelta(minutes=2),
    )
    res = await client.get(
        f"/api/agent/projects/{project.id}/messages",
        params={"session_id": "session-a"},
        headers=_headers(task),
    )
    assert res.status_code == 200, res.text
    ids = [m["id"] for m in res.json()["messages"]]
    assert ids == [str(own.id), str(legacy.id)]
    assert str(other.id) not in ids


async def _make_resident_poll_worker(
    db_session, *, generation: int = 2, session_id: str = "resident-session"
):
    project, task = await _make_project_task(db_session, vm="agent-vm-resident-poll")
    task.context = {
        "_resident_session": True,
        "strategic_session_id": session_id,
        "_resident_worker_generation": generation,
    }
    task.status = TaskStatus.IN_PROGRESS
    project.vision = {
        "_resident_strategic_session": {
            "session_id": session_id,
            "task_id": str(task.id),
            "worker_generation": generation,
            "status": "active",
        }
    }
    db_session.add_all([project, task])
    await db_session.commit()
    return project, task


@pytest.mark.asyncio
async def test_resident_poll_uses_authenticated_session_scope(
    client: AsyncClient, db_session
):
    project, task = await _make_resident_poll_worker(db_session)
    own = await _add_message(
        db_session, project, from_type="user", status="open",
        message_type=MessageType.NOTE, content="current session",
        resident_session_id="resident-session", created_at=_BASE,
    )
    await _add_message(
        db_session, project, from_type="user", status="open",
        message_type=MessageType.NOTE, content="wrong session",
        resident_session_id="other-session",
        created_at=_BASE + timedelta(minutes=1),
    )

    response = await client.get(
        f"/api/agent/projects/{project.id}/messages",
        headers=_headers(task, vm="agent-vm-resident-poll"),
    )
    assert response.status_code == 200, response.text
    assert [row["id"] for row in response.json()["messages"]] == [str(own.id)]
    assert response.json()["claim_required"] is True


@pytest.mark.asyncio
async def test_resident_claim_is_durable_and_idempotent_for_same_generation(
    client: AsyncClient, db_session
):
    project, task = await _make_resident_poll_worker(db_session)
    first = await _add_message(
        db_session,
        project,
        from_type="user",
        status="open",
        content="first claimed input",
        resident_session_id="resident-session",
        created_at=_BASE,
    )
    second = await _add_message(
        db_session,
        project,
        from_type="user",
        status="open",
        content="second claimed input",
        resident_session_id="resident-session",
        created_at=_BASE + timedelta(seconds=1),
    )
    payload = {
        "task_id": str(task.id),
        "session_id": "resident-session",
        "worker_generation": 2,
        "incarnation_id": "00000000-0000-0000-0000-000000000111",
        "message_ids": [str(first.id), str(second.id)],
        "lease_seconds": 45,
    }

    claimed = await client.post(
        f"/api/agent/projects/{project.id}/messages/claim",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json=payload,
    )
    assert claimed.status_code == 200, claimed.text
    body = claimed.json()
    assert body["message_ids"] == [str(first.id), str(second.id)]
    assert body["attempt"] == 1

    repeated = await client.post(
        f"/api/agent/projects/{project.id}/messages/claim",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json=payload,
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["claim_id"] == body["claim_id"]
    assert repeated.json()["attempt"] == 1

    await db_session.refresh(first)
    await db_session.refresh(second)
    for row in (first, second):
        assert row.handling_state == MessageHandlingState.CLAIMED
        assert str(row.conversation_claim_id) == body["claim_id"]
        assert row.handled_by_task_id == task.id
        assert row.conversation_claim_generation == 2
        assert str(row.conversation_claim_incarnation) == payload["incarnation_id"]
        assert row.conversation_claim_attempt == 1
        assert row.conversation_claim_expires_at is not None
    assert not await renew_conversation_claim(
        db_session,
        project_id=project.id,
        claim_id=uuid.UUID(body["claim_id"]),
        task_id=task.id,
        worker_generation=2,
        incarnation_id=uuid.UUID(
            "00000000-0000-0000-0000-000000000999"
        ),
        lease_seconds=90,
    )
    assert await renew_conversation_claim(
        db_session,
        project_id=project.id,
        claim_id=uuid.UUID(body["claim_id"]),
        task_id=task.id,
        worker_generation=2,
        incarnation_id=uuid.UUID(payload["incarnation_id"]),
        lease_seconds=90,
    )


@pytest.mark.asyncio
async def test_live_claim_rejects_different_process_incarnation(
    client: AsyncClient, db_session
):
    project, task = await _make_resident_poll_worker(db_session)
    message = await _add_message(
        db_session,
        project,
        from_type="user",
        status="open",
        content="single-owner input",
        resident_session_id="resident-session",
        created_at=_BASE,
    )
    payload = {
        "task_id": str(task.id),
        "session_id": "resident-session",
        "worker_generation": 2,
        "incarnation_id": "00000000-0000-0000-0000-000000000333",
        "message_ids": [str(message.id)],
    }
    first = await client.post(
        f"/api/agent/projects/{project.id}/messages/claim",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json=payload,
    )
    assert first.status_code == 200, first.text

    overlapping = await client.post(
        f"/api/agent/projects/{project.id}/messages/claim",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            **payload,
            "incarnation_id": "00000000-0000-0000-0000-000000000444",
        },
    )
    assert overlapping.status_code == 409


@pytest.mark.asyncio
async def test_resident_process_incarnation_fences_different_message_batches(
    client: AsyncClient, db_session
):
    project, task = await _make_resident_poll_worker(db_session)
    first = await _add_message(
        db_session,
        project,
        from_type="user",
        status="open",
        content="first process batch",
        resident_session_id="resident-session",
        created_at=_BASE,
    )
    second = await _add_message(
        db_session,
        project,
        from_type="user",
        status="open",
        content="second process batch",
        resident_session_id="resident-session",
        created_at=_BASE + timedelta(seconds=1),
    )
    first_incarnation = "00000000-0000-0000-0000-000000000611"
    second_incarnation = "00000000-0000-0000-0000-000000000622"

    admitted = await client.post(
        f"/api/agent/projects/{project.id}/messages/claim",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            "task_id": str(task.id),
            "session_id": "resident-session",
            "worker_generation": 2,
            "resident_incarnation_id": first_incarnation,
            "incarnation_id": first_incarnation,
            "message_ids": [str(first.id)],
        },
    )
    assert admitted.status_code == 200, admitted.text
    assert admitted.json()["resident_incarnation_id"] == first_incarnation

    competing = await client.post(
        f"/api/agent/projects/{project.id}/messages/claim",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            "task_id": str(task.id),
            "session_id": "resident-session",
            "worker_generation": 2,
            "resident_incarnation_id": second_incarnation,
            "incarnation_id": second_incarnation,
            "message_ids": [str(second.id)],
        },
    )
    assert competing.status_code == 409
    await db_session.refresh(second)
    assert second.handling_state == MessageHandlingState.PENDING
    assert second.conversation_claim_id is None


@pytest.mark.asyncio
async def test_message_claim_rejects_call_activated_after_poll(
    client: AsyncClient, db_session
):
    project, task = await _make_resident_poll_worker(db_session)
    message = await _add_message(
        db_session,
        project,
        from_type="user",
        status="open",
        content="must wait for Fast Resident",
        resident_session_id="resident-session",
        created_at=_BASE,
    )
    resident = dict(project.vision["_resident_strategic_session"])
    resident["call"] = {
        "call_id": "call-activated-during-preflight",
        "generation": 1,
        "status": "active",
        "expires_at": (
            datetime.now(timezone.utc) + timedelta(minutes=5)
        ).isoformat(),
        "worker_task_id": str(task.id),
        "worker_generation": 2,
    }
    project.vision = {"_resident_strategic_session": resident}
    db_session.add(project)
    await db_session.commit()

    response = await client.post(
        f"/api/agent/projects/{project.id}/messages/claim",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            "task_id": str(task.id),
            "session_id": "resident-session",
            "worker_generation": 2,
            "resident_incarnation_id":
                "00000000-0000-0000-0000-000000000633",
            "message_ids": [str(message.id)],
        },
    )
    assert response.status_code == 409
    assert "Fast Resident" in response.text
    await db_session.refresh(message)
    assert message.handling_state == MessageHandlingState.PENDING


@pytest.mark.asyncio
async def test_legacy_resident_claim_gets_stable_rollout_incarnation(
    client: AsyncClient, db_session
):
    project, task = await _make_resident_poll_worker(db_session)
    message = await _add_message(
        db_session,
        project,
        from_type="user",
        status="open",
        content="rolling deploy input",
        resident_session_id="resident-session",
        created_at=_BASE,
    )
    payload = {
        "task_id": str(task.id),
        "session_id": "resident-session",
        "worker_generation": 2,
        "message_ids": [str(message.id)],
    }

    first = await client.post(
        f"/api/agent/projects/{project.id}/messages/claim",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json=payload,
    )
    repeated = await client.post(
        f"/api/agent/projects/{project.id}/messages/claim",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json=payload,
    )

    assert first.status_code == 200, first.text
    assert first.json()["incarnation_supported"] is True
    assert first.json()["incarnation_id"]
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["claim_id"] == first.json()["claim_id"]
    await db_session.refresh(message)
    assert message.conversation_claim_incarnation is not None


@pytest.mark.asyncio
async def test_terminal_resident_task_cannot_claim_operator_batch(
    client: AsyncClient, db_session
):
    project, task = await _make_resident_poll_worker(db_session)
    message = await _add_message(
        db_session,
        project,
        from_type="user",
        status="open",
        content="must remain pending",
        resident_session_id="resident-session",
        created_at=_BASE,
    )
    task.status = TaskStatus.CANCELLED
    db_session.add(task)
    await db_session.commit()

    response = await client.post(
        f"/api/agent/projects/{project.id}/messages/claim",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            "task_id": str(task.id),
            "session_id": "resident-session",
            "worker_generation": 2,
            "incarnation_id": "00000000-0000-0000-0000-000000000555",
            "message_ids": [str(message.id)],
        },
    )
    # Token verification rejects terminal tasks before the route's own
    # under-lock status fence; both layers fail closed.
    assert response.status_code == 401
    await db_session.refresh(message)
    assert message.handling_state == MessageHandlingState.PENDING


@pytest.mark.asyncio
async def test_stale_resident_generation_cannot_claim_operator_batch(
    client: AsyncClient, db_session
):
    project, stale_task = await _make_resident_poll_worker(db_session, generation=1)
    message = await _add_message(
        db_session,
        project,
        from_type="user",
        status="open",
        content="generation-fenced input",
        resident_session_id="resident-session",
        created_at=_BASE,
    )
    current_task = Task(
        project_id=project.id,
        payloads=[{}],
        vm_instance_id="agent-vm-current-resident",
        context={
            "_resident_session": True,
            "strategic_session_id": "resident-session",
            "_resident_worker_generation": 2,
        },
    )
    db_session.add(current_task)
    await db_session.commit()
    await db_session.refresh(current_task)
    project.vision = {
        "_resident_strategic_session": {
            "session_id": "resident-session",
            "task_id": str(current_task.id),
            "worker_generation": 2,
            "status": "active",
        }
    }
    db_session.add(project)
    await db_session.commit()

    response = await client.post(
        f"/api/agent/projects/{project.id}/messages/claim",
        headers=_headers(stale_task, vm="agent-vm-resident-poll"),
        json={
            "task_id": str(stale_task.id),
            "session_id": "resident-session",
            "worker_generation": 1,
            "incarnation_id": "00000000-0000-0000-0000-000000000222",
            "message_ids": [str(message.id)],
        },
    )
    assert response.status_code == 409
    await db_session.refresh(message)
    assert message.handling_state == MessageHandlingState.PENDING
    assert message.conversation_claim_id is None


@pytest.mark.asyncio
async def test_resident_final_fence_orders_equal_timestamps_deterministically(
    client: AsyncClient, db_session
):
    project, task = await _make_resident_poll_worker(db_session)
    equal_time = _BASE
    seq_four = await _add_message(
        db_session,
        project,
        id=uuid.UUID("00000000-0000-0000-0000-000000000040"),
        from_type="user",
        status="open",
        content="sequence four inserted first",
        sequence=4,
        created_at=equal_time,
    )
    seq_two = await _add_message(
        db_session,
        project,
        id=uuid.UUID("00000000-0000-0000-0000-000000000020"),
        from_type="user",
        status="open",
        content="sequence two inserted second",
        sequence=2,
        created_at=equal_time,
    )
    seq_one = await _add_message(
        db_session,
        project,
        id=uuid.UUID("00000000-0000-0000-0000-000000000010"),
        from_type="user",
        status="open",
        content="sequence one inserted third",
        sequence=1,
        created_at=equal_time,
    )
    seq_three = await _add_message(
        db_session,
        project,
        id=uuid.UUID("00000000-0000-0000-0000-000000000030"),
        from_type="user",
        status="open",
        content="sequence three inserted last",
        sequence=3,
        created_at=equal_time,
    )

    response = await client.get(
        f"/api/agent/projects/{project.id}/messages",
        headers=_headers(task, vm="agent-vm-resident-poll"),
    )

    assert response.status_code == 200, response.text
    assert [row["id"] for row in response.json()["messages"]] == [
        str(seq_one.id),
        str(seq_two.id),
        str(seq_three.id),
        str(seq_four.id),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {"session_id": "spoofed-session", "worker_generation": 2},
        {"session_id": "resident-session", "worker_generation": 3},
    ],
)
async def test_resident_poll_rejects_query_identity_spoof(
    client: AsyncClient, db_session, params
):
    project, task = await _make_resident_poll_worker(db_session)
    response = await client.get(
        f"/api/agent/projects/{project.id}/messages",
        params=params,
        headers=_headers(task, vm="agent-vm-resident-poll"),
    )
    assert response.status_code == 409
    assert "identity mismatch" in response.text


@pytest.mark.asyncio
async def test_stale_resident_poll_is_rejected_without_generation_query(
    client: AsyncClient, db_session
):
    project, stale_task = await _make_resident_poll_worker(db_session, generation=1)
    current_task = Task(
        project_id=project.id,
        payloads=[{}],
        vm_instance_id="agent-vm-current-resident",
        context={
            "_resident_session": True,
            "strategic_session_id": "resident-session",
            "_resident_worker_generation": 2,
        },
    )
    db_session.add(current_task)
    await db_session.commit()
    await db_session.refresh(current_task)
    project.vision = {
        "_resident_strategic_session": {
            "session_id": "resident-session",
            "task_id": str(current_task.id),
            "worker_generation": 2,
            "status": "active",
        }
    }
    db_session.add(project)
    await db_session.commit()

    response = await client.get(
        f"/api/agent/projects/{project.id}/messages",
        headers=_headers(stale_task, vm="agent-vm-resident-poll"),
    )
    assert response.status_code == 409
    assert "superseded" in response.text
