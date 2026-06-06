"""Pydantic request/response schemas for Blueprint API endpoints."""

import re
import uuid
from datetime import datetime
from typing import List, Dict, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models import ArtifactType, ComputeJobOutcome, ComputeJobStatus, TaskStatus
from app.task_blocked import NEED_KINDS, NEED_LABEL_MAX_LEN, NEED_REF_MAX_LEN


class DeployPayload(BaseModel):
    name: str
    payloads: List[Dict[str, Any]]
    github_owner: str | None = Field(None, pattern="^[a-zA-Z0-9_.-]+$")
    github_repo: str | None = Field(None, pattern="^[a-zA-Z0-9_.-]+$")


class TaskEvaluationSummaryItem(BaseModel):
    label: str
    value: str


class TaskEvaluationSummary(BaseModel):
    status: str
    verdict: str
    evidence: List[TaskEvaluationSummaryItem] = Field(default_factory=list)
    highlights: List[TaskEvaluationSummaryItem] = Field(default_factory=list)


class BlockedNeed(BaseModel):
    """Normalized, machine-resolvable prerequisite a blocked task waits on.

    The canonical NEED primitive: ``kind`` names the reference category (a
    sibling task, a PR, or a capability/foundation), ``ref`` is the concrete
    identifier, and ``label`` is an optional human description. Kept in sync
    with ``app.task_blocked`` (single source of truth for the kinds and bounds)
    so agent blockers and research findings share one shape.
    """

    model_config = ConfigDict(extra="ignore")
    kind: str = Field(..., max_length=20)
    ref: str = Field(..., min_length=1, max_length=NEED_REF_MAX_LEN)
    label: str | None = Field(None, max_length=NEED_LABEL_MAX_LEN)

    @field_validator("kind")
    @classmethod
    def _validate_kind(cls, v: str) -> str:
        if v not in NEED_KINDS:
            raise ValueError(f"kind must be one of {NEED_KINDS}")
        return v


class ReproduceVerdict(BaseModel):
    """Durable reproduce-on-HEAD verdict for bug-report-/finding-derived work.

    Carries a canonical defect-truth status for a task whose source is a bug
    report or a research finding: did the cited defect actually reproduce
    against automain HEAD, was it already resolved / non-reproducing, or could
    it not be determined. Mirrors ``BlockedNeed``'s additive, extra-ignoring
    shape so older agents that omit it (or send only ``no_op_reason``) behave
    identically. Persisted into ``task.context["reproduce_verdict"]`` (a
    non-underscore key so the review/fix respawn carry-forward preserves it for
    free) and consumed by task-outcome / digest / strategic scoring.
    """

    model_config = ConfigDict(extra="ignore")
    status: str = Field(..., max_length=20)
    reason: str | None = Field(None, max_length=2000)
    # Durable evidence metadata (citations checked, symbols probed, etc.). Kept
    # as a free Dict so seeders and the agent can attach whatever provenance is
    # available without a schema lock-step.
    evidence: Dict[str, Any] | None = None

    @field_validator("status")
    @classmethod
    def _validate_status(cls, v: str) -> str:
        allowed = {"reproduced", "already_resolved", "indeterminate"}
        if v not in allowed:
            raise ValueError(f"status must be one of {allowed}")
        return v


class AgentStatusPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    task_id: uuid.UUID
    status: TaskStatus
    artifact_type: ArtifactType | None = None
    commit_hash: str | None = None
    repo_url: str | None = None
    pr_url: str | None = None
    storage_uri: str | None = None
    logs: str | None = Field(None, max_length=500000)
    verdict_json: str | None = Field(None, max_length=10000)
    failure_category: str | None = Field(None, max_length=50)
    # Structured provider-run error report captured by the agent runner for the
    # last provider CLI run (exit code, stderr tail, provider error events,
    # model_output_seen / result_event_seen run shape). Shipped on FAILED
    # statuses; the PRIMARY failure-classification signal — text heuristics are
    # the fallback for old agents that never sent one. Sanitized + bounded by
    # ``app.failure_classification.sanitize_agent_error_report`` before being
    # persisted into task.context["agent_error_report"].
    error_report: Dict[str, Any] | None = None
    provider: str | None = Field(None, max_length=50)
    auth_mode: str | None = Field(None, max_length=50)
    model: str | None = Field(None, max_length=200)
    execution_mode: str | None = Field(None, max_length=50)
    scope_gate_triggered: str | None = Field(None, max_length=50)
    changed_files: List[str] | None = Field(None, max_length=100)
    test_failure_summary: str | None = Field(None, max_length=5000)
    # Kept as Dict[str, Any] so agent_status.py can store it directly in the
    # task.context JSON column without an extra .model_dump() call.
    self_review_outcome: Dict[str, Any] | None = None
    diff_summary: List[Dict[str, Any]] | None = Field(None, max_length=50)
    file_change_details: List[Dict[str, Any]] | None = Field(None, max_length=50)
    analysis_plan: str | None = Field(None, max_length=5000)
    # Warm-VM continuity telemetry (Phase 0). Captured by the agent for each
    # role and persisted into task.context["_timing"] so cold-vs-warm setup
    # cost is observable from the dashboard. Optional — older agents that
    # don't yet send these fields silently skip persistence.
    setup_secs: float | None = None
    warm_start: bool | None = None
    prior_role_vm_id: str | None = Field(None, min_length=1, max_length=200)
    # Wave 5 follow-up (warm-VM continuity, deferred): handler-level wall-time
    # measured agent-side as `Date.now() - role_claim_timestamp` after the
    # role's handler returns. setup_secs measures provisioning latency before
    # the handler runs; handler_duration_secs measures the work-loop itself.
    # Together they decompose total role time so cold-vs-warm gains can be
    # attributed to the right phase. Bounded conservatively (24h) so a
    # broken agent clock can't write nonsense to the timing record.
    handler_duration_secs: float | None = Field(None, ge=0, le=86400)
    # Structured fix-agent rationale for disputed reviewer issues. The fix
    # agent writes one entry per CRITICAL/MAJOR issue it leaves unaddressed,
    # naming the issue and providing mechanically-verifiable claims that the
    # next reviewer must run before re-flagging. Capped at 5 per iteration
    # (review_pipeline cap-enforcer drops excess); each verify list capped
    # 10 entries; each string fields capped to keep context lean.
    fix_agent_responses: List[Dict[str, Any]] | None = Field(None, max_length=5)
    # Fix-loop telemetry events. Same shape as HeartbeatRequest.fix_loop_events;
    # accepting on the status endpoint too lets the agent flush a final
    # terminal event on the same call as a PR open / failure update.
    # Validated by ``app.fix_loop_telemetry``.
    fix_loop_events: List[Dict[str, Any]] | None = Field(None, max_length=20)
    # Stable 12-hex prompt fingerprint (mirrors HeartbeatRequest.prompt_fingerprint).
    # Lets the terminal status POST also carry it so tasks that skip heartbeats
    # (short-lived roles) still get a fingerprint persisted.
    prompt_fingerprint: str | None = Field(None, pattern="^[0-9a-f]{12}$")
    # Affirmative no-op completion reason. Set ONLY by `./done --already-done`
    # (agent/src/prompts/done-helper.ts) when the agent verified the task's
    # deliverable already exists on the base branch and there is no diff/PR to
    # produce (e.g. a sibling PR already added the migration/model/endpoint). On
    # a COMPLETED status with no PR, this is the affirmative signal that the
    # no-op is intentional — persisted into task.context["no_op_reason"] so
    # operators see WHY there is no PR. A no-diff finish WITHOUT this reason is
    # not a no-op completion; the agent wrapper fails it as `incomplete`.
    no_op_reason: str | None = Field(None, max_length=2000)
    # Optional normalized machine-resolvable prerequisite for a BLOCKED status.
    # Set by `./blocked --needs-task|--needs-pr|--needs-capability`. Persisted
    # into context['blocker']['need'] on the BLOCKED-entry path. Absent payloads
    # behave identically to today (free-text waiting_on/reason only).
    blocked_need: BlockedNeed | None = None
    # Optional durable reproduce-on-HEAD verdict for bug-report-/finding-derived
    # work. Set by the agent on a terminal status when it determined whether the
    # cited defect reproduces against HEAD. Persisted into
    # task.context["reproduce_verdict"]. Absent payloads behave identically to
    # today (older agents only send no_op_reason).
    reproduce_verdict: ReproduceVerdict | None = None


class UploadUrlRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    task_id: uuid.UUID
    filename: str = Field(..., min_length=1)


class HeartbeatMetrics(BaseModel):
    model_config = ConfigDict(extra="ignore")
    rss_mb: float | None = None
    heap_used_mb: float | None = None
    disk_free_mb: float | None = None
    disk_total_mb: float | None = None
    cgroup_mb: float | None = None
    cgroup_limit_mb: float | None = None


class ToolTraceEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")
    t: str
    tool: str
    input_preview: str


class HeartbeatRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    task_id: uuid.UUID
    status_message: str | None = None
    metrics: HeartbeatMetrics | None = None
    # Ring buffer of recent tool invocations from the agent. Persists to
    # task.context._tool_trace on each heartbeat, so the last-successful
    # heartbeat's value serves as a post-mortem when the reaper later kills
    # the task for heartbeat silence (AGENT_TIMEOUT diagnosis).
    tool_trace: List[ToolTraceEntry] | None = None
    # Fix-loop telemetry events emitted by the agent each iteration of a
    # lint/test/check fix loop. Validated by ``app.fix_loop_telemetry``;
    # piggybacks on heartbeat so we don't need a separate ingest endpoint.
    # Optional + capped so older agents that don't emit this continue to
    # work, and a runaway loop can't burst-emit thousands per heartbeat.
    fix_loop_events: List[Dict[str, Any]] | None = Field(None, max_length=20)
    # Stable 12-hex SHA-256 fingerprint of the static prompt guidance blocks
    # (task-type hints + tool guidance + failure guidance). Set once per task
    # on the first heartbeat after the handler registers it. Used to correlate
    # behavioral regressions back to prompt edits cycle-over-cycle.
    prompt_fingerprint: str | None = Field(None, pattern="^[0-9a-f]{12}$")
    # GCP boot-disk image identity, read from the metadata service at agent
    # startup. Lets the orchestrator detect VMs running stale Packer images
    # (e.g. pre-Chromium-bake) that will fail declared pre-PR checks.
    # Falls back to None when the metadata service is unreachable (local dev).
    vm_image_name: str | None = Field(None, max_length=200)
    # Unix timestamp (seconds) extracted from the image name suffix, e.g.
    # "blueprint-agent-v2-1730000000" → 1730000000. None when unparseable.
    vm_image_build_ts: int | None = None
    # SHA-256 fingerprint of the agent source files baked into the VM image,
    # written to /opt/agent/AGENT_RUNTIME_FINGERPRINT during Packer bake and
    # read at agent startup. Lets stale-image diagnostics distinguish a
    # recently-built image with an older agent runtime from the current one.
    # None on local dev or pre-fingerprint images.
    agent_runtime_fingerprint: str | None = Field(None, pattern="^[0-9a-f]{64}$")
    # Git commit the agent VM image was built from, baked into the image as
    # /opt/agent/AGENT_SOURCE_SHA during Packer bake and read at agent startup.
    # Maps a running VM back to its exact source commit (the runtime fingerprint
    # only identifies a content hash, not a git SHA). Permissive 7-40 hex so both
    # abbreviated and full SHAs validate; None on local dev or pre-marker images.
    agent_source_sha: str | None = Field(None, pattern="^[0-9a-f]{7,40}$")
    # Cumulative per-task LLM telemetry. Optional/backwards-compatible — older
    # agents that don't yet emit these fields silently skip persistence; newer
    # agents send running totals on every heartbeat. Persisted into
    # Task.api_call_summary so the orchestrator can aggregate
    # api-calls-per-task and total-tokens-per-task across the fleet.
    llm_call_count: int | None = Field(None, ge=0)
    total_input_tokens: int | None = Field(None, ge=0)
    total_output_tokens: int | None = Field(None, ge=0)


