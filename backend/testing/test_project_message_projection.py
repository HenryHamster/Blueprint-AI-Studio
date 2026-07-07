import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlmodel import select

from app.models import MessageType, Project, ProjectMember, ProjectMessage
from app.project_message_projection import (
    admin_message_dict,
    admin_message_list_dict,
    agent_fetch_message_dict,
    agent_thread_message_dict,
    legacy_direction_message_dict,
    message_preview_dict,
)
from testing.factories import make_task

TEST_USER_ID = "test-user-123"


def _message(**overrides) -> ProjectMessage:
    base = {
        "id": uuid.UUID("11111111-1111-4111-8111-111111111111"),
        "project_id": uuid.UUID("22222222-2222-4222-8222-222222222222"),
        "message_type": MessageType.BLOCKER,
        "from_type": "system",
        "from_task_id": uuid.UUID("33333333-3333-4333-8333-333333333333"),
        "content": "Investigate the stalled rollout.",
        "context": None,
        "status": "linked",
        "resolution": "Owned by direction d-1",
        "resolved_at": datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        "created_at": datetime(2026, 1, 1, 3, 4, 5, tzinfo=timezone.utc),
    }
    base.update(overrides)
    return ProjectMessage(**base)


async def _make_project(session) -> Project:
    project = Project(name="Message Contract", user_id=TEST_USER_ID)
    session.add(project)
    await session.commit()
    await session.refresh(project)
    session.add(ProjectMember(project_id=project.id, user_id=TEST_USER_ID, role="owner"))
    await session.commit()
    return project


def test_admin_message_dict_normalizes_enum_timestamp_and_empty_context():
    msg = _message()

    data = admin_message_dict(msg)

    assert data["id"] == str(msg.id)
    assert data["project_id"] == str(msg.project_id)
    assert data["message_type"] == "BLOCKER"
    assert data["from_type"] == "system"
    assert data["from_task_id"] == str(msg.from_task_id)
    assert data["context"] == {}
    assert data["status"] == "linked"
    assert data["resolution"] == "Owned by direction d-1"
    assert data["created_at"] == "2026-01-01T03:04:05+00:00"
    assert data["resolved_at"] == "2026-01-02T03:04:05+00:00"


def test_compatibility_wrappers_keep_existing_public_aliases():
    msg = _message(message_type=MessageType.FEEDBACK, from_type="user", status="open")

    legacy = legacy_direction_message_dict(msg)
    thread = agent_thread_message_dict(msg)
    fetched = agent_fetch_message_dict(msg, convergence_link={"kind": "task", "id": "t-1"})

    assert legacy["type"] == "FEEDBACK"
    assert legacy["from"] == "user"
    assert legacy["context"] == {}
    assert "message_type" not in legacy

    assert thread["type"] == "FEEDBACK"
    assert thread["from"] == "user"
    assert thread["resolved_at"] == "2026-01-02T03:04:05+00:00"

    assert fetched["type"] == "FEEDBACK"
    assert fetched["from_type"] == "user"
    assert fetched["convergence_link"] == {"kind": "task", "id": "t-1"}


def test_message_preview_compacts_content_and_context_subset():
    msg = _message(
        content="A" * 400,
        context={
            "event_type": "blocker_linked",
            "direction_id": "d-" + ("x" * 200),
            "huge": "not returned",
        },
    )

    data = message_preview_dict(msg)

    assert data["message_type"] == "BLOCKER"
    assert len(data["content"]) <= 280
    assert data["content"].endswith("…")
    assert data["context"]["event_type"] == "blocker_linked"
    assert len(data["context"]["direction_id"]) <= 120
    assert data["context"]["direction_id"].endswith("…")
    assert "huge" not in data["context"]


def test_message_preview_preserves_bounded_strategic_session_snapshot():
    msg = _message(
        context={
            "event_type": "blocker_linked",
            "strategic_session": {
                "version": 1,
                "interaction_mode": "continuous",
                "active_surface": "pipeline",
                "session_started_at": "2026-06-29T12:00:00Z",
                "captured_at": "2026-06-29T12:05:00Z",
                "context_package_id": "strategic-context:proj-1",
                "selected_refs": [
                    {"kind": "canvas", "id": "strategic-canvas:proj-1", "label": "Context map"},
                    {"kind": "canvas", "id": "strategic-canvas:proj-1", "label": "Duplicate"},
                    {"kind": "artifact", "id": "artifact-1", "label": "x" * 500, "storage_uri": "gs://secret"},
                    {"kind": "", "id": "ignored"},
                ],
                "storage_uri": "gs://secret",
            },
            "huge": "not returned",
        },
    )

    data = message_preview_dict(msg)

    assert data["context"]["event_type"] == "blocker_linked"
    assert "huge" not in data["context"]
    session = data["context"]["strategic_session"]
    assert session["interaction_mode"] == "continuous"
    assert session["active_surface"] == "pipeline"
    assert session["context_package_id"] == "strategic-context:proj-1"
    assert session["captured_at"] == "2026-06-29T12:05:00Z"
    assert session["selected_refs"] == [
        {"kind": "canvas", "id": "strategic-canvas:proj-1", "label": "Context map"},
        {"kind": "artifact", "id": "artifact-1", "label": "x" * 160},
    ]
    assert "storage_uri" not in session


