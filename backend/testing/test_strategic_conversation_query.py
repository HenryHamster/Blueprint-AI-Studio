"""Focused coverage for the shared Strategic conversation read facade."""

from datetime import datetime, timedelta, timezone
import inspect
import json
import uuid

import pytest
import sqlalchemy as sa
from fastapi import HTTPException
from httpx import AsyncClient
from sqlalchemy.dialects import postgresql, sqlite

from app.auth import create_agent_token
from app.models import (
    MessageHandlingState,
    MessageSurface,
    MessageType,
    Project,
    ProjectMessage,
    Task,
    TaskStatus,
)
from app.routers.agent_strategic_fetch import (
    _RESIDENT_CONVERSATION_AGGREGATE_TEXT_CAP,
    _RESIDENT_CONVERSATION_RESPONSE_BYTES_CAP,
    _authorize_resident_conversation_reader,
    _fit_resident_item_to_serialized_budget,
)
from app.strategic_conversation_query import (
    _HasVerifiedCoveredInput,
    query_strategic_conversation,
    strategic_conversation_predicate,
)

_VM = "strategic-conversation-query-vm"
_SESSION_ID = "resident-conversation-query-session"
_GENERATION = 3
_INCARNATION_ID = uuid.UUID("11111111-2222-4333-8444-555555555555")


def test_serialized_budget_truncation_keeps_refetch_pointer():
    message_id = str(uuid.uuid4())
    project_id = uuid.uuid4()
    item = {
        "id": message_id,
        "content": "\u0000" * 400,
        "resolution": None,
        "content_truncated": False,
        "resolution_truncated": False,
        "context_truncated": False,
    }

    fitted = _fit_resident_item_to_serialized_budget(
        item,
        item_budget=500,
        project_id=project_id,
        message_id=message_id,
    )

    assert fitted["content_truncated"] is True
    assert fitted["refetch"]["path"].endswith(f"/messages/{message_id}")
    assert (
        len(
            json.dumps(
                fitted,
                separators=(",", ":"),
                default=str,
                ensure_ascii=False,
            ).encode("utf-8")
        )
        <= 500
    )


def test_resident_reader_locks_project_then_task_and_checks_live_status():
    source = inspect.getsource(_authorize_resident_conversation_reader)
    project_lock = source.index("select(Project)")
    task_lock = source.index("select(Task)")
    live_status = source.index("task.status != TaskStatus.IN_PROGRESS")
    assert source.count(".with_for_update()") == 2
    assert project_lock < task_lock < live_status


