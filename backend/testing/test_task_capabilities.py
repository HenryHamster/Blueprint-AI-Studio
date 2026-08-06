"""Tests for the task capability registry."""

import os
import re

import pytest
from httpx import ASGITransport, AsyncClient

from app.payload_schemas import TaskType, validate_task_payload
from app.task_capabilities import (
    AGENT_EXECUTED_TASK_TYPES,
    ALL_TASK_TYPES,
    BACKEND_MANAGED_TASK_TYPES,
    agent_capability_slice,
    CODE_REVIEW_GATED_TASK_TYPES,
    COMPLETION_HANDLER_BY_TASK_TYPE,
    COMPUTE_JOB_LAUNCH_TASK_TYPES,
    EVALUATION_STRATEGY_BY_TASK_TYPE,
    FILTER_VISIBLE_TASK_TYPES,
    PERIODIC_TASK_TYPES,
    PLAN_KNOWLEDGE_SUBTASK_TYPES,
    PLAN_SUBTASK_TYPES,
    PRODUCT_TASK_TYPES,
    PROVIDER_CONFIGURABLE_TASK_TYPES,
    READ_ONLY_TASK_TYPES,
    RESERVABLE_TASK_TYPES,
    RUN_SHAPE_BY_TASK_TYPE,
    STRATEGIC_PHASE_BY_TASK_TYPE,
    STRATEGIC_TASK_TYPES,
    STRATEGIST_CONTEXTUAL_TASK_TYPES,
    STRATEGIST_SPAWNABLE_TASK_TYPES,
    TASK_CAPABILITIES,
    TASK_CAPABILITY_BY_TYPE,
    USER_LAUNCHABLE_TASK_TYPES,
    WRITE_TASK_TYPES,
    get_task_capability,
    task_capability_contract,
    run_shape_for,
    strategic_phase_for,
    task_type_catalog,
)


def test_strategist_dispatch_policy_uses_one_spawn_task_contract():
    assert {
        "research",
        "web_research",
        "design",
        "plan",
        "compute_experiment",
        "create",
        "refactor",
        "qa",
    }.issubset(STRATEGIST_SPAWNABLE_TASK_TYPES)
    assert STRATEGIST_CONTEXTUAL_TASK_TYPES == {"fix"}


def test_task_capability_registry_covers_task_type_enum():
    assert {capability.id for capability in TASK_CAPABILITIES} == set(TaskType)
    assert set(TASK_CAPABILITY_BY_TYPE) == set(TaskType)
    assert frozenset(t.value for t in TaskType) == ALL_TASK_TYPES


def test_task_capability_modes_partition_all_known_types():
    assert READ_ONLY_TASK_TYPES
    assert WRITE_TASK_TYPES
    assert READ_ONLY_TASK_TYPES.isdisjoint(WRITE_TASK_TYPES)
    assert READ_ONLY_TASK_TYPES | WRITE_TASK_TYPES == ALL_TASK_TYPES


