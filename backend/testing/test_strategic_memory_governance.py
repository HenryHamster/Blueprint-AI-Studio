"""Focused safety tests for the Strategic Memory V2 governance boundary."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlmodel import select

from app.models import (
    Project,
    ProjectMessage,
    ProjectMessageAttachmentUpload,
    StrategicMemoryItem,
    StrategicMemoryProjectState,
    StrategicMemorySource,
)
from app.strategic_memory_control import claim_consolidation_job, enqueue_contiguous_job
from app.strategic_memory_governance import (
    MemoryGovernanceError,
    apply_governed_memory_action,
    delete_native_project_message_governed,
    get_project_memory_detail,
    list_project_memory,
    memory_v2_rollout,
)
from app.strategic_memory_service import (
    ItemMutation,
    LedgerProposal,
    SourceDispositionMutation,
    SourceSpanProposal,
    validate_and_admit_proposal,
)
from app.strategic_memory_sources import SourceReceiptInput, record_source, sha256_hex
from app.time_utils import _utcnow


async def _admit_initial_item(db_session, project: Project):
    payload = b"eta is 0.2"
    receipt = await record_source(
        db_session,
        SourceReceiptInput(
            project_id=project.id,
            source_type="task_result",
            source_key="governance-test-initial",
            source_revision="1",
            content_hash=sha256_hex(payload),
            occurred_at=_utcnow(),
            actor_kind="user",
            actor_id="operator",
            canonical_payload=payload,
            mutable=True,
        ),
    )
    job = await enqueue_contiguous_job(db_session, project.id)
    assert job is not None
    job = await claim_consolidation_job(db_session, job_id=job.id, lease_owner="governance-test")
    proposal = LedgerProposal(
        schema_version=1,
        job_id=job.id,
        base_ledger_revision=0,
        idempotency_key="governance-test-initial",
        dispositions=[
            SourceDispositionMutation(
                source_id=receipt.source.id,
                source_hash=receipt.source.content_hash,
                status="incorporated",
            )
        ],
        items=[
            ItemMutation(
                kind="notation",
                logical_key="eta",
                canonical_text="eta is 0.2",
                epistemic_status="operator_explicit",
                confidence=1,
                extractor_version="governance-test",
                sources=[
                    SourceSpanProposal(
                        source_id=receipt.source.id,
                        source_role="assertion",
                        span_index=0,
                        span_kind="text",
                        span_start=0,
                        span_end=len(payload),
                        span_hash=sha256_hex(payload),
                    )
                ],
            )
        ],
    )
    await validate_and_admit_proposal(
        db_session, proposal, lease_owner="governance-test", lease_generation=job.lease_generation
    )
    return await db_session.scalar(
        select(StrategicMemoryItem).where(StrategicMemoryItem.project_id == project.id)
    )


@pytest.mark.asyncio
async def test_governed_retract_uses_the_fenced_ledger_and_replays(db_session):
    project = Project(name="governed-memory", user_id="governed-memory-user")
    db_session.add(project)
    await db_session.flush()
    item = await _admit_initial_item(db_session, project)
    assert item is not None

    result = await apply_governed_memory_action(
        db_session,
        project_id=project.id,
        actor_id="governed-memory-user",
        operation_id="operator-retract-1",
        action="retract",
        item_id=item.id,
        expected_version=1,
        expected_ledger_revision=1,
        reason="The source was incorrect.",
    )
    state = await db_session.get(StrategicMemoryProjectState, project.id)
    assert not result.replayed
    assert result.ledger_revision == 2
    assert state is not None and state.ledger_revision == 2

    replay = await apply_governed_memory_action(
        db_session,
        project_id=project.id,
        actor_id="governed-memory-user",
        operation_id="operator-retract-1",
        action="retract",
        item_id=item.id,
        expected_version=1,
        expected_ledger_revision=1,
        reason="The source was incorrect.",
    )
    assert replay.replayed
    assert replay.ledger_revision == 2


@pytest.mark.asyncio
async def test_governed_action_rejects_a_stale_ledger_revision(db_session):
    project = Project(name="governed-stale", user_id="governed-stale-user")
    db_session.add(project)
    await db_session.flush()
    item = await _admit_initial_item(db_session, project)
    assert item is not None
    with pytest.raises(MemoryGovernanceError, match="ledger revision changed"):
        await apply_governed_memory_action(
            db_session,
            project_id=project.id,
            actor_id="governed-stale-user",
            operation_id=str(uuid.uuid4()),
            action="forget",
            item_id=item.id,
            expected_version=1,
            expected_ledger_revision=0,
            reason="Test stale fence.",
        )


@pytest.mark.asyncio
async def test_deleted_only_head_is_hidden_and_detail_does_not_return_canonical_bytes(db_session):
    project = Project(name="privacy-safe-memory", user_id="privacy-safe-user")
    db_session.add(project)
    await db_session.flush()
    item = await _admit_initial_item(db_session, project)
    assert item is not None
    source = await db_session.scalar(
        select(StrategicMemorySource).where(StrategicMemorySource.project_id == project.id)
    )
    assert source is not None
    source.payload_state = "deleted"
    source.tombstoned_at = _utcnow()
    await db_session.flush()

    assert await list_project_memory(db_session, project_id=project.id) == []
    with pytest.raises(Exception, match="Memory item not found"):
        await get_project_memory_detail(db_session, project_id=project.id, item_id=item.id)


@pytest.mark.asyncio
async def test_native_delete_fails_closed_when_attachment_storage_is_not_erased(db_session):
    project = Project(name="attachment-privacy", user_id="attachment-privacy-user")
    db_session.add(project)
    await db_session.flush()
    message = ProjectMessage(project_id=project.id, sequence=1, content="message with attachment")
    db_session.add(message)
    await db_session.flush()
    db_session.add(
        ProjectMessageAttachmentUpload(
            project_id=project.id,
            actor_user_id="attachment-privacy-user",
            storage_uri="gs://unerasable/test",
            filename="private.pdf",
            expected_size_bytes=1,
            expires_at=_utcnow() + timedelta(hours=1),
            message_id=message.id,
        )
    )
    await db_session.flush()
    with pytest.raises(MemoryGovernanceError, match="attachments"):
        await delete_native_project_message_governed(
            db_session, project_id=project.id, message_id=message.id
        )
    assert (await db_session.get(ProjectMessage, message.id)).content == "message with attachment"


def test_memory_v2_retrieval_defaults_on_while_mutating_surfaces_stay_closed(monkeypatch):
    """Read is on by default; every mutating surface still requires opt-in.

    Retrieval shipped dark for its entire life -- ``STRATEGIC_MEMORY_V2_READ_ENABLED``
    is read by ``memory_v2_rollout`` and set in no config file anywhere in the
    tree -- so knowledge documents accumulated that nothing ever read. The
    default is now open. Both escape hatches must keep working, or "default on"
    would be irreversible during an incident.
    """
    for name in (
        "STRATEGIC_MEMORY_V2_READ_ENABLED",
        "STRATEGIC_MEMORY_V2_SHADOW_ENABLED",
        "STRATEGIC_MEMORY_V2_ACTIONS_ENABLED",
        "STRATEGIC_MEMORY_V2_BACKFILL_ENABLED",
        "STRATEGIC_MEMORY_V2_PROJECT_IDS",
    ):
        monkeypatch.delenv(name, raising=False)
    assert memory_v2_rollout().read_enabled
    # Shadow is a comparison harness; actions and backfill mutate durable
    # semantic history. All three stay fail-closed.
    assert not memory_v2_rollout().shadow_enabled
    assert not memory_v2_rollout().actions_enabled
    assert not memory_v2_rollout().backfill_enabled

    # Escape hatch 1: the explicit kill switch must survive the default flip.
    monkeypatch.setenv("STRATEGIC_MEMORY_V2_READ_ENABLED", "0")
    assert not memory_v2_rollout().read_enabled
    monkeypatch.delenv("STRATEGIC_MEMORY_V2_READ_ENABLED", raising=False)

    # Escape hatch 2: an allow-list, when supplied, still fences every surface.
    project_id = uuid.uuid4()
    monkeypatch.setenv("STRATEGIC_MEMORY_V2_BACKFILL_ENABLED", "true")
    assert not memory_v2_rollout(project_id).backfill_enabled
    monkeypatch.setenv("STRATEGIC_MEMORY_V2_PROJECT_IDS", str(project_id))
    assert memory_v2_rollout(project_id).read_enabled
    assert memory_v2_rollout(project_id).backfill_enabled
    assert not memory_v2_rollout(uuid.uuid4()).read_enabled
    assert not memory_v2_rollout(uuid.uuid4()).backfill_enabled
