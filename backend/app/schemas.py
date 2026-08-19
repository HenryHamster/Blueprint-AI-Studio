"""Pydantic request/response schemas for Blueprint API endpoints."""

import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.failure_classification import _CRED_FIELD_RE, _CRED_REDACT_SUBS
from app.execution_capability_requirements import (
    POSTGRES_RUNTIME_UNAVAILABLE_SENTINEL,
)
from app.models import ArtifactType, ComputeJobOutcome, ComputeJobStatus, TaskStatus
from app.vm_cost_rates import SUPPORTED_ACCELERATORS, supported_machine_types
from app.payload_schemas import RepoDomainOverview, RepoBootstrapOverview
from app.task_blocked import (
    NEED_KINDS,
    NEED_LABEL_MAX_LEN,
    NEED_REF_MAX_LEN,
    canonicalize_exact_task_ref,
    is_exact_task_ref,
)


class DeployPayload(BaseModel):
    name: str
    payloads: List[Dict[str, Any]]
    github_owner: str | None = Field(None, pattern="^[a-zA-Z0-9_.-]+$")
    github_repo: str | None = Field(None, pattern="^[a-zA-Z0-9_.-]+$")


class ArtifactDownloadResponse(BaseModel):
    """Signed, time-limited GET URL minted for a project-owned ``gs://`` artifact
    reference by ``GET /api/admin/artifacts/download-url``."""

    url: str


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

    @field_validator("ref")
    @classmethod
    def _validate_and_normalize_ref(cls, v: str, info) -> str:
        if not isinstance(v, str):
            return v
        ref = v.strip()
        kind = info.data.get("kind") if isinstance(info.data, dict) else None
        if kind == "task":
            canonical = canonicalize_exact_task_ref(ref)
            if canonical is None:
                raise ValueError("task ref must be a real task UUID or finding hash")
            return canonical
        return ref


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


class CommandMemoryOutcome(BaseModel):
    """Agent-reported repo_bootstrap command write-back outcome.

    Carries whether the bootstrap agent confirmed and wrote back build/test/lint
    commands, reported *separately* from ``repo_overview`` persistence. A valid
    overview no longer implies complete command memory: the agent reports one of
    ``success`` (commands confirmed and written back), ``missing_commands`` (no
    runnable command could be confirmed), or ``writeback_failed`` (the write-back
    request itself failed). Mirrors ``ReproduceVerdict``'s additive,
    extra-ignoring shape so older agents that omit it behave identically — the
    completion handler then degrades the command-memory marker to ``unknown``
    rather than falsely claiming success. Persisted into
    ``task.context["command_memory"]`` and consumed by
    ``_handle_repo_bootstrap_completion``.
    """

    model_config = ConfigDict(extra="ignore")
    status: str = Field(..., max_length=32)
    detail: str | None = Field(None, max_length=2000)
    updated_paths: List[str] | None = Field(None, max_length=50)

    @field_validator("status")
    @classmethod
    def _validate_status(cls, v: str) -> str:
        allowed = {"success", "missing_commands", "writeback_failed"}
        if v not in allowed:
            raise ValueError(f"status must be one of {allowed}")
        return v


class WorkspacePrepOutcome(BaseModel):
    """Bounded agent-reported workspace-preparation outcome for lane telemetry."""

    model_config = ConfigDict(extra="ignore")
    path: Literal["warm", "cold"]
    warm_reuse_succeeded: bool
    checkpoint_reset_applied: bool
    cold_fallback_reason: str | None = Field(None, max_length=500)
    lane_phase: bool


class TaskParkRequest(BaseModel):
    """An agent's request to PARK a task waiting on an external, non-agent signal.

    The agent declares *which* source it is waiting on (``wake_source_kind`` —
    an open string, e.g. ``"compute_job_terminal"``, plus an optional
    ``wake_source_ref`` such as a ComputeJob id), what to do when that source
    fires (``wake_action`` — ``"resume"`` to re-queue the task, or ``"finalize"``
    to drive it terminal), an optional ``deadline_seconds`` after which the
    reaper ages the park-out rather than stranding it, and free-form
    ``source_metadata`` the firing side / operators read. Mirrors ``BlockedNeed``'s
    additive, ``extra="ignore"`` shape so older agents that never send a park
    request behave identically.
    """

    model_config = ConfigDict(extra="ignore")
    wake_source_kind: str = Field(..., min_length=1, max_length=64)
    wake_source_ref: str | None = Field(None, max_length=255)
    wake_action: str = Field("resume", max_length=20)
    deadline_seconds: int | None = Field(None, ge=1, le=30 * 24 * 60 * 60)
    reason: str | None = Field(None, max_length=2000)
    source_metadata: Dict[str, Any] | None = None

    @field_validator("wake_action")
    @classmethod
    def _validate_action(cls, v: str) -> str:
        allowed = {"resume", "finalize"}
        if v not in allowed:
            raise ValueError(f"wake_action must be one of {allowed}")
        return v


class PublicationGateReceipt(BaseModel):
    """Bounded receipt from the local publication gate.

    This is intentionally a closed, additive shape: the blocker transition
    stores only these fields in task context for durable diagnostics.
    """

    model_config = ConfigDict(extra="ignore")
    status: str = Field(..., min_length=1, max_length=50)
    command: str = Field(..., min_length=1, max_length=200)
    timeout_seconds: int = Field(..., ge=1, le=900)
    hard_timeout_seconds: int = Field(..., ge=1, le=1800)
    elapsed_seconds: int = Field(..., ge=0, le=86400)
    deadline_extended: bool
    exit_code: int = Field(..., ge=0, le=255)
    authoritative_ci_required: bool
    recorded_at: str = Field(..., min_length=1, max_length=64)
    attribution_state: str | None = Field(None, max_length=4000)
    new_failures: Dict[str, str | Dict[str, str]] | None = None
    suppressed_failures: Dict[str, str | Dict[str, str]] | None = None
    disappeared_failures: Dict[str, str | Dict[str, str]] | None = None
    skipped_failures: Dict[str, str | Dict[str, str]] | None = None
    attribution_counts: Dict[str, Dict[str, int]] | None = None
    # Cross-lane guard-suppression projection: {lane: {state, suppressed,
    # suppressed_observed_count}}, merged from every guard lane's receipt (see
    # scripts/guard_suppression_receipt.py). Backend-pytest-only until now;
    # this widens the same receipt to cover static/vitest/typecheck/viewport too.
    guard_suppressions: Dict[str, Dict[str, object]] | None = None
    guard_suppressions_counts: Dict[str, int] | None = None


class AgentStatusPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    task_id: uuid.UUID
    resident_incarnation_id: uuid.UUID | None = None
    status: TaskStatus
    artifact_type: ArtifactType | None = None
    commit_hash: str | None = None
    repo_url: str | None = None
    pr_url: str | None = None
    storage_uri: str | None = None
    logs: str | None = Field(None, max_length=500000)
    verdict_json: str | None = Field(None, max_length=10000)
    failure_category: str | None = Field(None, max_length=50)
    # Set only by the generated terminal helpers after recognizing an exact
    # Blueprint-owned runtime frame. Free-form logs are never routing authority.
    execution_capability: Literal["backend_verification"] | None = None
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
    # Artifact ids this task actually consumed while completing. New agents send
    # this explicitly; older agents are handled by a backend fallback over
    # delivered artifact_prerequisites for direct depends_on producers.
    consumed_artifact_ids: List[uuid.UUID] | None = Field(None, max_length=100)
    test_failure_summary: str | None = Field(None, max_length=5000)
    # Kept as Dict[str, Any] so agent_status.py can store it directly in the
    # task.context JSON column without an extra .model_dump() call.
    self_review_outcome: Dict[str, Any] | None = None
    # Structured repo_bootstrap overview returned in the terminal COMPLETED
    # payload. Persisted into task.context so the completion handler can write
    # it into project.design_doc under the durable repo_overview section.
    repo_overview: RepoBootstrapOverview | None = None
    workspace_prep: WorkspacePrepOutcome | None = None
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
    # Structured local ./check verdict captured when the publication gate
    # blocks before a PR exists. Persisted under context['blocker']; omitted by
    # older agents and all non-gate status reports.
    publication_gate_receipt: PublicationGateReceipt | None = None
    # Optional request to PARK this task on an external wake source (released
    # fleet slot + durable wake registration). Honored when ``status == PARKED``.
    # Absent payloads behave identically to today — backward compatible.
    park_request: TaskParkRequest | None = None
    # Optional durable reproduce-on-HEAD verdict for bug-report-/finding-derived
    # work. Set by the agent on a terminal status when it determined whether the
    # cited defect reproduces against HEAD. Persisted into
    # task.context["reproduce_verdict"]. Absent payloads behave identically to
    # today (older agents only send no_op_reason).
    reproduce_verdict: ReproduceVerdict | None = None
    # Optional repo_bootstrap command write-back outcome, reported separately
    # from repo_overview. Set by the bootstrap agent on its terminal COMPLETED
    # status to declare whether build/test/lint commands were confirmed and
    # written back (success / missing_commands / writeback_failed). Persisted
    # into task.context["command_memory"]. Absent payloads behave identically to
    # today (older agents omit it, and completion degrades command memory to
    # "unknown" rather than claiming success).
    command_memory: CommandMemoryOutcome | None = None
    # Optional product-domain overview extracted from README/docs during
    # repo_bootstrap. Persisted into task.context["domain_overview"] by the
    # status endpoint, then upserted as a KnowledgeDocument by the completion
    # handler. Absent payloads are a clean no-op — domain capture is best-effort.
    domain_overview: RepoDomainOverview | None = None
    # Last branch/head SHA the agent successfully `git push`ed this role, set
    # ONLY by the heartbeat self-terminate path (agent/src/index.ts,
    # postTerminalStatusSync's pushedWorkExtras()) when a non-transient
    # heartbeat 4xx forces a REASSIGNED report after a push already landed a
    # commit on GitHub but before the PR-creation call could run. Without this,
    # a REASSIGNED->CANCELLED task with no PR artifact and no task_pr_linkage
    # orphans that pushed commit entirely — nothing routes it to review (see
    # kill-path: NeurIPS task 38b16082, 2026-08-18). Persisted into
    # task.context["orphaned_push"] so downstream review-loop recovery can pick
    # the commit up instead of treating the branch as never having existed.
    # Absent for every other status report.
    pushed_branch: str | None = Field(None, max_length=200)
    pushed_head_sha: str | None = Field(None, pattern="^[0-9a-fA-F]{7,64}$")

    @model_validator(mode="after")
    def _validate_execution_capability_provenance(self):
        capability = self.execution_capability
        if capability is None:
            return self
        if self.status != TaskStatus.FAILED:
            raise ValueError("execution_capability requires FAILED status")
        compatible = (
            self.failure_category == "agent_self_failed"
            and self.logs == POSTGRES_RUNTIME_UNAVAILABLE_SENTINEL
        )
        if not compatible:
            raise ValueError(
                "execution_capability is incompatible with terminal category/frame"
            )
        return self

    @field_validator("blocked_need", mode="before")
    @classmethod
    def _drop_title_like_task_need(cls, v: Any) -> Any:
        """Graceful API-boundary enforcement of the exact-task-ref policy.

        A ``kind=task`` blocked-need whose ``ref`` is a title-like string (not a
        task UUID / finding hash, per
        :func:`app.task_blocked.is_exact_task_ref`) names no machine-resolvable
        foundation, so it must not be persisted as durable stranded demand.
        Rather than 422 the entire status report — which would drop the BLOCKED
        transition the agent is trying to record — drop ONLY the malformed need
        to ``None`` (identical to an absent ``blocked_need``, backward-compatible).
        Named foundations must be declared as ``kind=capability`` instead. Other
        malformed needs (unknown kind, empty ref) still raise via ``BlockedNeed``.
        """
        if isinstance(v, dict):
            kind = v.get("kind")
            ref = v.get("ref")
            # Only intercept a NON-EMPTY title-like task ref. Empty / missing /
            # non-string refs fall through to BlockedNeed's field validation so
            # they still raise (preserving the malformed-input contract).
            if (
                isinstance(kind, str)
                and kind.strip().lower() == "task"
                and isinstance(ref, str)
                and ref.strip()
                and not is_exact_task_ref(ref)
            ):
                return None
        return v


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


