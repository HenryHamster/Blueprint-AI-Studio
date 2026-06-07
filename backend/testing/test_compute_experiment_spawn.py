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

import pytest

from app.research_completion import (
    COMPUTE_EXPERIMENT_FINDING_TYPES,
    _author_compute_launch_contract,
    _resolve_compute_spend_threshold,
    _validate_compute_launch_contract,
)


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
        ({"command": "run", "spend_cap_usd": 1.0, "deadline_seconds": None}, "deadline"),
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
    assert _resolve_compute_spend_threshold(rc, {"compute_experiment_spend_threshold_usd": 0}) == 10.0
    assert _resolve_compute_spend_threshold(rc, {"compute_experiment_spend_threshold_usd": "x"}) == 10.0
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
