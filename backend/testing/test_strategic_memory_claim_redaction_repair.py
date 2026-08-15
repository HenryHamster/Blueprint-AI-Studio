from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, update
from sqlmodel import select

from app.conversation_coordinator import (
    ConversationPublicReplyOwner,
    claim_operator_messages,
)
from app.models import (
    MessageHandlingState,
    MessageSurface,
    Project,
    ProjectMember,
    ProjectMessage,
    StrategicMemoryConsolidationJob,
    StrategicMemorySource,
    StrategicMemorySourceDisposition,
    Task,
)
from app.strategic_memory_claim_redaction_repair import (
    ClaimRedactionRepairError,
    REPAIR_AUDIT_REASON,
    REPAIR_DISPOSITION_REASON,
    apply_false_fast_claim_redaction_repair,
    audit_false_fast_claim_redactions,
)
from app.strategic_memory_runtime import dispatch_pending_memory_consolidation
from app.time_utils import _utcnow


async def _make_false_claim_redaction(
    db_session,
    *,
    project: Project | None = None,
    sequence: int = 1,
    context: dict | None = None,
) -> tuple[Project, ProjectMessage, StrategicMemorySource, StrategicMemorySourceDisposition]:
    if project is None:
        project = Project(name="claim-redaction-repair", user_id="test-user-123")
        db_session.add(project)
        await db_session.flush()
    task = Task(
        project_id=project.id,
        title=f"Fast conversation {sequence}",
        payloads=[],
    )
    message = ProjectMessage(
        project_id=project.id,
        sequence=sequence,
        surface=MessageSurface.CONVERSATION,
        handling_state=MessageHandlingState.PENDING,
        from_type="user",
        actor_user_id="test-user-123",
        content=f"operator input {sequence}",
        context=dict(context or {}),
        status="open",
        resident_session_id="repair-session",
    )
    db_session.add_all([task, message])
    await db_session.commit()
    claim = await claim_operator_messages(
        db_session,
        project_id=project.id,
        session_id="repair-session",
        task_id=task.id,
        worker_generation=3,
        incarnation_id=uuid.uuid4(),
        message_ids=[message.id],
        lease_seconds=45,
        public_reply_owner=ConversationPublicReplyOwner.FAST,
        canonical_conversation_only=True,
        client_claim_id=uuid.uuid4(),
        claim_idempotency_key=f"repair-fast-claim-{sequence}",
    )
    assert claim is not None
    source = await db_session.scalar(
        select(StrategicMemorySource).where(
            StrategicMemorySource.project_id == project.id,
            StrategicMemorySource.source_type == "project_message",
            StrategicMemorySource.source_key == str(message.id),
        )
    )
    assert source is not None
    disposition = await db_session.get(StrategicMemorySourceDisposition, source.id)
    assert disposition is not None
    source.payload_state = "redacted"
    source.tombstoned_at = _utcnow()
    disposition.status = "no_memory"
    disposition.reason = "source_redacted"
    disposition.processed_at = source.tombstoned_at
    db_session.add_all([source, disposition])
    await db_session.commit()
    return project, message, source, disposition


def _window() -> tuple[str, str]:
    now = _utcnow().replace(tzinfo=timezone.utc)
    return (
        (now - timedelta(hours=1)).isoformat(),
        (now + timedelta(minutes=1)).isoformat(),
    )


@pytest.mark.asyncio
async def test_audit_accepts_only_exact_fast_claim_signature(db_session):
    project, exact_message, exact_source, _ = await _make_false_claim_redaction(
        db_session,
        sequence=1,
    )
    _, privacy_message, privacy_source, _ = await _make_false_claim_redaction(
        db_session,
        project=project,
        sequence=2,
        context={"audience": None},
    )
    _, hash_message, hash_source, _ = await _make_false_claim_redaction(
        db_session,
        project=project,
        sequence=3,
    )
    _, deleted_message, deleted_source, _ = await _make_false_claim_redaction(
        db_session,
        project=project,
        sequence=4,
    )
    # Bypass the ORM privacy hook to model a native row that no longer proves
    # its immutable receipt. The repair must reject it on content hash.
    await db_session.execute(
        update(ProjectMessage)
        .where(ProjectMessage.id == hash_message.id)
        .values(content="different native bytes")
    )
    await db_session.execute(
        delete(ProjectMessage).where(ProjectMessage.id == deleted_message.id)
    )
    await db_session.commit()

    start, end = _window()
    audit = await audit_false_fast_claim_redactions(
        db_session,
        project_id=project.id,
        window_start=datetime.fromisoformat(start),
        window_end=datetime.fromisoformat(end),
    )

    assert audit.candidate_count == 1
    assert [candidate.source.id for candidate in audit.candidates] == [exact_source.id]
    assert privacy_source.id not in {candidate.source.id for candidate in audit.candidates}
    assert hash_source.id not in {candidate.source.id for candidate in audit.candidates}
    assert deleted_source.id not in {
        candidate.source.id for candidate in audit.candidates
    }
    assert exact_message.context["_conversation_public_reply_owner"] == "fast"
    assert "audience" in privacy_message.context

    outside = await audit_false_fast_claim_redactions(
        db_session,
        project_id=project.id,
        window_start=_utcnow().replace(tzinfo=timezone.utc) - timedelta(days=3),
        window_end=_utcnow().replace(tzinfo=timezone.utc) - timedelta(days=2),
    )
    assert outside.candidate_count == 0


