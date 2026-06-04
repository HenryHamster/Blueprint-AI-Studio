"""End-to-end lifecycle tests for the compute_experiment task type.

The compute_experiment type is the reference proof that long-running compute
work is represented honestly by the shared task capability contract. These
tests drive a compute_experiment task across its full lifecycle — a budgeted
ComputeJob is launched and polled by the substrate, the substrate enforces the
spend-cap / wall-clock-deadline kills, and the task's terminal COMPUTE_RESULT
artifact is gated by the registry-declared 'metric' forward gate via
``_handle_compute_experiment_completion``.

The substrate's job-processing proofs (spend-cap / deadline / teardown) live in
test_compute.py; here we assert those terminal outcomes surface *through the
task* and that an experiment which never reached a terminal, costed state FAILS
the forward gate rather than completing clean.
"""

import json
import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import compute_jobs
from app.compute_backends import (
    ComputeCostResult,
    ComputeLaunchResult,
    ComputeStatusResult,
)
from app.database import resolve_async_engine
from app.models import (
    ComputeJob,
    ComputeJobOutcome,
    ComputeJobStatus,
    Project,
    Task,
    TaskStatus,
)
from app.task_capabilities import (
    EVALUATION_STRATEGY_BY_TASK_TYPE,
    RUN_SHAPE_BY_TASK_TYPE,
    COMPLETION_HANDLER_BY_TASK_TYPE,
    get_task_capability,
)
from app.task_completion import _handle_compute_experiment_completion


@pytest.fixture
def _quiet_compute_io(mocker):
    mocker.patch(
        "app.task_completion.broadcast_to_project", new=AsyncMock(return_value=None)
    )


# ── Registry: the declaration is honest ─────────────────────────────────────


def test_compute_experiment_capability_is_honest():
    """The registry declaration matches the type's real behaviour: a
    compute-substrate experiment, graded by a metric forward gate."""
    cap = get_task_capability("compute_experiment")
    assert cap is not None
    assert cap.requires_code is False
    assert cap.requires_compute is True
    assert cap.run_shape == "experiment_loop"
    assert cap.output_kind == "verdict"
    assert cap.evaluation == "metric"
    assert cap.completion_handler == "compute_experiment"
    # Derived maps stay in sync (registry is the single source of truth).
    assert EVALUATION_STRATEGY_BY_TASK_TYPE["compute_experiment"] == "metric"
    assert RUN_SHAPE_BY_TASK_TYPE["compute_experiment"] == "experiment_loop"
    assert COMPLETION_HANDLER_BY_TASK_TYPE["compute_experiment"] == "compute_experiment"


# ── Completion handler: forward metric gate ─────────────────────────────────


