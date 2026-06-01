# Backend Modules Reference

Guide to `backend/app/` modules and their responsibilities.

---

## Core Infrastructure

### `main.py`

FastAPI application entry point. Defines:
- HTTP routes (admin, projects, agent, GitHub proxy)
- WebSocket handlers for real-time log streaming
- Request/response validation via Pydantic schemas
- CORS, rate limiting, error handling middleware
- Lifecycle hooks (startup, shutdown)
- VM deletion tracking (`_orchestrator_deleted_vms`) to distinguish orchestrator cleanup from GCP preemption

**Key functions:**
- `_ensure_vm_deleted()` — Retry-safe VM deletion with GCP coordination
- `fire_and_forget_vm_delete()` — Non-blocking background VM cleanup
- `is_orchestrator_initiated_delete()` — Check if VM was deleted by orchestrator (for reaper)

**Task priority constants:**
```python
TASK_PRIORITY_MAP = {"high": 100, "medium": 50, "low": 0}
```

**Schema revision:** Always matches the latest Alembic migration (currently `0131`).

---

### `models.py`

SQLModel data models (ORM) for all database tables. ~2600 lines covering:

**Core entities:**
- `Project` — Repository container
- `Task` — Work unit with lifecycle states
- `Artifact` — Task outputs (PR, commit, logs, reports)
- `TaskHistory` — Audit trail of status changes

**Enums:**
- `TaskStatus` — QUEUED, PROVISIONING, IN_PROGRESS, EVALUATING, COMPLETED, FAILED, CANCELLED
- `ProjectStatus` — DRAFT, DEPLOYING, COMPLETED, FAILED, CANCELLED
- `ArtifactType` — 13 types (GIT_COMMIT, PULL_REQUEST, ERROR_LOG, RESEARCH_FINDINGS, etc.)

**Auth & integration:**
- `AllowedUser` — Access control
- `UserIntegration` — Per-user GitHub credentials (App installation ID or fine-grained PAT)

**Strategic research mode:**
- `Direction` state management (stored as JSONB in `Project.directions`)
- Diagnosis workflow markers (auto-pause, diagnosis task spawn)

**Custom types:**
- `NaiveUTCDateTime` — SQLAlchemy decorator that strips timezone on read (DB uses TIMESTAMPTZ for server defaults, Python uses naive UTC)

See `docs/DATA_MODELS.md` for schema details.

---

### `schemas.py`

Pydantic request/response DTOs for API endpoints.

**Key schemas:**
- `DeployPayload` — Launch task request
- `AgentStatusPayload` — Task completion/failure report (~200 fields for telemetry)
- `HeartbeatRequest` — Agent keep-alive (~100 fields for metrics)
- `SpawnTaskRequest` — Child task creation
- `GitHubPRReviewRequest` — PR review submission
- `HeartbeatMetrics`, `ToolTraceEntry` — Structured telemetry

All schemas use `ConfigDict(extra="ignore")` to tolerate agent clients sending extra fields (backward compatibility).

---

### `database.py`

SQLAlchemy async session management.

**Exports:**
- `engine` — AsyncEngine connected to PostgreSQL
- `AsyncSessionLocal` — Session factory for dependency injection
- `get_session()` — FastAPI dependency for DB access

Uses `asyncpg` driver for true async/await with PostgreSQL.

---

### `auth.py`

Authentication & authorization.

**Functions:**
- `verify_agent_jwt()` — Check agent VM token (scoped to task ID, 2-hour expiry)
- `create_agent_token()` — Issue new JWT for agent
- `get_current_user()` — FastAPI dependency for Firebase-authenticated requests
- `verify_ws_ticket()` — Validate WebSocket ticket for real-time log streaming
- `create_ws_ticket()` — Generate short-lived WebSocket admission token

**Secrets:**
- `AGENT_JWT_SECRET` — Signs agent JWTs
- `WS_TICKET_SECRET` — Signs WebSocket tickets
- `ADMIN_API_KEY` — Server-to-server authentication

---

## Orchestration

### `orchestrator.py`

Main coordination loop. Implements:
- **Task polling** — Periodically fetches QUEUED tasks and transitions to PROVISIONING
- **VM provisioning** — Delegates to `compute.py`
- **Concurrency limits** — Respects `MAX_CONCURRENT_VMS` and `MAX_PROVISION_SLOTS`
- **Timeout enforcement** — Enforces 1-hour hard TTL on tasks
- **Retry logic** — Retries failed tasks up to 3 times with exponential backoff
- **Circuit breaker** — Halts new tasks if a project exceeds failure threshold

