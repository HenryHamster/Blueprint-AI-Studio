# Backend Modules Reference

Guide to `backend/app/` modules and their responsibilities.

---

## Core Infrastructure

### `main.py`

FastAPI application entry point. Defines:
- Registers the router families under `backend/app/routers/`, including the
  public project/task routes, admin dashboard routes, agent lifecycle routes,
  GitHub proxy routes, warm-continuity routes, and deploy-state routes
- WebSocket handlers for real-time log streaming
- Request/response validation via Pydantic schemas
- CORS, rate limiting, error handling middleware
- Lifecycle hooks (startup, shutdown)
- VM deletion tracking (`_orchestrator_deleted_vms`) to distinguish orchestrator cleanup from GCP preemption

**Key functions** (canonical implementations live in `vm_lifecycle.py`; `main.py`
re-exports them with `# noqa: F401` for back-compat — new code should import from
`app.vm_lifecycle` directly):
- `_ensure_vm_deleted()` — Retry-safe VM deletion with GCP coordination
- `fire_and_forget_vm_delete()` — Non-blocking background VM cleanup
- `is_orchestrator_initiated_delete()` — Check if VM was deleted by orchestrator (for reaper)

**Task priority constants:**
```python
TASK_PRIORITY_MAP = {"high": 100, "medium": 50, "low": 0}
```

**Schema revision:** Always matches the latest Alembic migration (currently `0330`).

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
- `AgentStatusPayload` — Task completion/failure report (many optional telemetry fields)
- `HeartbeatRequest` — Agent keep-alive (carries optional metrics/tool-trace fields)
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
- `verify_agent_jwt()` — Check agent VM token scoped to a task ID
- `create_agent_token()` — Issue an agent JWT (1-hour standard TTL; 8-hour bootstrap TTL before first refresh)
- `get_current_user()` — FastAPI dependency for Firebase-authenticated requests
- `verify_ws_ticket()` — Validate WebSocket ticket for real-time log streaming
- `create_ws_ticket()` — Generate short-lived WebSocket admission token

**Secrets:**
- `AGENT_JWT_SECRET` — Signs agent JWTs
- `WS_TICKET_SECRET` — Signs WebSocket tickets
- `ADMIN_API_KEY` — Server-to-server authentication

---

### `lifecycle.py`

Application startup/shutdown lifecycle (extracted from `main.py`). Responsibilities:
- Launches and supervises the background worker tasks (`orchestrator_worker`,
  `reaper_service`, `gcp_cleanup_service`, `secret_reconciliation_service`,
  `pipeline_alert_scan_service`, `digest_precompute_service`, and others)
- Session-scoped advisory-lock leader election (only the leader instance runs the
  full worker set; non-leaders run a reduced set)
- Background-task crash monitoring with operator alerts

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
- `orchestrator_worker()` — Main polling loop (runs every 15s, configurable via `ORCHESTRATOR_HARD_WATCHDOG_POLL_SECONDS`); also drives
  autonomous-research spawning by calling `_check_idle_research()` each cycle
- `_lock_provisioning_task_for_vm()` — Locks an existing PROVISIONING task to a VM
  before handing it to the agent (the provisioning flow is spread across
  `orchestrator_worker()` and its inner `_provision_and_handle()` helper rather
  than a single `_provision_task()` entry point)

---

### `reaper.py`

Background worker that kills VMs on heartbeat timeout.

**Behavior:**
- Polls tasks every 30s with heartbeat timeout = 5 minutes
- For timed-out IN_PROGRESS tasks, marks task as FAILED and deletes VM
- Uses `is_orchestrator_initiated_delete()` to avoid misclassifying orchestrator-initiated cleanup as spot preemption
- Logs all preemptions and orphan cleanup for metrics

---

### `reaper_sweeps.py`

Reaper sweep dispatcher: routes each named sweep to its implementation and
re-exports public symbols from the extracted sub-modules so existing import
sites require no changes.

**Sub-modules extracted here:**
- `reaper_fleet_health_sweeps.py` — fleet-level degradation alerts
- `blocked_task_unblock_sweeps.py` — BLOCKED task auto-unblock / escalation

---

### `reaper_fleet_health_sweeps.py`

Fleet-level health monitoring sweeps extracted from `reaper_sweeps.py`.
Monitors degradation patterns across all projects and raises operator alerts
when fleet-wide thresholds are breached.

