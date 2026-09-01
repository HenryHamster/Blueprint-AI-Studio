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


async def _claim_continuation(
    client,
    project,
    task,
    incarnation,
    *,
    session_id=SESSION_ID,
    generation=GENERATION,
):
    return await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/"
        "continuations/claim-next",
        headers=_headers(task),
        json={
            "task_id": str(task.id),
            "session_id": session_id,
            "worker_generation": generation,
            "incarnation_id": str(incarnation),
            "lease_seconds": 45,
        },
    )


@pytest.mark.asyncio
async def test_autonomous_handoff_begin_fails_closed_when_project_autonomy_is_disabled(
    client: AsyncClient, db_session
):
    project, task, incarnation = await _make_worker(db_session)
    before_vision = project.vision
    assert before_vision["_resident_strategic_session"]["activity"][
        "autonomy_state"
    ] == "active"

    # Model the production race: the last Resident heartbeat left stale-active
    # activity in vision, then the operator disabled the canonical project
    # switch before the worker attempted to acquire its next private-turn lease.
    project.autonomous_enabled = False
    db_session.add(project)
    await db_session.commit()

    rejected = await _begin(client, task, incarnation)

    assert rejected.status_code == 409
    assert rejected.json()["error"] == {
        "code": 409,
        "message": "Project autonomy is disabled",
    }
    await db_session.refresh(project)
    assert project.autonomous_enabled is False
    assert project.vision == before_vision
    assert (
        "autonomous_private_turn"
        not in project.vision["_resident_strategic_session"]
    )
    messages = (
        await db_session.execute(
            select(ProjectMessage).where(ProjectMessage.project_id == project.id)
        )
    ).scalars().all()
    assert messages == []


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
    live_authority = continuation["current_live_work_authority"]
    assert live_authority["consultation_mutation_receipts"] == []
    assert live_authority["collection_metadata"][
        "consultation_mutation_receipts"
    ] == {"returned_count": 0, "has_more": False}
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
@pytest.mark.parametrize(
    "tamper",
    [
        "forged_public_source",
        "producer_incarnation",
        "resident_session",
        "malformed_server_key",
        "missing_source_task",
    ],
)
async def test_autonomous_handoff_provenance_tamper_is_quarantined(
    client: AsyncClient,
    db_session,
    tamper: str,
):
    project, task, incarnation = await _make_worker(db_session)
    client_turn_id = uuid.uuid4()
    begun = await _begin(
        client,
        task,
        incarnation,
        client_turn_id=client_turn_id,
    )
    assert begun.status_code == 200, begun.text
    report = await client.post(
        "/api/agent/strategic",
        headers=_headers(task),
        json=_report_payload(task, incarnation, begun.json()),
    )
    assert report.status_code == 200, report.text
    assert (await _end(client, task, incarnation, begun.json())).status_code == 200

    result_id = uuid.UUID(report.json()["result_packet_id"])
    result = await db_session.get(ProjectMessage, result_id)
    assert result is not None
    original_key = f"autonomous-workday-handoff:{task.id}:{client_turn_id}"
    assert result.idempotency_key == original_key
    expected_key = original_key
    result_context = dict(result.context or {})
    if tamper == "forged_public_source":
        forged_source_id = uuid.uuid4()
        result_context["source_packet_id"] = str(forged_source_id)
        result_context["_workday_consultation_atomic_reply_id"] = str(
            forged_source_id
        )
    elif tamper == "producer_incarnation":
        result_context["autonomous_worker_incarnation"] = str(uuid.uuid4())
    elif tamper == "resident_session":
        result.resident_session_id = f"forged-session-{uuid.uuid4()}"
    elif tamper == "malformed_server_key":
        expected_key = (
            f"autonomous-workday-handoff:not-a-uuid:{client_turn_id}"
        )
        result.idempotency_key = expected_key
    else:
        expected_key = (
            f"autonomous-workday-handoff:{uuid.uuid4()}:{client_turn_id}"
        )
        result.idempotency_key = expected_key
    result.context = result_context
    db_session.add(result)
    await db_session.commit()

    claimed = await _claim_continuation(client, project, task, incarnation)
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["status"] == "empty"

    quarantined = (
        await db_session.execute(
            select(ProjectMessage)
            .where(ProjectMessage.id == result_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert quarantined.idempotency_key == expected_key
    assert quarantined.status == "resolved"
    assert quarantined.handling_state == MessageHandlingState.SUPERSEDED
    assert quarantined.handled_by_task_id is None
    assert (
        quarantined.resolution
        == "private_handoff_continuation_provenance_invalid"
    )
    quarantined_context = dict(quarantined.context or {})
    assert quarantined_context["_private_handoff_retirement"]["reason"] == (
        "continuation_provenance_invalid"
    )
    assert not any(
        key.startswith("_workday_continuation_claim_")
        for key in quarantined_context
    )

    rows = (
        await db_session.execute(
            select(ProjectMessage).where(ProjectMessage.project_id == project.id)
        )
    ).scalars().all()
    assert not any(
        row.from_type == "strategic"
        and isinstance(row.context, dict)
        and row.context.get("message_role") == "workday_consultation_continuation"
        for row in rows
    )
    assert not any(
        str(row.idempotency_key or "").startswith(
            "workday-consultation-terminal-recovery:"
        )
        or str(row.idempotency_key or "").startswith(
            "workday-consultation-terminal-recovery-replacement:"
        )
        for row in rows
    )
    replay = await _claim_continuation(client, project, task, incarnation)
    assert replay.status_code == 200, replay.text
    assert replay.json()["status"] == "empty"


@pytest.mark.asyncio
async def test_autonomous_handoff_rebound_retry_rejects_producer_tamper(
    client: AsyncClient,
    db_session,
):
    project, source_task, source_incarnation = await _make_worker(db_session)
    client_turn_id = uuid.uuid4()
    begun = await _begin(
        client,
        source_task,
        source_incarnation,
        client_turn_id=client_turn_id,
    )
    assert begun.status_code == 200, begun.text
    report = await client.post(
        "/api/agent/strategic",
        headers=_headers(source_task),
        json=_report_payload(source_task, source_incarnation, begun.json()),
    )
    assert report.status_code == 200, report.text
    assert (
        await _end(client, source_task, source_incarnation, begun.json())
    ).status_code == 200
    result_id = uuid.UUID(report.json()["result_packet_id"])
    original_key = (
        f"autonomous-workday-handoff:{source_task.id}:{client_turn_id}"
    )
    source_result = await db_session.get(ProjectMessage, result_id)
    assert source_result is not None
    original_snapshot_id = uuid.UUID(
        source_result.context["envelope"]["snapshot_id"]
    )

    successor_session_id = "autonomous-handoff-successor-session"
    successor_generation = GENERATION + 1
    successor_task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id=VM_ID,
        payloads=[
            {
                "type": "strategic_review",
                "prompt": "successor typed Fast",
                "typed_fast_enabled": True,
            }
        ],
        context={
            "_resident_session": True,
            "strategic_session_id": successor_session_id,
            "_resident_worker_generation": successor_generation,
        },
    )
    db_session.add(successor_task)
    await db_session.commit()
    await db_session.refresh(successor_task)
    successor_incarnation = uuid.uuid4()
    project = (
        await db_session.execute(
            select(Project)
            .where(Project.id == project.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    vision = dict(project.vision or {})
    resident = dict(vision.get("_resident_strategic_session") or {})
    resident.update(
        session_id=successor_session_id,
        task_id=str(successor_task.id),
        worker_generation=successor_generation,
        worker_incarnation_id=str(successor_incarnation),
        worker_incarnation_expires_at=(
            _utcnow() + timedelta(minutes=10)
        ).isoformat(),
        status="active",
    )
    vision["_resident_strategic_session"] = resident
    project.vision = vision
    flag_modified(project, "vision")
    db_session.add(project)
    await db_session.commit()

    first_claim = await _claim_continuation(
        client,
        project,
        successor_task,
        successor_incarnation,
        session_id=successor_session_id,
        generation=successor_generation,
    )
    assert first_claim.status_code == 200, first_claim.text
    first_body = first_claim.json()
    assert first_body["status"] == "claimed"
    continuation = first_body["continuation"]
    assert continuation["result_packet_id"] == str(result_id)

    rebound_result = (
        await db_session.execute(
            select(ProjectMessage)
            .where(ProjectMessage.id == result_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    rebound_context = dict(rebound_result.context or {})
    assert rebound_result.resident_session_id == successor_session_id
    assert rebound_result.handling_state == MessageHandlingState.CLAIMED
    assert rebound_result.handled_by_task_id == successor_task.id
    assert "_resident_handoff_from_session_id" not in rebound_context
    assert rebound_context["_workday_continuation_claim_id"] == continuation[
        "continuation_claim_id"
    ]
    rebound_snapshot_id = uuid.UUID(rebound_context["envelope"]["snapshot_id"])
    assert rebound_snapshot_id != original_snapshot_id
    rebound_snapshot = await db_session.get(
        StrategicContextEnvelopeSnapshot,
        rebound_snapshot_id,
    )
    assert rebound_snapshot is not None
    assert rebound_snapshot.session_id == successor_session_id
    assert rebound_snapshot.worker_generation == successor_generation
    assert rebound_snapshot.worker_incarnation == successor_incarnation
    rebound_trace = dict(rebound_snapshot.retrieval_trace or {})
    assert rebound_trace["workday_worker_rebind"] is True
    assert rebound_trace["source_snapshot_id"] == str(original_snapshot_id)
    assert rebound_trace["source_session_id"] == SESSION_ID
    assert rebound_trace["target_session_id"] == successor_session_id
    assert rebound_trace["workday_rebind_depth"] == 1
    assert rebound_trace["workday_rebind_root_snapshot_id"] == str(
        original_snapshot_id
    )
    assert rebound_trace["workday_rebind_root_session_id"] == SESSION_ID
    assert rebound_trace["workday_rebind_root_worker_generation"] == GENERATION
    assert rebound_trace["workday_rebind_root_worker_incarnation"] == str(
        source_incarnation
    )

    original_snapshot = await db_session.get(
        StrategicContextEnvelopeSnapshot,
        original_snapshot_id,
    )
    assert original_snapshot is not None
    await db_session.delete(original_snapshot)
    await db_session.commit()
    assert (
        await db_session.get(
            StrategicContextEnvelopeSnapshot,
            original_snapshot_id,
        )
        is None
    )

    clean_retry = await _claim_continuation(
        client,
        project,
        successor_task,
        successor_incarnation,
        session_id=successor_session_id,
        generation=successor_generation,
    )
    assert clean_retry.status_code == 200, clean_retry.text
    assert clean_retry.json()["status"] == "claimed"
    clean_continuation = clean_retry.json()["continuation"]
    assert clean_continuation["result_packet_id"] == str(result_id)
    assert clean_continuation["continuation_claim_id"] == continuation[
        "continuation_claim_id"
    ]

    rebound_result = (
        await db_session.execute(
            select(ProjectMessage)
            .where(ProjectMessage.id == result_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    rebound_context = dict(rebound_result.context or {})
    rebound_context["autonomous_worker_incarnation"] = str(uuid.uuid4())
    rebound_result.context = rebound_context
    db_session.add(rebound_result)
    await db_session.commit()

    retried = await _claim_continuation(
        client,
        project,
        successor_task,
        successor_incarnation,
        session_id=successor_session_id,
        generation=successor_generation,
    )
    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == "empty"

    quarantined = (
        await db_session.execute(
            select(ProjectMessage)
            .where(ProjectMessage.id == result_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert quarantined.idempotency_key == original_key
    assert quarantined.status == "resolved"
    assert quarantined.handling_state == MessageHandlingState.SUPERSEDED
    assert quarantined.handled_by_task_id is None
    assert quarantined.resolution == "private_handoff_continuation_provenance_invalid"
    quarantined_context = dict(quarantined.context or {})
    assert quarantined_context["_private_handoff_retirement"]["reason"] == (
        "continuation_provenance_invalid"
    )
    assert not any(
        key.startswith("_workday_continuation_claim_")
        for key in quarantined_context
    )
    rows = (
        await db_session.execute(
            select(ProjectMessage).where(ProjectMessage.project_id == project.id)
        )
    ).scalars().all()
    assert not any(
        row.from_type == "strategic"
        and isinstance(row.context, dict)
        and row.context.get("message_role") == "workday_consultation_continuation"
        for row in rows
    )
    assert not any(
        str(row.idempotency_key or "").startswith(
            "workday-consultation-terminal-recovery:"
        )
        or str(row.idempotency_key or "").startswith(
            "workday-consultation-terminal-recovery-replacement:"
        )
        for row in rows
    )


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
