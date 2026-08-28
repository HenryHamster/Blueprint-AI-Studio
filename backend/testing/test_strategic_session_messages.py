"""Inbound Strategic message polling and receipt-bound conversation routes."""

import base64
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select
from starlette.requests import Request

from app.auth import create_agent_token
from app.conversation_coordinator import renew_conversation_claim
from app.main import app
from app.models import (
    KnowledgeDocument,
    MessageHandlingState,
    MessageSurface,
    MessageType,
    MessagesNoisePrefs,
    Project,
    ProjectMessage,
    ReleaseDeployArtifact,
    ReleaseVersion,
    StrategicContextEnvelopeSnapshot,
    Task,
    TaskStatus,
)
from app.routers.agent_strategic_messages import (
    _RESIDENT_POLL_EXPECTED_PER_MINUTE,
    _RESIDENT_POLL_RATE_LIMIT_PER_MINUTE,
    _RESIDENT_POLL_REPLACEMENT_OVERLAP,
    _canonical_delivery_attachment_manifests,
    _ordered_delivery_attachments,
    _resident_poll_rate_limit_key,
    WorkdayArtifactBatchLimit,
    agent_poll_messages,
    router as _messages_router,
)
from app.routers.agent import verify_agent_token
from app.shared_state import limiter
from app.strategic_memory_envelope import (
    canonical_attachment_manifest,
    context_envelope_reference,
    get_or_create_project_memory_state,
    purge_expired_context_envelope_snapshots,
)
from app.strategic_memory_sources import canonical_json_bytes
from app.resident_conversation_scope import (
    RESIDENT_CONVERSATION_FLOOR_SEQUENCE_KEY,
    RESIDENT_OPEN_MESSAGE_CURSOR_FLOOR_KEY,
)
from app.project_message_creation import lock_project_and_next_message_sequence
from app.session_message_handoff import handoff_session_messages
from app.time_utils import _utcnow
from app.workbench_state import WORKBENCH_STATE_KEY

# The inbound-poll router is intentionally NOT wired into app.main here — that
# registration line is reported to the operator, not committed by the plumbing
# builder (see task instructions / CLAUDE.md: do not edit main.py). Mount it onto
# the app the `client` fixture serves so the real route + auth + rate-limit are
# exercised end-to-end. Guarded so a re-import can't double-register the path.
_POLL_PATH = "/api/agent/projects/{project_id}/messages"


if not any(getattr(r, "path", None) == _POLL_PATH for r in app.routes):
    app.include_router(_messages_router)


def test_delivery_attachment_order_matches_signed_manifest_and_degradation():
    ordinary = [{"attachment_id": "ordinary-1"}, {"attachment_id": "ordinary-2"}]
    artifacts = [{"artifact_ref_id": "artifact-1"}, {"artifact_ref_id": "artifact-2"}]

    delivered = _ordered_delivery_attachments(
        {"attachments": ordinary},
        artifacts,
        degraded=False,
    )

    assert delivered == ordinary + artifacts
    assert canonical_attachment_manifest(delivered) == canonical_attachment_manifest(
        ordinary + artifacts
    )
    degraded = _ordered_delivery_attachments(
        {"attachments": ordinary},
        artifacts,
        degraded=True,
    )
    assert degraded == []
    assert canonical_attachment_manifest(degraded) == []


@pytest.mark.asyncio
async def test_claim_manifest_replays_poll_materialization_and_degradation(monkeypatch):
    row_id = uuid.uuid4()
    ordinary = [{"attachment_id": "ordinary-1"}]
    artifacts = [{"artifact_ref_id": "artifact-1"}]
    row = ProjectMessage(id=row_id, context={"attachments": ordinary})
    observed_exemptions: list[frozenset[uuid.UUID]] = []

    async def _materialize(*args, image_budget_exempt_message_ids=frozenset()):
        observed_exemptions.append(image_budget_exempt_message_ids)
        if len(observed_exemptions) == 2:
            raise WorkdayArtifactBatchLimit(row_id)
        return {row_id: artifacts}

    monkeypatch.setattr(
        "app.routers.agent_strategic_messages._materialize_workday_artifact_attachments",
        _materialize,
    )
    delivered = await _canonical_delivery_attachment_manifests(
        None,
        uuid.uuid4(),
        [row],
    )
    assert observed_exemptions == [frozenset()]
    assert delivered[str(row_id)] == canonical_attachment_manifest(
        ordinary + artifacts
    )

    degraded = await _canonical_delivery_attachment_manifests(
        None,
        uuid.uuid4(),
        [row],
    )
    assert observed_exemptions == [frozenset(), frozenset()]
    assert degraded[str(row_id)] == []


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
    task_type = str(task.first_payload.get("type") or "")
    return {"x-agent-token": create_agent_token(str(task.id), vm, task_type)}


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
    now = _utcnow().isoformat()
    project.vision = {
        "_resident_strategic_session": {
            "session_id": session_id,
            "task_id": str(task.id),
            "worker_generation": generation,
            "status": "active",
            "operator_user_id": "test-user-msgpoll",
        },
        "_strategic_local_continuation": {
            "version": 1, "active": True,
            "operator_user_id": "test-user-msgpoll",
            "updated_at": now, "last_seen_at": now,
        },
    }
    db_session.add_all([project, task])
    await db_session.commit()
    return project, task


@pytest.mark.asyncio
async def test_resident_poll_applies_durable_session_floor_to_null_session_rows(
    client: AsyncClient, db_session
):
    project, task = await _make_resident_poll_worker(db_session)
    predecessor_claim_id = uuid.uuid4()
    predecessor_incarnation = uuid.uuid4()
    predecessor_claim_context = {
        "_conversation_public_reply_owner": "fast",
        "_conversation_claim_idempotency_key": "predecessor-shared-turn",
    }
    legacy_activity_sequence = await lock_project_and_next_message_sequence(
        db_session, project.id
    )
    legacy_activity = await _add_message(
        db_session,
        project,
        from_type="user",
        status="open",
        message_type=MessageType.BUG_REPORT,
        surface=MessageSurface.ACTIVITY,
        content="legacy activity bug report remains visible",
        sequence=legacy_activity_sequence,
        resident_session_id="resident-predecessor",
        created_at=_BASE - timedelta(seconds=1),
        handling_state=MessageHandlingState.CLAIMED,
        handled_by_task_id=task.id,
        conversation_claim_id=predecessor_claim_id,
        conversation_claim_generation=1,
        conversation_claim_incarnation=predecessor_incarnation,
        conversation_claim_expires_at=_BASE + timedelta(minutes=10),
        conversation_claim_attempt=1,
        context=predecessor_claim_context,
    )
    own_pre_floor_sequence = await lock_project_and_next_message_sequence(
        db_session, project.id
    )
    own_pre_floor = await _add_message(
        db_session,
        project,
        from_type="user",
        status="open",
        message_type=MessageType.NOTE,
        surface=MessageSurface.CONVERSATION,
        content="intentionally rebound predecessor input",
        sequence=own_pre_floor_sequence,
        resident_session_id="resident-session",
        created_at=_BASE,
    )
    legacy_null_sequence = await lock_project_and_next_message_sequence(
        db_session, project.id
    )
    legacy_null = await _add_message(
        db_session,
        project,
        from_type="user",
        status="open",
        message_type=MessageType.NOTE,
        surface=MessageSurface.CONVERSATION,
        content="legacy null-session backlog",
        sequence=legacy_null_sequence,
        resident_session_id=None,
        created_at=_BASE + timedelta(seconds=1),
        handling_state=MessageHandlingState.CLAIMED,
        handled_by_task_id=task.id,
        conversation_claim_id=predecessor_claim_id,
        conversation_claim_generation=1,
        conversation_claim_incarnation=predecessor_incarnation,
        conversation_claim_expires_at=_BASE + timedelta(minutes=10),
        conversation_claim_attempt=1,
        context=predecessor_claim_context,
    )
    # PostgreSQL's rolling-compatibility INSERT trigger projects every user
    # conversation row to pending. Restore the intended pre-existing claim with
    # an UPDATE, which the gated trigger deliberately leaves untouched; SQLite
    # already preserved the requested state, so this is idempotent there.
    legacy_null.handling_state = MessageHandlingState.CLAIMED
    db_session.add(legacy_null)
    await db_session.commit()
    await db_session.refresh(legacy_null)
    current_null_sequence = await lock_project_and_next_message_sequence(
        db_session, project.id
    )
    current_null = await _add_message(
        db_session,
        project,
        from_type="user",
        status="open",
        message_type=MessageType.NOTE,
        surface=MessageSurface.CONVERSATION,
        content="current-session null input",
        sequence=current_null_sequence,
        resident_session_id=None,
        created_at=_BASE + timedelta(seconds=2),
    )
    new_activity_sequence = await lock_project_and_next_message_sequence(
        db_session, project.id
    )
    new_activity = await _add_message(
        db_session,
        project,
        from_type="user",
        status="open",
        message_type=MessageType.BUG_REPORT,
        surface=MessageSurface.ACTIVITY,
        content="new activity after resident restart",
        sequence=new_activity_sequence,
        resident_session_id=None,
        created_at=_BASE + timedelta(seconds=3),
    )

    # Use the production allocator so PostgreSQL's compatibility trigger and
    # SQLite preserve the same ordering. Anchor the floor to the canonical row
    # actually stored to exercise both pre-floor and inclusive-boundary paths.
    assert legacy_activity.sequence is not None
    assert own_pre_floor.sequence is not None
    assert legacy_null.sequence is not None
    assert current_null.sequence is not None
    assert legacy_activity.sequence < current_null.sequence
    assert own_pre_floor.sequence < current_null.sequence
    assert legacy_null.sequence < current_null.sequence
    resident = dict(project.vision["_resident_strategic_session"])
    resident[RESIDENT_CONVERSATION_FLOOR_SEQUENCE_KEY] = current_null.sequence
    project.vision = {
        **project.vision,
        "_resident_strategic_session": resident,
    }
    flag_modified(project, "vision")
    db_session.add(project)
    await db_session.commit()

    changed = await handoff_session_messages(
        db_session, project.id, "resident-predecessor"
    )
    assert changed == 1
    await db_session.refresh(legacy_activity)
    assert legacy_activity.resident_session_id is None
    assert legacy_activity.handling_state == MessageHandlingState.PENDING
    assert legacy_activity.conversation_claim_id is None
    assert legacy_activity.conversation_claim_generation is None
    assert legacy_activity.conversation_claim_incarnation is None
    assert legacy_activity.conversation_claim_expires_at is None
    assert "_conversation_public_reply_owner" not in legacy_activity.context
    assert "_conversation_claim_idempotency_key" not in legacy_activity.context

    response = await client.get(
        f"/api/agent/projects/{project.id}/messages?limit=20",
        headers=_headers(task, vm="agent-vm-resident-poll"),
    )

    assert response.status_code == 200, response.text
    visible_ids = [row["id"] for row in response.json()["messages"]]
    assert visible_ids == [
        str(legacy_activity.id),
        str(own_pre_floor.id),
        str(current_null.id),
        str(new_activity.id),
    ]
    assert str(legacy_null.id) not in visible_ids

    incarnation_id = "00000000-0000-0000-0000-000000000097"
    claim_payload = {
        "task_id": str(task.id),
        "session_id": "resident-session",
        "worker_generation": 2,
        "incarnation_id": incarnation_id,
        "lease_seconds": 45,
    }
    activity_claim = await client.post(
        f"/api/agent/projects/{project.id}/messages/claim",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            **claim_payload,
            "message_ids": [str(legacy_activity.id), str(new_activity.id)],
        },
    )
    assert activity_claim.status_code == 200, activity_claim.text
    assert activity_claim.json()["message_ids"] == [
        str(legacy_activity.id),
        str(new_activity.id),
    ]

    stale_chat_claim = await client.post(
        f"/api/agent/projects/{project.id}/messages/claim",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={**claim_payload, "message_ids": [str(legacy_null.id)]},
    )
    assert stale_chat_claim.status_code == 409, stale_chat_claim.text

    await db_session.refresh(legacy_activity)
    await db_session.refresh(new_activity)
    await db_session.refresh(legacy_null)
    assert legacy_activity.handling_state == MessageHandlingState.CLAIMED
    assert legacy_activity.conversation_claim_id is not None
    assert legacy_activity.conversation_claim_id != predecessor_claim_id
    assert new_activity.handling_state == MessageHandlingState.CLAIMED
    assert new_activity.conversation_claim_id == legacy_activity.conversation_claim_id
    assert legacy_null.handling_state == MessageHandlingState.CLAIMED
    assert legacy_null.conversation_claim_id == predecessor_claim_id


