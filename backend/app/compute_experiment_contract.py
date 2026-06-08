"""Shared compute_experiment launch-contract helpers.

This module stays neutral so both research- and plan-completion can author the
same compute_experiment payload shape without importing each other.
"""

from __future__ import annotations

from typing import Any


COMPUTE_EXPERIMENT_PREREQUISITE_MODE_CONTEXT_KEY = (
    "compute_experiment_prerequisite_mode"
)
COMPUTE_EXPERIMENT_PREREQUISITE_MODE_ADVISORY = "advisory_success_criterion"


def _author_compute_launch_contract(finding: dict[str, Any]) -> dict[str, Any]:
    """Assemble a compute_experiment launch contract from a finding/subtask.

    The launch fields may live flat on the dict or nested under a
    ``compute_experiment`` / ``launch_contract`` object. Validation is the
    caller's job; this helper only collects the raw launch spec.
    """
    nested = finding.get("compute_experiment") or finding.get("launch_contract") or {}
    if not isinstance(nested, dict):
        nested = {}

    def _pick(key: str) -> Any:
        val = nested.get(key)
        if val is None:
            val = finding.get(key)
        return val

    contract: dict[str, Any] = {
        "command": _pick("command"),
        "spend_cap_usd": _pick("spend_cap_usd"),
        "deadline_seconds": _pick("deadline_seconds"),
    }
    success = _pick("success_criterion")
    if success is not None:
        contract["success_criterion"] = success
    for opt_key in (
        "machine_type",
        "experiment",
        "benchmark",
        "metric",
        "cwd",
        "env",
        "accelerator",
        "provisioning_model",
    ):
        val = _pick(opt_key)
        if val is not None:
            contract[opt_key] = val
    return contract


def _validate_compute_launch_contract(
    contract: dict[str, Any],
) -> tuple[bool, str]:
    """Return ``(ok, reason)`` for a proposed compute_experiment launch contract."""
    command = contract.get("command")
    if not (isinstance(command, str) and command.strip()):
        return False, "missing or empty command"
    try:
        spend = float(contract.get("spend_cap_usd"))
    except (TypeError, ValueError):
        return False, f"spend_cap_usd is not a number ({contract.get('spend_cap_usd')!r})"
    if not spend > 0:
        return False, f"spend_cap_usd must be positive (got {spend})"
    try:
        deadline = int(contract.get("deadline_seconds"))
    except (TypeError, ValueError):
        return (
            False,
            f"deadline_seconds is not an integer ({contract.get('deadline_seconds')!r})",
        )
    if not deadline > 0:
        return False, f"deadline_seconds must be positive (got {deadline})"
    return True, ""