async def _make_compute_task(db_session) -> Task:
    project = Project(name="ComputeExp", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="compute_experiment: run a budgeted job",
        status=TaskStatus.COMPLETED,  # agent reported terminal; handler decides
        payloads=[{"type": "compute_experiment", "base_branch": "main"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


def _result_artifact(**overrides) -> str:
    base = {
        "job_id": "job-abc",
        "outcome": "SUCCESS",
        "status": "SUCCEEDED",
        "final_cost_usd": 0.37,
        "accrued_cost_usd": 0.37,
        "intentional_terminal": True,
    }
    base.update(overrides)
    return json.dumps(base)


@pytest.mark.asyncio
async def test_compute_completion_success_passes_gate_and_completes(
    db_session, _quiet_compute_io
):
    task = await _make_compute_task(db_session)

    await _handle_compute_experiment_completion(task, _result_artifact(), db_session)

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    assert task.plan_output["compute_result"]["outcome"] == "SUCCESS"
    assert task.plan_output["compute_result"]["final_cost_usd"] == pytest.approx(0.37)
    assert "SUCCESS" in (task.latest_status_message or "")


@pytest.mark.asyncio
async def test_compute_completion_stamps_terminal_result_reference(
    db_session, _quiet_compute_io
):
    task = await _make_compute_task(db_session)
    task.status = TaskStatus.IN_PROGRESS
    db_session.add(task)
    await db_session.commit()

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="echo hi",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )
    job.status = ComputeJobStatus.SUCCEEDED
    job.outcome = ComputeJobOutcome.SUCCESS
    job.accrued_cost_usd = 0.37
    job.final_cost_usd = 0.37
    job.terminal_reason = "compute job finished"
    job.completed_at = datetime.utcnow()
    db_session.add(job)
    await db_session.commit()

    result_reference = "https://storage.example.com/jobs/job-abc/result.json"
    await _handle_compute_experiment_completion(
        task,
        _result_artifact(job_id=str(job.id), result_reference=result_reference),
        db_session,
    )

    await db_session.refresh(job)
    assert job.result_reference == result_reference


@pytest.mark.asyncio
async def test_compute_completion_spend_cap_kill_is_honest_terminal(
    db_session, _quiet_compute_io
):
    """A spend-cap kill is an intentional terminal outcome: it carries an outcome
    and a cost, so it passes the gate and surfaces through the task as COMPLETED
    with the kill outcome recorded — not a crash."""
    task = await _make_compute_task(db_session)

    await _handle_compute_experiment_completion(
        task,
        _result_artifact(outcome="SPEND_CAP_EXCEEDED", status="FAILED"),
        db_session,
    )

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    assert task.plan_output["compute_result"]["outcome"] == "SPEND_CAP_EXCEEDED"


@pytest.mark.asyncio
async def test_compute_completion_classifies_outcome_after_gate(
    db_session, _quiet_compute_io
):
    """The terminal classification of a completed compute_experiment matches the
    stored compute_result: a SUCCESS run is a success outcome while a spend-cap
    kill is neutral — tying the analytics verdict to the gated terminal result."""
    from app.task_outcomes import evaluate_task_outcome

    success = await _make_compute_task(db_session)
    await _handle_compute_experiment_completion(
        success, _result_artifact(), db_session
    )
    await db_session.refresh(success)
    assert evaluate_task_outcome(success).status == "success"

    spend_cap = await _make_compute_task(db_session)
    await _handle_compute_experiment_completion(
        spend_cap,
        _result_artifact(outcome="SPEND_CAP_EXCEEDED", status="FAILED"),
        db_session,
    )
    await db_session.refresh(spend_cap)
    assert evaluate_task_outcome(spend_cap).status == "neutral"


@pytest.mark.asyncio
async def test_compute_completion_deadline_kill_is_honest_terminal(
    db_session, _quiet_compute_io
):
    task = await _make_compute_task(db_session)

    await _handle_compute_experiment_completion(
        task,
        _result_artifact(outcome="TIMED_OUT", status="TIMED_OUT"),
        db_session,
    )

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    assert task.plan_output["compute_result"]["outcome"] == "TIMED_OUT"


@pytest.mark.asyncio
async def test_compute_completion_no_terminal_outcome_fails_gate(
    db_session, _quiet_compute_io
):
    """An experiment that produced no terminal job outcome must FAIL the metric
    gate (terminal FAILED), not complete clean on the agent's say-so."""
    task = await _make_compute_task(db_session)

    payload = json.dumps({"job_id": "job-abc", "final_cost_usd": 0.1})
    await _handle_compute_experiment_completion(task, payload, db_session)

    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED
    assert task.context["evaluation_gate"]["passed"] is False
    assert task.context["evaluation_gate"]["strategy"] == "metric"


@pytest.mark.asyncio
async def test_compute_completion_no_cost_fails_gate(db_session, _quiet_compute_io):
    """An experiment that reached a terminal outcome but measured no cost must
    FAIL the gate — a compute experiment is a measurement-producing task."""
    task = await _make_compute_task(db_session)

    payload = json.dumps({"job_id": "job-abc", "outcome": "SUCCESS"})
    await _handle_compute_experiment_completion(task, payload, db_session)

    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED
    assert task.context["evaluation_gate"]["passed"] is False


@pytest.mark.asyncio
async def test_compute_completion_unparseable_fails_gate(
    db_session, _quiet_compute_io
):
    task = await _make_compute_task(db_session)

    await _handle_compute_experiment_completion(
        task, "the job crashed before emitting anything", db_session
    )

    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED
    assert task.context["evaluation_gate"]["strategy"] == "metric"


# ── Full lifecycle: substrate kill → task completion ────────────────────────


def _result_from_terminal_job(job: ComputeJob, polls: int = 1) -> str:
    """Build the COMPUTE_RESULT artifact the agent emits from a terminal job.

    Mirrors ``agent/src/handlers/compute-experiment.ts``: outcome is the job's
    terminal outcome, final cost prefers final_cost_usd then accrued_cost_usd.
    """
    def _v(x):
        return getattr(x, "value", x)

    outcome = _v(job.outcome) if job.outcome is not None else None
    final_cost = job.final_cost_usd if job.final_cost_usd is not None else job.accrued_cost_usd
    return json.dumps(
        {
            "job_id": str(job.id),
            "status": _v(job.status) if job.status is not None else None,
            "outcome": outcome,
            "final_cost_usd": final_cost,
            "accrued_cost_usd": job.accrued_cost_usd,
            "terminal_reason": job.terminal_reason,
            "poll_attempts": polls,
        }
    )


async def _drive_job_to_terminal(db_session, monkeypatch, job, backend):
    monkeypatch.setattr(compute_jobs, "get_compute_backend", lambda _name: backend)
    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )
    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)
    await db_session.refresh(job)