@pytest.mark.asyncio
async def test_admin_messages_endpoint_keeps_full_message_contract(
    client: AsyncClient,
    db_session,
):
    project = await _make_project(db_session)
    task_id = uuid.UUID("44444444-4444-4444-8444-444444444444")
    db_session.add(
        make_task(
            id=task_id,
            project_id=project.id,
            title="Message owner task",
        )
    )
    await db_session.flush()
    msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.BUG_REPORT,
        from_type="agent",
        from_task_id=task_id,
        content="Full admin route contract",
        context=None,
        status="linked",
        resolution="Owned by task",
        resolved_at=datetime(2026, 2, 3, 4, 5, 6, tzinfo=timezone.utc),
        created_at=datetime(2026, 2, 1, 4, 5, 6, tzinfo=timezone.utc),
    )
    db_session.add(msg)
    await db_session.commit()

    resp = await client.get(
        f"/api/admin/messages?project_id={project.id}&status=all"
    )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total"] == 1
    item = data["messages"][0]
    assert set(item) == {
        "id",
        "project_id",
        "message_type",
        "from_type",
        "from_task_id",
        "content",
        "context",
        "status",
        "resolution",
        "resolved_at",
        "created_at",
    }
    assert item["message_type"] == "BUG_REPORT"
    assert item["from_type"] == "agent"
    assert item["from_task_id"] == "44444444-4444-4444-8444-444444444444"
    assert item["context"] == {}
    assert item["status"] == "linked"
    assert item["resolution"] == "Owned by task"
    assert item["resolved_at"].startswith("2026-02-03T04:05:06")


@pytest.mark.asyncio
async def test_legacy_project_messages_endpoint_keeps_type_from_aliases(
    client: AsyncClient,
    db_session,
):
    project = await _make_project(db_session)
    msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.QUESTION,
        from_type="strategic",
        content="Legacy alias contract",
        context=None,
        status="open",
        created_at=datetime(2026, 2, 1, 4, 5, 6, tzinfo=timezone.utc),
    )
    db_session.add(msg)
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/messages")

    assert resp.status_code == 200, resp.text
    item = resp.json()[0]
    assert set(item) == {
        "id",
        "type",
        "content",
        "from",
        "status",
        "context",
        "created_at",
    }
    assert item["type"] == "QUESTION"
    assert item["from"] == "strategic"
    assert "message_type" not in item
    assert "from_type" not in item
    assert item["context"] == {}


@pytest.mark.asyncio
async def test_legacy_project_message_create_snapshots_strategic_session(
    client: AsyncClient,
    db_session,
):
    project = await _make_project(db_session)

    with patch(
        "app.routers.directions.broadcast_to_project",
        new_callable=AsyncMock,
    ) as broadcast:
        resp = await client.post(
            f"/api/admin/projects/{project.id}/messages",
            json={
                "message_type": "NOTE",
                "content": "Keep this canvas around.",
                "workbench_context": {
                    "interaction_mode": "continuous",
                    "active_surface": "project",
                    "selected_refs": [
                        {"kind": "canvas", "id": "strategic-canvas:proj-1", "label": "Context map"}
                    ],
                },
            },
        )

    assert resp.status_code == 200, resp.text
    broadcast.assert_awaited_once()
    assert broadcast.await_args.args[:2] == (str(project.id), "message_created")
    event_payload = broadcast.await_args.args[2]
    assert event_payload["schema_version"] == 1
    assert event_payload["project_id"] == str(project.id)
    assert event_payload["resource"] == "project_message"
    assert event_payload["action"] == "created"
    assert event_payload["refetch"] == {"surfaces": ["messages", "strategic_session"]}
    assert event_payload["message_id"] == resp.json()["id"]
    assert event_payload["type"] == "NOTE"
    assert event_payload["from"] == "user"
    assert event_payload["message"]["id"] == resp.json()["id"]
    assert event_payload["message"]["status"] == "open"
    msg = (
        await db_session.execute(
            select(ProjectMessage).where(ProjectMessage.id == uuid.UUID(resp.json()["id"]))
        )
    ).scalar_one()
    session = (msg.context or {})["strategic_session"]
    assert session["interaction_mode"] == "continuous"
    assert session["active_surface"] == "project"
    assert session["selected_refs"] == [
        {"kind": "canvas", "id": "strategic-canvas:proj-1", "label": "Context map"}
    ]


