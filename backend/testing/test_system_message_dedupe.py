"""Tests for the shared create-time de-dup primitive that prevents recurring
system BUG_REPORT/BLOCKER conditions from flooding the strategist inbox with
duplicates of one root cause (notification_dispatch.open_system_bug_or_blocker_exists).
"""

import uuid

import pytest
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import MessageType, Project, ProjectMessage
from app.notification_dispatch import (
    open_system_bug_or_blocker_exists,
    post_auto_fix_escalation_message,
    post_direction_burn_pause_message,
    post_residual_fix_escalation_message,
)


async def _project(session: AsyncSession) -> Project:
    project = Project(
        name="Test Repo",
        user_id="u1",
        github_owner="acme",
        github_repo="webapp",
    )
    session.add(project)
    await session.flush()
    return project


async def _count_bug_messages(session: AsyncSession, project_id: uuid.UUID) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(ProjectMessage)
        .where(
            ProjectMessage.project_id == project_id,
            ProjectMessage.message_type.in_(
                (MessageType.BUG_REPORT, MessageType.BLOCKER)
            ),
        )
    )
    return int(result.scalar_one())


@pytest.mark.asyncio
async def test_auto_fix_escalation_dedupes_same_branch(db_session: AsyncSession):
    project = await _project(db_session)
    first = await post_auto_fix_escalation_message(
        db_session, project.id, branch="release/x", failure_count=3
    )
    await db_session.flush()
    second = await post_auto_fix_escalation_message(
        db_session, project.id, branch="release/x", failure_count=4
    )
    await db_session.flush()

    # Same row refreshed in-place, not a duplicate.
    assert second.id == first.id
    assert second.context["consecutive_failures"] == 4
    assert second.context["dedupe_key"] == "auto_fix_escalated:release/x"
    assert await _count_bug_messages(db_session, project.id) == 1


@pytest.mark.asyncio
async def test_distinct_branches_are_not_deduped(db_session: AsyncSession):
    project = await _project(db_session)
    await post_auto_fix_escalation_message(
        db_session, project.id, branch="release/x", failure_count=3
    )
    await db_session.flush()
    await post_auto_fix_escalation_message(
        db_session, project.id, branch="release/y", failure_count=3
    )
    await db_session.flush()
    assert await _count_bug_messages(db_session, project.id) == 2


@pytest.mark.asyncio
async def test_residual_escalation_dedupes_same_fingerprint(db_session: AsyncSession):
    project = await _project(db_session)
    first = await post_residual_fix_escalation_message(
        db_session, project.id, fingerprint="fp-abc", failure_count=3
    )
    await db_session.flush()
    second = await post_residual_fix_escalation_message(
        db_session, project.id, fingerprint="fp-abc", failure_count=5
    )
    await db_session.flush()
    assert second.id == first.id
    assert await _count_bug_messages(db_session, project.id) == 1


@pytest.mark.asyncio
async def test_direction_pause_without_id_stays_byte_identical(db_session: AsyncSession):
    """An un-keyed creator must be a no-op for de-dup: two pauses with no
    direction_id both create messages and carry no dedupe_key field."""
    project = await _project(db_session)
    m1 = await post_direction_burn_pause_message(
        db_session, project.id, direction_text="d", reason="r", direction_id=None
    )
    await db_session.flush()
    m2 = await post_direction_burn_pause_message(
        db_session, project.id, direction_text="d", reason="r", direction_id=None
    )
    await db_session.flush()
    assert m1.id != m2.id
    assert "dedupe_key" not in m1.context
    assert await _count_bug_messages(db_session, project.id) == 2


@pytest.mark.asyncio
async def test_direction_pause_with_id_dedupes(db_session: AsyncSession):
    project = await _project(db_session)
    m1 = await post_direction_burn_pause_message(
        db_session, project.id, direction_text="d", reason="r", direction_id="dir-1"
    )
    await db_session.flush()
    m2 = await post_direction_burn_pause_message(
        db_session, project.id, direction_text="d", reason="r2", direction_id="dir-1"
    )
    await db_session.flush()
    assert m2.id == m1.id
    assert await _count_bug_messages(db_session, project.id) == 1


@pytest.mark.asyncio
async def test_empty_key_and_user_message_never_match(db_session: AsyncSession):
    project = await _project(db_session)
    # Falsy key never matches.
    assert await open_system_bug_or_blocker_exists(db_session, project.id, "") is None

    # A user-authored bug message is never matched even with a colliding key.
    db_session.add(
        ProjectMessage(
            project_id=project.id,
            message_type=MessageType.BUG_REPORT,
            from_type="user",
            content="manual report",
            context={"dedupe_key": "k1"},
        )
    )
    await db_session.flush()
    assert await open_system_bug_or_blocker_exists(db_session, project.id, "k1") is None
