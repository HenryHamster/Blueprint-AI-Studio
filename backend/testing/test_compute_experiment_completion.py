"""End-to-end lifecycle tests for the compute_experiment task type.

The compute_experiment type is the reference proof that long-running compute
work is represented honestly by the shared task capability contract. These
tests drive a compute_experiment task across its full lifecycle — a budgeted
ComputeJob is launched and polled by the substrate, the substrate enforces the
spend-cap / wall-clock-deadline kills, and the task's terminal COMPUTE_RESULT
artifact is gated by the registry-declared 'artifact-delivery' forward gate via
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
    ReleaseDeployArtifact,
    ReleaseVersion,
    SourceKind,
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
    compute-substrate experiment delivering a durable artifact/resource."""
    cap = get_task_capability("compute_experiment")
    assert cap is not None
    assert cap.requires_code is False
    assert cap.requires_compute is True
    assert cap.run_shape == "experiment_loop"
    assert cap.output_kind == "artifact"
    assert cap.evaluation == "artifact-delivery"
    assert cap.completion_handler == "compute_experiment"
    # Derived maps stay in sync (registry is the single source of truth).
    assert EVALUATION_STRATEGY_BY_TASK_TYPE["compute_experiment"] == "artifact-delivery"
    assert RUN_SHAPE_BY_TASK_TYPE["compute_experiment"] == "experiment_loop"
    assert COMPLETION_HANDLER_BY_TASK_TYPE["compute_experiment"] == "compute_experiment"


# ── Completion handler: artifact-delivery gate ──────────────────────────────


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
    assert task.plan_output["compute_result"]["result_reference"] == result_reference
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
async def test_compute_downstream_artifact_requires_durable_output_normal_mode(
    db_session, _quiet_compute_io
):
    """Normal mode + downstream_artifact acceptance evidence + transient result →
    FAILED. Declaring a downstream_artifact means the completed compute result
    must be durably delivered, not just measured."""
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
    await _handle_compute_experiment_completion(
        task,
        _result_artifact(job_id=str(job.id), result_reference=transient_ref),
        db_session,
    )

    await db_session.refresh(task)
    await db_session.refresh(dependent)
    assert task.status == TaskStatus.FAILED
    gate = task.context["evaluation_gate"]
    assert gate["passed"] is False
    assert gate["strategy"] == "artifact-delivery"
    assert "require_durable_artifact" in gate["reason"]
    delivery = task.plan_output["compute_result"]["artifact_delivery"]
    assert delivery["delivered"] is False
    assert delivery["durability"] == "transient"
    assert (dependent.context or {}).get(ARTIFACT_PREREQUISITES_KEY) is None


@pytest.mark.asyncio
async def test_compute_downstream_artifact_passes_with_durable_output_normal_mode(
    db_session, _quiet_compute_io
):
    """Normal mode + downstream_artifact acceptance evidence + gs:// result →
    COMPLETED and delivers the artifact pointer to queued dependents."""
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

    gs_uri = "gs://blueprint-agent-artifacts/compute-jobs/downstream-result.json"
    await _handle_compute_experiment_completion(
        task,
        _result_artifact(job_id=str(job.id), result_reference=gs_uri),
        db_session,
    )

    await db_session.refresh(task)
    await db_session.refresh(dependent)
    assert task.status == TaskStatus.COMPLETED
    assert "evaluation_gate" not in task.context
    delivery = task.plan_output["compute_result"]["artifact_delivery"]
    assert delivery["delivered"] is True
    assert delivery["durability"] == "durable"
    prereqs = (dependent.context or {}).get(ARTIFACT_PREREQUISITES_KEY)
    assert prereqs and prereqs[0]["logical_key"] == "experiment-result-bundle"


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
    compute_result = task.plan_output["compute_result"]
    assert compute_result["result_reference"] == result_reference
    assert compute_result["artifact_id"] == str(artifact.id)
    assert compute_result["storage_uri"] == result_reference

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
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

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
    assert doc.forward_quality_verdict["analysis_mode"] == "deterministic"
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
async def test_backfill_artifact_prerequisites_stays_within_project(
    db_session, _quiet_compute_io
):
    """Claim-time backfill must not copy artifact metadata across projects."""
    producer_project = Project(name="BackfillCrossProjectProducer", user_id="test-user")
    dependent_project = Project(name="BackfillCrossProjectDependent", user_id="test-user")
    db_session.add_all([producer_project, dependent_project])
    await db_session.commit()
    await db_session.refresh(producer_project)
    await db_session.refresh(dependent_project)

    producer = Task(
        project_id=producer_project.id,
        title="compute_experiment: foreign producer",
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
        logical_key="foreign-bundle",
        storage_uri="gs://blueprint-agent-artifacts/compute-jobs/foreign.json",
    )
    db_session.add(artifact)
    await db_session.commit()
    await db_session.refresh(artifact)

    dependent = Task(
        project_id=dependent_project.id,
        title="feat: consume foreign bundle",
        status=TaskStatus.QUEUED,
        payloads=[{"type": "create", "prompt": "use result", "base_branch": "main"}],
        depends_on=[str(producer.id)],
    )
    db_session.add(dependent)
    await db_session.commit()
    await db_session.refresh(dependent)

    await backfill_artifact_prerequisites(dependent, db_session)
    await db_session.commit()
    await db_session.refresh(dependent)

    assert (dependent.context or {}).get(ARTIFACT_PREREQUISITES_KEY) is None


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
    assert task.context["evaluation_gate"]["strategy"] == "artifact-delivery"


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
    assert task.context["evaluation_gate"]["strategy"] == "artifact-delivery"


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
async def test_compute_completion_reported_metric_unmet_completes_and_records(
    db_session, _quiet_compute_io
):
    """A reported experiment metric that misses its goal completes artifact
    delivery and records the unmet criterion durably."""
    task = await _make_compute_task_with_criterion(
        db_session, "accuracy", ">=", 0.9
    )

    await _handle_compute_experiment_completion(
        task, _result_artifact(accuracy=0.83), db_session
    )

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    assert "evaluation_gate" not in task.context
    criterion = task.plan_output["compute_result"]["criterion"]
    assert criterion["met"] is False
    assert criterion["measurable"] is True
    assert criterion["value"] == pytest.approx(0.83)
    ctx_criterion = task.context["compute_success_criterion"]
    assert ctx_criterion["met"] is False
    assert ctx_criterion["measurable"] is True
    assert ctx_criterion["value"] == pytest.approx(0.83)


@pytest.mark.asyncio
async def test_compute_completion_unmet_criterion_completes_and_records_evidence(
    db_session, _quiet_compute_io
):
    """An experiment that finishes but misses its declared goal on a MEASURABLE
    metric completes artifact delivery and records honest negative evidence."""
    task = await _make_compute_task_with_criterion(
        db_session, "final_cost_usd", "<=", 5.0
    )

    await _handle_compute_experiment_completion(
        task, _result_artifact(final_cost_usd=10.0), db_session
    )

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    assert "evaluation_gate" not in task.context
    criterion = task.plan_output["compute_result"]["criterion"]
    assert criterion["met"] is False
    assert criterion["measurable"] is True
    assert criterion["value"] == pytest.approx(10.0)
    assert task.context["compute_success_criterion"]["met"] is False


@pytest.mark.asyncio
async def test_compute_completion_absent_metric_completes_and_records(
    db_session, _quiet_compute_io
):
    """A declared experiment-domain metric the command did NOT report (absent from
    the terminal artifact) completes artifact delivery while recording the
    metric was not measurable."""
    task = await _make_compute_task_with_criterion(db_session, "accuracy", ">=", 0.9)

    # Real terminal artifact shape: no 'accuracy' key reported.
    await _handle_compute_experiment_completion(task, _result_artifact(), db_session)

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    assert "evaluation_gate" not in task.context
    criterion = task.plan_output["compute_result"]["criterion"]
    assert criterion["measurable"] is False
    assert criterion["met"] is False
    assert criterion["metric"] == "accuracy"
    assert criterion["value"] is None
    ctx_criterion = task.context["compute_success_criterion"]
    assert ctx_criterion["measurable"] is False
    assert ctx_criterion["met"] is False
    assert ctx_criterion["metric"] == "accuracy"
    assert ctx_criterion["value"] is None


# ── Release-echo success criteria: backend-derived, always evaluable ─────────