**Key sweeps:**
- `sweep_merge_gate_verification_degradation()` — alerts when the no-CI
  merge-gate verification success rate drops below threshold fleet-wide

---

### `blocked_task_unblock_sweeps.py`

Auto-unblock and escalation sweep helpers extracted from `reaper_sweeps.py`.
Implements the Step 12 BLOCKED-task lifecycle: clears blocking conditions
when dependencies are resolved, escalates to operators when tasks remain
blocked past the configured threshold.

**Key functions:**
- `_clear_blocked_fields(preserve_first_blocked)` — clears blocker metadata
  with two modes: optimistic (preserve escalation stamp and defer counter on
  conflict requeue) vs. genuine clear (wipe all defer state)
- Re-exports all public sweep entry points to `app.reaper_sweeps`

---

### `vm_lifecycle.py`

Canonical home for orchestrator-side VM deletion tracking (extracted from `main.py`,
which re-exports for back-compat).

**Key functions:**
- `_ensure_vm_deleted()` — Retry-safe VM deletion coordinated with GCP
- `fire_and_forget_vm_delete()` — Non-blocking background VM cleanup
- `is_orchestrator_initiated_delete()` — Distinguish orchestrator cleanup from GCP
  preemption so the reaper does not misclassify it

Also maintains the in-process set of orchestrator-deleted VMs and GCP VM-count
caching used for concurrency limits.

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
- `_process_post_commit()` — The async entry point that processes an agent's
  terminal status report: resolves the verdict, advances the task through its
  terminal states, records history, and triggers follow-on work. State-transition
  and history logic is embedded here and in `lifecycle.py` rather than in
  standalone `transition_task_status()` / `record_task_history()` helpers.
- `_has_active_strategic_task()`, `_increment_research_spawn_count()` — strategic-cycle helpers

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
- Specialized: `security_audit`, `perf`, `debug`, `investigate`, `smoketest`, `migrate`
- Docs: `docs`
- Ops/system: `deps`, `self_audit`, `release_audit`, `memo`

Each type has a handler in `agent/src/handlers/`.

---

## HTTP Routers

### `backend/app/routers/projects.py`

Public project/task launch endpoints under `/api/*`.

**Routes:**
- `POST /api/deploy`
- `GET /api/projects`
- `GET /api/projects/{project_id}`
- `GET /api/projects/{project_id}/tasks`
- `POST /api/projects/{project_id}/ws-ticket`
- `POST /api/projects/{project_id}/cancel`
- `GET /api/projects/{project_id}/status`

### `backend/app/routers/agent.py`

Primary agent lifecycle surface under `/api/agent/*`.

**Routes:**
- `POST /api/agent/claim`
- `POST /api/agent/status`
- `POST /api/agent/heartbeat`
- `POST /api/agent/spawn`
- `POST /api/agent/refresh-token`
- `POST /api/agent/artifacts/upload-url`
- `POST /api/agent/viewport/failure-screenshot-upload-url`
- `GET /api/agent/github/token`
- `GET /api/agent/github/check-runs`
- `POST /api/agent/github/commit-status`
- `POST /api/agent/github/pr-comment`
- `POST /api/agent/github/pr-review-comment`
- `POST /api/agent/github/pr-review`
- `POST /api/agent/github/issues`
- `GET /api/agent/github/issues`
- `POST /api/agent/github/branches`
- `POST /api/agent/github/pull-requests`

### `backend/app/routers/agent_warm_continuity.py`

Warm-VM continuity and backlog-reuse endpoints under `/api/agent/*`.

**Routes:**
- `POST /api/agent/vm-heartbeat`
- `POST /api/agent/next-role`

### `backend/app/routers/github.py`

GitHub OAuth, PAT, and webhook integration under `/api/github/*`.

**Routes:**
- `POST /api/github/pat`
- `GET /api/github/oauth/authorize`
- `GET /api/github/oauth/callback`
- `POST /api/github/webhook`

### `backend/app/routers/deploy.py`

Deployment-state and zone-health operational routes.

**Routes:**
- `GET /deploy-state`
- `POST /deploy-state/pause`
- `POST /deploy-state/resume`
- `POST /deploy-state/reset-health`
- `POST /deploy-state/mark-stable`
- `POST /deploy-state/rollback`
- `GET /zone-health`

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