**Key functions:**
- `orchestrator_worker()` — Main polling loop (runs every 10s)
- `_provision_task()` — Handles the transition from QUEUED → PROVISIONING → (awaiting agent claim)

---

### `reaper.py`

Background worker that kills VMs on heartbeat timeout.

**Behavior:**
- Polls tasks every 30s with heartbeat timeout = 5 minutes
- For timed-out IN_PROGRESS tasks, marks task as FAILED and deletes VM
- Uses `is_orchestrator_initiated_delete()` to avoid misclassifying orchestrator-initiated cleanup as spot preemption
- Logs all preemptions and orphan cleanup for metrics

---

### `compute.py`

GCP Compute Engine interaction.

**Functions:**
- `provision_agent_vm()` — Create a VM instance from the Packer-built agent image
- `delete_agent_vm()` — Terminate a VM (with retries)
- `get_vm_details()` — Query VM status, IP, etc.

**Environment variables:**
- `GCP_PROJECT_ID`, `GCP_ZONE` — GCP target
- `MACHINE_TYPE` — VM size (e.g., e2-standard-2)
- `AGENT_IMAGE_FAMILY` — Packer-built image path
- `AGENT_SERVICE_ACCOUNT_EMAIL` — VM's service account (has zero default permissions)

VMs are created on **Spot** instances when available (cost savings), with fallback to on-demand.

---

### `compute_manifest.py`

Generates agent VM startup script (via `compute_startup_script.py`).

The script is base64-encoded and passed to GCP as `--metadata-from-file startup-script`. It:
1. Decodes the blueprint agent binary from the image
2. Sets up Node.js environment
3. Configures logging and diagnostics
4. Starts the agent process

---

## Task Lifecycle

### `task_lifecycle.py`

High-level task state machine.

**Transitions:**
- QUEUED → PROVISIONING → IN_PROGRESS → (optional EVALUATING) → COMPLETED/FAILED/CANCELLED
- QUEUED → CANCELLED (manual cancellation)
- IN_PROGRESS → FAILED (agent reported failure)

**Functions:**
- `transition_task_status()` — Atomically update task status with validation
- `record_task_history()` — Audit trail entry
- `_verify_state_transition()` — Enforce valid state machine

---

### `task_completion.py`

Handles terminal status transitions (COMPLETED, FAILED).

**Responsibilities:**
- Store artifacts and logs
- Compute task success/failure category
- Update project status
- Trigger cleanup (VM deletion)
- Emit telemetry (execution time, token usage, etc.)
- Check circuit breaker (halt if failure rate too high)

---

### `task_capabilities.py`

Task type → handler mapping and validation.

**Task types:**
- Code changes: `create`, `refactor`, `fix`, `qa`
- Review: `review`
- Planning & research: `plan`, `research`, `strategic_review`, `design_doc_update`
- Specialized: `security_audit`, `perf`, `debug`, `investigate`
- Docs: `docs`
- Ops: `deps`, `migrate`, `smoketest`
- System/diagnostic: `self_audit`, `release_audit`, `memo`

Each type has a handler in `agent/src/handlers/`.

---

## Autonomous Research Mode

### `autonomous_research.py`

Main loop for autonomous task spawning.

**Cycle:**
1. Load directions from project config
2. Select eligible directions (active, healthy, not starvation case)
3. For each direction, spawn a `research` task
4. Wait for results, feed into strategic cycle

**Starvation handling:** If no directions are active/healthy, create fallback directions to keep the pipeline fed.

---

### `strategic_spawning.py`

Decides which directions to spawn from and creates research tasks.

**Functions:**
- `select_direction_for_spawn()` — Pick an active direction using weighted random selection
- `spawn_research_task()` — Create a child research task

---

### `strategic_completion.py`

Post-research processing to generate next directions.

**Workflow:**
1. Collect research findings (structured JSON from agent)
2. Feed into a strategic review prompt
3. Generate candidate directions
4. Persist to project config

---

### `direction_lifecycle.py`

Direction state management (pause, resume, complete).

**Key states:**
- `active` — Currently spawning research tasks
- `paused` — On hold (user pause or auto-pause from health check)
- `completed` — Work done, no more tasks to spawn

**Auto-pause conditions:**
- Direction has failed N times in a row
- Success rate below threshold
- Stale (no recent activity)

