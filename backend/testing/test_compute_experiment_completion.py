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

import asyncio
import json
import os
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import compute
from app import compute_jobs
from app.time_utils import _utcnow
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
    LOCAL_BACKEND_NAME,
    ComputeCostResult,
    ComputeLaunchResult,
    ComputeStatusResult,
    LocalSubprocessComputeBackend,
    TerminalArtifactReference,
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
from app.knowledge_completion import (
    KNOWLEDGE_PREREQUISITES_KEY,
    _build_compute_experiment_document,
    _extract_job_terminal_diagnostics,
)
from app.knowledge_query import build_compute_sweep_groups, build_sweep_knowledge_index
from app.task_completion import _handle_compute_experiment_completion


@pytest.fixture
def _quiet_compute_io(mocker):
    mocker.patch(
        "app.task_completion.broadcast_to_project", new=AsyncMock(return_value=None)
    )
    # Hold the always-dispose follow-on spawn constant so these tests stay focused
    # on the completion GATE + faithful capture + knowledge persistence contract.
    # Under always-dispose every terminal experiment now routes through
    # _spawn_act_on_verdict_followon (success → reason-and-ship → parent EVALUATING;
    # failure → diagnose-and-fix; intentional kills → skipped). That orthogonal
    # disposition behaviour — child shape, prompt framing, EVALUATING transition,
    # kill exclusion, concurrency cap — is owned and asserted by
    # testing/test_compute_experiment_spawn.py. Mocking it here (return None) keeps
    # the success path COMPLETED and the failure paths FAILED so each test below
    # asserts exactly the gate/capture/knowledge behaviour it was written for.
    mocker.patch(
        "app.task_completion._spawn_act_on_verdict_followon",
        new=AsyncMock(return_value=None),
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
    job.completed_at = _utcnow()
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
    job.completed_at = _utcnow()
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
    job.completed_at = _utcnow()
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
    job.completed_at = _utcnow()
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
    with (
        patch("app.artifact_prerequisites._DEPENDENT_SCAN_CAP", 1),
        patch.object(
            db_session,
            "execute",
            new=AsyncMock(side_effect=[first_page, RuntimeError("scan boom")]),
        ),
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
    job.completed_at = _utcnow()
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
async def test_compute_knowledge_stamps_sweep_identity_from_payload(
    db_session, _quiet_compute_io
):
    """A completed compute_experiment doc carries sweep_id + arm_label.

    sweep_id mirrors task.first_payload['sweep_id']; arm_label prefers the
    explicit experiment name over the hypothesis fallback.
    """
    from app.knowledge_completion import persist_compute_experiment_knowledge

    project = Project(name="SweepIdentityStamp", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="compute_experiment: arm A",
        status=TaskStatus.COMPLETED,
        payloads=[
            {
                "type": "compute_experiment",
                "base_branch": "main",
                "sweep_id": "sw-deadbeef",
                "experiment": "arm-A-baseline",
                "hypothesis": "baseline beats treatment",
            }
        ],
        context={"finding_hash": "compute-sweep-identity"},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

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
    assert doc.sweep_id == "sw-deadbeef"
    # Explicit experiment name wins over the hypothesis fallback.
    assert doc.arm_label == "arm-A-baseline"


@pytest.mark.asyncio
async def test_compute_knowledge_arm_label_is_none_when_no_experiment_or_arm_label(
    db_session, _quiet_compute_io
):
    """arm_label is derived from payload 'experiment' or 'arm_label' only.
    When neither is present (e.g. only 'hypothesis' is set), arm_label is None."""
    from app.knowledge_completion import persist_compute_experiment_knowledge

    project = Project(name="SweepArmFallback", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    long_hyp = "h" * 200
    task = Task(
        project_id=project.id,
        title="compute_experiment: arm B",
        status=TaskStatus.COMPLETED,
        payloads=[
            {
                "type": "compute_experiment",
                "base_branch": "main",
                "sweep_id": "sw-cafebabe",
                "hypothesis": long_hyp,
            }
        ],
        context={"finding_hash": "compute-sweep-fallback"},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

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
    assert doc.sweep_id == "sw-cafebabe"
    # No 'experiment' or 'arm_label' in payload → arm_label_val is None.
    assert doc.arm_label is None


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
    job.completed_at = _utcnow()
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
    assert (
        prereqs[0]["storage_uri"]
        == "gs://blueprint-agent-artifacts/compute-jobs/result.json"
    )


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
    dependent_project = Project(
        name="BackfillCrossProjectDependent", user_id="test-user"
    )
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
    await _handle_compute_experiment_completion(success, _result_artifact(), db_session)
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
async def test_compute_completion_unparseable_fails_gate(db_session, _quiet_compute_io):
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
    task = await _make_compute_task_with_criterion(db_session, "accuracy", ">=", 0.9)

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
    task = await _make_compute_task_with_criterion(db_session, "accuracy", ">=", 0.9)

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
async def test_unmeasured_metric_criterion_fails_loud(db_session, _quiet_compute_io):
    """LOUD: a declared kind=metric criterion the command produced NO numeric value
    for must terminate as a measurable FAILURE with the distinct
    ``compute_unmeasured_criterion`` failure_category — it must NOT complete clean
    as the misleading 'outcome=SUCCESS / goal missed (value=null)'. 'The job ran'
    can never masquerade as 'the hypothesis was measured'.

    The criterion evidence (value=None, measurable=False) is still recorded
    durably on plan_output and task.context for the operator/strategist."""
    task = await _make_compute_task_with_criterion(db_session, "accuracy", ">=", 0.9)

    # Real terminal artifact shape: outcome=SUCCESS but no 'accuracy' key reported.
    await _handle_compute_experiment_completion(task, _result_artifact(), db_session)

    await db_session.refresh(task)
    # Loud, terminal FAILURE — never a clean COMPLETED.
    assert task.status == TaskStatus.FAILED
    assert task.context["failure_category"] == "compute_unmeasured_criterion"
    assert task.context["evaluation_gate"]["passed"] is False
    assert task.context["evaluation_gate"]["strategy"] == "compute-unmeasured-criterion"
    # Criterion evidence is still recorded durably (faithful negative record).
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


@pytest.mark.asyncio
async def test_unmeasured_criterion_classifies_as_code_error(
    db_session, _quiet_compute_io
):
    """The distinct ``compute_unmeasured_criterion`` literal routes to CODE_ERROR
    (a genuine work defect: the command must emit the declared metric) so it
    burns the retry budget / drives diagnosis rather than being budget-exempt."""
    from app.failure_classification import CODE_ERROR, classify_failure

    task = await _make_compute_task_with_criterion(db_session, "accuracy", ">=", 0.9)
    await _handle_compute_experiment_completion(task, _result_artifact(), db_session)

    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED
    assert classify_failure(task) == CODE_ERROR


@pytest.mark.asyncio
async def test_unmeasured_criterion_stdout_fallback_measures_and_passes(
    db_session, _quiet_compute_io
):
    """End-to-end MEASURABLE path: a command that reported its metric only via the
    captured stdout JSON (no result file) is measured, so the criterion is met and
    the task COMPLETES — it does NOT trip the unmeasured-criterion failure.

    Mirrors what ``build_compute_result_logs`` produces: the stdout JSON object's
    numeric keys are flattened to top-level keys on the terminal result."""
    task = await _make_compute_task_with_criterion(db_session, "accuracy", ">=", 0.9)

    # The terminal result the stdout fallback would yield: accuracy flattened.
    await _handle_compute_experiment_completion(
        task, _result_artifact(accuracy=0.95), db_session
    )

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    assert "evaluation_gate" not in task.context
    assert task.context["compute_success_criterion"]["met"] is True
    assert task.context["compute_success_criterion"]["measurable"] is True


@pytest.mark.asyncio
async def test_unmeasured_criterion_spend_cap_kill_stays_clean(
    db_session, _quiet_compute_io
):
    """An intentional budget kill with an unmeasured declared metric stays exactly
    as today — a clean terminal COMPLETED with the kill outcome recorded. The
    experiment never got to finish, so the unmeasured metric is expected, NOT the
    loud unmeasured-criterion failure."""
    task = await _make_compute_task_with_criterion(db_session, "accuracy", ">=", 0.9)

    await _handle_compute_experiment_completion(
        task,
        _result_artifact(outcome="SPEND_CAP_EXCEEDED", status="FAILED"),
        db_session,
    )

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    assert task.context.get("failure_category") != "compute_unmeasured_criterion"
    assert task.plan_output["compute_result"]["outcome"] == "SPEND_CAP_EXCEEDED"


@pytest.mark.asyncio
async def test_unmeasured_criterion_deadline_kill_stays_clean(
    db_session, _quiet_compute_io
):
    """An intentional deadline (TIMED_OUT) kill with an unmeasured declared metric
    stays a clean terminal COMPLETED — unchanged by the loud failure."""
    task = await _make_compute_task_with_criterion(db_session, "accuracy", ">=", 0.9)

    await _handle_compute_experiment_completion(
        task,
        _result_artifact(outcome="TIMED_OUT", status="TIMED_OUT"),
        db_session,
    )

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    assert task.context.get("failure_category") != "compute_unmeasured_criterion"
    assert task.plan_output["compute_result"]["outcome"] == "TIMED_OUT"


@pytest.mark.asyncio
async def test_multiarm_measured_criterion_is_measurable_and_not_failed_loud(
    db_session, _quiet_compute_io
):
    """A multi-arm experiment whose arms all report the declared metric must be
    evaluated as measurable=True using best-arm semantics — it must NOT trip the
    loud ``compute_unmeasured_criterion`` failure just because no flat metric key
    is present at the top level.

    Drives the production code path: build_compute_result_logs flattens numeric
    result_metrics AND preserves the 'arms' block so evaluate_compute_success_criterion
    can apply best-arm semantics (>= → max across arms)."""
    task = await _make_compute_task_with_criterion(db_session, "accuracy", ">=", 0.9)
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
    job.status = ComputeJobStatus.SUCCEEDED
    job.outcome = ComputeJobOutcome.SUCCESS
    job.accrued_cost_usd = 0.50
    job.final_cost_usd = 0.50
    job.terminal_reason = "compute job finished"
    job.completed_at = _utcnow()
    # Multi-arm result: arms each report accuracy; no top-level 'accuracy' key.
    # build_compute_result_logs must surface the arms block so the evaluator can
    # use best-arm semantics.
    job.polling_metadata = {
        **(job.polling_metadata or {}),
        "result_metrics": {
            "arms": {
                "arm_a": {"accuracy": 0.93, "exit_code": 0},
                "arm_b": {"accuracy": 0.85, "exit_code": 0},
            },
        },
    }
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    # Production path: build_compute_result_logs → _handle_compute_experiment_completion
    logs = compute_jobs.build_compute_result_logs(job)
    task.status = TaskStatus.COMPLETED
    db_session.add(task)
    await db_session.commit()

    await _handle_compute_experiment_completion(task, logs, db_session)

    await db_session.refresh(task)
    # Must NOT fail loud — arms measured the criterion.
    assert task.status == TaskStatus.COMPLETED
    assert task.context.get("failure_category") != "compute_unmeasured_criterion"
    # Criterion is measurable (best-arm value = max across arms for >=).
    ctx_criterion = task.context["compute_success_criterion"]
    assert ctx_criterion["measurable"] is True
    assert ctx_criterion["met"] is True
    assert ctx_criterion["value"] == pytest.approx(0.93)
    # plan_output also records the criterion durably.
    criterion = task.plan_output["compute_result"]["criterion"]
    assert criterion["measurable"] is True
    assert criterion["met"] is True
    assert criterion["value"] == pytest.approx(0.93)
    # per_arm breakdown available for consumers.
    assert criterion["per_arm"]["arm_a"] == pytest.approx(0.93)
    assert criterion["per_arm"]["arm_b"] == pytest.approx(0.85)


@pytest.mark.asyncio
async def test_multiarm_criterion_min_operator(
    db_session, _quiet_compute_io
):
    """Multi-arm with a <= criterion uses min-arm semantics (lowest value wins)."""
    task = await _make_compute_task_with_criterion(db_session, "latency_ms", "<=", 200)
    task.status = TaskStatus.IN_PROGRESS
    db_session.add(task)
    await db_session.commit()

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="python bench.py",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )
    job.status = ComputeJobStatus.SUCCEEDED
    job.outcome = ComputeJobOutcome.SUCCESS
    job.accrued_cost_usd = 0.20
    job.final_cost_usd = 0.20
    job.terminal_reason = "compute job finished"
    job.completed_at = _utcnow()
    job.polling_metadata = {
        **(job.polling_metadata or {}),
        "result_metrics": {
            "arms": {
                "fast": {"latency_ms": 150},
                "slow": {"latency_ms": 300},
            },
        },
    }
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    logs = compute_jobs.build_compute_result_logs(job)
    task.status = TaskStatus.COMPLETED
    db_session.add(task)
    await db_session.commit()

    await _handle_compute_experiment_completion(task, logs, db_session)

    await db_session.refresh(task)
    criterion = task.plan_output["compute_result"]["criterion"]
    # min across arms: 150 <= 200 → met
    assert criterion["measurable"] is True
    assert criterion["value"] == pytest.approx(150)
    assert criterion["met"] is True
    assert criterion["per_arm"]["fast"] == pytest.approx(150)
    assert criterion["per_arm"]["slow"] == pytest.approx(300)


@pytest.mark.asyncio
async def test_multiarm_criterion_equality_operator(
    db_session, _quiet_compute_io
):
    """Multi-arm with an == criterion prefers the first arm that satisfies it."""
    task = await _make_compute_task_with_criterion(db_session, "status_code", "==", 0)
    task.status = TaskStatus.IN_PROGRESS
    db_session.add(task)
    await db_session.commit()

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="python run.py",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )
    job.status = ComputeJobStatus.SUCCEEDED
    job.outcome = ComputeJobOutcome.SUCCESS
    job.accrued_cost_usd = 0.10
    job.final_cost_usd = 0.10
    job.terminal_reason = "compute job finished"
    job.completed_at = _utcnow()
    job.polling_metadata = {
        **(job.polling_metadata or {}),
        "result_metrics": {
            "arms": {
                "failing_arm": {"status_code": 1},
                "passing_arm": {"status_code": 0},
            },
        },
    }
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    logs = compute_jobs.build_compute_result_logs(job)
    task.status = TaskStatus.COMPLETED
    db_session.add(task)
    await db_session.commit()

    await _handle_compute_experiment_completion(task, logs, db_session)

    await db_session.refresh(task)
    criterion = task.plan_output["compute_result"]["criterion"]
    # equality fallthrough: prefers the arm that satisfies == 0
    assert criterion["measurable"] is True
    assert criterion["value"] == pytest.approx(0)
    assert criterion["met"] is True


@pytest.mark.asyncio
async def test_multiarm_criterion_partial_measurability(
    db_session, _quiet_compute_io
):
    """One arm reports the metric; the other does not. The measurable arm's value
    is used for best-arm evaluation; per_arm carries None for the missing arm."""
    task = await _make_compute_task_with_criterion(db_session, "accuracy", ">=", 0.9)
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
    job.status = ComputeJobStatus.SUCCEEDED
    job.outcome = ComputeJobOutcome.SUCCESS
    job.accrued_cost_usd = 0.30
    job.final_cost_usd = 0.30
    job.terminal_reason = "compute job finished"
    job.completed_at = _utcnow()
    job.polling_metadata = {
        **(job.polling_metadata or {}),
        "result_metrics": {
            "arms": {
                "arm_with_metric": {"accuracy": 0.95},
                "arm_without_metric": {"loss": 0.05},  # no 'accuracy' key
            },
        },
    }
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    logs = compute_jobs.build_compute_result_logs(job)
    task.status = TaskStatus.COMPLETED
    db_session.add(task)
    await db_session.commit()

    await _handle_compute_experiment_completion(task, logs, db_session)

    await db_session.refresh(task)
    criterion = task.plan_output["compute_result"]["criterion"]
    # Partially measurable: best-arm value from the arm that reported it
    assert criterion["measurable"] is True
    assert criterion["value"] == pytest.approx(0.95)
    assert criterion["met"] is True
    assert criterion["per_arm"]["arm_with_metric"] == pytest.approx(0.95)
    assert criterion["per_arm"]["arm_without_metric"] is None


@pytest.mark.asyncio
async def test_multiarm_criterion_all_arms_missing_metric(
    db_session, _quiet_compute_io
):
    """When no arm reports the declared metric the criterion is unmeasurable
    (fail-closed) and must NOT trip the loud compute_unmeasured_criterion failure."""
    task = await _make_compute_task_with_criterion(db_session, "accuracy", ">=", 0.9)
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
    job.status = ComputeJobStatus.SUCCEEDED
    job.outcome = ComputeJobOutcome.SUCCESS
    job.accrued_cost_usd = 0.30
    job.final_cost_usd = 0.30
    job.terminal_reason = "compute job finished"
    job.completed_at = _utcnow()
    job.polling_metadata = {
        **(job.polling_metadata or {}),
        "result_metrics": {
            "arms": {
                "arm_a": {"loss": 0.1},
                "arm_b": {"loss": 0.2},
            },
        },
    }
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    logs = compute_jobs.build_compute_result_logs(job)
    task.status = TaskStatus.COMPLETED
    db_session.add(task)
    await db_session.commit()

    await _handle_compute_experiment_completion(task, logs, db_session)

    await db_session.refresh(task)
    criterion = task.plan_output["compute_result"]["criterion"]
    # All arms missing the metric → fail-closed
    assert criterion["measurable"] is False
    assert criterion["met"] is False
    assert criterion["value"] is None


@pytest.mark.asyncio
async def test_artifact_produced_criterion_unaffected_by_unmeasured_failure(
    db_session, _quiet_compute_io
):
    """A kind=artifact_produced criterion gates on durable_file_count, NOT a
    reported numeric metric, so it must NEVER trip the unmeasured-criterion
    failure even when no durable artifact file was produced."""
    task = await _make_compute_task(db_session)
    task.payloads = [
        {
            "type": "compute_experiment",
            "base_branch": "main",
            "success_criterion": {"kind": "artifact_produced", "min_file_count": 1},
        }
    ]
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # No durable_file_count reported (no artifact) — durable_file_count absent.
    await _handle_compute_experiment_completion(task, _result_artifact(), db_session)

    await db_session.refresh(task)
    # The artifact_produced criterion is unmeasured, but it must NOT route to the
    # metric-only loud failure category.
    assert task.context.get("failure_category") != "compute_unmeasured_criterion"


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
            await session.execute(
                text("SELECT 1 FROM table_that_must_not_exist_xyz_abc")
            )
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
    neg_artifact_uri = (
        "gs://blueprint-agent-artifacts/compute-jobs/negative-result.json"
    )
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
    final_cost = (
        job.final_cost_usd if job.final_cost_usd is not None else job.accrued_cost_usd
    )
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
    job.started_at = _utcnow() - timedelta(seconds=60)
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
    job.started_at = _utcnow() - timedelta(seconds=120)
    job.wall_clock_deadline = _utcnow() - timedelta(seconds=1)
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
    """End-to-end happy path: a job with a declared criterion runs to SUCCESS."""
    # Use a task with a success_criterion — criterion-free experiments must not
    # record clean SUCCESS (they are reclassified as FAILURE by the no-data guard).
    # declared_metric is NOT forwarded to create_compute_job to avoid triggering the
    # preflight smoke step (the fake backend has no smoke support); the success_criterion
    # in the task payload is enough to satisfy _is_criterion_free_experiment.
    task = await _make_compute_task_with_criterion(db_session, "accuracy", ">=", 0.8)
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
    # Bypass smoke: this test focuses on the job-monitor launch/poll/terminal path.
    # Dedicated smoke tests cover the preflight gate separately.
    meta = dict(job.polling_metadata or {})
    meta["preflight_smoke_done"] = True
    job.polling_metadata = meta
    await db_session.commit()
    started_at = _utcnow() - timedelta(seconds=30)

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
                finished_at=_utcnow(),
                message="completed",
                metrics={"accuracy": 0.92},
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
    # Two passes (smoke bypassed): STARTING→launch, RUNNING→poll-to-terminal.
    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)
    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)
    await db_session.refresh(job)
    assert job.outcome == ComputeJobOutcome.SUCCESS

    # Use build_compute_result_logs (not _result_from_terminal_job) so that the
    # accuracy metric stored in polling_metadata["result_metrics"] is flattened
    # into the result artifact and the criterion evaluator can assess it.
    await _handle_compute_experiment_completion(
        task, compute_jobs.build_compute_result_logs(job), db_session
    )

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    assert task.plan_output["compute_result"]["outcome"] == "SUCCESS"
    assert task.plan_output["compute_result"]["final_cost_usd"] == pytest.approx(0.03)


# ── Terminal-reason threading: failure_category classification ───────────────


@pytest.mark.asyncio
async def test_compute_completion_provisioning_failure_routes_to_provision_failure(
    db_session, monkeypatch, _quiet_compute_io
):
    """A compute job that died in the provisioning phase (VM never started)
    must classify the owning task as PROVISION_FAILURE — an INFRA class that
    does not burn the retry budget or penalise direction health.

    Acceptance gate: test_compute_completion_provisioning_failure_routes_to_provision_failure
    """
    from app.failure_classification import PROVISION_FAILURE

    task = await _make_compute_task(db_session)
    task.status = TaskStatus.IN_PROGRESS
    db_session.add(task)
    await db_session.commit()

    # Create a job that never got to STARTING (started_at=None → vm_phase="provisioning")
    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="echo hi",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )
    # Manually terminal-ise the job row as a provisioning failure.
    job.status = ComputeJobStatus.FAILED
    job.terminal_reason = "compute job timed out while provisioning"
    # started_at stays None → _extract_job_terminal_diagnostics → vm_phase="provisioning"
    db_session.add(job)
    await db_session.commit()

    # Emit a COMPUTE_RESULT that carries no outcome/cost (pre-metric failure path).
    logs = json.dumps({"job_id": str(job.id), "status": "FAILED"})
    await _handle_compute_experiment_completion(task, logs, db_session)

    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED
    ctx = task.context or {}
    assert ctx.get("failure_category") == PROVISION_FAILURE, (
        f"Expected PROVISION_FAILURE but got {ctx.get('failure_category')!r}; "
        f"context: {ctx}"
    )
    diag = ctx.get("compute_terminal_diagnostics")
    assert diag is not None, "compute_terminal_diagnostics must be stamped on context"
    assert diag["vm_phase"] == "provisioning"
    assert "provisioning" in (diag.get("terminal_reason") or "").lower()
    # The status message should surface the reason to the operator.
    assert task.latest_status_message is not None
    assert "provisioning" in (task.latest_status_message or "").lower()


