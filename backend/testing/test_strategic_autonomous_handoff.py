"""Receipt-bound autonomous Workday finding handoff to typed Fast."""

import uuid
from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.auth import create_agent_token
from app.models import (
    MessageHandlingState,
    MessageSurface,
    Project,
    ProjectMessage,
    StrategicContextEnvelopeSnapshot,
    Task,
    TaskStatus,
)
from app.time_utils import _utcnow


TEST_USER_ID = "test-user-123"
SESSION_ID = "autonomous-handoff-session"
GENERATION = 2
VM_ID = "vm-autonomous-handoff"


async def _make_worker(db_session):
    project = Project(name="Autonomous Workday handoff", user_id=TEST_USER_ID)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id=VM_ID,
        payloads=[
            {
                "type": "strategic_review",
                "prompt": "continuous typed Fast",
                "typed_fast_enabled": True,
            }
        ],
        context={
            "_resident_session": True,
            "strategic_session_id": SESSION_ID,
            "_resident_worker_generation": GENERATION,
        },
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    incarnation = uuid.uuid4()
    now = _utcnow()
    project.vision = {
        "_resident_strategic_session": {
            "session_id": SESSION_ID,
            "task_id": str(task.id),
            "worker_generation": GENERATION,
            "worker_incarnation_id": str(incarnation),
            "worker_incarnation_expires_at": (
                now + timedelta(minutes=10)
            ).isoformat(),
            "operator_user_id": TEST_USER_ID,
            "status": "active",
            "activity": {"phase": "active", "autonomy_state": "active"},
        },
        "_strategic_local_continuation": {
            "version": 1,
            "active": True,
            "operator_user_id": TEST_USER_ID,
            "updated_at": now.isoformat(),
            "last_seen_at": now.isoformat(),
        },
    }
    flag_modified(project, "vision")
    db_session.add(project)
    await db_session.commit()
    return project, task, incarnation


def _headers(task: Task) -> dict[str, str]:
    return {
        "x-agent-token": create_agent_token(
            str(task.id), VM_ID, task_type="strategic_review"
        )
    }


def _identity(task: Task, incarnation: uuid.UUID) -> dict[str, object]:
    return {
        "task_id": str(task.id),
        "resident_incarnation_id": str(incarnation),
    }


async def _begin(client, task, incarnation, *, client_turn_id=None):
    client_turn_id = client_turn_id or uuid.uuid4()
    response = await client.post(
        "/api/agent/strategic",
        headers=_headers(task),
        json={
            **_identity(task, incarnation),
            "op": "begin_autonomous_turn",
            "data": {
                "session_id": SESSION_ID,
                "worker_generation": GENERATION,
                "client_turn_id": str(client_turn_id),
                "ttl_seconds": 300,
            },
        },
    )
    return response


def _report_payload(task, incarnation, lease, *, summary="A durable blocker exists."):
    return {
        **_identity(task, incarnation),
        "autonomous_turn_id": lease["autonomous_turn_id"],
        "autonomous_turn_lease_id": lease["autonomous_turn_lease_id"],
        "autonomous_handoff_idempotency_key": lease[
            "autonomous_handoff_idempotency_key"
        ],
        "op": "report_autonomous_finding",
        "data": {
            "session_id": SESSION_ID,
            "worker_generation": GENERATION,
            "kind": "blocker",
            "summary": summary,
            "evidence_refs": ["task:durable-blocker"],
        },
    }


async def _end(client, task, incarnation, lease):
    return await client.post(
        "/api/agent/strategic",
        headers=_headers(task),
        json={
            **_identity(task, incarnation),
            "autonomous_turn_id": lease["autonomous_turn_id"],
            "autonomous_turn_lease_id": lease["autonomous_turn_lease_id"],
            "autonomous_handoff_idempotency_key": lease[
                "autonomous_handoff_idempotency_key"
            ],
            "op": "end_autonomous_turn",
            "data": {
                "session_id": SESSION_ID,
                "worker_generation": GENERATION,
            },
        },
    )


async def _claim_continuation(client, project, task, incarnation):
    return await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/"
        "continuations/claim-next",
        headers=_headers(task),
        json={
            "task_id": str(task.id),
            "session_id": SESSION_ID,
            "worker_generation": GENERATION,
            "incarnation_id": str(incarnation),
            "lease_seconds": 45,
        },
    )