---

### `autonomous_research_diagnosis.py`

Auto-pause investigation. When a direction is auto-paused:
1. Spawns an `investigate` task to diagnose the issue
2. Stores investigation findings in the direction
3. Recommends whether to resume or mark completed

---

## GitHub Integration

### `github.py`

GitHub API client using OAuth tokens.

**Functions:**
- `generate_installation_access_token()` — Get short-lived token for app-installed repo
- `create_pull_request()` — Open a PR
- `create_pr_comment()` — Add comment to PR
- `create_pr_review_comment()` — Comment on a specific code line
- `submit_pr_review()` — Submit "Approved" or "Changes Requested" review
- `update_commit_status()` — Set commit status (pending, success, failure)
- `create_check_run()` — Create GitHub check (for CI integration)
- `get_pull_request()` — Fetch PR metadata
- `get_issues()` — Search issues
- `create_issue()` — Open a new issue
- `create_branch()` — Create a branch from a base commit

All methods are async and include retry logic.

---

### `routers/github.py`

HTTP endpoint for GitHub webhooks.

**Webhooks handled:**
- `push` — Branch updates
- `pull_request` — PR state changes
- `check_run` — CI result notifications

---

## Code Analysis & Quality

### `failure_classification.py`

Categorize task failures: `build_failed`, `test_failed`, `lint_failed`, `clone_failed`, etc.

Used to:
- Group similar failures (metrics)
- Adjust retry logic per category
- Provide operators with actionable error bucketing

---

### `sr_blind_spots.py`

Aggregate self-review blind spots from agents across multiple tasks.

**Functions:**
- `aggregate_sr_blind_spot_themes()` — Find common blind spot patterns
- `aggregate_sr_co_failure_pairs()` — Identify which check types fail together

Feeds into strategic planning to focus investigation on high-risk areas.

---

### `finding_deduplication.py`

Prevent re-reporting the same issue from multiple research tasks.

**Logic:**
1. Normalize finding text (lowercase, strip whitespace)
2. Hash to fuzzy-match similar strings
3. Tag duplicate findings with original source task

---

## Analytics & Reporting

### `routers/admin_analytics.py`

Metrics dashboards for admin UI.

**Endpoints:**
- Task completion rates by type
- Success/failure distribution
- Cost breakdown (on-demand vs. spot)
- Latency percentiles (p50, p95, p99)
- Token usage by model
- Provider utilization

---

### `routers/admin_digest.py`

Digest summaries for humans (email-able reports).

---

### `efficiency_snapshot.py`

Capture snapshot of system efficiency metrics at periodic intervals.

**Metrics:**
- VM utilization (time idle, time working)
- Task queue depth
- Failure rate trend

---

## Security & Configuration

### `security.py`

Input validation & sanitization.

**Functions:**
- `sanitize_github_url()` — Prevent path traversal in repo URLs
- `validate_prompt()` — Reject known injection patterns
- `check_url_format()` — HTTP URL validation

---

### `url_security.py`

URL scheme whitelisting (prevent SSRF).

---

### `secrets_manager.py`

Interface to GCP Secret Manager.

**Functions:**
- `get_user_secret()` — Fetch encrypted token for a user (GitHub, Anthropic, etc.)
- `set_user_secret()` — Store encrypted token

---

### `routers/admin_settings.py`

User-facing settings management (autonomous mode config, retry budgets, etc.).

---

## Monitoring & Debugging

### `shared_state.py`

Global runtime state:
- Rate limiter instance (`limiter`)
- Redis client (`redis_client`)
- Runtime config dictionary (`runtime_config`)

---

### `broadcast.py`

WebSocket broadcast mechanism for log streaming.

**Functions:**
- `broadcast_to_project()` — Send log line to all connected clients
- `manager.connect()`, `manager.disconnect()` — Lifecycle

---

### `notification_dispatch.py`

Send notifications to users (email, Slack, in-app alerts).

---

### `routers/admin_health.py`

Health check endpoints for monitoring systems.

---

## Utility Modules

### `time_utils.py`

DateTime utilities.

**Functions:**
- `_utcnow()` — Current time in naive UTC (not `datetime.now()`)
- `_to_naive_utc()` — Strip timezone from aware datetimes

**Rationale:** The codebase uses naive UTC throughout for simplicity. The `NaiveUTCDateTime` SQLAlchemy decorator handles the DB layer (TIMESTAMPTZ columns).

---

