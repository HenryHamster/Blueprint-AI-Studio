"""Durable qualification-control receipts and central start-fence coverage."""

from __future__ import annotations

import asyncio
import os
import uuid
from unittest.mock import AsyncMock

from httpx import AsyncClient
import pytest
from sqlalchemy import func
from sqlmodel import select

from app import release_qualifier
from app.auth import get_current_user
from app.main import app
from app.models import (
    Project,
    ProjectMember,
    ReleaseQualificationControlRevision,
    ReleaseVersion,
)
from app.release_qualification_control import (
    QualificationControlConflict,
    apply_qualification_control,
    qualification_control_readback,
)


_OWNER_ID = "test-user-123"


async def _project(session, *, name: str = "Qualification Control") -> Project:
    project = Project(name=name, user_id=_OWNER_ID)
    session.add(project)
    await session.flush()
    return project


async def _project_version(session) -> tuple[Project, ReleaseVersion]:
    project = await _project(session)
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="stabilize",
        status="open",
    )
    session.add(version)
    await session.commit()
    return project, version


async def _hold(session, project_id: uuid.UUID, *, key: str = "hold-v57"):
    result = await apply_qualification_control(
        session,
        project_id=project_id,
        expected_revision=0,
        automatic_starts_enabled=False,
        idempotency_key=key,
        actor_user_id=_OWNER_ID,
        reason="Bound Resident qualification canary",
    )
    await session.commit()
    return result


@pytest.mark.asyncio
async def test_control_ledger_replays_before_cas_and_is_project_scoped(db_session):
    project = await _project(db_session, name="Controlled")
    other = await _project(db_session, name="Independent")
    await db_session.commit()

    default = await qualification_control_readback(db_session, project.id)
    assert default["current"]["revision"] == 0
    assert default["current"]["automatic_starts_enabled"] is True
    assert default["receipt"] is None

    first = await _hold(db_session, project.id)
    assert first["idempotent_replay"] is False
    assert first["receipt"]["revision"] == 1
    assert first["current"]["automatic_starts_enabled"] is False
    independent = await qualification_control_readback(db_session, other.id)
    assert independent["current"]["revision"] == 0
    assert independent["current"]["automatic_starts_enabled"] is True

    second = await apply_qualification_control(
        db_session,
        project_id=project.id,
        expected_revision=1,
        automatic_starts_enabled=True,
        idempotency_key="arm-after-v57",
        actor_user_id=_OWNER_ID,
        reason="Canary qualification completed",
    )
    await db_session.commit()
    assert second["receipt"]["revision"] == 2

    replay = await apply_qualification_control(
        db_session,
        project_id=project.id,
        expected_revision=0,
        automatic_starts_enabled=False,
        idempotency_key="hold-v57",
        actor_user_id=_OWNER_ID,
        reason="Bound Resident qualification canary",
    )
    assert replay["idempotent_replay"] is True
    assert replay["receipt"]["revision"] == 1
    assert replay["current"]["revision"] == 2

    with pytest.raises(QualificationControlConflict):
        await apply_qualification_control(
            db_session,
            project_id=project.id,
            expected_revision=0,
            automatic_starts_enabled=False,
            idempotency_key="hold-v57",
            actor_user_id="different-owner",
            reason="Bound Resident qualification canary",
        )
    with pytest.raises(QualificationControlConflict):
        await apply_qualification_control(
            db_session,
            project_id=project.id,
            expected_revision=0,
            automatic_starts_enabled=False,
            idempotency_key="stale-unused-key",
            actor_user_id=_OWNER_ID,
            reason="Stale request",
        )
    row_count = await db_session.scalar(
        select(func.count()).select_from(ReleaseQualificationControlRevision)
    )
    assert row_count == 2