async def _make_timed_out_compute_task_with_job(
    db_session,
    metric: str,
    op: str,
    threshold: float,
    *,
    echo_sha: str | None = None,
    expected_sha: str | None = None,
) -> tuple["Task", "ComputeJob"]:
    """Create a task with a declared success criterion and a TIMED_OUT compute job.

    Sets up a realistic TIMED_OUT scenario: the job has terminal status and
    optionally has a release_echo_sha in its polling_metadata.
    """
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

    job = ComputeJob(
        project_id=task.project_id,
        task_id=task.id,
        backend="local",
        status=ComputeJobStatus.TIMED_OUT,
        accrued_cost_usd=0.05,
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    polling_meta: dict = {}
    if echo_sha is not None:
        polling_meta["release_echo_sha"] = echo_sha
    if expected_sha is not None:
        polling_meta["live_release_evidence"] = {
            "promoted_sha": expected_sha,
            "aggregate_serving_status": "serving_confirmed",
            "release_version_number": 10,
        }
    job.polling_metadata = polling_meta
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    return task, job


@pytest.mark.asyncio
async def test_release_echo_criterion_evaluable_on_timed_out_job(
    db_session, _quiet_compute_io
):
    """A TIMED_OUT job with a captured echo SHA makes release_echo_present
    evaluable as a numeric 1.0 — even with no COMPUTE_RESULT in the logs."""
    task, job = await _make_timed_out_compute_task_with_job(
        db_session,
        "release_echo_present",
        "==",
        1.0,
        echo_sha="abc123",
        expected_sha="abc123",
    )

    # No COMPUTE_RESULT emitted — agent timed out (empty/None logs).
    logs = json.dumps({"outcome": "TIMED_OUT", "final_cost_usd": 0.05})
    await _handle_compute_experiment_completion(task, logs, db_session)

    await db_session.refresh(task)
    ctx_criterion = task.context["compute_success_criterion"]
    assert ctx_criterion["metric"] == "release_echo_present"
    assert ctx_criterion["measurable"] is True
    assert ctx_criterion["value"] == pytest.approx(1.0)
    assert ctx_criterion["met"] is True
    assert ctx_criterion["binding_status"] == "verified"


@pytest.mark.asyncio
async def test_release_echo_present_absent_echo_evaluable_as_fail(
    db_session, _quiet_compute_io
):
    """A TIMED_OUT job with NO echo SHA still makes release_echo_present evaluable
    as a numeric 0.0 and met=False — NOT absent/unmeasurable/unreported."""
    task, job = await _make_timed_out_compute_task_with_job(
        db_session,
        "release_echo_present",
        "==",
        1.0,
        echo_sha=None,
        expected_sha="expected-sha-abc",
    )

    logs = json.dumps({"outcome": "TIMED_OUT", "final_cost_usd": 0.05})
    await _handle_compute_experiment_completion(task, logs, db_session)

    await db_session.refresh(task)
    ctx_criterion = task.context["compute_success_criterion"]
    assert ctx_criterion["metric"] == "release_echo_present"
    assert ctx_criterion["measurable"] is True
    assert ctx_criterion["value"] == pytest.approx(0.0)
    assert ctx_criterion["met"] is False
    assert ctx_criterion["binding_status"] == "absent"


@pytest.mark.asyncio
async def test_release_echo_verified_criterion_mismatched_sha_fails(
    db_session, _quiet_compute_io
):
    """A job with a mismatched echo SHA fails release_echo_verified (value=0.0)
    but passes release_echo_present (value=1.0), and binding_status='mismatched'
    is exposed on the criterion record."""
    task, job = await _make_timed_out_compute_task_with_job(
        db_session,
        "release_echo_verified",
        "==",
        1.0,
        echo_sha="actual-sha",
        expected_sha="expected-sha",
    )

    logs = json.dumps({"outcome": "TIMED_OUT", "final_cost_usd": 0.05})
    await _handle_compute_experiment_completion(task, logs, db_session)

    await db_session.refresh(task)
    ctx_criterion = task.context["compute_success_criterion"]
    assert ctx_criterion["metric"] == "release_echo_verified"
    assert ctx_criterion["measurable"] is True
    assert ctx_criterion["value"] == pytest.approx(0.0)
    assert ctx_criterion["met"] is False
    assert ctx_criterion["binding_status"] == "mismatched"


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
    # The knowledge document id is recorded so the acceptance criterion is
    # verifiable from the task record alone (no separate KD query needed).
    assert "id" in task.plan_output["knowledge_document"]
    assert task.plan_output["knowledge_document"]["id"] == str(doc.id)


@pytest.mark.asyncio
async def test_compute_knowledge_persistence_internal_failure_stamps_plan_output(
    db_session, _quiet_compute_io
):
    """When persist_compute_experiment_knowledge raises internally, the best-effort
    wrapper stamps plan_output with persisted=False and the task stays COMPLETED."""
    task = await _make_compute_task_with_identity(
        db_session, metric="accuracy", op=">=", threshold=0.9
    )

    with patch(
        "app.knowledge_completion.persist_compute_experiment_knowledge",
        new=AsyncMock(side_effect=RuntimeError("simulated persistence failure")),
    ):
        await _handle_compute_experiment_completion(
            task, _result_artifact(accuracy=0.93), db_session
        )

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    assert task.plan_output["compute_result"]["outcome"] == "SUCCESS"
    assert task.plan_output.get("knowledge_document") == {"persisted": False}
    # No KnowledgeDocument was created.
    assert await _knowledge_docs_for(db_session, task) == []


@pytest.mark.asyncio
async def test_compute_knowledge_persistence_db_failure_stamps_plan_output_after_rollback(
    db_session, _quiet_compute_io
):
    """Production-shaped: when persist_compute_experiment_knowledge fails after
    executing a DB statement (leaving the session in pending-rollback state),
    the wrapper must rollback the poisoned transaction before stamping
    plan_output={persisted: false} in a clean transaction.

    Without the rollback, the fallback commit raises PendingRollbackError,
    is silently swallowed, and the task is left without the failure marker.
    """
    from sqlalchemy import text

    task = await _make_compute_task_with_identity(
        db_session, metric="accuracy", op=">=", threshold=0.9
    )

    async def _db_level_failing_persist(task_arg, *, session, **_kwargs):
        # Execute a statement that fails at the DB level, leaving the session
        # in pending-rollback state without rolling back — this is the
        # production-shaped failure a flush/commit error would produce.
        try:
            await session.execute(text("SELECT 1 FROM table_that_must_not_exist_xyz_abc"))
        except Exception:
            pass  # intentionally leave session in pending-rollback state
        raise RuntimeError("simulated DB-level persistence failure")

    with patch(
        "app.knowledge_completion.persist_compute_experiment_knowledge",
        new=_db_level_failing_persist,
    ):
        await _handle_compute_experiment_completion(
            task, _result_artifact(accuracy=0.93), db_session
        )

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    assert task.plan_output.get("knowledge_document") == {"persisted": False}
    assert await _knowledge_docs_for(db_session, task) == []


@pytest.mark.asyncio
async def test_compute_unmet_criterion_persists_honest_negative(
    db_session, _quiet_compute_io
):
    """An experiment that finishes but misses its declared criterion completes
    and records an honest negative result as knowledge."""
    task = await _make_compute_task_with_identity(
        db_session, metric="accuracy", op=">=", threshold=0.9
    )

    await _handle_compute_experiment_completion(
        task, _result_artifact(accuracy=0.42), db_session
    )

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
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
    job.polling_metadata = {
        **(job.polling_metadata or {}),
        "result_metrics": {"accuracy": accuracy},
    }
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
    """Completion persists deterministic knowledge; explicit analysis upgrades it.

    Runs a compute_experiment to terminal completion, simulates VM teardown,
    re-reads the durable artifact, and asserts the persisted KnowledgeDocument
    first carries the deterministic artifact-resource result without an LLM call,
    then carries REAL artifact/metric citations after the explicit LLM step.
    The paired deterministic run remains searchable measured knowledge, but not
    acceptance evidence.
    """
    from app.knowledge_completion import analyze_compute_experiment_knowledge
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

    with patch("anthropic.AsyncAnthropic", mock_cls):
        await _handle_compute_experiment_completion(task, logs, db_session)

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    mock_create.assert_not_awaited()

    docs = await _knowledge_docs_for(db_session, task)
    assert len(docs) == 1
    assert docs[0].forward_quality_verdict["analysis_mode"] == "deterministic"
    assert "Compute experiment verdict" in docs[0].body

    with (
        patch.dict(
            os.environ,
            {"ANTHROPIC_API_KEY": "test-key", "DISABLE_LLM_COMPUTE_ANALYSIS": ""},
        ),
        patch("anthropic.AsyncAnthropic", mock_cls),
    ):
        analysis_result = await analyze_compute_experiment_knowledge(
            task,
            outcome="SUCCESS",
            cost=0.37,
            criterion={
                "metric": "accuracy",
                "operator": ">=",
                "threshold": 0.9,
                "value": 0.93,
                "met": True,
                "measurable": True,
            },
            result_reference=result_reference,
            session=db_session,
            logs=logs,
        )
    assert analysis_result["status"] == "upgraded"

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
    assert detail["analysis_fallback_reason"] is None
    index = await build_knowledge_index(db_session, task.project_id)
    assert index[0]["knowledge_acceptance_evidence"] is True
    assert index[0]["compute_acceptance_grade"] == "acceptance"
    assert index[0]["compute_acceptance_reason"] == "llm_artifact_cited"
    assert index[0]["analysis_fallback_reason"] is None
    substance = await fetch_knowledge_substance_index(db_session, task.project_id)
    assert substance[0].forward_quality_verdict["knowledge_acceptance_evidence"] is True
    assert substance[0].forward_quality_verdict["compute_acceptance_grade"] == "acceptance"

    deterministic_task = await _make_compute_task_with_identity(
        db_session, metric="accuracy", op=">=", threshold=0.9
    )
    deterministic_job, _ = await _setup_durable_job_with_artifact(
        db_session, deterministic_task
    )
    deterministic_logs = _result_artifact(
        job_id=str(deterministic_job.id),
        accuracy=0.93,
        result_reference=result_reference,
    )
    await _handle_compute_experiment_completion(
        deterministic_task, deterministic_logs, db_session
    )

    await db_session.refresh(deterministic_task)
    deterministic_docs = await _knowledge_docs_for(db_session, deterministic_task)
    assert len(deterministic_docs) == 1
    deterministic_verdict = deterministic_docs[0].forward_quality_verdict
    assert deterministic_verdict["analysis_mode"] == "deterministic"
    assert deterministic_verdict["knowledge_acceptance_evidence"] is False
    assert deterministic_verdict["compute_acceptance_grade"] == "measured_only"
    assert deterministic_verdict["compute_acceptance_reason"] == "analysis_mode_not_llm"
    deterministic_detail = serialize_knowledge_document(
        deterministic_docs[0], include_body=True
    )
    assert deterministic_detail["knowledge_acceptance_evidence"] is False
    assert deterministic_detail["compute_acceptance_grade"] == "measured_only"
    assert deterministic_detail["analysis_fallback_reason"] is None
    deterministic_index = await build_knowledge_index(db_session, deterministic_task.project_id)
    assert deterministic_index[0]["compute_acceptance_reason"] == "analysis_mode_not_llm"
    assert deterministic_index[0]["analysis_fallback_reason"] is None
    deterministic_substance = await fetch_knowledge_substance_index(
        db_session, deterministic_task.project_id
    )
    assert deterministic_substance[0].forward_quality_verdict["knowledge_acceptance_evidence"] is False
    assert deterministic_substance[0].forward_quality_verdict["compute_acceptance_grade"] == "measured_only"
    assert (
        deterministic_task.plan_output["knowledge_document"][
            "knowledge_acceptance_evidence"
        ]
        is False
    )
    assert (
        deterministic_task.plan_output["knowledge_document"]["compute_acceptance_grade"]
        == "measured_only"
    )
    assert deterministic_task.plan_output["knowledge_document"]["analysis_fallback_reason"] is None
    non_compute_doc = KnowledgeDocument(
        project_id=task.project_id,
        source_task_id=task.id,
        source_kind=SourceKind.DESIGN.value,
        title="Design note",
        summary="Design summary",
        body="Design body",
        topic="design",
        tags=[],
        source_citations=[],
        forward_quality_verdict={"passed": True, "reason": "ok", "strategy": "forward-quality"},
    )
    non_compute_detail = serialize_knowledge_document(non_compute_doc, include_body=True)
    assert non_compute_detail["analysis_fallback_reason"] is None
    assert evaluate_task_outcome(deterministic_task).status == "neutral"


@pytest.mark.asyncio
async def test_live_proof_knowledge_document_carries_release_evidence(
    db_session, mocker, _quiet_compute_io
):
    from app.knowledge_query import serialize_knowledge_document

    mocker.patch(
        "app.storage.generate_download_signed_url",
        new=AsyncMock(side_effect=RuntimeError("no gcs in tests")),
    )

    task = await _make_compute_task_with_identity(
        db_session, metric="accuracy", op=">=", threshold=0.9
    )
    task.context = {
        **(task.context or {}),
        "live_release_proof_marker": True,
    }
    served_at = datetime.utcnow()
    version = ReleaseVersion(
        project_id=task.project_id,
        version_number=8,
        status="promoted",
        promoted_sha="liveabc123",
        promoted_at=served_at,
        serving_status="serving_confirmed",
        served_sha="liveabc123",
        served_at=served_at,
    )
    db_session.add_all([task, version])
    await db_session.flush()
    db_session.add_all(
        [
            ReleaseDeployArtifact(
                version_id=version.id,
                artifact_kind="backend_revision",
                serving_status="serving_confirmed",
                served_identifier="liveabc123",
                served_at=served_at,
            ),
            ReleaseDeployArtifact(
                version_id=version.id,
                artifact_kind="agent_image",
                serving_status="serving_confirmed",
                served_identifier="liveabc123",
                served_at=served_at,
            ),
        ]
    )
    await db_session.commit()
    await db_session.refresh(task)

    job, artifact_uri = await _setup_durable_job_with_artifact(db_session, task)
    older_release_evidence = {
        "promoted_sha": "liveabc123",
        "aggregate_serving_status": "serving_confirmed",
        "release_version_number": 8,
    }
    job.polling_metadata = {
        **(job.polling_metadata or {}),
        "live_release_evidence": older_release_evidence,
    }
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    newer_job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="python rerun.py",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )
    newer_job.status = ComputeJobStatus.SUCCEEDED
    newer_job.outcome = ComputeJobOutcome.SUCCESS
    newer_job.accrued_cost_usd = 0.11
    newer_job.final_cost_usd = 0.11
    newer_job.terminal_reason = "compute job finished"
    newer_job.completed_at = datetime.utcnow()
    db_session.add(newer_job)
    await db_session.commit()
    await db_session.refresh(newer_job)

    metric_ref = f"{artifact_uri}#metric:accuracy=0.93"
    mock_cls, _mock_create = _mock_anthropic_returning([artifact_uri, metric_ref])
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
    docs = await _knowledge_docs_for(db_session, task)
    assert len(docs) == 1
    doc = docs[0]
    evidence = doc.forward_quality_verdict["release_evidence"]
    assert evidence == older_release_evidence
    assert task.plan_output["knowledge_document"]["release_evidence"] == older_release_evidence

    detail = serialize_knowledge_document(doc, include_body=True)
    assert detail["release_evidence"] == older_release_evidence


