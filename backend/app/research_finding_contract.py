"""Strict parsing and validation for research finding artifacts."""

from __future__ import annotations

import json
import math
import re
from typing import Any, Dict

from app.models import Task
from app.payload_schemas import normalize_acceptance_evidence
from app.research_finding_policy import (
    COMPUTE_EXPERIMENT_FINDING_TYPES,
    NON_SPAWNING_FINDING_TYPES,
)
from app.research_output_contract import (
    RESEARCH_OUTPUT_CONTRACT_VERSION,
    research_output_contract_version,
)


RESEARCH_IMPLEMENTATION_FINDING_TYPES = frozenset(
    {
        "feature",
        "improvement",
        "bug",
        "refactor",
        "perf",
        "security_audit",
        "qa",
        "deps",
        "docs",
        "migrate",
        "test",
    }
)

RESEARCH_ALLOWED_FINDING_TYPES = (
    RESEARCH_IMPLEMENTATION_FINDING_TYPES
    | frozenset({"research", "web_research", "design"})
    | COMPUTE_EXPERIMENT_FINDING_TYPES
    | NON_SPAWNING_FINDING_TYPES
)
RESEARCH_EMPIRICAL_FINDING_TYPES = (
    frozenset({"research", "web_research"}) | COMPUTE_EXPERIMENT_FINDING_TYPES
)
RESEARCH_WORK_CLASSES = frozenset({"exploratory", "confirmatory", "productization"})
RESEARCH_SCOPES = frozenset({"small", "medium", "large"})
RESEARCH_RISKS = frozenset({"low", "medium", "high"})
RESEARCH_PRIORITIES = frozenset({"high", "medium", "low"})
RESEARCH_ACTIONABILITY_GRADES = frozenset({"HIGH", "MEDIUM", "LOW"})
RESEARCH_VERIFICATION_BASES = frozenset(
    {"code_verified", "path_verified", "pattern_inferred", "deduced"}
)
RESEARCH_DIRECTION_TARGET_FINDING_TYPES = frozenset(
    {"direction_note", "direction_update"}
)

_ACCEPTANCE_EVIDENCE_FIELDS: dict[str, tuple[str, ...]] = {
    "named_test": ("test_path", "test_id"),
    "observable_behavior": ("description",),
    "downstream_artifact": ("artifact_id",),
    "visual": ("component", "viewport_width", "assertion_type"),
    "coverage_delta": ("threshold",),
}
_TRIVIAL_ACCEPTANCE_EVIDENCE = frozenset(
    {
        "",
        "...",
        "<...>",
        "test",
        "test_true",
        "test_truth",
        "test_always_true",
        "always_true",
        "assert true",
        "assert_true",
        "asserttrue",
        "assert true == true",
        "true",
        "pass",
        "test_pass",
        "noop",
        "test_noop",
        "placeholder",
        "test_placeholder",
        "dummy",
        "test_dummy",
        "todo",
        "tbd",
        "n/a",
        "na",
        "none",
    }
)
_COMPUTE_OPERATORS = frozenset({">=", "<=", ">", "<", "==", "!="})
_COMPUTE_METRIC_KEY_RE = re.compile(r"^[A-Za-z0-9_./-]+$")
_COMPUTE_ARM_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_COMPUTE_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_COMPUTE_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_PYTHON_LITERAL_ESCAPED_NEWLINE_RE = re.compile(
    r"\bpython(?:3(?:\.\d+)?)?\s+-c\s+[\"']\\n"
)


def parse_strict_research_findings(text: str) -> list[Any] | None:
    """Parse the exact JSON-array envelope emitted by contract-v2 agents.

    Legacy completion may salvage complete objects from truncated output. A v2
    producer already performs one bounded repair and posts the serialized parsed
    array, so accepting a prefix here could route only part of a task graph.
    """

    try:
        parsed = json.loads(text.strip())
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, list) else None


def meaningful_research_acceptance_string(value: Any, *, min_length: int = 1) -> bool:
    """Mirror the research agent's placeholder/string check exactly."""

    if not isinstance(value, str):
        return False
    normalized = " ".join(value.split()).lower()
    if len(normalized) < min_length or normalized in _TRIVIAL_ACCEPTANCE_EVIDENCE:
        return False
    return not (
        len(normalized) >= 3
        and normalized.startswith("<")
        and normalized.endswith(">")
        and ">" not in normalized[1:-1]
    )


