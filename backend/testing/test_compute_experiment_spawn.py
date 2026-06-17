"""Unit tests for the autonomous compute_experiment spawn path.

These cover the pure pieces of the research-finding -> compute_experiment
launch wiring in ``research_completion.py``:

  * launch-contract authoring from flat or nested finding fields,
  * loud validation (non-empty command, positive spend cap, positive deadline),
  * the spend-threshold resolution (global default + per-direction override),
  * the finding_type -> compute_experiment routing set, and
  * the runtime-config knob (default + DB-sync whitelist).

The full DB-backed spawn loop is exercised by the sibling test task; here we
pin the contract-validation and threshold-gate logic that decides whether a
launch is dispatched, deferred, or held for operator approval.
"""

import json
import uuid
from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app import compute_jobs
from app.artifact_prerequisites import ARTIFACT_PREREQUISITES_KEY
from app.models import (
    ComputeJobOutcome,
    ComputeJobStatus,
    Project,
    Task,
    TaskHistory,
    TaskStatus,
)
from app.research_completion import (
    COMPUTE_EXPERIMENT_FINDING_TYPES,
    _author_compute_launch_contract,
    _resolve_compute_spend_threshold,
    _validate_compute_launch_contract,
)
from app.task_capabilities import STRATEGIST_SPAWNABLE_TASK_TYPES
from app.task_completion import _handle_compute_experiment_completion


# --- finding_type routing -------------------------------------------------


def test_compute_experiment_finding_types_membership():
    assert "compute_experiment" in COMPUTE_EXPERIMENT_FINDING_TYPES
    assert "measurement" in COMPUTE_EXPERIMENT_FINDING_TYPES
    assert "benchmark" in COMPUTE_EXPERIMENT_FINDING_TYPES
    # Not a measurement intent — must not route to compute_experiment.
    assert "create" not in COMPUTE_EXPERIMENT_FINDING_TYPES
    assert "research" not in COMPUTE_EXPERIMENT_FINDING_TYPES


# --- contract authoring ---------------------------------------------------


def test_author_contract_flat_fields():
    finding = {
        "type": "benchmark",
        "command": "python bench.py",
        "spend_cap_usd": 4.0,
        "deadline_seconds": 600,
        "success_criterion": {"metric": "tps", "operator": ">=", "threshold": 100},
        "machine_type": "n2-standard-4",
        "experiment": "throughput",
    }
    contract = _author_compute_launch_contract(finding)
    assert contract["command"] == "python bench.py"
    assert contract["spend_cap_usd"] == 4.0
    assert contract["deadline_seconds"] == 600
    assert contract["success_criterion"]["metric"] == "tps"
    assert contract["machine_type"] == "n2-standard-4"
    assert contract["experiment"] == "throughput"


def test_author_contract_nested_overrides_flat():
    # Nested launch spec wins over the flat field of the same name.
    finding = {
        "command": "flat-cmd",
        "compute_experiment": {
            "command": "nested-cmd",
            "spend_cap_usd": 2.5,
            "deadline_seconds": 300,
        },
    }
    contract = _author_compute_launch_contract(finding)
    assert contract["command"] == "nested-cmd"
    assert contract["spend_cap_usd"] == 2.5
    assert contract["deadline_seconds"] == 300


def test_author_contract_omits_absent_optionals():
    finding = {"command": "go test", "spend_cap_usd": 1.0, "deadline_seconds": 120}
    contract = _author_compute_launch_contract(finding)
    assert "success_criterion" not in contract
    assert "machine_type" not in contract
    assert "experiment" not in contract


def test_author_contract_tolerates_non_dict_nested():
    finding = {
        "compute_experiment": "not-a-dict",
        "command": "run",
        "spend_cap_usd": 1.0,
        "deadline_seconds": 60,
    }
    contract = _author_compute_launch_contract(finding)
    assert contract["command"] == "run"


def test_author_contract_forwards_accelerator():
    # GPU accelerator and provisioning_model are forwarded when flat on finding
    finding = {
        "command": "python train.py",
        "spend_cap_usd": 5.0,
        "deadline_seconds": 900,
        "machine_type": "g2-standard-4",
        "accelerator": "T4",
    }
    contract = _author_compute_launch_contract(finding)
    assert contract["accelerator"] == "T4"
    assert contract["machine_type"] == "g2-standard-4"