@pytest.mark.asyncio
async def test_ordinary_compute_knowledge_document_has_no_release_evidence(
    db_session, _quiet_compute_io
):
    from app.knowledge_query import serialize_knowledge_document

    task = await _make_compute_task_with_identity(
        db_session, metric="accuracy", op=">=", threshold=0.9
    )
    job, _artifact_uri = await _setup_durable_job_with_artifact(db_session, task)
    logs = _result_artifact(job_id=str(job.id), accuracy=0.93)

    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
        await _handle_compute_experiment_completion(task, logs, db_session)

    await db_session.refresh(task)
    docs = await _knowledge_docs_for(db_session, task)
    assert len(docs) == 1
    doc = docs[0]
    assert doc.forward_quality_verdict.get("release_evidence") is None
    assert task.plan_output["knowledge_document"]["release_evidence"] is None

    detail = serialize_knowledge_document(doc, include_body=True)
    assert detail["release_evidence"] is None


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
    assert doc.forward_quality_verdict["analysis_mode"] == "deterministic"
    assert doc.forward_quality_verdict["outcome"] == "SUCCESS"
    # Deterministic template: https:// result_reference is not a durable citation,
    # so source_citations is empty. The body is the hardcoded verdict markdown.
    assert doc.source_citations == []
    assert "Compute experiment verdict" in doc.body


@pytest.mark.asyncio
async def test_compute_experiment_knowledge_falls_back_on_ungrounded_citation(
    db_session, mocker, _quiet_compute_io
):
    """Ungrounded explicit analysis reports unavailable and leaves the doc intact."""
    from app.knowledge_completion import analyze_compute_experiment_knowledge

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
        result = await analyze_compute_experiment_knowledge(
            task,
            outcome="SUCCESS",
            cost=0.37,
            criterion={
                "metric": "accuracy",
                "operator": ">=",
                "threshold": 0.9,
                "value": 0.93,
                "met": True,
                "measurable": True,
            },
            result_reference=result_reference,
            session=db_session,
            logs=logs,
        )
    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    docs = await _knowledge_docs_for(db_session, task)
    assert len(docs) == 1
    assert result == {
        "status": "analysis_unavailable",
        "reason": "citations-ungrounded",
        "analysis_mode": "unavailable",
        "knowledge_document_id": str(docs[0].id),
    }
    assert docs[0].forward_quality_verdict["analysis_mode"] == "deterministic"
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


# ── Explicit analysis upgrade: deterministic → llm ──────────────────────────


@pytest.mark.asyncio
async def test_compute_knowledge_explicit_analysis_upgrades_deterministic_to_llm(
    db_session, mocker, _quiet_compute_io
):
    """The explicit LLM step upgrades the stored deterministic document in-place.

    This is the acceptance-evidence test: production code in
    _persist_compute_knowledge_document performs the upgrade (not a no-op).
    """
    from app.knowledge_completion import analyze_compute_experiment_knowledge

    mocker.patch(
        "app.storage.generate_download_signed_url",
        new=AsyncMock(side_effect=RuntimeError("no gcs in tests")),
    )

    task = await _make_compute_task_with_identity(
        db_session, metric="accuracy", op=">=", threshold=0.9
    )
    job, artifact_uri = await _setup_durable_job_with_artifact(db_session, task)
    release_evidence = {
        "promoted_sha": "upgradeabc123",
        "aggregate_serving_status": "serving_confirmed",
        "release_version_number": 9,
    }
    job.polling_metadata = {
        **(job.polling_metadata or {}),
        "live_release_evidence": release_evidence,
    }
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    logs = _result_artifact(job_id=str(job.id), accuracy=0.93)

    # ── Completion pass: deterministic document persisted, no LLM required ──
    await _handle_compute_experiment_completion(task, logs, db_session)

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED

    docs_after_first = await _knowledge_docs_for(db_session, task)
    assert len(docs_after_first) == 1
    assert docs_after_first[0].forward_quality_verdict["analysis_mode"] == "deterministic"
    assert (
        docs_after_first[0].forward_quality_verdict["release_evidence"]
        == release_evidence
    )
    deterministic_body = docs_after_first[0].body
    assert "Compute experiment verdict" in deterministic_body

    # ── Explicit analysis: LLM available → document upgraded in-place ────────
    metric_ref = f"{artifact_uri}#metric:accuracy=0.93"
    mock_cls, mock_create = _mock_anthropic_returning([artifact_uri, metric_ref])

    with (
        patch.dict(
            os.environ,
            {"ANTHROPIC_API_KEY": "test-key", "DISABLE_LLM_COMPUTE_ANALYSIS": ""},
        ),
        patch("anthropic.AsyncAnthropic", mock_cls),
    ):
        analysis_result = await analyze_compute_experiment_knowledge(
            task,
            outcome="SUCCESS",
            cost=0.37,
            criterion={
                "metric": "accuracy",
                "operator": ">=",
                "threshold": 0.9,
                "value": 0.93,
                "met": True,
                "measurable": True,
            },
            result_reference=None,
            session=db_session,
            logs=logs,
        )
    assert analysis_result["status"] == "upgraded"

    await db_session.refresh(task)

    docs_after_second = await _knowledge_docs_for(db_session, task)
    # Must be exactly ONE row — upgrade is in-place, not a new insert.
    assert len(docs_after_second) == 1
    doc = docs_after_second[0]

    # Content reflects the LLM analysis, not the deterministic fallback.
    verdict = doc.forward_quality_verdict
    assert verdict["analysis_mode"] == "llm", (
        "persisted doc must be upgraded from deterministic to llm explicitly"
    )
    assert verdict["release_evidence"] == release_evidence
    assert artifact_uri in doc.source_citations
    assert metric_ref in doc.source_citations
    assert "analysis" in doc.body.lower()
    # The deterministic template body was replaced.
    assert "Compute experiment verdict" not in doc.body

    # plan_output mirrors the upgraded values.
    assert task.plan_output["knowledge_document"]["analysis_mode"] == "llm"
    assert task.plan_output["knowledge_document"]["citation_count"] >= 2
    assert (
        task.plan_output["knowledge_document"]["release_evidence"]
        == release_evidence
    )

    # The LLM was actually invoked on the second pass.
    mock_create.assert_awaited_once()


@pytest.mark.asyncio
async def test_compute_knowledge_does_not_downgrade_llm_when_analysis_unavailable(
    db_session, mocker, _quiet_compute_io
):
    """An unavailable explicit analysis does NOT overwrite an existing LLM doc."""
    from app.knowledge_completion import analyze_compute_experiment_knowledge

    mocker.patch(
        "app.storage.generate_download_signed_url",
        new=AsyncMock(side_effect=RuntimeError("no gcs in tests")),
    )

    task = await _make_compute_task_with_identity(
        db_session, metric="accuracy", op=">=", threshold=0.9
    )
    job, artifact_uri = await _setup_durable_job_with_artifact(db_session, task)

    logs = _result_artifact(job_id=str(job.id), accuracy=0.93)

    # ── Completion persists deterministic; explicit analysis upgrades to llm ─
    metric_ref = f"{artifact_uri}#metric:accuracy=0.93"
    mock_cls, _ = _mock_anthropic_returning([artifact_uri, metric_ref])

    await _handle_compute_experiment_completion(task, logs, db_session)
    with (
        patch.dict(
            os.environ,
            {"ANTHROPIC_API_KEY": "test-key", "DISABLE_LLM_COMPUTE_ANALYSIS": ""},
        ),
        patch("anthropic.AsyncAnthropic", mock_cls),
    ):
        analysis_result = await analyze_compute_experiment_knowledge(
            task,
            outcome="SUCCESS",
            cost=0.37,
            criterion={
                "metric": "accuracy",
                "operator": ">=",
                "threshold": 0.9,
                "value": 0.93,
                "met": True,
                "measurable": True,
            },
            result_reference=None,
            session=db_session,
            logs=logs,
        )
    assert analysis_result["status"] == "upgraded"

    await db_session.refresh(task)
    docs_after_first = await _knowledge_docs_for(db_session, task)
    assert len(docs_after_first) == 1
    assert docs_after_first[0].forward_quality_verdict["analysis_mode"] == "llm"
    llm_body = docs_after_first[0].body

    # ── Second analysis: no API key → unavailable, must NOT downgrade ────────
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
        unavailable = await analyze_compute_experiment_knowledge(
            task,
            outcome="SUCCESS",
            cost=0.37,
            criterion=None,
            result_reference=None,
            session=db_session,
            logs=logs,
        )
    await db_session.refresh(task)
    docs_after_second = await _knowledge_docs_for(db_session, task)
    assert len(docs_after_second) == 1
    doc = docs_after_second[0]
    assert unavailable == {
        "status": "analysis_unavailable",
        "reason": "precondition-not-met",
        "analysis_mode": "unavailable",
        "knowledge_document_id": str(doc.id),
    }

    # The llm verdict and body must be preserved.
    assert doc.forward_quality_verdict["analysis_mode"] == "llm", (
        "llm document must not be downgraded when later analysis is unavailable"
    )
    assert doc.body == llm_body
    assert task.plan_output["knowledge_document"]["analysis_mode"] == "llm"


