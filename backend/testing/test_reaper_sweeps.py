"""Tests for reaper sweep helpers.

Covers:
- Step 18e: unlinked BLOCKER decay (sweep_decay_stale_unlinked_blocker_messages)
- Viewable-media retirement (sweep_retire_compute_job_viewable_files)
"""

import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

from app.models import MessageType, Project, ProjectMessage
from app.reaper_bug_message_sweeps import (
    STALE_OPEN_UNLINKED_BLOCKER_DECAY_DAYS,
    sweep_decay_stale_unlinked_blocker_messages,
)
from app.reaper_sweeps import sweep_retire_compute_job_viewable_files
from app.time_utils import _utcnow


@pytest_asyncio.fixture()
async def sweep_db():
    """Isolated in-memory SQLite DB for sweep unit tests."""
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
    project = Project(name="sweep-unit", user_id="user1")
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


@pytest.mark.asyncio
async def test_blocker_message_auto_decay(sweep_db):
    """An unlinked, unowned OPEN BLOCKER older than the threshold is auto-decayed.

    This is the primary acceptance test for Step 18e. It verifies that a
    user-authored BLOCKER with no _convergence_link and no ownership context
    is resolved with 'auto-decayed (unlinked dead thread)' after the configured
    decay window.
    """
    project = await _make_project(sweep_db)
    old = _utcnow() - timedelta(days=STALE_OPEN_UNLINKED_BLOCKER_DECAY_DAYS + 1)
    msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.BLOCKER,
        from_type="user",
        content="stale operator-filed blocker with no follow-up",
        status="open",
        context={},
        created_at=old,
    )
    sweep_db.add(msg)
    await sweep_db.commit()
    await sweep_db.refresh(msg)

    acted = await sweep_decay_stale_unlinked_blocker_messages(sweep_db, _utcnow())

    assert acted == 1
    await sweep_db.refresh(msg)
    assert msg.status == "resolved"
    assert msg.resolution == "auto-decayed (unlinked dead thread)"
    assert msg.resolved_at is not None


@pytest.mark.asyncio
async def test_blocker_message_auto_decay_system_authored(sweep_db):
    """A system-authored BLOCKER with no link/ownership also decays via Step 18e."""
    project = await _make_project(sweep_db)
    old = _utcnow() - timedelta(days=STALE_OPEN_UNLINKED_BLOCKER_DECAY_DAYS + 5)
    msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.BLOCKER,
        from_type="system",
        content="old system blocker with no owner",
        status="open",
        context={},
        created_at=old,
    )
    sweep_db.add(msg)
    await sweep_db.commit()
    await sweep_db.refresh(msg)

    acted = await sweep_decay_stale_unlinked_blocker_messages(sweep_db, _utcnow())

    assert acted == 1
    await sweep_db.refresh(msg)
    assert msg.status == "resolved"
    assert msg.resolution == "auto-decayed (unlinked dead thread)"


@pytest.mark.asyncio
async def test_blocker_auto_decay_skips_fresh(sweep_db):
    """A BLOCKER newer than the decay threshold is left open."""
    project = await _make_project(sweep_db)
    recent = _utcnow() - timedelta(days=STALE_OPEN_UNLINKED_BLOCKER_DECAY_DAYS - 1)
    msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.BLOCKER,
        from_type="user",
        content="recent blocker still within window",
        status="open",
        context={},
        created_at=recent,
    )
    sweep_db.add(msg)
    await sweep_db.commit()
    await sweep_db.refresh(msg)

    acted = await sweep_decay_stale_unlinked_blocker_messages(sweep_db, _utcnow())

    assert acted == 0
    await sweep_db.refresh(msg)
    assert msg.status == "open"


@pytest.mark.asyncio
async def test_blocker_auto_decay_skips_linked(sweep_db):
    """A BLOCKER with a live _convergence_link is skipped (Step 15 owns it)."""
    project = await _make_project(sweep_db)
    old = _utcnow() - timedelta(days=STALE_OPEN_UNLINKED_BLOCKER_DECAY_DAYS + 10)
    msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.BLOCKER,
        from_type="user",
        content="blocker linked to a direction",
        status="open",
        context={"_convergence_link": {"kind": "direction", "id": "d-some-dir"}},
        created_at=old,
    )
    sweep_db.add(msg)
    await sweep_db.commit()
    await sweep_db.refresh(msg)

    acted = await sweep_decay_stale_unlinked_blocker_messages(sweep_db, _utcnow())

    assert acted == 0
    await sweep_db.refresh(msg)
    assert msg.status == "open"


@pytest.mark.asyncio
async def test_blocker_auto_decay_skips_direction_owned(sweep_db):
    """A BLOCKER with direction_id ownership is skipped (Step 17 owns it)."""
    project = await _make_project(sweep_db)
    old = _utcnow() - timedelta(days=STALE_OPEN_UNLINKED_BLOCKER_DECAY_DAYS + 10)
    msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.BLOCKER,
        from_type="system",
        content="system blocker tied to an active direction",
        status="open",
        context={"direction_id": "d-still-active"},
        created_at=old,
    )
    sweep_db.add(msg)
    await sweep_db.commit()
    await sweep_db.refresh(msg)

    acted = await sweep_decay_stale_unlinked_blocker_messages(sweep_db, _utcnow())

    assert acted == 0
    await sweep_db.refresh(msg)
    assert msg.status == "open"