Builds the enriched payload (design-doc view, problem model, system-maturity rows,
efficiency snapshot) that is shipped to the strategic agent each cycle. It is a
context/payload builder, **not** the spawner — the actual direction selection and
research-task creation live in `autonomous_research.py` (`_spawn_research_task`,
`_check_idle_research`).

**Functions:**
- `_agent_view_design_doc()` — Strip operator-only keys from the design doc before it goes to the agent
- `_agent_view_problem_model()` — Trim per-gap history to stay under the payload size cap

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

> **Note:** auto-pause diagnosis is not a standalone module. The
> `diagnosis_in_progress` / `diagnosis_task_id` markers on a direction (see
> `docs/DATA_MODELS.md`) are managed within `direction_lifecycle.py`.

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

### `fix_loop_telemetry.py`

Records per-iteration fix-loop telemetry (lint/test fix attempts and outcomes)
reported by agents. (There is no general `telemetry.py` / OpenTelemetry tracing
module; execution metrics arrive on the agent status and heartbeat payloads and
are aggregated by `digest_metrics.py`.)

---

## Router Modules

### `routers/projects.py`

Project CRUD, deployment, cancellation, status, and WebSocket for logs.

**Endpoints:**
- `POST /api/deploy` — Launch task
- `GET /api/projects` — List visible projects
- `GET /api/projects/{id}` — Fetch project
- `GET /api/projects/{id}/tasks` — List tasks
- `POST /api/projects/{id}/ws-ticket` — Issue WebSocket ticket
- `POST /api/projects/{id}/cancel` — Cancel project
- `GET /api/projects/{id}/status` — Project status summary
- `WebSocket /api/projects/{id}/ws` — Real-time logs

---

### `routers/agent.py`

Agent VM endpoints.

**Endpoints:**
- `POST /api/agent/claim` — Task claim
- `POST /api/agent/heartbeat` — Keep-alive
- `POST /api/agent/spawn` — Create child task
- `POST /api/agent/refresh-token` — JWT renewal
- `POST /api/agent/artifacts/upload-url` — Signed artifact upload URL
- `POST /api/agent/viewport/failure-screenshot-upload-url` — Signed viewport screenshot upload URL

### `routers/agent_status.py`

Terminal agent status updates and task result recording.

**Endpoints:**
- `POST /api/agent/status` — Terminal status report

---

### `routers/agent_warm_continuity.py`

Warm-VM continuity for task chaining and backlog reuse.

**Endpoints:**
- `POST /api/agent/vm-heartbeat` — VM-scoped heartbeat
- `POST /api/agent/next-role` — Warm handoff / backlog claim

---

### `routers/agent_credentials.py`

Credential rotation for running agents.

**Endpoints:**
- `POST /api/agent/credentials/rotate` — Propagate refreshed provider secret

### `routers/agent_strategic_fetch.py`

Read-only endpoints for an agent to pull full detail on strategic-payload items
that were trimmed at claim time (agent JWT, rate-limited 60/min).

**Endpoints:**
- `GET /api/agent/projects/{project_id}/directions/{direction_id}`
- `GET /api/agent/projects/{project_id}/playbook/{key:path}`
- `GET /api/agent/projects/{project_id}/gaps/{gap_id}`

---

### `routers/agent_github.py`

Agent-side GitHub API proxy.

**Endpoints:**
- `GET /api/agent/github/token`
- `GET /api/agent/github/check-runs`
- `POST /api/agent/github/commit-status`
- `POST /api/agent/github/pr-comment`
- `POST /api/agent/github/pr-review-comment`
- `POST /api/agent/github/pr-review`
- `POST /api/agent/github/issues`
- `GET /api/agent/github/issues`
- `POST /api/agent/github/branches`
- `POST /api/agent/github/pull-requests`

---

### `routers/github.py`

User GitHub OAuth flow and webhook ingestion.

**Endpoints:**
- `POST /api/github/pat`
- `GET /api/github/oauth/authorize`
- `GET /api/github/oauth/callback`
- `POST /api/github/webhook`

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

### Branch & Release Model

#### `branch_resolution.py`

Per-project branch/namespace resolution and agent work-branch naming (split out of
`shared_state.py`). This is the single source of truth for **which branch a task
targets**, so the merge/promotion gates never hardcode `automain`/`main` — they
resolve through these helpers instead.

