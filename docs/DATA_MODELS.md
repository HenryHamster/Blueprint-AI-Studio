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

**Branch & namespace fields (onboarding-relevant):**

- `integration_branch` — per-project trunk for autonomous task spawning. `NULL`
  falls back to `"main"`. Set at onboarding to the repo's default branch and
  operator-managed thereafter via
  `PATCH /api/admin/projects/{id}/integration-branch`.
- `promotion_target` — per-project promotion/LTS branch with identical
  `NULL → "main"` semantics. Set at onboarding (defaults to the default branch,
  or the operator-supplied value) and tuned via `.../promotion-target`.
- `task_branch_namespace` — **not a column**; it is a derived property that reads
  `build_config["task_branch_namespace"]` and falls back to `"blueprint"`.
  Onboarding slugifies it from the repo name; agent task branches are named
  `{namespace}/{prefix}-{hex}`. Tuned via `.../task-branch-namespace`.

**`build_config`** — JSONB blob of dirs, commands, and git identity delivered to
the agent in its claim response. Validated against the backend `BuildConfig`
Pydantic schema (`backend/app/schemas.py`) both at onboarding
(`build_config_override`) and via `PATCH /api/admin/projects/{id}/build-config`.
Fields the schema does not declare are dropped at validation time, so a knob
only round-trips to the agent if `BuildConfig` declares it.

- `build_config.migration` — per-project **migration layout** for repos whose
  Python project does not follow Blueprint's `backend/alembic/versions` +
  `app/models` + `app/main.py` layout. Mirrors the agent's `MigrationConfig`
  (`agent/src/utils/test-runners.ts`); the migrate handler consumes it through
  `resolveMigrationLayout` (`agent/src/handlers/specialized.ts`). All sub-fields
  are optional; an unset field falls back to the Blueprint default shown below:

  | Sub-field | Meaning | Default when unset |
  |-----------|---------|--------------------|
  | `versions_dir` | Dir holding alembic version files | `<pythonDir>/alembic/versions` |
  | `models_dir` | Dir/file holding the SQLModel/SQLAlchemy models | `<pythonDir>/app/models` |
  | `alembic_cwd` | Working dir alembic runs from (where `alembic.ini` lives) | `<pythonDir>` |
  | `schema_revision_file` | File declaring the schema-revision constant | `<pythonDir>/app/main.py` |
  | `schema_revision_const` | Name of the schema-revision constant to bump | `EXPECTED_SCHEMA_REVISION` |

  An **empty-string** `schema_revision_file` (`""`) is meaningful and preserved:
  it signals "no schema-revision gate", so the migrate prompt omits the
  revision-bump step entirely rather than falling back to the default file. Set
  the whole object at onboard time via `build_config_override` and/or later via
  the project build-config PATCH endpoint; omit it entirely and the agent uses
  Blueprint's defaults.

  > **Distinct from `Project.alembic_versions_path`** (the single-string
  > `build_config.alembic_versions_path` knob): that field feeds only the
  > backend-side migration-file guard (which paths require migration review),
  > **not** the agent's `resolveMigrationLayout`. Set `build_config.migration`
  > to relocate where the *agent* looks for migrations; the two are not
  > interchangeable.

**`approval_gate_config`** — shape `{"infra_files": [...], "critical_files": [...]}`.
Read at PR time by `task_lifecycle_github_helpers._get_approval_gate_file_lists`
to decide which changed files require human approval before merge. **When
empty (the default), both sets are empty and every changed file skips the
file-based approval gate.**

Operators manage it through `GET|PATCH /api/admin/projects/{id}/approval-gate-config`
(`admin_project_config.py`): GET is member-gated, PATCH is owner-gated and a
partial update of the `infra_files` / `critical_files` lists. It is still seeded
**only for the self-repo** (`is_self_repo = true`) by Alembic migration
`0120_seed_self_repo_approval_gate_config.py`, but non-self projects can now be
set directly through the admin API.

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

## Release Lifecycle Models

