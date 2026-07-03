"""Tests for the enforce-local-runner dispatch hold and its bounded timeout.

Covers the graceful side of ``require_local_runner``:
  * ``_fail_stale_local_runner_holds`` — the bounded-timeout safety valve that
    terminally fails a compute_experiment held QUEUED past the wait window so a
    require-local project can never wedge forever when the operator's hardware
    stays offline.
  * ``_try_runner_dispatch`` clearing a stale hold stamp on the pin path.

The hold-stamping side (keeping the task QUEUED instead of provisioning it in
the cloud) lives inline in the orchestrator dispatch loop, which is not unit
testable in isolation; it is exercised end-to-end by the orchestrator and the
compute_jobs defense-in-depth rejection that other modules own.
"""

import uuid
from datetime import timedelta

import pytest
from sqlmodel import select

from app import auth as auth_mod
from app import orchestrator as orch
from app.failure_classification import PROVISION_FAILURE
from app.models import (
    Alert,
    AlertSeverity,
    LocalRunner,
    Project,
    ProjectMember,
    Task,
    TaskStatus,
    _utcnow,
)
from app.workbench_state import (
    STRATEGIC_LOCAL_RUNNER_HOLD_CONTEXT_KEY,
    STRATEGIC_LOCAL_RUNNER_REASON_CONTEXT_KEY,
    STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY,
)

TEST_USER_ID = "test-user-holds"


async def _make_project(session, require_local_runner=True, name="Local-Runner Project"):
    build_config = (
        {"resource_envelope": {"require_local_runner": True}}
        if require_local_runner
        else {}
    )
    project = Project(name=name, user_id=TEST_USER_ID, build_config=build_config)
    session.add(project)
    await session.commit()
    await session.refresh(project)
    session.add(
        ProjectMember(project_id=project.id, user_id=TEST_USER_ID, role="owner")
    )
    await session.commit()
    return project