### `payload_schemas.py`

Validation of task-specific payloads (per task type).

Each task type can have custom payload structure; this module validates against schemas.

---

### `storage.py`

GCS artifact storage interface.

**Functions:**
- `generate_upload_signed_url()` — Get signed URL for agent to upload file
- `download_artifact()` — Fetch artifact by URI

---

### `telemetry.py`

Distributed tracing setup (OpenTelemetry / Cloud Trace integration).

---

## Router Modules

### `routers/projects.py`

Project CRUD, deployment, WebSocket for logs.

**Endpoints:**
- `POST /api/deploy` — Launch task
- `GET /api/projects/{id}` — Fetch project
- `GET /api/projects/{id}/tasks` — List tasks
- `WebSocket /api/projects/{id}/ws` — Real-time logs

---

### `routers/agent.py`

Agent VM endpoints.

**Endpoints:**
- `POST /api/agent/claim` — Task claim
- `POST /api/agent/status` — Terminal status report
- `POST /api/agent/heartbeat` — Keep-alive
- `POST /api/agent/spawn-task` — Create child task
- `POST /api/agent/refresh-token` — JWT renewal
- GitHub proxy: `POST /api/agent/github/*`

---

### `routers/admin_*`

Admin UI endpoints (all require `ADMIN_API_KEY` or Firebase admin token).

**Major routers:**
- `admin_projects.py` — CRUD projects, apply filters, bulk ops
- `admin_settings.py` — Config management
- `admin_analytics.py` — Metrics & dashboards
- `admin_digest.py` — Reports
- `admin_health.py` — Orchestrator health
- `admin_notifications.py` — Alert management
- `admin_onboard.py` — First-time setup wizard

---

## Supporting & Integration Modules

### CI/PR Integration

#### `ci_completion.py`

Processes CI check results for tasks. Handles merge decisions based on CI status.

**Concepts:**
- **Merge gate** — Requires both CI passing (`ci_passed: True`) AND review approval (`review_approved: True`)
- **PR parking** — Task waits for CI before attempting merge
- Handles edge cases: merge conflicts, race conditions, stale checks

**Key functions:**
- `handle_ci_completion()` — Process CI result webhook/poll
- `_pr_number_match()` — Query payload by PR number (dialect-aware for Postgres/SQLite)

---

#### `ci_resolution.py`

Detects and classifies CI failures; spawns fix tasks for analysis and remediation.

---

#### `auto_remediation.py`

Detects failed checks on protected branches (main/master) and spawns fix tasks automatically.

**Protections:**
- Rate limiting — prevents spam
- Duplicate detection — avoids re-spawning for same failure
- Concurrency limits — caps fix-task spawning per project
- Suspension mechanism — temporarily halts remediation after repeated failures (exponential backoff)

**Key constants:**
- `SUSPENSION_BASE_COOLDOWN_HOURS` = 24 (initial; doubles per retry)
- `MAX_AUTO_RECOVERY_ATTEMPTS` = 3

---

### Issue Management

#### `issue_implementation.py`

GitHub issue integration: when an issue receives a trigger label (default: `auto:implement`), spawns a create task.

**Features:**
- Fetches issue context and comments
- Creates implementation task from issue body
- Posts status comment on issue
- Guard rails: rate limit, duplicate detection, concurrent-task limit

---

## Advanced Modules

### Release Qualification & Promotion

#### `release_qualifier.py`

Audit-driven release readiness assessment.

**Workflow:**
1. Evaluate code quality, test coverage, CI health
2. Score sub-categories (infra, audit, readiness)
3. Compute promotion score (0–1 range)
4. Decide: promote, hold, or remediate

**Key functions:**
- `run_qualification()` — Score entire release
- Pure scoring math lives in `release_scoring.py`; promotion execution in `release_promotion.py`; out-of-band reconciliation in `release_reconciliation.py`; lifecycle-stall alerts in `release_stall_alerts.py`.

#### `release_promotion.py`

Executes promotion after qualification passes.

---

#### `release_scoring.py`

Computes readiness scores from test results, metrics, and heuristics.

---

### Failure & Anomaly Detection

#### `failure_classification.py`

Categorizes task failures: `network_error`, `timeout`, `resource_exhaustion`, `user_error`, etc.

Used by orchestrator to decide: retry, halt, or escalate.

---

#### `anomaly_detection.py`

Detects anomalous task behavior (hangs, resource spikes, unusual latencies).

---

### GitHub Integration