@pytest.mark.asyncio
async def test_resident_poll_floor_recovers_activity_released_after_start(
    client: AsyncClient, db_session
):
    """A live poll advertises an old row released after bootstrap packing."""

    project, task = await _make_resident_poll_worker(db_session)
    resident = dict(project.vision["_resident_strategic_session"])
    resident[RESIDENT_CONVERSATION_FLOOR_SEQUENCE_KEY] = 100
    project.vision = {
        **project.vision,
        "_resident_strategic_session": resident,
    }
    flag_modified(project, "vision")
    db_session.add(project)
    await db_session.commit()

    released_later = await _add_message(
        db_session,
        project,
        from_type="user",
        status="open",
        message_type=MessageType.NOTE,
        surface=MessageSurface.ACTIVITY,
        content="old ordinary Activity released after Resident bootstrap",
        sequence=7,
        resident_session_id="ended-resident-after-bootstrap",
        created_at=_BASE,
        handling_state=MessageHandlingState.PENDING,
    )
    newer_cursor = (
        (_BASE + timedelta(hours=1))
        .replace(tzinfo=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
    poll_url = f"/api/agent/projects/{project.id}/messages"
    headers = _headers(task, vm="agent-vm-resident-poll")

    before_release = await client.get(
        poll_url,
        params={"since": newer_cursor, "limit": 20},
        headers=headers,
    )
    assert before_release.status_code == 200, before_release.text
    assert before_release.json()["messages"] == []
    assert (
        before_release.json()[RESIDENT_OPEN_MESSAGE_CURSOR_FLOOR_KEY]
        is None
    )

    changed = await handoff_session_messages(
        db_session,
        project.id,
        "ended-resident-after-bootstrap",
        replacement_resident_active=True,
    )
    assert changed == 1
    await db_session.refresh(released_later)
    assert released_later.resident_session_id is None

    control_poll = await client.get(
        poll_url,
        params={"since": newer_cursor, "limit": 20},
        headers=headers,
    )
    assert control_poll.status_code == 200, control_poll.text
    body = control_poll.json()
    assert body["messages"] == []
    expected_floor = (
        released_later.created_at.replace(tzinfo=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
    assert body[RESIDENT_OPEN_MESSAGE_CURSOR_FLOOR_KEY] == expected_floor

    rewound_poll = await client.get(
        poll_url,
        params={"since": expected_floor, "limit": 20},
        headers=headers,
    )
    assert rewound_poll.status_code == 200, rewound_poll.text
    assert [row["id"] for row in rewound_poll.json()["messages"]] == [
        str(released_later.id)
    ]


async def _activate_typed_fast_worker(db_session, project, task) -> str:
    """Install one current typed-Fast process fence for route-level tests."""
    incarnation = str(uuid.uuid4())
    resident = dict(project.vision["_resident_strategic_session"])
    resident["worker_incarnation_id"] = incarnation
    resident["worker_incarnation_expires_at"] = (
        datetime.now(timezone.utc) + timedelta(minutes=5)
    ).isoformat()
    project.vision = {**project.vision, "_resident_strategic_session": resident}
    flag_modified(project, "vision")
    db_session.add(project)
    await db_session.commit()
    return incarnation


async def _add_typed_fast_input(db_session, project, *, sequence: int = 1):
    return await _add_message(
        db_session,
        project,
        sequence=sequence,
        surface=MessageSurface.CONVERSATION,
        message_type=MessageType.NOTE,
        from_type="user",
        status="open",
        content="Explain the current theory direction.",
        resident_session_id="resident-session",
        context={"audience": "operator-fyi"},
    )


def _typed_fast_claim_payload(task, incarnation: str, message_id: uuid.UUID, *, client_claim_id: str):
    return {
        "task_id": str(task.id),
        "session_id": "resident-session",
        "worker_generation": 2,
        "incarnation_id": incarnation,
        "client_claim_id": client_claim_id,
        "idempotency_key": f"typed-fast-claim:{client_claim_id}",
        "message_ids": [str(message_id)],
        "owner": "fast",
        "lease_seconds": 45,
    }


async def _activate_test_call(db_session, project, task):
    incarnation = "00000000-0000-0000-0000-000000000744"
    resident = dict(project.vision["_resident_strategic_session"])
    resident["worker_incarnation_id"] = incarnation
    resident["worker_incarnation_expires_at"] = (
        datetime.now(timezone.utc) + timedelta(minutes=5)
    ).isoformat()
    resident["call"] = {
        "call_id": "call-commit-fence",
        "generation": 3,
        "status": "active",
        "worker_task_id": str(task.id),
        "worker_generation": 2,
        "actor_user_id": "test-user-msgpoll",
        "input_revision": 1,
        "expires_at": (
            datetime.now(timezone.utc) + timedelta(minutes=5)
        ).isoformat(),
    }
    project.vision = {
        **project.vision,
        "_resident_strategic_session": resident,
    }
    flag_modified(project, "vision")
    db_session.add(project)
    await db_session.commit()
    return incarnation


@pytest.mark.asyncio
async def test_call_completion_replay_is_exact_and_terminal_task_cannot_commit(
    client: AsyncClient, db_session
):
    project, task = await _make_resident_poll_worker(db_session)
    incarnation = await _activate_test_call(db_session, project, task)
    inbound = await _add_message(
        db_session,
        project,
        from_type="user",
        status="open",
        content="voice question",
        resident_session_id="resident-session",
        context={
            "call_id": "call-commit-fence",
            "call_generation": 3,
            "input_revision": 1,
            "input_source": "voice",
        },
    )
    payload = {
        "task_id": str(task.id),
        "resident_incarnation_id": incarnation,
        "call_id": "call-commit-fence",
        "call_generation": 3,
        "input_revision": 1,
        "idempotency_key": "call-completion-exact-replay",
        "covered_message_ids": [str(inbound.id)],
        "content": "spoken answer",
        "exact_text": "exact answer",
        "display_blocks": [{"type": "latex", "content": "x^2"}],
    }

    created = await client.post(
        f"/api/agent/projects/{project.id}/strategic-call/complete-turn",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json=payload,
    )
    assert created.status_code == 200, created.text
    duplicate = await client.post(
        f"/api/agent/projects/{project.id}/strategic-call/complete-turn",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json=payload,
    )
    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.json()["status"] == "duplicate"

    mismatched = await client.post(
        f"/api/agent/projects/{project.id}/strategic-call/complete-turn",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={**payload, "exact_text": "different exact answer"},
    )
    assert mismatched.status_code == 409

    task.status = TaskStatus.FAILED
    db_session.add(task)
    await db_session.commit()
    app.dependency_overrides[verify_agent_token] = lambda: {
        "sub": str(task.id),
        "vm_instance_id": "agent-vm-resident-poll",
        "task_type": "strategic_review",
    }
    try:
        terminal = await client.post(
            f"/api/agent/projects/{project.id}/strategic-call/complete-turn",
            headers=_headers(task, vm="agent-vm-resident-poll"),
            json={**payload, "idempotency_key": "terminal-task-must-not-reply"},
        )
    finally:
        app.dependency_overrides.pop(verify_agent_token, None)
    assert terminal.status_code == 409


@pytest.mark.asyncio
async def test_typed_fast_claim_replay_completion_fences_and_abandon(
    client: AsyncClient, db_session
):
    """The typed public-turn boundary survives response loss and rejects stale owners."""
    project, task = await _make_resident_poll_worker(db_session)
    incarnation = await _activate_typed_fast_worker(db_session, project, task)
    project.directions = [
        {
            "id": "d-expired-work",
            "status": "expired",
            "text": "This direction must not be presented as live work",
            "priority": "high",
        },
        {
            "id": "d-superseded-work",
            "status": "superseded",
            "text": "This superseded direction must not be presented as live work",
            "priority": "high",
        },
        {
            "id": "d-auto-completed-work",
            "status": "active",
            "auto_completed": True,
            "text": "This auto-completed direction must not be presented as live work",
            "priority": "high",
        },
        {
            "id": "d-live-work",
            "status": "active",
            "text": "Keep the Strategic design grounded in durable work",
            "priority": "high",
        },
    ]
    existing_work = Task(
        project_id=project.id,
        title="Implement the grounded Strategic surface",
        status=TaskStatus.BLOCKED,
        created_by="STRATEGIC_AGENT",
        origination="STRATEGIST",
        latest_status_message="Waiting for the viewport gate",
        blocked_reason="Chromium bootstrap unavailable",
        context={"direction_id": "d-live-work"},
    )
    completed_work = Task(
        project_id=project.id,
        title="Verify the grounded Strategic backend",
        status=TaskStatus.COMPLETED,
        created_by="STRATEGIC_AGENT",
        origination="STRATEGIST",
        completed_at=_utcnow(),
        context={"direction_id": "d-live-work"},
    )
    now = _utcnow().isoformat()
    continuous_workbench = MessagesNoisePrefs(
        user_id=project.user_id,
        project_id=project.id,
        prefs={
            WORKBENCH_STATE_KEY: {
                "version": 1,
                "interaction_mode": "continuous",
                "active_surface": "strategic",
                "selected_refs": [],
                "session_started_at": now,
                "last_interaction_at": now,
            }
        },
    )
    project.vision["_resident_strategic_session"]["activity"] = {
        "autonomy_state": "active",
        "autonomy_reason_code": None,
    }
    flag_modified(project, "vision")
    db_session.add(project)
    db_session.add(existing_work)
    db_session.add(completed_work)
    db_session.add(continuous_workbench)
    await db_session.commit()
    inbound = await _add_typed_fast_input(db_session, project)
    bound_attachment = {
        "attachment_id": "22222222-2222-4222-8222-222222222222",
        "type": "attachment",
        "mime_type": "text/plain",
        "name": "evidence.txt",
        "size_bytes": 12,
        "checksum": "b" * 64,
        "storage_uri": "gs://private-bucket/evidence.txt",
    }
    inbound.context = {
        **(inbound.context or {}),
        "attachments": [bound_attachment],
    }
    flag_modified(inbound, "context")
    db_session.add(inbound)
    await db_session.commit()
    client_claim_id = str(uuid.uuid4())
    claim_payload = _typed_fast_claim_payload(
        task, incarnation, inbound.id, client_claim_id=client_claim_id
    )
    path = f"/api/agent/projects/{project.id}/conversation/turns/claim"

    claimed = await client.post(
        path,
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json=claim_payload,
    )
    assert claimed.status_code == 200, claimed.text
    canonical = json.loads(base64.b64decode(
        claimed.json()["envelope"]["canonical_envelope_b64"]
    ))
    claimed_row = next(
        row
        for row in canonical["recent_conversation"]
        if row["message_id"] == str(inbound.id)
    )
    assert claimed_row["attachment_manifest"] == canonical_attachment_manifest(
        [bound_attachment]
    )
    assert "private-bucket" not in json.dumps(claimed_row["attachment_manifest"])
    assert canonical["decisions"]["role_contract"] == {
        "public_reply_owner": "fast",
        "workday_request_receipt": "private_consultation_only",
        "interaction_mode": "continuous",
        "private_workday_autonomy_configured": True,
        "private_workday_autonomy": "enabled",
        "private_workday_autonomy_reason_code": None,
        "project_autonomous_enabled": True,
    }
    assert canonical["decisions"]["resident"]["interaction_mode"] == "continuous"
    assert canonical["decisions"]["resident"]["private_autonomy_enabled"] is True
    assert canonical["decisions"]["collection_metadata"] == {
        "active_directions": {"returned_count": 1, "has_more": False},
        "active_tasks": {"returned_count": 1, "has_more": False},
        "recent_terminal_tasks": {"returned_count": 1, "has_more": False},
        "release_deployments": {"returned_count": 0, "has_more": False},
        "result_correlated_tasks": {"returned_count": 0, "has_more": False},
        "result_correlated_documents": {"returned_count": 0, "has_more": False},
        "result_correlated_releases": {"returned_count": 0, "has_more": False},
        "receipt_correlated_directions": {"returned_count": 0, "has_more": False},
        "receipt_correlated_tasks": {"returned_count": 0, "has_more": False},
        "receipt_correlated_documents": {"returned_count": 0, "has_more": False},
        "absence_rule": (
            "When a collection has_more, an unlisted id or state is unknown; "
            "absence never proves that a direction, task, document, release, or "
            "deployment does not exist or has not reached a particular state."
        ),
    }
    assert canonical["decisions"]["active_directions"] == [{
        "id": "d-live-work",
        "status": "active",
        "text": "Keep the Strategic design grounded in durable work",
        "priority": "high",
    }]
    live_task = canonical["decisions"]["active_tasks"][0]
    assert live_task["id"] == str(existing_work.id)
    assert live_task["status"] == "BLOCKED"
    assert live_task["direction_id"] == "d-live-work"
    assert live_task["blocked_reason"] == "Chromium bootstrap unavailable"
    terminal_task = canonical["decisions"]["recent_terminal_tasks"][0]
    assert terminal_task["id"] == str(completed_work.id)
    assert terminal_task["status"] == "COMPLETED"
    assert terminal_task["direction_id"] == "d-live-work"
    assert terminal_task["completed_at"] == completed_work.completed_at.isoformat()
    replay = await client.post(
        path,
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json=claim_payload,
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["claim_id"] == claimed.json()["claim_id"]
    assert replay.json()["envelope"]["snapshot_id"] == claimed.json()["envelope"]["snapshot_id"]

    completion = {
        "task_id": str(task.id),
        "worker_generation": 2,
        "incarnation_id": incarnation,
        "claim_id": claimed.json()["claim_id"],
        "owner": "fast",
        "content": "Here is the immediate answer.",
        "idempotency_key": "typed-fast-completion-fence",
        "envelope": claimed.json()["envelope"],
    }
    stale_owner = await client.post(
        f"/api/agent/projects/{project.id}/conversation/turns/complete",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={**completion, "incarnation_id": str(uuid.uuid4())},
    )
    assert stale_owner.status_code == 409
    stale_envelope = dict(completion["envelope"])
    stale_envelope["canonical_sha256"] = "0" * 64
    stale_snapshot = await client.post(
        f"/api/agent/projects/{project.id}/conversation/turns/complete",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            **completion,
            "envelope": stale_envelope,
            "workday_request": {
                "delivery_key": "must-not-persist",
                "question": "This private packet must roll back with completion.",
            },
        },
    )
    assert stale_snapshot.status_code == 409
    packets_after_failed_complete = (
        await db_session.execute(
            select(ProjectMessage).where(
                ProjectMessage.project_id == project.id,
                ProjectMessage.from_type == "system",
            )
        )
    ).scalars().all()
    assert packets_after_failed_complete == []

    abandoned = await client.post(
        f"/api/agent/projects/{project.id}/conversation/turns/abandon",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            "task_id": str(task.id),
            "session_id": "resident-session",
            "worker_generation": 2,
            "incarnation_id": incarnation,
            "claim_id": claimed.json()["claim_id"],
        },
    )
    assert abandoned.status_code == 200, abandoned.text
    assert abandoned.json() == {
        "status": "abandoned",
        "legacy_fallback_admissible": True,
        "consultation_id": None,
    }


@pytest.mark.asyncio
async def test_typed_fast_exact_completion_replay_rebroadcasts_canonical_message(
    client: AsyncClient, db_session, mocker
):
    """A lost post-commit event is recovered without creating another reply."""

    project, task = await _make_resident_poll_worker(db_session)
    incarnation = await _activate_typed_fast_worker(db_session, project, task)
    claim_id = uuid.uuid4()
    reply = await _add_message(
        db_session,
        project,
        sequence=1,
        surface=MessageSurface.CONVERSATION,
        message_type=MessageType.NOTE,
        from_type="strategic",
        from_task_id=task.id,
        status="open",
        handling_state=MessageHandlingState.RESPONDED,
        content="Here is the durable immediate answer.",
        resident_session_id="resident-session",
        idempotency_key="typed-fast-rebroadcast-exact-replay",
        context={
            "message_role": "operator_reply",
            "_conversation_claim_id": str(claim_id),
            "_conversation_public_reply_owner": "fast",
            "audience": "operator-fyi",
        },
    )
    broadcast = mocker.patch(
        "app.routers.agent_strategic_messages.broadcast_to_project",
        new_callable=mocker.AsyncMock,
    )
    payload = {
        "task_id": str(task.id),
        "worker_generation": 2,
        "incarnation_id": incarnation,
        "claim_id": str(claim_id),
        "owner": "fast",
        "content": reply.content,
        "idempotency_key": reply.idempotency_key,
        # Exact durable replays deliberately do not dereference the expired
        # prompt envelope again.
        "envelope": {},
    }

    replay = await client.post(
        f"/api/agent/projects/{project.id}/conversation/turns/complete",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json=payload,
    )

    assert replay.status_code == 200, replay.text
    assert replay.json() == {"id": str(reply.id), "status": "duplicate"}
    assert "_workday_consultation_requested" not in reply.context
    broadcast.assert_awaited_once()
    assert broadcast.await_args.args[:2] == (str(project.id), "message_created")
    assert broadcast.await_args.args[2]["message"]["id"] == str(reply.id)

    conflict = await client.post(
        f"/api/agent/projects/{project.id}/conversation/turns/complete",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={**payload, "content": "Conflicting text must not be broadcast."},
    )
    assert conflict.status_code == 409
    assert broadcast.await_count == 1


@pytest.mark.asyncio
async def test_workday_continuation_exact_replay_rebroadcasts_canonical_message(
    client: AsyncClient, db_session, mocker
):
    """Continuation recovery re-emits the exact committed row-id event."""

    project, task = await _make_resident_poll_worker(db_session)
    incarnation = await _activate_typed_fast_worker(db_session, project, task)
    continuation_claim_id = uuid.uuid4()
    result_packet_id = uuid.uuid4()
    reply = await _add_message(
        db_session,
        project,
        sequence=1,
        surface=MessageSurface.CONVERSATION,
        message_type=MessageType.NOTE,
        from_type="strategic",
        from_task_id=task.id,
        status="open",
        handling_state=MessageHandlingState.RESPONDED,
        content="The checked result is now available.",
        resident_session_id="resident-session",
        idempotency_key="typed-workday-continuation-rebroadcast",
        context={
            "message_role": "workday_consultation_continuation",
            "_workday_continuation_claim_id": str(continuation_claim_id),
            "_workday_result_packet_id": str(result_packet_id),
            "_conversation_public_reply_owner": "fast",
            "audience": "operator-fyi",
        },
    )
    broadcast = mocker.patch(
        "app.routers.agent_strategic_messages.broadcast_to_project",
        new_callable=mocker.AsyncMock,
    )
    payload = {
        "task_id": str(task.id),
        "session_id": "resident-session",
        "worker_generation": 2,
        "incarnation_id": incarnation,
        "continuation_claim_id": str(continuation_claim_id),
        "content": reply.content,
        "idempotency_key": reply.idempotency_key,
    }
    path = (
        f"/api/agent/projects/{project.id}/workday-consultations/"
        f"continuations/{result_packet_id}/complete"
    )

    replay = await client.post(
        path,
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json=payload,
    )

    assert replay.status_code == 200, replay.text
    assert replay.json() == {"id": str(reply.id), "status": "duplicate"}
    broadcast.assert_awaited_once()
    assert broadcast.await_args.args[:2] == (str(project.id), "message_created")
    assert broadcast.await_args.args[2]["message"]["id"] == str(reply.id)

    conflict = await client.post(
        path,
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={**payload, "content": "Conflicting continuation text."},
    )
    assert conflict.status_code == 409
    assert broadcast.await_count == 1


@pytest.mark.asyncio
async def test_post_ledger_fast_claim_and_workday_consultation_use_live_envelope_revision(
    client: AsyncClient, db_session
):
    """A V2 admission must not strand the foreground or private hand-off."""

    project, task = await _make_resident_poll_worker(db_session)
    incarnation = await _activate_typed_fast_worker(db_session, project, task)
    inbound = await _add_typed_fast_input(db_session, project)
    state = await get_or_create_project_memory_state(db_session, project.id)
    state.ledger_revision = 7
    await db_session.commit()

    claim = await client.post(
        f"/api/agent/projects/{project.id}/conversation/turns/claim",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json=_typed_fast_claim_payload(
            task, incarnation, inbound.id, client_claim_id=str(uuid.uuid4())
        ),
    )
    assert claim.status_code == 200, claim.text
    assert claim.json()["envelope"]["ledger_revision"] == 7

    consultation = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            "task_id": str(task.id),
            "session_id": "resident-session",
            "worker_generation": 2,
            "incarnation_id": incarnation,
            "claim_id": claim.json()["claim_id"],
            "delivery_key": "post-ledger-consultation",
            "question": "Please inspect the derivation.",
        },
    )
    assert consultation.status_code == 200, consultation.text
    assert consultation.json()["envelope"]["ledger_revision"] == 7


@pytest.mark.asyncio
async def test_fast_claim_retires_stale_private_result_and_delivers_next_continuation(
    client: AsyncClient, db_session
):
    """A revoked result prompt cannot poison later Fast continuations."""

    project, task = await _make_resident_poll_worker(db_session)
    incarnation = await _activate_typed_fast_worker(db_session, project, task)
    inbound = await _add_typed_fast_input(db_session, project)
    claim = await client.post(
        f"/api/agent/projects/{project.id}/conversation/turns/claim",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json=_typed_fast_claim_payload(
            task, incarnation, inbound.id, client_claim_id=str(uuid.uuid4())
        ),
    )
    assert claim.status_code == 200, claim.text
    live_reference = context_envelope_reference(claim.json()["envelope"])
    stale_reference = {**live_reference, "snapshot_id": str(uuid.uuid4())}
    common_context = {
        "_workday_consultation_result": True,
        "audience": "strategist-internal",
        "message_role": "workday_consultation_result",
        "public_reply_owner": "fast",
    }
    stale = await _add_message(
        db_session,
        project,
        sequence=20,
        surface=MessageSurface.CONVERSATION,
        message_type=MessageType.NOTE,
        from_type="system",
        status="open",
        handling_state=MessageHandlingState.PENDING,
        content="stale result must never be surfaced",
        resident_session_id="resident-session",
        context={
            **common_context,
            "delivery_key": "stale",
            "envelope": stale_reference,
        },
    )
    valid = await _add_message(
        db_session,
        project,
        sequence=21,
        surface=MessageSurface.CONVERSATION,
        message_type=MessageType.NOTE,
        from_type="system",
        status="open",
        handling_state=MessageHandlingState.PENDING,
        content="valid checked result",
        resident_session_id="resident-session",
        context={**common_context, "delivery_key": "valid", "envelope": live_reference},
    )

    response = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/"
        "continuations/claim-next",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            "task_id": str(task.id),
            "session_id": "resident-session",
            "worker_generation": 2,
            "incarnation_id": incarnation,
            "lease_seconds": 45,
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "claimed"
    assert response.json()["continuation"]["result_packet_id"] == str(valid.id)
    assert response.json()["continuation"]["result_summary"] == "valid checked result"
    await db_session.refresh(stale)
    retired = stale
    assert retired.status == "resolved"
    assert retired.handling_state == MessageHandlingState.SUPERSEDED
    assert retired.handled_by_task_id is None
    assert retired.resolution == "private_handoff_context_envelope_unavailable"
    assert retired.context["_private_handoff_retirement"]["reason"] == (
        "context_envelope_unavailable"
    )
    assert "_workday_continuation_claim_id" not in retired.context


@pytest.mark.asyncio
async def test_typed_workday_packet_and_continuation_routes_are_receipt_bound(
    client: AsyncClient, db_session
):
    """Private delivery and public continuation use distinct renewable receipts."""
    project, task = await _make_resident_poll_worker(db_session)
    task.payloads = [{"type": "strategic_review", "typed_fast_enabled": True}]
    flag_modified(task, "payloads")
    db_session.add(task)
    await db_session.commit()
    incarnation = await _activate_typed_fast_worker(db_session, project, task)
    inbound = await _add_typed_fast_input(db_session, project)
    claim = await client.post(
        f"/api/agent/projects/{project.id}/conversation/turns/claim",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json=_typed_fast_claim_payload(
            task, incarnation, inbound.id, client_claim_id=str(uuid.uuid4())
        ),
    )
    assert claim.status_code == 200, claim.text
    claim_id = claim.json()["claim_id"]
    identity = {
        "task_id": str(task.id),
        "session_id": "resident-session",
        "worker_generation": 2,
        "incarnation_id": incarnation,
    }
    completed_turn = await client.post(
        f"/api/agent/projects/{project.id}/conversation/turns/complete",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            "task_id": str(task.id),
            "worker_generation": 2,
            "incarnation_id": incarnation,
            "claim_id": claim_id,
            "owner": "fast",
            "content": "I will check the missing derivation and follow up.",
            "idempotency_key": "typed-workday-initial-answer",
            "envelope": claim.json()["envelope"],
            "workday_request": {
                "delivery_key": "derive-bound",
                "question": "Check the missing derivation privately.",
            },
        },
    )
    assert completed_turn.status_code == 200, completed_turn.text
    packet_id = completed_turn.json()["workday_packet"]["packet_id"]
    consultation_id = completed_turn.json()["workday_packet"]["consultation_id"]
    duplicate_turn = await client.post(
        f"/api/agent/projects/{project.id}/conversation/turns/complete",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            "task_id": str(task.id),
            "worker_generation": 2,
            "incarnation_id": incarnation,
            "claim_id": claim_id,
            "owner": "fast",
            "content": "I will check the missing derivation and follow up.",
            "idempotency_key": "typed-workday-initial-answer",
            "envelope": claim.json()["envelope"],
            "workday_request": {
                "delivery_key": "derive-bound",
                "question": "Check the missing derivation privately.",
            },
        },
    )
    assert duplicate_turn.status_code == 200, duplicate_turn.text
    assert duplicate_turn.json()["status"] == "duplicate"
    assert duplicate_turn.json()["workday_packet"]["packet_id"] == packet_id
    assert duplicate_turn.json()["workday_packet"]["consultation_id"] == consultation_id
    initial_reply = (
        await db_session.execute(
            select(ProjectMessage).where(
                ProjectMessage.project_id == project.id,
                ProjectMessage.idempotency_key == "typed-workday-initial-answer",
            )
        )
    ).scalars().one()
    assert initial_reply.context["message_role"] == "operator_reply"
    assert initial_reply.context["_workday_consultation_requested"] is True
    packet_claim = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/claim-next",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={**identity, "lease_seconds": 45},
    )
    assert packet_claim.status_code == 200, packet_claim.text
    packet = packet_claim.json()["packet"]
    assert packet["packet_id"] == packet_id
    assert packet["consultation_id"] == str(initial_reply.id)
    assert consultation_id == str(initial_reply.id)
    renewed_packet = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/{packet_id}/renew",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            **identity,
            "workday_claim_id": packet["workday_claim_id"],
            "lease_seconds": 45,
        },
    )
    assert renewed_packet.status_code == 200, renewed_packet.text
    problem_model_op = {
        "task_id": str(task.id),
        "resident_incarnation_id": incarnation,
        "op": "set_problem_model",
        "data": {
            "intent": {
                "purpose": "Ground the missing derivation.",
                "current_focus": "Verify the correction.",
            },
            "capabilities": [],
            "gaps": [],
        },
    }
    workday_operation_id = str(uuid.uuid4())
    missing_provenance = await client.post(
        "/api/agent/strategic",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json=problem_model_op,
    )
    assert missing_provenance.status_code == 409, missing_provenance.text
    missing_pair = await client.post(
        "/api/agent/strategic",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={**problem_model_op, "packet_id": packet_id},
    )
    assert missing_pair.status_code == 422, missing_pair.text
    wrong_claim = await client.post(
        "/api/agent/strategic",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            **problem_model_op,
            "packet_id": packet_id,
            "workday_claim_id": str(uuid.uuid4()),
            "workday_operation_id": workday_operation_id,
        },
    )
    assert wrong_claim.status_code == 409, wrong_claim.text
    problem_model_write = await client.post(
        "/api/agent/strategic",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            **problem_model_op,
            "packet_id": packet_id,
            "workday_claim_id": packet["workday_claim_id"],
            "workday_operation_id": workday_operation_id,
        },
    )
    assert problem_model_write.status_code == 200, problem_model_write.text
    problem_model_revision = problem_model_write.json()["revision"]
    replayed_problem_model_write = await client.post(
        "/api/agent/strategic",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            **problem_model_op,
            "packet_id": packet_id,
            "workday_claim_id": packet["workday_claim_id"],
            "workday_operation_id": workday_operation_id,
        },
    )
    assert replayed_problem_model_write.status_code == 200, replayed_problem_model_write.text
    assert replayed_problem_model_write.json()["idempotent_replay"] is True
    assert replayed_problem_model_write.json()["revision"] == problem_model_revision
    conflicting_problem_model_write = await client.post(
        "/api/agent/strategic",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            **problem_model_op,
            "data": {
                **problem_model_op["data"],
                "intent": {
                    "purpose": "A conflicting replay must fail.",
                    "current_focus": "Do not mutate.",
                },
            },
            "packet_id": packet_id,
            "workday_claim_id": packet["workday_claim_id"],
            "workday_operation_id": workday_operation_id,
        },
    )
    assert conflicting_problem_model_write.status_code == 409
    await db_session.refresh(project)
    problem_model_sha = hashlib.sha256(
        canonical_json_bytes(project.problem_model)
    ).hexdigest()
    correlated_terminal_task = Task(
        project_id=project.id,
        title="Derivation implementation",
        status=TaskStatus.COMPLETED,
        completed_at=_utcnow(),
        updated_at=_utcnow() - timedelta(hours=2),
        context={
            "direction_id": "direction-derive-bound",
            "strategic_work_order_id": "work-order-derive-bound",
        },
    )
    unrelated_terminal_task = Task(
        project_id=project.id,
        title="Unrelated completed work",
        status=TaskStatus.COMPLETED,
        completed_at=_utcnow() - timedelta(seconds=1),
        updated_at=_utcnow() - timedelta(hours=3),
        context={"strategic_work_order_id": "unrelated-work-order"},
    )
    newer_linked_terminal_tasks = [
        Task(
            project_id=project.id,
            title=f"Newer unrelated linked outcome {index}",
            status=TaskStatus.COMPLETED,
            completed_at=_utcnow() + timedelta(minutes=index),
            updated_at=_utcnow() + timedelta(minutes=index),
            context={"strategic_work_order_id": f"newer-work-order-{index}"},
        )
        for index in range(7)
    ]
    foreign_project = Project(
        name="Foreign result authority",
        user_id="test-user-msgpoll",
    )
    foreign_task = Task(
        project_id=foreign_project.id,
        title="Other project task",
        status=TaskStatus.COMPLETED,
        completed_at=_utcnow(),
        context={"strategic_work_order_id": "foreign-work-order"},
    )
    random_result_uuid = uuid.uuid4()
    correlated_document = KnowledgeDocument(
        project_id=project.id,
        source_task_id=correlated_terminal_task.id,
        source_kind="strategic_memory",
        title="Durable derivation note",
        summary="The derivation failure and its correction.",
        body="Canonical bounded note body.",
    )
    foreign_document = KnowledgeDocument(
        project_id=foreign_project.id,
        source_task_id=foreign_task.id,
        source_kind="strategic_memory",
        title="Other project note",
        summary="Must not cross the project authority boundary.",
        body="Foreign canonical note body.",
    )
    served_sha = "c" * 40
    unsupported_served_sha = "d" * 40
    served_release = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        status="promoted",
        promoted_sha=served_sha,
        promoted_at=_utcnow(),
        serving_status="serving_confirmed",
        served_sha=served_sha,
        served_at=_utcnow(),
    )
    served_release_artifact = ReleaseDeployArtifact(
        version_id=served_release.id,
        artifact_kind="promotion_merge",
        serving_status="serving_confirmed",
        served_identifier=served_sha,
        served_at=_utcnow(),
    )
    newer_unconfirmed_releases = [
        ReleaseVersion(
            project_id=project.id,
            version_number=index,
            status="promoted",
            promoted_sha=str(index) * 40,
            promoted_at=_utcnow() + timedelta(minutes=index),
            serving_status="unconfirmed",
        )
        for index in range(2, 7)
    ]
    project.running_update = "The derivation is now implemented and under review."
    db_session.add_all(
        [
            project,
            foreign_project,
            correlated_terminal_task,
            unrelated_terminal_task,
            *newer_linked_terminal_tasks,
            foreign_task,
            served_release,
            *newer_unconfirmed_releases,
        ]
    )
    await db_session.flush()
    db_session.add_all(
        [correlated_document, foreign_document, served_release_artifact]
    )
    await db_session.commit()
    acknowledged = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/{packet_id}/ack",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            **identity,
            "workday_claim_id": packet["workday_claim_id"],
            "outcome": "completed",
            "result_summary": (
                f"Task {correlated_terminal_task.id} completed; "
                f"task {foreign_task.id} from another project also completed; "
                f"document {correlated_document.id} was written, while foreign "
                f"document {foreign_document.id} also exists; unknown receipt "
                f"{random_result_uuid}; Workday reports {served_sha} "
                f"and {unsupported_served_sha} are serving and the "
                "failure was durably recorded in a project note."
            ),
        },
    )
    assert acknowledged.status_code == 200, acknowledged.text

    continuation_claim = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/continuations/claim-next",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={**identity, "lease_seconds": 45},
    )
    assert continuation_claim.status_code == 200, continuation_claim.text
    continuation = continuation_claim.json()["continuation"]
    assert continuation["attempt"] == 1
    assert str(correlated_terminal_task.id) in continuation["result_summary"]
    assert unsupported_served_sha in continuation["result_summary"]
    original_envelope = json.loads(
        base64.b64decode(
            continuation["envelope"]["canonical_envelope_b64"]
        ).decode("utf-8")
    )
    assert str(correlated_terminal_task.id) not in json.dumps(original_envelope)
    assert served_sha not in json.dumps(original_envelope)
    current_authority = continuation["current_live_work_authority"]
    current_terminal_tasks = current_authority["recent_terminal_tasks"]
    assert len(current_terminal_tasks) == 6
    assert str(correlated_terminal_task.id) not in {
        item["id"] for item in current_terminal_tasks
    }
    assert current_authority["collection_metadata"]["recent_terminal_tasks"] == {
        "returned_count": 6,
        "has_more": True,
    }
    assert current_authority["result_correlation"] == {
        "candidate_source": "bounded_workday_result_refs_for_correlation_only",
        "canonical_state_source": "project_scoped_database_projection",
        "uuid_candidate_count": 5,
        "sha_candidate_count": 2,
        "candidate_refs_have_more": False,
    }
    correlated_tasks = current_authority["result_correlated_tasks"]
    assert [item["id"] for item in correlated_tasks] == [
        str(correlated_terminal_task.id)
    ]
    correlated_projection = correlated_tasks[0]
    assert correlated_projection["correlation_refs"] == [
        str(correlated_terminal_task.id)
    ]
    assert correlated_projection["direction_id"] == "direction-derive-bound"
    assert correlated_projection["strategic_work_order_id"] == "work-order-derive-bound"
    assert "serving_status" not in correlated_projection
    assert current_authority["collection_metadata"]["result_correlated_tasks"] == {
        "returned_count": 1,
        "has_more": False,
    }
    authority_json = json.dumps(current_authority)
    assert str(foreign_task.id) not in authority_json
    assert str(random_result_uuid) not in authority_json
    correlated_documents = current_authority["result_correlated_documents"]
    assert [item["id"] for item in correlated_documents] == [
        str(correlated_document.id)
    ]
    assert correlated_documents[0]["correlation_refs"] == [
        str(correlated_document.id)
    ]
    assert correlated_documents[0]["source_task_id"] == str(
        correlated_terminal_task.id
    )
    assert correlated_documents[0]["summary"] == (
        "The derivation failure and its correction."
    )
    assert "body" not in correlated_documents[0]
    assert str(foreign_document.id) not in authority_json
    assert current_authority["collection_metadata"][
        "result_correlated_documents"
    ] == {"returned_count": 1, "has_more": False}
    assert current_authority["current_project_state_authority"] == {
        "problem_model": {
            "present": True,
            "revision": problem_model_revision,
            "sha256": problem_model_sha,
            "last_updated": project.problem_model["last_updated"],
            "last_updated_by": "agent",
        },
        "running_update": {
            "present": True,
            "sha256": hashlib.sha256(
                project.running_update.encode("utf-8")
            ).hexdigest(),
            "bytes": len(project.running_update.encode("utf-8")),
        },
        "vision": {
            "present": False,
            "revision": None,
            "sha256": hashlib.sha256(b"{}").hexdigest(),
        },
        "authorship_rule": (
            "Current existence, revision, or digest does not identify which actor "
            "or consultation wrote it without an exact durable mutation receipt."
        ),
    }

    assert str(served_release.id) not in {
        item["id"] for item in current_authority["release_deployments"]
    }
    assert all(
        item["serving_status"] != "serving_confirmed"
        for item in current_authority["release_deployments"]
    )
    assert current_authority["collection_metadata"]["release_deployments"] == {
        "returned_count": 4,
        "has_more": True,
    }
    correlated_releases = current_authority["result_correlated_releases"]
    assert len(correlated_releases) == 1
    current_release = correlated_releases[0]
    assert current_release["id"] == str(served_release.id)
    assert current_release["correlation_refs"] == [served_sha]
    assert current_release["promoted_sha"] == served_sha
    assert current_release["served_sha"] == served_sha
    assert current_release["serving_status"] == "serving_confirmed"
    assert current_authority["collection_metadata"][
        "result_correlated_releases"
    ] == {"returned_count": 1, "has_more": False}
    assert unsupported_served_sha not in json.dumps(correlated_releases)
    mutation_receipts = current_authority["consultation_mutation_receipts"]
    assert len(mutation_receipts) == 1
    mutation_receipt = mutation_receipts[0]
    assert mutation_receipt["packet_id"] == packet_id
    assert mutation_receipt["workday_claim_id"] == packet["workday_claim_id"]
    assert mutation_receipt["operation_id"] == workday_operation_id
    assert mutation_receipt["task_id"] == str(task.id)
    assert mutation_receipt["session_id"] == "resident-session"
    assert mutation_receipt["worker_generation"] == 2
    assert mutation_receipt["incarnation_id"] == incarnation
    assert mutation_receipt["op"] == "set_problem_model"
    assert mutation_receipt["target_revision"] == problem_model_revision
    assert mutation_receipt["target_sha256"] == problem_model_sha
    assert mutation_receipt["context_authority_generation_after"] == (
        mutation_receipt["context_authority_generation_before"] + 1
    )
    assert current_authority["collection_metadata"][
        "consultation_mutation_receipts"
    ] == {"returned_count": 1, "has_more": False}
    result_packet_id = continuation["result_packet_id"]
    renewed_continuation = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/continuations/{result_packet_id}/renew",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            **identity,
            "continuation_claim_id": continuation["continuation_claim_id"],
            "lease_seconds": 45,
        },
    )
    assert renewed_continuation.status_code == 200, renewed_continuation.text
    released = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/continuations/{result_packet_id}/release",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            **identity,
            "continuation_claim_id": continuation["continuation_claim_id"],
        },
    )
    assert released.status_code == 200, released.text
    assert released.json()["status"] == "released"
    reclaimed = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/continuations/claim-next",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={**identity, "lease_seconds": 45},
    )
    assert reclaimed.status_code == 200, reclaimed.text
    follow_up = reclaimed.json()["continuation"]
    assert follow_up["attempt"] == 2
    completed = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/continuations/{result_packet_id}/complete",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            **identity,
            "continuation_claim_id": follow_up["continuation_claim_id"],
            "content": "I checked it: conditioning gives the missing step.",
            "idempotency_key": "typed-workday-follow-up",
        },
    )
    assert completed.status_code == 200, completed.text
    # The result packet pins its source envelope only as a bounded prompt
    # cache. A lost HTTP response must still be exactly replayable after that
    # cache has expired and been purged: no new prompt, packet, or public text
    # is needed to return the already-created receipt.
    snapshot = await db_session.get(
        StrategicContextEnvelopeSnapshot,
        uuid.UUID(continuation["envelope"]["snapshot_id"]),
    )
    assert snapshot is not None
    snapshot.created_at = _utcnow() - timedelta(minutes=20)
    snapshot.expires_at = _utcnow() - timedelta(seconds=1)
    db_session.add(snapshot)
    await db_session.commit()
    await purge_expired_context_envelope_snapshots(
        db_session,
        project_id=project.id,
    )
    await db_session.commit()
    assert (
        await db_session.get(StrategicContextEnvelopeSnapshot, snapshot.snapshot_id)
    ) is None
    duplicate = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/continuations/{result_packet_id}/complete",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            **identity,
            "continuation_claim_id": follow_up["continuation_claim_id"],
            "content": "I checked it: conditioning gives the missing step.",
            "idempotency_key": "typed-workday-follow-up",
        },
    )
    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.json()["status"] == "duplicate"
    conflicting_replay = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/continuations/{result_packet_id}/complete",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            **identity,
            "continuation_claim_id": follow_up["continuation_claim_id"],
            "content": "Different text must not reuse the continuation receipt.",
            "idempotency_key": "typed-workday-follow-up",
        },
    )
    assert conflicting_replay.status_code == 409


