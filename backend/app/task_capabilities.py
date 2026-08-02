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
    # NOTE: The autonomous resident task type (resident_turn) was permanently
    # removed in migration 0657. Do NOT re-add it here or in any dispatch /
    # spawning module — the Workday protection suite scans the full
    # backend/app tree and agent/src/handlers/ for its literal value and
    # will fail CI if it reappears.
    create = "create"
    review = "review"
    fix = "fix"
    plan = "plan"
    research = "research"
    discovery_review = "discovery_review"
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
    release_audit_challenge = "release_audit_challenge"
    release_merge_grade = "release_merge_grade"
    memo = "memo"
    verify_tests = "verify_tests"
    compute_experiment = "compute_experiment"
    web_research = "web_research"
    design = "design"
    repo_bootstrap = "repo_bootstrap"
    salvage_bundle = "salvage_bundle"
    capability_proof_trial = "capability_proof_trial"
    walkthrough_e2e = "walkthrough_e2e"
    memory_consolidation = "memory_consolidation"


AgentMode = Literal["write", "read_only"]
ExecutionOwner = Literal["agent", "backend"]
WorkspaceRequirement = Literal["repo", "none"]

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
#   "artifact-delivery" — a non-gating durable-artifact/resource delivery check.
#       A missing terminal artifact fails, but measured negative results pass
#       with the metric outcome recorded as evidence.
#   "structured-verdict" — a strict parse/shape gate for multi-lens verdict
#       artifacts. Empty, unparseable, or incomplete verdicts fail.
#   "none" — no forward gate (plan and any type with no meaningful artifact
#       gate). Always passes.
EvaluationStrategyKey = Literal[
    "code-ci-review",
    "forward-quality",
    "metric",
    "artifact-delivery",
    "structured-verdict",
    "none",
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
#   experiment_loop— a long-running, iterated experiment loop (the reference
#       compute_experiment task runs here). The closed vocabulary keeps the
#       shape declarable without requiring a new enum value later.
RunShape = Literal["single_agent", "workflow", "experiment_loop"]


@dataclass(frozen=True)
class TaskCapability:
    id: TaskType
    label: str
    agent_mode: AgentMode
    # Which runtime owns execution. ``agent`` is the normal provision/claim
    # path; ``backend`` is reserved for bounded control-plane tasks that must
    # never be handed to an agent process (completed by a zero-model backend
    # sweep).
    execution_owner: ExecutionOwner = "agent"
    default_category: str | None = None
    user_launchable: bool = False
    operator_contract_launchable: bool = False
    provider_configurable: bool = True
    # Optional hard routing boundary for task capabilities whose execution
    # sandbox has only been qualified for a subset of providers.  Unlike a
    # routing preference, this is enforced after every runtime-config layer
    # (including routing experiments) and cannot be widened by an operator
    # setting or stale per-task runtime identity.
    allowed_providers: frozenset[str] | None = None
    filter_visible: bool = True
    description: str = ""
    # --- Enriched declarative registry fields (operator catalog + eval routing).
    # All default so existing literals and derived frozensets stay valid; each
    # capability below sets them explicitly.
    run_shape: RunShape = "single_agent"
    output_kind: OutputKind = "none"
    requires_code: bool = True
    requires_web: bool = False
    requires_compute: bool = False
    # ``none`` is an explicit opt-out for narrow, task-JWT-scoped workers that
    # never clone or touch the project's repository. Any omitted/unknown value
    # remains repository-backed by default in the agent runtime.
    workspace_requirement: WorkspaceRequirement = "repo"
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
    # Whether an operator may reserve pipeline slots for this task type via the
    # `reserved_slots_for_types` settings map. This is the SINGLE source of truth
    # for reserved-slot eligibility — the PATCH validator derives its allowlist
    # from RESERVABLE_TASK_TYPES rather than hardcoding a tuple, so a newly
    # registered reservable type is accepted automatically.
    reservable: bool = False
    # Whether this task type can be scheduled as an autonomous periodic task via
    # the `autonomous_periodic_tasks` settings map. SINGLE source of truth for
    # periodic eligibility — see PERIODIC_TASK_TYPES.
    periodic_eligible: bool = False
    # Whether a completing plan task may spawn this type as a fresh implementation
    # subtask. SINGLE source of truth for the plan-decomposition child allowlist
    # (plan_completion.py) — see PLAN_SUBTASK_TYPES. This is the PR-producing write
    # types that can start from a fresh branch with only a prompt; it is
    # deliberately NARROWER than WRITE_TASK_TYPES because some write types (notably
    # `fix`) require a pre-existing PR context (pr_number/branch) a plan cannot
    # supply, so they are not valid fresh plan children.
    plan_subtask_eligible: bool = False
    # Whether a completing plan task may spawn this type as a knowledge-prerequisite
    # subtask — a non-PR-producing child that yields a durable knowledge document
    # (web_research / design) which a downstream implementation child can depend on.
    # SINGLE source of truth for the plan-decomposition KNOWLEDGE child allowlist
    # (plan_completion.py) — see PLAN_KNOWLEDGE_SUBTASK_TYPES. This is DELIBERATELY
    # disjoint from plan_subtask_eligible: a knowledge prerequisite does NOT push a
    # branch/PR, has no pre-existing PR context, and must NOT be classified as a
    # PR-producing implementation subtype, so it carries its own separate flag
    # rather than widening PLAN_SUBTASK_TYPES.
    plan_knowledge_prereq_eligible: bool = False
    # Whether Strategic may dispatch this type through the unified spawn_task
    # contract. ``direct`` needs only a valid task payload; ``requires_context``
    # additionally requires the type's normal launch context (currently a PR or
    # branch for ``fix``). Direction association is optional for either policy.
    strategist_dispatch: Literal["disabled", "direct", "requires_context"] = (
        "disabled"
    )
    # Short presentation badge for compact operator surfaces (task cards, filter
    # chips). Defaults to ``label`` when unset via the published contract.
    badge: str | None = None


TASK_CAPABILITIES: tuple[TaskCapability, ...] = (
    TaskCapability(
        TaskType.create,
        "Create",
        "write",
        default_category="build",
        user_launchable=True,
        delivers_product=True,
        reservable=True,
        plan_subtask_eligible=True,
        strategist_dispatch="direct",
        description="Implement new functionality and open a PR.",
        run_shape="workflow",
        output_kind="pr",
        evaluation_strategy="ci_and_review",
        cost_ceiling_usd=8.0,
        evaluation="code-ci-review",
    ),
    TaskCapability(
        TaskType.review,
        "Review",
        "read_only",
        default_category="review",
        user_launchable=True,
        description="Review an existing PR.",
        run_shape="single_agent",
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
        strategist_dispatch="requires_context",
        description="Fix a PR from review feedback or CI failures.",
        run_shape="workflow",
        output_kind="pr",
        evaluation_strategy="ci_and_review",
        cost_ceiling_usd=8.0,
        evaluation="code-ci-review",
    ),
    TaskCapability(
        TaskType.plan,
        "Plan",
        "read_only",
        default_category="strategic",
        user_launchable=True,
        reservable=True,
        strategist_dispatch="direct",
        description="Decompose work into subtasks.",
        run_shape="single_agent",
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
        reservable=True,
        strategist_dispatch="direct",
        description="Analyze the codebase and produce findings.",
        run_shape="single_agent",
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
        run_shape="single_agent",
        output_kind="knowledge",
        evaluation_strategy="review_only",
        cost_ceiling_usd=4.0,
        completion_handler="strategic",
        completion_needs_locked_project=True,
        strategic_phase="phase2",
        evaluation="forward-quality",
    ),
    TaskCapability(
        TaskType.discovery_review,
        "Discovery Review",
        "read_only",
        default_category="strategic",
        description=(
            "Judge a bounded discovery cycle against its frozen bet and evidence."
        ),
        run_shape="single_agent",
        output_kind="verdict",
        evaluation_strategy="review_only",
        cost_ceiling_usd=4.0,
        completion_handler="discovery_review",
        evaluation="structured-verdict",
    ),
    TaskCapability(
        TaskType.design_doc_update,
        "Design Doc",
        "read_only",
        default_category="strategic",
        description="Update the living project design document.",
        run_shape="single_agent",
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
        periodic_eligible=True,
        description="Audit for security risks without changing code.",
        run_shape="single_agent",
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
        periodic_eligible=True,
        plan_subtask_eligible=True,
        strategist_dispatch="direct",
        description="Add or improve test coverage.",
        run_shape="workflow",
        output_kind="pr",
        evaluation_strategy="ci_and_review",
        cost_ceiling_usd=6.0,
        evaluation="code-ci-review",
    ),
    TaskCapability(
        TaskType.deps,
        "Deps",
        "write",
        default_category="ops",
        periodic_eligible=True,
        plan_subtask_eligible=True,
        description="Update dependencies and verify compatibility.",
        run_shape="workflow",
        output_kind="pr",
        evaluation_strategy="ci_and_review",
        cost_ceiling_usd=5.0,
        evaluation="code-ci-review",
    ),
    TaskCapability(
        TaskType.docs,
        "Docs",
        "write",
        default_category="ops",
        plan_subtask_eligible=True,
        description="Write or update documentation.",
        run_shape="workflow",
        output_kind="pr",
        evaluation_strategy="review_only",
        cost_ceiling_usd=4.0,
        evaluation="code-ci-review",
    ),
    TaskCapability(
        TaskType.migrate,
        "Migrate",
        "write",
        default_category="build",
        user_launchable=True,
        delivers_product=True,
        plan_subtask_eligible=True,
        description="Create or update database migrations.",
        run_shape="workflow",
        output_kind="pr",
        evaluation_strategy="ci_and_review",
        cost_ceiling_usd=6.0,
        evaluation="code-ci-review",
    ),
    TaskCapability(
        TaskType.smoketest,
        "Smoketest",
        "read_only",
        default_category="ops",
        description="Run smoke checks against a deployment.",
        run_shape="single_agent",
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
        plan_subtask_eligible=True,
        strategist_dispatch="direct",
        description="Improve code structure without changing behavior.",
        run_shape="workflow",
        output_kind="pr",
        evaluation_strategy="ci_and_review",
        cost_ceiling_usd=8.0,
        evaluation="code-ci-review",
    ),
    TaskCapability(
        TaskType.debug,
        "Debug",
        "read_only",
        default_category="review",
        description="Investigate a failure or bug report.",
        run_shape="single_agent",
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
        run_shape="single_agent",
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
        run_shape="single_agent",
        output_kind="knowledge",
        # Operator-launchable so a bug report can be turned into a targeted
        # read-only investigation directly (the citation gate in
        # admin.create_task_manually re-validates the originating message's
        # citations against HEAD). Read-only + prompt-only contract — no launch
        # metadata beyond the prompt the manual creation path already supplies.
        user_launchable=True,
        evaluation_strategy="review_only",
        cost_ceiling_usd=4.0,
        evaluation="forward-quality",
    ),
    TaskCapability(
        TaskType.self_audit,
        "Self Audit",
        "read_only",
        description="Score system maturity and recommend operating mode.",
        run_shape="single_agent",
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
        run_shape="single_agent",
        output_kind="verdict",
        evaluation_strategy="review_only",
        cost_ceiling_usd=4.0,
        evaluation="forward-quality",
    ),
    TaskCapability(
        TaskType.release_audit_challenge,
        "Release Audit Challenge",
        "read_only",
        description=(
            "Binding, independent challenger pass over a release-audit "
            "candidate: re-examines the same pinned candidate/diff and emits "
            "structured verdicts over higher-severity primary findings."
        ),
        run_shape="single_agent",
        output_kind="verdict",
        evaluation_strategy="review_only",
        cost_ceiling_usd=4.0,
        # No forward gate: the deterministic release qualifier consumes the
        # challenge artifact directly instead of grading it through a generic
        # terminal-artifact forward-quality check here.
        evaluation="none",
    ),
    TaskCapability(
        TaskType.release_merge_grade,
        "Release Merge Grade",
        "read_only",
        description=(
            "Blind goal-coverage grade: an uninvested agent grades the merged "
            "diffs of a release version against the frozen version goal — blind "
            "to the strategist running_update — and names the single biggest "
            "thing not done toward the goal."
        ),
        run_shape="single_agent",
        output_kind="verdict",
        evaluation_strategy="review_only",
        cost_ceiling_usd=4.0,
        # No forward gate: the deterministic release qualifier consumes the
        # merge-grade artifact directly as a hard promotion blocker.
        evaluation="none",
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
        run_shape="single_agent",
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
        run_shape="single_agent",
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
        operator_contract_launchable=True,
        plan_knowledge_prereq_eligible=True,
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
        # the delivered COMPUTE_RESULT artifact/resource. The forward gate checks
        # only that a terminal, costed result was delivered; success criteria are
        # recorded as evidence and do not block artifact-resource completion.
        # Dispatch is by the bespoke agent handler, so the experiment_loop
        # run_shape is declarative only and does not change how the task is
        # claimed/executed.
        output_kind="artifact",
        requires_code=False,
        requires_compute=True,
        evaluation_strategy="none",
        cost_ceiling_usd=5.0,
        completion_handler="compute_experiment",
        evaluation="artifact-delivery",
        run_shape="experiment_loop",
        strategist_dispatch="direct",
    ),
    TaskCapability(
        TaskType.web_research,
        "Web Research",
        "read_only",
        default_category="strategic",
        user_launchable=True,
        strategist_dispatch="direct",
        plan_knowledge_prereq_eligible=True,
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
    TaskCapability(
        TaskType.design,
        "Design",
        "read_only",
        default_category="strategic",
        user_launchable=True,
        strategist_dispatch="direct",
        plan_knowledge_prereq_eligible=True,
        description=(
            "Codebase-internal design work: ground a design question in the "
            "actual code and produce a durable, queryable design document (with "
            "file-reference citations) into the knowledge-document store. Unlike "
            "web_research it sources from the repo, not the web."
        ),
        output_kind="knowledge",
        requires_code=True,
        requires_web=False,
        evaluation_strategy="forward_quality",
        cost_ceiling_usd=4.0,
        completion_handler="design",
        evaluation="forward-quality",
    ),
    TaskCapability(
        TaskType.repo_bootstrap,
        "Repo Bootstrap",
        "read_only",
        default_category="strategic",
        description=(
            "One-time bootstrap for new external repos: reads project structure, "
            "confirms build/test commands, and seeds design doc sections before "
            "implementation tasks fan out."
        ),
        run_shape="single_agent",
        output_kind="none",
        requires_code=True,
        requires_web=False,
        evaluation_strategy="none",
        cost_ceiling_usd=3.0,
        completion_handler="repo_bootstrap",
        completion_needs_locked_project=False,
        strategic_phase=None,
        filter_visible=False,
        evaluation="none",
    ),
    TaskCapability(
        TaskType.salvage_bundle,
        "Salvage Bundle",
        "write",
        description=(
            "Consolidated fix batch for research findings discarded via "
            "repeated deferral decay. Produces a single PR addressing "
            "all salvaged findings as one consolidated batch."
        ),
        run_shape="workflow",
        output_kind="pr",
        evaluation_strategy="ci_and_review",
        cost_ceiling_usd=8.0,
        evaluation="code-ci-review",
    ),
    TaskCapability(
        TaskType.capability_proof_trial,
        "Capability Proof Trial",
        "read_only",
        default_category="ops",
        user_launchable=False,
        # No LLM provider runs — the handler runs a bounded, targeted test suite
        # (the capability's declared convention-named covering tests) and reports a
        # pass/fail verdict; it never selects a model/account.
        provider_configurable=False,
        # Not operator/strategist-spawnable and not a plan child: it is enrolled
        # programmatically by a later slice's sweep (via
        # ``capability_proof.enroll_capability_proof_trial``), never authored by an
        # agent or operator.
        description=(
            "Machine-verified capability-proof trial: run ONLY a working "
            "capability's declared convention-named covering tests as a targeted "
            "runner job, decoupled from the merge gate. A green run mints a genuine "
            "``proven`` named_test proof into the capability-proof ledger; a red run "
            "records nothing and NEVER demotes an existing proof. Operators can "
            "pause it with ``capability_proof_execution_trials_enabled``."
        ),
        # A single targeted agent run produces the verdict; it needs a code
        # checkout to run the tests, but opens no PR — its durable product is the
        # ingested proof, so output_kind is ``none`` and there is no forward merge
        # gate (``evaluation="none"``). The ingestion completion handler consumes
        # the reported result.
        output_kind="none",
        requires_code=True,
        evaluation_strategy="metric",
        cost_ceiling_usd=2.0,
        completion_handler="capability_proof_trial",
        evaluation="none",
        run_shape="single_agent",
    ),
    TaskCapability(
        TaskType.walkthrough_e2e,
        "Walkthrough E2E",
        "read_only",
        default_category="ops",
        user_launchable=False,
        description=(
            "Opt-in qualifier live-stack walkthrough whose SHA-pinned report "
            "and CriterionEvidence directly gate that qualification attempt."
        ),
        run_shape="single_agent",
        output_kind="verdict",
        requires_compute=True,
        evaluation_strategy="metric",
        cost_ceiling_usd=4.0,
        completion_handler="walkthrough_e2e",
        evaluation="none",
    ),
    TaskCapability(
        TaskType.memory_consolidation,
        "Memory Consolidation",
        "read_only",
        default_category="strategic",
        user_launchable=False,
        operator_contract_launchable=False,
        provider_configurable=False,
        allowed_providers=frozenset({"anthropic"}),
        filter_visible=False,
        description=(
            "Receipt-bound, budgeted Strategic Memory consolidation. Runs with a "
            "task-scoped model account but never receives repository credentials "
            "or a project checkout."
        ),
        run_shape="single_agent",
        output_kind="none",
        requires_code=False,
        workspace_requirement="none",
        evaluation_strategy="none",
        cost_ceiling_usd=0.25,
        evaluation="none",
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
AGENT_EXECUTED_TASK_TYPES: frozenset[str] = frozenset(
    capability.id.value
    for capability in TASK_CAPABILITIES
    if capability.execution_owner == "agent"
)
BACKEND_MANAGED_TASK_TYPES: frozenset[str] = frozenset(
    capability.id.value
    for capability in TASK_CAPABILITIES
    if capability.execution_owner == "backend"
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
# Canonical registry of task types an operator may reserve pipeline slots for
# (the `reserved_slots_for_types` settings map). SINGLE source of truth for that
# settings surface's allowlist — the PATCH validator derives from this set so a
# newly registered reservable type is honored without editing the validator.
RESERVABLE_TASK_TYPES: frozenset[str] = frozenset(
    capability.id.value for capability in TASK_CAPABILITIES if capability.reservable
)
# Canonical registry of task types eligible for autonomous periodic scheduling
# (the `autonomous_periodic_tasks` settings map). SINGLE source of truth for that
# settings surface's allowlist.
PERIODIC_TASK_TYPES: frozenset[str] = frozenset(
    capability.id.value
    for capability in TASK_CAPABILITIES
    if capability.periodic_eligible
)
# Canonical registry of task types a completing plan task may spawn as a fresh
# implementation subtask. SINGLE source of truth for the plan-decomposition child
# allowlist (plan_completion.py) — the gate derives from this set so a newly
# registered fresh-startable write type becomes a valid plan child by declaration
# alone (replacing the hardcoded {create, refactor, migrate, qa} literal). This is
# intentionally a SUBSET of WRITE_TASK_TYPES: `fix` is a write/PR type but needs a
# pre-existing PR context (pr_number/branch) a plan cannot supply, so it is not a
# valid fresh plan child and does not declare plan_subtask_eligible.
PLAN_SUBTASK_TYPES: frozenset[str] = frozenset(
    capability.id.value
    for capability in TASK_CAPABILITIES
    if capability.plan_subtask_eligible
)
# Canonical registry of task types a completing plan task may spawn as a
# KNOWLEDGE-PREREQUISITE subtask. SINGLE source of truth for the plan-decomposition
# knowledge-child allowlist (plan_completion.py). These types (web_research, design)
# produce a durable knowledge document, NOT a branch/PR, and have no pre-existing PR
# context — so a downstream implementation child can validly depend on the knowledge
# they yield before code work proceeds. This set is DELIBERATELY DISJOINT from
# PLAN_SUBTASK_TYPES (the PR-producing write/implementation subtypes): a knowledge
# prerequisite is read-only and must NOT be classified as PR-producing, so it is
# derived from a separate ``plan_knowledge_prereq_eligible`` flag rather than folded
# into PLAN_SUBTASK_TYPES. A newly registered knowledge type becomes a valid plan
# prerequisite by declaring that flag alone.
PLAN_KNOWLEDGE_SUBTASK_TYPES: frozenset[str] = frozenset(
    capability.id.value
    for capability in TASK_CAPABILITIES
    if capability.plan_knowledge_prereq_eligible
)
# Canonical registry of task types the autonomous strategist may dispatch through
# spawn_task without extra launch context.
STRATEGIST_SPAWNABLE_TASK_TYPES: frozenset[str] = frozenset(
    capability.id.value
    for capability in TASK_CAPABILITIES
    if capability.strategist_dispatch == "direct"
)
# Task types accepted by spawn_task only when their normal launch context is
# present. Kept separate from the direct set so the router can emit a precise
# validation error rather than silently constructing an unusable task.
STRATEGIST_CONTEXTUAL_TASK_TYPES: frozenset[str] = frozenset(
    capability.id.value
    for capability in TASK_CAPABILITIES
    if capability.strategist_dispatch == "requires_context"
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
# Canonical registry-derived set of task types whose output ships through the
# CI-green + reviewer-sign-off code-review merge gate (``evaluation ==
# "code-ci-review"``). This is the SINGLE source of truth for "does this type's
# work get reviewed like code?" — currently {create, fix, refactor, migrate, qa,
# deps, docs}. Launch-context learning signals that are only meaningful for
# reviewed code work (the per-type first-review rejection patterns and the
# self-review catch-rate calibration injected by routers/agent.py) gate on this
# set rather than a hardcoded type tuple, so a newly registered code-review type
# is equipped automatically. NOTE: this is intentionally BROADER than the
# narrower "code-change" SR-metrics filter {create, fix, refactor, migrate};
# qa/docs/deps also pass through review and so belong in the launch-context gate.
CODE_REVIEW_GATED_TASK_TYPES: frozenset[str] = frozenset(
    capability.id.value
    for capability in TASK_CAPABILITIES
    if capability.evaluation == "code-ci-review"
)
# Map of task-type value -> declared run-shape, derived from the registry. This
# is the SINGLE source of truth for "how does this type run?" that future
# dispatchers (the multi-agent-workflow engine, the experiment-loop engine) read
# instead of re-deriving the shape. Every task type is present (the field is
# non-optional with a "single_agent" default). Resolved via ``run_shape_for``.
RUN_SHAPE_BY_TASK_TYPE: dict[str, str] = {
    capability.id.value: capability.run_shape for capability in TASK_CAPABILITIES
}
# Canonical registry-derived set of task types that LAUNCH work on the budgeted,
# auto-torn-down compute-job substrate (the agent only launches and polls; it
# never holds the heavy VM). This is the SINGLE source of truth for the
# agent-facing compute-job launch gate (routers/agent_compute_jobs.py) — a type
# becomes launchable there simply by declaring run_shape="experiment_loop".
# NOTE: this is intentionally NOT the broader requires_compute flag. smoketest /
# perf / verify_tests also set requires_compute=True but run their compute INSIDE
# their own agent VM rather than offloading to the job substrate; the
# experiment_loop run-shape is precisely the "launches and polls a budgeted job"
# distinction the gate needs.
COMPUTE_JOB_LAUNCH_TASK_TYPES: frozenset[str] = frozenset(
    capability.id.value
    for capability in TASK_CAPABILITIES
    if capability.run_shape == "experiment_loop"
)
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
# Dispatch compatibility is intentionally distinct from legacy strategic-cycle
# completion membership and its Project lock/handler semantics.
STRATEGIC_LOCAL_AGENT_TASK_TYPES: frozenset[str] = STRATEGIC_TASK_TYPES
# Default autonomous spawn-budget categories. Runtime config may override these
# sets, but defaults stay registry-owned so new task types are classified from
# the same catalog that defines their capabilities.
AUTONOMOUS_SPAWN_INITIATIVE_TASK_TYPES: frozenset[str] = frozenset(
    {
        TaskType.create.value,
        TaskType.research.value,
        TaskType.plan.value,
        TaskType.web_research.value,
        TaskType.design.value,
        TaskType.compute_experiment.value,
        TaskType.investigate.value,
        TaskType.memo.value,
        TaskType.repo_bootstrap.value,
        *STRATEGIC_TASK_TYPES,
    }
)
AUTONOMOUS_SPAWN_SERVICING_TASK_TYPES: frozenset[str] = frozenset(
    {
        TaskType.review.value,
        TaskType.fix.value,
        TaskType.perf.value,
        TaskType.verify_tests.value,
        TaskType.debug.value,
        TaskType.qa.value,
        TaskType.deps.value,
        TaskType.docs.value,
        TaskType.refactor.value,
        TaskType.migrate.value,
        TaskType.security_audit.value,
        TaskType.smoketest.value,
        TaskType.release_audit.value,
        TaskType.release_audit_challenge.value,
        TaskType.release_merge_grade.value,
        TaskType.self_audit.value,
        TaskType.discovery_review.value,
        TaskType.salvage_bundle.value,
        TaskType.capability_proof_trial.value,
        TaskType.walkthrough_e2e.value,
        TaskType.memory_consolidation.value,
    }
)
# Map of strategic task-type value -> its declared two-phase-cycle ordering
# ("phase1"/"phase2"), derived from the registry. Non-strategic types are
# absent. Resolved via ``strategic_phase_for`` at the failure/backstop sites.
STRATEGIC_PHASE_BY_TASK_TYPE: dict[str, str] = {
    capability.id.value: capability.strategic_phase
    for capability in TASK_CAPABILITIES
    if capability.strategic_phase is not None
}
# Canonical registry of task types that run their heavy work on the budgeted
# compute-job substrate rather than on the agent VM — they hold only a
# lightweight monitoring VM while real spend accrues on the linked
# ``ComputeJob`` (persisted to ``ComputeJob.final_cost_usd``). The discriminating
# signal is ``requires_compute AND NOT requires_code``: such a type needs compute
# capacity but does not check out a code-working VM, so its honest observed cost
# lives on the compute job, not on Task VM fields. This is the SINGLE source of
# truth for "is this an off-VM / compute-substrate task type?" — cost-adherence
# reporting reads it to source spend from the compute-job ledger. Today only
# ``compute_experiment`` matches; future off-VM types are included automatically
# by declaring the same capability shape (no edit at the metric call site).
COMPUTE_SUBSTRATE_TASK_TYPES: frozenset[str] = frozenset(
    capability.id.value
    for capability in TASK_CAPABILITIES
    if capability.requires_compute and not capability.requires_code
)
# Canonical set of task types eligible for Cloud Run Jobs routing.
# This is the static registry-derived set; intersection with the runtime
# ``cloud_run_task_type_allowlist`` (default empty) determines which types are
# actually routed to Cloud Run at dispatch time. All read-only types are
# candidates — they never push a branch/PR, never need persistent disk, and
# are the dominant idle-cost component. The intersection happens in
# orchestrator.py; this set never imports runtime_config_state.
CLOUD_RUN_CANDIDATE_TASK_TYPES: frozenset[str] = (
    READ_ONLY_TASK_TYPES & AGENT_EXECUTED_TASK_TYPES
)


def runs_on_compute_substrate(task_type: str | TaskType | None) -> bool:
    """True if ``task_type`` runs its heavy work on the compute-job substrate.

    Membership is derived from the registry (``COMPUTE_SUBSTRATE_TASK_TYPES``);
    unknown / unregistered types return False (conservative default — measure
    from the VM-cost path).
    """
    if task_type is None:
        return False
    value = task_type.value if isinstance(task_type, TaskType) else str(task_type)
    return value in COMPUTE_SUBSTRATE_TASK_TYPES


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


def _capability_to_contract(capability: TaskCapability) -> dict[str, object]:
    """Flatten one capability into a JSON-serializable contract entry."""
    return {
        "type": capability.id.value,
        "label": capability.label,
        # ``badge`` falls back to ``label`` so every entry has a renderable
        # compact tag without each surface re-deriving the default.
        "badge": capability.badge or capability.label,
        "agent_mode": capability.agent_mode,
        "execution_owner": capability.execution_owner,
        "default_category": capability.default_category,
        "description": capability.description,
        "user_launchable": capability.user_launchable,
        "operator_contract_launchable": capability.operator_contract_launchable,
        "provider_configurable": capability.provider_configurable,
        "allowed_providers": (
            None
            if capability.allowed_providers is None
            else sorted(capability.allowed_providers)
        ),
        "filter_visible": capability.filter_visible,
        "delivers_product": capability.delivers_product,
        "reservable": capability.reservable,
        "periodic_eligible": capability.periodic_eligible,
        "run_shape": capability.run_shape,
        "output_kind": capability.output_kind,
        "requires_code": capability.requires_code,
        "requires_web": capability.requires_web,
        "requires_compute": capability.requires_compute,
        "workspace_requirement": capability.workspace_requirement,
    }


def task_capability_contract() -> dict[str, dict[str, object]]:
    """Return the published task-capability contract.

    This is the authoritative, backend-owned metadata map that operator and
    agent surfaces consume instead of maintaining private task-type tables. One
    entry per registered task type, keyed by the task-type value, declaring its
    presentation (label/badge), run-shape (agent_mode), and the capability flags
    that gate where it may be used (launchability, provider configurability,
    filter visibility, reserved-slot eligibility, periodic eligibility). A newly
    registered task type flows through here — and through the settings validators
    that derive from the same registry — with no per-surface allowlist edits.
    """
    return {
        capability.id.value: _capability_to_contract(capability)
        for capability in TASK_CAPABILITIES
    }


def agent_task_capability_contract() -> dict[str, dict[str, object]]:
    """Return the agent contract with non-routable compatibility tombstones.

    Runner Docker images may briefly lag the backend during a rolling update.
    Older agents fail closed unless every task type compiled into that image has
    an ``agent_mode`` entry, even when a removed type can no longer be claimed.
    Keep those deleted ids out of the registry/operator catalog while publishing
    minimal read-only tombstones to agent VMs. They grant no routing or launch
    capability; they only let a stale image start long enough to run a current
    task and self-heal.
    """
    contract = task_capability_contract()
    legacy_ids = (
        "panel_" + "review",
        "strategic_" + "resident_turn",
    )
    for task_type in legacy_ids:
        contract[task_type] = {
            "type": task_type,
            "label": "Removed task type",
            "badge": "Removed",
            "agent_mode": "read_only",
            "execution_owner": "backend",
            "default_category": None,
            "description": "Compatibility tombstone; not registered or routable.",
            "user_launchable": False,
            "operator_contract_launchable": False,
            "provider_configurable": False,
            "filter_visible": False,
            "delivers_product": False,
            "reservable": False,
            "periodic_eligible": False,
            "run_shape": "single_agent",
            "output_kind": "none",
            "requires_code": False,
            "requires_web": False,
            "requires_compute": False,
        }
    return contract


def agent_capability_slice(task_type: str | TaskType) -> dict | None:
    """Return the agent-facing capability slice for a claimed task type."""
    capability = get_task_capability(task_type)
    if capability is None:
        return None
    return _capability_to_catalog_entry(capability)


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
        "execution_owner": capability.execution_owner,
        "default_category": capability.default_category,
        "user_launchable": capability.user_launchable,
        "operator_contract_launchable": capability.operator_contract_launchable,
        "provider_configurable": capability.provider_configurable,
        "filter_visible": capability.filter_visible,
        "run_shape": capability.run_shape,
        "output_kind": capability.output_kind,
        "requires_code": capability.requires_code,
        "requires_web": capability.requires_web,
        "requires_compute": capability.requires_compute,
        "workspace_requirement": capability.workspace_requirement,
        "evaluation_strategy": capability.evaluation_strategy,
        "cost_ceiling_usd": capability.cost_ceiling_usd,
    }


def is_pr_eligible_task_type(task_type: str | TaskType) -> bool:
    """Return True if tasks of this type can produce a merged PR.

    Only ``ci_and_review`` evaluation-strategy types (create, fix, qa, deps,
    migrate, refactor, …) can land a PR; all read-only / research / audit types
    return False.  Unknown types return False (fail-safe: treat as ineligible).
    """
    capability = get_task_capability(task_type)
    return capability is not None and capability.evaluation_strategy == "ci_and_review"


def task_type_catalog() -> list[dict]:
    """Return the full task-type registry as a list of plain dicts.

    Pure and static (no DB access) — safe to serve directly from a cheap GET.
    Ordering follows the declaration order in ``TASK_CAPABILITIES`` so the
    operator catalog renders deterministically.
    """
    return [_capability_to_catalog_entry(c) for c in TASK_CAPABILITIES]
