"""Focused contracts for the bounded backend-only Resident turn runtime."""

from datetime import timedelta
import uuid

import pytest
from sqlalchemy.dialects import postgresql
from sqlmodel import select

from app.models import (
    MessageSurface,
    MessageType,
    Project,
    ProjectMessage,
    StrategicResident,
    Task,
    TaskStatus,
)
from app.strategic_resident_shadow import (
    MAX_MANIFEST_BYTES,
    _build_shadow_manifest,
    sweep_shadow_resident_turns,
)
from app.strategic_resident_turns import (
    INPUT_FROZEN_CONTEXT_KEY,
    SHADOW_CONTEXT_KEY,
    _canonical_json,
    coalesce_shadow_resident_turn,
)
from app.time_utils import _utcnow
from app.database import AsyncSessionLocal
from app.orchestrator import _lock_dispatchable_task_for_mutation


def test_pre_split_projection_imports_remain_compatible():
    from app.strategic_resident_shadow import (
        MAX_MANIFEST_BYTES as direct_max_manifest_bytes,
        sweep_shadow_resident_turns as direct_sweep,
    )
    from app.strategic_resident_turns import (
        MAX_MANIFEST_BYTES as compatible_max_manifest_bytes,
        sweep_shadow_resident_turns as compatible_sweep,
    )

    assert compatible_max_manifest_bytes == direct_max_manifest_bytes
    assert compatible_sweep is direct_sweep


async def _project(db_session, name="Resident runtime"):
    project = Project(name=name, user_id=f"user-{name}")
    db_session.add(project)
    await db_session.flush()
    return project


async def _conversation(
    db_session,
    project,
    sequence,
    *,
    content="hello",
    context=None,
    reply_to=None,
):
    message = ProjectMessage(
        project_id=project.id,
        actor_user_id="operator",
        sequence=sequence,
        surface=MessageSurface.CONVERSATION,
        message_type=MessageType.NOTE,
        from_type="user",
        content=content,
        context=context or {},
        reply_to_message_id=reply_to,
    )
    db_session.add(message)
    await db_session.flush()
    return message


def test_strategic_spawn_parent_lock_is_no_key_update():
    statement = (
        select(Project.id)
        .where(Project.id == uuid.uuid4())
        .with_for_update(skip_locked=True, key_share=True)
    )
    rendered = str(statement.compile(dialect=postgresql.dialect()))
    assert "FOR NO KEY UPDATE SKIP LOCKED" in rendered


@pytest.mark.asyncio
async def test_one_global_active_and_one_coalesced_interactive_successor(db_session):
    project = await _project(db_session)
    await _conversation(db_session, project, 1)
    await _conversation(db_session, project, 2)
    await _conversation(db_session, project, 3)

    first = await coalesce_shadow_resident_turn(
        db_session, project_id=project.id, message_sequence=1
    )
    active = await db_session.get(Task, first.active_task_id)
    frozen_payload = dict(active.first_payload)
    second = await coalesce_shadow_resident_turn(
        db_session, project_id=project.id, message_sequence=2
    )
    third = await coalesce_shadow_resident_turn(
        db_session, project_id=project.id, message_sequence=3
    )
    resident = await db_session.get(StrategicResident, project.id)
    successor = await db_session.get(Task, resident.interactive_successor_task_id)

    assert first.state == "queued"
    assert second.state == third.state == "coalesced"
    assert second.task_id == third.task_id == successor.id
    assert active.first_payload == frozen_payload
    assert successor.first_payload["message_sequence_start"] == 2
    assert successor.first_payload["message_sequence_end"] == 3
    assert successor.first_payload["trigger_count"] == 2
    assert active.archived is True
    assert active.context[SHADOW_CONTEXT_KEY] is True
    assert active.context[INPUT_FROZEN_CONTEXT_KEY] is True
    assert successor.context[INPUT_FROZEN_CONTEXT_KEY] is False


@pytest.mark.asyncio
async def test_interactive_preempts_background_and_projects_are_isolated(db_session):
    first_project = await _project(db_session, "first")
    second_project = await _project(db_session, "second")
    await _conversation(db_session, first_project, 1)
    background = await coalesce_shadow_resident_turn(
        db_session,
        project_id=first_project.id,
        lane="background",
        trigger_kind="project_change",
        project_change_cursor={"task_revision": 2},
    )
    interactive = await coalesce_shadow_resident_turn(
        db_session, project_id=first_project.id, message_sequence=1
    )
    other = await coalesce_shadow_resident_turn(
        db_session,
        project_id=second_project.id,
        lane="background",
        trigger_kind="project_change",
        project_change_cursor={"task_revision": 1},
    )
    resident = await db_session.get(StrategicResident, first_project.id)

    assert resident.active_turn_task_id == background.task_id
    assert resident.interactive_successor_task_id == interactive.task_id
    assert resident.scheduling_state["preempt_for_task_id"] == str(interactive.task_id)
    assert other.active_task_id != background.active_task_id