@pytest.mark.asyncio
async def test_control_api_enforces_roles_strict_types_and_uncached_readback(
    client: AsyncClient, db_session
):
    project = await _project(db_session)
    db_session.add(
        ProjectMember(project_id=project.id, user_id=_OWNER_ID, role="owner")
    )
    await db_session.commit()
    path = f"/api/admin/projects/{project.id}/qualification-control"

    read = await client.get(path)
    assert read.status_code == 200, read.text
    assert read.headers["cache-control"] == "no-store, private"
    assert read.json()["requester_actor_user_id"] == _OWNER_ID
    assert read.json()["requester_can_mutate"] is True
    for invalid in (
        {
            "expected_revision": True,
            "automatic_starts_enabled": False,
            "idempotency_key": "strict-revision",
            "reason": "Reject bool revision",
        },
        {
            "expected_revision": 0,
            "automatic_starts_enabled": "false",
            "idempotency_key": "strict-state",
            "reason": "Reject string state",
        },
    ):
        assert (await client.post(path, json=invalid)).status_code == 422

    hold = await client.post(
        path,
        json={
            "expected_revision": 0,
            "automatic_starts_enabled": False,
            "idempotency_key": "api-hold",
            "reason": "API proof",
        },
    )
    assert hold.status_code == 200, hold.text
    assert hold.headers["cache-control"] == "no-store, private"
    assert hold.json()["receipt"]["actor_user_id"] == _OWNER_ID
    assert hold.json()["requester_actor_user_id"] == _OWNER_ID
    assert hold.json()["requester_can_mutate"] is True

    async def viewer_user():
        return {"user_id": "control-viewer", "email": "viewer@example.com"}

    db_session.add(
        ProjectMember(
            project_id=project.id,
            user_id="control-viewer",
            role="viewer",
        )
    )
    await db_session.commit()
    original_override = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = viewer_user
    try:
        viewer_read = await client.get(path)
        assert viewer_read.status_code == 200
        assert viewer_read.json()["requester_actor_user_id"] == "control-viewer"
        assert viewer_read.json()["requester_can_mutate"] is False
        assert (
            await client.post(
                path,
                json={
                    "expected_revision": 1,
                    "automatic_starts_enabled": True,
                    "idempotency_key": "viewer-arm",
                    "reason": "Forbidden",
                },
            )
        ).status_code == 403

        async def foreign_user():
            return {"user_id": "not-a-member", "email": "foreign@example.com"}

        app.dependency_overrides[get_current_user] = foreign_user
        assert (await client.get(path)).status_code == 404
    finally:
        if original_override is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = original_override


def _cheap_promotion_disabled(monkeypatch) -> None:
    async def compatible(*_args, **_kwargs):
        return set(), None

    monkeypatch.setattr(
        release_qualifier, "_drop_incompatible_product_probes", compatible
    )
    monkeypatch.setattr(
        release_qualifier, "resolve_project_promotion_enabled", lambda _pid: False
    )


@pytest.mark.asyncio
async def test_held_fresh_automatic_start_stops_before_downstream_work(
    db_session, monkeypatch
):
    project, version = await _project_version(db_session)
    project_id = project.id
    await _hold(db_session, project_id)

    async def downstream_bomb(*_args, **_kwargs):
        raise AssertionError("held automatic start reached downstream work")

    monkeypatch.setattr(
        release_qualifier, "_drop_incompatible_product_probes", downstream_bomb
    )
    result = await release_qualifier.run_qualification(
        db_session, project.id, version, auto_trigger=True
    )
    assert result["status"] == "automatic_qualification_disabled"
    assert version.last_qualification is None
    assert version.qualification_attempts == 0


@pytest.mark.asyncio
async def test_locked_fence_closes_early_read_race(db_session, monkeypatch):
    project, version = await _project_version(db_session)
    await _hold(db_session, project.id)
    _cheap_promotion_disabled(monkeypatch)

    async def stale_early_read(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        release_qualifier,
        "automatic_qualification_start_block",
        stale_early_read,
    )
    result = await release_qualifier.run_qualification(
        db_session, project.id, version, auto_trigger=True
    )
    assert result["status"] == "automatic_qualification_disabled"
    assert version.last_qualification is None


