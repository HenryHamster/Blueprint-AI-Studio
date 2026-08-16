"""Postgres regressions for pause-safe qualification finalization."""

from __future__ import annotations

import os
import uuid
from datetime import timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm.attributes import flag_modified

from _release_qualifier_testhelpers import *  # noqa: F403
from app import release_qualifier as rq
from app import release_qualifier_sweep as rqs
from app import shared_state
from app.models import Alert, DeploymentState, DeploymentStatus, Task, TaskStatus
from app.release_qualification_deferral import (
    QUALIFICATION_FINALIZATION_DEFERRED,
)
from app.release_task_admission import ReleaseTaskAdmissionClosed
from app.time_utils import _utcnow


requires_postgres = pytest.mark.skipif(
    not (
        os.getenv("USE_TEST_POSTGRES", "").lower() == "true"
        and os.getenv("DATABASE_URL", "").startswith("postgresql")
    ),
    reason="qualification admission lock regression requires PostgreSQL",
)


async def _seed_deferred_challenge_candidate(db_session):
    project, version = await _make_project_and_version(db_session)  # noqa: F405
    audit_task, _ = await _seed_qualification_children(  # noqa: F405
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 0.8,
            "findings": [
                {
                    "severity": "high",
                    "summary": "Paused finalization must not lose this finding.",
                    "file": "backend/app/release_qualifier.py",
                    "verification_note": "Confirmed at the continuation-spawn fence.",
                }
            ],
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 1,
                "test_count_delta": 1,
            },
        },
        smoke_logs={"passed": True},
    )
    version.last_qualification = {
        **version.last_qualification,
        "started_at": _utcnow().isoformat(),
        "challenge_task_id": None,
        "audit_challenge_task_id": None,
        "audit_challenge_spawn_pending": True,
    }
    flag_modified(version, "last_qualification")
    db_session.add(version)
    await db_session.commit()
    return project, version, audit_task


@requires_postgres
@pytest.mark.asyncio
async def test_repeated_paused_sweeps_hold_then_resume_one_challenge(
    db_session, mocker
):
    mocker.patch.dict(shared_state.runtime_config, {"promotion_enabled": False})
    project, version, _ = await _seed_deferred_challenge_candidate(db_session)
    project.paused = True
    db_session.add(project)
    await db_session.commit()
    attempt_count = version.qualification_attempts
    task_count = await db_session.scalar(select(func.count()).select_from(Task))
    ci_gate = mocker.spy(rq, "_resolve_ci_at_finalize")
    identity_gate = mocker.spy(rq, "_resolve_migration_identity_at_finalize")
    apply_gate = mocker.spy(rq, "_resolve_migration_apply_at_finalize")
    authorship_gate = mocker.spy(rq, "_measure_candidate_operator_authorship")
    challenge_spawn = mocker.spy(rq, "_spawn_release_audit_challenge_task")
    timeout_force = mocker.spy(rq, "_force_finalize_timed_out_qualification")
    crash_force = mocker.spy(rqs, "_force_finalize_sweep_crash_loop")

    first_deferred_at = None
    for _ in range(3):
        finalized = await rq.sweep_stuck_qualifications(
            db_session,
            session_factory=_shared_session_factory(db_session),  # noqa: F405
        )
        assert finalized == 0
        await db_session.refresh(version)
        qualification = version.last_qualification
        assert qualification["status"] == "pending"
        assert qualification["pending_outcome"] == QUALIFICATION_FINALIZATION_DEFERRED
        assert qualification["finalization_deferred_reason"] == "project_paused"
        assert "sweep_crash_streak" not in qualification
        first_deferred_at = (
            first_deferred_at or qualification["finalization_deferred_at"]
        )
        assert qualification["finalization_deferred_at"] == first_deferred_at

    assert version.qualification_attempts == attempt_count
    assert await db_session.scalar(select(func.count()).select_from(Task)) == task_count
    ci_gate.assert_not_awaited()
    identity_gate.assert_not_awaited()
    apply_gate.assert_not_awaited()
    authorship_gate.assert_not_awaited()
    challenge_spawn.assert_not_awaited()
    timeout_force.assert_not_awaited()
    crash_force.assert_not_awaited()
    crash_alerts = await db_session.scalar(
        select(func.count())
        .select_from(Alert)
        .where(Alert.event_type == "release.qualification_crash_loop")
    )
    assert crash_alerts == 0

    await db_session.refresh(project)
    project.paused = False
    db_session.add(project)
    await db_session.commit()
    await rq.sweep_stuck_qualifications(
        db_session,
        session_factory=_shared_session_factory(db_session),  # noqa: F405
    )
    await db_session.refresh(version)
    qualification = version.last_qualification
    assert qualification["status"] == "pending"
    assert "pending_outcome" not in qualification
    assert "finalization_deferred_reason" not in qualification
    challenge_id = qualification["challenge_task_id"]
    assert challenge_id
    challenge = await db_session.get(Task, uuid.UUID(challenge_id))
    assert challenge is not None
    assert challenge_spawn.await_count == 1
    assert challenge.first_payload["primary_findings"][0]["summary"] == (
        "Paused finalization must not lose this finding."
    )
    assert (
        await db_session.scalar(select(func.count()).select_from(Task))
        == task_count + 1
    )

    await rq.sweep_stuck_qualifications(
        db_session,
        session_factory=_shared_session_factory(db_session),  # noqa: F405
    )
    assert challenge_spawn.await_count == 1
    assert (
        await db_session.scalar(select(func.count()).select_from(Task))
        == task_count + 1
    )