@pytest.mark.asyncio
async def test_compute_completion_runtime_deadline_routes_to_agent_timeout(
    db_session, monkeypatch, _quiet_compute_io
):
    """A compute job killed for exceeding the runtime deadline — which ran long
    enough to start (started_at set) but had no artifact — classifies as
    AGENT_TIMEOUT on the owning task."""
    from app.failure_classification import AGENT_TIMEOUT

    task = await _make_compute_task(db_session)
    task.status = TaskStatus.IN_PROGRESS
    db_session.add(task)
    await db_session.commit()

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="sleep 999",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )
    job.status = ComputeJobStatus.FAILED
    job.started_at = _utcnow() - timedelta(seconds=300)
    job.terminal_reason = "compute job exceeded runtime deadline"
    db_session.add(job)
    await db_session.commit()

    logs = json.dumps({"job_id": str(job.id), "status": "FAILED"})
    await _handle_compute_experiment_completion(task, logs, db_session)

    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED
    ctx = task.context or {}
    assert ctx.get("failure_category") == AGENT_TIMEOUT, (
        f"Expected AGENT_TIMEOUT but got {ctx.get('failure_category')!r}"
    )
    diag = ctx.get("compute_terminal_diagnostics")
    assert diag is not None
    assert diag["vm_phase"] == "runtime"


