"""Tests for the task capability registry."""

from app.payload_schemas import TaskType, validate_task_payload
from app.task_capabilities import (
    ALL_TASK_TYPES,
    FILTER_VISIBLE_TASK_TYPES,
    PROVIDER_CONFIGURABLE_TASK_TYPES,
    READ_ONLY_TASK_TYPES,
    TASK_CAPABILITIES,
    TASK_CAPABILITY_BY_TYPE,
    USER_LAUNCHABLE_TASK_TYPES,
    WRITE_TASK_TYPES,
    get_task_capability,
)


def test_task_capability_registry_covers_task_type_enum():
    assert {capability.id for capability in TASK_CAPABILITIES} == set(TaskType)
    assert set(TASK_CAPABILITY_BY_TYPE) == set(TaskType)
    assert ALL_TASK_TYPES == frozenset(t.value for t in TaskType)


def test_task_capability_modes_partition_all_known_types():
    assert READ_ONLY_TASK_TYPES
    assert WRITE_TASK_TYPES
    assert READ_ONLY_TASK_TYPES.isdisjoint(WRITE_TASK_TYPES)
    assert READ_ONLY_TASK_TYPES | WRITE_TASK_TYPES == ALL_TASK_TYPES


def test_task_capability_ui_subsets_only_reference_known_types():
    assert set(USER_LAUNCHABLE_TASK_TYPES).issubset(ALL_TASK_TYPES)
    assert set(PROVIDER_CONFIGURABLE_TASK_TYPES).issubset(ALL_TASK_TYPES)
    assert set(FILTER_VISIBLE_TASK_TYPES).issubset(ALL_TASK_TYPES)
    assert USER_LAUNCHABLE_TASK_TYPES == (
        "create",
        "review",
        "fix",
        "plan",
        "research",
        "migrate",
        "refactor",
    )


def test_task_capability_lookup_is_metadata_only():
    assert get_task_capability("create").label == "Create"
    assert get_task_capability("future_fundamental_type") is None


def test_registry_does_not_constrain_payload_extension_fields():
    payload = validate_task_payload(
        {
            "type": "create",
            "prompt": "build it",
            "future_capability_field": {"nested": ["still", "allowed"]},
        }
    )

    assert payload["future_capability_field"] == {"nested": ["still", "allowed"]}