def normalize_research_acceptance_evidence(raw: Any) -> dict[str, Any] | None:
    """Validate and canonicalize the strict producer evidence vocabulary."""

    if not isinstance(raw, dict):
        return None
    raw_kind = raw.get("kind")
    if not meaningful_research_acceptance_string(raw_kind):
        return None
    kind = raw_kind.strip().lower()
    fields = _ACCEPTANCE_EVIDENCE_FIELDS.get(kind)
    if fields is None:
        return None

    if kind == "named_test":
        if not isinstance(raw.get("test_path"), str):
            return None
        test_id = raw.get("test_id")
        if test_id is not None and not meaningful_research_acceptance_string(test_id):
            return None
    elif kind == "observable_behavior":
        if not meaningful_research_acceptance_string(
            raw.get("description"), min_length=10
        ):
            return None
    elif kind == "downstream_artifact":
        if not meaningful_research_acceptance_string(raw.get("artifact_id")):
            return None
    elif kind == "visual":
        if not meaningful_research_acceptance_string(raw.get("component")):
            return None
        viewport_width = raw.get("viewport_width")
        if (
            isinstance(viewport_width, bool)
            or not isinstance(viewport_width, (int, float))
            or not math.isfinite(viewport_width)
            or not float(viewport_width).is_integer()
            or not 1 <= viewport_width <= 4096
        ):
            return None
    elif kind == "coverage_delta":
        threshold = raw.get("threshold")
        if (
            isinstance(threshold, bool)
            or not isinstance(threshold, (int, float))
            or not math.isfinite(threshold)
            or not 0 <= threshold <= 100
        ):
            return None

    candidate = {"kind": kind}
    candidate.update({field: raw[field] for field in fields if field in raw})
    if kind == "visual" and not isinstance(candidate.get("assertion_type"), str):
        candidate.pop("assertion_type", None)
    payload_evidence, _ = normalize_acceptance_evidence(candidate)
    return payload_evidence


def normalize_research_finding_types(
    findings: list[Dict[str, Any]],
) -> tuple[bool, str | None]:
    """Canonicalize supported finding types and reject unknown routing input."""

    for finding in findings:
        normalized = str(finding.get("type") or "").strip().lower()
        if normalized not in RESEARCH_ALLOWED_FINDING_TYPES:
            return False, normalized or "<missing>"
        finding["type"] = normalized
    return True, None


def research_output_contract_enabled(task: Task) -> bool:
    """Return whether this attempt was claimed under the strict contract."""

    return (
        research_output_contract_version(task.context)
        >= RESEARCH_OUTPUT_CONTRACT_VERSION
    )


def prepare_research_metadata_targets(
    findings: list[Dict[str, Any]],
    *,
    parent_direction_id: Any,
    valid_direction_ids: set[str] | frozenset[str],
) -> None:
    """Stamp the one safe implicit metadata target before strict validation.

    A direction-owned research task has a server-authored parent direction in
    ``task.context``.  Preserve that historical convenience only when the id is
    still present in this project's live direction set.  Direction-free work,
    stale parent ids, and explicit cross-direction targets remain explicit and
    are validated by :func:`research_finding_contract_errors`.
    """

    normalized_parent = (
        parent_direction_id.strip()
        if isinstance(parent_direction_id, str)
        else ""
    )
    if not normalized_parent or normalized_parent not in valid_direction_ids:
        return
    for finding in findings:
        if finding.get("type") not in RESEARCH_DIRECTION_TARGET_FINDING_TYPES:
            continue
        target = finding.get("direction_id")
        if not isinstance(target, str) or not target.strip():
            finding["direction_id"] = normalized_parent