@pytest.mark.asyncio
async def test_autonomous_handoff_requires_exact_server_turn_and_replays_once(
    client: AsyncClient, db_session
):
    project, task, incarnation = await _make_worker(db_session)
    no_lease = await client.post(
        "/api/agent/strategic",
        headers=_headers(task),
        json={
            **_identity(task, incarnation),
            "op": "report_autonomous_finding",
            "data": {
                "session_id": SESSION_ID,
                "worker_generation": GENERATION,
                "kind": "finding",
                "summary": "No server turn owns this report.",
            },
        },
    )
    assert no_lease.status_code == 409

    client_turn_id = uuid.uuid4()
    begun = await _begin(
        client, task, incarnation, client_turn_id=client_turn_id
    )
    assert begun.status_code == 200, begun.text
    lease = begun.json()
    replayed_begin = await _begin(
        client, task, incarnation, client_turn_id=client_turn_id
    )
    assert replayed_begin.status_code == 200, replayed_begin.text
    assert replayed_begin.json()["status"] == "duplicate"
    assert replayed_begin.json()["autonomous_turn_id"] == lease["autonomous_turn_id"]

    mismatched = _report_payload(task, incarnation, lease)
    mismatched["autonomous_turn_lease_id"] = str(uuid.uuid4())
    rejected = await client.post(
        "/api/agent/strategic", headers=_headers(task), json=mismatched
    )
    assert rejected.status_code == 409

    report_payload = _report_payload(task, incarnation, lease)
    created = await client.post(
        "/api/agent/strategic", headers=_headers(task), json=report_payload
    )
    assert created.status_code == 200, created.text
    assert created.json()["status"] == "created"
    duplicate = await client.post(
        "/api/agent/strategic", headers=_headers(task), json=report_payload
    )
    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.json() == {
        "status": "duplicate",
        "result_packet_id": created.json()["result_packet_id"],
        "delivery_owner": "fast",
    }
    conflict = await client.post(
        "/api/agent/strategic",
        headers=_headers(task),
        json=_report_payload(
            task, incarnation, lease, summary="Changed lost-response replay."
        ),
    )
    assert conflict.status_code == 409

    result = await db_session.get(
        ProjectMessage, uuid.UUID(created.json()["result_packet_id"])
    )
    assert result is not None
    assert result.from_type == "system"
    assert result.surface == MessageSurface.CONVERSATION
    assert result.handling_state == MessageHandlingState.PENDING
    assert result.context["_autonomous_workday_handoff"] is True
    assert result.context["public_reply_owner"] == "fast"
    assert result.context["autonomous_turn_id"] == lease["autonomous_turn_id"]
    assert result.context["autonomous_worker_incarnation"] == str(incarnation)
    assert result.context["claim_id"] == lease["autonomous_turn_id"]
    snapshot = await db_session.get(
        StrategicContextEnvelopeSnapshot,
        uuid.UUID(result.context["envelope"]["snapshot_id"]),
    )
    assert snapshot is not None
    assert str(snapshot.claim_id) == lease["autonomous_turn_id"]
    assert snapshot.session_id == SESSION_ID
    assert snapshot.worker_generation == GENERATION
    assert snapshot.worker_incarnation == incarnation
    public_rows = (
        await db_session.execute(
            select(ProjectMessage).where(
                ProjectMessage.project_id == project.id,
                ProjectMessage.from_type == "strategic",
                ProjectMessage.surface == MessageSurface.CONVERSATION,
            )
        )
    ).scalars().all()
    assert public_rows == []


