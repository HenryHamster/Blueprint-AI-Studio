"""Focused authoritative Resident scheduling and context contracts."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
import hashlib
import json
from fastapi import HTTPException
from sqlmodel import select

from app.models import (
    ArtifactRef,
    MessageSurface,
    MessageType,
    Project,
    ProjectMessage,
    Task,
    TaskStatus,
)
from app.artifact_refs import (
    RESIDENT_ARTIFACT_REF_GRANTS_KEY,
    resident_artifact_ref_grants,
)
from app.database import AsyncSessionLocal
from app.orchestrator import _agent_executed_task_type_filter
from app.routers.agent_resident_artifacts import get_resident_turn_artifact
from app.routers.agent_resident_turn import get_resident_turn_context
from app.strategic_resident_agent_binding import RESIDENT_CLAIM_NONCE_CONTEXT_KEY
from app.strategic_resident_checkpointing import initialize_strategic_resident
from app.strategic_resident_capabilities import compact_capability_catalog
from app.strategic_resident_context import (
    CAPABILITY_RESULTS_HARD_BYTES,
    HARD_INPUT_BYTES,
    PROVIDER_INPUT_FIT_BYTES,
    PROVIDER_PROMPT_OVERHEAD_RESERVE_BYTES,
    reconstruct_authoritative_turn_input,
)
from app.strategic_resident_runtime import coalesce_authoritative_resident_turn
from app.strategic_resident_failure_recovery import _merge_interactive_range
from app.strategic_resident_message_tail import ensure_message_tail_successor
from app.strategic_resident_wakeups import coalesce_meaningful_background_wakeup
from app.time_utils import _utcnow
from testing._resident_seed import seed_without_authority_enforcement


async def _authoritative_project(db_session):
    project = Project(name="Authoritative Resident", user_id="operator")
    db_session.add(project)
    await db_session.flush()
    # initialize_strategic_resident seeds a legacy resident (gen 1) with a v1
    # checkpoint pointer pending; flipping ownership + generation while setting
    # current_checkpoint_id in one UPDATE trips the rotation fence ("checkpoint
    # rotation cannot change authority generation"). This is a valid
    # authoritative starting state that the fences only permit via a real turn,
    # so seed it with enforcement relaxed (no-op on SQLite). Every asserted
    # action below runs with fences ACTIVE.
    async with seed_without_authority_enforcement(db_session):
        resident = await initialize_strategic_resident(
            db_session, project.id, locked_project=project
        )
        resident.engine_owner = "strategic_resident"
        resident.engine_version = "strategic-resident-v1"
        resident.authority_generation = 2
        db_session.add(resident)
        await db_session.flush()
    return project, resident


def _claim_token(task: Task) -> dict[str, str]:
    return {
        "sub": str(task.id),
        "project_id": str(task.project_id),
        "vm_instance_id": "runner:test",
        "task_type": "strategic_resident_turn",
        "claim_nonce": "nonce",
    }


def _mark_claimed(task: Task) -> None:
    task.status = TaskStatus.IN_PROGRESS
    task.vm_instance_id = "runner:test"
    context = dict(task.context or {})
    context[RESIDENT_CLAIM_NONCE_CONTEXT_KEY] = "nonce"
    task.context = context


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
    capabilities = provider_input["capabilities"]
    assert capabilities == {
        "mode": "typed_project_scoped_only",
        "catalog": compact_capability_catalog(),
        "ambient_shell": False,
        "ambient_credentials": False,
        "repository_write": False,
    }
    catalog = capabilities["catalog"]
    assert catalog
    assert {"task.inspect", "pr.merge", "deployment.rollback"} <= {
        item["id"] for item in catalog
    }
    assert all(
        set(item) == {
            "id", "mode", "risk", "parameters", "approval", "availability"
        }
        for item in catalog
    )
    catalog_json = json.dumps(catalog, separators=(",", ":"), sort_keys=True)
    assert len(catalog_json.encode("utf-8")) <= 8_192
    for forbidden in (
        "service",
        "evidence_preconditions",
        "receipt_fields",
        "credentials",
        "token",
        "shell",
    ):
        assert forbidden not in catalog_json
    assert provider_input["limits"]["encoded_bytes"] <= PROVIDER_INPUT_FIT_BYTES
    assert (
        provider_input["limits"]["provider_input_fit_bytes"]
        + CAPABILITY_RESULTS_HARD_BYTES
        + PROVIDER_PROMPT_OVERHEAD_RESERVE_BYTES
        == HARD_INPUT_BYTES
    )
    actual_bytes = len(
        json.dumps(
            provider_input,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
    )
    assert actual_bytes <= PROVIDER_INPUT_FIT_BYTES
    assert provider_input["limits"]["encoded_bytes"] == actual_bytes
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
async def test_oversized_unhandled_conversation_retains_prefix_and_tail_grants(
    db_session,
):
    project, resident = await _authoritative_project(db_session)
    message_count = 24
    refs = [
        ArtifactRef(
            project_id=project.id,
            tool_id=uuid.uuid4(),
            kind="notes",
            revision=index,
            checksum=f"{index % 16:x}" * 64,
        )
        for index in range(1, message_count + 1)
    ]
    messages = [
        ProjectMessage(
            project_id=project.id,
            actor_user_id="operator",
            sequence=index,
            surface=MessageSurface.CONVERSATION,
            message_type=MessageType.NOTE,
            from_type="user",
            content=f"message-{index:02d}:" + ("x" * 11_900),
            context={
                "artifact_refs": [
                    {"artifact_ref_id": str(refs[index - 1].id)}
                ]
            },
        )
        for index in range(1, message_count + 1)
    ]
    db_session.add_all([*refs, *messages])
    await db_session.flush()
    wake = await coalesce_authoritative_resident_turn(
        db_session,
        project_id=project.id,
        message_sequence=message_count,
        artifact_ref_grants=frozenset(ref.id for ref in refs),
    )
    active = await db_session.get(Task, wake.task_id)
    frozen_payload = dict(active.first_payload)
    frozen_payload["message_sequence_start"] = 1
    frozen_payload["message_sequence_end"] = message_count
    active.payloads = [frozen_payload]
    db_session.add(active)
    await db_session.flush()

    provider_input = await reconstruct_authoritative_turn_input(
        db_session,
        project_id=project.id,
        turn_task_id=wake.task_id,
    )
    retained = provider_input["lane_context"]["recent_conversation"]
    retained_sequences = [item["sequence"] for item in retained]
    window = provider_input["lane_context"]["conversation_window"]
    assert retained_sequences == list(range(1, retained_sequences[-1] + 1))
    assert retained_sequences[-1] < message_count
    assert window["represented_end"] == retained_sequences[-1]
    assert window["requested_end"] == message_count
    assert window["omitted_messages"] == message_count - len(retained)
    assert all(item["is_unhandled"] for item in retained)
    for item in retained:
        expected_ref = refs[item["sequence"] - 1]
        assert item["references"]["artifact_refs"][0][
            "artifact_ref_id"
        ] == str(expected_ref.id)
    expected_conversation_hash = hashlib.sha256(
        json.dumps(
            retained,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    assert (
        provider_input["lineage"]["conversation_hash"]
        == expected_conversation_hash
    )
    assert resident_artifact_ref_grants(active) == frozenset(
        refs[index - 1].id for index in retained_sequences
    )
    assert not (
        resident_artifact_ref_grants(active)
        & frozenset(ref.id for ref in refs[retained_sequences[-1] :])
    )

    successor = await ensure_message_tail_successor(
        db_session,
        resident,
        {active.id: active},
        represented_end=retained_sequences[-1],
        requested_end=message_count,
    )
    assert successor is not None
    successor_payload = successor.first_payload
    assert successor_payload["message_sequence_start"] == retained_sequences[-1] + 1
    assert successor_payload["message_sequence_end"] == message_count
    assert resident_artifact_ref_grants(successor) == frozenset(
        ref.id for ref in refs[retained_sequences[-1] :]
    )


@pytest.mark.asyncio
async def test_finalized_context_grants_included_historical_artifact_refs(db_session):
    project, resident = await _authoritative_project(db_session)
    refs = [
        ArtifactRef(
            project_id=project.id,
            tool_id=uuid.uuid4(),
            kind="notes",
            revision=index,
            checksum=str(index) * 64,
        )
        for index in (1, 2)
    ]
    messages = [
        ProjectMessage(
            project_id=project.id,
            actor_user_id="operator",
            sequence=index,
            surface=MessageSurface.CONVERSATION,
            message_type=MessageType.NOTE,
            from_type="user",
            content=f"message {index}",
            context={
                "artifact_refs": [{"artifact_ref_id": str(refs[index - 1].id)}]
            },
        )
        for index in (1, 2)
    ]
    db_session.add_all([*refs, *messages])
    resident.handled_message_sequence = 1
    db_session.add(resident)
    await db_session.flush()

    wake = await coalesce_authoritative_resident_turn(
        db_session,
        project_id=project.id,
        message_sequence=2,
        artifact_ref_grants=frozenset({refs[1].id}),
    )
    active = await db_session.get(Task, wake.task_id)
    assert resident_artifact_ref_grants(active) == frozenset({refs[1].id})

    first_input = await reconstruct_authoritative_turn_input(
        db_session,
        project_id=project.id,
        turn_task_id=wake.task_id,
    )
    first_hash = first_input["lineage"]["context_hash"]
    assert [
        message["sequence"]
        for message in first_input["lane_context"]["recent_conversation"]
    ] == [1, 2]
    assert resident_artifact_ref_grants(active) == frozenset(
        {refs[0].id, refs[1].id}
    )

    completion_input = await reconstruct_authoritative_turn_input(
        db_session,
        project_id=project.id,
        turn_task_id=wake.task_id,
    )
    assert completion_input["lineage"]["context_hash"] == first_hash
    assert completion_input["lineage"]["artifact_ref_grants_hash"] == first_input[
        "lineage"
    ]["artifact_ref_grants_hash"]
    assert resident_artifact_ref_grants(active) == frozenset(
        {refs[0].id, refs[1].id}
    )


@pytest.mark.asyncio
async def test_context_route_persists_historical_artifact_grant_for_retrieval(
    db_session,
):
    project, resident = await _authoritative_project(db_session)
    refs = [
        ArtifactRef(
            project_id=project.id,
            tool_id=uuid.uuid4(),
            kind="notes",
            revision=index,
            checksum=str(index) * 64,
            content_snapshot={"text": f"revision {index}"},
        )
        for index in (1, 2)
    ]
    messages = [
        ProjectMessage(
            project_id=project.id,
            actor_user_id="operator",
            sequence=index,
            surface=MessageSurface.CONVERSATION,
            message_type=MessageType.NOTE,
            from_type="user",
            content=f"message {index}",
            context={
                "artifact_refs": [{"artifact_ref_id": str(refs[index - 1].id)}]
            },
        )
        for index in (1, 2)
    ]
    db_session.add_all([*refs, *messages])
    resident.handled_message_sequence = 1
    db_session.add(resident)
    await db_session.flush()
    wake = await coalesce_authoritative_resident_turn(
        db_session,
        project_id=project.id,
        message_sequence=2,
        artifact_ref_grants=frozenset({refs[1].id}),
    )
    active = await db_session.get(Task, wake.task_id)
    _mark_claimed(active)
    db_session.add(active)
    await db_session.commit()
    token = _claim_token(active)

    async with AsyncSessionLocal() as context_session:
        first = await get_resident_turn_context(
            None,
            task_id=active.id,
            session=context_session,
            token=token,
        )
    async with AsyncSessionLocal() as completion_session:
        second = await get_resident_turn_context(
            None,
            task_id=active.id,
            session=completion_session,
            token=token,
        )
    assert second["provider_input"]["lineage"]["context_hash"] == first[
        "provider_input"
    ]["lineage"]["context_hash"]

    async with AsyncSessionLocal() as retrieval_session:
        historical = await get_resident_turn_artifact(
            None,
            refs[0].id,
            retrieval_session,
            token,
        )
    assert historical["content"]["text"] == "revision 1"
    async with AsyncSessionLocal() as verify_session:
        persisted = await verify_session.get(Task, active.id)
        assert resident_artifact_ref_grants(persisted) == frozenset(
            {refs[0].id, refs[1].id}
        )


@pytest.mark.asyncio
async def test_context_route_persists_omitted_tail_denial(db_session):
    project, _resident = await _authoritative_project(db_session)
    message_count = 24
    refs = [
        ArtifactRef(
            project_id=project.id,
            tool_id=uuid.uuid4(),
            kind="notes",
            revision=index,
            checksum=f"{index % 16:x}" * 64,
            content_snapshot={"text": f"revision {index}"},
        )
        for index in range(1, message_count + 1)
    ]
    messages = [
        ProjectMessage(
            project_id=project.id,
            actor_user_id="operator",
            sequence=index,
            surface=MessageSurface.CONVERSATION,
            message_type=MessageType.NOTE,
            from_type="user",
            content=f"message-{index:02d}:" + ("x" * 11_900),
            context={
                "artifact_refs": [
                    {"artifact_ref_id": str(refs[index - 1].id)}
                ]
            },
        )
        for index in range(1, message_count + 1)
    ]
    db_session.add_all([*refs, *messages])
    await db_session.flush()
    wake = await coalesce_authoritative_resident_turn(
        db_session,
        project_id=project.id,
        message_sequence=message_count,
        artifact_ref_grants=frozenset(ref.id for ref in refs),
    )
    active = await db_session.get(Task, wake.task_id)
    payload = dict(active.first_payload)
    payload["message_sequence_start"] = 1
    payload["message_sequence_end"] = message_count
    active.payloads = [payload]
    _mark_claimed(active)
    db_session.add(active)
    await db_session.commit()
    token = _claim_token(active)

    async with AsyncSessionLocal() as context_session:
        response = await get_resident_turn_context(
            None,
            task_id=active.id,
            session=context_session,
            token=token,
        )
    retained = response["provider_input"]["lane_context"]["recent_conversation"]
    represented_end = retained[-1]["sequence"]
    assert represented_end < message_count

    async with AsyncSessionLocal() as denied_session:
        with pytest.raises(HTTPException) as denied:
            await get_resident_turn_artifact(
                None,
                refs[represented_end].id,
                denied_session,
                token,
            )
    assert denied.value.status_code == 404
    async with AsyncSessionLocal() as allowed_session:
        allowed = await get_resident_turn_artifact(
            None,
            refs[represented_end - 1].id,
            allowed_session,
            token,
        )
    assert allowed["content"]["text"] == f"revision {represented_end}"


@pytest.mark.asyncio
async def test_authoritative_turn_freezes_and_unions_message_artifact_grants(db_session):
    project, resident = await _authoritative_project(db_session)
    refs = [
        ArtifactRef(
            project_id=project.id,
            tool_id=uuid.uuid4(),
            kind="notes",
            revision=index,
            checksum=str(index) * 64,
        )
        for index in (1, 2, 3)
    ]
    messages = [
        ProjectMessage(
            project_id=project.id,
            sequence=index,
            surface=MessageSurface.CONVERSATION,
            content=f"message {index}",
            context={
                "artifact_refs": [{"artifact_ref_id": str(refs[index - 1].id)}]
            },
        )
        for index in (1, 2, 3)
    ]
    db_session.add_all([*refs, *messages])
    await db_session.flush()

    first = await coalesce_authoritative_resident_turn(
        db_session,
        project_id=project.id,
        message_sequence=1,
        artifact_ref_grants=frozenset({refs[0].id}),
    )
    first_task = await db_session.get(Task, first.task_id)
    assert first_task.context[RESIDENT_ARTIFACT_REF_GRANTS_KEY] == [str(refs[0].id)]

    second = await coalesce_authoritative_resident_turn(
        db_session,
        project_id=project.id,
        message_sequence=2,
        artifact_ref_grants=frozenset({refs[1].id}),
    )
    assert second.successor_task_id is not None
    third = await coalesce_authoritative_resident_turn(
        db_session,
        project_id=project.id,
        message_sequence=3,
        artifact_ref_grants=frozenset({refs[2].id}),
    )
    assert third.task_id == second.task_id
    successor = await db_session.get(Task, third.task_id)
    assert set(successor.context[RESIDENT_ARTIFACT_REF_GRANTS_KEY]) == {
        str(refs[1].id),
        str(refs[2].id),
    }
    assert resident.active_turn_task_id == first.task_id


def test_failure_retry_merges_frozen_artifact_grants():
    project_id = uuid.uuid4()
    first_ref, second_ref = uuid.uuid4(), uuid.uuid4()
    successor = Task(
        project_id=project_id,
        payloads=[{
            "type": "strategic_resident_turn",
            "message_sequence_start": 3,
            "message_sequence_end": 3,
        }],
        context={RESIDENT_ARTIFACT_REF_GRANTS_KEY: [str(second_ref)]},
    )
    failed = Task(
        project_id=project_id,
        payloads=[{
            "type": "strategic_resident_turn",
            "message_sequence_start": 1,
            "message_sequence_end": 2,
        }],
        context={RESIDENT_ARTIFACT_REF_GRANTS_KEY: [str(first_ref)]},
    )
    _merge_interactive_range(successor, failed)
    assert set(successor.context[RESIDENT_ARTIFACT_REF_GRANTS_KEY]) == {
        str(first_ref), str(second_ref)
    }


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
async def test_signal_project_change_wakes_only_on_an_advancing_cursor(db_session):
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from sqlmodel import select as _select

    from app.strategic_resident_wakeups import signal_project_change

    project, _resident = await _authoritative_project(db_session)
    await db_session.commit()
    factory = async_sessionmaker(
        bind=db_session.bind, expire_on_commit=False, autoflush=False
    )

    # An empty / non-advancing cursor makes zero Tasks and zero model calls.
    assert (
        await signal_project_change(
            factory, project_id=project.id, project_change_cursor={}
        )
        is None
    )
    assert (await db_session.execute(_select(Task))).scalars().first() is None

    # A genuinely advancing cursor wakes the background lane.
    task_id = await signal_project_change(
        factory, project_id=project.id, project_change_cursor={"revision": 5}
    )
    assert task_id is not None
    await db_session.rollback()  # drop this session's stale snapshot
    task = await db_session.get(Task, task_id)
    assert task is not None
    assert str((task.first_payload or {}).get("type")) == "strategic_resident_turn"


@pytest.mark.asyncio
async def test_pending_worker_proposal_sweep_wakes_then_is_idle(db_session):
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from sqlmodel import select as _select

    from app.models import StrategicWorkerMemoryProposal
    from app.strategic_resident_wakeups import (
        sweep_pending_worker_proposal_wakeups,
    )

    project, resident = await _authoritative_project(db_session)
    resident.background_policy = "active"
    db_session.add(resident)
    db_session.add(
        StrategicWorkerMemoryProposal(
            project_id=project.id,
            proposal_id="wp-sweep",
            layer="working_strategy",
            updates={"priorities": {"x": 1}},
            rationale="cycle recommends",
            evidence=[],
            target_checkpoint_version=1,
            target_checkpoint_hash="a" * 64,
            status="pending",
        )
    )
    await db_session.commit()
    factory = async_sessionmaker(
        bind=db_session.bind, expire_on_commit=False, autoflush=False
    )

    # A pending proposal wakes the background lane.
    assert await sweep_pending_worker_proposal_wakeups(session_factory=factory) == 1
    await db_session.rollback()
    tasks = (await db_session.execute(_select(Task))).scalars().all()
    assert len(tasks) == 1

    # Repeated sweeps while the proposal is still pending coalesce onto the same
    # lane (bounded to one active turn plus one successor — never unbounded).
    await sweep_pending_worker_proposal_wakeups(session_factory=factory)
    await sweep_pending_worker_proposal_wakeups(session_factory=factory)
    await db_session.rollback()
    tasks = (await db_session.execute(_select(Task))).scalars().all()
    assert len(tasks) <= 2

    # Once the turn drains the proposal (no longer pending), the scan skips the
    # project entirely — an unchanged project performs zero model calls.
    async with factory() as drain:
        row = (
            await drain.execute(_select(StrategicWorkerMemoryProposal))
        ).scalar_one()
        row.status = "admitted"
        drain.add(row)
        await drain.commit()
    assert await sweep_pending_worker_proposal_wakeups(session_factory=factory) == 0


@pytest.mark.asyncio
async def test_worker_proposal_cursor_advances_after_bounded_partial_drain(
    db_session,
):
    from sqlmodel import select as _select

    from app.models import StrategicWorkerMemoryProposal
    from app.strategic_resident_worker_proposal_intake import (
        pending_worker_proposal_cursor,
    )

    project, _resident = await _authoritative_project(db_session)
    rows = [
        StrategicWorkerMemoryProposal(
            project_id=project.id,
            proposal_id=f"backlog-{index:03d}",
            layer="working_strategy",
            updates={"priorities": {f"p{index}": index}},
            rationale="bounded backlog",
            evidence=[],
            target_checkpoint_version=1,
            target_checkpoint_hash="a" * 64,
            status="pending",
        )
        for index in range(51)
    ]
    db_session.add_all(rows)
    await db_session.commit()

    first = await pending_worker_proposal_cursor(
        db_session,
        project_id=project.id,
    )
    ordered = (
        (
            await db_session.execute(
                _select(StrategicWorkerMemoryProposal)
                .where(StrategicWorkerMemoryProposal.project_id == project.id)
                .order_by(
                    StrategicWorkerMemoryProposal.created_at.asc(),
                    StrategicWorkerMemoryProposal.id.asc(),
                )
            )
        )
        .scalars()
        .all()
    )
    for row in ordered[:50]:
        row.status = "admitted"
        db_session.add(row)
    await db_session.commit()

    second = await pending_worker_proposal_cursor(
        db_session,
        project_id=project.id,
    )
    assert second
    assert second != first

    ordered[-1].status = "admitted"
    db_session.add(ordered[-1])
    await db_session.commit()
    assert not await pending_worker_proposal_cursor(
        db_session,
        project_id=project.id,
    )


@pytest.mark.asyncio
async def test_signal_project_change_is_zero_call_when_cursor_already_handled(db_session):
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from sqlmodel import select as _select

    from app.strategic_resident_wakeups import signal_project_change

    project, resident = await _authoritative_project(db_session)
    # The resident has already handled this high-water.
    resident.handled_project_change_cursor = {"revision": 7}
    db_session.add(resident)
    await db_session.commit()
    factory = async_sessionmaker(
        bind=db_session.bind, expire_on_commit=False, autoflush=False
    )

    # A cursor that does not advance the handled high-water is a no-op.
    assert (
        await signal_project_change(
            factory, project_id=project.id, project_change_cursor={"revision": 7}
        )
        is None
    )
    assert (await db_session.execute(_select(Task))).scalars().first() is None


@pytest.mark.asyncio
async def test_paused_project_change_is_preserved_and_queued_on_resume(db_session):
    from app.strategic_resident_wakeups import sync_resident_background_policy

    project, resident = await _authoritative_project(db_session)
    resident.background_policy = "paused"
    db_session.add(resident)
    await db_session.flush()

    suppressed = await coalesce_meaningful_background_wakeup(
        db_session,
        project_id=project.id,
        trigger_kind="project_change",
        project_change_cursor={"qualification_verdict_id": "verdict-9"},
    )
    assert suppressed is not None
    assert suppressed.state == "paused"
    assert suppressed.task_id is None
    assert resident.scheduling_state["pending_project_change_cursor"] == {
        "qualification_verdict_id": "verdict-9"
    }

    resumed = await sync_resident_background_policy(
        db_session,
        project_id=project.id,
        paused=False,
    )
    assert resumed is not None
    assert resumed.state == "queued"
    assert resumed.task_id is not None
    task = await db_session.get(Task, resumed.task_id)
    assert task is not None
    assert task.payloads[0]["project_change_cursor"] == {
        "qualification_verdict_id": "verdict-9"
    }
    assert "pending_project_change_cursor" not in resident.scheduling_state


@pytest.mark.asyncio
async def test_pending_change_on_resume_preserves_exact_commitment(db_session):
    from app.strategic_resident_wakeups import sync_resident_background_policy

    project, resident = await _authoritative_project(db_session)
    now = _utcnow()
    commitment_at = now + timedelta(hours=2)
    cursor = {"qualification_verdict_id": "verdict-with-commitment"}
    resident.background_policy = "paused"
    resident.next_wakeup_at = None
    resident.scheduling_state = {
        "pending_project_change_cursor": cursor,
        "paused_next_wakeup_at": commitment_at.isoformat(),
    }
    db_session.add(resident)
    await db_session.flush()

    resumed = await sync_resident_background_policy(
        db_session, project_id=project.id, paused=False, now=now
    )
    assert resumed is not None
    assert resumed.state == "queued"
    assert resumed.task_id is not None
    assert resident.next_wakeup_at == commitment_at
    assert "paused_next_wakeup_at" not in resident.scheduling_state


@pytest.mark.asyncio
async def test_resume_restores_overdue_deadline_without_model_call(db_session):
    from app.strategic_resident_wakeups import sync_resident_background_policy

    project, resident = await _authoritative_project(db_session)
    now = _utcnow()
    resident.background_policy = "paused"
    resident.next_wakeup_at = None
    resident.scheduling_state = {
        "deadline_at": (now - timedelta(minutes=5)).isoformat(),
        "deadline_reason": "overdue_commitment",
    }
    db_session.add(resident)
    await db_session.flush()

    wake = await sync_resident_background_policy(
        db_session,
        project_id=project.id,
        paused=False,
        now=now,
    )
    assert wake is None
    assert resident.background_policy == "active"
    assert resident.next_wakeup_at == now
    assert resident.active_turn_task_id is None


@pytest.mark.asyncio
async def test_pause_resume_preserves_recovery_retry_schedule(db_session):
    from app.strategic_resident_wakeups import sync_resident_background_policy

    project, resident = await _authoritative_project(db_session)
    now = _utcnow()
    retry_at = now + timedelta(minutes=9)
    resident.next_wakeup_at = retry_at
    resident.recovery_state = {
        "status": "retrying",
        "failure_category": "provider_transient",
        "failure_count": 1,
        "retry_limit": 3,
        "unknown_operation_receipt_ids": [],
        "updated_at": now.isoformat(),
    }
    db_session.add(resident)
    await db_session.flush()

    await sync_resident_background_policy(
        db_session, project_id=project.id, paused=True, now=now
    )
    assert resident.next_wakeup_at is None
    assert resident.scheduling_state["paused_next_wakeup_at"] == retry_at.isoformat()

    await sync_resident_background_policy(
        db_session, project_id=project.id, paused=False, now=now
    )
    assert resident.next_wakeup_at == retry_at
    assert "paused_next_wakeup_at" not in resident.scheduling_state


@pytest.mark.asyncio
async def test_resume_reconstructs_retry_without_stored_wakeup(db_session):
    from app.strategic_resident_failure_state import failure_backoff_seconds
    from app.strategic_resident_wakeups import sync_resident_background_policy

    project, resident = await _authoritative_project(db_session)
    now = _utcnow()
    resident.background_policy = "paused"
    resident.next_wakeup_at = None
    resident.scheduling_state = {}
    resident.recovery_state = {
        "status": "retrying",
        "failure_category": "provider_transient",
        "failure_count": 1,
        "retry_limit": 3,
        "unknown_operation_receipt_ids": [],
        "updated_at": now.isoformat(),
    }
    db_session.add(resident)
    await db_session.flush()

    wake = await sync_resident_background_policy(
        db_session, project_id=project.id, paused=False, now=now
    )
    assert wake is None
    assert resident.next_wakeup_at == now + timedelta(
        seconds=failure_backoff_seconds("provider_transient", 1)
    )


@pytest.mark.asyncio
async def test_failure_during_pause_resumes_due_retry_and_health(db_session):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.strategic_resident_wakeups import (
        sweep_due_authoritative_resident_wakeups,
        sync_resident_background_policy,
    )

    project, resident = await _authoritative_project(db_session)
    now = _utcnow()
    resident.background_policy = "paused"
    resident.health_status = "blocked"
    resident.next_wakeup_at = None
    resident.recovery_state = {
        "status": "retrying",
        "failure_category": "provider_transient",
        "failure_count": 1,
        "retry_limit": 3,
        "unknown_operation_receipt_ids": [],
        "updated_at": (now - timedelta(minutes=10)).isoformat(),
    }
    db_session.add(resident)
    await db_session.flush()

    wake = await sync_resident_background_policy(
        db_session, project_id=project.id, paused=False, now=now
    )
    assert wake is None
    assert resident.next_wakeup_at == now
    assert resident.health_status == "recovering"
    await db_session.commit()

    assert await sweep_due_authoritative_resident_wakeups(
        session_factory=async_sessionmaker(
            bind=db_session.bind, expire_on_commit=False, autoflush=False
        ),
        now=now,
        batch_size=10,
    ) == 1


@pytest.mark.asyncio
async def test_resume_recovery_block_returns_waiting_and_preserves_pending(db_session):
    from app.strategic_resident_wakeups import sync_resident_background_policy

    project, resident = await _authoritative_project(db_session)
    now = _utcnow()
    cursor = {"strategic_cycle_terminal": "v2:00000000000000000001:a:completed"}
    resident.background_policy = "paused"
    resident.health_status = "blocked"
    resident.recovery_state = {"unknown_operation_receipt_ids": ["unknown"]}
    resident.scheduling_state = {
        "pending_project_change_cursor": cursor,
        "paused_next_wakeup_at": (now + timedelta(hours=1)).isoformat(),
    }
    db_session.add(resident)
    await db_session.flush()

    wake = await sync_resident_background_policy(
        db_session, project_id=project.id, paused=False, now=now
    )
    assert wake is not None
    assert wake.state == "waiting_for_capacity"
    assert wake.task_id is None
    assert resident.scheduling_state["pending_project_change_cursor"] == cursor
    assert resident.next_wakeup_at == now + timedelta(hours=1)
    assert resident.health_status == "blocked"


@pytest.mark.asyncio
async def test_handled_pending_cursor_does_not_skip_deadline_restore(db_session):
    from app.strategic_resident_wakeups import sync_resident_background_policy

    project, resident = await _authoritative_project(db_session)
    now = _utcnow()
    deadline = now + timedelta(hours=1)
    cursor = {"qualification_verdict": "verdict-10"}
    resident.background_policy = "paused"
    resident.handled_project_change_cursor = cursor
    resident.scheduling_state = {
        "pending_project_change_cursor": cursor,
        "deadline_at": deadline.isoformat(),
    }
    db_session.add(resident)
    await db_session.flush()

    wake = await sync_resident_background_policy(
        db_session, project_id=project.id, paused=False, now=now
    )
    assert wake is None
    assert resident.next_wakeup_at == deadline
    assert "pending_project_change_cursor" not in resident.scheduling_state


@pytest.mark.asyncio
async def test_recovery_block_preserves_project_change_for_later_turn(db_session):
    project, resident = await _authoritative_project(db_session)
    resident.recovery_state = {"unknown_operation_receipt_ids": [str(uuid.uuid4())]}
    db_session.add(resident)
    await db_session.flush()

    wake = await coalesce_meaningful_background_wakeup(
        db_session,
        project_id=project.id,
        trigger_kind="project_change",
        project_change_cursor={"strategic_cycle_terminal": "cycle-12"},
    )
    assert wake is not None
    assert wake.state == "waiting_for_capacity"
    assert wake.task_id is None
    assert resident.scheduling_state["pending_project_change_cursor"] == {
        "strategic_cycle_terminal": "cycle-12"
    }


@pytest.mark.asyncio
async def test_out_of_order_durable_signal_retains_newer_inflight_cursor(db_session):
    project, resident = await _authoritative_project(db_session)
    newer = "v2:00000000000000000020:newer:completed"
    older = "v2:00000000000000000010:older:completed"

    first = await coalesce_meaningful_background_wakeup(
        db_session,
        project_id=project.id,
        trigger_kind="project_change",
        project_change_cursor={"strategic_cycle_terminal": newer},
    )
    second = await coalesce_meaningful_background_wakeup(
        db_session,
        project_id=project.id,
        trigger_kind="project_change",
        project_change_cursor={"strategic_cycle_terminal": older},
    )

    assert first is not None and first.task_id is not None
    assert second is None
    tasks = (
        await db_session.execute(select(Task).where(Task.project_id == project.id))
    ).scalars().all()
    assert len(tasks) == 1
    assert tasks[0].first_payload["project_change_cursor"] == {
        "strategic_cycle_terminal": newer
    }
    assert resident.background_successor_task_id is None


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