@pytest.mark.asyncio
async def test_legacy_project_message_create_normalizes_message_type_case(
    client: AsyncClient,
    db_session,
):
    project = await _make_project(db_session)

    resp = await client.post(
        f"/api/admin/projects/{project.id}/messages",
        json={
            "message_type": "feedback",
            "content": "Lowercase type should match the canonical message route.",
        },
    )

    assert resp.status_code == 200, resp.text
    msg = (
        await db_session.execute(
            select(ProjectMessage).where(
                ProjectMessage.id == uuid.UUID(resp.json()["id"])
            )
        )
    ).scalar_one()
    assert msg.message_type == MessageType.FEEDBACK
    assert msg.content == "Lowercase type should match the canonical message route."


@pytest.mark.asyncio
async def test_legacy_project_message_create_rejects_oversized_workbench_context(
    client: AsyncClient,
    db_session,
):
    project = await _make_project(db_session)

    resp = await client.post(
        f"/api/admin/projects/{project.id}/messages",
        json={
            "message_type": "NOTE",
            "content": "Too much context.",
            "workbench_context": {
                "selected_refs": [{"kind": "task", "id": "x" * 60_000}],
            },
        },
    )

    assert resp.status_code == 400
    assert "Workbench context too large" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_strategic_open_message_payload_keeps_agent_aliases(db_session):
    from app.strategic_spawning import _fetch_open_messages

    project = await _make_project(db_session)
    open_msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.BLOCKER,
        from_type="agent",
        content="Open payload contract",
        context=None,
        status="open",
        created_at=datetime(2026, 2, 1, 4, 5, 6, tzinfo=timezone.utc),
    )
    linked_msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.BUG_REPORT,
        from_type="agent",
        content="Already owned",
        status="linked",
        created_at=datetime(2026, 2, 1, 4, 6, 6, tzinfo=timezone.utc),
    )
    db_session.add(open_msg)
    db_session.add(linked_msg)
    await db_session.commit()

    payload = await _fetch_open_messages(db_session, project.id)

    assert len(payload) == 1
    item = payload[0]
    assert item["type"] == "BLOCKER"
    assert item["from"] == "agent"
    assert item["context"] == {}
    assert item["status"] == "open"
    assert item["resolved_at"] is None
    assert "message_type" not in item
    assert "from_type" not in item


def test_admin_message_list_dict_returns_compact_context():
    """admin_message_list_dict preserves UI-needed context keys and strips large keys."""
    msg_with_context = _message(
        context={"event_type": "task_failed", "severity": "error", "strategic_session": {"version": 1}}
    )
    msg_no_context = _message(context=None)

    data_with = admin_message_list_dict(msg_with_context)
    data_none = admin_message_list_dict(msg_no_context)

    # UI-needed keys are preserved
    assert data_with["context"]["event_type"] == "task_failed"
    assert data_with["context"]["severity"] == "error"
    # Large keys not in the compact set are stripped
    assert "strategic_session" not in data_with["context"]
    # No context → empty dict
    assert data_none["context"] == {}
    # All other fields are still present and correct
    assert data_with["message_type"] == "BLOCKER"
    assert data_with["from_type"] == "system"
    assert data_with["status"] == "linked"


@pytest.mark.asyncio
async def test_admin_messages_list_strips_large_context_keys(
    client: AsyncClient,
    db_session,
):
    """List endpoint returns compact context (UI-needed keys only), stripping large keys like strategic_session."""
    project = await _make_project(db_session)
    msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.BUG_REPORT,
        from_type="system",
        content="Message with large context",
        context={"event_type": "task_failed", "strategic_session": {"version": 1, "huge": "x" * 10_000}},
        status="open",
    )
    db_session.add(msg)
    await db_session.commit()

    resp = await client.get(f"/api/admin/messages?project_id={project.id}")

    assert resp.status_code == 200, resp.text
    items = resp.json()["messages"]
    assert len(items) == 1
    # event_type is a UI-needed key and must be present
    assert items[0]["context"]["event_type"] == "task_failed"
    # strategic_session is a large key excluded from the compact set
    assert "strategic_session" not in items[0]["context"]
    assert items[0]["content"] == "Message with large context"
