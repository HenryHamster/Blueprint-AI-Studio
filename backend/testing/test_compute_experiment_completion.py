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
import os
import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import compute_jobs
from app.artifact_prerequisites import (
    ARTIFACT_PREREQUISITES_KEY,
    backfill_artifact_prerequisites,
    deliver_artifact_to_dependents,
)
from app.compute_experiment_contract import (
    COMPUTE_EXPERIMENT_PREREQUISITE_MODE_ADVISORY,
    COMPUTE_EXPERIMENT_PREREQUISITE_MODE_CONTEXT_KEY,
)
from app.compute_backends import (
    ComputeCostResult,
    ComputeLaunchResult,
    ComputeStatusResult,
)
from app.database import resolve_async_engine
from app.models import (
    Artifact,
    ArtifactType,
    ComputeJob,
    ComputeJobOutcome,
    ComputeJobStatus,
    KnowledgeDocument,
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
from app.knowledge_completion import KNOWLEDGE_PREREQUISITES_KEY
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
    # An https:// reference must not create an Artifact row even when the task
    # carries no downstream_artifact acceptance_evidence.
    assert job.artifact_id is None


@pytest.mark.asyncio
async def test_compute_downstream_artifact_rejects_transient_result_reference(
    db_session, _quiet_compute_io
):
    """A transient https:// result_reference must not create an Artifact row or
    deliver a prerequisite pointer to dependents, even when the task declares a
    downstream_artifact logical key.  The reference is still recorded on
    job.result_reference for diagnostics.
    """
    task = await _make_compute_task(db_session)
    task.status = TaskStatus.IN_PROGRESS
    task.payloads = [
        {
            "type": "compute_experiment",
            "base_branch": "main",
            "acceptance_evidence": {
                "kind": "downstream_artifact",
                "artifact_id": "experiment-result-bundle",
            },
        }
    ]
    dependent = Task(
        project_id=task.project_id,
        title="feat: consume experiment bundle",
        status=TaskStatus.QUEUED,
        payloads=[{"type": "create", "prompt": "use result", "base_branch": "main"}],
        depends_on=[str(task.id)],
    )
    db_session.add(task)
    db_session.add(dependent)
    await db_session.commit()
    await db_session.refresh(task)
    await db_session.refresh(dependent)

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

    transient_ref = "https://signed.example.com/jobs/abc/result.json?token=xyz"
    await compute_jobs.stamp_compute_job_result_reference(
        db_session,
        task_id=task.id,
        job_id=job.id,
        result_reference=transient_ref,
    )
    await db_session.commit()

    await db_session.refresh(job)
    # Diagnostic reference is always recorded.
    assert job.result_reference == transient_ref
    # No Artifact row created for a transient https:// reference.
    assert job.artifact_id is None

    await db_session.refresh(dependent)
    prereqs = (dependent.context or {}).get(ARTIFACT_PREREQUISITES_KEY)
    assert prereqs is None, (
        "transient https:// result_reference must not deliver artifact prerequisites "
        "to dependents"
    )


@pytest.mark.asyncio
async def test_deliver_artifact_no_partial_commit_on_scan_failure(
    db_session, _quiet_compute_io
):
    task = await _make_compute_task(db_session)
    task.status = TaskStatus.IN_PROGRESS
    task.payloads = [
        {
            "type": "compute_experiment",
            "base_branch": "main",
            "acceptance_evidence": {
                "kind": "downstream_artifact",
                "artifact_id": "experiment-result-bundle",
            },
        }
    ]
    dependent = Task(
        project_id=task.project_id,
        title="feat: consume experiment bundle",
        status=TaskStatus.QUEUED,
        payloads=[{"type": "create", "prompt": "use result", "base_branch": "main"}],
        depends_on=[str(task.id)],
    )
    db_session.add(task)
    db_session.add(dependent)
    await db_session.commit()
    await db_session.refresh(task)
    await db_session.refresh(dependent)

    artifact = Artifact(
        task_id=task.id,
        artifact_type=ArtifactType.COMPUTE_RESULT,
        logical_key="experiment-result-bundle",
        storage_uri="gs://blueprint-agent-artifacts/compute-jobs/result.json",
    )
    db_session.add(artifact)
    await db_session.commit()
    await db_session.refresh(artifact)

    first_page = MagicMock()
    first_page.scalars.return_value.all.return_value = [dependent]
    with patch("app.artifact_prerequisites._DEPENDENT_SCAN_CAP", 1), patch.object(
        db_session,
        "execute",
        new=AsyncMock(side_effect=[first_page, RuntimeError("scan boom")]),
    ):
        await deliver_artifact_to_dependents(task, artifact, db_session, commit=True)

    await db_session.refresh(dependent)
    assert (dependent.context or {}).get(ARTIFACT_PREREQUISITES_KEY) is None


@pytest.mark.asyncio
async def test_compute_result_artifact_logical_key_delivered_to_dependent(
    db_session, _quiet_compute_io
):
    task = await _make_compute_task(db_session)
    task.status = TaskStatus.IN_PROGRESS
    task.payloads = [
        {
            "type": "compute_experiment",
            "base_branch": "main",
            "acceptance_evidence": {
                "kind": "downstream_artifact",
                "artifact_id": "experiment-result-bundle",
            },
        }
    ]
    dependent = Task(
        project_id=task.project_id,
        title="feat: consume experiment bundle",
        status=TaskStatus.QUEUED,
        payloads=[{"type": "create", "prompt": "use result", "base_branch": "main"}],
        depends_on=[str(task.id)],
    )
    db_session.add(task)
    db_session.add(dependent)
    await db_session.commit()
    await db_session.refresh(task)
    await db_session.refresh(dependent)

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

    result_reference = "gs://blueprint-agent-artifacts/compute-jobs/result.json"
    await _handle_compute_experiment_completion(
        task,
        _result_artifact(job_id=str(job.id), result_reference=result_reference),
        db_session,
    )

    await db_session.refresh(job)
    artifact = await db_session.get(Artifact, job.artifact_id)
    assert artifact is not None
    assert artifact.artifact_type == ArtifactType.COMPUTE_RESULT
    assert artifact.logical_key == "experiment-result-bundle"
    assert artifact.storage_uri == result_reference

    await db_session.refresh(dependent)
    prereqs = (dependent.context or {}).get(ARTIFACT_PREREQUISITES_KEY)
    assert prereqs == [
        {
            "artifact_id": str(artifact.id),
            "logical_key": "experiment-result-bundle",
            "artifact_type": ArtifactType.COMPUTE_RESULT.value,
            "source_task_id": str(task.id),
            "storage_uri": result_reference,
        }
    ]


@pytest.mark.asyncio
async def test_hollow_compute_knowledge_does_not_deliver_to_dependents(
    db_session, _quiet_compute_io
):
    from app.knowledge_completion import persist_compute_experiment_knowledge

    project = Project(name="HollowComputeDelivery", user_id="test-user")
    task = Task(
        project_id=project.id,
        title="compute_experiment: hollow doc",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "compute_experiment", "base_branch": "main"}],
        context={"finding_hash": "compute-hollow-delivery"},
    )
    dependent = Task(
        project_id=project.id,
        title="feat: wait on compute knowledge",
        status=TaskStatus.BLOCKED,
        payloads=[{"type": "create", "prompt": "use result", "base_branch": "main"}],
        depends_on=[str(task.id)],
        context={
            "blocker": {
                "source": "agent",
                "waiting_on": "compute-hollow-delivery",
                "reason": "BLOCKER: waiting on compute knowledge",
                "need": {"kind": "task", "ref": "compute-hollow-delivery"},
            }
        },
    )
    db_session.add_all([task, dependent])
    await db_session.commit()
    await db_session.refresh(task)
    await db_session.refresh(dependent)

    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
        doc = await persist_compute_experiment_knowledge(
            task,
            outcome="SUCCESS",
            cost=0.11,
            criterion=None,
            result_reference="https://signed.example.com/jobs/result.json",
            session=db_session,
            logs=None,
        )

    assert doc is not None
    await db_session.refresh(dependent)
    assert (dependent.context or {}).get(KNOWLEDGE_PREREQUISITES_KEY) is None
    assert dependent.status == TaskStatus.BLOCKED