@pytest.mark.asyncio
async def test_lifecycle_spend_cap_kill_surfaces_through_task(
    db_session, monkeypatch, _quiet_compute_io
):
    """End-to-end: substrate enforces the spend cap and kills the job; the kill
    surfaces through the compute_experiment task as an honest terminal COMPLETED
    outcome (not just at the job-processor level)."""
    task = await _make_compute_task(db_session)
    task.status = TaskStatus.IN_PROGRESS
    db_session.add(task)
    await db_session.commit()

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="sleep 10",
        machine_type="e2-standard-2",
        spend_cap_usd=0.02,
        deadline_seconds=300,
    )
    job.status = ComputeJobStatus.RUNNING
    job.external_compute_id = "fake-cap-kill"
    job.started_at = datetime.utcnow() - timedelta(seconds=60)
    db_session.add(job)
    await db_session.commit()

    class FakeBackend:
        async def launch(self, spec):
            raise AssertionError("launch should not be called")

        async def poll_cost(self, persisted_job, *, as_of):
            return ComputeCostResult(total_cost_usd=0.05, measured_at=as_of)

        async def poll_status(self, persisted_job):
            return ComputeStatusResult(status=ComputeJobStatus.RUNNING)

        async def terminate(self, persisted_job, *, reason):
            return True

    await _drive_job_to_terminal(db_session, monkeypatch, job, FakeBackend())
    assert job.outcome == ComputeJobOutcome.SPEND_CAP_EXCEEDED

    # The agent observes the terminal job and emits a COMPUTE_RESULT; the task's
    # completion gate runs on it.
    await _handle_compute_experiment_completion(
        task, _result_from_terminal_job(job), db_session
    )

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    assert task.plan_output["compute_result"]["outcome"] == "SPEND_CAP_EXCEEDED"


@pytest.mark.asyncio
async def test_lifecycle_deadline_kill_surfaces_through_task(
    db_session, monkeypatch, _quiet_compute_io
):
    """End-to-end: substrate enforces the wall-clock deadline; the deadline kill
    surfaces through the task as an honest terminal COMPLETED outcome."""
    task = await _make_compute_task(db_session)
    task.status = TaskStatus.IN_PROGRESS
    db_session.add(task)
    await db_session.commit()

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="sleep 10",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=60,
    )
    job.status = ComputeJobStatus.RUNNING
    job.external_compute_id = "fake-deadline-kill"
    job.started_at = datetime.utcnow() - timedelta(seconds=120)
    job.wall_clock_deadline = datetime.utcnow() - timedelta(seconds=1)
    db_session.add(job)
    await db_session.commit()

    class FakeBackend:
        async def launch(self, spec):
            raise AssertionError("launch should not be called")

        async def poll_cost(self, persisted_job, *, as_of):
            return ComputeCostResult(total_cost_usd=0.01, measured_at=as_of)

        async def poll_status(self, persisted_job):
            return ComputeStatusResult(status=ComputeJobStatus.RUNNING)

        async def terminate(self, persisted_job, *, reason):
            return True

    await _drive_job_to_terminal(db_session, monkeypatch, job, FakeBackend())
    assert job.outcome == ComputeJobOutcome.TIMED_OUT

    await _handle_compute_experiment_completion(
        task, _result_from_terminal_job(job), db_session
    )

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    assert task.plan_output["compute_result"]["outcome"] == "TIMED_OUT"


@pytest.mark.asyncio
async def test_lifecycle_success_completes_through_task(
    db_session, monkeypatch, _quiet_compute_io
):
    """End-to-end happy path: a job runs to SUCCESS and the task passes the gate."""
    task = await _make_compute_task(db_session)
    task.status = TaskStatus.IN_PROGRESS
    db_session.add(task)
    await db_session.commit()

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="echo hi",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )
    started_at = datetime.utcnow() - timedelta(seconds=30)

    class FakeBackend:
        def external_id_for(self, persisted_job):
            return "fake-success"

        async def launch(self, spec):
            return ComputeLaunchResult(
                external_id="fake-success",
                started_at=started_at,
                cost_rate_per_second=0.001,
                message="launched",
            )

        async def poll_cost(self, persisted_job, *, as_of):
            return ComputeCostResult(total_cost_usd=0.03, measured_at=as_of)

        async def poll_status(self, persisted_job):
            return ComputeStatusResult(
                status=ComputeJobStatus.SUCCEEDED,
                exit_code=0,
                finished_at=datetime.utcnow(),
                message="completed",
            )

        async def terminate(self, persisted_job, *, reason):
            raise AssertionError("terminate should not be called")

    backend = FakeBackend()
    monkeypatch.setattr(compute_jobs, "get_compute_backend", lambda _name: backend)
    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )
    # Two passes: STARTING→launch, then RUNNING→poll-to-terminal.
    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)
    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)
    await db_session.refresh(job)
    assert job.outcome == ComputeJobOutcome.SUCCESS

    await _handle_compute_experiment_completion(
        task, _result_from_terminal_job(job), db_session
    )

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    assert task.plan_output["compute_result"]["outcome"] == "SUCCESS"
    assert task.plan_output["compute_result"]["final_cost_usd"] == pytest.approx(0.03)