class ComputeJobResponse(BaseModel):
    """Agent-facing serialization for persisted compute jobs."""

    model_config = ConfigDict(from_attributes=True, use_enum_values=True)

    id: uuid.UUID
    project_id: uuid.UUID
    task_id: uuid.UUID | None = None
    backend: str
    external_compute_id: str | None = None
    machine_type: str | None = None
    status: ComputeJobStatus
    terminal_reason: str | None = None
    outcome: ComputeJobOutcome | None = None
    spend_cap_usd: float | None = None
    wall_clock_deadline: datetime | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    last_polled_at: datetime | None = None
    next_poll_at: datetime | None = None
    poll_attempt_count: int = 0
    accrued_cost_usd: float | None = None
    final_cost_usd: float | None = None
    result_reference: str | None = None
    artifact_id: uuid.UUID | None = None
    polling_metadata: Dict[str, Any] = Field(default_factory=dict)


def serialize_compute_job_response(job: object) -> Dict[str, Any]:
    """Return a JSON-ready compute-job response payload."""

    return ComputeJobResponse.model_validate(job).model_dump(mode="json")


class SpawnTaskRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    task_id: uuid.UUID
    payloads: List[Dict[str, Any]] = Field(max_length=20)
    requires_approval: bool = False
    context: Dict[str, Any] | None = None
    priority: int | None = Field(default=None, ge=0, le=100)


class GitHubCommitStatusRequest(BaseModel):
    task_id: uuid.UUID
    owner: str = Field(..., min_length=1)
    repo: str = Field(..., min_length=1)
    sha: str = Field(..., pattern="^[a-fA-F0-9]{40}$")
    state: str = Field(..., pattern="^(error|failure|pending|success)$")
    description: str = ""
    context: str = "blueprint-agent"
    target_url: str = ""


class GitHubPRCommentRequest(BaseModel):
    task_id: uuid.UUID
    owner: str = Field(..., min_length=1)
    repo: str = Field(..., min_length=1)
    pr_number: int
    body: str = Field(..., min_length=1)


class GitHubPRReviewCommentRequest(BaseModel):
    task_id: uuid.UUID
    owner: str = Field(..., min_length=1)
    repo: str = Field(..., min_length=1)
    pr_number: int
    commit_id: str = Field(..., pattern="^[a-fA-F0-9]{40}$")
    path: str = Field(..., min_length=1)
    line: int
    body: str = Field(..., min_length=1)


class GitHubSubmitPRReviewRequest(BaseModel):
    task_id: uuid.UUID
    owner: str = Field(..., min_length=1)
    repo: str = Field(..., min_length=1)
    pr_number: int
    event: str = Field(..., pattern="^(APPROVE|REQUEST_CHANGES)$")
    body: str = Field(..., min_length=1)
    comments: list[dict] = Field(default_factory=list)


class GitHubCreateIssueRequest(BaseModel):
    task_id: uuid.UUID
    owner: str = Field(..., min_length=1)
    repo: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1)
    body: str = ""
    assignees: List[str] | None = None


class GitHubGetIssuesRequest(BaseModel):
    task_id: uuid.UUID
    owner: str = Field(..., min_length=1)
    repo: str = Field(..., min_length=1)
    state: str = Field("open", pattern="^(open|closed|all)$")


