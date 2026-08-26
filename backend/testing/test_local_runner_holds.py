"""Tests for the enforce-local-runner dispatch hold and its bounded timeout.

Covers the graceful side of ``require_local_runner``:
  * ``_fail_stale_local_runner_holds`` — the bounded-timeout safety valve that
    terminally fails a compute_experiment held QUEUED past the wait window so a
    require-local project can never wedge forever when the operator's hardware
    stays offline.
  * ``_try_runner_dispatch`` clearing a stale hold stamp on the pin path.
  * ``_resolve_local_runner_hold_alert`` — dismissing the hold alert when the
    held task finally dispatches.

The hold-stamping side (keeping the task QUEUED instead of provisioning it in
the cloud) lives inline in the orchestrator dispatch loop, which is not unit
testable in isolation; it is exercised end-to-end by the orchestrator and the
compute_jobs defense-in-depth rejection that other modules own.
"""

import uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import ANY, AsyncMock

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
from app.orchestrator_runner_dispatch import (
    AWAITING_FORCED_RUNNER_CONTEXT_KEY,
    EXECUTION_PLACEMENT_WAIT_CONTEXT_KEY,
    FORCED_RUNNER_TARGET_CONTEXT_KEY,
    _hold_task_for_local_only_placement,
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


class TestHeldTaskClearsItsStampOnDispatch:
    """Successful dispatch is the ONLY place a compute runner-hold stamp is cleared.

    The dispatch path stamps three hold keys (local / container / forced runner) and
    clears them in one place: the ``runner is not None`` pin block, which now iterates
    :data:`COMPUTE_RUNNER_HOLD_CONTEXT_KEYS` rather than popping hand-listed literals.
    That single source of truth is shared with the bounded hold-timeout sweep, and the
    two must stay in lockstep — a key that is STAMPED but never CLEARED means the sweep
    terminal-FAILS a task that is already running happily on a runner.

    This test is the regression cover for that clear-on-dispatch loop: break it and a
    held task that finally gets its runner keeps a stale stamp and gets reaped.

    (Note: it is NOT cover for excluding holds from the dispatch candidate query —
    that exclusion was deliberately NOT implemented, because a stamp is cleared only on
    successful dispatch, so an excluded task could never re-enter dispatch to notice its
    runner returning. The window BACKFILL is what stops holds from burning slots, and
    its regression cover lives in
    ``test_orchestrator.py::TestDispatchWindowPreservesRank``.)
    """

    @pytest.mark.asyncio
    async def test_held_task_still_dispatches_when_its_runner_returns(self, db_session):
        """A held task whose runner comes back must launch AND drop its hold stamp."""
        project = await _make_project(db_session, require_local_runner=True)
        task = await _make_held_task(db_session, project.id, held_ago_seconds=120)
        assert (task.context or {}).get("awaiting_local_runner_since")

        # The operator's runner comes back online this cycle.
        runner = LocalRunner(
            project_id=project.id,
            token_hash=auth_mod.hash_runner_token(
                auth_mod.create_runner_token(uuid.uuid4())
            ),
            last_seen_at=_utcnow(),
            capabilities={"task_types": "all", "has_gpu": False, "max_concurrent": 2},
        )
        db_session.add(runner)
        await db_session.commit()
        await db_session.refresh(runner)

        task.payloads = [
            {
                "type": "compute_experiment",
                "command": "echo hi",
                "spend_cap_usd": 1.0,
                "deadline_seconds": 300,
            }
        ]
        db_session.add(task)
        await db_session.commit()

        outcome = await orch._dispatch_compute_experiment_directly(
            db_session, task, {project.id: [runner]}, {}
        )

        assert outcome == "launched"
        await db_session.refresh(task)
        assert "awaiting_local_runner_since" not in (task.context or {}), (
            "the hold stamp must be cleared once the task finally dispatches"
        )


class TestFailStaleLocalRunnerHolds:
    @pytest.mark.asyncio
    async def test_explicit_local_only_migrates_legacy_hold_without_failing(
        self, db_session
    ):
        """The new task-wide policy is durable backpressure, not a 6h failure.

        This covers an important rollout case: a task may already have an old
        require_local_runner marker when an operator switches the project to
        local_only.  The sweeper must migrate that row to the structured wait
        instead of applying the legacy terminal timeout.
        """
        project = await _make_project(db_session)
        project.build_config = {
            "resource_envelope": {"execution_placement_policy": "local_only"}
        }
        db_session.add(project)
        await db_session.commit()
        task = await _make_held_task(
            db_session,
            project.id,
            held_ago_seconds=orch._LOCAL_RUNNER_WAIT_TIMEOUT_SECONDS + 600,
        )

        failed = await orch._fail_stale_local_runner_holds(db_session)

        assert failed == 0
        await db_session.refresh(task)
        assert task.status == TaskStatus.QUEUED
        ctx = task.context or {}
        assert "awaiting_local_runner_since" not in ctx
        assert ctx[EXECUTION_PLACEMENT_WAIT_CONTEXT_KEY] == {
            "policy": "local_only",
            "reason": "no_eligible_local_runner",
            "since": ANY,
        }

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
    async def test_strict_pin_hold_timeout_names_pinned_runner(self, db_session):
        """A timed-out strict runner_target hold fails with a truthful reason
        naming the pinned runner and the last diagnosed why — never the generic
        require_local_runner wording (which would lie about why it was held)."""
        project = await _make_project(db_session)
        pinned_runner_id = str(uuid.uuid4())
        task = await _make_held_task(
            db_session,
            project.id,
            held_ago_seconds=orch._LOCAL_RUNNER_WAIT_TIMEOUT_SECONDS + 600,
        )
        ctx = dict(task.context or {})
        ctx["runner_pin_hold"] = {
            "runner_id": pinned_runner_id,
            "strict": True,
            "reason_code": "offline",
            "detail": "runner (host dgx-1) is not live",
        }
        task.context = ctx
        db_session.add(task)
        await db_session.commit()

        failed = await orch._fail_stale_local_runner_holds(db_session)

        assert failed == 1
        await db_session.refresh(task)
        assert task.status == TaskStatus.FAILED
        message = task.latest_status_message or ""
        assert "runner_target" in message
        assert pinned_runner_id in message
        assert "offline" in message
        assert "require_local_runner" not in message
        assert (task.context or {}).get("failure_category") == PROVISION_FAILURE

    @pytest.mark.asyncio
    async def test_forced_runner_hold_timeout_names_pinned_runner(self, db_session):
        """A timed-out FORCED-runner pin fails with a truthful reason naming the
        pinned runner — the bounded-wait contract its sibling pins already had.

        Regression cover for the unbounded-hold gap: ``forced_runner_target_id``
        stamped ``awaiting_forced_runner_since``, but NO sweep read that stamp, so
        the task sat QUEUED forever with nobody watching. It must now terminal-FAIL
        at the 6h timeout like the strict payload pin does — and the reason must
        name the forced runner, never the generic require_local_runner wording
        (which would lie: other runners may well have been online and idle; the pin
        is what excluded them).
        """
        project = await _make_project(db_session)
        forced_runner_id = str(uuid.uuid4())
        task = await _make_held_task(
            db_session,
            project.id,
            held_ago_seconds=orch._LOCAL_RUNNER_WAIT_TIMEOUT_SECONDS + 600,
            stamp=False,
        )
        ctx = dict(task.context or {})
        ctx[AWAITING_FORCED_RUNNER_CONTEXT_KEY] = (
            _utcnow() - timedelta(seconds=orch._LOCAL_RUNNER_WAIT_TIMEOUT_SECONDS + 600)
        ).isoformat()
        ctx[FORCED_RUNNER_TARGET_CONTEXT_KEY] = forced_runner_id
        task.context = ctx
        db_session.add(task)
        await db_session.commit()

        failed = await orch._fail_stale_local_runner_holds(db_session)

        assert failed == 1, "a forced-runner hold past 6h must be terminal-failed"
        await db_session.refresh(task)
        assert task.status == TaskStatus.FAILED
        message = task.latest_status_message or ""
        assert "forced_runner_target_id" in message
        assert forced_runner_id in message, "the reason must name the pinned runner"
        assert "require_local_runner" not in message, (
            "must not reuse the generic require-local wording — it would lie about "
            "why the task was held"
        )
        assert (task.context or {}).get("failure_category") == PROVISION_FAILURE

    @pytest.mark.asyncio
    async def test_keeps_recent_forced_runner_hold(self, db_session):
        """A forced-runner hold still inside the 6h window keeps waiting."""
        project = await _make_project(db_session)
        task = await _make_held_task(
            db_session, project.id, held_ago_seconds=60, stamp=False
        )
        ctx = dict(task.context or {})
        ctx[AWAITING_FORCED_RUNNER_CONTEXT_KEY] = (
            _utcnow() - timedelta(seconds=60)
        ).isoformat()
        ctx[FORCED_RUNNER_TARGET_CONTEXT_KEY] = str(uuid.uuid4())
        task.context = ctx
        db_session.add(task)
        await db_session.commit()

        failed = await orch._fail_stale_local_runner_holds(db_session)

        assert failed == 0
        await db_session.refresh(task)
        assert task.status == TaskStatus.QUEUED

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


class TestTaskWideLocalOnlyPlacement:
    @pytest.mark.asyncio
    async def test_direct_compute_waits_without_managed_fallback(self, db_session):
        """The direct ComputeJob path honors local_only before it can bill cloud."""
        project = await _make_project(db_session, require_local_runner=False)
        project.build_config = {
            "resource_envelope": {"execution_placement_policy": "local_only"}
        }
        task = Task(
            project_id=project.id,
            payloads=[
                {
                    "type": "compute_experiment",
                    "command": "echo local",
                    "spend_cap_usd": 1.0,
                    "deadline_seconds": 60,
                }
            ],
            status=TaskStatus.QUEUED,
        )
        db_session.add_all([project, task])
        await db_session.commit()

        outcome = await orch._dispatch_compute_experiment_directly(
            db_session, task, {project.id: []}, {}
        )

        assert outcome == "held"
        await db_session.refresh(task)
        ctx = task.context or {}
        assert ctx[EXECUTION_PLACEMENT_WAIT_CONTEXT_KEY]["policy"] == "local_only"
        assert "awaiting_local_runner_since" not in ctx

    @pytest.mark.asyncio
    async def test_holds_ordinary_task_without_a_runner(self, db_session):
        """local_only applies to ordinary work, not just compute experiments."""
        project = await _make_project(db_session, require_local_runner=False)
        project.build_config = {
            "resource_envelope": {"execution_placement_policy": "local_only"}
        }
        task = Task(
            project_id=project.id,
            payloads=[{"type": "create"}],
            status=TaskStatus.QUEUED,
        )
        db_session.add_all([project, task])
        await db_session.commit()

        handled = await _hold_task_for_local_only_placement(db_session, task)

        assert handled is True
        await db_session.refresh(task)
        ctx = task.context or {}
        wait = ctx[EXECUTION_PLACEMENT_WAIT_CONTEXT_KEY]
        assert wait["policy"] == "local_only"
        assert wait["reason"] == "no_eligible_local_runner"
        assert wait["since"]
        assert task.status == TaskStatus.QUEUED
        assert "local_only" in (task.latest_status_message or "")


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


class TestResolveLocalRunnerHoldAlert:
    """_resolve_local_runner_hold_alert — dismissing the companion hold alert
    when a held require-local compute task is finally pinned to a runner."""

    @pytest.mark.asyncio
    async def test_resolve_dismisses_open_hold_alert(self, db_session, mocker):
        """Seed an open hold alert, call resolve, verify dismissed_at is set and
        alert_dismissed is broadcast."""
        from app.alert_dispatch import (
            _LOCAL_RUNNER_HOLD_ALERT_TITLE,
            _resolve_local_runner_hold_alert,
        )

        bcast = mocker.patch(
            "app.runner_health_alerts.broadcast_to_project", new_callable=AsyncMock
        )

        project = await _make_project(db_session)
        task = await _make_held_task(db_session, project.id, held_ago_seconds=120)

        # Seed an open hold alert as _maybe_create_local_runner_hold_alert would.
        alert = Alert(
            project_id=project.id,
            severity=AlertSeverity.WARNING,
            title=_LOCAL_RUNNER_HOLD_ALERT_TITLE,
            message="A require-local compute experiment has been waiting...",
            domain="core",
            task_id=task.id,
        )
        db_session.add(alert)
        await db_session.commit()
        await db_session.refresh(alert)

        assert alert.dismissed_at is None

        dismissed = await _resolve_local_runner_hold_alert(db_session, task.id)
        assert dismissed == 1

        await db_session.refresh(alert)
        assert alert.dismissed_at is not None, "alert must be dismissed"

        # Broadcast fires alert_dismissed once.
        dismiss_calls = [
            c for c in bcast.call_args_list if c.args[1] == "alert_dismissed"
        ]
        assert len(dismiss_calls) == 1
        assert dismiss_calls[0].args[2]["alert_id"] == str(alert.id)
        assert dismiss_calls[0].args[2]["title"] == _LOCAL_RUNNER_HOLD_ALERT_TITLE

    @pytest.mark.asyncio
    async def test_resolve_is_noop_without_open_alert(self, db_session):
        """_resolve_local_runner_hold_alert returns 0 when no open hold alert exists."""
        from app.alert_dispatch import _resolve_local_runner_hold_alert

        project = await _make_project(db_session)
        task = await _make_held_task(db_session, project.id, held_ago_seconds=120)

        dismissed = await _resolve_local_runner_hold_alert(db_session, task.id)
        assert dismissed == 0

    @pytest.mark.asyncio
    async def test_resolve_is_idempotent(self, db_session):
        """Second resolve call is a no-op — returns 0 when alert is already dismissed."""
        from app.alert_dispatch import (
            _LOCAL_RUNNER_HOLD_ALERT_TITLE,
            _resolve_local_runner_hold_alert,
        )

        project = await _make_project(db_session)
        task = await _make_held_task(db_session, project.id, held_ago_seconds=120)

        alert = Alert(
            project_id=project.id,
            severity=AlertSeverity.WARNING,
            title=_LOCAL_RUNNER_HOLD_ALERT_TITLE,
            message="A require-local compute experiment has been waiting...",
            domain="core",
            task_id=task.id,
        )
        db_session.add(alert)
        await db_session.commit()

        first = await _resolve_local_runner_hold_alert(db_session, task.id)
        second = await _resolve_local_runner_hold_alert(db_session, task.id)

        assert first == 1
        assert second == 0, "second resolve is a no-op — alert already dismissed"


class TestLocalOnlyUnsatisfiablePlacement:
    """A ``local_only`` wait that can NEVER be satisfied must not read as capacity.

    Live platform defect (NeurIPS project ``d0955450``): the project's only bound
    runner was PAUSED, so zero live runners could serve it. The wait has no
    timeout by design, so 16 consecutive strategic cycles queued behind the
    generic "Waiting for local execution capacity" message and were force-failed
    by the stalled-cycle reaper at 3h each. Nothing in the message or the wait
    record said the condition was permanent.

    The axis that matters for a no-timeout wait is "can any LIVE runner serve
    this project", which is exactly what the orchestrator's active-runner cache
    encodes. Note the pre-existing ``runner_project_binding`` counter is NOT
    sufficient on its own: it counts a paused runner as bound (see
    ``LOCAL_CAPACITY_WAIT_MESSAGE``), which is precisely why this case slipped
    through.
    """

    @staticmethod
    async def _local_only_project(session, name):
        project = Project(
            name=name,
            user_id=TEST_USER_ID,
            build_config={
                "resource_envelope": {"execution_placement_policy": "local_only"}
            },
        )
        session.add(project)
        await session.commit()
        await session.refresh(project)
        return project

    @staticmethod
    async def _queued_task(session, project_id):
        task = Task(
            project_id=project_id,
            payloads=[{"type": "create", "prompt": "do the thing"}],
            status=TaskStatus.QUEUED,
        )
        session.add(task)
        await session.commit()
        await session.refresh(task)
        return task

    @pytest.mark.asyncio
    async def test_zero_active_bound_runners_names_the_block_not_capacity(
        self, db_session
    ):
        """No live runner at all -> distinct reason code + a remedy in the message."""
        from app.orchestrator_runner_dispatch import _try_runner_dispatch
        from app.runner_project_binding import (
            NO_ACTIVE_RUNNER_BOUND_REASON,
            RUNNER_BINDING_GRANT_ENDPOINT,
        )

        project = await self._local_only_project(db_session, "unsatisfiable-local-only")
        task = await self._queued_task(db_session, project.id)

        # An EMPTY active-runner pool for this project is the whole signal.
        assert await _try_runner_dispatch(db_session, task, {}, {}) is False
        assert await _hold_task_for_local_only_placement(
            db_session, task, active_runners_cache={}
        ) is True

        await db_session.refresh(task)
        ctx = task.context or {}
        wait = ctx[EXECUTION_PLACEMENT_WAIT_CONTEXT_KEY]
        assert wait["reason"] == NO_ACTIVE_RUNNER_BOUND_REASON
        assert wait["reason"] != "no_eligible_local_runner"
        assert wait["unsatisfiable"] is True
        assert wait["block"]["bound_runner_count"] == 0
        assert wait["block"]["active_runner_count"] == 0
        # The operator message must name the problem AND the remedy, and must
        # NOT tell the reader to wait for capacity that can never arrive.
        message = task.latest_status_message or ""
        assert message.startswith("Blocked (project policy: local_only):")
        assert "no local runner is bound to this project" in message
        assert "can never be satisfied" in message
        assert RUNNER_BINDING_GRANT_ENDPOINT in message
        assert "Waiting for local execution capacity" not in message
        # The task is still HELD, never failed and never sent to cloud.
        assert task.status == TaskStatus.QUEUED
        # PIN on the chosen seam: the pool is passed as an argument, never
        # stamped into ``task.context`` at dispatch.  A persisted marker would
        # land in the context of every managed-cloud task, which takes this same
        # "no runner" branch on every cycle.
        assert "placement_block_snapshot" not in ctx

    @pytest.mark.asyncio
    async def test_bound_but_paused_runner_names_resume_not_grant(self, db_session):
        """The exact live shape: a runner IS bound, but it is paused.

        ``count_runners_allowed_for_project`` counts it (status != 'deleted'), so
        the pre-existing binding-gap detector stays silent. The wait is still
        unsatisfiable, and the remedy is to resume it -- not to grant another.
        """
        from app.orchestrator_runner_dispatch import _try_runner_dispatch
        from app.runner_project_binding import (
            NO_ACTIVE_RUNNER_BOUND_REASON,
            count_runners_allowed_for_project,
        )

        project = await self._local_only_project(db_session, "paused-only-fleet")
        db_session.add(
            LocalRunner(
                project_id=project.id,
                token_hash=uuid.uuid4().hex,
                status="paused",
                last_seen_at=_utcnow(),
                capabilities={"task_types": "all", "max_concurrent": 2},
            )
        )
        await db_session.commit()
        task = await self._queued_task(db_session, project.id)

        # Bound by the binding counter, yet absent from the ACTIVE pool.
        assert await count_runners_allowed_for_project(db_session, project.id) == 1
        assert await _try_runner_dispatch(db_session, task, {}, {}) is False
        assert await _hold_task_for_local_only_placement(
            db_session, task, active_runners_cache={}
        ) is True

        await db_session.refresh(task)
        wait = (task.context or {})[EXECUTION_PLACEMENT_WAIT_CONTEXT_KEY]
        assert wait["reason"] == NO_ACTIVE_RUNNER_BOUND_REASON
        assert wait["unsatisfiable"] is True
        assert wait["block"]["bound_runner_count"] == 1
        message = task.latest_status_message or ""
        assert (
            "1 local runner(s) are bound to this project but NONE are active"
            in message
        )
        assert "resume or restore a bound runner" in message
        assert "Waiting for local execution capacity" not in message

    @pytest.mark.asyncio
    async def test_bound_and_active_but_busy_keeps_generic_backpressure(
        self, db_session
    ):
        """PIN: the genuinely transient case must be byte-for-byte unchanged.

        A bound, live, but fully-loaded runner IS ordinary backpressure -- it
        drains on its own. This is the behaviour the fix must not break, so both
        the reason code and the historic message are pinned literally.
        """
        from app.orchestrator_runner_dispatch import (
            RunnerDispatchSnapshot,
            _try_runner_dispatch,
        )

        project = await self._local_only_project(db_session, "busy-bound-fleet")
        task = await self._queued_task(db_session, project.id)
        # Live and eligible, but already at its max_concurrent ceiling.
        busy = RunnerDispatchSnapshot(
            uuid.uuid4(),
            project.id,
            {
                "task_types": "all",
                "max_concurrent": 1,
                "_active_agent_task_count": 1,
            },
        )

        assert (
            await _try_runner_dispatch(db_session, task, {project.id: [busy]}, {})
            is False
        )
        assert await _hold_task_for_local_only_placement(
            db_session, task, active_runners_cache={project.id: [busy]}
        ) is True

        await db_session.refresh(task)
        ctx = task.context or {}
        wait = ctx[EXECUTION_PLACEMENT_WAIT_CONTEXT_KEY]
        assert wait["reason"] == "no_eligible_local_runner"
        assert "unsatisfiable" not in wait
        assert "block" not in wait
        assert task.latest_status_message == (
            "Waiting for local execution capacity (project policy: local_only)."
        )
        assert task.status == TaskStatus.QUEUED
        assert "placement_block_snapshot" not in ctx

    @pytest.mark.asyncio
    async def test_repeated_dispatch_cycles_keep_one_stable_wait_record(
        self, db_session
    ):
        """The unsatisfiable record must not churn ``since`` across cycles.

        A record that rewrites its own start time every cycle would make queue
        age lie and would defeat any dedupe keyed on the wait, re-notifying once
        per cycle -- the failure mode this whole change exists to remove.
        """
        from app.orchestrator_runner_dispatch import _try_runner_dispatch

        project = await self._local_only_project(db_session, "repeat-cycle-local-only")
        task = await self._queued_task(db_session, project.id)

        seen = []
        for _ in range(3):
            await _try_runner_dispatch(db_session, task, {}, {})
            await _hold_task_for_local_only_placement(
                db_session, task, active_runners_cache={}
            )
            await db_session.refresh(task)
            seen.append((task.context or {})[EXECUTION_PLACEMENT_WAIT_CONTEXT_KEY])

        assert {record["since"] for record in seen} == {seen[0]["since"]}
        assert all(record["unsatisfiable"] is True for record in seen)

class TestUnsatisfiablePlacementAlert:
    """The unsatisfiable hold must be LOUD, and loud exactly once per project.

    Skipping the hold in the stalled-cycle reaper removes the only signal the
    old behaviour produced (16 force-terminated cycles). Trading a noisy
    livelock for a silent one is strictly worse, so the skip is only defensible
    while this alert exists -- these tests are what keeps that true.
    """

    @staticmethod
    async def _local_only_project(session, name):
        project = Project(
            name=name,
            user_id=TEST_USER_ID,
            build_config={
                "resource_envelope": {"execution_placement_policy": "local_only"}
            },
        )
        session.add(project)
        await session.commit()
        await session.refresh(project)
        return project

    @staticmethod
    async def _queued_task(session, project_id):
        task = Task(
            project_id=project_id,
            payloads=[{"type": "create", "prompt": "do the thing"}],
            status=TaskStatus.QUEUED,
        )
        session.add(task)
        await session.commit()
        await session.refresh(task)
        return task

    @staticmethod
    async def _open_alerts(session, project_id):
        rows = await session.execute(
            select(Alert).where(
                Alert.project_id == project_id,
                Alert.event_type == "placement.unsatisfiable_local_hold",
                Alert.dismissed_at.is_(None),
            )
        )
        return list(rows.scalars().all())

    @pytest.mark.asyncio
    async def test_unsatisfiable_hold_raises_condition_scoped_alert(self, db_session):
        """Staging the hold pages the operator, keyed on the CONDITION."""
        from app.orchestrator_runner_dispatch import _try_runner_dispatch
        from app.orchestrator_runner_placement import (
            unsatisfiable_placement_dedupe_key,
        )
        from app.runner_project_binding import RUNNER_BINDING_GRANT_ENDPOINT

        project = await self._local_only_project(db_session, "alert-unsatisfiable")
        task = await self._queued_task(db_session, project.id)

        assert await _try_runner_dispatch(db_session, task, {}, {}) is False
        assert await _hold_task_for_local_only_placement(
            db_session, task, active_runners_cache={}
        ) is True

        alerts = await self._open_alerts(db_session, project.id)
        assert len(alerts) == 1
        alert = alerts[0]
        assert alert.severity == AlertSeverity.WARNING
        assert alert.dedupe_key == unsatisfiable_placement_dedupe_key(project.id)
        # Occurrence identity (task id / cycle id) must NOT be in the key: it
        # would mint an immortal row per occurrence and reset created_at, so the
        # age-based escalation below could never fire.
        assert str(task.id) not in (alert.dedupe_key or "")
        assert "can never be satisfied" in alert.message
        assert RUNNER_BINDING_GRANT_ENDPOINT in alert.message
        assert "local_only" in alert.message

    @pytest.mark.asyncio
    async def test_repeat_staging_bumps_occurrence_instead_of_adding_a_row(
        self, db_session
    ):
        """Three dispatcher cycles, one row -- and it gets MORE informative."""
        from app.orchestrator_runner_dispatch import _try_runner_dispatch

        project = await self._local_only_project(db_session, "alert-dedupe")
        task = await self._queued_task(db_session, project.id)

        for _ in range(3):
            await _try_runner_dispatch(db_session, task, {}, {})
            await _hold_task_for_local_only_placement(
                db_session, task, active_runners_cache={}
            )

        alerts = await self._open_alerts(db_session, project.id)
        assert len(alerts) == 1
        assert alerts[0].occurrence_count == 3
        assert alerts[0].last_seen_at is not None

    @pytest.mark.asyncio
    async def test_alert_escalates_to_error_once_the_block_is_old(self, db_session):
        """Dedupe on the condition; escalate on its AGE.

        Only possible because the key is condition-scoped: the same row survives
        every cycle, so ``created_at`` records when the project actually stopped
        being able to run anything.
        """
        from app.orchestrator_runner_dispatch import _try_runner_dispatch

        project = await self._local_only_project(db_session, "alert-escalate")
        task = await self._queued_task(db_session, project.id)

        await _try_runner_dispatch(db_session, task, {}, {})
        await _hold_task_for_local_only_placement(
            db_session, task, active_runners_cache={}
        )
        alerts = await self._open_alerts(db_session, project.id)
        assert len(alerts) == 1
        assert alerts[0].severity == AlertSeverity.WARNING

        alerts[0].created_at = _utcnow() - timedelta(hours=4)
        db_session.add(alerts[0])
        await db_session.commit()

        await _try_runner_dispatch(db_session, task, {}, {})
        await _hold_task_for_local_only_placement(
            db_session, task, active_runners_cache={}
        )

        alerts = await self._open_alerts(db_session, project.id)
        assert len(alerts) == 1
        assert alerts[0].severity == AlertSeverity.ERROR
        assert "Unresolved for 4h" in alerts[0].message

    @pytest.mark.asyncio
    async def test_active_runner_taking_work_resolves_the_alert(self, db_session):
        """SOURCE_CONDITION retirement, proved through the production pin path."""
        from app.orchestrator_runner_dispatch import _try_runner_dispatch
        from app.runner_eligibility import RunnerDispatchSnapshot

        project = await self._local_only_project(db_session, "alert-resolve-pin")
        task = await self._queued_task(db_session, project.id)

        await _try_runner_dispatch(db_session, task, {}, {})
        await _hold_task_for_local_only_placement(
            db_session, task, active_runners_cache={}
        )
        assert len(await self._open_alerts(db_session, project.id)) == 1

        runner = LocalRunner(
            project_id=project.id,
            token_hash=uuid.uuid4().hex,
            status="active",
            last_seen_at=_utcnow(),
            capabilities={"task_types": "all", "max_concurrent": 2},
        )
        db_session.add(runner)
        await db_session.commit()
        await db_session.refresh(runner)

        pinned = await _try_runner_dispatch(
            db_session,
            task,
            {
                project.id: [
                    RunnerDispatchSnapshot(
                        runner.id,
                        project.id,
                        {"task_types": "all", "max_concurrent": 2},
                    )
                ]
            },
            {},
        )
        assert pinned is True
        assert await self._open_alerts(db_session, project.id) == []

    @pytest.mark.asyncio
    async def test_busy_but_live_fleet_retires_a_stale_alert(self, db_session):
        """A fleet that came back but is momentarily full is NOT an outage."""
        from app.orchestrator_runner_dispatch import _try_runner_dispatch
        from app.runner_eligibility import RunnerDispatchSnapshot

        project = await self._local_only_project(db_session, "alert-resolve-busy")
        task = await self._queued_task(db_session, project.id)

        await _try_runner_dispatch(db_session, task, {}, {})
        await _hold_task_for_local_only_placement(
            db_session, task, active_runners_cache={}
        )
        assert len(await self._open_alerts(db_session, project.id)) == 1

        busy = RunnerDispatchSnapshot(
            uuid.uuid4(),
            project.id,
            {
                "task_types": "all",
                "max_concurrent": 1,
                "_active_agent_task_count": 1,
            },
        )
        assert (
            await _try_runner_dispatch(db_session, task, {project.id: [busy]}, {})
            is False
        )
        assert await _hold_task_for_local_only_placement(
            db_session, task, active_runners_cache={project.id: [busy]}
        ) is True
        assert await self._open_alerts(db_session, project.id) == []

    @pytest.mark.asyncio
    async def test_managed_cloud_project_is_never_paged(self, db_session):
        """The empty-pool dispatch branch is taken by EVERY cloud project."""
        from app.orchestrator_runner_dispatch import _try_runner_dispatch

        project = await _make_project(
            db_session, require_local_runner=False, name="cloud-default"
        )
        task = await self._queued_task(db_session, project.id)

        assert await _try_runner_dispatch(db_session, task, {}, {}) is False
        # Not a local_only project: the hold declines and nothing is staged.
        assert await _hold_task_for_local_only_placement(
            db_session, task, active_runners_cache={}
        ) is False
        assert await self._open_alerts(db_session, project.id) == []


class TestUnsatisfiablePlacementSeamIsWired:
    """Every production caller must hand the hold the live runner pool.

    ``active_runners_cache`` is the ONLY authoritative signal that zero live
    runners can serve a project -- the hold cannot re-derive it, because
    "active" there means ``status='active'`` AND heartbeating, which is the
    dispatcher's own pool-building rule. Omitting the argument is therefore not
    a neutral default: it silently restores the exact behaviour that killed 16
    NeurIPS cycles with no alert. It is spelled as an optional keyword so
    non-dispatch callers can decline it explicitly, which makes this structural
    pin the thing that stops a NEW caller from declining it by accident.
    """

    @staticmethod
    def _app_dir():
        return Path(__file__).resolve().parents[1] / "app"

    def test_every_app_call_site_passes_the_runner_pool(self):
        import ast

        offenders: list[str] = []
        checked = 0
        for module in sorted(self._app_dir().rglob("*.py")):
            tree = ast.parse(module.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = (
                    func.id
                    if isinstance(func, ast.Name)
                    else func.attr if isinstance(func, ast.Attribute) else None
                )
                if name != "_hold_task_for_local_only_placement":
                    continue
                checked += 1
                if not any(
                    kw.arg == "active_runners_cache" for kw in node.keywords
                ):
                    offenders.append(f"{module.name}:{node.lineno}")
        assert checked >= 2, (
            "found no call sites to check -- the guard has gone vacuous "
            "(renamed function?), which is worse than a failure"
        )
        assert offenders == [], (
            "these callers hold a local_only task without observing the live "
            "runner pool, so an unsatisfiable placement will park silently "
            f"instead of paging: {offenders}"
        )