**Branch model:** Agent PRs target a project's *integration branch* (the
qualification trunk, `"automain"` for Blueprint's self-repo, the GitHub default
branch for an onboarded external repo). Promotion lands on the *promotion target*
(`"main"`). When the two coincide, the long-term-support (LTS) release model is
inactive and the project is "single-branch".

**Key functions:**
- `get_project_integration_branch(project)` — Integration/qualification branch.
  Fallback chain: `project.integration_branch` column → `"automain"` (self-repo)
  → `"main"`. Fails *safe* (never raises) so it is callable from read endpoints,
  recovery loops, and hot paths; it stages an operator signal when the column is
  unset.
- `get_project_promotion_target(project)` — Promotion/release branch.
  `project.promotion_target` column → `"main"`.
- `is_single_branch_project(project)` — Canonical "LTS model inactive" predicate:
  `True` when integration branch == promotion target. The release machinery keys
  several short-circuits on this (`release_promotion.promote`,
  `hotfix_backmerge`, `release_reconciliation`).
- `get_project_task_branch_namespace(project)` — Branch namespace
  (`build_config['task_branch_namespace']` → global default `"blueprint"`).
- `derive_task_branch(...)` / `resolve_task_branch(task, namespace)` — Synthesize
  or look up the agent work branch (`{namespace}/{prefix}-{8hex}`).
  `resolve_task_branch` prefers the branch the agent actually pushed
  (`context["review_loop"]["branch"]` → `first_payload["branch"]` →
  `context["branch"]`) and only synthesizes a name as a last resort.
- `get_project_pr_title_prefix(project)` / `ensure_project_pr_title_prefix(...)` —
  Read and idempotently apply a project's configured PR title prefix.
- `backfill_branch_columns(session, project_id=None)` — Startup backfill that
  populates NULL `integration_branch` / `promotion_target` from GitHub defaults;
  only writes still-NULL columns, so it is safe to re-run.

`InvalidProjectBranchConfiguration` is raised when a project's integration branch
would route work directly to the promotion target — a gate-bypass guard enforced
at the merge/rollup boundary.

---

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

#### `release_readiness.py`

Aggregates recent qualification history into an operator-facing readiness
verdict, indicating whether promotion is ready and by how much.

Tracks two complementary precision signals:
- **Self-approval rate** (`would_promote_rate`) — how often the qualifier's
  own confidence exceeded the promotion threshold (available immediately)
- **Validated precision** — post-promotion `serving_confirmed` outcomes
  recorded by `release_reconciliation`; only surfaced once the resolved
  sample reaches the minimum threshold (`ground_truth_validated` flag)

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
- `_check_idle_research()` — Idle-pipeline entry point; called each `orchestrator_worker()`
  cycle (there is no dedicated `autonomous_research_worker` loop — autonomy piggybacks
  on the orchestrator loop)
- `_check_autonomous_research(owner, repo)` — Event-driven entry point invoked from the
  GitHub webhook handler
- `_spawn_research_task()` / `_spawn_research_task_inner()` — Create a research task from a direction
- `_spawn_periodic_task()` — Spawn periodic non-research strategic tasks

---

#### `autonomous_research_residual.py`

Residual-fingerprint create-fix lane: the explicitly-preserved
auto-remediation lane re-homed from the deleted `autonomous_research_investigations`
module. Contains three functions:

- `_infer_fingerprint_audit_system()` — maps a fingerprint's contributing
  tasks back to a `SystemMaturity` row; used to defer remediation when a
  system is being actively audited
- `_record_residual_audit_deferral()` — stamps deferral on the
  `ResidualRemediationCase` row and one-shot-escalates at the configured
  threshold
- `_spawn_residual_fingerprint_remediation()` — spawns at most one fix
  task per call for the top uncooled residual CODE_ERROR fingerprint;
  re-exported from `autonomous_research.py` for back-compat

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

#### Execution metrics

Execution metrics (token usage, time per phase, tool invocations, error
frequencies) are not collected by a dedicated `telemetry.py`. They arrive on
the `AgentStatusPayload` (terminal report) and `HeartbeatRequest` (keep-alive)
schemas and are aggregated for cost/performance reporting by the modules below.

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

### CI & Merge Workflows

#### `ci_completion.py`

Processes CI check results for tasks waiting on CI.

**Responsibilities:**
- Poll or receive GitHub check-run status
- Merge on success (via merge decision logic)
- Spawn fix tasks on failure
- Handle edge cases: merge conflicts, race conditions, abandoned PRs