@pytest.mark.asyncio
async def test_paused_background_suppresses_work_but_interactive_still_wakes(
    db_session,
):
    project = await _project(db_session, "paused")
    await _conversation(db_session, project, 1)
    await coalesce_shadow_resident_turn(
        db_session, project_id=project.id, message_sequence=1
    )
    resident = await db_session.get(StrategicResident, project.id)
    resident.background_policy = "paused"
    db_session.add(resident)
    await db_session.flush()
    wake = await coalesce_shadow_resident_turn(
        db_session,
        project_id=project.id,
        lane="background",
        trigger_kind="project_change",
    )
    assert wake.state == "paused"
    assert wake.task_id is None
    assert resident.background_successor_task_id is None


@pytest.mark.asyncio
async def test_pause_transition_cancels_background_and_promotes_interactive(db_session):
    project = await _project(db_session, "pause transition")
    await _conversation(db_session, project, 1)
    background = await coalesce_shadow_resident_turn(
        db_session,
        project_id=project.id,
        lane="background",
        trigger_kind="project_change",
    )
    interactive = await coalesce_shadow_resident_turn(
        db_session, project_id=project.id, message_sequence=1
    )
    resident = await db_session.get(StrategicResident, project.id)
    resident.background_policy = "paused"
    db_session.add(resident)
    await db_session.commit()

    assert await sweep_shadow_resident_turns(session_factory=AsyncSessionLocal) == 0
    await db_session.refresh(resident)
    background_task = await db_session.get(Task, background.task_id)
    interactive_task = await db_session.get(Task, interactive.task_id)
    await db_session.refresh(background_task)
    await db_session.refresh(interactive_task)
    assert background_task.status == TaskStatus.CANCELLED
    assert resident.active_turn_task_id == interactive.task_id
    assert interactive_task.context[INPUT_FROZEN_CONTEXT_KEY] is True


@pytest.mark.asyncio
async def test_pause_transition_drops_background_successor_after_interactive(
    db_session,
):
    project = await _project(db_session, "pause successor")
    await _conversation(db_session, project, 1)
    await coalesce_shadow_resident_turn(
        db_session, project_id=project.id, message_sequence=1
    )
    background = await coalesce_shadow_resident_turn(
        db_session,
        project_id=project.id,
        lane="background",
        trigger_kind="project_change",
    )
    resident = await db_session.get(StrategicResident, project.id)
    resident.background_policy = "paused"
    db_session.add(resident)
    await db_session.commit()

    assert await sweep_shadow_resident_turns(session_factory=AsyncSessionLocal) == 1
    await db_session.refresh(resident)
    background_task = await db_session.get(Task, background.task_id)
    await db_session.refresh(background_task)
    assert resident.active_turn_task_id is None
    assert resident.background_successor_task_id is None
    assert background_task.status == TaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_pointer_repair_discards_cross_project_and_terminal_tasks(db_session):
    project = await _project(db_session, "repair")
    other = await _project(db_session, "foreign")
    foreign_task = Task(
        project_id=other.id,
        payloads=[{"type": "strategic_resident_turn"}],
        status=TaskStatus.QUEUED,
    )
    db_session.add(foreign_task)
    await db_session.flush()
    await coalesce_shadow_resident_turn(
        db_session,
        project_id=project.id,
        lane="background",
        trigger_kind="project_change",
    )
    resident = await db_session.get(StrategicResident, project.id)
    resident.active_turn_task_id = foreign_task.id
    resident.active_turn_lane = "background"
    db_session.add(resident)
    await db_session.flush()

    wake = await coalesce_shadow_resident_turn(
        db_session,
        project_id=project.id,
        lane="background",
        trigger_kind="project_change",
    )
    repaired = await db_session.get(Task, wake.active_task_id)
    assert repaired.project_id == project.id
    assert repaired.id != foreign_task.id


