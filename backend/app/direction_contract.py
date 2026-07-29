"""Shared validation for meaning-bearing Strategic direction contract fields."""

from typing import Any, Mapping


DIRECTION_CONTRACT_FIELD_MAX = 500
DIRECTION_CONTRACT_FIELDS = (
    "success_evidence",
    "retirement_condition",
    "implementation_constraints",
)


def normalize_direction_contract_field(value: Any, field_name: str) -> str:
    """Return a trimmed contract field or reject values that would lose meaning."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string; no changes were applied")
    if len(value) > DIRECTION_CONTRACT_FIELD_MAX:
        raise ValueError(
            f"{field_name} exceeds the {DIRECTION_CONTRACT_FIELD_MAX} character "
            f"limit ({len(value)} supplied); no changes were applied"
        )
    return value.strip()


def normalize_direction_contract_changes(
    changes: Mapping[str, Any],
    *,
    field_prefix: str = "",
) -> dict[str, Any]:
    """Copy changes and atomically normalize every supplied contract field."""
    normalized = dict(changes)
    for field in DIRECTION_CONTRACT_FIELDS:
        if field in normalized:
            normalized[field] = normalize_direction_contract_field(
                normalized[field], f"{field_prefix}{field}"
            )
    return normalized
