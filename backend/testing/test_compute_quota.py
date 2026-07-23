"""
Tests for compute-job spend ceiling and concurrency cap enforcement.

Covers:
- Project daily spend ceiling (blocked / allowed / idempotent reuse / no ceiling)
- Resource envelope spend ceiling
- Inflight spend counting (active jobs, agent VM, task window, no double-count)
- Per-project job concurrency cap (blocked at cap / under cap / no cap / isolation)
"""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select

from app import compute_jobs
from app.models import (
    ComputeJob,
    ComputeJobStatus,
    Project,
    Task,
    TaskStatus,
)
from app.time_utils import _utcnow
# ── Project spend ceiling tests ───────────────────────────────────────────────


def _make_project_with_ceiling(ceiling_usd: float) -> Project:
    return Project(
        name=f"Ceiling Project {ceiling_usd}",
        user_id="test-user",
        build_config={"resource_envelope": {"spend_ceiling_usd": ceiling_usd}},
    )


async def _make_terminal_job(
    db_session, *, project_id, final_cost_usd: float
) -> ComputeJob:
    """Helper: insert a SUCCEEDED job with final_cost_usd completed 1 h ago."""
    job = ComputeJob(
        project_id=project_id,
        backend="local",
        machine_type="e2-standard-2",
        status=ComputeJobStatus.SUCCEEDED,
        final_cost_usd=final_cost_usd,
        accrued_cost_usd=final_cost_usd,
        completed_at=_utcnow() - timedelta(hours=1),
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)
    return job


