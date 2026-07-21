"""Typed, project-scoped capability declarations for Strategic Resident turns.

The worker receives capability identifiers, never ambient credentials or an
administrator shell.  The backend remains the execution owner and can enforce
the authority/receipt boundary before invoking an existing service.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CapabilityMode(str, Enum):
    inspect = "inspect"
    mutate = "mutate"


class CapabilityRisk(str, Enum):
    read_only = "read_only"
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class CapabilityParameterKind(str, Enum):
    """JSON wire type expected by a capability adapter."""

    uuid = "uuid"
    integer = "integer"
    string = "string"
    ref = "ref"
    enum = "enum"


class CapabilityParameterSpec(BaseModel):
    """Compact server-side schema for one declared parameter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=80)
    kind: CapabilityParameterKind
    min_length: int | None = Field(default=None, ge=0, le=8_192)
    max_length: int | None = Field(default=None, ge=1, le=8_192)
    minimum: int | None = None
    maximum: int | None = None
    choices: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _valid_bounds(self) -> CapabilityParameterSpec:
        if (
            self.min_length is not None
            and self.max_length is not None
            and self.min_length > self.max_length
        ):
            raise ValueError("parameter min_length cannot exceed max_length")
        if (
            self.minimum is not None
            and self.maximum is not None
            and self.minimum > self.maximum
        ):
            raise ValueError("parameter minimum cannot exceed maximum")
        if self.kind is CapabilityParameterKind.enum and not self.choices:
            raise ValueError("enum parameter requires choices")
        if self.kind is not CapabilityParameterKind.enum and self.choices:
            raise ValueError("only enum parameters may declare choices")
        return self


ApprovalPolicy = Literal["none", "project_policy", "explicit"]
IdempotencyPolicy = Literal["read_only", "operation_id", "resource_key"]
ReconciliationPolicy = Literal[
    "not_required",
    "read_after_write",
    "receipt_before_retry",
    "external_effect_probe",
]


