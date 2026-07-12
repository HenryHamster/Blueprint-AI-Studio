"""Durable TaskHistory reason rows for non-success compute-job terminals.

``terminal_reason`` otherwise lives only in mutable channels
(``task.latest_status_message`` / ``polling_metadata.status_message``) that the
next reap or status update overwrites. These tests pin the durable channel:
every non-success terminal transition (deadline kill, spend cap, provisioning
timeout, runner-reported failure, cancellation) appends a TaskHistory row
(``action="COMPUTE_TERMINATED"``) carrying the job's terminal_reason — while a
clean SUCCESS stays silent (the reason channel is for failures, not a
completion log).
"""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import compute_jobs
from app.compute_backends import ComputeStatusResult
from app.database import resolve_async_engine
from app.models import (
    ComputeJobOutcome,
    ComputeJobStatus,
    Project,
    Task,
    TaskHistory,
    TaskStatus,
)
from app.time_utils import _utcnow


async def _make_task_and_job(db_session, *, task_status=TaskStatus.PARKED):
    project = Project(name="ComputeReasonHistory", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, status=task_status)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="sleep 999",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )
    job.status = ComputeJobStatus.RUNNING
    job.started_at = _utcnow() - timedelta(minutes=5)
    job.external_compute_id = f"vm-{uuid.uuid4().hex[:8]}"
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)
    return task, job


def _session_factory(db_session):
    return async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )


async def _terminated_rows(db_session, task_id):
    rows = (
        (
            await db_session.execute(
                select(TaskHistory).where(TaskHistory.task_id == task_id)
            )
        )
        .scalars()
        .all()
    )
    return [r for r in rows if r.action == "COMPUTE_TERMINATED"]


@pytest.mark.asyncio
async def test_deadline_kill_appends_durable_reason_history(db_session):
    """A monitor deadline kill leaves a TaskHistory row naming WHY — the reason
    survives even after later status messages overwrite the mutable channel."""
    task, job = await _make_task_and_job(db_session)

    applied = await compute_jobs._apply_polled_compute_job_result(
        _session_factory(db_session),
        job.id,
        now=_utcnow(),
        poll_attempt_count=1,
        next_poll_at=_utcnow() + timedelta(seconds=15),
        status=ComputeStatusResult(status=ComputeJobStatus.FAILED),
        accrued_cost_usd=0.5,
        terminal_status=ComputeJobStatus.TIMED_OUT,
        outcome=ComputeJobOutcome.TIMED_OUT,
        terminal_reason="compute job exceeded runtime deadline",
    )
    assert applied is True

    rows = await _terminated_rows(db_session, task.id)
    assert len(rows) == 1
    details = rows[0].details
    assert details["terminal_reason"] == "compute job exceeded runtime deadline"
    assert details["outcome"] == "TIMED_OUT"
    assert details["status"] == "TIMED_OUT"
    assert details["compute_job_id"] == str(job.id)


@pytest.mark.asyncio
async def test_runner_reported_failure_history_carries_exit_code(db_session):
    """A runner-reported failure records the reason AND the exit code the
    finalizer stamped into polling_metadata."""
    task, job = await _make_task_and_job(db_session)

    applied = await compute_jobs._apply_polled_compute_job_result(
        _session_factory(db_session),
        job.id,
        now=_utcnow(),
        poll_attempt_count=1,
        next_poll_at=_utcnow() + timedelta(seconds=15),
        status=ComputeStatusResult(status=ComputeJobStatus.FAILED),
        accrued_cost_usd=0.1,
        terminal_status=ComputeJobStatus.FAILED,
        outcome=ComputeJobOutcome.FAILURE,
        terminal_reason="command exited with code 1",
        exit_code=1,
    )
    assert applied is True

    rows = await _terminated_rows(db_session, task.id)
    assert len(rows) == 1
    details = rows[0].details
    assert details["terminal_reason"] == "command exited with code 1"
    assert details["outcome"] == "FAILURE"
    assert details["exit_code"] == 1


@pytest.mark.asyncio
async def test_success_terminal_appends_no_reason_history(db_session):
    """SUCCESS is skipped — the durable reason channel is for non-success
    terminals, not a completion log."""
    task, job = await _make_task_and_job(db_session)

    applied = await compute_jobs._apply_polled_compute_job_result(
        _session_factory(db_session),
        job.id,
        now=_utcnow(),
        poll_attempt_count=1,
        next_poll_at=_utcnow() + timedelta(seconds=15),
        status=ComputeStatusResult(status=ComputeJobStatus.SUCCEEDED),
        accrued_cost_usd=0.2,
        terminal_status=ComputeJobStatus.SUCCEEDED,
        outcome=ComputeJobOutcome.SUCCESS,
        terminal_reason="compute job finished",
        exit_code=0,
    )
    assert applied is True

    assert await _terminated_rows(db_session, task.id) == []


@pytest.mark.asyncio
async def test_task_terminal_cancellation_appends_reason_history(db_session):
    """The owning-task-terminal cancellation sweep also records WHY the job was
    cancelled."""
    # create_compute_job refuses terminal tasks, so mirror the real flow: the
    # job launches against a live task, THEN the task goes terminal, THEN the
    # sweep cancels the orphaned job.
    task, job = await _make_task_and_job(db_session)
    task.status = TaskStatus.FAILED
    db_session.add(task)
    await db_session.commit()

    ok = await compute_jobs._apply_task_terminal_cancellation(
        _session_factory(db_session),
        job.id,
        now=_utcnow(),
        terminate_ok=True,
    )
    assert ok is True

    rows = await _terminated_rows(db_session, task.id)
    assert len(rows) == 1
    details = rows[0].details
    assert details["outcome"] == "CANCELLED"
    assert "task is terminal" in details["terminal_reason"]