def test_author_contract_forwards_provisioning_model():
    # provisioning_model is forwarded when present
    finding = {
        "command": "python eval.py",
        "spend_cap_usd": 3.0,
        "deadline_seconds": 600,
        "accelerator": "L4",
        "provisioning_model": "on_demand",
    }
    contract = _author_compute_launch_contract(finding)
    assert contract["accelerator"] == "L4"
    assert contract["provisioning_model"] == "on_demand"


def test_author_contract_forwards_accelerator_nested():
    # Nested accelerator overrides flat, both forwarded through contract
    finding = {
        "command": "flat-cmd",
        "accelerator": "T4",
        "compute_experiment": {
            "command": "nested-cmd",
            "spend_cap_usd": 2.5,
            "deadline_seconds": 300,
            "accelerator": "V100",
            "provisioning_model": "spot",
        },
    }
    contract = _author_compute_launch_contract(finding)
    assert contract["accelerator"] == "V100"
    assert contract["provisioning_model"] == "spot"


def test_author_contract_omits_absent_gpu_fields():
    # When GPU fields are absent, they are not included in the contract
    finding = {
        "command": "python test.py",
        "spend_cap_usd": 1.0,
        "deadline_seconds": 120,
    }
    contract = _author_compute_launch_contract(finding)
    assert "accelerator" not in contract
    assert "provisioning_model" not in contract


# --- contract validation --------------------------------------------------


def test_validate_contract_accepts_complete():
    ok, reason = _validate_compute_launch_contract(
        {"command": "run", "spend_cap_usd": 3.0, "deadline_seconds": 300}
    )
    assert ok is True
    assert reason == ""


@pytest.mark.parametrize(
    "contract, needle",
    [
        ({"command": "", "spend_cap_usd": 1.0, "deadline_seconds": 60}, "command"),
        ({"command": "   ", "spend_cap_usd": 1.0, "deadline_seconds": 60}, "command"),
        ({"spend_cap_usd": 1.0, "deadline_seconds": 60}, "command"),
        ({"command": "run", "spend_cap_usd": 0, "deadline_seconds": 60}, "spend_cap"),
        ({"command": "run", "spend_cap_usd": -2, "deadline_seconds": 60}, "spend_cap"),
        ({"command": "run", "spend_cap_usd": "x", "deadline_seconds": 60}, "spend_cap"),
        ({"command": "run", "spend_cap_usd": 1.0, "deadline_seconds": 0}, "deadline"),
        ({"command": "run", "spend_cap_usd": 1.0, "deadline_seconds": -5}, "deadline"),
        (
            {"command": "run", "spend_cap_usd": 1.0, "deadline_seconds": None},
            "deadline",
        ),
    ],
)
def test_validate_contract_rejects_malformed(contract, needle):
    ok, reason = _validate_compute_launch_contract(contract)
    assert ok is False
    assert needle in reason


# --- threshold resolution -------------------------------------------------


def test_resolve_threshold_global_default():
    rc = {"compute_experiment_spend_threshold_usd": 10.0}
    assert _resolve_compute_spend_threshold(rc, None) == 10.0


def test_resolve_threshold_direction_override_wins():
    rc = {"compute_experiment_spend_threshold_usd": 10.0}
    direction = {"compute_experiment_spend_threshold_usd": 50.0}
    assert _resolve_compute_spend_threshold(rc, direction) == 50.0


def test_resolve_threshold_ignores_nonpositive_override():
    rc = {"compute_experiment_spend_threshold_usd": 10.0}
    # Non-positive / non-numeric overrides fall back to the global default.
    assert (
        _resolve_compute_spend_threshold(
            rc, {"compute_experiment_spend_threshold_usd": 0}
        )
        == 10.0
    )
    assert (
        _resolve_compute_spend_threshold(
            rc, {"compute_experiment_spend_threshold_usd": "x"}
        )
        == 10.0
    )
    assert _resolve_compute_spend_threshold(rc, {}) == 10.0


