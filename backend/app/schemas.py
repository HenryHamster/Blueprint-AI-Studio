"""Pydantic request/response schemas for Blueprint API endpoints."""

import uuid
from typing import List, Dict, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models import TaskStatus, ArtifactType


class DeployPayload(BaseModel):
    name: str
    payloads: List[Dict[str, Any]]
    github_owner: str | None = Field(None, pattern="^[a-zA-Z0-9_.-]+$")
    github_repo: str | None = Field(None, pattern="^[a-zA-Z0-9_.-]+$")


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
    # Cumulative per-task LLM telemetry. Optional/backwards-compatible — older
    # agents that don't yet emit these fields silently skip persistence; newer
    # agents send running totals on every heartbeat. Persisted into
    # Task.api_call_summary so the orchestrator can aggregate
    # api-calls-per-task and total-tokens-per-task across the fleet.
    llm_call_count: int | None = Field(None, ge=0)
    total_input_tokens: int | None = Field(None, ge=0)
    total_output_tokens: int | None = Field(None, ge=0)


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


class BuildConfig(BaseModel):
    """Schema for project build configuration."""

    language: str | None = Field(None, pattern="^(python|typescript|go|rust|mixed)$")
    directories: List[BuildConfigDirectoryEntry] = Field(default_factory=list)
    env_vars: Dict[str, str] = Field(default_factory=dict)
    git_user_name: str = "Blueprint Agent"
    git_user_email: str = "blueprint-agent@automated.dev"
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


class OnboardPayload(BaseModel):
    """Payload for the project onboarding endpoint."""

    github_owner: str = Field(..., min_length=1, pattern="^[a-zA-Z0-9_.-]+$")
    github_repo: str = Field(..., min_length=1, pattern="^[a-zA-Z0-9_.-]+$")
    name: str | None = None
    build_config_override: Dict[str, Any] | None = None


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
