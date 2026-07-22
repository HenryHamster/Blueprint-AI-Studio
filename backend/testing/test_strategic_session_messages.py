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

import pytest
from httpx import AsyncClient

from app.auth import create_agent_token
from app.main import app
from app.models import MessageType, Project, ProjectMessage, Task
from app.routers.agent_strategic_messages import router as _messages_router

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
async def test_poll_session_id_is_accepted_but_no_longer_scopes(
    client: AsyncClient, db_session
):
    """``session_id`` is retained on the poll endpoint for backward compatibility
    with older agents, but per-session scoping has been retired: the poll now
    returns the project's messages project-wide whether or not a ``session_id`` is
    supplied. This guards against a regression where passing ``session_id`` would
    hide messages (the old per-session scoping behaviour, since retired)."""
    project, task = await _make_project_task(db_session)

    msg_1 = await _add_message(
        db_session, project, from_type="user", status="open",
        message_type=MessageType.NOTE, content="first msg",
        created_at=_BASE,
    )
    msg_2 = await _add_message(
        db_session, project, from_type="user", status="open",
        message_type=MessageType.NOTE, content="second msg",
        created_at=_BASE + timedelta(minutes=1),
    )
    await db_session.commit()

    # Poll WITH an arbitrary session_id — all project messages are still returned
    # (session_id no longer filters anything).
    res = await client.get(
        f"/api/agent/projects/{project.id}/messages",
        params={"session_id": "any-session-token"},
        headers=_headers(task),
    )
    assert res.status_code == 200, res.text
    ids = [m["id"] for m in res.json()["messages"]]
    assert str(msg_1.id) in ids
    assert str(msg_2.id) in ids

    # Poll WITHOUT session_id — identical project-wide result.
    res_wide = await client.get(
        f"/api/agent/projects/{project.id}/messages",
        headers=_headers(task),
    )
    assert res_wide.status_code == 200, res_wide.text
    ids_wide = [m["id"] for m in res_wide.json()["messages"]]
    assert ids_wide == ids
