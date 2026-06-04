"""Tests for the task capability registry."""

import pytest
from httpx import ASGITransport, AsyncClient

from app.payload_schemas import TaskType, validate_task_payload
from app.task_capabilities import (
    ALL_TASK_TYPES,
    COMPLETION_HANDLER_BY_TASK_TYPE,
    EVALUATION_STRATEGY_BY_TASK_TYPE,
    FILTER_VISIBLE_TASK_TYPES,
    PRODUCT_TASK_TYPES,
    PROVIDER_CONFIGURABLE_TASK_TYPES,
    READ_ONLY_TASK_TYPES,
    STRATEGIC_PHASE_BY_TASK_TYPE,
    STRATEGIC_TASK_TYPES,
    TASK_CAPABILITIES,
    TASK_CAPABILITY_BY_TYPE,
    USER_LAUNCHABLE_TASK_TYPES,
    WRITE_TASK_TYPES,
    get_task_capability,
    strategic_phase_for,
    task_type_catalog,
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


def test_completion_handler_declarations_match_legacy_dispatch():
    """The registry's completion-handler declarations must reproduce exactly the
    type→handler routing the old if/elif cascade encoded, so the registry-driven
    dispatch in task_lifecycle is behaviour-preserving."""
    assert COMPLETION_HANDLER_BY_TASK_TYPE == {
        "plan": "plan",
        "design_doc_update": "design_doc",
        "strategic_review": "strategic",
        "research": "research",
        "security_audit": "security_audit",
        "perf": "perf",
        "self_audit": "self_audit",
        "memo": "memo",
    }
    # Only the two strategic completion handlers consume the pre-locked Project.
    needs_project = {
        cap.id.value
        for cap in TASK_CAPABILITIES
        if cap.completion_needs_locked_project
    }
    assert needs_project == {"design_doc_update", "strategic_review"}
    # A capability can only require the locked project if it has a handler.
    for cap in TASK_CAPABILITIES:
        if cap.completion_needs_locked_project:
            assert cap.completion_handler is not None


def test_strategic_task_types_match_legacy_tuple():
    """The registry-derived strategic membership set must reproduce exactly the
    literal ("design_doc_update", "strategic_review") tuple that was previously
    hardcoded across the lock-ordering peek, the spawn guards, the
    strategic-failure circuit breaker, the lifecycle backstop, and the
    active-strategic-task DB queries. This is the SINGLE source of truth those
    sites now consume, so a drift here is a behavior change everywhere."""
    assert STRATEGIC_TASK_TYPES == frozenset(
        {"design_doc_update", "strategic_review"}
    )
    # Membership is derived from the same flag that drives the completion lock.
    assert STRATEGIC_TASK_TYPES == {
        cap.id.value
        for cap in TASK_CAPABILITIES
        if cap.completion_needs_locked_project
    }


def test_strategic_phase_ordering_is_registry_declared():
    """Phase is an ORDERING within the strategic cycle, declared per-type.
    design_doc_update is Phase 1, strategic_review is Phase 2. The failure /
    backstop sites resolve it via strategic_phase_for with a "phase2" default,
    preserving the old `"phase1" if type == "design_doc_update" else "phase2"`
    semantics (including the default for any non-strategic type)."""
    assert STRATEGIC_PHASE_BY_TASK_TYPE == {
        "design_doc_update": "phase1",
        "strategic_review": "phase2",
    }
    # Every type that declares a phase is a strategic type and vice versa.
    assert set(STRATEGIC_PHASE_BY_TASK_TYPE) == STRATEGIC_TASK_TYPES
    assert strategic_phase_for("design_doc_update") == "phase1"
    assert strategic_phase_for("strategic_review") == "phase2"
    # Legacy "else phase2" fallback for non-strategic / unknown types.
    assert (strategic_phase_for("research") or "phase2") == "phase2"
    assert (strategic_phase_for("totally_unknown_type") or "phase2") == "phase2"


def test_task_lifecycle_binds_every_declared_completion_handler():
    """task_lifecycle must bind a concrete function for every logical handler
    name the registry declares — a declared-but-unbound name would raise KeyError
    at dispatch time. This keeps the registry and its bindings in lockstep."""
    import inspect

    from app import task_lifecycle

    source = inspect.getsource(task_lifecycle._process_post_commit)
    for handler_name in set(COMPLETION_HANDLER_BY_TASK_TYPE.values()):
        assert f'"{handler_name}":' in source, (
            f"completion handler {handler_name!r} declared in the registry is "
            "not bound in task_lifecycle._process_post_commit"
        )


def test_every_capability_declares_a_valid_evaluation_strategy():
    """The additive `evaluation` field is present on every capability with a
    value from the closed strategy vocabulary, and the derived map covers all
    types. Selection of the forward gate is registry-driven via this field."""
    valid = {"code-ci-review", "forward-quality", "metric", "none"}
    for cap in TASK_CAPABILITIES:
        assert cap.evaluation in valid, (cap.id, cap.evaluation)
    assert set(EVALUATION_STRATEGY_BY_TASK_TYPE) == ALL_TASK_TYPES


def test_code_pr_types_keep_code_ci_review_gate():
    """Code / PR-producing types MUST declare code-ci-review so the existing
    CI + review merge gate is preserved exactly — they are never routed through
    the new forward-evaluation module."""
    for t in ("create", "fix", "refactor", "qa", "migrate", "deps", "docs"):
        assert EVALUATION_STRATEGY_BY_TASK_TYPE[t] == "code-ci-review"
    # Knowledge/audit types get the forward-quality gate; perf gets metric.
    for t in ("research", "memo", "security_audit", "self_audit", "investigate"):
        assert EVALUATION_STRATEGY_BY_TASK_TYPE[t] == "forward-quality"
    assert EVALUATION_STRATEGY_BY_TASK_TYPE["perf"] == "metric"
    assert EVALUATION_STRATEGY_BY_TASK_TYPE["plan"] == "none"


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


# ── Enriched declarative fields + operator catalog ──────────────────────────────

_CATALOG_FIELDS = {
    "id",
    "label",
    "description",
    "agent_mode",
    "default_category",
    "user_launchable",
    "provider_configurable",
    "filter_visible",
    "output_kind",
    "requires_code",
    "requires_web",
    "requires_compute",
    "evaluation_strategy",
    "cost_ceiling_usd",
}
_VALID_OUTPUT_KINDS = {"pr", "artifact", "knowledge", "verdict", "none"}
_VALID_EVAL_STRATEGIES = {
    "ci_and_review",
    "review_only",
    "forward_quality",
    "metric",
    "none",
}


def test_task_type_catalog_covers_every_capability_with_all_fields():
    catalog = task_type_catalog()
    assert len(catalog) == len(TASK_CAPABILITIES)
    assert {e["id"] for e in catalog} == {t.value for t in TaskType}
    for entry in catalog:
        assert set(entry.keys()) == _CATALOG_FIELDS, entry["id"]
        assert entry["agent_mode"] in {"write", "read_only"}
        assert entry["output_kind"] in _VALID_OUTPUT_KINDS, entry["id"]
        assert entry["evaluation_strategy"] in _VALID_EVAL_STRATEGIES, entry["id"]
        assert isinstance(entry["requires_code"], bool)
        assert isinstance(entry["requires_web"], bool)
        assert isinstance(entry["requires_compute"], bool)
        assert entry["cost_ceiling_usd"] is None or isinstance(
            entry["cost_ceiling_usd"], (int, float)
        )


def test_task_type_catalog_launchable_flags_and_output_kinds():
    catalog = task_type_catalog()
    launchable = {e["id"] for e in catalog if e["user_launchable"]}
    assert launchable == set(USER_LAUNCHABLE_TASK_TYPES)
    by_id = {e["id"]: e for e in catalog}
    # Code-change types open a PR and are CI+review gated.
    assert by_id["create"]["output_kind"] == "pr"
    assert by_id["create"]["evaluation_strategy"] == "ci_and_review"
    # The reference non-code type produces an artifact and is not gated.
    assert by_id["memo"]["output_kind"] == "artifact"
    assert by_id["memo"]["requires_code"] is False
    assert by_id["memo"]["evaluation_strategy"] == "none"
    # Research is judged by forward quality, not a merged PR.
    assert by_id["research"]["evaluation_strategy"] == "forward_quality"


def test_enrichment_leaves_agent_mode_derived_sets_consistent():
    for entry in task_type_catalog():
        if entry["agent_mode"] == "write":
            assert entry["id"] in WRITE_TASK_TYPES
        else:
            assert entry["id"] in READ_ONLY_TASK_TYPES


@pytest.mark.asyncio
async def test_task_types_endpoint_returns_full_catalog(client: AsyncClient):
    resp = await client.get("/api/admin/task-types")
    assert resp.status_code == 200
    body = resp.json()
    assert body["task_types"] == task_type_catalog()
    assert len(body["task_types"]) == len(TaskType)


@pytest.mark.asyncio
async def test_task_types_endpoint_requires_auth():
    """Router-level get_current_user dependency must reject an unauthorized call."""
    from fastapi import HTTPException

    from app.auth import get_current_user
    from app.main import app

    async def _reject():
        raise HTTPException(status_code=401, detail="unauthorized")

    app.dependency_overrides[get_current_user] = _reject
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.get("/api/admin/task-types")
        assert resp.status_code == 401
    finally:
        app.dependency_overrides.pop(get_current_user, None)
