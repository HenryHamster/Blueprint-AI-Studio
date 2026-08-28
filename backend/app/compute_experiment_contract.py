"""Shared compute_experiment launch-contract helpers.

This module stays neutral so both research- and plan-completion can author the
same compute_experiment payload shape without importing each other.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# NOTE on matching: every charset gate below MUST be applied with `.fullmatch()`,
# never `.match()`. Python's `$` also matches just *before* a trailing newline, so
# `_RE.match("GOOD\n")` SUCCEEDS on all three patterns — the trailing newline is
# never seen, and the value reaches the shell as an identifier split across two
# lines. `fullmatch` requires the whole string to be consumed and is the only
# correct anchor here; the `^...$` below is kept only so the pattern reads well
# when it is interpolated into an operator-facing rejection reason.

# Safe identifier charset for arm names: no shell metacharacters, no colons
# (colon is the delimiter in the NAME:PATH aggregator spec), no slashes/dots
# (would escape per-arm result directories).
ARM_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# Safe charset for metric keys: allows common patterns like val/loss, top-1
# while excluding shell metacharacters (", $, ;, backticks, spaces).
METRIC_KEY_RE = re.compile(r"^[A-Za-z0-9_./-]+$")
MAX_DECLARED_METRIC_LENGTH = 128

# Env var names must be valid shell identifiers. The GCP startup script emits
# `export {key}=...` with the key interpolated BARE, and it runs without
# `set -e` — so a key like "my-model" would error and the script would
# silently continue, billing an experiment run that is missing that env var.
# Reject at contract-validation time so a bad contract fails loud at
# authoring instead (see also the defense-in-depth guard in
# compute_backends._build_gcp_startup_script, which imports this pattern).
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

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


# Multi-arm wrappers are stored as `echo <blob> | base64 -d | bash`.  The
# encoding is deliberate and stays: the wrapper nests Python inside bash inside
# a Python string literal, and compounding escape layers is the bug class
# CLAUDE.md explicitly warns about.  What the encoding costs is legibility — an
# operator reading the job sees an opaque blob — so we decode on READ rather
# than give up the escape safety.
_GENERATED_COMMAND_RE = re.compile(
    r"^echo\s+(?P<blob>[A-Za-z0-9+/=]+)\s*\|\s*base64\s+-d\s*\|\s*bash$"
)

# ``python -c "\\n..."`` hands Python a literal backslash-n rather than a
# source newline, so it always fails before the experiment can start.
_PYTHON_C_LITERAL_ESCAPED_NEWLINE_RE = re.compile(
    r"\bpython(?:3(?:\.\d+)?)?\s+-c\s+[\"']\\n"
)

# Within the decoded wrapper the aggregator is itself a base64 blob of a static
# file (compute_arm_aggregator.py), thousands of characters long.  Expanding it
# would bury the per-arm commands, which are the part an operator actually reads.
#
# The blob is matched EXACTLY, not by shape: `echo <blob> | base64 -d > <dest>`
# is the very pattern CLAUDE.md tells authors to use for embedding a script, so
# an arm's own command may legitimately contain one.  Eliding by shape alone
# would relabel that arm's blob as the aggregator and destroy its contents —
# a legibility feature that lies about what ran is worse than no feature.
_EMBEDDED_AGGREGATOR_RE = re.compile(
    r"^echo\s+(?P<blob>[A-Za-z0-9+/=]{64,})\s*\|\s*base64\s+-d\s*>\s*(?P<dest>\S+)$",
    re.MULTILINE,
)


def decode_generated_command(
    command: str | None, *, elide_aggregator: bool = True
) -> str | None:
    """Return the readable bash behind a generated ``base64 -d | bash`` command.

    ``None`` means *command* is not one of our generated wrappers — single-arm
    experiments are never encoded, and an operator-authored command is passed
    through verbatim.  Callers fall back to showing the raw command.

    Never raises: a truncated or non-UTF-8 blob yields ``None`` rather than
    turning a read of an old job into a 500.
    """
    if not isinstance(command, str):
        return None
    match = _GENERATED_COMMAND_RE.match(command.strip())
    if match is None:
        return None
    try:
        script = base64.b64decode(match.group("blob"), validate=True).decode()
    except (ValueError, UnicodeDecodeError):
        return None
    if elide_aggregator:

        def _elide(m: re.Match[str]) -> str:
            # Only OUR aggregator blob is elided. An arm that embeds its own
            # base64 script is left byte-for-byte intact.
            if m.group("blob") != _AGGREGATOR_B64:
                return m.group(0)
            return (
                "echo <base64 blob: compute_arm_aggregator.py> "
                f"| base64 -d > {m.group('dest')}"
            )

        script = _EMBEDDED_AGGREGATOR_RE.sub(_elide, script)
    return script


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
    success_many = _pick("success_criteria")
    if success_many is not None:
        contract["success_criteria"] = success_many
    for opt_key in (
        "work_class",
        "stop_or_pivot_condition",
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
        "setup_step",
        "container_image",
        "container_registry_auth",
        "allow_host_fallback",
        "spawn_followup",
        "no_progress_timeout_seconds",
        "input_artifact_job_id",
        "runner_target",
        "vram_gb",
        "min_compute_capability",
    ):
        val = _pick(opt_key)
        if val is not None:
            if opt_key in {"work_class", "stop_or_pivot_condition"} and isinstance(
                val, str
            ):
                val = val.strip()
            contract[opt_key] = val
    return contract


def _stamp_container_image_pin_status(
    contract: dict[str, Any],
    context: Any = None,
) -> None:
    """Record whether a contract's resolved ``container_image`` is digest-pinned.

    The images-first substrate promises run-to-run reproducibility via digest
    pinning, but ``resolve_digest_pinned_default_image`` fails open to the raw
    mutable manifest tag whenever no matching build record exists. That leaves the
    operator believing a run is digest-reproducible when it is not, with no signal
    on the job itself.

    When a ``container_image`` is set, stamp ``container_image_digest_pinned`` onto
    the contract (True iff the reference is content-addressed) so the flag persists
    on the spawned child payload and can be surfaced in the UI. A mutable default
    additionally logs a warning. When no image is declared the flag is left absent
    (a bare ``bash -lc`` job has no image to pin). ``context`` is any identifier
    (e.g. a task) included in the warning for correlation.
    """
    from app.compute_backends import is_digest_pinned

    image = contract.get("container_image")
    if not image:
        return
    pinned = is_digest_pinned(image)
    contract["container_image_digest_pinned"] = pinned
    if not pinned:
        logger.warning(
            "[COMPUTE] compute_experiment (%s) defaults to MUTABLE image %r "
            "(no @sha256: digest) — run-to-run reproducibility is NOT guaranteed",
            context,
            image,
        )


def _validate_compute_launch_contract(
    contract: dict[str, Any],
    *,
    require_smoke: bool = True,
) -> tuple[bool, str]:
    """Return ``(ok, reason)`` for a proposed compute_experiment launch contract.

    ``require_smoke=True`` (the legacy default for author-generated contracts)
    previously hard-rejected contracts whose ``smoke_sample`` was missing or
    reported failure.  This check is now ADVISORY ONLY: the platform executes a
    real pre-flight smoke (capability ``platform-preflight-smoke``) before any
    full VM is billed, which is the binding gate.  That smoke runs the command
    CONTAINERIZED — on the same substrate class as the full run (``docker pull``
    + ``docker run`` of the declared image) — when ``container_image`` is set AND
    Docker is available on the orchestrator host; it degrades to a bare-host
    ``bash -lc`` smoke when no ``container_image`` is declared or Docker is absent
    on the orchestrator (see ``run_preflight_smoke`` in compute_backends.py; this
    closes the substrate-fidelity gap tracked as bug a58156be).
    A missing or self-reported-failing author smoke_sample is logged as a warning
    but no longer blocks the contract.  Operator-triggered launches may pass
    ``require_smoke=False`` to silence even the advisory log.
    """
    work_class = contract.get("work_class")
    if work_class is not None and (
        not isinstance(work_class, str)
        or work_class
        not in {
            "exploratory",
            "confirmatory",
            "productization",
        }
    ):
        return False, (
            "work_class must be one of exploratory, confirmatory, productization"
        )

    stop_or_pivot = contract.get("stop_or_pivot_condition")
    if stop_or_pivot is not None:
        if not (isinstance(stop_or_pivot, str) and stop_or_pivot.strip()):
            return False, "stop_or_pivot_condition must be a non-empty string"
        if len(stop_or_pivot) > 4000:
            return False, "stop_or_pivot_condition must be at most 4000 characters"

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
            if not ARM_NAME_RE.fullmatch(arm_name):
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
            if _PYTHON_C_LITERAL_ESCAPED_NEWLINE_RE.search(arm_cmd):
                return (
                    False,
                    f"arms[{i}].command starts python -c with a literal \\n; "
                    "use a real script file or a base64-decoded script instead",
                )
        declared_metric = contract.get("declared_metric")
        if not (isinstance(declared_metric, str) and declared_metric.strip()):
            return False, "declared_metric is required and must be a non-empty string when arms are specified"
        if len(declared_metric) > MAX_DECLARED_METRIC_LENGTH:
            return False, (
                f"declared_metric must be at most {MAX_DECLARED_METRIC_LENGTH} characters"
            )
        if not METRIC_KEY_RE.fullmatch(declared_metric):
            return (
                False,
                f"declared_metric {declared_metric!r} contains invalid characters — "
                "metric keys must match ^[A-Za-z0-9_./-]+$",
            )
        # Synthesize the canonical per-arm isolation script on every validation.
        # A normalized contract legitimately carries both ``arms`` and this exact
        # generated command; accepting it makes validation idempotent. Any other
        # simultaneous command remains user-authored dual mode and is rejected.
        lines: list[str] = [
            '_BRF_MAIN="${BLUEPRINT_RESULT_FILE:-/tmp/blueprint_result.json}"',
            '_BRF_BASE="$(dirname "${_BRF_MAIN}")/.blueprint_arm_results"',
            'mkdir -p "${_BRF_BASE}"',
        ]
        arm_result_specs: list[str] = []
        arm_exit_specs: list[str] = []
        arm_stderr_specs: list[str] = []
        arm_duration_specs: list[str] = []
        for arm in arms:
            aname = arm["name"]
            acmd = arm["command"].strip()
            lines += [
                f'mkdir -p "${{_BRF_BASE}}/{aname}"',
                '_BRF_T1=$(date +%s)',
                f'(export BLUEPRINT_RESULT_FILE="${{_BRF_BASE}}/{aname}/result.json"; {acmd})'
                f' 2>"${{_BRF_BASE}}/{aname}/stderr"',
                f'echo $? > "${{_BRF_BASE}}/{aname}/exit_code"',
                f'echo $(($(date +%s) - _BRF_T1)) > "${{_BRF_BASE}}/{aname}/elapsed"',
            ]
            arm_result_specs.append(f'"{aname}:${{_BRF_BASE}}/{aname}/result.json"')
            arm_exit_specs.append(f'"{aname}:${{_BRF_BASE}}/{aname}/exit_code"')
            arm_stderr_specs.append(f'"{aname}:${{_BRF_BASE}}/{aname}/stderr"')
            arm_duration_specs.append(f'"{aname}:${{_BRF_BASE}}/{aname}/elapsed"')
        arm_specs_arg = " ".join(arm_result_specs)
        exit_specs_arg = " ".join(arm_exit_specs)
        stderr_specs_arg = " ".join(arm_stderr_specs)
        duration_specs_arg = " ".join(arm_duration_specs)
        lines += [
            f"echo {_AGGREGATOR_B64} | base64 -d > /tmp/blueprint_arm_agg.py",
            f'python3 /tmp/blueprint_arm_agg.py --arms {arm_specs_arg}'
            f' --declared-metric "{declared_metric}" --output "${{_BRF_MAIN}}"'
            f' --exit-codes {exit_specs_arg}'
            f' --stderr {stderr_specs_arg}'
            f' --durations {duration_specs_arg}',
        ]
        script = "\n".join(lines)
        encoded = base64.b64encode(script.encode()).decode()
        normalized_command = f"echo {encoded} | base64 -d | bash"
        if isinstance(command, str) and command.strip():
            if command != normalized_command:
                return False, "command and arms are mutually exclusive"
        else:
            contract["command"] = normalized_command
    elif not (isinstance(command, str) and command.strip()):
        return False, "missing or empty command"

    command = contract.get("command")
    if isinstance(command, str) and _PYTHON_C_LITERAL_ESCAPED_NEWLINE_RE.search(command):
        return (
            False,
            "command starts python -c with a literal \\n; "
            "use a real script file or a base64-decoded script instead",
        )

    declared_metric = contract.get("declared_metric")
    if declared_metric is not None:
        if not (isinstance(declared_metric, str) and declared_metric.strip()):
            return False, "declared_metric must be a non-empty string"
        if len(declared_metric) > MAX_DECLARED_METRIC_LENGTH:
            return False, (
                f"declared_metric must be at most {MAX_DECLARED_METRIC_LENGTH} characters"
            )
        if not METRIC_KEY_RE.fullmatch(declared_metric):
            return (
                False,
                f"declared_metric {declared_metric!r} contains invalid characters — "
                "metric keys must match ^[A-Za-z0-9_./-]+$",
            )
    raw_criteria = contract.get("success_criteria")
    if raw_criteria is not None:
        if not isinstance(raw_criteria, list) or not raw_criteria:
            return False, "success_criteria must be a non-empty array"
        for index, item in enumerate(raw_criteria):
            if not isinstance(item, dict):
                return False, f"success_criteria[{index}] must be an object"
            kind = str(item.get("kind") or "metric")
            if kind == "artifact_produced":
                continue
            metric_name = item.get("metric")
            if not (
                isinstance(metric_name, str)
                and metric_name.strip()
                and METRIC_KEY_RE.fullmatch(metric_name)
            ):
                return False, (
                    f"success_criteria[{index}].metric must be a non-empty metric key"
                )
            if item.get("operator") not in {">=", "<=", ">", "<", "==", "!="}:
                return False, f"success_criteria[{index}].operator is invalid"
            threshold = item.get("threshold")
            if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
                return False, f"success_criteria[{index}].threshold must be numeric"
        if declared_metric is not None:
            criterion_metric_names = {
                str(item.get("metric")).strip()
                for item in raw_criteria
                if isinstance(item, dict)
                and str(item.get("kind") or "metric") == "metric"
            }
            if declared_metric not in criterion_metric_names:
                return False, (
                    "declared_metric must name one of the success_criteria metrics"
                )
    elif declared_metric is not None:
        singular = contract.get("success_criterion")
        if (
            isinstance(singular, dict)
            and str(singular.get("kind") or "metric") == "metric"
            and singular.get("metric") != declared_metric
        ):
            return False, (
                "declared_metric must match success_criterion.metric "
                f"({declared_metric!r} != {singular.get('metric')!r})"
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

    no_progress_timeout = contract.get("no_progress_timeout_seconds")
    if no_progress_timeout is not None:
        try:
            npt = int(no_progress_timeout)
        except (TypeError, ValueError):
            return (
                False,
                f"no_progress_timeout_seconds is not an integer "
                f"({no_progress_timeout!r})",
            )
        if not npt > 0:
            return False, (
                f"no_progress_timeout_seconds must be positive (got {npt})"
            )
        if npt > deadline:
            return False, (
                f"no_progress_timeout_seconds ({npt}) must not exceed "
                f"deadline_seconds ({deadline})"
            )

    input_artifact_job_id = contract.get("input_artifact_job_id")
    if input_artifact_job_id is not None:
        # Shape-check first so malformed direct-dispatch payloads retain their
        # actionable UUID diagnostic rather than being masked by the resume guard.
        try:
            uuid.UUID(str(input_artifact_job_id))
        except (ValueError, AttributeError, TypeError):
            return (
                False,
                f"input_artifact_job_id must be a UUID string "
                f"({input_artifact_job_id!r})",
            )
        if no_progress_timeout is None:
            return (
                False,
                "resumed compute experiments require no_progress_timeout_seconds",
            )
        if not (isinstance(declared_metric, str) and declared_metric.strip()):
            return (
                False,
                "resumed compute experiments require a declared_metric for the watchdog",
            )
        # The orchestrator performs authoritative same-project + bundle-exists
        # validation at create_compute_job time (it has the DB session).

    env = contract.get("env")
    if env is not None:
        if not isinstance(env, dict):
            return False, "env must be an object of NAME -> value pairs"
        for env_key in env:
            if not (isinstance(env_key, str) and ENV_KEY_RE.fullmatch(env_key)):
                return (
                    False,
                    f"env key {env_key!r} is not a valid shell identifier — "
                    "env keys must match ^[A-Za-z_][A-Za-z0-9_]*$ (the startup "
                    "script exports them as `export KEY=value`)",
                )

    container_image = contract.get("container_image")
    registry_auth = contract.get("container_registry_auth")
    if registry_auth is not None and container_image is None:
        return False, "container_registry_auth requires container_image"
    if container_image is not None:
        if not isinstance(container_image, str) or not container_image.strip():
            return False, "container_image must be a non-empty string URI when specified"
        if registry_auth is not None:
            if not isinstance(registry_auth, dict):
                return False, "container_registry_auth must be an object with server/username/password"
            for req_key in ("server", "username", "password"):
                val = registry_auth.get(req_key)
                if val is not None and not isinstance(val, str):
                    return False, f"container_registry_auth.{req_key} must be a string"

    runner_target = contract.get("runner_target")
    if runner_target is not None:
        # Operator/agent pin of the job to a specific enrolled runner.
        # Shape: {runner_id: uuid-string, strict: bool=false}. Enrollment
        # (runner exists AND belongs to the launching project) is a DB check —
        # see validate_compute_runner_target_enrollment; this validator only
        # rejects malformed shapes.
        if not isinstance(runner_target, dict):
            return (
                False,
                "runner_target must be an object of the form "
                "{runner_id: <uuid>, strict: <bool>}",
            )
        rt_runner_id = runner_target.get("runner_id")
        if not (isinstance(rt_runner_id, str) and rt_runner_id.strip()):
            return (
                False,
                "runner_target.runner_id is required and must be a non-empty string",
            )
        try:
            uuid.UUID(rt_runner_id.strip())
        except ValueError:
            return (
                False,
                f"runner_target.runner_id {rt_runner_id!r} is not a valid UUID",
            )
        rt_strict = runner_target.get("strict")
        if rt_strict is not None and not isinstance(rt_strict, bool):
            return False, "runner_target.strict must be a boolean"

    if require_smoke:
        # Author-attested smoke is now ADVISORY ONLY.  The platform-executed
        # pre-flight smoke (capability `platform-preflight-smoke`) runs the
        # command before provisioning the full VM and is the binding gate — it
        # runs CONTAINERIZED on the same substrate class (docker pull + docker
        # run of the declared container_image) when container_image is set AND
        # Docker is present on the orchestrator, and degrades to a bare-host
        # bash -lc smoke otherwise (no container_image, or Docker absent).  A
        # missing or self-reported-failing author smoke_sample is logged here for
        # observability but does NOT reject the contract.
        smoke = contract.get("smoke_sample")
        if not smoke or not isinstance(smoke, dict):
            logger.warning(
                "compute_experiment contract has no author smoke_sample — "
                "platform-preflight-smoke will gate at launch"
            )
        elif smoke.get("unsmokable"):
            reason = str(smoke.get("unsmokable_reason") or "cannot be smoked cheaply")
            logger.warning(
                "compute_experiment author smoke_sample marked un-smokable: %s — "
                "platform-preflight-smoke will gate at launch",
                reason,
            )
        elif not smoke.get("startup_ok"):
            error_excerpt = str(smoke.get("error_excerpt") or "unknown error")[:200]
            logger.warning(
                "compute_experiment author smoke_sample reports startup failure: %s — "
                "platform-preflight-smoke will gate at launch",
                error_excerpt,
            )
        elif (
            contract.get("success_criterion") or contract.get("success_criteria")
        ) and not smoke.get("metric_captured"):
            logger.warning(
                "compute_experiment author smoke_sample did not capture declared metric — "
                "platform-preflight-smoke will gate at launch"
            )

    return True, ""


async def validate_compute_runner_target_enrollment(
    session: AsyncSession,
    project_id: Any,
    runner_target: dict[str, Any],
) -> dict[str, Any] | None:
    """Check a shape-valid ``runner_target`` against the project's runner allow-list.

    Returns ``None`` when the pinned runner exists AND is on ``project_id``'s
    allow-list (enrollment project or a secondary binding), else a machine-readable
    422 detail dict::

        {"error": "runner_target_invalid", "reason_code": "not_found"|"not_enrolled",
         "runner_id": "<id>", "message": "..."}

    Liveness is deliberately NOT checked here — an offline-but-allowed runner
    is a valid authoring target (a soft pin falls through at dispatch; a strict
    pin holds with an explicit reason until the runner comes back). A runner not
    on the project's allow-list is rejected at authoring time so a typo'd or stale
    id fails the launch with a clear 422 instead of silently never dispatching.
    """
    from app.models import LocalRunner  # local import keeps this module neutral

    runner_id = str(runner_target.get("runner_id") or "").strip()
    runner = await session.get(LocalRunner, uuid.UUID(runner_id))
    if runner is None:
        return {
            "error": "runner_target_invalid",
            "reason_code": "not_found",
            "runner_id": runner_id,
            "message": (
                f"runner_target.runner_id {runner_id} does not match any "
                "registered runner"
            ),
        }
    from app.runner_project_binding import allowed_project_ids

    if project_id not in await allowed_project_ids(session, runner):
        return {
            "error": "runner_target_invalid",
            "reason_code": "not_enrolled",
            "runner_id": runner_id,
            "message": (
                f"runner {runner_id} is not on this project's allow-list — "
                "runner_target must name one of the project's runners"
            ),
        }
    return None


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


async def resolve_compute_spend_direction(
    session: "AsyncSession",
    *,
    project_id: Any,
    context: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Return the optional direction selected for a compute spend decision.

    The lookup deliberately selects ``Project.directions`` as a scalar instead
    of traversing a possibly partial ``Project`` instance. Compute launches
    are reached from both agent and dispatcher sessions, some of which use
    ``raiseload=True`` while assembling a task. A direction is advisory to
    threshold selection, so an absent, stale, or malformed id must simply use
    the global threshold rather than creating another launch path or failing
    an otherwise valid bare compute job.
    """
    raw_direction_id = context.get("direction_id") if isinstance(context, dict) else None
    direction_id = str(raw_direction_id or "").strip()
    if not direction_id or project_id is None:
        return None

    from sqlmodel import select

    from app.models import Project

    result = await session.execute(
        select(Project.directions).where(Project.id == project_id)
    )
    directions = result.scalar_one_or_none()
    if not isinstance(directions, list):
        return None
    return next(
        (
            candidate
            for candidate in directions
            if isinstance(candidate, dict)
            and str(candidate.get("id") or "") == direction_id
        ),
        None,
    )