@pytest.mark.asyncio
async def test_compute_knowledge_same_mode_retry_is_idempotent(
    db_session, _quiet_compute_io
):
    """Two identical deterministic persistence calls result in exactly ONE row with
    no errors — idempotent retry is safe."""
    task = await _make_compute_task(db_session)
    logs = _result_artifact()

    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
        await _handle_compute_experiment_completion(task, logs, db_session)
        await _handle_compute_experiment_completion(task, logs, db_session)

    await db_session.refresh(task)
    docs = await _knowledge_docs_for(db_session, task)
    assert len(docs) == 1
    assert docs[0].forward_quality_verdict["analysis_mode"] == "deterministic"


@pytest.mark.asyncio
async def test_explicit_analysis_reports_unavailable_when_precondition_not_met(
    db_session, _quiet_compute_io
):
    """No API key is reported honestly and leaves deterministic knowledge intact."""
    from app.knowledge_completion import analyze_compute_experiment_knowledge

    task = await _make_compute_task(db_session)
    logs = _result_artifact()

    await _handle_compute_experiment_completion(task, logs, db_session)
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
        result = await analyze_compute_experiment_knowledge(
            task,
            outcome="SUCCESS",
            cost=0.37,
            criterion=None,
            result_reference=None,
            session=db_session,
            logs=logs,
        )
    docs = await _knowledge_docs_for(db_session, task)
    assert len(docs) == 1
    assert result == {
        "status": "analysis_unavailable",
        "reason": "precondition-not-met",
        "analysis_mode": "unavailable",
        "knowledge_document_id": str(docs[0].id),
    }
    verdict = docs[0].forward_quality_verdict
    assert verdict["analysis_mode"] == "deterministic"
    assert "fallback_reason" not in verdict


@pytest.mark.asyncio
async def test_explicit_analysis_reports_unavailable_when_analysis_disabled(
    db_session, _quiet_compute_io
):
    """When analysis is disabled, the explicit step reports unavailable."""
    from app.knowledge_completion import analyze_compute_experiment_knowledge

    task = await _make_compute_task(db_session)
    logs = _result_artifact()

    await _handle_compute_experiment_completion(task, logs, db_session)
    with patch.dict(
        os.environ,
        {"ANTHROPIC_API_KEY": "test-key", "DISABLE_LLM_COMPUTE_ANALYSIS": "1"},
    ):
        result = await analyze_compute_experiment_knowledge(
            task,
            outcome="SUCCESS",
            cost=0.37,
            criterion=None,
            result_reference=None,
            session=db_session,
            logs=logs,
        )
    docs = await _knowledge_docs_for(db_session, task)
    assert len(docs) == 1
    assert result == {
        "status": "analysis_unavailable",
        "reason": "analysis-disabled",
        "analysis_mode": "unavailable",
        "knowledge_document_id": str(docs[0].id),
    }
    verdict = docs[0].forward_quality_verdict
    assert verdict["analysis_mode"] == "deterministic"
    assert "fallback_reason" not in verdict


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

    Completion now auto-delivers the cited analysis, so the upgrade happens
    without an explicit operator analyze call.
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
        # Completion auto-delivers the cited analysis — no explicit operator call.
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


# ── artifact_delivery record stamping ──────────────────────────────────────


@pytest.mark.asyncio
async def test_artifact_delivery_record_durable_uri_stamped(
    db_session, _quiet_compute_io
):
    """A gs:// result_reference creates a COMPUTE_RESULT Artifact; completion
    stamps artifact_delivery={delivered: True, durability: durable, artifact_id, result_reference}
    into compute_result."""
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

    gs_uri = "gs://blueprint-agent-artifacts/compute-jobs/result.json"
    await _handle_compute_experiment_completion(
        task,
        _result_artifact(job_id=str(job.id), result_reference=gs_uri),
        db_session,
    )

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    delivery = task.plan_output["compute_result"]["artifact_delivery"]
    assert delivery["delivered"] is True
    assert delivery["durability"] == "durable"
    assert delivery["result_reference"] == gs_uri
    # No logical_key in payload means no Artifact row is created, so artifact_id is absent.
    assert "artifact_id" not in delivery


@pytest.mark.asyncio
async def test_artifact_delivery_record_transient_uri_stamped(
    db_session, _quiet_compute_io
):
    """An https:// result_reference stamps artifact_delivery={delivered: False, durability: transient}."""
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

    https_uri = "https://signed.example.com/jobs/job-abc/result.json?token=xyz"
    await _handle_compute_experiment_completion(
        task,
        _result_artifact(job_id=str(job.id), result_reference=https_uri),
        db_session,
    )

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    delivery = task.plan_output["compute_result"]["artifact_delivery"]
    assert delivery["delivered"] is False
    assert delivery["durability"] == "transient"
    assert delivery["result_reference"] == https_uri


@pytest.mark.asyncio
async def test_artifact_delivery_record_absent_stamped(db_session, _quiet_compute_io):
    """No result_reference stamps artifact_delivery={delivered: False, durability: none}."""
    task = await _make_compute_task(db_session)

    await _handle_compute_experiment_completion(task, _result_artifact(), db_session)

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    delivery = task.plan_output["compute_result"]["artifact_delivery"]
    assert delivery["delivered"] is False
    assert delivery["durability"] == "none"


# ── require_durable_artifact Tier 2 gate ───────────────────────────────────


@pytest.mark.asyncio
async def test_require_durable_artifact_passes_with_durable_uri(
    db_session, _quiet_compute_io
):
    """require_durable_artifact=True + gs:// result → COMPLETED."""
    task = await _make_compute_task(db_session)
    task.payloads = [
        {
            "type": "compute_experiment",
            "base_branch": "main",
            "require_durable_artifact": True,
        }
    ]
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

    gs_uri = "gs://blueprint-agent-artifacts/compute-jobs/result.json"
    await _handle_compute_experiment_completion(
        task,
        _result_artifact(job_id=str(job.id), result_reference=gs_uri),
        db_session,
    )

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    delivery = task.plan_output["compute_result"]["artifact_delivery"]
    assert delivery["delivered"] is True
    assert delivery["durability"] == "durable"


@pytest.mark.asyncio
async def test_require_durable_artifact_gate_fails_without_durable_artifact(
    db_session, _quiet_compute_io
):
    """require_durable_artifact=True + no artifact → FAILED with explicit miss reason."""
    task = await _make_compute_task(db_session)
    task.payloads = [
        {
            "type": "compute_experiment",
            "base_branch": "main",
            "require_durable_artifact": True,
        }
    ]
    db_session.add(task)
    await db_session.commit()

    await _handle_compute_experiment_completion(task, _result_artifact(), db_session)

    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED
    gate = task.context["evaluation_gate"]
    assert gate["passed"] is False
    assert gate["strategy"] == "artifact-delivery"
    assert "require_durable_artifact" in gate["reason"]
    assert "none" in gate["reason"]


@pytest.mark.asyncio
async def test_require_durable_artifact_false_does_not_gate(
    db_session, _quiet_compute_io
):
    """require_durable_artifact absent/False + no artifact → COMPLETED (Tier 1 only)."""
    task = await _make_compute_task(db_session)
    task.payloads = [
        {
            "type": "compute_experiment",
            "base_branch": "main",
            "require_durable_artifact": False,
        }
    ]
    db_session.add(task)
    await db_session.commit()

    await _handle_compute_experiment_completion(task, _result_artifact(), db_session)

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    assert "evaluation_gate" not in task.context
    delivery = task.plan_output["compute_result"]["artifact_delivery"]
    assert delivery["delivered"] is False
    assert delivery["durability"] == "none"


@pytest.mark.asyncio
async def test_require_durable_artifact_gate_fails_with_linked_transient_artifact(
    db_session, _quiet_compute_io
):
    """require_durable_artifact=True + linked Artifact with https:// storage_uri and
    no durable result_reference → FAILED.

    Artifact.storage_uri is not guaranteed durable (agents can supply https:// signed
    URLs). The gate must apply the durable-URI predicate to storage_uri, not treat any
    non-None storage_uri as durable.
    """
    task = await _make_compute_task(db_session)
    task.payloads = [
        {
            "type": "compute_experiment",
            "base_branch": "main",
            "require_durable_artifact": True,
        }
    ]
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
    job.accrued_cost_usd = 0.25
    job.final_cost_usd = 0.25
    job.terminal_reason = "compute job finished"
    job.completed_at = datetime.utcnow()

    # Attach an Artifact with a transient https:// storage_uri (no gs:// / s3:// etc.)
    artifact = Artifact(
        task_id=task.id,
        artifact_type=ArtifactType.COMPUTE_RESULT,
        storage_uri="https://signed-download.example.com/result.tar.gz?token=xyz",
    )
    db_session.add(artifact)
    await db_session.commit()
    await db_session.refresh(artifact)

    job.artifact_id = artifact.id
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    # result_reference is also transient (https://) — no durable URI anywhere.
    https_ref = "https://example.com/jobs/result.json?token=abc"
    await _handle_compute_experiment_completion(
        task,
        _result_artifact(job_id=str(job.id), result_reference=https_ref),
        db_session,
    )

    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED, (
        "require_durable_artifact=True must fail when the linked Artifact has only "
        "an https:// storage_uri (transient); _derive_artifact_delivery_record must "
        "apply _is_durable_artifact_uri to storage_uri before marking delivered=True"
    )
    gate = task.context["evaluation_gate"]
    assert gate["passed"] is False
    assert gate["strategy"] == "artifact-delivery"
    assert "require_durable_artifact" in gate["reason"]
    delivery = task.plan_output["compute_result"]["artifact_delivery"]
    assert delivery["delivered"] is False
    assert delivery["durability"] == "transient"


# ── require_durable_artifact Tier 2 gate — PREREQUISITE mode ────────────────