async def _make_held_task(
    session,
    project_id,
    *,
    held_ago_seconds,
    task_type="compute_experiment",
    stamp=True,
):
    context = {}
    if stamp:
        held_since = _utcnow() - timedelta(seconds=held_ago_seconds)
        context["awaiting_local_runner_since"] = held_since.isoformat()
    task = Task(
        project_id=project_id,
        payloads=[{"type": task_type}],
        status=TaskStatus.QUEUED,
        context=context,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


class TestFailStaleLocalRunnerHolds:
    @pytest.mark.asyncio
    async def test_fails_hold_older_than_timeout(self, db_session):
        project = await _make_project(db_session)
        task = await _make_held_task(
            db_session,
            project.id,
            held_ago_seconds=orch._LOCAL_RUNNER_WAIT_TIMEOUT_SECONDS + 600,
        )

        failed = await orch._fail_stale_local_runner_holds(db_session)

        assert failed == 1
        await db_session.refresh(task)
        assert task.status == TaskStatus.FAILED
        assert "require_local_runner" in (task.latest_status_message or "")
        assert "requeue" in (task.latest_status_message or "")
        # INFRA-class failure so it never poisons research dedup as a code error.
        assert (task.context or {}).get("failure_category") == PROVISION_FAILURE

    @pytest.mark.asyncio
    async def test_keeps_recent_hold(self, db_session):
        project = await _make_project(db_session)
        task = await _make_held_task(
            db_session,
            project.id,
            # Well within the wait window.
            held_ago_seconds=60,
        )

        failed = await orch._fail_stale_local_runner_holds(db_session)

        assert failed == 0
        await db_session.refresh(task)
        assert task.status == TaskStatus.QUEUED
        assert (
            task.context or {}
        ).get("awaiting_local_runner_since") is not None

    @pytest.mark.asyncio
    async def test_fails_strategic_local_hold_older_than_timeout(
        self, db_session, mocker
    ):
        project = await _make_project(db_session)
        held_since = _utcnow() - timedelta(
            seconds=orch._LOCAL_RUNNER_WAIT_TIMEOUT_SECONDS + 600
        )
        record_task_result = mocker.patch("app.routers.agent_status.record_task_result")
        task = Task(
            project_id=project.id,
            payloads=[{"type": "strategic_review"}],
            status=TaskStatus.QUEUED,
            context={
                STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY: True,
                STRATEGIC_LOCAL_RUNNER_REASON_CONTEXT_KEY: "workbench_continuous",
                STRATEGIC_LOCAL_RUNNER_HOLD_CONTEXT_KEY: held_since.isoformat(),
            },
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        failed = await orch._fail_stale_local_runner_holds(db_session)

        assert failed == 1
        await db_session.refresh(task)
        assert task.status == TaskStatus.FAILED
        assert "strategic_local_runner_required" in (
            task.latest_status_message or ""
        )
        assert "Strategic-capable local runner" in (
            task.latest_status_message or ""
        )
        assert "requeue" in (task.latest_status_message or "")
        assert (task.context or {}).get("failure_category") == PROVISION_FAILURE
        record_task_result.assert_called_once_with(
            success=False,
            failure_class=PROVISION_FAILURE,
            task_id=str(task.id),
            task_type="strategic_review",
        )
        alerts = (
            (
                await db_session.execute(
                    select(Alert).where(
                        Alert.task_id == task.id,
                        Alert.severity == AlertSeverity.ERROR,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(alerts) == 1
        assert "Strategic-capable local runner" in alerts[0].message

    @pytest.mark.asyncio
    async def test_keeps_recent_strategic_local_hold(self, db_session):
        project = await _make_project(db_session)
        held_since = _utcnow() - timedelta(seconds=60)
        task = Task(
            project_id=project.id,
            payloads=[{"type": "strategic_review"}],
            status=TaskStatus.QUEUED,
            context={
                STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY: True,
                STRATEGIC_LOCAL_RUNNER_REASON_CONTEXT_KEY: "workbench_continuous",
                STRATEGIC_LOCAL_RUNNER_HOLD_CONTEXT_KEY: held_since.isoformat(),
            },
        )
        db_session.add(task)
        await db_session.commit()

        failed = await orch._fail_stale_local_runner_holds(db_session)

        assert failed == 0
        await db_session.refresh(task)
        assert task.status == TaskStatus.QUEUED
        assert (
            task.context or {}
        ).get(STRATEGIC_LOCAL_RUNNER_HOLD_CONTEXT_KEY) is not None

    @pytest.mark.asyncio
    async def test_ignores_malformed_strategic_local_hold_markers(self, db_session):
        project = await _make_project(db_session)
        held_since = (
            _utcnow()
            - timedelta(seconds=orch._LOCAL_RUNNER_WAIT_TIMEOUT_SECONDS + 600)
        ).isoformat()
        missing_required = Task(
            project_id=project.id,
            payloads=[{"type": "strategic_review"}],
            status=TaskStatus.QUEUED,
            context={STRATEGIC_LOCAL_RUNNER_HOLD_CONTEXT_KEY: held_since},
        )
        wrong_type = Task(
            project_id=project.id,
            payloads=[{"type": "create"}],
            status=TaskStatus.QUEUED,
            context={
                STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY: True,
                STRATEGIC_LOCAL_RUNNER_HOLD_CONTEXT_KEY: held_since,
            },
        )
        approval_gate = Task(
            project_id=project.id,
            payloads=[{"type": "strategic_review"}],
            status=TaskStatus.QUEUED,
            requires_approval=True,
            context={
                STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY: True,
                STRATEGIC_LOCAL_RUNNER_HOLD_CONTEXT_KEY: held_since,
            },
        )
        archived = Task(
            project_id=project.id,
            payloads=[{"type": "strategic_review"}],
            status=TaskStatus.QUEUED,
            archived=True,
            context={
                STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY: True,
                STRATEGIC_LOCAL_RUNNER_HOLD_CONTEXT_KEY: held_since,
            },
        )
        string_required = Task(
            project_id=project.id,
            payloads=[{"type": "strategic_review"}],
            status=TaskStatus.QUEUED,
            context={
                STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY: "true",
                STRATEGIC_LOCAL_RUNNER_HOLD_CONTEXT_KEY: held_since,
            },
        )
        db_session.add_all(
            [missing_required, wrong_type, approval_gate, archived, string_required]
        )
        await db_session.commit()

        failed = await orch._fail_stale_local_runner_holds(db_session)

        assert failed == 0
        for task in (
            missing_required,
            wrong_type,
            approval_gate,
            archived,
            string_required,
        ):
            await db_session.refresh(task)
            assert task.status == TaskStatus.QUEUED

    @pytest.mark.asyncio
    async def test_ignores_unstamped_queued_task(self, db_session):
        project = await _make_project(db_session)
        task = await _make_held_task(
            db_session,
            project.id,
            held_ago_seconds=0,
            stamp=False,
        )

        failed = await orch._fail_stale_local_runner_holds(db_session)

        assert failed == 0
        await db_session.refresh(task)
        assert task.status == TaskStatus.QUEUED

    @pytest.mark.asyncio
    async def test_ignores_non_compute_experiment(self, db_session):
        """A stale stamp on a non-compute_experiment task is never failed."""
        project = await _make_project(db_session)
        task = await _make_held_task(
            db_session,
            project.id,
            held_ago_seconds=orch._LOCAL_RUNNER_WAIT_TIMEOUT_SECONDS + 600,
            task_type="create",
        )

        failed = await orch._fail_stale_local_runner_holds(db_session)

        assert failed == 0
        await db_session.refresh(task)
        assert task.status == TaskStatus.QUEUED


class TestPinClearsStaleHoldStamp:
    @pytest.mark.asyncio
    async def test_try_runner_dispatch_clears_awaiting_stamp(self, db_session):
        """A held task that is then pinned to a runner must drop the wait stamp,
        so the bounded-timeout sweep can never fail an already-claimed task."""
        project = await _make_project(db_session)
        token = auth_mod.create_runner_token(uuid.uuid4())
        runner = LocalRunner(
            project_id=project.id,
            token_hash=auth_mod.hash_runner_token(token),
            last_seen_at=_utcnow(),
            capabilities={"task_types": "all", "has_gpu": True, "max_concurrent": 2},
        )
        db_session.add(runner)
        await db_session.commit()
        await db_session.refresh(runner)

        held_since = _utcnow() - timedelta(seconds=120)
        task = Task(
            project_id=project.id,
            payloads=[{"type": "compute_experiment"}],
            status=TaskStatus.QUEUED,
            context={"awaiting_local_runner_since": held_since.isoformat()},
        )
        db_session.add(task)
        await db_session.commit()

        pinned = await orch._try_runner_dispatch(
            db_session, task, {project.id: [runner]}
        )

        assert pinned is True
        await db_session.refresh(task)
        assert task.status == TaskStatus.PROVISIONING
        assert task.context.get("runner_target_id") == str(runner.id)
        assert "awaiting_local_runner_since" not in task.context
