# Blueprint Data Models

This document describes the core data structures used throughout Blueprint.

---

## Database Models (SQLModel)

All models are defined in `backend/app/models.py` and stored in PostgreSQL via SQLAlchemy ORM.

### Project

Represents a code repository where tasks are executed.

```python
class Project(SQLModel, table=True):
    id: UUID                       # Unique identifier
    name: str                      # User-defined project name
    user_id: str                   # Firebase UID of the project owner (indexed)
    status: ProjectStatus          # DRAFT, DEPLOYING, COMPLETED, FAILED, CANCELLED
    github_owner: str | None       # GitHub org/user (e.g., "anthropics")
    github_repo: str | None        # GitHub repo name (e.g., "claude-code")
    created_at: DateTime           # Creation timestamp (naive UTC)

    # Autonomous / strategic state (all JSONB)
    directions: List[Dict]         # Strategic directions (active/paused/proposed/...)
    vision: Dict                   # Legacy product vision (being replaced by problem_model)
    problem_model: Dict            # Structured strategic understanding (app.problem_model)
    self_reflection_log: List[Dict]# Strategic agent's per-cycle observations
    design_doc: Dict               # Living architectural description
    running_update: str | None     # Markdown living narrative (rewritten each cycle)
    running_update_acknowledged_at: DateTime | None

    # Configuration & flags
    autonomous_enabled: bool = True # Toggle autonomous research/task generation
    is_self_repo: bool = False      # True when repo is Blueprint's own
    build_config: Dict | None       # Dirs, commands, git identity for the agent
    integration_branch: str | None  # Trunk for autonomous spawning (NULL → "main")
    promotion_target: str | None    # Promotion (LTS) branch (NULL → "main")
    approval_gate_config: Dict      # PR file-path flagging (infra/critical paths)
    sr_catch_rate_targets: Dict     # Per-category self-review catch-rate targets
    remediation_tracking: Dict | None  # Project-level CI auto-remediation outcomes

    # Relationship
    tasks: List[Task]               # All tasks in this project
```

> Project membership is a separate `ProjectMember` table (no `members`
> relationship on `Project`). There is no circuit-breaker column on the
> model — failure-loop halting is handled by the pipeline-health and
> anomaly-detection subsystems, not a per-Project flag.

**Status transitions:**
```
DRAFT → DEPLOYING → (COMPLETED | FAILED | CANCELLED)
```

---

### Task

Represents a single unit of work (e.g., "implement feature", "write tests", "review PR").

> **Note on task type:** there is no `task_type` column. A task's type
> (`create`, `refactor`, `review`, …) and all type-specific parameters
> live inside the `payloads` JSONB list. Use `task.first_payload` (the
> standard accessor for `payloads[0]`) to read it.

```python
class Task(SQLModel, table=True):
    id: UUID
    project_id: UUID               # FK to Project (indexed)
    parent_task_id: UUID | None    # FK to Task — hierarchy/DAG (indexed)
    title: str = "Untitled Task"
    description: str = ""          # Task prompt/requirements
    status: TaskStatus             # QUEUED, PROVISIONING, IN_PROGRESS, EVALUATING, COMPLETED, FAILED, CANCELLED
    priority: int = 50             # 0 (low) to 100 (high); used for queue ordering
    created_by: str = "USER"       # Provenance: USER | ORCHESTRATOR | STRATEGIC_AGENT | SYSTEM_* (indexed)
    requires_approval: bool = False
    idempotency_key: str | None

    # Work definition & shared state (all JSONB)
    payloads: List[Dict]           # Type-specific parameters; payloads[0] holds task_type
    context: Dict                  # Execution context (timing, metrics, errors, metadata)
    depends_on: List[str]          # Task UUIDs that must complete+merge before dispatch
    status_log: List[Dict]         # Append-only status-message history
    fix_loop_history: Dict         # Per-task lint/test/check fix-loop telemetry
    plan_output: Dict | None       # Parsed plan output (plan-type tasks)

    # Execution / VM
    vm_instance_id: str | None     # GCP Compute Engine instance name (indexed)
    vm_warm_start: bool | None     # Whether the VM was reused warm
    provider: str | None           # "anthropic" | "openai" | "gemini"
    model: str | None              # LLM model used
    account_id: str | None

    # Heartbeat / liveness
    last_heartbeat: DateTime | None
    heartbeat_count: int = 0
    last_tool_use_at: DateTime | None
    prompt_fingerprint: str | None # 12-hex digest of the assembled guidance

    # Lifecycle
    created_at: DateTime           # (indexed)
    started_at: DateTime | None
    updated_at: DateTime
    archived: bool = False

    # Retry & failure tracking
    attempt_count: int = 0         # Number of execution attempts
    previous_error_fingerprint: str | None
    auto_remediation: bool = False
    strategic_cycle_id: str | None

    # Optimistic-concurrency guard
    version: int = 0

    # Relationships
    artifacts: List[Artifact]      # Generated PRs, commits, reports, screenshots
    history: List[TaskHistory]     # Audit trail
```

