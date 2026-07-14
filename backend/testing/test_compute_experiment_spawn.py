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
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app import compute_jobs
from app.artifact_prerequisites import ARTIFACT_PREREQUISITES_KEY
from app.auth import create_agent_token
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


def test_author_contract_collects_input_artifact_job_id():
    predecessor = str(uuid.uuid4())
    finding = {
        "command": "resume.py",
        "spend_cap_usd": 1.0,
        "deadline_seconds": 300,
        "input_artifact_job_id": predecessor,
    }
    contract = _author_compute_launch_contract(finding)
    assert contract["input_artifact_job_id"] == predecessor


def test_validate_contract_accepts_valid_input_artifact_job_id():
    ok, reason = _validate_compute_launch_contract(
        {
            "command": "run",
            "spend_cap_usd": 1.0,
            "deadline_seconds": 300,
            "input_artifact_job_id": str(uuid.uuid4()),
        },
        require_smoke=False,
    )
    assert ok is True
    assert reason == ""


def test_validate_contract_rejects_malformed_input_artifact_job_id():
    ok, reason = _validate_compute_launch_contract(
        {
            "command": "run",
            "spend_cap_usd": 1.0,
            "deadline_seconds": 300,
            "input_artifact_job_id": "not-a-uuid",
        },
        require_smoke=False,
    )
    assert ok is False
    assert "input_artifact_job_id" in reason


def test_compute_experiment_payload_retains_input_artifact_job_id():
    """The validated compute_experiment payload must keep input_artifact_job_id
    so the orchestrator dispatch consumer can rehydrate the predecessor bundle.

    Regression guard: the field was previously absent from the schema, so the
    contract-authored/orchestrator resume reference was silently dropped at
    validation before it could reach create_compute_job.
    """
    from app.payload_schemas import ComputeExperimentPayload

    predecessor = str(uuid.uuid4())
    payload = ComputeExperimentPayload.model_validate(
        {
            "command": "resume.py",
            "spend_cap_usd": 1.0,
            "deadline_seconds": 300,
            "input_artifact_job_id": predecessor,
        }
    )
    assert payload.input_artifact_job_id == predecessor
    assert payload.model_dump().get("input_artifact_job_id") == predecessor


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


def test_author_contract_forwards_sweep_id():
    # sweep_id is forwarded when present so the completion handler can route the
    # result through the sweep aggregate path instead of the standalone verdict path.
    finding = {
        "command": "python sweep.py",
        "spend_cap_usd": 2.0,
        "deadline_seconds": 300,
        "sweep_id": "sid-x",
    }
    contract = _author_compute_launch_contract(finding)
    assert contract["sweep_id"] == "sid-x"


def test_author_contract_omits_absent_sweep_id():
    finding = {"command": "python run.py", "spend_cap_usd": 1.0, "deadline_seconds": 60}
    contract = _author_compute_launch_contract(finding)
    assert "sweep_id" not in contract


def test_author_contract_forwards_setup_step():
    # setup_step is forwarded when present (flat) so the platform can run it
    # on the same substrate BEFORE the main command.
    finding = {
        "command": "python eval.py",
        "spend_cap_usd": 2.0,
        "deadline_seconds": 300,
        "setup_step": "pip install -r requirements.txt",
    }
    contract = _author_compute_launch_contract(finding)
    assert contract["setup_step"] == "pip install -r requirements.txt"


def test_author_contract_omits_absent_setup_step():
    # When setup_step is absent, it must not appear in the contract — callers
    # that check for its presence rely on absence meaning "no setup needed".
    finding = {"command": "python run.py", "spend_cap_usd": 1.0, "deadline_seconds": 60}
    contract = _author_compute_launch_contract(finding)
    assert "setup_step" not in contract


# --- contract validation --------------------------------------------------


_VALID_SMOKE = {
    "exit_code": 0,
    "startup_ok": True,
    "metric_captured": True,
    "timed_out": False,
}


def test_validate_contract_accepts_complete():
    ok, reason = _validate_compute_launch_contract(
        {
            "command": "run",
            "spend_cap_usd": 3.0,
            "deadline_seconds": 300,
            "smoke_sample": _VALID_SMOKE,
        }
    )
    assert ok is True
    assert reason == ""