@pytest.mark.asyncio
async def test_reuse_promote_rechecks_hold_after_early_observation(
    db_session, monkeypatch
):
    from conftest import TestingSessionLocal

    project, version = await _project_version(db_session)
    project_id, version_id = project.id, version.id
    version.last_qualification = {
        "status": "promotion_failed",
        "promotion_failure_count": 0,
    }
    await db_session.commit()

    assert (
        await release_qualifier.automatic_qualification_start_block(
            db_session, project_id
        )
        is None
    )
    await db_session.rollback()
    async with TestingSessionLocal() as operator:
        await _hold(operator, project_id, key="hold-after-reuse-observation")

    monkeypatch.setattr(
        release_qualifier, "_promotion_failed_reuse_eligible", lambda *_args: True
    )
    promote = AsyncMock(return_value={"status": "promoted"})
    monkeypatch.setattr(release_qualifier, "promote", promote)
    reconcile = AsyncMock()
    trigger = AsyncMock()
    monkeypatch.setattr(
        release_qualifier, "_maybe_reconcile_after_promotion_conflict", reconcile
    )
    monkeypatch.setattr(
        release_qualifier, "_maybe_trigger_strategic_after_qualification", trigger
    )
    result = await release_qualifier._reattempt_promotion_failed_in_dedicated_session(
        version_id,
        "a" * 40,
        "b" * 64,
        session_factory=TestingSessionLocal,
        fresh_automatic=True,
    )

    assert result == {
        "status": "automatic_qualification_disabled",
        "qualification_control_revision": 1,
        "qualification_control_reason": "Bound Resident qualification canary",
    }
    promote.assert_not_awaited()
    reconcile.assert_not_awaited()
    trigger.assert_not_awaited()
    async with TestingSessionLocal() as verify:
        persisted = await verify.get(ReleaseVersion, version_id)
        assert persisted.status == "open"
        assert persisted.last_qualification["status"] == "promotion_failed"


@pytest.mark.asyncio
async def test_hold_allows_explicit_start_and_exact_continuation(
    db_session, monkeypatch
):
    project, version = await _project_version(db_session)
    project_id = project.id
    await _hold(db_session, project_id)
    _cheap_promotion_disabled(monkeypatch)

    mixed = await release_qualifier.run_qualification(
        db_session,
        project_id,
        version,
        explicit_request=True,
        auto_trigger=True,
    )
    assert mixed == {"status": "invalid_qualification_authority"}
    explicit = await release_qualifier.run_qualification(
        db_session,
        project_id,
        version,
        explicit_request=True,
    )
    assert explicit["status"] == "external_blocker"
    await db_session.rollback()
    await db_session.refresh(version)

    attempt_id = uuid.uuid4()
    version.active_qualification_attempt_id = attempt_id
    version.last_qualification = {"qualification_attempt_id": str(attempt_id)}
    db_session.add(version)
    await db_session.commit()
    continuation = await release_qualifier.run_qualification(
        db_session,
        project_id,
        version,
        qualification_attempt_id=attempt_id,
    )
    assert continuation["status"] == "external_blocker"


@pytest.mark.asyncio
@pytest.mark.parametrize("active_attempt", [None, uuid.uuid4()])
async def test_missing_or_mismatched_continuation_is_rejected_before_work(
    db_session, monkeypatch, active_attempt
):
    project, version = await _project_version(db_session)
    requested = uuid.uuid4()
    version.active_qualification_attempt_id = active_attempt
    version.last_qualification = (
        {"qualification_attempt_id": str(active_attempt)}
        if active_attempt is not None
        else None
    )
    await db_session.commit()

    async def downstream_bomb(*_args, **_kwargs):
        raise AssertionError("stale continuation reached downstream work")

    monkeypatch.setattr(
        release_qualifier, "_drop_incompatible_product_probes", downstream_bomb
    )
    result = await release_qualifier.run_qualification(
        db_session,
        project.id,
        version,
        qualification_attempt_id=requested,
    )
    assert result == {
        "status": "stale_qualification_attempt",
        "qualification_attempt_id": str(requested),
        "active_qualification_attempt_id": (
            str(active_attempt) if active_attempt is not None else None
        ),
    }
    malformed = await release_qualifier.run_qualification(
        db_session,
        project.id,
        version,
        qualification_attempt_id="not-a-uuid",
    )
    assert malformed == {"status": "invalid_qualification_attempt_id"}