def test_task_capability_execution_owners_partition_all_known_types():
    assert AGENT_EXECUTED_TASK_TYPES
    assert not BACKEND_MANAGED_TASK_TYPES
    assert AGENT_EXECUTED_TASK_TYPES.isdisjoint(BACKEND_MANAGED_TASK_TYPES)
    assert AGENT_EXECUTED_TASK_TYPES | BACKEND_MANAGED_TASK_TYPES == ALL_TASK_TYPES


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
        "investigate",
        "web_research",
        "design",
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
    type→handler routing the old if/elif cascade encoded (plus the
    compute_experiment artifact-delivery gate), so the registry-driven dispatch in
    task_lifecycle is behaviour-preserving."""
    assert COMPLETION_HANDLER_BY_TASK_TYPE == {
        "plan": "plan",
        "design_doc_update": "design_doc",
        "strategic_review": "strategic",
        "discovery_review": "discovery_review",
        "research": "research",
        "security_audit": "security_audit",
        "perf": "perf",
        "compute_experiment": "compute_experiment",
        "self_audit": "self_audit",
        "memo": "memo",
        "web_research": "web_research",
        "design": "design",
        "repo_bootstrap": "repo_bootstrap",
        "capability_proof_trial": "capability_proof_trial",
        "criterion_named_test_trial": "criterion_named_test_trial",
        "walkthrough_e2e": "walkthrough_e2e",
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


def test_discovery_review_is_a_normal_fail_closed_review_task():
    cap = get_task_capability("discovery_review")

    assert cap is not None
    assert cap.agent_mode == "read_only"
    assert cap.default_category == "strategic"
    assert cap.output_kind == "verdict"
    assert cap.evaluation == "structured-verdict"
    assert cap.completion_handler == "discovery_review"
    assert cap.provider_configurable is False
    assert cap.allowed_providers == frozenset({"anthropic"})
    assert cap.requires_code is False
    assert cap.workspace_requirement == "none"
    assert cap.user_launchable is False
    assert cap.strategist_dispatch == "disabled"
    assert cap.periodic_eligible is False
    assert cap.completion_needs_locked_project is False
    assert cap.strategic_phase is None
    assert "discovery_review" not in STRATEGIC_TASK_TYPES


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


def test_strategic_local_runner_preset_tracks_registry():
    from app.task_capabilities import STRATEGIC_LOCAL_AGENT_TASK_TYPES
    from app.routers import admin_runners

    assert set(admin_runners._STRATEGIC_LOCAL_TASK_TYPES) == set(
        STRATEGIC_LOCAL_AGENT_TASK_TYPES
    )
    assert STRATEGIC_LOCAL_AGENT_TASK_TYPES == STRATEGIC_TASK_TYPES


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
    assert "block_failed_discovery_review(" in source


def test_every_capability_declares_a_valid_evaluation_strategy():
    """The additive `evaluation` field is present on every capability with a
    value from the closed strategy vocabulary, and the derived map covers all
    types. Selection of the forward gate is registry-driven via this field."""
    valid = {"code-ci-review", "forward-quality", "metric", "artifact-delivery", "none"}
    valid.add("structured-verdict")
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
    assert EVALUATION_STRATEGY_BY_TASK_TYPE["compute_experiment"] == "artifact-delivery"
    assert EVALUATION_STRATEGY_BY_TASK_TYPE["plan"] == "none"


def test_code_review_gated_task_types_derived_from_registry():
    """CODE_REVIEW_GATED_TASK_TYPES is exactly the code-ci-review capabilities.

    This locks the membership of the launch-context learning-signal gate (the
    per-type rejection patterns + self-review calibration injected by
    routers/agent.py): every reviewed-code type — including qa/docs/deps — is in,
    and read-only / knowledge types stay out. Derived, not a parallel literal."""
    assert CODE_REVIEW_GATED_TASK_TYPES == {
        "create",
        "fix",
        "refactor",
        "migrate",
        "qa",
        "deps",
        "docs",
        "salvage_bundle",
    }
    derived = frozenset(
        cap.id.value
        for cap in TASK_CAPABILITIES
        if cap.evaluation == "code-ci-review"
    )
    assert CODE_REVIEW_GATED_TASK_TYPES == derived
    # Read-only / knowledge types are never gated in.
    for t in ("research", "plan", "review", "memo", "security_audit"):
        assert t not in CODE_REVIEW_GATED_TASK_TYPES


def test_every_capability_declares_a_valid_run_shape():
    """The additive `run_shape` field is present on every capability with a value
    from the closed run-shape vocabulary, and the derived map covers every type.
    This is the registry invariant locking the new declarative axis, mirroring
    the evaluation-strategy invariant above."""
    valid = {"single_agent", "workflow", "experiment_loop"}
    for cap in TASK_CAPABILITIES:
        assert cap.run_shape in valid, (cap.id, cap.run_shape)
    assert set(RUN_SHAPE_BY_TASK_TYPE) == ALL_TASK_TYPES


def test_run_shape_for_accessor_and_honest_per_type_shapes():
    """`run_shape_for` is the single accessor future dispatchers read. PR-producing
    code types run the create→review→fix WORKFLOW; terminal read-only analyses run
    as a SINGLE agent. Unknown types resolve to None."""
    # The PR-producing code workflow types declare "workflow".
    for t in ("create", "fix", "refactor", "qa", "migrate", "deps", "docs"):
        assert run_shape_for(t) == "workflow", t
    # Terminal single-agent analyses declare "single_agent".
    for t in ("research", "plan", "review", "memo", "security_audit", "investigate"):
        assert run_shape_for(t) == "single_agent", t
    # Unknown / unregistered types have no declared shape.
    assert run_shape_for("totally_unknown_type") is None


def test_experiment_loop_is_a_valid_declarable_run_shape():
    """`experiment_loop` is part of the closed run-shape vocabulary declared by
    compute_experiment — assert the registry-level invariant."""
    # compute_experiment is the registered type that runs the experiment loop.
    assert RUN_SHAPE_BY_TASK_TYPE["compute_experiment"] == "experiment_loop"
    assert run_shape_for("compute_experiment") == "experiment_loop"


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


def test_reservable_task_types_derived_from_registry():
    """RESERVABLE_TASK_TYPES is the single source of truth for which task types
    an operator may reserve pipeline slots for. It must be registry-derived,
    contain only known types, and preserve the historical reservable set so
    existing reserved_slots_for_types configs keep validating."""
    expected = {cap.id.value for cap in TASK_CAPABILITIES if cap.reservable}
    assert frozenset(expected) == RESERVABLE_TASK_TYPES
    assert RESERVABLE_TASK_TYPES.issubset(ALL_TASK_TYPES)
    # Backward-compat: the historically-allowed reserved types must remain.
    assert RESERVABLE_TASK_TYPES == {"create", "research", "plan"}


def test_periodic_task_types_derived_from_registry():
    """PERIODIC_TASK_TYPES is the single source of truth for autonomous periodic
    eligibility, registry-derived and preserving the historical set."""
    expected = {cap.id.value for cap in TASK_CAPABILITIES if cap.periodic_eligible}
    assert frozenset(expected) == PERIODIC_TASK_TYPES
    assert PERIODIC_TASK_TYPES.issubset(ALL_TASK_TYPES)
    assert PERIODIC_TASK_TYPES == {"security_audit", "qa", "deps"}


def test_compute_job_launch_types_derived_from_experiment_loop_run_shape():
    """COMPUTE_JOB_LAUNCH_TASK_TYPES is the single source of truth for the
    agent-facing compute-job launch gate. It must be derived from the
    experiment_loop run-shape (the "launches and polls a budgeted job" shape) —
    NOT the broader requires_compute flag, which smoketest/perf/verify_tests also
    set despite running their compute inside their own agent VM."""
    expected = {
        cap.id.value
        for cap in TASK_CAPABILITIES
        if cap.run_shape == "experiment_loop"
    }
    assert frozenset(expected) == COMPUTE_JOB_LAUNCH_TASK_TYPES
    assert COMPUTE_JOB_LAUNCH_TASK_TYPES.issubset(ALL_TASK_TYPES)
    # compute_experiment is the registered experiment_loop type today.
    assert COMPUTE_JOB_LAUNCH_TASK_TYPES == {"compute_experiment"}
    # requires_compute types that run compute INSIDE their own VM must NOT be in
    # the launch set — they don't offload to the job substrate.
    for in_vm_compute in ("smoketest", "perf", "verify_tests"):
        assert in_vm_compute not in COMPUTE_JOB_LAUNCH_TASK_TYPES
        assert TASK_CAPABILITY_BY_TYPE[TaskType(in_vm_compute)].requires_compute
    # The agent router gates on this exact registry object (no private copy).
    from app.routers import agent_compute_jobs

    assert (
        agent_compute_jobs.COMPUTE_JOB_LAUNCH_TASK_TYPES
        is COMPUTE_JOB_LAUNCH_TASK_TYPES
    )


def test_plan_subtask_types_derived_from_registry():
    """PLAN_SUBTASK_TYPES is the single source of truth for the plan-decomposition
    child allowlist. It must be registry-derived, a SUBSET of WRITE_TASK_TYPES
    (only PR-producing write types are eligible), preserve the historical
    {create, refactor, migrate, qa} set, additionally include the now-eligible
    {deps, docs}, and EXCLUDE `fix` (which needs a pre-existing PR context a plan
    cannot supply)."""
    expected = {
        cap.id.value for cap in TASK_CAPABILITIES if cap.plan_subtask_eligible
    }
    assert frozenset(expected) == PLAN_SUBTASK_TYPES
    assert PLAN_SUBTASK_TYPES.issubset(ALL_TASK_TYPES)
    # Only write/PR-producing types may be fresh plan children.
    assert PLAN_SUBTASK_TYPES.issubset(WRITE_TASK_TYPES)
    # Historical allowlist preserved; deps/docs newly honored.
    assert {"create", "refactor", "migrate", "qa"}.issubset(PLAN_SUBTASK_TYPES)
    assert {"deps", "docs"}.issubset(PLAN_SUBTASK_TYPES)
    # `fix` is a write type but is NOT a valid fresh plan child.
    assert "fix" not in PLAN_SUBTASK_TYPES
    assert "fix" in WRITE_TASK_TYPES
    # The plan-completion handler gates on this exact registry object.
    from app import plan_completion

    assert plan_completion.PLAN_SUBTASK_TYPES is PLAN_SUBTASK_TYPES


def test_plan_knowledge_subtask_types_derived_from_registry():
    """PLAN_KNOWLEDGE_SUBTASK_TYPES is the single source of truth for the
    plan-decomposition KNOWLEDGE-prerequisite child allowlist. It must be
    registry-derived from ``plan_knowledge_prereq_eligible``, include the
    durable non-PR prerequisite producers {web_research, design,
    compute_experiment}, be DISJOINT from PLAN_SUBTASK_TYPES (knowledge
    prereqs are NOT PR-producing implementation subtypes), and none of the
    types may appear in WRITE_TASK_TYPES or PRODUCT_TASK_TYPES."""
    expected = {
        cap.id.value
        for cap in TASK_CAPABILITIES
        if cap.plan_knowledge_prereq_eligible
    }
    assert frozenset(expected) == PLAN_KNOWLEDGE_SUBTASK_TYPES
    assert PLAN_KNOWLEDGE_SUBTASK_TYPES == {
        "compute_experiment",
        "design",
        "web_research",
    }
    assert PLAN_KNOWLEDGE_SUBTASK_TYPES.issubset(ALL_TASK_TYPES)
    # Disjoint from the PR-producing implementation allowlist — knowledge
    # prereqs must NOT be classified as PR-producing subtypes.
    assert PLAN_KNOWLEDGE_SUBTASK_TYPES.isdisjoint(PLAN_SUBTASK_TYPES)
    # Neither type produces a branch/PR or ships product value.
    assert PLAN_KNOWLEDGE_SUBTASK_TYPES.isdisjoint(WRITE_TASK_TYPES)
    assert PLAN_KNOWLEDGE_SUBTASK_TYPES.isdisjoint(PRODUCT_TASK_TYPES)
    # The plan-completion handler gates on this exact registry object.
    from app import plan_completion

    assert (
        plan_completion.PLAN_KNOWLEDGE_SUBTASK_TYPES is PLAN_KNOWLEDGE_SUBTASK_TYPES
    )


def test_task_capability_contract_covers_every_registered_type():
    """The published contract has exactly one entry per registered task type and
    surfaces every flag the operator/agent surfaces consume — so a newly
    registered task type appears in the contract with no per-surface edit."""
    contract = task_capability_contract()
    assert set(contract) == ALL_TASK_TYPES
    required_fields = {
        "type",
        "label",
        "badge",
        "agent_mode",
        "default_category",
        "description",
        "user_launchable",
        "operator_contract_launchable",
        "provider_configurable",
        "filter_visible",
        "delivers_product",
        "reservable",
        "periodic_eligible",
        "run_shape",
        "output_kind",
        "requires_code",
        "requires_web",
        "requires_compute",
    }
    for task_type, entry in contract.items():
        assert entry["type"] == task_type
        assert required_fields.issubset(entry)
        # badge defaults to label when unset.
        assert entry["badge"]
        assert entry["agent_mode"] in ("write", "read_only")
        assert entry["run_shape"] in {"single_agent", "workflow", "experiment_loop"}


def test_contract_flags_match_derived_registry_subsets():
    """Each capability-flag column in the contract is consistent with the
    registry-derived subset that the settings validators key off."""
    contract = task_capability_contract()
    assert {t for t, e in contract.items() if e["user_launchable"]} == set(
        USER_LAUNCHABLE_TASK_TYPES
    )
    assert {t for t, e in contract.items() if e["provider_configurable"]} == set(
        PROVIDER_CONFIGURABLE_TASK_TYPES
    )
    assert {t for t, e in contract.items() if e["reservable"]} == RESERVABLE_TASK_TYPES
    assert {
        t for t, e in contract.items() if e["periodic_eligible"]
    } == PERIODIC_TASK_TYPES


def test_task_capability_lookup_is_metadata_only():
    assert get_task_capability("create").label == "Create"
    assert get_task_capability("future_fundamental_type") is None


def test_agent_capability_slice_exposes_agent_contract_fields():
    research = agent_capability_slice("research")
    create = agent_capability_slice("create")
    catalog = {entry["id"]: entry for entry in task_type_catalog()}

    assert research == catalog["research"]
    assert create == catalog["create"]
    assert agent_capability_slice("future_fundamental_type") is None


def test_registry_does_not_constrain_payload_extension_fields():
    payload = validate_task_payload(
        {
            "type": "create",
            "prompt": "build it",
            "future_capability_field": {"nested": ["still", "allowed"]},
            "acceptance_evidence": {
                "kind": "observable_behavior",
                "description": "ships a real, testable change",
            },
        }
    )

    assert payload["future_capability_field"] == {"nested": ["still", "allowed"]}


# ── Enriched declarative fields + operator catalog ──────────────────────────────

_CATALOG_FIELDS = {
    "id",
    "label",
    "description",
    "agent_mode",
    "execution_owner",
    "default_category",
    "user_launchable",
    "operator_contract_launchable",
    "provider_configurable",
    "filter_visible",
    "run_shape",
    "output_kind",
    "requires_code",
    "requires_web",
    "requires_compute",
    "execution_site",
    "workspace_requirement",
    "evaluation_strategy",
    "cost_ceiling_usd",
}
_VALID_OUTPUT_KINDS = {"pr", "artifact", "knowledge", "verdict", "none"}
_VALID_RUN_SHAPES = {"single_agent", "workflow", "experiment_loop"}
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
        assert entry["execution_owner"] in {"agent", "backend"}
        assert entry["run_shape"] in _VALID_RUN_SHAPES, entry["id"]
        assert entry["output_kind"] in _VALID_OUTPUT_KINDS, entry["id"]
        assert entry["evaluation_strategy"] in _VALID_EVAL_STRATEGIES, entry["id"]
        assert isinstance(entry["requires_code"], bool)
        assert isinstance(entry["requires_web"], bool)
        assert isinstance(entry["requires_compute"], bool)
        assert entry["workspace_requirement"] in {"repo", "none"}
        assert entry["cost_ceiling_usd"] is None or isinstance(
            entry["cost_ceiling_usd"], (int, float)
        )


def test_task_type_catalog_launchable_flags_and_output_kinds():
    catalog = task_type_catalog()
    launchable = {e["id"] for e in catalog if e["user_launchable"]}
    assert launchable == set(USER_LAUNCHABLE_TASK_TYPES)
    by_id = {e["id"]: e for e in catalog}
    assert {
        e["id"] for e in catalog if e["operator_contract_launchable"]
    } == {"compute_experiment"}
    assert by_id["create"]["run_shape"] == "workflow"
    # Code-change types open a PR and are CI+review gated.
    assert by_id["create"]["output_kind"] == "pr"
    assert by_id["create"]["evaluation_strategy"] == "ci_and_review"
    # The reference non-code type produces an artifact and is not gated.
    assert by_id["memo"]["output_kind"] == "artifact"
    assert by_id["memo"]["requires_code"] is False
    assert by_id["memo"]["evaluation_strategy"] == "none"
    # Research is judged by forward quality, not a merged PR.
    assert by_id["research"]["run_shape"] == "single_agent"
    assert by_id["research"]["evaluation_strategy"] == "forward_quality"
    # The reference compute-loop task is a compute-only artifact producer with a
    # declared ceiling and non-gating artifact-delivery metadata.
    assert by_id["compute_experiment"] == {
        "id": "compute_experiment",
        "label": "Compute Experiment",
        "description": by_id["compute_experiment"]["description"],
        "agent_mode": "read_only",
        "execution_owner": "agent",
        "default_category": "ops",
        "user_launchable": False,
        "operator_contract_launchable": True,
        "provider_configurable": False,
        "filter_visible": True,
        "output_kind": "artifact",
        "requires_code": False,
        "requires_web": False,
        "requires_compute": True,
        "execution_site": "default",
        "evaluation_strategy": "none",
        "cost_ceiling_usd": 5.0,
        "run_shape": "experiment_loop",
        "workspace_requirement": "repo",
    }


def test_enrichment_leaves_agent_mode_derived_sets_consistent():
    for entry in task_type_catalog():
        if entry["agent_mode"] == "write":
            assert entry["id"] in WRITE_TASK_TYPES
        else:
            assert entry["id"] in READ_ONLY_TASK_TYPES


def test_every_capability_declares_valid_run_shape_and_catalog_round_trips():
    expected_by_id = {}
    for capability in TASK_CAPABILITIES:
        assert capability.run_shape in _VALID_RUN_SHAPES, capability.id.value
        expected_by_id[capability.id.value] = capability.run_shape

    assert {
        entry["id"]: entry["run_shape"] for entry in task_type_catalog()
    } == expected_by_id


@pytest.mark.asyncio
async def test_task_types_endpoint_returns_full_catalog(client: AsyncClient):
    resp = await client.get("/api/admin/task-types")
    assert resp.status_code == 200
    body = resp.json()
    assert body["task_types"] == task_type_catalog()
    assert len(body["task_types"]) == len(TaskType)
    assert {
        entry["id"]: entry["run_shape"] for entry in body["task_types"]
    } == {
        capability.id.value: capability.run_shape for capability in TASK_CAPABILITIES
    }


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


# ── Agent ↔ backend dispatch-coverage CI guard ──────────────────────────────────
#
# Adding a new kind of task is meant to be a matter of registering it. But the
# backend task-type registry (TaskType / TASK_CAPABILITIES) and the agent's
# task→handler dispatch map (agent/src/handlers/index.ts) are two separate
# hand-maintained tables. If a type is registered on the backend but no handler
# is wired on the agent, the task passes backend validation, gets queued,
# provisions an agent VM, and only THEN fails with a runtime "unknown task type"
# error — wasting the VM spin-up. The agent has its own vitest test for this, but
# CI only runs `tsc` on the agent, so that invariant is enforced nowhere
# automatically. These tests close the loop: drift between the two tables breaks
# the backend build (which DOES run in CI) loudly and early, naming the offending
# type. They complement — and do not duplicate — the agent consuming the backend
# capability contract for its read-only/write modes: handler FUNCTIONS are code
# and cannot come from a JSON contract, so a code-coverage cross-check is still
# needed (and matters more once the agent trusts the contract for the type list).


def _resolve_agent_src_file(relative_path: str) -> "str | None":
    """Locate a file under ``agent/src`` across local + Docker test environments.

    Search order mirrors ``_resolve_prompt_ts_path`` in test_invariants.py:
      1. ``/agent/src/<relative_path>`` (Docker volume-mount target used by
         ``scripts/test-backend.sh``).
      2. Host filesystem at ``../../agent/src/<relative_path>`` from this test
         file (``backend/testing/`` → repo root).

    Returns the first existing path or ``None`` if none resolve (e.g. CI's
    backend-only Docker build context, where the caller skips gracefully).
    """
    candidates = [
        os.path.join("/agent/src", relative_path),
        os.path.normpath(
            os.path.join(
                os.path.dirname(__file__), "..", "..", "agent", "src", relative_path
            )
        ),
    ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return None


def _extract_agent_handler_task_types(source: str) -> "set[str]":
    """Parse the dispatchable task-type keys from the ``handlers`` object literal
    in ``agent/src/handlers/index.ts``.

    Balances braces from the literal's opening ``{`` so nested generic types in
    the declaration line don't confuse key extraction, then reads each top-level
    ``<key>:`` entry. Raises if the literal can't be located/parsed — a silent
    empty set would defeat the guard."""
    marker = "export const handlers"
    start = source.find(marker)
    if start == -1:
        raise ValueError(
            "could not find `export const handlers` in agent/src/handlers/index.ts"
        )
    brace = source.find("{", start)
    if brace == -1:
        raise ValueError("malformed handlers literal: no opening brace")
    depth = 0
    end = -1
    for i in range(brace, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end == -1:
        raise ValueError("malformed handlers literal: unbalanced braces")
    body = source[brace + 1 : end]
    return set(re.findall(r"^\s*([A-Za-z_]\w*)\s*:", body, re.MULTILINE))


def test_agent_handler_dispatch_matches_backend_registry():
    """Every backend-registered task type MUST have an agent handler, and every
    agent handler MUST correspond to a registered type. Drift in either direction
    breaks the build early instead of failing on a freshly-provisioned VM."""
    agent_index = _resolve_agent_src_file(os.path.join("handlers", "index.ts"))
    if agent_index is None:
        pytest.skip("agent/src not available in this test environment")
    with open(agent_index) as f:
        source = f.read()

    handler_types = _extract_agent_handler_task_types(source)
    assert handler_types, (
        "parsed zero handler keys from agent/src/handlers/index.ts — the "
        "`handlers` object-literal format likely changed; fix the parser"
    )

    registered = set(AGENT_EXECUTED_TASK_TYPES)

    missing_handler = registered - handler_types
    assert not missing_handler, (
        f"Task types registered on the backend but with NO agent handler in "
        f"agent/src/handlers/index.ts: {sorted(missing_handler)}. Such a type "
        f"would queue, provision a VM, then fail at runtime with 'unknown task "
        f"type'. Add a handler entry to the `handlers` map (or unregister the "
        f"type from the TaskType enum)."
    )

    orphan_handler = handler_types - registered
    assert not orphan_handler, (
        f"Agent handlers wired for task types NOT registered on the backend: "
        f"{sorted(orphan_handler)}. Either register the type in the TaskType enum "
        f"/ TASK_CAPABILITIES, or remove the stale handler entry from "
        f"agent/src/handlers/index.ts."
    )