@pytest.mark.asyncio
async def test_autonomous_handoff_fast_continuation_is_sole_public_delivery(
    client: AsyncClient, db_session
):
    project, task, incarnation = await _make_worker(db_session)
    begun = await _begin(client, task, incarnation)
    assert begun.status_code == 200, begun.text
    report = await client.post(
        "/api/agent/strategic",
        headers=_headers(task),
        json=_report_payload(task, incarnation, begun.json()),
    )
    assert report.status_code == 200, report.text
    ended = await _end(client, task, incarnation, begun.json())
    assert ended.status_code == 200, ended.text

    direct_public = await client.post(
        "/api/agent/strategic",
        headers=_headers(task),
        json={
            **_identity(task, incarnation),
            "op": "create_message",
            "data": {
                "type": "NOTE",
                "content": "Workday must not publish this directly.",
                "context": {
                    "audience": "operator-fyi",
                    "event_type": "question_answer",
                },
            },
        },
    )
    assert direct_public.status_code == 409

    claimed = await _claim_continuation(client, project, task, incarnation)
    assert claimed.status_code == 200, claimed.text
    continuation = claimed.json()["continuation"]
    assert continuation["result_packet_id"] == report.json()["result_packet_id"]
    assert continuation["consultation_id"] is None
    assert "A durable blocker exists." in continuation["result_summary"]
    complete_payload = {
        "task_id": str(task.id),
        "session_id": SESSION_ID,
        "worker_generation": GENERATION,
        "incarnation_id": str(incarnation),
        "continuation_claim_id": continuation["continuation_claim_id"],
        "content": "I found a durable blocker and here is what to do next.",
        "idempotency_key": "autonomous-fast-public-delivery",
    }
    completed = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/"
        f"continuations/{continuation['result_packet_id']}/complete",
        headers=_headers(task),
        json=complete_payload,
    )
    assert completed.status_code == 200, completed.text
    duplicate = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/"
        f"continuations/{continuation['result_packet_id']}/complete",
        headers=_headers(task),
        json=complete_payload,
    )
    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.json()["status"] == "duplicate"
    public_rows = (
        await db_session.execute(
            select(ProjectMessage).where(
                ProjectMessage.project_id == project.id,
                ProjectMessage.from_type == "strategic",
                ProjectMessage.surface == MessageSurface.CONVERSATION,
            )
        )
    ).scalars().all()
    assert len(public_rows) == 1
    assert public_rows[0].content == complete_payload["content"]
    assert public_rows[0].context["_conversation_public_reply_owner"] == "fast"
    assert public_rows[0].context["_workday_result_packet_id"] == continuation[
        "result_packet_id"
    ]


@pytest.mark.asyncio
async def test_autonomous_handoff_blocks_unresolved_and_enforces_terminal_cooldown(
    client: AsyncClient, db_session
):
    project, task, incarnation = await _make_worker(db_session)
    first = await _begin(client, task, incarnation)
    assert first.status_code == 200, first.text
    first_report = await client.post(
        "/api/agent/strategic",
        headers=_headers(task),
        json=_report_payload(task, incarnation, first.json()),
    )
    assert first_report.status_code == 200, first_report.text
    assert (await _end(client, task, incarnation, first.json())).status_code == 200

    second = await _begin(client, task, incarnation)
    assert second.status_code == 200, second.text
    blocked_pending = await client.post(
        "/api/agent/strategic",
        headers=_headers(task),
        json=_report_payload(task, incarnation, second.json()),
    )
    assert blocked_pending.status_code == 409
    assert (await _end(client, task, incarnation, second.json())).status_code == 200

    claimed = await _claim_continuation(client, project, task, incarnation)
    assert claimed.status_code == 200, claimed.text
    continuation = claimed.json()["continuation"]
    completed = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/"
        f"continuations/{continuation['result_packet_id']}/complete",
        headers=_headers(task),
        json={
            "task_id": str(task.id),
            "session_id": SESSION_ID,
            "worker_generation": GENERATION,
            "incarnation_id": str(incarnation),
            "continuation_claim_id": continuation["continuation_claim_id"],
            "content": "Fast delivered the first finding.",
            "idempotency_key": "autonomous-cooldown-delivery",
        },
    )
    assert completed.status_code == 200, completed.text

    third = await _begin(client, task, incarnation)
    assert third.status_code == 200, third.text
    blocked_cooldown = await client.post(
        "/api/agent/strategic",
        headers=_headers(task),
        json=_report_payload(task, incarnation, third.json()),
    )
    assert blocked_cooldown.status_code == 409
    result = await db_session.get(
        ProjectMessage, uuid.UUID(first_report.json()["result_packet_id"])
    )
    assert result is not None
    result.updated_at = _utcnow() - timedelta(minutes=6)
    db_session.add(result)
    await db_session.commit()
    admitted_after_cooldown = await client.post(
        "/api/agent/strategic",
        headers=_headers(task),
        json=_report_payload(task, incarnation, third.json()),
    )
    assert admitted_after_cooldown.status_code == 200, admitted_after_cooldown.text
    assert admitted_after_cooldown.json()["status"] == "created"