def resolve_compute_spend_approval(
    *,
    runtime_config: dict[str, Any],
    direction: dict[str, Any] | None,
    spend_cap_usd: float,
) -> tuple[bool, float]:
    """Compatibility wrapper around :func:`resolve_compute_spend_decision`.

    New launch paths should retain the full decision rather than discarding the
    execution-mode and reason information.  Keeping this small wrapper avoids
    changing unrelated callers while the authoring surfaces move over.
    """
    decision = resolve_compute_spend_decision(
        runtime_config=runtime_config,
        direction=direction,
        spend_cap_usd=spend_cap_usd,
    )
    return decision.requires_monetary_approval, decision.threshold_usd


@dataclass(frozen=True)
class ComputeSpendDecision:
    """The billable-risk decision for one normalized compute launch.

    ``spend_cap_usd`` remains a useful runtime safety cap even for a local job,
    but it is not a prediction of money Blueprint will spend.  The decision
    keeps that distinction explicit so all authoring surfaces, final job
    creation, and previews use identical approval semantics.
    """

    execution_mode: str
    billable_estimate_usd: float
    requires_monetary_approval: bool
    threshold_usd: float
    reason_code: str
    declared_spend_cap_usd: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "execution_mode": self.execution_mode,
            "billable_estimate_usd": self.billable_estimate_usd,
            "requires_monetary_approval": self.requires_monetary_approval,
            "threshold_usd": self.threshold_usd,
            "reason_code": self.reason_code,
            "declared_spend_cap_usd": self.declared_spend_cap_usd,
        }


