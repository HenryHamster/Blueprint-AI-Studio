"""Crash/reaper coverage for authoritative Strategic Resident turns."""

from __future__ import annotations

import hashlib
import uuid

import pytest

from app import max_plan_pool
from app.models import (
    Project,
    StrategicOperationReceipt,
    StrategicResident,
    Task,
    TaskStatus,
)
from app.strategic_resident_agent_binding import (
    RESIDENT_CLAIM_ATTEMPT_CONTEXT_KEY,
    RESIDENT_CLAIM_NONCE_CONTEXT_KEY,
)
from app.strategic_resident_failure_recovery import (
    reap_authoritative_resident_turn,
)


def _turn(
    project_id: uuid.UUID,
    *,
    frozen: bool,
    start: int,
    end: int,
    status: TaskStatus = TaskStatus.QUEUED,
) -> Task:
    return Task(
        project_id=project_id,
        status=status,
        vm_instance_id="resident-reaper-vm" if frozen else None,
        priority=95,
        payloads=[
            {
                "type": "strategic_resident_turn",
                "resident_id": str(project_id),
                "lane": "interactive",
                "authority_generation": 4,
                "engine_owner": "strategic_resident",
                "shadow_only": False,
                "trigger_kind": "operator_message",
                "checkpoint_version": 1,
                "checkpoint_hash": "a" * 64,
                "message_sequence_start": start,
                "message_sequence_end": end,
            }
        ],
        context={
            "_resident_mutation_authorized": True,
            "_resident_shadow_only": False,
            "_resident_input_frozen": frozen,
            "_resident_project_id": str(project_id),
            "_resident_authority_generation": 4,
            "_resident_engine_owner_observed": "strategic_resident",
            **(
                {
                    RESIDENT_CLAIM_NONCE_CONTEXT_KEY: "live-claim-nonce",
                    RESIDENT_CLAIM_ATTEMPT_CONTEXT_KEY: "live-attempt-jti",
                }
                if frozen
                else {}
            ),
        },
        archived=True,
    )


async def _seed(db_session, *, with_successor: bool):
    project = Project(
        name=f"Resident reaper {uuid.uuid4()}",
        user_id="operator",
        vision={},
        problem_model={},
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    active = _turn(
        project.id,
        frozen=True,
        start=10,
        end=12,
        status=TaskStatus.IN_PROGRESS,
    )
    successor = (
        _turn(project.id, frozen=False, start=13, end=15)
        if with_successor
        else None
    )
    resident = StrategicResident(
        project_id=project.id,
        engine_owner="strategic_resident",
        engine_version="resident-v2",
        authority_generation=4,
        background_policy="paused",
    )
    db_session.add(active)
    if successor is not None:
        db_session.add(successor)
    db_session.add(resident)
    await db_session.flush()
    resident.active_turn_task_id = active.id
    resident.active_turn_lane = "interactive"
    if successor is not None:
        resident.interactive_successor_task_id = successor.id
    await db_session.commit()
    return project, resident, active, successor


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "receipt_status",
    ["pending", "applying", "reconciling", "unknown_effect"],
)
async def test_reaper_blocks_retry_when_turn_receipt_is_unsettled(
    db_session,
    receipt_status,
):
    project, resident, active, successor = await _seed(
        db_session, with_successor=True
    )
    receipt = StrategicOperationReceipt(
        project_id=project.id,
        turn_task_id=active.id,
        authority_generation=4,
        operation_id="deploy-1",
        operation_kind="deployment",
        capability="deployment.redeploy",
        request_hash=hashlib.sha256(b"deploy-1").hexdigest(),
        status=receipt_status,
    )
    db_session.add(receipt)
    await db_session.commit()

    outcome = await reap_authoritative_resident_turn(
        db_session,
        project_id=project.id,
        task_id=active.id,
        reap_reason="No heartbeat after deployment dispatch",
        reap_reason_code="heartbeat_timeout_post_claim",
    )

    assert outcome.promoted_task_id is None
    assert outcome.unsettled_receipt_ids == (str(receipt.id),)
    await db_session.refresh(active)
    await db_session.refresh(successor)
    await db_session.refresh(resident)
    assert active.status == TaskStatus.FAILED
    assert RESIDENT_CLAIM_NONCE_CONTEXT_KEY not in active.context
    assert RESIDENT_CLAIM_ATTEMPT_CONTEXT_KEY not in active.context
    assert resident.active_turn_task_id is None
    assert resident.interactive_successor_task_id == successor.id
    assert resident.recovery_state["status"] == "unknown_effect"
    assert resident.recovery_state["unknown_operation_receipt_ids"] == [
        str(receipt.id)
    ]
    payload = successor.first_payload
    assert payload["message_sequence_start"] == 10
    assert payload["message_sequence_end"] == 15
    assert successor.context["_resident_input_frozen"] is False