@requires_postgres
@pytest.mark.asyncio
async def test_long_paused_hold_banks_timeout_until_nonterminal_child_resumes(
    db_session, mocker
):
    mocker.patch.dict(shared_state.runtime_config, {"promotion_enabled": False})
    project, version = await _make_project_and_version(db_session)  # noqa: F405
    audit_task, _ = await _seed_qualification_children(  # noqa: F405
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 1.0,
            "findings": [],
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 1,
                "test_count_delta": 1,
            },
        },
        smoke_logs={"passed": True},
    )
    audit_task.status = TaskStatus.IN_PROGRESS
    project.paused = True
    db_session.add(audit_task)
    db_session.add(project)
    await db_session.commit()

    await rq.sweep_stuck_qualifications(
        db_session,
        session_factory=_shared_session_factory(db_session),  # noqa: F405
    )
    await db_session.refresh(version)
    now = _utcnow()
    started_at = now - timedelta(seconds=rq._MAX_QUALIFICATION_DURATION_SECONDS + 180)
    deferred_at = started_at + timedelta(seconds=60)
    version.last_qualification = {
        **version.last_qualification,
        "started_at": started_at.isoformat(),
        # Historical writers emitted both naive and aware UTC ISO strings.
        "finalization_deferred_at": deferred_at.replace(
            tzinfo=timezone.utc
        ).isoformat(),
    }
    flag_modified(version, "last_qualification")
    await db_session.refresh(project)
    project.paused = False
    db_session.add(version)
    db_session.add(project)
    await db_session.commit()

    finalized = await rq.sweep_stuck_qualifications(
        db_session,
        session_factory=_shared_session_factory(db_session),  # noqa: F405
    )
    assert finalized == 0
    await db_session.refresh(version)
    qualification = version.last_qualification
    assert qualification["status"] == "pending"
    assert qualification.get("blocker") != "qualification_timeout"
    assert "pending_outcome" not in qualification
    assert qualification["finalization_deferred_seconds"] > (
        rq._MAX_QUALIFICATION_DURATION_SECONDS
    )

    await db_session.refresh(audit_task)
    audit_task.status = TaskStatus.COMPLETED
    db_session.add(audit_task)
    await db_session.commit()
    await rq.sweep_stuck_qualifications(
        db_session,
        session_factory=_shared_session_factory(db_session),  # noqa: F405
    )
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "dry_run"
    assert version.last_qualification.get("blocker") != "qualification_timeout"