**Key functions:**
- `handle_ci_completion()` — Main handler called from webhook or polling loop

---

#### `ci_merge_helpers.py`

Handles merge decisions and conflict resolution.

**Functions:**
- `_review_approved()` — Validate the review loop reached approval
- `_capture_merge_snapshot()` — Snapshot gate fields before releasing the row lock
- `_relock_merge_task()` — Re-acquire the lock and re-validate against the snapshot
- `_normalize_merge_error()` / `_is_deterministic_merge_error()` — Classify merge errors
- `_post_merge_summary()` — Post the post-merge summary comment

---

### Monitoring & Alerting

#### `alert_dispatch.py`

Sends operator alerts for high-severity issues.

**Triggers:**
- Circuit breaker opened (repeated failures)
- Zone exhaustion
- GitHub rate limit exceeded
- Provider quota reached

**Delivery:** Webhook/Slack/email (via `notification_service`).

---

#### `anomaly_detection.py`

Detects pipeline anomalies in real time.

**Signals:**
- Success rate drops below threshold
- Token cost spike
- Latency spike
- Failure clustering

**Response:**
- Auto-throttle concurrency
- Pause autonomous research
- Create operator alert

---

#### `deploy_health.py`

Monitors deployment health after a new release.

**Checks:**
- Error rate within baseline
- Latency within baseline
- No cascading failures

**Actions:**
- Mark deploy as STABLE when safe
- Trigger rollback if unsafe

---

### Utilities & Helpers

#### `admin.py`

Admin-only endpoints and helpers (re-exported from main.py).

---

#### `admin_helpers.py`

Utilities for admin operations (project config, user management).

---

#### `admin_task_mutations.py`

Helpers for admin-triggered task operations (spawn, cancel, pause).

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
| **Core Infrastructure** | | |
| main.py | FastAPI app + routes | schemas, models, orchestrator |
| models.py | ORM/database schema | database.py |
| database.py | Async PostgreSQL | SQLAlchemy |
| schemas.py | Pydantic request/response DTOs | fastapi |
| auth.py | Firebase, JWT, API key auth | firebase_admin |
| **Orchestration** | | |
| orchestrator.py | Task queue polling | task_lifecycle, compute |
| reaper.py | Heartbeat timeout handler | task_lifecycle, compute |
| reaper_sweeps.py | Sweep dispatcher + re-exports | reaper_fleet_health_sweeps, blocked_task_unblock_sweeps |
| reaper_fleet_health_sweeps.py | Fleet-level degradation alerts | reaper_sweeps |
| blocked_task_unblock_sweeps.py | BLOCKED task auto-unblock/escalation | models, task_lifecycle |
| compute.py | GCP VM lifecycle | google-cloud-compute |
| compute_manifest.py | Agent secret manifest building | models |
| **Task Management** | | |
| task_lifecycle.py | State machine | models |
| task_completion.py | Terminal status handling | task_lifecycle |
| ci_completion.py | CI result processing | github, task_lifecycle |
| task_capabilities.py | Task type → handler mapping | — |
| **GitHub & Repositories** | | |
| github.py | GitHub API client | httpx, async |
| branch_resolution.py | Per-project branch/promotion resolution | models |
| **Autonomous Research** | | |
| autonomous_research.py | Research task spawning | task_lifecycle, strategic_spawning |
| autonomous_research_residual.py | Residual fingerprint fix lane | models, task_lifecycle |
| strategic_spawning.py | Context building for strategic agent | models |
| strategic_completion.py | Post-research processing | models |
| direction_lifecycle.py | Direction state management | models |
| **Release Qualification** | | |
| release_qualifier.py | Promotion gate scoring | release_scoring, release_promotion |
| release_scoring.py | Readiness score computation | models |
| release_readiness.py | Qualification history aggregation | models |
| release_promotion.py | Promotion execution | github, models |
| **Monitoring & Diagnostics** | | |
| failure_classification.py | Error categorization | — |
| ci_merge_helpers.py | Merge decision logic | github, task_lifecycle |
| alert_dispatch.py | Operator alert delivery | models |
| anomaly_detection.py | Pipeline anomalies | models |
| **Utilities** | | |
| lifecycle.py | Background workers startup/shutdown | asyncio |
| payload_schemas.py | Task payload validation | models |
