"""Transactional ORM receipt coverage for the 0713 source/control schema."""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import select

import app.models as models_module
from app.models import (
    MessageSurface,
    Project,
    ProjectMessage,
    StrategicMemorySource,
    StrategicMemorySourceDisposition,
)


@pytest.mark.asyncio
async def test_canonical_project_message_flush_writes_pending_source_receipt(db_session):
    project = Project(name="source receipt", user_id="source-test-user")
    db_session.add(project)
    await db_session.flush()
    message = ProjectMessage(
        project_id=project.id,
        sequence=1,
        surface=MessageSurface.CONVERSATION,
        from_type="user",
        actor_user_id="source-test-user",
        resident_session_id="strategic-session",
        content="η = 0.2 exactly",
    )
    db_session.add(message)
    await db_session.flush()

    source = (
        await db_session.scalars(
            select(StrategicMemorySource).where(
                StrategicMemorySource.project_id == project.id
            )
        )
    ).one()
    disposition = await db_session.get(StrategicMemorySourceDisposition, source.id)
    assert source.source_sequence == 1
    assert source.native_sequence == 1
    assert source.source_type == "project_message"
    assert source.source_key == str(message.id)
    assert source.source_revision == "1"
    assert source.content_hash == hashlib.sha256(message.content.encode("utf-8")).hexdigest()
    assert source.canonical_payload is None
    assert source.source_metadata == {"surface": "conversation"}
    assert disposition is not None
    assert disposition.status == "pending"


@pytest.mark.asyncio
async def test_problem_model_receipts_use_monotonic_authority_generation_not_payload_hash(db_session):
    project = Project(name="problem source", user_id="source-test-user")
    db_session.add(project)
    await db_session.flush()

    project.problem_model = {"notation": {"eta": 0.2}}
    await db_session.flush()
    first = (
        await db_session.scalars(
            select(StrategicMemorySource)
            .where(
                StrategicMemorySource.project_id == project.id,
                StrategicMemorySource.source_type == "problem_model_revision",
            )
            .order_by(StrategicMemorySource.source_sequence)
        )
    ).all()
    assert [row.source_revision for row in first] == ["1"]
    first_hash = first[0].content_hash

    # Equal assignment must not fabricate a temporal revision.
    project.problem_model = {"notation": {"eta": 0.2}}
    await db_session.flush()
    assert (
        await db_session.scalars(
            select(StrategicMemorySource).where(
                StrategicMemorySource.project_id == project.id,
                StrategicMemorySource.source_type == "problem_model_revision",
            )
        )
    ).all() == first

    project.problem_model = {"notation": {"eta": 0.3}}
    await db_session.flush()
    project.problem_model = {"notation": {"eta": 0.2}}
    await db_session.flush()
    receipts = (
        await db_session.scalars(
            select(StrategicMemorySource)
            .where(
                StrategicMemorySource.project_id == project.id,
                StrategicMemorySource.source_type == "problem_model_revision",
            )
            .order_by(StrategicMemorySource.source_sequence)
        )
    ).all()
    assert [row.source_revision for row in receipts] == ["1", "2", "3"]
    assert receipts[0].content_hash == receipts[2].content_hash == first_hash
    assert receipts[0].canonical_payload == receipts[2].canonical_payload


@pytest.mark.asyncio
async def test_problem_model_receipt_rejects_nonfinite_json_without_silent_loss(db_session):
    project = Project(name="invalid problem source", user_id="source-test-user")
    db_session.add(project)
    await db_session.flush()
    project.problem_model = {"eta": float("nan")}
    with pytest.raises(ValueError, match="NaN and infinity"):
        await db_session.flush()


