"""Tests for the task capability registry."""

from app.payload_schemas import TaskType, validate_task_payload
from app.task_capabilities import (
    ALL_TASK_TYPES,
    FILTER_VISIBLE_TASK_TYPES,
    PRODUCT_TASK_TYPES,
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
    assert frozenset(t.value for t in TaskType) == ALL_TASK_TYPES


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


def test_write_task_types_is_canonical_pr_producing_set():
    """WRITE_TASK_TYPES is the single source of truth for "produces a branch/PR".

    Guards against the consolidated recovery/dispatch allowlists silently
    diverging if the registry is edited: every write-mode capability — and only
    those — must be a member. `qa` in particular MUST be present (it opens a PR);
    the pre-consolidation orphan-recovery tuple omitted it.
    """
    expected = {
        cap.id.value for cap in TASK_CAPABILITIES if cap.agent_mode == "write"
    }
    assert frozenset(expected) == WRITE_TASK_TYPES
    assert "qa" in WRITE_TASK_TYPES
    # Read-only types that merely operate on an existing PR must NOT leak in.
    assert "review" not in WRITE_TASK_TYPES
    assert "research" not in WRITE_TASK_TYPES


def test_recovery_paths_source_write_allowlist_from_registry():
    """The orphan-recovery / reaped-PR sweeps and the completion router must read
    the registry object, not a private copy. Importing the same frozenset
    identity into those modules is what makes the registry the only edit point."""
    from app import services, task_completion, task_lifecycle

    assert services.WRITE_TASK_TYPES is WRITE_TASK_TYPES
    assert task_completion.WRITE_TASK_TYPES is WRITE_TASK_TYPES
    assert task_lifecycle.WRITE_TASK_TYPES is WRITE_TASK_TYPES
    # task_lifecycle's PR-opening completion branch derives from the registry,
    # excluding `fix` (own elif branch) and including the legacy "" default.
    assert ((WRITE_TASK_TYPES - {"fix"}) | {""}) == (
        task_lifecycle._PR_OPENING_COMPLETION_TYPES
    )


def test_product_task_types_derived_from_registry():
    """PRODUCT_TASK_TYPES is the single source of truth for the product/structural
    split. It must be derived from the registry's delivers_product flag, contain
    only known types, include current value-shipping types, and exclude pure
    overhead/read-only types as well as the stale legacy {feature, improvement}."""
    expected = {
        cap.id.value for cap in TASK_CAPABILITIES if cap.delivers_product
    }
    assert frozenset(expected) == PRODUCT_TASK_TYPES
    assert PRODUCT_TASK_TYPES.issubset(ALL_TASK_TYPES)

    # Current value-shipping types count as product.
    assert {"create", "fix", "migrate"}.issubset(PRODUCT_TASK_TYPES)

    # Overhead / read-only types must NOT be product.
    for structural in ("review", "plan", "research", "refactor", "qa", "deps", "docs"):
        assert structural not in PRODUCT_TASK_TYPES

    # The stale hardcoded legacy set referenced task types that no longer exist.
    assert "feature" not in PRODUCT_TASK_TYPES
    assert "improvement" not in PRODUCT_TASK_TYPES


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