@requires_postgres
@pytest.mark.asyncio
async def test_global_and_project_pause_windows_are_union_accounted(db_session, mocker):
    """Overlapping global pause time is excluded once, not by both clocks."""

    mocker.patch.dict(shared_state.runtime_config, {"promotion_enabled": False})
    mocker.patch.object(
        rq,
        "_maybe_trigger_strategic_after_qualification",
        new=AsyncMock(return_value=None),
    )
    mocker.patch.object(
        rqs,
        "_redrive_infra_failure_qualification",
        new=AsyncMock(return_value=False),
    )
    mocker.patch.object(rqs, "fire_and_forget_vm_delete")
    project, version = await _make_project_and_version(db_session)  # noqa: F405
    audit_task, _ = await _seed_qualification_children(  # noqa: F405
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 1.0,
            "findings": [],
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 1,
                "test_count_delta": 1,
            },
        },
        smoke_logs={"passed": True},
    )
    audit_task.status = TaskStatus.IN_PROGRESS
    prior_global_pause = 100.0
    version.last_qualification = {
        **version.last_qualification,
        "pause_baseline": prior_global_pause,
    }
    flag_modified(version, "last_qualification")
    project.paused = True
    deploy_state = DeploymentState(
        id=1,
        status=DeploymentStatus.STABLE,
        total_manual_paused_seconds=prior_global_pause,
        total_auto_paused_seconds=0.0,
    )
    db_session.add_all([audit_task, version, project, deploy_state])
    await db_session.commit()

    await rq.sweep_stuck_qualifications(
        db_session,
        session_factory=_shared_session_factory(db_session),  # noqa: F405
    )
    await db_session.refresh(version)
    assert version.last_qualification[
        "finalization_deferred_pause_baseline"
    ] == pytest.approx(prior_global_pause)

    first_wall = 8 * 60 * 60
    first_global_overlap = 6 * 60 * 60
    now = _utcnow()
    version.last_qualification = {
        **version.last_qualification,
        "started_at": (now - timedelta(seconds=first_wall + 3600)).isoformat(),
        "finalization_deferred_at": (now - timedelta(seconds=first_wall))
        .replace(tzinfo=timezone.utc)
        .isoformat(),
    }
    flag_modified(version, "last_qualification")
    await db_session.refresh(project)
    project.paused = False
    await db_session.refresh(deploy_state)
    deploy_state.total_manual_paused_seconds = prior_global_pause + first_global_overlap
    db_session.add_all([version, project, deploy_state])
    await db_session.commit()

    await rq.sweep_stuck_qualifications(
        db_session,
        session_factory=_shared_session_factory(db_session),  # noqa: F405
    )
    await db_session.refresh(version)
    first_bank = version.last_qualification["finalization_deferred_seconds"]
    assert first_bank == pytest.approx(first_wall - first_global_overlap, abs=5)
    assert version.last_qualification["status"] == "pending"
    assert "pending_outcome" not in version.last_qualification

    await db_session.refresh(project)
    project.paused = True
    db_session.add(project)
    await db_session.commit()
    await rq.sweep_stuck_qualifications(
        db_session,
        session_factory=_shared_session_factory(db_session),  # noqa: F405
    )
    await db_session.refresh(version)
    assert version.last_qualification[
        "finalization_deferred_pause_baseline"
    ] == pytest.approx(prior_global_pause + first_global_overlap)

    second_wall = 3 * 60 * 60
    second_global_overlap = 60 * 60
    now = _utcnow()
    version.last_qualification = {
        **version.last_qualification,
        "started_at": (
            now - timedelta(seconds=first_wall + second_wall + 3600)
        ).isoformat(),
        "finalization_deferred_at": (now - timedelta(seconds=second_wall)).isoformat(),
    }
    flag_modified(version, "last_qualification")
    await db_session.refresh(project)
    project.paused = False
    await db_session.refresh(deploy_state)
    total_global_overlap = first_global_overlap + second_global_overlap
    deploy_state.total_manual_paused_seconds = prior_global_pause + total_global_overlap
    db_session.add_all([version, project, deploy_state])
    await db_session.commit()

    await rq.sweep_stuck_qualifications(
        db_session,
        session_factory=_shared_session_factory(db_session),  # noqa: F405
    )
    await db_session.refresh(version)
    total_bank = version.last_qualification["finalization_deferred_seconds"]
    expected_bank = (first_wall - first_global_overlap) + (
        second_wall - second_global_overlap
    )
    assert total_bank == pytest.approx(expected_bank, abs=10)
    assert version.last_qualification["status"] == "pending"

    # Mutation-proof the union: exactly 60 seconds of active time beyond the
    # limit must still time out. Double-subtracting the overlap would spare it.
    now = _utcnow()
    version.last_qualification = {
        **version.last_qualification,
        "started_at": (
            now
            - timedelta(
                seconds=(
                    rq._MAX_QUALIFICATION_DURATION_SECONDS
                    + total_global_overlap
                    + total_bank
                    + 60
                )
            )
        ).isoformat(),
    }
    flag_modified(version, "last_qualification")
    db_session.add(version)
    await db_session.commit()
    finalized = await rq.sweep_stuck_qualifications(
        db_session,
        session_factory=_shared_session_factory(db_session),  # noqa: F405
    )
    assert finalized == 1
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "infra_failure"
    assert version.last_qualification["blocker"] == "qualification_timeout"