@pytest.mark.asyncio
async def test_apply_service_restores_quarantines_and_queues_exact_receipt(
    db_session,
    monkeypatch,
):
    monkeypatch.setenv("DEPLOY_SHA", "fixed-revision")
    monkeypatch.setenv("STRATEGIC_MEMORY_V2_SHADOW_ENABLED", "true")
    project, _message, source, disposition = await _make_false_claim_redaction(
        db_session
    )
    now = _utcnow().replace(tzinfo=timezone.utc)
    audit = await audit_false_fast_claim_redactions(
        db_session,
        project_id=project.id,
        window_start=now - timedelta(hours=1),
        window_end=now + timedelta(minutes=1),
    )
    result = await apply_false_fast_claim_redaction_repair(
        db_session,
        project_id=project.id,
        actor_id="test-user-123",
        operation_id="repair-service-1",
        buggy_serving_revision="buggy-revision",
        expected_serving_revision="fixed-revision",
        window_start=now - timedelta(hours=1),
        window_end=now + timedelta(minutes=1),
        expected_candidate_count=audit.candidate_count,
        expected_candidate_digest=audit.candidate_digest,
    )
    await db_session.flush()

    assert result.restored_count == 1
    assert result.reconsolidation_pending_count == 1
    assert result.queued_job_id is not None
    assert source.payload_state == "active"
    assert source.tombstoned_at is None
    assert disposition.status == "quarantined"
    assert disposition.reason == REPAIR_DISPOSITION_REASON
    audit_receipt = await db_session.get(StrategicMemorySource, result.audit_source_id)
    assert audit_receipt is not None
    audit_disposition = await db_session.get(
        StrategicMemorySourceDisposition,
        audit_receipt.id,
    )
    assert audit_disposition is not None
    assert audit_disposition.status == "no_memory"
    assert audit_disposition.reason == REPAIR_AUDIT_REASON
    queued_job = await db_session.get(
        StrategicMemoryConsolidationJob,
        result.queued_job_id,
    )
    assert queued_job is not None
    assert queued_job.mode == "reconsolidation"
    assert queued_job.target_source_id == source.id

    dispatched = await dispatch_pending_memory_consolidation(db_session)
    assert dispatched.created_tasks == 1
    task = await db_session.scalar(
        select(Task).where(
            Task.project_id == project.id,
            Task.context["memory_consolidation_job_id"].as_string()
            == str(queued_job.id),
        )
    )
    assert task is not None