These two models back the **qualify → promote release gate** that promotes work
from a project's **integration branch** to its **promotion target** (the
`integration_branch` / `promotion_target` fields on `Project`). They are
repo-agnostic: a "version" is simply the span of the integration branch between
two promotions. For Blueprint's own self-repo, the integration branch is
`automain` and the promotion target is `main`; for an onboarded repo, substitute
the branches you configured. See
[ONBOARDING.md — The release model](ONBOARDING.md#5-the-release-model--how-a-change-reaches-production)
for the operator-facing walkthrough.

### ReleaseVersion

One row per release "version" — the window of integration-branch work between
two promotions. The strategic layer (and operator endpoints) drive its phase
transitions; the release qualifier writes its qualification results.

```python
class ReleaseVersion(SQLModel, table=True):
    id: UUID
    project_id: UUID               # FK to Project (indexed)
    version_number: int = 1        # Monotonically increasing per project (1-based)
    phase: str = "build"           # build | stabilize (durable phases)
    status: str = "open"           # open | promoted | abandoned
    goal: str | None               # Snapshot of the version's intent when opened

    opened_at: DateTime            # When this version opened
    phase_changed_at: DateTime | None  # Stamped on build ↔ stabilize transitions
    promoted_at: DateTime | None   # Stamped when status → "promoted"

    base_sha: str | None           # Integration-branch HEAD when the version opened
    promoted_sha: str | None       # Integration-branch HEAD at successful promotion

    last_qualification: Dict | None    # Latest qualify result (OVERWRITTEN each run)
    qualification_attempts: int = 0    # Total qualification runs on this version
    phase_reasoning: str | None        # Strategic agent's reasoning for the phase

    created_at: DateTime
    updated_at: DateTime
```

**Lifecycle phases** (the version's place in the release pipeline):

- **`build`** — normal feature work landing on the integration branch via the
  two-flag merge gate.
- **`stabilize`** — work-mix shifts to audit / cleanup / hardening before a
  qualification run.
- **qualify** and **promote** are *transient orchestrator operations*, not durable
  `phase` values: a qualify run is reflected by `qualification_attempts` /
  `last_qualification`, and a successful promotion is reflected by
  `status="promoted"` + `promoted_at` + `promoted_sha`, after which the next
  version opens.

> Invariants: at most one row per project has `status="open"` at a time, and
> `(project_id, version_number)` is unique.  `last_qualification` holds only the
> **latest** run's result — the durable per-verdict history lives in
> `QualificationVerdict` below.  (The model also carries several
> `*_alerted_*` deduplication-anchor columns used by the calcification /
> build-stall / stabilize-stall / promotion-readiness sweeps — see
> `backend/app/models.py` for the full list.)

### QualificationVerdict

Immutable, append-only history of release-qualifier outcomes — **one row per
finalized qualification run**, never overwritten. Because
`ReleaseVersion.last_qualification` is overwritten on every run, this table is
what lets the dry-run track record be retained and aggregated into a
promotion-readiness recommendation (`release_qualifier.compute_promotion_readiness`).
Writes are best-effort and never roll back a qualification or promotion.

> **No ground truth (self-approval only).** `compute_promotion_readiness`
> aggregates `would_promote`, which is the qualifier's *own* confidence ≥
> threshold — so `would_promote_rate` is the rate at which the qualifier approved
> itself, not a validated precision. There is **no** post-promotion outcome column
> on this model (was the promoted candidate reverted, did main CI / a regression
> follow) and no outcome-feedback code in the `release_*` modules. A qualifier that
> systematically over-scores a defect class therefore reads ~100% "ready" while
> being wrong. The signal is consequently surfaced as a **qualifier self-approval
> streak** (the readiness dict carries `ground_truth_validated: False` and a
> `caveat` string; the UI and reason text drop the "trustworthy" wording). To
> upgrade this into a validated trust signal, add a post-promotion outcome record
> here, stamp it when known, and derive a true precision / false-positive rate.

```python
class QualificationVerdict(SQLModel, table=True):
    id: UUID
    project_id: UUID               # FK to Project (indexed)
    release_version_id: UUID       # FK to ReleaseVersion (indexed)
    candidate_sha: str | None      # Integration-branch HEAD SHA that was scored

    confidence: float = 0.0        # Composite promotion confidence in [0, 1]
    sub_scores: Dict | None        # Per-dimension: {audit, test_erosion, smoketest}
    blocker: str | None            # Hard-blocker code, or NULL when none fired
    audit_missing_reason: str | None   # Why the audit sub-score was unscoreable

    would_promote: bool = False    # Did the candidate clear the threshold?
    promotion_enabled: bool = False    # runtime_config["promotion_enabled"] at scoring
    terminal_status: str = ""      # dry_run | not_promoted | promoted | promotion_failed
    threshold: float | None        # The promotion threshold in force at scoring

    started_at: DateTime | None    # When the qualification started
    finalized_at: DateTime | None  # When it finalized
    created_at: DateTime
```

**Terminal outcomes** (`terminal_status`):

- **`dry_run`** — candidate would have promoted (or was scored) while
  `promotion_enabled` was False; no actual promotion happened. This is the normal
  outcome during the calibration window.
- **`not_promoted`** — confidence did not clear the threshold (or a blocker fired).
- **`promoted`** — confidence cleared the threshold with promotion enabled, and the
  integration branch was promoted to the promotion target.
- **`promotion_failed`** — promotion was attempted but the merge/promotion failed.

Operators read this history via
`GET /api/admin/projects/{project_id}/qualification-verdicts`.

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
  "status": "active|proposed|paused|rejected|expired|completed|retired|superseded",
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