def _prerequisite_context(extra: dict | None = None) -> dict:
    ctx = dict(extra or {})
    ctx[COMPUTE_EXPERIMENT_PREREQUISITE_MODE_CONTEXT_KEY] = (
        COMPUTE_EXPERIMENT_PREREQUISITE_MODE_ADVISORY
    )
    return ctx


@pytest.mark.asyncio
async def test_prerequisite_require_durable_artifact_fails_without_durable_uri(
    db_session, _quiet_compute_io
):
    """Prerequisite mode + require_durable_artifact=True + no durable artifact →
    FAILED. A measured verdict alone must NOT satisfy a prerequisite that demands
    a durable artifact; the Tier-2 gate now also fires in the prerequisite path."""
    task = await _make_compute_task(db_session)
    task.payloads = [
        {
            "type": "compute_experiment",
            "base_branch": "main",
            "require_durable_artifact": True,
        }
    ]
    task.context = _prerequisite_context()
    db_session.add(task)
    await db_session.commit()

    await _handle_compute_experiment_completion(task, _result_artifact(), db_session)

    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED
    gate = task.context["evaluation_gate"]
    assert gate["passed"] is False
    assert gate["strategy"] == "artifact-delivery"
    assert "require_durable_artifact" in gate["reason"]
    assert "none" in gate["reason"]


@pytest.mark.asyncio
async def test_prerequisite_require_durable_artifact_passes_with_durable_uri(
    db_session, _quiet_compute_io
):
    """Prerequisite mode + require_durable_artifact=True + gs:// durable artifact →
    COMPLETED. The gate fires only on non-delivery; durable delivery still
    completes."""
    task = await _make_compute_task(db_session)
    task.payloads = [
        {
            "type": "compute_experiment",
            "base_branch": "main",
            "require_durable_artifact": True,
        }
    ]
    task.context = _prerequisite_context()
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

    gs_uri = "gs://blueprint-agent-artifacts/compute-jobs/prereq-result.json"
    await _handle_compute_experiment_completion(
        task,
        _result_artifact(job_id=str(job.id), result_reference=gs_uri),
        db_session,
    )

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    assert "evaluation_gate" not in task.context
    delivery = task.plan_output["compute_result"]["artifact_delivery"]
    assert delivery["delivered"] is True
    assert delivery["durability"] == "durable"


@pytest.mark.asyncio
async def test_prerequisite_downstream_artifact_evidence_fails_without_durable_uri(
    db_session, _quiet_compute_io
):
    """Prerequisite mode + downstream_artifact acceptance evidence (no explicit
    flag) + no durable artifact → FAILED. Declaring a downstream_artifact implies
    the experiment must hand a durable artifact to its dependent."""
    task = await _make_compute_task(db_session)
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
    task.context = _prerequisite_context()
    db_session.add(task)
    await db_session.commit()

    await _handle_compute_experiment_completion(task, _result_artifact(), db_session)

    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED
    gate = task.context["evaluation_gate"]
    assert gate["passed"] is False
    assert gate["strategy"] == "artifact-delivery"
    assert "require_durable_artifact" in gate["reason"]


@pytest.mark.asyncio
async def test_prerequisite_no_flag_remains_fail_soft_without_durable(
    db_session, _quiet_compute_io
):
    """Prerequisite mode + no flag + no downstream_artifact evidence + no durable
    artifact → COMPLETED. Ordinary prerequisites stay fail-soft; the gate must
    not fire for them."""
    task = await _make_compute_task(db_session)
    task.payloads = [
        {
            "type": "compute_experiment",
            "base_branch": "main",
        }
    ]
    task.context = _prerequisite_context()
    db_session.add(task)
    await db_session.commit()

    await _handle_compute_experiment_completion(task, _result_artifact(), db_session)

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    assert "evaluation_gate" not in task.context
    delivery = task.plan_output["compute_result"]["artifact_delivery"]
    assert delivery["delivered"] is False
    assert delivery["durability"] == "none"


@pytest.mark.asyncio
async def test_completion_auto_delivers_cited_llm_analysis_acceptance_grade(
    db_session, mocker, _quiet_compute_io
):
    """Completion AUTONOMOUSLY upgrades the deterministic doc to a cited llm
    analysis — no operator analyze call — yielding an acceptance-grade document
    and a success task outcome.

    Proves the auto-delivery path: after the deterministic knowledge record
    exists, the same machinery the admin endpoint uses
    (``analyze_compute_experiment_knowledge``) runs automatically and upgrades
    ``analysis_mode`` to ``llm`` when durable artifact evidence grounds it.
    """
    from app.task_outcomes import evaluate_task_outcome

    mocker.patch(
        "app.storage.generate_download_signed_url",
        new=AsyncMock(side_effect=RuntimeError("no gcs in tests")),
    )
    task = await _make_compute_task_with_identity(
        db_session, metric="accuracy", op=">=", threshold=0.9
    )
    job, artifact_uri = await _setup_durable_job_with_artifact(db_session, task)
    metric_ref = f"{artifact_uri}#metric:accuracy=0.93"
    mock_cls, mock_create = _mock_anthropic_returning([artifact_uri, metric_ref])
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
    # The analysis was attempted automatically during completion — once.
    mock_create.assert_awaited_once()

    docs = await _knowledge_docs_for(db_session, task)
    assert len(docs) == 1
    verdict = docs[0].forward_quality_verdict
    assert verdict["analysis_mode"] == "llm"
    assert verdict["knowledge_acceptance_evidence"] is True
    assert verdict["compute_acceptance_grade"] == "acceptance"
    assert verdict["compute_acceptance_reason"] == "llm_artifact_cited"
    # Grounded in REAL durable artifact/metric evidence.
    assert artifact_uri in docs[0].source_citations
    # The compute_result the handler owns is untouched by the knowledge pass.
    assert task.plan_output["compute_result"]["outcome"] == "SUCCESS"
    assert task.plan_output["knowledge_document"]["analysis_mode"] == "llm"
    assert (
        task.plan_output["knowledge_document"]["compute_acceptance_grade"]
        == "acceptance"
    )
    # Acceptance-grade knowledge ⇒ a success task outcome.
    assert evaluate_task_outcome(task).status == "success"


@pytest.mark.asyncio
async def test_completion_auto_analysis_terminal_unavailable_stays_measured_only(
    db_session, _quiet_compute_io
):
    """A structurally-unavailable analysis persists a machine-readable
    ``unavailable_cause``, the task still COMPLETES, and the doc stays
    measured-only / non-acceptance — never silently credited, never retried.

    No re-coupling: the artifact-completion gate does not depend on the analysis,
    so the task reaches COMPLETED with the deterministic doc even though the
    cited analysis is unavailable.
    """
    from app.task_outcomes import evaluate_task_outcome

    task = await _make_compute_task_with_identity(
        db_session, metric="accuracy", op=">=", threshold=0.9
    )
    job, _artifact_uri = await _setup_durable_job_with_artifact(db_session, task)
    logs = _result_artifact(job_id=str(job.id), accuracy=0.93)

    # Operator-disabled analysis is a TERMINAL cause: stamped once, never retried.
    with patch.dict(
        os.environ,
        {"ANTHROPIC_API_KEY": "test-key", "DISABLE_LLM_COMPUTE_ANALYSIS": "1"},
    ):
        await _handle_compute_experiment_completion(task, logs, db_session)

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    docs = await _knowledge_docs_for(db_session, task)
    assert len(docs) == 1
    verdict = docs[0].forward_quality_verdict
    # Still deterministic / measured-only — the analysis never upgraded it.
    assert verdict["analysis_mode"] == "deterministic"
    assert verdict["compute_acceptance_grade"] == "measured_only"
    assert verdict["knowledge_acceptance_evidence"] is False
    # The terminal cause is durably stamped in a machine-readable form.
    assert verdict["unavailable_cause"] == "analysis-disabled"
    assert "analysis_attempted_at" in verdict
    # SUCCESS compute outcome but measured-only ⇒ NOT a success task outcome.
    assert evaluate_task_outcome(task).status == "neutral"


@pytest.mark.asyncio
async def test_recoverable_analysis_retries_via_wake_sweep_until_upgraded(
    db_session, mocker, _quiet_compute_io
):
    """A recoverable analysis error leaves the durable wake marker + a backoff
    retry timestamp; the compute-wake post-commit retry (the callee reaper_sweeps
    Pass 0 invokes for marked DELIVERED rows) re-attempts and converges on an
    acceptance-grade llm upgrade.
    """
    from app.models import TaskWakeRegistration, WakeAction, WakeDeliveryState
    from app.task_completion import (
        _COMPUTE_WAKE_NEXT_RETRY_KEY,
        _COMPUTE_WAKE_POST_COMMIT_META_KEY,
        run_compute_wake_post_commit,
    )

    mocker.patch(
        "app.storage.generate_download_signed_url",
        new=AsyncMock(side_effect=RuntimeError("no gcs in tests")),
    )
    task = await _make_compute_task_with_identity(
        db_session, metric="accuracy", op=">=", threshold=0.9
    )
    job, artifact_uri = await _setup_durable_job_with_artifact(db_session, task)
    metric_ref = f"{artifact_uri}#metric:accuracy=0.93"

    # A DELIVERED compute-wake registration carrying the durable post-commit
    # marker — exactly the row reaper_sweeps Pass 0 selects and re-attempts.
    reg = TaskWakeRegistration(
        task_id=task.id,
        project_id=task.project_id,
        wake_source_kind="compute_job_terminal",
        wake_source_ref=str(job.id),
        wake_action=WakeAction.FINALIZE,
        delivery_state=WakeDeliveryState.DELIVERED,
        delivered_at=datetime.utcnow(),
        source_metadata={_COMPUTE_WAKE_POST_COMMIT_META_KEY: {"attempts": 0}},
    )
    db_session.add(reg)
    await db_session.commit()
    await db_session.refresh(reg)

    success_doc = {
        "title": "Analysis: durable artifacts cleared the bar",
        "summary": "Grounded analysis citing the durable artifact bundle + metric.",
        "body": "# Analysis\n\nThe run cleared the bar; see the cited evidence.",
        "source_citations": [artifact_uri, metric_ref],
    }

    with patch.dict(
        os.environ,
        {"ANTHROPIC_API_KEY": "test-key", "DISABLE_LLM_COMPUTE_ANALYSIS": ""},
    ):
        # Attempt 1: the LLM call fails transiently (recoverable). Deterministic
        # knowledge persists, but the analysis cannot upgrade the doc yet.
        mocker.patch(
            "app.knowledge_completion._call_compute_analysis_llm",
            new=AsyncMock(return_value=None),
        )
        ok = await run_compute_wake_post_commit(db_session, reg)
        assert ok is False

        await db_session.refresh(reg)
        # The durable marker is KEPT and a backoff retry timestamp was written so
        # Pass 0 re-attempts after the window elapses.
        assert _COMPUTE_WAKE_POST_COMMIT_META_KEY in (reg.source_metadata or {})
        assert _COMPUTE_WAKE_NEXT_RETRY_KEY in (reg.source_metadata or {})
        docs = await _knowledge_docs_for(db_session, task)
        assert len(docs) == 1
        det_verdict = docs[0].forward_quality_verdict
        assert det_verdict["analysis_mode"] == "deterministic"
        assert det_verdict["unavailable_cause"] == "llm-error"

        # Attempt 2 (the sweep re-attempt): the LLM now succeeds → llm upgrade.
        mocker.patch(
            "app.knowledge_completion._call_compute_analysis_llm",
            new=AsyncMock(return_value=success_doc),
        )
        ok2 = await run_compute_wake_post_commit(db_session, reg)
        assert ok2 is True

    await db_session.refresh(reg)
    # Converged: the durable marker is cleared — no further retries.
    assert _COMPUTE_WAKE_POST_COMMIT_META_KEY not in (reg.source_metadata or {})
    assert _COMPUTE_WAKE_NEXT_RETRY_KEY not in (reg.source_metadata or {})
    docs = await _knowledge_docs_for(db_session, task)
    assert len(docs) == 1
    verdict = docs[0].forward_quality_verdict
    assert verdict["analysis_mode"] == "llm"
    assert verdict["compute_acceptance_grade"] == "acceptance"
    assert verdict["knowledge_acceptance_evidence"] is True
    assert artifact_uri in docs[0].source_citations
    # The unavailable_cause stamp from the failed attempt is gone after upgrade.
    assert "unavailable_cause" not in verdict


