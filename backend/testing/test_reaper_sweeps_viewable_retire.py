"""Tests for sweep_retire_viewable_artifacts (Step 23).

Covers:
- Happy path: ComputeJobFile rows for jobs completed > retention window are deleted.
- Fresh jobs: rows for recently-completed jobs are untouched.
- Active jobs: rows for RUNNING/QUEUED jobs with no completed_at are skipped.
- GCS failure: when delete_storage_uri_best_effort returns False, the DB row is kept.
- Batch limit: only up to batch_size rows are processed per call.
- Retention param: the retention_days arg controls the cutoff.
"""

import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, select

from app.models import ComputeJob, ComputeJobFile, ComputeJobStatus, Project
from app.reaper_sweeps import (
    VIEWABLE_ARTIFACT_RETENTION_DAYS,
    sweep_retire_viewable_artifacts,
)
from app.time_utils import _utcnow

# Anchor to the sweep's own live clock so _RECENT stays the intended distance
# inside the retention window regardless of the real calendar date. A hardcoded
# _NOW was a wall-clock time-bomb: once real time advanced past it, _RECENT fell
# before the sweep's live cutoff and "recent" jobs were wrongly retired.
_NOW = _utcnow()
_OLD = _NOW - timedelta(days=VIEWABLE_ARTIFACT_RETENTION_DAYS + 1)
_RECENT = _NOW - timedelta(days=VIEWABLE_ARTIFACT_RETENTION_DAYS - 1)


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


async def _make_job(
    db: AsyncSession,
    project_id: uuid.UUID,
    *,
    completed_at: datetime | None = None,
    status: ComputeJobStatus = ComputeJobStatus.SUCCEEDED,
) -> ComputeJob:
    job = ComputeJob(
        project_id=project_id,
        backend="local_runner",
        status=status,
        completed_at=completed_at,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    return job


async def _make_file(
    db: AsyncSession,
    job_id: uuid.UUID,
    *,
    basename: str = "plot.png",
) -> ComputeJobFile:
    f = ComputeJobFile(
        compute_job_id=job_id,
        safe_relpath=basename,
        media_type="image",
        storage_uri=f"gs://bucket/compute-jobs/{job_id}/artifacts/viewable/{basename}",
        content_type="image/png",
    )
    db.add(f)
    await db.commit()
    await db.refresh(f)
    return f


@pytest.mark.asyncio
async def test_retire_old_completed_job_files(db):
    """Files for a job completed past the retention window are deleted."""
    project = await _make_project(db)
    job = await _make_job(db, project.id, completed_at=_OLD)
    f = await _make_file(db, job.id)

    with patch(
        "app.reaper_sweeps.delete_storage_uri_best_effort",
        new=AsyncMock(return_value=True),
    ) as mock_delete:
        retired = await sweep_retire_viewable_artifacts(
            db, retention_days=VIEWABLE_ARTIFACT_RETENTION_DAYS
        )

    assert retired == 1
    mock_delete.assert_awaited_once_with(f.storage_uri)

    remaining = (await db.execute(select(ComputeJobFile))).scalars().all()
    assert remaining == []


@pytest.mark.asyncio
async def test_skip_recently_completed_job_files(db):
    """Files for a job completed within the retention window are untouched."""
    project = await _make_project(db)
    job = await _make_job(db, project.id, completed_at=_RECENT)
    await _make_file(db, job.id)

    with patch(
        "app.reaper_sweeps.delete_storage_uri_best_effort",
        new=AsyncMock(return_value=True),
    ) as mock_delete:
        retired = await sweep_retire_viewable_artifacts(
            db, retention_days=VIEWABLE_ARTIFACT_RETENTION_DAYS
        )

    assert retired == 0
    mock_delete.assert_not_awaited()

    remaining = (await db.execute(select(ComputeJobFile))).scalars().all()
    assert len(remaining) == 1


@pytest.mark.asyncio
async def test_skip_active_job_files(db):
    """Files for a RUNNING job (no completed_at) are never eligible."""
    project = await _make_project(db)
    job = await _make_job(
        db, project.id, completed_at=None, status=ComputeJobStatus.RUNNING
    )
    await _make_file(db, job.id)

    with patch(
        "app.reaper_sweeps.delete_storage_uri_best_effort",
        new=AsyncMock(return_value=True),
    ) as mock_delete:
        retired = await sweep_retire_viewable_artifacts(db)

    assert retired == 0
    mock_delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_gcs_failure_preserves_db_row(db):
    """When GCS delete returns False, the DB row is NOT removed."""
    project = await _make_project(db)
    job = await _make_job(db, project.id, completed_at=_OLD)
    f = await _make_file(db, job.id)

    with patch(
        "app.reaper_sweeps.delete_storage_uri_best_effort",
        new=AsyncMock(return_value=False),
    ):
        retired = await sweep_retire_viewable_artifacts(db)

    assert retired == 0

    remaining = (await db.execute(select(ComputeJobFile))).scalars().all()
    assert len(remaining) == 1
    assert remaining[0].id == f.id


@pytest.mark.asyncio
async def test_batch_size_limits_per_call(db):
    """Only batch_size rows are processed per call; the rest remain."""
    project = await _make_project(db)
    job = await _make_job(db, project.id, completed_at=_OLD)
    [await _make_file(db, job.id, basename=f"img{i}.png") for i in range(5)]

    with patch(
        "app.reaper_sweeps.delete_storage_uri_best_effort",
        new=AsyncMock(return_value=True),
    ):
        retired = await sweep_retire_viewable_artifacts(db, batch_size=2)

    assert retired == 2
    remaining = (await db.execute(select(ComputeJobFile))).scalars().all()
    assert len(remaining) == 3


@pytest.mark.asyncio
async def test_mixed_jobs_only_old_retired(db):
    """Only files from the expired job are retired; fresh-job files survive."""
    project = await _make_project(db)
    old_job = await _make_job(db, project.id, completed_at=_OLD)
    new_job = await _make_job(db, project.id, completed_at=_RECENT)
    old_file = await _make_file(db, old_job.id, basename="old.png")
    await _make_file(db, new_job.id, basename="new.png")

    with patch(
        "app.reaper_sweeps.delete_storage_uri_best_effort",
        new=AsyncMock(return_value=True),
    ) as mock_delete:
        retired = await sweep_retire_viewable_artifacts(db)

    assert retired == 1
    mock_delete.assert_awaited_once_with(old_file.storage_uri)

    remaining_uris = {
        r.storage_uri
        for r in (await db.execute(select(ComputeJobFile))).scalars().all()
    }
    assert old_file.storage_uri not in remaining_uris
    assert f"gs://bucket/compute-jobs/{new_job.id}/artifacts/viewable/new.png" in remaining_uris


@pytest.mark.asyncio
async def test_custom_retention_days(db):
    """A shorter retention_days arg shifts the cutoff accordingly."""
    project = await _make_project(db)
    job_90d = await _make_job(
        db, project.id, completed_at=_NOW - timedelta(days=91)
    )
    job_30d = await _make_job(
        db, project.id, completed_at=_NOW - timedelta(days=31)
    )
    file_90d = await _make_file(db, job_90d.id, basename="old.png")
    await _make_file(db, job_30d.id, basename="fresh.png")

    with patch(
        "app.reaper_sweeps.delete_storage_uri_best_effort",
        new=AsyncMock(return_value=True),
    ) as mock_delete:
        retired = await sweep_retire_viewable_artifacts(db, retention_days=90)

    assert retired == 1
    mock_delete.assert_awaited_once_with(file_90d.storage_uri)
