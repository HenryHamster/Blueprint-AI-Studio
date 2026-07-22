"""Tests for message-attachment retention semantics.

Covers:
- Orphan sweep selection: consumed/linked receipts are NEVER selected by
  sweep_expired_message_attachment_uploads (only unconsumed + unlinked ones are).
- Consumed-attachment retirement: sweep_retire_consumed_message_attachments
  deletes GCS objects and DB rows for receipts whose consumed_at is past the
  retention window, and leaves recent/unconsumed ones untouched.
- GCS failure: when delete_storage_uri_best_effort returns False, the DB row is kept.
- Batch limit: only up to batch_size rows are processed per call.
- Drift guard: MESSAGE_ATTACHMENT_RETENTION_DAYS in reaper_sweeps re-exports the
  same value from storage so both the sweep and any future expiry stamp share one
  constant.
"""

import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, select

from app.models import Project, ProjectMessageAttachmentUpload
from app.reaper_sweeps import (
    MESSAGE_ATTACHMENT_RETENTION_DAYS as REAPER_RETENTION_DAYS,
    sweep_expired_message_attachment_uploads,
    sweep_retire_consumed_message_attachments,
)
from app.storage import MESSAGE_ATTACHMENT_RETENTION_DAYS as STORAGE_RETENTION_DAYS
from app.time_utils import _utcnow

_NOW = _utcnow()
_OLD = _NOW - timedelta(days=REAPER_RETENTION_DAYS + 1)
_RECENT = _NOW - timedelta(days=REAPER_RETENTION_DAYS - 1)
_EXPIRES_FUTURE = _NOW + timedelta(hours=1)
_EXPIRES_PAST = _NOW - timedelta(minutes=15)


@pytest_asyncio.fixture()
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    Session = sessionmaker(
        bind=engine,
        class_=AsyncSession,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )
    async with Session() as s:
        yield s

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await engine.dispose()


async def _make_project(db: AsyncSession) -> Project:
    p = Project(name="test-proj", user_id="user-1")
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return p


def _make_receipt(
    project_id: uuid.UUID,
    *,
    consumed_at=None,
    message_id: uuid.UUID | None = None,
    expires_at=None,
    index: int = 0,
) -> ProjectMessageAttachmentUpload:
    aid = uuid.uuid4()
    effective_expires = expires_at if expires_at is not None else _EXPIRES_FUTURE
    # created_at must be before expires_at (model check constraint).
    created_at = effective_expires - timedelta(hours=1)
    return ProjectMessageAttachmentUpload(
        attachment_id=aid,
        project_id=project_id,
        actor_user_id=f"user-{index}",
        storage_uri=f"gs://bucket/message-attachments/{project_id}/{aid}/file.bin",
        filename="file.bin",
        expected_size_bytes=100,
        expires_at=effective_expires,
        consumed_at=consumed_at,
        message_id=message_id,
        created_at=created_at,
    )


# ---------------------------------------------------------------------------
# Drift guard
# ---------------------------------------------------------------------------


def test_retention_constant_shared_between_reaper_and_storage():
    """MESSAGE_ATTACHMENT_RETENTION_DAYS in reaper_sweeps must equal storage."""
    assert REAPER_RETENTION_DAYS == STORAGE_RETENTION_DAYS, (
        f"reaper_sweeps exports {REAPER_RETENTION_DAYS} but storage defines "
        f"{STORAGE_RETENTION_DAYS} — they must share one authoritative constant"
    )


# ---------------------------------------------------------------------------
# Orphan sweep selection semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orphan_sweep_skips_consumed_receipt(db):
    """A consumed receipt (consumed_at set) must not be selected by the orphan sweep."""
    project = await _make_project(db)
    receipt = _make_receipt(
        project.id,
        consumed_at=_NOW - timedelta(minutes=5),
        expires_at=_EXPIRES_PAST,
    )
    db.add(receipt)
    await db.commit()

    with patch(
        "app.reaper_sweeps.delete_storage_uri_best_effort",
        new=AsyncMock(return_value=True),
    ) as mock_delete:
        removed = await sweep_expired_message_attachment_uploads(db)

    assert removed == 0
    mock_delete.assert_not_awaited()
    remaining = (
        await db.execute(select(ProjectMessageAttachmentUpload))
    ).scalars().all()
    assert len(remaining) == 1


@pytest.mark.asyncio
async def test_orphan_sweep_skips_linked_receipt(db):
    """A receipt linked to a message_id must not be selected by the orphan sweep."""
    project = await _make_project(db)
    # SQLite doesn't enforce the FK to projectmessage, so we can use a fake UUID
    receipt = _make_receipt(
        project.id,
        message_id=uuid.uuid4(),
        expires_at=_EXPIRES_PAST,
    )
    db.add(receipt)
    await db.commit()

    with patch(
        "app.reaper_sweeps.delete_storage_uri_best_effort",
        new=AsyncMock(return_value=True),
    ) as mock_delete:
        removed = await sweep_expired_message_attachment_uploads(db)

    assert removed == 0
    mock_delete.assert_not_awaited()
    remaining = (
        await db.execute(select(ProjectMessageAttachmentUpload))
    ).scalars().all()
    assert len(remaining) == 1