def _strict_compute_launch_error(contract: dict[str, Any]) -> str | None:
    """Mirror the JSON producer's fail-closed compute contract acceptance."""

    command = contract.get("command")
    spend_cap = contract.get("spend_cap_usd")
    deadline = contract.get("deadline_seconds")
    if not isinstance(command, str) or not command.strip():
        return "missing or empty command"
    if _PYTHON_LITERAL_ESCAPED_NEWLINE_RE.search(command):
        return "command starts python -c with a literal escaped newline"
    if (
        isinstance(spend_cap, bool)
        or not isinstance(spend_cap, (int, float))
        or not math.isfinite(spend_cap)
        or spend_cap <= 0
    ):
        return "spend_cap_usd must be a positive finite number"
    if (
        isinstance(deadline, bool)
        or not isinstance(deadline, (int, float))
        or not math.isfinite(deadline)
        or not float(deadline).is_integer()
        or deadline <= 0
    ):
        return "deadline_seconds must be a positive integer"

    work_class = contract.get("work_class")
    if work_class is not None and work_class not in RESEARCH_WORK_CLASSES:
        return "work_class is invalid"
    stop_condition = contract.get("stop_or_pivot_condition")
    if stop_condition is not None and (
        not isinstance(stop_condition, str)
        or not stop_condition.strip()
        or len(stop_condition.strip()) > 4_000
    ):
        return "stop_or_pivot_condition is invalid"

    declared_metric = contract.get("declared_metric")
    arms = contract.get("arms")
    if arms is not None:
        if not isinstance(arms, list) or not arms:
            return "arms must be a non-empty array"
        seen_names: set[str] = set()
        for index, arm in enumerate(arms):
            if not isinstance(arm, dict):
                return f"arms[{index}] must be an object"
            name = arm.get("name")
            arm_command = arm.get("command")
            if (
                not isinstance(name, str)
                or not name.strip()
                or not _COMPUTE_ARM_NAME_RE.fullmatch(name)
                or name in seen_names
            ):
                return f"arms[{index}].name is invalid or duplicated"
            seen_names.add(name)
            if (
                not isinstance(arm_command, str)
                or not arm_command.strip()
                or _PYTHON_LITERAL_ESCAPED_NEWLINE_RE.search(arm_command)
            ):
                return f"arms[{index}].command is invalid"
        if (
            not isinstance(declared_metric, str)
            or not declared_metric.strip()
            or not _COMPUTE_METRIC_KEY_RE.fullmatch(declared_metric)
        ):
            return "arms require a valid declared_metric"
    elif declared_metric is not None and (
        not isinstance(declared_metric, str)
        or not declared_metric.strip()
        or not _COMPUTE_METRIC_KEY_RE.fullmatch(declared_metric)
    ):
        return "declared_metric is invalid"

    success_criterion = contract.get("success_criterion")
    if success_criterion is not None:
        if not isinstance(success_criterion, dict):
            return "success_criterion must be an object"
        metric = success_criterion.get("metric")
        operator = success_criterion.get("operator")
        threshold = success_criterion.get("threshold")
        if (
            not isinstance(metric, str)
            or not metric.strip()
            or not isinstance(operator, str)
            or operator not in _COMPUTE_OPERATORS
            or isinstance(threshold, bool)
            or not isinstance(threshold, (int, float))
            or not math.isfinite(threshold)
        ):
            return (
                "success_criterion requires a metric, supported operator, "
                "and finite numeric threshold"
            )

    success_criteria = contract.get("success_criteria")
    if success_criteria is not None:
        if not isinstance(success_criteria, list) or not success_criteria:
            return "success_criteria must be a non-empty array"
        metric_names: set[str] = set()
        for index, criterion in enumerate(success_criteria):
            if not isinstance(criterion, dict):
                return f"success_criteria[{index}] must be an object"
            kind = str(criterion.get("kind") or "metric")
            if kind == "artifact_produced":
                continue
            metric = criterion.get("metric")
            threshold = criterion.get("threshold")
            if (
                not isinstance(metric, str)
                or not metric.strip()
                or not _COMPUTE_METRIC_KEY_RE.fullmatch(metric)
                or not isinstance(criterion.get("operator"), str)
                or criterion.get("operator") not in _COMPUTE_OPERATORS
                or isinstance(threshold, bool)
                or not isinstance(threshold, (int, float))
                or not math.isfinite(threshold)
            ):
                return f"success_criteria[{index}] is invalid"
            if kind == "metric":
                metric_names.add(metric.strip())
        if declared_metric is not None and declared_metric not in metric_names:
            return "declared_metric must name a success_criteria metric"
    elif (
        declared_metric is not None
        and isinstance(success_criterion, dict)
        and str(success_criterion.get("kind") or "metric") == "metric"
        and success_criterion.get("metric") != declared_metric
    ):
        return "declared_metric must match success_criterion.metric"

    no_progress = contract.get("no_progress_timeout_seconds")
    if no_progress is not None and (
        isinstance(no_progress, bool)
        or not isinstance(no_progress, (int, float))
        or not math.isfinite(no_progress)
        or not float(no_progress).is_integer()
        or no_progress <= 0
        or no_progress > deadline
    ):
        return "no_progress_timeout_seconds is invalid"
    input_job_id = contract.get("input_artifact_job_id")
    if input_job_id is not None:
        if not isinstance(input_job_id, str) or not _COMPUTE_UUID_RE.fullmatch(
            input_job_id
        ):
            return "input_artifact_job_id is invalid"
        if no_progress is None or not (
            isinstance(declared_metric, str) and declared_metric.strip()
        ):
            return "resumed compute requires timeout and declared_metric"

    env = contract.get("env")
    if env is not None and (
        not isinstance(env, dict)
        or any(
            not isinstance(key, str) or not _COMPUTE_ENV_KEY_RE.fullmatch(key)
            for key in env
        )
    ):
        return "env is invalid"
    container_image = contract.get("container_image")
    if container_image is not None:
        if not isinstance(container_image, str) or not container_image.strip():
            return "container_image is invalid"
        registry_auth = contract.get("container_registry_auth")
        if registry_auth is not None and (
            not isinstance(registry_auth, dict)
            or any(
                registry_auth.get(key) is not None
                and not isinstance(registry_auth.get(key), str)
                for key in ("server", "username", "password")
            )
        ):
            return "container_registry_auth is invalid"

    runner_target = contract.get("runner_target")
    if runner_target is not None and (
        not isinstance(runner_target, dict)
        or not isinstance(runner_target.get("runner_id"), str)
        or not _COMPUTE_UUID_RE.fullmatch(runner_target.get("runner_id", ""))
        or (
            runner_target.get("strict") is not None
            and not isinstance(runner_target.get("strict"), bool)
        )
    ):
        return "runner_target is invalid"
    return None


