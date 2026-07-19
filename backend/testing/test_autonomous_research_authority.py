"""Race tests for legacy autonomous writes at the Resident cutover boundary."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlmodel import select

from app.models import Project, StrategicResident
from app import notification_dispatch
from app.strategic_resident_checkpointing import initialize_strategic_resident
from app.strategic_resident_transition_locks import (
    LegacyEngineAuthorityExpired,
    ResidentOwnershipTransitionError,
    commit_legacy_authorized,
    lock_and_validate_legacy_authority,
)
from app.time_utils import _utcnow


async def _project_with_legacy_resident(db_session) -> Project:
    project = Project(
        id=uuid.uuid4(),
        name="Legacy authority race",
        user_id="operator",
        running_update="before",
    )
    db_session.add(project)
    await db_session.flush()
    await initialize_strategic_resident(
        db_session,
        project.id,
        locked_project=project,
    )
    await db_session.commit()
    return project


async def _project_without_resident(db_session) -> Project:
    project = Project(
        id=uuid.uuid4(),
        name="Pre-backfill legacy authority",
        user_id="operator",
        running_update="before",
    )
    db_session.add(project)
    await db_session.commit()
    return project


def test_authority_expiry_preserves_transition_exception_hierarchy():
    assert issubclass(LegacyEngineAuthorityExpired, ResidentOwnershipTransitionError)


@pytest.mark.asyncio
async def test_commit_legacy_authorized_commits_current_generation(db_session):
    project = await _project_with_legacy_resident(db_session)
    generation = await lock_and_validate_legacy_authority(db_session, project.id)
    current = await db_session.get(Project, project.id)
    assert current is not None
    current.running_update = "authorized"

    await commit_legacy_authorized(db_session, project.id, generation)

    persisted = await db_session.scalar(
        select(Project).where(Project.id == project.id)
    )
    assert persisted is not None
    assert persisted.running_update == "authorized"


@pytest.mark.asyncio
async def test_commit_legacy_authorized_rolls_back_after_cutover(db_session):
    project = await _project_with_legacy_resident(db_session)
    # The fence rolls the session back on the expiry path, which expires `project`;
    # snapshot the PK first so the post-rollback read below does not lazy-load an
    # expired instance (MissingGreenlet on Postgres).
    project_id = project.id
    generation = await lock_and_validate_legacy_authority(db_session, project.id)
    await db_session.commit()

    resident = await db_session.get(StrategicResident, project.id)
    assert resident is not None
    resident.engine_owner = "strategic_resident"
    resident.authority_generation = generation + 1
    db_session.add(resident)
    await db_session.commit()

    current = await db_session.get(Project, project.id)
    assert current is not None
    current.running_update = "stale legacy mutation"
    with pytest.raises(LegacyEngineAuthorityExpired):
        await commit_legacy_authorized(db_session, project.id, generation)

    persisted = await db_session.scalar(
        select(Project).where(Project.id == project_id)
    )
    assert persisted is not None
    await db_session.refresh(persisted)
    assert persisted.running_update == "before"


@pytest.mark.asyncio
async def test_pre_backfill_project_uses_reserved_generation_zero(db_session):
    project = await _project_without_resident(db_session)
    generation = await lock_and_validate_legacy_authority(db_session, project.id)
    assert generation == 0
    current = await db_session.get(Project, project.id)
    assert current is not None
    current.running_update = "authorized before backfill"

    await commit_legacy_authorized(db_session, project.id, generation)

    await db_session.refresh(current)
    assert current.running_update == "authorized before backfill"


@pytest.mark.asyncio
async def test_pre_backfill_token_survives_compatible_legacy_backfill(db_session):
    project = await _project_without_resident(db_session)
    generation = await lock_and_validate_legacy_authority(db_session, project.id)
    assert generation == 0
    await db_session.commit()

    await initialize_strategic_resident(db_session, project.id)
    await db_session.commit()
    current = await db_session.get(Project, project.id)
    assert current is not None
    current.running_update = "authorized after compatible backfill"

    await commit_legacy_authorized(db_session, project.id, generation)

    await db_session.refresh(current)
    assert current.running_update == "authorized after compatible backfill"


def _capture_scheduled_notifications(monkeypatch) -> list[object]:
    scheduled: list[object] = []

    def _capture(coro):
        scheduled.append(coro)
        coro.close()

    monkeypatch.setattr(notification_dispatch.asyncio, "ensure_future", _capture)
    return scheduled


@pytest.mark.asyncio
async def test_fenced_commit_flushes_then_restores_immediate_notification_dispatch(
    db_session, monkeypatch
):
    project = await _project_with_legacy_resident(db_session)
    generation = await lock_and_validate_legacy_authority(db_session, project.id)
    scheduled = _capture_scheduled_notifications(monkeypatch)
    notification_dispatch.begin_deferred_external_notifications(db_session)
    notification_dispatch._schedule_external_notification(
        db_session, project.id, "TEST_FENCED", {"phase": "fenced"}
    )
    assert scheduled == []

    await commit_legacy_authorized(db_session, project.id, generation)
    assert len(scheduled) == 1

    notification_dispatch._schedule_external_notification(
        db_session, project.id, "TEST_ORDINARY", {"phase": "ordinary"}
    )
    assert len(scheduled) == 2


@pytest.mark.asyncio
async def test_failed_fence_discards_then_restores_immediate_notification_dispatch(
    db_session, monkeypatch
):
    project = await _project_with_legacy_resident(db_session)
    # The failed fence rolls back and expires `project`; snapshot the PK so the
    # post-rollback notification read below does not lazy-load an expired instance
    # (MissingGreenlet on Postgres).
    project_id = project.id
    generation = await lock_and_validate_legacy_authority(db_session, project.id)
    await db_session.commit()
    scheduled = _capture_scheduled_notifications(monkeypatch)
    notification_dispatch.begin_deferred_external_notifications(db_session)
    notification_dispatch._schedule_external_notification(
        db_session, project.id, "TEST_STALE", {"phase": "stale"}
    )

    resident = await db_session.get(StrategicResident, project.id)
    assert resident is not None
    resident.engine_owner = "strategic_resident"
    resident.authority_generation = generation + 1
    await db_session.commit()
    with pytest.raises(LegacyEngineAuthorityExpired):
        await commit_legacy_authorized(db_session, project.id, generation)
    assert scheduled == []

    notification_dispatch._schedule_external_notification(
        db_session, project_id, "TEST_ORDINARY", {"phase": "ordinary"}
    )
    assert len(scheduled) == 1


@pytest.mark.asyncio
async def test_expired_authority_cannot_advance_shared_circuit_breaker(db_session):
    from app.autonomous_research import _advance_circuit_breaker_and_check

    project = await _project_with_legacy_resident(db_session)
    generation = await lock_and_validate_legacy_authority(db_session, project.id)
    await db_session.commit()
    resident = await db_session.get(StrategicResident, project.id)
    assert resident is not None
    resident.engine_owner = "strategic_resident"
    resident.authority_generation = generation + 1
    await db_session.commit()
    breaker = {
        "state": "OPEN",
        "opened_at": _utcnow() - timedelta(seconds=10),
        "cooldown_seconds": 1,
        "half_open_task_id": None,
    }

    with pytest.raises(LegacyEngineAuthorityExpired):
        await _advance_circuit_breaker_and_check(
            project,
            db_session,
            breaker,
            "authority race test",
            expected_legacy_generation=generation,
        )

    assert breaker["state"] == "OPEN"


@pytest.mark.asyncio
async def test_operator_notification_does_not_swallow_authority_expiry(
    db_session, monkeypatch
):
    from app.autonomous_research import _maybe_notify_operator_steering_acted
    from app import strategic_resident_transition_locks as transition_locks

    project = await _project_with_legacy_resident(db_session)

    async def _stage_message(*_args, **_kwargs):
        return None

    async def _expired(*_args, **_kwargs):
        raise LegacyEngineAuthorityExpired("superseded")

    monkeypatch.setattr(
        notification_dispatch,
        "post_strategic_steering_acted_message",
        _stage_message,
    )
    monkeypatch.setattr(transition_locks, "commit_legacy_authorized", _expired)

    with pytest.raises(LegacyEngineAuthorityExpired):
        await _maybe_notify_operator_steering_acted(
            project,
            db_session,
            expected_legacy_generation=1,
        )