@pytest.mark.asyncio
async def test_compute_completion_intentional_kills_still_complete(
    db_session, monkeypatch, _quiet_compute_io
):
    """Intentional spend-cap and deadline kills carry an outcome + cost and pass
    the gate — they must still surface as COMPLETED, not FAILED."""
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
    job.status = ComputeJobStatus.SUCCEEDED
    job.outcome = ComputeJobOutcome.SPEND_CAP_EXCEEDED
    job.started_at = _utcnow() - timedelta(seconds=60)
    job.final_cost_usd = 0.05
    job.terminal_reason = "spend cap exceeded"
    db_session.add(job)
    await db_session.commit()

    logs = _result_from_terminal_job(job)
    await _handle_compute_experiment_completion(task, logs, db_session)

    await db_session.refresh(task)
    # Intentional kills COMPLETE — never FAILED.
    assert task.status == TaskStatus.COMPLETED
    assert task.plan_output["compute_result"]["outcome"] == "SPEND_CAP_EXCEEDED"


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
    job.completed_at = _utcnow()
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


@pytest.mark.asyncio
async def test_faithful_capture_persists_arms_and_verdict(
    db_session, _quiet_compute_io
):
    """Faithful capture (axis A, always-on): completing a compute_experiment
    persists exactly ONE deterministic KnowledgeDocument that records the REAL
    reported metrics, BOTH A/B arms, and the measured success-criterion verdict —
    deterministically, with NO interpretation and NO VM-analysis re-queue.

    This is the replacement for the deleted bespoke VM-analysis leg: a result is
    never dropped or left a vacuous SUCCESS — it is captured faithfully into the
    existing knowledge substrate so the strategist and the later opt-in judgment
    agent can query and act on it.
    """
    task = await _make_compute_task_with_identity(
        db_session, metric="accuracy", op=">=", threshold=0.9
    )
    job, _artifact_uri = await _setup_durable_job_with_artifact(db_session, task)
    # The experiment reported a multi-arm A/B comparison alongside scalar metrics;
    # the COMPUTE_RESULT logs only carry the flattened scalars, so the arms must be
    # captured from the durable job row's preserved result object.
    job.polling_metadata = {
        **(job.polling_metadata or {}),
        "result_metrics": {
            "accuracy": 0.93,
            "baseline_accuracy": 0.88,
            "arms": {
                "A": {"accuracy": 0.93, "latency_ms": 120},
                "B": {"accuracy": 0.88, "latency_ms": 95},
            },
        },
    }
    db_session.add(job)
    await db_session.commit()

    logs = _result_artifact(job_id=str(job.id), accuracy=0.93)
    await _handle_compute_experiment_completion(task, logs, db_session)

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED

    docs = await _knowledge_docs_for(db_session, task)
    assert len(docs) == 1
    verdict = docs[0].forward_quality_verdict

    # (1) The measured success-criterion VERDICT: measurable / met / value.
    crit = verdict["criterion"]
    assert crit["metric"] == "accuracy"
    assert crit["measurable"] is True
    assert crit["met"] is True
    assert crit["value"] == pytest.approx(0.93)
    assert verdict["criterion_met"] is True

    # (2) The REAL reported numbers captured verbatim (not a truncated narrative).
    assert verdict["result_metrics"]["accuracy"] == pytest.approx(0.93)
    assert verdict["result_metrics"]["baseline_accuracy"] == pytest.approx(0.88)

    # (3) BOTH A/B arms preserved intact for later comparison.
    arms = verdict["arms"]
    assert set(arms) == {"A", "B"}
    assert arms["A"]["accuracy"] == pytest.approx(0.93)
    assert arms["B"]["latency_ms"] == 95

    # Capture is DETERMINISTIC — never an llm interpretation — and renders the
    # real numbers + arms in the body for human readers too.
    assert verdict["analysis_mode"] == "deterministic"
    assert "Reported result metrics" in docs[0].body
    assert "Experiment arms" in docs[0].body

    # NO VM-analysis re-queue: the task stays COMPLETED and the deleted bespoke
    # leg's context keys must never be stamped.
    assert "compute_vm_analysis_request" not in (task.context or {})
    assert "compute_vm_analysis_attempts" not in (task.context or {})


@pytest.mark.asyncio
async def test_arms_only_result_persists_direction_linked_knowledge(
    db_session, _quiet_compute_io
):
    """Multi-arm results are arms-ONLY (no top-level scalars); verify end-to-end.

    Production multi-arm aggregators emit a result with NO top-level scalars,
    only an arms block (per-arm comparisons). Verify that an arms-only result
    captures correctly into a direction-linked KnowledgeDocument so arm results
    cannot silently regress for the shape production actually emits.

    This test uses a task with NO declared metric criterion so the gate passes
    on presence (outcome + cost). The real production flow may eventually measure
    a criterion from the arms or use a different criterion kind; this test pins
    that arm-only results ARE captured and persisted faithfully when they occur.
    """
    # Task with identity and direction, but NO declared success criterion.
    # This allows arms-only results to complete without an unmeasured-criterion
    # failure; the arms themselves become the durable measured knowledge.
    task = await _make_compute_task(db_session)
    task.payloads = [
        {
            "type": "compute_experiment",
            "base_branch": "main",
            "experiment": "A/B comparison",
            "benchmark": "bench-v2",
            "tags": ["perf"],
            # No success_criterion declared.
        }
    ]
    task.context = {"direction_id": "d-compute"}
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job, _artifact_uri = await _setup_durable_job_with_artifact(db_session, task)
    # Arms-only result: no top-level scalars, just the arms block.
    # This is the real multi-arm aggregator shape from production.
    job.polling_metadata = {
        **(job.polling_metadata or {}),
        "result_metrics": {
            "arms": [
                {"name": "baseline", "latency_ms": 105},
                {"name": "optimized", "latency_ms": 95},
            ],
        },
    }
    db_session.add(job)
    await db_session.commit()

    logs = _result_artifact(job_id=str(job.id))
    await _handle_compute_experiment_completion(task, logs, db_session)

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED

    docs = await _knowledge_docs_for(db_session, task)
    assert len(docs) == 1
    doc = docs[0]

    # Direction tagging preserved: the knowledge document is linked to the
    # direction that commissioned the experiment.
    assert doc.direction_id == "d-compute"

    verdict = doc.forward_quality_verdict

    # Arms-only result: result_metrics carries ONLY the arms block, no scalars.
    assert verdict["result_metrics"] == {
        "arms": [
            {"name": "baseline", "latency_ms": 105},
            {"name": "optimized", "latency_ms": 95},
        ],
    }

    # Arms preserved in verdict_record for strategic comparison.
    arms = verdict["arms"]
    assert isinstance(arms, list)
    assert len(arms) == 2
    assert arms[0]["name"] == "baseline"
    assert arms[1]["name"] == "optimized"

    # Body includes the arms section (but NOT a separate scalar metrics section).
    assert "Experiment arms" in doc.body
    assert "Reported result metrics" not in doc.body

    # Deterministic capture: no LLM interpretation.
    assert verdict["analysis_mode"] == "deterministic"


@pytest.mark.asyncio
async def test_compute_doc_renders_per_arm_criterion_verdict(
    db_session, _quiet_compute_io
):
    """Per-arm criterion verdicts render deterministically in the knowledge document.

    When a criterion is declared and the experiment reports multi-arm results,
    the document should show which arms met the criterion and which did not,
    without requiring JSON parsing. The per-arm verdicts are both rendered in
    the human-readable body AND stored in forward_quality_verdict for querying.
    """
    task = await _make_compute_task(db_session)
    task.payloads = [
        {
            "type": "compute_experiment",
            "base_branch": "main",
            "experiment": "A/B performance test",
            "benchmark": "bench-suite-v2",
            "tags": ["perf"],
            "success_criterion": {
                "metric": "latency_ms",
                "operator": "<=",
                "threshold": 100,
            },
        }
    ]
    task.context = {"direction_id": "d-perf-test"}
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job, _artifact_uri = await _setup_durable_job_with_artifact(db_session, task)
    # Multi-arm result: each arm reports the metric
    # - baseline: 105ms (FAILS criterion: 105 > 100)
    # - optimized: 95ms (MEETS criterion: 95 <= 100)
    job.polling_metadata = {
        **(job.polling_metadata or {}),
        "result_metrics": {
            "arms": [
                {"name": "baseline", "latency_ms": 105},
                {"name": "optimized", "latency_ms": 95},
            ],
        },
    }
    db_session.add(job)
    await db_session.commit()

    # The result_artifact needs to include at least one metric value (the best arm)
    # so the criterion evaluation succeeds. The completion handler will look in
    # result_metrics (from polling_metadata) for the arms; the COMPUTE_RESULT logs
    # carry the flattened top-level metric.
    logs = _result_artifact(job_id=str(job.id), latency_ms=95)
    await _handle_compute_experiment_completion(task, logs, db_session)

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED

    docs = await _knowledge_docs_for(db_session, task)
    assert len(docs) == 1
    doc = docs[0]

    verdict = doc.forward_quality_verdict

    # (1) Per-arm verdicts are stored in the verdict_record for querying
    assert "per_arm_verdicts" in verdict
    per_arm = verdict["per_arm_verdicts"]
    assert "baseline" in per_arm
    assert "optimized" in per_arm

    # baseline: 105 does NOT meet criterion (105 > 100)
    assert per_arm["baseline"]["value"] == 105
    assert per_arm["baseline"]["met"] is False

    # optimized: 95 DOES meet criterion (95 <= 100)
    assert per_arm["optimized"]["value"] == 95
    assert per_arm["optimized"]["met"] is True

    # (2) Per-arm verdicts render in the human-readable body
    assert "Per-arm criterion evaluation" in doc.body
    assert "**baseline:**" in doc.body
    assert "**optimized:**" in doc.body
    assert "✓ MET" in doc.body  # optimized meets criterion
    assert "✗ NOT MET" in doc.body  # baseline does not meet criterion
    assert "latency_ms=105" in doc.body  # baseline value shown
    assert "latency_ms=95" in doc.body  # optimized value shown

    # (3) The best arm's value is captured at the top level (unchanged from before)
    assert verdict["criterion"]["value"] == 95  # best arm value
    assert verdict["criterion"]["met"] is True  # best arm meets criterion