@pytest.mark.asyncio
async def test_reaper_preserves_paused_interactive_retry_with_backoff(db_session):
    project, resident, active, _successor = await _seed(
        db_session, with_successor=False
    )
    active.context = {
        **active.context,
        "llm_account_id": "dead-account",
        "llm_provider": "openai",
        "llm_model": "gpt-dead",
        "runner_target_id": "dead-runner",
    }
    db_session.add(active)
    await db_session.commit()

    outcome = await reap_authoritative_resident_turn(
        db_session,
        project_id=project.id,
        task_id=active.id,
        reap_reason="Agent process exited",
        reap_reason_code="heartbeat_timeout_post_claim",
    )

    assert outcome.promoted_task_id is not None
    promoted = await db_session.get(Task, outcome.promoted_task_id)
    await db_session.refresh(resident)
    assert promoted is not None
    assert resident.background_policy == "paused"
    assert resident.active_turn_task_id == promoted.id
    assert resident.active_turn_lane == "interactive"
    assert resident.interactive_successor_task_id is None
    assert promoted.first_payload["message_sequence_start"] == 10
    assert promoted.first_payload["message_sequence_end"] == 12
    assert promoted.first_payload["trigger_kind"] == "operator_message"
    assert promoted.first_payload["recovery_source_turn_task_id"] == str(active.id)
    assert promoted.context["_resident_input_frozen"] is True
    assert RESIDENT_CLAIM_NONCE_CONTEXT_KEY not in promoted.context
    assert RESIDENT_CLAIM_ATTEMPT_CONTEXT_KEY not in promoted.context
    assert "llm_account_id" not in promoted.context
    assert "llm_provider" not in promoted.context
    assert "llm_model" not in promoted.context
    assert "runner_target_id" not in promoted.context
    assert promoted.retry_after_until is not None
    delay = (promoted.retry_after_until - active.completed_at).total_seconds()
    assert 0 < delay <= 1_800


@pytest.mark.asyncio
async def test_reaper_does_not_promote_background_retry_while_paused(db_session):
    project, resident, active, successor = await _seed(
        db_session, with_successor=True
    )
    active.payloads = [{**active.first_payload, "lane": "background"}]
    successor.payloads = [{**successor.first_payload, "lane": "background"}]
    resident.active_turn_lane = "background"
    resident.interactive_successor_task_id = None
    resident.background_successor_task_id = successor.id
    db_session.add(active)
    db_session.add(successor)
    db_session.add(resident)
    await db_session.commit()

    outcome = await reap_authoritative_resident_turn(
        db_session,
        project_id=project.id,
        task_id=active.id,
        reap_reason="Agent process exited after pause",
        reap_reason_code="heartbeat_timeout_post_claim",
    )

    assert outcome.promoted_task_id is None
    await db_session.refresh(resident)
    await db_session.refresh(successor)
    assert resident.active_turn_task_id is None
    assert resident.active_turn_lane is None
    assert resident.background_successor_task_id == successor.id
    assert successor.context["_resident_input_frozen"] is False


@pytest.mark.asyncio
async def test_reaper_broadcast_failure_does_not_undo_durable_recovery(
    db_session,
    mocker,
    monkeypatch,
):
    project, resident, active, _successor = await _seed(
        db_session, with_successor=False
    )
    active.context = {
        **(active.context or {}),
        "llm_account_id": "resident-reaper-account",
    }
    active.llm_account_id = "resident-reaper-account"
    db_session.add(active)
    await db_session.commit()
    account = {
        "id": "resident-reaper-account",
        "name": "Resident reaper",
        "active_tasks": 1,
        "project_state": {str(project.id): {"active_tasks": 1}},
    }
    monkeypatch.setitem(
        max_plan_pool._account_by_id,
        "resident-reaper-account",
        account,
    )
    mocker.patch(
        "app.strategic_resident_terminal.broadcast_to_project",
        side_effect=RuntimeError("realtime unavailable"),
    )

    outcome = await reap_authoritative_resident_turn(
        db_session,
        project_id=project.id,
        task_id=active.id,
        reap_reason="Agent process exited",
        reap_reason_code="heartbeat_timeout_post_claim",
    )

    assert outcome.promoted_task_id is not None
    await db_session.refresh(active)
    await db_session.refresh(resident)
    assert active.status == TaskStatus.FAILED
    assert active.context["_slot_released"] is True
    assert account["active_tasks"] == 0
    assert account["project_state"][str(project.id)]["active_tasks"] == 0
    assert resident.active_turn_task_id == outcome.promoted_task_id