@pytest.mark.asyncio
async def test_no_backend_key_compute_analysis_upgrades_via_vm_substrate(
    db_session, mocker, _quiet_compute_io
):
    """Production-shaped (no backend ANTHROPIC_API_KEY): an autonomous compute
    completion routes the cited analysis onto the agent-VM centralized-accounts
    substrate instead of grading measured_only forever, and a grounded VM
    submission UPGRADES the doc to analysis_mode='llm' with an acceptance-grade
    reason — zero operator action and NEVER via a backend key (operator policy
    p-13f153e9).
    """
    from app.knowledge_completion import submit_compute_vm_analysis
    from app.models import TaskWakeRegistration, WakeAction, WakeDeliveryState
    from app.task_completion import (
        _COMPUTE_VM_ANALYSIS_ATTEMPTS_CONTEXT_KEY,
        _COMPUTE_VM_ANALYSIS_REQUEST_CONTEXT_KEY,
        _COMPUTE_WAKE_POST_COMMIT_META_KEY,
        run_compute_wake_post_commit,
    )

    mocker.patch(
        "app.storage.generate_download_signed_url",
        new=AsyncMock(side_effect=RuntimeError("no gcs in tests")),
    )

    task = await _make_compute_task_with_identity(
        db_session, metric="accuracy", op=">=", threshold=0.9
    )
    job, artifact_uri = await _setup_durable_job_with_artifact(db_session, task)
    metric_ref = f"{artifact_uri}#metric:accuracy=0.93"

    # A DELIVERED compute-wake registration — the production autonomous path that
    # persists durable knowledge AFTER the finalize commit.
    reg = TaskWakeRegistration(
        task_id=task.id,
        project_id=task.project_id,
        wake_source_kind="compute_job_terminal",
        wake_source_ref=str(job.id),
        wake_action=WakeAction.FINALIZE,
        delivery_state=WakeDeliveryState.DELIVERED,
        delivered_at=datetime.utcnow(),
        source_metadata={_COMPUTE_WAKE_POST_COMMIT_META_KEY: {"attempts": 0}},
    )
    db_session.add(reg)
    await db_session.commit()
    await db_session.refresh(reg)

    # ── Autonomous completion with NO backend key → route to the VM substrate ──
    with patch.dict(
        os.environ, {"ANTHROPIC_API_KEY": "", "DISABLE_LLM_COMPUTE_ANALYSIS": ""}
    ):
        ok = await run_compute_wake_post_commit(db_session, reg)
    assert ok is True

    await db_session.refresh(task)
    # The deterministic measured_only doc persisted as the durable base record.
    docs = await _knowledge_docs_for(db_session, task)
    assert len(docs) == 1
    verdict = docs[0].forward_quality_verdict
    assert verdict["analysis_mode"] == "deterministic"
    assert verdict["compute_acceptance_grade"] == "measured_only"
    assert verdict["knowledge_acceptance_evidence"] is False
    # NOT a dead-end terminal grade: the task is re-queued onto the VM substrate
    # carrying the backend-built analysis request bundle, bounded by an attempt
    # counter so a structurally-stuck job can never loop forever.
    assert task.status == TaskStatus.QUEUED
    bundle = (task.context or {}).get(_COMPUTE_VM_ANALYSIS_REQUEST_CONTEXT_KEY)
    assert isinstance(bundle, dict)
    assert bundle.get("prompt")
    assert bundle.get("result_logs")
    # The backend built the authoritative prompt grounded in the durable artifact.
    assert artifact_uri in bundle["prompt"]
    assert task.context[_COMPUTE_VM_ANALYSIS_ATTEMPTS_CONTEXT_KEY] == 1

    # ── Simulate the VM submitting a grounded analysis (the agent endpoint runs
    #    this same backend core; citation grounding stays backend-authoritative
    #    and never trusts the VM). Still NO backend ANTHROPIC_API_KEY. ──
    vm_document = {
        "title": "VM analysis: durable artifacts cleared the bar",
        "summary": "Grounded analysis citing the durable artifact bundle + metric.",
        "body": "# Analysis\n\nThe run cleared the bar; see the cited evidence.",
        "source_citations": [artifact_uri, metric_ref],
    }
    with patch.dict(
        os.environ, {"ANTHROPIC_API_KEY": "", "DISABLE_LLM_COMPUTE_ANALYSIS": ""}
    ):
        envelope = await submit_compute_vm_analysis(
            task,
            vm_document=vm_document,
            outcome="SUCCESS",
            cost=0.37,
            criterion={
                "metric": "accuracy",
                "operator": ">=",
                "threshold": 0.9,
                "value": 0.93,
                "met": True,
                "measurable": True,
            },
            result_reference=job.result_reference,
            session=db_session,
            logs=compute_jobs.build_compute_result_logs(job),
        )
    assert envelope["status"] == "upgraded"
    assert envelope["analysis_mode"] == "llm"

    docs = await _knowledge_docs_for(db_session, task)
    assert len(docs) == 1  # in-place upgrade, not a new row
    doc = docs[0]
    assert artifact_uri in doc.source_citations
    assert metric_ref in doc.source_citations
    upgraded = doc.forward_quality_verdict
    assert upgraded["analysis_mode"] == "llm"
    # Provenance: the model ran on the VM centralized-accounts substrate, NOT the
    # backend process — the whole point of operator policy p-13f153e9.
    assert upgraded["analysis_execution"] == "vm_substrate"
    assert upgraded["knowledge_acceptance_evidence"] is True
    assert upgraded["compute_acceptance_grade"] == "acceptance"
    assert upgraded["compute_acceptance_reason"] == "llm_artifact_cited"
    assert "analysis" in doc.body.lower()


@pytest.mark.asyncio
async def test_compute_vm_analysis_routing_is_bounded(
    db_session, mocker, _quiet_compute_io
):
    """The VM-substrate routing is bounded: once the per-task attempt ceiling is
    reached it stops re-queueing and leaves the deterministic measured_only doc
    intact rather than looping forever (and never downgrades it)."""
    from app.knowledge_completion import persist_compute_experiment_knowledge
    from app.task_completion import (
        _COMPUTE_VM_ANALYSIS_ATTEMPTS_CONTEXT_KEY,
        _COMPUTE_VM_ANALYSIS_MAX_ATTEMPTS,
        _route_compute_analysis_to_vm,
    )

    mocker.patch(
        "app.storage.generate_download_signed_url",
        new=AsyncMock(side_effect=RuntimeError("no gcs in tests")),
    )
    task = await _make_compute_task_with_identity(
        db_session, metric="accuracy", op=">=", threshold=0.9
    )
    job, _artifact_uri = await _setup_durable_job_with_artifact(db_session, task)
    logs = compute_jobs.build_compute_result_logs(job)
    criterion = {
        "metric": "accuracy",
        "operator": ">=",
        "threshold": 0.9,
        "value": 0.93,
        "met": True,
        "measurable": True,
    }
    # The deterministic measured_only doc is the durable base record.
    await persist_compute_experiment_knowledge(
        task,
        outcome="SUCCESS",
        cost=0.37,
        criterion=criterion,
        result_reference=job.result_reference,
        session=db_session,
        logs=logs,
    )

    # One below the ceiling: routing re-queues and bumps the counter to the cap.
    task.status = TaskStatus.COMPLETED
    task.context = {
        **(task.context or {}),
        _COMPUTE_VM_ANALYSIS_ATTEMPTS_CONTEXT_KEY: _COMPUTE_VM_ANALYSIS_MAX_ATTEMPTS - 1,
    }
    db_session.add(task)
    await db_session.commit()
    routed = await _route_compute_analysis_to_vm(task, logs, criterion, db_session)
    assert routed is True
    await db_session.refresh(task)
    assert task.status == TaskStatus.QUEUED
    assert (
        task.context[_COMPUTE_VM_ANALYSIS_ATTEMPTS_CONTEXT_KEY]
        == _COMPUTE_VM_ANALYSIS_MAX_ATTEMPTS
    )

    # At the ceiling: routing is a no-op — the structurally-stuck job does NOT
    # loop, and the deterministic measured_only doc is left intact.
    task.status = TaskStatus.COMPLETED
    db_session.add(task)
    await db_session.commit()
    routed_again = await _route_compute_analysis_to_vm(task, logs, criterion, db_session)
    assert routed_again is False
    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    docs = await _knowledge_docs_for(db_session, task)
    assert len(docs) == 1
    assert docs[0].forward_quality_verdict["analysis_mode"] == "deterministic"
    assert docs[0].forward_quality_verdict["compute_acceptance_grade"] == "measured_only"