@pytest.mark.asyncio
async def test_owner_dry_run_digest_apply_and_idempotent_replay(
    client,
    db_session,
    monkeypatch,
):
    monkeypatch.setenv("DEPLOY_SHA", "fixed-revision")
    monkeypatch.setenv("STRATEGIC_MEMORY_V2_SHADOW_ENABLED", "true")
    project, _message, source, _disposition = await _make_false_claim_redaction(
        db_session
    )
    db_session.add(
        ProjectMember(
            project_id=project.id,
            user_id="test-user-123",
            role="owner",
        )
    )
    await db_session.commit()
    window_start, window_end = _window()
    endpoint = (
        f"/api/admin/projects/{project.id}/memory/repairs/fast-claim-redactions"
    )
    base = {
        "window_start": window_start,
        "window_end": window_end,
        "buggy_serving_revision": "buggy-revision",
        "expected_serving_revision": "fixed-revision",
    }

    dry_run = await client.post(endpoint, json={**base, "mode": "dry_run"})
    assert dry_run.status_code == 200, dry_run.text
    reviewed = dry_run.json()
    assert reviewed["candidate_count"] == 1
    assert reviewed["content_returned"] is False
    await db_session.refresh(source)
    assert source.payload_state == "redacted"

    wrong_digest = await client.post(
        endpoint,
        json={
            **base,
            "mode": "apply",
            "operation_id": "repair-claim-redactions-1",
            "expected_candidate_count": 1,
            "expected_candidate_digest": "0" * 64,
        },
    )
    assert wrong_digest.status_code == 409
    await db_session.refresh(source)
    assert source.payload_state == "redacted"
    await db_session.rollback()

    apply = await client.post(
        endpoint,
        json={
            **base,
            "mode": "apply",
            "operation_id": "repair-claim-redactions-1",
            "expected_candidate_count": reviewed["candidate_count"],
            "expected_candidate_digest": reviewed["candidate_digest"],
        },
    )
    assert apply.status_code == 200, apply.text
    applied = apply.json()
    assert applied["restored_count"] == 1
    assert applied["reconsolidation_pending_count"] == 1
    assert applied["queued_job_id"] is not None
    assert applied["replayed"] is False

    monkeypatch.setenv("STRATEGIC_MEMORY_V2_SHADOW_ENABLED", "false")
    replay = await client.post(
        endpoint,
        json={
            **base,
            "mode": "apply",
            "operation_id": "repair-claim-redactions-1",
            "expected_candidate_count": reviewed["candidate_count"],
            "expected_candidate_digest": reviewed["candidate_digest"],
        },
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["replayed"] is True
    assert replay.json()["restored_count"] == 1
    assert replay.json()["reconsolidation_pending_count"] == 1


@pytest.mark.asyncio
async def test_apply_fails_closed_when_shadow_dispatch_is_disabled(
    db_session,
    monkeypatch,
):
    monkeypatch.setenv("DEPLOY_SHA", "fixed-revision")
    monkeypatch.setenv("STRATEGIC_MEMORY_V2_SHADOW_ENABLED", "false")
    project, _message, source, disposition = await _make_false_claim_redaction(
        db_session
    )
    project_id = project.id
    now = _utcnow().replace(tzinfo=timezone.utc)
    audit = await audit_false_fast_claim_redactions(
        db_session,
        project_id=project_id,
        window_start=now - timedelta(hours=1),
        window_end=now + timedelta(minutes=1),
    )

    with pytest.raises(
        ClaimRedactionRepairError,
        match="shadow processing must be enabled",
    ):
        await apply_false_fast_claim_redaction_repair(
            db_session,
            project_id=project_id,
            actor_id="test-user-123",
            operation_id="repair-shadow-disabled",
            buggy_serving_revision="buggy-revision",
            expected_serving_revision="fixed-revision",
            window_start=now - timedelta(hours=1),
            window_end=now + timedelta(minutes=1),
            expected_candidate_count=audit.candidate_count,
            expected_candidate_digest=audit.candidate_digest,
        )
    await db_session.rollback()

    await db_session.refresh(source)
    await db_session.refresh(disposition)
    assert source.payload_state == "redacted"
    assert source.tombstoned_at is not None
    assert disposition.status == "no_memory"
    assert disposition.reason == "source_redacted"
    assert await db_session.scalar(
        select(StrategicMemorySource).where(
            StrategicMemorySource.project_id == project_id,
            StrategicMemorySource.source_type == "operator_action",
        )
    ) is None
    assert await db_session.scalar(
        select(StrategicMemoryConsolidationJob).where(
            StrategicMemoryConsolidationJob.project_id == project_id,
        )
    ) is None


@pytest.mark.asyncio
async def test_repair_is_owner_scoped_and_revision_fenced(
    client,
    db_session,
    monkeypatch,
):
    monkeypatch.setenv("DEPLOY_SHA", "fixed-revision")
    project, _message, _source, _disposition = await _make_false_claim_redaction(
        db_session
    )
    await db_session.commit()
    window_start, window_end = _window()
    endpoint = (
        f"/api/admin/projects/{project.id}/memory/repairs/fast-claim-redactions"
    )
    request = {
        "mode": "dry_run",
        "window_start": window_start,
        "window_end": window_end,
        "buggy_serving_revision": "buggy-revision",
        "expected_serving_revision": "fixed-revision",
    }
    assert (await client.post(endpoint, json=request)).status_code == 404

    db_session.add(
        ProjectMember(
            project_id=project.id,
            user_id="test-user-123",
            role="owner",
        )
    )
    await db_session.commit()
    request["expected_serving_revision"] = "different-revision"
    mismatch = await client.post(endpoint, json=request)
    assert mismatch.status_code == 409
