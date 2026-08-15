"""Focused phase-truth and realtime-ordering tests for Workday consultations."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from inspect import unwrap
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

import app.routers.agent_strategic_messages as messages_router_module
import app.strategic_consultation_status as consultation_status_module
import app.strategic_resident_session as resident_session_module
from app.auth import create_agent_token
from app.main import app
from app.models import (
    MessageHandlingState,
    MessageSurface,
    MessageType,
    Project,
    ProjectMessage,
    StrategicMemorySource,
    Task,
    TaskStatus,
)
from app.routers.agent_strategic_messages import router as _messages_router
from app.strategic_consultation_status import (
    VerifiedConsultationActivity,
    consultation_activity_is_verified,
    derive_verified_consultation_activity,
)
from app.strategic_resident_session import RESIDENT_SESSION_KEY
from app.time_utils import _utcnow


_POLL_PATH = "/api/agent/projects/{project_id}/messages"
if not any(getattr(route, "path", None) == _POLL_PATH for route in app.routes):
    app.include_router(_messages_router)


def _headers(task: Task) -> dict[str, str]:
    return {
        "x-agent-token": create_agent_token(
            str(task.id),
            "agent-vm-consultation-status",
        )
    }


async def _make_resident(db_session) -> tuple[Project, Task, str]:
    project = Project(name="Consultation status", user_id="consultation-status-user")
    db_session.add(project)
    await db_session.commit()
    task = Task(
        project_id=project.id,
        payloads=[{}],
        vm_instance_id="agent-vm-consultation-status",
        status=TaskStatus.IN_PROGRESS,
        context={
            "_resident_session": True,
            "strategic_session_id": "resident-session",
            "_resident_worker_generation": 2,
        },
    )
    incarnation = str(uuid.uuid4())
    project.vision = {
        RESIDENT_SESSION_KEY: {
            "session_id": "resident-session",
            "task_id": str(task.id),
            "worker_generation": 2,
            "worker_incarnation_id": incarnation,
            "worker_incarnation_expires_at": (
                datetime.now(timezone.utc) + timedelta(minutes=5)
            ).isoformat(),
            "status": "active",
            "activity": {
                "phase": "waiting",
                "last_action": "Waiting for project movement",
                "reason_code": None,
                "consultation_id": None,
                "consultation_verified": False,
                "autonomy_state": "active",
                "autonomy_reason_code": None,
            },
        }
    }
    flag_modified(project, "vision")
    db_session.add_all([project, task])
    await db_session.commit()
    return project, task, incarnation


@pytest.mark.asyncio
async def test_consultation_activity_verification_requires_live_canonical_packet(
    db_session,
):
    project = Project(name="activity correlation", user_id="activity-user")
    db_session.add(project)
    await db_session.commit()
    consultation_id = uuid.uuid4()
    inbound = ProjectMessage(
        project_id=project.id,
        sequence=1,
        surface=MessageSurface.CONVERSATION,
        from_type="user",
        content="Please ask Workday to investigate.",
        resident_session_id="resident-session",
    )
    source = ProjectMessage(
        id=consultation_id,
        project_id=project.id,
        sequence=2,
        surface=MessageSurface.CONVERSATION,
        from_type="strategic",
        content="I asked Workday to investigate.",
        resident_session_id="resident-session",
        reply_to_message_id=inbound.id,
        context={
            "message_role": "operator_reply",
            "_workday_consultation_requested": True,
            "_conversation_public_reply_owner": "fast",
        },
    )
    packet = ProjectMessage(
        project_id=project.id,
        sequence=3,
        surface=MessageSurface.CONVERSATION,
        from_type="system",
        content="private request",
        resident_session_id="resident-session",
        status="open",
        context={
            "audience": "strategist-internal",
            "message_role": "workday_consultation",
            "public_reply_owner": "fast",
            "_conversation_consultation_packet": True,
            "_workday_consultation_atomic_reply_id": str(consultation_id),
        },
    )
    transcript_churn = [
        ProjectMessage(
            project_id=project.id,
            sequence=sequence,
            surface=MessageSurface.CONVERSATION,
            from_type="user",
            content=f"later message {sequence}",
            resident_session_id="resident-session",
        )
        for sequence in range(4, 39)
    ]
    db_session.add_all([inbound, source, packet, *transcript_churn])
    await db_session.commit()

    expected_queued = VerifiedConsultationActivity(
        consultation_id=consultation_id,
        reason_code="consultation_queued",
        phase="waiting",
    )
    assert (
        await derive_verified_consultation_activity(
            db_session,
            project_id=project.id,
            resident_session_id="resident-session",
        )
        == expected_queued
    )

    source.context = {**source.context, "privacy_state": "redacted"}
    db_session.add(source)
    await db_session.commit()
    assert (
        await derive_verified_consultation_activity(
            db_session,
            project_id=project.id,
            resident_session_id="resident-session",
        )
        is None
    )
    source.context = {
        key: value for key, value in source.context.items() if key != "privacy_state"
    }
    db_session.add(source)
    await db_session.commit()

    source_receipt = (
        await db_session.execute(
            select(StrategicMemorySource).where(
                StrategicMemorySource.project_id == project.id,
                StrategicMemorySource.source_type == "project_message",
                StrategicMemorySource.source_key == str(source.id),
            )
        )
    ).scalar_one()
    source_receipt.payload_state = "redacted"
    source_receipt.tombstoned_at = _utcnow()
    db_session.add(source_receipt)
    await db_session.commit()
    assert (
        await derive_verified_consultation_activity(
            db_session,
            project_id=project.id,
            resident_session_id="resident-session",
        )
        is None
    )
    source_receipt.payload_state = "active"
    source_receipt.tombstoned_at = None
    db_session.add(source_receipt)
    await db_session.commit()

    queued_activity = {
        "reason_code": "consultation_queued",
        "consultation_id": str(consultation_id),
    }
    running_activity = {
        "reason_code": "consultation_running",
        "consultation_id": str(consultation_id),
    }
    assert await consultation_activity_is_verified(
        db_session,
        project_id=project.id,
        resident_session_id="resident-session",
        activity=queued_activity,
    )
    assert not await consultation_activity_is_verified(
        db_session,
        project_id=project.id,
        resident_session_id="resident-session",
        activity=running_activity,
    )

    task = Task(project_id=project.id, payloads=[{}], status=TaskStatus.IN_PROGRESS)
    db_session.add(task)
    await db_session.commit()
    workday_claim_id = uuid.uuid4()
    packet.handling_state = MessageHandlingState.CLAIMED
    packet.handled_by_task_id = task.id
    packet.context = {
        **packet.context,
        "_workday_consultation_claim_id": str(workday_claim_id),
        "_workday_consultation_claim_generation": 1,
        "_workday_consultation_claim_incarnation": str(uuid.uuid4()),
        "_workday_consultation_claim_expires_at": (
            _utcnow() + timedelta(minutes=1)
        ).isoformat(),
    }
    db_session.add(packet)
    await db_session.commit()
    assert await derive_verified_consultation_activity(
        db_session,
        project_id=project.id,
        resident_session_id="resident-session",
    ) == VerifiedConsultationActivity(
        consultation_id=consultation_id,
        reason_code="consultation_running",
        phase="working",
    )
    assert await consultation_activity_is_verified(
        db_session,
        project_id=project.id,
        resident_session_id="resident-session",
        activity=running_activity,
    )

    packet.context = {
        **packet.context,
        "_workday_consultation_claim_expires_at": (
            _utcnow() - timedelta(seconds=1)
        ).isoformat(),
    }
    db_session.add(packet)
    await db_session.commit()
    assert (
        await derive_verified_consultation_activity(
            db_session,
            project_id=project.id,
            resident_session_id="resident-session",
        )
        is None
    )

    packet.status = "handled"
    packet.handling_state = MessageHandlingState.RESPONDED
    result = ProjectMessage(
        project_id=project.id,
        sequence=39,
        surface=MessageSurface.CONVERSATION,
        from_type="system",
        content="private findings",
        resident_session_id="resident-session",
        status="open",
        handling_state=MessageHandlingState.PENDING,
        context={
            "audience": "strategist-internal",
            "message_role": "workday_consultation_result",
            "public_reply_owner": "fast",
            "_workday_consultation_result": True,
            "_workday_consultation_atomic_reply_id": str(consultation_id),
            "source_packet_id": str(packet.id),
            "_workday_consultation_ack_claim_id": str(workday_claim_id),
        },
    )
    db_session.add_all([packet, result])
    await db_session.commit()
    for result_state in (
        MessageHandlingState.PENDING,
        MessageHandlingState.CLAIMED,
    ):
        result.handling_state = result_state
        result.handled_by_task_id = (
            task.id if result_state == MessageHandlingState.CLAIMED else None
        )
        db_session.add(result)
        await db_session.commit()
        assert await derive_verified_consultation_activity(
            db_session,
            project_id=project.id,
            resident_session_id="resident-session",
        ) == VerifiedConsultationActivity(
            consultation_id=consultation_id,
            reason_code="consultation_delivering",
            phase="working",
        )

    result.status = "handled"
    result.handling_state = MessageHandlingState.RESPONDED
    db_session.add(result)
    await db_session.commit()
    assert (
        await derive_verified_consultation_activity(
            db_session,
            project_id=project.id,
            resident_session_id="resident-session",
        )
        is None
    )
    assert (
        await derive_verified_consultation_activity(
            db_session,
            project_id=project.id,
            resident_session_id="other-session",
        )
        is None
    )


@pytest.mark.asyncio
async def test_heartbeat_emits_only_ordered_semantic_activity_transitions(monkeypatch):
    consultation_id = "11111111-1111-4111-8111-111111111111"
    task_id = str(uuid.uuid4())
    project = SimpleNamespace(
        id=uuid.uuid4(),
        vision={
            RESIDENT_SESSION_KEY: {
                "session_id": "semantic-events",
                "task_id": task_id,
                "status": "active",
                "activity": {
                    "phase": "waiting",
                    "last_action": "Waiting",
                    "reason_code": None,
                    "consultation_id": None,
                    "autonomy_state": "active",
                    "autonomy_reason_code": None,
                },
            }
        },
    )
    session = AsyncMock()
    monkeypatch.setattr(
        resident_session_module,
        "_resolve_agent_project",
        AsyncMock(return_value=project),
    )
    monkeypatch.setattr(
        consultation_status_module,
        "derive_verified_consultation_activity",
        AsyncMock(
            side_effect=[
                VerifiedConsultationActivity(
                    consultation_id=uuid.UUID(consultation_id),
                    reason_code="consultation_queued",
                    phase="waiting",
                ),
                VerifiedConsultationActivity(
                    consultation_id=uuid.UUID(consultation_id),
                    reason_code="consultation_queued",
                    phase="waiting",
                ),
                VerifiedConsultationActivity(
                    consultation_id=uuid.UUID(consultation_id),
                    reason_code="consultation_running",
                    phase="working",
                ),
                None,
            ]
        ),
    )

    renewed: list[dict] = []

    async def renew(_session, current_project, **kwargs):
        verified = (
            await consultation_status_module.derive_verified_consultation_activity(
                _session,
                project_id=current_project.id,
                resident_session_id=kwargs["session_id"],
            )
        )
        effective_activity = consultation_status_module.reconcile_consultation_activity(
            kwargs["activity"],
            verified,
        )
        recorded = {
            **kwargs,
            "activity": effective_activity,
            "consultation_verified": verified is not None,
        }
        renewed.append(recorded)
        resident = dict(current_project.vision[RESIDENT_SESSION_KEY])
        resident["activity"] = {
            **effective_activity,
            "consultation_verified": verified is not None,
        }
        current_project.vision = {
            **current_project.vision,
            RESIDENT_SESSION_KEY: resident,
        }
        return {
            "authority_held": True,
            "resident_incarnation_held": True,
            "stop_requested": False,
        }

    monkeypatch.setattr(resident_session_module, "renew_resident_session_lease", renew)
    emitted = AsyncMock()
    monkeypatch.setattr(
        "app.strategic_events.emit_strategic_session_state_updated",
        emitted,
    )

    def payload(reason_code, phase, autonomy_state="paused"):
        return resident_session_module.ResidentSessionHeartbeatRequest(
            session_id="semantic-events",
            task_id=task_id,
            activity={
                "phase": phase,
                "last_action": "same diagnostic prose",
                "reason_code": reason_code,
                "consultation_id": consultation_id if reason_code else None,
                "autonomy_state": autonomy_state,
                "autonomy_reason_code": reason_code,
            },
        )

    endpoint = unwrap(resident_session_module.resident_session_heartbeat)
    for heartbeat_payload in (
        payload("consultation_running", "working"),
        payload("consultation_running", "working"),
        payload("consultation_queued", "waiting"),
        payload("consultation_delivering", "working"),
    ):
        await endpoint(
            request=AsyncMock(),
            project_id=project.id,
            payload=heartbeat_payload,
            session=session,
            token={"sub": task_id},
        )

    assert emitted.await_count == 3
    assert [
        call.kwargs["activity"]["reason_code"] for call in emitted.await_args_list
    ] == ["consultation_queued", "consultation_running", None]
    assert [item["activity"]["reason_code"] for item in renewed] == [
        "consultation_queued",
        "consultation_queued",
        "consultation_running",
        None,
    ]
    assert [item["consultation_verified"] for item in renewed] == [
        True,
        True,
        True,
        False,
    ]


@pytest.mark.asyncio
async def test_short_consultation_projects_delivering_then_settles_before_message(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    project, task, incarnation = await _make_resident(db_session)
    inbound = ProjectMessage(
        project_id=project.id,
        sequence=1,
        surface=MessageSurface.CONVERSATION,
        message_type=MessageType.NOTE,
        from_type="user",
        status="open",
        content="Please check the missing derivation.",
        resident_session_id="resident-session",
        context={"audience": "operator-fyi"},
    )
    db_session.add(inbound)
    await db_session.commit()
    identity = {
        "task_id": str(task.id),
        "session_id": "resident-session",
        "worker_generation": 2,
        "incarnation_id": incarnation,
    }
    claim = await client.post(
        f"/api/agent/projects/{project.id}/conversation/turns/claim",
        headers=_headers(task),
        json={
            **identity,
            "client_claim_id": str(uuid.uuid4()),
            "idempotency_key": "status-ordering-claim",
            "message_ids": [str(inbound.id)],
            "owner": "fast",
            "lease_seconds": 45,
        },
    )
    assert claim.status_code == 200, claim.text
    completed_turn = await client.post(
        f"/api/agent/projects/{project.id}/conversation/turns/complete",
        headers=_headers(task),
        json={
            "task_id": str(task.id),
            "worker_generation": 2,
            "incarnation_id": incarnation,
            "claim_id": claim.json()["claim_id"],
            "owner": "fast",
            "content": "I asked Workday and will report back.",
            "idempotency_key": "status-ordering-initial-answer",
            "envelope": claim.json()["envelope"],
            "workday_request": {
                "delivery_key": "status-ordering",
                "question": "Check the missing derivation privately.",
            },
        },
    )
    assert completed_turn.status_code == 200, completed_turn.text
    packet_id = completed_turn.json()["workday_packet"]["packet_id"]
    packet_claim = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/claim-next",
        headers=_headers(task),
        json={**identity, "lease_seconds": 45},
    )
    assert packet_claim.status_code == 200, packet_claim.text
    packet = packet_claim.json()["packet"]

    events: list[tuple[str, str | None]] = []

    async def record_state(_project_id, *, activity, **_kwargs):
        events.append(("state", activity.get("reason_code")))

    async def record_message(_project_id, event_type, payload):
        events.append((event_type, payload.get("message_id")))

    monkeypatch.setattr(
        "app.strategic_events.emit_strategic_session_state_updated",
        record_state,
    )
    monkeypatch.setattr(messages_router_module, "broadcast_to_project", record_message)

    acknowledged = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/{packet_id}/ack",
        headers=_headers(task),
        json={
            **identity,
            "workday_claim_id": packet["workday_claim_id"],
            "outcome": "completed",
            "result_summary": "The missing derivation is conditioning on X.",
        },
    )
    assert acknowledged.status_code == 200, acknowledged.text
    await db_session.refresh(project)
    ack_activity = project.vision[RESIDENT_SESSION_KEY]["activity"]
    assert ack_activity["reason_code"] == "consultation_delivering"
    assert ack_activity["consultation_verified"] is True
    assert events == [("state", "consultation_delivering")]

    continuation_claim = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/continuations/claim-next",
        headers=_headers(task),
        json={**identity, "lease_seconds": 45},
    )
    assert continuation_claim.status_code == 200, continuation_claim.text
    continuation = continuation_claim.json()["continuation"]
    events.clear()
    completed = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/continuations/"
        f"{continuation['result_packet_id']}/complete",
        headers=_headers(task),
        json={
            **identity,
            "continuation_claim_id": continuation["continuation_claim_id"],
            "content": "I checked it: conditioning supplies the missing step.",
            "idempotency_key": "status-ordering-follow-up",
        },
    )
    assert completed.status_code == 200, completed.text
    await db_session.refresh(project)
    settled_activity = project.vision[RESIDENT_SESSION_KEY]["activity"]
    assert settled_activity["reason_code"] is None
    assert settled_activity["consultation_id"] is None
    assert settled_activity["consultation_verified"] is False
    assert events == [
        ("state", None),
        ("message_created", completed.json()["id"]),
    ]
