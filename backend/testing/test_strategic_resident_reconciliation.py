"""Focused adversarial tests for Resident-only turn reconciliation."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app import max_plan_pool
from app.auth import create_agent_token
from app.models import (
    MessageHandlingState,
    MessageSurface,
    MessageType,
    Project,
    ProjectMessage,
    StrategicOperationReceipt,
    StrategicResident,
    StrategicResidentCheckpoint,
    Task,
    TaskStatus,
)
from app.strategic_operation_authority import (
    AuthorityGenerationMismatch,
    ExpectedRevisionMismatch,
    OperationIdentityCollision,
    OperationResultTooLarge,
)
from app.strategic_resident_reconciliation import (
    DecisiveEvidenceRequired,
    ReconciliationCursorError,
    ReconciliationResultInvalid,
    StrategicResidentReconcileRequest,
    reconcile_strategic_resident_turn,
)
from app.strategic_resident_context import reconstruct_authoritative_turn_input
from app.strategic_resident_evidence_policy import evidence_key
from app.strategic_resident_agent_binding import (
    RESIDENT_CLAIM_NONCE_CONTEXT_KEY,
    ResidentAgentBinding,
    ResidentAgentBindingError,
)
from app.strategic_resident_turn_results import ResidentEvidenceInspection
from app.strategic_resident_turns import SHADOW_CONTEXT_KEY
from app.time_utils import _utcnow
def _canonical(value) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()


def _factory(session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=session.bind,
        expire_on_commit=False,
        autoflush=False,
    )


async def _resident_turn_rows(
    session,
    *,
    generation: int = 7,
    successor_lane: str | None = None,
    account_id: str | None = None,
):
    project = Project(
        name=f"Reconciliation {uuid.uuid4()}",
        user_id="operator",
        vision={},
        problem_model={},
        directions=[],
    )
    session.add(project)
    await session.commit()
    await session.refresh(project)
    resident = StrategicResident(
        project_id=project.id,
        engine_owner="strategic_resident",
        engine_version="resident-v2",
        authority_generation=generation,
    )
    session.add(resident)
    await session.commit()
    checkpoint_body = {
        "schema_version": 1,
        "charter": {"purpose": "Ship safely"},
        "working_strategy": {
            "priorities": {"reliability": "high"},
            "strategic_memory": [],
            "open_loops": [
                {"loop_id": "old-loop", "summary": "Resolve old question"}
            ],
        },
        "cursors": {
            "handled_message_sequence": 0,
            "project_change": {},
        },
    }
    checkpoint_hash = hashlib.sha256(_canonical(checkpoint_body)).hexdigest()
    checkpoint = StrategicResidentCheckpoint(
        project_id=project.id,
        version=1,
        content_hash=checkpoint_hash,
        checkpoint=checkpoint_body,
        authority_generation=generation,
        source_turn_task_id=None,
    )
    session.add(checkpoint)
    await session.commit()
    user_message = ProjectMessage(
        project_id=project.id,
        actor_user_id="operator",
        sequence=1,
        surface=MessageSurface.CONVERSATION,
        message_type=MessageType.NOTE,
        from_type="user",
        content="Should we change the rollout strategy?",
        context={
            "source_refs": [
                {
                    "id": "run-42",
                    "kind": "ci_run",
                    "revision": "sha-abc",
                    "checksum": "f" * 64,
                }
            ]
        },
    )
    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id="resident-turn-vm",
        llm_account_id=account_id,
        payloads=[
            {
                "type": "strategic_resident_turn",
                "resident_id": str(project.id),
                "lane": "interactive",
                "authority_generation": generation,
                "engine_owner": "strategic_resident",
                "shadow_only": False,
                "checkpoint_version": 1,
                "checkpoint_hash": checkpoint_hash,
                "message_sequence_start": 1,
                "message_sequence_end": 1,
                "project_change_cursor": {"pipeline_revision": 4},
            }
        ],
        context={
            RESIDENT_CLAIM_NONCE_CONTEXT_KEY: "test-resident-claim-nonce",
            "_resident_mutation_authorized": True,
            SHADOW_CONTEXT_KEY: False,
            "_resident_input_frozen": True,
            "_resident_project_id": str(project.id),
            "_resident_authority_generation": generation,
            "_resident_engine_owner_observed": "strategic_resident",
            **({"llm_account_id": account_id} if account_id else {}),
        },
    )
    session.add(user_message)
    session.add(task)
    await session.commit()
    await session.refresh(task)
    successor = None
    if successor_lane is not None:
        successor = Task(
            project_id=project.id,
            status=TaskStatus.QUEUED,
            payloads=[
                {
                    "type": "strategic_resident_turn",
                    "resident_id": str(project.id),
                    "lane": successor_lane,
                    "authority_generation": generation,
                    "engine_owner": "strategic_resident",
                    "shadow_only": False,
                    "trigger_kind": "operator_message",
                    "checkpoint_version": 1,
                    "checkpoint_hash": checkpoint_hash,
                    "message_sequence_start": 2,
                    "message_sequence_end": 2,
                }
            ],
            context={
                SHADOW_CONTEXT_KEY: False,
                "_resident_mutation_authorized": True,
                "_resident_input_frozen": False,
                "_resident_project_id": str(project.id),
                "_resident_authority_generation": generation,
                "_resident_engine_owner_observed": "strategic_resident",
            },
        )
        session.add(successor)
        await session.commit()
        await session.refresh(successor)
    resident.current_checkpoint_id = checkpoint.id
    resident.active_turn_task_id = task.id
    resident.active_turn_lane = "interactive"
    if successor_lane == "interactive":
        resident.interactive_successor_task_id = successor.id
    elif successor_lane == "background":
        resident.background_successor_task_id = successor.id
    session.add(resident)
    await session.commit()
    await session.refresh(resident)
    provider_input = await reconstruct_authoritative_turn_input(
        session,
        project_id=project.id,
        turn_task_id=task.id,
    )
    task_context = dict(task.context or {})
    task_context["_test_provider_context_hash"] = provider_input["lineage"][
        "context_hash"
    ]
    task.context = task_context
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return project, resident, task, checkpoint, user_message, successor


def _turn_result(*, reply: str = "Yes. Tighten the rollout gate.") -> dict:
    return {
        "reply": reply,
        "facts": [
            {
                "fact_id": "fact-1",
                "statement": "CI evidence contradicts the current rollout assumption.",
                "confidence": 0.95,
                "evidence_ids": ["evidence-1"],
            }
        ],
        "evidence": [
            {
                "evidence_id": "evidence-1",
                "kind": "ci_run",
                "source_id": "run-42",
                "revision": "sha-abc",
                "checksum": "f" * 64,
                "summary": "The decisive qualification run failed.",
            }
        ],
        "operations": [],
        "receipts": [],
        "unknown_effects": [],
        "strategic_implications": [
            {
                "implication_id": "implication-1",
                "kind": "strategy_change",
                "summary": "Require qualification before rollout.",
                "evidence_ids": ["evidence-1"],
            }
        ],
        "proposed_memory": [
            {
                "proposal_id": "proposal-1",
                "layer": "working_strategy",
                "updates": {
                    "priorities": {"qualification_before_rollout": True},
                    "decision": "Require qualification before rollout.",
                },
                "rationale": "The decisive CI evidence invalidated the old assumption.",
                "evidence_ids": ["evidence-1"],
            }
        ],
        "follow_up": {
            "summary": "Verify the next qualification run.",
            "open_loops": [
                {
                    "loop_id": "new-loop",
                    "summary": "Verify the next qualification run.",
                    "owner": "resident",
                    "evidence_ids": ["evidence-1"],
                }
            ],
        },
        "required_attention": [],
    }


def _reply_only_result(reply: str) -> dict:
    result = _turn_result(reply=reply)
    result.update(
        facts=[],
        evidence=[],
        strategic_implications=[],
        proposed_memory=[],
        follow_up=None,
    )
    return result


def _request(project, task, checkpoint, *, result=None, generation=7, **changes):
    values = {
        "project_id": project.id,
        "turn_task_id": task.id,
        "authority_generation": generation,
        "reconciliation_id": "reconcile-1",
        "expected_turn_task_version": int(task.version),
        "expected_checkpoint_id": checkpoint.id,
        "expected_checkpoint_version": int(checkpoint.version),
        "expected_checkpoint_hash": checkpoint.content_hash,
        "context_hash": (task.context or {}).get(
            "_test_provider_context_hash", "0" * 64
        ),
        "agent_binding": ResidentAgentBinding(
            task_id=task.id,
            project_id=project.id,
            vm_instance_id=task.vm_instance_id,
            task_type="strategic_resident_turn",
            claim_nonce=str(
                (task.context or {}).get(RESIDENT_CLAIM_NONCE_CONTEXT_KEY) or ""
            ),
        ),
        "turn_result": result or _turn_result(),
        "accepted_memory_proposal_ids": ("proposal-1",),
        "accepted_implication_ids": ("implication-1",),
        "accepted_open_loop_ids": ("new-loop",),
        "resolved_open_loop_ids": ("old-loop",),
        "evidence_inspections": (
            ResidentEvidenceInspection(
                evidence_id="evidence-1",
                kind="ci_run",
                source_id="run-42",
                revision="sha-abc",
                checksum="f" * 64,
                inspected_at="2026-07-16T12:00:00Z",
                decisive=True,
            ),
        ),
    }
    values.update(changes)
    return StrategicResidentReconcileRequest(**values)


async def _completion_body(session, project, task, checkpoint) -> dict:
    provider_input = await reconstruct_authoritative_turn_input(
        session,
        project_id=project.id,
        turn_task_id=task.id,
    )
    request = _request(project, task, checkpoint)
    return {
        "task_id": str(task.id),
        "project_id": str(project.id),
        "authority_generation": request.authority_generation,
        "reconciliation_id": request.reconciliation_id,
        "expected_turn_task_version": request.expected_turn_task_version,
        "expected_checkpoint_id": str(request.expected_checkpoint_id),
        "expected_checkpoint_version": request.expected_checkpoint_version,
        "expected_checkpoint_hash": request.expected_checkpoint_hash,
        "context_hash": provider_input["lineage"]["context_hash"],
        "result": request.turn_result,
        "accepted_memory_proposal_ids": list(
            request.accepted_memory_proposal_ids
        ),
        "accepted_implication_ids": list(request.accepted_implication_ids),
        "accepted_open_loop_ids": list(request.accepted_open_loop_ids),
        "resolved_open_loop_ids": list(request.resolved_open_loop_ids),
        "evidence_inspections": [
            inspection.model_dump(mode="json")
            for inspection in request.evidence_inspections
        ],
    }


@pytest.mark.asyncio
async def test_completion_post_replays_after_committed_response_is_lost(
    client,
    db_session,
):
    project, _resident, task, checkpoint, _message, _successor = (
        await _resident_turn_rows(db_session)
    )
    body = await _completion_body(db_session, project, task, checkpoint)
    token = create_agent_token(
        str(task.id),
        task.vm_instance_id,
        task_type="strategic_resident_turn",
        project_id=str(project.id),
        claim_nonce=str(task.context[RESIDENT_CLAIM_NONCE_CONTEXT_KEY]),
    )
    headers = {"x-agent-token": token}

    first = await client.post(
        "/api/agent/resident-turn/complete",
        json=body,
        headers=headers,
    )
    assert first.status_code == 200, first.text
    assert first.json()["idempotent_replay"] is False

    changed_hash_body = {**body, "context_hash": "e" * 64}
    collision = await client.post(
        "/api/agent/resident-turn/complete",
        json=changed_hash_body,
        headers=headers,
    )
    assert collision.status_code == 409

    replay = await client.post(
        "/api/agent/resident-turn/complete",
        json=body,
        headers=headers,
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["idempotent_replay"] is True
    assert replay.json()["checkpoint_id"] == first.json()["checkpoint_id"]
    assert replay.json()["reply_message_id"] == first.json()["reply_message_id"]


@pytest.mark.asyncio
async def test_completion_broadcasts_canonical_messages_in_commit_order_once(
    client,
    db_session,
    mocker,
):
    project, _resident, task, checkpoint, first_message, _successor = (
        await _resident_turn_rows(db_session, successor_lane=None)
    )
    second_message = ProjectMessage(
        project_id=project.id,
        actor_user_id="operator",
        sequence=2,
        surface=MessageSurface.CONVERSATION,
        handling_state=MessageHandlingState.PENDING,
        message_type=MessageType.NOTE,
        from_type="user",
        content="Include this coalesced follow-up.",
        context={},
    )
    payload = dict(task.first_payload)
    payload["message_sequence_end"] = 2
    task.payloads = [payload]
    db_session.add_all([task, second_message])
    await db_session.commit()
    body = await _completion_body(db_session, project, task, checkpoint)
    token = create_agent_token(
        str(task.id),
        task.vm_instance_id,
        task_type="strategic_resident_turn",
        project_id=str(project.id),
        claim_nonce=str(task.context[RESIDENT_CLAIM_NONCE_CONTEXT_KEY]),
    )
    broadcast = mocker.patch(
        "app.strategic_resident_realtime.broadcast_to_project",
        new_callable=mocker.AsyncMock,
    )

    response = await client.post(
        "/api/agent/resident-turn/complete",
        json=body,
        headers={"x-agent-token": token},
    )

    assert response.status_code == 200, response.text
    assert response.json()["idempotent_replay"] is False
    calls = broadcast.await_args_list
    assert [call.args[1] for call in calls] == [
        "message_updated",
        "message_updated",
        "message_created",
        "task_updated",
    ]
    assert all(call.args[0] == str(project.id) for call in calls)
    first_projection = calls[0].args[2]["message"]
    second_projection = calls[1].args[2]["message"]
    reply_projection = calls[2].args[2]["message"]
    assert first_projection["id"] == str(first_message.id)
    assert first_projection["sequence"] == 1
    assert first_projection["handling_state"] == "responded"
    assert first_projection["handled_by_task_id"] == str(task.id)
    assert second_projection["id"] == str(second_message.id)
    assert second_projection["sequence"] == 2
    assert reply_projection["id"] == response.json()["reply_message_id"]
    assert reply_projection["sequence"] == 3
    assert reply_projection["reply_to_message_id"] == str(second_message.id)
    assert calls[3].args[2] == {
        "task_id": str(task.id),
        "status": TaskStatus.COMPLETED,
    }

    broadcast.reset_mock()
    replay = await client.post(
        "/api/agent/resident-turn/complete",
        json=body,
        headers={"x-agent-token": token},
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["idempotent_replay"] is True
    broadcast.assert_not_awaited()


@pytest.mark.asyncio
async def test_completion_broadcast_failure_does_not_reverse_commit(
    client,
    db_session,
    mocker,
):
    project, _resident, task, checkpoint, message, _successor = (
        await _resident_turn_rows(db_session, successor_lane=None)
    )
    body = await _completion_body(db_session, project, task, checkpoint)
    token = create_agent_token(
        str(task.id),
        task.vm_instance_id,
        task_type="strategic_resident_turn",
        project_id=str(project.id),
        claim_nonce=str(task.context[RESIDENT_CLAIM_NONCE_CONTEXT_KEY]),
    )
    broadcast = mocker.patch(
        "app.strategic_resident_realtime.broadcast_to_project",
        new_callable=mocker.AsyncMock,
        side_effect=RuntimeError("realtime unavailable"),
    )

    response = await client.post(
        "/api/agent/resident-turn/complete",
        json=body,
        headers={"x-agent-token": token},
    )

    assert response.status_code == 200, response.text
    assert broadcast.await_count == 3
    async with _factory(db_session)() as verify:
        completed = await verify.get(Task, task.id)
        handled = await verify.get(ProjectMessage, message.id)
        reply = await verify.get(
            ProjectMessage,
            uuid.UUID(response.json()["reply_message_id"]),
        )
    assert completed.status == TaskStatus.COMPLETED
    assert handled.handling_state == MessageHandlingState.RESPONDED
    assert handled.handled_by_task_id == task.id
    assert reply is not None and reply.reply_to_message_id == message.id


@pytest.mark.asyncio
async def test_completion_post_rejects_foreign_and_stale_vm_tokens(
    client,
    db_session,
):
    project, _resident, task, checkpoint, _message, _successor = (
        await _resident_turn_rows(db_session)
    )
    body = await _completion_body(db_session, project, task, checkpoint)
    foreign = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id="foreign-resident-vm",
        payloads=[{"type": "strategic_resident_turn"}],
    )
    db_session.add(foreign)
    await db_session.commit()
    await db_session.refresh(foreign)
    foreign_token = create_agent_token(
        str(foreign.id),
        foreign.vm_instance_id,
        task_type="strategic_resident_turn",
        project_id=str(project.id),
        claim_nonce=str(task.context[RESIDENT_CLAIM_NONCE_CONTEXT_KEY]),
    )
    stale_token = create_agent_token(
        str(task.id),
        "retired-resident-vm",
        task_type="strategic_resident_turn",
        project_id=str(project.id),
    )

    foreign_response = await client.post(
        "/api/agent/resident-turn/complete",
        json=body,
        headers={"x-agent-token": foreign_token},
    )
    stale_response = await client.post(
        "/api/agent/resident-turn/complete",
        json=body,
        headers={"x-agent-token": stale_token},
    )
    assert foreign_response.status_code == 403
    assert stale_response.status_code == 403


@pytest.mark.asyncio
async def test_reconciliation_rejects_stale_same_vm_claim_nonce(db_session):
    project, _resident, task, checkpoint, _message, _successor = (
        await _resident_turn_rows(db_session)
    )
    request = _request(
        project,
        task,
        checkpoint,
        agent_binding=ResidentAgentBinding(
            task_id=task.id,
            project_id=project.id,
            vm_instance_id=task.vm_instance_id,
            task_type="strategic_resident_turn",
            claim_nonce="superseded-claim-nonce",
        ),
    )

    with pytest.raises(ResidentAgentBindingError, match="nonce"):
        await reconcile_strategic_resident_turn(_factory(db_session), request)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_claim", ["project", "task_type"])
async def test_completion_post_rejects_same_task_wrong_token_scope(
    client,
    db_session,
    bad_claim,
):
    project, _resident, task, checkpoint, _message, _successor = (
        await _resident_turn_rows(db_session)
    )
    body = await _completion_body(db_session, project, task, checkpoint)
    token = create_agent_token(
        str(task.id),
        task.vm_instance_id,
        task_type=("create" if bad_claim == "task_type" else "strategic_resident_turn"),
        project_id=(str(uuid.uuid4()) if bad_claim == "project" else str(project.id)),
        claim_nonce=str(task.context[RESIDENT_CLAIM_NONCE_CONTEXT_KEY]),
    )
    response = await client.post(
        "/api/agent/resident-turn/complete",
        json=body,
        headers={"x-agent-token": token},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_task_scope", ["payload_type", "mutation"])
async def test_completion_post_rejects_locked_task_scope_change(
    client,
    db_session,
    bad_task_scope,
):
    project, _resident, task, checkpoint, _message, _successor = (
        await _resident_turn_rows(db_session)
    )
    body = await _completion_body(db_session, project, task, checkpoint)
    token = create_agent_token(
        str(task.id),
        task.vm_instance_id,
        task_type="strategic_resident_turn",
        project_id=str(project.id),
        claim_nonce=str(task.context[RESIDENT_CLAIM_NONCE_CONTEXT_KEY]),
    )
    if bad_task_scope == "payload_type":
        payload = dict(task.first_payload)
        payload["type"] = "create"
        task.payloads = [payload]
    else:
        context = dict(task.context or {})
        context["_resident_mutation_authorized"] = False
        task.context = context
    db_session.add(task)
    await db_session.commit()

    response = await client.post(
        "/api/agent/resident-turn/complete",
        json=body,
        headers={"x-agent-token": token},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_one_hundred_coalesced_messages_are_reconciled_without_cursor_loss(
    db_session,
):
    project, _resident, task, checkpoint, _message, _successor = (
        await _resident_turn_rows(db_session)
    )
    db_session.add_all(
        [
            ProjectMessage(
                project_id=project.id,
                actor_user_id="operator",
                sequence=sequence,
                surface=MessageSurface.CONVERSATION,
                message_type=MessageType.NOTE,
                from_type="user",
                content=f"rapid message {sequence}",
                context={},
            )
            for sequence in range(2, 101)
        ]
    )
    payload = dict(task.first_payload)
    payload["message_sequence_end"] = 100
    task.payloads = [payload]
    db_session.add(task)
    await db_session.commit()

    factory = _factory(db_session)
    task_id = task.id
    checkpoint_id = checkpoint.id
    expected_cursors = (48, 96, 100)
    for index, expected_cursor in enumerate(expected_cursors, start=1):
        async with factory() as prepare, prepare.begin():
            current = await prepare.get(Task, task_id)
            context = dict(current.context or {})
            context[RESIDENT_CLAIM_NONCE_CONTEXT_KEY] = f"burst-claim-{index}"
            current.context = context
            if index > 1:
                current.status = TaskStatus.IN_PROGRESS
                current.vm_instance_id = f"resident-turn-vm-{index}"
            prepare.add(current)
        async with factory() as read:
            current = await read.get(Task, task_id)
            current_checkpoint = await read.get(
                StrategicResidentCheckpoint,
                checkpoint_id,
            )
            provider_input = await reconstruct_authoritative_turn_input(
                read,
                project_id=project.id,
                turn_task_id=task_id,
            )
            request = _request(
                project,
                current,
                current_checkpoint,
                result=_reply_only_result(f"Handled batch {index}."),
                reconciliation_id=f"burst-reconciliation-{index}",
                context_hash=provider_input["lineage"]["context_hash"],
                agent_binding=ResidentAgentBinding(
                    task_id=current.id,
                    project_id=project.id,
                    vm_instance_id=current.vm_instance_id,
                    task_type="strategic_resident_turn",
                    claim_nonce=str(
                        (current.context or {}).get(
                            RESIDENT_CLAIM_NONCE_CONTEXT_KEY
                        )
                        or ""
                    ),
                ),
                accepted_memory_proposal_ids=(),
                accepted_implication_ids=(),
                accepted_open_loop_ids=(),
                resolved_open_loop_ids=(),
                evidence_inspections=(),
            )
        outcome = await reconcile_strategic_resident_turn(factory, request)
        async with factory() as verify:
            resident = await verify.get(StrategicResident, project.id)
            reply = await verify.get(ProjectMessage, outcome.reply_message_id)
            reply_target = await verify.get(
                ProjectMessage,
                reply.reply_to_message_id,
            )
            assert resident.handled_message_sequence == expected_cursor
            assert reply_target.sequence == expected_cursor
            if expected_cursor < 100:
                promoted = await verify.get(Task, outcome.promoted_task_id)
                promoted_payload = promoted.first_payload
                assert promoted_payload["message_sequence_start"] == (
                    expected_cursor + 1
                )
                assert promoted_payload["message_sequence_end"] == 100
                task_id = promoted.id
            else:
                assert outcome.promoted_task_id is None
            checkpoint_id = outcome.checkpoint_id


@pytest.mark.asyncio
@pytest.mark.parametrize("successor_lane", ["interactive", "background"])
async def test_reconciliation_atomically_updates_checkpoint_reply_cursors_and_pointer(
    db_session,
    successor_lane,
):
    project, _resident, task, checkpoint, user_message, successor = (
        await _resident_turn_rows(db_session, successor_lane=successor_lane)
    )
    factory = _factory(db_session)
    outcome = await reconcile_strategic_resident_turn(
        factory,
        _request(project, task, checkpoint),
    )

    assert outcome.checkpoint_version == 2
    assert outcome.reply_message_id is not None
    assert outcome.promoted_task_id == successor.id
    async with factory() as verify:
        resident = await verify.get(StrategicResident, project.id)
        completed = await verify.get(Task, task.id)
        promoted = await verify.get(Task, successor.id)
        next_checkpoint = await verify.get(
            StrategicResidentCheckpoint,
            outcome.checkpoint_id,
        )
        reply = await verify.get(ProjectMessage, outcome.reply_message_id)
        reconstructed = await reconstruct_authoritative_turn_input(
            verify,
            project_id=project.id,
            turn_task_id=successor.id,
        )
    assert resident.previous_checkpoint_id == checkpoint.id
    assert resident.current_checkpoint_id == outcome.checkpoint_id
    assert resident.handled_message_sequence == 1
    assert resident.handled_project_change_cursor == {"pipeline_revision": 4}
    assert resident.active_turn_task_id == successor.id
    assert resident.active_turn_lane == successor_lane
    assert completed.status == TaskStatus.COMPLETED
    assert promoted.context["_resident_input_frozen"] is True
    assert promoted.first_payload["checkpoint_version"] == 2
    assert promoted.first_payload["checkpoint_hash"] == outcome.checkpoint_hash
    assert reconstructed["lineage"]["checkpoint_version"] == 2
    assert reconstructed["lineage"]["checkpoint_hash"] == outcome.checkpoint_hash
    assert reply.from_type == "strategic"
    assert reply.surface == MessageSurface.CONVERSATION
    assert reply.reply_to_message_id == user_message.id
    assert reply.from_task_id == task.id
    assert next_checkpoint.content_hash == hashlib.sha256(
        _canonical(next_checkpoint.checkpoint)
    ).hexdigest()
    strategy = next_checkpoint.checkpoint["working_strategy"]
    assert strategy["priorities"]["qualification_before_rollout"] is True
    assert [item["loop_id"] for item in strategy["open_loops"]] == ["new-loop"]
    assert next_checkpoint.checkpoint["reconciliation"]["reply_message_id"] == str(
        reply.id
    )


@pytest.mark.asyncio
async def test_reconciliation_owns_all_unresolved_messages_in_represented_range(
    db_session,
):
    project, _resident, task, checkpoint, first_message, _successor = (
        await _resident_turn_rows(db_session, successor_lane=None)
    )
    prior_handler = Task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        context={},
    )
    db_session.add(prior_handler)
    await db_session.flush()
    second_message = ProjectMessage(
        project_id=project.id,
        actor_user_id="operator",
        sequence=2,
        surface=MessageSurface.CONVERSATION,
        handling_state=MessageHandlingState.CLAIMED,
        message_type=MessageType.NOTE,
        from_type="user",
        content="Also preserve my reply association.",
        reply_to_message_id=first_message.id,
        context={},
    )
    already_responded = ProjectMessage(
        project_id=project.id,
        actor_user_id="operator",
        sequence=3,
        surface=MessageSurface.CONVERSATION,
        handling_state=MessageHandlingState.RESPONDED,
        handled_by_task_id=prior_handler.id,
        message_type=MessageType.NOTE,
        from_type="user",
        content="This was already handled elsewhere.",
        context={},
    )
    activity = ProjectMessage(
        project_id=project.id,
        sequence=4,
        surface=MessageSurface.ACTIVITY,
        handling_state=MessageHandlingState.PENDING,
        from_type="user",
        content="Routine activity is not Resident conversation input.",
        context={},
    )
    system_message = ProjectMessage(
        project_id=project.id,
        sequence=5,
        surface=MessageSurface.CONVERSATION,
        handling_state=MessageHandlingState.PENDING,
        from_type="system",
        content="System projection is context, not operator work to claim.",
        context={},
    )
    payload = dict(task.first_payload)
    payload["message_sequence_end"] = 5
    task.payloads = [payload]
    db_session.add_all(
        [task, second_message, already_responded, activity, system_message]
    )
    await db_session.commit()
    provider_input = await reconstruct_authoritative_turn_input(
        db_session,
        project_id=project.id,
        turn_task_id=task.id,
    )

    outcome = await reconcile_strategic_resident_turn(
        _factory(db_session),
        _request(
            project,
            task,
            checkpoint,
            context_hash=provider_input["lineage"]["context_hash"],
        ),
    )

    async with _factory(db_session)() as verify:
        first = await verify.get(ProjectMessage, first_message.id)
        second = await verify.get(ProjectMessage, second_message.id)
        prior = await verify.get(ProjectMessage, already_responded.id)
        unchanged_activity = await verify.get(ProjectMessage, activity.id)
        unchanged_system = await verify.get(ProjectMessage, system_message.id)
        reply = await verify.get(ProjectMessage, outcome.reply_message_id)
        resident = await verify.get(StrategicResident, project.id)
    assert first.handling_state == MessageHandlingState.RESPONDED
    assert first.handled_by_task_id == task.id
    assert second.handling_state == MessageHandlingState.RESPONDED
    assert second.handled_by_task_id == task.id
    assert second.reply_to_message_id == first_message.id
    assert prior.handling_state == MessageHandlingState.RESPONDED
    assert prior.handled_by_task_id == prior_handler.id
    assert unchanged_activity.handling_state == MessageHandlingState.PENDING
    assert unchanged_activity.handled_by_task_id is None
    assert unchanged_system.handling_state == MessageHandlingState.PENDING
    assert unchanged_system.handled_by_task_id is None
    assert reply.reply_to_message_id == second_message.id
    assert resident.handled_message_sequence == 5


@pytest.mark.asyncio
async def test_reconciliation_rejects_foreign_unresolved_message_owner(db_session):
    project, resident, task, checkpoint, message, _successor = (
        await _resident_turn_rows(db_session, successor_lane=None)
    )
    foreign_handler = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "create"}],
        context={},
    )
    db_session.add(foreign_handler)
    await db_session.flush()
    message.handling_state = MessageHandlingState.CLAIMED
    message.handled_by_task_id = foreign_handler.id
    db_session.add(message)
    await db_session.commit()
    provider_input = await reconstruct_authoritative_turn_input(
        db_session,
        project_id=project.id,
        turn_task_id=task.id,
    )

    with pytest.raises(
        ReconciliationCursorError,
        match="owned by another Task",
    ):
        await reconcile_strategic_resident_turn(
            _factory(db_session),
            _request(
                project,
                task,
                checkpoint,
                context_hash=provider_input["lineage"]["context_hash"],
            ),
        )

    async with _factory(db_session)() as verify:
        unchanged_message = await verify.get(ProjectMessage, message.id)
        unchanged_resident = await verify.get(StrategicResident, project.id)
        replies = (
            (
                await verify.execute(
                    select(ProjectMessage).where(
                        ProjectMessage.project_id == project.id,
                        ProjectMessage.from_type == "strategic",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert unchanged_message.handling_state == MessageHandlingState.CLAIMED
    assert unchanged_message.handled_by_task_id == foreign_handler.id
    assert unchanged_resident.handled_message_sequence == (
        resident.handled_message_sequence
    )
    assert replies == []


@pytest.mark.asyncio
async def test_unaccepted_worker_memory_proposal_cannot_write_strategy(db_session):
    project, _resident, task, checkpoint, _message, _successor = (
        await _resident_turn_rows(db_session)
    )
    factory = _factory(db_session)
    request = _request(
        project,
        task,
        checkpoint,
        accepted_memory_proposal_ids=(),
        accepted_implication_ids=(),
        accepted_open_loop_ids=(),
        resolved_open_loop_ids=(),
        evidence_inspections=(),
    )
    outcome = await reconcile_strategic_resident_turn(factory, request)
    async with factory() as verify:
        next_checkpoint = await verify.get(
            StrategicResidentCheckpoint,
            outcome.checkpoint_id,
        )
    assert "decision" not in next_checkpoint.checkpoint["working_strategy"]
    assert (
        next_checkpoint.checkpoint["working_strategy"]["priorities"]
        == {"reliability": "high"}
    )


@pytest.mark.asyncio
async def test_reconciliation_is_idempotent_and_detects_result_collision(
    db_session,
    monkeypatch,
):
    project, _resident, task, checkpoint, _message, _successor = (
        await _resident_turn_rows(db_session, account_id="resident-complete-account")
    )
    account = {
        "id": "resident-complete-account",
        "name": "Resident complete",
        "active_tasks": 1,
        "project_state": {str(project.id): {"active_tasks": 1}},
    }
    monkeypatch.setitem(
        max_plan_pool._account_by_id,
        "resident-complete-account",
        account,
    )
    factory = _factory(db_session)
    request = _request(project, task, checkpoint)
    first = await reconcile_strategic_resident_turn(factory, request)
    replay = await reconcile_strategic_resident_turn(factory, request)
    assert replay.idempotent_replay is True
    assert replay.checkpoint_id == first.checkpoint_id
    assert replay.reply_message_id == first.reply_message_id
    assert account["active_tasks"] == 0
    assert account["project_state"][str(project.id)]["active_tasks"] == 0
    async with factory() as verify_release:
        released_task = await verify_release.get(Task, task.id)
    assert released_task.context["_slot_released"] is True
    with pytest.raises(OperationIdentityCollision):
        await reconcile_strategic_resident_turn(
            factory,
            _request(
                project,
                task,
                checkpoint,
                result=_turn_result(reply="A different result."),
            ),
        )
    async with factory() as verify:
        checkpoints = (
            (
                await verify.execute(
                    select(StrategicResidentCheckpoint).where(
                        StrategicResidentCheckpoint.project_id == project.id
                    )
                )
            )
            .scalars()
            .all()
        )
        replies = (
            (
                await verify.execute(
                    select(ProjectMessage).where(
                        ProjectMessage.project_id == project.id,
                        ProjectMessage.from_type == "strategic",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(checkpoints) == 2
    assert len(replies) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "request_change",
    [
        {"accepted_memory_proposal_ids": ()},
        {"accepted_open_loop_ids": ()},
        {"expected_turn_task_version": 999},
    ],
)
async def test_reconciliation_identity_covers_full_mutation_request(
    db_session,
    request_change,
):
    project, _resident, task, checkpoint, _message, _successor = (
        await _resident_turn_rows(db_session)
    )
    factory = _factory(db_session)
    await reconcile_strategic_resident_turn(
        factory,
        _request(project, task, checkpoint),
    )
    with pytest.raises(OperationIdentityCollision):
        await reconcile_strategic_resident_turn(
            factory,
            _request(project, task, checkpoint, **request_change),
        )


@pytest.mark.asyncio
async def test_reconciliation_identity_covers_evidence_inspection(db_session):
    project, _resident, task, checkpoint, _message, _successor = (
        await _resident_turn_rows(db_session)
    )
    factory = _factory(db_session)
    request = _request(project, task, checkpoint)
    await reconcile_strategic_resident_turn(factory, request)
    changed = request.evidence_inspections[0].model_copy(
        update={"inspected_at": "2026-07-16T12:01:00Z"}
    )
    with pytest.raises(OperationIdentityCollision):
        await reconcile_strategic_resident_turn(
            factory,
            _request(
                project,
                task,
                checkpoint,
                evidence_inspections=(changed,),
            ),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["strategy_change", "abandon_work", "merge", "deploy"])
async def test_high_impact_implication_requires_decisive_revisioned_evidence(
    db_session,
    kind,
):
    project, _resident, task, checkpoint, _message, _successor = (
        await _resident_turn_rows(db_session)
    )
    factory = _factory(db_session)
    result = _turn_result()
    result["strategic_implications"][0]["kind"] = kind
    result["evidence"][0]["revision"] = None
    with pytest.raises(DecisiveEvidenceRequired):
        await reconcile_strategic_resident_turn(
            factory,
            _request(project, task, checkpoint, result=result),
        )
    async with factory() as verify:
        assert (await verify.get(Task, task.id)).status == TaskStatus.IN_PROGRESS


@pytest.mark.asyncio
async def test_accepted_strategy_update_requires_matching_resident_inspection(db_session):
    project, _resident, task, checkpoint, _message, _successor = (
        await _resident_turn_rows(db_session)
    )
    factory = _factory(db_session)
    wrong = ResidentEvidenceInspection(
        evidence_id="evidence-1",
        kind="ci_run",
        source_id="run-42",
        revision="different-revision",
        checksum="f" * 64,
        inspected_at="2026-07-16T12:00:00Z",
        decisive=True,
    )
    with pytest.raises(DecisiveEvidenceRequired):
        await reconcile_strategic_resident_turn(
            factory,
            _request(
                project,
                task,
                checkpoint,
                evidence_inspections=(wrong,),
            ),
        )


@pytest.mark.asyncio
async def test_invented_evidence_and_matching_self_attestation_are_rejected(db_session):
    project, _resident, task, checkpoint, _message, _successor = (
        await _resident_turn_rows(db_session)
    )
    result = _turn_result()
    result["evidence"][0].update(
        source_id="invented-run",
        revision="invented-sha",
        checksum="e" * 64,
    )
    inspection = ResidentEvidenceInspection(
        evidence_id="evidence-1",
        kind="ci_run",
        source_id="invented-run",
        revision="invented-sha",
        checksum="e" * 64,
        inspected_at="2026-07-16T12:00:00Z",
        decisive=True,
    )
    with pytest.raises(DecisiveEvidenceRequired, match="frozen input"):
        await reconcile_strategic_resident_turn(
            _factory(db_session),
            _request(
                project,
                task,
                checkpoint,
                result=result,
                evidence_inspections=(inspection,),
            ),
        )


def _operation(operation_id: str, capability: str, status: str) -> dict:
    return {
        "operation_id": operation_id,
        "capability": capability,
        "status": status,
        "summary": "Durable operation outcome.",
    }


@pytest.mark.asyncio
async def test_fabricated_operation_receipt_is_rejected(db_session):
    project, _resident, task, checkpoint, _message, _successor = (
        await _resident_turn_rows(db_session)
    )
    result = _turn_result()
    result["operations"] = [_operation("op-1", "ci.run.inspect", "reconciled")]
    result["receipts"] = [
        {
            "operation_id": "op-1",
            "receipt_id": str(uuid.uuid4()),
            "status": "reconciled",
            "summary": "Fabricated receipt.",
        }
    ]
    with pytest.raises(ReconciliationResultInvalid):
        await reconcile_strategic_resident_turn(
            _factory(db_session),
            _request(project, task, checkpoint, result=result),
        )


@pytest.mark.asyncio
async def test_matching_durable_operation_receipt_is_accepted(db_session):
    project, _resident, task, checkpoint, _message, _successor = (
        await _resident_turn_rows(db_session)
    )
    receipt = StrategicOperationReceipt(
        project_id=project.id,
        turn_task_id=task.id,
        authority_generation=7,
        operation_id="op-1",
        operation_kind="cancel_task",
        capability="task.cancel",
        risk_level="low",
        request_hash="a" * 64,
        status="reconciled",
    )
    db_session.add(receipt)
    await db_session.commit()
    result = _turn_result()
    result["operations"] = [_operation("op-1", "task.cancel", "reconciled")]
    result["receipts"] = [
        {
            "operation_id": "op-1",
            "receipt_id": str(receipt.id),
            "status": "reconciled",
            "summary": "Durable mutation receipt.",
        }
    ]
    outcome = await reconcile_strategic_resident_turn(
        _factory(db_session),
        _request(project, task, checkpoint, result=result),
    )
    assert outcome.checkpoint_version == 2


@pytest.mark.asyncio
async def test_context_bound_inspection_receipt_can_supply_decisive_evidence(
    db_session,
):
    project, _resident, task, checkpoint, _message, _successor = (
        await _resident_turn_rows(db_session)
    )
    context_hash = task.context["_test_provider_context_hash"]
    receipt = StrategicOperationReceipt(
        project_id=project.id,
        turn_task_id=task.id,
        authority_generation=7,
        operation_id="op-inspection",
        operation_kind="inspect_ci",
        capability="ci.run.inspect",
        risk_level="low",
        request_hash="8" * 64,
        expected_revisions={"context_hash": context_hash},
        receipt={
            "schema_version": 1,
            "structured_result": {
                "evidence": [
                    {
                        "kind": "ci_run",
                        "source_id": "run-from-receipt",
                        "revision": "sha-from-receipt",
                        "checksum": "7" * 64,
                    }
                ]
            },
        },
        status="reconciled",
    )
    db_session.add(receipt)
    await db_session.commit()
    result = _turn_result()
    result["evidence"][0].update(
        source_id="run-from-receipt",
        revision="sha-from-receipt",
        checksum="7" * 64,
    )
    # Inspection receipts are server-authored, read-only evidence: the agent is
    # never handed their ids and does NOT claim them as operations. The evidence
    # is still trusted (via build_trusted_evidence_index over the locked ledger),
    # so the turn reconciles without claiming the inspection receipt.
    result["operations"] = []
    result["receipts"] = []
    inspection = ResidentEvidenceInspection(
        evidence_id="evidence-1",
        kind="ci_run",
        source_id="run-from-receipt",
        revision="sha-from-receipt",
        checksum="7" * 64,
        inspected_at="2026-07-16T12:00:00Z",
        decisive=True,
    )
    outcome = await reconcile_strategic_resident_turn(
        _factory(db_session),
        _request(
            project,
            task,
            checkpoint,
            result=result,
            evidence_inspections=(inspection,),
        ),
    )
    assert outcome.checkpoint_version == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("capability", "operation_kind", "status", "evidence_item"),
    [
        ("evil.inspect", "inspect_ci", "reconciled", None),
        ("ci.run.inspect", "deploy", "reconciled", None),
        ("ci.run.inspect", "inspect_ci", "failed", None),
        (
            "ci.run.inspect",
            "inspect_ci",
            "reconciled",
            {
                "kind": "ci_run",
                "artifact_id": "run-from-receipt",
                "revision": "sha-from-receipt",
                "checksum": "7" * 64,
            },
        ),
        (
            "ci.run.inspect",
            "inspect_ci",
            "reconciled",
            {
                "kind": "not_a_kind",
                "source_id": "run-from-receipt",
                "revision": "sha-from-receipt",
                "checksum": "7" * 64,
            },
        ),
    ],
)
async def test_inspection_receipt_spoofs_cannot_supply_evidence(
    db_session,
    capability,
    operation_kind,
    status,
    evidence_item,
):
    project, _resident, task, checkpoint, _message, _successor = (
        await _resident_turn_rows(db_session)
    )
    context_hash = task.context["_test_provider_context_hash"]
    valid_evidence = {
        "kind": "ci_run",
        "source_id": "run-from-receipt",
        "revision": "sha-from-receipt",
        "checksum": "7" * 64,
    }
    receipt = StrategicOperationReceipt(
        project_id=project.id,
        turn_task_id=task.id,
        authority_generation=7,
        operation_id="op-spoofed-inspection",
        operation_kind=operation_kind,
        capability=capability,
        risk_level="low",
        request_hash="6" * 64,
        expected_revisions={"context_hash": context_hash},
        receipt={
            "schema_version": 1,
            "structured_result": {"evidence": [evidence_item or valid_evidence]},
        },
        status=status,
    )
    db_session.add(receipt)
    await db_session.commit()
    from app.strategic_resident_evidence_policy import INSPECTION_CAPABILITY_KINDS

    result = _turn_result()
    result["evidence"][0].update(valid_evidence)
    # A real inspection capability's receipt is server-authored evidence, never
    # claimed as an operation (excluded from the claim invariants); an unknown
    # capability (evil.inspect) is treated as a mutation and must be claimed to
    # pass receipt validation. Either way the spoof is rejected downstream because
    # build_trusted_evidence_index refuses to trust a receipt with the wrong
    # capability/operation_kind/status/evidence, so the cited evidence is not in
    # the frozen trusted set.
    if capability in INSPECTION_CAPABILITY_KINDS:
        result["operations"] = []
        result["receipts"] = []
    else:
        result["operations"] = [
            _operation("op-spoofed-inspection", capability, "reconciled")
        ]
        result["receipts"] = [
            {
                "operation_id": "op-spoofed-inspection",
                "receipt_id": str(receipt.id),
                "status": status,
                "summary": "Spoofed typed inspection receipt.",
            }
        ]
    with pytest.raises(DecisiveEvidenceRequired, match="frozen input"):
        await reconcile_strategic_resident_turn(
            _factory(db_session),
            _request(project, task, checkpoint, result=result),
        )


@pytest.mark.asyncio
async def test_turn_with_unclaimed_inspection_receipt_reconciles(db_session):
    """Regression: a turn that inspected a task (leaving a server-authored
    inspection receipt in the ledger) reconciles WITHOUT claiming that receipt.

    Before inspection receipts were excluded from the operation-claim invariants,
    any inspect-then-complete turn failed reconciliation with 'must claim every
    durable turn receipt' — the agent is never handed the receipt id to claim.
    """
    project, _resident, task, checkpoint, _message, _successor = (
        await _resident_turn_rows(db_session)
    )
    context_hash = task.context["_test_provider_context_hash"]
    # A server-authored inspection receipt, exactly as /capability writes one.
    db_session.add(
        StrategicOperationReceipt(
            project_id=project.id,
            turn_task_id=task.id,
            authority_generation=7,
            operation_id=f"inspect:{task.id}:{'a' * 64}",
            operation_kind="inspect_task",
            capability="task.inspect",
            risk_level="low",
            request_hash="a" * 64,
            expected_revisions={"context_hash": context_hash},
            receipt={"schema_version": 1, "structured_result": {"evidence": []}},
            status="applied",
        )
    )
    await db_session.commit()

    # The turn result claims NO operations/receipts (the agent never saw the id).
    result = _turn_result()
    result["operations"] = []
    result["receipts"] = []
    outcome = await reconcile_strategic_resident_turn(
        _factory(db_session),
        _request(project, task, checkpoint, result=result),
    )
    assert outcome.checkpoint_version == 2


@pytest.mark.asyncio
async def test_orphan_operation_is_rejected_when_ledger_is_empty(db_session):
    project, _resident, task, checkpoint, _message, _successor = (
        await _resident_turn_rows(db_session)
    )
    result = _turn_result()
    result["operations"] = [_operation("orphan", "ci.run.inspect", "reconciled")]
    with pytest.raises(ReconciliationResultInvalid, match="map 1:1"):
        await reconcile_strategic_resident_turn(
            _factory(db_session),
            _request(project, task, checkpoint, result=result),
        )


@pytest.mark.asyncio
async def test_proposed_operation_remains_receipt_free(db_session):
    project, _resident, task, checkpoint, _message, _successor = (
        await _resident_turn_rows(db_session)
    )
    result = _turn_result()
    result["operations"] = [
        _operation("proposed-op", "deployment.execute", "proposed")
    ]
    outcome = await reconcile_strategic_resident_turn(
        _factory(db_session),
        _request(project, task, checkpoint, result=result),
    )
    assert outcome.checkpoint_version == 2


@pytest.mark.asyncio
async def test_operation_capability_must_match_durable_receipt(db_session):
    project, _resident, task, checkpoint, _message, _successor = (
        await _resident_turn_rows(db_session)
    )
    receipt = StrategicOperationReceipt(
        project_id=project.id,
        turn_task_id=task.id,
        authority_generation=7,
        operation_id="op-mismatch",
        operation_kind="cancel_task",
        capability="task.cancel",
        risk_level="low",
        request_hash="9" * 64,
        status="reconciled",
    )
    db_session.add(receipt)
    await db_session.commit()
    result = _turn_result()
    result["operations"] = [
        _operation("op-mismatch", "deployment.execute", "reconciled")
    ]
    result["receipts"] = [
        {
            "operation_id": "op-mismatch",
            "receipt_id": str(receipt.id),
            "status": "reconciled",
            "summary": "Durable receipt.",
        }
    ]
    with pytest.raises(ReconciliationResultInvalid, match="capability/status"):
        await reconcile_strategic_resident_turn(
            _factory(db_session),
            _request(project, task, checkpoint, result=result),
        )


@pytest.mark.asyncio
async def test_clean_success_resets_failure_retry_state(db_session):
    project, resident, task, checkpoint, _message, _successor = (
        await _resident_turn_rows(db_session)
    )
    resident.health_status = "recovering"
    resident.health_state = {"consecutive_turn_failures": 4}
    resident.next_wakeup_at = _utcnow() + timedelta(minutes=10)
    resident.recovery_state = {
        "status": "retrying",
        "failure_count": 4,
        "failure_category": "rate_limited",
    }
    db_session.add(resident)
    await db_session.commit()

    await reconcile_strategic_resident_turn(
        _factory(db_session),
        _request(project, task, checkpoint),
    )

    async with _factory(db_session)() as verify:
        stored = await verify.get(StrategicResident, project.id)
    assert stored.health_state["consecutive_turn_failures"] == 0
    assert stored.recovery_state["failure_count"] == 0
    assert stored.recovery_state["status"] == "recovered"
    assert stored.health_status == "healthy"
    assert stored.next_wakeup_at is None


@pytest.mark.asyncio
async def test_omitted_settled_durable_receipt_is_rejected(db_session):
    project, _resident, task, checkpoint, _message, _successor = (
        await _resident_turn_rows(db_session)
    )
    db_session.add(
        StrategicOperationReceipt(
            project_id=project.id,
            turn_task_id=task.id,
            authority_generation=7,
            operation_id="op-omitted-settled",
            operation_kind="cancel_task",
            capability="task.cancel",
            risk_level="low",
            request_hash="e" * 64,
            status="reconciled",
        )
    )
    await db_session.commit()

    with pytest.raises(ReconciliationResultInvalid, match="every durable"):
        await reconcile_strategic_resident_turn(
            _factory(db_session),
            _request(project, task, checkpoint),
        )


@pytest.mark.asyncio
async def test_unknown_operation_effect_is_admitted_once_and_fences_promotion(
    db_session,
):
    project, resident, task, checkpoint, _message, _successor = (
        await _resident_turn_rows(db_session, successor_lane="interactive")
    )
    resident.health_status = "recovering"
    resident.health_state = {"consecutive_turn_failures": 4}
    resident.recovery_state = {
        "status": "retrying",
        "failure_count": 4,
        "failure_category": "rate_limited",
    }
    resident.next_wakeup_at = _utcnow() + timedelta(minutes=10)
    receipt = StrategicOperationReceipt(
        project_id=project.id,
        turn_task_id=task.id,
        authority_generation=7,
        operation_id="op-unknown",
        operation_kind="deploy",
        capability="deployment.execute",
        risk_level="high",
        request_hash="b" * 64,
        status="unknown_effect",
        unknown_effects=[{"effect_id": "effect-1", "summary": "Commit uncertain"}],
    )
    db_session.add(receipt)
    await db_session.commit()
    result = _turn_result()
    result["operations"] = [
        _operation("op-unknown", "deployment.execute", "applying")
    ]
    result["receipts"] = [
        {
            "operation_id": "op-unknown",
            "receipt_id": str(receipt.id),
            "status": "unknown_effect",
            "summary": "Deployment receipt has an unknown external effect.",
        }
    ]
    result["unknown_effects"] = [
        {
            "effect_id": "effect-1",
            "operation_id": "op-unknown",
            "receipt_id": str(receipt.id),
            "summary": "Deployment commit outcome is uncertain.",
            "reconciliation_status": "unknown",
        }
    ]
    result["required_attention"] = [
        {
            "attention_id": "attention-1",
            "severity": "critical",
            "summary": "Reconcile the deployment receipt before retrying.",
            "evidence_ids": [],
            "receipt_ids": [str(receipt.id)],
        }
    ]
    request = _request(project, task, checkpoint, result=result)
    first = await reconcile_strategic_resident_turn(_factory(db_session), request)
    replay = await reconcile_strategic_resident_turn(_factory(db_session), request)
    assert replay.idempotent_replay is True
    assert replay.checkpoint_id == first.checkpoint_id
    assert replay.reply_message_id == first.reply_message_id
    assert first.promoted_task_id is None
    async with _factory(db_session)() as verify:
        resident = await verify.get(StrategicResident, project.id)
        active = await verify.get(Task, task.id)
        successor = await verify.get(Task, _successor.id)
        stored_receipt = await verify.get(StrategicOperationReceipt, receipt.id)
        checkpoints = (
            (
                await verify.execute(
                    select(StrategicResidentCheckpoint).where(
                        StrategicResidentCheckpoint.project_id == project.id
                    )
                )
            )
            .scalars()
            .all()
        )
        replies = (
            (
                await verify.execute(
                    select(ProjectMessage).where(
                        ProjectMessage.project_id == project.id,
                        ProjectMessage.from_type == "strategic",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert resident.current_checkpoint_id == first.checkpoint_id
    assert resident.active_turn_task_id is None
    assert resident.interactive_successor_task_id == successor.id
    assert resident.health_status == "blocked"
    assert resident.health_state["consecutive_turn_failures"] == 4
    assert resident.recovery_state["failure_count"] == 4
    assert resident.recovery_state["status"] == "unknown_effect"
    assert resident.next_wakeup_at is None
    assert resident.recovery_state["unknown_operation_receipt_ids"] == [
        str(receipt.id)
    ]
    assert active.status == TaskStatus.COMPLETED
    assert successor.status == TaskStatus.QUEUED
    assert successor.context["_resident_input_frozen"] is False
    assert stored_receipt.status == "unknown_effect"
    assert len(checkpoints) == 2
    assert len(replies) == 1


@pytest.mark.asyncio
async def test_omitted_unsettled_durable_receipt_rolls_back_reconciliation(db_session):
    project, resident, task, checkpoint, _message, successor = (
        await _resident_turn_rows(db_session, successor_lane="interactive")
    )
    receipt = StrategicOperationReceipt(
        project_id=project.id,
        turn_task_id=task.id,
        authority_generation=7,
        operation_id="op-omitted",
        operation_kind="deploy",
        capability="deployment.execute",
        risk_level="high",
        request_hash="c" * 64,
        status="unknown_effect",
        unknown_effects=[{"effect_id": "effect-omitted"}],
    )
    db_session.add(receipt)
    await db_session.commit()

    with pytest.raises(ReconciliationResultInvalid, match="omitted unsettled"):
        await reconcile_strategic_resident_turn(
            _factory(db_session),
            _request(project, task, checkpoint),
        )

    async with _factory(db_session)() as verify:
        stored_resident = await verify.get(StrategicResident, project.id)
        stored_task = await verify.get(Task, task.id)
        stored_successor = await verify.get(Task, successor.id)
    assert stored_resident.current_checkpoint_id == resident.current_checkpoint_id
    assert stored_resident.active_turn_task_id == task.id
    assert stored_task.status == TaskStatus.IN_PROGRESS
    assert stored_successor.status == TaskStatus.QUEUED


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["applying", "reconciling", "unknown_effect"])
async def test_receipt_only_unsettled_status_cannot_complete_turn(db_session, status):
    project, resident, task, checkpoint, _message, _successor = (
        await _resident_turn_rows(db_session)
    )
    receipt = StrategicOperationReceipt(
        project_id=project.id,
        turn_task_id=task.id,
        authority_generation=7,
        operation_id="op-receipt-only",
        operation_kind="deploy",
        capability="deployment.execute",
        risk_level="high",
        request_hash="d" * 64,
        status=status,
        unknown_effects=[{"effect_id": "effect-receipt-only"}],
    )
    db_session.add(receipt)
    await db_session.commit()
    result = _turn_result()
    result["receipts"] = [
        {
            "operation_id": receipt.operation_id,
            "receipt_id": str(receipt.id),
            "status": status,
            "summary": "The durable operation is not settled.",
        }
    ]

    with pytest.raises(ReconciliationResultInvalid, match="exact unknown-effect"):
        await reconcile_strategic_resident_turn(
            _factory(db_session),
            _request(project, task, checkpoint, result=result),
        )

    async with _factory(db_session)() as verify:
        stored_resident = await verify.get(StrategicResident, project.id)
        stored_task = await verify.get(Task, task.id)
    assert stored_resident.current_checkpoint_id == resident.current_checkpoint_id
    assert stored_resident.active_turn_task_id == task.id
    assert stored_task.status == TaskStatus.IN_PROGRESS


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["body_hash", "schema", "latest_lineage"])
async def test_checkpoint_corruption_after_context_fetch_rolls_back_atomically(
    db_session,
    monkeypatch,
    corruption,
):
    project, resident, task, checkpoint, _message, successor = (
        await _resident_turn_rows(db_session, successor_lane="interactive")
    )
    original_body = json.loads(json.dumps(checkpoint.checkpoint))
    real_reconstruct = reconstruct_authoritative_turn_input

    async def reconstruct_then_corrupt(session, **kwargs):
        provider_input = await real_reconstruct(session, **kwargs)
        locked = await session.get(StrategicResidentCheckpoint, checkpoint.id)
        if corruption == "body_hash":
            locked.checkpoint = {**locked.checkpoint, "corrupted_after_context": True}
            session.add(locked)
        elif corruption == "schema":
            locked.schema_version = 2
            session.add(locked)
        else:
            next_body = {**locked.checkpoint, "newer_after_context": True}
            session.add(
                StrategicResidentCheckpoint(
                    project_id=project.id,
                    version=2,
                    schema_version=1,
                    content_hash=hashlib.sha256(_canonical(next_body)).hexdigest(),
                    checkpoint=next_body,
                    authority_generation=7,
                    source_turn_task_id=task.id,
                )
            )
        await session.flush()
        return provider_input

    monkeypatch.setattr(
        "app.strategic_resident_reconciliation.reconstruct_authoritative_turn_input",
        reconstruct_then_corrupt,
    )
    with pytest.raises(ExpectedRevisionMismatch, match="integrity validation"):
        await reconcile_strategic_resident_turn(
            _factory(db_session),
            _request(project, task, checkpoint),
        )

    async with _factory(db_session)() as verify:
        stored_checkpoint = await verify.get(StrategicResidentCheckpoint, checkpoint.id)
        stored_resident = await verify.get(StrategicResident, project.id)
        stored_task = await verify.get(Task, task.id)
        stored_successor = await verify.get(Task, successor.id)
        checkpoints = (
            (
                await verify.execute(
                    select(StrategicResidentCheckpoint).where(
                        StrategicResidentCheckpoint.project_id == project.id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert stored_checkpoint.checkpoint == original_body
    assert stored_checkpoint.schema_version == 1
    assert stored_resident.current_checkpoint_id == resident.current_checkpoint_id
    assert stored_resident.active_turn_task_id == task.id
    assert stored_task.status == TaskStatus.IN_PROGRESS
    assert stored_successor.status == TaskStatus.QUEUED
    assert len(checkpoints) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"generation": 8}, AuthorityGenerationMismatch),
        ({"expected_turn_task_version": 999}, ExpectedRevisionMismatch),
        ({"expected_checkpoint_hash": "0" * 64}, ExpectedRevisionMismatch),
    ],
)
async def test_stale_generation_task_or_checkpoint_lineage_is_fenced(
    db_session,
    change,
    error,
):
    project, _resident, task, checkpoint, _message, _successor = (
        await _resident_turn_rows(db_session)
    )
    factory = _factory(db_session)
    with pytest.raises(error):
        await reconcile_strategic_resident_turn(
            factory,
            _request(project, task, checkpoint, **change),
        )


@pytest.mark.asyncio
async def test_non_active_or_non_authorized_worker_cannot_reconcile(db_session):
    project, resident, task, checkpoint, _message, _successor = (
        await _resident_turn_rows(db_session)
    )
    factory = _factory(db_session)
    task.context = {**task.context, "_resident_mutation_authorized": False}
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    with pytest.raises(ResidentAgentBindingError):
        await reconcile_strategic_resident_turn(
            factory,
            _request(project, task, checkpoint),
        )
    resident.active_turn_task_id = None
    resident.active_turn_lane = None
    db_session.add(resident)
    await db_session.commit()


@pytest.mark.asyncio
async def test_interactive_result_requires_reply_and_unknown_open_loop_is_rejected(
    db_session,
):
    project, _resident, task, checkpoint, _message, _successor = (
        await _resident_turn_rows(db_session)
    )
    factory = _factory(db_session)
    with pytest.raises(ReconciliationResultInvalid):
        await reconcile_strategic_resident_turn(
            factory,
            _request(project, task, checkpoint, result=_turn_result(reply="")),
        )
    with pytest.raises(ReconciliationResultInvalid):
        await reconcile_strategic_resident_turn(
            factory,
            _request(
                project,
                task,
                checkpoint,
                resolved_open_loop_ids=("not-represented",),
            ),
        )


def test_structured_turn_result_rejects_adversarial_oversize():
    result = _turn_result()
    result["facts"] = [
        {
            "fact_id": f"fact-{index}",
            "statement": "x" * 4_000,
            "evidence_ids": [],
        }
        for index in range(50)
    ]
    with pytest.raises(OperationResultTooLarge):
        # Validation/bounding occurs before any database authority is acquired.
        from app.strategic_resident_turn_results import (
            normalize_strategic_resident_turn_result,
        )

        normalize_strategic_resident_turn_result(result)


@pytest.mark.asyncio
async def test_reconciliation_schedules_earliest_meaningful_follow_up(db_session):
    project, resident, task, checkpoint, _message, _successor = (
        await _resident_turn_rows(db_session)
    )
    result = _turn_result()
    explicit = _utcnow() + timedelta(hours=1)
    later = explicit + timedelta(hours=2)
    resident.health_status = "recovering"
    resident.health_state = {"consecutive_turn_failures": 3}
    resident.recovery_state = {"status": "retrying", "failure_count": 3}
    resident.next_wakeup_at = _utcnow() + timedelta(minutes=10)
    db_session.add(resident)
    await db_session.commit()
    result["follow_up"]["next_wakeup_at"] = explicit.isoformat() + "Z"
    result["follow_up"]["open_loops"][0]["due_at"] = later.isoformat() + "Z"
    await reconcile_strategic_resident_turn(
        _factory(db_session),
        _request(project, task, checkpoint, result=result),
    )
    async with _factory(db_session)() as verify:
        resident = await verify.get(StrategicResident, project.id)
    assert resident.next_wakeup_at == explicit
    assert resident.health_state["consecutive_turn_failures"] == 0
    assert resident.recovery_state["failure_count"] == 0
    assert resident.scheduling_state["deadline_reason"] == "follow_up"
    assert resident.scheduling_state["deadline_source"] == "follow_up"


@pytest.mark.asyncio
async def test_overdue_open_loop_schedules_immediate_meaningful_wakeup(db_session):
    project, _resident, task, checkpoint, _message, _successor = (
        await _resident_turn_rows(db_session)
    )
    started = _utcnow()
    result = _turn_result()
    result["follow_up"]["open_loops"][0]["due_at"] = "2020-01-01T00:00:00Z"
    await reconcile_strategic_resident_turn(
        _factory(db_session),
        _request(project, task, checkpoint, result=result),
    )
    async with _factory(db_session)() as verify:
        resident = await verify.get(StrategicResident, project.id)
    assert resident.next_wakeup_at >= started
    assert resident.scheduling_state["deadline_reason"] == "overdue_commitment"
    assert resident.scheduling_state["deadline_source"] == "open_loop_deadline"


@pytest.mark.asyncio
async def test_malformed_follow_up_timestamp_rolls_back_reconciliation(db_session):
    project, _resident, task, checkpoint, _message, _successor = (
        await _resident_turn_rows(db_session)
    )
    result = _turn_result()
    result["follow_up"]["next_wakeup_at"] = "tomorrow-ish"
    with pytest.raises(ReconciliationResultInvalid):
        await reconcile_strategic_resident_turn(
            _factory(db_session),
            _request(project, task, checkpoint, result=result),
        )
    async with _factory(db_session)() as verify:
        resident = await verify.get(StrategicResident, project.id)
        active = await verify.get(Task, task.id)
    assert resident.current_checkpoint_id == checkpoint.id
    assert resident.active_turn_task_id == task.id
    assert active.status == TaskStatus.IN_PROGRESS


@pytest.mark.parametrize(
    "spoof",
    [
        {"source_id": "run", "artifact_id": "other"},
        {"source_id": "r" * 513},
        {"revision": "v" * 513},
        {"checksum": "h" * 256 + "suffix"},
        {"kind": "ci_run_alias"},
    ],
)
def test_evidence_identity_spoofs_are_never_truncated_or_aliased(spoof):
    item = {
        "kind": "ci_run",
        "source_id": "run",
        "revision": "sha",
        "checksum": "h" * 256,
        **spoof,
    }
    assert evidence_key(item, allow_source_aliases=True) is None


def test_receipt_evidence_requires_exact_source_id_not_an_alias():
    item = {
        "kind": "ci_run",
        "artifact_id": "run",
        "revision": "sha",
        "checksum": "h" * 64,
    }
    assert evidence_key(item, allow_source_aliases=False) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation_status", "receipt_status", "match"),
    [
        ("applied", "reconciled", "capability/status"),
        ("failed", "failed", "receipt claim"),
    ],
)
async def test_operation_and_receipt_statuses_match_durable_ledger_exactly(
    db_session,
    operation_status,
    receipt_status,
    match,
):
    project, _resident, task, checkpoint, _message, _successor = (
        await _resident_turn_rows(db_session)
    )
    receipt = StrategicOperationReceipt(
        project_id=project.id,
        turn_task_id=task.id,
        authority_generation=7,
        operation_id="op-status-mismatch",
        operation_kind="cancel_task",
        capability="task.cancel",
        risk_level="low",
        request_hash="5" * 64,
        status="reconciled",
    )
    db_session.add(receipt)
    await db_session.commit()
    result = _turn_result()
    result["operations"] = [
        _operation("op-status-mismatch", "task.cancel", operation_status)
    ]
    result["receipts"] = [
        {
            "operation_id": "op-status-mismatch",
            "receipt_id": str(receipt.id),
            "status": receipt_status,
            "summary": "Deliberately mismatched status.",
        }
    ]
    with pytest.raises(ReconciliationResultInvalid, match=match):
        await reconcile_strategic_resident_turn(
            _factory(db_session),
            _request(project, task, checkpoint, result=result),
        )