def resolve_compute_spend_decision(
    *,
    runtime_config: dict[str, Any],
    direction: dict[str, Any] | None,
    spend_cap_usd: float,
    require_local_runner: bool = False,
    task_context: dict[str, Any] | None = None,
    launch_contract: dict[str, Any] | None = None,
) -> ComputeSpendDecision:
    """Classify a compute launch before applying monetary approval policy.

    Only an existing *guaranteed* local contract can be billed as zero:

    * project ``require_local_runner``;
    * an operator hard pin (``forced_runner_target_id``); or
    * a validated strict ``runner_target`` in the launch contract.

    A soft preferred runner is deliberately cloud-capable and therefore stays
    approval-gated.  In particular, a transient ``runner_target_id`` written
    after a runner claims a task is not treated as an authoring guarantee: doing
    so could let a cloud-capable task bypass approval merely because its local
    preference happened to win one dispatch race.
    """
    declared_cap = float(spend_cap_usd)
    threshold = resolve_compute_spend_threshold(runtime_config, direction)
    context = task_context if isinstance(task_context, dict) else {}
    contract = launch_contract if isinstance(launch_contract, dict) else {}
    runner_target = contract.get("runner_target")
    strict_target = isinstance(runner_target, dict) and runner_target.get("strict") is True
    forced_target = bool(str(context.get("forced_runner_target_id") or "").strip())

    if require_local_runner:
        reason_code = "project_requires_local_runner"
    elif forced_target:
        reason_code = "forced_runner_target"
    elif strict_target:
        reason_code = "strict_runner_target"
    else:
        return ComputeSpendDecision(
            execution_mode="cloud_possible",
            billable_estimate_usd=declared_cap,
            requires_monetary_approval=declared_cap > threshold,
            threshold_usd=threshold,
            reason_code="cloud_fallback_possible",
            declared_spend_cap_usd=declared_cap,
        )

    return ComputeSpendDecision(
        execution_mode="local_only",
        billable_estimate_usd=0.0,
        requires_monetary_approval=False,
        threshold_usd=threshold,
        reason_code=reason_code,
        declared_spend_cap_usd=declared_cap,
    )


