"""Task capability registry.

This module centralizes metadata about known task capabilities without
defining their payload shape. Payload validation stays in payload_schemas.py
and remains extra-field tolerant so new task fields can be introduced without
turning this registry into a product constraint.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal


class TaskType(str, Enum):
    create = "create"
    review = "review"
    fix = "fix"
    plan = "plan"
    research = "research"
    strategic_review = "strategic_review"
    design_doc_update = "design_doc_update"
    security_audit = "security_audit"
    qa = "qa"
    deps = "deps"
    docs = "docs"
    migrate = "migrate"
    smoketest = "smoketest"
    refactor = "refactor"
    debug = "debug"
    perf = "perf"
    investigate = "investigate"
    self_audit = "self_audit"
    release_audit = "release_audit"
    memo = "memo"
    verify_tests = "verify_tests"
    compute_experiment = "compute_experiment"
    web_research = "web_research"


AgentMode = Literal["write", "read_only"]

# Closed set of forward evaluation-gate strategy keys. Each TaskType DECLARES
# which one applies via ``TaskCapability.evaluation``; the pluggable evaluator
# framework in ``app.task_evaluators`` resolves the concrete strategy from this
# declaration (registry-driven, never a hardwired type→strategy if/elif).
#   "code-ci-review" — gated by the existing CI-green + review sign-off merge
#       path (code / PR-producing types). This is a SENTINEL the framework
#       recognizes but does NOT act on: code tasks keep their existing two-flag
#       merge gate and are never routed through the new evaluator module.
#   "forward-quality" — a cheap parse / coherence / structural-validity check
#       on the terminal artifact (knowledge / reasoning / audit types). An
#       unparseable or empty artifact fails.
#   "metric" — a check that the required measurement / structured result was
#       actually produced and is within declared bounds (measurement-producing
#       types). A report that produced no measurement fails.
#   "none" — no forward gate (plan and any type with no meaningful artifact
#       gate). Always passes.
EvaluationStrategyKey = Literal[
    "code-ci-review", "forward-quality", "metric", "none"
]

# What a task type ultimately produces. This is the declarative answer to
# "what is the durable output of this kind of work?" and lets the operator
# catalog (and downstream evaluation routing) reason about non-code types
# without special-casing each one.
#   pr       — pushes a branch and opens a pull request (code change)
#   artifact — persists a structured Artifact (e.g. a memo / design doc)
#   knowledge— produces findings / analysis consumed by the strategic loop
#   verdict  — posts a structured pass/fail judgement (review, verify, audit)
#   none     — no durable product beyond task history
OutputKind = Literal["pr", "artifact", "knowledge", "verdict", "none"]

# How a task type's output is judged to be "good". The evaluation lane wires
# concrete evaluators to these keys; the catalog surfaces them so an operator
# can see that e.g. research is graded by forward quality, not by a merged PR.
#   ci_and_review — code lands a PR gated by CI plus reviewer sign-off
#   review_only   — a reviewer / strategic read judges the output (no CI gate)
#   forward_quality — graded by whether the knowledge proved actionable later
#   metric        — judged by a measured number (perf delta, test pass count)
#   none          — terminal artifact, not gated
EvaluationStrategy = Literal[
    "ci_and_review", "review_only", "forward_quality", "metric", "none"
]

# Ordering of a strategic-style task type within the two-phase strategic cycle.
# This is an ORDERING, not mere membership: ``phase1`` is the architecture /
# design-doc analysis pass and ``phase2`` is the vision / direction-update pass.
# Declared per-type so the strategic-failure circuit breaker and the lifecycle
# backstop can resolve a task's phase from the registry instead of a hardcoded
# ``"phase1" if type == "design_doc_update" else "phase2"`` conditional. ``None``
# for every non-strategic type. See STRATEGIC_TASK_TYPES below for membership.
StrategicPhase = Literal["phase1", "phase2"]

# HOW a task type runs — its execution shape. This is the declarative answer to
# "what kind of run produces this type's output?" and is the axis the operator
# catalog surfaces alongside produces/needs/evaluated/cost. The closed
# vocabulary names the three shapes the GENERALITY direction calls for:
#   single_agent   — one terminal agent run produces the output (read-only
#       analyses: research, plan, review, audits, memo, etc.).
#   workflow       — a fixed multi-step agent workflow (the existing
#       create→review→fix pipeline that PR-producing code types pass through).
#   experiment_loop— a long-running, iterated experiment loop. DECLARABLE but
#       currently UNUSED: no task type runs this shape yet, but the experiment
#       proof direction will register one against this value. Keeping it in the
#       vocabulary now lets that future type declare its shape with no edit here.
RunShape = Literal["single_agent", "workflow", "experiment_loop"]


@dataclass(frozen=True)
class TaskCapability:
    id: TaskType
    label: str
    agent_mode: AgentMode
    default_category: str | None = None
    user_launchable: bool = False
    provider_configurable: bool = True
    filter_visible: bool = True
    description: str = ""
    # --- Enriched declarative registry fields (operator catalog + eval routing).
    # All default so existing literals and derived frozensets stay valid; each
    # capability below sets them explicitly.
    output_kind: OutputKind = "none"
    requires_code: bool = True
    requires_web: bool = False
    requires_compute: bool = False
    evaluation_strategy: EvaluationStrategy = "review_only"
    # Declared soft per-task spend ceiling in USD, or None for "no declared
    # bound". This is a guidance/display figure, not an enforced quota.
    cost_ceiling_usd: float | None = None
    # Forward evaluation-gate strategy for this task type's terminal output,
    # resolved by ``app.task_evaluators.resolve_evaluation_strategy``. See
    # ``EvaluationStrategyKey`` above for the meaning of each value. Declared
    # additively with a non-gating default so future/unknown types are not
    # silently blocked by a gate they never opted into.
    evaluation: EvaluationStrategyKey = "none"
    # Logical name of the post-commit completion handler this task type
    # dispatches to, or ``None`` for types that have no dedicated handler
    # (they fall through the completion router untouched). This DECLARES the
    # routing that used to live as a hand-maintained if/elif cascade in
    # task_lifecycle._process_post_commit; task_lifecycle binds each logical
    # name to its concrete handler function so the registry stays import-cycle
    # free while remaining the single source of truth for "which types route
    # to a completion handler".
    completion_handler: str | None = None
    # Whether the declared completion handler must be invoked with the
    # canonically pre-locked Project (Project → Task lock order, see
    # docs/concurrency.md). Only the strategic completion handlers
    # (design_doc_update, strategic_review) consume it.
    completion_needs_locked_project: bool = False
    # Position of this task type within the two-phase strategic cycle, or
    # ``None`` if it is not a strategic-style type. Only the two strategic types
    # set it: design_doc_update -> "phase1", strategic_review -> "phase2". This
    # is the registry-declared ORDERING that the strategic-failure circuit
    # breaker and the lifecycle backstop resolve via ``strategic_phase_for``
    # (replacing the hardcoded design_doc_update/strategic_review conditional).
    # Kept aligned with ``completion_needs_locked_project`` (the same two types):
    # one declares the cycle ordering, the other the completion lock requirement.
    strategic_phase: StrategicPhase | None = None
    # Whether this task type directly ships user-facing product value (as opposed
    # to structural / overhead work). This is a NEW, independent analytics
    # dimension — intentionally distinct from agent_mode and WRITE_TASK_TYPES.
    # See PRODUCT_TASK_TYPES below for the derived single source of truth.
    delivers_product: bool = False
    # HOW this task type runs (its execution shape). Defaults to "single_agent"
    # so a newly-registered terminal type needs no edit; the PR-producing code
    # types declare "workflow" (the create→review→fix pipeline) explicitly.
    # "experiment_loop" is a valid declarable value with no current user yet.
    # Surfaced in the operator catalog and resolved via ``run_shape_for``.
    run_shape: RunShape = "single_agent"


TASK_CAPABILITIES: tuple[TaskCapability, ...] = (
    TaskCapability(
        TaskType.create,
        "Create",
        "write",
        default_category="build",
        user_launchable=True,
        delivers_product=True,
        description="Implement new functionality and open a PR.",
        output_kind="pr",
        evaluation_strategy="ci_and_review",
        cost_ceiling_usd=8.0,
        evaluation="code-ci-review",
        run_shape="workflow",
    ),
    TaskCapability(
        TaskType.review,
        "Review",
        "read_only",
        default_category="review",
        user_launchable=True,
        description="Review an existing PR.",
        output_kind="verdict",
        evaluation_strategy="review_only",
        cost_ceiling_usd=3.0,
        evaluation="none",
    ),
    TaskCapability(
        TaskType.fix,
        "Fix",
        "write",
        default_category="review",
        user_launchable=True,
        delivers_product=True,
        description="Fix a PR from review feedback or CI failures.",
        output_kind="pr",
        evaluation_strategy="ci_and_review",
        cost_ceiling_usd=8.0,
        evaluation="code-ci-review",
        run_shape="workflow",
    ),
    TaskCapability(
        TaskType.plan,
        "Plan",
        "read_only",
        default_category="strategic",
        user_launchable=True,
        description="Decompose work into subtasks.",
        output_kind="knowledge",
        evaluation_strategy="review_only",
        cost_ceiling_usd=3.0,
        completion_handler="plan",
        evaluation="none",
    ),
    TaskCapability(
        TaskType.research,
        "Research",
        "read_only",
        default_category="strategic",
        user_launchable=True,
        description="Analyze the codebase and produce findings.",
        output_kind="knowledge",
        requires_web=True,
        evaluation_strategy="forward_quality",
        cost_ceiling_usd=4.0,
        completion_handler="research",
        evaluation="forward-quality",
    ),
    TaskCapability(
        TaskType.strategic_review,
        "Strategic Review",
        "read_only",
        default_category="strategic",
        description="Evaluate project direction and propose strategy updates.",
        output_kind="knowledge",
        evaluation_strategy="review_only",
        cost_ceiling_usd=4.0,
        completion_handler="strategic",
        completion_needs_locked_project=True,
        strategic_phase="phase2",
        evaluation="forward-quality",
    ),
    TaskCapability(
        TaskType.design_doc_update,
        "Design Doc",
        "read_only",
        default_category="strategic",
        description="Update the living project design document.",
        output_kind="artifact",
        evaluation_strategy="review_only",
        cost_ceiling_usd=3.0,
        completion_handler="design_doc",
        completion_needs_locked_project=True,
        strategic_phase="phase1",
        evaluation="forward-quality",
    ),
    TaskCapability(
        TaskType.security_audit,
        "Security Audit",
        "read_only",
        default_category="review",
        description="Audit for security risks without changing code.",
        output_kind="knowledge",
        evaluation_strategy="review_only",
        cost_ceiling_usd=4.0,
        completion_handler="security_audit",
        evaluation="forward-quality",
    ),
    TaskCapability(
        TaskType.qa,
        "QA",
        "write",
        default_category="review",
        description="Add or improve test coverage.",
        output_kind="pr",
        evaluation_strategy="ci_and_review",
        cost_ceiling_usd=6.0,
        evaluation="code-ci-review",
        run_shape="workflow",
    ),
    TaskCapability(
        TaskType.deps,
        "Deps",
        "write",
        default_category="ops",
        description="Update dependencies and verify compatibility.",
        output_kind="pr",
        evaluation_strategy="ci_and_review",
        cost_ceiling_usd=5.0,
        evaluation="code-ci-review",
        run_shape="workflow",
    ),
    TaskCapability(
        TaskType.docs,
        "Docs",
        "write",
        default_category="ops",
        description="Write or update documentation.",
        output_kind="pr",
        evaluation_strategy="review_only",
        cost_ceiling_usd=4.0,
        evaluation="code-ci-review",
        run_shape="workflow",
    ),
    TaskCapability(
        TaskType.migrate,
        "Migrate",
        "write",
        default_category="build",
        user_launchable=True,
        delivers_product=True,
        description="Create or update database migrations.",
        output_kind="pr",
        evaluation_strategy="ci_and_review",
        cost_ceiling_usd=6.0,
        evaluation="code-ci-review",
        run_shape="workflow",
    ),
    TaskCapability(
        TaskType.smoketest,
        "Smoketest",
        "read_only",
        default_category="ops",
        description="Run smoke checks against a deployment.",
        output_kind="verdict",
        requires_compute=True,
        evaluation_strategy="metric",
        cost_ceiling_usd=2.0,
        evaluation="none",
    ),
    TaskCapability(
        TaskType.refactor,
        "Refactor",
        "write",
        default_category="build",
        user_launchable=True,
        description="Improve code structure without changing behavior.",
        output_kind="pr",
        evaluation_strategy="ci_and_review",
        cost_ceiling_usd=8.0,
        evaluation="code-ci-review",
        run_shape="workflow",
    ),
    TaskCapability(
        TaskType.debug,
        "Debug",
        "read_only",
        default_category="review",
        description="Investigate a failure or bug report.",
        output_kind="knowledge",
        evaluation_strategy="review_only",
        cost_ceiling_usd=4.0,
        evaluation="forward-quality",
    ),
    TaskCapability(
        TaskType.perf,
        "Perf",
        "read_only",
        default_category="review",
        description="Measure and diagnose performance bottlenecks.",
        output_kind="knowledge",
        requires_compute=True,
        evaluation_strategy="metric",
        cost_ceiling_usd=5.0,
        completion_handler="perf",
        evaluation="metric",
    ),
    TaskCapability(
        TaskType.investigate,
        "Investigate",
        "read_only",
        description="Open-ended read-only investigation.",
        output_kind="knowledge",
        evaluation_strategy="review_only",
        cost_ceiling_usd=4.0,
        evaluation="forward-quality",
    ),
    TaskCapability(
        TaskType.self_audit,
        "Self Audit",
        "read_only",
        description="Score system maturity and recommend operating mode.",
        output_kind="knowledge",
        evaluation_strategy="review_only",
        cost_ceiling_usd=3.0,
        completion_handler="self_audit",
        evaluation="forward-quality",
    ),
    TaskCapability(
        TaskType.release_audit,
        "Release Audit",
        "read_only",
        description="Differential audit of the promotion-candidate commit against the base branch for the release qualifier.",
        output_kind="verdict",
        evaluation_strategy="review_only",
        cost_ceiling_usd=4.0,
        evaluation="forward-quality",
    ),
    TaskCapability(
        TaskType.memo,
        "Memo",
        "read_only",
        default_category="strategic",
        user_launchable=False,
        provider_configurable=True,
        filter_visible=True,
        description=(
            "Terminal reasoning artifact: persist structured reasoning about a "
            "topic without opening a PR or spawning follow-up tasks. Strategic "
            "review can cite the resulting Artifact by id."
        ),
        output_kind="artifact",
        requires_code=False,
        evaluation_strategy="none",
        cost_ceiling_usd=2.0,
        completion_handler="memo",
        evaluation="forward-quality",
    ),
    TaskCapability(
        TaskType.verify_tests,
        "Verify Tests",
        "read_only",
        default_category="review",
        user_launchable=False,
        description=(
            "Clone a PR head, run the repo's detected test suite, and post a "
            "structured pass/fail verdict the no-CI merge gate can consult."
        ),
        output_kind="verdict",
        requires_compute=True,
        evaluation_strategy="metric",
        cost_ceiling_usd=3.0,
        evaluation="none",
    ),
    TaskCapability(
        TaskType.compute_experiment,
        "Compute Experiment",
        "read_only",
        default_category="ops",
        user_launchable=False,
        # No LLM provider runs — the handler is a programmatic launch+poll loop,
        # so it never selects a model/account.
        provider_configurable=False,
        description=(
            "Reference compute-heavy task: launch a budgeted, auto-torn-down "
            "compute job on the compute substrate and monitor it to terminal "
            "state. The agent only launches and polls — it never holds the heavy "
            "VM. Reports the job outcome and final cost; budget/deadline kills "
            "are intentional terminal outcomes, not crashes."
        ),
        # This reference type runs the long-running experiment_loop shape: the
        # agent launches a budgeted job and polls it to terminal state. It needs
        # the compute substrate, not a code checkout, and its durable product is
        # a verdict (the terminal job outcome + final cost), graded by a metric
        # forward gate that fails an experiment which produced no terminal,
        # costed result. Dispatch is by the bespoke agent handler, so the
        # experiment_loop run_shape is declarative only and does not change how
        # the task is claimed/executed.
        output_kind="verdict",
        requires_code=False,
        requires_compute=True,
        evaluation_strategy="metric",
        cost_ceiling_usd=5.0,
        completion_handler="compute_experiment",
        evaluation="metric",
        run_shape="experiment_loop",
    ),
    TaskCapability(
        TaskType.web_research,
        "Web Research",
        "read_only",
        default_category="strategic",
        user_launchable=True,
        description=(
            "Web-first research that produces a durable, queryable knowledge "
            "document (with web citations) into the knowledge-document store."
        ),
        output_kind="knowledge",
        requires_code=False,
        requires_web=True,
        evaluation_strategy="forward_quality",
        cost_ceiling_usd=4.0,
        completion_handler="web_research",
        evaluation="forward-quality",
    ),
)

TASK_CAPABILITY_BY_TYPE: dict[TaskType, TaskCapability] = {
    capability.id: capability for capability in TASK_CAPABILITIES
}
TASK_CAPABILITY_BY_VALUE: dict[str, TaskCapability] = {
    capability.id.value: capability for capability in TASK_CAPABILITIES
}

# Backward-compatible constants consumed by existing code.
ALL_TASK_TYPES: frozenset[str] = frozenset(
    capability.id.value for capability in TASK_CAPABILITIES
)
READ_ONLY_TASK_TYPES: frozenset[str] = frozenset(
    capability.id.value
    for capability in TASK_CAPABILITIES
    if capability.agent_mode == "read_only"
)
# Canonical registry of task types that run the agent in *write* mode — i.e. they
# push a branch and open a PR. This is the SINGLE source of truth for "does this
# task type produce a branch/PR?" and MUST be the only edit point for that
# question. The orphan-recovery / reaped-but-PR-exists sweeps (services.py), the
# completion review-pipeline router (task_completion.py / task_lifecycle.py) and
# any other dispatch allowlist derive their membership from this set rather than
# hardcoding string-literal tuples — so a newly-added write task type is honored
# everywhere automatically. NOTE: this is intentionally distinct from the various
# analytics filters (e.g. "code-change" = {create, fix, refactor, migrate} for SR
# metrics, "review-eligible" = {create, refactor}); those measure a narrower thing
# than "opens a PR" and are deliberately NOT folded in here.
WRITE_TASK_TYPES: frozenset[str] = frozenset(
    capability.id.value
    for capability in TASK_CAPABILITIES
    if capability.agent_mode == "write"
)
# Canonical registry of task types that directly ship user-facing PRODUCT value
# (as opposed to structural / overhead work). This is the SINGLE source of truth
# for the product/structural split used by direction-outcome analytics
# (direction_outcomes.py product_ratio / structural_count) and the digest /
# dashboard surfaces that read those pre-computed values. A new task type — code
# or non-code — becomes "product" simply by declaring delivers_product=True on
# its capability; no edit at any metric call site is required. Unknown /
# unregistered task types (get_task_capability() -> None) are NOT in this set and
# therefore default to STRUCTURAL, which is the conservative, documented default.
# NOTE: this is a deliberately independent dimension from agent_mode /
# WRITE_TASK_TYPES — a write task (e.g. refactor, qa, docs) is not necessarily
# product-shipping.
PRODUCT_TASK_TYPES: frozenset[str] = frozenset(
    capability.id.value
    for capability in TASK_CAPABILITIES
    if capability.delivers_product
)
USER_LAUNCHABLE_TASK_TYPES: tuple[str, ...] = tuple(
    capability.id.value
    for capability in TASK_CAPABILITIES
    if capability.user_launchable
)
PROVIDER_CONFIGURABLE_TASK_TYPES: tuple[str, ...] = tuple(
    capability.id.value
    for capability in TASK_CAPABILITIES
    if capability.provider_configurable
)
FILTER_VISIBLE_TASK_TYPES: tuple[str, ...] = tuple(
    capability.id.value for capability in TASK_CAPABILITIES if capability.filter_visible
)
# Map of task-type value -> declared completion-handler logical name, derived
# from the registry. This REPLACES the hand-maintained if/elif completion
# cascade in task_lifecycle._process_post_commit: that module binds each
# logical name to its concrete handler function and dispatches via this map, so
# adding a type with a completion handler is a single registry edit. Types with
# no dedicated handler are absent from this map.
COMPLETION_HANDLER_BY_TASK_TYPE: dict[str, str] = {
    capability.id.value: capability.completion_handler
    for capability in TASK_CAPABILITIES
    if capability.completion_handler is not None
}
# Map of task-type value -> declared forward-evaluation strategy key, derived
# from the registry. This is the SINGLE source of truth the evaluator framework
# (``app.task_evaluators.resolve_evaluation_strategy``) reads to select a
# strategy — selection is registry-driven, never a hardwired type→strategy
# if/elif. Every task type is present (the field is non-optional with a
# non-gating default).
EVALUATION_STRATEGY_BY_TASK_TYPE: dict[str, str] = {
    capability.id.value: capability.evaluation for capability in TASK_CAPABILITIES
}
# Map of task-type value -> declared run-shape, derived from the registry. This
# is the SINGLE source of truth for "how does this type run?" that future
# dispatchers (the multi-agent-workflow engine, the experiment-loop engine) read
# instead of re-deriving the shape. Every task type is present (the field is
# non-optional with a "single_agent" default). Resolved via ``run_shape_for``.
RUN_SHAPE_BY_TASK_TYPE: dict[str, str] = {
    capability.id.value: capability.run_shape for capability in TASK_CAPABILITIES
}
# Canonical registry-derived set of "strategic-style" task types — those whose
# completion runs the strategic-cycle chain under the canonical Project→Task
# lock (``completion_needs_locked_project=True``, currently design_doc_update
# and strategic_review). This is the SINGLE source of truth for the question
# "is this a strategic task type?" that was previously hardcoded as the literal
# tuple ``("design_doc_update", "strategic_review")`` across the post-commit
# lock-ordering peek, the concurrent-strategic spawn guards, the
# strategic-failure circuit breaker, the lifecycle backstop, and the
# active-strategic-task DB queries. Adding a new strategic-style task type is
# now a single registry edit (declare ``completion_needs_locked_project=True``
# plus a ``strategic_phase``) rather than a hunt-and-edit across modules.
STRATEGIC_TASK_TYPES: frozenset[str] = frozenset(
    capability.id.value
    for capability in TASK_CAPABILITIES
    if capability.completion_needs_locked_project
)
# Map of strategic task-type value -> its declared two-phase-cycle ordering
# ("phase1"/"phase2"), derived from the registry. Non-strategic types are
# absent. Resolved via ``strategic_phase_for`` at the failure/backstop sites.
STRATEGIC_PHASE_BY_TASK_TYPE: dict[str, str] = {
    capability.id.value: capability.strategic_phase
    for capability in TASK_CAPABILITIES
    if capability.strategic_phase is not None
}


def strategic_phase_for(task_type: str | TaskType) -> str | None:
    """Return the declared strategic-cycle phase ("phase1"/"phase2") for a task
    type, or ``None`` if it is not a strategic-style type.

    Callers that historically defaulted a non-design_doc strategic task to
    "phase2" preserve that behavior with ``strategic_phase_for(t) or "phase2"``.
    """
    capability = get_task_capability(task_type)
    return capability.strategic_phase if capability is not None else None


def run_shape_for(task_type: str | TaskType) -> str | None:
    """Return the declared run-shape ("single_agent"/"workflow"/"experiment_loop")
    for a task type, or ``None`` if the type is unknown/unregistered.

    This is the single registry accessor future dispatchers consult to learn how
    a type runs, mirroring ``strategic_phase_for``.
    """
    capability = get_task_capability(task_type)
    return capability.run_shape if capability is not None else None


def get_task_capability(task_type: str | TaskType) -> TaskCapability | None:
    """Return metadata for a known task type, or None for future/unknown types."""
    value = task_type.value if isinstance(task_type, TaskType) else task_type
    return TASK_CAPABILITY_BY_VALUE.get(value)


def agent_capability_slice(task_type: str | TaskType) -> dict | None:
    """Return the agent-facing capability slice for a claimed task type."""
    capability = get_task_capability(task_type)
    if capability is None:
        return None
    return {
        "run_shape": capability.run_shape,
        "agent_mode": capability.agent_mode,
        "output_kind": capability.output_kind,
        "requires_code": capability.requires_code,
        "requires_web": capability.requires_web,
        "requires_compute": capability.requires_compute,
        "is_bespoke_dispatch": False,
    }


def _capability_to_catalog_entry(capability: TaskCapability) -> dict:
    """Serialize one capability into a plain JSON-safe catalog entry.

    The shape here is the single backend source of truth for the operator
    task-type catalog and the admin-ui launcher. Keep field names in sync with
    the frontend `TaskTypeCatalogEntry` type in admin-ui/src/api.ts.
    """
    return {
        "id": capability.id.value,
        "label": capability.label,
        "description": capability.description,
        "agent_mode": capability.agent_mode,
        "default_category": capability.default_category,
        "user_launchable": capability.user_launchable,
        "provider_configurable": capability.provider_configurable,
        "filter_visible": capability.filter_visible,
        "output_kind": capability.output_kind,
        "requires_code": capability.requires_code,
        "requires_web": capability.requires_web,
        "requires_compute": capability.requires_compute,
        "evaluation_strategy": capability.evaluation_strategy,
        "cost_ceiling_usd": capability.cost_ceiling_usd,
        "run_shape": capability.run_shape,
    }


def task_type_catalog() -> list[dict]:
    """Return the full task-type registry as a list of plain dicts.

    Pure and static (no DB access) — safe to serve directly from a cheap GET.
    Ordering follows the declaration order in ``TASK_CAPABILITIES`` so the
    operator catalog renders deterministically.
    """
    return [_capability_to_catalog_entry(c) for c in TASK_CAPABILITIES]
