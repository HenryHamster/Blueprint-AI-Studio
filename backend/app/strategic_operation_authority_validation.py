"""Validation contracts for Strategic Resident database mutation authority."""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Project,
    StrategicResidentCheckpoint,
    Task,
)


RESIDENT_ENGINE_OWNER = "strategic_resident"
RESIDENT_TURN_TASK_TYPE = "strategic_resident_turn"
_MUTATION_AUTHORIZED_CONTEXT_KEY = "_resident_mutation_authorized"
_INPUT_FROZEN_CONTEXT_KEY = "_resident_input_frozen"
_TASK_PROJECT_CONTEXT_KEY = "_resident_project_id"
_TASK_GENERATION_CONTEXT_KEY = "_resident_authority_generation"
_TASK_OWNER_CONTEXT_KEY = "_resident_engine_owner_observed"
_REQUEST_MAX_BYTES = 100_000
_REQUEST_MAX_DEPTH = 10
_REQUEST_NODE_MAX = 5_000
_MUTATION_FIELD_MAX_BYTES = 100_000
_MUTATION_PLAN_MAX_BYTES = 200_000
_MUTATION_MAX_DEPTH = 10
_MUTATION_NODE_MAX = 5_000
_SUPPORTED_EXPECTED_REVISIONS = frozenset(
    {
        "authority_generation",
        "active_turn_task_version",
        "current_checkpoint_id",
        "resident_checkpoint_version",
        "resident_checkpoint_hash",
        "context_hash",
        "running_update_revision",
        "vision_revision",
        "problem_model_revision",
        "design_doc_revision",
        "target_task_updated_at",
    }
)
_REQUIRED_AUTHORITY_REVISIONS = frozenset(
    {
        "authority_generation",
        "active_turn_task_version",
        "current_checkpoint_id",
        "resident_checkpoint_version",
        "resident_checkpoint_hash",
    }
)
_MUTATION_FIELD_REVISION_KEYS = {
    "running_update": "running_update_revision",
    "vision": "vision_revision",
    "problem_model": "problem_model_revision",
    "design_doc": "design_doc_revision",
}
_TERMINAL_RECEIPT_STATUSES = frozenset({"applied", "reconciled", "failed"})


class StrategicAuthorityError(RuntimeError):
    code = "strategic_authority_error"

    def __init__(self, message: str, **metadata: Any):
        super().__init__(message)
        self.metadata = metadata


class FencedOperationUsageError(StrategicAuthorityError):
    code = "fenced_operation_usage_error"


class AuthorityGenerationMismatch(StrategicAuthorityError):
    code = "authority_generation_mismatch"


class ResidentEngineOwnerMismatch(StrategicAuthorityError):
    code = "resident_engine_owner_mismatch"


class InactiveTurnAuthority(StrategicAuthorityError):
    code = "inactive_turn_authority"


class AuthorityBoundaryMutation(StrategicAuthorityError):
    code = "authority_boundary_mutation"


class OperationIdentityCollision(StrategicAuthorityError):
    code = "operation_identity_collision"


class ExpectedRevisionMismatch(StrategicAuthorityError):
    code = "expected_revision_mismatch"


class UnsupportedRevisionKey(StrategicAuthorityError):
    code = "unsupported_revision_key"


class OperationPayloadTooLarge(StrategicAuthorityError):
    code = "operation_payload_too_large"


class OperationResultTooLarge(StrategicAuthorityError):
    code = "operation_result_too_large"


class MutationPlanTooLarge(StrategicAuthorityError):
    code = "mutation_plan_too_large"


class BoundedOperationResult(BaseModel):
    reply: str | None = None
    facts: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    operations: list[dict[str, Any]] = Field(default_factory=list)
    receipts: list[dict[str, Any]] = Field(default_factory=list)
    unknown_effects: list[dict[str, Any]] = Field(default_factory=list)
    strategic_implications: list[str | dict[str, Any]] = Field(default_factory=list)
    proposed_memory: list[dict[str, Any]] = Field(default_factory=list)
    follow_up: dict[str, Any] | None = None
    required_attention: list[dict[str, Any]] = Field(default_factory=list)