async def _make_resident(db_session, *, project: Project | None = None):
    project = project or Project(
        name="Strategic conversation query",
        user_id="conversation-query-user",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = Task(
        project_id=project.id,
        payloads=[{}],
        vm_instance_id=_VM,
        status=TaskStatus.IN_PROGRESS,
        context={
            "_resident_session": True,
            "strategic_session_id": _SESSION_ID,
            "_resident_worker_generation": _GENERATION,
        },
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    project.vision = {
        "_resident_strategic_session": {
            "status": "active",
            "session_id": _SESSION_ID,
            "task_id": str(task.id),
            "worker_generation": _GENERATION,
            "worker_incarnation_id": str(_INCARNATION_ID),
            "worker_incarnation_expires_at": (
                datetime.now(timezone.utc) + timedelta(minutes=5)
            ).isoformat(),
            "stop_requested": False,
        }
    }
    db_session.add(project)
    await db_session.commit()
    return project, task


def _headers(task: Task) -> dict[str, str]:
    return {"x-agent-token": create_agent_token(str(task.id), _VM)}


def _url(project_id: uuid.UUID, **params: object) -> str:
    query = {
        "resident_session_id": _SESSION_ID,
        "worker_generation": str(_GENERATION),
        "worker_incarnation_id": str(_INCARNATION_ID),
        **{key: str(value) for key, value in params.items()},
    }
    encoded = "&".join(f"{key}={value}" for key, value in query.items())
    return f"/api/agent/projects/{project_id}/strategic-conversation?{encoded}"


def test_covered_message_proof_compiles_for_postgres_and_sqlite():
    expression = _HasVerifiedCoveredInput(ProjectMessage.context)
    postgres_sql = str(expression.compile(dialect=postgresql.dialect()))
    sqlite_sql = str(expression.compile(dialect=sqlite.dialect()))

    assert "jsonb_array_elements_text" in postgres_sql
    assert "covered.value ~ '^[0-9a-f]" in postgres_sql
    assert "covered.value ~*" not in postgres_sql
    assert "covered_input.project_id = projectmessage.project_id" in postgres_sql
    assert "json_each" in sqlite_sql
    assert "covered.value GLOB '[0-9a-f]" in sqlite_sql
    assert "covered_input.project_id = projectmessage.project_id" in sqlite_sql


def test_covered_message_proof_executes_against_sqlite_guid_storage():
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    message = sa.Table(
        "projectmessage",
        metadata,
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("project_id", sa.String(32), nullable=False),
        sa.Column("surface", sa.String(), nullable=False),
        sa.Column("from_type", sa.String(), nullable=False),
        sa.Column("reply_to_message_id", sa.String(32)),
        sa.Column("context", sa.JSON()),
    )
    metadata.create_all(engine)
    project_id, input_id, reply_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            message.insert(),
            [
                {
                    "id": input_id.hex,
                    "project_id": project_id.hex,
                    "surface": "conversation",
                    "from_type": "user",
                    "context": {},
                },
                {
                    "id": reply_id.hex,
                    "project_id": project_id.hex,
                    "surface": "conversation",
                    "from_type": "strategic",
                    "context": {
                        "audience": "strategist-internal",
                        "_resident_reply_covered_message_ids": [
                            str(input_id)
                        ],
                    },
                },
            ],
        )
        predicate_sql = str(
            strategic_conversation_predicate().compile(
                dialect=sqlite.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        visible = connection.execute(
            sa.text(
                "SELECT id FROM projectmessage "
                f"WHERE id=:reply_id AND {predicate_sql}"
            ),
            {"reply_id": reply_id.hex},
        ).scalar_one_or_none()
    assert visible == reply_id.hex


@pytest.mark.asyncio
async def test_conversation_query_excludes_legacy_mode_knob_cut_rows(db_session):
    project = Project(
        name="CUT recommendation history filter",
        user_id="conversation-query-user",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    cut_row = ProjectMessage(
        project_id=project.id,
        sequence=1,
        surface=MessageSurface.CONVERSATION,
        message_type=MessageType.NOTE,
        from_type="system",
        content="Mode-knob recommendation from an older deployment",
        context={
            "event_type": "mode_knob_cut_recommended",
            "audience": "operator-fyi",
        },
    )
    operator_row = ProjectMessage(
        project_id=project.id,
        sequence=2,
        surface=MessageSurface.CONVERSATION,
        message_type=MessageType.NOTE,
        from_type="user",
        content="Keep this actual conversation row",
        context={},
    )
    db_session.add_all([cut_row, operator_row])
    await db_session.commit()

    page = await query_strategic_conversation(db_session, project.id, limit=20)

    assert [row.id for row in page.rows] == [operator_row.id]


@pytest.mark.asyncio
async def test_resident_conversation_query_includes_open_and_handled_history(
    client: AsyncClient,
    db_session,
):
    project, task = await _make_resident(db_session)
    question = ProjectMessage(
            project_id=project.id,
            sequence=1,
            surface=MessageSurface.CONVERSATION,
            message_type=MessageType.NOTE,
            from_type="user",
            content="open current question",
            status="open",
            handling_state=MessageHandlingState.PENDING,
        )
    rows = [
        question,
        ProjectMessage(
            project_id=project.id,
            sequence=2,
            surface=MessageSurface.CONVERSATION,
            message_type=MessageType.NOTE,
            from_type="strategic",
            content="handled historical answer",
            status="resolved",
            handling_state=MessageHandlingState.RESPONDED,
            reply_to_message_id=question.id,
            context={"message_role": "operator_reply"},
        ),
        ProjectMessage(
            project_id=project.id,
            sequence=3,
            surface=MessageSurface.CONVERSATION,
            message_type=MessageType.NOTE,
            from_type="system",
            content="internal bookkeeping",
            context={"audience": "strategist-internal"},
        ),
        ProjectMessage(
            project_id=project.id,
            sequence=4,
            surface=MessageSurface.ACTIVITY,
            message_type=MessageType.NOTE,
            from_type="strategic",
            content="activity row",
        ),
    ]
    db_session.add_all(rows)
    await db_session.commit()

    response = await client.get(_url(project.id), headers=_headers(task))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["order"] == "oldest_first"
    assert [item["sequence"] for item in body["items"]] == [1, 2]
    assert [item["status"] for item in body["items"]] == ["open", "resolved"]
    assert [item["handling_state"] for item in body["items"]] == [
        "pending",
        "responded",
    ]


@pytest.mark.asyncio
async def test_resident_conversation_query_reuses_cursor_and_search_semantics(
    client: AsyncClient,
    db_session,
):
    project, task = await _make_resident(db_session)
    db_session.add_all(
        [
            ProjectMessage(
                project_id=project.id,
                sequence=sequence,
                surface=MessageSurface.CONVERSATION,
                message_type=MessageType.NOTE,
                from_type="user",
                content=(
                    "formula eta_value"
                    if sequence == 2
                    else f"conversation {sequence}"
                ),
            )
            for sequence in range(1, 6)
        ]
    )
    await db_session.commit()

    latest = await client.get(
        _url(project.id, limit=2),
        headers=_headers(task),
    )
    assert latest.status_code == 200, latest.text
    assert [item["sequence"] for item in latest.json()["items"]] == [4, 5]
    assert latest.json()["next_before_sequence"] == 4
    assert latest.json()["has_more"] is True

    older = await client.get(
        _url(project.id, before_sequence=4, limit=2),
        headers=_headers(task),
    )
    assert older.status_code == 200, older.text
    assert [item["sequence"] for item in older.json()["items"]] == [2, 3]

    searched = await client.get(
        _url(project.id, search="eta_value"),
        headers=_headers(task),
    )
    assert searched.status_code == 200, searched.text
    assert [item["sequence"] for item in searched.json()["items"]] == [2]

    searched_older = await client.get(
        _url(project.id, search="conversation", before_sequence=5, limit=2),
        headers=_headers(task),
    )
    assert searched_older.status_code == 200, searched_older.text
    assert [item["sequence"] for item in searched_older.json()["items"]] == [3, 4]

    invalid = await client.get(
        _url(project.id, before_sequence=4, after_sequence=1),
        headers=_headers(task),
    )
    assert invalid.status_code == 400


@pytest.mark.asyncio
async def test_resident_conversation_query_fences_project_session_and_generation(
    client: AsyncClient,
    db_session,
):
    project, task = await _make_resident(db_session)
    other_project = Project(name="Other project", user_id="other-user")
    db_session.add(other_project)
    await db_session.commit()
    await db_session.refresh(other_project)

    cross_project = await client.get(
        _url(other_project.id),
        headers=_headers(task),
    )
    assert cross_project.status_code == 404

    wrong_session = await client.get(
        _url(project.id).replace(_SESSION_ID, "wrong-session"),
        headers=_headers(task),
    )
    assert wrong_session.status_code == 409

    wrong_generation = await client.get(
        _url(project.id).replace(
            f"worker_generation={_GENERATION}",
            "worker_generation=2",
        ),
        headers=_headers(task),
    )
    assert wrong_generation.status_code == 409

    ordinary_task = Task(
        project_id=project.id,
        payloads=[{}],
        vm_instance_id=_VM,
    )
    db_session.add(ordinary_task)
    await db_session.commit()
    await db_session.refresh(ordinary_task)
    ordinary = await client.get(
        _url(project.id),
        headers=_headers(ordinary_task),
    )
    assert ordinary.status_code == 403

    task.status = TaskStatus.COMPLETED
    db_session.add(task)
    await db_session.commit()
    terminal = await client.get(_url(project.id), headers=_headers(task))
    # The shared agent-token dependency rejects terminal tasks before this
    # route's narrower Resident authorization runs.
    assert terminal.status_code == 401
    task.status = TaskStatus.IN_PROGRESS
    db_session.add(task)
    await db_session.commit()

    project.vision = {
        "_resident_strategic_session": {
            "status": "active",
            "session_id": _SESSION_ID,
            "task_id": str(task.id),
            "worker_generation": _GENERATION + 1,
            "worker_incarnation_id": str(_INCARNATION_ID),
            "worker_incarnation_expires_at": (
                datetime.now(timezone.utc) + timedelta(minutes=5)
            ).isoformat(),
        }
    }
    db_session.add(project)
    await db_session.commit()
    superseded = await client.get(_url(project.id), headers=_headers(task))
    assert superseded.status_code == 409


@pytest.mark.asyncio
async def test_resident_conversation_query_rejects_invalid_or_expired_incarnation(
    client: AsyncClient,
    db_session,
):
    project, task = await _make_resident(db_session)

    wrong_incarnation = await client.get(
        _url(project.id).replace(str(_INCARNATION_ID), str(uuid.uuid4())),
        headers=_headers(task),
    )
    assert wrong_incarnation.status_code == 409

    resident = dict(project.vision["_resident_strategic_session"])
    resident["worker_incarnation_expires_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()
    project.vision = {"_resident_strategic_session": resident}
    db_session.add(project)
    await db_session.commit()
    expired = await client.get(_url(project.id), headers=_headers(task))
    assert expired.status_code == 409

    with pytest.raises(HTTPException) as invalid_sub:
        await _authorize_resident_conversation_reader(
            db_session,
            project.id,
            {"sub": "not-a-uuid"},
            resident_session_id=_SESSION_ID,
            worker_generation=_GENERATION,
            worker_incarnation_id=_INCARNATION_ID,
        )
    assert invalid_sub.value.status_code == 401


@pytest.mark.asyncio
async def test_resident_conversation_query_excludes_unproven_internal_replies(
    client: AsyncClient,
    db_session,
):
    project, task = await _make_resident(db_session)
    covered_input = ProjectMessage(
        id=uuid.uuid4(),
        project_id=project.id,
        sequence=9,
        surface=MessageSurface.CONVERSATION,
        message_type=MessageType.NOTE,
        from_type="user",
        content="the same-project operator input covered by this reply",
    )
    other_project = Project(name="Other proof source", user_id="other-user")
    db_session.add(other_project)
    await db_session.commit()
    await db_session.refresh(other_project)
    cross_project_input = ProjectMessage(
        project_id=other_project.id,
        sequence=1,
        surface=MessageSurface.CONVERSATION,
        message_type=MessageType.NOTE,
        from_type="user",
        content="an input from another project",
    )
    db_session.add(cross_project_input)
    await db_session.commit()
    db_session.add_all(
        [
            ProjectMessage(
                project_id=project.id,
                sequence=1,
                surface=MessageSurface.CONVERSATION,
                message_type=MessageType.NOTE,
                from_type="strategic",
                content="event type alone is not reply proof",
                context={
                    "audience": "strategist-internal",
                    "event_type": "strategic_message",
                },
            ),
            ProjectMessage(
                project_id=project.id,
                sequence=2,
                surface=MessageSurface.CONVERSATION,
                message_type=MessageType.NOTE,
                from_type="strategic",
                content="empty receipt is not reply proof",
                context={
                    "audience": "strategist-internal",
                    "_resident_reply_covered_message_ids": [],
                },
            ),
            ProjectMessage(
                project_id=project.id,
                sequence=3,
                surface=MessageSurface.CONVERSATION,
                message_type=MessageType.NOTE,
                from_type="strategic",
                content="malformed receipt is not reply proof",
                context={
                    "audience": "strategist-internal",
                    "_resident_reply_covered_message_ids": ["not-a-uuid"],
                },
            ),
            ProjectMessage(
                project_id=project.id,
                sequence=4,
                surface=MessageSurface.CONVERSATION,
                message_type=MessageType.NOTE,
                from_type="strategic",
                content="hyphen shape is not UUID proof",
                context={
                    "audience": "strategist-internal",
                    "_resident_reply_covered_message_ids": [
                        "zzzzzzzz-zzzz-zzzz-zzzz-zzzzzzzzzzzz"
                    ],
                },
            ),
            ProjectMessage(
                project_id=project.id,
                sequence=5,
                surface=MessageSurface.CONVERSATION,
                message_type=MessageType.NOTE,
                from_type="strategic",
                content="same-project covered input remains visible",
                context={
                    "audience": "strategist-internal",
                    "_resident_reply_covered_message_ids": [
                        "not-a-uuid",
                        str(covered_input.id),
                    ],
                },
            ),
            ProjectMessage(
                project_id=project.id,
                sequence=6,
                surface=MessageSurface.CONVERSATION,
                message_type=MessageType.NOTE,
                from_type="strategic",
                content="mutable role marker is not reply proof",
                context={
                    "audience": "strategist-internal",
                    "message_role": "operator_reply",
                },
            ),
            ProjectMessage(
                project_id=project.id,
                sequence=7,
                surface=MessageSurface.CONVERSATION,
                message_type=MessageType.NOTE,
                from_type="strategic",
                content="cross-project covered input is not reply proof",
                context={
                    "audience": "strategist-internal",
                    "_resident_reply_covered_message_ids": [
                        str(cross_project_input.id)
                    ],
                },
            ),
        ]
    )
    await db_session.commit()
    # PostgreSQL's legacy compatibility trigger owns the per-project sequence
    # allocator.  Insert the deliberately later covered input after the
    # adversarial replies, so its canonical sequence is allocated after 1..7
    # rather than being silently rewritten to 1 and colliding with the first
    # reply fixture. A cross-project reply_to_message_id is not seeded here:
    # the database correctly rejects that impossible canonical state.
    db_session.add(covered_input)
    await db_session.commit()
    await db_session.refresh(covered_input)

    response = await client.get(_url(project.id), headers=_headers(task))
    assert response.status_code == 200, response.text
    assert [item["sequence"] for item in response.json()["items"]] == [
        5,
        covered_input.sequence,
    ]


@pytest.mark.asyncio
async def test_resident_conversation_target_cursors_and_payload_bounds_are_truthful(
    client: AsyncClient,
    db_session,
):
    project, task = await _make_resident(db_session)
    rows = [
        ProjectMessage(
            project_id=project.id,
            sequence=sequence,
            surface=MessageSurface.CONVERSATION,
            message_type=MessageType.NOTE,
            from_type="user",
            content=("x" * 10_000) if sequence == 3 else f"row {sequence}",
            resolution=("r" * 3_000) if sequence == 3 else None,
        )
        for sequence in range(1, 8)
    ]
    db_session.add_all(rows)
    await db_session.commit()

    response = await client.get(
        _url(project.id, include_message_id=rows[2].id, limit=3),
        headers=_headers(task),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["sequence"] for item in body["items"]] == [2, 3, 4]
    assert body["has_more"] is True
    assert body["next_before_sequence"] == 2
    assert body["next_after_sequence"] == 4
    assert body["response_text_cap"] == _RESIDENT_CONVERSATION_AGGREGATE_TEXT_CAP
    target = body["items"][1]
    assert target["content_truncated"] is True
    assert target["resolution_truncated"] is True
    assert target["refetch"]["path"].endswith(f"/messages/{rows[2].id}")
    total_text = sum(
        len(item["content"] or "") + len(item["resolution"] or "")
        for item in body["items"]
    )
    assert total_text <= _RESIDENT_CONVERSATION_AGGREGATE_TEXT_CAP
    assert len(json.dumps(body, separators=(",", ":")).encode("utf-8")) <= (
        _RESIDENT_CONVERSATION_RESPONSE_BYTES_CAP
    )


@pytest.mark.asyncio
async def test_resident_conversation_response_byte_cap_includes_context(
    client: AsyncClient,
    db_session,
):
    project, task = await _make_resident(db_session)
    db_session.add_all(
        [
            ProjectMessage(
                project_id=project.id,
                sequence=sequence,
                surface=MessageSurface.CONVERSATION,
                message_type=MessageType.NOTE,
                from_type="user",
                content="😀" * 2_000,
                context={
                    "source_refs": [
                        {"id": str(uuid.uuid4()), "title": "x" * 500}
                        for _ in range(12)
                    ]
                },
            )
            for sequence in range(1, 51)
        ]
    )
    await db_session.commit()

    response = await client.get(
        _url(project.id, limit=50), headers=_headers(task)
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["response_bytes_cap"] == _RESIDENT_CONVERSATION_RESPONSE_BYTES_CAP
    assert len(response.content) <= _RESIDENT_CONVERSATION_RESPONSE_BYTES_CAP
    retained_text_bytes = sum(
        len((item["content"] or "").encode("utf-8"))
        + len((item["resolution"] or "").encode("utf-8"))
        for item in body["items"]
    )
    assert retained_text_bytes <= _RESIDENT_CONVERSATION_AGGREGATE_TEXT_CAP
    assert body["truncated_item_count"] > 0
    assert any(item["context_truncated"] for item in body["items"])
    assert any(
        item.get("context_refetch_available") is False
        for item in body["items"]
    )


@pytest.mark.asyncio
async def test_resident_conversation_target_reports_missing_and_sequence_less_rows(
    client: AsyncClient,
    db_session,
):
    project, task = await _make_resident(db_session)
    # Seed an already-persisted mixed-version row through Core rather than the
    # ORM writer. New canonical conversation writes correctly reject a missing
    # sequence before the receipt listener runs; this test verifies that the
    # reader still handles a legacy row created before that invariant existed.
    legacy_target_id = uuid.uuid4()
    connection = await db_session.connection()
    postgres_compat_trigger = connection.dialect.name == "postgresql"
    if postgres_compat_trigger:
        await db_session.execute(
            sa.text(
                "ALTER TABLE projectmessage "
                "DISABLE TRIGGER trg_projectmessage_0567_compat_insert"
            )
        )
    try:
        await db_session.execute(
            sa.insert(ProjectMessage).values(
                id=legacy_target_id,
                project_id=project.id,
                sequence=None,
                surface=MessageSurface.CONVERSATION,
                message_type=MessageType.NOTE,
                from_type="user",
                content="legacy target without a sequence",
                status="open",
                handling_state=MessageHandlingState.PENDING,
            )
        )
    finally:
        if postgres_compat_trigger:
            await db_session.execute(
                sa.text(
                    "ALTER TABLE projectmessage "
                    "ENABLE TRIGGER trg_projectmessage_0567_compat_insert"
                )
            )
    newer = ProjectMessage(
        project_id=project.id,
        sequence=1,
        surface=MessageSurface.CONVERSATION,
        message_type=MessageType.NOTE,
        from_type="user",
        content="ordinary sequenced row",
    )
    db_session.add(newer)
    await db_session.commit()

    legacy = await client.get(
        _url(project.id, include_message_id=legacy_target_id),
        headers=_headers(task),
    )
    assert legacy.status_code == 200, legacy.text
    assert legacy.json()["target_found"] is True
    assert [item["id"] for item in legacy.json()["items"]] == [
        str(legacy_target_id)
    ]
    assert legacy.json()["has_more"] is False
    assert legacy.json()["next_before_sequence"] is None
    assert legacy.json()["next_after_sequence"] is None

    missing = await client.get(
        _url(project.id, include_message_id=uuid.uuid4()),
        headers=_headers(task),
    )
    assert missing.status_code == 200, missing.text
    assert missing.json()["target_found"] is False
    assert missing.json()["items"] == []
    assert missing.json()["has_more"] is False