class CapabilitySpec(BaseModel):
    """One stable capability contract advertised to a Resident worker."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    capability_id: str = Field(min_length=3, max_length=120)
    mode: CapabilityMode
    scope: Literal["project"] = "project"
    risk: CapabilityRisk
    service: str = Field(min_length=3, max_length=160)
    required_parameters: tuple[str, ...] = ()
    parameter_specs: tuple[CapabilityParameterSpec, ...] = ()
    evidence_preconditions: tuple[str, ...] = ()
    approval_policy: ApprovalPolicy
    idempotency: IdempotencyPolicy
    reconciliation: ReconciliationPolicy
    receipt_fields: tuple[str, ...]


_INSPECTION_RECEIPT = (
    "capability_id",
    "project_id",
    "source_id",
    "revision",
    "checksum",
    "observed_at",
)
_MUTATION_RECEIPT = (
    "operation_id",
    "capability_id",
    "project_id",
    "turn_task_id",
    "authority_generation",
    "expected_revisions",
    "observed_revisions_before",
    "observed_revisions_after",
    "status",
    "unknown_effects",
)


_PARAMETER_SPECS: dict[str, CapabilityParameterSpec] = {
    "task_id": CapabilityParameterSpec(name="task_id", kind=CapabilityParameterKind.uuid),
    "version_id": CapabilityParameterSpec(name="version_id", kind=CapabilityParameterKind.uuid),
    "runner_id": CapabilityParameterSpec(
        name="runner_id", kind=CapabilityParameterKind.uuid
    ),
    "pr_number": CapabilityParameterSpec(
        name="pr_number", kind=CapabilityParameterKind.integer, minimum=1, maximum=2**31 - 1
    ),
    "run_id": CapabilityParameterSpec(
        name="run_id", kind=CapabilityParameterKind.integer, minimum=1, maximum=2**63 - 1
    ),
    "priority": CapabilityParameterSpec(
        name="priority", kind=CapabilityParameterKind.integer, minimum=0, maximum=100
    ),
    "reason": CapabilityParameterSpec(
        name="reason", kind=CapabilityParameterKind.string, min_length=1, max_length=2_000
    ),
    "running_update": CapabilityParameterSpec(
        name="running_update",
        kind=CapabilityParameterKind.string,
        min_length=1,
        max_length=8_000,
    ),
    "title": CapabilityParameterSpec(
        name="title", kind=CapabilityParameterKind.string, min_length=1, max_length=500
    ),
    "prompt": CapabilityParameterSpec(
        name="prompt", kind=CapabilityParameterKind.string, min_length=1, max_length=8_000
    ),
    "audit_kind": CapabilityParameterSpec(
        name="audit_kind", kind=CapabilityParameterKind.string, min_length=1, max_length=80
    ),
    "ref": CapabilityParameterSpec(
        name="ref", kind=CapabilityParameterKind.ref, min_length=1, max_length=512
    ),
    "head": CapabilityParameterSpec(
        name="head", kind=CapabilityParameterKind.ref, min_length=1, max_length=512
    ),
    "base": CapabilityParameterSpec(
        name="base", kind=CapabilityParameterKind.ref, min_length=1, max_length=512
    ),
    "revision": CapabilityParameterSpec(
        name="revision", kind=CapabilityParameterKind.ref, min_length=1, max_length=512
    ),
    "expected_head_sha": CapabilityParameterSpec(
        name="expected_head_sha",
        kind=CapabilityParameterKind.ref,
        min_length=7,
        max_length=64,
    ),
    "head_sha": CapabilityParameterSpec(
        name="head_sha",
        kind=CapabilityParameterKind.ref,
        min_length=40,
        max_length=64,
    ),
}


def _parameter_specs(parameters: tuple[str, ...]) -> tuple[CapabilityParameterSpec, ...]:
    try:
        return tuple(_PARAMETER_SPECS[name] for name in parameters)
    except KeyError as exc:  # pragma: no cover - import-time declaration invariant
        raise RuntimeError(f"missing capability parameter schema: {exc.args[0]}") from exc


def _inspect(
    capability_id: str,
    service: str,
    *parameters: str,
) -> CapabilitySpec:
    return CapabilitySpec(
        capability_id=capability_id,
        mode=CapabilityMode.inspect,
        risk=CapabilityRisk.read_only,
        service=service,
        required_parameters=parameters,
        parameter_specs=_parameter_specs(parameters),
        approval_policy="none",
        idempotency="read_only",
        reconciliation="not_required",
        receipt_fields=_INSPECTION_RECEIPT,
    )


def _mutate(
    capability_id: str,
    service: str,
    risk: CapabilityRisk,
    parameters: tuple[str, ...],
    evidence: tuple[str, ...],
    *,
    approval: ApprovalPolicy = "project_policy",
    reconciliation: ReconciliationPolicy = "receipt_before_retry",
) -> CapabilitySpec:
    return CapabilitySpec(
        capability_id=capability_id,
        mode=CapabilityMode.mutate,
        risk=risk,
        service=service,
        required_parameters=parameters,
        parameter_specs=_parameter_specs(parameters),
        evidence_preconditions=evidence,
        approval_policy=approval,
        idempotency="operation_id",
        reconciliation=reconciliation,
        receipt_fields=_MUTATION_RECEIPT,
    )


RESIDENT_CAPABILITIES: tuple[CapabilitySpec, ...] = (
    _inspect("task.inspect", "resident_task_inspection", "task_id"),
    _inspect("task.lineage.inspect", "resident_task_inspection", "task_id"),
    _inspect("task.logs.inspect", "resident_task_inspection", "task_id"),
    _inspect("task.failure.inspect", "resident_task_inspection", "task_id"),
    _inspect("task.provider.inspect", "resident_task_inspection", "task_id"),
    _inspect("runner.inspect", "admin_runners", "runner_id"),
    _inspect("runner.list", "admin_runners"),
    _inspect("pipeline.inspect", "pipeline_health"),
    _inspect("qualification.inspect", "qualification_state_resolver"),
    _inspect("pr.inspect", "github_pulls", "pr_number"),
    _inspect("pr.diff.inspect", "github_pulls", "pr_number"),
    _inspect("pr.checks.inspect", "github_checks", "pr_number", "head_sha"),
    _inspect("pr.reviews.inspect", "github_pulls", "pr_number"),
    _inspect("pr.comments.inspect", "github", "pr_number"),
    _inspect("pr.mergeability.inspect", "github_pulls", "pr_number"),
    _inspect("branch.inspect", "github_refs", "ref"),
    _inspect("ci.run.inspect", "github_checks", "run_id"),
    _inspect("ci.logs.inspect", "github_checks", "run_id"),
    _inspect("deployment.inspect", "deploy_health"),
    # DB-only strategic-memory mutation: the first capability wired to the fenced
    # receipt executor (FencedMutationWriter.set_running_update). Low-risk,
    # contained side effect (one Project field), no external effect. Declares a
    # ("task",) evidence precondition (an evidence item of that KIND must be
    # supplied); note the envelope only checks kind-presence today, not that the
    # ref is a real trusted inspection — lost-update safety comes from the field
    # CAS (running_update_revision). Do NOT treat this as a trust boundary when
    # wiring higher-risk capabilities. See routers/agent_resident_capabilities.py.
    _mutate("strategy.running_update.set", "resident_strategy_memory", CapabilityRisk.low, ("running_update",), ("task",)),
    _mutate("task.dispatch", "orchestrator_runner_dispatch", CapabilityRisk.medium, ("task_id",), ("task", "runner_capacity")),
    _mutate("task.redirect", "task_lifecycle_requeue", CapabilityRisk.medium, ("task_id", "runner_id"), ("task", "runner")),
    _mutate("task.reprioritize", "task_lifecycle", CapabilityRisk.low, ("task_id", "priority"), ("task",)),
    _mutate("task.retry", "task_lifecycle_requeue", CapabilityRisk.medium, ("task_id",), ("task",)),
    _mutate("task.requeue", "task_lifecycle_requeue", CapabilityRisk.medium, ("task_id",), ("task",)),
    _mutate("task.unblock", "task_lifecycle_requeue", CapabilityRisk.medium, ("task_id",), ("task",)),
    _mutate("task.park", "task_lifecycle", CapabilityRisk.low, ("task_id", "reason"), ("task",)),
    _mutate("task.cancel", "task_lifecycle", CapabilityRisk.high, ("task_id", "reason"), ("task",)),
    _mutate("task.salvage", "task_lifecycle", CapabilityRisk.high, ("task_id",), ("task", "branch")),
    _mutate("task.cleanup", "task_lifecycle", CapabilityRisk.high, ("task_id",), ("task", "lineage")),
    _mutate("task.review_fix_loop", "review_pipeline", CapabilityRisk.medium, ("task_id",), ("task", "pr_checks")),
    # A fresh code change may originate directly from the frozen operator turn
    # and its revisioned ArtifactRefs; requiring an unrelated task inspection
    # creates fake provenance. Authority/context/receipt lineage remains exact.
    _mutate("task.code_change.create", "resident_managed_task_create", CapabilityRisk.medium, ("title", "prompt"), ()),
    _mutate("task.debug.create", "resident_managed_task_create", CapabilityRisk.medium, ("task_id", "title", "prompt"), ("task",)),
    _mutate("task.large_review.create", "resident_managed_task_create", CapabilityRisk.medium, ("title", "prompt", "pr_number"), ("pr_diff", "pr_check")),
    _mutate("task.ci_repair.create", "resident_managed_task_create", CapabilityRisk.high, ("title", "prompt", "pr_number"), ("pr_check", "ci_logs")),
    _mutate("pr.create", "github_pulls", CapabilityRisk.medium, ("head", "base", "title"), ("branch",), reconciliation="external_effect_probe"),
    _mutate("pr.update", "github_pulls", CapabilityRisk.medium, ("pr_number",), ("pr",), reconciliation="external_effect_probe"),
    _mutate("pr.merge", "github_pulls", CapabilityRisk.critical, ("pr_number", "expected_head_sha"), ("pr", "pr_diff", "pr_check"), approval="explicit", reconciliation="external_effect_probe"),
    _mutate("qualification.run", "qualification_triggers", CapabilityRisk.high, ("version_id",), ("qualification_state",), reconciliation="external_effect_probe"),
    _mutate("audit.run", "qualification_triggers", CapabilityRisk.high, ("version_id", "audit_kind"), ("qualification_state",), reconciliation="external_effect_probe"),
    _mutate("deployment.deploy", "deploy", CapabilityRisk.critical, ("revision",), ("qualification", "revision"), approval="explicit", reconciliation="external_effect_probe"),
    _mutate("deployment.redeploy", "deploy", CapabilityRisk.critical, ("revision",), ("deployment", "health"), approval="explicit", reconciliation="external_effect_probe"),
    _mutate("deployment.rollback", "deploy", CapabilityRisk.critical, ("revision",), ("deployment", "health", "verification"), approval="explicit", reconciliation="external_effect_probe"),
)

_BY_ID = {spec.capability_id: spec for spec in RESIDENT_CAPABILITIES}
if len(_BY_ID) != len(RESIDENT_CAPABILITIES):  # pragma: no cover - import invariant
    raise RuntimeError("duplicate Strategic Resident capability id")
for _spec in RESIDENT_CAPABILITIES:  # pragma: no branch - import invariant
    if tuple(item.name for item in _spec.parameter_specs) != _spec.required_parameters:
        raise RuntimeError(
            f"Strategic Resident capability parameter schema mismatch: {_spec.capability_id}"
        )

# One source of truth for what the Agent may actually invoke. An entry belongs
# here only when its route adapter exists and every declared evidence kind can
# be supplied by server-authored turn context or an inspection receipt.
INVOKABLE_CAPABILITY_IDS = frozenset(
    {
        "task.inspect",
        "task.lineage.inspect",
        "task.logs.inspect",
        "task.failure.inspect",
        "task.provider.inspect",
        "runner.inspect",
        "runner.list",
        "pr.inspect",
        "pr.diff.inspect",
        "pr.checks.inspect",
        "strategy.running_update.set",
        "task.reprioritize",
        "task.retry",
        "task.requeue",
        "task.unblock",
        "task.cancel",
        "task.code_change.create",
        "task.debug.create",
    }
)


def capability_for(capability_id: str) -> CapabilitySpec:
    try:
        return _BY_ID[capability_id]
    except KeyError as exc:
        raise ValueError(f"unknown Strategic Resident capability: {capability_id}") from exc


def _compact_parameter_spec(parameter: CapabilityParameterSpec) -> str | dict[str, Any]:
    result: dict[str, Any] = {"type": parameter.kind.value}
    for field, compact_field in (
        ("min_length", "min_len"),
        ("max_length", "max_len"),
        ("minimum", "min"),
        ("maximum", "max"),
    ):
        value = getattr(parameter, field)
        if value is not None:
            result[compact_field] = value
    if parameter.choices:
        result["choices"] = list(parameter.choices)
    if len(result) == 1:
        return parameter.kind.value
    return result


def compact_capability_catalog() -> list[dict[str, Any]]:
    """Latency-conscious discovery view with compact declared parameter schemas."""

    return [
        {
            "id": spec.capability_id,
            "mode": spec.mode.value,
            "risk": spec.risk.value,
            "parameter_specs": {
                parameter.name: _compact_parameter_spec(parameter)
                for parameter in spec.parameter_specs
            },
            "approval": spec.approval_policy,
            "availability": (
                "invoke"
                if spec.capability_id in INVOKABLE_CAPABILITY_IDS
                else "propose_only"
            ),
        }
        for spec in RESIDENT_CAPABILITIES
    ]


__all__ = [
    "INVOKABLE_CAPABILITY_IDS",
    "RESIDENT_CAPABILITIES",
    "CapabilityMode",
    "CapabilityParameterKind",
    "CapabilityParameterSpec",
    "CapabilityRisk",
    "CapabilitySpec",
    "capability_for",
    "compact_capability_catalog",
]
