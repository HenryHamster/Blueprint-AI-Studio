"""Focused regressions for durable Resident qualification authority."""

import asyncio
import uuid
from datetime import timedelta

import pytest
from _release_qualifier_testhelpers import (
    _make_project_and_version,
    _shared_session_factory,
)
from sqlalchemy import update
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.models import (
    ReleaseQualificationIntent,
    ReleaseVersion,
    Task,
    TaskStatus,
)
from app.release_qualification_intents import (
    QUALIFICATION_INTENT_MAX_ATTEMPTS,
    QualificationIntentClaimLost,
    _commit_fenced_receipt,
    drain_qualification_intent,
    prepare_qualification_intent,
)
from app.time_utils import _utcnow


async def _source_task(db_session, project):
    task = Task(
        project_id=project.id,
        title="Resident qualification source",
        status=TaskStatus.IN_PROGRESS,
        created_by="STRATEGIC_AGENT",
        payloads=[{"type": "strategic_review"}],
    )
    db_session.add(task)
    await db_session.flush()
    return task


@pytest.mark.asyncio
async def test_prepare_qualification_intent_is_atomic_and_idempotent(db_session):
    project, version = await _make_project_and_version(
        db_session,
        phase="stabilize",
    )
    task = await _source_task(db_session, project)
    request = {"reason": "bounded canary is ready", "probes": []}

    first = await prepare_qualification_intent(
        db_session,
        request=request,
        task=task,
        project=project,
    )
    replay = await prepare_qualification_intent(
        db_session,
        request=request,
        task=task,
        project=project,
    )
    assert first is not None
    assert replay is not None
    assert replay.id == first.id
    assert first.release_version_id == version.id
    assert first.source_task_id == task.id
    assert first.status == "pending"
    assert first.authority_key.startswith("legacy-task-spec:")
    assert len(first.specification_hash) == 64
    await db_session.commit()

    rows = (
        (
            await db_session.execute(
                select(ReleaseQualificationIntent).where(
                    ReleaseQualificationIntent.project_id == project.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert [row.id for row in rows] == [first.id]


@pytest.mark.asyncio
async def test_workday_operation_authority_allows_new_ask_but_rejects_conflict(
    db_session,
):
    project, _version = await _make_project_and_version(
        db_session,
        phase="stabilize",
    )
    task = await _source_task(db_session, project)
    request = {"reason": "same bounded request", "probes": []}

    first = await prepare_qualification_intent(
        db_session,
        request=request,
        task=task,
        project=project,
        authority_key="workday:operation-1",
        authority_kind="managed_workday_operation",
    )
    replay = await prepare_qualification_intent(
        db_session,
        request=request,
        task=task,
        project=project,
        authority_key="workday:operation-1",
        authority_kind="managed_workday_operation",
    )
    later = await prepare_qualification_intent(
        db_session,
        request=request,
        task=task,
        project=project,
        authority_key="workday:operation-2",
        authority_kind="managed_workday_operation",
    )
    assert first is not None and replay is not None and later is not None
    assert replay.id == first.id
    assert later.id != first.id

    with pytest.raises(ValueError, match="different request"):
        await prepare_qualification_intent(
            db_session,
            request={"reason": "conflicting request", "probes": []},
            task=task,
            project=project,
            authority_key="workday:operation-1",
            authority_kind="managed_workday_operation",
        )


@pytest.mark.asyncio
async def test_workday_operation_cannot_rebind_after_version_cut(db_session):
    project, version = await _make_project_and_version(
        db_session,
        phase="stabilize",
    )
    task = await _source_task(db_session, project)
    request = {"reason": "exact v1 request", "probes": []}
    first = await prepare_qualification_intent(
        db_session,
        request=request,
        task=task,
        project=project,
        authority_key="workday:immutable-operation",
        authority_kind="managed_workday_operation",
    )
    assert first is not None
    version.status = "abandoned"
    successor = ReleaseVersion(
        project_id=project.id,
        version_number=2,
        phase="stabilize",
        phase_changed_at=_utcnow(),
        status="open",
        base_sha="b" * 40,
    )
    db_session.add(version)
    db_session.add(successor)
    await db_session.commit()

    with pytest.raises(ValueError, match="different request"):
        await prepare_qualification_intent(
            db_session,
            request=request,
            task=task,
            project=project,
            authority_key="workday:immutable-operation",
            authority_kind="managed_workday_operation",
        )


@pytest.mark.asyncio
async def test_paused_intent_stays_due_then_applies_once(db_session, mocker):
    project, version = await _make_project_and_version(
        db_session,
        phase="stabilize",
    )
    task = await _source_task(db_session, project)
    intent = await prepare_qualification_intent(
        db_session,
        request={"reason": "apply after resume", "probes": []},
        task=task,
        project=project,
    )
    assert intent is not None
    project.paused = True
    db_session.add(project)
    await db_session.commit()

    factory = _shared_session_factory(db_session)
    assert not await drain_qualification_intent(
        intent.id,
        session_factory=factory,
    )
    await db_session.refresh(intent)
    assert intent.status == "pending"
    assert intent.attempt_count == 0
    assert intent.next_attempt_at is not None

    project.paused = False
    db_session.add(project)
    intent.next_attempt_at = _utcnow() - timedelta(seconds=1)
    db_session.add(intent)
    await db_session.commit()

    async def _apply(
        request,
        source_task,
        source_project,
        session,
        *,
        path,
        target_version,
        expected_phase_changed_at,
        exact_authority,
        result_out,
    ):
        assert request == {"reason": "apply after resume", "probes": []}
        assert source_task.id == task.id
        assert source_project.id == project.id
        assert path == "durable_qualification_intent"
        assert target_version.id == version.id
        assert expected_phase_changed_at == version.phase_changed_at
        assert exact_authority is True
        version.last_qualification = {
            "status": "pending",
            "candidate_sha": "a" * 40,
            "qualification_attempt_id": "attempt-1",
        }
        flag_modified(version, "last_qualification")
        session.add(version)
        result_out.update(
            status="spawned",
            candidate_sha="a" * 40,
            qualification_attempt_id="attempt-1",
        )
        return False

    apply_mock = mocker.patch(
        "app.strategic_completion._apply_qualify_request",
        side_effect=_apply,
    )
    assert await drain_qualification_intent(
        intent.id,
        session_factory=factory,
    )
    await db_session.refresh(intent)
    assert intent.status == "applied"
    assert intent.attempt_count == 1
    assert intent.claim_id is None
    assert intent.result["status"] == "spawned"
    assert intent.result["delivery_state"] == "committed"
    assert intent.result["candidate_sha"] == "a" * 40
    assert intent.result["qualification_attempt_id"] == "attempt-1"
    assert intent.result["release_version_id"] == str(version.id)
    assert intent.result["specification_hash"] == intent.specification_hash
    assert intent.result["refresh_required"] is False
    assert apply_mock.await_count == 1
    assert not await drain_qualification_intent(
        intent.id,
        session_factory=factory,
    )
    assert apply_mock.await_count == 1


@pytest.mark.asyncio
async def test_phase_epoch_drift_supersedes_without_execution(db_session, mocker):
    project, version = await _make_project_and_version(
        db_session,
        phase="stabilize",
    )
    task = await _source_task(db_session, project)
    intent = await prepare_qualification_intent(
        db_session,
        request={"reason": "stale request", "probes": []},
        task=task,
        project=project,
    )
    assert intent is not None
    version.phase_changed_at = _utcnow() + timedelta(seconds=1)
    db_session.add(version)
    await db_session.commit()
    apply_mock = mocker.patch(
        "app.strategic_completion._apply_qualify_request",
        new_callable=mocker.AsyncMock,
    )

    assert not await drain_qualification_intent(
        intent.id,
        session_factory=_shared_session_factory(db_session),
    )
    await db_session.refresh(intent)
    assert intent.status == "superseded"
    assert intent.result["release_version_phase"] == "stabilize"
    assert apply_mock.await_count == 0


@pytest.mark.asyncio
async def test_committed_receipt_recovers_without_replaying_apply(db_session, mocker):
    project, version = await _make_project_and_version(
        db_session,
        phase="stabilize",
    )
    task = await _source_task(db_session, project)
    intent = await prepare_qualification_intent(
        db_session,
        request={"reason": "ambiguous commit", "probes": []},
        task=task,
        project=project,
        authority_key="workday:ambiguous-commit",
        authority_kind="managed_workday_operation",
    )
    assert intent is not None
    intent.status = "attempting"
    intent.attempt_count = 1
    intent.claim_id = uuid.uuid4()
    intent.lease_expires_at = _utcnow() - timedelta(seconds=1)
    intent.result = {
        "delivery_state": "committed",
        "status": "spawned",
        "release_version_id": str(version.id),
        "specification_hash": intent.specification_hash,
    }
    db_session.add(intent)
    await db_session.commit()
    apply_mock = mocker.patch(
        "app.strategic_completion._apply_qualify_request",
        new_callable=mocker.AsyncMock,
    )

    assert not await drain_qualification_intent(
        intent.id,
        session_factory=_shared_session_factory(db_session),
    )
    await db_session.refresh(intent)
    assert intent.status == "applied"
    assert intent.claim_id is None
    assert intent.result["status"] == "spawned"
    assert apply_mock.await_count == 0


@pytest.mark.asyncio
async def test_lost_claim_fences_dirty_release_mutation(db_session):
    project, version = await _make_project_and_version(
        db_session,
        phase="stabilize",
    )
    task = await _source_task(db_session, project)
    intent = await prepare_qualification_intent(
        db_session,
        request={"reason": "claim fence", "probes": []},
        task=task,
        project=project,
    )
    assert intent is not None
    actual_claim = uuid.uuid4()
    stale_claim = uuid.uuid4()
    intent.status = "attempting"
    intent.claim_id = actual_claim
    intent.lease_expires_at = _utcnow() + timedelta(minutes=5)
    intent.attempt_count = 1
    db_session.add(intent)
    await db_session.commit()

    version.last_qualification = {"status": "must_rollback"}
    flag_modified(version, "last_qualification")
    db_session.add(version)
    with pytest.raises(QualificationIntentClaimLost):
        await _commit_fenced_receipt(
            db_session,
            intent_id=intent.id,
            claim_id=stale_claim,
            receipt={"delivery_state": "committed", "status": "spawned"},
        )
    await db_session.rollback()
    await db_session.refresh(version)
    assert version.last_qualification is None


@pytest.mark.asyncio
async def test_unknown_committed_status_fails_closed_without_replay(
    db_session,
    mocker,
):
    project, version = await _make_project_and_version(
        db_session,
        phase="stabilize",
    )
    task = await _source_task(db_session, project)
    intent = await prepare_qualification_intent(
        db_session,
        request={"reason": "contract drift", "probes": []},
        task=task,
        project=project,
    )
    assert intent is not None
    intent.status = "attempting"
    intent.attempt_count = 1
    intent.claim_id = uuid.uuid4()
    intent.lease_expires_at = _utcnow() - timedelta(seconds=1)
    intent.result = {
        "delivery_state": "committed",
        "status": "new_unclassified_outcome",
    }
    db_session.add(intent)
    await db_session.commit()
    apply_mock = mocker.patch(
        "app.strategic_completion._apply_qualify_request",
        new_callable=mocker.AsyncMock,
    )

    assert not await drain_qualification_intent(
        intent.id,
        session_factory=_shared_session_factory(db_session),
    )
    await db_session.refresh(intent)
    assert intent.status == "failed"
    assert intent.last_error == (
        "unexpected committed qualification status: new_unclassified_outcome"
    )
    apply_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_claim_at_retry_cap_terminalizes_without_replay(
    db_session,
    mocker,
):
    project, _version = await _make_project_and_version(
        db_session,
        phase="stabilize",
    )
    task = await _source_task(db_session, project)
    intent = await prepare_qualification_intent(
        db_session,
        request={"reason": "crash loop", "probes": []},
        task=task,
        project=project,
    )
    assert intent is not None
    intent.status = "attempting"
    intent.attempt_count = QUALIFICATION_INTENT_MAX_ATTEMPTS
    intent.claim_id = uuid.uuid4()
    intent.lease_expires_at = _utcnow() - timedelta(seconds=1)
    db_session.add(intent)
    await db_session.commit()
    apply_mock = mocker.patch(
        "app.strategic_completion._apply_qualify_request",
        new_callable=mocker.AsyncMock,
    )

    assert not await drain_qualification_intent(
        intent.id,
        session_factory=_shared_session_factory(db_session),
    )
    await db_session.refresh(intent)
    assert intent.status == "failed"
    assert intent.attempt_count == QUALIFICATION_INTENT_MAX_ATTEMPTS
    assert intent.claim_id is None
    assert "retry cap" in intent.last_error
    apply_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_committed_refresh_receipt_arms_wake_atomically_once(
    db_session,
    mocker,
):
    project, _version = await _make_project_and_version(
        db_session,
        phase="stabilize",
    )
    task = await _source_task(db_session, project)
    intent = await prepare_qualification_intent(
        db_session,
        request={"reason": "actionable block", "probes": []},
        task=task,
        project=project,
    )
    assert intent is not None
    intent.status = "attempting"
    intent.attempt_count = 1
    intent.claim_id = uuid.uuid4()
    intent.lease_expires_at = _utcnow() - timedelta(seconds=1)
    intent.result = {
        "delivery_state": "committed",
        "status": "ci_compliance_blocked",
        "refresh_required": True,
    }
    db_session.add(intent)
    await db_session.commit()
    apply_mock = mocker.patch(
        "app.strategic_completion._apply_qualify_request",
        new_callable=mocker.AsyncMock,
    )

    assert not await drain_qualification_intent(
        intent.id,
        session_factory=_shared_session_factory(db_session),
    )
    await db_session.refresh(intent)
    await db_session.refresh(project)
    assert intent.status == "applied"
    assert intent.result["refresh_delivery_state"] == "delivered"
    marker = project.vision["_strategic_refresh"]
    assert marker["origin"] == "qualification"
    assert marker["count"] == 1
    completed_at = intent.completed_at
    settled_result = dict(intent.result)
    assert not await drain_qualification_intent(
        intent.id,
        session_factory=_shared_session_factory(db_session),
    )
    await db_session.refresh(project)
    await db_session.refresh(intent)
    assert project.vision["_strategic_refresh"]["count"] == 1
    assert intent.completed_at == completed_at
    assert intent.result == settled_result
    apply_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_null_phase_epoch_remains_exact_authority(db_session):
    from app.release_qualifier import _lock_exact_qualification_authority

    project, version = await _make_project_and_version(
        db_session,
        phase="stabilize",
    )
    version.phase_changed_at = None
    db_session.add(version)
    await db_session.commit()
    project_id = project.id
    version_id = version.id
    version_number = version.version_number

    locked, block = await _lock_exact_qualification_authority(
        db_session,
        project_id=project_id,
        version_id=version_id,
        version_number=version_number,
        expected_phase_changed_at=None,
    )
    assert locked is not None
    assert block is None
    await db_session.rollback()

    version.phase_changed_at = _utcnow()
    db_session.add(version)
    await db_session.commit()
    locked, block = await _lock_exact_qualification_authority(
        db_session,
        project_id=project_id,
        version_id=version_id,
        version_number=version_number,
        expected_phase_changed_at=None,
    )
    assert locked is None
    assert block == {"status": "superseded_phase_epoch"}


@pytest.mark.asyncio
async def test_exact_null_epoch_fences_no_credentials_write_after_drift(
    db_session,
    mocker,
):
    from app import release_qualifier as qualifier

    project, version = await _make_project_and_version(
        db_session,
        phase="stabilize",
    )
    version.phase_changed_at = None
    db_session.add(version)
    await db_session.commit()

    async def _drift_before_no_credentials(session, _project):
        await session.execute(
            update(ReleaseVersion)
            .where(ReleaseVersion.id == version.id)
            .values(phase_changed_at=_utcnow())
        )
        return None

    mocker.patch.object(
        qualifier,
        "resolve_project_promotion_enabled",
        return_value=True,
    )
    mocker.patch.object(
        qualifier,
        "_resolve_github_with_session",
        side_effect=_drift_before_no_credentials,
    )
    result = await qualifier.run_qualification(
        db_session,
        project.id,
        version,
        expected_phase_changed_at=None,
        exact_authority=True,
        explicit_request=True,
    )
    assert result == {"status": "superseded_phase_epoch"}
    await db_session.refresh(version)
    assert version.last_qualification is None
    assert version.qualification_attempts == 0


@pytest.mark.asyncio
async def test_exact_null_epoch_fences_sha_error_write_after_drift(
    db_session,
    mocker,
):
    from app import release_qualifier as qualifier

    project, version = await _make_project_and_version(
        db_session,
        phase="stabilize",
    )
    version.phase_changed_at = None
    db_session.add(version)
    await db_session.commit()

    async def _drift_then_raise(*_args, **_kwargs):
        await db_session.execute(
            update(ReleaseVersion)
            .where(ReleaseVersion.id == version.id)
            .values(phase_changed_at=_utcnow())
        )
        raise RuntimeError("candidate ref unavailable")

    mocker.patch.object(
        qualifier,
        "resolve_project_promotion_enabled",
        return_value=True,
    )
    mocker.patch.object(
        qualifier,
        "_resolve_github_with_session",
        return_value=("token", "owner", "repo"),
    )
    mocker.patch.object(
        qualifier.github,
        "get_branch_sha",
        side_effect=_drift_then_raise,
    )
    result = await qualifier.run_qualification(
        db_session,
        project.id,
        version,
        expected_phase_changed_at=None,
        exact_authority=True,
        explicit_request=True,
    )
    assert result == {"status": "superseded_phase_epoch"}
    await db_session.refresh(version)
    assert version.last_qualification is None
    assert version.qualification_attempts == 0


@pytest.mark.asyncio
async def test_execution_timeout_rolls_back_and_releases_claim(
    db_session,
    mocker,
):
    project, version = await _make_project_and_version(
        db_session,
        phase="stabilize",
    )
    task = await _source_task(db_session, project)
    intent = await prepare_qualification_intent(
        db_session,
        request={"reason": "bounded timeout", "probes": []},
        task=task,
        project=project,
    )
    assert intent is not None
    await db_session.commit()

    async def _hung_apply(
        request,
        source_task,
        source_project,
        session,
        **kwargs,
    ):
        version.last_qualification = {"status": "must_rollback"}
        flag_modified(version, "last_qualification")
        session.add(version)
        await asyncio.sleep(60)

    mocker.patch(
        "app.strategic_completion._apply_qualify_request",
        side_effect=_hung_apply,
    )
    mocker.patch(
        "app.release_qualification_intents.QUALIFICATION_INTENT_EXECUTION_TIMEOUT_SECONDS",
        0.01,
    )

    assert not await drain_qualification_intent(
        intent.id,
        session_factory=_shared_session_factory(db_session),
    )
    await db_session.refresh(intent)
    await db_session.refresh(version)
    assert intent.status == "pending"
    assert intent.attempt_count == 1
    assert intent.claim_id is None
    assert intent.last_error.startswith("TimeoutError:")
    assert version.last_qualification is None