@dataclass(frozen=True)
class FencedOperationRequest:
    project_id: uuid.UUID
    turn_task_id: uuid.UUID
    authority_generation: int
    operation_id: str
    operation_kind: str
    capability: str
    risk: str
    expected_revisions: Mapping[str, Any]
    request_payload: Mapping[str, Any]
    # Optional: a non-turn task (same project) this operation mutates through the
    # bounded ``FencedTaskWriter``, CAS'd on ``expected_revisions.target_task_updated_at``
    # (``updated_at`` advances on every write; ``version`` is a forensic counter
    # only). ``None`` keeps the Project-field-only mutation path unchanged.
    target_task_id: uuid.UUID | None = None


@dataclass
class FencedMutationWriter:
    """Detached plan for bounded strategic Project-field mutations."""

    __changes: dict[str, Any]

    def set_running_update(self, value: str | None) -> None:
        if value is not None and not isinstance(value, str):
            raise FencedOperationUsageError("running_update must be string or null")
        if value is not None and len(value.encode("utf-8")) > _MUTATION_FIELD_MAX_BYTES:
            raise MutationPlanTooLarge("running_update exceeds mutation byte limit")
        self.__changes["running_update"] = value

    def set_vision(self, value: Mapping[str, Any]) -> None:
        self.__changes["vision"] = _validated_mutation_mapping("vision", value)

    def set_problem_model(self, value: Mapping[str, Any]) -> None:
        self.__changes["problem_model"] = _validated_mutation_mapping(
            "problem_model", value
        )

    def set_design_doc(self, value: Mapping[str, Any]) -> None:
        self.__changes["design_doc"] = _validated_mutation_mapping("design_doc", value)

    def _validated_changes(self) -> dict[str, Any]:
        allowed = {"running_update", "vision", "problem_model", "design_doc"}
        unknown = set(self.__changes) - allowed
        if unknown:
            raise FencedOperationUsageError(
                f"mutation plan contains unsupported fields: {sorted(unknown)}"
            )
        node_budget = [_MUTATION_NODE_MAX]
        validated: dict[str, Any] = {}
        for field, value in self.__changes.items():
            if field == "running_update":
                if value is not None and not isinstance(value, str):
                    raise FencedOperationUsageError(
                        "running_update must be string or null"
                    )
                if (
                    value is not None
                    and len(value.encode("utf-8")) > _MUTATION_FIELD_MAX_BYTES
                ):
                    raise MutationPlanTooLarge(
                        "running_update exceeds mutation byte limit"
                    )
                node_budget[0] -= 1
                validated[field] = value
            else:
                validated[field] = _validated_mutation_mapping(
                    field,
                    value,
                    node_budget=node_budget,
                )
        if len(_canonical_json(validated)) > _MUTATION_PLAN_MAX_BYTES:
            raise MutationPlanTooLarge("combined mutation plan exceeds byte limit")
        return validated

    def _apply(
        self,
        project: Project,
        *,
        expected_revisions: Mapping[str, Any],
    ) -> None:
        validated = self._validated_changes()
        _require_expected_revision_keys(
            expected_revisions,
            {_MUTATION_FIELD_REVISION_KEYS[field] for field in validated},
            reason="mutation plan",
        )
        for field, value in validated.items():
            setattr(project, field, deepcopy(value))


_TASK_PRIORITY_MIN = 0
_TASK_PRIORITY_MAX = 100


@dataclass
class FencedTaskWriter:
    """Detached plan for a bounded mutation of one non-turn target Task.

    Like ``FencedMutationWriter``, the callback only records intent here; the
    executor applies it to the locked target task after the authority-identity
    snapshot check, so the callback can never touch any row directly.
    """

    __changes: dict[str, Any]

    def set_priority(self, value: int) -> None:
        if type(value) is not int:
            raise FencedOperationUsageError("priority must be an integer")
        if not _TASK_PRIORITY_MIN <= value <= _TASK_PRIORITY_MAX:
            raise FencedOperationUsageError(
                f"priority must be within [{_TASK_PRIORITY_MIN}, {_TASK_PRIORITY_MAX}]"
            )
        self.__changes["priority"] = value

    def _validated_changes(self) -> dict[str, Any]:
        allowed = {"priority"}
        unknown = set(self.__changes) - allowed
        if unknown:
            raise FencedOperationUsageError(
                f"task mutation plan contains unsupported fields: {sorted(unknown)}"
            )
        if not self.__changes:
            raise FencedOperationUsageError("task mutation plan is empty")
        return dict(self.__changes)

    def has_changes(self) -> bool:
        return bool(self.__changes)

    def _apply(self, task: Task) -> None:
        for field, value in self._validated_changes().items():
            setattr(task, field, value)