def test_resolve_threshold_missing_global_is_zero():
    assert _resolve_compute_spend_threshold({}, None) == 0.0


# --- runtime-config knob --------------------------------------------------


def test_runtime_config_default_present():
    from app.runtime_config_state import runtime_config

    val = runtime_config.get("compute_experiment_spend_threshold_usd")
    assert isinstance(val, (int, float))
    assert val > 0  # sane positive default so small experiments dispatch


@pytest.mark.asyncio
async def test_sync_whitelist_applies_persisted_threshold():
    from types import SimpleNamespace

    from app import runtime_config_state as rcs
    from app.models import DeploymentStatus

    prev = rcs.runtime_config.get("compute_experiment_spend_threshold_usd")
    try:
        state = SimpleNamespace(
            status=DeploymentStatus.STABLE,
            runtime_max_concurrent_vms=None,
            runtime_max_retries=None,
            runtime_max_autonomous_tasks_per_hour=None,
            runtime_max_global_autonomous_tasks_per_hour=None,
            runtime_autonomous_research_interval_minutes=None,
            runtime_autonomous_direction_cooldown_minutes=None,
            runtime_config_json={"compute_experiment_spend_threshold_usd": 42.5},
        )
        changed = await rcs.sync_runtime_config_from_state(state)
        assert "compute_experiment_spend_threshold_usd" in changed
        assert rcs.runtime_config["compute_experiment_spend_threshold_usd"] == 42.5
    finally:
        if prev is not None:
            rcs.runtime_config["compute_experiment_spend_threshold_usd"] = prev


# --- act_on_verdict reason-and-ship follow-on (axis B) --------------------
#
# These DB-backed tests pin the ORCHESTRATOR completion->spawn-create behaviour
# the ``act_on_verdict`` payload flag gates. The follow-on reuses the
# security_audit completion->spawn pattern: one ``type=create`` child,
# created_by=ORCHESTRATOR, carrying the experiment hypothesis + both arms in its
# prompt and (when a durable COMPUTE_RESULT artifact exists) an
# artifact_prerequisites pointer in its context. The strategist allowlist is NOT
# touched — the ORCHESTRATOR does the spawn, exactly like security_audit.


@pytest.fixture
def _quiet_compute_io(mocker):
    mocker.patch(
        "app.task_completion.broadcast_to_project", new=AsyncMock(return_value=None)
    )