class GitHubCreateBranchRequest(BaseModel):
    task_id: uuid.UUID
    owner: str = Field(..., min_length=1)
    repo: str = Field(..., min_length=1)
    branch_name: str = Field(..., pattern="^[a-zA-Z0-9_./-]+$")
    sha: str = Field(..., pattern="^[a-fA-F0-9]{40}$")

    @field_validator("branch_name")
    @classmethod
    def validate_branch_safe(cls, v: str) -> str:
        if ".." in v:
            raise ValueError("Branch name cannot contain '..'")
        if v.startswith("/") or v.startswith("-"):
            raise ValueError("Branch name cannot start with '/' or '-'")
        return v


class GitHubCreatePRRequest(BaseModel):
    task_id: uuid.UUID
    owner: str = Field(..., min_length=1)
    repo: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1)
    head: str = Field(..., pattern="^[a-zA-Z0-9_./:-]+$")
    base: str = Field(..., pattern="^[a-zA-Z0-9_./:-]+$")
    body: str = ""


# --- Build Config & Onboarding ---


class BuildConfigDirectoryEntry(BaseModel):
    """A single directory entry within a project's build configuration."""

    path: str = Field(..., min_length=1)
    type: str = Field(..., pattern="^(python|node|go|rust|other)$")
    lint_cmd: str | None = None
    build_cmd: str | None = None
    test_cmd: str | None = None
    install_cmd: str | None = None


class MigrationConfig(BaseModel):
    """Per-project Alembic/migration layout override.

    Mirrors the agent-side ``MigrationConfig`` (agent/src/utils/test-runners.ts):
    the migrate handler's ``resolveMigrationLayout`` consumes these fields so a
    repo with a non-standard Python layout (e.g. migrations under
    ``src/db/versions`` instead of ``backend/alembic/versions``) can run migrate
    tasks. Every field is optional; an unset field falls back to Blueprint's
    defaults (``backend/alembic/versions`` + ``app/models`` + ``app/main.py``
    ``EXPECTED_SCHEMA_REVISION``).

    This is DISTINCT from ``BuildConfig.alembic_versions_path`` (a single string
    feeding only the backend-side migration-file guard); this object drives the
    agent's full layout resolution.

    Note: an explicit empty-string ``schema_revision_file`` is meaningful — it
    signals "this repo has no schema-revision gate" and is preserved through
    validation rather than coerced to None.
    """

    model_config = ConfigDict(extra="ignore")
    versions_dir: str | None = None
    models_dir: str | None = None
    alembic_cwd: str | None = None
    schema_revision_file: str | None = None
    schema_revision_const: str | None = None