@pytest.mark.asyncio
async def test_grounded_negative_compute_knowledge_delivers_to_dependents(
    db_session, _quiet_compute_io
):
    from app.knowledge_completion import persist_compute_experiment_knowledge

    project = Project(name="NegativeComputeDelivery", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="compute_experiment: negative result",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "compute_experiment", "base_branch": "main"}],
        context={"finding_hash": "compute-negative-delivery"},
    )
    dependent = Task(
        project_id=project.id,
        title="feat: consume negative compute result",
        status=TaskStatus.BLOCKED,
        payloads=[{"type": "create", "prompt": "use result", "base_branch": "main"}],
        depends_on=[str(task.id)],
        context={
            "blocker": {
                "source": "agent",
                "waiting_on": "compute-negative-delivery",
                "reason": "BLOCKER: waiting on compute knowledge",
                "need": {"kind": "task", "ref": "compute-negative-delivery"},
            }
        },
    )
    artifact_uri = "gs://blueprint-agent-artifacts/compute-jobs/result.json"
    artifact = Artifact(
        task_id=task.id,
        artifact_type=ArtifactType.COMPUTE_RESULT,
        logical_key="experiment-result-bundle",
        storage_uri=artifact_uri,
    )
    job = ComputeJob(
        project_id=project.id,
        task_id=task.id,
        backend="local",
        status=ComputeJobStatus.SUCCEEDED,
        artifact_id=None,
        result_reference=artifact_uri,
    )
    db_session.add_all([task, dependent, artifact, job])
    await db_session.commit()
    await db_session.refresh(task)
    await db_session.refresh(dependent)
    await db_session.refresh(artifact)
    await db_session.refresh(job)
    job.artifact_id = artifact.id
    job.outcome = ComputeJobOutcome.SPEND_CAP_EXCEEDED
    job.accrued_cost_usd = 0.37
    job.final_cost_usd = 0.37
    job.terminal_reason = "compute job finished"
    job.completed_at = datetime.utcnow()
    db_session.add(job)
    await db_session.commit()

    logs_json = json.dumps({"job_id": str(job.id), "outcome": "SPEND_CAP_EXCEEDED"})

    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
        doc = await persist_compute_experiment_knowledge(
            task,
            outcome="SPEND_CAP_EXCEEDED",
            cost=0.37,
            criterion={"metric": "accuracy", "op": ">=", "value": 0.9},
            result_reference=artifact_uri,
            session=db_session,
            logs=logs_json,
        )

    assert doc is not None
    assert doc.forward_quality_verdict["analysis_mode"] == "fallback"
    assert artifact_uri in doc.source_citations

    await db_session.refresh(dependent)
    prereqs = (dependent.context or {}).get(KNOWLEDGE_PREREQUISITES_KEY)
    assert prereqs and prereqs[0]["source_task_id"] == str(task.id)
    assert dependent.status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_late_compute_artifact_dependent_is_hydrated_before_launch(
    db_session, _quiet_compute_io
):
    """A dependent task queued AFTER its producer completed must receive artifact
    prerequisite pointers via backfill_artifact_prerequisites at claim time.

    Acceptance gate: the test must call backfill_artifact_prerequisites against
    a completed producer and assert the pointer lands in the dependent's context
    with the correct shape.  This exercises the production code path that fires
    during claim_task for late-arriving dependents.
    """
    project = Project(name="BackfillTest", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    producer = Task(
        project_id=project.id,
        title="compute_experiment: measure GPU",
        status=TaskStatus.COMPLETED,
        payloads=[
            {
                "type": "compute_experiment",
                "base_branch": "main",
                "acceptance_evidence": {
                    "kind": "downstream_artifact",
                    "artifact_id": "experiment-result-bundle",
                },
            }
        ],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(producer)
    await db_session.commit()
    await db_session.refresh(producer)

    artifact = Artifact(
        task_id=producer.id,
        artifact_type=ArtifactType.COMPUTE_RESULT,
        logical_key="experiment-result-bundle",
        storage_uri="gs://blueprint-agent-artifacts/compute-jobs/result.json",
    )
    db_session.add(artifact)
    await db_session.commit()
    await db_session.refresh(artifact)

    # Dependent created AFTER producer completion — push path never fired.
    dependent = Task(
        project_id=project.id,
        title="feat: consume experiment bundle",
        status=TaskStatus.QUEUED,
        payloads=[{"type": "create", "prompt": "use result", "base_branch": "main"}],
        depends_on=[str(producer.id)],
    )
    db_session.add(dependent)
    await db_session.commit()
    await db_session.refresh(dependent)

    # Confirm no prerequisites exist yet (push path did not fire).
    assert (dependent.context or {}).get(ARTIFACT_PREREQUISITES_KEY) is None

    # Backfill fires at claim time.
    await backfill_artifact_prerequisites(dependent, db_session)
    await db_session.commit()
    await db_session.refresh(dependent)

    prereqs = (dependent.context or {}).get(ARTIFACT_PREREQUISITES_KEY)
    assert prereqs is not None, "backfill must populate artifact_prerequisites"
    assert len(prereqs) == 1
    assert prereqs[0]["artifact_id"] == str(artifact.id)
    assert prereqs[0]["logical_key"] == "experiment-result-bundle"
    assert prereqs[0]["artifact_type"] == ArtifactType.COMPUTE_RESULT.value
    assert prereqs[0]["source_task_id"] == str(producer.id)
    assert prereqs[0]["storage_uri"] == "gs://blueprint-agent-artifacts/compute-jobs/result.json"


@pytest.mark.asyncio
async def test_backfill_artifact_prerequisites_idempotent(
    db_session, _quiet_compute_io
):
    """Calling backfill_artifact_prerequisites twice must not duplicate pointers."""
    project = Project(name="BackfillIdempotent", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    producer = Task(
        project_id=project.id,
        title="compute_experiment: measure GPU",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "compute_experiment", "base_branch": "main"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(producer)
    await db_session.commit()
    await db_session.refresh(producer)

    artifact = Artifact(
        task_id=producer.id,
        artifact_type=ArtifactType.COMPUTE_RESULT,
        logical_key="idempotent-bundle",
        storage_uri="gs://blueprint-agent-artifacts/compute-jobs/result.json",
    )
    db_session.add(artifact)
    await db_session.commit()
    await db_session.refresh(artifact)

    dependent = Task(
        project_id=project.id,
        title="feat: consume bundle",
        status=TaskStatus.QUEUED,
        payloads=[{"type": "create", "prompt": "use result", "base_branch": "main"}],
        depends_on=[str(producer.id)],
    )
    db_session.add(dependent)
    await db_session.commit()
    await db_session.refresh(dependent)

    await backfill_artifact_prerequisites(dependent, db_session)
    await db_session.commit()
    await backfill_artifact_prerequisites(dependent, db_session)
    await db_session.commit()
    await db_session.refresh(dependent)

    prereqs = (dependent.context or {}).get(ARTIFACT_PREREQUISITES_KEY)
    assert prereqs is not None
    assert len(prereqs) == 1, "second backfill must not add a duplicate pointer"


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
    stored compute_result: a measured-only SUCCESS is neutral while a spend-cap
    kill is neutral — tying the analytics verdict to the gated terminal result."""
    from app.task_outcomes import evaluate_task_outcome

    success = await _make_compute_task(db_session)
    await _handle_compute_experiment_completion(
        success, _result_artifact(), db_session
    )
    await db_session.refresh(success)
    assert evaluate_task_outcome(success).status == "neutral"

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


async def _make_compute_task_with_criterion(db_session, metric, op, threshold) -> Task:
    task = await _make_compute_task(db_session)
    task.payloads = [
        {
            "type": "compute_experiment",
            "base_branch": "main",
            "success_criterion": {
                "metric": metric,
                "operator": op,
                "threshold": threshold,
            },
        }
    ]
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


@pytest.mark.asyncio
async def test_compute_completion_met_criterion_completes_and_records_goal(
    db_session, _quiet_compute_io
):
    """A declared criterion on a MEASURABLE (job-level) metric the result meets
    PASSES the gate, completes the task, and records the goal outcome on the
    durable compute_result for the summary.

    Uses ``final_cost_usd`` — a key the real terminal COMPUTE_RESULT actually
    carries — rather than an experiment-domain metric the agent never emits."""
    task = await _make_compute_task_with_criterion(
        db_session, "final_cost_usd", "<=", 0.9
    )

    await _handle_compute_experiment_completion(
        task, _result_artifact(final_cost_usd=0.42), db_session
    )

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    criterion = task.plan_output["compute_result"]["criterion"]
    assert criterion["met"] is True
    assert criterion["metric"] == "final_cost_usd"
    assert criterion["value"] == pytest.approx(0.42)
    # Durable criterion verdict is also recorded on task.context for consumers.
    ctx_criterion = task.context["compute_success_criterion"]
    assert ctx_criterion["met"] is True
    assert ctx_criterion["measurable"] is True


@pytest.mark.asyncio
async def test_compute_completion_reported_metric_met_records_goal(
    db_session, _quiet_compute_io
):
    """A command-reported experiment metric (accuracy) flattened to a top-level
    key that MEETS its declared goal PASSES, completes the task, and records the
    met verdict durably on both plan_output and task.context."""
    task = await _make_compute_task_with_criterion(
        db_session, "accuracy", ">=", 0.9
    )

    await _handle_compute_experiment_completion(
        task, _result_artifact(accuracy=0.93), db_session
    )

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    criterion = task.plan_output["compute_result"]["criterion"]
    assert criterion["met"] is True
    assert criterion["measurable"] is True
    assert criterion["value"] == pytest.approx(0.93)
    assert task.context["compute_success_criterion"]["met"] is True


@pytest.mark.asyncio
async def test_compute_completion_reported_metric_unmet_fails_gate(
    db_session, _quiet_compute_io
):
    """A reported experiment metric that MISSES its declared goal FAILS the gate
    and records the unmet criterion durably on task.context."""
    task = await _make_compute_task_with_criterion(
        db_session, "accuracy", ">=", 0.9
    )

    await _handle_compute_experiment_completion(
        task, _result_artifact(accuracy=0.83), db_session
    )

    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED
    assert task.context["evaluation_gate"]["passed"] is False
    assert "not met" in task.context["evaluation_gate"]["reason"]
    ctx_criterion = task.context["compute_success_criterion"]
    assert ctx_criterion["met"] is False
    assert ctx_criterion["measurable"] is True
    assert ctx_criterion["value"] == pytest.approx(0.83)


@pytest.mark.asyncio
async def test_compute_completion_unmet_criterion_fails_gate(
    db_session, _quiet_compute_io
):
    """An experiment that finishes but misses its declared goal on a MEASURABLE
    metric FAILS the gate honestly with an operator-readable reason, rather than
    completing clean."""
    task = await _make_compute_task_with_criterion(
        db_session, "final_cost_usd", "<=", 5.0
    )

    await _handle_compute_experiment_completion(
        task, _result_artifact(final_cost_usd=10.0), db_session
    )

    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED
    assert task.context["evaluation_gate"]["passed"] is False
    assert "not met" in task.context["evaluation_gate"]["reason"]
    assert task.context["compute_success_criterion"]["met"] is False


@pytest.mark.asyncio
async def test_compute_completion_absent_metric_fails_closed_and_records(
    db_session, _quiet_compute_io
):
    """A declared experiment-domain metric the command did NOT report (absent from
    the terminal artifact) FAILS the gate CLOSED — it must NOT silently complete
    as 'unmeasurable'. The durable criterion records the metric was not
    measurable (absent) and the gate reason is operator-readable."""
    task = await _make_compute_task_with_criterion(db_session, "accuracy", ">=", 0.9)

    # Real terminal artifact shape: no 'accuracy' key reported.
    await _handle_compute_experiment_completion(task, _result_artifact(), db_session)

    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED
    assert task.context["evaluation_gate"]["passed"] is False
    assert "not evaluable" in task.context["evaluation_gate"]["reason"]
    ctx_criterion = task.context["compute_success_criterion"]
    assert ctx_criterion["measurable"] is False
    assert ctx_criterion["met"] is False
    assert ctx_criterion["metric"] == "accuracy"
    assert ctx_criterion["value"] is None


# ── Durable knowledge: the verdict survives as a queryable KnowledgeDocument ──


async def _knowledge_docs_for(db_session, task) -> list[KnowledgeDocument]:
    return (
        (
            await db_session.execute(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.source_task_id == task.id
                )
            )
        )
        .scalars()
        .all()
    )


async def _make_compute_task_with_identity(
    db_session, *, metric, op, threshold, direction_id="d-compute"
) -> Task:
    """A compute task carrying benchmark identity + a strategic direction."""
    task = await _make_compute_task(db_session)
    task.payloads = [
        {
            "type": "compute_experiment",
            "base_branch": "main",
            "experiment": "X speeds up Y",
            "benchmark": "bench-suite-v2",
            "tags": ["perf"],
            "success_criterion": {
                "metric": metric,
                "operator": op,
                "threshold": threshold,
            },
        }
    ]
    task.context = {"direction_id": direction_id}
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


@pytest.mark.asyncio
async def test_compute_success_persists_one_knowledge_document(
    db_session, _quiet_compute_io
):
    """A completed experiment whose criterion is met persists exactly ONE
    KnowledgeDocument carrying the verdict: source_kind, outcome, metric/value,
    criterion-met flag, cost, source_task_id, and direction_id."""
    task = await _make_compute_task_with_identity(
        db_session, metric="accuracy", op=">=", threshold=0.9
    )

    await _handle_compute_experiment_completion(
        task, _result_artifact(accuracy=0.93), db_session
    )

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    docs = await _knowledge_docs_for(db_session, task)
    assert len(docs) == 1
    doc = docs[0]
    assert doc.source_kind == "compute_experiment"
    assert doc.source_task_id == task.id
    assert doc.direction_id == "d-compute"
    # The verdict (outcome, metric/value, criterion-met, cost) is recorded.
    verdict = doc.forward_quality_verdict
    assert verdict["outcome"] == "SUCCESS"
    assert verdict["criterion_met"] is True
    assert verdict["criterion"]["metric"] == "accuracy"
    assert verdict["criterion"]["value"] == pytest.approx(0.93)
    assert verdict["cost_usd"] == pytest.approx(0.37)
    # The metric and its value surface in human-readable summary text.
    assert "accuracy" in doc.summary
    # plan_output mirrors a compact copy WITHOUT clobbering compute_result.
    assert task.plan_output["compute_result"]["outcome"] == "SUCCESS"
    assert task.plan_output["knowledge_document"]["source_kind"] == "compute_experiment"


@pytest.mark.asyncio
async def test_compute_unmet_criterion_persists_honest_negative(
    db_session, _quiet_compute_io
):
    """An experiment that finishes but misses its declared criterion FAILS the
    gate yet still records an honest negative result as knowledge ("we tried X
    and it did NOT meet the bar"), rather than silently dropping it."""
    task = await _make_compute_task_with_identity(
        db_session, metric="accuracy", op=">=", threshold=0.9
    )

    await _handle_compute_experiment_completion(
        task, _result_artifact(accuracy=0.42), db_session
    )

    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED
    docs = await _knowledge_docs_for(db_session, task)
    assert len(docs) == 1
    doc = docs[0]
    assert doc.source_kind == "compute_experiment"
    assert doc.forward_quality_verdict["criterion_met"] is False
    assert doc.forward_quality_verdict["criterion"]["value"] == pytest.approx(0.42)
    assert "NOT met" in doc.summary


@pytest.mark.asyncio
async def test_compute_success_without_criterion_persists_document(
    db_session, _quiet_compute_io
):
    """An experiment with NO declared criterion that completes still persists its
    verdict (outcome + cost) as knowledge, with criterion_met recorded as null."""
    task = await _make_compute_task(db_session)  # no success_criterion in payload

    await _handle_compute_experiment_completion(task, _result_artifact(), db_session)

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    docs = await _knowledge_docs_for(db_session, task)
    assert len(docs) == 1
    assert docs[0].forward_quality_verdict["criterion_met"] is None
    assert docs[0].forward_quality_verdict["outcome"] == "SUCCESS"


@pytest.mark.asyncio
async def test_compute_no_measurement_persists_no_document(
    db_session, _quiet_compute_io
):
    """A gate failure with no terminal measurement (no outcome / no cost) has no
    verdict to record and persists NO knowledge document."""
    task = await _make_compute_task(db_session)

    payload = json.dumps({"job_id": "job-abc", "final_cost_usd": 0.1})
    await _handle_compute_experiment_completion(task, payload, db_session)

    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED
    assert await _knowledge_docs_for(db_session, task) == []


@pytest.mark.asyncio
async def test_compute_knowledge_is_discoverable_as_prior_knowledge(
    db_session, _quiet_compute_io
):
    """The persisted verdict is retrievable through the SAME prior-knowledge path
    (build_knowledge_index / list_knowledge_documents) that injects
    web_research/design documents into future research/strategist prompts."""
    from app.knowledge_query import build_knowledge_index, list_knowledge_documents

    task = await _make_compute_task_with_identity(
        db_session, metric="accuracy", op=">=", threshold=0.9
    )
    await _handle_compute_experiment_completion(
        task, _result_artifact(accuracy=0.93), db_session
    )
    await db_session.refresh(task)

    index = await build_knowledge_index(db_session, task.project_id)
    assert any(e["source_kind"] == "compute_experiment" for e in index)

    listing = await list_knowledge_documents(
        db_session, task.project_id, source_kind="compute_experiment"
    )
    assert listing["total"] == 1
    assert listing["documents"][0]["source_task_id"] == str(task.id)


@pytest.mark.asyncio
async def test_compute_knowledge_delivered_to_queued_dependent(
    db_session, _quiet_compute_io
):
    """A compute_experiment's verdict is delivered to a QUEUED dependent that
    declared this experiment in depends_on, via the shared knowledge-prerequisite
    pointer machinery."""
    from app.knowledge_completion import KNOWLEDGE_PREREQUISITES_KEY

    task = await _make_compute_task_with_identity(
        db_session, metric="accuracy", op=">=", threshold=0.9
    )
    artifact_uri = "gs://blueprint-agent-artifacts/compute-jobs/result.json"
    artifact = Artifact(
        task_id=task.id,
        artifact_type=ArtifactType.COMPUTE_RESULT,
        logical_key="experiment-result-bundle",
        storage_uri=artifact_uri,
    )
    job = ComputeJob(
        project_id=task.project_id,
        task_id=task.id,
        backend="local",
        status=ComputeJobStatus.SUCCEEDED,
        artifact_id=None,
        result_reference=artifact_uri,
    )
    dependent = Task(
        project_id=task.project_id,
        title="feat: act on the experiment result",
        status=TaskStatus.QUEUED,
        payloads=[{"type": "create", "prompt": "use result", "base_branch": "main"}],
        depends_on=[str(task.id)],
    )
    db_session.add_all([artifact, job, dependent])
    await db_session.commit()
    await db_session.refresh(artifact)
    job.artifact_id = artifact.id
    job.outcome = ComputeJobOutcome.SUCCESS
    job.accrued_cost_usd = 0.37
    job.final_cost_usd = 0.37
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)
    await db_session.refresh(dependent)

    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
        await _handle_compute_experiment_completion(
            task,
            _result_artifact(
                accuracy=0.93,
                job_id=str(job.id),
                result_reference=artifact_uri,
            ),
            db_session,
        )

    await db_session.refresh(dependent)
    prereqs = (dependent.context or {}).get(KNOWLEDGE_PREREQUISITES_KEY)
    assert prereqs and prereqs[0]["source_kind"] == "compute_experiment"
    assert prereqs[0]["source_task_id"] == str(task.id)


@pytest.mark.asyncio
async def test_prerequisite_measured_negative_completes_and_delivers_while_broken_fails(
    db_session, _quiet_compute_io
):
    """Prerequisite mode treats the criterion as advisory, not as the gate.

    A measured negative result still completes and delivers durable knowledge to
    its queued dependent, while a broken run with no measured verdict still
    fails and delivers nothing.
    """
    from app.knowledge_completion import KNOWLEDGE_PREREQUISITES_KEY

    negative = await _make_compute_task_with_identity(
        db_session, metric="accuracy", op=">=", threshold=0.9
    )
    negative.context = {
        **(negative.context or {}),
        COMPUTE_EXPERIMENT_PREREQUISITE_MODE_CONTEXT_KEY: (
            COMPUTE_EXPERIMENT_PREREQUISITE_MODE_ADVISORY
        ),
    }
    neg_artifact_uri = "gs://blueprint-agent-artifacts/compute-jobs/negative-result.json"
    neg_artifact = Artifact(
        task_id=negative.id,
        artifact_type=ArtifactType.COMPUTE_RESULT,
        logical_key="negative-result-bundle",
        storage_uri=neg_artifact_uri,
    )
    neg_job = ComputeJob(
        project_id=negative.project_id,
        task_id=negative.id,
        backend="local",
        status=ComputeJobStatus.SUCCEEDED,
        artifact_id=None,
        result_reference=neg_artifact_uri,
    )
    negative_dependent = Task(
        project_id=negative.project_id,
        title="act on negative measurement",
        status=TaskStatus.QUEUED,
        payloads=[{"type": "create", "prompt": "use result", "base_branch": "main"}],
        depends_on=[str(negative.id)],
    )
    db_session.add(negative)
    db_session.add_all([neg_artifact, neg_job, negative_dependent])
    await db_session.commit()
    await db_session.refresh(negative)
    await db_session.refresh(neg_artifact)
    neg_job.artifact_id = neg_artifact.id
    neg_job.outcome = ComputeJobOutcome.SUCCESS
    neg_job.accrued_cost_usd = 0.37
    neg_job.final_cost_usd = 0.37
    db_session.add(neg_job)
    await db_session.commit()
    await db_session.refresh(neg_job)
    await db_session.refresh(negative_dependent)

    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
        await _handle_compute_experiment_completion(
            negative,
            _result_artifact(
                accuracy=0.42,
                job_id=str(neg_job.id),
                result_reference=neg_artifact_uri,
            ),
            db_session,
        )

    await db_session.refresh(negative)
    await db_session.refresh(negative_dependent)
    assert negative.status == TaskStatus.COMPLETED
    assert negative.plan_output["compute_result"]["criterion"]["met"] is False
    assert negative.context["compute_success_criterion"]["met"] is False
    negative_docs = await _knowledge_docs_for(db_session, negative)
    assert len(negative_docs) == 1
    negative_prereqs = (negative_dependent.context or {}).get(
        KNOWLEDGE_PREREQUISITES_KEY
    )
    assert negative_prereqs and negative_prereqs[0]["source_task_id"] == str(
        negative.id
    )

    broken = await _make_compute_task_with_identity(
        db_session, metric="accuracy", op=">=", threshold=0.9
    )
    broken.context = {
        **(broken.context or {}),
        COMPUTE_EXPERIMENT_PREREQUISITE_MODE_CONTEXT_KEY: (
            COMPUTE_EXPERIMENT_PREREQUISITE_MODE_ADVISORY
        ),
    }
    broken_dependent = Task(
        project_id=broken.project_id,
        title="act on broken measurement",
        status=TaskStatus.QUEUED,
        payloads=[{"type": "create", "prompt": "use result", "base_branch": "main"}],
        depends_on=[str(broken.id)],
    )
    db_session.add(broken)
    db_session.add(broken_dependent)
    await db_session.commit()
    await db_session.refresh(broken)
    await db_session.refresh(broken_dependent)

    await _handle_compute_experiment_completion(
        broken, json.dumps({"job_id": "job-broken", "status": "FAILED"}), db_session
    )

    await db_session.refresh(broken)
    await db_session.refresh(broken_dependent)
    assert broken.status == TaskStatus.FAILED
    assert broken.context["evaluation_gate"]["passed"] is False
    assert await _knowledge_docs_for(db_session, broken) == []
    assert (broken_dependent.context or {}).get(KNOWLEDGE_PREREQUISITES_KEY) is None


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


# ── Cited LLM analysis over durable artifacts as acceptance evidence ──────────


async def _setup_durable_job_with_artifact(
    db_session,
    task: Task,
    *,
    accuracy: float = 0.93,
    file_count: int = 3,
) -> tuple[ComputeJob, str]:
    """Create a terminal ComputeJob whose durable artifacts SURVIVED VM teardown.

    Mirrors the artifact-survival scaffolding already shipped on this branch: a
    per-job artifact bundle URI, an auto-populated COMPUTE_RESULT Artifact row
    whose ``.logs`` JSON carries ``artifact_uri``/``file_count``, preserved numeric
    metrics on ``polling_metadata['result_metrics']``, and the job pointing at the
    artifact via ``artifact_id``. Returns ``(job, artifact_uri)``.
    """
    # create_compute_job rejects a terminal task; the experiment is still running
    # when its job is launched. The completion handler drives it to COMPLETED.
    task.status = TaskStatus.IN_PROGRESS
    db_session.add(task)
    await db_session.commit()
    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="python train.py",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )
    artifact_uri = f"gs://test-bucket/compute-jobs/{job.id}/artifacts/bundle.tar.gz"
    artifact = Artifact(
        task_id=task.id,
        artifact_type=ArtifactType.COMPUTE_RESULT,
        storage_uri=artifact_uri,
        logs=json.dumps(
            {
                "compute_job_id": str(job.id),
                "backend": "gcp_batch",
                "artifact_uri": artifact_uri,
                "file_count": file_count,
                "outcome": "SUCCESS",
                "final_cost_usd": 0.37,
            },
            sort_keys=True,
        ),
    )
    db_session.add(artifact)
    await db_session.commit()
    await db_session.refresh(artifact)

    # Simulate VM teardown: the job is terminal, the durable artifact + preserved
    # metrics remain on the row, and the artifact is re-readable post-teardown.
    job.status = ComputeJobStatus.SUCCEEDED
    job.outcome = ComputeJobOutcome.SUCCESS
    job.accrued_cost_usd = 0.37
    job.final_cost_usd = 0.37
    job.completed_at = datetime.utcnow()
    job.artifact_id = artifact.id
    job.result_reference = artifact_uri
    job.polling_metadata = {"result_metrics": {"accuracy": accuracy}}
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)
    return job, artifact_uri


def _mock_anthropic_returning(citations: list[str]):
    """Build a mock anthropic.AsyncAnthropic class that returns a cited analysis."""
    fake_text = json.dumps(
        {
            "title": "Analysis: X speeds up Y on bench-suite-v2",
            "summary": (
                "Grounded analysis of the durable artifacts: the run reached "
                "accuracy 0.93, clearing the declared bar, and the checkpoints "
                "and metrics survive VM teardown."
            ),
            "body": (
                "# Compute experiment analysis\n\n"
                "The experiment produced durable artifacts and a measured "
                "accuracy that meets the success criterion. See the cited "
                "artifact bundle and metric references for the evidence."
            ),
            "source_citations": citations,
        }
    )
    mock_content_item = MagicMock()
    mock_content_item.text = fake_text
    mock_resp = MagicMock()
    mock_resp.content = [mock_content_item]
    mock_create = AsyncMock(return_value=mock_resp)
    mock_messages = MagicMock()
    mock_messages.create = mock_create
    mock_client = MagicMock()
    mock_client.messages = mock_messages
    return MagicMock(return_value=mock_client), mock_create


@pytest.mark.asyncio
async def test_compute_experiment_knowledge_marks_only_llm_artifact_cited_as_acceptance_grade(
    db_session, mocker, _quiet_compute_io
):
    """The PRIMARY path persists a cited LLM analysis over the durable artifacts.

    Runs a compute_experiment to terminal completion, simulates VM teardown,
    re-reads the durable artifact, and asserts the persisted KnowledgeDocument
    carries REAL artifact/metric citations (resolving to durable evidence, not a
    bare result_reference) and was produced by the LLM path. The paired fallback
    run remains searchable measured knowledge, but not acceptance evidence.
    """
    from app.knowledge_query import (
        build_knowledge_index,
        fetch_knowledge_substance_index,
        list_knowledge_documents,
        serialize_knowledge_document,
    )
    from app.task_outcomes import evaluate_task_outcome

    # Signed-URL minting hits GCS; force the optional best-effort path to no-op so
    # the candidate citation set is deterministic (artifact + metric refs only).
    mocker.patch(
        "app.storage.generate_download_signed_url",
        new=AsyncMock(side_effect=RuntimeError("no gcs in tests")),
    )

    task = await _make_compute_task_with_identity(
        db_session, metric="accuracy", op=">=", threshold=0.9
    )
    job, artifact_uri = await _setup_durable_job_with_artifact(db_session, task)

    metric_ref = f"{artifact_uri}#metric:accuracy=0.93"
    result_reference = "https://signed.example.com/jobs/result.json"
    mock_cls, mock_create = _mock_anthropic_returning([artifact_uri, metric_ref])

    logs = _result_artifact(
        job_id=str(job.id),
        accuracy=0.93,
        result_reference=result_reference,
    )

    with (
        patch.dict(
            os.environ,
            {"ANTHROPIC_API_KEY": "test-key", "DISABLE_LLM_COMPUTE_ANALYSIS": ""},
        ),
        patch("anthropic.AsyncAnthropic", mock_cls),
    ):
        await _handle_compute_experiment_completion(task, logs, db_session)

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED

    # The LLM was actually invoked over the assembled evidence.
    mock_create.assert_awaited_once()
    prompt = mock_create.await_args.kwargs["messages"][0]["content"]
    assert artifact_uri in prompt  # grounded in the durable artifact bundle

    docs = await _knowledge_docs_for(db_session, task)
    assert len(docs) == 1
    doc = docs[0]
    assert doc.source_kind == "compute_experiment"
    # REAL artifact/metric citations, NOT just the bare result_reference.
    assert artifact_uri in doc.source_citations
    assert metric_ref in doc.source_citations
    assert result_reference not in doc.source_citations
    # Produced by the LLM path and the verdict records the mode + measured facts.
    verdict = doc.forward_quality_verdict
    assert verdict["analysis_mode"] == "llm"
    assert verdict["outcome"] == "SUCCESS"
    assert verdict["criterion_met"] is True
    assert verdict["criterion"]["value"] == pytest.approx(0.93)
    assert verdict["knowledge_acceptance_evidence"] is True
    assert verdict["compute_acceptance_grade"] == "acceptance"
    assert verdict["compute_acceptance_reason"] == "llm_artifact_cited"
    # The LLM-authored prose replaced the deterministic template body.
    assert "analysis" in doc.body.lower()
    assert task.plan_output["knowledge_document"]["analysis_mode"] == "llm"
    assert (
        task.plan_output["knowledge_document"]["knowledge_acceptance_evidence"]
        is True
    )
    assert (
        task.plan_output["knowledge_document"]["compute_acceptance_grade"]
        == "acceptance"
    )
    # The compute_result the handler owns is untouched by the knowledge pass.
    assert task.plan_output["compute_result"]["outcome"] == "SUCCESS"
    assert evaluate_task_outcome(task).status == "success"

    listing = await list_knowledge_documents(
        db_session, task.project_id, source_kind="compute_experiment"
    )
    assert listing["documents"][0]["knowledge_acceptance_evidence"] is True
    assert listing["documents"][0]["compute_acceptance_grade"] == "acceptance"
    detail = serialize_knowledge_document(doc, include_body=True)
    assert detail["knowledge_acceptance_evidence"] is True
    assert detail["compute_acceptance_grade"] == "acceptance"
    index = await build_knowledge_index(db_session, task.project_id)
    assert index[0]["knowledge_acceptance_evidence"] is True
    assert index[0]["compute_acceptance_grade"] == "acceptance"
    substance = await fetch_knowledge_substance_index(db_session, task.project_id)
    assert substance[0].forward_quality_verdict["knowledge_acceptance_evidence"] is True
    assert substance[0].forward_quality_verdict["compute_acceptance_grade"] == "acceptance"

    fallback_task = await _make_compute_task_with_identity(
        db_session, metric="accuracy", op=">=", threshold=0.9
    )
    fallback_job, _ = await _setup_durable_job_with_artifact(
        db_session, fallback_task
    )
    fallback_logs = _result_artifact(
        job_id=str(fallback_job.id),
        accuracy=0.93,
        result_reference=result_reference,
    )
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
        await _handle_compute_experiment_completion(
            fallback_task, fallback_logs, db_session
        )

    await db_session.refresh(fallback_task)
    fallback_docs = await _knowledge_docs_for(db_session, fallback_task)
    assert len(fallback_docs) == 1
    fallback_verdict = fallback_docs[0].forward_quality_verdict
    assert fallback_verdict["analysis_mode"] == "fallback"
    assert fallback_verdict["knowledge_acceptance_evidence"] is False
    assert fallback_verdict["compute_acceptance_grade"] == "measured_only"
    assert fallback_verdict["compute_acceptance_reason"] == "analysis_mode_not_llm"
    fallback_detail = serialize_knowledge_document(
        fallback_docs[0], include_body=True
    )
    assert fallback_detail["knowledge_acceptance_evidence"] is False
    assert fallback_detail["compute_acceptance_grade"] == "measured_only"
    fallback_substance = await fetch_knowledge_substance_index(
        db_session, fallback_task.project_id
    )
    assert fallback_substance[0].forward_quality_verdict["knowledge_acceptance_evidence"] is False
    assert fallback_substance[0].forward_quality_verdict["compute_acceptance_grade"] == "measured_only"
    assert (
        fallback_task.plan_output["knowledge_document"][
            "knowledge_acceptance_evidence"
        ]
        is False
    )
    assert (
        fallback_task.plan_output["knowledge_document"]["compute_acceptance_grade"]
        == "measured_only"
    )
    assert evaluate_task_outcome(fallback_task).status == "neutral"


@pytest.mark.asyncio
async def test_compute_experiment_knowledge_falls_back_without_llm(
    db_session, _quiet_compute_io
):
    """With no ANTHROPIC_API_KEY the deterministic outcome/cost/criterion document
    is persisted unchanged — durable knowledge never depends on the LLM."""
    task = await _make_compute_task_with_identity(
        db_session, metric="accuracy", op=">=", threshold=0.9
    )
    job, _artifact_uri = await _setup_durable_job_with_artifact(db_session, task)

    result_reference = "https://signed.example.com/jobs/result.json"
    logs = _result_artifact(
        job_id=str(job.id),
        accuracy=0.93,
        result_reference=result_reference,
    )

    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
        await _handle_compute_experiment_completion(task, logs, db_session)

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    docs = await _knowledge_docs_for(db_session, task)
    assert len(docs) == 1
    doc = docs[0]
    assert doc.forward_quality_verdict["analysis_mode"] == "fallback"
    assert doc.forward_quality_verdict["outcome"] == "SUCCESS"
    # Deterministic template: https:// result_reference is not a durable citation,
    # so source_citations is empty. The body is the hardcoded verdict markdown.
    assert doc.source_citations == []
    assert "Compute experiment verdict" in doc.body


@pytest.mark.asyncio
async def test_compute_experiment_knowledge_falls_back_on_ungrounded_citation(
    db_session, mocker, _quiet_compute_io
):
    """If the LLM returns only an ungrounded / bare-result citation it fails the
    citation gate and the deterministic document is persisted instead."""
    mocker.patch(
        "app.storage.generate_download_signed_url",
        new=AsyncMock(side_effect=RuntimeError("no gcs in tests")),
    )

    task = await _make_compute_task_with_identity(
        db_session, metric="accuracy", op=">=", threshold=0.9
    )
    job, _artifact_uri = await _setup_durable_job_with_artifact(db_session, task)

    result_reference = "https://signed.example.com/jobs/result.json"
    # The model cites a fabricated URL that is NOT in the durable evidence set,
    # plus the bare result_reference — neither grounds the doc in real artifacts.
    mock_cls, _ = _mock_anthropic_returning(
        ["https://fabricated.example.com/made-up", result_reference]
    )
    logs = _result_artifact(
        job_id=str(job.id),
        accuracy=0.93,
        result_reference=result_reference,
    )

    with (
        patch.dict(
            os.environ,
            {"ANTHROPIC_API_KEY": "test-key", "DISABLE_LLM_COMPUTE_ANALYSIS": ""},
        ),
        patch("anthropic.AsyncAnthropic", mock_cls),
    ):
        await _handle_compute_experiment_completion(task, logs, db_session)

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    docs = await _knowledge_docs_for(db_session, task)
    assert len(docs) == 1
    assert docs[0].forward_quality_verdict["analysis_mode"] == "fallback"
    assert docs[0].source_citations == []


@pytest.mark.asyncio
async def test_compute_experiment_knowledge_requires_durable_citations(
    db_session, _quiet_compute_io
):
    """Compute knowledge citations must be durable: transient https:// URLs are
    excluded from source_citations; a gs:// result_reference backed by a real
    Artifact row IS included.

    Two cases exercised in one test (the acceptance-evidence gate for this PR):
      1. https:// result_reference (signed URL) → source_citations == [] even when
         the LLM path is disabled, because _is_durable_citation rejects http(s)://.
      2. gs:// result_reference backed by a real Artifact → source_citations
         contains the gs:// URI, confirming durable evidence is preserved.
    """
    from app.knowledge_completion import persist_compute_experiment_knowledge

    project = Project(name="DurableCitationTest", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    def _make_bare_task(name: str) -> Task:
        return Task(
            project_id=project.id,
            title=f"compute_experiment: {name}",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "compute_experiment", "experiment": name, "base_branch": "main"}],
        )

    # ── Case 1: https:// result_reference → no durable citation stored ───────
    task_https = _make_bare_task("https-ref-experiment")
    db_session.add(task_https)
    await db_session.commit()
    await db_session.refresh(task_https)

    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
        doc_https = await persist_compute_experiment_knowledge(
            task_https,
            outcome="SUCCESS",
            cost=0.10,
            criterion=None,
            result_reference="https://signed.example.com/jobs/abc/result.json",
            session=db_session,
            logs=None,
        )

    assert doc_https is not None
    assert doc_https.source_citations == [], (
        "transient https:// result_reference must not be stored as a durable citation"
    )

    # ── Case 2: gs:// result_reference backed by real Artifact → citation kept ─
    task_gs = _make_bare_task("gs-ref-experiment")
    db_session.add(task_gs)
    await db_session.commit()
    await db_session.refresh(task_gs)

    gs_uri = "gs://test-bucket/jobs/durable-job/result.json"
    artifact = Artifact(
        task_id=task_gs.id,
        artifact_type=ArtifactType.COMPUTE_RESULT,
        storage_uri=gs_uri,
        logs=json.dumps({"artifact_uri": gs_uri, "file_count": 2}),
    )
    db_session.add(artifact)
    await db_session.commit()
    await db_session.refresh(artifact)

    job = ComputeJob(
        project_id=project.id,
        task_id=task_gs.id,
        backend="local",
        status=ComputeJobStatus.SUCCEEDED,
        artifact_id=artifact.id,
        result_reference=gs_uri,
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    logs_json = json.dumps({"job_id": str(job.id), "outcome": "SUCCESS"})

    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
        doc_gs = await persist_compute_experiment_knowledge(
            task_gs,
            outcome="SUCCESS",
            cost=0.15,
            criterion=None,
            result_reference=gs_uri,
            session=db_session,
            logs=logs_json,
        )

    assert doc_gs is not None
    assert gs_uri in doc_gs.source_citations, (
        "gs:// result_reference backed by a real Artifact must be preserved as "
        "durable citation evidence"
    )


@pytest.mark.asyncio
async def test_plan_spawned_compute_experiment_carries_downstream_artifact_evidence(
    db_session, _quiet_compute_io, mocker
):
    """A compute_experiment spawned by plan_completion with downstream_artifact
    acceptance_evidence stamps the terminal COMPUTE_RESULT Artifact with the
    declared logical key and delivers the artifact prerequisite pointer to queued
    dependents.

    This is the end-to-end path: plan_completion sets the evidence on the payload;
    _handle_compute_experiment_completion reads it via artifact_prerequisites and
    stamps the artifact.
    """
    import json as _json

    from sqlmodel import select as _select

    from app.plan_completion import _handle_plan_completion
    from testing.factories import make_project, make_task

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch("app.plan_completion.broadcast_to_project", new_callable=AsyncMock)

    project = make_project(name="PlanComputeEvidenceE2E")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    plan_task = make_task(
        project_id=project.id,
        title="Plan: measure and implement",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "plan", "prompt": "plan it", "base_branch": "main"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(plan_task)
    await db_session.commit()
    await db_session.refresh(plan_task)

    mocker.patch(
        "app.task_completion._ensure_branch",
        new_callable=AsyncMock,
        return_value=plan_task,
    )

    plan_logs = _json.dumps(
        {
            "subtasks": [
                {
                    "title": "Measure baseline",
                    "prompt": "run the benchmark",
                    "type": "compute_experiment",
                    "command": "python bench.py",
                    "spend_cap_usd": 2.0,
                    "deadline_seconds": 600,
                    "acceptance_evidence": {
                        "kind": "downstream_artifact",
                        "artifact_id": "plan-result-bundle",
                    },
                },
            ]
        }
    )
    await _handle_plan_completion(plan_task, plan_logs, db_session)

    # Grab the spawned compute_experiment child.
    compute_child = (
        (
            await db_session.execute(
                _select(Task).where(Task.parent_task_id == plan_task.id)
            )
        )
        .scalars()
        .first()
    )
    assert compute_child is not None
    payload = compute_child.first_payload or {}
    assert payload["type"] == "compute_experiment"
    assert payload.get("acceptance_evidence", {}).get("artifact_id") == "plan-result-bundle", (
        f"acceptance_evidence not in payload after plan_completion spawn: {payload}"
    )

    # Now wire the child to complete and verify artifact stamp.
    compute_child.status = TaskStatus.IN_PROGRESS
    db_session.add(compute_child)
    await db_session.commit()
    await db_session.refresh(compute_child)

    job = await compute_jobs.create_compute_job(
        db_session,
        compute_child,
        command="python bench.py",
        machine_type="e2-standard-2",
        spend_cap_usd=2.0,
        deadline_seconds=600,
    )
    from datetime import datetime as _dt
    job.status = ComputeJobStatus.SUCCEEDED
    job.outcome = ComputeJobOutcome.SUCCESS
    job.accrued_cost_usd = 0.25
    job.final_cost_usd = 0.25
    job.terminal_reason = "compute job finished"
    job.completed_at = _dt.utcnow()
    db_session.add(job)
    await db_session.commit()

    result_reference = "gs://blueprint-agent-artifacts/compute-jobs/plan-result.json"
    await _handle_compute_experiment_completion(
        compute_child,
        _result_artifact(job_id=str(job.id), result_reference=result_reference),
        db_session,
    )

    await db_session.refresh(job)
    artifact = await db_session.get(Artifact, job.artifact_id)
    assert artifact is not None
    assert artifact.logical_key == "plan-result-bundle"
    assert artifact.artifact_type == ArtifactType.COMPUTE_RESULT


# ── Reentry upgrade: fallback → llm ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_compute_knowledge_reentry_upgrades_fallback_to_llm(
    db_session, mocker, _quiet_compute_io
):
    """A second persistence pass with a stronger LLM analysis upgrades the stored
    fallback document in-place. Exactly ONE KnowledgeDocument row exists, its
    content reflects the LLM analysis, and plan_output mirrors the upgraded values.

    This is the acceptance-evidence test: production code in
    _persist_compute_knowledge_document performs the upgrade (not a no-op).
    """
    mocker.patch(
        "app.storage.generate_download_signed_url",
        new=AsyncMock(side_effect=RuntimeError("no gcs in tests")),
    )

    task = await _make_compute_task_with_identity(
        db_session, metric="accuracy", op=">=", threshold=0.9
    )
    job, artifact_uri = await _setup_durable_job_with_artifact(db_session, task)

    logs = _result_artifact(job_id=str(job.id), accuracy=0.93)

    # ── First pass: no ANTHROPIC_API_KEY → fallback document persisted ───────
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
        await _handle_compute_experiment_completion(task, logs, db_session)

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED

    docs_after_first = await _knowledge_docs_for(db_session, task)
    assert len(docs_after_first) == 1
    assert docs_after_first[0].forward_quality_verdict["analysis_mode"] == "fallback"
    fallback_body = docs_after_first[0].body
    assert "Compute experiment verdict" in fallback_body

    # ── Second pass: LLM available → document upgraded to llm in-place ───────
    metric_ref = f"{artifact_uri}#metric:accuracy=0.93"
    mock_cls, mock_create = _mock_anthropic_returning([artifact_uri, metric_ref])

    with (
        patch.dict(
            os.environ,
            {"ANTHROPIC_API_KEY": "test-key", "DISABLE_LLM_COMPUTE_ANALYSIS": ""},
        ),
        patch("anthropic.AsyncAnthropic", mock_cls),
    ):
        await _handle_compute_experiment_completion(task, logs, db_session)

    await db_session.refresh(task)

    docs_after_second = await _knowledge_docs_for(db_session, task)
    # Must be exactly ONE row — upgrade is in-place, not a new insert.
    assert len(docs_after_second) == 1
    doc = docs_after_second[0]

    # Content reflects the LLM analysis, not the deterministic fallback.
    verdict = doc.forward_quality_verdict
    assert verdict["analysis_mode"] == "llm", (
        "persisted doc must be upgraded from fallback to llm on reentry"
    )
    assert artifact_uri in doc.source_citations
    assert metric_ref in doc.source_citations
    assert "analysis" in doc.body.lower()
    # The deterministic template body was replaced.
    assert "Compute experiment verdict" not in doc.body

    # plan_output mirrors the upgraded values.
    assert task.plan_output["knowledge_document"]["analysis_mode"] == "llm"
    assert task.plan_output["knowledge_document"]["citation_count"] >= 2

    # The LLM was actually invoked on the second pass.
    mock_create.assert_awaited_once()


@pytest.mark.asyncio
async def test_compute_knowledge_does_not_downgrade_llm_to_fallback(
    db_session, mocker, _quiet_compute_io
):
    """A persistence pass with a weaker fallback does NOT overwrite an existing
    stronger LLM document. The stored row keeps its llm values unchanged."""
    mocker.patch(
        "app.storage.generate_download_signed_url",
        new=AsyncMock(side_effect=RuntimeError("no gcs in tests")),
    )

    task = await _make_compute_task_with_identity(
        db_session, metric="accuracy", op=">=", threshold=0.9
    )
    job, artifact_uri = await _setup_durable_job_with_artifact(db_session, task)

    logs = _result_artifact(job_id=str(job.id), accuracy=0.93)

    # ── First pass: LLM available → llm document persisted ───────────────────
    metric_ref = f"{artifact_uri}#metric:accuracy=0.93"
    mock_cls, _ = _mock_anthropic_returning([artifact_uri, metric_ref])

    with (
        patch.dict(
            os.environ,
            {"ANTHROPIC_API_KEY": "test-key", "DISABLE_LLM_COMPUTE_ANALYSIS": ""},
        ),
        patch("anthropic.AsyncAnthropic", mock_cls),
    ):
        await _handle_compute_experiment_completion(task, logs, db_session)

    await db_session.refresh(task)
    docs_after_first = await _knowledge_docs_for(db_session, task)
    assert len(docs_after_first) == 1
    assert docs_after_first[0].forward_quality_verdict["analysis_mode"] == "llm"
    llm_body = docs_after_first[0].body

    # ── Second pass: no API key → fallback path, must NOT downgrade ──────────
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
        await _handle_compute_experiment_completion(task, logs, db_session)

    await db_session.refresh(task)
    docs_after_second = await _knowledge_docs_for(db_session, task)
    assert len(docs_after_second) == 1
    doc = docs_after_second[0]

    # The llm verdict and body must be preserved.
    assert doc.forward_quality_verdict["analysis_mode"] == "llm", (
        "llm document must not be downgraded to fallback on reentry"
    )
    assert doc.body == llm_body
    assert task.plan_output["knowledge_document"]["analysis_mode"] == "llm"


@pytest.mark.asyncio
async def test_compute_knowledge_same_mode_retry_is_idempotent(
    db_session, _quiet_compute_io
):
    """Two identical fallback persistence calls result in exactly ONE row with
    no errors — idempotent retry is safe."""
    task = await _make_compute_task(db_session)
    logs = _result_artifact()

    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
        await _handle_compute_experiment_completion(task, logs, db_session)
        await _handle_compute_experiment_completion(task, logs, db_session)

    await db_session.refresh(task)
    docs = await _knowledge_docs_for(db_session, task)
    assert len(docs) == 1
    assert docs[0].forward_quality_verdict["analysis_mode"] == "fallback"


@pytest.mark.asyncio
async def test_fallback_verdict_records_skip_reason_when_precondition_not_met(
    db_session, _quiet_compute_io
):
    """When the LLM precondition is not met (no API key), the persisted fallback
    KnowledgeDocument's forward_quality_verdict includes a ``fallback_reason``
    field naming why the LLM path was skipped."""
    task = await _make_compute_task(db_session)
    logs = _result_artifact()

    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
        await _handle_compute_experiment_completion(task, logs, db_session)

    docs = await _knowledge_docs_for(db_session, task)
    assert len(docs) == 1
    verdict = docs[0].forward_quality_verdict
    assert verdict["analysis_mode"] == "fallback"
    assert verdict.get("fallback_reason") == "precondition-not-met", (
        f"expected fallback_reason='precondition-not-met', got {verdict.get('fallback_reason')!r}"
    )


@pytest.mark.asyncio
async def test_fallback_verdict_records_skip_reason_when_analysis_disabled(
    db_session, _quiet_compute_io
):
    """When analysis is explicitly disabled via env, the fallback verdict names
    ``analysis-disabled`` as the reason."""
    task = await _make_compute_task(db_session)
    logs = _result_artifact()

    with patch.dict(
        os.environ,
        {"ANTHROPIC_API_KEY": "test-key", "DISABLE_LLM_COMPUTE_ANALYSIS": "1"},
    ):
        await _handle_compute_experiment_completion(task, logs, db_session)

    docs = await _knowledge_docs_for(db_session, task)
    assert len(docs) == 1
    verdict = docs[0].forward_quality_verdict
    assert verdict["analysis_mode"] == "fallback"
    assert verdict.get("fallback_reason") == "analysis-disabled", (
        f"expected fallback_reason='analysis-disabled', got {verdict.get('fallback_reason')!r}"
    )


@pytest.mark.asyncio
async def test_compute_llm_strips_signed_url_when_durable_citation_also_present(
    db_session, mocker, _quiet_compute_io
):
    """When the LLM cites BOTH a durable gs:// artifact AND a transient signed
    https:// URL, only the gs:// citation survives in source_citations.

    Signed URLs expire and are transient; only durable artifact/metric citations
    are acceptance-grade evidence. This verifies that supplementary citations
    included in the LLM prompt (for convenience) are stripped from storage even
    when the LLM explicitly cites them alongside real durable evidence.
    """
    signed_url = "https://signed.storage.example.com/token=abc123/artifacts.tar.gz"
    mocker.patch(
        "app.storage.generate_download_signed_url",
        new=AsyncMock(return_value=signed_url),
    )

    task = await _make_compute_task_with_identity(
        db_session, metric="accuracy", op=">=", threshold=0.9
    )
    job, artifact_uri = await _setup_durable_job_with_artifact(db_session, task)

    metric_ref = f"{artifact_uri}#metric:accuracy=0.93"
    # LLM cites BOTH the durable artifact/metric AND the transient signed URL.
    mock_cls, mock_create = _mock_anthropic_returning(
        [artifact_uri, metric_ref, signed_url]
    )

    logs = _result_artifact(job_id=str(job.id), accuracy=0.93)

    with (
        patch.dict(
            os.environ,
            {"ANTHROPIC_API_KEY": "test-key", "DISABLE_LLM_COMPUTE_ANALYSIS": ""},
        ),
        patch("anthropic.AsyncAnthropic", mock_cls),
    ):
        await _handle_compute_experiment_completion(task, logs, db_session)

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    mock_create.assert_awaited_once()

    docs = await _knowledge_docs_for(db_session, task)
    assert len(docs) == 1
    doc = docs[0]
    assert doc.forward_quality_verdict["analysis_mode"] == "llm"

    # Durable citations survive.
    assert artifact_uri in doc.source_citations
    assert metric_ref in doc.source_citations
    # Transient signed URL is stripped even though the LLM cited it.
    assert signed_url not in doc.source_citations, (
        "transient signed HTTPS URL must be stripped from source_citations even "
        "when the LLM also cited durable artifact evidence"
    )


@pytest.mark.asyncio
async def test_fallback_grounding_mismatched_result_reference_unresolved(
    db_session, _quiet_compute_io
):
    """Fallback grounding marks a citation unresolved when result_reference does
    not match the ComputeJob's artifact URI — bare artifact-row presence is not
    sufficient. The mismatched citation is stripped from source_citations."""
    from app.knowledge_completion import persist_compute_experiment_knowledge

    project = Project(name="MismatchGroundingTest", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="compute_experiment: mismatched ref",
        status=TaskStatus.COMPLETED,
        payloads=[{
            "type": "compute_experiment",
            "experiment": "mismatch-test",
            "base_branch": "main",
        }],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Artifact is for job-a's bundle.
    artifact = Artifact(
        task_id=task.id,
        artifact_type=ArtifactType.COMPUTE_RESULT,
        storage_uri="gs://bucket/jobs/job-a/artifacts/",
        logs=json.dumps({
            "artifact_uri": "gs://bucket/jobs/job-a/artifacts/",
            "file_count": 2,
        }),
    )
    db_session.add(artifact)
    await db_session.commit()
    await db_session.refresh(artifact)

    job = ComputeJob(
        project_id=project.id,
        task_id=task.id,
        backend="local",
        status=ComputeJobStatus.SUCCEEDED,
        artifact_id=artifact.id,
        result_reference="gs://bucket/jobs/job-a/artifacts/result.json",
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    # result_reference points to a DIFFERENT path — mismatched.
    mismatched_ref = "gs://bucket/jobs/job-b/result.json"
    logs_json = json.dumps({"job_id": str(job.id), "outcome": "SUCCESS"})

    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
        doc = await persist_compute_experiment_knowledge(
            task,
            outcome="SUCCESS",
            cost=0.10,
            criterion=None,
            result_reference=mismatched_ref,
            session=db_session,
            logs=logs_json,
        )

    assert doc is not None
    # Mismatched reference is not backed by the artifact → citation stripped.
    assert mismatched_ref not in doc.source_citations, (
        "a gs:// result_reference that doesn't match the artifact URI must not "
        "be stored as durable citation evidence"
    )
    grounding = (doc.forward_quality_verdict.get("probes") or {}).get("grounding") or {}
    assert grounding.get("grounded", 0) == 0, (
        "grounded must be 0 when result_reference doesn't match the artifact URI"
    )


@pytest.mark.asyncio
async def test_real_evidence_rejects_http_artifact_location(
    db_session, _quiet_compute_io
):
    """HTTP(s) URLs are never admitted to the real_evidence set, even when
    returned as the resolved artifact location. The durable citation filter
    must reject transient signed URLs and bare http(s) result URLs."""
    from app.knowledge_completion import _gather_compute_evidence

    project = Project(name="HTTPArtifactTest", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="compute_experiment: http artifact test",
        status=TaskStatus.COMPLETED,
        payloads=[{
            "type": "compute_experiment",
            "experiment": "http-test",
            "base_branch": "main",
        }],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Create an artifact with http:// storage_uri (the fallback for artifact_uri).
    artifact = Artifact(
        task_id=task.id,
        artifact_type=ArtifactType.COMPUTE_RESULT,
        storage_uri="https://signed-download.example.com/result.tar.gz?token=xyz",
        logs=json.dumps({
            "artifact_uri": None,
            "file_count": 2,
        }),
    )
    db_session.add(artifact)
    await db_session.commit()
    await db_session.refresh(artifact)

    # Job references the artifact but result_reference is also http://.
    job = ComputeJob(
        project_id=project.id,
        task_id=task.id,
        backend="local",
        status=ComputeJobStatus.SUCCEEDED,
        artifact_id=artifact.id,
        result_reference="https://example.com/jobs/result.json",
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    logs_json = json.dumps({"job_id": str(job.id)})

    # _gather_compute_evidence should return None because there is no
    # durable artifact location — all URIs are http(s) and thus rejected
    # by _is_durable_citation.
    evidence = await _gather_compute_evidence(
        task,
        logs=logs_json,
        result_reference="https://example.com/signed-download",
        criterion=None,
        session=db_session,
    )

    assert evidence is None, (
        "http(s) URLs must be rejected from durable evidence set; "
        "_gather_compute_evidence should return None (fallback)"
    )