async def _make_compute_task(db_session, payload: dict) -> Task:
    project = Project(name="ActOnVerdict", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="compute_experiment: act-on-verdict",
        status=TaskStatus.IN_PROGRESS,  # agent reported terminal; handler decides
        payloads=[payload],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


def _result_with_arms(**overrides) -> str:
    """A faithful result record carrying both A/B arms with real numbers."""
    base = {
        "job_id": "job-abc",
        "outcome": "SUCCESS",
        "status": "SUCCEEDED",
        "final_cost_usd": 0.42,
        "accrued_cost_usd": 0.42,
        "intentional_terminal": True,
        "arm_a": {"win_rate": 0.514},
        "arm_b": {"win_rate": 0.627},
    }
    base.update(overrides)
    return json.dumps(base)


async def _succeeded_job(db_session, task, *, result_reference: str | None = None):
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
    job.accrued_cost_usd = 0.42
    job.final_cost_usd = 0.42
    job.terminal_reason = "compute job finished"
    job.completed_at = datetime.utcnow()
    db_session.add(job)
    await db_session.commit()
    return job


@pytest.mark.asyncio
async def test_act_on_verdict_spawns_write_capable_followon(
    db_session, _quiet_compute_io
):
    """act_on_verdict=true → exactly one type=create child, created_by=ORCHESTRATOR,
    carrying the hypothesis + both arms in its prompt and (since a durable
    gs:// artifact exists) an artifact_prerequisites pointer in its context."""
    task = await _make_compute_task(
        db_session,
        {
            "type": "compute_experiment",
            "base_branch": "main",
            "experiment": "mcts-rollout-depth",
            "hypothesis": "deeper rollouts beat the shallow baseline",
            "metric": "win_rate",
            "act_on_verdict": True,
            "acceptance_evidence": {
                "kind": "downstream_artifact",
                "artifact_id": "experiment-result-bundle",
            },
        },
    )
    gs_uri = "gs://blueprint-agent-artifacts/compute-jobs/result.json"
    job = await _succeeded_job(db_session, task, result_reference=gs_uri)

    await _handle_compute_experiment_completion(
        task,
        _result_with_arms(job_id=str(job.id), result_reference=gs_uri),
        db_session,
    )

    await db_session.refresh(task)
    # Faithful capture (axis A) still runs.
    assert task.plan_output["compute_result"]["outcome"] == "SUCCESS"
    # Parent advances to EVALUATING to await the follow-on.
    assert task.status == TaskStatus.EVALUATING

    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    assert len(children) == 1
    child = children[0]
    assert child.first_payload["type"] == "create"
    assert child.created_by == "ORCHESTRATOR"
    assert child.status == TaskStatus.QUEUED
    assert child.requires_approval is False

    prompt = child.first_payload["prompt"]
    # Original hypothesis + both arms' real numbers are present.
    assert "deeper rollouts beat the shallow baseline" in prompt
    assert "0.514" in prompt
    assert "0.627" in prompt
    assert "mcts-rollout-depth" in prompt

    # Pillar 5 spawn-forward: durable artifact pointer wired into child context.
    await db_session.refresh(job)
    prereqs = (child.context or {}).get(ARTIFACT_PREREQUISITES_KEY)
    assert prereqs and len(prereqs) == 1
    assert prereqs[0]["artifact_id"] == str(job.artifact_id)
    assert prereqs[0]["storage_uri"] == gs_uri
    assert prereqs[0]["source_task_id"] == str(task.id)

    # A SPAWNED history row records the orchestrator origin.
    history = (
        (
            await db_session.execute(
                select(TaskHistory).where(TaskHistory.task_id == child.id)
            )
        )
        .scalars()
        .all()
    )
    assert any(
        h.action == "SPAWNED" and h.details.get("spawned_by") == "ACT_ON_VERDICT"
        for h in history
    )


@pytest.mark.asyncio
async def test_act_on_verdict_absent_spawns_no_followon_but_captures(
    db_session, _quiet_compute_io
):
    """act_on_verdict absent/false → NO child spawned, parent COMPLETED, and the
    faithful capture (axis A) is still recorded on plan_output."""
    task = await _make_compute_task(
        db_session,
        {
            "type": "compute_experiment",
            "base_branch": "main",
            "experiment": "mcts-rollout-depth",
            "hypothesis": "deeper rollouts beat the shallow baseline",
        },
    )

    await _handle_compute_experiment_completion(task, _result_with_arms(), db_session)

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    # Axis A capture still happens.
    assert task.plan_output["compute_result"]["outcome"] == "SUCCESS"
    assert task.plan_output["compute_result"]["final_cost_usd"] == pytest.approx(0.42)

    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    assert children == []


@pytest.mark.asyncio
async def test_act_on_verdict_spawn_is_idempotent_on_replay(
    db_session, _quiet_compute_io
):
    """A second completion dispatch must not spawn a duplicate follow-on."""
    task = await _make_compute_task(
        db_session,
        {
            "type": "compute_experiment",
            "base_branch": "main",
            "experiment": "mcts-rollout-depth",
            "hypothesis": "deeper rollouts beat the shallow baseline",
            "act_on_verdict": True,
        },
    )

    await _handle_compute_experiment_completion(task, _result_with_arms(), db_session)
    await _handle_compute_experiment_completion(task, _result_with_arms(), db_session)

    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    assert len(children) == 1


def test_strategist_allowlist_excludes_create_unchanged():
    """The act_on_verdict spawn does NOT widen the strategist allowlist: the
    strategist still cannot spawn create (the ORCHESTRATOR does the spawn)."""
    assert "create" not in STRATEGIST_SPAWNABLE_TASK_TYPES
    assert "refactor" not in STRATEGIST_SPAWNABLE_TASK_TYPES
    assert "fix" not in STRATEGIST_SPAWNABLE_TASK_TYPES