@pytest.mark.asyncio
async def test_finalized_transcript_rejects_terminal_resident_task(
    client: AsyncClient, db_session
):
    project, task = await _make_resident_poll_worker(db_session)
    incarnation = await _activate_test_call(db_session, project, task)
    task.status = TaskStatus.CANCELLED
    db_session.add(task)
    await db_session.commit()

    app.dependency_overrides[verify_agent_token] = lambda: {
        "sub": str(task.id),
        "vm_instance_id": "agent-vm-resident-poll",
        "task_type": "strategic_review",
    }
    try:
        response = await client.post(
            f"/api/agent/projects/{project.id}/strategic-call/finalized-transcript",
            headers=_headers(task, vm="agent-vm-resident-poll"),
            json={
                "task_id": str(task.id),
                "resident_incarnation_id": incarnation,
                "call_id": "call-commit-fence",
                "call_generation": 3,
                "content": "must not become durable",
                "idempotency_key": "terminal-transcript",
                "input_source": "voice",
            },
        )
    finally:
        app.dependency_overrides.pop(verify_agent_token, None)
    assert response.status_code == 409
    rows = (
        await db_session.execute(
            select(ProjectMessage).where(
                ProjectMessage.project_id == project.id,
                ProjectMessage.content == "must not become durable",
            )
        )
    ).scalars().all()
    assert rows == []


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
async def test_resident_poll_returns_stale_fast_claim_group_atomically_across_page_cap(
    client: AsyncClient, db_session, monkeypatch
):
    monkeypatch.setenv(
        "CALL_FOREGROUND_QUALIFICATION", "claude-warm-stream-json:phase0-v1"
    )
    project, task = await _make_resident_poll_worker(db_session)
    older = await _add_message(
        db_session,
        project,
        from_type="user",
        status="open",
        message_type=MessageType.NOTE,
        surface=MessageSurface.CONVERSATION,
        content="older unclaimed input",
        sequence=1,
        resident_session_id="resident-session",
        created_at=_BASE,
    )
    claim_id = uuid.uuid4()
    stale_rows = []
    for offset in range(20):
        stale_rows.append(
            await _add_message(
                db_session,
                project,
                from_type="user",
                status="open",
                message_type=MessageType.NOTE,
                surface=MessageSurface.CONVERSATION,
                content=f"stale claimed input {offset}",
                sequence=offset + 2,
                resident_session_id="resident-session",
                created_at=_BASE + timedelta(seconds=offset + 1),
                handling_state=MessageHandlingState.CLAIMED,
                handled_by_task_id=task.id,
                conversation_claim_id=claim_id,
                conversation_claim_generation=1,
                conversation_claim_incarnation=uuid.uuid4(),
                conversation_claim_expires_at=_BASE - timedelta(minutes=1),
                conversation_claim_attempt=1,
                context={
                    "_conversation_public_reply_owner": "fast",
                    "_conversation_claim_idempotency_key": "stale-fast-turn",
                },
            )
        )
    newer = await _add_message(
        db_session,
        project,
        from_type="user",
        status="open",
        message_type=MessageType.NOTE,
        surface=MessageSurface.CONVERSATION,
        content="newer unclaimed input",
        sequence=22,
        resident_session_id="resident-session",
        created_at=_BASE + timedelta(minutes=1),
    )

    response = await client.get(
        f"/api/agent/projects/{project.id}/messages?limit=20",
        headers=_headers(task, vm="agent-vm-resident-poll"),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    expected_ids = [str(row.id) for row in stale_rows]
    assert [row["id"] for row in body["messages"]] == expected_ids
    assert body["fast_message_ids"] == expected_ids
    assert body["fast_claim_groups"] == [
        {
            "idempotency_key": "stale-fast-turn",
            "message_ids": expected_ids,
            "member_count": 20,
            "complete": True,
            "artifacts_degraded": False,
        }
    ]
    await db_session.refresh(older)
    await db_session.refresh(newer)
    assert older.status == "open"
    assert newer.status == "open"
    assert older.conversation_claim_id is None
    assert newer.conversation_claim_id is None


@pytest.mark.asyncio
async def test_resident_poll_leaves_expired_ordinary_claim_group_to_workday(
    client: AsyncClient, db_session
):
    project, task = await _make_resident_poll_worker(db_session)
    old_claim_id = uuid.uuid4()
    old_incarnation = uuid.uuid4()
    claim_fields = {
        "from_type": "user",
        "status": "open",
        "message_type": MessageType.NOTE,
        "resident_session_id": "resident-session",
        "handling_state": MessageHandlingState.CLAIMED,
        "handled_by_task_id": task.id,
        "conversation_claim_id": old_claim_id,
        "conversation_claim_generation": 1,
        "conversation_claim_incarnation": old_incarnation,
        "conversation_claim_expires_at": _BASE - timedelta(minutes=1),
        "conversation_claim_attempt": 1,
        "context": {},
    }
    conversation_row = await _add_message(
        db_session,
        project,
        surface=MessageSurface.CONVERSATION,
        content="ordinary conversation input",
        sequence=1,
        created_at=_BASE,
        **claim_fields,
    )
    activity_row = await _add_message(
        db_session,
        project,
        surface=MessageSurface.ACTIVITY,
        content="ordinary activity input",
        sequence=2,
        created_at=_BASE + timedelta(seconds=1),
        **claim_fields,
    )

    response = await client.get(
        f"/api/agent/projects/{project.id}/messages?limit=20",
        headers=_headers(task, vm="agent-vm-resident-poll"),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    expected_ids = [str(conversation_row.id), str(activity_row.id)]
    assert [row["id"] for row in body["messages"]] == expected_ids
    assert body["fast_message_ids"] == []
    assert body["fast_claim_groups"] == []

    replacement_incarnation = str(uuid.uuid4())
    reclaimed = await client.post(
        f"/api/agent/projects/{project.id}/messages/claim",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            "task_id": str(task.id),
            "session_id": "resident-session",
            "worker_generation": 2,
            "incarnation_id": replacement_incarnation,
            "message_ids": expected_ids,
            "lease_seconds": 45,
        },
    )
    assert reclaimed.status_code == 200, reclaimed.text
    assert reclaimed.json()["claim_id"] == str(old_claim_id)
    assert reclaimed.json()["message_ids"] == expected_ids


@pytest.mark.asyncio
async def test_stale_fast_claim_group_degrades_artifacts_without_losing_membership(
    client: AsyncClient, db_session, monkeypatch
):
    monkeypatch.setenv(
        "CALL_FOREGROUND_QUALIFICATION", "claude-warm-stream-json:phase0-v1"
    )
    project, task = await _make_resident_poll_worker(db_session)
    claim_id = uuid.uuid4()
    stale_rows = []
    for offset in range(2):
        stale_rows.append(
            await _add_message(
                db_session,
                project,
                from_type="user",
                status="open",
                message_type=MessageType.NOTE,
                surface=MessageSurface.CONVERSATION,
                content=f"claimed artifact input {offset}",
                sequence=offset + 1,
                resident_session_id="resident-session",
                created_at=_BASE + timedelta(seconds=offset),
                handling_state=MessageHandlingState.CLAIMED,
                handled_by_task_id=task.id,
                conversation_claim_id=claim_id,
                conversation_claim_generation=1,
                conversation_claim_incarnation=uuid.uuid4(),
                conversation_claim_expires_at=_BASE - timedelta(minutes=1),
                conversation_claim_attempt=1,
                context={
                    "_conversation_public_reply_owner": "fast",
                    "_conversation_claim_idempotency_key": "stale-artifact-turn",
                    "attachments": [{"type": "image", "url": "https://expired"}],
                },
            )
        )

    async def _exceed_artifact_budget(*args, **kwargs):
        raise WorkdayArtifactBatchLimit(stale_rows[1].id)

    monkeypatch.setattr(
        "app.routers.agent_strategic_messages._materialize_workday_artifact_attachments",
        _exceed_artifact_budget,
    )
    response = await client.get(
        f"/api/agent/projects/{project.id}/messages?limit=20",
        headers=_headers(task, vm="agent-vm-resident-poll"),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    expected_ids = [str(row.id) for row in stale_rows]
    assert [row["id"] for row in body["messages"]] == expected_ids
    assert body["fast_message_ids"] == expected_ids
    assert body["fast_claim_groups"][0]["complete"] is True
    assert body["fast_claim_groups"][0]["artifacts_degraded"] is True
    for row in body["messages"]:
        assert "System recovery notice" not in row["content"]
        assert row["context"]["_artifact_delivery_deferred"] is True
        assert "attachments" not in row["context"]


@pytest.mark.asyncio
async def test_resident_poll_never_fast_marks_rows_without_server_qualification(
    client: AsyncClient, db_session, monkeypatch
):
    """Without a server-qualified Fast activation, conversation rows must stay
    on the legacy Workday reply path. The agent treats fast-marked rows as
    exclusively Fast-owned, so stamping them when no Resident can ever activate
    the Fast lane parks every operator message forever — the live no-reply
    deadlock (operator "hi" retried silently until the session was stopped)."""
    monkeypatch.delenv("CALL_FOREGROUND_QUALIFICATION", raising=False)
    project, task = await _make_resident_poll_worker(db_session)
    row = await _add_message(
        db_session,
        project,
        from_type="user",
        status="open",
        message_type=MessageType.FEEDBACK,
        surface=MessageSurface.CONVERSATION,
        content="hi",
        sequence=1,
        resident_session_id="resident-session",
        created_at=_BASE,
    )

    response = await client.get(
        f"/api/agent/projects/{project.id}/messages?limit=20",
        headers=_headers(task, vm="agent-vm-resident-poll"),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["id"] for item in body["messages"]] == [str(row.id)]
    assert body["fast_message_ids"] == []
    # The legacy Workday claim path stays available for the un-marked row.
    assert body["claim_required"] is True


@pytest.mark.asyncio
async def test_resident_poll_fast_marks_conversation_rows_with_server_qualification(
    client: AsyncClient, db_session, monkeypatch
):
    monkeypatch.setenv(
        "CALL_FOREGROUND_QUALIFICATION", "claude-warm-stream-json:phase0-v1"
    )
    project, task = await _make_resident_poll_worker(db_session)
    row = await _add_message(
        db_session,
        project,
        from_type="user",
        status="open",
        message_type=MessageType.FEEDBACK,
        surface=MessageSurface.CONVERSATION,
        content="hi",
        sequence=1,
        resident_session_id="resident-session",
        created_at=_BASE,
    )

    response = await client.get(
        f"/api/agent/projects/{project.id}/messages?limit=20",
        headers=_headers(task, vm="agent-vm-resident-poll"),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["id"] for item in body["messages"]] == [str(row.id)]
    assert body["fast_message_ids"] == [str(row.id)]


@pytest.mark.asyncio
async def test_resident_poll_routes_explicit_direct_strategic_ops_to_workday(
    client: AsyncClient, db_session, monkeypatch
):
    """Mutation-only recovery must reach the lane that owns ./strategic."""

    monkeypatch.setenv(
        "CALL_FOREGROUND_QUALIFICATION", "claude-warm-stream-json:phase0-v1"
    )
    project, task = await _make_resident_poll_worker(db_session)
    first = await _add_message(
        db_session,
        project,
        from_type="user",
        status="open",
        message_type=MessageType.NOTE,
        surface=MessageSurface.CONVERSATION,
        content=(
            "GENERATION-7 MUTATION-ONLY RECOVERY. The project is paused. "
            "Use direct strategic ops only: do not consult/delegate, "
            "spawn/cancel/retry tasks, request qualification, promote, or "
            "unpause. Refetch the live problem-model revision. Directly call "
            "patch-problem-model with patch={} and return the raw JSON receipt."
        ),
        sequence=1,
        resident_session_id="resident-session",
        created_at=_BASE,
    )
    correction = await _add_message(
        db_session,
        project,
        from_type="user",
        status="open",
        message_type=MessageType.NOTE,
        surface=MessageSurface.CONVERSATION,
        content=(
            "CANONICAL READBACK CONTRADICTS YOUR CLAIM. You used zero tools "
            "and persisted nothing. Execute the four direct ops from operator "
            "message 002f852f now. Do not consult, summarize, claim staging, "
            "or reply until you have actual raw JSON receipts. Then refetch "
            "goal and qualification and quote exact canonical readback."
        ),
        sequence=2,
        resident_session_id="resident-session",
        created_at=_BASE + timedelta(seconds=1),
    )
    ordinary = await _add_message(
        db_session,
        project,
        from_type="user",
        status="open",
        message_type=MessageType.NOTE,
        surface=MessageSurface.CONVERSATION,
        content="Explain whether a private consultation would help this plan.",
        sequence=3,
        resident_session_id="resident-session",
        created_at=_BASE + timedelta(seconds=2),
    )
    informational = await _add_message(
        db_session,
        project,
        from_type="user",
        status="open",
        message_type=MessageType.NOTE,
        surface=MessageSurface.CONVERSATION,
        content="Explain why we should not perform direct ops without consultation.",
        sequence=4,
        resident_session_id="resident-session",
        created_at=_BASE + timedelta(seconds=3),
    )
    negated = await _add_message(
        db_session,
        project,
        from_type="user",
        status="open",
        message_type=MessageType.NOTE,
        surface=MessageSurface.CONVERSATION,
        content=(
            "Do not run ./strategic promote directly; explain what it would do."
        ),
        sequence=5,
        resident_session_id="resident-session",
        created_at=_BASE + timedelta(seconds=4),
    )
    quoted = await _add_message(
        db_session,
        project,
        from_type="user",
        status="open",
        message_type=MessageType.NOTE,
        surface=MessageSurface.CONVERSATION,
        content=(
            'Explain this instruction: "Use direct strategic ops only; do not '
            'consult; return raw JSON receipts."'
        ),
        sequence=6,
        resident_session_id="resident-session",
        created_at=_BASE + timedelta(seconds=5),
    )

    response = await client.get(
        f"/api/agent/projects/{project.id}/messages?limit=20",
        headers=_headers(task, vm="agent-vm-resident-poll"),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["id"] for item in body["messages"]] == [
        str(first.id),
        str(correction.id),
        str(ordinary.id),
        str(informational.id),
        str(negated.id),
        str(quoted.id),
    ]
    assert body["fast_message_ids"] == [
        str(ordinary.id),
        str(informational.id),
        str(negated.id),
        str(quoted.id),
    ]
    projected_by_id = {item["id"]: item for item in body["messages"]}
    assert projected_by_id[str(first.id)]["context"]["_direct_workday_execution"] is True
    assert (
        projected_by_id[str(correction.id)]["context"]["_direct_workday_execution"]
        is True
    )
    for row in (ordinary, informational, negated, quoted):
        assert "_direct_workday_execution" not in projected_by_id[str(row.id)]["context"]


@pytest.mark.asyncio
async def test_resident_poll_honors_structured_workday_execution_intent(
    client: AsyncClient, db_session, monkeypatch
):
    monkeypatch.setenv(
        "CALL_FOREGROUND_QUALIFICATION", "claude-warm-stream-json:phase0-v1"
    )
    project, task = await _make_resident_poll_worker(db_session)
    row = await _add_message(
        db_session,
        project,
        from_type="user",
        status="open",
        message_type=MessageType.NOTE,
        surface=MessageSurface.CONVERSATION,
        content="Apply the bounded state correction.",
        sequence=1,
        resident_session_id="resident-session",
        context={
            "operator_intent": {
                "kind": "strategic_mutation",
                "action": "execute",
                "lane_owner": "workday",
            }
        },
        created_at=_BASE,
    )

    response = await client.get(
        f"/api/agent/projects/{project.id}/messages?limit=20",
        headers=_headers(task, vm="agent-vm-resident-poll"),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["id"] for item in body["messages"]] == [str(row.id)]
    assert body["fast_message_ids"] == []
    assert body["messages"][0]["context"]["_direct_workday_execution"] is True


@pytest.mark.asyncio
async def test_resident_poll_rejects_caller_supplied_direct_execution_budget(
    client: AsyncClient, db_session, monkeypatch
):
    """Only the server routing predicate may grant the deeper Workday budget."""

    monkeypatch.setenv(
        "CALL_FOREGROUND_QUALIFICATION", "claude-warm-stream-json:phase0-v1"
    )
    project, task = await _make_resident_poll_worker(db_session)
    row = await _add_message(
        db_session,
        project,
        from_type="user",
        status="open",
        message_type=MessageType.NOTE,
        surface=MessageSurface.CONVERSATION,
        content="Explain what the bounded correction would do.",
        sequence=1,
        resident_session_id="resident-session",
        context={"_direct_workday_execution": True},
        created_at=_BASE,
    )

    response = await client.get(
        f"/api/agent/projects/{project.id}/messages?limit=20",
        headers=_headers(task, vm="agent-vm-resident-poll"),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["fast_message_ids"] == [str(row.id)]
    assert "_direct_workday_execution" not in body["messages"][0]["context"]


@pytest.mark.asyncio
@pytest.mark.parametrize("claim_is_live", [True, False])
async def test_resident_poll_preserves_fast_owned_direct_op_claim(
    client: AsyncClient, db_session, monkeypatch, claim_is_live
):
    """A rolling deploy cannot hand a Fast-owned row to legacy Workday."""

    monkeypatch.setenv(
        "CALL_FOREGROUND_QUALIFICATION", "claude-warm-stream-json:phase0-v1"
    )
    project, task = await _make_resident_poll_worker(db_session)
    claim_id = uuid.uuid4()
    row = await _add_message(
        db_session,
        project,
        from_type="user",
        status="open",
        message_type=MessageType.NOTE,
        surface=MessageSurface.CONVERSATION,
        content=(
            "Use direct strategic ops only: do not consult/delegate. Execute "
            "the bounded mutation and return the raw JSON receipt."
        ),
        sequence=1,
        resident_session_id="resident-session",
        handling_state=MessageHandlingState.CLAIMED,
        handled_by_task_id=task.id,
        conversation_claim_id=claim_id,
        conversation_claim_generation=2 if claim_is_live else 1,
        conversation_claim_incarnation=uuid.uuid4(),
        conversation_claim_expires_at=(
            _utcnow() + timedelta(minutes=1)
            if claim_is_live
            else _utcnow() - timedelta(minutes=1)
        ),
        conversation_claim_attempt=1,
        context={
            "_conversation_public_reply_owner": "fast",
            "_conversation_claim_idempotency_key": "direct-op-fast-turn",
        },
        created_at=_BASE,
    )

    response = await client.get(
        f"/api/agent/projects/{project.id}/messages?limit=20",
        headers=_headers(task, vm="agent-vm-resident-poll"),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["id"] for item in body["messages"]] == [str(row.id)]
    assert body["fast_message_ids"] == [str(row.id)]
    assert body["fast_claim_groups"][0]["message_ids"] == [str(row.id)]


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
    resident = dict(project.vision["_resident_strategic_session"])
    resident["worker_incarnation_id"] = first_incarnation
    resident["worker_incarnation_expires_at"] = (
        datetime.now(timezone.utc) + timedelta(minutes=5)
    ).isoformat()
    project.vision = {
        **project.vision,
        "_resident_strategic_session": resident,
    }
    flag_modified(project, "vision")
    db_session.add(project)
    await db_session.commit()

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