class PromptSectionSize(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = Field(max_length=100)
    bytes: int = Field(ge=0)


class HeartbeatRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    task_id: uuid.UUID
    status_message: str | None = None
    metrics: HeartbeatMetrics | None = None
    # Structured local ./check verdict captured when the publication gate blocks
    # before a PR exists. A blocked gate is RETRYABLE — the agent fixes the
    # failures and re-runs ./pr open — so the receipt must be persistable
    # WITHOUT the terminal/BLOCKED transition the status endpoint performs.
    # Persisted into task.context["publication_gate_receipt"]; omitted by older
    # agents and by every ordinary heartbeat.
    # Loosely typed on purpose (same rationale as fix_loop_events): heartbeat is
    # the liveness channel, so a malformed receipt from a forward/backward-rolled
    # agent must be dropped by the handler's bounded sanitizer rather than 422
    # the whole heartbeat. The closed shape is enforced by
    # ``app.task_blocked.sanitize_publication_gate_receipt``.
    publication_gate_receipt: Dict[str, Any] | None = None
    # Ring buffer of recent tool invocations from the agent. Persists to
    # task.context._tool_trace on each heartbeat, so the last-successful
    # heartbeat's value serves as a post-mortem when the reaper later kills
    # the task for heartbeat silence (AGENT_TIMEOUT diagnosis).
    tool_trace: List[ToolTraceEntry] | None = None
    # Genuine output/progress timestamp for the currently-running tool.  This
    # is separate from ``status_message`` because task-authored progress prose
    # may replace the literal ``Using tool:`` prefix while the same foreground
    # command is still running.  The reaper folds this into its existing
    # last_tool_use_at progress clock, avoiding false ``agent_stalled`` reaps
    # during long but productive checks without treating mere heartbeats as
    # progress.  Omitted when no tool is in flight or it has emitted no
    # sub-progress.
    active_tool_progress_at: datetime | None = None
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
    num_turns: int | None = Field(None, ge=0)
    total_input_tokens: int | None = Field(None, ge=0)
    total_output_tokens: int | None = Field(None, ge=0)
    # Optional cache-class sub-counts of total_input_tokens (which stays the
    # INCLUSIVE sum of uncached + cache-creation + cache-read input). Anthropic
    # prices cache-read input at ~0.1x and cache-creation at ~1.25x the base input
    # rate, so breaking them out lets the cost snapshot price each class truthfully.
    # Backwards-compatible: older agents omit these; absent them persistence is
    # byte-for-byte unchanged and pricing falls back to the base input rate.
    total_cache_creation_input_tokens: int | None = Field(None, ge=0)
    total_cache_read_input_tokens: int | None = Field(None, ge=0)
    # Provider-reported token usage from the agent's SEPARATE accumulator
    # (Component B of cost-telemetry reconciliation). Raw provider response.usage
    # fields accumulated independently from api_call_summary. The reconciliation
    # cross-checks this against api_call_summary to detect accumulation bugs.
    # Shape (reported): {"input_tokens": int, "output_tokens": int,
    #         "cache_creation_input_tokens": int, "cache_read_input_tokens": int,
    #         "reported": true}.
    # Shape (not reported — LLM calls made but no usage block observed):
    #         {"reported": false} — an explicit marker, never a silent null.
    # Optional/backwards-compatible — agents that don't emit this leave it NULL.
    provider_reported_usage: Dict[str, Any] | None = None
    # Cumulative per-task structured-output format-compliance telemetry, sibling
    # to the LLM fields above. Optional/backwards-compatible — older agents that
    # don't emit these leave Task.format_compliance_summary NULL. format_miss_by_reason
    # maps each extraction-layer reason class to a count of unparseable final outputs.
    format_miss_by_reason: Dict[str, int] | None = None
    format_repair_miss_by_reason: Dict[str, int] | None = None
    format_error_total: int | None = Field(None, ge=0)
    format_repair_attempts: int | None = Field(None, ge=0)
    format_repair_successes: int | None = Field(None, ge=0)
    # Per-schema breakdown of format misses, repair attempts, and successes.
    # Keys are schema label strings (e.g. "ResearchPlan", "StructuredVerdict").
    # Older agents omitting these fields contribute only to scalar totals.
    format_miss_by_schema: Dict[str, int] | None = None
    # Per-schema, per-reason breakdown of format misses (nested map: schema → reason → count).
    # Backwards-compatible: older agents omit this field; treated as {}.
    format_miss_by_schema_and_reason: Dict[str, Dict[str, int]] | None = None
    format_repair_attempts_by_schema: Dict[str, int] | None = None
    format_repair_successes_by_schema: Dict[str, int] | None = None
    # Count of reopen/retry runs that crashed (exitCode != 0) — these are infra
    # failures, not genuine format failures. Excluded from the self_repair_success_rate
    # denominator so crashed reopens don't silently inflate the failure rate.
    # Backwards-compatible: older agents omit these; treated as 0 / {}.
    format_repair_infra_failures: int | None = Field(None, ge=0)
    format_repair_infra_failures_by_schema: Dict[str, int] | None = None
    # Token cost of repair re-ask runs. Cumulative input/output tokens consumed
    # across all reopen calls within this task. Backwards-compatible: older
    # agents omit these fields; treated as 0 / {}.
    format_repair_input_tokens: int | None = Field(None, ge=0)
    format_repair_output_tokens: int | None = Field(None, ge=0)
    format_repair_input_tokens_by_schema: Dict[str, int] | None = None
    format_repair_output_tokens_by_schema: Dict[str, int] | None = None
    format_repair_miss_by_schema: Dict[str, int] | None = None
    # Count of viewport screenshots forwarded into agent prompts this task.
    # Backwards-compatible: older agents omit this field; treated as 0.
    viewport_screenshot_forwarded_count: int | None = Field(None, ge=0)
    # Cumulative byte-count of all tool-result content blocks forwarded to the
    # LLM within this task. Backwards-compatible: older agents omit this field;
    # absent means unknown (not zero) in downstream aggregations.
    total_tool_result_bytes: int | None = Field(None, ge=0)
    # Prompt-size telemetry for research/plan invocations. prompt_size_bytes is
    # the true byte-length of the assembled prompt sent to the model (NOT the
    # inflated sum of component sections). prompt_section_sizes records the
    # top-3 largest named component sections (excluding the assembled prompt
    # itself). Backwards-compatible: older agents omit these fields; absent
    # means unknown in downstream aggregations.
    prompt_size_bytes: int | None = Field(None, ge=0)
    prompt_section_count: int | None = Field(None, ge=0)
    prompt_section_sizes: List[PromptSectionSize] | None = Field(None, max_length=10)
    # Number of agent turns (user→assistant exchanges) in this CLI invocation.
    # Distinct from llm_call_count (API calls): allows per-turn context-size
    # decomposition in the efficiency snapshot. Backwards-compatible: older
    # agents omit this field; treated as absent in all downstream aggregations.
    num_turns: int | None = Field(None, ge=0)
    # Whether the language-aware repo map was non-empty and injected into the
    # agent prompt for this task. Set once on the first heartbeat after the
    # standard handler registers it; used as the receipt/compliance signal
    # for the repo-map capability trial.
    had_repo_map: bool | None = None
    # Deterministic arm-assignment flag for the repo-map capability trial.
    # True = treatment arm (task was assigned to receive the repo map),
    # False = holdback arm. Separate from had_repo_map so intent-to-treat
    # analysis uses assignment, not receipt. Only present when a trial is
    # actively running (holdback_fraction > 0); older agents omit this field.
    repo_map_arm: bool | None = None
    # Whether module-interface, related-test, and affected-consumer sections
    # were non-empty and injected into the agent prompt. Write-once flags
    # mirroring had_repo_map; older agents that omit these fields leave the
    # task.context key absent (fail-open).
    had_module_interfaces: bool | None = None
    had_related_tests: bool | None = None
    had_affected_consumers: bool | None = None
    # Deterministic arm-assignment flags for the three newly-assigned capability
    # trials (module_interfaces, related_tests, affected_consumers). True =
    # treatment arm, False = holdback arm. Separate from the had_* receipt
    # flags so intent-to-treat analysis uses assignment, not receipt. Older
    # agents simply omit these fields; the context key stays absent.
    module_interfaces_arm: bool | None = None
    related_tests_arm: bool | None = None
    affected_consumers_arm: bool | None = None
    # Whether the post-edit diagnostics hook was armed and ran for this task.
    # Set once on the first heartbeat that carries this field; used as the
    # receipt/compliance signal for the diagnostics-after-edit capability trial.
    had_diagnostics_after_edit: bool | None = None
    # Deterministic arm-assignment flag for the diagnostics-after-edit trial.
    # True = treatment arm (task was assigned to run the hook),
    # False = holdback arm. Separate from had_diagnostics_after_edit so
    # intent-to-treat analysis uses assignment, not receipt. Only present when
    # the trial is actively running (holdback_fraction > 0 and enabled=True);
    # older agents omit this field.
    diagnostics_after_edit_arm: bool | None = None


class ComputeTerminalLog(BaseModel):
    """Canonical redacted terminal output for a compute job."""

    source: Literal["backend", "runner", "legacy"]
    text: str
    truncated: bool = False


class ComputeJobResponse(BaseModel):
    """Agent-facing serialization for persisted compute jobs."""

    model_config = ConfigDict(from_attributes=True, use_enum_values=True)

    id: uuid.UUID
    project_id: uuid.UUID
    task_id: uuid.UUID | None = None
    backend: str
    external_compute_id: str | None = None
    machine_type: str | None = None
    accelerator_type: str | None = None
    accelerator_count: int | None = None
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
    terminal_log: ComputeTerminalLog | None = None
    compute_rpc_diagnostic: Dict[str, Any] | None = None
    # Immutable launch intent and execution-side placement facts.  These are
    # deliberately separate from polling_metadata so task intent cannot be
    # mistaken for what the worker actually ran.
    # Rows created before the operability migration legitimately carry NULL.
    # Preserve that wire shape; callers already normalize absent provenance.
    execution_manifest: Dict[str, Any] | None = None
    runtime_provenance: Dict[str, Any] | None = None
    # Cross-job checkpoint resume, promoted from polling_metadata so the operator
    # can see (without digging into the launch bag) that a run resumed and from
    # where. ``input_artifact_job_id`` is the declared+validated resume source;
    # ``resume_provenance`` is what actually happened at hydration time.
    input_artifact_job_id: str | None = None
    resume_provenance: Dict[str, Any] | None = None


def serialize_compute_job_response(job: object) -> Dict[str, Any]:
    """Return a JSON-ready compute-job response payload."""

    data = ComputeJobResponse.model_validate(job).model_dump(mode="json")
    if data.get("compute_rpc_diagnostic") is None:
        diagnostic = (data.get("polling_metadata") or {}).get("compute_rpc_diagnostic")
        if isinstance(diagnostic, dict):
            data["compute_rpc_diagnostic"] = diagnostic
    # Promote the resume source + provenance from polling_metadata so both agent
    # and operator surfaces expose them as first-class fields. They remain in
    # polling_metadata too (not stripped by the _safe redactor).
    metadata = data.get("polling_metadata")
    if isinstance(metadata, dict):
        source = metadata.get("input_artifact_job_id")
        if isinstance(source, str) and source:
            data["input_artifact_job_id"] = source
        provenance = metadata.get("resume_provenance")
        if isinstance(provenance, dict) and provenance:
            data["resume_provenance"] = provenance
        command_log = metadata.get("command_log")
        source = "backend"
        if not isinstance(command_log, dict):
            command_log = metadata.get("runner_command_log")
            source = "runner"
        if isinstance(command_log, dict) and isinstance(command_log.get("text"), str):
            if command_log.get("source") == "runner":
                source = "runner"
            data["terminal_log"] = {
                "source": source,
                "text": command_log["text"],
                "truncated": command_log.get("truncated") is True,
            }
    # Promote watchdog protection-status fields so the agent surface always has them.
    # Lazy import avoids the circular dependency (compute_jobs imports schemas).
    from app.compute_jobs import derive_watchdog_protection_status  # noqa: PLC0415
    data.update(
        derive_watchdog_protection_status(
            data.get("polling_metadata"),
            started_at=data.get("started_at"),
        )
    )
    return data


# Launch-spec keys removed from polling_metadata before operator-facing exposure.
# ``container_registry_auth`` carries registry credentials (e.g. an NGC API key /
# password) used to pull the experiment container for runner-subprocess jobs — it
# must never reach the admin API alongside env/command/cwd internal launch material.
_POLLING_METADATA_LAUNCH_SPEC_KEYS = frozenset(
    {"env", "command", "cwd", "container_registry_auth"}
)
_TASK_PAYLOAD_SECRET_KEYS = frozenset({"env", "container_registry_auth"})


def redact_task_payloads(payloads: Any) -> Any:
    """Strip secret keys from operator-facing task payloads.

    A ``compute_experiment`` task stores its launch contract inline in
    ``Task.payloads`` — including ``container_registry_auth`` (a registry
    password / NGC API key) and ``env`` (operator-supplied secrets). The
    agent-claim path legitimately needs these (the claiming agent forwards them
    when it creates the compute job, and a retried task re-reads them), so the
    values must stay in the row. But the operator-facing task list/detail
    endpoints must not echo those secret-bearing fields back, or the very
    registry password redacted in one operator view leaks in another.

    Returns a new list with the secret keys removed from each payload dict. The
    input is never mutated (a fresh dict is built only for entries that carry a
    secret key), so redaction can't leak back into an ORM-attached attribute and
    accidentally persist. A non-list ``payloads`` and non-dict entries pass
    through untouched.
    """
    if not isinstance(payloads, list):
        return payloads
    redacted = []
    for entry in payloads:
        if isinstance(entry, dict) and any(k in entry for k in _TASK_PAYLOAD_SECRET_KEYS):
            entry = {
                k: v
                for k, v in entry.items()
                if k not in _TASK_PAYLOAD_SECRET_KEYS
            }
        redacted.append(entry)
    return redacted


# Regex patterns for scrubbing secrets/credentials from RPC error messages.
_URL_QUERY_RE = re.compile(r"\?[^\s'\"<>]*")
_BEARER_LIKE_RE = re.compile(
    r"(?i)(bearer|token|secret|password|authorization|sig|x-goog-signature)"
    r"\s*[=:]\s*\S+",
)
_RPC_MSG_SAFE_LIMIT = 400
# Matches ``Bearer <token>`` (space-separated) — _BEARER_LIKE_RE only handles
# ``keyword=value`` / ``keyword: value`` forms and would leave the actual token
# value dangling when the full form is ``Authorization: Bearer <token>``.
_BEARER_TOKEN_RE = re.compile(r"(?i)\bbearer\s+\S+")
_ENV_EXPORT_RE = re.compile(r"(?i)export\s+\w+=\S+")


def redact_command_log_text(text: str) -> str:
    """Redact credential-bearing patterns from command stdout/stderr text.

    Applies the broad _CRED_REDACT_SUBS table (bare GitHub tokens, Anthropic/
    OpenAI/Google API keys, JWTs, embedded git-URL credentials, etc.) first,
    then the URL-query, bearer-token, and shell-export scrubbers used for RPC
    diagnostics — so raw stdout/stderr never reaches operator-facing surfaces
    or KnowledgeDocuments with credentials intact.
    """
    for pattern, replacement in _CRED_REDACT_SUBS:
        text = pattern.sub(replacement, text)
    text = _CRED_FIELD_RE.sub(r"\1***\3", text)
    text = _URL_QUERY_RE.sub("?[redacted]", text)
    # Apply Bearer-value pattern BEFORE _BEARER_LIKE_RE so that
    # ``Authorization: Bearer TOKEN`` loses TOKEN before the header itself
    # is collapsed — otherwise TOKEN survives as a dangling word.
    text = _BEARER_TOKEN_RE.sub("Bearer [redacted]", text)
    text = _BEARER_LIKE_RE.sub(r"\1=[redacted]", text)
    text = _ENV_EXPORT_RE.sub("[export redacted]", text)
    return text


def sanitize_rpc_diagnostic(raw: Dict[str, Any] | None) -> Dict[str, Any] | None:
    """Sanitize a ``compute_rpc_diagnostic`` dict for safe public exposure.

    Bounds string fields, strips URL query strings, and redacts bearer-like
    tokens so neither operator-facing API responses nor knowledge documents ever
    expose secrets or signed URLs from raw provider exceptions.  Safe to call on
    ``None`` (returns ``None``).

    This helper is the shared sanitization path used by BOTH
    ``serialize_compute_job_response_safe`` (admin API) and
    ``_extract_job_terminal_diagnostics`` (knowledge documents) so the two
    surfaces produce identical sanitized output.
    """
    if not isinstance(raw, dict):
        return None
    out: Dict[str, Any] = {}
    for key, limit in (("operation", 64), ("error_type", 200), ("reason", 200)):
        val = raw.get(key)
        if isinstance(val, str) and val:
            out[key] = val[:limit]
    msg = raw.get("error_message")
    if isinstance(msg, str) and msg:
        msg = _URL_QUERY_RE.sub("?[redacted]", msg)
        msg = _BEARER_LIKE_RE.sub(r"\1=[redacted]", msg)
        out["error_message"] = msg[:_RPC_MSG_SAFE_LIMIT]
    return out if out else None


def _redact_dict_strings(obj: Any) -> Any:
    """Recursively apply redact_command_log_text to all string values in a dict/list."""
    if isinstance(obj, dict):
        return {k: _redact_dict_strings(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_dict_strings(item) for item in obj]
    if isinstance(obj, str):
        return redact_command_log_text(obj)
    return obj


def _operator_safe_execution_manifest(manifest: Any) -> Dict[str, Any]:
    """Return readable execution intent without copying launch secrets.

    The same structure is persisted with the job for durable provenance, so
    this guard is intentionally defensive for legacy rows that may predate the
    write-time scrub in ``create_compute_job``.
    """
    if not isinstance(manifest, dict):
        return {}
    safe = dict(manifest)
    normalized = safe.get("normalized_contract")
    if isinstance(normalized, dict):
        safe_normalized = dict(normalized)
        for key in _POLLING_METADATA_LAUNCH_SPEC_KEYS:
            safe_normalized.pop(key, None)
        safe["normalized_contract"] = safe_normalized
    return _redact_dict_strings(safe)


def serialize_compute_job_response_safe(job: object) -> Dict[str, Any]:
    """Return a JSON-ready compute-job response with launch-spec fields redacted.

    Strips ``env``, ``command``, ``cwd``, and ``container_registry_auth`` from
    ``polling_metadata`` before returning so operator-facing endpoints never
    expose internal launch material that may contain secrets (e.g. a registry
    password / NGC API key) or internal paths.  Also sanitizes the promoted
    ``compute_rpc_diagnostic`` to strip URL query strings and credential-bearing
    tokens from raw provider exception text, and promotes ``vm_phase``,
    and ``launch_contract`` for operator diagnostics.  The agent-facing endpoints
    use ``serialize_compute_job_response`` directly — agents own their job and
    need the full metadata.
    """
    data = serialize_compute_job_response(job)
    metadata = data.get("polling_metadata")
    if isinstance(metadata, dict):
        for key in _POLLING_METADATA_LAUNCH_SPEC_KEYS:
            metadata.pop(key, None)

    # Redact credential-bearing patterns from command_log and live_output text so
    # raw stdout/stderr captured on the VM never reaches operator-facing API responses.
    if isinstance(metadata, dict):
        command_log = metadata.get("command_log")
        if isinstance(command_log, dict) and isinstance(command_log.get("text"), str):
            command_log = dict(command_log)
            command_log["text"] = redact_command_log_text(command_log["text"])
            metadata["command_log"] = command_log
        live_output = metadata.get("live_output")
        if isinstance(live_output, dict) and isinstance(live_output.get("text"), str):
            live_output = dict(live_output)
            live_output["text"] = redact_command_log_text(live_output["text"])
            metadata["live_output"] = live_output
    terminal_log = data.get("terminal_log")
    if isinstance(terminal_log, dict) and isinstance(terminal_log.get("text"), str):
        terminal_log = dict(terminal_log)
        terminal_log["text"] = redact_command_log_text(terminal_log["text"])
        data["terminal_log"] = terminal_log
    # Recursively redact all string values in result_metrics so credentials in
    # any experiment output field cannot leak through unrecognized paths.
    if isinstance(metadata, dict):
        result_metrics = metadata.get("result_metrics")
        if isinstance(result_metrics, dict):
            metadata["result_metrics"] = _redact_dict_strings(result_metrics)

    data["execution_manifest"] = _operator_safe_execution_manifest(
        data.get("execution_manifest")
    )
    data["runtime_provenance"] = _redact_dict_strings(
        data.get("runtime_provenance") or {}
    )
    # The historical top-level hardware fields are launch requests, not a
    # statement of placement. Keep them for compatibility but publish explicit
    # requested/observed blocks so callers cannot mistake an L40S local runner
    # for the V100 requested by a portable launch contract.
    data["requested_hardware"] = {
        "machine_type": data.get("machine_type"),
        "accelerator_type": data.get("accelerator_type"),
        "accelerator_count": data.get("accelerator_count"),
    }
    runtime_provenance = data["runtime_provenance"]
    observed = (
        runtime_provenance.get("observed_hardware")
        if isinstance(runtime_provenance, dict)
        else None
    )
    data["observed_hardware"] = observed if isinstance(observed, dict) else None

    # Local runners report modeled capacity cost for operational accounting, but
    # it is not a cloud bill and must never imply that the job spent money.
    monetary = metadata.get("monetary_decision") if isinstance(metadata, dict) else None
    execution_mode = monetary.get("execution_mode") if isinstance(monetary, dict) else None
    modeled_cost = (
        data.get("final_cost_usd")
        if data.get("final_cost_usd") is not None
        else data.get("accrued_cost_usd")
    )
    is_local = execution_mode == "local_only"
    data["cost_accounting"] = {
        "execution_mode": execution_mode,
        "is_billable": False if is_local else (None if execution_mode is None else True),
        "billable_estimate_usd": (
            monetary.get("billable_estimate_usd")
            if isinstance(monetary, dict)
            else None
        ),
        "billable_cost_usd": 0.0 if is_local else modeled_cost,
        "modeled_resource_cost_usd": modeled_cost,
    }

    # Sanitize the promoted diagnostic — may carry raw provider exception text
    # with signed URLs or credential values.  Overwrite both the top-level
    # promoted field AND the copy inside polling_metadata so neither path leaks
    # secrets to operator-facing callers.
    sanitized_diag = sanitize_rpc_diagnostic(data.get("compute_rpc_diagnostic"))
    data["compute_rpc_diagnostic"] = sanitized_diag
    if isinstance(metadata, dict) and "compute_rpc_diagnostic" in metadata:
        metadata["compute_rpc_diagnostic"] = sanitized_diag

    # Promote vm_phase so operators can see whether a failure occurred before
    # the VM started (provisioning) or during execution (runtime).
    started_at = data.get("started_at")
    data["vm_phase"] = "provisioning" if started_at is None else "runtime"

    # Promote launch_contract fields for provisioning-context diagnostics.
    if isinstance(metadata, dict):
        deadline = metadata.get("requested_deadline_seconds")
        prov_model = metadata.get("provisioning_model")
        contract: Dict[str, Any] = {}
        if deadline is not None:
            contract["requested_deadline_seconds"] = deadline
        if prov_model is not None:
            contract["provisioning_model"] = prov_model
        if contract:
            data["launch_contract"] = contract

    # Promote exit_code and command_log as first-class fields so callers do not
    # need to reach into the opaque polling_metadata bag to surface them.
    # command_log is already redacted in-place above; read from metadata so the
    # promoted copy is consistent with the in-bag copy.
    if isinstance(metadata, dict):
        exit_code = metadata.get("exit_code")
        data["exit_code"] = exit_code if isinstance(exit_code, int) else None
        command_log = metadata.get("command_log")
        data["command_log"] = command_log if isinstance(command_log, dict) else None
    else:
        data["exit_code"] = None
        data["command_log"] = None

    # Promote progress_series and terminal_kind as first-class fields so the
    # frontend has a stable shape and does not reach into the opaque bag.
    # Both are absent/None on legacy rows that predate these fields.
    if isinstance(metadata, dict):
        progress_series = metadata.get("progress_series")
        data["progress_series"] = progress_series if isinstance(progress_series, list) else None
        terminal_kind = metadata.get("terminal_kind")
        data["terminal_kind"] = terminal_kind if isinstance(terminal_kind, str) else None
    else:
        data["progress_series"] = None
        data["terminal_kind"] = None

    return data


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


class ProjectVerificationConfig(BaseModel):
    """Explicit opt-in to Blueprint's receipt-emitting verification protocol."""

    model_config = ConfigDict(extra="forbid")
    protocol: Literal["blueprint-v1"]
    harness: str = Field(
        ...,
        min_length=1,
        max_length=500,
        pattern=r"^[A-Za-z0-9_./-]+$",
    )

    @field_validator("harness")
    @classmethod
    def _validate_harness_path(cls, v: str) -> str:
        if v.startswith("/") or ".." in v or "\0" in v:
            raise ValueError("harness must be a safe repository-relative path")
        return v


class BuildConfigDirectoryEntry(BaseModel):
    """A single directory entry within a project's build configuration."""

    path: str = Field(..., min_length=1)
    type: str = Field(..., pattern="^(python|node|go|rust|other)$")
    lint_cmd: str | None = None
    build_cmd: str | None = None
    test_cmd: str | None = None
    install_cmd: str | None = None
    # Agent-confirmed commands (write-back from runtime verification).
    # These are set by the write-back endpoint and never overwrite the
    # operator-detected originals (lint_cmd / test_cmd / build_cmd / install_cmd).
    confirmed_lint_cmd: str | None = None
    confirmed_test_cmd: str | None = None
    confirmed_build_cmd: str | None = None
    confirmed_install_cmd: str | None = None
    commands_confirmed_at: str | None = None  # ISO 8601 UTC timestamp
    confirming_task_id: str | None = None
    project_verification: ProjectVerificationConfig | None = None

    @field_validator("path")
    @classmethod
    def _canonicalize_path(cls, v: str) -> str:
        normalized = v.strip()
        normalized = normalized.rstrip("/")
        return normalized or "."


class DiscoveredBuildCommandEntry(BaseModel):
    """A single directory entry reported by an agent VM after run-time build detection.

    Field names mirror ``BuildConfigDirectoryEntry`` so the merge in
    ``persist_discovered_build_commands`` is mechanical. ``executed_successfully``
    is the per-entry evidence gate — only entries with it True are persisted.
    """

    model_config = ConfigDict(extra="ignore")
    path: str = Field(..., min_length=1, max_length=500)
    type: str | None = Field(None, pattern="^(python|node|go|rust|other)$")
    lint_cmd: str | None = Field(None, max_length=500)
    build_cmd: str | None = Field(None, max_length=500)
    test_cmd: str | None = Field(None, max_length=500)
    install_cmd: str | None = Field(None, max_length=500)
    executed_successfully: bool = False


class BuildDiscoveryReport(BaseModel):
    """Request body for POST /api/agent/build-config-discovery."""

    model_config = ConfigDict(extra="ignore")
    discovered_directories: List[DiscoveredBuildCommandEntry] = Field(
        default_factory=list, max_length=50
    )
    # The agent VM's live full-clone verdict on whether the repo is empty/
    # near-empty (same "real source" exclusion rules as the backend classifier).
    # When present and it disagrees with the durable ``build_config.repo_is_empty``
    # in EITHER direction, the endpoint re-runs authoritative detection rather
    # than trusting the stale durable signal. Optional/None for older agents that
    # do not report it.
    observed_repo_is_empty: bool | None = None


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


class ResourceEnvelope(BaseModel):
    """Per-project resource bounds for concurrent VM caps and spend ceilings.

    All fields are optional; ``null``/absent means unlimited (inherit global
    behavior).  Validated as a nested object inside ``BuildConfig`` under the
    ``resource_envelope`` key so operators can set it via the project PATCH path.
    """

    model_config = ConfigDict(extra="forbid")
    # Maximum concurrent agent-VM tasks allowed for this project.
    max_concurrent_vms: int | None = Field(None, ge=1)
    # Spend ceiling in USD over the rolling window (spend_window_days).
    spend_ceiling_usd: float | None = Field(None, gt=0)
    # Rolling window length in days for spend aggregation. Default 30.
    spend_window_days: int = Field(30, ge=1)
    # Maximum concurrent compute jobs allowed for this project (stored in runtime_config).
    max_concurrent_compute_jobs: int | None = Field(None, ge=1)
    # Fraction of spend_ceiling_usd at which to fire a proactive approaching-ceiling alert.
    # e.g. 0.8 fires when rolling spend reaches 80% of the ceiling. Default 0.8 when absent.
    spend_warning_fraction: float | None = Field(None, gt=0, le=1)
    # If set, CPU compute jobs must use one of these machine types.
    # GPU machine types are determined by the accelerator and are not checked here.
    allowed_machine_types: list[str] | None = Field(None)
    # If set, GPU compute jobs must use one of these accelerators (e.g. ["T4", "L4"]).
    # An empty list blocks all GPU jobs. CPU jobs (no accelerator) are never blocked by this field.
    allowed_accelerator_types: list[str] | None = Field(None)
    # When True, compute_experiment tasks for this project must run on a
    # registered self-hosted local runner — they NEVER fall back to billable
    # cloud compute. With no live runner the experiment is held (and eventually
    # failed by a bounded timeout) rather than provisioned in the cloud. Scoped
    # to compute_experiment tasks for now; a future field could extend the
    # policy to other task types. Absent/None ⇒ False (cloud fallback permitted).
    require_local_runner: bool | None = Field(None)
    # Placement policy for all executable project work.  This supersedes the
    # compute-experiment-only ``require_local_runner`` flag above.  It remains
    # optional solely for backwards compatibility: absent means
    # ``managed_allowed`` while a legacy true flag retains its narrow scope.
    execution_placement_policy: Literal[
        "local_only", "local_preferred", "managed_allowed"
    ] | None = Field(None)
    # When True, exceeding the spend ceiling automatically pauses the project
    # (project.paused = True) until an operator manually unpauses it.
    # Absent/None ⇒ False (soft-defer tasks but don't pause the project).
    pause_on_ceiling_exceeded: bool | None = Field(None)

    @model_validator(mode="after")
    def _reject_conflicting_placement_policies(self) -> "ResourceEnvelope":
        """Do not silently reinterpret the old narrow policy as a broad one."""
        if (
            self.execution_placement_policy is not None
            and self.require_local_runner is True
        ):
            raise ValueError(
                "execution_placement_policy conflicts with legacy "
                "require_local_runner=true; clear the legacy field before "
                "setting the broader policy"
            )
        return self

    @field_validator("allowed_machine_types", mode="before")
    @classmethod
    def _validate_allowed_machine_types(cls, v: object) -> object:
        if v is None:
            return v
        if not isinstance(v, list):
            raise ValueError("must be a list of strings")
        valid = supported_machine_types()
        for item in v:
            if item not in valid:
                raise ValueError(
                    f"unknown machine type {item!r}: must be one of {sorted(valid)}"
                )
        return v

    @field_validator("allowed_accelerator_types", mode="before")
    @classmethod
    def _validate_allowed_accelerator_types(cls, v: object) -> object:
        if v is None:
            return v
        if not isinstance(v, list):
            raise ValueError("must be a list of strings")
        valid = set(SUPPORTED_ACCELERATORS.keys())
        for item in v:
            if item not in valid:
                raise ValueError(
                    f"unknown accelerator type {item!r}: must be one of {sorted(valid)}"
                )
        return v


class BuildConfig(BaseModel):
    """Schema for project build configuration."""

    language: str | None = Field(None, pattern="^(python|typescript|go|rust|mixed)$")
    directories: List[BuildConfigDirectoryEntry] = Field(default_factory=list)
    env_vars: Dict[str, str] = Field(default_factory=dict)
    git_user_name: str | None = None
    git_user_email: str | None = None
    detected_at: str | None = None
    enable_parallel_review: bool = False
    enable_parallel_fix: bool = False
    # Project-owned admission switch for Strategic work originated by the
    # autonomous research loop.  Tactical direction research remains governed
    # by ``Project.autonomous_enabled`` and is intentionally unaffected when
    # this is False.  Explicit/manual Strategic surfaces also bypass this flag.
    # Default True preserves every existing project's historical behavior.
    automatic_strategic_cycles_enabled: bool = True
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
    # Whether the engine self-heals its OWN release-promotion CI failures. When a
    # promotion PR's full CI matrix goes red, the reconciler harvests the failing
    # tests into a fix task on the integration branch (or re-qualifies on an infra
    # flake) so the next qualification does not re-promote the same red code — a
    # release-pipeline concern, DISTINCT from the generic protected-branch
    # auto-remediation gate (``design_doc._auto_remediation.enabled``, which the
    # strategist rewrites each cycle). It lives here in operator-owned
    # ``build_config`` precisely so the strategist cannot clobber it, and survives
    # validation on every write path (onboard, PATCH, re-detect). Default ON for
    # every project and project-agnostic by design — the harvest only ever runs
    # when a promotion PR's CI fails, so a blanket default is self-limiting and
    # needs no repo-identity / self-repo special-casing. Resolved via
    # ``release_reconciliation.promotion_ci_selfheal_enabled`` and overridable per
    # project (set False to opt out).
    promotion_ci_selfheal_enabled: bool = True
    # Deterministic empty/near-empty repo signal, computed at onboarding from
    # the already-fetched root + first-level subdirectory listings during
    # build-config detection (see ``admin_onboard._classify_repo_emptiness``).
    # True ONLY when the repo read SUCCEEDED and was COMPLETE and the repo has
    # at most the greenfield source-file threshold after excluding
    # docs/config/data files, dotfiles, and common manifests. If any listing
    # needed for that determination failed to fetch (root or first-level
    # subdirectory), the read is incomplete and this stays None (undetermined)
    # — emptiness is NEVER inferred from a partial read, exactly like the
    # ``has_ci`` sibling below. Keep this count-based rule aligned with the
    # agent's prompt-time detector so research/plan bootstrap guidance and the
    # durable onboarding signal agree. Tri-state: True = the bounded walk
    # completed and found at most the greenfield source threshold; False = the
    # walk completed and found more; None = the read was incomplete/undetected
    # (legacy stored configs also have None). ``model_dump(exclude_none=True)``
    # drops the key when None so an undetermined partial walk persists as an
    # absent key (which every consumer coalesces to "consult the live signal /
    # self-heal") rather than a durable, authoritative False.
    repo_is_empty: bool | None = None
    # The authoritative real-source-file count from the same bounded onboarding
    # walk that produced ``repo_is_empty`` (documentation/config/dotfiles/common
    # manifests excluded by the shared exclusion rules). Persisted so the agent's
    # live clone-time detector can compare like-for-like against the backend's
    # count instead of recomputing under its own rules. None when the read was
    # incomplete (emptiness indeterminate) or for configs predating this field.
    source_file_count: int | None = None
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
    # Per-project, DEFAULT-OFF opt-in policy for external no-CI repos. When ON,
    # the no-CI merge gate BLOCKS a PR (holds it open) instead of merging it
    # UNVERIFIED whenever the gate EXPECTED to verify the code but could NOT run
    # the repo's suite — i.e. a DEGRADED fall-open reason (see
    # ``ci_resolution.DEGRADED_UNVERIFIED_REASONS``). Benign fall-opens (there
    # was honestly nothing to verify) still merge UNVERIFIED exactly as today.
    # Absent/False == today's behavior for every existing project, so this is
    # fully additive and backward-compatible; onboarding never turns it on.
    # Read by ``ci_resolution.should_block_degraded_unverified_merge``.
    block_degraded_unverified_merge: bool = False
    # Read by ci_resolution.should_block_no_test_unverified_merge.
    block_no_test_unverified_merge: bool = False
    # Per-project promotion rubric override (Fix 5, 2026-07-02). A project may
    # legitimately promote at a different bar than Blueprint's trusted self-
    # improvement core (e.g. an external research/witness repo). When set, these
    # override the fleet-global promotion_threshold / promotion_weights;
    # absent (None) means "use the fleet rubric". Resolved by
    # release_qualifier._policy_from_build_config, which CLAMPS the threshold to a
    # safety floor and ignores weights that don't sum ~1.0. Declared here (NOT
    # extra-ignored) so they survive ``model_dump`` round-trips on every
    # save/PATCH/re-detect — otherwise the override would be silently stripped and
    # never persist. Carried across re-detect by apply_operator_build_config_overrides.
    promotion_threshold: float | None = Field(None, ge=0.0, le=1.0)
    promotion_weights: dict[str, float] | None = None
    # Deprecated compatibility field. The qualifier ignores it; ordinary
    # post-promotion smoke uses the fleet runtime flag.
    smoketest_enabled: bool | None = None
    # Per-project alembic versions directory path. None means "use the project default".
    # NOTE: this single string feeds only the backend-side migration-file guard;
    # it is NOT the agent's full layout resolver — see ``migration`` below.
    alembic_versions_path: str | None = None
    # Per-project migration-layout override consumed by the agent's migrate
    # handler (resolveMigrationLayout). None means "use Blueprint defaults".
    # Round-tripped unchanged through detect/save/PATCH so an operator-supplied
    # build_config.migration actually reaches the agent's claim response.
    migration: MigrationConfig | None = None
    # Per-project parallelism overrides. None means "use the global runtime
    # settings" (``max_parallel_research_per_project``,
    # ``max_total_research_per_project``, ``max_autonomous_tasks_per_hour``
    # respectively). Set on a project to throttle it independently of the
    # fleet — e.g. a trial-run project on an external repo sharing VM
    # capacity with Blueprint's own pipeline. Bounds mirror the
    # admin-settings validation for the corresponding global knobs. Resolved
    # at spawn time by ``autonomous_research.project_parallelism_cap`` and
    # carried across re-detect by ``apply_operator_build_config_overrides``.
    max_parallel_research: int | None = Field(None, ge=1, le=50)
    max_total_research: int | None = Field(None, ge=1, le=50)
    max_autonomous_tasks_per_hour: int | None = Field(None, ge=1, le=50)
    # ISO timestamp stamped by _handle_repo_bootstrap_completion when a
    # repo_bootstrap task finishes successfully. None (absent) means the bootstrap
    # has not yet run or has not completed. The strategic spawn gate checks this
    # to defer implementation tasks until the bootstrap has grounded repo knowledge.
    bootstrap_completed_at: str | None = None
    # Durable command-memory outcome stamped by repo_bootstrap completion and
    # preserved through ordinary project saves and re-detects. These fields are
    # operator-visible state, not repo-derived config, so they must round-trip
    # through BuildConfig validation.
    bootstrap_command_memory_status: str | None = None
    bootstrap_command_memory_complete: bool = False
    bootstrap_command_memory_detail: str | None = None
    bootstrap_command_memory_paths: List[str] | None = None
    bootstrap_command_memory_updated_at: str | None = None
    # Durable write-back evidence stamped by agent_write_back_build_config_commands.
    # Read by _stamp_command_memory_status at repo_bootstrap completion to confirm
    # command memory without re-scanning every directory entry.  Must be part of
    # the schema so a settings-UI PATCH (which round-trips through BuildConfig
    # validation + model_dump(exclude_none=True)) does not silently erase the
    # evidence before completion runs and downgrade a successful write-back to
    # writeback_failed.
    last_command_writeback_at: str | None = None
    last_command_writeback_task_id: str | None = None
    last_command_writeback_paths: List[str] | None = None
    # Per-project resource envelope controlling concurrent VM caps and spend
    # ceilings.  None/absent means unlimited (inherit global behavior).
    # Validated by ResourceEnvelope; carried across re-detect by
    # ``apply_operator_build_config_overrides``.
    resource_envelope: "ResourceEnvelope | None" = None
    # Reusable alert-domain enablement.  Core alerts are always enabled; optional
    # domains such as release_lifecycle, sr_quality, warm_pool, research_directions,
    # rollup_lts, migrations, and self_improvement are operator-owned and must
    # survive detection/save/PATCH round-trips.  Shape:
    # {"warm_pool": {"enabled": true}, "sr_quality": {"enabled": false}}.
    alert_domains: Dict[str, Any] | None = None
    # Per-project 24h rolling-window spend ceiling. When set, autonomous spawning
    # is paused (and a deduped operator alert emitted) once the project's window
    # spend meets or exceeds this value. None = disabled (today's behavior).
    # Operator-created tasks are never blocked — this is a soft autonomous-only gate.
    daily_spend_ceiling_usd: float | None = Field(None, gt=0)
    # Explicit degraded marker stamped when repo_bootstrap completed without a
    # structured overview. Preserved through build_config validation so routine
    # saves do not erase the truth.
    bootstrap_degraded_reason: str | None = None
    # Stateful automatic repo_bootstrap recovery metadata. These fields are
    # maintained by repo_bootstrap/task_completion so automatic sweeps can back
    # off and eventually stop while operator retry remains available.
    bootstrap_recovery_attempts: int | None = Field(None, ge=0)
    bootstrap_recovery_state: str | None = None
    bootstrap_recovery_next_at: str | None = None
    bootstrap_recovery_last_degraded_at: str | None = None
    bootstrap_recovery_exhausted_at: str | None = None
    # Provenance for the persisted repo_overview, stamped by
    # _handle_repo_bootstrap_completion whenever a bootstrap completes with a
    # structured overview. Surfaced by the admin UI (which reads
    # project.build_config) so operators can see overview freshness and which
    # branch/revision it describes. All nullable for backward compatibility.
    bootstrap_overview_refreshed_at: str | None = None
    bootstrap_overview_source_branch: str | None = None
    bootstrap_overview_source_revision: str | None = None
    # Durable onboarding-proof milestone timestamps stamped idempotently by
    # recording sites (autonomous_research, agent_github, ci_resolution,
    # landed_signal).  None means the milestone has not yet occurred.  Preserved
    # through detect/save/PATCH round-trips so a re-detect never erases evidence.
    first_task_spawned_at: str | None = None
    first_pr_opened_at: str | None = None
    first_merge_gate_verdict_at: str | None = None
    # Truthful gate-verdict sub-fields: executed_tests is NEVER True when the
    # gate fell open unverified; unverified_reason carries the fall-open key.
    first_merge_gate_executed_tests: bool | None = None
    first_merge_gate_unverified_reason: str | None = None
    first_pr_merged_at: str | None = None

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
    # When set, enables the warm VM pool for this project at onboarding time
    # with the given per-project target size, avoiding a separate operator
    # action after creation. preview_only=True suppresses this.
    warm_pool_target_size: int | None = Field(None, ge=1, le=50)