@pytest.mark.asyncio
async def test_per_arm_verdicts_carry_apportioned_cost_usd(
    db_session, _quiet_compute_io
):
    """per_arm_verdicts carry value, met, AND apportioned cost_usd based on elapsed_seconds.

    When the arms block carries elapsed_seconds (written by the aggregator from
    wall-clock timing), the backend apportions total experiment cost proportionally
    so each per-arm verdict has a real cost_usd instead of None.
    """
    task = await _make_compute_task(db_session)
    task.payloads = [
        {
            "type": "compute_experiment",
            "base_branch": "main",
            "experiment": "cost-apportionment test",
            "success_criterion": {
                "metric": "accuracy",
                "operator": ">=",
                "threshold": 0.9,
            },
        }
    ]
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job, _artifact_uri = await _setup_durable_job_with_artifact(db_session, task)
    # Arms block carries elapsed_seconds for proportional cost split:
    # baseline ran 60s, optimized ran 40s → total 100s
    # total cost = $1.00 → baseline $0.60, optimized $0.40
    job.polling_metadata = {
        **(job.polling_metadata or {}),
        "result_metrics": {
            "arms": {
                "baseline": {"accuracy": 0.85, "exit_code": 0, "elapsed_seconds": 60.0},
                "optimized": {"accuracy": 0.95, "exit_code": 0, "elapsed_seconds": 40.0},
            },
        },
    }
    job.accrued_cost_usd = 1.0
    job.final_cost_usd = 1.0
    db_session.add(job)
    await db_session.commit()

    # Cost of $1.00 in the logs drives apportionment
    logs = _result_artifact(job_id=str(job.id), accuracy=0.95, final_cost_usd=1.0)
    await _handle_compute_experiment_completion(task, logs, db_session)

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED

    docs = await _knowledge_docs_for(db_session, task)
    assert len(docs) == 1
    verdict = docs[0].forward_quality_verdict

    assert "per_arm_verdicts" in verdict
    pav = verdict["per_arm_verdicts"]

    # value and met populated
    assert pav["baseline"]["value"] == pytest.approx(0.85)
    assert pav["baseline"]["met"] is False
    assert pav["optimized"]["value"] == pytest.approx(0.95)
    assert pav["optimized"]["met"] is True

    # cost_usd apportioned by elapsed_seconds: 60/(60+40)=0.6, 40/100=0.4
    assert pav["baseline"]["cost_usd"] == pytest.approx(0.60, rel=1e-4)
    assert pav["optimized"]["cost_usd"] == pytest.approx(0.40, rel=1e-4)


@pytest.mark.asyncio
async def test_per_arm_verdicts_even_split_when_no_durations(
    db_session, _quiet_compute_io
):
    """per_arm_verdicts fall back to even cost split when elapsed_seconds is absent.

    When the arms block has no elapsed_seconds (older aggregator or timing not
    available), cost is split evenly across arms.
    """
    task = await _make_compute_task(db_session)
    task.payloads = [
        {
            "type": "compute_experiment",
            "base_branch": "main",
            "experiment": "even-split fallback test",
            "success_criterion": {
                "metric": "accuracy",
                "operator": ">=",
                "threshold": 0.9,
            },
        }
    ]
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job, _artifact_uri = await _setup_durable_job_with_artifact(db_session, task)
    # Arms block WITHOUT elapsed_seconds — triggers even-split fallback
    job.polling_metadata = {
        **(job.polling_metadata or {}),
        "result_metrics": {
            "arms": {
                "arm_a": {"accuracy": 0.92, "exit_code": 0},
                "arm_b": {"accuracy": 0.88, "exit_code": 0},
            },
        },
    }
    job.accrued_cost_usd = 0.50
    job.final_cost_usd = 0.50
    db_session.add(job)
    await db_session.commit()

    # Cost of $0.50 in the logs drives even-split
    logs = _result_artifact(job_id=str(job.id), accuracy=0.92, final_cost_usd=0.50)
    await _handle_compute_experiment_completion(task, logs, db_session)

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED

    docs = await _knowledge_docs_for(db_session, task)
    assert len(docs) == 1
    verdict = docs[0].forward_quality_verdict

    assert "per_arm_verdicts" in verdict
    pav = verdict["per_arm_verdicts"]

    # Even split: 2 arms, total $0.50 → $0.25 each
    assert pav["arm_a"]["cost_usd"] == pytest.approx(0.25, rel=1e-4)
    assert pav["arm_b"]["cost_usd"] == pytest.approx(0.25, rel=1e-4)