class BuildConfig(BaseModel):
    """Schema for project build configuration."""

    language: str | None = Field(None, pattern="^(python|typescript|go|rust|mixed)$")
    directories: List[BuildConfigDirectoryEntry] = Field(default_factory=list)
    env_vars: Dict[str, str] = Field(default_factory=dict)
    git_user_name: str | None = None
    git_user_email: str | None = None
    detected_at: str | None = None
    enable_fresh_reviewer: bool = False
    enable_parallel_review: bool = False
    enable_parallel_fix: bool = False
    # Per-project branch and PR metadata — must be preserved through
    # detect/save/patch round-trips so project-specific branch naming and
    # PR title prefixes survive schema validation.
    task_branch_namespace: str | None = None
    task_branch_prefix: str | None = None
    pr_title_prefix: str | None = None
    # Per-project staging/cleanup branch config. None means "use project defaults".
    # An explicit empty list [] is semantically meaningful for stale_branch_prefixes
    # (disables cleanup for the project) — do NOT coerce [] to None.
    staging_branch_prefixes: List[str] | None = None
    stale_branch_prefixes: List[str] | None = None
    protected_branches: List[str] | None = None
    enable_self_improvement: bool = False
    # Whether this project's PRs must wait for required CI status checks before
    # merging. Default True preserves CI-gated behavior (like Blueprint's own
    # repo) for every existing project. An operator can declare
    # ``requires_ci_checks = False`` for a greenfield/no-CI repository so a
    # *definitive* absence of checks is treated as 'CI not applicable' instead
    # of being polled until the merge backstop FAILs the task. This must survive
    # validation on every write path (onboard, PATCH, re-detect, auto-refresh)
    # — the merge gate reads it via ``ci_resolution.requires_ci_checks``.
    requires_ci_checks: bool = True
    # Deterministic empty/near-empty repo signal, computed at onboarding from
    # the already-fetched root + first-level subdirectory listings during
    # build-config detection (see ``admin_onboard._classify_repo_emptiness``).
    # True ONLY when the repo read SUCCEEDED and was COMPLETE and the repo has
    # at most the greenfield source-file threshold after excluding
    # docs/config/data files, dotfiles, and common manifests. If any listing
    # needed for that determination failed to fetch (root or first-level
    # subdirectory), the read is incomplete and this degrades to False
    # (treat-as-non-empty) — emptiness is never inferred from a partial read.
    # Keep this count-based rule aligned with the agent's prompt-time detector
    # so research/plan bootstrap guidance and the durable onboarding signal
    # agree. Default False so pre-existing projects (and any repo with detected
    # source) degrade gracefully.
    repo_is_empty: bool = False
    # Marker that build-config detection did NOT complete at onboarding time —
    # set True only when ``_detect_build_config`` returned an empty/falsy result
    # or raised (a transient GitHub failure: 5xx, rate-limit, network blip, or a
    # non-200 repo-contents listing). A detection that COMPLETED and genuinely
    # found no tests sets ``repo_is_empty``/``has_ci`` instead and leaves this
    # False, so a legitimately test-less repo is distinguishable from one whose
    # detection never ran. The landing-time ``maybe_refresh_empty_build_config``
    # treats True as a second re-detect trigger (alongside ``repo_is_empty``);
    # a successful re-detect drops the marker (it is NOT in the fresh detected
    # dict and ``apply_operator_build_config_overrides`` does not carry it
    # forward), so the auto-refresh becomes a no-op once GitHub is reachable.
    detection_incomplete: bool = False
    # Tri-state CI-presence signal computed at onboarding/re-detect from the
    # already-fetched ``.github/workflows`` listing during build-config detection
    # (see ``admin_onboard._detect_build_config``). True = a GitHub Actions
    # workflow file was observed; False = the bounded repo walk completed and
    # found none; None = the read was incomplete/undetected (legacy stored
    # configs also have None). Surfaced read-only in the UI to warn operators
    # that a no-CI repo left with ``requires_ci_checks`` ON will stall PRs.
    # ``model_dump(exclude_none=True)`` drops the key when None so legacy configs
    # round-trip unchanged. This is detection/guidance only — it never auto-flips
    # ``requires_ci_checks``.
    has_ci: bool | None = None
    # Per-project alembic versions directory path. None means "use the project default".
    # NOTE: this single string feeds only the backend-side migration-file guard;
    # it is NOT the agent's full layout resolver — see ``migration`` below.
    alembic_versions_path: str | None = None
    # Per-project migration-layout override consumed by the agent's migrate
    # handler (resolveMigrationLayout). None means "use Blueprint defaults".
    # Round-tripped unchanged through detect/save/PATCH so an operator-supplied
    # build_config.migration actually reaches the agent's claim response.
    migration: MigrationConfig | None = None

    @field_validator(
        "task_branch_namespace", "task_branch_prefix", "pr_title_prefix", mode="before"
    )
    @classmethod
    def _coerce_empty_to_none(cls, v: object) -> object:
        """Normalize empty/whitespace strings to None so they fall back to project defaults."""
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator(
        "staging_branch_prefixes",
        "stale_branch_prefixes",
        "protected_branches",
        mode="before",
    )
    @classmethod
    def _validate_branch_list(cls, v: object) -> object:
        """Validate each entry in a branch-name list: safe charset, no '..', no leading '/' or '-'."""
        if v is None:
            return v
        if not isinstance(v, list):
            raise ValueError("must be a list of strings")
        _safe = re.compile(r"^[a-zA-Z0-9_./-]+$")
        for item in v:
            if not isinstance(item, str) or not item:
                raise ValueError("branch list entries must be non-empty strings")
            if not _safe.match(item):
                raise ValueError(f"branch entry {item!r} contains invalid characters")
            if ".." in item:
                raise ValueError(f"branch entry {item!r} cannot contain '..'")
            if item.startswith("/") or item.startswith("-"):
                raise ValueError(f"branch entry {item!r} cannot start with '/' or '-'")
        return v


