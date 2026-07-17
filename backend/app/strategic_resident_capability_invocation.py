"""Fail-closed invocation envelope for Strategic Resident capabilities."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator

from app.strategic_operation_authority_validation import (
    OperationPayloadTooLarge,
    _validated_request_payload,
)
from app.strategic_resident_capabilities import (
    CapabilityMode,
    CapabilityParameterKind,
    CapabilitySpec,
    capability_for,
)


MAX_CAPABILITY_PARAMETER_BYTES = 16_384
_FORBIDDEN_PARAMETER_KEYS = frozenset(
    {
        "project_id",
        "projectid",
        "turn_task_id",
        "turntaskid",
        "authority_generation",
        "authoritygeneration",
        "credentials",
        "credential",
        "token",
        "secret",
        "api_key",
        "apikey",
        "shell",
        "command",
        "cmd",
        "script",
        "environment",
        "env",
    }
)
_FORBIDDEN_PARAMETER_SUFFIXES = (
    "_credential",
    "_credentials",
    "_token",
    "_secret",
    "_api_key",
)


def _normalized_parameter_key(value: str) -> str:
    return value.strip().casefold().replace("-", "_").replace(" ", "_")


def _forbidden_parameter_key(value: str) -> bool:
    normalized = _normalized_parameter_key(value)
    return normalized in _FORBIDDEN_PARAMETER_KEYS or normalized.endswith(
        _FORBIDDEN_PARAMETER_SUFFIXES
    )


def validated_capability_parameters(
    spec: CapabilitySpec,
    parameters: Any,
) -> dict[str, Any]:
    """Validate exact names, JSON size, and declared scalar wire types."""

    validated = _validated_request_payload(parameters)
    encoded = json.dumps(
        validated,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    if len(encoded) > MAX_CAPABILITY_PARAMETER_BYTES:
        raise OperationPayloadTooLarge(
            f"capability parameters exceed {MAX_CAPABILITY_PARAMETER_BYTES} bytes"
        )

    def reject_forbidden_keys(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if _forbidden_parameter_key(key):
                    raise ValueError(
                        "parameters cannot override authority or credential scope"
                    )
                reject_forbidden_keys(child)
        elif isinstance(value, list):
            for child in value:
                reject_forbidden_keys(child)

    reject_forbidden_keys(validated)
    if set(validated) != set(spec.required_parameters):
        unknown = set(validated) - set(spec.required_parameters)
        if unknown:
            raise ValueError(
                "unsupported capability parameters: " + ", ".join(sorted(unknown))
            )
        missing = [key for key in spec.required_parameters if key not in validated]
        raise ValueError(f"missing required capability parameters: {', '.join(missing)}")

    result: dict[str, Any] = {}
    for parameter in spec.parameter_specs:
        value = validated[parameter.name]
        if parameter.kind is CapabilityParameterKind.uuid:
            if not isinstance(value, str):
                raise ValueError(f"{parameter.name} must be a UUID string")
            try:
                result[parameter.name] = str(uuid.UUID(value))
            except ValueError:
                raise ValueError(f"{parameter.name} must be a UUID string") from None
            continue
        if parameter.kind is CapabilityParameterKind.integer:
            if type(value) is not int:
                raise ValueError(f"{parameter.name} must be an integer")
            if parameter.minimum is not None and value < parameter.minimum:
                raise ValueError(f"{parameter.name} is below its minimum")
            if parameter.maximum is not None and value > parameter.maximum:
                raise ValueError(f"{parameter.name} exceeds its maximum")
            result[parameter.name] = value
            continue
        if parameter.kind in {
            CapabilityParameterKind.string,
            CapabilityParameterKind.ref,
            CapabilityParameterKind.enum,
        }:
            if not isinstance(value, str):
                raise ValueError(f"{parameter.name} must be a string")
            if not value.strip():
                raise ValueError(f"{parameter.name} must not be blank")
            if any(
                ord(character) == 127
                or (
                    ord(character) < 32
                    and (
                        parameter.kind is not CapabilityParameterKind.string
                        or character not in {"\n", "\r", "\t"}
                    )
                )
                for character in value
            ):
                raise ValueError(f"{parameter.name} contains control characters")
            if parameter.kind is CapabilityParameterKind.ref and any(
                character.isspace() for character in value
            ):
                raise ValueError(f"{parameter.name} reference contains whitespace")
            if parameter.min_length is not None and len(value) < parameter.min_length:
                raise ValueError(f"{parameter.name} is shorter than its minimum")
            if parameter.max_length is not None and len(value) > parameter.max_length:
                raise ValueError(f"{parameter.name} exceeds its maximum length")
            if parameter.kind is CapabilityParameterKind.enum and value not in parameter.choices:
                raise ValueError(f"{parameter.name} is not an allowed value")
            result[parameter.name] = value
            continue
        raise ValueError(f"unsupported parameter schema for {parameter.name}")
    return result


class CapabilityEvidenceRef(BaseModel):
    """Revisioned evidence supplied to a mutation precondition check."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str = Field(min_length=1, max_length=80)
    project_id: uuid.UUID
    source_id: str = Field(min_length=1, max_length=512)
    revision: str = Field(min_length=1, max_length=512)
    checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    inspected_at: datetime


class ResidentCapabilityInvocation(BaseModel):
    """Validated request envelope; credentials are deliberately not accepted."""

    model_config = ConfigDict(extra="forbid")

    capability_id: str = Field(min_length=3, max_length=120)
    project_id: uuid.UUID
    turn_task_id: uuid.UUID
    authority_generation: int = Field(ge=1)
    parameters: dict[str, Any] = Field(default_factory=dict)
    operation_id: str | None = Field(default=None, min_length=1, max_length=128)
    expected_revisions: dict[str, StrictInt | StrictStr] = Field(default_factory=dict)
    evidence: tuple[CapabilityEvidenceRef, ...] = Field(default=(), max_length=50)
    approval_ref: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def _matches_declaration(self) -> ResidentCapabilityInvocation:
        spec = capability_for(self.capability_id)
        self.parameters = validated_capability_parameters(spec, self.parameters)
        if spec.mode is CapabilityMode.mutate:
            if not self.operation_id:
                raise ValueError("mutation capability requires operation_id")
            if not self.expected_revisions:
                raise ValueError("mutation capability requires expected_revisions")
            if len(self.expected_revisions) > 50:
                raise ValueError("at most 50 expected revisions are allowed")
            for key, value in self.expected_revisions.items():
                if not key or len(key) > 160 or (isinstance(value, str) and len(value) > 512):
                    raise ValueError("expected revisions must use bounded keys and values")
            if any(item.project_id != self.project_id for item in self.evidence):
                raise ValueError("capability evidence belongs to another project")
            observed_kinds = {item.kind for item in self.evidence}
            absent_evidence = set(spec.evidence_preconditions) - observed_kinds
            if absent_evidence:
                raise ValueError(
                    "missing evidence preconditions: "
                    + ", ".join(sorted(absent_evidence))
                )
            if spec.approval_policy == "explicit" and not self.approval_ref:
                raise ValueError("capability requires explicit approval_ref")
        return self


__all__ = [
    "MAX_CAPABILITY_PARAMETER_BYTES",
    "CapabilityEvidenceRef",
    "ResidentCapabilityInvocation",
    "validated_capability_parameters",
]
