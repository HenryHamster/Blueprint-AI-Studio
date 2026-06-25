"""Shared compute_experiment launch-contract helpers.

This module stays neutral so both research- and plan-completion can author the
same compute_experiment payload shape without importing each other.
"""

from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import Any

# Safe identifier charset for arm names: no shell metacharacters, no colons
# (colon is the delimiter in the NAME:PATH aggregator spec), no slashes/dots
# (would escape per-arm result directories).
_ARM_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# Safe charset for metric keys: allows common patterns like val/loss, top-1
# while excluding shell metacharacters (", $, ;, backticks, spaces).
_METRIC_KEY_RE = re.compile(r"^[A-Za-z0-9_./-]+$")

# Read and encode the aggregator script at module-load time.  The blob is then
# embedded in generated per-arm bash wrappers so the aggregator runs on the
# remote compute node without a separate deploy step.
_AGGREGATOR_B64 = base64.b64encode(
    (Path(__file__).parent / "compute_arm_aggregator.py").read_bytes()
).decode()


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
        "arms",
        "declared_metric",
        "sweep_id",
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
    arms = contract.get("arms")
    command = contract.get("command")

    if arms is not None:
        # Arms-based multi-arm experiment: validate each arm and synthesize command.
        if not isinstance(arms, list) or len(arms) == 0:
            return False, "arms must be a non-empty list of {name, command} objects"
        seen_names: set[str] = set()
        for i, arm in enumerate(arms):
            if not isinstance(arm, dict):
                return False, f"arms[{i}] must be a dict with 'name' and 'command' fields"
            arm_name = arm.get("name")
            arm_cmd = arm.get("command")
            if not (isinstance(arm_name, str) and arm_name.strip()):
                return False, f"arms[{i}].name is missing or empty"
            if not _ARM_NAME_RE.match(arm_name):
                return (
                    False,
                    f"arms[{i}].name {arm_name!r} contains invalid characters — "
                    "arm names must match ^[A-Za-z0-9_-]+$",
                )
            if arm_name in seen_names:
                return False, f"duplicate arm name {arm_name!r} — arm names must be unique"
            seen_names.add(arm_name)
            if not (isinstance(arm_cmd, str) and arm_cmd.strip()):
                return False, f"arms[{i}].command (arm '{arm_name}') is missing or empty"
        declared_metric = contract.get("declared_metric")
        if not (isinstance(declared_metric, str) and declared_metric.strip()):
            return False, "declared_metric is required and must be a non-empty string when arms are specified"
        if not _METRIC_KEY_RE.match(declared_metric):
            return (
                False,
                f"declared_metric {declared_metric!r} contains invalid characters — "
                "metric keys must match ^[A-Za-z0-9_./-]+$",
            )
        if not (isinstance(command, str) and command.strip()):
            # Synthesize a per-arm isolation script. Each arm runs in its own
            # subshell with BLUEPRINT_RESULT_FILE pointed to a private path so
            # arms cannot overwrite each other's metric (no last-arm-wins).
            # A subshell failure is captured as an exit_code file and does NOT
            # abort the remaining arms.  After all arms finish, the aggregator
            # script (embedded as a base64 blob) combines each arm's result into
            # the canonical {"arms": {name: {declared_metric: value}}} block and
            # writes it to the real BLUEPRINT_RESULT_FILE.
            lines: list[str] = [
                '_BRF_MAIN="${BLUEPRINT_RESULT_FILE:-/tmp/blueprint_result.json}"',
                '_BRF_BASE="$(dirname "${_BRF_MAIN}")/.blueprint_arm_results"',
                'mkdir -p "${_BRF_BASE}"',
            ]
            arm_result_specs: list[str] = []
            arm_exit_specs: list[str] = []
            arm_stderr_specs: list[str] = []
            for arm in arms:
                aname = arm["name"]
                acmd = arm["command"].strip()
                lines += [
                    f'mkdir -p "${{_BRF_BASE}}/{aname}"',
                    f'(export BLUEPRINT_RESULT_FILE="${{_BRF_BASE}}/{aname}/result.json"; {acmd})'
                    f' 2>"${{_BRF_BASE}}/{aname}/stderr"',
                    f'echo $? > "${{_BRF_BASE}}/{aname}/exit_code"',
                ]
                arm_result_specs.append(f'"{aname}:${{_BRF_BASE}}/{aname}/result.json"')
                arm_exit_specs.append(f'"{aname}:${{_BRF_BASE}}/{aname}/exit_code"')
                arm_stderr_specs.append(f'"{aname}:${{_BRF_BASE}}/{aname}/stderr"')
            arm_specs_arg = " ".join(arm_result_specs)
            exit_specs_arg = " ".join(arm_exit_specs)
            stderr_specs_arg = " ".join(arm_stderr_specs)
            lines += [
                f"echo {_AGGREGATOR_B64} | base64 -d > /tmp/blueprint_arm_agg.py",
                f'python3 /tmp/blueprint_arm_agg.py --arms {arm_specs_arg}'
                f' --declared-metric "{declared_metric}" --output "${{_BRF_MAIN}}"'
                f' --exit-codes {exit_specs_arg}'
                f' --stderr {stderr_specs_arg}',
            ]
            script = "\n".join(lines)
            encoded = base64.b64encode(script.encode()).decode()
            contract["command"] = f"echo {encoded} | base64 -d | bash"
    elif not (isinstance(command, str) and command.strip()):
        return False, "missing or empty command"

    declared_metric = contract.get("declared_metric")
    if declared_metric is not None:
        if not (isinstance(declared_metric, str) and declared_metric.strip()):
            return False, "declared_metric must be a non-empty string"
        if not _METRIC_KEY_RE.match(declared_metric):
            return (
                False,
                f"declared_metric {declared_metric!r} contains invalid characters — "
                "metric keys must match ^[A-Za-z0-9_./-]+$",
            )
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
    wins; otherwise the global runtime-config default applies. Per-project threshold
    overrides were removed in v19 — threshold never comes from Project fields.
    Malformed config degrades to ``0.0`` so any positive proposed spend requires
    approval.
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
