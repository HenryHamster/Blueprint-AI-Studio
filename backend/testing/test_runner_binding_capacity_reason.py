"""The local-execution hold must name the condition that actually bound.

Regression cover for a live platform defect: a freshly onboarded project is on
ZERO runner allow-lists, so its first task can never be pinned to local
hardware. With managed cloud execution disabled the task held QUEUED forever
behind the message "Waiting for local capacity: managed cloud execution is
disabled" -- which points the reader at a busy fleet while the fleet was in
fact idle and the real blocker was a missing ``runner_project_binding`` grant.

Per the repo's standing "surface reasons, not states" rule, these tests pin the
two cases apart:

* zero runners bound to the project  -> the message must name the binding gap
  AND the grant endpoint that clears it;
* runners bound but unavailable      -> the historic capacity wording stands.
"""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import compute_jobs
from app.database import resolve_async_engine
from app.models import (
    ComputeJob,
    ComputeJobStatus,
    LocalRunner,
    Project,
    RunnerProjectBinding,
    Task,
    TaskStatus,
    _utcnow,
)
from app.runner_project_binding import (
    LOCAL_CAPACITY_WAIT_MESSAGE,
    count_runners_allowed_for_project,
    local_capacity_wait_message,
    resolve_local_capacity_hold,
    runner_binding_wait_snapshot,
)

TEST_USER_ID = "test-user-binding-reason"


async def _make_project(session, name="Binding Reason Project") -> Project:
    project = Project(name=name, user_id=TEST_USER_ID)
    session.add(project)
    await session.commit()
    await session.refresh(project)
    return project


async def _make_runner(session, project_id, *, status: str = "active") -> LocalRunner:
    runner = LocalRunner(
        project_id=project_id,
        token_hash=uuid.uuid4().hex,
        status=status,
        last_seen_at=_utcnow(),
        capabilities={"task_types": "all", "max_concurrent": 2},
    )
    session.add(runner)
    await session.commit()
    await session.refresh(runner)
    return runner


class TestLocalCapacityWaitMessage:
    """The pure decision: which message does a given bound-runner count get?"""

    def test_zero_bound_runners_names_the_binding_gap(self):
        message = local_capacity_wait_message(0)
        assert "no local runner is bound to this project" in message
        # The remedy must be in the message, not folklore.
        assert (
            "POST /api/admin/projects/{enrollment_project_id}/runners/"
            "{runner_id}/projects" in message
        )
        assert message != LOCAL_CAPACITY_WAIT_MESSAGE

    def test_bound_fleet_keeps_capacity_wording(self):
        assert local_capacity_wait_message(2) == LOCAL_CAPACITY_WAIT_MESSAGE
        assert (
            local_capacity_wait_message(2)
            == "Waiting for local capacity: managed cloud execution is disabled"
        )

    def test_unknown_count_fails_safe_to_capacity_wording(self):
        """A failed count must not accuse the operator of a missing grant."""
        assert local_capacity_wait_message(None) == LOCAL_CAPACITY_WAIT_MESSAGE
        assert runner_binding_wait_snapshot(None)["reason"] == "runner_binding_unknown"

    def test_snapshot_reasons_are_distinct(self):
        assert runner_binding_wait_snapshot(0)["reason"] == "no_runner_bound_to_project"
        assert runner_binding_wait_snapshot(0)["remedy"]
        assert runner_binding_wait_snapshot(3)["reason"] == "local_capacity_unavailable"
        assert runner_binding_wait_snapshot(3)["remedy"] is None