@pytest.mark.asyncio
async def test_absent_or_measured_only_knowledge_never_earns_acceptance(
    db_session, _quiet_compute_io
):
    """Fail-closed acceptance: a compute SUCCESS with an ABSENT KnowledgeDocument,
    or a measured-only (deterministic) one, is NEVER a success task outcome — only
    an acceptance-grade llm document credits success."""
    from app.task_outcomes import evaluate_task_outcome

    # Case 1: SUCCESS compute_result but NO knowledge_document mirror at all.
    absent = await _make_compute_task(db_session)
    absent.plan_output = {
        "compute_result": {"outcome": "SUCCESS", "final_cost_usd": 0.2}
    }
    db_session.add(absent)
    await db_session.commit()
    await db_session.refresh(absent)
    assert "knowledge_document" not in (absent.plan_output or {})
    assert evaluate_task_outcome(absent).status == "neutral"

    # Case 2: SUCCESS with a measured-only deterministic knowledge mirror.
    measured = await _make_compute_task_with_identity(
        db_session, metric="accuracy", op=">=", threshold=0.9
    )
    job, _uri = await _setup_durable_job_with_artifact(db_session, measured)
    logs = _result_artifact(job_id=str(job.id), accuracy=0.93)
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
        await _handle_compute_experiment_completion(measured, logs, db_session)
    await db_session.refresh(measured)
    assert (
        measured.plan_output["knowledge_document"]["compute_acceptance_grade"]
        == "measured_only"
    )
    assert evaluate_task_outcome(measured).status == "neutral"


# ── Live-proof release identity binding acceptance gate ─────────────────────


@pytest.mark.asyncio
async def test_live_proof_acceptance_requires_echo_verified(
    db_session, mocker, _quiet_compute_io
):
    """A live-proof job without an echo degrades acceptance to measured_only."""
    mocker.patch(
        "app.storage.generate_download_signed_url",
        new=AsyncMock(side_effect=RuntimeError("no gcs in tests")),
    )
    task = await _make_compute_task_with_identity(
        db_session, metric="accuracy", op=">=", threshold=0.9
    )
    job, artifact_uri = await _setup_durable_job_with_artifact(db_session, task)
    release_evidence = {
        "promoted_sha": "liveproof001sha",
        "aggregate_serving_status": "serving_confirmed",
        "release_version_number": 9,
    }
    # Set live_release_evidence but NO release_echo_sha (echo absent).
    job.polling_metadata = {
        **(job.polling_metadata or {}),
        "live_release_evidence": release_evidence,
    }
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    metric_ref = f"{artifact_uri}#metric:accuracy=0.93"
    mock_cls, _ = _mock_anthropic_returning([artifact_uri, metric_ref])
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
    docs = await _knowledge_docs_for(db_session, task)
    assert len(docs) == 1
    verdict = docs[0].forward_quality_verdict
    assert verdict["release_evidence"] == release_evidence
    assert verdict["release_binding"]["status"] == "absent"
    assert verdict["compute_acceptance_grade"] == "measured_only"
    assert verdict["compute_acceptance_reason"] == "release_binding_absent"
    assert verdict["knowledge_acceptance_evidence"] is False


@pytest.mark.asyncio
async def test_live_proof_acceptance_with_verified_echo(
    db_session, mocker, _quiet_compute_io
):
    """A live-proof job with a matching echo earns acceptance grade."""
    mocker.patch(
        "app.storage.generate_download_signed_url",
        new=AsyncMock(side_effect=RuntimeError("no gcs in tests")),
    )
    task = await _make_compute_task_with_identity(
        db_session, metric="accuracy", op=">=", threshold=0.9
    )
    job, artifact_uri = await _setup_durable_job_with_artifact(db_session, task)
    promoted_sha = "verifiedechosha999"
    release_evidence = {
        "promoted_sha": promoted_sha,
        "aggregate_serving_status": "serving_confirmed",
        "release_version_number": 9,
    }
    # Both live_release_evidence and release_echo_sha set; echo matches.
    job.polling_metadata = {
        **(job.polling_metadata or {}),
        "live_release_evidence": release_evidence,
        "release_echo_sha": promoted_sha,
    }
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    metric_ref = f"{artifact_uri}#metric:accuracy=0.93"
    mock_cls, _ = _mock_anthropic_returning([artifact_uri, metric_ref])
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
    docs = await _knowledge_docs_for(db_session, task)
    assert len(docs) == 1
    verdict = docs[0].forward_quality_verdict
    assert verdict["release_evidence"] == release_evidence
    assert verdict["release_binding"]["status"] == "verified"
    assert verdict["release_binding"]["echo_sha"] == promoted_sha
    assert verdict["compute_acceptance_grade"] == "acceptance"
    assert verdict["compute_acceptance_reason"] == "llm_artifact_cited"
    assert verdict["knowledge_acceptance_evidence"] is True


@pytest.mark.asyncio
async def test_live_proof_mismatched_echo_degrades_to_measured_only(
    db_session, mocker, _quiet_compute_io
):
    """A live-proof job with a mismatched echo records mismatched reason."""
    mocker.patch(
        "app.storage.generate_download_signed_url",
        new=AsyncMock(side_effect=RuntimeError("no gcs in tests")),
    )
    task = await _make_compute_task_with_identity(
        db_session, metric="accuracy", op=">=", threshold=0.9
    )
    job, artifact_uri = await _setup_durable_job_with_artifact(db_session, task)
    release_evidence = {
        "promoted_sha": "expected-sha-111",
        "aggregate_serving_status": "serving_confirmed",
        "release_version_number": 9,
    }
    job.polling_metadata = {
        **(job.polling_metadata or {}),
        "live_release_evidence": release_evidence,
        "release_echo_sha": "different-sha-999",
    }
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    metric_ref = f"{artifact_uri}#metric:accuracy=0.93"
    mock_cls, _ = _mock_anthropic_returning([artifact_uri, metric_ref])
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
    docs = await _knowledge_docs_for(db_session, task)
    assert len(docs) == 1
    verdict = docs[0].forward_quality_verdict
    assert verdict["release_binding"]["status"] == "mismatched"
    assert verdict["release_binding"]["echo_sha"] == "different-sha-999"
    assert verdict["release_binding"]["expected_sha"] == "expected-sha-111"
    assert verdict["compute_acceptance_grade"] == "measured_only"
    assert verdict["compute_acceptance_reason"] == "release_binding_mismatched"


@pytest.mark.asyncio
async def test_non_live_proof_acceptance_unaffected_by_release_binding(
    db_session, mocker, _quiet_compute_io
):
    """Jobs without release_evidence are unaffected by the release binding check."""
    mocker.patch(
        "app.storage.generate_download_signed_url",
        new=AsyncMock(side_effect=RuntimeError("no gcs in tests")),
    )
    task = await _make_compute_task_with_identity(
        db_session, metric="accuracy", op=">=", threshold=0.9
    )
    job, artifact_uri = await _setup_durable_job_with_artifact(db_session, task)
    # No live_release_evidence on the job — ordinary (non-live-proof) experiment.

    metric_ref = f"{artifact_uri}#metric:accuracy=0.93"
    mock_cls, _ = _mock_anthropic_returning([artifact_uri, metric_ref])
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
    docs = await _knowledge_docs_for(db_session, task)
    assert len(docs) == 1
    verdict = docs[0].forward_quality_verdict
    # No release_evidence means no release_binding key and no binding check.
    assert verdict.get("release_evidence") is None
    assert "release_binding" not in verdict
    assert verdict["compute_acceptance_grade"] == "acceptance"
    assert verdict["knowledge_acceptance_evidence"] is True