(Additional columns exist for VM cost/token banking and post-commit
bookkeeping — see `backend/app/models.py` for the full list.)

**Status flow:**
```
QUEUED → PROVISIONING → IN_PROGRESS → (EVALUATING → COMPLETED | FAILED | CANCELLED)
```

---

### Artifact

Output from a task (PR link, commit, log file, screenshot, report).

```python
class Artifact(SQLModel, table=True):
    id: UUID
    task_id: UUID                  # FK to Task (indexed)
    artifact_type: ArtifactType    # GIT_COMMIT, PULL_REQUEST, ERROR_LOG, RESEARCH_FINDINGS, etc.
    commit_hash: str | None        # Commit SHA (for GIT_COMMIT)
    repo_url: str | None
    pr_url: str | None             # PR URL (for PULL_REQUEST)
    storage_uri: str | None        # gs://bucket/path (for uploaded files)
    logs: str | None
    iteration: int = 0             # Review/fix iteration that produced this artifact
    created_at: DateTime
```

> There is no `metadata` JSONB column — type-specific data is captured in
> the dedicated `commit_hash` / `pr_url` / `repo_url` / `storage_uri`
> columns. A partial UNIQUE index enforces at most one `PULL_REQUEST`
> artifact per task.

**Artifact types** (13 total — `ArtifactType` enum):
- `GIT_COMMIT` — Code changes with commit hash
- `PULL_REQUEST` — PR URL and metadata
- `SCREENSHOT` — PNG/JPG from UI testing
- `ERROR_LOG` — Task failure logs
- `RESEARCH_FINDINGS` — Structured investigation results
- `SECURITY_REPORT` — Security audit findings
- `SMOKETEST_RESULT` — Deployment verification
- `DEBUG_RESULT` — Debug task output
- `PERF_REPORT` — Performance profiling
- `INVESTIGATION_REPORT` — Investigate task output
- `SELF_AUDIT_REPORT` — Self-audit task output
- `RELEASE_AUDIT_REPORT` — Release-audit task output
- `MEMO` — Reasoning artifact (autonomous mode)

---

### TaskHistory

Audit trail of task status changes.

```python
class TaskHistory(SQLModel, table=True):
    id: UUID
    task_id: UUID                  # FK to Task (indexed)
    timestamp: DateTime
    action: str                    # "STATUS_CHANGE", "SPLIT", "SPAWNED_CHILD", "MODIFIED_PAYLOAD"
    details: Dict                  # Additional state at the time of the action (JSONB)
```

---

## Enums

### TaskStatus

```python
class TaskStatus(str, Enum):
    QUEUED        # Waiting for orchestrator to provision VM
    PROVISIONING  # VM being created on GCP
    IN_PROGRESS   # Agent running on VM
    EVALUATING    # Post-execution review/testing (optional)
    COMPLETED     # Successfully finished
    FAILED        # Execution failed; may be retried
    CANCELLED     # Manually cancelled
```

### ProjectStatus

```python
class ProjectStatus(str, Enum):
    DRAFT      # Project created but not yet deployed
    DEPLOYING  # Initial task(s) in progress
    COMPLETED  # All work done successfully
    FAILED     # Project-level failure (circuit breaker)
    CANCELLED  # Manually stopped
```

### ArtifactType

See above under Artifact section.

---

## Pydantic Schemas (API DTOs)

Request/response objects defined in `backend/app/schemas.py`.

### DeployPayload

Task creation request.

```python
class DeployPayload(BaseModel):
    name: str
    payloads: List[Dict[str, Any]]
    github_owner: str | None
    github_repo: str | None
```

### AgentStatusPayload

Task completion/failure report from agent VM.

```python
class AgentStatusPayload(BaseModel):
    task_id: UUID
    status: TaskStatus
    artifact_type: ArtifactType | None
    pr_url: str | None
    commit_hash: str | None
    logs: str | None               # Up to 500KB
    verdict_json: str | None       # Self-review structured result
    failure_category: str | None
    provider: str | None           # "anthropic", "openai", "gemini"
    auth_mode: str | None          # "oauth", "api_key", "service_account"
    model: str | None              # LLM model used
    self_review_outcome: Dict[str, Any] | None
    diff_summary: List[Dict] | None
```

### HeartbeatRequest

Keep-alive from agent VM (sent every ~60 seconds).

```python
class HeartbeatRequest(BaseModel):
    task_id: UUID
    status_message: str | None
    metrics: HeartbeatMetrics | None   # rss_mb, heap_used_mb, disk_free_mb, cgroup_*
    tool_trace: List[ToolTraceEntry] | None  # Ring buffer of recent tool calls
    fix_loop_events: List[Dict] | None
    prompt_fingerprint: str | None     # 12-hex guidance fingerprint
    vm_image_name: str | None          # GCP boot-disk image identity
```