@requires_postgres
@pytest.mark.asyncio
async def test_typed_spawn_denial_rolls_back_and_becomes_pending_deferral(
    db_session, mocker
):
    project, version, _ = await _seed_deferred_challenge_candidate(db_session)
    denial = mocker.patch.object(
        rq,
        "_spawn_release_audit_challenge_task",
        side_effect=ReleaseTaskAdmissionClosed(project.id, "project_paused"),
    )

    result = await rq._finalize_in_dedicated_session(
        version.id,
        session_factory=_shared_session_factory(db_session),  # noqa: F405
    )
    assert result == {
        "status": QUALIFICATION_FINALIZATION_DEFERRED,
        "admission_reason": "project_paused",
    }
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "pending"
    assert version.last_qualification["pending_outcome"] == (
        QUALIFICATION_FINALIZATION_DEFERRED
    )
    assert "sweep_crash_streak" not in version.last_qualification

    mocker.stop(denial)
    resumed = await rq._finalize_in_dedicated_session(
        version.id,
        session_factory=_shared_session_factory(db_session),  # noqa: F405
    )
    assert resumed["status"] == "audit_challenge_respawned"
    await db_session.refresh(version)
    assert version.last_qualification["challenge_task_id"]
    assert "pending_outcome" not in version.last_qualification


