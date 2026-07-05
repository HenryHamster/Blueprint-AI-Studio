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
    async def test_strategic_local_hold_falls_back_to_cloud_after_timeout(
        self, db_session, mocker
    ):
        """SAFETY: an abandoned required-local Strategic hold self-heals to CLOUD.

        A stale Strategic hold (opened-and-left, dead/absent runner) is NOT
        terminally failed — the local-required pin is cleared and the task is
        returned to normal dispatch so it provisions in the cloud. No
        PROVISION_FAILURE, no failed task result, and the operator alert is INFO
        (a graceful fallback), not ERROR.
        """
        project = await _make_project(db_session)
        held_since = _utcnow() - timedelta(
            seconds=orch._strategic_local_hold_timeout_seconds() + 600
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
                "preferred_runner_target_id": "runner-123",
            },
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        # Not counted as a failure — Strategic holds fall back to cloud.
        failed = await orch._fail_stale_local_runner_holds(db_session)

        assert failed == 0
        await db_session.refresh(task)
        # Stays QUEUED so the normal dispatch loop provisions it in the cloud.
        assert task.status == TaskStatus.QUEUED
        # The hard-local pin is fully cleared so dispatch no longer re-holds it.
        ctx = task.context or {}
        assert STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY not in ctx
        assert STRATEGIC_LOCAL_RUNNER_HOLD_CONTEXT_KEY not in ctx
        assert STRATEGIC_LOCAL_RUNNER_REASON_CONTEXT_KEY not in ctx
        assert "preferred_runner_target_id" not in ctx
        # Not a failure: no failure category, no recorded failed result.
        assert ctx.get("failure_category") is None
        assert "cloud" in (task.latest_status_message or "").lower()
        assert "strategic_local_runner_required" in (
            task.latest_status_message or ""
        )
        record_task_result.assert_not_called()
        # Operator is alerted, but at INFO/degraded severity (graceful fallback).
        alerts = (
            (
                await db_session.execute(
                    select(Alert).where(Alert.task_id == task.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(alerts) == 1
        assert alerts[0].severity == AlertSeverity.INFO
        error_alerts = [a for a in alerts if a.severity == AlertSeverity.ERROR]
        assert error_alerts == []

    @pytest.mark.asyncio
    async def test_strategic_local_hold_uses_configured_timeout(
        self, db_session, mocker
    ):
        """The strategic fallback cutoff comes from the runtime setting, and the
        compute (6h) fallback is unaffected by it."""
        mocker.patch.dict(
            orch._runtime_config,
            {"strategic_local_hold_timeout_seconds": 120},
        )
        project = await _make_project(db_session)
        held_since = (_utcnow() - timedelta(seconds=200)).isoformat()  # > 120s
        strategic_task = Task(
            project_id=project.id,
            payloads=[{"type": "strategic_review"}],
            status=TaskStatus.QUEUED,
            context={
                STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY: True,
                STRATEGIC_LOCAL_RUNNER_REASON_CONTEXT_KEY: "workbench_continuous",
                STRATEGIC_LOCAL_RUNNER_HOLD_CONTEXT_KEY: held_since,
            },
        )
        # A compute hold at the SAME 200s age must NOT trip — compute keeps the 6h
        # window, so the small strategic setting never fails compute work early.
        compute_task = Task(
            project_id=project.id,
            payloads=[{"type": "compute_experiment"}],
            status=TaskStatus.QUEUED,
            context={"awaiting_local_runner_since": held_since},
        )
        db_session.add_all([strategic_task, compute_task])
        await db_session.commit()

        failed = await orch._fail_stale_local_runner_holds(db_session)

        assert failed == 0  # strategic → cloud fallback, compute → still holding
        await db_session.refresh(strategic_task)
        await db_session.refresh(compute_task)
        assert strategic_task.status == TaskStatus.QUEUED
        assert (
            STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY
            not in (strategic_task.context or {})
        )
        # Compute hold is untouched (well within its 6h window).
        assert compute_task.status == TaskStatus.QUEUED
        assert (
            compute_task.context or {}
        ).get("awaiting_local_runner_since") == held_since

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


class TestHoldStrategicLocalRunnerTask:
    @pytest.mark.asyncio
    async def test_repeated_hold_no_change_does_not_crash(self, db_session):
        """A held Strategic task with no live local runner is re-evaluated every
        orchestrator cycle. The first cycle stamps the hold (commit path); every
        cycle after makes no change and takes the ``session.rollback()`` branch.

        Rollback expires every session-bound ORM instance (even under
        ``expire_on_commit=False`` — that setting governs commit, not rollback),
        so any ``task.<attr>`` read after it fires a lazy-load. Under asyncpg
        that reload happens outside a greenlet and raises ``MissingGreenlet``,
        which quarantined the real task 8c37771e after 3 cycles. The hold must
        snapshot identity before the rollback and never touch the ORM instance
        after. This guards the branch and its idempotency; the exact greenlet
        crash is asyncpg-pool-specific and is documented by the production
        traceback rather than reproduced here."""
        project = await _make_project(db_session)
        task = Task(
            project_id=project.id,
            payloads=[{"type": "strategic_review"}],
            status=TaskStatus.QUEUED,
            context={
                STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY: True,
                STRATEGIC_LOCAL_RUNNER_REASON_CONTEXT_KEY: "workbench_continuous",
            },
        )
        db_session.add(task)
        await db_session.commit()

        # Cycle 1: stamps the hold + status message -> changed -> commit path.
        first = await orch._hold_strategic_local_runner_task(db_session, task)
        assert first is True

        # Reload as the orchestrator does each cycle (fresh, attributes loaded).
        await db_session.refresh(task)

        # Cycle 2: nothing changes -> rollback branch. Pre-fix this raised
        # MissingGreenlet on the expired ``task.id`` read; it must not now.
        second = await orch._hold_strategic_local_runner_task(db_session, task)
        assert second is True

        await db_session.refresh(task)
        assert task.status == TaskStatus.QUEUED
        assert (
            task.context or {}
        ).get(STRATEGIC_LOCAL_RUNNER_HOLD_CONTEXT_KEY) is not None