class TestBoundRunnerCount:
    @pytest.mark.asyncio
    async def test_project_with_no_runners_counts_zero(self, db_session):
        project = await _make_project(db_session)
        assert await count_runners_allowed_for_project(db_session, project.id) == 0

        message, snapshot = await resolve_local_capacity_hold(db_session, project.id)
        assert "no local runner is bound to this project" in message
        assert snapshot["reason"] == "no_runner_bound_to_project"
        assert snapshot["bound_runner_count"] == 0

    @pytest.mark.asyncio
    async def test_enrolled_runner_makes_the_project_bound(self, db_session):
        project = await _make_project(db_session)
        await _make_runner(db_session, project.id)

        assert await count_runners_allowed_for_project(db_session, project.id) == 1
        message, snapshot = await resolve_local_capacity_hold(db_session, project.id)
        assert message == LOCAL_CAPACITY_WAIT_MESSAGE
        assert snapshot["reason"] == "local_capacity_unavailable"

    @pytest.mark.asyncio
    async def test_explicit_allow_list_grant_makes_the_project_bound(self, db_session):
        """A runner enrolled ELSEWHERE but granted here is a bound runner."""
        enrollment = await _make_project(db_session, name="Enrollment project")
        target = await _make_project(db_session, name="Granted project")
        runner = await _make_runner(db_session, enrollment.id)

        assert await count_runners_allowed_for_project(db_session, target.id) == 0
        db_session.add(RunnerProjectBinding(runner_id=runner.id, project_id=target.id))
        await db_session.commit()

        assert await count_runners_allowed_for_project(db_session, target.id) == 1
        message, _snapshot = await resolve_local_capacity_hold(db_session, target.id)
        assert message == LOCAL_CAPACITY_WAIT_MESSAGE

    @pytest.mark.asyncio
    async def test_deleted_runner_is_not_a_bound_runner(self, db_session):
        project = await _make_project(db_session)
        await _make_runner(db_session, project.id, status="deleted")

        assert await count_runners_allowed_for_project(db_session, project.id) == 0
        message, _snapshot = await resolve_local_capacity_hold(db_session, project.id)
        assert "no local runner is bound to this project" in message


async def _make_prelaunch_job(db_session, project) -> ComputeJob:
    task = Task(project_id=project.id, status=TaskStatus.QUEUED)
    db_session.add(task)
    await db_session.flush()
    job = ComputeJob(
        project_id=project.id,
        task_id=task.id,
        backend="gcp",
        status=ComputeJobStatus.STARTING,
        started_at=_utcnow(),
    )
    db_session.add(job)
    await db_session.commit()
    return job


class TestComputeJobManagedHoldMessage:
    """End-to-end through a real hold write, not just the message helper."""

    @pytest.mark.asyncio
    async def test_hold_names_the_missing_binding_when_no_runner_is_bound(
        self, db_session
    ):
        project = await _make_project(db_session, name="Unbound compute project")
        job = await _make_prelaunch_job(db_session, project)
        session_factory = async_sessionmaker(
            resolve_async_engine(db_session),
            expire_on_commit=False,
            class_=AsyncSession,
        )

        await compute_jobs._hold_compute_job_for_managed_compute(
            session_factory, job.id, now=_utcnow(), local_only=False
        )

        await db_session.refresh(job)
        meta = job.polling_metadata or {}
        assert job.status == ComputeJobStatus.QUEUED
        assert "no local runner is bound to this project" in meta["status_message"]
        assert meta["runner_binding_wait"]["reason"] == "no_runner_bound_to_project"
        assert meta["runner_binding_wait"]["bound_runner_count"] == 0
        # The managed-compute audit snapshot is preserved, not replaced.
        assert meta["managed_compute"]["backend"] == "gcp_compute_job"

    @pytest.mark.asyncio
    async def test_hold_keeps_capacity_wording_when_a_runner_is_bound(self, db_session):
        project = await _make_project(db_session, name="Bound compute project")
        await _make_runner(db_session, project.id)
        job = await _make_prelaunch_job(db_session, project)
        session_factory = async_sessionmaker(
            resolve_async_engine(db_session),
            expire_on_commit=False,
            class_=AsyncSession,
        )

        await compute_jobs._hold_compute_job_for_managed_compute(
            session_factory, job.id, now=_utcnow(), local_only=False
        )

        await db_session.refresh(job)
        meta = job.polling_metadata or {}
        assert job.status == ComputeJobStatus.QUEUED
        assert (
            meta["status_message"]
            == "Waiting for local capacity: managed cloud execution is disabled"
        )
        assert meta["runner_binding_wait"]["reason"] == "local_capacity_unavailable"
