"""Tests for reaper bug-message sweep helpers (Step 18e: unlinked BLOCKER decay)."""

import pytest
import pytest_asyncio
from datetime import timedelta
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

from app.models import MessageType, Project, ProjectMessage
from app.reaper_bug_message_sweeps import (
    STALE_OPEN_UNLINKED_BLOCKER_DECAY_DAYS,
    sweep_decay_stale_unlinked_blocker_messages,
)
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