@pytest.mark.asyncio
async def test_orphan_sweep_deletes_unconsumed_expired_receipt(db):
    """An unconsumed, unlinked, expired receipt IS selected by the orphan sweep."""
    project = await _make_project(db)
    receipt = _make_receipt(project.id, expires_at=_EXPIRES_PAST)
    db.add(receipt)
    await db.commit()

    with patch(
        "app.reaper_sweeps.delete_storage_uri_best_effort",
        new=AsyncMock(return_value=True),
    ):
        removed = await sweep_expired_message_attachment_uploads(db)

    assert removed == 1
    remaining = (
        await db.execute(select(ProjectMessageAttachmentUpload))
    ).scalars().all()
    assert remaining == []


# ---------------------------------------------------------------------------
# Consumed-attachment retirement sweep
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retire_consumed_old_attachment(db):
    """A consumed receipt past the retention window is deleted."""
    project = await _make_project(db)
    receipt = _make_receipt(project.id, consumed_at=_OLD)
    db.add(receipt)
    await db.commit()

    with patch(
        "app.reaper_sweeps.delete_storage_uri_best_effort",
        new=AsyncMock(return_value=True),
    ) as mock_delete:
        retired = await sweep_retire_consumed_message_attachments(
            db, retention_days=REAPER_RETENTION_DAYS
        )

    assert retired == 1
    mock_delete.assert_awaited_once_with(receipt.storage_uri)
    remaining = (
        await db.execute(select(ProjectMessageAttachmentUpload))
    ).scalars().all()
    assert remaining == []


@pytest.mark.asyncio
async def test_skip_recently_consumed_attachment(db):
    """A consumed receipt within the retention window is untouched."""
    project = await _make_project(db)
    receipt = _make_receipt(project.id, consumed_at=_RECENT)
    db.add(receipt)
    await db.commit()

    with patch(
        "app.reaper_sweeps.delete_storage_uri_best_effort",
        new=AsyncMock(return_value=True),
    ) as mock_delete:
        retired = await sweep_retire_consumed_message_attachments(
            db, retention_days=REAPER_RETENTION_DAYS
        )

    assert retired == 0
    mock_delete.assert_not_awaited()
    remaining = (
        await db.execute(select(ProjectMessageAttachmentUpload))
    ).scalars().all()
    assert len(remaining) == 1


@pytest.mark.asyncio
async def test_skip_unconsumed_receipt_in_retirement_sweep(db):
    """An unconsumed receipt (consumed_at=None) is not touched by the retirement sweep."""
    project = await _make_project(db)
    # Even if expires_at is old, the retirement sweep only looks at consumed_at
    receipt = _make_receipt(project.id, consumed_at=None, expires_at=_EXPIRES_PAST)
    db.add(receipt)
    await db.commit()

    with patch(
        "app.reaper_sweeps.delete_storage_uri_best_effort",
        new=AsyncMock(return_value=True),
    ) as mock_delete:
        retired = await sweep_retire_consumed_message_attachments(db)

    assert retired == 0
    mock_delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_gcs_failure_keeps_db_row(db):
    """When GCS delete fails the DB row is retained for retry."""
    project = await _make_project(db)
    receipt = _make_receipt(project.id, consumed_at=_OLD)
    db.add(receipt)
    await db.commit()

    with patch(
        "app.reaper_sweeps.delete_storage_uri_best_effort",
        new=AsyncMock(return_value=False),
    ):
        retired = await sweep_retire_consumed_message_attachments(db)

    assert retired == 0
    remaining = (
        await db.execute(select(ProjectMessageAttachmentUpload))
    ).scalars().all()
    assert len(remaining) == 1


@pytest.mark.asyncio
async def test_batch_limit_respected(db):
    """sweep_retire_consumed_message_attachments processes at most batch_size rows."""
    project = await _make_project(db)
    receipts = [_make_receipt(project.id, consumed_at=_OLD, index=i) for i in range(4)]
    db.add_all(receipts)
    await db.commit()

    with patch(
        "app.reaper_sweeps.delete_storage_uri_best_effort",
        new=AsyncMock(return_value=True),
    ):
        retired = await sweep_retire_consumed_message_attachments(db, batch_size=2)

    assert retired == 2
    remaining = (
        await db.execute(select(ProjectMessageAttachmentUpload))
    ).scalars().all()
    assert len(remaining) == 2
