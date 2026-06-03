"""Tests for the durable unsafe-branch-configuration operator signal.

Covers ``branch_resolution.signal_unsafe_branch_configuration``:
  * the Alert and the paired BLOCKER ProjectMessage are actually persisted
    (committed), not merely staged;
  * the database-existence dedupe is restart-safe — re-invoking after the
    in-process dedupe sets have been cleared (simulating a fresh process /
    second instance) does NOT create a second Alert or BLOCKER message.
"""

import pytest
from sqlmodel import select

from app.branch_resolution import (
    _UNSAFE_BRANCH_ALERT_TITLE,
    _blocking_project_branch_alerts,
    signal_unsafe_branch_configuration,
)
from app.models import Alert, AlertSeverity, MessageType, ProjectMessage
from testing.factories import make_project


async def _alerts(db_session, project_id):
    stmt = select(Alert).where(
        Alert.project_id == project_id,
        Alert.title == _UNSAFE_BRANCH_ALERT_TITLE,
        Alert.dismissed_at.is_(None),
    )
    return (await db_session.execute(stmt)).scalars().all()


async def _blocker_messages(db_session, project_id):
    stmt = select(ProjectMessage).where(
        ProjectMessage.project_id == project_id,
        ProjectMessage.message_type == MessageType.BLOCKER,
    )
    return (await db_session.execute(stmt)).scalars().all()


@pytest.mark.asyncio
async def test_signal_persists_alert_and_blocker_message(db_session):
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await signal_unsafe_branch_configuration(
        db_session,
        project,
        issue_code="integration_equals_promotion",
        detail="Project would route fleet work onto the promotion target.",
    )

    # Read through a fresh session-state to prove the rows were committed, not
    # just staged on a transaction that a caller might roll back.
    db_session.expunge_all()

    alerts = await _alerts(db_session, project.id)
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.ERROR

    messages = await _blocker_messages(db_session, project.id)
    assert len(messages) == 1
    assert messages[0].status == "open"
    assert messages[0].from_type == "system"
    assert (
        messages[0].context.get("dedupe_key")
        == f"unsafe_branch_configuration:{project.id}:integration_equals_promotion"
    )


@pytest.mark.asyncio
async def test_dedupe_survives_process_restart(db_session):
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await signal_unsafe_branch_configuration(
        db_session,
        project,
        issue_code="integration_equals_promotion",
        detail="first emission",
    )

    # Simulate a fresh process / second backend instance: the in-process dedupe
    # set is empty, so any in-memory guard would let a duplicate through. The
    # database-existence dedupe must still suppress it.
    _blocking_project_branch_alerts.clear()

    await signal_unsafe_branch_configuration(
        db_session,
        project,
        issue_code="integration_equals_promotion",
        detail="second emission after restart",
    )

    db_session.expunge_all()
    assert len(await _alerts(db_session, project.id)) == 1
    assert len(await _blocker_messages(db_session, project.id)) == 1