@pytest.mark.asyncio
async def test_blocker_auto_decay_skips_task_owned(sweep_db):
    """A BLOCKER with pr_number+source_task_id ownership is skipped (Step 17 owns it)."""
    project = await _make_project(sweep_db)
    old = _utcnow() - timedelta(days=STALE_OPEN_UNLINKED_BLOCKER_DECAY_DAYS + 20)
    msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.BLOCKER,
        from_type="agent",
        content="agent blocker linked to a specific task PR",
        status="open",
        context={
            "pr_number": 9999,
            "source_task_id": "00000000-0000-0000-0000-000000000002",
        },
        created_at=old,
    )
    sweep_db.add(msg)
    await sweep_db.commit()
    await sweep_db.refresh(msg)

    acted = await sweep_decay_stale_unlinked_blocker_messages(sweep_db, _utcnow())

    assert acted == 0
    await sweep_db.refresh(msg)
    assert msg.status == "open"


@pytest.mark.asyncio
async def test_blocker_auto_decay_does_not_touch_bug_report(sweep_db):
    """Step 18e targets BLOCKER only; BUG_REPORT is untouched (Step 18d covers it)."""
    project = await _make_project(sweep_db)
    old = _utcnow() - timedelta(days=STALE_OPEN_UNLINKED_BLOCKER_DECAY_DAYS + 5)
    msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.BUG_REPORT,
        from_type="user",
        content="user-authored bug report — not a blocker",
        status="open",
        context={},
        created_at=old,
    )
    sweep_db.add(msg)
    await sweep_db.commit()
    await sweep_db.refresh(msg)

    acted = await sweep_decay_stale_unlinked_blocker_messages(sweep_db, _utcnow())

    assert acted == 0
    await sweep_db.refresh(msg)
    assert msg.status == "open"


# ── sweep_retire_compute_job_viewable_files ───────────────────────────────────


def _fake_compute_job_file(storage_uri: str) -> MagicMock:
    """Build a ComputeJobFile-like mock for use in session.execute results."""
    row = MagicMock(spec_set=["id", "compute_job_id", "safe_relpath", "media_type",
                               "storage_uri", "content_type", "file_size_bytes"])
    row.id = uuid.uuid4()
    row.compute_job_id = uuid.uuid4()
    row.safe_relpath = "plot.png"
    row.media_type = "image"
    row.storage_uri = storage_uri
    row.content_type = "image/png"
    row.file_size_bytes = 1024
    return row


def _mock_session_returning(rows):
    """Return an AsyncMock session whose execute().scalars().all() yields rows."""
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = rows
    session = AsyncMock()
    session.execute.return_value = mock_result
    return session


@pytest.mark.asyncio
async def test_sweep_retire_compute_job_viewable_files():
    """Old terminal-job viewable files: GCS deleted and DB row removed."""
    row = _fake_compute_job_file(
        "gs://bucket/compute-jobs/abc/artifacts/viewable/plot.png"
    )
    session = _mock_session_returning([row])

    with patch(
        "app.reaper_sweeps.delete_storage_uri_best_effort",
        new_callable=AsyncMock,
        return_value=True,
    ) as mock_del:
        result = await sweep_retire_compute_job_viewable_files(session, batch_size=50)

    assert result == 1
    mock_del.assert_awaited_once_with(row.storage_uri)
    session.delete.assert_awaited_once_with(row)
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_sweep_retire_returns_zero_when_no_eligible_rows():
    """No rows older than horizon: sweep returns 0 and commits nothing."""
    session = _mock_session_returning([])

    with patch(
        "app.reaper_sweeps.delete_storage_uri_best_effort",
        new_callable=AsyncMock,
    ) as mock_del:
        result = await sweep_retire_compute_job_viewable_files(session, batch_size=50)

    assert result == 0
    mock_del.assert_not_awaited()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_sweep_retire_gcs_failure_leaves_db_row():
    """When GCS delete returns False the DB row must NOT be deleted."""
    row = _fake_compute_job_file(
        "gs://bucket/compute-jobs/abc/artifacts/viewable/vid.mp4"
    )
    session = _mock_session_returning([row])

    with patch(
        "app.reaper_sweeps.delete_storage_uri_best_effort",
        new_callable=AsyncMock,
        return_value=False,
    ):
        result = await sweep_retire_compute_job_viewable_files(session, batch_size=50)

    assert result == 0
    session.delete.assert_not_awaited()
    session.commit.assert_not_awaited()
    session.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_sweep_retire_batch_size_limits_query():
    """batch_size=1 restricts the SELECT; only the returned rows are processed."""
    row_a = _fake_compute_job_file(
        "gs://bucket/compute-jobs/a/artifacts/viewable/a.png"
    )
    # The query honours the cap — simulate by returning only row_a.
    session = _mock_session_returning([row_a])

    with patch(
        "app.reaper_sweeps.delete_storage_uri_best_effort",
        new_callable=AsyncMock,
        return_value=True,
    ) as mock_del:
        result = await sweep_retire_compute_job_viewable_files(session, batch_size=1)

    assert result == 1
    mock_del.assert_awaited_once_with(row_a.storage_uri)


@pytest.mark.asyncio
async def test_sweep_retire_timeout_rolls_back():
    """TimeoutError during GCS deletes triggers rollback and returns 0."""
    row = _fake_compute_job_file(
        "gs://bucket/compute-jobs/abc/artifacts/viewable/plot.png"
    )
    session = _mock_session_returning([row])

    with patch(
        "app.reaper_sweeps.delete_storage_uri_best_effort",
        new_callable=AsyncMock,
        side_effect=TimeoutError,
    ):
        result = await sweep_retire_compute_job_viewable_files(
            session, batch_size=50
        )

    assert result == 0
    session.delete.assert_not_awaited()
    session.commit.assert_not_awaited()
    session.rollback.assert_awaited_once()