def research_finding_contract_errors(
    finding: Dict[str, Any],
    *,
    valid_direction_ids: set[str] | frozenset[str] | None = None,
) -> list[str]:
    """Return strict producer-contract violations for one normalized finding."""

    errors = [
        f"missing_{field}"
        for field in ("title", "prompt")
        if not isinstance(finding.get(field), str)
        or not str(finding.get(field) or "").strip()
    ]

    finding_type = finding.get("type")
    raw_actionability = finding.get("actionability_grade")
    actionability = (
        raw_actionability.strip().upper()
        if isinstance(raw_actionability, str)
        else ""
    )
    if actionability not in RESEARCH_ACTIONABILITY_GRADES:
        errors.append("missing_or_invalid_actionability_grade")
    else:
        finding["actionability_grade"] = actionability

    if finding_type in RESEARCH_DIRECTION_TARGET_FINDING_TYPES:
        raw_direction_id = finding.get("direction_id")
        direction_id = (
            raw_direction_id.strip()
            if isinstance(raw_direction_id, str)
            else ""
        )
        if not direction_id:
            errors.append("missing_or_invalid_direction_id")
        elif (
            valid_direction_ids is not None
            and direction_id not in valid_direction_ids
        ):
            errors.append("unknown_direction_id")
        else:
            finding["direction_id"] = direction_id

    if finding_type == "direction_note":
        description = finding.get("description")
        if (
            not meaningful_research_acceptance_string(description, min_length=10)
            or len(description.strip()) > 2_000
        ):
            errors.append("missing_or_invalid_direction_note_description")
        else:
            finding["description"] = description.strip()

    if finding_type == "direction_update":
        description = finding.get("description")
        if not meaningful_research_acceptance_string(description, min_length=10):
            errors.append("missing_or_invalid_direction_update_rationale")
        else:
            finding["description"] = description.strip()

        has_substantive_update = False
        if "suggested_text" in finding:
            suggested_text = finding.get("suggested_text")
            if not meaningful_research_acceptance_string(
                suggested_text, min_length=10
            ):
                errors.append("invalid_suggested_text")
            else:
                finding["suggested_text"] = suggested_text.strip()
                has_substantive_update = True
        if "suggested_priority" in finding:
            raw_priority = finding.get("suggested_priority")
            suggested_priority = (
                raw_priority.strip().lower()
                if isinstance(raw_priority, str)
                else ""
            )
            if suggested_priority not in RESEARCH_PRIORITIES:
                errors.append("invalid_suggested_priority")
            else:
                finding["suggested_priority"] = suggested_priority
                has_substantive_update = True
        if "suggested_paths" in finding:
            suggested_paths = finding.get("suggested_paths")
            if (
                not isinstance(suggested_paths, list)
                or not suggested_paths
                or any(
                    not isinstance(path, str) or not path.strip()
                    for path in suggested_paths
                )
            ):
                errors.append("invalid_suggested_paths")
            else:
                finding["suggested_paths"] = [
                    path.strip() for path in suggested_paths
                ]
                has_substantive_update = True
        if not has_substantive_update:
            errors.append("missing_substantive_direction_update")

    if (
        finding_type == "direction_proposal"
        and not meaningful_research_acceptance_string(
            finding.get("prompt"), min_length=10
        )
    ):
        errors.append("missing_or_invalid_direction_proposal_text")

    if finding_type == "design_doc_observation":
        description = finding.get("description")
        if not meaningful_research_acceptance_string(description, min_length=10):
            errors.append("missing_or_invalid_design_observation_description")
        else:
            finding["description"] = description.strip()
        section = finding.get("section")
        if not meaningful_research_acceptance_string(section):
            errors.append("missing_or_invalid_design_observation_section")
        else:
            finding["section"] = section.strip()

    if finding_type in RESEARCH_EMPIRICAL_FINDING_TYPES:
        raw_work_class = finding.get("work_class")
        work_class = (
            raw_work_class.strip().lower() if isinstance(raw_work_class, str) else ""
        )
        if work_class not in RESEARCH_WORK_CLASSES:
            errors.append("missing_or_invalid_work_class")
        else:
            finding["work_class"] = work_class
        stop_condition = finding.get("stop_or_pivot_condition")
        if (
            not isinstance(stop_condition, str)
            or not stop_condition.strip()
            or len(stop_condition.strip()) > 4_000
        ):
            errors.append("missing_or_invalid_stop_or_pivot_condition")
        else:
            finding["stop_or_pivot_condition"] = stop_condition.strip()

    if finding_type in COMPUTE_EXPERIMENT_FINDING_TYPES:
        # The producer must hand the server a runnable launch contract. Accepting
        # a green artifact and only deferring it later turns a failed producer
        # contract into a misleadingly successful research parent.
        from app.compute_experiment_contract import (
            _author_compute_launch_contract,
            _validate_compute_launch_contract,
        )

        launch_contract = _author_compute_launch_contract(finding)
        # The shared launch validator deliberately accepts a few legacy/coercible
        # shapes (for example ``"300"`` as a deadline and an arms-only contract
        # whose command is synthesized later). Contract-v2 output has a stricter
        # producer schema: mirror the TypeScript validator here so malformed JSON
        # cannot pass the agent, be accepted by completion, and only fail during
        # downstream compute routing.
        strict_launch_error = _strict_compute_launch_error(launch_contract)

        if strict_launch_error is not None:
            errors.append(f"invalid_compute_launch_contract:{strict_launch_error}")
        else:
            launch_ok, launch_reason = _validate_compute_launch_contract(
                launch_contract,
                require_smoke=False,
            )
            if not launch_ok:
                errors.append(f"invalid_compute_launch_contract:{launch_reason}")
        smoke_sample = launch_contract.get("smoke_sample")
        smoke_valid = False
        if isinstance(smoke_sample, dict):
            if smoke_sample.get("unsmokable") is True:
                smoke_valid = meaningful_research_acceptance_string(
                    smoke_sample.get("unsmokable_reason"), min_length=10
                )
            else:
                exit_code = smoke_sample.get("exit_code")
                timed_out = smoke_sample.get("timed_out")
                metric_captured = smoke_sample.get("metric_captured")
                smoke_valid = (
                    not isinstance(exit_code, bool)
                    and isinstance(exit_code, (int, float))
                    and math.isfinite(exit_code)
                    and float(exit_code).is_integer()
                    and smoke_sample.get("startup_ok") is True
                    and isinstance(metric_captured, bool)
                    and isinstance(timed_out, bool)
                    and isinstance(smoke_sample.get("error_excerpt"), str)
                    and (timed_out or metric_captured)
                )
        if not smoke_valid:
            errors.append("missing_or_invalid_smoke_sample")

    if finding_type not in RESEARCH_IMPLEMENTATION_FINDING_TYPES:
        return errors

    if not meaningful_research_acceptance_string(
        finding.get("description"), min_length=10
    ):
        errors.append("missing_or_invalid_description")
    for field, allowed in (
        ("scope", RESEARCH_SCOPES),
        ("risk", RESEARCH_RISKS),
        ("priority", RESEARCH_PRIORITIES),
    ):
        raw_value = finding.get(field)
        value = raw_value.strip().lower() if isinstance(raw_value, str) else ""
        if value not in allowed:
            errors.append(f"missing_or_invalid_{field}")
        else:
            finding[field] = value

    raw_verification = finding.get("verification_basis")
    verification = (
        raw_verification.strip().lower()
        if isinstance(raw_verification, str)
        else ""
    )
    if verification not in RESEARCH_VERIFICATION_BASES:
        errors.append("missing_or_invalid_verification_basis")
    else:
        finding["verification_basis"] = verification

    related_files = finding.get("related_files")
    if not isinstance(related_files, list) or not any(
        isinstance(path, str) and path.strip() for path in related_files
    ):
        errors.append("missing_or_invalid_related_files")

    claims = finding.get("claims")
    if not isinstance(claims, list) or not any(
        isinstance(claim, str) and claim.strip() for claim in claims
    ):
        errors.append("missing_claim")

    sources = finding.get("sources")
    if not isinstance(sources, list) or not any(
        isinstance(source, dict)
        and isinstance(source.get("ref"), str)
        and source["ref"].strip()
        for source in sources
    ):
        errors.append("missing_source_ref")

    payload_evidence = normalize_research_acceptance_evidence(
        finding.get("acceptance_evidence")
    )
    if payload_evidence is None:
        errors.append("missing_or_invalid_acceptance_evidence")
    else:
        finding["acceptance_evidence"] = payload_evidence
    return errors