@dataclass(frozen=True)
class FencedOperationContext:
    project_id: uuid.UUID
    turn_task_id: uuid.UUID
    receipt_id: uuid.UUID
    operation_id: str
    authority_generation: int
    request_payload: Mapping[str, Any]
    observed_revisions: Mapping[str, Any]
    writer: FencedMutationWriter
    managed_task_writer: Any
    # Present only when the request declares a ``target_task_id``.
    task_writer: FencedTaskWriter | None = None


MutationCallback = Callable[
    [FencedOperationContext],
    BoundedOperationResult | Mapping[str, Any],
]


def _enum_value(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str(raw)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


def _validated_request_payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise FencedOperationUsageError("request_payload must be an object")
    node_budget = [_REQUEST_NODE_MAX]
    ancestors: set[int] = set()

    def copy_value(item: Any, depth: int) -> Any:
        node_budget[0] -= 1
        if node_budget[0] < 0:
            raise OperationPayloadTooLarge("operation request exceeds the node budget")
        if depth >= _REQUEST_MAX_DEPTH:
            raise OperationPayloadTooLarge("operation request exceeds the depth limit")
        if item is None or isinstance(item, (str, bool, int)):
            return item
        if isinstance(item, float):
            if not math.isfinite(item):
                raise FencedOperationUsageError(
                    "request_payload contains a non-finite number"
                )
            return item
        if isinstance(item, Mapping):
            identity = id(item)
            if identity in ancestors:
                raise OperationPayloadTooLarge(
                    "operation request contains a reference cycle"
                )
            ancestors.add(identity)
            try:
                result: dict[str, Any] = {}
                for raw_key, child in item.items():
                    if (
                        not isinstance(raw_key, str)
                        or not raw_key
                        or len(raw_key) > 256
                    ):
                        raise FencedOperationUsageError(
                            "request_payload keys must be non-empty bounded strings"
                        )
                    result[raw_key] = copy_value(child, depth + 1)
                return result
            finally:
                ancestors.remove(identity)
        if isinstance(item, (list, tuple)):
            identity = id(item)
            if identity in ancestors:
                raise OperationPayloadTooLarge(
                    "operation request contains a reference cycle"
                )
            ancestors.add(identity)
            try:
                return [copy_value(child, depth + 1) for child in item]
            finally:
                ancestors.remove(identity)
        raise FencedOperationUsageError(
            "request_payload contains a non-JSON value",
            value_type=type(item).__name__,
        )

    return copy_value(value, 0)


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType(
            {key: _freeze_json(child) for key, child in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_json(child) for child in value)
    return value


def _request_fingerprint(
    request: FencedOperationRequest,
    *,
    request_payload: Mapping[str, Any],
) -> str:
    operation_id = str(request.operation_id or "").strip()
    operation_kind = str(request.operation_kind or "").strip()
    capability = str(request.capability or "").strip()
    risk = str(request.risk or "").strip().lower()
    if not operation_id or len(operation_id) > 128:
        raise FencedOperationUsageError("operation_id must be 1-128 characters")
    if not capability or len(capability) > 64:
        raise FencedOperationUsageError("capability must be 1-64 characters")
    if not operation_kind or len(operation_kind) > 64:
        raise FencedOperationUsageError("operation_kind must be 1-64 characters")
    if risk not in {"low", "medium", "high", "critical"}:
        raise FencedOperationUsageError(
            "risk must be low, medium, high, or critical"
        )
    if int(request.authority_generation) < 1:
        raise FencedOperationUsageError("authority_generation must be positive")
    expected_revisions = dict(request.expected_revisions or {})
    if len(expected_revisions) > 50:
        raise FencedOperationUsageError("at most 50 expected revisions are allowed")
    for key, value in expected_revisions.items():
        if not isinstance(key, str) or not key or len(key) > 160:
            raise FencedOperationUsageError(
                "expected revision keys must be non-empty bounded strings"
            )
        if isinstance(value, bool) or not isinstance(value, (int, str, type(None))):
            raise FencedOperationUsageError(
                "expected revision values must be int, string, or null",
                revision_key=key,
            )
        if isinstance(value, str) and len(value) > 512:
            raise FencedOperationUsageError(
                "expected revision string exceeds 512 characters",
                revision_key=key,
            )
    encoded = _canonical_json(
        {
            "project_id": str(request.project_id),
            "turn_task_id": str(request.turn_task_id),
            "authority_generation": int(request.authority_generation),
            "operation_id": operation_id,
            "operation_kind": operation_kind,
            "capability": capability,
            "risk": risk,
            "expected_revisions": expected_revisions,
            "request_payload": request_payload,
            # Bind the mutation target explicitly so an operation_id replay with a
            # different target task collides (409) rather than being under-specified,
            # even for a future capability whose params don't echo the target id.
            "target_task_id": (
                str(request.target_task_id)
                if request.target_task_id is not None
                else None
            ),
        }
    )
    if len(encoded) > _REQUEST_MAX_BYTES:
        raise OperationPayloadTooLarge(
            f"operation request exceeds {_REQUEST_MAX_BYTES} bytes",
            size_bytes=len(encoded),
        )
    return hashlib.sha256(encoded).hexdigest()


def _require_expected_revision_keys(
    expected: Mapping[str, Any],
    required: set[str] | frozenset[str],
    *,
    reason: str,
) -> None:
    supplied = set(dict(expected or {}))
    missing = sorted(set(required) - supplied)
    if missing:
        raise ExpectedRevisionMismatch(
            f"{reason} is missing required expected revisions: {', '.join(missing)}",
            missing_revision_keys=missing,
        )


def _revision_from_mapping(value: Any) -> Any:
    return value.get("revision") if isinstance(value, dict) else None


async def _observed_revisions(
    session: AsyncSession,
    project: Project,
    resident: Any,
    task: Task,
    expected: Mapping[str, Any],
) -> dict[str, Any]:
    checkpoint_id = getattr(resident, "current_checkpoint_id", None)
    if checkpoint_id is None:
        raise InactiveTurnAuthority("Strategic Resident has no current checkpoint")
    checkpoint_row = (
        await session.execute(
            select(
                StrategicResidentCheckpoint.version,
                StrategicResidentCheckpoint.content_hash,
            ).where(
                StrategicResidentCheckpoint.id == checkpoint_id,
                StrategicResidentCheckpoint.project_id == project.id,
            )
        )
    ).one_or_none()
    if checkpoint_row is None:
        raise InactiveTurnAuthority("Strategic Resident current checkpoint is missing")
    checkpoint_version = checkpoint_row.version
    checkpoint_hash = checkpoint_row.content_hash
    observed = {
        "authority_generation": int(getattr(resident, "authority_generation", 0)),
        "active_turn_task_version": int(getattr(task, "version", 0)),
        "current_checkpoint_id": str(checkpoint_id) if checkpoint_id else None,
        "resident_checkpoint_version": int(checkpoint_version),
        "resident_checkpoint_hash": str(checkpoint_hash),
        "running_update_revision": hashlib.sha256(
            (project.running_update or "").encode("utf-8")
        ).hexdigest(),
        "vision_revision": _revision_from_mapping(project.vision),
        "problem_model_revision": _revision_from_mapping(project.problem_model),
        "design_doc_revision": _revision_from_mapping(project.design_doc),
    }
    if "context_hash" in expected:
        # Deferred to avoid a module-load cycle: context reconstruction imports
        # the authority lineage validators above. The hash is derived under the
        # already-held Project/Resident/Task locks, never copied from the caller.
        from app.strategic_resident_context import (
            reconstruct_authoritative_turn_input,
        )

        provider_input = await reconstruct_authoritative_turn_input(
            session,
            project_id=project.id,
            turn_task_id=task.id,
        )
        observed["context_hash"] = str(
            (provider_input.get("lineage") or {}).get("context_hash") or ""
        )
    artifact_keys = [
        key for key in expected if isinstance(key, str) and key.startswith("artifact:")
    ]
    if artifact_keys:
        raise UnsupportedRevisionKey(
            "artifact revisions are unavailable until ArtifactRef lands",
            revision_key=artifact_keys[0],
        )
    return observed


def _validate_expected_revisions(
    expected: Mapping[str, Any],
    observed: Mapping[str, Any],
) -> None:
    for key, expected_value in dict(expected or {}).items():
        if not isinstance(key, str) or (
            key not in _SUPPORTED_EXPECTED_REVISIONS and not key.startswith("artifact:")
        ):
            raise UnsupportedRevisionKey(
                f"unsupported expected revision key: {key}",
                revision_key=key,
            )
        observed_value = observed.get(key)
        if observed_value != expected_value:
            raise ExpectedRevisionMismatch(
                f"expected revision mismatch for {key}",
                revision_key=key,
                expected=expected_value,
                observed=observed_value,
            )


def _validated_mutation_mapping(
    field: str,
    value: Mapping[str, Any],
    *,
    node_budget: list[int] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise FencedOperationUsageError(f"{field} must be an object")
    if node_budget is None:
        node_budget = [_MUTATION_NODE_MAX]
    ancestors: set[int] = set()

    def copy_value(item: Any, depth: int) -> Any:
        node_budget[0] -= 1
        if node_budget[0] < 0:
            raise MutationPlanTooLarge(f"{field} exceeds mutation node limit")
        if depth >= _MUTATION_MAX_DEPTH:
            raise MutationPlanTooLarge(f"{field} exceeds mutation depth limit")
        if item is None or isinstance(item, (str, int, float, bool)):
            return item
        if isinstance(item, Mapping):
            identity = id(item)
            if identity in ancestors:
                raise MutationPlanTooLarge(f"{field} contains a reference cycle")
            ancestors.add(identity)
            try:
                result: dict[str, Any] = {}
                for raw_key, child in item.items():
                    if (
                        not isinstance(raw_key, str)
                        or not raw_key
                        or len(raw_key) > 256
                    ):
                        raise FencedOperationUsageError(
                            f"{field} keys must be non-empty bounded strings"
                        )
                    result[raw_key] = copy_value(child, depth + 1)
                return result
            finally:
                ancestors.remove(identity)
        if isinstance(item, (list, tuple)):
            identity = id(item)
            if identity in ancestors:
                raise MutationPlanTooLarge(f"{field} contains a reference cycle")
            ancestors.add(identity)
            try:
                return [copy_value(child, depth + 1) for child in item]
            finally:
                ancestors.remove(identity)
        raise FencedOperationUsageError(
            f"{field} contains a non-JSON value: {type(item).__name__}"
        )

    copied = copy_value(value, 0)
    encoded = _canonical_json(copied)
    if len(encoded) > _MUTATION_FIELD_MAX_BYTES:
        raise MutationPlanTooLarge(f"{field} exceeds mutation byte limit")
    return copied


def _task_is_mutation_authorized(task: Task) -> bool:
    context = task.context if isinstance(task.context, dict) else {}
    return bool(context.get(_MUTATION_AUTHORIZED_CONTEXT_KEY) is True)


def assert_locked_turn_mutation_authority(
    resident: Any,
    turn_task: Any,
    request: FencedOperationRequest,
) -> int:
    """Fail closed unless the locked rows still authorize this operation.

    Pure check sequence over already-locked ``resident``/``turn_task`` rows,
    shared by the DB-only and external-effect executors so both fence identically:
    Resident owns the engine, generation matches, the turn is this Resident's
    active in-progress mutation-authorized ``strategic_resident_turn`` in the
    request's project. Returns the observed authority generation.
    """

    from app.models import TaskStatus

    if _enum_value(getattr(resident, "engine_owner", "")) != RESIDENT_ENGINE_OWNER:
        raise ResidentEngineOwnerMismatch(
            "Strategic Resident does not own the project engine",
            observed_owner=_enum_value(getattr(resident, "engine_owner", "")),
        )
    observed_generation = int(getattr(resident, "authority_generation", 0))
    if observed_generation != int(request.authority_generation):
        raise AuthorityGenerationMismatch(
            "Resident authority generation is stale",
            expected=request.authority_generation,
            observed=observed_generation,
        )
    if turn_task is None:
        raise InactiveTurnAuthority("active turn task not found")
    if turn_task.project_id != request.project_id:
        raise InactiveTurnAuthority("turn task belongs to another project")
    if getattr(resident, "active_turn_task_id", None) != request.turn_task_id:
        raise InactiveTurnAuthority("turn task is not the Resident active turn")
    if (
        str((turn_task.first_payload or {}).get("type") or "")
        != RESIDENT_TURN_TASK_TYPE
    ):
        raise InactiveTurnAuthority("task is not a strategic_resident_turn")
    if _enum_value(turn_task.status) != _enum_value(TaskStatus.IN_PROGRESS):
        raise InactiveTurnAuthority("turn task is not in progress")
    if not _task_is_mutation_authorized(turn_task):
        raise InactiveTurnAuthority("turn task is not mutation-authorized")
    return observed_generation


def _validate_frozen_turn_lineage_values(
    payload: Mapping[str, Any],
    context: Mapping[str, Any],
    *,
    project_id: uuid.UUID,
    engine_owner: str,
    authority_generation: int,
    checkpoint_version: int | None,
    checkpoint_hash: str | None,
) -> None:
    expected_project_id = str(project_id)
    if str(payload.get("resident_id") or "") != expected_project_id:
        raise InactiveTurnAuthority(
            "turn payload belongs to a different Resident project"
        )
    if str(context.get(_TASK_PROJECT_CONTEXT_KEY) or "") != expected_project_id:
        raise InactiveTurnAuthority(
            "turn context belongs to a different Resident project"
        )
    if context.get(_INPUT_FROZEN_CONTEXT_KEY) is not True:
        raise InactiveTurnAuthority("turn input is not frozen")

    try:
        payload_generation = int(payload.get("authority_generation"))
        context_generation = int(context.get(_TASK_GENERATION_CONTEXT_KEY))
    except (TypeError, ValueError):
        raise InactiveTurnAuthority(
            "turn authority generation lineage is missing"
        ) from None
    if (
        payload_generation != authority_generation
        or context_generation != authority_generation
    ):
        raise InactiveTurnAuthority("turn authority generation lineage is stale")

    if str(payload.get("engine_owner") or "") != engine_owner:
        raise InactiveTurnAuthority("turn engine-owner lineage is stale")
    if str(context.get(_TASK_OWNER_CONTEXT_KEY) or "") != engine_owner:
        raise InactiveTurnAuthority("turn context engine-owner lineage is stale")

    payload_checkpoint_version = payload.get("checkpoint_version")
    payload_checkpoint_hash = payload.get("checkpoint_hash")
    if checkpoint_version is None:
        if (
            payload_checkpoint_version is not None
            or payload_checkpoint_hash is not None
        ):
            raise InactiveTurnAuthority("turn checkpoint lineage is stale")
        return
    try:
        payload_checkpoint_version = int(payload_checkpoint_version)
    except (TypeError, ValueError):
        raise InactiveTurnAuthority(
            "turn checkpoint version lineage is missing"
        ) from None
    if payload_checkpoint_version != checkpoint_version or str(
        payload_checkpoint_hash or ""
    ) != str(checkpoint_hash or ""):
        raise InactiveTurnAuthority("turn checkpoint lineage is stale")


def _validate_frozen_turn_lineage(
    task: Task,
    *,
    project_id: uuid.UUID,
    engine_owner: str,
    authority_generation: int,
    checkpoint_version: int | None,
    checkpoint_hash: str | None,
) -> None:
    payload = task.first_payload if isinstance(task.first_payload, dict) else {}
    context = task.context if isinstance(task.context, dict) else {}
    _validate_frozen_turn_lineage_values(
        payload,
        context,
        project_id=project_id,
        engine_owner=engine_owner,
        authority_generation=authority_generation,
        checkpoint_version=checkpoint_version,
        checkpoint_hash=checkpoint_hash,
    )