def build_compute_contract_preview(
    contract: dict[str, Any],
    *,
    runtime_config: dict[str, Any],
    direction: dict[str, Any] | None = None,
    require_local_runner: bool = False,
    task_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a side-effect-free receipt using the production contract normalizer.

    This is deliberately a service, not a second validator.  Authoring APIs and
    the Strategic helper can expose it before dispatch, while job creation keeps
    applying the same ``_validate_compute_launch_contract`` normalizer as its
    binding guard.  The preview is migration-free: eventual execution manifests
    can persist this exact result once the integration migration lands.
    """
    normalized = dict(contract) if isinstance(contract, dict) else {}
    ok, reason = _validate_compute_launch_contract(normalized, require_smoke=False)
    if not ok:
        return {
            "ok": False,
            "normalized_contract": None,
            "failure": {
                "phase": "validation",
                "classification": "contract_invalid",
                "code": "compute_contract_invalid",
                "retryable": False,
                "hint": reason,
            },
        }

    try:
        declared_cap = float(normalized["spend_cap_usd"])
    except (KeyError, TypeError, ValueError):  # guarded above; defensive for callers
        return {
            "ok": False,
            "normalized_contract": None,
            "failure": {
                "phase": "validation",
                "classification": "contract_invalid",
                "code": "compute_spend_cap_invalid",
                "retryable": False,
                "hint": "spend_cap_usd must be a positive number",
            },
        }
    decision = resolve_compute_spend_decision(
        runtime_config=runtime_config,
        direction=direction,
        spend_cap_usd=declared_cap,
        require_local_runner=require_local_runner,
        task_context=task_context,
        launch_contract=normalized,
    )
    command = normalized.get("command")
    raw_command = command if isinstance(command, str) else ""
    readable_command = decode_generated_command(raw_command) or raw_command
    source_digest = hashlib.sha256(raw_command.encode("utf-8")).hexdigest()
    criterion = normalized.get("success_criterion")
    criteria = normalized.get("success_criteria")
    result_expectations = {
        "declared_metric": normalized.get("declared_metric"),
        "success_criterion": criterion if isinstance(criterion, dict) else None,
        "success_criteria": criteria if isinstance(criteria, list) else None,
        "artifact_required": normalized.get("require_durable_artifact") is True,
        "input_artifact_job_id": normalized.get("input_artifact_job_id"),
    }
    requirements = {
        "machine_type": normalized.get("machine_type"),
        "accelerator": normalized.get("accelerator"),
        "runner_target": normalized.get("runner_target"),
        "container_image": normalized.get("container_image"),
        "vram_gb": normalized.get("vram_gb"),
        "min_compute_capability": normalized.get("min_compute_capability"),
    }
    return {
        "ok": True,
        "contract_version": 1,
        "normalized_contract": normalized,
        "authored_command": readable_command,
        "effective_launcher": raw_command,
        "source_digest": source_digest,
        "result_expectations": result_expectations,
        "capability_requirements": requirements,
        "monetary_decision": decision.as_dict(),
        "preflight": {
            "status": "not_supported",
            "reason_code": "preview_does_not_allocate_execution_environment",
        },
        "failure": None,
    }