`HeartbeatMetrics` carries `rss_mb`, `heap_used_mb`, `disk_free_mb`,
`disk_total_mb`, `cgroup_mb`, and `cgroup_limit_mb`. The heartbeat does
**not** carry a `status` enum — terminal status is reported separately
via `POST /api/agent/status` (`AgentStatusPayload`).

---

## Autonomous Research Mode

### Direction

Strategic direction for the autonomous research worker. Stored as JSONB in `Project.directions`.

The direction blob has an open vocabulary; the promoted/known keys are:

```json
{
  "id": "string",                 // direction_key
  "text": "string",               // "Reduce test latency", etc.
  "status": "active|proposed|paused|rejected|expired",
  "priority": "high|medium|low",
  "spawned_task_ids": ["uuid", ...],
  "last_spawned_at": "ISO8601 | null",
  "recommended_mode": "string",   // "active" or "paused" based on health
  "diagnosis_in_progress": false, // Auto-pause diagnosis pending
  "diagnosis_task_id": "uuid | null",
  "diagnosis_started_at": "ISO8601 | null"
}
```

> Directions are persisted both in `Project.directions` (JSONB) and a
> dedicated `Direction` table (migration 0131); `app.directions_repo`
> reads/writes the table while keeping the same dict shape.

---

## Task Type Metadata

Each task type (create, review, fix, research, docs, etc.) can have type-specific payloads.

### Standard Task Payloads

**create:**
```json
{
  "prompt": "Implement feature X",
  "base_branch": "main",
  "target_branch": "feature/x"
}
```

**refactor:**
```json
{
  "description": "Extract utility functions",
  "scope": "backend/app/models.py"
}
```

**review:**
```json
{
  "pr_number": 123,
  "focus_areas": ["performance", "security"]
}
```

**research:**
```json
{
  "research_topic": "Database scalability",
  "scope": ["backend/app/database.py"],
  "research_mode": "autonomous|manual"
}
```

**docs:**
```json
{
  "documentation_task": "API reference",
  "scope": "backend/app/routers"
}
```

---

## Authentication Models

### AllowedUser

Allowlist of users permitted to access Blueprint.

```python
class AllowedUser(SQLModel, table=True):
    id: UUID
    email: str                     # Unique email (indexed)
    firebase_uid: str | None       # Firebase UID for Firebase Auth (indexed)
    role: str = "user"             # "admin" or "user"
    display_name: str | None
    created_at: DateTime
    last_login_at: DateTime | None
```

### UserIntegration

Per-user GitHub credentials (one row per user, unique on `user_id`).

```python
class UserIntegration(SQLModel, table=True):
    id: UUID
    user_id: str                   # Firebase UID (required, unique)
    github_installation_id: str | None  # GitHub App installation ID
    github_account_name: str | None
    github_pat: str | None         # Fine-grained PAT (alternative to App install)
    created_at: DateTime
    updated_at: DateTime
```

---

## Telemetry & Metrics

### FixLoopEvent

Track fix-loop iterations during code review and remediation.

```json
{
  "loop_name": "lint|test|check",
  "iteration": 1,
  "timestamp": "ISO8601",
  "exit_reason": "success|max_iterations|timeout",
  "duration_ms": 12345
}
```

### SelfReviewOutcome

Self-attestation from agent on code quality.

```json
{
  "verdict": "pass|fail|unknown",
  "check_results": {
    "correctness": { "passed": true, "reasoning": "..." },
    "security": { "passed": true, ... },
    "test_coverage": { "passed": false, "issue": "..." }
  },
  "blind_spots": [
    "Did not verify edge case X",
    "Unsure if Y is thread-safe"
  ]
}
```

---

## Indexes

Key indexes for query performance (see `Task.__table_args__` in
`backend/app/models.py` for the full set, including composites and GIN
indexes):

- `Task.project_id`, `Task.parent_task_id`, `Task.vm_instance_id`, `Task.created_by` — single-column lookups
- `ix_task_status_priority_created` (`status`, `priority`, `created_at`) — dispatch queue ordering
- `ix_task_project_status_created` (`project_id`, `status`, `created_at`) — per-project status queries
- `ix_task_context_gin`, `ix_task_depends_on_gin` — JSONB containment queries
- `Artifact.task_id` — Output retrieval
- `Project.user_id` — Owner's project listing
- `AllowedUser.email`, `AllowedUser.firebase_uid` — Authentication lookup

See `backend/alembic/versions/` for migration history.

---

## Schema Versioning

The current schema revision is tracked in `backend/app/main.py`:
```python
EXPECTED_SCHEMA_REVISION = "0131"
```

This matches the latest Alembic migration. When adding new models or columns:
1. Create a new migration chained from the latest `down_revision` (do NOT
   modify existing migration files — applied history is immutable)
2. Apply locally: `bash backend/run_migrations.sh`
3. Update `EXPECTED_SCHEMA_REVISION` in `main.py` to match the new head
4. Run the backend test suite (`bash scripts/test-backend.sh`)

See `CLAUDE.md` — File Restrictions for details on modifying migrations.