@pytest.mark.asyncio
async def test_per_arm_cost_apportionment_mixed_durations_sums_to_total(
    db_session, _quiet_compute_io
):
    """per-arm costs sum to the experiment total in a mixed-duration run; a
    crashed arm (no elapsed_seconds, no metric) is reported as unmeasured
    (met=None), NOT as 'not met' (met=False).

    Three-arm experiment: two arms finish with timing data; one crashes and
    reports neither a metric nor elapsed_seconds.  The timed arms' apportioned
    costs must sum to the full experiment total (not exceed it), and the
    crashed arm must carry cost_usd=None and met=None.
    """
    task = await _make_compute_task(db_session)
    task.payloads = [
        {
            "type": "compute_experiment",
            "base_branch": "main",
            "experiment": "mixed-duration-apportionment",
            "success_criterion": {
                "metric": "accuracy",
                "operator": ">=",
                "threshold": 0.9,
            },
        }
    ]
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job, _artifact_uri = await _setup_durable_job_with_artifact(db_session, task)
    # fast: 30 s, 0.91 accuracy (MET); slow: 90 s, 0.88 accuracy (NOT MET);
    # crashed: no timing, no metric (UNMEASURED).
    # total elapsed from timed arms = 120 s; total cost = $1.20
    # → fast gets $1.20 * 30/120 = $0.30, slow gets $1.20 * 90/120 = $0.90,
    #   crashed gets None (no timing → cannot apportion).
    job.polling_metadata = {
        **(job.polling_metadata or {}),
        "result_metrics": {
            "arms": {
                "fast": {"accuracy": 0.91, "exit_code": 0, "elapsed_seconds": 30.0},
                "slow": {"accuracy": 0.88, "exit_code": 0, "elapsed_seconds": 90.0},
                "crashed": {"exit_code": 1},  # no metric, no elapsed
            },
        },
    }
    job.accrued_cost_usd = 1.20
    job.final_cost_usd = 1.20
    db_session.add(job)
    await db_session.commit()

    logs = _result_artifact(job_id=str(job.id), accuracy=0.91, final_cost_usd=1.20)
    await _handle_compute_experiment_completion(task, logs, db_session)

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED

    docs = await _knowledge_docs_for(db_session, task)
    assert len(docs) == 1
    verdict = docs[0].forward_quality_verdict

    assert "per_arm_verdicts" in verdict
    pav = verdict["per_arm_verdicts"]

    # Timed arms: value + met populated
    assert pav["fast"]["value"] == pytest.approx(0.91)
    assert pav["fast"]["met"] is True  # 0.91 >= 0.9
    assert pav["slow"]["value"] == pytest.approx(0.88)
    assert pav["slow"]["met"] is False  # 0.88 < 0.9

    # Crashed arm: unmeasured — met must be None, not False
    assert pav["crashed"]["value"] is None
    assert pav["crashed"]["met"] is None, (
        "crashed arm (no metric) must be unmeasured (met=None), not 'not met' (met=False)"
    )
    assert pav["crashed"]["cost_usd"] is None

    # Timed arms' apportioned costs must sum to the experiment total
    fast_cost = pav["fast"]["cost_usd"]
    slow_cost = pav["slow"]["cost_usd"]
    assert fast_cost is not None
    assert slow_cost is not None
    assert fast_cost + slow_cost == pytest.approx(1.20, rel=1e-4), (
        "per-arm costs for timed arms must sum to the experiment total"
    )
    assert fast_cost == pytest.approx(0.30, rel=1e-4)
    assert slow_cost == pytest.approx(0.90, rel=1e-4)


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
        task.plan_output["knowledge_document"]["knowledge_acceptance_evidence"] is True
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
    assert (
        substance[0].forward_quality_verdict["compute_acceptance_grade"] == "acceptance"
    )

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
    deterministic_index = await build_knowledge_index(
        db_session, deterministic_task.project_id
    )
    assert (
        deterministic_index[0]["compute_acceptance_reason"] == "analysis_mode_not_llm"
    )
    assert deterministic_index[0]["analysis_fallback_reason"] is None
    deterministic_substance = await fetch_knowledge_substance_index(
        db_session, deterministic_task.project_id
    )
    assert (
        deterministic_substance[0].forward_quality_verdict[
            "knowledge_acceptance_evidence"
        ]
        is False
    )
    assert (
        deterministic_substance[0].forward_quality_verdict["compute_acceptance_grade"]
        == "measured_only"
    )
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
    assert (
        deterministic_task.plan_output["knowledge_document"]["analysis_fallback_reason"]
        is None
    )
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
        forward_quality_verdict={
            "passed": True,
            "reason": "ok",
            "strategy": "forward-quality",
        },
    )
    non_compute_detail = serialize_knowledge_document(
        non_compute_doc, include_body=True
    )
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
    served_at = _utcnow()
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
    newer_job.completed_at = _utcnow()
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
    assert (
        task.plan_output["knowledge_document"]["release_evidence"]
        == older_release_evidence
    )

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
            payloads=[
                {
                    "type": "compute_experiment",
                    "experiment": name,
                    "base_branch": "main",
                }
            ],
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

    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
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
                    "smoke_sample": {
                        "exit_code": 0,
                        "startup_ok": True,
                        "metric_captured": True,
                        "timed_out": False,
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
    assert (
        payload.get("acceptance_evidence", {}).get("artifact_id")
        == "plan-result-bundle"
    ), f"acceptance_evidence not in payload after plan_completion spawn: {payload}"

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
    job.status = ComputeJobStatus.SUCCEEDED
    job.outcome = ComputeJobOutcome.SUCCESS
    job.accrued_cost_usd = 0.25
    job.final_cost_usd = 0.25
    job.terminal_reason = "compute job finished"
    job.completed_at = _utcnow()
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
    assert (
        docs_after_first[0].forward_quality_verdict["analysis_mode"] == "deterministic"
    )
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
        task.plan_output["knowledge_document"]["release_evidence"] == release_evidence
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

    Completion captures the deterministic doc faithfully; the explicit
    (operator-invoked) analyze step performs the cited LLM upgrade and stripping.
    """
    from app.knowledge_completion import analyze_compute_experiment_knowledge

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

    await _handle_compute_experiment_completion(task, logs, db_session)
    with (
        patch.dict(
            os.environ,
            {"ANTHROPIC_API_KEY": "test-key", "DISABLE_LLM_COMPUTE_ANALYSIS": ""},
        ),
        patch("anthropic.AsyncAnthropic", mock_cls),
    ):
        await analyze_compute_experiment_knowledge(
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
            result_reference=artifact_uri,
            session=db_session,
            logs=logs,
        )

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
        payloads=[
            {
                "type": "compute_experiment",
                "experiment": "mismatch-test",
                "base_branch": "main",
            }
        ],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Artifact is for job-a's bundle.
    artifact = Artifact(
        task_id=task.id,
        artifact_type=ArtifactType.COMPUTE_RESULT,
        storage_uri="gs://bucket/jobs/job-a/artifacts/",
        logs=json.dumps(
            {
                "artifact_uri": "gs://bucket/jobs/job-a/artifacts/",
                "file_count": 2,
            }
        ),
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
async def test_grounding_stamp_error_fails_closed(
    db_session, mocker, _quiet_compute_io
):
    """When citation-grounding probe hits an unexpected error, the document is
    persisted with citations cleared (fail-closed), not with ungrounded citations."""
    from app.knowledge_completion import persist_compute_experiment_knowledge

    project = Project(name="GroundingErrorTest", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="compute_experiment: grounding error",
        status=TaskStatus.COMPLETED,
        payloads=[
            {
                "type": "compute_experiment",
                "experiment": "error-test",
                "base_branch": "main",
            }
        ],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    gs_uri = "gs://bucket/jobs/result.json"

    # Create an artifact to back the result_reference
    artifact = Artifact(
        task_id=task.id,
        artifact_type=ArtifactType.COMPUTE_RESULT,
        storage_uri=gs_uri,
        logs=json.dumps({"artifact_uri": gs_uri, "file_count": 1}),
    )
    db_session.add(artifact)
    await db_session.commit()
    await db_session.refresh(artifact)

    # Create a job referencing the artifact
    job = ComputeJob(
        project_id=project.id,
        task_id=task.id,
        backend="local",
        status=ComputeJobStatus.SUCCEEDED,
        artifact_id=artifact.id,
        result_reference=gs_uri,
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    logs_json = json.dumps({"job_id": str(job.id), "outcome": "SUCCESS"})

    # Mock _compute_citation_grounding_stamp to raise an exception
    mocker.patch(
        "app.knowledge_completion._compute_citation_grounding_stamp",
        side_effect=RuntimeError("database connection lost"),
    )

    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
        doc = await persist_compute_experiment_knowledge(
            task,
            outcome="SUCCESS",
            cost=0.10,
            criterion=None,
            result_reference=gs_uri,
            session=db_session,
            logs=logs_json,
        )

    assert doc is not None
    # Fail-closed: citations must be empty when grounding probe fails
    assert doc.source_citations == [], (
        "citations must be cleared when grounding stamp probe fails unexpectedly"
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
        payloads=[
            {
                "type": "compute_experiment",
                "experiment": "http-test",
                "base_branch": "main",
            }
        ],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Create an artifact with http:// storage_uri (the fallback for artifact_uri).
    artifact = Artifact(
        task_id=task.id,
        artifact_type=ArtifactType.COMPUTE_RESULT,
        storage_uri="https://signed-download.example.com/result.tar.gz?token=xyz",
        logs=json.dumps(
            {
                "artifact_uri": None,
                "file_count": 2,
            }
        ),
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


@pytest.mark.asyncio
async def test_gather_compute_evidence_surfaces_command_log_text(
    db_session, _quiet_compute_io
):
    """When the COMPUTE_RESULT artifact text embeds command_log_text (a result that
    lives in stdout/stderr rather than a scalar), _gather_compute_evidence surfaces
    it so the analysis prompt can preserve it rather than discarding it."""
    from app.knowledge_completion import _gather_compute_evidence

    project = Project(name="CmdLogEvidence", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="compute_experiment: command-log result",
        status=TaskStatus.COMPLETED,
        payloads=[
            {"type": "compute_experiment", "experiment": "eval", "base_branch": "main"}
        ],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    artifact = Artifact(
        task_id=task.id,
        artifact_type=ArtifactType.COMPUTE_RESULT,
        storage_uri="gs://bucket/jobs/result/bundle.tar.gz",
        logs=json.dumps(
            {"artifact_uri": "gs://bucket/jobs/result/bundle.tar.gz", "file_count": 3}
        ),
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
        result_reference="gs://bucket/jobs/result/bundle.tar.gz",
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    logs_json = json.dumps(
        {
            "job_id": str(job.id),
            "command_log_text": "act-1-clear improvement: +0.16 over baseline\n",
        }
    )

    evidence = await _gather_compute_evidence(
        task,
        logs=logs_json,
        result_reference=None,
        criterion=None,
        session=db_session,
    )

    assert evidence is not None
    assert evidence["command_log_text"] is not None
    assert "act-1-clear improvement: +0.16" in evidence["command_log_text"]


@pytest.mark.asyncio
async def test_gather_compute_evidence_no_artifact_no_command_log_is_safe(
    db_session, _quiet_compute_io
):
    """Backward-compat: a metrics-only job with a durable result_reference + metrics
    but NO linked artifact_id and no embedded command log must not raise (regression
    guard for the art_logs UnboundLocalError) and yields command_log_text=None."""
    from app.knowledge_completion import _gather_compute_evidence

    project = Project(name="MetricsOnlyEvidence", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="compute_experiment: metrics only",
        status=TaskStatus.COMPLETED,
        payloads=[
            {"type": "compute_experiment", "experiment": "eval", "base_branch": "main"}
        ],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = ComputeJob(
        project_id=project.id,
        task_id=task.id,
        backend="local",
        status=ComputeJobStatus.SUCCEEDED,
        artifact_id=None,  # no linked COMPUTE_RESULT artifact row
        result_reference="gs://bucket/jobs/result/bundle.tar.gz",
        polling_metadata={"result_metrics": {"accuracy": 0.93}},
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    logs_json = json.dumps({"job_id": str(job.id)})  # no command_log_text key

    evidence = await _gather_compute_evidence(
        task,
        logs=logs_json,
        result_reference=None,
        criterion=None,
        session=db_session,
    )

    assert evidence is not None
    assert evidence["command_log_text"] is None
    assert evidence["metrics"] == {"accuracy": 0.93}


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
    job.completed_at = _utcnow()
    job.polling_metadata = {
        **(job.polling_metadata or {}),
        "runner_artifact": {
            "stored": True,
            "verified": True,
            "size": 123,
            "crc32c": "AAAAAA==",
            "generation": "7",
        },
    }
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
    assert delivery["transfer_status"] == "verified"
    assert delivery["verified"] is True
    assert delivery["size"] == 123
    assert delivery["crc32c"] == "AAAAAA=="
    assert delivery["generation"] == "7"
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
    job.completed_at = _utcnow()
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


@pytest.mark.asyncio
async def test_artifact_delivery_record_failed_transfer_stamped(
    db_session, _quiet_compute_io
):
    """A finalized direct-transfer failure is distinguishable from no attempt."""
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
    job.polling_metadata = {
        **(job.polling_metadata or {}),
        "runner_artifact_transfer": {
            "transfer_id": str(uuid.uuid4()),
            "status": "failed",
            "failure_reason": "metadata_mismatch",
        },
    }
    db_session.add(job)
    await db_session.commit()

    await _handle_compute_experiment_completion(
        task,
        _result_artifact(job_id=str(job.id)),
        db_session,
    )

    await db_session.refresh(task)
    delivery = task.plan_output["compute_result"]["artifact_delivery"]
    assert delivery["delivered"] is False
    assert delivery["durability"] == "none"
    assert delivery["transfer_status"] == "failed"
    assert delivery["verified"] is False


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
    job.completed_at = _utcnow()
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
    job.completed_at = _utcnow()

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
    job.completed_at = _utcnow()
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

    await _handle_compute_experiment_completion(task, logs, db_session)
    from app.knowledge_completion import analyze_compute_experiment_knowledge

    with (
        patch.dict(
            os.environ,
            {"ANTHROPIC_API_KEY": "test-key", "DISABLE_LLM_COMPUTE_ANALYSIS": ""},
        ),
        patch("anthropic.AsyncAnthropic", mock_cls),
    ):
        await analyze_compute_experiment_knowledge(
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
            result_reference=artifact_uri,
            session=db_session,
            logs=logs,
        )

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

    await _handle_compute_experiment_completion(task, logs, db_session)
    from app.knowledge_completion import analyze_compute_experiment_knowledge

    with (
        patch.dict(
            os.environ,
            {"ANTHROPIC_API_KEY": "test-key", "DISABLE_LLM_COMPUTE_ANALYSIS": ""},
        ),
        patch("anthropic.AsyncAnthropic", mock_cls),
    ):
        await analyze_compute_experiment_knowledge(
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
            result_reference=artifact_uri,
            session=db_session,
            logs=logs,
        )

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

    await _handle_compute_experiment_completion(task, logs, db_session)
    from app.knowledge_completion import analyze_compute_experiment_knowledge

    with (
        patch.dict(
            os.environ,
            {"ANTHROPIC_API_KEY": "test-key", "DISABLE_LLM_COMPUTE_ANALYSIS": ""},
        ),
        patch("anthropic.AsyncAnthropic", mock_cls),
    ):
        await analyze_compute_experiment_knowledge(
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
            result_reference=artifact_uri,
            session=db_session,
            logs=logs,
        )

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

    await _handle_compute_experiment_completion(task, logs, db_session)
    from app.knowledge_completion import analyze_compute_experiment_knowledge

    with (
        patch.dict(
            os.environ,
            {"ANTHROPIC_API_KEY": "test-key", "DISABLE_LLM_COMPUTE_ANALYSIS": ""},
        ),
        patch("anthropic.AsyncAnthropic", mock_cls),
    ):
        await analyze_compute_experiment_knowledge(
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
            result_reference=artifact_uri,
            session=db_session,
            logs=logs,
        )

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
    job.started_at = _utcnow() - timedelta(seconds=120)
    job.wall_clock_deadline = _utcnow() - timedelta(seconds=1)
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
    job.started_at = _utcnow() - timedelta(seconds=60)
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
    job.started_at = _utcnow() - timedelta(seconds=120)
    job.wall_clock_deadline = _utcnow() - timedelta(seconds=1)
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


# ── Backend-derived durable-artifact success criteria ───────────────────────


@pytest.mark.asyncio
async def test_durable_artifact_criterion_backend_derived_on_timeout(
    db_session, _quiet_compute_io
):
    """Acceptance proof: a TIMED_OUT job that left a durable artifact evaluates
    durable_payload_bytes as measurable=True from the backend's own measurement
    even though the command never reported it.  A job with no durable artifact
    evaluates as met=False (fail-closed), not unmeasurable-and-skipped.

    This is the named acceptance test declared in the task plan:
    ``test_durable_artifact_criterion_backend_derived_on_timeout``.
    """
    task = await _make_compute_task_with_criterion(
        db_session, "durable_payload_bytes", ">=", 8192
    )
    task.status = TaskStatus.IN_PROGRESS
    db_session.add(task)
    await db_session.commit()

    # ── Part A: TIMED_OUT job WITH backend-measured durable artifact ─────────
    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="echo hi",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )
    job.status = ComputeJobStatus.TIMED_OUT
    job.outcome = ComputeJobOutcome.TIMED_OUT
    job.accrued_cost_usd = 0.20
    job.final_cost_usd = 0.20
    job.terminal_reason = "wall clock deadline exceeded"
    job.completed_at = _utcnow()
    # Simulate what _link_terminal_artifact stamps after detecting a durable artifact.
    job.polling_metadata = {
        **(job.polling_metadata or {}),
        "durable_payload_bytes": 16384,
        "durable_file_count": 2,
    }
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    logs = compute_jobs.build_compute_result_logs(job)
    task.status = TaskStatus.COMPLETED
    db_session.add(task)
    await db_session.commit()
    await _handle_compute_experiment_completion(task, logs, db_session)

    await db_session.refresh(task)
    criterion = task.plan_output["compute_result"]["criterion"]
    assert criterion["measurable"] is True, (
        "durable_payload_bytes stamped by backend must be measurable even on TIMED_OUT"
    )
    assert criterion["met"] is True
    assert criterion["value"] == pytest.approx(16384)
    assert criterion["metric"] == "durable_payload_bytes"
    assert task.context["compute_success_criterion"]["measurable"] is True
    assert task.context["compute_success_criterion"]["met"] is True

    # ── Part B: job with NO durable artifact → fail-closed (met=False) ───────
    task2 = await _make_compute_task_with_criterion(
        db_session, "durable_payload_bytes", ">=", 8192
    )
    # No durable_payload_bytes in polling_metadata — no artifact survived teardown.
    logs2 = _result_artifact(
        outcome="TIMED_OUT", status="TIMED_OUT", final_cost_usd=0.10
    )
    await _handle_compute_experiment_completion(task2, logs2, db_session)

    await db_session.refresh(task2)
    criterion2 = task2.plan_output["compute_result"]["criterion"]
    assert criterion2["met"] is False, (
        "absent durable_payload_bytes must be fail-closed (met=False), not a pass"
    )


@pytest.mark.asyncio
async def test_compute_durable_payload_bytes_criterion_unmet(
    db_session, _quiet_compute_io
):
    """Backend-measured payload below the declared threshold records met=False."""
    task = await _make_compute_task_with_criterion(
        db_session, "durable_payload_bytes", ">=", 65536
    )
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
    job.accrued_cost_usd = 0.10
    job.final_cost_usd = 0.10
    job.terminal_reason = "compute job finished"
    job.completed_at = _utcnow()
    job.polling_metadata = {
        **(job.polling_metadata or {}),
        "durable_payload_bytes": 4096,  # below threshold of 65536
        "durable_file_count": 1,
    }
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    logs = compute_jobs.build_compute_result_logs(job)
    task.status = TaskStatus.COMPLETED
    db_session.add(task)
    await db_session.commit()
    await _handle_compute_experiment_completion(task, logs, db_session)

    await db_session.refresh(task)
    criterion = task.plan_output["compute_result"]["criterion"]
    assert criterion["measurable"] is True
    assert criterion["met"] is False
    assert criterion["value"] == pytest.approx(4096)
    assert task.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_compute_durable_file_count_criterion(db_session, _quiet_compute_io):
    """Backend-measured durable_file_count satisfies a declared >= criterion."""
    task = await _make_compute_task_with_criterion(
        db_session, "durable_file_count", ">=", 3
    )
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
    job.accrued_cost_usd = 0.15
    job.final_cost_usd = 0.15
    job.terminal_reason = "compute job finished"
    job.completed_at = _utcnow()
    job.polling_metadata = {
        **(job.polling_metadata or {}),
        "durable_payload_bytes": 8192,
        "durable_file_count": 5,  # above threshold of 3
    }
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    logs = compute_jobs.build_compute_result_logs(job)
    task.status = TaskStatus.COMPLETED
    db_session.add(task)
    await db_session.commit()
    await _handle_compute_experiment_completion(task, logs, db_session)

    await db_session.refresh(task)
    criterion = task.plan_output["compute_result"]["criterion"]
    assert criterion["measurable"] is True
    assert criterion["met"] is True
    assert criterion["value"] == pytest.approx(5)
    assert criterion["metric"] == "durable_file_count"
    assert task.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_link_terminal_artifact_stamps_durable_metrics(
    db_session, _quiet_compute_io, mocker
):
    """_link_terminal_artifact stamps durable_file_count and durable_payload_bytes
    into job.polling_metadata when a TerminalArtifactReference is found, and
    build_compute_result_logs surfaces those values so the success criterion gate
    can evaluate them.

    This test drives the real producer (_link_terminal_artifact) rather than
    manually inserting values into polling_metadata, so a regression where the
    collector stops populating total_size_bytes or the stamping step is removed
    will be caught here.
    """
    task = await _make_compute_task_with_criterion(
        db_session, "durable_payload_bytes", ">=", 8192
    )
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
    job.accrued_cost_usd = 0.18
    job.final_cost_usd = 0.18
    job.terminal_reason = "compute job finished"
    job.completed_at = _utcnow()
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    # Mock collect_terminal_artifact_reference to return a realistic reference.
    fake_ref = TerminalArtifactReference(
        backend="gcp",
        uri="gs://blueprint-agent-artifacts/compute-jobs/test-job/",
        file_count=3,
        total_size_bytes=20480,
    )
    mocker.patch(
        "app.compute_jobs.collect_terminal_artifact_reference",
        new=AsyncMock(return_value=fake_ref),
    )

    await compute_jobs._link_terminal_artifact(db_session, job)
    await db_session.refresh(job)

    # Verify _link_terminal_artifact stamped durable metrics.
    meta = job.polling_metadata or {}
    assert meta.get("durable_file_count") == 3, (
        "_link_terminal_artifact must stamp durable_file_count from TerminalArtifactReference"
    )
    assert meta.get("durable_payload_bytes") == 20480, (
        "_link_terminal_artifact must stamp durable_payload_bytes from TerminalArtifactReference"
    )

    # Verify build_compute_result_logs surfaces those values as top-level keys.
    logs = compute_jobs.build_compute_result_logs(job)
    result = json.loads(logs)
    assert result.get("durable_file_count") == 3
    assert result.get("durable_payload_bytes") == 20480

    # Verify the criterion evaluates using the backend-stamped values.
    task.status = TaskStatus.COMPLETED
    db_session.add(task)
    await db_session.commit()
    await _handle_compute_experiment_completion(task, logs, db_session)

    await db_session.refresh(task)
    criterion = task.plan_output["compute_result"]["criterion"]
    assert criterion["measurable"] is True
    assert criterion["met"] is True
    assert criterion["value"] == pytest.approx(20480)


@pytest.mark.asyncio
async def test_command_cannot_spoof_durable_metrics_via_result_metrics(
    db_session, _quiet_compute_io
):
    """A command-reported result_metrics entry named durable_payload_bytes or
    durable_file_count must NEVER satisfy a durable-artifact success criterion,
    even when the backend stamped no value (i.e. no artifact survived teardown).

    Without unconditional blocking of these reserved names in the result_metrics
    flattening loop, a job that produced no durable output could be made to appear
    as if it had by including the reserved key in its BLUEPRINT_RESULT_FILE payload.
    """
    task = await _make_compute_task_with_criterion(
        db_session, "durable_payload_bytes", ">=", 8192
    )
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
    job.accrued_cost_usd = 0.10
    job.final_cost_usd = 0.10
    job.terminal_reason = "compute job finished"
    job.completed_at = _utcnow()
    # No durable artifact was linked; command reports spoofed reserved keys.
    job.polling_metadata = {
        **(job.polling_metadata or {}),
        "result_metrics": {
            "durable_payload_bytes": 999999,
            "durable_file_count": 100,
        },
    }
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    logs = compute_jobs.build_compute_result_logs(job)
    result = json.loads(logs)
    # Reserved names must not appear even though no backend value was stamped.
    assert "durable_payload_bytes" not in result, (
        "command-reported durable_payload_bytes must be blocked unconditionally"
    )
    assert "durable_file_count" not in result, (
        "command-reported durable_file_count must be blocked unconditionally"
    )

    task.status = TaskStatus.COMPLETED
    db_session.add(task)
    await db_session.commit()
    await _handle_compute_experiment_completion(task, logs, db_session)

    await db_session.refresh(task)
    criterion = task.plan_output["compute_result"]["criterion"]
    assert criterion["met"] is False, (
        "spoofed durable_payload_bytes via result_metrics must not satisfy the criterion"
    )


# ── Provisioning-phase terminal diagnostics ──────────────────────────────────


def test_provisioning_death_terminal_diagnostics_includes_cause_and_contract():
    """A provisioning-phase job (started_at=None) must surface sanitized
    provisioning_cause and launch_contract in _extract_job_terminal_diagnostics,
    and a planted secret/signed-URL in the raw diagnostic must NOT appear.
    """
    job = MagicMock()
    job.terminal_reason = "compute job timed out while provisioning"
    job.started_at = None
    job.accelerator_type = "nvidia-tesla-t4"
    job.machine_type = "n1-standard-4"
    job.polling_metadata = {
        "compute_rpc_diagnostic": {
            "operation": "create_instance",
            "error_type": "GoogleAPIError",
            "error_message": (
                "RESOURCE_EXHAUSTED: quota exceeded "
                "https://gcp.googleapis.com/v1/instances?"
                "token=SECRET123&x-goog-signature=ABCDEF"
            ),
            "reason": "QUOTA_EXCEEDED",
        },
        "requested_deadline_seconds": 3600,
        "provisioning_model": "SPOT",
    }

    diag = _extract_job_terminal_diagnostics(job)

    assert diag is not None
    assert diag["vm_phase"] == "provisioning"
    assert diag["terminal_reason"] == "compute job timed out while provisioning"
    assert diag["accelerator_type"] == "nvidia-tesla-t4"
    assert diag["machine_type"] == "n1-standard-4"

    cause = diag.get("provisioning_cause")
    assert cause is not None, (
        "provisioning_cause must be populated from compute_rpc_diagnostic"
    )
    assert cause["error_type"] == "GoogleAPIError"
    assert cause["reason"] == "QUOTA_EXCEEDED"
    msg = cause.get("error_message", "")
    assert "SECRET123" not in msg, "planted secret token must be redacted"
    assert "ABCDEF" not in msg, "planted signature value must be redacted"
    assert "[redacted]" in msg, "query string must be replaced with [redacted]"

    contract = diag.get("launch_contract")
    assert contract is not None, (
        "launch_contract must be populated from polling_metadata"
    )
    assert contract["requested_deadline_seconds"] == 3600
    assert contract["provisioning_model"] == "SPOT"


def test_provisioning_death_knowledge_document_renders_cause_and_contract():
    """A provisioning-phase terminal job must produce a knowledge document whose
    summary, body, AND verdict_record carry the sanitized provisioning_cause and
    launch_contract, and no raw secrets appear.
    """
    job = MagicMock()
    job.terminal_reason = "compute job timed out while provisioning"
    job.started_at = None
    job.accelerator_type = None
    job.machine_type = None
    job.polling_metadata = {
        "compute_rpc_diagnostic": {
            "operation": "create_instance",
            "error_type": "ZoneCapacityError",
            "error_message": (
                "No capacity in zone us-central1-a "
                "https://provider.example.com/api?X-Amz-Signature=MYSECRET&key=MYKEY"
            ),
            "reason": "ZONE_CAPACITY",
        },
        "requested_deadline_seconds": 7200,
        "provisioning_model": "STANDARD",
    }

    diag = _extract_job_terminal_diagnostics(job)
    assert diag is not None

    task = MagicMock()
    task.first_payload = {"experiment": "GPU throughput test", "tags": []}

    doc = _build_compute_experiment_document(
        task,
        outcome="TIMED_OUT",
        cost=0.0,
        criterion=None,
        result_reference=None,
        terminal_diagnostics=diag,
    )

    summary = doc["summary"]
    body = doc["body"]
    vr = doc["verdict_record"]

    # Secret values must not appear anywhere in the document
    for secret in ("MYSECRET", "MYKEY"):
        assert secret not in summary, f"secret {secret!r} must not appear in summary"
        assert secret not in body, f"secret {secret!r} must not appear in body"

    # Sanitized diagnostic fields must be visible
    assert "ZoneCapacityError" in summary or "ZoneCapacityError" in body
    assert "ZONE_CAPACITY" in summary or "ZONE_CAPACITY" in body
    assert "7200" in body
    assert "STANDARD" in body

    # verdict_record must carry both new fields
    td = vr.get("terminal_diagnostics", {})
    assert "provisioning_cause" in td, (
        "verdict_record terminal_diagnostics must have provisioning_cause"
    )
    assert "launch_contract" in td, (
        "verdict_record terminal_diagnostics must have launch_contract"
    )
    assert td["launch_contract"]["requested_deadline_seconds"] == 7200
    assert td["launch_contract"]["provisioning_model"] == "STANDARD"


def test_runtime_phase_terminal_diagnostics_no_provisioning_fields():
    """A runtime-phase job (started_at set) must NOT carry provisioning_cause or
    launch_contract in its terminal diagnostics (runtime failures keep today's
    behavior byte-for-byte unchanged).
    """
    job = MagicMock()
    job.terminal_reason = "compute job spend cap exceeded"
    job.started_at = _utcnow()
    job.accelerator_type = "nvidia-tesla-t4"
    job.machine_type = "n1-standard-4"
    job.polling_metadata = {
        "compute_rpc_diagnostic": {
            "operation": "get_status",
            "error_type": "BudgetError",
            "error_message": "spend cap exceeded",
        },
        "requested_deadline_seconds": 3600,
        "provisioning_model": "SPOT",
    }

    diag = _extract_job_terminal_diagnostics(job)

    assert diag is not None
    assert diag["vm_phase"] == "runtime"
    assert "provisioning_cause" not in diag, (
        "runtime-phase terminal diagnostics must not include provisioning_cause"
    )
    assert "launch_contract" not in diag, (
        "runtime-phase terminal diagnostics must not include launch_contract"
    )


def test_compute_terminal_diagnostics_surface_executing_backend_and_runner():
    """_extract_job_terminal_diagnostics must surface 'executing_backend' from
    job.backend and 'runner_target_id' from polling_metadata, so a compute
    experiment proves which backend/runner executed the job."""
    # Runtime-phase job with a runner target
    job = MagicMock()
    job.terminal_reason = "compute job finished"
    job.started_at = _utcnow()
    job.accelerator_type = "nvidia-tesla-t4"
    job.machine_type = "n1-standard-4"
    job.backend = "runner-subprocess"
    job.polling_metadata = {
        "runner_target_id": "runner-abc-123",
        "runner_hostname": "worker-host",
    }

    diag = _extract_job_terminal_diagnostics(job)

    assert diag is not None
    assert diag["executing_backend"] == "runner-subprocess"
    assert diag["runner_target_id"] == "runner-abc-123"
    assert diag["runner_hostname"] == "worker-host"

    # Cloud job with no runner_target_id should not carry runner_target_id
    cloud_job = MagicMock()
    cloud_job.terminal_reason = "compute job finished"
    cloud_job.started_at = _utcnow()
    cloud_job.accelerator_type = None
    cloud_job.machine_type = None
    cloud_job.backend = "gcp"
    cloud_job.polling_metadata = {}

    diag_cloud = _extract_job_terminal_diagnostics(cloud_job)

    assert diag_cloud is not None
    assert diag_cloud["executing_backend"] == "gcp"
    assert "runner_target_id" not in diag_cloud

    # Knowledge document body must surface both fields
    task = MagicMock()
    task.first_payload = {"experiment": "GPU throughput test", "tags": []}

    doc = _build_compute_experiment_document(
        task,
        outcome="SUCCESS",
        cost=0.37,
        criterion=None,
        result_reference=None,
        terminal_diagnostics=diag,
    )

    body = doc["body"]
    assert "Executing backend:" in body
    assert "runner-subprocess" in body
    assert "Runner:" in body
    assert "runner-abc-123" in body
    assert "worker-host" in body

    # verdict_record must carry both fields
    td = doc["verdict_record"].get("terminal_diagnostics", {})
    assert td["executing_backend"] == "runner-subprocess"
    assert td["runner_target_id"] == "runner-abc-123"
    assert td["runner_hostname"] == "worker-host"


# ── E2E local-backend smoke ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_local_backend_e2e_smoke(db_session, monkeypatch, _quiet_compute_io):
    """End-to-end smoke: real LocalSubprocessComputeBackend drives create →
    launch → poll → verdict-persistence without a live GCP VM.

    Uses a measured-NEGATIVE result (accuracy=0.85 misses criterion >=0.90) to
    confirm that an unmet criterion still persists an honest verdict and
    delivers it to a queued dependent — the key regression scenario (a broken
    implementation discards the negative verdict instead of recording it).
    """
    # Enable LOCAL_MODE so create_compute_job accepts the local-subprocess backend.
    monkeypatch.setattr(compute, "LOCAL_MODE", True)

    task = await _make_compute_task_with_criterion(db_session, "accuracy", ">=", 0.90)
    task.status = TaskStatus.IN_PROGRESS
    db_session.add(task)
    await db_session.commit()

    dependent = Task(
        project_id=task.project_id,
        title="act on compute result",
        status=TaskStatus.QUEUED,
        payloads=[{"type": "create", "prompt": "use result", "base_branch": "main"}],
        depends_on=[str(task.id)],
    )
    db_session.add(dependent)
    await db_session.commit()
    await db_session.refresh(dependent)

    # Command writes accuracy=0.85 (misses >=0.90) to the result file and produces
    # stdout so command.log.json lands in $BLUEPRINT_ARTIFACT_DIR — required for
    # _link_terminal_artifact to detect a non-empty artifact dir and set a file://
    # result_reference, which in turn makes knowledge_substance_has_durable_compute_grounding
    # return True so the dependent receives the prerequisite pointer.
    command = (
        'printf \'{"accuracy": 0.85}\' > "$BLUEPRINT_RESULT_FILE" && echo computed'
    )

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command=command,
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=60,
        backend_name=LOCAL_BACKEND_NAME,
    )

    backend = LocalSubprocessComputeBackend()
    monkeypatch.setattr(compute_jobs, "get_compute_backend", lambda _name: backend)
    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )

    # Pass 1: STARTING → launch (subprocess starts).
    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)
    await db_session.refresh(job)

    # Poll until terminal (command is near-instant; 10 passes x 0.2 s = 2 s max).
    for _ in range(10):
        if job.status in (
            ComputeJobStatus.SUCCEEDED,
            ComputeJobStatus.FAILED,
            ComputeJobStatus.TIMED_OUT,
        ):
            break
        await asyncio.sleep(0.2)
        await compute_jobs.process_compute_jobs_once(session_factory=session_factory)
        await db_session.refresh(job)

    # The command exits 0, so the job must reach SUCCEEDED.
    assert job.status == ComputeJobStatus.SUCCEEDED, (
        f"job status is {job.status!r} after polling; "
        "expected SUCCEEDED (command exits 0)"
    )
    assert job.outcome == ComputeJobOutcome.SUCCESS
    # At least one poll attempt was recorded.
    assert (job.poll_attempt_count or 0) >= 1

    # Build the COMPUTE_RESULT artifact the way the leader-wake worker does.
    result_logs = compute_jobs.build_compute_result_logs(job)

    # Completion handler persists the verdict.
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
        await _handle_compute_experiment_completion(task, result_logs, db_session)

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    assert task.plan_output["compute_result"]["outcome"] == "SUCCESS"

    # Criterion recorded: accuracy was captured from BLUEPRINT_RESULT_FILE.
    # If the subprocess env didn't propagate the var, the metric is absent and
    # measurable=False; either way met must be False (never met on negative run).
    ctx_criterion = task.context.get("compute_success_criterion", {})
    assert ctx_criterion.get("met") is False, (
        "measured-negative result must record met=False, not discard the verdict"
    )

    # Knowledge document persisted with honest negative verdict.
    docs = await _knowledge_docs_for(db_session, task)
    assert len(docs) == 1, "negative result must still persist a knowledge document"
    doc = docs[0]
    assert doc.source_kind == "compute_experiment"
    assert doc.forward_quality_verdict["criterion_met"] is False

    # Dependent receives the knowledge prerequisite from the completed producer.
    await db_session.refresh(dependent)
    prereqs = (dependent.context or {}).get(KNOWLEDGE_PREREQUISITES_KEY)
    assert prereqs and prereqs[0]["source_task_id"] == str(task.id), (
        "dependent must receive knowledge prerequisite pointer after task completes"
    )


# ── Sweep-arm knowledge indexing ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_completed_arm_knowledge_retrievable_by_sweep_and_surfaced_to_strategist(
    db_session, _quiet_compute_io, mocker
):
    """Completed sweep arms create KnowledgeDocuments tagged with sweep_id and
    arm_label; build_sweep_knowledge_index returns them grouped by sweep; and
    build_compute_sweep_groups exposes the sweep to the strategist payload."""
    # Suppress sweep-aggregate spawn so arms complete cleanly and this test
    # stays focused on knowledge document creation / sweep indexing.
    mocker.patch(
        "app.task_completion._maybe_spawn_sweep_aggregate_followon",
        new=AsyncMock(return_value=None),
    )
    sweep_id = f"sweep-{uuid.uuid4().hex[:12]}"
    project = Project(name="SweepTest", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    async def _make_arm(experiment_name: str) -> Task:
        arm = Task(
            project_id=project.id,
            title=f"compute_experiment: arm {experiment_name}",
            status=TaskStatus.COMPLETED,
            payloads=[
                {
                    "type": "compute_experiment",
                    "base_branch": "main",
                    "sweep_id": sweep_id,
                    "experiment": experiment_name,
                    "success_criterion": {
                        "metric": "accuracy",
                        "operator": ">=",
                        "threshold": 0.9,
                    },
                }
            ],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        )
        db_session.add(arm)
        await db_session.commit()
        await db_session.refresh(arm)
        return arm

    arm_a = await _make_arm("baseline")
    arm_b = await _make_arm("optimized")

    # Complete both arms — each should produce one KnowledgeDocument.
    for arm, accuracy in [(arm_a, 0.93), (arm_b, 0.88)]:
        await _handle_compute_experiment_completion(
            arm, _result_artifact(accuracy=accuracy), db_session
        )

    await db_session.refresh(arm_a)
    await db_session.refresh(arm_b)
    assert arm_a.status == TaskStatus.COMPLETED
    assert arm_b.status == TaskStatus.COMPLETED

    # Each arm must have exactly one KnowledgeDocument with sweep_id + arm_label set.
    docs_a = await _knowledge_docs_for(db_session, arm_a)
    docs_b = await _knowledge_docs_for(db_session, arm_b)
    assert len(docs_a) == 1, "arm_a must produce one KnowledgeDocument"
    assert len(docs_b) == 1, "arm_b must produce one KnowledgeDocument"

    doc_a = docs_a[0]
    doc_b = docs_b[0]
    assert doc_a.sweep_id == sweep_id, "doc_a.sweep_id must be set"
    assert doc_b.sweep_id == sweep_id, "doc_b.sweep_id must be set"
    assert doc_a.arm_label == "baseline", "doc_a.arm_label must be 'baseline'"
    assert doc_b.arm_label == "optimized", "doc_b.arm_label must be 'optimized'"
    assert doc_a.source_kind == "compute_experiment"
    assert doc_b.source_kind == "compute_experiment"

    # build_sweep_knowledge_index returns both arm docs for the sweep.
    sweep_entries = await build_sweep_knowledge_index(db_session, project.id, sweep_id)
    assert len(sweep_entries) == 2, (
        f"build_sweep_knowledge_index must return 2 entries for sweep {sweep_id!r}; "
        f"got {len(sweep_entries)}"
    )
    entry_labels = {e["arm_label"] for e in sweep_entries}
    assert entry_labels == {"baseline", "optimized"}, (
        f"sweep entries must have arm_labels baseline and optimized; got {entry_labels}"
    )
    for e in sweep_entries:
        assert e["sweep_id"] == sweep_id
        assert e["source_kind"] == "compute_experiment"

    # build_compute_sweep_groups groups this sweep for the strategist.
    groups = await build_compute_sweep_groups(db_session, project.id)
    assert sweep_id in groups, (
        f"build_compute_sweep_groups must include sweep {sweep_id!r}; got keys {list(groups.keys())}"
    )
    group_entries = groups[sweep_id]
    assert len(group_entries) == 2
    group_labels = {e["arm_label"] for e in group_entries}
    assert group_labels == {"baseline", "optimized"}


# ── Multi-arm criterion evaluation from arms block ────────────────────────────


@pytest.mark.asyncio
async def test_multi_arm_criterion_evaluated_from_arms_block(
    db_session, _quiet_compute_io
):
    """A multi-arm result (dict-arms form) with accuracy >= 0.9: the evaluator
    picks the max arm (0.93), reports measurable=True, met=True, and the task
    COMPLETES without compute_unmeasured_criterion."""
    task = await _make_compute_task_with_criterion(db_session, "accuracy", ">=", 0.9)

    multi_arm_result = _result_artifact(
        **{"arms": {"A": {"accuracy": 0.93}, "B": {"accuracy": 0.88}}}
    )
    await _handle_compute_experiment_completion(task, multi_arm_result, db_session)

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    assert task.context.get("failure_category") != "compute_unmeasured_criterion"
    ctx_criterion = task.context["compute_success_criterion"]
    assert ctx_criterion["measurable"] is True
    assert ctx_criterion["met"] is True
    assert ctx_criterion["value"] == pytest.approx(0.93)
    assert ctx_criterion.get("best_arm") == "A"


@pytest.mark.asyncio
async def test_multi_arm_criterion_unmet_completes_as_evidence(
    db_session, _quiet_compute_io
):
    """A multi-arm result where no arm meets the threshold: measurable=True,
    met=False, task COMPLETES as honest evidence (no loud failure)."""
    task = await _make_compute_task_with_criterion(db_session, "accuracy", ">=", 0.9)

    multi_arm_result = _result_artifact(
        **{"arms": {"A": {"accuracy": 0.85}, "B": {"accuracy": 0.82}}}
    )
    await _handle_compute_experiment_completion(task, multi_arm_result, db_session)

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    assert task.context.get("failure_category") != "compute_unmeasured_criterion"
    ctx_criterion = task.context["compute_success_criterion"]
    assert ctx_criterion["measurable"] is True
    assert ctx_criterion["met"] is False
    assert ctx_criterion["value"] == pytest.approx(0.85)


@pytest.mark.asyncio
async def test_multi_arm_criterion_list_arms_form_evaluates_correctly(
    db_session, _quiet_compute_io
):
    """A list-arms form result (arms as a list of dicts with 'name' field) is
    evaluated correctly — the evaluator finds the metric and compares it."""
    task = await _make_compute_task_with_criterion(db_session, "accuracy", ">=", 0.9)

    list_arms_result = _result_artifact(
        **{"arms": [{"name": "baseline", "accuracy": 0.93}]}
    )
    await _handle_compute_experiment_completion(task, list_arms_result, db_session)

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    ctx_criterion = task.context["compute_success_criterion"]
    assert ctx_criterion["measurable"] is True
    assert ctx_criterion["met"] is True
    assert ctx_criterion["value"] == pytest.approx(0.93)


@pytest.mark.asyncio
async def test_multi_arm_metric_absent_from_all_arms_still_fires_unmeasured(
    db_session, _quiet_compute_io
):
    """When all arms lack the declared metric, compute_unmeasured_criterion still
    fires — the new arms fallback must NOT bypass the guard."""
    task = await _make_compute_task_with_criterion(db_session, "accuracy", ">=", 0.9)

    # Arms present but metric key absent from all of them.
    no_metric_result = _result_artifact(
        **{"arms": {"A": {"loss": 0.4}, "B": {"loss": 0.3}}}
    )
    await _handle_compute_experiment_completion(task, no_metric_result, db_session)

    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED
    assert task.context["failure_category"] == "compute_unmeasured_criterion"
    ctx_criterion = task.context["compute_success_criterion"]
    assert ctx_criterion["measurable"] is False
    assert ctx_criterion["met"] is False
    assert ctx_criterion["value"] is None


@pytest.mark.asyncio
async def test_per_arm_cost_sums_to_total(db_session, _quiet_compute_io):
    """Per-arm cost apportionment: two arms with elapsed_seconds sum cost correctly.

    When total_cost=0.40, arm1 has elapsed_seconds=30, arm2 has elapsed_seconds=90,
    verify that arm1.cost_usd ≈ 0.10 and arm2.cost_usd ≈ 0.30, and sum ≈ 0.40.
    """
    from app.compute_jobs import build_compute_result_logs

    task = await _make_compute_task(db_session)
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
    job.status = ComputeJobStatus.SUCCEEDED
    job.outcome = ComputeJobOutcome.SUCCESS
    job.accrued_cost_usd = 0.40
    job.final_cost_usd = 0.40
    job.terminal_reason = "compute job finished"
    job.completed_at = _utcnow()
    job.polling_metadata = {
        **(job.polling_metadata or {}),
        "result_metrics": {
            "arms": {
                "arm_1": {"elapsed_seconds": 30, "accuracy": 0.92},
                "arm_2": {"elapsed_seconds": 90, "accuracy": 0.88},
            },
        },
    }
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    logs = build_compute_result_logs(job)
    result = json.loads(logs)

    assert result["final_cost_usd"] == pytest.approx(0.40)
    assert "arms" in result
    arms = result["arms"]
    assert "arm_1" in arms
    assert "arm_2" in arms

    arm_1_cost = arms["arm_1"]["cost_usd"]
    arm_2_cost = arms["arm_2"]["cost_usd"]

    assert arm_1_cost == pytest.approx(0.10)
    assert arm_2_cost == pytest.approx(0.30)
    assert arm_1_cost + arm_2_cost == pytest.approx(0.40)


def test_is_zero_cost_backend_runner_subprocess():
    """RUNNER_SUBPROCESS_BACKEND_NAME is a zero-cost backend."""
    from app.compute_backends import (
        is_zero_cost_backend,
        RUNNER_SUBPROCESS_BACKEND_NAME,
    )

    assert is_zero_cost_backend(RUNNER_SUBPROCESS_BACKEND_NAME) is True


@pytest.mark.asyncio
async def test_per_arm_cost_zero_elapsed_no_raise(db_session, _quiet_compute_io):
    """Per-arm cost injection: zero or missing elapsed_seconds gracefully skips injection.

    When arms have no elapsed_seconds (or all are 0), the function must not raise
    and must return valid JSON without injecting cost_usd keys.
    """
    from app.compute_jobs import build_compute_result_logs

    task = await _make_compute_task(db_session)
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
    job.status = ComputeJobStatus.SUCCEEDED
    job.outcome = ComputeJobOutcome.SUCCESS
    job.accrued_cost_usd = 0.20
    job.final_cost_usd = 0.20
    job.terminal_reason = "compute job finished"
    job.completed_at = _utcnow()
    job.polling_metadata = {
        **(job.polling_metadata or {}),
        "result_metrics": {
            "arms": {
                "arm_a": {"elapsed_seconds": 0, "accuracy": 0.95},
                "arm_b": {"accuracy": 0.92},  # no elapsed_seconds
            },
        },
    }
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    logs = build_compute_result_logs(job)
    result = json.loads(logs)

    assert result["final_cost_usd"] == pytest.approx(0.20)
    assert "arms" in result
    arms = result["arms"]
    assert "arm_a" in arms
    assert "arm_b" in arms

    # cost_usd should not be injected when elapsed_seconds is 0 or missing
    assert "cost_usd" not in arms["arm_a"]
    assert "cost_usd" not in arms["arm_b"]

    # Arms data should be intact
    assert arms["arm_a"]["accuracy"] == pytest.approx(0.95)
    assert arms["arm_b"]["accuracy"] == pytest.approx(0.92)


# ── Sweep aggregate best-effort finalization past deadline ────────────────────


@pytest.mark.asyncio
async def test_sweep_aggregate_best_effort_when_arm_wedged_past_deadline(
    db_session, _quiet_compute_io, mocker
):
    """When a sweep arm is wedged (non-terminal) but the best-effort deadline has
    elapsed, _maybe_spawn_sweep_aggregate_followon must finalize on partial results
    rather than deferring indefinitely.

    Acceptance: the aggregate follow-on is spawned (not None) in failure/diagnose
    mode, and its prompt references the wedged arm's status so the agent knows one
    arm never finished.
    """
    from app.task_completion import (
        _ACT_ON_VERDICT_SWEEP_KEY,
        _maybe_spawn_sweep_aggregate_followon,
    )

    sweep_id = f"sweep-{uuid.uuid4().hex[:12]}"

    project = Project(name="WedgedSweepDeadlineTest", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Arm A: terminally completed with a result.
    terminal_arm = Task(
        project_id=project.id,
        title="compute_experiment: arm-A-done",
        status=TaskStatus.COMPLETED,
        payloads=[
            {
                "type": "compute_experiment",
                "base_branch": "main",
                "sweep_id": sweep_id,
                "experiment": "arm-A-done",
            }
        ],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    terminal_arm.plan_output = {
        "compute_result": {"outcome": "SUCCESS", "final_cost_usd": 0.25}
    }
    db_session.add(terminal_arm)

    # Arm B: QUEUED — simulates a backend that became permanently unreachable
    # before transitioning to a terminal state (the "wedged" arm).
    wedged_arm = Task(
        project_id=project.id,
        title="compute_experiment: arm-B-wedged",
        status=TaskStatus.QUEUED,
        payloads=[
            {
                "type": "compute_experiment",
                "base_branch": "main",
                "sweep_id": sweep_id,
                "experiment": "arm-B-wedged",
            }
        ],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(wedged_arm)
    await db_session.commit()
    await db_session.refresh(terminal_arm)
    await db_session.refresh(wedged_arm)

    # Simulate the deadline having elapsed: patch _utcnow to return a time well
    # past 24 h after the oldest arm's created_at so the deadline branch fires.
    future_time = terminal_arm.created_at + timedelta(hours=25)
    mocker.patch("app.task_completion._utcnow", return_value=future_time)

    result = await _maybe_spawn_sweep_aggregate_followon(
        db_session,
        terminal_arm,
        sweep_id,
        {},
        "",
        failure_mode=False,
    )
    await db_session.commit()

    # Must have spawned despite the wedged arm — not deferred.
    assert result is not None, (
        "Expected aggregate follow-on to be spawned past deadline, got None"
    )
    # Wedged arm counts as failure → diagnose-and-fix title.
    assert "diagnose" in result.title.lower(), (
        f"Expected diagnose-and-fix title for wedged sweep; got: {result.title!r}"
    )
    # Context carries the sweep key for idempotency.
    assert (result.context or {}).get(_ACT_ON_VERDICT_SWEEP_KEY) == sweep_id
    # Prompt must reference the wedged arm so the agent is aware results are partial.
    prompt = (result.first_payload or {}).get("prompt", "")
    assert "arm-B-wedged" in prompt or "QUEUED" in prompt, (
        f"Aggregate prompt must reference the wedged arm; got: {prompt[:300]!r}"
    )
