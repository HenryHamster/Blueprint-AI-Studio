# Blueprint Data Models

This document describes the core data structures used throughout Blueprint.

---

## Database Models (SQLModel)

All models are defined in `backend/app/models.py` and stored in PostgreSQL via SQLAlchemy ORM.

### Project

Represents a code repository where tasks are executed.

```python
class Project(SQLModel, table=True):
    id: UUID                      # Unique identifier
    name: str                      # User-defined project name
    status: ProjectStatus          # DRAFT, DEPLOYING, COMPLETED, FAILED, CANCELLED
    github_owner: str | None       # GitHub org/user (e.g., "anthropics")
    github_repo: str | None        # GitHub repo name (e.g., "claude-code")
    github_branch: str = "main"    # Base branch for PRs
    description: str | None        # Project description
    created_at: DateTime           # Creation timestamp
    updated_at: DateTime           # Last modification timestamp
    created_by: str | None         # User email who created it
    
    # Many-to-one relationships
    tasks: List[Task]              # All tasks in this project
    members: List[ProjectMember]   # Users with access
    directions: JSONB              # Strategic directions metadata
    
    # Circuit breaker state
    circuit_breaker_enabled: bool = False
    failure_count_24h: int = 0
    last_reset_at: DateTime | None
```

**Status transitions:**
```
DRAFT → DEPLOYING → (COMPLETED | FAILED | CANCELLED)
```

---

### Task

Represents a single unit of work (e.g., "implement feature", "write tests", "review PR").

```python
class Task(SQLModel, table=True):
    id: UUID
    project_id: UUID               # FK to Project
    task_type: str                 # "create", "refactor", "review", "research", etc.
    status: TaskStatus             # QUEUED, PROVISIONING, IN_PROGRESS, EVALUATING, COMPLETED, FAILED, CANCELLED
    priority: int = 50             # 0 (low) to 100 (high); used for queue ordering
    
    # Execution context
    vm_instance_id: str | None     # GCP Compute Engine instance name
    assigned_to: str | None        # User email (for manual tasks)
    
    # Input & output
    description: str               # Task prompt/requirements
    payloads: List[Dict]           # Task-specific parameters (JSON)
    context: Dict                  # Execution context (logs, errors, metadata)
    
    # GitHub integration
    github_owner: str | None
    github_repo: str | None
    github_branch: str | None      # Feature branch name
    pr_url: str | None             # Generated PR URL
    commit_hash: str | None        # Final commit SHA
    
    # Artifacts (outputs)
    artifacts: List[Artifact]      # Generated files, reports, screenshots
    
    # Lifecycle
    created_at: DateTime
    updated_at: DateTime
    started_at: DateTime | None
    completed_at: DateTime | None
    ttl_at: DateTime | None        # Hard deadline for VM cleanup
    
    # Retry & failure tracking
    retry_count: int = 0
    failure_category: str | None   # "build_failed", "test_failed", etc.
    
    # Strategic research mode
    research_direction_id: UUID | None  # If spawned by autonomous mode
    is_async_research: bool = False
```

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
    task_id: UUID                  # FK to Task
    artifact_type: ArtifactType    # GIT_COMMIT, PULL_REQUEST, ERROR_LOG, RESEARCH_FINDINGS, etc.
    storage_uri: str | None        # gs://bucket/path (for files)
    metadata: Dict                 # Type-specific data (PR number, commit SHA, etc.)
    created_at: DateTime
```

**Artifact types:**
- `GIT_COMMIT` — Code changes with commit hash
- `PULL_REQUEST` — PR URL and metadata
- `SCREENSHOT` — PNG/JPG from UI testing
- `ERROR_LOG` — Task failure logs
- `RESEARCH_FINDINGS` — Structured investigation results
- `SECURITY_REPORT` — Security audit findings
- `SMOKETEST_RESULT` — Deployment verification
- `PERF_REPORT` — Performance profiling
- `MEMO` — Reasoning artifact (autonomous mode)

---

### TaskHistory

Audit trail of task status changes.

```python
class TaskHistory(SQLModel, table=True):
    id: UUID
    task_id: UUID
    status: TaskStatus
    changed_at: DateTime
    changed_by: str | None         # User or system
    context: Dict | None           # Additional state at transition time
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

Keep-alive from agent VM (sent every 60 seconds).

```python
class HeartbeatRequest(BaseModel):
    task_id: UUID
    status: TaskStatus
    rss_mb: float | None           # Memory usage
    disk_free_mb: float | None
    logs_tail: str | None
```

---

## Autonomous Research Mode

### Direction

Strategic direction for the autonomous research worker. Stored as JSONB in `Project.directions`.

```json
{
  "id": "uuid",
  "title": "string",              // "Reduce test latency", etc.
  "description": "string",
  "mode": "active|paused|completed",
  "priority": 0-100,
  "spawned_task_ids": ["uuid", ...],
  "last_spawned_at": "ISO8601 | null",
  "resolution_findings": {...},   // End-state summary
  "recommended_mode": "string",   // "active" or "paused" based on health
  "diagnosis_in_progress": bool,  // Auto-pause diagnosis pending
  "diagnosis_task_id": "uuid | null",
  "diagnosis_started_at": "ISO8601 | null"
}
```

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
    email: str                     # Unique email
    firebase_uid: str | None       # Firebase UID for Firebase Auth
    role: str                      # "admin" or "user"
```

### UserIntegration

OAuth tokens and API credentials for users (GitHub, Anthropic, etc.).

```python
class UserIntegration(SQLModel, table=True):
    id: UUID
    user_id: UUID | None
    provider: str                  # "github", "anthropic", etc.
    refresh_token: str | None
    access_token_encrypted: str    # Encrypted at rest
    expires_at: DateTime | None
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

Key indexes for query performance:

- `Task.project_id` — Quick project task lookup
- `Task.status` — Filter by status (QUEUED, IN_PROGRESS, etc.)
- `Task.created_at` — Timeline queries
- `Task.assigned_to` — User task listing
- `Artifact.task_id` — Output retrieval
- `AllowedUser.email` — Authentication lookup

See `backend/alembic/versions/` for migration history.

---

## Schema Versioning

The current schema revision is tracked in `backend/app/main.py`:
```python
EXPECTED_SCHEMA_REVISION = "0114"
```

This matches the latest Alembic migration. When adding new models or columns:
1. Create a new migration: `alembic revision --autogenerate -m "message"`
2. Apply locally: `bash backend/run_migrations.sh`
3. Update `EXPECTED_SCHEMA_REVISION` in `main.py`
4. Test with `bash scripts/test-backend.sh testing/test_models.py`

See `CLAUDE.md` — File Restrictions for details on modifying migrations.