@requires_postgres
@pytest.mark.asyncio
async def test_project_lock_contention_never_spends_finalizer_crash_budget(
    db_session, mocker
):
    """Both authority seams fail closed immediately while Project is held."""

    project, version = await _make_project_and_version(db_session)  # noqa: F405
    attempt_id = uuid.uuid4()
    version.active_qualification_attempt_id = attempt_id
    version.last_qualification = {
        "status": "pending",
        "qualification_attempt_id": str(attempt_id),
        "candidate_sha": "c" * 40,
        "started_at": _utcnow().isoformat(),
    }
    db_session.add(version)
    await db_session.commit()
    project_id = project.id
    version_id = version.id

    Session = sessionmaker(
        bind=db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with Session() as lock_session:
        locked_project = (
            await lock_session.execute(
                select(Project)
                .where(Project.id == project_id)
                .with_for_update(key_share=True)
            )
        ).scalar_one()

        # The normal finalization seam sees SKIP LOCKED as unavailable and
        # leaves the exact pending attempt for a later sweep. It must not reach
        # the generic sweep exception path that increments crash streak.
        finalized = await rq.sweep_stuck_qualifications(
            db_session,
            session_factory=Session,
        )
        assert finalized == 0

        # The typed child-spawn denial handler rolls back and re-enters the same
        # Project -> Version order. If Project is acquired by another writer in
        # that gap, persistence is likewise a clean no-op, never an escaping
        # lock timeout that the sweep would classify as a finalizer crash.
        forced_denial = mocker.patch.object(
            rq,
            "_finalize_locked_version",
            new=AsyncMock(
                side_effect=ReleaseTaskAdmissionClosed(
                    project_id,
                    "project_unavailable",
                )
            ),
        )
        typed_result = await rq._finalize_in_dedicated_session(
            version_id,
            session_factory=Session,
        )
        assert typed_result is None
        mocker.stop(forced_denial)
        # Keep the ORM reference live so the row lock is unambiguously retained
        # through both calls; release happens at context exit.
        assert locked_project.id == project_id

    db_session.expire_all()
    refreshed = await db_session.get(rv.ReleaseVersion, version_id)
    assert refreshed is not None
    assert refreshed.last_qualification["status"] == "pending"
    assert "sweep_crash_streak" not in refreshed.last_qualification
    crash_alerts = await db_session.scalar(
        select(func.count())
        .select_from(Alert)
        .where(Alert.event_type == "release.qualification_crash_loop")
    )
    assert crash_alerts == 0


@requires_postgres
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rotate_attempt", "rotate_candidate"),
    [(True, False), (False, True)],
)
async def test_pre_gate_snapshot_cannot_finalize_rotated_snapshot(
    db_session, mocker, rotate_attempt, rotate_candidate
):
    """A sweep cannot apply old gate facts after the pending owner rotates."""

    project, version = await _make_project_and_version(db_session)
    project_id = project.id
    version_id = version.id
    first_attempt = uuid.uuid4()
    next_attempt = uuid.uuid4() if rotate_attempt else first_attempt
    first_candidate = "a" * 40
    next_candidate = "b" * 40 if rotate_candidate else first_candidate
    version.active_qualification_attempt_id = first_attempt
    version.last_qualification = {
        "status": "pending",
        "qualification_attempt_id": str(first_attempt),
        "candidate_sha": first_candidate,
        "started_at": _utcnow().isoformat(),
    }
    db_session.add(version)
    await db_session.commit()

    Session = sessionmaker(
        bind=db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async def _rotate_during_first_gate(*_args, **_kwargs):
        async with Session() as rotation_session:
            locked_project = (
                await rotation_session.execute(
                    select(Project)
                    .where(Project.id == project_id)
                    .with_for_update(key_share=True)
                )
            ).scalar_one()
            locked_version = (
                await rotation_session.execute(
                    select(rv.ReleaseVersion)
                    .where(rv.ReleaseVersion.id == version_id)
                    .with_for_update()
                )
            ).scalar_one()
            locked_version.active_qualification_attempt_id = next_attempt
            locked_version.last_qualification = {
                **locked_version.last_qualification,
                "qualification_attempt_id": str(next_attempt),
                "candidate_sha": next_candidate,
            }
            flag_modified(locked_version, "last_qualification")
            rotation_session.add_all([locked_project, locked_version])
            await rotation_session.commit()
        return True, {"state": "green"}

    mocker.patch.object(
        rq,
        "_required_children_terminal",
        new=AsyncMock(return_value=True),
    )
    mocker.patch.object(
        rq,
        "_resolve_ci_at_finalize",
        side_effect=_rotate_during_first_gate,
    )
    mocker.patch.object(
        rq,
        "_resolve_migration_identity_at_finalize",
        new=AsyncMock(return_value=("pass", {})),
    )
    mocker.patch.object(
        rq,
        "_resolve_migration_apply_at_finalize",
        new=AsyncMock(return_value=("pass", {})),
    )
    mocker.patch.object(
        rq,
        "_measure_candidate_operator_authorship",
        new=AsyncMock(return_value={}),
    )
    finalizer = mocker.patch.object(
        rq,
        "_finalize_qualification",
        new=AsyncMock(return_value={"status": "dry_run"}),
    )

    result = await rq._finalize_in_dedicated_session(
        version_id,
        session_factory=Session,
    )

    assert result is None
    finalizer.assert_not_awaited()
    db_session.expire_all()
    refreshed = await db_session.get(rv.ReleaseVersion, version_id)
    assert refreshed.active_qualification_attempt_id == next_attempt
    assert refreshed.last_qualification["qualification_attempt_id"] == str(
        next_attempt
    )
    assert refreshed.last_qualification["candidate_sha"] == next_candidate