@pytest.mark.asyncio
async def test_manifest_preserves_chips_intent_and_reply_identity(db_session):
    project = await _project(db_session, "context")
    target = await _conversation(db_session, project, 1, content="decisive excerpt")
    context = {
        "source_refs": [{"id": "s1", "label": "Notes", "excerpt": "bounded"}],
        "artifact_refs": [{"artifact_id": "a1", "revision": "7"}],
        "attachments": [{"id": "f1", "filename": "board.png", "checksum": "abc"}],
        "operator_intent": {"action": "verify", "lane_owner": "resident"},
    }
    await _conversation(
        db_session,
        project,
        2,
        content="please inspect",
        context=context,
        reply_to=target.id,
    )
    await coalesce_shadow_resident_turn(
        db_session, project_id=project.id, message_sequence=1
    )
    await coalesce_shadow_resident_turn(
        db_session, project_id=project.id, message_sequence=2
    )
    await db_session.commit()
    # Complete the first immutable input so sequence 2 is promoted.
    assert await sweep_shadow_resident_turns(session_factory=AsyncSessionLocal) == 1
    resident = await db_session.get(StrategicResident, project.id)
    task = await db_session.get(Task, resident.active_turn_task_id)
    manifest = await _build_shadow_manifest(
        db_session, project=project, resident=resident, task=task
    )
    entry = manifest["messages"][0]

    assert entry["source_refs"][0]["label"] == "Notes"
    assert entry["artifact_refs"][0]["revision"] == "7"
    assert entry["attachments"][0]["filename"] == "board.png"
    assert entry["operator_intent"]["action"] == "verify"
    assert entry["reply_target"]["sequence"] == 1
    assert len(entry["reply_target"]["excerpt_sha256"]) == 64


