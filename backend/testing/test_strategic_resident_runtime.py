"""Focused authoritative Resident scheduling and context contracts."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
import hashlib
import json

from app.models import (
    MessageSurface,
    MessageType,
    Project,
    ProjectMessage,
    Task,
    TaskStatus,
)
from app.orchestrator import _agent_executed_task_type_filter
from app.strategic_resident_checkpointing import initialize_strategic_resident
from app.strategic_resident_context import (
    HARD_INPUT_BYTES,
    reconstruct_authoritative_turn_input,
)
from app.strategic_resident_runtime import coalesce_authoritative_resident_turn
from app.strategic_resident_wakeups import coalesce_meaningful_background_wakeup
from app.time_utils import _utcnow


async def _authoritative_project(db_session):
    project = Project(name="Authoritative Resident", user_id="operator")
    db_session.add(project)
    await db_session.flush()
    resident = await initialize_strategic_resident(
        db_session, project.id, locked_project=project
    )
    resident.engine_owner = "strategic_resident"
    resident.engine_version = "strategic-resident-v1"
    resident.authority_generation = 2
    db_session.add(resident)
    await db_session.flush()
    return project, resident


@pytest.mark.asyncio
async def test_authoritative_turn_reconstructs_visible_context_and_is_bounded(
    db_session,
):
    project, _resident = await _authoritative_project(db_session)
    prior = ProjectMessage(
        project_id=project.id,
        actor_user_id="prior-operator",
        sequence=1,
        surface=MessageSurface.CONVERSATION,
        message_type=MessageType.NOTE,
        from_type="user",
        content="prior context",
    )
    current = ProjectMessage(
        project_id=project.id,
        actor_user_id="operator",
        sequence=2,
        surface=MessageSurface.CONVERSATION,
        message_type=MessageType.NOTE,
        from_type="user",
        content="please inspect these",
        context={
            "source_refs": [{"id": "task-1", "label": "Visible task"}],
            "attachments": [
                {
                    "attachment_id": "attachment-1",
                    "filename": "board.png",
                    "mime_type": "image/png",
                    "size_bytes": 123,
                    "storage_uri": "gs://bucket/board.png",
                }
            ],
        },
    )
    db_session.add_all([prior, current])
    await db_session.flush()
    wake = await coalesce_authoritative_resident_turn(
        db_session,
        project_id=project.id,
        lane="interactive",
        message_sequence=2,
    )
    provider_input = await reconstruct_authoritative_turn_input(
        db_session,
        project_id=project.id,
        turn_task_id=wake.task_id,
    )
    messages = provider_input["lane_context"]["recent_conversation"]
    assert [item["sequence"] for item in messages] == [1, 2]
    assert messages[-1]["actor_user_id"] == "operator"
    source = messages[-1]["references"]["source_refs"][0]
    attachment = messages[-1]["references"]["attachments"][0]
    assert source["id"] == "task-1"
    assert source["label"] == "Visible task"
    assert source["reference_integrity"] == "displayed_unrevisioned"
    assert attachment["attachment_id"] == "attachment-1"
    assert attachment["filename"] == "board.png"
    assert attachment["mime_type"] == "image/png"
    assert attachment["size_bytes"] == "123"
    assert provider_input["limits"]["encoded_bytes"] <= HARD_INPUT_BYTES
    assert len(provider_input["lineage"]["context_hash"]) == 64
    expected_evidence_hash = hashlib.sha256(
        json.dumps(
            provider_input["evidence"],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    assert provider_input["lineage"]["evidence_hash"] == expected_evidence_hash


@pytest.mark.asyncio
async def test_authoritative_scheduler_fails_closed_on_corrupt_pointer(db_session):
    project, resident = await _authoritative_project(db_session)
    corrupt = Task(
        project_id=project.id,
        title="wrong type",
        payloads=[{"type": "memo"}],
        context={},
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
    )
    db_session.add(corrupt)
    await db_session.flush()
    resident.active_turn_task_id = corrupt.id
    resident.active_turn_lane = "background"
    db_session.add(resident)
    await db_session.flush()
    with pytest.raises(RuntimeError, match="pointer is invalid"):
        await coalesce_authoritative_resident_turn(
            db_session,
            project_id=project.id,
            lane="background",
            project_change_cursor={"revision": 2},
        )


@pytest.mark.asyncio
async def test_cross_project_reference_rejected(db_session):
    project, _resident = await _authoritative_project(db_session)
    message = ProjectMessage(
        project_id=project.id,
        actor_user_id="operator",
        sequence=1,
        surface=MessageSurface.CONVERSATION,
        message_type=MessageType.NOTE,
        from_type="user",
        content="bad reference",
        context={
            "source_refs": [
                {"id": "foreign", "project_id": str(uuid.uuid4())}
            ]
        },
    )
    db_session.add(message)
    await db_session.flush()
    wake = await coalesce_authoritative_resident_turn(
        db_session, project_id=project.id, message_sequence=1
    )
    with pytest.raises(Exception, match="cross-project reference"):
        await reconstruct_authoritative_turn_input(
            db_session,
            project_id=project.id,
            turn_task_id=wake.task_id,
        )


@pytest.mark.asyncio
async def test_cross_project_typed_reference_without_scope_is_rejected(db_session):
    project, _resident = await _authoritative_project(db_session)
    foreign_project = Project(name="Foreign", user_id="other")
    db_session.add(foreign_project)
    await db_session.flush()
    foreign_task = Task(project_id=foreign_project.id, payloads=[{"type": "memo"}])
    db_session.add(foreign_task)
    await db_session.flush()
    message = ProjectMessage(
        project_id=project.id,
        actor_user_id="operator",
        sequence=1,
        surface=MessageSurface.CONVERSATION,
        message_type=MessageType.NOTE,
        from_type="user",
        content="hidden foreign reference",
        context={"source_refs": [{"task_id": str(foreign_task.id)}]},
    )
    db_session.add(message)
    await db_session.flush()
    wake = await coalesce_authoritative_resident_turn(
        db_session, project_id=project.id, message_sequence=1
    )

    with pytest.raises(Exception, match="cross-project reference"):
        await reconstruct_authoritative_turn_input(
            db_session,
            project_id=project.id,
            turn_task_id=wake.task_id,
        )


def test_dynamic_dispatch_filter_admits_only_frozen_authoritative_turns(monkeypatch):
    from app import orchestrator
    from sqlalchemy.dialects import postgresql

    monkeypatch.setattr(
        orchestrator,
        "AGENT_EXECUTED_TASK_TYPES",
        frozenset({"create", "strategic_resident_turn"}),
    )
    rendered = str(
        _agent_executed_task_type_filter().compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "strategic_resident_turn" in rendered
    assert "_resident_mutation_authorized" in rendered
    assert "_resident_input_frozen" in rendered
    assert "shadow_only" in rendered
    assert rendered.count("'strategic_resident_turn'") == 1


@pytest.mark.asyncio
async def test_unchanged_background_check_is_zero_call_and_due_wakeup_coalesces(
    db_session,
):
    project, resident = await _authoritative_project(db_session)
    resident.next_wakeup_at = _utcnow() + timedelta(hours=1)
    db_session.add(resident)
    await db_session.flush()
    assert await coalesce_meaningful_background_wakeup(
        db_session,
        project_id=project.id,
        trigger_kind="deadline",
    ) is None
    resident.next_wakeup_at = _utcnow() - timedelta(seconds=1)
    await db_session.flush()
    wake = await coalesce_meaningful_background_wakeup(
        db_session,
        project_id=project.id,
        trigger_kind="deadline",
    )
    assert wake is not None
    assert wake.state == "queued"
    assert resident.next_wakeup_at is None


@pytest.mark.asyncio
async def test_required_offline_local_runner_returns_truthful_waiting_state(db_session):
    project, resident = await _authoritative_project(db_session)
    resident.runner_policy = {"require_local_runner": True}
    db_session.add(resident)
    await db_session.flush()
    wake = await coalesce_authoritative_resident_turn(
        db_session,
        project_id=project.id,
        lane="background",
        project_change_cursor={"revision": 1},
    )
    task = await db_session.get(Task, wake.task_id)
    assert wake.state == "waiting_for_capacity"
    assert task.context["strategic_local_runner_required"] is True
    assert task.context["_resident_no_silent_provider_downgrade"] is True


@pytest.mark.asyncio
async def test_cloud_disabled_returns_truthful_waiting_state(
    db_session,
    monkeypatch,
):
    from app import shared_state

    project, resident = await _authoritative_project(db_session)
    resident.runner_policy = {"mode": "cloud"}
    monkeypatch.setitem(shared_state.runtime_config, "max_concurrent_vms", 0)
    wake = await coalesce_authoritative_resident_turn(
        db_session,
        project_id=project.id,
        lane="interactive",
        message_sequence=1,
    )
    assert wake.state == "waiting_for_capacity"
    task = await db_session.get(Task, wake.task_id)
    assert task is not None
    assert task.status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_paused_project_accepts_interactive_message_as_waiting(db_session):
    project, resident = await _authoritative_project(db_session)
    project.paused = True
    resident.background_policy = "paused"
    wake = await coalesce_authoritative_resident_turn(
        db_session,
        project_id=project.id,
        lane="interactive",
        message_sequence=1,
    )
    assert wake.task_id is not None
    assert wake.state == "waiting_for_capacity"