@pytest.mark.asyncio
async def test_create_compute_job_respects_project_daily_spend_ceiling(db_session):
    """Ceiling set, rolling settled spend + requested cap > ceiling → blocked."""
    project = _make_project_with_ceiling(5.0)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Settled spend: $4.50 in the last 24 h.
    await _make_terminal_job(db_session, project_id=project.id, final_cost_usd=4.50)

    task = Task(project_id=project.id, status=TaskStatus.IN_PROGRESS)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Requesting $1.00 cap: 4.50 + 1.00 = 5.50 > 5.00 → should raise.
    with pytest.raises(compute_jobs.ComputeLaunchPreconditionFailed) as exc_info:
        await compute_jobs.create_compute_job(
            db_session,
            task,
            command="echo ceiling",
            machine_type="e2-standard-2",
            spend_cap_usd=1.0,
            deadline_seconds=300,
        )

    detail = exc_info.value.detail
    assert detail["error"] == "project_spend_ceiling_exceeded"
    assert detail["ceiling_usd"] == pytest.approx(5.0)
    assert detail["rolling_spend_usd"] == pytest.approx(4.50)
    assert detail["requested_cap_usd"] == pytest.approx(1.0)

    # No job row should have been created.
    jobs = (
        (
            await db_session.execute(
                select(ComputeJob).where(
                    ComputeJob.project_id == project.id,
                    ComputeJob.status.in_(
                        [
                            ComputeJobStatus.QUEUED,
                            ComputeJobStatus.STARTING,
                            ComputeJobStatus.RUNNING,
                            ComputeJobStatus.POLLING,
                        ]
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    assert jobs == [], "no compute job should be created when ceiling is exceeded"


@pytest.mark.asyncio
async def test_create_compute_job_respects_resource_envelope_spend_ceiling(db_session):
    """Ceiling set via resource_envelope key blocks launch when rolling spend >= ceiling."""
    project = Project(
        name="Resource Envelope Ceiling Project",
        user_id="test-user",
        build_config={
            "resource_envelope": {
                "spend_ceiling_usd": 5.0,
                "spend_window_days": 30,
            }
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Settled spend: $4.50 within the 30-day window.
    await _make_terminal_job(db_session, project_id=project.id, final_cost_usd=4.50)

    task = Task(project_id=project.id, status=TaskStatus.IN_PROGRESS)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Requesting $1.00 cap: 4.50 + 1.00 = 5.50 >= 5.00 → should raise.
    with pytest.raises(compute_jobs.ComputeLaunchPreconditionFailed) as exc_info:
        await compute_jobs.create_compute_job(
            db_session,
            task,
            command="echo ceiling via resource_envelope",
            machine_type="e2-standard-2",
            spend_cap_usd=1.0,
            deadline_seconds=300,
        )

    detail = exc_info.value.detail
    assert detail["error"] == "project_spend_ceiling_exceeded"
    assert detail["ceiling_usd"] == pytest.approx(5.0)
    assert detail["rolling_spend_usd"] == pytest.approx(4.50)
    assert detail["requested_cap_usd"] == pytest.approx(1.0)

    # No active job row should have been created.
    jobs = (
        (
            await db_session.execute(
                select(ComputeJob).where(
                    ComputeJob.project_id == project.id,
                    ComputeJob.status.in_(
                        [
                            ComputeJobStatus.QUEUED,
                            ComputeJobStatus.STARTING,
                            ComputeJobStatus.RUNNING,
                            ComputeJobStatus.POLLING,
                        ]
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    assert jobs == [], (
        "no compute job should be created when resource_envelope ceiling is exceeded"
    )


@pytest.mark.asyncio
async def test_create_compute_job_ceiling_allows_when_under_budget(db_session):
    """Ceiling set, rolling spend + cap is within the ceiling → job created."""
    project = _make_project_with_ceiling(10.0)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Settled spend: $4.00.
    await _make_terminal_job(db_session, project_id=project.id, final_cost_usd=4.00)

    task = Task(project_id=project.id, status=TaskStatus.IN_PROGRESS)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # 4.00 + 1.00 = 5.00 < 10.00 → allowed.
    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="echo under budget",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )

    assert job.status == ComputeJobStatus.QUEUED


@pytest.mark.asyncio
async def test_create_compute_job_idempotent_reuse_ignores_ceiling(db_session):
    """Fingerprint-matching reuse returns the existing job even when over budget."""
    project = _make_project_with_ceiling(1.0)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, status=TaskStatus.IN_PROGRESS)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # First launch succeeds before any spend is recorded.
    first = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="echo idempotent ceiling",
        machine_type="e2-standard-2",
        spend_cap_usd=0.50,
        deadline_seconds=300,
    )

    # Now push settled spend over the ceiling.
    await _make_terminal_job(db_session, project_id=project.id, final_cost_usd=0.90)

    # Same fingerprint: the existing job is reused — ceiling check is skipped.
    second = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="echo idempotent ceiling",
        machine_type="e2-standard-2",
        spend_cap_usd=0.50,
        deadline_seconds=300,
    )

    assert first.id == second.id, "idempotent reuse must return the same job"


@pytest.mark.asyncio
async def test_create_compute_job_no_ceiling_configured(db_session):
    """No daily_spend_ceiling_usd → job is always created without spend checks."""
    project = Project(name="No Ceiling Project", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Large settled spend; no ceiling configured so it should not block.
    await _make_terminal_job(db_session, project_id=project.id, final_cost_usd=9999.0)

    task = Task(project_id=project.id, status=TaskStatus.IN_PROGRESS)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="echo no ceiling",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )

    assert job.status == ComputeJobStatus.QUEUED


@pytest.mark.asyncio
async def test_create_compute_job_ceiling_counts_active_inflight_spend(db_session):
    """In-flight (active) job accrued costs push total over ceiling → blocked."""
    project = _make_project_with_ceiling(5.0)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # An existing in-flight job with $4.50 accrued cost (not yet terminal).
    running_task = Task(project_id=project.id, status=TaskStatus.IN_PROGRESS)
    db_session.add(running_task)
    await db_session.commit()
    await db_session.refresh(running_task)

    inflight = ComputeJob(
        project_id=project.id,
        task_id=running_task.id,
        backend="local",
        machine_type="e2-standard-2",
        status=ComputeJobStatus.RUNNING,
        accrued_cost_usd=4.50,
        final_cost_usd=None,
    )
    db_session.add(inflight)
    await db_session.commit()

    # New task wants to launch a $1.00 job: 0 settled + 4.50 inflight + 1.00 > 5.00.
    new_task = Task(project_id=project.id, status=TaskStatus.IN_PROGRESS)
    db_session.add(new_task)
    await db_session.commit()
    await db_session.refresh(new_task)

    with pytest.raises(compute_jobs.ComputeLaunchPreconditionFailed) as exc_info:
        await compute_jobs.create_compute_job(
            db_session,
            new_task,
            command="echo inflight ceiling",
            machine_type="e2-standard-2",
            spend_cap_usd=1.0,
            deadline_seconds=300,
        )

    detail = exc_info.value.detail
    assert detail["error"] == "project_spend_ceiling_exceeded"
    assert detail["rolling_spend_usd"] == pytest.approx(4.50)


@pytest.mark.asyncio
async def test_create_compute_job_ceiling_counts_agent_vm_spend(db_session):
    """Cross-lane: agent-VM/LLM spend alone (zero compute) blocks a compute launch.

    A project with no compute jobs but $4.50 of agent-VM/LLM spend should block
    a new compute launch when 4.50 + 1.00 cap = 5.50 > 5.00 ceiling.
    """
    project = _make_project_with_ceiling(5.0)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, status=TaskStatus.IN_PROGRESS)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # No compute jobs at all — mock agent-VM/LLM window spend at $4.50.
    # 0.0 compute + 4.50 task + 1.00 cap = 5.50 > 5.00 → blocked.
    with patch(
        "app.efficiency_snapshot.compute_project_window_spend_usd",
        new=AsyncMock(return_value=4.50),
    ), pytest.raises(compute_jobs.ComputeLaunchPreconditionFailed) as exc_info:
        await compute_jobs.create_compute_job(
            db_session,
            task,
            command="echo cross-lane ceiling",
            machine_type="e2-standard-2",
            spend_cap_usd=1.0,
            deadline_seconds=300,
        )

    detail = exc_info.value.detail
    assert detail["error"] == "project_spend_ceiling_exceeded"
    assert detail["ceiling_usd"] == pytest.approx(5.0)
    # rolling = compute(0.0) + task(4.50) = 4.50; rolling + cap = 5.50 > ceiling
    assert detail["rolling_spend_usd"] == pytest.approx(4.50)
    assert detail["task_spend_usd"] == pytest.approx(4.50)
    assert detail["compute_spend_usd"] == pytest.approx(0.0)
    assert detail["requested_cap_usd"] == pytest.approx(1.0)

    # No active compute job row should exist.
    jobs = (
        (
            await db_session.execute(
                select(ComputeJob).where(
                    ComputeJob.project_id == project.id,
                    ComputeJob.status.in_(
                        [
                            ComputeJobStatus.QUEUED,
                            ComputeJobStatus.STARTING,
                            ComputeJobStatus.RUNNING,
                            ComputeJobStatus.POLLING,
                        ]
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    assert jobs == [], (
        "no compute job should be created when agent-VM spend exceeds ceiling"
    )


@pytest.mark.asyncio
async def test_create_compute_job_ceiling_task_spend_uses_correct_window(db_session):
    """compute_project_window_spend_usd is called with window_hours=window_days*24.

    When resource_envelope.spend_window_days=30, the agent-VM/LLM spend helper
    must be queried with window_hours=720 (not the default 24), so both compute
    and task spend are measured over the same rolling window.
    """
    project = Project(
        name="Window Match Project",
        user_id="test-user",
        build_config={
            "resource_envelope": {
                "spend_ceiling_usd": 100.0,
                "spend_window_days": 30,
            }
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, status=TaskStatus.IN_PROGRESS)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    captured_kwargs: dict = {}

    async def capture_call(session, pid, *, window_hours=24):
        captured_kwargs["window_hours"] = window_hours
        return 0.0  # well below ceiling — we only care about the kwarg

    with patch(
        "app.efficiency_snapshot.compute_project_window_spend_usd",
        new=capture_call,
    ):
        # Should succeed (0.0 spend + 1.0 cap = 1.0 < 100.0 ceiling).
        await compute_jobs.create_compute_job(
            db_session,
            task,
            command="echo window match",
            machine_type="e2-standard-2",
            spend_cap_usd=1.0,
            deadline_seconds=300,
        )

    assert captured_kwargs.get("window_hours") == 30 * 24, (
        f"expected window_hours=720 (30-day window), got {captured_kwargs.get('window_hours')}"
    )


@pytest.mark.asyncio
async def test_create_compute_job_ceiling_no_double_count(db_session):
    """Compute spend and agent-VM spend are summed without overlap.

    A project with $3.00 settled compute spend + $1.50 agent-VM/LLM spend =
    $4.50 total.  Adding a $1.00 cap → 5.50 > 5.00 → blocked.  Reducing the
    task spend to $0.00 (5.00 + 0.00 + 1.00 = 6.00 > 5.00... wait, still
    blocked).  Use $0.50 task spend: 3.00 + 0.50 + 1.00 = 4.50 < 5.00 → allowed.
    """
    project = _make_project_with_ceiling(5.0)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # $3.00 settled compute spend.
    await _make_terminal_job(db_session, project_id=project.id, final_cost_usd=3.00)

    task = Task(project_id=project.id, status=TaskStatus.IN_PROGRESS)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # With $0.50 agent-VM spend: 3.00 + 0.50 + 1.00 = 4.50 < 5.00 → allowed.
    with patch(
        "app.efficiency_snapshot.compute_project_window_spend_usd",
        new=AsyncMock(return_value=0.50),
    ):
        job = await compute_jobs.create_compute_job(
            db_session,
            task,
            command="echo no double count under",
            machine_type="e2-standard-2",
            spend_cap_usd=1.0,
            deadline_seconds=300,
        )
    assert job.status == ComputeJobStatus.QUEUED

    # Now block new task: 3.00 settled + 1.50 agent-VM + 1.00 cap = 5.50 > 5.00.
    task2 = Task(project_id=project.id, status=TaskStatus.IN_PROGRESS)
    db_session.add(task2)
    await db_session.commit()
    await db_session.refresh(task2)

    with patch(
        "app.efficiency_snapshot.compute_project_window_spend_usd",
        new=AsyncMock(return_value=1.50),
    ), pytest.raises(compute_jobs.ComputeLaunchPreconditionFailed) as exc_info:
        await compute_jobs.create_compute_job(
            db_session,
            task2,
            command="echo no double count over",
            machine_type="e2-standard-2",
            spend_cap_usd=1.0,
            deadline_seconds=300,
        )

    detail = exc_info.value.detail
    assert detail["error"] == "project_spend_ceiling_exceeded"
    # Total = 3.00 compute (settled) + QUEUED job's accrued 0 + 1.50 agent = 4.50
    # But the QUEUED job from above adds to inflight accrued_cost_usd (0.0).
    assert detail["compute_spend_usd"] == pytest.approx(3.0)
    assert detail["task_spend_usd"] == pytest.approx(1.50)
    assert detail["rolling_spend_usd"] == pytest.approx(4.50)


# ---------------------------------------------------------------------------
# Per-project compute-job concurrency cap tests
# ---------------------------------------------------------------------------


async def _make_active_compute_job(db_session, *, project_id) -> ComputeJob:
    """Helper: insert an active (RUNNING) ComputeJob for the given project."""
    job = ComputeJob(
        project_id=project_id,
        backend="local",
        machine_type="e2-standard-2",
        status=ComputeJobStatus.RUNNING,
        accrued_cost_usd=0.0,
        spend_cap_usd=1.0,
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)
    return job


@pytest.mark.asyncio
async def test_compute_job_concurrency_cap_blocks_at_cap(db_session):
    """Project at its configured cap → launch raises compute_job_concurrency_limit_exceeded."""
    from app.shared_state import runtime_config

    project = Project(name="Cap Blocks At Cap", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Insert 2 active jobs so the project is AT cap=2.
    await _make_active_compute_job(db_session, project_id=project.id)
    await _make_active_compute_job(db_session, project_id=project.id)

    task = Task(project_id=project.id, status=TaskStatus.IN_PROGRESS)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    orig = dict(runtime_config.get("max_concurrent_compute_jobs_by_project") or {})
    runtime_config["max_concurrent_compute_jobs_by_project"] = {str(project.id): 2}
    try:
        with pytest.raises(compute_jobs.ComputeLaunchPreconditionFailed) as exc_info:
            await compute_jobs.create_compute_job(
                db_session,
                task,
                command="echo cap-test",
                machine_type="e2-standard-2",
                spend_cap_usd=1.0,
                deadline_seconds=300,
            )
    finally:
        runtime_config["max_concurrent_compute_jobs_by_project"] = orig

    detail = exc_info.value.detail
    assert detail["error"] == "compute_job_concurrency_limit_exceeded"
    assert detail["active_count"] == 2
    assert detail["cap"] == 2


@pytest.mark.asyncio
async def test_compute_job_concurrency_cap_allows_under_cap(db_session):
    """Project under its configured cap → launch succeeds."""
    from app.shared_state import runtime_config

    project = Project(name="Cap Allows Under Cap", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Insert 1 active job; cap is 2 → under cap.
    await _make_active_compute_job(db_session, project_id=project.id)

    task = Task(project_id=project.id, status=TaskStatus.IN_PROGRESS)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    orig = dict(runtime_config.get("max_concurrent_compute_jobs_by_project") or {})
    runtime_config["max_concurrent_compute_jobs_by_project"] = {str(project.id): 2}
    try:
        job = await compute_jobs.create_compute_job(
            db_session,
            task,
            command="echo under-cap",
            machine_type="e2-standard-2",
            spend_cap_usd=1.0,
            deadline_seconds=300,
        )
    finally:
        runtime_config["max_concurrent_compute_jobs_by_project"] = orig

    assert job is not None
    assert job.project_id == project.id


@pytest.mark.asyncio
async def test_compute_job_concurrency_cap_no_cap_configured(db_session):
    """No cap entry for the project → launch succeeds regardless of active count."""
    from app.shared_state import runtime_config

    project = Project(name="Cap No Cap Configured", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Insert many active jobs; no cap configured → should not block.
    for _ in range(5):
        await _make_active_compute_job(db_session, project_id=project.id)

    task = Task(project_id=project.id, status=TaskStatus.IN_PROGRESS)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    orig = dict(runtime_config.get("max_concurrent_compute_jobs_by_project") or {})
    # Ensure no entry for this project.
    runtime_config["max_concurrent_compute_jobs_by_project"] = {}
    try:
        job = await compute_jobs.create_compute_job(
            db_session,
            task,
            command="echo no-cap",
            machine_type="e2-standard-2",
            spend_cap_usd=1.0,
            deadline_seconds=300,
        )
    finally:
        runtime_config["max_concurrent_compute_jobs_by_project"] = orig

    assert job is not None


@pytest.mark.asyncio
async def test_compute_job_concurrency_cap_per_project_isolation(db_session):
    """Project A at cap does not block Project B's launch."""
    from app.shared_state import runtime_config

    project_a = Project(name="Cap Isolation A", user_id="test-user")
    project_b = Project(name="Cap Isolation B", user_id="test-user")
    db_session.add(project_a)
    db_session.add(project_b)
    await db_session.commit()
    await db_session.refresh(project_a)
    await db_session.refresh(project_b)

    # Project A is at cap=1 with 1 active job.
    await _make_active_compute_job(db_session, project_id=project_a.id)

    task_b = Task(project_id=project_b.id, status=TaskStatus.IN_PROGRESS)
    db_session.add(task_b)
    await db_session.commit()
    await db_session.refresh(task_b)

    orig = dict(runtime_config.get("max_concurrent_compute_jobs_by_project") or {})
    runtime_config["max_concurrent_compute_jobs_by_project"] = {
        str(project_a.id): 1,
    }
    try:
        # Project B has no cap and no active jobs → should succeed.
        job = await compute_jobs.create_compute_job(
            db_session,
            task_b,
            command="echo project-b",
            machine_type="e2-standard-2",
            spend_cap_usd=1.0,
            deadline_seconds=300,
        )
    finally:
        runtime_config["max_concurrent_compute_jobs_by_project"] = orig

    assert job is not None
    assert job.project_id == project_b.id