class MergeGateRuntime(BaseModel):
    """Runtime-backed readiness of the no-CI merge gate — what the gate ACTUALLY
    did the last time it ran a PR to a terminal outcome.

    Distinct from the stored-config ``merge_verification_verdict`` advisory: this
    is written best-effort by ``ci_resolution.record_no_ci_gate_runtime`` and read
    back via ``ci_resolution.merge_gate_runtime_block``.  Every field is optional
    so older clients and projects whose gate has never run remain valid (the API
    returns ``null`` for the whole block in that case).  ``last_unverified_*`` are
    populated only when the gate fell open to an UNVERIFIED merge and are
    preserved across later verified runs.
    """

    model_config = ConfigDict(extra="ignore")

    last_run_at: str | None = None
    executed_real_tests: bool = False
    last_unverified_at: str | None = None
    last_unverified_reason: str | None = None
    # Additive history fields (bounded rolling record + per-reason tally +
    # derived summary). All optional with safe defaults so never-ran and legacy
    # four-key-only records remain valid and the response shape stays
    # backward-compatible. ``recent_outcomes`` entries are
    # ``{"at": iso, "verified": bool, "reason": key | None}``; ``summary``
    # carries ``window`` / ``verified_runs`` / ``fell_open_runs`` /
    # ``total_fall_opens`` / ``most_common_reason``.
    recent_outcomes: List[Dict[str, Any]] | None = None
    unverified_reason_counts: Dict[str, int] | None = None
    summary: Dict[str, Any] | None = None


class OnboardPayload(BaseModel):
    """Payload for the project onboarding endpoint."""

    github_owner: str = Field(..., min_length=1, pattern="^[a-zA-Z0-9_.-]+$")
    github_repo: str = Field(..., min_length=1, pattern="^[a-zA-Z0-9_.-]+$")
    preview_only: bool = False
    name: str | None = None
    build_config_override: Dict[str, Any] | None = None
    promotion_target: str | None = None
    # User's goal for this repo — a short statement of what Blueprint should
    # achieve. Seeded at creation time into the project's strategic intent
    # (problem_model.intent + a user conversation message) so the first
    # strategic cycle reasons from the user's actual ask rather than a blank
    # slate. Optional: an empty/omitted goal preserves today's behavior.
    goal: str | None = Field(None, max_length=5000)
    # Optional initial steering merged into the user-editable strategic-intent
    # subset (only the ``context`` / ``constraints`` keys are applied; other
    # keys are ignored by the seeding path). None/omitted is a no-op.
    initial_steering: Dict[str, Any] | None = None


# --- Pipeline Efficiency ---


class PipelineEfficiencyWindow(BaseModel):
    """Per-time-window metrics for the pipeline-efficiency endpoint."""

    model_config = ConfigDict(extra="ignore")
    setup_p50_secs: float | None = None
    setup_p95_secs: float | None = None
    setup_sample_n: int = 0
    warm_reuse_rate: float | None = None
    warm_reuse_sample_n: int = 0
    review_iter_median: float | None = None
    review_iter_p95: float | None = None
    review_iter_sample_n: int = 0
    vs_prior_setup_p50_delta: float | None = None
    vs_prior_warm_reuse_rate_delta: float | None = None
    vs_prior_review_iter_median_delta: float | None = None


class PipelineEfficiencyResponse(BaseModel):
    """Response schema for GET /api/admin/pipeline-efficiency."""

    computed_at: str
    window_24h: PipelineEfficiencyWindow
    window_7d: PipelineEfficiencyWindow