def research_contract_rejection_receipts(
    findings: list[Dict[str, Any]],
    *,
    source: str,
    limit: int = 20,
    valid_direction_ids: set[str] | frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    """Build a bounded, non-prompt audit receipt for invalid findings."""

    receipts: list[dict[str, Any]] = []
    for index, finding in enumerate(findings):
        errors = research_finding_contract_errors(
            finding,
            valid_direction_ids=valid_direction_ids,
        )
        if not errors:
            continue
        receipts.append(
            {
                "index": index,
                "source": source,
                "type": str(finding.get("type") or "")[:80],
                "title": str(finding.get("title") or "")[:200],
                "errors": errors,
            }
        )
        if len(receipts) >= limit:
            break
    return receipts


# Established private names remain available to the completion module/tests.
_RESEARCH_IMPLEMENTATION_FINDING_TYPES = RESEARCH_IMPLEMENTATION_FINDING_TYPES
_RESEARCH_ALLOWED_FINDING_TYPES = RESEARCH_ALLOWED_FINDING_TYPES
_parse_strict_research_findings = parse_strict_research_findings
_meaningful_research_acceptance_string = meaningful_research_acceptance_string
_normalize_research_acceptance_evidence = normalize_research_acceptance_evidence
_normalize_research_finding_types = normalize_research_finding_types
_prepare_research_metadata_targets = prepare_research_metadata_targets
_research_output_contract_enabled = research_output_contract_enabled
_research_finding_contract_errors = research_finding_contract_errors
_research_contract_rejection_receipts = research_contract_rejection_receipts
