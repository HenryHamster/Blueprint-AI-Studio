"""Regression tests for the stranded-sidecar recovery sweep (WI-1).

Covers the infinite-retry wedge (orchestrator error-budget remediation): a
source whose durable ``sidecar_verdict_application_pending`` marker references a
side-car whose own SOURCE_KEY has diverged — the referenced review is already
resolved / a replayed terminal status — is handed to the lane resolver, which
short-circuits "already resolved" WITHOUT clearing the *marked* source's marker.
Pre-fix the sweep re-selects, re-logs (``… has unapplied side-car verdict … —
retrying application``) and re-counts the same handful of sources every ~2-min
cycle forever. The sweep must authoritatively clear the marker on the source it
selected once the resolver returns without raising.
"""

import uuid

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

# Import ``app.services`` FIRST to establish the
# services → recovery_sweeps → recovery_post_commit → recovery_sidecar load
# order; importing ``app.recovery_sidecar`` directly first triggers a circular
# import (recovery_sidecar → services → … → recovery_sidecar partially init).
import app.services  # noqa: F401
from app.models import Project, Task, TaskStatus
from app.recovery_sidecar import (
    _SIDECAR_VERDICT_APPLICATION_PENDING_KEY,
    _clear_stranded_pending_marker,
)


@pytest_asyncio.fixture()
async def rs_db(mocker):
    """Isolated in-memory SQLite DB for recovery-sidecar sweep tests."""
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

    # The sweep opens its own session via ``services.AsyncSessionLocal``; the
    # review resolver's nested re-dispatch uses ``task_lifecycle.AsyncSessionLocal``.
    mocker.patch("app.services.AsyncSessionLocal", Session)
    mocker.patch("app.task_lifecycle.AsyncSessionLocal", Session)
    mocker.patch("app.task_lifecycle.broadcast_to_project", new_callable=AsyncMock)

    async with Session() as session:
        yield session, Session

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await engine.dispose()


async def _project(session) -> Project:
    project = Project(name="rs", user_id="u")
    session.add(project)
    await session.commit()
    await session.refresh(project)
    return project


def _pending_marker(sidecar_id) -> dict:
    return {
        "lane": "review_second_opinion",
        "sidecar_task_id": str(sidecar_id),
        "verdict_key": "review_second_opinion_reconciliation",
    }


async def _add_task(session, project, *, context, status=TaskStatus.COMPLETED) -> Task:
    task = Task(
        project_id=project.id,
        payloads=[{"type": "review", "pr_number": 1, "branch": "main"}],
        status=status,
        created_by="ORCHESTRATOR",
        context=context,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


@pytest.mark.asyncio
async def test_clear_stranded_pending_marker_clears_matching(rs_db):
    """Direct: the helper clears the marker (and only the marker) when the
    sidecar id matches, preserving unrelated context."""
    session, _ = rs_db
    project = await _project(session)
    sidecar_id = uuid.uuid4()
    src = await _add_task(
        session,
        project,
        context={
            _SIDECAR_VERDICT_APPLICATION_PENDING_KEY: _pending_marker(sidecar_id),
            "other": "keep",
        },
    )

    cleared = await _clear_stranded_pending_marker(
        session, source_id=str(src.id), sidecar_id=str(sidecar_id)
    )

    assert cleared is True
    await session.refresh(src)
    ctx = src.context or {}
    assert _SIDECAR_VERDICT_APPLICATION_PENDING_KEY not in ctx
    assert ctx.get("other") == "keep"


@pytest.mark.asyncio
async def test_clear_stranded_pending_marker_preserves_marker_for_other_sidecar(rs_db):
    """A marker that now references a freshly-spawned challenger (different
    sidecar id) is legitimately-pending work and must be left untouched."""
    session, _ = rs_db
    project = await _project(session)
    src = await _add_task(
        session,
        project,
        context={_SIDECAR_VERDICT_APPLICATION_PENDING_KEY: _pending_marker(uuid.uuid4())},
    )

    cleared = await _clear_stranded_pending_marker(
        session, source_id=str(src.id), sidecar_id=str(uuid.uuid4())
    )

    assert cleared is False
    await session.refresh(src)
    assert _SIDECAR_VERDICT_APPLICATION_PENDING_KEY in (src.context or {})


@pytest.mark.asyncio
async def test_clear_stranded_pending_marker_missing_source_is_noop(rs_db):
    """A vanished source id resolves to a clean no-op, not an exception."""
    session, _ = rs_db
    cleared = await _clear_stranded_pending_marker(
        session, source_id=str(uuid.uuid4()), sidecar_id=str(uuid.uuid4())
    )
    assert cleared is False
