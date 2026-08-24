"""
Tests for the compute-job park/wake machinery.

Covers:
- test_wake_delivery_missing_job_is_truthfully_classified
- Park/wake race-safety under lock contention (Postgres-gated)
- Row-lock not held across backend RPCs
- Park/wake timeout (provisioning vs runtime split)
- Sweep strand guard
- Deliver-wake noop when task not parked
- kill_compute_job fires wake for parked task
- kill_compute_job releases row lock across backend RPC
- kill_compute_job gracefully handles peer terminal
- Park/wake timeout diagnostics
- Wake finalization triggers daily cost ceiling alert
"""

import asyncio
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app import compute_jobs
from app.compute_backends import (
    ComputeCostResult,
    ComputeLaunchResult,
    ComputeStatusResult,
)
from app.database import resolve_async_engine
from app.models import (
    Alert,
    AlertSeverity,
    ComputeJob,
    ComputeJobOutcome,
    ComputeJobStatus,
    Project,
    Task,
    TaskStatus,
    TaskWakeRegistration,
    WakeAction,
    WakeDeliveryState,
)
from app.time_utils import _utcnow
from testing.conftest import USE_POSTGRES


@pytest.mark.asyncio
async def test_wake_delivery_missing_job_is_truthfully_classified(db_session):
    """When a compute_experiment wake fires but the ComputeJob row is gone, the
    parked task must land FAILED with a distinct ``compute_job_record_missing``
    classification — not a generic metric-gate miss with no failure_category.

    Exercises the missing-job branch added to deliver_wake's compute FINALIZE
    path: park the task with a dangling wake_source_ref that references a
    non-existent job UUID, fire the wake manually, then run the delivery sweep
    and assert:
      - task.status == FAILED  (not COMPLETED, not stuck PARKED)
      - task.context["failure_category"] == "compute_job_record_missing"
      - task.context["failure_class"] == "compute_job_record_missing"
      - registration.result_state == "job_record_missing"  (NOT "finalized")
      - registration.delivery_state == DELIVERED
    """
    from app.compute_jobs import COMPUTE_JOB_TERMINAL_WAKE_KIND
    from app.reaper_sweeps import sweep_deliver_task_wakes
    from app.task_lifecycle import fire_task_wakes, park_task

    project = Project(name="Missing Job Wake", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="compute_experiment: missing job at wake",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment", "base_branch": "main"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:12]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Use a UUID that has no backing ComputeJob row — dangling reference.
    dangling_job_id = str(uuid.uuid4())

    await park_task(
        db_session,
        task,
        wake_source_kind=COMPUTE_JOB_TERMINAL_WAKE_KIND,
        wake_source_ref=dangling_job_id,
        wake_action=WakeAction.FINALIZE,
        deadline_at=_utcnow() + timedelta(hours=1),
        reason=f"parked on compute job {dangling_job_id}",
    )
    await db_session.commit()

    db_session.expire_all()
    await db_session.refresh(task)
    assert task.status == TaskStatus.PARKED

    # Fire the wake manually — simulates the monitor calling fire_task_wakes.
    fired = await fire_task_wakes(
        db_session,
        wake_source_kind=COMPUTE_JOB_TERMINAL_WAKE_KIND,
        wake_source_ref=dangling_job_id,
    )
    await db_session.commit()
    assert fired >= 1

    reg = (await db_session.execute(select(TaskWakeRegistration))).scalars().one()
    assert reg.delivery_state == WakeDeliveryState.FIRED

    # The delivery sweep must finalize with a truthful classification.
    delivered = await sweep_deliver_task_wakes(db_session, _utcnow())
    assert delivered == 1

    db_session.expire_all()
    await db_session.refresh(task)
    reg = (await db_session.execute(select(TaskWakeRegistration))).scalars().one()

    assert task.status == TaskStatus.FAILED, (
        "missing job must land task FAILED, not COMPLETED or PARKED"
    )
    assert task.parked_at is None
    assert (task.context or {}).get(
        "failure_category"
    ) == "compute_job_record_missing", (
        "failure_category must be 'compute_job_record_missing', not a generic gate miss"
    )
    assert (task.context or {}).get("failure_class") == "compute_job_record_missing"
    assert reg.delivery_state == WakeDeliveryState.DELIVERED
    assert reg.result_state == "job_record_missing", (
        "result_state must be 'job_record_missing', not 'finalized'"
    )
    assert reg.attempt_count == 1

    # Idempotency: a second sweep does nothing (DELIVERED row never re-selected).
    again = await sweep_deliver_task_wakes(db_session, _utcnow())
    assert again == 0


# ---------------------------------------------------------------------------
# Park/wake safety-net tests and race-contention proof
# ---------------------------------------------------------------------------
# (USE_POSTGRES is imported from testing.conftest above — it was re-derived here
# with the identical expression, which shadowed the import.)

# The metric the race-safety experiment declares and its stub backend reports.
# Named once so the payload declaration and the poll_status report cannot drift:
# if they ever disagree the job finalizes FAILURE and the race assertions become
# assertions about compute disposition instead.
_RACE_METRIC = "race_ok"


@pytest.mark.skipif(
    not USE_POSTGRES,
    reason="row-lock contention test requires PostgreSQL (SELECT … FOR UPDATE)",
)
@pytest.mark.asyncio
async def test_compute_park_wake_is_race_safe_under_lock_contention(
    db_session, monkeypatch
):
    """Postgres-gated: the exactly-once park/wake guarantee holds under REAL lock
    contention, in BOTH race orderings.

    The shared serialization point is the compute-job row lock (_lock_compute_job,
    SELECT … FOR UPDATE). The two writers are:
      A) the monitor terminal-fire path (process_compute_jobs_once →
         fire_task_wakes in the SAME commit as the job finalization), and
      B) the park-time terminal recheck (_maybe_fire_terminal_compute_wake,
         invoked from park_task).

    Ordering 1 — monitor wins the lock (terminal-before-park):
      The monitor finalizes and fires (nothing to fire yet).
      The park-time recheck sees the terminal job under the lock and fires the
      just-created registration. Sweep delivers exactly once → COMPLETED.

    Ordering 2 — park wins the lock (park-before-terminal):
      park_task registers PENDING, acquires the lock, sees a non-terminal job, and
      leaves the registration PENDING. The monitor later finalizes and fires.
      Sweep delivers exactly once → COMPLETED.

    The test exercises BOTH orderings inside a single asyncio.gather by running the
    two orderings sequentially, each in its own setup + gather round.  Every
    assertion checks: exactly one FIRED→DELIVERED registration, exactly one task
    COMPLETED, no double-fire or double-deliver.
    """
    from sqlalchemy.orm import sessionmaker as _sessionmaker

    from app.compute_jobs import COMPUTE_JOB_TERMINAL_WAKE_KIND, _lock_compute_job
    from app.reaper_sweeps import sweep_deliver_task_wakes
    from app.task_lifecycle import fire_task_wakes, park_task

    started_at = _utcnow() - timedelta(seconds=30)

    class _AlwaysSucceedsBackend:
        def external_id_for(self, persisted_job):
            return f"race-job-{persisted_job.id}"

        async def launch(self, spec):
            return ComputeLaunchResult(
                external_id=f"race-job-{spec.job_id}",
                started_at=started_at,
                cost_rate_per_second=0.001,
                message="launched",
            )

        async def poll_cost(self, persisted_job, *, as_of):
            return ComputeCostResult(total_cost_usd=0.05, measured_at=as_of)

        async def poll_status(self, persisted_job):
            # Report the declared metric. Ordering-1 hand-finalizes the job, but
            # ordering-2 goes through the REAL finalizer, which reclassifies a
            # SUCCEEDED run that measured nothing as outcome=FAILURE. Without a
            # reported metric the two orderings would be racing different jobs.
            return ComputeStatusResult(
                status=ComputeJobStatus.SUCCEEDED,
                exit_code=0,
                finished_at=_utcnow(),
                message="completed",
                metrics={_RACE_METRIC: 1.0},
            )

        async def terminate(self, persisted_job, *, reason):
            raise AssertionError("terminate should not be called")

    monkeypatch.setattr(
        compute_jobs, "get_compute_backend", lambda _name: _AlwaysSucceedsBackend()
    )
    monkeypatch.setattr(
        "app.task_completion._spawn_act_on_verdict_followon",
        AsyncMock(return_value=None),
    )

    Session = _sessionmaker(
        bind=db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )

    async def _make_task_and_job():
        project = Project(name=f"Race test {uuid.uuid4().hex[:6]}", user_id="test-user")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = Task(
            project_id=project.id,
            title="compute_experiment: race safety",
            status=TaskStatus.IN_PROGRESS,
            payloads=[
                {
                    "type": "compute_experiment",
                    "base_branch": "main",
                    # A compute_experiment that declares no metric, no arms and no
                    # success_criterion is a criterion-free experiment: the real
                    # finalizer marks it outcome=FAILURE even on exit 0, which
                    # terminates the task FAILED. This test is about park/wake race
                    # safety, so the vehicle must be a well-formed experiment that
                    # genuinely SUCCEEDs -- otherwise the ordering-2 assertion is
                    # measuring compute disposition, not the race.
                    "declared_metric": _RACE_METRIC,
                }
            ],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:12]}",
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)
        job = await compute_jobs.create_compute_job(
            db_session,
            task,
            command="echo ok",
            machine_type="e2-standard-2",
            spend_cap_usd=1.0,
            deadline_seconds=300,
        )
        # Skip smoke gate — these tests exercise wake/park race conditions.
        meta = dict(job.polling_metadata or {})
        meta["preflight_smoke_done"] = True
        job.polling_metadata = meta
        await db_session.commit()
        return task, job

    # ── Ordering 1: terminal-before-park race ─────────────────────────────────
    # The monitor races park_task. The asyncio.Event barrier ensures the monitor
    # session holds the compute-job row lock (FOR UPDATE) before park_task's
    # terminal recheck attempts to acquire it. The Postgres-level serialization
    # determines which side fires the registration.
    task1, job1 = await _make_task_and_job()

    monitor_has_lock = asyncio.Event()

    async def monitor_holds_and_fires():
        """Acquire row lock, signal barrier, hold briefly, then finalize + fire."""
        async with Session() as session:
            locked = await _lock_compute_job(session, job1.id)
            assert locked is not None
            monitor_has_lock.set()
            await asyncio.sleep(0.15)
            # Manually finalize the job (mirror what _process_single_compute_job does).
            locked.status = ComputeJobStatus.SUCCEEDED
            locked.outcome = ComputeJobOutcome.SUCCESS
            locked.final_cost_usd = 0.05
            locked.accrued_cost_usd = 0.05
            session.add(locked)
            await fire_task_wakes(
                session,
                wake_source_kind=COMPUTE_JOB_TERMINAL_WAKE_KIND,
                wake_source_ref=str(job1.id),
            )
            await session.commit()

    async def parked_task_races():
        """Wait for the monitor to hold the lock, then park (triggering recheck)."""
        await monitor_has_lock.wait()
        async with Session() as session:
            task_fresh = await session.get(Task, task1.id)
            await park_task(
                session,
                task_fresh,
                wake_source_kind=COMPUTE_JOB_TERMINAL_WAKE_KIND,
                wake_source_ref=str(job1.id),
                wake_action=WakeAction.FINALIZE,
                deadline_at=_utcnow() + timedelta(hours=1),
                reason=f"parked on compute job {job1.id}",
            )
            await session.commit()

    await asyncio.gather(monitor_holds_and_fires(), parked_task_races())

    # After concurrent sessions committed, force-refresh task1 so the db_session
    # identity map reflects the PARKED status before the sweep runs.  Without this,
    # SQLAlchemy 2.0 (expire_on_commit=False) returns the still-IN_PROGRESS cached
    # instance from the identity map when deliver_wake queries the task row, which
    # makes deliver_wake think the task is not PARKED and marks the registration
    # task_not_parked instead of finalizing.
    # expire_all BEFORE the refresh: the refresh re-loads task1 so its
    # attributes (task1.id below) stay readable — an expired instance would
    # lazy-load and raise MissingGreenlet under real asyncpg expiry.
    db_session.expire_all()
    await db_session.refresh(task1)
    regs1 = (
        (
            await db_session.execute(
                select(TaskWakeRegistration)
                .where(TaskWakeRegistration.task_id == task1.id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    assert len(regs1) == 1, (
        f"exactly one registration expected after ordering-1 race, got {len(regs1)}"
    )
    assert regs1[0].delivery_state == WakeDeliveryState.FIRED, (
        "registration must be FIRED after the terminal-before-park race "
        f"(got {regs1[0].delivery_state!r})"
    )

    delivered1 = await sweep_deliver_task_wakes(db_session, _utcnow())
    assert delivered1 == 1, "sweep must deliver exactly once in ordering-1 race"

    await db_session.refresh(task1)
    reg1 = (
        (
            await db_session.execute(
                select(TaskWakeRegistration)
                .where(TaskWakeRegistration.task_id == task1.id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .one()
    )
    assert task1.status == TaskStatus.COMPLETED, (
        f"task must be COMPLETED after ordering-1, got {task1.status!r}"
    )
    assert task1.parked_at is None
    assert reg1.delivery_state == WakeDeliveryState.DELIVERED
    assert reg1.result_state == "finalized"
    assert reg1.attempt_count == 1
    assert (task1.context or {}).get("failure_category") != "park_wake_timeout", (
        "task must NOT carry park_wake_timeout after a successful race delivery"
    )

    # Idempotency: a second sweep delivers nothing.
    again1 = await sweep_deliver_task_wakes(db_session, _utcnow())
    assert again1 == 0

    # ── Ordering 2: park-before-terminal race ─────────────────────────────────
    # park_task runs first (job is still QUEUED). Its terminal recheck acquires the
    # lock but sees a non-terminal job → registration stays PENDING. The monitor
    # later finalizes and fires the PENDING registration.
    task2, job2 = await _make_task_and_job()

    # Park first (job is QUEUED — not terminal).
    await park_task(
        db_session,
        task2,
        wake_source_kind=COMPUTE_JOB_TERMINAL_WAKE_KIND,
        wake_source_ref=str(job2.id),
        wake_action=WakeAction.FINALIZE,
        deadline_at=_utcnow() + timedelta(hours=1),
        reason=f"parked on compute job {job2.id}",
    )
    await db_session.commit()

    await db_session.refresh(task2)
    reg2_pending = (
        (
            await db_session.execute(
                select(TaskWakeRegistration)
                .where(TaskWakeRegistration.task_id == task2.id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .one()
    )
    assert task2.status == TaskStatus.PARKED
    assert reg2_pending.delivery_state == WakeDeliveryState.PENDING, (
        "registration must be PENDING when job is not yet terminal at park time"
    )

    # Now run the monitor to terminal + fire.
    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)
    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)

    await db_session.refresh(task2)
    await db_session.refresh(reg2_pending)
    assert reg2_pending.delivery_state == WakeDeliveryState.FIRED, (
        "registration must be FIRED after monitor fires it"
    )

    delivered2 = await sweep_deliver_task_wakes(db_session, _utcnow())
    assert delivered2 == 1, "sweep must deliver exactly once in ordering-2 race"

    await db_session.refresh(task2)
    reg2_done = (
        (
            await db_session.execute(
                select(TaskWakeRegistration)
                .where(TaskWakeRegistration.task_id == task2.id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .one()
    )
    # Name the job's own verdict in the failure message. The completion gate
    # classifies job.outcome, so "task is FAILED" is unactionable without it --
    # a stale read and a genuinely failed run look identical from the task row.
    await db_session.refresh(job2)
    assert task2.status == TaskStatus.COMPLETED, (
        f"task must be COMPLETED after ordering-2, got {task2.status!r} "
        f"(job outcome={job2.outcome!r} status={job2.status!r} "
        f"terminal_reason={job2.terminal_reason!r}; gate="
        f"{(task2.context or {}).get('evaluation_gate')!r})"
    )
    assert reg2_done.delivery_state == WakeDeliveryState.DELIVERED
    assert reg2_done.result_state == "finalized"
    assert reg2_done.attempt_count == 1

    again2 = await sweep_deliver_task_wakes(db_session, _utcnow())
    assert again2 == 0


@pytest.mark.skipif(
    not USE_POSTGRES,
    reason="row-lock contention test requires PostgreSQL (SELECT … FOR UPDATE)",
)
@pytest.mark.asyncio
async def test_compute_monitor_does_not_hold_row_lock_across_backend_rpcs(
    db_session, monkeypatch
):
    """Regression: backend RPCs run without pinning the compute-job row lock.

    Also rechecks the terminal-before-park wake path in the same test so the
    refactor keeps the row-lock-serialized terminal wake handoff intact.
    """
    from app.compute_jobs import COMPUTE_JOB_TERMINAL_WAKE_KIND
    from app.reaper_sweeps import sweep_deliver_task_wakes
    from app.task_lifecycle import park_task

    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )

    # --- Lock-release proof: launch RPCs must not hold the row lock. ---------
    project = Project(name="Compute Lock Release", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="compute_experiment: lock release",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment", "base_branch": "main", "success_criterion": "artifact_produced"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:12]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="echo ok",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )
    # Skip smoke gate so the monitor reaches backend.launch() on the first tick.
    meta = dict(job.polling_metadata or {})
    meta["preflight_smoke_done"] = True
    job.polling_metadata = meta
    await db_session.commit()

    launch_entered = asyncio.Event()
    launch_release = asyncio.Event()
    started_at = _utcnow() - timedelta(seconds=30)

    class _BlockingLaunchBackend:
        def external_id_for(self, persisted_job):
            return f"lock-release-{persisted_job.id}"

        async def launch(self, spec):
            launch_entered.set()
            await launch_release.wait()
            return ComputeLaunchResult(
                external_id=f"lock-release-{spec.job_id}",
                started_at=started_at,
                cost_rate_per_second=0.001,
                message="launched",
            )

        async def poll_cost(self, persisted_job, *, as_of):
            raise AssertionError("poll_cost should not run during launch")

        async def poll_status(self, persisted_job):
            raise AssertionError("poll_status should not run during launch")

        async def terminate(self, persisted_job, *, reason):
            raise AssertionError("terminate should not run during launch")

    monkeypatch.setattr(
        compute_jobs, "get_compute_backend", lambda _name: _BlockingLaunchBackend()
    )
    monkeypatch.setattr(
        "app.task_completion._spawn_act_on_verdict_followon",
        AsyncMock(return_value=None),
    )

    monitor_task = asyncio.create_task(
        compute_jobs.process_compute_jobs_once(session_factory=session_factory)
    )
    await asyncio.wait_for(launch_entered.wait(), timeout=5)

    async with session_factory() as lock_session:
        locked = await lock_session.execute(
            select(ComputeJob)
            .where(ComputeJob.id == job.id)
            .with_for_update(nowait=True)
        )
        locked_job = locked.scalars().one()
        assert locked_job.status == ComputeJobStatus.STARTING

    launch_release.set()
    await monitor_task

    db_session.expire_all()
    await db_session.refresh(job)
    assert job.status == ComputeJobStatus.RUNNING
    assert job.started_at is not None
    assert job.external_compute_id == f"lock-release-{job.id}"

    # --- Race safety: terminal-before-park still fires exactly once. ---------
    project2 = Project(name="Compute Terminal Wake", user_id="test-user")
    db_session.add(project2)
    await db_session.commit()
    await db_session.refresh(project2)

    task2 = Task(
        project_id=project2.id,
        title="compute_experiment: terminal wake",
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "compute_experiment",
                "base_branch": "main",
                "declared_metric": "score",
            }
        ],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:12]}",
    )
    db_session.add(task2)
    await db_session.commit()
    await db_session.refresh(task2)

    job2 = await compute_jobs.create_compute_job(
        db_session,
        task2,
        command="echo ok",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )
    # Skip smoke gate — this test exercises terminal-before-park wake handoff.
    meta2 = dict(job2.polling_metadata or {})
    meta2["preflight_smoke_done"] = True
    job2.polling_metadata = meta2
    await db_session.commit()

    class _TerminalBackend:
        def external_id_for(self, persisted_job):
            return f"terminal-wake-{persisted_job.id}"

        async def launch(self, spec):
            return ComputeLaunchResult(
                external_id=f"terminal-wake-{spec.job_id}",
                started_at=started_at,
                cost_rate_per_second=0.001,
                message="launched",
            )

        async def poll_cost(self, persisted_job, *, as_of):
            return ComputeCostResult(total_cost_usd=0.21, measured_at=as_of)

        async def poll_status(self, persisted_job):
            return ComputeStatusResult(
                status=ComputeJobStatus.SUCCEEDED,
                exit_code=0,
                finished_at=_utcnow(),
                message="completed",
                metrics={"score": 1.0},
            )

        async def terminate(self, persisted_job, *, reason):
            raise AssertionError("terminate should not be called")

    monkeypatch.setattr(
        compute_jobs, "get_compute_backend", lambda _name: _TerminalBackend()
    )

    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)
    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)

    db_session.expire_all()
    await db_session.refresh(job2)
    # Re-load the task too: park_task reads task2 attributes, and an expired
    # instance would lazy-load (MissingGreenlet under real asyncpg expiry).
    await db_session.refresh(task2)
    assert job2.status == ComputeJobStatus.SUCCEEDED
    # criterion-free compute_experiment → outcome may be FAILURE (no metric declared)
    assert job2.outcome in (ComputeJobOutcome.SUCCESS, ComputeJobOutcome.FAILURE)

    await park_task(
        db_session,
        task2,
        wake_source_kind=COMPUTE_JOB_TERMINAL_WAKE_KIND,
        wake_source_ref=str(job2.id),
        wake_action=WakeAction.FINALIZE,
        deadline_at=_utcnow() + timedelta(hours=1),
        reason=f"parked on compute job {job2.id}",
    )
    await db_session.commit()

    db_session.expire_all()
    await db_session.refresh(task2)
    reg = (await db_session.execute(select(TaskWakeRegistration))).scalars().one()
    assert reg.delivery_state == WakeDeliveryState.FIRED

    delivered = await sweep_deliver_task_wakes(db_session, _utcnow())
    assert delivered == 1

    db_session.expire_all()
    await db_session.refresh(task2)
    reg = (await db_session.execute(select(TaskWakeRegistration))).scalars().one()
    assert task2.status == TaskStatus.COMPLETED
    assert reg.delivery_state == WakeDeliveryState.DELIVERED
    assert reg.attempt_count == 1

    again = await sweep_deliver_task_wakes(db_session, _utcnow())
    assert again == 0


@pytest.mark.asyncio
async def test_park_wake_timeout_disambiguates_compute_job_state(db_session):
    """Safety net: a PENDING registration whose deadline_at has passed causes the
    PARKED task to go FAILED with failure_class/failure_category 'park_wake_timeout',
    and the diagnostic payload distinguishes why the wake expired.

    Also verifies idempotency: a second sweep pass on a non-PENDING registration
    is a strict no-op (registration not re-expired, task status unchanged).
    """
    from app.reaper_sweeps import sweep_deliver_task_wakes
    from app.task_lifecycle import park_task

    project = Project(name="Expire Wake Safety Net", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="expire-wake test task",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment", "base_branch": "main"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:12]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="sleep 60",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )

    # Park with a deadline ALREADY in the past so sweep pass 2 expires it.
    past_deadline = _utcnow() - timedelta(minutes=5)
    await park_task(
        db_session,
        task,
        wake_source_kind="compute_job_terminal",
        wake_source_ref=str(job.id),
        wake_action=WakeAction.FINALIZE,
        deadline_at=past_deadline,
        reason="parked on slow job",
    )
    await db_session.commit()

    db_session.expire_all()
    await db_session.refresh(task)
    reg = (await db_session.execute(select(TaskWakeRegistration))).scalars().one()
    assert task.status == TaskStatus.PARKED
    # Job is still QUEUED (not terminal), so park-time recheck leaves it PENDING.
    assert reg.delivery_state == WakeDeliveryState.PENDING
    assert reg.deadline_at is not None

    # Run the sweep — pass 2 (expire) must trigger on the past deadline.
    delivered = await sweep_deliver_task_wakes(db_session, _utcnow())
    assert delivered == 0, "no FIRED registration to deliver"

    db_session.expire_all()
    await db_session.refresh(task)
    reg = (await db_session.execute(select(TaskWakeRegistration))).scalars().one()

    assert task.status == TaskStatus.FAILED, (
        f"task must be FAILED after park wake expiry, got {task.status!r}"
    )
    assert task.parked_at is None
    assert (task.context or {}).get("failure_category") == "park_wake_timeout", (
        f"expected failure_category='park_wake_timeout', got {task.context!r}"
    )
    assert (task.context or {}).get("failure_class") == "park_wake_timeout", (
        f"expected failure_class='park_wake_timeout', got {task.context!r}"
    )
    assert (task.context or {}).get(
        "failure_detail"
    ) == "compute_job_terminal_job_not_terminal_by_deadline"
    diagnostics = (task.context or {}).get("park_wake_timeout_diagnostics") or {}
    assert diagnostics["job_terminal_by_deadline"] is False
    assert diagnostics["job_status"] == ComputeJobStatus.QUEUED.value
    assert diagnostics["job_completed_at"] is None
    assert diagnostics["deadline_at"] == past_deadline.isoformat()
    assert diagnostics["requested_at"] == reg.requested_at.isoformat()
    assert diagnostics["claimed_at"] is None
    assert diagnostics["fired_at"] is None
    # Provisioning-vs-runtime enrichment: job never started, so execution_seconds is None.
    assert "job_created_at" in diagnostics
    assert diagnostics["job_started_at"] is None
    assert diagnostics["provisioning_seconds"] is not None
    assert diagnostics["execution_seconds"] is None
    assert "expired before the job reached terminal state" in (
        task.latest_status_message or ""
    )
    assert reg.result_detail == task.latest_status_message
    assert reg.delivery_state == WakeDeliveryState.EXPIRED
    assert reg.result_state == "expired"

    # Idempotency: a second sweep is a strict no-op.
    again = await sweep_deliver_task_wakes(db_session, _utcnow())
    assert again == 0

    db_session.expire_all()
    await db_session.refresh(task)
    reg = (await db_session.execute(select(TaskWakeRegistration))).scalars().one()
    assert task.status == TaskStatus.FAILED
    assert reg.delivery_state == WakeDeliveryState.EXPIRED


@pytest.mark.asyncio
async def test_park_wake_timeout_when_job_finished_but_wake_never_fired(db_session):
    """A terminal compute job that completed before the park deadline but never
    fired its wake should expire with the alternate diagnostic branch.
    """
    from app.reaper_sweeps import sweep_deliver_task_wakes
    from app.task_lifecycle import park_task

    project = Project(name="Expire Wake Terminal Job", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="expire-wake terminal job task",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment", "base_branch": "main"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:12]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="sleep 60",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )
    # Park while job is still QUEUED so _maybe_fire_terminal_compute_wake leaves
    # the registration PENDING — simulating the window before the monitor fires.
    now = _utcnow()
    future_deadline = now + timedelta(hours=1)
    await park_task(
        db_session,
        task,
        wake_source_kind="compute_job_terminal",
        wake_source_ref=str(job.id),
        wake_action=WakeAction.FINALIZE,
        deadline_at=future_deadline,
        reason="parked on job",
    )
    await db_session.commit()

    # Simulate the lost-wake race: job completed before the (now-backdated) deadline,
    # but the fire was never delivered. Backdate both the job completion and the
    # registration deadline so the sweep sees an expired-but-terminal scenario.
    completed_at = now - timedelta(minutes=10)
    past_deadline = now - timedelta(minutes=5)
    job.status = ComputeJobStatus.SUCCEEDED
    job.outcome = ComputeJobOutcome.SUCCESS
    job.terminal_reason = "completed successfully"
    job.completed_at = completed_at
    # Commit the job mutations BEFORE expire_all: real ORM expiry discards
    # pending (unflushed) attribute changes, silently un-finishing the job.
    db_session.add(job)
    await db_session.commit()

    db_session.expire_all()
    await db_session.refresh(task)
    reg = (await db_session.execute(select(TaskWakeRegistration))).scalars().one()
    # Backdate the deadline so Pass 2 (expire) triggers on this registration.
    reg.deadline_at = past_deadline
    await db_session.commit()

    db_session.expire_all()
    await db_session.refresh(task)
    reg = (await db_session.execute(select(TaskWakeRegistration))).scalars().one()
    assert task.status == TaskStatus.PARKED
    assert reg.delivery_state == WakeDeliveryState.PENDING

    delivered = await sweep_deliver_task_wakes(db_session, _utcnow())
    assert delivered == 0

    db_session.expire_all()
    await db_session.refresh(task)
    reg = (await db_session.execute(select(TaskWakeRegistration))).scalars().one()

    assert task.status == TaskStatus.FAILED
    assert (task.context or {}).get("failure_category") == "park_wake_timeout"
    assert (task.context or {}).get("failure_class") == "park_wake_timeout"
    assert (task.context or {}).get(
        "failure_detail"
    ) == "compute_job_terminal_wake_lost_after_job_completed"
    diagnostics = (task.context or {}).get("park_wake_timeout_diagnostics") or {}
    assert diagnostics["job_terminal_by_deadline"] is True
    assert diagnostics["job_status"] == ComputeJobStatus.SUCCEEDED.value
    assert diagnostics["job_completed_at"] == completed_at.isoformat()
    assert diagnostics["deadline_at"] == past_deadline.isoformat()
    assert diagnostics["requested_at"] == reg.requested_at.isoformat()
    assert diagnostics["claimed_at"] is None
    assert diagnostics["fired_at"] is None
    # Provisioning-vs-runtime enrichment fields are present (job_created_at always set).
    assert "job_created_at" in diagnostics
    assert "job_started_at" in diagnostics
    assert "provisioning_seconds" in diagnostics
    assert "execution_seconds" in diagnostics
    assert "wake never fired before the deadline" in (task.latest_status_message or "")
    assert reg.result_detail == task.latest_status_message
    assert reg.delivery_state == WakeDeliveryState.EXPIRED
    assert reg.result_state == "expired"


@pytest.mark.asyncio
async def test_sweep_strand_guard_fails_task_with_no_live_registration(db_session):
    """Safety net: sweep pass 3 (strand-guard) FAILs a PARKED task whose parked_at
    is older than _PARK_STRAND_SECONDS and has NO live (PENDING/FIRED) registration,
    while leaving recently-parked tasks and tasks with live registrations untouched.

    Uses backdated parked_at to trigger the guard without sleeping.
    """
    from app.reaper_sweeps import sweep_deliver_task_wakes
    from app.task_lifecycle import _PARK_STRAND_SECONDS

    now = _utcnow()
    project = Project(name="Strand Guard Safety Net", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Task A: stranded — old parked_at, no live registration.
    task_a = Task(
        project_id=project.id,
        title="stranded task",
        status=TaskStatus.PARKED,
        payloads=[{"type": "compute_experiment", "base_branch": "main"}],
        parked_at=now - timedelta(seconds=_PARK_STRAND_SECONDS + 60),
        park_reason="stranded",
    )
    db_session.add(task_a)

    # Task B: recently parked — within the strand window, should NOT be stranded.
    task_b = Task(
        project_id=project.id,
        title="recently parked task",
        status=TaskStatus.PARKED,
        payloads=[{"type": "compute_experiment", "base_branch": "main"}],
        parked_at=now - timedelta(minutes=10),
        park_reason="parked recently",
    )
    db_session.add(task_b)
    await db_session.commit()
    await db_session.refresh(task_a)
    await db_session.refresh(task_b)

    # Task C: old parked_at but still has a live PENDING registration — must NOT be stranded.
    task_c = Task(
        project_id=project.id,
        title="old but has live registration",
        status=TaskStatus.PARKED,
        payloads=[{"type": "compute_experiment", "base_branch": "main"}],
        parked_at=now - timedelta(seconds=_PARK_STRAND_SECONDS + 300),
        park_reason="old but live",
    )
    db_session.add(task_c)
    await db_session.commit()
    await db_session.refresh(task_c)

    job_c = await compute_jobs.create_compute_job(
        db_session,
        task_c,
        command="sleep 60",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )
    # Add a live PENDING registration for task_c so the strand guard skips it.
    reg_c = TaskWakeRegistration(
        task_id=task_c.id,
        project_id=project.id,
        wake_source_kind="compute_job_terminal",
        wake_source_ref=str(job_c.id),
        wake_action=WakeAction.FINALIZE,
        delivery_state=WakeDeliveryState.PENDING,
        requested_at=now,
        deadline_at=now + timedelta(hours=1),
    )
    db_session.add(reg_c)
    await db_session.commit()

    # Run sweep — no FIRED registrations (pass 1 delivers 0), no past deadlines
    # (pass 2 expires 0 for tasks A/C since no registration at all for A and
    # deadline is future for C). Pass 3 strands task_a only.
    delivered = await sweep_deliver_task_wakes(db_session, now)
    assert delivered == 0, "no FIRED registrations to deliver"

    db_session.expire_all()
    await db_session.refresh(task_a)
    await db_session.refresh(task_b)
    await db_session.refresh(task_c)

    # Stranded task is now FAILED with the correct classification.
    assert task_a.status == TaskStatus.FAILED, (
        f"stranded task_a must be FAILED, got {task_a.status!r}"
    )
    assert task_a.parked_at is None
    assert (task_a.context or {}).get("failure_category") == "park_wake_stranded", (
        f"expected failure_category='park_wake_stranded', got {task_a.context!r}"
    )
    assert (task_a.context or {}).get("failure_class") == "park_wake_stranded", (
        f"expected failure_class='park_wake_stranded', got {task_a.context!r}"
    )

    # Recently-parked task is left untouched.
    assert task_b.status == TaskStatus.PARKED, (
        f"recently-parked task_b must remain PARKED, got {task_b.status!r}"
    )

    # Old task with a live registration is left untouched.
    assert task_c.status == TaskStatus.PARKED, (
        f"task_c with live registration must remain PARKED, got {task_c.status!r}"
    )


@pytest.mark.asyncio
async def test_deliver_wake_noop_when_task_not_parked(db_session):
    """Safety net: when the task is no longer PARKED at delivery time, deliver_wake
    records result_state='task_not_parked' (delivery_state FAILED) and leaves the
    task untouched — no spurious finalize or status change.

    Also covers the idempotency guard: a non-FIRED registration is a no-op.
    """
    from app.task_lifecycle import deliver_wake

    project = Project(name="Deliver Wake Not Parked", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Create the task as IN_PROGRESS so create_compute_job accepts it.
    task = Task(
        project_id=project.id,
        title="already completed task",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment", "base_branch": "main"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="echo ok",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )

    # Now advance the task to COMPLETED (simulates having been woken by something else
    # before this particular registration is delivered).
    task.status = TaskStatus.COMPLETED
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Create a FIRED registration (as if the monitor fired it, but the task moved on).
    reg = TaskWakeRegistration(
        task_id=task.id,
        project_id=project.id,
        wake_source_kind="compute_job_terminal",
        wake_source_ref=str(job.id),
        wake_action=WakeAction.FINALIZE,
        delivery_state=WakeDeliveryState.FIRED,
        requested_at=_utcnow(),
        fired_at=_utcnow(),
        deadline_at=_utcnow() + timedelta(hours=1),
    )
    db_session.add(reg)
    await db_session.commit()

    # Attempt delivery — task is COMPLETED (not PARKED), so this is a no-op.
    advanced = await deliver_wake(db_session, reg, now=_utcnow())
    await db_session.commit()

    assert advanced is False, "deliver_wake must return False when task is not PARKED"

    # Snapshot the id before expire_all: reading reg.id on an expired instance
    # lazy-loads (MissingGreenlet under real asyncpg expiry).
    reg_id = reg.id
    db_session.expire_all()
    await db_session.refresh(task)
    reg_fresh = await db_session.get(TaskWakeRegistration, reg_id)

    assert task.status == TaskStatus.COMPLETED, (
        "task status must be unchanged (COMPLETED) after no-op delivery"
    )
    assert reg_fresh.delivery_state == WakeDeliveryState.FAILED, (
        f"registration must be FAILED (not_parked no-op), got {reg_fresh.delivery_state!r}"
    )
    assert reg_fresh.result_state == "task_not_parked", (
        f"expected result_state='task_not_parked', got {reg_fresh.result_state!r}"
    )

    # Idempotency: a second deliver_wake call on the now-FAILED registration is a no-op.
    advanced2 = await deliver_wake(db_session, reg_fresh, now=_utcnow())
    await db_session.commit()
    assert advanced2 is False, (
        "deliver_wake on a non-FIRED registration must return False"
    )

    db_session.expire_all()
    reg_again = await db_session.get(TaskWakeRegistration, reg_id)
    assert reg_again.delivery_state == WakeDeliveryState.FAILED
    assert reg_again.result_state == "task_not_parked"


@pytest.mark.asyncio
async def test_kill_compute_job_fires_wake_for_parked_task(db_session, monkeypatch):
    """Safety net: kill_compute_job fires the compute-job-terminal wake for any
    PARKED task that is waiting on the job, so the task wakes immediately rather
    than sitting until its park deadline.
    """
    from app.reaper_sweeps import sweep_deliver_task_wakes
    from app.task_lifecycle import park_task

    project = Project(name="Kill Job Fires Wake", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="compute_experiment: parked awaiting kill",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment", "base_branch": "main"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:12]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="sleep 3600",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=3600,
    )
    # Advance job to RUNNING so kill_compute_job can accept it.
    job.status = ComputeJobStatus.RUNNING
    job.external_compute_id = f"kill-job-{uuid.uuid4().hex[:8]}"
    db_session.add(job)
    await db_session.commit()

    # Park the task on the running job.
    await park_task(
        db_session,
        task,
        wake_source_kind="compute_job_terminal",
        wake_source_ref=str(job.id),
        wake_action=WakeAction.FINALIZE,
        deadline_at=_utcnow() + timedelta(hours=1),
        reason=f"parked on compute job {job.id}",
    )
    await db_session.commit()

    # Snapshot ids before expire_all: reading them on expired instances
    # lazy-loads (MissingGreenlet under real asyncpg expiry).
    job_id = job.id
    project_id = project.id
    db_session.expire_all()
    await db_session.refresh(task)
    reg = (await db_session.execute(select(TaskWakeRegistration))).scalars().one()
    assert task.status == TaskStatus.PARKED
    # Job is RUNNING (not terminal): park-time recheck leaves registration PENDING.
    assert reg.delivery_state == WakeDeliveryState.PENDING

    # Stub the backend so terminate() succeeds without touching GCP.
    class _FakeBackend:
        async def terminate(self, persisted_job, *, reason):
            return True

    monkeypatch.setattr(
        compute_jobs, "get_compute_backend", lambda _name: _FakeBackend()
    )

    # Operator kill — must fire the wake in the same commit.
    killed = await compute_jobs.kill_compute_job(
        db_session,
        job_id=job_id,
        project_id=project_id,
        killed_by="operator@test.com",
    )
    assert killed.status == ComputeJobStatus.CANCELLED

    db_session.expire_all()
    reg = (await db_session.execute(select(TaskWakeRegistration))).scalars().one()
    assert reg.delivery_state == WakeDeliveryState.FIRED, (
        "kill_compute_job must fire the wake registration so the parked task wakes "
        f"immediately (got {reg.delivery_state!r})"
    )

    # Sweep delivers the fired wake and finalizes the task.
    delivered = await sweep_deliver_task_wakes(db_session, _utcnow())
    assert delivered == 1

    db_session.expire_all()
    await db_session.refresh(task)
    reg = (await db_session.execute(select(TaskWakeRegistration))).scalars().one()
    assert task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED), (
        f"task must be finalized after wake delivery, got {task.status!r}"
    )
    assert task.parked_at is None
    assert reg.delivery_state == WakeDeliveryState.DELIVERED
    assert reg.result_state == "finalized"
    assert reg.attempt_count == 1


@pytest.mark.asyncio
async def test_kill_compute_job_releases_row_lock_across_backend_rpc(
    db_session, monkeypatch
):
    """Regression test: operator-kill must not hold the ComputeJob row lock
    across the backend teardown RPC. The lock is released after validating the
    job is non-terminal (pre-RPC), then re-acquired after the RPC to apply the
    terminal state. This prevents stalling park-recheck paths that fire
    compute-job-terminal wakes while the kill is in flight.

    Proof: the ComputeJob row is acquirable from a separate session during the
    kill's backend terminate RPC, and a concurrent peer-terminal during the
    RPC produces exactly one terminal finalize, not two.
    """
    from app.database import AsyncSessionLocal

    project = Project(name="Kill Job Releases Lock", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, status=TaskStatus.IN_PROGRESS)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="sleep 100",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )
    job.status = ComputeJobStatus.RUNNING
    job.external_compute_id = "lock-release-test-job"
    db_session.add(job)
    await db_session.commit()

    lock_acquire_succeeded = False
    rpc_in_flight = asyncio.Event()
    rpc_can_proceed = asyncio.Event()

    class _FakeBackend:
        async def terminate(self, persisted_job, *, reason):
            # Signal that RPC is in flight so the concurrent task can try to lock.
            rpc_in_flight.set()
            # Wait for the concurrent lock attempt to complete before proceeding.
            await rpc_can_proceed.wait()
            return True

    monkeypatch.setattr(
        compute_jobs, "get_compute_backend", lambda _name: _FakeBackend()
    )

    async def try_acquire_lock_during_rpc():
        nonlocal lock_acquire_succeeded
        # Wait for the RPC to start.
        await rpc_in_flight.wait()
        # Try to acquire the lock in a separate session.
        async with AsyncSessionLocal() as peer_session:
            peer_job = await compute_jobs._lock_compute_job(peer_session, job.id)
            lock_acquire_succeeded = peer_job is not None
        # Signal that the lock attempt is complete so the RPC can proceed.
        rpc_can_proceed.set()

    # Run the lock-attempt task concurrently with the kill.
    kill_task = asyncio.create_task(
        compute_jobs.kill_compute_job(
            db_session,
            job_id=job.id,
            project_id=project.id,
            killed_by="operator@test.com",
        )
    )
    lock_task = asyncio.create_task(try_acquire_lock_during_rpc())

    await asyncio.gather(kill_task, lock_task)

    assert lock_acquire_succeeded, (
        "ComputeJob row lock must be released during backend RPC so concurrent "
        "writers are not stalled (e.g., park-recheck wakes)"
    )

    db_session.expire_all()
    await db_session.refresh(job)
    assert job.status == ComputeJobStatus.CANCELLED
    assert job.outcome == ComputeJobOutcome.CANCELLED


@pytest.mark.asyncio
async def test_kill_compute_job_gracefully_handles_peer_terminal(
    db_session, monkeypatch
):
    """Regression test: if a peer (monitor, deadline/budget teardown, or
    task-terminal cancel) drives the job terminal during the kill's lock-free
    backend RPC window, the kill must detect this on re-acquire and degrade to
    no-op (return None) instead of double-finalizing.
    """
    from app.database import AsyncSessionLocal

    project = Project(name="Kill Job Peer Terminal", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, status=TaskStatus.IN_PROGRESS)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="sleep 100",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )
    job.status = ComputeJobStatus.RUNNING
    job.external_compute_id = "peer-terminal-test-job"
    db_session.add(job)
    await db_session.commit()

    rpc_in_flight = asyncio.Event()
    rpc_can_proceed = asyncio.Event()

    class _FakeBackend:
        async def terminate(self, persisted_job, *, reason):
            # Signal that RPC is in flight.
            rpc_in_flight.set()
            # Wait for the peer to make it terminal.
            await rpc_can_proceed.wait()
            return True

    monkeypatch.setattr(
        compute_jobs, "get_compute_backend", lambda _name: _FakeBackend()
    )

    async def peer_drive_terminal():
        # Wait for the RPC to start.
        await rpc_in_flight.wait()
        # Drive the job terminal as a peer would (e.g., deadline/budget teardown).
        async with AsyncSessionLocal() as peer_session:
            peer_job = await compute_jobs._lock_compute_job(peer_session, job.id)
            assert peer_job is not None
            peer_job.status = ComputeJobStatus.FAILED
            peer_job.outcome = ComputeJobOutcome.SPEND_CAP_EXCEEDED
            peer_job.completed_at = _utcnow()
            peer_job.terminal_reason = "peer made terminal"
            await peer_session.commit()
        # Signal that the peer is done so the RPC can proceed.
        rpc_can_proceed.set()

    # Run the peer-terminal task concurrently with the kill.
    kill_task = asyncio.create_task(
        compute_jobs.kill_compute_job(
            db_session,
            job_id=job.id,
            project_id=project.id,
            killed_by="operator@test.com",
        )
    )
    peer_task = asyncio.create_task(peer_drive_terminal())

    await asyncio.gather(kill_task, peer_task)

    db_session.expire_all()
    await db_session.refresh(job)
    # The peer's outcome should be preserved (not overwritten by kill).
    assert job.outcome == ComputeJobOutcome.SPEND_CAP_EXCEEDED
    assert job.terminal_reason == "peer made terminal"


@pytest.mark.asyncio
async def test_park_wake_timeout_diagnostics_provisioning_vs_runtime_split(db_session):
    """Acceptance: park_wake_timeout diagnostics carry the provisioning-vs-runtime split.

    Two scenarios:
    (A) Job never started running (started_at is None) — all budget went to provisioning.
        Diagnostics: provisioning_seconds set, execution_seconds None.
    (B) Job started running but did not complete in time.
        Diagnostics: provisioning_seconds + execution_seconds both set.
    """
    from app.reaper_sweeps import sweep_deliver_task_wakes
    from app.task_lifecycle import park_task

    project = Project(name="Provisioning Split Test", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    # Use a future deadline first so park_task accepts it, then backdate after commit.
    future_deadline = now + timedelta(hours=1)
    # The "actual" past deadline used for diagnostics — must be after job created_at.
    past_deadline = now - timedelta(minutes=5)

    # --- Scenario A: job never started running (started_at is None) ---
    task_a = Task(
        project_id=project.id,
        title="provisioning-starved task",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment", "base_branch": "main"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:12]}",
    )
    db_session.add(task_a)
    await db_session.commit()
    await db_session.refresh(task_a)

    job_a = await compute_jobs.create_compute_job(
        db_session,
        task_a,
        command="sleep 60",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )
    # Backdate job_a's created_at so it precedes the past_deadline.
    job_a.created_at = past_deadline - timedelta(minutes=10)
    await db_session.commit()

    # Leave job_a.started_at as None — simulates provisioning starvation.
    await park_task(
        db_session,
        task_a,
        wake_source_kind="compute_job_terminal",
        wake_source_ref=str(job_a.id),
        wake_action=WakeAction.FINALIZE,
        deadline_at=future_deadline,
        reason="parked on slow job",
    )
    # Backdate the registration deadline so sweep expires it.
    reg_a = (
        (
            await db_session.execute(
                select(TaskWakeRegistration).where(
                    TaskWakeRegistration.task_id == task_a.id
                )
            )
        )
        .scalars()
        .one()
    )
    reg_a.deadline_at = past_deadline
    await db_session.commit()

    # --- Scenario B: job started running but did not complete ---
    task_b = Task(
        project_id=project.id,
        title="ran-out-of-time task",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment", "base_branch": "main"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:12]}",
    )
    db_session.add(task_b)
    await db_session.commit()
    await db_session.refresh(task_b)

    job_b = await compute_jobs.create_compute_job(
        db_session,
        task_b,
        command="sleep 600",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )
    # Backdate job_b's created_at so it precedes started_at and the past_deadline.
    job_b_created_at = past_deadline - timedelta(minutes=10)
    job_b.created_at = job_b_created_at
    # Simulate the job reaching running state 2 minutes after creation.
    started_at = job_b_created_at + timedelta(minutes=2)
    job_b.started_at = started_at
    await db_session.commit()

    await park_task(
        db_session,
        task_b,
        wake_source_kind="compute_job_terminal",
        wake_source_ref=str(job_b.id),
        wake_action=WakeAction.FINALIZE,
        deadline_at=future_deadline,
        reason="parked on slow job",
    )
    # Backdate the registration deadline.
    reg_b = (
        (
            await db_session.execute(
                select(TaskWakeRegistration).where(
                    TaskWakeRegistration.task_id == task_b.id
                )
            )
        )
        .scalars()
        .one()
    )
    reg_b.deadline_at = past_deadline
    await db_session.commit()

    # Expire both tasks via the sweep.
    delivered = await sweep_deliver_task_wakes(db_session, now)
    assert delivered == 0, "no FIRED registrations to deliver"

    db_session.expire_all()
    await db_session.refresh(task_a)
    await db_session.refresh(task_b)

    # --- Verify Scenario A diagnostics ---
    assert task_a.status == TaskStatus.FAILED
    assert (task_a.context or {}).get("failure_class") == "park_wake_timeout"
    assert (task_a.context or {}).get("failure_detail") == (
        "compute_job_terminal_job_not_terminal_by_deadline"
    )
    diag_a = (task_a.context or {}).get("park_wake_timeout_diagnostics") or {}
    assert diag_a["job_created_at"] is not None, "job_created_at must be recorded"
    assert diag_a["job_started_at"] is None, "job never started — must be None"
    assert diag_a["provisioning_seconds"] is not None, (
        "provisioning_seconds must reflect full deadline when job never started"
    )
    assert diag_a["execution_seconds"] is None, (
        "execution_seconds must be None when job never started"
    )
    assert diag_a["provisioning_seconds"] > 0

    # --- Verify Scenario B diagnostics ---
    assert task_b.status == TaskStatus.FAILED
    assert (task_b.context or {}).get("failure_class") == "park_wake_timeout"
    diag_b = (task_b.context or {}).get("park_wake_timeout_diagnostics") or {}
    assert diag_b["job_created_at"] is not None
    assert diag_b["job_started_at"] is not None, (
        "job_started_at must reflect started_at"
    )
    assert diag_b["provisioning_seconds"] is not None, (
        "provisioning_seconds must reflect time from creation to start"
    )
    assert diag_b["execution_seconds"] is not None, (
        "execution_seconds must reflect time from start to deadline"
    )
    # job ran for (deadline - started_at); provisioning is (started_at - created_at).
    # Both should be positive.
    assert diag_b["provisioning_seconds"] > 0


# ══════════════════════════════════════════════════════════════════════════════
# Daily cost ceiling alert — compute wake finalization path
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_compute_wake_finalization_triggers_daily_cost_ceiling_alert(
    db_session, monkeypatch
):
    """Compute wake finalization creates a budget alert when spend exceeds the ceiling.

    Verifies three behaviours in one test:
    (a) A terminal compute job whose spend pushes the project over the ceiling
        creates exactly ONE budget alert via run_compute_wake_post_commit.
    (b) Under the ceiling, no alert is created.
    (c) Cross-path dedup: if an agent task already alerted today, a subsequent
        compute wake does NOT create a second alert.
    """
    from unittest.mock import patch as _patch
    from app.compute_jobs import COMPUTE_JOB_TERMINAL_WAKE_KIND
    import app.shared_state as _shared_state
    from app.reaper_sweeps import sweep_deliver_task_wakes
    from app.task_lifecycle import fire_task_wakes, park_task

    # ── Setup ────────────────────────────────────────────────────────────────
    project = Project(name="Cost Ceiling Compute Wake Test", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    # Snapshot the id: the expire_all() calls below expire `project`, and
    # reading project.id afterwards lazy-loads (MissingGreenlet on asyncpg).
    project_id = project.id

    monkeypatch.setattr(
        "app.task_completion.broadcast_to_project", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        "app.cost_ceiling.broadcast_to_project", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        "app.notification_dispatch.dispatch_external_notification",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.knowledge_completion._analyze_compute_experiment_with_llm",
        AsyncMock(return_value=(None, "precondition-not-met")),
    )

    async def _make_parked_task_and_terminal_job(final_cost_usd: float):
        """Create a parked compute_experiment task backed by a terminal job."""
        task = Task(
            project_id=project_id,
            title="compute_experiment: cost ceiling test",
            status=TaskStatus.IN_PROGRESS,
            payloads=[{"type": "compute_experiment", "base_branch": "main"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:8]}",
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        job = await compute_jobs.create_compute_job(
            db_session,
            task,
            command="echo ok",
            machine_type="e2-standard-2",
            spend_cap_usd=final_cost_usd + 1.0,
            deadline_seconds=300,
        )
        await park_task(
            db_session,
            task,
            wake_source_kind=COMPUTE_JOB_TERMINAL_WAKE_KIND,
            wake_source_ref=str(job.id),
            wake_action=WakeAction.FINALIZE,
            deadline_at=_utcnow() + timedelta(hours=1),
            reason=f"parked on compute job {job.id}",
        )
        await db_session.commit()

        job.status = ComputeJobStatus.SUCCEEDED
        job.outcome = ComputeJobOutcome.SUCCESS
        job.final_cost_usd = final_cost_usd
        job.accrued_cost_usd = final_cost_usd
        job.completed_at = _utcnow().replace(tzinfo=None)
        db_session.add(job)
        await fire_task_wakes(
            db_session,
            wake_source_kind=COMPUTE_JOB_TERMINAL_WAKE_KIND,
            wake_source_ref=str(job.id),
        )
        await db_session.commit()
        return task, job

    # ── Scenario (b): under ceiling — no alert ───────────────────────────────
    with _patch.dict(_shared_state.runtime_config, {"daily_cost_ceiling_usd": 100.0}):
        _task_b, _job_b = await _make_parked_task_and_terminal_job(final_cost_usd=0.10)
        n_delivered = await sweep_deliver_task_wakes(db_session, _utcnow())
        assert n_delivered >= 1

    db_session.expire_all()
    alerts_b = (
        (await db_session.execute(select(Alert).where(Alert.project_id == project_id)))
        .scalars()
        .all()
    )
    assert len(alerts_b) == 0, "no alert expected when spend is under ceiling"

    # ── Scenario (a): over ceiling — exactly one alert ───────────────────────
    with _patch.dict(_shared_state.runtime_config, {"daily_cost_ceiling_usd": 0.05}):
        _task_a, _job_a = await _make_parked_task_and_terminal_job(final_cost_usd=0.60)
        n_delivered = await sweep_deliver_task_wakes(db_session, _utcnow())
        assert n_delivered >= 1

    db_session.expire_all()
    alerts_a = (
        (
            await db_session.execute(
                select(Alert).where(
                    Alert.project_id == project_id,
                    Alert.title == "Daily cost ceiling exceeded",
                    Alert.severity == AlertSeverity.WARNING,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts_a) == 1, f"expected exactly 1 alert, got {len(alerts_a)}"
    assert alerts_a[0].task_id is None, "alert must be project-level (task_id IS NULL)"

    # ── Scenario (c): cross-path dedup — no second alert on second wake ───────
    with _patch.dict(_shared_state.runtime_config, {"daily_cost_ceiling_usd": 0.05}):
        _task_c, _job_c = await _make_parked_task_and_terminal_job(final_cost_usd=0.70)
        n_delivered = await sweep_deliver_task_wakes(db_session, _utcnow())
        assert n_delivered >= 1

    db_session.expire_all()
    alerts_c = (
        (
            await db_session.execute(
                select(Alert).where(
                    Alert.project_id == project_id,
                    Alert.title == "Daily cost ceiling exceeded",
                    Alert.severity == AlertSeverity.WARNING,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts_c) == 1, (
        f"dedup guard must prevent a second alert, got {len(alerts_c)}"
    )