def test_validate_contract_accepts_without_smoke_when_require_smoke_false():
    # Operator-triggered launches skip the smoke requirement.
    ok, reason = _validate_compute_launch_contract(
        {"command": "run", "spend_cap_usd": 3.0, "deadline_seconds": 300},
        require_smoke=False,
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
    # Basic field rejections come before smoke validation, so require_smoke=False
    # keeps the fixture minimal while testing the field under scrutiny.
    ok, reason = _validate_compute_launch_contract(contract, require_smoke=False)
    assert ok is False
    assert needle in reason


# --- runner_target (operator/agent runner pin) -----------------------------


def test_author_contract_forwards_runner_target():
    # Agent authoring paths pass the pin through flat or nested, like the
    # other optional launch fields.
    runner_id = "0b6c9c1e-8f39-4b53-9a3e-1a2b3c4d5e6f"
    flat = {
        "command": "run",
        "spend_cap_usd": 1.0,
        "deadline_seconds": 60,
        "runner_target": {"runner_id": runner_id, "strict": True},
    }
    assert _author_compute_launch_contract(flat)["runner_target"] == {
        "runner_id": runner_id,
        "strict": True,
    }
    nested = {
        "compute_experiment": {
            "command": "run",
            "spend_cap_usd": 1.0,
            "deadline_seconds": 60,
            "runner_target": {"runner_id": runner_id},
        }
    }
    assert _author_compute_launch_contract(nested)["runner_target"] == {
        "runner_id": runner_id
    }


def test_validate_contract_accepts_runner_target():
    ok, reason = _validate_compute_launch_contract(
        {
            "command": "run",
            "spend_cap_usd": 1.0,
            "deadline_seconds": 60,
            "runner_target": {
                "runner_id": "0b6c9c1e-8f39-4b53-9a3e-1a2b3c4d5e6f",
                "strict": True,
            },
        },
        require_smoke=False,
    )
    assert ok is True
    assert reason == ""


@pytest.mark.parametrize(
    "runner_target, needle",
    [
        ("not-a-dict", "runner_target must be an object"),
        ({}, "runner_target.runner_id is required"),
        ({"runner_id": ""}, "runner_target.runner_id is required"),
        ({"runner_id": 42}, "runner_target.runner_id is required"),
        ({"runner_id": "not-a-uuid"}, "not a valid UUID"),
        (
            {
                "runner_id": "0b6c9c1e-8f39-4b53-9a3e-1a2b3c4d5e6f",
                "strict": "yes",
            },
            "runner_target.strict must be a boolean",
        ),
    ],
)
def test_validate_contract_rejects_malformed_runner_target(runner_target, needle):
    ok, reason = _validate_compute_launch_contract(
        {
            "command": "run",
            "spend_cap_usd": 1.0,
            "deadline_seconds": 60,
            "runner_target": runner_target,
        },
        require_smoke=False,
    )
    assert ok is False
    assert needle in reason


# --- env validation ---------------------------------------------------------
# Env keys are interpolated BARE into `export {key}=...` in the GCP startup
# script, which runs without `set -e` — a non-identifier key would error and
# the script would silently continue, billing a run missing that env var.
# The contract validator must therefore fail loud at authoring time.


def test_validate_contract_accepts_valid_env_keys():
    ok, reason = _validate_compute_launch_contract(
        {
            "command": "run",
            "spend_cap_usd": 3.0,
            "deadline_seconds": 300,
            "env": {"MY_SEED": "42", "_PRIVATE": "x", "DATASET2": "/data"},
        },
        require_smoke=False,
    )
    assert ok is True
    assert reason == ""


@pytest.mark.parametrize(
    "bad_key",
    [
        "my-model",
        "1LEADING",
        "A B",
        "PATH=X",
        "KEY;rm -rf /",
        "$(whoami)",
        "",
        # Trailing newline: `re.match(r"^\w+$", "GOOD\n")` SUCCEEDS (Python's `$`
        # also matches just before a final newline), so a `.match()` gate lets this
        # through and the script emits `export GOOD\n=v` — bash runs `export GOOD`
        # (empty) then `=v` (command not found) and, with no `set -e`, bills the run
        # anyway. `bash -n` passes it too, so the shell harness cannot catch it.
        # Only `.fullmatch()` rejects it. This case is the regression lock.
        "GOOD\n",
        "GOOD\nEVIL=1",
        "\nGOOD",
    ],
)
def test_validate_contract_rejects_env_keys_that_are_not_shell_identifiers(bad_key):
    ok, reason = _validate_compute_launch_contract(
        {
            "command": "run",
            "spend_cap_usd": 3.0,
            "deadline_seconds": 300,
            "env": {bad_key: "value"},
        },
        require_smoke=False,
    )
    assert ok is False
    assert "env key" in reason
    assert "shell identifier" in reason


def test_validate_contract_rejects_non_string_env_key():
    ok, reason = _validate_compute_launch_contract(
        {
            "command": "run",
            "spend_cap_usd": 3.0,
            "deadline_seconds": 300,
            "env": {42: "value"},
        },
        require_smoke=False,
    )
    assert ok is False
    assert "env key" in reason


def test_validate_contract_rejects_non_dict_env():
    ok, reason = _validate_compute_launch_contract(
        {
            "command": "run",
            "spend_cap_usd": 3.0,
            "deadline_seconds": 300,
            "env": ["MY_SEED=42"],
        },
        require_smoke=False,
    )
    assert ok is False
    assert "env must be an object" in reason


# --- smoke_sample validation ---------------------------------------------


def _base_contract(**extra):
    """Minimal valid base contract; caller overrides smoke_sample via **extra."""
    return {
        "command": "python bench.py",
        "spend_cap_usd": 2.0,
        "deadline_seconds": 120,
        **extra,
    }


class TestValidateContractSmokeAdvisory:
    """Author smoke_sample failures are ADVISORY, not blocking — v20 design decision.

    ``_validate_compute_launch_contract`` deliberately demotes every author-attested
    smoke failure from a hard rejection to a logged warning.  The binding gate is the
    platform-executed pre-flight smoke (capability ``platform-preflight-smoke``) which
    runs the actual command on the real compute substrate before any VM is billed.
    Author smoke runs cheaply on the orchestrator host and cannot account for GPU
    hardware, private container registries, or custom base images — so a failed or
    absent author smoke must NEVER block launch.

    These tests pin that intentional demotion.  If any test in this class starts
    asserting ``ok is False``, that is a regression: the platform smoke gate is the
    correct enforcement point, not the author contract validator.
    """

    def test_accepts_without_author_smoke_sample(self):
        # No smoke_sample at all: platform smoke will gate at launch.
        ok, reason = _validate_compute_launch_contract(_base_contract())
        assert ok is True
        assert reason == ""

    def test_advisory_non_dict_smoke_sample(self):
        # A malformed (non-dict) smoke_sample is advisory — contract still accepted.
        ok, reason = _validate_compute_launch_contract(_base_contract(smoke_sample="yes"))
        assert ok is True
        assert reason == ""

    def test_advisory_failed_smoke_startup(self):
        # Startup failure in author smoke_sample is advisory — accepted.
        smoke = {
            "exit_code": 1,
            "startup_ok": False,
            "metric_captured": False,
            "error_excerpt": "ModuleNotFoundError: No module named 'backend'",
        }
        ok, reason = _validate_compute_launch_contract(_base_contract(smoke_sample=smoke))
        assert ok is True
        assert reason == ""

    def test_advisory_unsmokable(self):
        # Unsmokable author attestation (e.g. GPU-only) is advisory — accepted.
        smoke = {
            "unsmokable": True,
            "unsmokable_reason": "requires T4 GPU not available on this VM",
        }
        ok, reason = _validate_compute_launch_contract(_base_contract(smoke_sample=smoke))
        assert ok is True
        assert reason == ""

    def test_advisory_metric_not_captured_when_criterion_declared(self):
        # Author smoke that did not capture the declared metric is advisory.
        smoke = {
            "exit_code": 0,
            "startup_ok": True,
            "metric_captured": False,
            "timed_out": False,
        }
        contract = _base_contract(
            smoke_sample=smoke,
            success_criterion={"metric": "accuracy", "operator": ">=", "threshold": 0.9},
        )
        ok, reason = _validate_compute_launch_contract(contract)
        assert ok is True
        assert reason == ""


def test_validate_contract_accepts_valid_smoke_no_criterion():
    # No success_criterion: startup_ok=True is sufficient even if metric not captured.
    smoke = {
        "exit_code": 0,
        "startup_ok": True,
        "metric_captured": False,
        "timed_out": False,
    }
    ok, reason = _validate_compute_launch_contract(_base_contract(smoke_sample=smoke))
    assert ok is True
    assert reason == ""


def test_validate_contract_accepts_timed_out_with_startup_ok():
    # A timeout (exit_code=124) is acceptable when startup succeeded.
    smoke = {
        "exit_code": 124,
        "startup_ok": True,
        "metric_captured": True,
        "timed_out": True,
    }
    ok, reason = _validate_compute_launch_contract(_base_contract(smoke_sample=smoke))
    assert ok is True
    assert reason == ""


def test_author_contract_passes_through_smoke_sample():
    # smoke_sample from the finding is carried into the contract for validation.
    finding = {
        "command": "python run.py",
        "spend_cap_usd": 1.0,
        "deadline_seconds": 60,
        "smoke_sample": _VALID_SMOKE,
    }
    contract = _author_compute_launch_contract(finding)
    assert contract["smoke_sample"] == _VALID_SMOKE


def test_author_contract_passes_through_nested_smoke_sample():
    # smoke_sample nested under compute_experiment is also collected.
    finding = {
        "compute_experiment": {
            "command": "python run.py",
            "spend_cap_usd": 1.0,
            "deadline_seconds": 60,
            "smoke_sample": _VALID_SMOKE,
        },
    }
    contract = _author_compute_launch_contract(finding)
    assert contract["smoke_sample"] == _VALID_SMOKE


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


def test_act_on_verdict_followon_cap_default_present():
    from app.runtime_config_state import runtime_config

    val = runtime_config.get("max_concurrent_act_on_verdict_followons")
    assert isinstance(val, int)
    assert val == 4  # sane positive default


@pytest.mark.asyncio
async def test_sync_whitelist_applies_act_on_verdict_followon_cap():
    from types import SimpleNamespace

    from app import runtime_config_state as rcs
    from app.models import DeploymentStatus

    prev = rcs.runtime_config.get("max_concurrent_act_on_verdict_followons")
    try:
        state = SimpleNamespace(
            status=DeploymentStatus.STABLE,
            runtime_max_concurrent_vms=None,
            runtime_max_retries=None,
            runtime_max_autonomous_tasks_per_hour=None,
            runtime_max_global_autonomous_tasks_per_hour=None,
            runtime_autonomous_research_interval_minutes=None,
            runtime_autonomous_direction_cooldown_minutes=None,
            runtime_config_json={"max_concurrent_act_on_verdict_followons": 9},
        )
        changed = await rcs.sync_runtime_config_from_state(state)
        assert "max_concurrent_act_on_verdict_followons" in changed
        assert rcs.runtime_config["max_concurrent_act_on_verdict_followons"] == 9
    finally:
        if prev is not None:
            rcs.runtime_config["max_concurrent_act_on_verdict_followons"] = prev


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
        # Engine-authored: the spawn_followup gate (#14223) suppresses
        # follow-ons for created_by="USER" tasks, which is not what these
        # autonomous-spawn tests exercise.
        created_by="ORCHESTRATOR",
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
    job.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db_session.add(job)
    await db_session.commit()
    return job


@pytest.mark.asyncio
async def test_act_on_verdict_spawns_write_capable_followon(
    db_session, _quiet_compute_io
):
    """Always-dispose: a successful experiment spawns exactly one type=create
    child, created_by=ORCHESTRATOR, carrying the hypothesis + both arms in its
    prompt and (since a durable gs:// artifact exists) an artifact_prerequisites
    pointer in its context. No authoring-time opt-in is required."""
    task = await _make_compute_task(
        db_session,
        {
            "type": "compute_experiment",
            "base_branch": "main",
            "experiment": "mcts-rollout-depth",
            "hypothesis": "deeper rollouts beat the shallow baseline",
            "metric": "win_rate",
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
async def test_success_always_spawns_followon_without_opt_in(
    db_session, _quiet_compute_io
):
    """Always-dispose (opt-in retired): a successful experiment with NO
    act_on_verdict flag now ALWAYS spawns exactly one disposition follow-on, and
    the parent advances to EVALUATING to await it. Faithful capture (axis A) is
    still recorded on plan_output."""
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
    # Parent advances to EVALUATING to await the always-spawned follow-on.
    assert task.status == TaskStatus.EVALUATING
    # Axis A capture still happens.
    assert task.plan_output["compute_result"]["outcome"] == "SUCCESS"
    assert task.plan_output["compute_result"]["final_cost_usd"] == pytest.approx(0.42)

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
    # Success framing: reason-and-ship, not diagnose-and-fix.
    assert child.title.startswith("act-on-verdict:")


@pytest.mark.asyncio
async def test_dedup_guard_blocks_double_spawn_on_replay(db_session, _quiet_compute_io):
    """A second completion dispatch must not spawn a duplicate follow-on — the
    ``_ACT_ON_VERDICT_SOURCE_KEY`` replay guard holds under always-dispose."""
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
    await _handle_compute_experiment_completion(task, _result_with_arms(), db_session)

    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    assert len(children) == 1


@pytest.mark.asyncio
async def test_failure_path_spawns_diagnose_followon(db_session, _quiet_compute_io):
    """A compute experiment that fails the artifact-delivery gate (its output is an
    unparseable crash traceback with no outcome/cost) spawns a tracked QUEUED
    diagnose-and-fix child; the child prompt names 'diagnose' and the originating
    experiment id; the experiment itself stays FAILED."""
    task = await _make_compute_task(
        db_session,
        {
            "type": "compute_experiment",
            "base_branch": "main",
            "experiment": "mcts-rollout-depth",
            "hypothesis": "deeper rollouts beat the shallow baseline",
        },
    )

    crash_logs = (
        "Traceback (most recent call last):\n"
        '  File "experiment.py", line 12, in <module>\n'
        "    run()\n"
        "RuntimeError: CUDA out of memory\n"
    )
    await _handle_compute_experiment_completion(task, crash_logs, db_session)

    await db_session.refresh(task)
    # The experiment is a tombstone-free FAILED — it is NOT flipped to EVALUATING.
    assert task.status == TaskStatus.FAILED

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
    assert child.title.startswith("diagnose-and-fix:")

    prompt = child.first_payload["prompt"]
    assert "diagnose" in prompt.lower()
    assert str(task.id) in prompt

    # SPAWNED history records the orchestrator origin.
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
async def test_failure_followon_prompt_contains_traceback(
    db_session, _quiet_compute_io
):
    """The captured stderr/traceback tail is embedded prominently in the failure
    follow-on's prompt — the key crash-visibility fix."""
    task = await _make_compute_task(
        db_session,
        {
            "type": "compute_experiment",
            "base_branch": "main",
            "experiment": "mcts-rollout-depth",
        },
    )

    crash_logs = (
        "Traceback (most recent call last):\n"
        '  File "train.py", line 5, in <module>\n'
        "ValueError: unique-traceback-marker-abc123\n"
    )
    await _handle_compute_experiment_completion(task, crash_logs, db_session)

    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    assert len(children) == 1
    prompt = children[0].first_payload["prompt"]
    assert "unique-traceback-marker-abc123" in prompt
    assert "Traceback" in prompt


# --- terminal-reason propagation into the follow-on ------------------------
#
# The COMPUTE_RESULT record carries the job's ``terminal_reason`` and the
# readable ``command_log_text``; the follow-on must receive WHY the job ended
# (deadline kill, spend cap, provisioning timeout, runner failure) — not just
# that it FAILED — so iteration agents stop diagnosing blind.


def _failed_result_record(**overrides) -> str:
    """A terminal COMPUTE_RESULT record as build_compute_result_logs emits it."""
    base = {
        "job_id": str(uuid.uuid4()),
        "outcome": "FAILURE",
        "status": "FAILED",
        "final_cost_usd": 0.18,
        "accrued_cost_usd": 0.18,
        "intentional_terminal": False,
        "terminal_reason": "command exited with code 1",
        "command_log_text": (
            "Traceback (most recent call last):\n"
            '  File "train.py", line 9, in <module>\n'
            "RuntimeError: readable-log-marker-xyz789\n"
        ),
        "command_log_truncated": False,
    }
    base.update(overrides)
    return json.dumps(base)


def test_failure_prompt_surfaces_terminal_reason_and_readable_log_tail():
    """failure_mode prompt: the terminal_reason line and the UNESCAPED
    command_log_text tail extracted from the terminal record — plus the terminal
    metadata — even when the caller passes compute_result={} (the gate-fail
    spawn)."""
    from app.task_completion import _build_act_on_verdict_prompt

    task = Task(
        project_id=uuid.uuid4(),
        title="compute_experiment: reason-surface",
        payloads=[
            {
                "type": "compute_experiment",
                "base_branch": "main",
                "experiment": "reason-surface",
            }
        ],
    )
    logs = _failed_result_record(
        terminal_reason="compute job exceeded runtime deadline"
    )
    prompt = _build_act_on_verdict_prompt(task, {}, logs, failure_mode=True)

    # WHY, stated explicitly — not inferred from a log tail.
    assert "Terminal reason" in prompt
    assert "compute job exceeded runtime deadline" in prompt
    # The log tail arrives readable (real newlines, not a JSON-escaped blob):
    # the multi-line traceback appears verbatim as separate prompt lines.
    assert '  File "train.py", line 9, in <module>' in prompt.splitlines()
    assert "RuntimeError: readable-log-marker-xyz789" in prompt.splitlines()
    # Terminal metadata survives a compute_result={} spawn (gate-fail branch).
    assert '"outcome": "FAILURE"' in prompt


def test_failure_prompt_falls_back_to_raw_crash_logs():
    """Unparseable logs (raw crash traceback, legacy output) keep the existing
    behaviour: the raw tail is shown and no reason line is fabricated."""
    from app.task_completion import _build_act_on_verdict_prompt

    task = Task(
        project_id=uuid.uuid4(),
        title="compute_experiment: raw-crash",
        payloads=[{"type": "compute_experiment", "base_branch": "main"}],
    )
    crash = (
        "Traceback (most recent call last):\n"
        '  File "train.py", line 5, in <module>\n'
        "ValueError: raw-crash-marker-456\n"
    )
    prompt = _build_act_on_verdict_prompt(task, {}, crash, failure_mode=True)

    assert "raw-crash-marker-456" in prompt
    assert "Terminal reason" not in prompt


@pytest.mark.asyncio
async def test_gate_fail_followon_context_carries_terminal_reason(
    db_session, _quiet_compute_io
):
    """A record with a terminal_reason but NO measured outcome fails the gate;
    the diagnose-and-fix child must still receive the reason — machine-readable
    in context AND stated in its prompt."""
    task = await _make_compute_task(
        db_session,
        {
            "type": "compute_experiment",
            "base_branch": "main",
            "experiment": "provisioning-timeout-surface",
        },
    )

    logs = json.dumps(
        {
            "job_id": str(uuid.uuid4()),
            "status": "TIMED_OUT",
            "terminal_reason": "compute job timed out while provisioning",
            "command_log_text": "",
        }
    )
    await _handle_compute_experiment_completion(task, logs, db_session)

    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED

    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    assert len(children) == 1
    child = children[0]
    assert (
        child.context["compute_terminal_reason"]
        == "compute job timed out while provisioning"
    )
    assert "compute job timed out while provisioning" in child.first_payload["prompt"]


@pytest.mark.asyncio
async def test_measured_failure_propagates_terminal_reason_to_result_and_child(
    db_session, _quiet_compute_io
):
    """A measured non-success outcome stamps terminal_reason into the durable
    plan_output.compute_result and forwards it into the follow-on's context."""
    task = await _make_compute_task(
        db_session,
        {
            "type": "compute_experiment",
            "base_branch": "main",
            "experiment": "measured-failure-reason",
        },
    )
    # Engine-authored: the success-path follow-on spawns by default (the
    # spawn_followup gate suppresses operator-authored USER tasks).
    task.created_by = "ORCHESTRATOR"
    db_session.add(task)
    await db_session.commit()

    await _handle_compute_experiment_completion(
        task, _failed_result_record(), db_session
    )

    await db_session.refresh(task)
    compute_result = task.plan_output["compute_result"]
    assert compute_result["outcome"] == "FAILURE"
    assert compute_result["terminal_reason"] == "command exited with code 1"

    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    assert len(children) == 1
    assert (
        children[0].context["compute_terminal_reason"] == "command exited with code 1"
    )


@pytest.mark.asyncio
async def test_compute_unmeasured_criterion_spawns_followon(
    db_session, _quiet_compute_io
):
    """Crash-visibility gap: outcome=SUCCESS but the declared kind=metric criterion
    produced no numeric value → compute_unmeasured_criterion fires AND a tracked
    diagnose-and-fix follow-on is spawned; the experiment stays FAILED."""
    task = await _make_compute_task(
        db_session,
        {
            "type": "compute_experiment",
            "base_branch": "main",
            "experiment": "mcts-rollout-depth",
            "success_criterion": {
                "kind": "metric",
                "metric": "accuracy",
                "operator": ">=",
                "threshold": 0.9,
            },
        },
    )

    # outcome=SUCCESS, both arms present, but no top-level 'accuracy' value.
    await _handle_compute_experiment_completion(task, _result_with_arms(), db_session)

    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED
    assert task.context["failure_category"] == "compute_unmeasured_criterion"

    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    assert len(children) == 1
    assert children[0].title.startswith("diagnose-and-fix:")


@pytest.mark.asyncio
async def test_unmeasured_criterion_surfaces_emitted_keys(
    db_session, _quiet_compute_io
):
    """compute_unmeasured_criterion failure message surfaces what the command
    actually emitted so a case/name mismatch is instantly visible.

    Case A (positive — locks in case-insensitive resolution): COMPUTE_RESULT
            emits 'AUC' (uppercase) and criterion declares 'auc' (lowercase).
            _find_metric_case_insensitive resolves the match, so the criterion IS
            measured and met (0.85 >= 0.8). The task must NOT fail with
            compute_unmeasured_criterion; it advances to EVALUATING (follow-on
            spawned) with compute_success_criterion showing met=True,
            measurable=True, value=0.85.
    Case A': genuine name mismatch — criterion declares 'auc' but the experiment
             emits 'roc_auc' (different name, not just different case). No
             case-insensitive match is found, so the criterion is unmeasured and
             the task FAILS with failure_category == 'compute_unmeasured_criterion'.
             The evaluation_gate reason must surface the declared metric and the
             emitted_keys with a name/case-mismatch hint.
    Case B: COMPUTE_RESULT has no experiment metrics at all (only cost fields)
            → miss_reason reports emitted_keys=[].
    Case C: no COMPUTE_RESULT parsed (crash before result) — exercised separately
            by test_unmeasured_criterion_no_artifact_is_crash_path.
    """
    # --- Case A: case-insensitive resolution — 'AUC' resolves declared 'auc' ---
    task_a = await _make_compute_task(
        db_session,
        {
            "type": "compute_experiment",
            "base_branch": "main",
            "experiment": "auc-experiment",
            "success_criterion": {
                "kind": "metric",
                "metric": "auc",
                "operator": ">=",
                "threshold": 0.8,
            },
        },
    )
    result_with_upper_case = json.dumps(
        {
            "outcome": "SUCCESS",
            "final_cost_usd": 0.10,
            "AUC": 0.85,  # emitted uppercase; resolves declared lowercase 'auc'
        }
    )
    await _handle_compute_experiment_completion(
        task_a, result_with_upper_case, db_session
    )
    await db_session.refresh(task_a)
    # Case-insensitive match: criterion IS measured and met — must NOT fail.
    assert task_a.status != TaskStatus.FAILED
    assert task_a.status in (TaskStatus.EVALUATING, TaskStatus.COMPLETED)
    assert "failure_category" not in (task_a.context or {})
    crit_a = (task_a.context or {}).get("compute_success_criterion", {})
    assert crit_a.get("met") is True
    assert crit_a.get("measurable") is True
    assert crit_a.get("value") == pytest.approx(0.85)

    # --- Case A': genuine name mismatch — 'roc_auc' does not match 'auc' ---
    task_a2 = await _make_compute_task(
        db_session,
        {
            "type": "compute_experiment",
            "base_branch": "main",
            "experiment": "auc-name-mismatch-experiment",
            "success_criterion": {
                "kind": "metric",
                "metric": "auc",
                "operator": ">=",
                "threshold": 0.8,
            },
        },
    )
    result_name_mismatch = json.dumps(
        {
            "outcome": "SUCCESS",
            "final_cost_usd": 0.10,
            "roc_auc": 0.85,  # different name — no case-insensitive match for 'auc'
        }
    )
    await _handle_compute_experiment_completion(
        task_a2, result_name_mismatch, db_session
    )
    await db_session.refresh(task_a2)
    assert task_a2.status == TaskStatus.FAILED
    assert task_a2.context["failure_category"] == "compute_unmeasured_criterion"
    gate_reason_a2 = task_a2.context["evaluation_gate"]["reason"]
    # Must name the declared metric, the emitted keys, and hint at name/case mismatch.
    assert "auc" in gate_reason_a2
    assert "roc_auc" in gate_reason_a2
    assert "emitted_keys" in gate_reason_a2
    assert "case" in gate_reason_a2.lower() or "name" in gate_reason_a2.lower()

    # --- Case B: no experiment metrics in result at all ---
    task_b = await _make_compute_task(
        db_session,
        {
            "type": "compute_experiment",
            "base_branch": "main",
            "experiment": "empty-metrics-experiment",
            "success_criterion": {
                "kind": "metric",
                "metric": "accuracy",
                "operator": ">=",
                "threshold": 0.9,
            },
        },
    )
    result_no_metrics = json.dumps(
        {
            "outcome": "SUCCESS",
            "final_cost_usd": 0.05,
            # intentionally no 'accuracy' or any other experiment metric key
        }
    )
    await _handle_compute_experiment_completion(
        task_b, result_no_metrics, db_session
    )
    await db_session.refresh(task_b)
    assert task_b.status == TaskStatus.FAILED
    assert task_b.context["failure_category"] == "compute_unmeasured_criterion"
    gate_reason_b = task_b.context["evaluation_gate"]["reason"]
    assert "accuracy" in gate_reason_b
    assert "emitted_keys=[]" in gate_reason_b


@pytest.mark.asyncio
@pytest.mark.parametrize("kill_outcome", ["TIMED_OUT", "SPEND_CAP_EXCEEDED"])
async def test_intentional_kill_spawns_no_followon(
    db_session, _quiet_compute_io, kill_outcome
):
    """An intentional kill (budget/deadline) is an honest terminal outcome with no
    result to dispose — it spawns NO follow-on and completes clean."""
    task = await _make_compute_task(
        db_session,
        {
            "type": "compute_experiment",
            "base_branch": "main",
            "experiment": "mcts-rollout-depth",
        },
    )

    await _handle_compute_experiment_completion(
        task, _result_with_arms(outcome=kill_outcome), db_session
    )

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    assert task.plan_output["compute_result"]["outcome"] == kill_outcome

    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    assert children == []


@pytest.mark.asyncio
async def test_concurrent_cap_suppresses_excess_spawn(db_session, _quiet_compute_io):
    """When max_concurrent_act_on_verdict_followons=1 and one follow-on is already
    QUEUED for the project, a second experiment's completion spawns no child (the
    dedup key is NOT stamped, so a later re-dispatch can retry)."""
    from app.runtime_config_state import runtime_config

    project = Project(name="CapTest", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    async def _exp(experiment: str) -> Task:
        t = Task(
            project_id=project.id,
            title=f"compute_experiment: {experiment}",
            status=TaskStatus.IN_PROGRESS,
            payloads=[
                {
                    "type": "compute_experiment",
                    "base_branch": "main",
                    "experiment": experiment,
                }
            ],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
            # Engine-authored: the spawn_followup gate (#14223) suppresses
            # follow-ons for created_by="USER" tasks; this test exercises the
            # CONCURRENCY cap, so the spawn must not be USER-suppressed.
            created_by="ORCHESTRATOR",
        )
        db_session.add(t)
        await db_session.commit()
        await db_session.refresh(t)
        return t

    prev = runtime_config.get("max_concurrent_act_on_verdict_followons")
    runtime_config["max_concurrent_act_on_verdict_followons"] = 1
    try:
        first = await _exp("first")
        await _handle_compute_experiment_completion(
            first, _result_with_arms(), db_session
        )
        # First experiment spawns one QUEUED follow-on (now 1 in-flight).
        first_children = (
            (
                await db_session.execute(
                    select(Task).where(Task.parent_task_id == first.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(first_children) == 1
        assert first_children[0].status == TaskStatus.QUEUED

        # Second experiment: cap reached → no spawn → completes clean.
        second = await _exp("second")
        await _handle_compute_experiment_completion(
            second, _result_with_arms(), db_session
        )
        await db_session.refresh(second)
        assert second.status == TaskStatus.COMPLETED

        second_children = (
            (
                await db_session.execute(
                    select(Task).where(Task.parent_task_id == second.id)
                )
            )
            .scalars()
            .all()
        )
        assert second_children == []
    finally:
        if prev is not None:
            runtime_config["max_concurrent_act_on_verdict_followons"] = prev
        else:
            runtime_config.pop("max_concurrent_act_on_verdict_followons", None)


def test_strategist_allowlist_excludes_create_unchanged():
    """The act_on_verdict spawn does NOT widen the strategist allowlist: the
    strategist still cannot spawn create (the ORCHESTRATOR does the spawn)."""
    assert "create" not in STRATEGIST_SPAWNABLE_TASK_TYPES
    assert "refactor" not in STRATEGIST_SPAWNABLE_TASK_TYPES
    assert "fix" not in STRATEGIST_SPAWNABLE_TASK_TYPES


# ---------------------------------------------------------------------------
# Sweep-aware disposition (O(1) follow-ons for N-arm experiments)
# ---------------------------------------------------------------------------


async def _make_sweep_arm(
    db_session,
    project,
    sweep_id: str,
    experiment: str,
    *,
    status=TaskStatus.IN_PROGRESS,
) -> Task:
    """Create a compute_experiment task as one arm of a sweep."""
    task = Task(
        project_id=project.id,
        title=f"compute_experiment: {experiment}",
        status=status,
        payloads=[
            {
                "type": "compute_experiment",
                "base_branch": "main",
                "experiment": experiment,
                "hypothesis": f"hypothesis for {experiment}",
                "sweep_id": sweep_id,
            }
        ],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        # Engine-authored: the spawn_followup gate (#14223) suppresses
        # follow-ons for created_by="USER" tasks; sweep tests exercise the
        # sweep-deferral/aggregate mechanics, not USER suppression.
        created_by="ORCHESTRATOR",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


@pytest.mark.asyncio
async def test_sweep_arm_suppresses_per_arm_disposition_while_siblings_pending(
    db_session, _quiet_compute_io
):
    """First sweep arm completes while a sibling is still IN_PROGRESS — no
    aggregate follow-on should spawn yet."""
    project = Project(name="SweepTest", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    sweep_id = "sweep-" + uuid.uuid4().hex[:8]
    arm1 = await _make_sweep_arm(db_session, project, sweep_id, "arm-1")
    # arm2 is still IN_PROGRESS (not yet terminal)
    _arm2 = await _make_sweep_arm(
        db_session, project, sweep_id, "arm-2", status=TaskStatus.IN_PROGRESS
    )

    await _handle_compute_experiment_completion(arm1, _result_with_arms(), db_session)

    await db_session.refresh(arm1)
    # arm1 completes (no per-arm follow-on since it's part of a sweep with pending siblings)
    assert arm1.status == TaskStatus.COMPLETED

    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == arm1.id)))
        .scalars()
        .all()
    )
    assert children == [], "no aggregate follow-on while a sibling is still pending"


@pytest.mark.asyncio
async def test_sweep_last_arm_spawns_aggregate_followon(db_session, _quiet_compute_io):
    """When the last arm completes and all siblings are terminal, exactly ONE
    aggregate follow-on is spawned; its prompt covers all arms' results."""
    project = Project(name="SweepAgg", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    sweep_id = "sweep-" + uuid.uuid4().hex[:8]
    arm1 = await _make_sweep_arm(db_session, project, sweep_id, "arm-1")
    arm2 = await _make_sweep_arm(db_session, project, sweep_id, "arm-2")

    # Complete arm1 first (arm2 still IN_PROGRESS → deferred)
    await _handle_compute_experiment_completion(arm1, _result_with_arms(), db_session)
    await db_session.refresh(arm1)

    # No follow-on yet — arm2 still pending
    arm1_children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == arm1.id)))
        .scalars()
        .all()
    )
    assert arm1_children == []

    # Complete arm2 — now all arms are terminal → aggregate spawns
    await _handle_compute_experiment_completion(arm2, _result_with_arms(), db_session)
    await db_session.refresh(arm2)
    assert arm2.status == TaskStatus.EVALUATING

    agg_children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == arm2.id)))
        .scalars()
        .all()
    )
    assert len(agg_children) == 1
    agg = agg_children[0]
    assert agg.first_payload["type"] == "create"
    assert agg.created_by == "ORCHESTRATOR"
    assert agg.status == TaskStatus.QUEUED

    prompt = agg.first_payload["prompt"]
    # Prompt must reference the sweep and both arms
    assert sweep_id in prompt
    assert "arm-1" in prompt
    assert "arm-2" in prompt

    # Sweep key in context identifies it as an aggregate
    from app.task_completion import _ACT_ON_VERDICT_SWEEP_KEY

    assert (agg.context or {}).get(_ACT_ON_VERDICT_SWEEP_KEY) == sweep_id

    # SPAWNED history row records the sweep origin
    from sqlalchemy import select as sa_select

    history = (
        (
            await db_session.execute(
                sa_select(TaskHistory).where(TaskHistory.task_id == agg.id)
            )
        )
        .scalars()
        .all()
    )
    assert any(
        h.action == "SPAWNED" and h.details.get("spawned_by") == "ACT_ON_VERDICT_SWEEP"
        for h in history
    )


