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
        "hypothesis",
        "cwd",
        "env",
        "accelerator",
        "provisioning_model",
        "live_proof",
        "live_proof_marker",
        "live_release_proof",
        "live_release_proof_marker",
        "requires_live_release",
        "require_durable_artifact",
        "smoke_sample",
    ):
        val = _pick(opt_key)
        if val is not None:
            contract[opt_key] = val
    return contract


def _validate_compute_launch_contract(
    contract: dict[str, Any],
    *,
    require_smoke: bool = True,
) -> tuple[bool, str]:
    """Return ``(ok, reason)`` for a proposed compute_experiment launch contract.

    ``require_smoke=True`` (the default for author-generated contracts) requires a
    ``smoke_sample`` proving the command starts and, when a ``success_criterion``
    is declared, emits a numeric metric.  Operator-triggered launches may pass
    ``require_smoke=False`` to skip that check.
    """
    command = contract.get("command")
    if not (isinstance(command, str) and command.strip()):
        return False, "missing or empty command"
    try:
        spend = float(contract.get("spend_cap_usd"))
    except (TypeError, ValueError):
        return (
            False,
            f"spend_cap_usd is not a number ({contract.get('spend_cap_usd')!r})",
        )
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

    if require_smoke:
        smoke = contract.get("smoke_sample")
        if not smoke or not isinstance(smoke, dict):
            return (
                False,
                "missing smoke_sample: author must smoke-test the command before "
                "dispatch — run a short variant, capture startup_ok + metric_captured, "
                "add smoke_sample to the finding",
            )
        if smoke.get("unsmokable"):
            reason = str(smoke.get("unsmokable_reason") or "cannot be smoked cheaply")
            return (
                False,
                f"un-smokable command requires explicit operator review: {reason}",
            )
        if not smoke.get("startup_ok"):
            error_excerpt = str(smoke.get("error_excerpt") or "unknown error")[:200]
            return (
                False,
                f"smoke failed — command did not start cleanly: {error_excerpt}",
            )
        if contract.get("success_criterion") and not smoke.get("metric_captured"):
            return (
                False,
                "smoke did not capture declared metric — verify command writes "
                "{metric: value} to $BLUEPRINT_RESULT_FILE",
            )

    return True, ""


def resolve_compute_spend_threshold(
    runtime_config: dict[str, Any],
    direction: dict[str, Any] | None,
) -> float:
    """Resolve the compute_experiment approval threshold (USD).

    A positive per-direction ``compute_experiment_spend_threshold_usd`` override
    wins; otherwise the global runtime-config default applies. Malformed config
    degrades to ``0.0`` so any positive proposed spend requires approval.
    """
    if direction:
        override = direction.get("compute_experiment_spend_threshold_usd")
        if isinstance(override, (int, float)) and override > 0:
            return float(override)
    try:
        return float(runtime_config.get("compute_experiment_spend_threshold_usd", 0.0))
    except (TypeError, ValueError):
        return 0.0


def resolve_compute_spend_approval(
    *,
    runtime_config: dict[str, Any],
    direction: dict[str, Any] | None,
    spend_cap_usd: float,
) -> tuple[bool, float]:
    """Return ``(requires_approval, threshold)`` for a proposed compute spend."""
    threshold = resolve_compute_spend_threshold(runtime_config, direction)
    return float(spend_cap_usd) > threshold, threshold