@pytest.mark.asyncio
async def test_deterministic_doc_records_job_terminal_diagnostics(
    db_session, _quiet_compute_io
):
    """A provisioning-timeout ComputeJob's terminal diagnostics surface in the
    persisted knowledge document: summary contains the terminal_reason, body
    carries vm_phase/accelerator_type, and forward_quality_verdict has a
    terminal_diagnostics sub-dict.
    """
    from app.knowledge_completion import persist_compute_experiment_knowledge

    project = Project(name="DiagTest", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_compute_task_with_identity(
        db_session, metric="accuracy", op=">=", threshold=0.9
    )

    # Simulate a provisioning timeout: VM never started (started_at=None),
    # has an accelerator request, and carries the canonical timeout reason.
    job = ComputeJob(
        project_id=project.id,
        task_id=task.id,
        backend="local",
        status=ComputeJobStatus.FAILED,
        outcome=ComputeJobOutcome.TIMED_OUT,
        terminal_reason="compute job timed out while provisioning",
        started_at=None,  # VM never started → provisioning phase
        accelerator_type="T4",
        machine_type="n1-standard-4-t4",
        accrued_cost_usd=0.0,
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    logs_json = json.dumps({"job_id": str(job.id), "outcome": "TIMED_OUT"})

    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
        doc = await persist_compute_experiment_knowledge(
            task,
            outcome="TIMED_OUT",
            cost=0.0,
            criterion=None,
            result_reference=None,
            session=db_session,
            logs=logs_json,
        )

    assert doc is not None
    # Human-readable summary carries the cause.
    assert "compute job timed out while provisioning" in doc.summary
    assert "provisioning" in doc.summary
    # Body carries the structured diagnostics.
    assert "compute job timed out while provisioning" in doc.body
    assert "provisioning" in doc.body
    assert "T4" in doc.body
    # verdict_record carries the structured sub-dict.
    td = doc.forward_quality_verdict.get("terminal_diagnostics")
    assert td is not None
    assert td["terminal_reason"] == "compute job timed out while provisioning"
    assert td["vm_phase"] == "provisioning"
    assert td["accelerator_type"] == "T4"


# ── Integration: POST /api/agent/compute-analysis endpoint ───────────────────


@pytest.mark.asyncio
async def test_agent_compute_analysis_endpoint_upgrades_doc_to_llm(
    client, db_session, mocker, _quiet_compute_io
):
    """POSTing to /api/agent/compute-analysis with a valid agent JWT and a grounded
    VM-produced analysis (a) upgrades the KnowledgeDocument to analysis_mode='llm'
    with compute_acceptance_reason='llm_artifact_cited', (b) clears the one-shot
    request bundle from task.context afterward, and (c) rejects a foreign job_id
    with 404.

    Exercises the router function's full logic:
      1. Token 'sub' → task_id resolution and 403 paths.
      2. job-ownership scoping via get_compute_job_for_task (security boundary).
      3. outcome/cost/criterion resolution from task.plan_output / task.context.
      4. One-shot bundle cleanup.

    Uses run_compute_wake_post_commit with no backend API key to drive the task to
    the VM-re-queued state (status=QUEUED, vm_instance_id=None), which is the only
    state from which the endpoint can be called (verify_agent_token rejects tokens
    for terminal tasks on non-idempotent paths).
    """
    from app.auth import create_agent_token
    from app.models import TaskWakeRegistration, WakeAction, WakeDeliveryState
    from app.task_completion import (
        _COMPUTE_VM_ANALYSIS_REQUEST_CONTEXT_KEY,
        _COMPUTE_WAKE_POST_COMMIT_META_KEY,
        run_compute_wake_post_commit,
    )

    mocker.patch(
        "app.storage.generate_download_signed_url",
        new=AsyncMock(side_effect=RuntimeError("no gcs in tests")),
    )

    task = await _make_compute_task_with_identity(
        db_session, metric="accuracy", op=">=", threshold=0.9
    )
    job, artifact_uri = await _setup_durable_job_with_artifact(db_session, task)
    metric_ref = f"{artifact_uri}#metric:accuracy=0.93"

    # Add the compute success criterion to task context so the endpoint can
    # resolve it via the fallback path (task.context["compute_success_criterion"]).
    task.context = {
        **(task.context or {}),
        "compute_success_criterion": {
            "metric": "accuracy",
            "operator": ">=",
            "threshold": 0.9,
            "value": 0.93,
            "met": True,
            "measurable": True,
        },
    }
    db_session.add(task)
    await db_session.commit()

    # A DELIVERED compute-wake registration — the production autonomous path that
    # persists durable knowledge AFTER the finalize commit.
    reg = TaskWakeRegistration(
        task_id=task.id,
        project_id=task.project_id,
        wake_source_kind="compute_job_terminal",
        wake_source_ref=str(job.id),
        wake_action=WakeAction.FINALIZE,
        delivery_state=WakeDeliveryState.DELIVERED,
        delivered_at=datetime.utcnow(),
        source_metadata={_COMPUTE_WAKE_POST_COMMIT_META_KEY: {"attempts": 0}},
    )
    db_session.add(reg)
    await db_session.commit()
    await db_session.refresh(reg)

    # With no backend key, run_compute_wake_post_commit persists the deterministic
    # doc and routes the analysis onto the VM substrate (task → QUEUED, vm_instance_id=None).
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "", "DISABLE_LLM_COMPUTE_ANALYSIS": ""}):
        ok = await run_compute_wake_post_commit(db_session, reg)
    assert ok is True

    await db_session.refresh(task)
    # Task is QUEUED for VM analysis with vm_instance_id cleared for re-dispatch.
    assert task.status == TaskStatus.QUEUED
    assert task.vm_instance_id is None
    assert _COMPUTE_VM_ANALYSIS_REQUEST_CONTEXT_KEY in (task.context or {})

    # Deterministic base record persisted.
    docs_before = await _knowledge_docs_for(db_session, task)
    assert len(docs_before) == 1
    assert docs_before[0].forward_quality_verdict["analysis_mode"] == "deterministic"

    # Token: since task.vm_instance_id is None, any vm_instance_id in the token
    # passes the vm_instance_mismatch guard in verify_agent_token.
    token = create_agent_token(str(task.id), "agent-vm-analysis-test")
    headers = {"x-agent-token": token}

    # ── (c) Foreign job_id → 404 ──────────────────────────────────────────────
    bad_resp = await client.post(
        "/api/agent/compute-analysis",
        headers=headers,
        json={
            "job_id": str(uuid.uuid4()),  # not this task's job
            "analysis": {
                "summary": "grounded summary",
                "body": "grounded body",
                "source_citations": [artifact_uri],
            },
        },
    )
    assert bad_resp.status_code == 404, (
        f"foreign job_id must be rejected with 404 (got {bad_resp.status_code}): {bad_resp.text}"
    )

    # ── (a) Grounded submission → upgrades doc to analysis_mode='llm' ─────────
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "", "DISABLE_LLM_COMPUTE_ANALYSIS": ""}):
        resp = await client.post(
            "/api/agent/compute-analysis",
            headers=headers,
            json={
                "job_id": str(job.id),
                "analysis": {
                    "title": "VM analysis: durable artifacts cleared the bar",
                    "summary": "Grounded analysis citing the durable artifact bundle + metric.",
                    "body": "# Analysis\n\nThe run cleared the bar; see the cited evidence.",
                    "source_citations": [artifact_uri, metric_ref],
                },
            },
        )
    assert resp.status_code == 200, f"expected 200 OK, got {resp.status_code}: {resp.text}"
    data = resp.json()
    assert data["status"] == "success"
    assert data["data"]["status"] == "upgraded"
    assert data["data"]["analysis_mode"] == "llm"

    # (a) Verify the KnowledgeDocument was upgraded in the DB. The endpoint's
    # session commits on the same StaticPool connection; use refresh() on the
    # known object (loaded in docs_before) to bypass the identity-map cache and
    # force a fresh DB read — the same pattern test_agent.py uses for task state.
    det_doc = docs_before[0]
    await db_session.refresh(det_doc)
    upgraded_verdict = det_doc.forward_quality_verdict
    assert upgraded_verdict["analysis_mode"] == "llm", (
        f"expected analysis_mode=llm after upgrade, got: {upgraded_verdict['analysis_mode']}"
    )
    assert upgraded_verdict["compute_acceptance_grade"] == "acceptance"
    assert upgraded_verdict["compute_acceptance_reason"] == "llm_artifact_cited"
    assert upgraded_verdict["knowledge_acceptance_evidence"] is True
    assert artifact_uri in det_doc.source_citations
    assert metric_ref in det_doc.source_citations

    # ── (b) One-shot bundle is cleared from task.context ─────────────────────
    await db_session.refresh(task)
    assert _COMPUTE_VM_ANALYSIS_REQUEST_CONTEXT_KEY not in (task.context or {}), (
        "the one-shot request bundle must be cleared from task.context after submission"
    )


@pytest.mark.asyncio
async def test_agent_compute_analysis_endpoint_rejects_bad_jwt(
    client, db_session, _quiet_compute_io
):
    """An invalid agent JWT is rejected (401) — the security boundary holds.

    Note: a missing x-agent-token header yields 422 (FastAPI required-header
    validation) rather than 401; the security boundary is enforced by token
    verification, not by the absence of the field.
    """
    resp_bad_token = await client.post(
        "/api/agent/compute-analysis",
        headers={"x-agent-token": "not-a-valid-jwt"},
        json={
            "job_id": str(uuid.uuid4()),
            "analysis": {
                "summary": "grounded summary",
                "body": "grounded body",
            },
        },
    )
    assert resp_bad_token.status_code in (401, 403)


# ── Post-terminate salvage poll ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deadline_kill_salvages_late_echo_sha(db_session, monkeypatch):
    """Deadline termination does a best-effort poll after terminate; if the
    post-terminate poll surfaces an echoed_release_sha that was absent in the
    pre-terminate snapshot, the SHA is merged into the finalized job's
    polling_metadata."""
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
    job.external_compute_id = "fake-deadline-salvage"
    job.started_at = datetime.utcnow() - timedelta(seconds=120)
    job.wall_clock_deadline = datetime.utcnow() - timedelta(seconds=1)
    db_session.add(job)
    await db_session.commit()

    poll_call_count = 0

    class FakeBackend:
        async def launch(self, spec):
            raise AssertionError("launch should not be called")

        async def poll_cost(self, persisted_job, *, as_of):
            return ComputeCostResult(total_cost_usd=0.01, measured_at=as_of)

        async def poll_status(self, persisted_job):
            nonlocal poll_call_count
            poll_call_count += 1
            if poll_call_count == 1:
                # Pre-terminate poll: no echo SHA yet
                return ComputeStatusResult(status=ComputeJobStatus.RUNNING)
            # Post-terminate salvage poll: SHA now available
            return ComputeStatusResult(
                status=ComputeJobStatus.RUNNING,
                echoed_release_sha="late-sha-abc",
            )

        async def terminate(self, persisted_job, *, reason):
            return True

    await _drive_job_to_terminal(db_session, monkeypatch, job, FakeBackend())

    assert job.outcome == ComputeJobOutcome.TIMED_OUT
    assert job.polling_metadata.get("release_echo_sha") == "late-sha-abc"


@pytest.mark.asyncio
async def test_spend_cap_kill_salvages_late_metrics(db_session, monkeypatch):
    """Spend-cap termination does a best-effort poll after terminate; if the
    post-terminate poll surfaces result_metrics absent in the pre-terminate
    snapshot, they are merged into the finalized job's polling_metadata."""
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
    job.external_compute_id = "fake-budget-salvage"
    job.started_at = datetime.utcnow() - timedelta(seconds=60)
    db_session.add(job)
    await db_session.commit()

    poll_call_count = 0

    class FakeBackend:
        async def launch(self, spec):
            raise AssertionError("launch should not be called")

        async def poll_cost(self, persisted_job, *, as_of):
            return ComputeCostResult(total_cost_usd=0.05, measured_at=as_of)

        async def poll_status(self, persisted_job):
            nonlocal poll_call_count
            poll_call_count += 1
            if poll_call_count == 1:
                # Pre-terminate poll: no metrics yet
                return ComputeStatusResult(status=ComputeJobStatus.RUNNING)
            # Post-terminate salvage poll: metrics now available
            return ComputeStatusResult(
                status=ComputeJobStatus.RUNNING,
                metrics={"accuracy": 0.91},
            )

        async def terminate(self, persisted_job, *, reason):
            return True

    await _drive_job_to_terminal(db_session, monkeypatch, job, FakeBackend())

    assert job.outcome == ComputeJobOutcome.SPEND_CAP_EXCEEDED
    assert job.polling_metadata.get("result_metrics") == {"accuracy": 0.91}


@pytest.mark.asyncio
async def test_salvage_poll_failure_does_not_block_termination(db_session, monkeypatch):
    """If the post-terminate salvage poll raises an exception, the terminal
    outcome (TIMED_OUT) is still applied correctly — the exception is swallowed."""
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
    job.external_compute_id = "fake-salvage-fail"
    job.started_at = datetime.utcnow() - timedelta(seconds=120)
    job.wall_clock_deadline = datetime.utcnow() - timedelta(seconds=1)
    db_session.add(job)
    await db_session.commit()

    poll_call_count = 0

    class FakeBackend:
        async def launch(self, spec):
            raise AssertionError("launch should not be called")

        async def poll_cost(self, persisted_job, *, as_of):
            return ComputeCostResult(total_cost_usd=0.01, measured_at=as_of)

        async def poll_status(self, persisted_job):
            nonlocal poll_call_count
            poll_call_count += 1
            if poll_call_count == 1:
                return ComputeStatusResult(status=ComputeJobStatus.RUNNING)
            # Post-terminate salvage poll raises
            raise RuntimeError("backend unreachable after terminate")

        async def terminate(self, persisted_job, *, reason):
            return True

    await _drive_job_to_terminal(db_session, monkeypatch, job, FakeBackend())

    # Terminal outcome must still be applied despite the salvage poll failure
    assert job.status == ComputeJobStatus.TIMED_OUT
    assert job.outcome == ComputeJobOutcome.TIMED_OUT
    assert "runtime deadline" in (job.terminal_reason or "")