@pytest.mark.asyncio
async def test_sweep_uses_whole_prefix_and_promotes_remainder_without_skipping(
    db_session,
):
    project = await _project(db_session, "remainder")
    for sequence in range(1, 71):
        await _conversation(
            db_session,
            project,
            sequence,
            content=f"message {sequence}",
            context={"source_refs": [{"id": f"s{sequence}", "label": "chip"}]},
        )
    await coalesce_shadow_resident_turn(
        db_session, project_id=project.id, message_sequence=1
    )
    # Coalesce the full accepted range into the one successor, then promote it.
    for sequence in range(2, 71):
        await coalesce_shadow_resident_turn(
            db_session, project_id=project.id, message_sequence=sequence
        )
    await db_session.commit()
    assert await sweep_shadow_resident_turns(session_factory=AsyncSessionLocal) == 1
    assert await sweep_shadow_resident_turns(session_factory=AsyncSessionLocal) == 1

    resident = await db_session.get(StrategicResident, project.id)
    completed = (
        (
            await db_session.execute(
                select(Task)
                .where(
                    Task.project_id == project.id,
                    Task.status == TaskStatus.COMPLETED,
                )
                .order_by(Task.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    prefix_manifest = completed[-1].plan_output["resident_shadow"]["manifest"]
    assert prefix_manifest["represented_message_sequence_end"] == 65
    assert prefix_manifest["requested_chip_count"] == 69
    assert prefix_manifest["represented_chip_count"] == 64
    assert resident.handled_message_sequence == 0
    assert resident.scheduling_state["shadow_handled_message_sequence"] == 65
    remainder = await db_session.get(Task, resident.active_turn_task_id)
    assert remainder.first_payload["message_sequence_start"] == 66
    assert remainder.first_payload["message_sequence_end"] == 70
    assert len(_canonical_json(prefix_manifest)) <= MAX_MANIFEST_BYTES
    assert all(row["source_refs"] for row in prefix_manifest["messages"])


@pytest.mark.asyncio
async def test_shadow_sweep_does_not_run_after_engine_flip(db_session):
    project = await _project(db_session, "owned")
    wake = await coalesce_shadow_resident_turn(
        db_session,
        project_id=project.id,
        lane="background",
        trigger_kind="project_change",
    )
    resident = await db_session.get(StrategicResident, project.id)
    resident.engine_owner = "strategic_resident"
    db_session.add(resident)
    await db_session.commit()
    assert (
        await sweep_shadow_resident_turns(
            session_factory=AsyncSessionLocal,
            now=_utcnow() + timedelta(seconds=1),
        )
        == 0
    )
    task = await db_session.get(Task, wake.task_id)
    assert task.status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_sweep_repairs_cancelled_active_pointer_without_new_event(db_session):
    project = await _project(db_session, "repair sweep")
    wake = await coalesce_shadow_resident_turn(
        db_session,
        project_id=project.id,
        lane="background",
        trigger_kind="project_change",
    )
    task = await db_session.get(Task, wake.task_id)
    task.status = TaskStatus.CANCELLED
    db_session.add(task)
    await db_session.commit()
    assert await sweep_shadow_resident_turns(session_factory=AsyncSessionLocal) == 0
    resident = await db_session.get(StrategicResident, project.id)
    assert resident.active_turn_task_id is None
    assert resident.scheduling_state["shadow_pointer_repaired_at"]


@pytest.mark.asyncio
async def test_sweep_private_sessions_leave_caller_pending_state_untouched(db_session):
    project = await _project(db_session, "private sweep")
    await coalesce_shadow_resident_turn(
        db_session,
        project_id=project.id,
        lane="background",
        trigger_kind="project_change",
    )
    await db_session.commit()
    pending_project = Project(name="pending caller", user_id="pending")
    pending_task = Task(project_id=pending_project.id, title="not flushed")
    db_session.add(pending_project)
    db_session.add(pending_task)

    assert await sweep_shadow_resident_turns(session_factory=AsyncSessionLocal) == 1
    assert pending_project in db_session.new
    assert pending_task in db_session.new


@pytest.mark.asyncio
async def test_sweep_failure_for_one_project_does_not_poison_another(
    db_session, monkeypatch
):
    import app.strategic_resident_shadow as shadow

    first = await _project(db_session, "fails")
    second = await _project(db_session, "succeeds")
    first_wake = await coalesce_shadow_resident_turn(
        db_session,
        project_id=first.id,
        lane="background",
        trigger_kind="project_change",
    )
    second_wake = await coalesce_shadow_resident_turn(
        db_session,
        project_id=second.id,
        lane="background",
        trigger_kind="project_change",
    )
    await db_session.commit()
    real_complete = shadow._complete_shadow_turn

    async def _fail_one(session, *, project_id, task_id, now):
        if project_id == first.id:
            raise RuntimeError("isolated projection failure")
        return await real_complete(
            session, project_id=project_id, task_id=task_id, now=now
        )

    monkeypatch.setattr(shadow, "_complete_shadow_turn", _fail_one)
    assert await sweep_shadow_resident_turns(session_factory=AsyncSessionLocal) == 1
    failed = await db_session.get(Task, first_wake.task_id)
    succeeded = await db_session.get(Task, second_wake.task_id)
    await db_session.refresh(failed)
    await db_session.refresh(succeeded)
    assert failed.status == TaskStatus.QUEUED
    assert succeeded.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_post_flip_shadow_enqueue_is_rejected(db_session):
    project = await _project(db_session, "reject")
    await coalesce_shadow_resident_turn(
        db_session,
        project_id=project.id,
        lane="background",
        trigger_kind="project_change",
    )
    resident = await db_session.get(StrategicResident, project.id)
    resident.engine_owner = "strategic_resident"
    resident.authority_generation += 1
    db_session.add(resident)
    await db_session.flush()
    with pytest.raises(RuntimeError, match="ownership flips"):
        await coalesce_shadow_resident_turn(
            db_session,
            project_id=project.id,
            lane="background",
            trigger_kind="project_change",
        )


@pytest.mark.asyncio
async def test_multibyte_message_is_byte_bounded_and_keeps_all_visible_refs(db_session):
    project = await _project(db_session, "unicode")
    refs = [{"id": f"s{i}", "label": f"chip-{i}"} for i in range(20)]
    await _conversation(
        db_session,
        project,
        1,
        content="😀" * 40_000,
        context={"source_refs": refs},
    )
    wake = await coalesce_shadow_resident_turn(
        db_session, project_id=project.id, message_sequence=1
    )
    resident = await db_session.get(StrategicResident, project.id)
    task = await db_session.get(Task, wake.task_id)
    manifest = await _build_shadow_manifest(
        db_session, project=project, resident=resident, task=task
    )
    entry = manifest["messages"][0]
    assert entry["content_truncated"] is True
    assert len(entry["content"].encode("utf-8")) <= 48 * 1024
    assert len(entry["source_refs"]) == 20
    assert len(_canonical_json(manifest)) <= MAX_MANIFEST_BYTES


@pytest.mark.asyncio
async def test_legacy_malformed_chip_context_counts_as_zero_without_failure(db_session):
    project = await _project(db_session, "malformed refs")
    await _conversation(
        db_session,
        project,
        1,
        context={
            "source_refs": {"not": "a list"},
            "artifact_refs": "legacy scalar",
            "attachments": None,
        },
    )
    wake = await coalesce_shadow_resident_turn(
        db_session, project_id=project.id, message_sequence=1
    )
    resident = await db_session.get(StrategicResident, project.id)
    task = await db_session.get(Task, wake.task_id)
    manifest = await _build_shadow_manifest(
        db_session, project=project, resident=resident, task=task
    )
    assert manifest["requested_chip_count"] == 0
    assert manifest["represented_chip_count"] == 0


@pytest.mark.asyncio
async def test_final_orchestrator_claim_fence_rejects_backend_managed_task(db_session):
    project = await _project(db_session, "claim fence")
    agent_task = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.QUEUED,
    )
    backend_task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_resident_turn"}],
        status=TaskStatus.QUEUED,
    )
    db_session.add(agent_task)
    db_session.add(backend_task)
    await db_session.commit()

    assert (
        await _lock_dispatchable_task_for_mutation(db_session, agent_task.id)
    ).id == agent_task.id
    await db_session.rollback()
    assert (
        await _lock_dispatchable_task_for_mutation(db_session, backend_task.id) is None
    )