@pytest.mark.asyncio
async def test_locked_recheck_rejects_continuation_changed_after_early_check(
    db_session, monkeypatch
):
    from conftest import TestingSessionLocal

    project, version = await _project_version(db_session)
    project_id = project.id
    version_id = version.id
    requested_attempt = uuid.uuid4()
    replacement_attempt = uuid.uuid4()
    version.active_qualification_attempt_id = requested_attempt
    version.last_qualification = {
        "status": "infra_failure",
        "qualification_attempt_id": str(requested_attempt),
    }
    await db_session.commit()

    early_checks = 0
    original_check = release_qualifier.qualification_continuation_block

    def observe_early_check(checked_version, checked_attempt):
        nonlocal early_checks
        early_checks += 1
        result = original_check(checked_version, checked_attempt)
        assert result is None
        return result

    concurrent = {
        "status": "infra_failure",
        "qualification_attempt_id": str(replacement_attempt),
        "race_marker": "concurrent-attempt-won",
    }

    async def replace_attempt(*_args, **_kwargs):
        assert early_checks == 1
        async with TestingSessionLocal() as other:
            competing = (
                await other.execute(
                    select(ReleaseVersion)
                    .where(ReleaseVersion.id == version_id)
                    .with_for_update()
                )
            ).scalar_one()
            competing.active_qualification_attempt_id = replacement_attempt
            competing.last_qualification = concurrent
            await other.commit()
        return set(), None

    monkeypatch.setattr(
        release_qualifier, "qualification_continuation_block", observe_early_check
    )
    monkeypatch.setattr(
        release_qualifier, "_drop_incompatible_product_probes", replace_attempt
    )
    monkeypatch.setattr(
        release_qualifier, "resolve_project_promotion_enabled", lambda _pid: False
    )
    result = await release_qualifier.run_qualification(
        db_session,
        project_id,
        version,
        qualification_attempt_id=requested_attempt,
    )
    assert early_checks == 1
    assert result == {
        "status": "stale_qualification_attempt",
        "qualification_attempt_id": str(requested_attempt),
        "active_qualification_attempt_id": str(replacement_attempt),
    }
    await db_session.rollback()
    async with TestingSessionLocal() as verify:
        persisted = await verify.get(ReleaseVersion, version_id)
        assert persisted.active_qualification_attempt_id == replacement_attempt
        assert persisted.last_qualification == concurrent
        assert persisted.qualification_attempts == 0


@pytest.mark.skipif(
    os.getenv("USE_TEST_POSTGRES", "").lower() != "true",
    reason="qualification-control row-lock race requires PostgreSQL",
)
@pytest.mark.asyncio
async def test_concurrent_revision_zero_writers_create_one_receipt(db_session):
    from conftest import TestingSessionLocal

    project = await _project(db_session, name="Qualification Control Race")
    project_id = project.id
    await db_session.commit()
    barrier = asyncio.Barrier(2)

    async def write(key: str):
        async with TestingSessionLocal() as session:
            await barrier.wait()
            try:
                result = await apply_qualification_control(
                    session,
                    project_id=project_id,
                    expected_revision=0,
                    automatic_starts_enabled=False,
                    idempotency_key=key,
                    actor_user_id=_OWNER_ID,
                    reason="Concurrent v57 hold",
                )
                await session.commit()
                return "receipt", key, result
            except QualificationControlConflict as exc:
                await session.rollback()
                return "conflict", key, str(exc)

    results = await asyncio.wait_for(
        asyncio.gather(write("race-hold-a"), write("race-hold-b")), timeout=30
    )
    assert sorted(result[0] for result in results) == ["conflict", "receipt"]
    winner = next(result for result in results if result[0] == "receipt")
    loser = next(result for result in results if result[0] == "conflict")
    assert winner[2]["idempotent_replay"] is False
    assert winner[2]["receipt"]["revision"] == 1
    assert "expected 0, current 1" in loser[2]

    async with TestingSessionLocal() as verify:
        rows = (
            await verify.execute(
                select(ReleaseQualificationControlRevision).where(
                    ReleaseQualificationControlRevision.project_id == project_id
                )
            )
        ).scalars().all()
        readback = await qualification_control_readback(verify, project_id)
    assert len(rows) == 1
    assert rows[0].idempotency_key == winner[1]
    assert readback["current"]["revision"] == 1
    assert readback["current"]["idempotency_key"] == winner[1]