#### `github.py`

GitHub API client (async, with caching and retry logic).

**Functions:**
- `get_pull_request()`, `create_pull_request()`, `get_check_runs()`
- `get_repo_installation_token()` — OAuth token for repo access
- `is_ancestor_of()` — Git ancestry check (via API)
- Caching: `@cache_for_minutes()` decorator (configurable TTL)

**Token management:**
- Refreshes tokens before expiry
- Caches token per repo to minimize API calls

---

#### `notification_dispatch.py`

Posts status updates, notifications, and diagnostic messages to GitHub.

**Notification types:**
- `post_auto_fix_spawned_message()` — Notify PR on auto-remediation
- `post_issue_implementation_spawned_message()` — Notify issue on auto-implement
- `post_failure_message()` — Diagnostic failure details
- Batches notifications to avoid spam

---

### Autonomy & Strategic Mode

#### `autonomous_research.py`

Spawns research tasks from strategic directions without manual intervention.

**Workflow:**
1. Load active directions from project config
2. For each direction: spawn research task
3. Collect findings → feed into strategic cycle
4. Deduplication: avoids re-proposing completed work

**Functions:**
- `autonomous_research_worker()` — Main background loop
- `_select_eligible_direction()` — Ranking & conflict-free selection
- `_spawn_research_task()` — Create task from direction

---

#### `strategic_spawning.py`

Generates strategic directions based on findings and heuristics.

**Inputs:**
- Research findings
- Codebase health metrics
- PR review history

**Outputs:**
- New Direction objects with recommended actions

---

#### `strategic_completion.py`

Handles completion of strategic-mode tasks (design doc updates, reviews, etc.).

---

### Telemetry & Analytics

#### `telemetry.py`

Collects execution metrics from agent heartbeats.

**Metrics:**
- Token usage
- Time per phase (planning, coding, review)
- Tool invocations
- Error types and frequencies

Used for cost reporting and performance analysis.

---

#### `digest_metrics.py`

Aggregates metrics over time windows (daily, weekly) for reporting.

---

### State & Storage

#### `shared_state.py`

Holds mutable state shared across background workers (e.g., circuit breaker status, deployment state).

**Pattern:**
- Redis-backed for distributed workers
- Falls back to in-memory dict in local mode
- Key-value store with TTL

---

#### `state_persistence.py`

Persists orchestrator state to database for crash recovery.

---

#### `storage.py`

Cloud Storage client for artifacts (logs, reports, JSON payloads).

---

### Utility Modules

#### `time_utils.py`

Timezone-safe datetime utilities:
- `_utcnow()` — Current UTC time (naive, without tzinfo)
- `_naive_from_iso()` — Parse ISO string to naive UTC
- Prevents timezone mismatches between DB and Python

---

#### `url_security.py`

Validates URLs for safety (prevents SSRF, open redirects).

---

#### `secrets_manager.py`

Encrypts/decrypts sensitive config (API keys, tokens) at rest in database.

---

#### `payload_schemas.py`

Defines task-specific payload schemas and validation logic.

Used by orchestrator to validate incoming task payloads before queueing.

---

## Testing Patterns

### `testing/conftest.py`

Pytest fixtures for common test setup:
- Database session
- Mock GCP credentials
- Sample project/task objects

---

### `testing/test_*.py`

Test files mirror source structure:
- `test_models.py` — Model validation
- `test_orchestrator.py` — Polling & provisioning
- `test_task_lifecycle.py` — State transitions
- `test_github.py` — GitHub API mocking
- etc.

**Key invariant checks:**
- Module size limits (`testing/module_size_limits.json`)
- Schema revision matching (`EXPECTED_SCHEMA_REVISION`)
- Test coverage for critical paths

---

## Summary Table

| Module | Purpose | Key Dependency |
|--------|---------|-----------------|
| main.py | FastAPI app + routes | schemas, models, orchestrator |
| models.py | ORM/database schema | database.py |
| database.py | Async PostgreSQL | SQLAlchemy |
| orchestrator.py | Task queue polling | task_lifecycle, compute |
| compute.py | GCP VM lifecycle | google-cloud-compute |
| task_lifecycle.py | State machine | models |
| github.py | GitHub API client | httpx, async |
| autonomous_research.py | Research task spawning | task_lifecycle, strategic_spawning |
| failure_classification.py | Error categorization | — |
| reaper.py | Heartbeat timeout handler | task_lifecycle, compute |
