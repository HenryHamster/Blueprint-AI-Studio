"""Static inventory for every production ``Task(...)`` constructor.

This is deliberately an exact site manifest, not a module allowlist.  A new or
moved constructor fails until its manual/automatic authority is reviewed and
classified.  Automatic helper constructors also declare their complete direct
caller chain so an unfenced call site cannot silently appear later.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1] / "app"
TaskSite = tuple[str, str, int]
AuthorityProof = tuple[str, str, str]


def _auto(*proofs: AuthorityProof) -> tuple[str, tuple[AuthorityProof, ...]]:
    return "automatic_or_mixed", proofs


MANUAL = ("manual_or_terminal", ())

# key = (module, containing function, ordinal within that function)
# proof = (module, lock/admission scope, exact authority symbol called there)
TASK_SITE_MANIFEST: dict[TaskSite, tuple[str, tuple[AuthorityProof, ...]]] = {
    ("acceptance_criteria", "enroll_criterion_named_test_trial", 1): _auto(("acceptance_criteria", "enroll_criterion_named_test_trial", "acquire_automatic_task_creation_authority")),
    ("acceptance_criteria", "spawn_corroboration_task", 1): _auto(("acceptance_criteria", "spawn_corroboration_task", "acquire_automatic_task_creation_authority")),
    ("admin", "create_compute_experiment_task", 1): MANUAL,
    ("admin", "create_task_manually", 1): MANUAL,
    ("admin", "rerun_compute_experiment", 1): MANUAL,
    ("admin_approval_followup_handlers", "maybe_spawn_rollup_smoketest", 1): _auto(("admin_approval_followup_handlers", "maybe_spawn_rollup_smoketest", "automatic_task_creation_parent_lock")),
    ("agent_status_spawn", "maybe_spawn_debug_create", 1): _auto(("agent_status_spawn", "maybe_spawn_debug_create", "automatic_task_creation_decision"), ("routers.agent_status", "update_agent_status", "_lock_agent_status_project_authority")),
    ("agent_status_spawn", "maybe_spawn_smoketest_fix", 1): _auto(("agent_status_spawn", "maybe_spawn_smoketest_fix", "automatic_task_creation_decision"), ("routers.agent_status", "update_agent_status", "_lock_agent_status_project_authority")),
    ("agent_status_spawn", "maybe_spawn_smoketest_fix", 2): _auto(("agent_status_spawn", "maybe_spawn_smoketest_fix", "automatic_task_creation_parent_lock"), ("routers.agent_status", "update_agent_status", "_lock_agent_status_project_authority")),
    ("auto_remediation", "_handle_main_branch_ci_failure_inner", 1): _auto(("auto_remediation", "_handle_main_branch_ci_failure_inner", "acquire_automatic_task_creation_authority")),
    ("auto_review", "_handle_auto_review_inner", 1): _auto(("auto_review", "_handle_auto_review_inner", "acquire_automatic_task_creation_authority")),
    ("autonomous_research", "_spawn_periodic_task", 1): _auto(("autonomous_research", "_spawn_periodic_task", "acquire_automatic_task_creation_authority")),
    ("autonomous_research", "_spawn_research_task_inner", 1): _auto(("autonomous_research", "_spawn_research_task_inner", "acquire_automatic_task_creation_authority")),
    ("autonomous_research_residual", "_spawn_residual_fingerprint_remediation", 1): _auto(("autonomous_research_residual", "_spawn_residual_fingerprint_remediation", "acquire_automatic_task_creation_authority")),
    ("autonomous_research_residual", "_spawn_viewport_regression_remediation", 1): _auto(("autonomous_research_residual", "_spawn_viewport_regression_remediation", "acquire_automatic_task_creation_authority")),
    ("capability_proof", "enroll_capability_proof_trial", 1): _auto(("capability_proof", "enroll_capability_proof_trial", "acquire_automatic_task_creation_authority")),
    ("direction_lifecycle", "_process_direction_lifecycle_locked", 1): _auto(("direction_lifecycle", "_process_direction_lifecycle_locked", "automatic_task_creation_decision"), ("direction_lifecycle", "_process_direction_lifecycle", "project_lock")),
    ("discovery_review", "maybe_close_and_spawn_discovery_review", 1): _auto(("discovery_review", "maybe_close_and_spawn_discovery_review", "acquire_automatic_task_creation_authority")),
    ("evergreen_backlog", "spawn_evergreen_probe", 1): _auto(("evergreen_backlog", "spawn_evergreen_probe", "acquire_automatic_task_creation_authority")),
    ("hotfix_backmerge", "_spawn_ci_fix_task", 1): _auto(("hotfix_backmerge", "_handle_ci_failure", "acquire_automatic_task_creation_authority")),
    ("hotfix_backmerge", "_spawn_migration_resolution_task", 1): _auto(("hotfix_backmerge", "_handle_migration_block", "acquire_automatic_task_creation_authority")),
    ("hotfix_backmerge", "_spawn_resolution_task", 1): _auto(("hotfix_backmerge", "_handle_conflict", "acquire_automatic_task_creation_authority")),
    ("issue_implementation", "_handle_issue_implementation_inner", 1): _auto(("issue_implementation", "_handle_issue_implementation_inner", "acquire_automatic_task_creation_authority")),
    ("module_finding_chain", "_guard_remediation_task", 1): _auto(("module_finding_chain", "elect_guard_suppression_owners", "acquire_automatic_task_creation_authority")),
    ("plan_completion", "_handle_plan_completion", 1): _auto(("plan_completion", "_handle_plan_completion", "automatic_task_creation_parent_lock")),
    ("release_qualifier_audit", "_spawn_direction_merge_grade_task", 1): _auto(("release_qualifier_audit", "_spawn_direction_merge_grade_task", "_require_qualifier_task_admission")),
    ("release_qualifier_audit", "_spawn_qualifier_walkthrough_task", 1): _auto(("release_qualifier_audit", "_spawn_qualifier_walkthrough_task", "_require_qualifier_task_admission")),
    ("release_qualifier_audit", "_spawn_release_audit_challenge_task", 1): _auto(("release_qualifier_audit", "_spawn_release_audit_challenge_task", "_require_qualifier_task_admission")),
    ("release_qualifier_audit", "_spawn_release_audit_task", 1): _auto(("release_qualifier_audit", "_spawn_release_audit_task", "_require_qualifier_task_admission")),
    ("release_qualifier_audit", "_spawn_release_merge_grade_task", 1): _auto(("release_qualifier_audit", "_spawn_release_merge_grade_task", "_require_qualifier_task_admission")),
    ("repo_bootstrap", "spawn_bootstrap_task", 1): _auto(("repo_bootstrap", "spawn_bootstrap_task", "acquire_automatic_task_creation_authority")),
    ("research_completion", "_handle_research_completion", 1): _auto(("research_completion", "_handle_research_completion", "lock_research_spawn_boundary")),
    ("research_completion", "_handle_research_completion", 2): _auto(("research_completion", "_handle_research_completion", "lock_research_spawn_boundary")),
    ("research_completion", "_handle_research_completion", 3): _auto(("research_completion", "_handle_research_completion", "lock_research_spawn_boundary")),
    ("review_pipeline", "_handle_lane_checkpoint_recovery", 1): _auto(("review_pipeline", "_spawn_pipeline_task", "acquire_automatic_task_creation_authority")),
    ("review_pipeline", "_spawn_pipeline_task", 1): _auto(("review_pipeline", "_spawn_pipeline_task", "acquire_automatic_task_creation_authority")),
    ("review_pipeline", "_spawn_verify_tests_task", 1): _auto(("review_pipeline", "_spawn_pipeline_task", "acquire_automatic_task_creation_authority")),
    ("routers.admin_debug", "spawn_canary_adversarial_challenge", 1): MANUAL,
    ("routers.admin_operator_triggers", "trigger_self_audit", 1): MANUAL,
    ("routers.agent", "spawn_subtask", 1): _auto(("routers.agent", "spawn_subtask", "acquire_automatic_task_creation_authority")),
    ("routers.agent_status", "update_agent_status", 1): _auto(("routers.agent_status", "update_agent_status", "automatic_task_creation_decision"), ("routers.agent_status", "update_agent_status", "_lock_agent_status_project_authority")),
    ("routers.directions", "create_direction_research_task", 1): MANUAL,
    ("routers.projects", "deploy_blueprint", 1): MANUAL,
    ("routers.projects", "deploy_blueprint", 2): MANUAL,
    ("sr_diagnostics_endpoints", "spawn_sr_investigation", 1): MANUAL,
    ("strategic_completion", "_spawn_retirement_memos", 1): _auto(("strategic_completion", "_spawn_retirement_memos", "acquire_automatic_task_creation_authority")),
    ("strategic_memory_runtime", "_ensure_memory_job_task", 1): _auto(("strategic_memory_runtime", "_ensure_memory_job_task", "acquire_automatic_task_creation_authority")),
    ("strategic_spawn_requests", "_task_from_specification", 1): _auto(("strategic_spawn_requests", "submit_strategic_spawn", "acquire_automatic_task_creation_authority"), ("strategic_spawn_requests", "drain_deferred_spawn_requests", "acquire_automatic_task_creation_authority")),
    ("strategic_spawning", "_finalize_strategic_review_spawn", 1): _auto(("strategic_spawning", "_finalize_strategic_review_spawn", "acquire_automatic_strategic_cycle_creation_authority")),
    ("strategic_spawning", "_maybe_spawn_ci_compliance_cap_bump_fix", 1): _auto(("strategic_spawning", "_maybe_spawn_ci_compliance_cap_bump_fix", "acquire_automatic_task_creation_authority")),
    ("strategic_spawning", "_spawn_design_doc_update", 1): _auto(("strategic_spawning", "_spawn_design_doc_update", "acquire_automatic_strategic_cycle_creation_authority")),
    ("task_completion", "_handle_perf_completion", 1): _auto(("task_completion", "_handle_perf_completion", "automatic_task_creation_parent_lock")),
    ("task_completion", "_handle_security_audit_completion", 1): _auto(("task_completion", "_handle_security_audit_completion", "automatic_task_creation_parent_lock")),
    ("task_completion", "_maybe_spawn_sweep_aggregate_followon", 1): _auto(("task_completion", "_handle_compute_experiment_completion", "automatic_task_creation_parent_lock"), ("task_completion", "run_compute_wake_post_commit", "automatic_task_creation_parent_lock")),
    ("task_completion", "_spawn_act_on_verdict_followon", 1): _auto(("task_completion", "_handle_compute_experiment_completion", "automatic_task_creation_parent_lock"), ("task_completion", "run_compute_wake_post_commit", "automatic_task_creation_parent_lock")),
    ("task_evaluators", "spawn_finding_challenge", 1): _auto(("task_evaluators", "spawn_finding_challenge", "automatic_task_creation_parent_lock"), ("task_evaluators", "spawn_finding_challenge", "automatic_task_creation_decision")),
    ("task_evaluators", "spawn_forward_quality_grader", 1): _auto(("task_evaluators", "spawn_forward_quality_grader", "automatic_task_creation_parent_lock"), ("task_evaluators", "spawn_forward_quality_grader", "automatic_task_creation_decision")),
    ("task_lifecycle", "_process_post_commit", 1): _auto(("task_lifecycle", "_process_post_commit", "automatic_task_creation_parent_lock")),
    ("task_lifecycle_requeue", "_requeue_parent_task", 1): _auto(("task_lifecycle_requeue", "_requeue_parent_task", "acquire_automatic_task_creation_authority")),
    ("warm_pool_maintainer", "sweep_stranded_lanes", 1): _auto(("warm_pool_maintainer", "sweep_stranded_lanes", "acquire_automatic_task_creation_authority")),
}


# Exact direct-call edges for constructor helpers whose authority lives in a
# caller.  Requiring equality (not membership) makes a new bypass caller fail.
STRUCTURAL_CALLERS: dict[tuple[str, str], set[tuple[str, str]]] = {
    ("agent_status_spawn", "maybe_spawn_debug_create"): {("routers.agent_status", "update_agent_status")},
    ("agent_status_spawn", "maybe_spawn_smoketest_fix"): {("routers.agent_status", "update_agent_status")},
    ("direction_lifecycle", "_process_direction_lifecycle_locked"): {("direction_lifecycle", "_process_direction_lifecycle")},
    ("hotfix_backmerge", "_spawn_ci_fix_task"): {("hotfix_backmerge", "_handle_ci_failure")},
    ("hotfix_backmerge", "_spawn_migration_resolution_task"): {("hotfix_backmerge", "_handle_migration_block")},
    ("hotfix_backmerge", "_spawn_resolution_task"): {("hotfix_backmerge", "_handle_conflict")},
    ("module_finding_chain", "_guard_remediation_task"): {("module_finding_chain", "elect_guard_suppression_owners")},
    ("review_pipeline", "_handle_lane_checkpoint_recovery"): {("review_pipeline", "_spawn_pipeline_task")},
    ("review_pipeline", "_spawn_verify_tests_task"): {("review_pipeline", "_spawn_pipeline_task")},
    ("strategic_spawn_requests", "_task_from_specification"): {("strategic_spawn_requests", "_dispatch_request")},
    ("strategic_spawn_requests", "_dispatch_request"): {("strategic_spawn_requests", "drain_deferred_spawn_requests"), ("strategic_spawn_requests", "submit_strategic_spawn")},
    ("task_completion", "_maybe_spawn_sweep_aggregate_followon"): {("task_completion", "_handle_compute_experiment_completion"), ("task_completion", "run_compute_wake_post_commit")},
    ("task_completion", "_spawn_act_on_verdict_followon"): {("task_completion", "_handle_compute_experiment_completion"), ("task_completion", "run_compute_wake_post_commit")},
}

# ---------------------------------------------------------------------------
# Payload-contract ratchet.
#
# Project authority answers "may this task be created at all".  It says nothing
# about whether the payload the spawner assembled is one the agent can actually
# run.  On 2026-08-23 the guard-suppression remediation path was found to have
# published a ``type: "fix"`` payload with no ``branch`` and no ``pr_number``
# every single time it fired: FixPayload requires both, and the agent's
# handleFix validates ``branch`` on its first line of real work.  Every task
# that path ever spawned died with ``Fatal error: branch is required`` and
# exhausted its three retries, while the operator was told a remediation task
# "now owns it".  The seam that would have caught it -- ``validate_internal_payload``
# -- already existed and was already used by 44 of the 60 production spawn
# sites; this path simply never called it.
#
# This guard is the ratchet: every production ``Task(...)`` site must validate
# its payload against the task_type contract, or be named here.  The listed
# sites are DEBT, not endorsement -- each one can still publish a payload its
# handler cannot run.  Removing an entry (by routing that spawner through the
# validator) is always the right direction; adding one requires deliberately
# editing this list.
_PAYLOAD_CONTRACT_VALIDATORS = ("validate_internal_payload", "validate_task_payload")

PAYLOAD_CONTRACT_UNVALIDATED: dict[tuple[str, str], str] = {
    ("acceptance_criteria", "enroll_criterion_named_test_trial"): "debt: not yet routed through the payload-contract validator",
    ("acceptance_criteria", "spawn_corroboration_task"): "debt: not yet routed through the payload-contract validator",
    ("auto_review", "_handle_auto_review_inner"): "debt: not yet routed through the payload-contract validator",
    ("capability_proof", "enroll_capability_proof_trial"): "debt: not yet routed through the payload-contract validator",
    ("discovery_review", "maybe_close_and_spawn_discovery_review"): "debt: not yet routed through the payload-contract validator",
    ("evergreen_backlog", "spawn_evergreen_probe"): "debt: not yet routed through the payload-contract validator",
    ("issue_implementation", "_handle_issue_implementation_inner"): "debt: not yet routed through the payload-contract validator",
    ("repo_bootstrap", "spawn_bootstrap_task"): "debt: not yet routed through the payload-contract validator",
    ("routers.admin_debug", "spawn_canary_adversarial_challenge"): "debt: not yet routed through the payload-contract validator",
    ("sr_diagnostics_endpoints", "spawn_sr_investigation"): "debt: not yet routed through the payload-contract validator",
    ("strategic_spawn_requests", "_task_from_specification"): "debt: not yet routed through the payload-contract validator",
    ("strategic_spawning", "_finalize_strategic_review_spawn"): "debt: not yet routed through the payload-contract validator",
    ("task_evaluators", "spawn_finding_challenge"): "debt: not yet routed through the payload-contract validator",
    ("task_evaluators", "spawn_forward_quality_grader"): "debt: not yet routed through the payload-contract validator",
}

# Literal pins for the two paths this ratchet was cut from.  Keeping them named
# means a regression that re-exempts either one fails on its own line instead of
# quietly changing a set comparison.
PAYLOAD_CONTRACT_REQUIRED_SITES: tuple[tuple[str, str], ...] = (
    ("module_finding_chain", "_guard_remediation_task"),
    ("auto_remediation", "_handle_main_branch_ci_failure_inner"),
)


# Research children are intentionally assembled as transient ORM objects during
# a long validation pass, then published as one batch only after a fresh
# Project -> parent Task boundary.  Their safe ordering is boundary-before-add,
# not boundary-before-Python-construction.
PRECONSTRUCTED_TRANSIENT_SITES: dict[TaskSite, str] = {
    ("research_completion", "_handle_research_completion", 1): "child_tasks",
    ("research_completion", "_handle_research_completion", 2): "_auxiliary_tasks",
    ("research_completion", "_handle_research_completion", 3): "_auxiliary_tasks",
}


class _ModuleInventory(ast.NodeVisitor):
    def __init__(self, module: str) -> None:
        self.module = module
        self.function_stack: list[str] = []
        self.task_lines: list[tuple[str, int]] = []
        self.functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.functions[node.name] = node
        self.function_stack.append(node.name)
        self.generic_visit(node)
        self.function_stack.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name) and node.func.id == "Task":
            assert self.function_stack, f"module-level Task constructor in {self.module}"
            self.task_lines.append((self.function_stack[-1], node.lineno))
        self.generic_visit(node)


def _inventories() -> dict[str, _ModuleInventory]:
    inventories = {}
    for path in APP_ROOT.rglob("*.py"):
        module = path.relative_to(APP_ROOT).with_suffix("").as_posix().replace("/", ".")
        inventory = _ModuleInventory(module)
        inventory.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        inventories[module] = inventory
    return inventories


def _called_names(node: ast.AST) -> set[str]:
    return {
        child.func.id
        for child in ast.walk(node)
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
    }


def _called_name_lines(node: ast.AST, name: str) -> list[int]:
    return sorted(
        child.lineno
        for child in ast.walk(node)
        if isinstance(child, ast.Call)
        and isinstance(child.func, ast.Name)
        and child.func.id == name
    )


def _constructor_line(inventories: dict[str, _ModuleInventory], site: TaskSite) -> int:
    module, function, ordinal = site
    lines = sorted(
        line
        for candidate_function, line in inventories[module].task_lines
        if candidate_function == function
    )
    return lines[ordinal - 1]


def _task_binding(scope: ast.AST, constructor_line: int) -> str:
    for assignment in ast.walk(scope):
        if not isinstance(assignment, ast.Assign) or len(assignment.targets) != 1:
            continue
        if not isinstance(assignment.targets[0], ast.Name):
            continue
        if (
            isinstance(assignment.value, ast.Call)
            and isinstance(assignment.value.func, ast.Name)
            and assignment.value.func.id == "Task"
            and assignment.value.lineno == constructor_line
        ):
            return assignment.targets[0].id
    raise AssertionError(f"Task constructor at line {constructor_line} is not a simple binding")


def _attribute_call_lines(scope: ast.AST, owner: str, method: str, arg: str) -> list[int]:
    return sorted(
        child.lineno
        for child in ast.walk(scope)
        if isinstance(child, ast.Call)
        and isinstance(child.func, ast.Attribute)
        and isinstance(child.func.value, ast.Name)
        and child.func.value.id == owner
        and child.func.attr == method
        and any(isinstance(value, ast.Name) and value.id == arg for value in child.args)
    )


def test_every_production_task_constructor_is_exactly_classified() -> None:
    actual: set[TaskSite] = set()
    for module, inventory in _inventories().items():
        by_function: dict[str, list[int]] = defaultdict(list)
        for function, line in inventory.task_lines:
            by_function[function].append(line)
        for function, lines in by_function.items():
            for ordinal, _line in enumerate(sorted(lines), start=1):
                actual.add((module, function, ordinal))

    assert actual == set(TASK_SITE_MANIFEST)
    classes = [classification for classification, _proofs in TASK_SITE_MANIFEST.values()]
    assert classes.count("manual_or_terminal") == 9
    assert classes.count("automatic_or_mixed") == 51


def test_every_automatic_constructor_has_executable_authority_proof() -> None:
    inventories = _inventories()
    for site, (classification, proofs) in TASK_SITE_MANIFEST.items():
        if classification == "manual_or_terminal":
            assert not proofs, site
            continue
        assert proofs, site
        for module, function, authority_symbol in proofs:
            scope = inventories[module].functions[function]
            authority_lines = _called_name_lines(scope, authority_symbol)
            assert authority_lines, (site, module, function, authority_symbol)
            # Cheap sequencing tripwire for same-function proof.  This is not a
            # full control-flow proof; behavioral/race tests own that job.  It
            # does prevent a refactor from moving the named admission call
            # below INSERT while leaving the manifest superficially satisfied.
            if (module, function) == site[:2] and site not in PRECONSTRUCTED_TRANSIENT_SITES:
                assert any(
                    line < _constructor_line(inventories, site)
                    for line in authority_lines
                ), (site, authority_symbol)


def test_preconstructed_research_tasks_are_unpublished_until_after_authority() -> None:
    inventories = _inventories()
    scope = inventories["research_completion"].functions["_handle_research_completion"]
    boundary_lines = _called_name_lines(scope, "lock_research_spawn_boundary")
    assert boundary_lines
    boundary_line = boundary_lines[-1]
    # One common, post-boundary publisher owns both transient collections.
    publish_lines = _attribute_call_lines(scope, "session", "add", "_new_task")
    assert publish_lines and min(publish_lines) > boundary_line

    for site, collection in PRECONSTRUCTED_TRANSIENT_SITES.items():
        constructor_line = _constructor_line(inventories, site)
        assert constructor_line < boundary_line
        binding = _task_binding(scope, constructor_line)
        append_lines = _attribute_call_lines(scope, collection, "append", binding)
        assert append_lines and min(append_lines) < boundary_line, site
        # No transient object may be directly attached to the session before
        # the common locked publisher.  This catches an eager session.add move
        # without pretending to be a full data-flow analyzer.
        assert not [
            line
            for line in _attribute_call_lines(scope, "session", "add", binding)
            if line < boundary_line
        ], site


def test_structurally_gated_constructor_helpers_have_no_bypass_callers() -> None:
    inventories = _inventories()
    for (module, callee), expected_callers in STRUCTURAL_CALLERS.items():
        actual_callers = {
            (caller_module, function)
            for caller_module, inventory in inventories.items()
            for function, node in inventory.functions.items()
            if function != callee and callee in _called_names(node)
        }
        assert actual_callers == expected_callers, (module, callee)


def _validates_payload_contract(scope: ast.AST) -> bool:
    """Whether *scope* calls a payload-contract validator (alias-tolerant)."""
    return any(
        name.endswith(_PAYLOAD_CONTRACT_VALIDATORS) for name in _called_names(scope)
    )


def test_every_production_task_constructor_validates_its_payload_contract() -> None:
    inventories = _inventories()
    unvalidated = {
        (module, function)
        for module, inventory in inventories.items()
        for function, _line in inventory.task_lines
        if not _validates_payload_contract(inventory.functions[function])
    }

    assert unvalidated == set(PAYLOAD_CONTRACT_UNVALIDATED), (
        "A production Task(...) spawn site must validate its payload against the "
        "task_type contract (validate_internal_payload / validate_task_payload) "
        "before the row is published, or be listed in PAYLOAD_CONTRACT_UNVALIDATED "
        "as reviewed debt. An unvalidated payload that omits a field its handler "
        "hard-requires is only discovered by a claimed VM that dies on its first "
        "line of real work and burns every retry.\n"
        f"  newly unvalidated: {sorted(unvalidated - set(PAYLOAD_CONTRACT_UNVALIDATED))}\n"
        f"  stale exemptions:  {sorted(set(PAYLOAD_CONTRACT_UNVALIDATED) - unvalidated)}"
    )


def test_named_remediation_spawners_validate_their_payload_contract() -> None:
    inventories = _inventories()
    for module, function in PAYLOAD_CONTRACT_REQUIRED_SITES:
        assert (module, function) not in PAYLOAD_CONTRACT_UNVALIDATED, (module, function)
        scope = inventories[module].functions[function]
        assert _validates_payload_contract(scope), (module, function)