@pytest.mark.asyncio
async def test_sweep_aggregate_spawn_is_idempotent(db_session, _quiet_compute_io):
    """Two arms completing 'simultaneously' (double-dispatch of the last arm)
    must spawn only ONE aggregate follow-on."""
    project = Project(name="SweepDedup", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    sweep_id = "sweep-" + uuid.uuid4().hex[:8]
    arm1 = await _make_sweep_arm(db_session, project, sweep_id, "arm-1")
    arm2 = await _make_sweep_arm(db_session, project, sweep_id, "arm-2")

    # arm1 completes first (deferred — arm2 pending)
    await _handle_compute_experiment_completion(arm1, _result_with_arms(), db_session)

    # arm2 completes (aggregate spawns)
    await _handle_compute_experiment_completion(arm2, _result_with_arms(), db_session)
    # arm2 completion dispatched again (replay)
    await _handle_compute_experiment_completion(arm2, _result_with_arms(), db_session)

    # Across both arms, there should be exactly one aggregate child
    all_children = (
        (
            await db_session.execute(
                select(Task).where(Task.parent_task_id.in_([arm1.id, arm2.id]))
            )
        )
        .scalars()
        .all()
    )
    assert len(all_children) == 1


@pytest.mark.asyncio
async def test_non_sweep_experiment_unaffected(db_session, _quiet_compute_io):
    """A compute experiment with no sweep_id still gets a per-experiment
    follow-on (Phase 1+2 behavior is preserved)."""
    task = await _make_compute_task(
        db_session,
        {
            "type": "compute_experiment",
            "base_branch": "main",
            "experiment": "standalone-experiment",
            "hypothesis": "a standalone hypothesis",
        },
    )

    await _handle_compute_experiment_completion(task, _result_with_arms(), db_session)

    await db_session.refresh(task)
    assert task.status == TaskStatus.EVALUATING

    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    assert len(children) == 1
    assert children[0].first_payload["type"] == "create"
    assert children[0].title.startswith("act-on-verdict:")


@pytest.mark.asyncio
async def test_sweep_aggregate_includes_failed_arm_results(
    db_session, _quiet_compute_io
):
    """One arm FAILED (crash), others COMPLETED; aggregate still fires and
    prompt covers all arms including the failed one."""
    project = Project(name="SweepFail", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    sweep_id = "sweep-" + uuid.uuid4().hex[:8]
    arm_success = await _make_sweep_arm(db_session, project, sweep_id, "arm-success")
    arm_crash = await _make_sweep_arm(db_session, project, sweep_id, "arm-crash")

    # arm_success completes cleanly first
    await _handle_compute_experiment_completion(
        arm_success, _result_with_arms(), db_session
    )

    # arm_crash receives a crash traceback — fails the artifact-delivery gate
    crash_logs = (
        "Traceback (most recent call last):\n"
        '  File "train.py", line 5, in <module>\n'
        "RuntimeError: sweep-crash-marker-xyz\n"
    )
    await _handle_compute_experiment_completion(arm_crash, crash_logs, db_session)

    await db_session.refresh(arm_crash)
    # The crash arm itself is FAILED
    assert arm_crash.status == TaskStatus.FAILED

    # The aggregate follow-on was spawned on the crash arm (last to be processed)
    agg_children = (
        (
            await db_session.execute(
                select(Task).where(Task.parent_task_id == arm_crash.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(agg_children) == 1
    agg = agg_children[0]
    prompt = agg.first_payload["prompt"]
    # Prompt covers both arms
    assert "arm-success" in prompt
    assert "arm-crash" in prompt
    # Failure framing because one arm failed
    assert "diagnose" in prompt.lower()


# ---------------------------------------------------------------------------
# Plan-decomposed compute_experiment spend-gate
# ---------------------------------------------------------------------------


async def _setup_plan_with_compute_experiment(
    db_session, mocker, spend_cap_usd: float, threshold_usd: float = 0.0
):
    """Create project + plan task and return (task, project) ready for _handle_plan_completion."""
    from app.plan_completion import _handle_plan_completion
    from app.shared_state import runtime_config

    runtime_config["compute_experiment_spend_threshold_usd"] = threshold_usd

    project = Project(name="PlanSpendGateTest", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    plan_logs = json.dumps(
        {
            "subtasks": [
                {
                    "title": "Run the benchmark",
                    "prompt": "measure baseline",
                    "type": "compute_experiment",
                    "command": "python bench.py",
                    "spend_cap_usd": spend_cap_usd,
                    "deadline_seconds": 600,
                    "smoke_sample": _VALID_SMOKE,
                }
            ]
        }
    )

    task = Task(
        project_id=project.id,
        title="Plan: spend gate test",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "plan", "prompt": "do things", "base_branch": "main"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    mocker.patch(
        "app.task_completion._ensure_branch",
        new_callable=AsyncMock,
        return_value=task,
    )
    mocker.patch("app.plan_completion.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )

    await _handle_plan_completion(task, plan_logs, db_session)
    return task


@pytest.mark.asyncio
async def test_plan_compute_experiment_above_threshold_requires_approval(
    db_session, mocker
):
    """Plan-decomposed compute_experiment with spend_cap above threshold is held for approval."""
    task = await _setup_plan_with_compute_experiment(
        db_session, mocker, spend_cap_usd=10.0, threshold_usd=5.0
    )

    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    assert len(children) == 1
    child = children[0]
    assert child.first_payload["type"] == "compute_experiment"
    assert child.requires_approval is True


@pytest.mark.asyncio
async def test_plan_compute_experiment_below_threshold_no_approval(db_session, mocker):
    """Plan-decomposed compute_experiment with spend_cap at or below threshold launches without approval."""
    task = await _setup_plan_with_compute_experiment(
        db_session, mocker, spend_cap_usd=3.0, threshold_usd=5.0
    )

    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    assert len(children) == 1
    child = children[0]
    assert child.first_payload["type"] == "compute_experiment"
    assert child.requires_approval is False


# ---------------------------------------------------------------------------
# Agent /spawn endpoint spend-gate regression tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_spawn_compute_experiment_above_threshold_requires_approval(
    client: AsyncClient, db_session
):
    """The /api/agent/spawn endpoint must enforce the spend gate for
    compute_experiment payloads — a caller-supplied requires_approval=False
    must be overridden to True when spend_cap_usd exceeds the threshold."""
    import app.shared_state as _shared_state

    project = Project(name="AgentSpawnSpendGate", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    parent_task = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "research something"}],
        vm_instance_id="agent-vm-spawn-gate-above",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(parent_task)
    await db_session.commit()
    await db_session.refresh(parent_task)

    prev_threshold = _shared_state.runtime_config.get(
        "compute_experiment_spend_threshold_usd"
    )
    _shared_state.runtime_config["compute_experiment_spend_threshold_usd"] = 5.0
    try:
        token = create_agent_token(str(parent_task.id), "agent-vm-spawn-gate-above")
        resp = await client.post(
            "/api/agent/spawn",
            headers={"x-agent-token": token},
            json={
                "task_id": str(parent_task.id),
                "payloads": [
                    {
                        "type": "compute_experiment",
                        "title": "Above-threshold experiment",
                        "command": "python bench.py",
                        "spend_cap_usd": 10.0,
                        "deadline_seconds": 600,
                    }
                ],
                "requires_approval": False,
            },
        )
        assert resp.status_code == 200
        child_id = uuid.UUID(resp.json()["task_ids"][0])
        child = await db_session.get(Task, child_id)
        assert child is not None
        assert child.requires_approval is True
        # Verify TaskHistory SPAWNED entry records the enforced value, not the caller request
        history_rows = await db_session.execute(
            select(TaskHistory).where(
                (TaskHistory.task_id == child_id) & (TaskHistory.action == "SPAWNED")
            )
        )
        history = history_rows.scalar_one_or_none()
        assert history is not None
        assert history.details["requires_approval"] is True
    finally:
        if prev_threshold is not None:
            _shared_state.runtime_config[
                "compute_experiment_spend_threshold_usd"
            ] = prev_threshold
        else:
            _shared_state.runtime_config.pop(
                "compute_experiment_spend_threshold_usd", None
            )


@pytest.mark.asyncio
async def test_agent_spawn_compute_experiment_below_threshold_no_approval(
    client: AsyncClient, db_session
):
    """The /api/agent/spawn endpoint must NOT require approval for
    compute_experiment payloads whose spend_cap_usd is at or below threshold."""
    import app.shared_state as _shared_state

    project = Project(name="AgentSpawnSpendGateBelow", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    parent_task = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "research something"}],
        vm_instance_id="agent-vm-spawn-gate-below",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(parent_task)
    await db_session.commit()
    await db_session.refresh(parent_task)

    prev_threshold = _shared_state.runtime_config.get(
        "compute_experiment_spend_threshold_usd"
    )
    _shared_state.runtime_config["compute_experiment_spend_threshold_usd"] = 5.0
    try:
        token = create_agent_token(str(parent_task.id), "agent-vm-spawn-gate-below")
        resp = await client.post(
            "/api/agent/spawn",
            headers={"x-agent-token": token},
            json={
                "task_id": str(parent_task.id),
                "payloads": [
                    {
                        "type": "compute_experiment",
                        "title": "Below-threshold experiment",
                        "command": "python bench.py",
                        "spend_cap_usd": 3.0,
                        "deadline_seconds": 600,
                    }
                ],
                "requires_approval": False,
            },
        )
        assert resp.status_code == 200
        child_id = uuid.UUID(resp.json()["task_ids"][0])
        child = await db_session.get(Task, child_id)
        assert child is not None
        assert child.requires_approval is False
    finally:
        if prev_threshold is not None:
            _shared_state.runtime_config[
                "compute_experiment_spend_threshold_usd"
            ] = prev_threshold
        else:
            _shared_state.runtime_config.pop(
                "compute_experiment_spend_threshold_usd", None
            )


# --- Chokepoint: create_compute_job spend-gate ----------------------------


@pytest.mark.asyncio
async def test_launch_refuses_above_threshold_unapproved_compute_job(db_session):
    """Chokepoint: create_compute_job raises ComputeLaunchPreconditionFailed when
    spend_cap_usd exceeds the project threshold and spend_approval_cleared_at is None,
    regardless of the requires_approval flag on the Task.

    The approval threshold lives in the GLOBAL runtime_config
    (``compute_experiment_spend_threshold_usd``) — the same source the four
    autonomous spawn sites and the chokepoint both read, per #11678. There is no
    per-project ``Project.runtime_config_json`` column (``runtime_config_json``
    is a column on ``DeploymentState``, not ``Project``), so the threshold is set
    here via the global config with save/restore, mirroring the sibling
    test_launch_allows_zero_config_project_below_global_threshold.
    """
    from app import runtime_config_state as rcs
    from app.compute_jobs import ComputeLaunchPreconditionFailed

    prev = rcs.runtime_config.get("compute_experiment_spend_threshold_usd")
    rcs.runtime_config["compute_experiment_spend_threshold_usd"] = 5.0
    try:
        project = Project(
            name="ChokeGateRefuse",
            user_id="test-user",
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = Task(
            project_id=project.id,
            title="compute_experiment: chokepoint refuse",
            status=TaskStatus.IN_PROGRESS,
            payloads=[{"type": "compute_experiment", "command": "python bench.py"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
            requires_approval=False,
            spend_approval_cleared_at=None,
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        with pytest.raises(ComputeLaunchPreconditionFailed) as exc_info:
            await compute_jobs.create_compute_job(
                db_session,
                task,
                command="python bench.py",
                machine_type="e2-standard-2",
                spend_cap_usd=10.0,
                deadline_seconds=300,
            )

        detail = exc_info.value.detail
        assert detail["error"] == "spend_cap_requires_approval"
        assert detail["spend_cap_usd"] == 10.0
        assert detail["threshold_usd"] == 5.0
    finally:
        if prev is not None:
            rcs.runtime_config["compute_experiment_spend_threshold_usd"] = prev
        else:
            rcs.runtime_config.pop("compute_experiment_spend_threshold_usd", None)


@pytest.mark.asyncio
async def test_launch_allows_operator_approved_above_threshold_compute_job(db_session):
    """Chokepoint: create_compute_job proceeds when spend_approval_cleared_at is set,
    even if spend_cap_usd exceeds the (global) threshold.

    Threshold is set via the global runtime_config (see the refuse test) so this
    genuinely exercises the above-threshold-but-operator-approved path rather than
    relying on a no-op per-project column.
    """
    from app import runtime_config_state as rcs

    prev = rcs.runtime_config.get("compute_experiment_spend_threshold_usd")
    rcs.runtime_config["compute_experiment_spend_threshold_usd"] = 5.0
    try:
        project = Project(
            name="ChokeGateAllow",
            user_id="test-user",
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = Task(
            project_id=project.id,
            title="compute_experiment: chokepoint allow",
            status=TaskStatus.IN_PROGRESS,
            payloads=[{"type": "compute_experiment", "command": "python bench.py"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
            requires_approval=False,
            spend_approval_cleared_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        job = await compute_jobs.create_compute_job(
            db_session,
            task,
            command="python bench.py",
            machine_type="e2-standard-2",
            spend_cap_usd=12.0,
            deadline_seconds=300,
        )
        assert job is not None
    finally:
        if prev is not None:
            rcs.runtime_config["compute_experiment_spend_threshold_usd"] = prev
        else:
            rcs.runtime_config.pop("compute_experiment_spend_threshold_usd", None)


@pytest.mark.asyncio
async def test_launch_allows_zero_config_project_below_global_threshold(db_session):
    """Chokepoint reads global runtime_config: threshold comes from
    ``runtime_config_state.runtime_config``, not any per-project column (Project has no
    such column). A spend below the global threshold launches without a precondition
    failure; a spend above it is refused even for the same project."""
    from app import runtime_config_state as rcs
    from app.compute_jobs import ComputeLaunchPreconditionFailed

    prev = rcs.runtime_config.get("compute_experiment_spend_threshold_usd")
    rcs.runtime_config["compute_experiment_spend_threshold_usd"] = 10.0
    try:
        project = Project(
            name="ZeroConfigGate",
            user_id="test-user",
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task_below = Task(
            project_id=project.id,
            title="compute_experiment: zero-config below threshold",
            status=TaskStatus.IN_PROGRESS,
            payloads=[{"type": "compute_experiment", "command": "python bench.py"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
            spend_approval_cleared_at=None,
        )
        db_session.add(task_below)
        await db_session.commit()
        await db_session.refresh(task_below)

        # Below global threshold (9.0 < 10.0) — must succeed
        job = await compute_jobs.create_compute_job(
            db_session,
            task_below,
            command="python bench.py",
            machine_type="e2-standard-2",
            spend_cap_usd=9.0,
            deadline_seconds=300,
        )
        assert job is not None

        # Above global threshold (11.0 > 10.0) — must be refused
        task_above = Task(
            project_id=project.id,
            title="compute_experiment: zero-config above threshold",
            status=TaskStatus.IN_PROGRESS,
            payloads=[{"type": "compute_experiment", "command": "python bench.py"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
            spend_approval_cleared_at=None,
        )
        db_session.add(task_above)
        await db_session.commit()
        await db_session.refresh(task_above)

        with pytest.raises(ComputeLaunchPreconditionFailed) as exc_info:
            await compute_jobs.create_compute_job(
                db_session,
                task_above,
                command="python bench.py",
                machine_type="e2-standard-2",
                spend_cap_usd=11.0,
                deadline_seconds=300,
            )
        detail = exc_info.value.detail
        assert detail["error"] == "spend_cap_requires_approval"
        assert detail["threshold_usd"] == pytest.approx(10.0)
    finally:
        if prev is not None:
            rcs.runtime_config["compute_experiment_spend_threshold_usd"] = prev
        else:
            rcs.runtime_config.pop("compute_experiment_spend_threshold_usd", None)


# ---------------------------------------------------------------------------
# Strategist spawn_task spend-gate regression tests
# ---------------------------------------------------------------------------


async def _make_strategic_task(db_session) -> tuple:
    """Create a project and a strategic_review task; return (project, task, token)."""
    project = Project(name="StrategistSpawnGate", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Strategic review",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "strategic_review", "prompt": "review"}],
        vm_instance_id="agent-vm-strategic-spawn",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(
        str(task.id), "agent-vm-strategic-spawn", task_type="strategic_review"
    )
    return project, task, token


@pytest.mark.asyncio
async def test_strategist_spawn_compute_experiment_above_threshold_requires_approval(
    client: AsyncClient, db_session
):
    """Strategic spawn_task for compute_experiment with spend_cap above threshold
    must set requires_approval=True on the created task."""
    import app.shared_state as _shared_state

    project, task, token = await _make_strategic_task(db_session)
    prev = _shared_state.runtime_config.get("compute_experiment_spend_threshold_usd")
    _shared_state.runtime_config["compute_experiment_spend_threshold_usd"] = 5.0
    try:
        resp = await client.post(
            "/api/agent/strategic",
            headers={"x-agent-token": token},
            json={
                "task_id": str(task.id),
                "op": "spawn_task",
                "data": {
                    "type": "compute_experiment",
                    "title": "Strategist above-threshold experiment",
                    "prompt": "Run the benchmark to measure throughput improvement",
                    "command": "python bench.py",
                    "spend_cap_usd": 10.0,
                    "deadline_seconds": 600,
                },
            },
        )
        assert resp.status_code == 200, resp.text
        child_id = uuid.UUID(resp.json()["task_id"])
        child = await db_session.get(Task, child_id)
        assert child is not None
        assert child.requires_approval is True
    finally:
        if prev is not None:
            _shared_state.runtime_config["compute_experiment_spend_threshold_usd"] = prev
        else:
            _shared_state.runtime_config.pop(
                "compute_experiment_spend_threshold_usd", None
            )


@pytest.mark.asyncio
async def test_strategist_spawn_compute_experiment_below_threshold_no_approval(
    client: AsyncClient, db_session
):
    """Strategic spawn_task for compute_experiment with spend_cap at or below
    threshold must NOT set requires_approval."""
    import app.shared_state as _shared_state

    project, task, token = await _make_strategic_task(db_session)
    prev = _shared_state.runtime_config.get("compute_experiment_spend_threshold_usd")
    _shared_state.runtime_config["compute_experiment_spend_threshold_usd"] = 5.0
    try:
        resp = await client.post(
            "/api/agent/strategic",
            headers={"x-agent-token": token},
            json={
                "task_id": str(task.id),
                "op": "spawn_task",
                "data": {
                    "type": "compute_experiment",
                    "title": "Strategist below-threshold experiment",
                    "prompt": "Run the benchmark to measure throughput improvement",
                    "command": "python bench.py",
                    "spend_cap_usd": 3.0,
                    "deadline_seconds": 600,
                },
            },
        )
        assert resp.status_code == 200, resp.text
        child_id = uuid.UUID(resp.json()["task_id"])
        child = await db_session.get(Task, child_id)
        assert child is not None
        assert child.requires_approval is False
    finally:
        if prev is not None:
            _shared_state.runtime_config["compute_experiment_spend_threshold_usd"] = prev
        else:
            _shared_state.runtime_config.pop(
                "compute_experiment_spend_threshold_usd", None
            )


@pytest.mark.asyncio
async def test_strategist_spawn_compute_experiment_invalid_contract_rejected(
    client: AsyncClient, db_session
):
    """Strategic spawn_task for compute_experiment with a malformed contract
    (missing command) must be rejected with 400 before any task is created."""
    project, task, token = await _make_strategic_task(db_session)
    resp = await client.post(
        "/api/agent/strategic",
        headers={"x-agent-token": token},
        json={
            "task_id": str(task.id),
            "op": "spawn_task",
            "data": {
                "type": "compute_experiment",
                "title": "Invalid contract experiment",
                "prompt": "Run something",
                "command": "",
                "spend_cap_usd": 2.0,
                "deadline_seconds": 300,
            },
        },
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    error_text = (
        body.get("detail")
        or (body.get("error") or {}).get("message")
        or ""
    ).lower()
    assert "contract invalid" in error_text


# ---------------------------------------------------------------------------
# Arms + declared_metric contract tests
# ---------------------------------------------------------------------------


def test_arms_contract_authoring_flat():
    """arms and declared_metric are picked up from flat finding fields."""
    finding = {
        "arms": [{"name": "adam", "command": "python train.py --opt adam"}],
        "declared_metric": "val_loss",
        "spend_cap_usd": 3.0,
        "deadline_seconds": 600,
    }
    contract = _author_compute_launch_contract(finding)
    assert contract["arms"] == [{"name": "adam", "command": "python train.py --opt adam"}]
    assert contract["declared_metric"] == "val_loss"


def test_arms_contract_authoring_nested():
    """arms and declared_metric are picked up from nested compute_experiment dict."""
    finding = {
        "compute_experiment": {
            "arms": [
                {"name": "a", "command": "python a.py"},
                {"name": "b", "command": "python b.py"},
            ],
            "declared_metric": "accuracy",
            "spend_cap_usd": 5.0,
            "deadline_seconds": 1800,
        }
    }
    contract = _author_compute_launch_contract(finding)
    assert len(contract["arms"]) == 2
    assert contract["declared_metric"] == "accuracy"


def test_arms_contract_authoring_omits_absent_arms():
    """When no arms key is present, the contract omits it."""
    finding = {"command": "python run.py", "spend_cap_usd": 1.0, "deadline_seconds": 60}
    contract = _author_compute_launch_contract(finding)
    assert "arms" not in contract
    assert "declared_metric" not in contract


def test_arms_contract_validation_ok():
    """A well-formed two-arm contract with declared_metric passes validation."""
    contract = {
        "arms": [
            {"name": "adam", "command": "python train.py --optimizer adam --epochs 5"},
            {"name": "sgd", "command": "python train.py --optimizer sgd --epochs 5"},
        ],
        "declared_metric": "val_loss",
        "spend_cap_usd": 5.0,
        "deadline_seconds": 3600,
    }
    ok, reason = _validate_compute_launch_contract(contract, require_smoke=False)
    assert ok is True
    assert reason == ""


def test_arms_contract_validation_synthesizes_command():
    """Validation synthesizes a base64-encoded command from arms so the compute job always has one."""
    import base64
    import re

    contract = {
        "arms": [
            {"name": "a", "command": "python a.py"},
            {"name": "b", "command": "python b.py"},
        ],
        "declared_metric": "score",
        "spend_cap_usd": 2.0,
        "deadline_seconds": 600,
    }
    ok, reason = _validate_compute_launch_contract(contract, require_smoke=False)
    assert ok is True
    assert "command" in contract
    cmd = contract["command"]
    # Command must use base64 to avoid shell-quoting issues.
    assert "base64" in cmd
    m = re.search(r"echo (\S+) \| base64 -d \| bash", cmd)
    assert m is not None, f"command did not match base64-decode pattern: {cmd!r}"
    decoded = base64.b64decode(m.group(1)).decode()
    assert "python a.py" in decoded
    assert "python b.py" in decoded


def test_decode_generated_command_round_trips_the_synthesized_wrapper():
    """The decoder inverts the encoder: an operator can read what actually ran."""
    from app.compute_experiment_contract import (
        _AGGREGATOR_B64,
        decode_generated_command,
    )

    contract = {
        "arms": [
            {"name": "baseline", "command": "python train.py --lr=0.1"},
            {"name": "tuned", "command": "python train.py --lr='0.01'"},
        ],
        "declared_metric": "score",
        "spend_cap_usd": 2.0,
        "deadline_seconds": 600,
    }
    ok, _reason = _validate_compute_launch_contract(contract, require_smoke=False)
    assert ok is True

    decoded = decode_generated_command(contract["command"])
    assert decoded is not None
    # The arm commands — the part an operator actually wants to read — survive verbatim.
    assert "python train.py --lr=0.1" in decoded
    assert "python train.py --lr='0.01'" in decoded
    # The static aggregator blob is elided, not expanded, so it cannot bury the arms.
    assert "compute_arm_aggregator.py" in decoded
    assert _AGGREGATOR_B64 not in decoded


def test_decode_generated_command_never_elides_an_arms_own_base64_blob():
    """An arm may legitimately embed its own base64 script — leave it INTACT.

    `echo <blob> | base64 -d > <dest>` is the exact pattern CLAUDE.md tells
    authors to use, so eliding by SHAPE would relabel an arm's blob as the
    aggregator and destroy its contents. A legibility feature that lies about
    what ran is worse than no feature.
    """
    import base64

    from app.compute_experiment_contract import decode_generated_command

    # Two things this test must get right or it silently proves nothing:
    #  1. The blob must encode to >= 64 base64 chars, or _EMBEDDED_AGGREGATOR_RE
    #     never matches it (an earlier draft used 60 and passed WITH the bug).
    #  2. The `echo` must NOT be the arm's first line. The generator wraps line 1
    #     as `(export BLUEPRINT_RESULT_FILE=...; <cmd>`, so a leading echo is not
    #     at line-start and `^echo` cannot match it. A MIDDLE line is the
    #     reachable case.
    script_src = (
        b"import json\n"
        b"cfg = {'lr': 0.01, 'epochs': 30, 'arch': 'resnet50'}\n"
        b"json.dump(cfg, open('cfg.json', 'w'))\n"
    )
    payload = base64.b64encode(script_src).decode()
    assert len(payload) >= 64, "payload too short to reach the elision regex"
    arm_cmd = (
        "python prep.py\n"
        f"echo {payload} | base64 -d > model_cfg.py\n"
        "python train.py --cfg model_cfg.py"
    )
    contract = {
        "arms": [{"name": "embedder", "command": arm_cmd}],
        "declared_metric": "score",
        "spend_cap_usd": 2.0,
        "deadline_seconds": 600,
    }
    ok, _reason = _validate_compute_launch_contract(contract, require_smoke=False)
    assert ok is True

    decoded = decode_generated_command(contract["command"])
    assert decoded is not None
    # The arm's OWN blob survives verbatim and is not mislabelled.
    assert payload in decoded
    assert "base64 blob: compute_arm_aggregator.py> | base64 -d > model_cfg.py" not in decoded
    # ...while the real aggregator blob is still elided.
    assert "echo <base64 blob: compute_arm_aggregator.py>" in decoded


def test_decode_generated_command_passes_through_unencoded_commands():
    """Single-arm/operator commands are stored verbatim — nothing to decode."""
    from app.compute_experiment_contract import decode_generated_command

    assert decode_generated_command("python train.py --epochs 3") is None
    assert decode_generated_command(None) is None
    assert decode_generated_command("") is None


def test_decode_generated_command_never_raises_on_a_corrupt_blob():
    """A truncated blob must not turn a read of an old job into a 500."""
    import base64

    from app.compute_experiment_contract import decode_generated_command

    # Well-formed wrapper shape, invalid base64 payload.
    assert decode_generated_command("echo !!!notb64!!! | base64 -d | bash") is None
    # Valid base64 that is not UTF-8 text.
    blob = base64.b64encode(b"\xff\xfe\x00\x01").decode()
    assert decode_generated_command(f"echo {blob} | base64 -d | bash") is None


def test_arms_contract_validation_single_quote_safe():
    """Arms with single quotes in commands produce a safe base64-encoded command."""
    import base64
    import re

    contract = {
        "arms": [
            {"name": "a", "command": "python -c 'print(1)'"},
            {"name": "b", "command": "python b.py --flag='value'"},
        ],
        "declared_metric": "score",
        "spend_cap_usd": 2.0,
        "deadline_seconds": 600,
    }
    ok, reason = _validate_compute_launch_contract(contract, require_smoke=False)
    assert ok is True
    cmd = contract["command"]
    m = re.search(r"echo (\S+) \| base64 -d \| bash", cmd)
    assert m is not None, f"command did not match base64-decode pattern: {cmd!r}"
    decoded = base64.b64decode(m.group(1)).decode()
    assert "python -c 'print(1)'" in decoded
    assert "python b.py --flag='value'" in decoded


def test_arm_synthesis_isolates_per_arm_results():
    """Synthesised bash script gives each arm its own BLUEPRINT_RESULT_FILE; two
    arms emitting different declared_metric values both appear in the aggregated
    output (not last-arm-wins)."""
    import re
    import tempfile
    from pathlib import Path

    from app.compute_arm_aggregator import aggregate

    contract = {
        "arms": [
            {"name": "adam", "command": "python train.py --opt adam"},
            {"name": "sgd", "command": "python train.py --opt sgd"},
        ],
        "declared_metric": "val_loss",
        "spend_cap_usd": 5.0,
        "deadline_seconds": 3600,
    }
    ok, reason = _validate_compute_launch_contract(contract, require_smoke=False)
    assert ok is True

    cmd = contract["command"]
    m = re.search(r"echo (\S+) \| base64 -d \| bash", cmd)
    assert m is not None, f"command did not match base64-decode pattern: {cmd!r}"
    import base64 as _b64

    script = _b64.b64decode(m.group(1)).decode()

    # Each arm must export BLUEPRINT_RESULT_FILE to its own per-arm result path.
    assert "adam/result.json" in script
    assert "sgd/result.json" in script
    # The two per-arm paths must be at different positions (not sharing one file).
    assert script.index("adam/result.json") != script.index("sgd/result.json")
    # Arms run in subshells so one failure cannot abort others.
    assert "(export BLUEPRINT_RESULT_FILE=" in script
    # Per-arm stderr is captured to a file.
    assert "2>" in script
    # Aggregator receives per-arm exit-code and stderr file specs.
    assert "--exit-codes" in script
    assert "--stderr" in script

    # Aggregator produces {"arms": {name: {metric: value}}} with BOTH arms,
    # even when they emit DIFFERENT values — verifies not last-arm-wins.
    with tempfile.TemporaryDirectory() as tmpdir:
        adam_result = Path(tmpdir) / "adam_result.json"
        sgd_result = Path(tmpdir) / "sgd_result.json"
        output = Path(tmpdir) / "main_result.json"

        adam_result.write_text(json.dumps({"val_loss": 0.42}))
        sgd_result.write_text(json.dumps({"val_loss": 0.61}))

        result = aggregate(
            [("adam", str(adam_result)), ("sgd", str(sgd_result))],
            "val_loss",
            str(output),
        )

    assert result == {
        "arms": {
            "adam": {"val_loss": 0.42},
            "sgd": {"val_loss": 0.61},
        }
    }
    # Confirm neither arm overwrote the other.
    assert result["arms"]["adam"]["val_loss"] != result["arms"]["sgd"]["val_loss"]


def test_arms_contract_validation_missing_declared_metric():
    """arms without declared_metric is rejected."""
    contract = {
        "arms": [{"name": "a", "command": "python a.py"}],
        "spend_cap_usd": 2.0,
        "deadline_seconds": 600,
    }
    ok, reason = _validate_compute_launch_contract(contract, require_smoke=False)
    assert ok is False
    assert "declared_metric" in reason


def test_arms_contract_validation_empty_declared_metric():
    """declared_metric that is an empty string is rejected."""
    contract = {
        "arms": [{"name": "a", "command": "python a.py"}],
        "declared_metric": "   ",
        "spend_cap_usd": 2.0,
        "deadline_seconds": 600,
    }
    ok, reason = _validate_compute_launch_contract(contract, require_smoke=False)
    assert ok is False
    assert "declared_metric" in reason


@pytest.mark.parametrize("bad_name", ["bad name", "a;rm -rf /", "arm:1", "a/b", "GOOD\n"])
def test_arms_contract_validation_rejects_arm_name_charset(bad_name):
    """Arm names outside the safe charset are rejected — including trailing-newline.

    `ARM_NAME_RE.match("GOOD\\n")` SUCCEEDS (Python's `$` matches before a final
    newline), and the arm name is interpolated into the generated per-arm bash
    wrapper and the NAME:PATH aggregator spec. Only `.fullmatch()` rejects it.
    """
    contract = {
        "arms": [{"name": bad_name, "command": "python a.py"}],
        "declared_metric": "score",
        "spend_cap_usd": 2.0,
        "deadline_seconds": 600,
    }
    ok, reason = _validate_compute_launch_contract(contract, require_smoke=False)
    assert ok is False
    assert "invalid characters" in reason


@pytest.mark.parametrize("bad_metric", ["val loss", "$(whoami)", "a;b", "GOOD\n"])
def test_arms_contract_validation_rejects_declared_metric_charset(bad_metric):
    """declared_metric outside the safe charset is rejected — including trailing-newline.

    Same `.match()` + `$` hole as the arm-name gate: the metric key reaches the
    generated wrapper, so a newline must not survive validation.
    """
    contract = {
        "arms": [{"name": "a", "command": "python a.py"}],
        "declared_metric": bad_metric,
        "spend_cap_usd": 2.0,
        "deadline_seconds": 600,
    }
    ok, reason = _validate_compute_launch_contract(contract, require_smoke=False)
    assert ok is False
    assert "invalid characters" in reason


def test_arms_contract_validation_empty_arms_list():
    """An empty arms list is rejected."""
    contract = {
        "arms": [],
        "declared_metric": "score",
        "spend_cap_usd": 2.0,
        "deadline_seconds": 600,
    }
    ok, reason = _validate_compute_launch_contract(contract, require_smoke=False)
    assert ok is False
    assert "arms" in reason


def test_arms_contract_validation_arm_missing_command():
    """An arm with an empty command string is rejected with a descriptive error."""
    contract = {
        "arms": [
            {"name": "good", "command": "python a.py"},
            {"name": "bad", "command": ""},
        ],
        "declared_metric": "score",
        "spend_cap_usd": 2.0,
        "deadline_seconds": 600,
    }
    ok, reason = _validate_compute_launch_contract(contract, require_smoke=False)
    assert ok is False
    assert "bad" in reason
    assert "command" in reason


def test_arms_contract_validation_arm_missing_name():
    """An arm with a missing name is rejected."""
    contract = {
        "arms": [{"name": "", "command": "python a.py"}],
        "declared_metric": "score",
        "spend_cap_usd": 2.0,
        "deadline_seconds": 600,
    }
    ok, reason = _validate_compute_launch_contract(contract, require_smoke=False)
    assert ok is False
    assert "name" in reason


def test_declared_metric_non_empty_string_required_standalone():
    """declared_metric alone (no arms) must be a non-empty string if present."""
    contract = {
        "command": "python run.py",
        "declared_metric": "",
        "spend_cap_usd": 2.0,
        "deadline_seconds": 600,
    }
    ok, reason = _validate_compute_launch_contract(contract, require_smoke=False)
    assert ok is False
    assert "declared_metric" in reason


def test_arms_duplicate_name_rejected():
    """Two arms with the same name must be rejected — they would silently overwrite
    each other's result file, reintroducing the last-arm-wins data loss."""
    contract = {
        "arms": [
            {"name": "dup", "command": "python a.py"},
            {"name": "dup", "command": "python b.py"},
        ],
        "declared_metric": "score",
        "spend_cap_usd": 2.0,
        "deadline_seconds": 600,
    }
    ok, reason = _validate_compute_launch_contract(contract, require_smoke=False)
    assert ok is False
    assert "dup" in reason
    assert "duplicate" in reason.lower() or "unique" in reason.lower()


def test_arms_invalid_name_charset_rejected():
    """Arm names with shell metacharacters are rejected before interpolation into bash."""
    bad_names = [
        'a"; touch /tmp/PWNED #',
        "arm with spaces",
        "arm$var",
        "arm;cmd",
        "arm`cmd`",
        "arm:colon",
        "arm/slash",
    ]
    for bad_name in bad_names:
        contract = {
            "arms": [{"name": bad_name, "command": "python a.py"}],
            "declared_metric": "score",
            "spend_cap_usd": 2.0,
            "deadline_seconds": 600,
        }
        ok, reason = _validate_compute_launch_contract(contract, require_smoke=False)
        assert ok is False, f"expected rejection for arm name {bad_name!r}, got ok=True"
        assert "invalid characters" in reason or "name" in reason


def test_declared_metric_invalid_charset_rejected():
    """declared_metric with shell metacharacters is rejected before interpolation."""
    bad_metrics = [
        'loss"; touch /tmp/X #',
        "metric with spaces",
        "metric$var",
        "metric;cmd",
    ]
    for bad_metric in bad_metrics:
        contract = {
            "arms": [{"name": "arm-a", "command": "python a.py"}],
            "declared_metric": bad_metric,
            "spend_cap_usd": 2.0,
            "deadline_seconds": 600,
        }
        ok, reason = _validate_compute_launch_contract(contract, require_smoke=False)
        assert ok is False, f"expected rejection for declared_metric {bad_metric!r}, got ok=True"
        assert "declared_metric" in reason or "invalid characters" in reason


def test_declared_metric_valid_charsets_accepted():
    """Metric keys with common valid patterns (val/loss, top-1, accuracy) are accepted."""
    valid_metrics = ["val_loss", "val/loss", "top-1", "accuracy", "f1.score"]
    for metric in valid_metrics:
        contract = {
            "arms": [{"name": "arm-a", "command": "python a.py"}],
            "declared_metric": metric,
            "spend_cap_usd": 2.0,
            "deadline_seconds": 600,
        }
        ok, reason = _validate_compute_launch_contract(contract, require_smoke=False)
        assert ok is True, f"expected acceptance for metric {metric!r}, got reason={reason!r}"


# ---------------------------------------------------------------------------
# container_image / container_registry_auth contract-forwarding tests
# ---------------------------------------------------------------------------


def test_author_contract_picks_up_container_image_when_present():
    """_author_compute_launch_contract forwards container_image from finding."""
    finding = {
        "command": "python train.py",
        "spend_cap_usd": 1.0,
        "deadline_seconds": 300,
        "container_image": "nvcr.io/nvidia/tensorflow:25.01-tf2-py3",
    }
    contract = _author_compute_launch_contract(finding)
    assert contract["container_image"] == "nvcr.io/nvidia/tensorflow:25.01-tf2-py3"


def test_author_contract_picks_up_container_registry_auth_when_present():
    """_author_compute_launch_contract forwards container_registry_auth from finding."""
    auth = {"server": "nvcr.io", "username": "$oauthtoken", "password": "secret"}
    finding = {
        "command": "python train.py",
        "spend_cap_usd": 1.0,
        "deadline_seconds": 300,
        "container_image": "nvcr.io/nvidia/tensorflow:25.01-tf2-py3",
        "container_registry_auth": auth,
    }
    contract = _author_compute_launch_contract(finding)
    assert contract["container_registry_auth"] == auth


def test_author_contract_omits_container_fields_when_absent():
    """_author_compute_launch_contract omits container fields when not in finding."""
    finding = {"command": "python run.py", "spend_cap_usd": 1.0, "deadline_seconds": 60}
    contract = _author_compute_launch_contract(finding)
    assert "container_image" not in contract
    assert "container_registry_auth" not in contract


def test_validate_contract_accepts_valid_container_image():
    """_validate_compute_launch_contract accepts a non-empty container_image string."""
    contract = {
        "command": "python run.py",
        "spend_cap_usd": 1.0,
        "deadline_seconds": 300,
        "container_image": "nvcr.io/nvidia/tensorflow:25.01-tf2-py3",
    }
    ok, reason = _validate_compute_launch_contract(contract, require_smoke=False)
    assert ok is True, f"expected ok but got reason={reason!r}"


def test_validate_contract_rejects_empty_container_image():
    """_validate_compute_launch_contract rejects an empty string container_image."""
    contract = {
        "command": "python run.py",
        "spend_cap_usd": 1.0,
        "deadline_seconds": 300,
        "container_image": "",
    }
    ok, reason = _validate_compute_launch_contract(contract, require_smoke=False)
    assert ok is False
    assert "container_image" in reason


def test_validate_contract_rejects_whitespace_container_image():
    """_validate_compute_launch_contract rejects a whitespace-only container_image."""
    contract = {
        "command": "python run.py",
        "spend_cap_usd": 1.0,
        "deadline_seconds": 300,
        "container_image": "   ",
    }
    ok, reason = _validate_compute_launch_contract(contract, require_smoke=False)
    assert ok is False
    assert "container_image" in reason


def test_validate_contract_rejects_non_string_container_image():
    """_validate_compute_launch_contract rejects a non-string container_image."""
    contract = {
        "command": "python run.py",
        "spend_cap_usd": 1.0,
        "deadline_seconds": 300,
        "container_image": 42,
    }
    ok, reason = _validate_compute_launch_contract(contract, require_smoke=False)
    assert ok is False
    assert "container_image" in reason


def test_validate_contract_rejects_non_dict_container_registry_auth():
    """_validate_compute_launch_contract rejects a non-dict container_registry_auth."""
    contract = {
        "command": "python run.py",
        "spend_cap_usd": 1.0,
        "deadline_seconds": 300,
        "container_image": "nvcr.io/nvidia/tensorflow:25.01-tf2-py3",
        "container_registry_auth": "not-a-dict",
    }
    ok, reason = _validate_compute_launch_contract(contract, require_smoke=False)
    assert ok is False
    assert "container_registry_auth" in reason


def test_validate_contract_rejects_non_string_field_in_container_registry_auth():
    """_validate_compute_launch_contract rejects container_registry_auth with a non-string field."""
    contract = {
        "command": "python run.py",
        "spend_cap_usd": 1.0,
        "deadline_seconds": 300,
        "container_image": "nvcr.io/nvidia/tensorflow:25.01-tf2-py3",
        "container_registry_auth": {"server": "nvcr.io", "username": 123, "password": "pw"},
    }
    ok, reason = _validate_compute_launch_contract(contract, require_smoke=False)
    assert ok is False
    assert "container_registry_auth" in reason


# ---------------------------------------------------------------------------
# compute_arm_aggregator: error-branch coverage
# ---------------------------------------------------------------------------


def test_aggregator_missing_result_file_captured_as_error():
    """A missing arm result file is recorded as an error for that arm; other
    arms still appear with their real metric values (failure isolation)."""
    import tempfile
    from pathlib import Path

    from app.compute_arm_aggregator import aggregate

    with tempfile.TemporaryDirectory() as tmpdir:
        good_result = Path(tmpdir) / "good_result.json"
        output = Path(tmpdir) / "main_result.json"
        good_result.write_text(json.dumps({"score": 0.95}))

        missing_path = Path(tmpdir) / "nonexistent_result.json"

        result = aggregate(
            [("good-arm", str(good_result)), ("missing-arm", str(missing_path))],
            "score",
            str(output),
        )

    assert result["arms"]["good-arm"] == {"score": 0.95}
    assert "error" in result["arms"]["missing-arm"]
    assert "not written" in result["arms"]["missing-arm"]["error"] or "result" in result["arms"]["missing-arm"]["error"]


def test_aggregator_metric_not_in_result_captured_as_error():
    """When the declared metric is absent from an arm's result file, that arm
    records an error while other arms still capture their metric."""
    import tempfile
    from pathlib import Path

    from app.compute_arm_aggregator import aggregate

    with tempfile.TemporaryDirectory() as tmpdir:
        good_result = Path(tmpdir) / "good_result.json"
        bad_result = Path(tmpdir) / "bad_result.json"
        output = Path(tmpdir) / "main_result.json"

        good_result.write_text(json.dumps({"score": 0.88}))
        bad_result.write_text(json.dumps({"other_metric": 42}))  # missing 'score'

        result = aggregate(
            [("arm-a", str(good_result)), ("arm-b", str(bad_result))],
            "score",
            str(output),
        )

    assert result["arms"]["arm-a"] == {"score": 0.88}
    assert "error" in result["arms"]["arm-b"]
    assert "score" in result["arms"]["arm-b"]["error"]


def test_aggregator_surfaces_per_arm_exit_code():
    """A crashed arm surfaces a non-zero exit_code and a human-readable reason;
    a sibling arm still shows its metric value (failure isolation preserved).
    Exit code 124 produces a timeout reason; exit code 0 with missing metric
    produces an unmeasured-criterion reason."""
    import tempfile
    from pathlib import Path

    from app.compute_arm_aggregator import aggregate

    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)

        # good arm: wrote result.json, exit 0
        good_result = base / "good_result.json"
        good_result.write_text(json.dumps({"score": 0.95}))
        good_exit = base / "good_exit"
        good_exit.write_text("0\n")

        # crashed arm: no result.json, exit 1, has stderr
        crashed_exit = base / "crashed_exit"
        crashed_exit.write_text("1\n")
        crashed_stderr = base / "crashed_stderr"
        crashed_stderr.write_text("Traceback (most recent call last):\n  RuntimeError: out of memory\n")

        # timed-out arm: no result.json, exit 124
        timeout_exit = base / "timeout_exit"
        timeout_exit.write_text("124\n")

        # unmeasured arm: wrote result.json but with wrong metric, exit 0
        unmeasured_result = base / "unmeasured_result.json"
        unmeasured_result.write_text(json.dumps({"other_metric": 42}))
        unmeasured_exit = base / "unmeasured_exit"
        unmeasured_exit.write_text("0\n")

        output = base / "main_result.json"
        result = aggregate(
            [
                ("good", str(good_result)),
                ("crashed", str(base / "nonexistent_result.json")),
                ("timed-out", str(base / "nonexistent_result2.json")),
                ("unmeasured", str(unmeasured_result)),
            ],
            "score",
            str(output),
            exit_codes={
                "good": str(good_exit),
                "crashed": str(crashed_exit),
                "timed-out": str(timeout_exit),
                "unmeasured": str(unmeasured_exit),
            },
            stderr_files={
                "crashed": str(crashed_stderr),
            },
        )

    # Good arm: metric value present, exit_code attached
    assert result["arms"]["good"]["score"] == 0.95
    assert result["arms"]["good"]["exit_code"] == 0

    # Crashed arm: non-zero exit_code, crash reason, stderr excerpt
    crashed = result["arms"]["crashed"]
    assert crashed["exit_code"] == 1
    assert "error" in crashed
    assert "crashed" in crashed["error"] or "1" in crashed["error"]
    assert "stderr_excerpt" in crashed
    assert "RuntimeError" in crashed["stderr_excerpt"]

    # Timed-out arm: exit_code 124, timeout reason
    timed_out = result["arms"]["timed-out"]
    assert timed_out["exit_code"] == 124
    assert "error" in timed_out
    assert "timed out" in timed_out["error"] or "124" in timed_out["error"]

    # Unmeasured arm: exit 0, but metric absent → unmeasured-criterion reason
    unmeasured = result["arms"]["unmeasured"]
    assert unmeasured["exit_code"] == 0
    assert "error" in unmeasured
    assert "unmeasured" in unmeasured["error"] or "score" in unmeasured["error"]


def test_aggregator_exit_code_absent_tolerated():
    """When no exit_code file is provided for an arm, aggregate() still records
    the failure reason without crashing; existing callers are unaffected."""
    import tempfile
    from pathlib import Path

    from app.compute_arm_aggregator import aggregate

    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)

        good_result = base / "good_result.json"
        good_result.write_text(json.dumps({"val": 1.0}))
        output = base / "out.json"

        # Call with no exit_codes/stderr_files (backward-compat path)
        result = aggregate(
            [("arm-good", str(good_result)), ("arm-bad", str(base / "missing.json"))],
            "val",
            str(output),
        )

    assert result["arms"]["arm-good"] == {"val": 1.0}
    assert "error" in result["arms"]["arm-bad"]
    # No exit_code field when not provided
    assert "exit_code" not in result["arms"]["arm-bad"]


# ---------------------------------------------------------------------------
# E2E: arms payload survives strategist spawn_task to child task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_strategist_spawn_compute_experiment_arms_e2e(
    client: AsyncClient, db_session
):
    """Strategic spawn_task with an arms payload must validate, merge into the
    child task payload, and land arms + declared_metric on the spawned child."""
    project, task, token = await _make_strategic_task(db_session)
    resp = await client.post(
        "/api/agent/strategic",
        headers={"x-agent-token": token},
        json={
            "task_id": str(task.id),
            "op": "spawn_task",
            "data": {
                "type": "compute_experiment",
                "title": "Arms e2e: compare adam vs sgd",
                "prompt": "Measure val_loss for two optimisers",
                "arms": [
                    {"name": "adam", "command": "python train.py --opt adam --epochs 5"},
                    {"name": "sgd", "command": "python train.py --opt sgd --epochs 5"},
                ],
                "declared_metric": "val_loss",
                "spend_cap_usd": 3.0,
                "deadline_seconds": 600,
            },
        },
    )
    assert resp.status_code == 200, resp.text
    child_id = uuid.UUID(resp.json()["task_id"])
    child = await db_session.get(Task, child_id)
    assert child is not None
    payload = child.first_payload
    assert payload.get("arms") == [
        {"name": "adam", "command": "python train.py --opt adam --epochs 5"},
        {"name": "sgd", "command": "python train.py --opt sgd --epochs 5"},
    ]
    assert payload.get("declared_metric") == "val_loss"
    # A synthesized top-level command must also be present for the compute job.
    assert isinstance(payload.get("command"), str) and payload["command"]


# --- pre-flight smoke gate ------------------------------------------------


@pytest.mark.asyncio
async def test_preflight_smoke_rejects_failing_command_before_full_provision(
    db_session, monkeypatch, _quiet_compute_io
):
    """Pre-flight smoke gate: a command that dies on the DB-less substrate is
    rejected before any full VM is provisioned.  backend.launch() must never be
    called; the job must end up FAILED/SMOKE_FAILED."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.compute_backends import PreflightSmokeResult
    from app.database import resolve_async_engine

    task = await _make_compute_task(
        db_session,
        {
            "type": "compute_experiment",
            "base_branch": "main",
            "command": "python3 -c 'import no_such_module'",
            "spend_cap_usd": 1.0,
            "deadline_seconds": 300,
        },
    )
    task.status = TaskStatus.IN_PROGRESS
    db_session.add(task)
    await db_session.commit()

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="python3 -c 'import no_such_module'",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )

    # Mock smoke to fail deterministically (no real subprocess needed).
    monkeypatch.setattr(
        compute_jobs,
        "run_preflight_smoke",
        AsyncMock(
            return_value=PreflightSmokeResult(
                startup_ok=False,
                exit_code=1,
                metric_captured=False,
                declared_metric_value=None,
                error_excerpt="ModuleNotFoundError: No module named 'no_such_module'",
            )
        ),
    )

    launch_called = []

    class FakeBackend:
        async def launch(self, spec):
            launch_called.append(spec)
            raise AssertionError("launch must not be called for smoke-failed jobs")

        async def poll_cost(self, persisted_job, *, as_of):
            from app.compute_backends import ComputeCostResult

            return ComputeCostResult(total_cost_usd=0.0, measured_at=as_of)

        async def poll_status(self, persisted_job):
            from app.compute_backends import ComputeStatusResult

            return ComputeStatusResult(status=ComputeJobStatus.QUEUED)

        async def terminate(self, persisted_job, *, reason):
            return True

    monkeypatch.setattr(compute_jobs, "get_compute_backend", lambda _name: FakeBackend())

    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )
    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)
    await db_session.refresh(job)

    assert not launch_called, "backend.launch() must never be called for smoke-failed jobs"
    assert job.status == ComputeJobStatus.FAILED
    assert job.outcome == ComputeJobOutcome.SMOKE_FAILED
    assert job.final_cost_usd == 0.0
    assert "compute_preflight_smoke_failed" in (job.terminal_reason or "")


@pytest.mark.asyncio
async def test_preflight_smoke_passing_command_proceeds_to_full_launch(
    db_session, monkeypatch, _quiet_compute_io
):
    """Pre-flight smoke gate: a command that passes the smoke is allowed to
    proceed to the normal full-VM provisioning path."""
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.compute_backends import (
        ComputeCostResult,
        ComputeLaunchResult,
        ComputeStatusResult,
        PreflightSmokeResult,
    )
    from app.database import resolve_async_engine

    task = await _make_compute_task(
        db_session,
        {
            "type": "compute_experiment",
            "base_branch": "main",
            "command": "echo hello",
            "spend_cap_usd": 1.0,
            "deadline_seconds": 300,
        },
    )
    task.status = TaskStatus.IN_PROGRESS
    db_session.add(task)
    await db_session.commit()

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="echo hello",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )

    monkeypatch.setattr(
        compute_jobs,
        "run_preflight_smoke",
        AsyncMock(
            return_value=PreflightSmokeResult(
                startup_ok=True,
                exit_code=0,
                metric_captured=True,
                declared_metric_value=None,
                error_excerpt="",
            )
        ),
    )

    launch_called = []

    class FakeBackend:
        def external_id_for(self, persisted_job):
            return "fake-vm-smoke-pass"

        async def launch(self, spec):
            launch_called.append(spec.job_id)
            return ComputeLaunchResult(
                external_id="fake-vm-smoke-pass",
                started_at=datetime.now(timezone.utc).replace(tzinfo=None),
                cost_rate_per_second=0.001,
            )

        async def poll_cost(self, persisted_job, *, as_of):
            return ComputeCostResult(total_cost_usd=0.0, measured_at=as_of)

        async def poll_status(self, persisted_job):
            return ComputeStatusResult(status=ComputeJobStatus.RUNNING)

        async def terminate(self, persisted_job, *, reason):
            return True

    monkeypatch.setattr(compute_jobs, "get_compute_backend", lambda _name: FakeBackend())

    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )

    # Tick 1: smoke runs and passes; job NOT yet launched (waits for next tick).
    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)
    await db_session.refresh(job)
    assert not launch_called, "launch must not be called on the smoke tick"
    assert (job.polling_metadata or {}).get("preflight_smoke_done") is True
    assert (job.polling_metadata or {}).get("preflight_smoke_status") == "passed"

    # Tick 2: smoke already done; job proceeds to full provisioning.
    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)
    await db_session.refresh(job)
    assert launch_called, "backend.launch() must be called after smoke passes"


@pytest.mark.asyncio
async def test_preflight_smoke_failed_task_completion_route(
    db_session, _quiet_compute_io
):
    """When a task's compute job has SMOKE_FAILED outcome, the completion handler
    marks the task FAILED (not COMPLETED) and stamps compute_preflight_smoke_failed
    as the failure_category — the gate must not treat SMOKE_FAILED as a measured
    terminal result."""
    from app.compute_jobs import build_compute_result_logs
    from app.failure_classification import COMPUTE_PREFLIGHT_SMOKE_FAILED

    task = await _make_compute_task(
        db_session,
        {
            "type": "compute_experiment",
            "base_branch": "main",
            "command": "python3 -c 'import no_such_module'",
            "spend_cap_usd": 1.0,
            "deadline_seconds": 300,
        },
    )
    task.status = TaskStatus.IN_PROGRESS
    db_session.add(task)
    await db_session.commit()

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="python3 -c 'import no_such_module'",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )
    # Simulate a SMOKE_FAILED terminal state (as set by the monitor).
    job.status = ComputeJobStatus.FAILED
    job.outcome = ComputeJobOutcome.SMOKE_FAILED
    job.final_cost_usd = 0.0
    job.accrued_cost_usd = 0.0
    job.terminal_reason = "compute_preflight_smoke_failed: exit 1"
    db_session.add(job)
    await db_session.commit()

    logs = build_compute_result_logs(job)
    await _handle_compute_experiment_completion(task, logs, db_session)

    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED
    assert (task.context or {}).get("failure_category") == COMPUTE_PREFLIGHT_SMOKE_FAILED


# --- run_preflight_smoke unit tests (real executor, no mock) ------------------


@pytest.mark.asyncio
async def test_run_preflight_smoke_failing_command():
    """run_preflight_smoke: a command that exits non-zero sets startup_ok=False."""
    from app.compute_backends import run_preflight_smoke

    result = await run_preflight_smoke("exit 42", timeout_seconds=10)
    assert result.startup_ok is False
    assert result.exit_code == 42
    assert result.metric_captured is True  # no declared_metric -> always "captured"


@pytest.mark.asyncio
async def test_run_preflight_smoke_long_running_command_is_pass():
    """run_preflight_smoke: a command that is still running at timeout is a PASS.

    A GPU training command will never finish within the short smoke window; the
    timeout (exit_code=None) must be treated as startup_ok=True, not a FAIL.
    """
    from app.compute_backends import run_preflight_smoke

    # 'sleep 60' runs far beyond the 2 s timeout — should be treated as alive/passing.
    result = await run_preflight_smoke("sleep 60", timeout_seconds=2)
    assert result.startup_ok is True
    assert result.exit_code is None  # timed out = still running
    assert result.metric_captured is True  # no declared_metric -> always "captured"


@pytest.mark.asyncio
async def test_run_preflight_smoke_clean_exit_with_metric():
    """run_preflight_smoke: clean exit with a valid result file captures the metric."""
    from app.compute_backends import run_preflight_smoke

    # Command writes the metric directly to the result file env var path.
    cmd = "python3 -c \"import json,os; json.dump({'val_loss': 0.42}, open(os.environ['BLUEPRINT_RESULT_FILE'], 'w'))\""
    result = await run_preflight_smoke(cmd, declared_metric="val_loss", timeout_seconds=10)
    assert result.startup_ok is True
    assert result.exit_code == 0
    assert result.metric_captured is True
    assert abs((result.declared_metric_value or 0) - 0.42) < 1e-9


@pytest.mark.asyncio
async def test_run_preflight_smoke_timeout_does_not_fail_on_declared_metric():
    """run_preflight_smoke: a timeout with a declared metric must still be a PASS.

    This covers the CRITICAL false-rejection: a GPU training command declares a
    metric (e.g. val_loss) but cannot write the result file within the short smoke
    window.  The timeout (exit_code=None) must not be counted as metric_captured=False.
    The _run_and_apply_preflight_smoke caller gates the metric check on exit_code==0.
    """
    from app.compute_backends import run_preflight_smoke

    # Command sleeps far past timeout so it never writes the result file.
    result = await run_preflight_smoke(
        "sleep 60", declared_metric="val_loss", timeout_seconds=2
    )
    assert result.startup_ok is True
    assert result.exit_code is None
    # metric_captured will be False (file not written), but the CALLER (compute_jobs)
    # must not penalize this — it only checks metric when exit_code == 0.
    assert result.metric_captured is False


# --- containerized pre-flight smoke (container_image set) ----------------------


def _make_fake_subprocess_exec(captured, *, returncode=0, stderr=b""):
    """Return a fake asyncio.create_subprocess_exec that records its argv.

    Captures each invocation's positional args/kwargs so tests can assert the
    constructed shell command WITHOUT spawning a real process or needing Docker.
    """

    async def _fake_exec(*args, **kwargs):
        captured.append({"args": args, "kwargs": kwargs})

        class _FakeProc:
            def __init__(self):
                self.returncode = returncode

            async def communicate(self):
                return (b"", stderr)

            def kill(self):
                pass

            async def wait(self):
                return returncode

        return _FakeProc()

    return _fake_exec


@pytest.mark.asyncio
async def test_preflight_smoke_runs_containerized_when_image_set(monkeypatch):
    """When container_image is set AND Docker is available, the smoke command is
    wrapped in docker pull + docker run of the declared image, with the result
    file mounted back to the host out dir — substrate-faithful smoke (bug a58156be)."""
    import app.compute_backends as cb

    captured: list[dict] = []
    monkeypatch.setattr(cb.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(cb, "detect_local_gpu", lambda: None)
    monkeypatch.setattr(cb.asyncio, "create_subprocess_exec", _make_fake_subprocess_exec(captured))

    result = await cb.run_preflight_smoke(
        "python3 train.py",
        container_image="ghcr.io/acme/trainer:latest",
        timeout_seconds=5,
    )

    assert len(captured) == 1, "exactly one (main) subprocess call expected"
    # The third positional arg to create_subprocess_exec is the bash -lc command.
    assert captured[0]["args"][0] == "bash"
    assert captured[0]["args"][1] == "-lc"
    cmd = captured[0]["args"][2]
    # docker pull + docker run of the declared image.
    assert "docker pull ghcr.io/acme/trainer:latest" in cmd
    assert "docker run --rm" in cmd
    assert "ghcr.io/acme/trainer:latest" in cmd
    assert "bash -lc" in cmd  # inner command re-wrapped inside the container
    # tmpdir is bind-mounted as the container out dir so result.json lands on host.
    assert ":/tmp/blueprint_out" in cmd
    assert "BLUEPRINT_RESULT_FILE=/tmp/blueprint_out/result.json" in cmd
    # No GPU detected -> no --gpus all flag.
    assert "--gpus" not in cmd
    assert result.ran_containerized is True
    assert result.degraded_no_docker is False
    assert result.startup_ok is True


@pytest.mark.asyncio
async def test_preflight_smoke_degrades_to_bare_host_when_docker_absent(monkeypatch):
    """When container_image is set but Docker is NOT on the orchestrator, the smoke
    falls back to the bare-host bash -lc path and flags degraded_no_docker — it must
    NOT hard-fail the job solely because Docker is missing on the orchestrator."""
    import app.compute_backends as cb

    monkeypatch.setattr(cb.shutil, "which", lambda _name: None)

    # No fake exec: let the real (bare) command run on the host.
    result = await cb.run_preflight_smoke(
        "echo hi",
        container_image="ghcr.io/acme/trainer:latest",
        timeout_seconds=5,
    )

    assert result.degraded_no_docker is True
    assert result.ran_containerized is False
    # Bare-host echo exits 0 -> startup OK -> the job is NOT failed for missing Docker.
    assert result.startup_ok is True
    assert result.exit_code == 0


@pytest.mark.asyncio
async def test_preflight_smoke_container_registry_auth_emits_docker_login(monkeypatch):
    """A registry auth carrying a password produces a docker login --password-stdin
    preamble before the pull (the orchestrated path strips the password, but the
    helper must honor it when provided directly)."""
    import app.compute_backends as cb

    captured: list[dict] = []
    monkeypatch.setattr(cb.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(cb, "detect_local_gpu", lambda: None)
    monkeypatch.setattr(cb.asyncio, "create_subprocess_exec", _make_fake_subprocess_exec(captured))

    result = await cb.run_preflight_smoke(
        "python3 train.py",
        container_image="reg.example.com/p/img:1",
        container_registry_auth={
            "server": "reg.example.com",
            "username": "robot",
            "password": "s3cret",
        },
        timeout_seconds=5,
    )

    cmd = captured[0]["args"][2]
    assert "docker login reg.example.com -u robot --password-stdin" in cmd
    assert "s3cret" in cmd  # password fed via printf | --password-stdin
    # login precedes the pull which precedes the run.
    assert cmd.index("docker login") < cmd.index("docker pull") < cmd.index("docker run")
    assert result.ran_containerized is True


@pytest.mark.asyncio
async def test_preflight_smoke_setup_step_runs_outside_container(monkeypatch):
    """setup_step runs OUTSIDE the container (bare bash -lc), matching the real
    local-subprocess backend, while the main command runs containerized."""
    import app.compute_backends as cb

    captured: list[dict] = []
    monkeypatch.setattr(cb.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(cb, "detect_local_gpu", lambda: None)
    monkeypatch.setattr(cb.asyncio, "create_subprocess_exec", _make_fake_subprocess_exec(captured))

    await cb.run_preflight_smoke(
        "python3 main.py",
        setup_step="pip install -e .",
        container_image="ghcr.io/acme/trainer:latest",
        timeout_seconds=6,
    )

    assert len(captured) == 2, "setup_step + main = two subprocess calls"
    setup_cmd = captured[0]["args"][2]
    main_cmd = captured[1]["args"][2]
    # setup runs bare on the host — no docker wrapping.
    assert setup_cmd == "pip install -e ."
    assert "docker" not in setup_cmd
    # main command is containerized.
    assert "docker run --rm" in main_cmd
    assert "docker pull ghcr.io/acme/trainer:latest" in main_cmd


@pytest.mark.asyncio
async def test_preflight_smoke_no_container_image_is_unchanged(monkeypatch):
    """The absent-container_image path is byte-for-byte unchanged: the command is
    passed bare to bash -lc even when Docker happens to be available."""
    import app.compute_backends as cb

    captured: list[dict] = []
    monkeypatch.setattr(cb.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(cb.asyncio, "create_subprocess_exec", _make_fake_subprocess_exec(captured))

    result = await cb.run_preflight_smoke("echo hello", timeout_seconds=5)

    cmd = captured[0]["args"][2]
    assert cmd == "echo hello"  # no docker wrapping
    assert result.ran_containerized is False
    assert result.degraded_no_docker is False


@pytest.mark.asyncio
async def test_degraded_smoke_does_not_reject_container_job(
    db_session, monkeypatch, _quiet_compute_io
):
    """When Docker is absent on the orchestrator (degraded_no_docker=True), a
    bare-host smoke failure must NOT gate a container_image job.  The command may
    legitimately depend on in-container libs not present on the host; the real VM
    will have Docker, so the job must proceed to full provisioning."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.compute_backends import PreflightSmokeResult
    from app.database import resolve_async_engine

    task = await _make_compute_task(
        db_session,
        {
            "type": "compute_experiment",
            "base_branch": "main",
            "command": "python3 train.py",
            "spend_cap_usd": 1.0,
            "deadline_seconds": 300,
        },
    )
    task.status = TaskStatus.IN_PROGRESS
    db_session.add(task)
    await db_session.commit()

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="python3 train.py",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
        container_image="ghcr.io/acme/trainer:latest",
    )

    # Simulate Docker-absent orchestrator: bare-host run of a container-only command
    # fails (the in-container dep is missing on host), but degraded_no_docker=True.
    monkeypatch.setattr(
        compute_jobs,
        "run_preflight_smoke",
        AsyncMock(
            return_value=PreflightSmokeResult(
                startup_ok=False,
                exit_code=1,
                metric_captured=False,
                declared_metric_value=None,
                error_excerpt="ModuleNotFoundError: No module named 'acme_trainer'",
                degraded_no_docker=True,
            )
        ),
    )

    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )
    from app.compute_jobs import _run_and_apply_preflight_smoke

    await _run_and_apply_preflight_smoke(session_factory, job, task)
    await db_session.refresh(job)

    # Degraded smoke must NOT reject the job — it should be marked as smoke-done
    # (pass) so the next monitor tick proceeds to full VM provisioning.
    assert job.status != ComputeJobStatus.FAILED, (
        "container job must not be rejected by a degraded (Docker-less) smoke failure"
    )
    assert job.outcome != ComputeJobOutcome.SMOKE_FAILED
    meta = job.polling_metadata or {}
    assert meta.get("preflight_smoke_done") is True
    assert meta.get("preflight_smoke_status") == "passed"
    assert meta.get("preflight_smoke_degraded_no_docker") is True


# ---------------------------------------------------------------------------
# Runner-subprocess pre-flight smoke skip (regression gate)
# ---------------------------------------------------------------------------


class TestRunnerSubprocessPreflightSmokeSkip:
    """_process_single_compute_job deliberately skips the leader-local pre-flight
    smoke for runner-subprocess jobs.

    The platform pre-flight smoke runs the command on the leader host before a
    full VM is billed.  For runner-subprocess jobs the actual command must run
    on operator hardware (which may need local GPU/CUDA/data that the leader
    lacks) — a leader-local smoke is wrong and would validate a different
    substrate than the real execution environment.

    These tests pin that intentional skip as a regression gate:
    - Negative case (acceptance-evidence selector): a QUEUED runner-subprocess
      job must NOT call ``_run_and_apply_preflight_smoke``.
    - Positive counterpart: a QUEUED job on ANY other backend MUST call it,
      confirming the skip is backend-specific and not accidentally global.

    If the negative test starts asserting that smoke was called, that is a
    regression — the platform smoke is the wrong gate for runner-subprocess.
    """

    def _mock_job(self, backend: str):
        """Return a minimal mock ComputeJob with QUEUED status for the given backend."""
        from unittest.mock import MagicMock

        job = MagicMock()
        # task_id=None: skips the task DB lookup so tests run without a real session.
        job.task_id = None
        job.status = ComputeJobStatus.QUEUED
        job.backend = backend
        # Empty dict, no preflight_smoke_done stamp — the job has not been smoked yet.
        job.polling_metadata = {}
        job.external_compute_id = None
        return job

    @pytest.mark.asyncio
    async def test_runner_subprocess_queued_job_skips_preflight_smoke(self, mocker):
        """A QUEUED runner-subprocess job must NOT trigger the leader-side
        pre-flight smoke.  Acceptance-evidence selector: pinned in task
        evidence as test_runner_subprocess_queued_job_skips_preflight_smoke."""
        from unittest.mock import MagicMock

        from app.compute_backends import RUNNER_SUBPROCESS_BACKEND_NAME
        from app.compute_jobs import _process_single_compute_job

        job = self._mock_job(RUNNER_SUBPROCESS_BACKEND_NAME)

        smoke_mock = mocker.patch(
            "app.compute_jobs._run_and_apply_preflight_smoke",
            new_callable=AsyncMock,
        )
        # Runner path bypasses smoke and proceeds to _reserve_compute_job_launch.
        # Return None so the function exits cleanly without needing a real session.
        mocker.patch(
            "app.compute_jobs._reserve_compute_job_launch",
            new_callable=AsyncMock,
            return_value=None,
        )
        mocker.patch(
            "app.compute_jobs.get_compute_backend",
            return_value=MagicMock(),
        )

        await _process_single_compute_job(job, session_factory=AsyncMock())

        smoke_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_runner_subprocess_queued_job_triggers_preflight_smoke(
        self, mocker
    ):
        """A QUEUED job on a non-runner-subprocess backend (e.g. local-subprocess)
        MUST trigger the leader-side pre-flight smoke — confirming the skip is
        backend-specific and that the general path is still wired correctly."""
        from unittest.mock import MagicMock

        from app.compute_jobs import _process_single_compute_job

        job = self._mock_job("local-subprocess")

        smoke_mock = mocker.patch(
            "app.compute_jobs._run_and_apply_preflight_smoke",
            new_callable=AsyncMock,
        )
        mocker.patch(
            "app.compute_jobs.get_compute_backend",
            return_value=MagicMock(),
        )

        await _process_single_compute_job(job, session_factory=AsyncMock())

        # Smoke must be called exactly once; the function returns immediately after.
        smoke_mock.assert_called_once()

    @pytest.mark.asyncio
    async def test_runner_subprocess_with_preflight_smoke_done_still_skips_smoke(
        self, mocker
    ):
        """Even if preflight_smoke_done were somehow stamped on a runner-subprocess
        job, the backend guard fires first — smoke is never called for this backend."""
        from unittest.mock import MagicMock

        from app.compute_backends import RUNNER_SUBPROCESS_BACKEND_NAME
        from app.compute_jobs import _process_single_compute_job

        job = self._mock_job(RUNNER_SUBPROCESS_BACKEND_NAME)
        # Simulate an already-stamped preflight_smoke_done on a runner-subprocess job.
        job.polling_metadata = {"preflight_smoke_done": True}

        smoke_mock = mocker.patch(
            "app.compute_jobs._run_and_apply_preflight_smoke",
            new_callable=AsyncMock,
        )
        mocker.patch(
            "app.compute_jobs._reserve_compute_job_launch",
            new_callable=AsyncMock,
            return_value=None,
        )
        mocker.patch(
            "app.compute_jobs.get_compute_backend",
            return_value=MagicMock(),
        )

        await _process_single_compute_job(job, session_factory=AsyncMock())

        smoke_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_runner_with_preflight_smoke_done_skips_smoke(self, mocker):
        """A non-runner-subprocess job that already has preflight_smoke_done=True
        must skip smoke (idempotency guard) and proceed straight to launch.
        This is the idempotent-stamp behaviour, not the backend-skip behaviour."""
        from unittest.mock import MagicMock

        from app.compute_jobs import _process_single_compute_job

        job = self._mock_job("local-subprocess")
        job.polling_metadata = {"preflight_smoke_done": True}

        smoke_mock = mocker.patch(
            "app.compute_jobs._run_and_apply_preflight_smoke",
            new_callable=AsyncMock,
        )
        mocker.patch(
            "app.compute_jobs._reserve_compute_job_launch",
            new_callable=AsyncMock,
            return_value=None,
        )
        mocker.patch(
            "app.compute_jobs.get_compute_backend",
            return_value=MagicMock(),
        )

        await _process_single_compute_job(job, session_factory=AsyncMock())

        # Smoke already stamped — idempotent guard means it must NOT be re-run.
        smoke_mock.assert_not_called()