@pytest.mark.asyncio
async def test_problem_model_orm_receipt_reuses_strict_canonical_number_rules(db_session):
    project = Project(name="canonical problem source", user_id="source-test-user")
    db_session.add(project)
    await db_session.flush()
    project.problem_model = {"tiny": 1e-7, "zero": -0.0}
    await db_session.flush()
    receipt = (
        await db_session.scalars(
            select(StrategicMemorySource).where(
                StrategicMemorySource.project_id == project.id,
                StrategicMemorySource.source_type == "problem_model_revision",
            )
        )
    ).one()
    # This is deliberately the exact representation used by non-ORM/backfill
    # receipt creation too: no second near-canonical JSON implementation.
    assert receipt.canonical_payload == b'{"tiny":1e-7,"zero":0}'


@pytest.mark.asyncio
async def test_message_redaction_tombstones_source_receipt_before_cache_invalidation(db_session):
    project = Project(name="source redaction", user_id="source-test-user")
    db_session.add(project)
    await db_session.flush()
    message = ProjectMessage(
        project_id=project.id,
        sequence=1,
        surface=MessageSurface.CONVERSATION,
        from_type="user",
        content="sensitive exact source",
    )
    db_session.add(message)
    await db_session.flush()
    source = (
        await db_session.scalars(
            select(StrategicMemorySource).where(
                StrategicMemorySource.project_id == project.id,
                StrategicMemorySource.source_key == str(message.id),
            )
        )
    ).one()

    message.content = "[redacted]"
    await db_session.flush()
    await db_session.refresh(source)
    assert source.payload_state == "redacted"
    assert source.tombstoned_at is not None
    # The immutable receipt remains auditable; only a governed source action
    # can remove it, so its disposition is not cascaded or silently erased.
    assert await db_session.get(StrategicMemorySourceDisposition, source.id) is not None


@pytest.mark.asyncio
async def test_message_delete_marks_source_receipt_deleted_without_semantic_cascade(db_session):
    project = Project(name="source deletion", user_id="source-test-user")
    db_session.add(project)
    await db_session.flush()
    message = ProjectMessage(
        project_id=project.id,
        sequence=1,
        surface=MessageSurface.CONVERSATION,
        from_type="user",
        content="delete this native source",
    )
    db_session.add(message)
    await db_session.flush()
    source = (
        await db_session.scalars(
            select(StrategicMemorySource).where(
                StrategicMemorySource.project_id == project.id,
                StrategicMemorySource.source_key == str(message.id),
            )
        )
    ).one()
    await db_session.delete(message)
    await db_session.flush()
    await db_session.refresh(source)
    assert source.payload_state == "deleted"
    assert source.tombstoned_at is not None
    assert await db_session.get(StrategicMemorySourceDisposition, source.id) is not None


@pytest.mark.asyncio
async def test_privacy_flush_acquires_state_before_tombstone_rows(
    db_session, monkeypatch
):
    project = Project(name="privacy lock order", user_id="source-test-user")
    db_session.add(project)
    await db_session.flush()
    message = ProjectMessage(
        project_id=project.id,
        sequence=1,
        surface=MessageSurface.CONVERSATION,
        from_type="user",
        content="lock ordered source",
    )
    db_session.add(message)
    await db_session.flush()

    events: list[str] = []
    original_state = models_module._strategic_memory_state_upsert_for_flush
    original_tombstone = (
        models_module._tombstone_project_message_source_receipts_for_privacy_mutation
    )

    def state_wrapper(*args, **kwargs):
        events.append("project_state")
        return original_state(*args, **kwargs)

    def tombstone_wrapper(*args, **kwargs):
        events.append("job_source_disposition")
        return original_tombstone(*args, **kwargs)

    monkeypatch.setattr(
        models_module, "_strategic_memory_state_upsert_for_flush", state_wrapper
    )
    monkeypatch.setattr(
        models_module,
        "_tombstone_project_message_source_receipts_for_privacy_mutation",
        tombstone_wrapper,
    )
    message.content = "[redacted]"
    await db_session.flush()

    assert events == ["project_state", "job_source_disposition"]
