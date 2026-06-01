# Blueprint API Reference

This document describes the public HTTP surface exposed by the Blueprint orchestrator.
Routes are grouped by router family; the implementation lives in `backend/app/main.py`
and the router modules under `backend/app/routers/`.

The API surface is split into a few families:

- Public project/task launch and status routes under `/api/*`
- Agent lifecycle, warm handoff, GitHub proxy, and artifact upload routes under `/api/agent/*`
- Admin settings, diagnostics, analytics, onboarding, and project-configuration routes under `/api/admin/*`
- GitHub OAuth and webhook routes under `/api/github/*`

## Authentication

- **Admin UI & User Endpoints**: Bearer token from Firebase Auth in `Authorization: Bearer <firebase_token>` header
- **Agent VM Endpoints**: Custom JWT in `Authorization: Bearer <agent_jwt>` header (2-hour expiry, scoped to task ID)
- **Server-to-Server**: `ADMIN_API_KEY` in `X-Admin-Key` header

---

## Projects API

### `POST /api/deploy`

Launch a new task on a repository.

**Request body:**
```json
{
  "name": "string",                    // Project name
  "payloads": [{"key": "value"}],      // Task-specific parameters
  "github_owner": "string | null",     // GitHub repo owner (optional)
  "github_repo": "string | null"       // GitHub repo name (optional)
}
```

**Response:**
```json
{
  "status": "accepted",
  "project_id": "uuid",
  "message": "Project created. <N> tasks queued for provisioning."
}
```

One `Project` and one parent `Task` are created, plus one child `Task` per
entry in `payloads`. The child tasks are enqueued (`QUEUED`) for the
orchestrator worker to provision; their IDs are not returned in the response —
fetch them via `GET /api/projects/{project_id}/tasks`.

**Status codes:**
- `200` — Project created and tasks queued
- `429` — Rate limited (5 per minute)
- `422` — Invalid task payload (failed `validate_task_payload`)
- `401` — Unauthorized

---

### `GET /api/projects`

List projects visible to the authenticated user.

**Query parameters:**
- `status`: Optional project status filter
- `limit`: Max projects to return (default 50)
- `offset`: Pagination offset (default 0)

**Response:** `{ "projects": [...], "pagination": { "limit": 50, "offset": 0, "count": 0 } }`

---

### `GET /api/projects/{project_id}`

Retrieve project details.

**Response:**
```json
{
  "id": "uuid",
  "name": "string",
  "status": "DRAFT|DEPLOYING|COMPLETED|FAILED|CANCELLED",
  "github_owner": "string",
  "github_repo": "string",
  "created_at": "ISO8601",
  "updated_at": "ISO8601",
  "tasks": [...]
}
```

---

### `GET /api/projects/{project_id}/tasks`

List all tasks in a project.

**Query parameters:**
- `status`: Filter by status (QUEUED, IN_PROGRESS, COMPLETED, FAILED, CANCELLED)
- `limit`: Max tasks to return (default 50)
- `offset`: Pagination offset (default 0)

**Response:** `{ "tasks": [...], "total": int, "has_more": bool }`

---

### `POST /api/projects/{project_id}/cancel`

Cancel a project and its active work.

---

### `GET /api/projects/{project_id}/status`

Return the current project/task status summary used by the dashboard.

---

### `WebSocket /api/projects/{project_id}/ws`

Stream live task logs and real-time project events via WebSocket. Authentication
uses a short-lived ticket sent as the **first WebSocket message** (not a query
parameter), obtained from `POST /api/projects/{project_id}/ws-ticket`.

**Connection flow:**
1. `POST /api/projects/{project_id}/ws-ticket` → returns `{"ticket": "string"}`
2. Connect to `ws://orchestrator/api/projects/{project_id}/ws`
3. Immediately send the ticket as the first JSON message: `{"ticket": "<ticket>"}`
   (a missing/invalid ticket, a `project_id` mismatch, or no message within 5 s
   closes the socket with code `1008`)
4. On success the server sends `{"event": "connected", ...}` followed by a
   `{"event": "deploy_state", ...}` hydration snapshot, then streams events.
   See [WebSocket Message Format](#websocket-message-format) for the envelope.

### Other project routes

- `POST /api/projects/{project_id}/ws-ticket` - Mint a short-lived WebSocket ticket
- `POST /api/projects/{project_id}/cancel` - Cancel the project and active work
- `GET /api/projects/{project_id}/status` - Dashboard-friendly status summary

---

## Agent API

These endpoints are for agent VMs. All require valid agent JWT.

### `POST /api/agent/claim`

Claim a task and start work.

**Request body:**
```json
{
  "task_id": "uuid"
}
```

**Response:**
```json
{
  "task_id": "uuid",
  "status": "IN_PROGRESS",
  "payload": {...},
  "github_owner": "string",
  "github_repo": "string",
  "github_branch": "string"
}
```

---

### `POST /api/agent/status`

Report task completion or failure (terminal status).

**Request body:**
```json
{
  "task_id": "uuid",
  "status": "COMPLETED|FAILED|CANCELLED",
  "artifact_type": "GIT_COMMIT|PULL_REQUEST|ERROR_LOG|...|null",
  "pr_url": "string | null",
  "commit_hash": "string | null",
  "logs": "string",
  "verdict_json": "{...}",
  "failure_category": "string | null"
}
```

**Response:** `{ "success": true }`

---

### `POST /api/agent/heartbeat`

Keep-alive signal (sent every 60s). Prevents VM reaper from terminating the instance.

**Request body:**
```json
{
  "task_id": "uuid",
  "status_message": "string | null",
  "metrics": { "rss_mb": 0.0, "disk_free_mb": 0.0, "...": "see HeartbeatMetrics" },
  "tool_trace": [ { "t": "ISO8601", "tool": "string", "input_preview": "string" } ],
  "prompt_fingerprint": "12-hex | null"
}
```

**Response:** `{ "success": true, "extend_until": "ISO8601" }`

---

### `POST /api/agent/spawn`

Create a subtask (child task spawned by the agent during its work).

**Request body:**
```json
{
  "task_id": "uuid",
  "child_task_type": "string",
  "payload": {...}
}
```

**Response:** `{ "child_task_id": "uuid" }`

---

### `POST /api/agent/refresh-token`

Extend the JWT expiry (renews for another 2 hours).

**Request body:**
```json
{
  "task_id": "uuid"
}
```

**Response:** `{ "token": "new.jwt.token", "expires_at": "ISO8601" }`

---

### `POST /api/agent/credentials/rotate`

Propagate a refreshed provider secret for the current task/account.

### `GET /api/agent/github/token`

Return a GitHub access token for the current task context.

### `GET /api/agent/github/check-runs`

List check runs relevant to the current task or PR context.

### `POST /api/agent/github/commit-status`

Create or update a commit status for the current repository.

### `POST /api/agent/github/pr-comment`

Create a pull request comment.

### `POST /api/agent/github/pr-review-comment`

Create an inline review comment on a pull request.

### `POST /api/agent/github/pr-review`

Submit a pull request review.

### `POST /api/agent/github/issues`

Create a GitHub issue in the target repository.

### `GET /api/agent/github/issues`

List GitHub issues for the target repository.

### `POST /api/agent/github/branches`

Create a branch through the GitHub proxy.

### `POST /api/agent/github/pull-requests`

Create a pull request through the GitHub proxy.

---

### `POST /api/agent/artifacts/upload-url`

Get a signed GCS URL to upload a file artifact.

**Request body:**
```json
{
  "task_id": "uuid",
  "filename": "string"
}
```

**Response:**
```json
{
  "url": "https://storage.googleapis.com/...",
  "storage_uri": "gs://bucket/path"
}
```

---

### `POST /api/agent/viewport/failure-screenshot-upload-url`

Get a signed upload URL for viewport-failure screenshots.

---

### `POST /api/agent/vm-heartbeat`

VM-scoped liveness for warm-VM continuity.

---

### `POST /api/agent/next-role`

Warm-VM handoff endpoint that either claims a pinned task or exits with no work.

### `POST /api/agent/tasks/{original_task_id}/reviewer-merge`

Internal helper used by review workflows to merge approved changes.

### `GET /api/agent/projects/{project_id}/directions/{direction_id}`

Fetch a single direction for agent-side strategic work.

### `GET /api/agent/projects/{project_id}/playbook/{key:path}`

Read a project playbook entry.

### `GET /api/agent/projects/{project_id}/gaps/{gap_id}`

Fetch a specific gap entry for the agent research workflow.

---

### `POST /api/agent/tasks/{original_task_id}/reviewer-merge`

Reviewer-merge helper invoked by a fresh-reviewer agent to record/perform the
merge decision for the task it reviewed.

---

### Strategic context fetch

Read-only endpoints the agent uses to pull full detail on items that were
trimmed/summarized in the strategic claim payload. All require the agent JWT,
are rate-limited 60/minute, and return `{ "status": "success", "data": ... }`.
Cross-project access returns 404 (not 403) to prevent id enumeration.

- `GET /api/agent/projects/{project_id}/directions/{direction_id}` — one direction
  by id, with `research_history` capped to the most recent 20 cycles.
- `GET /api/agent/projects/{project_id}/playbook/{key:path}` — a single
  `playbook/<key>` design-doc entry with full content (honors `pending_source`
  visibility rules).
- `GET /api/agent/projects/{project_id}/gaps/{gap_id}` — a single problem-model
  gap (active or archived) with full history/past_moves/evidence.

---

### `WebSocket /api/agent/ws/{task_id}`

WebSocket channel used by the agent VM to stream high-throughput logs and
real-time events back to the orchestrator. The agent must send its agent JWT as
the first message (`{"token": "<agent_jwt>"}`) before streaming; an invalid or
missing token closes the socket. This is the agent-side ingress counterpart to
the read-only `WebSocket /api/projects/{project_id}/ws` that the admin UI
consumes.

---

## GitHub Proxy Endpoints

Agent VMs use these to call GitHub APIs without storing credentials locally.

### `/api/agent/github/*`

The agent GitHub proxy router exposes:

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

### `/api/github/*`

The user GitHub OAuth/webhook router exposes:

- `POST /api/github/pat`
- `GET /api/github/oauth/authorize`
- `GET /api/github/oauth/callback`
- `POST /api/github/webhook`

## Admin API

Admin routes are mounted under `/api/admin` unless noted otherwise.

### Common admin families

- `GET /api/admin/settings`, `PATCH /api/admin/settings`, `GET /api/admin/providers`
- `GET /api/admin/github/repos`, `GET /api/admin/github/branches`
- `POST /api/admin/restart-orchestrator`
- `POST /api/admin/memory/tracemalloc/start`, `POST /api/admin/memory/tracemalloc/stop`, `GET /api/admin/memory/snapshot`
- `POST /api/admin/github-quota/refresh`, `POST /api/admin/github-quota/reset-tracking`
- `GET /api/admin/projects`, `GET /api/admin/projects/{project_id}`, `PATCH /api/admin/projects/{project_id}`, `DELETE /api/admin/projects/{project_id}`
- `POST /api/admin/projects/onboard`
- `POST /api/admin/projects/{project_id}/detect-build-config`
- `POST /api/admin/projects/{project_id}/running-update/acknowledge`
- `GET /api/admin/autonomous-status`, `POST /api/admin/autonomous/pause-all`
- `GET /api/admin/health-overview`, `GET /api/admin/warm-vm-state`, `GET /api/admin/pr-behind-detector-stats`
- `GET /api/admin/activity`
- `GET /api/admin/alerts`, `POST /api/admin/alerts/{alert_id}/dismiss`, `POST /api/admin/alerts/dismiss-all`
- `GET /api/admin/messages`, `POST /api/admin/messages`
- `GET /api/admin/digest`
- `GET /api/admin/provider-cost-summary`, `GET /api/admin/fix-loop-convergence`, `GET /api/admin/outer-loop-convergence`, `GET /api/admin/pipeline-regressions`
- `GET /api/admin/recent-orchestrator-errors`
- `POST /api/admin/projects/{project_id}/self-audit`, `POST /api/admin/projects/{project_id}/qualify`, `POST /api/admin/tasks/{task_id}/rollup`
- `GET /api/admin/projects/{project_id}/findings`
- `GET|PUT /api/admin/projects/{project_id}/playbook`
- `DELETE /api/admin/projects/{project_id}/playbook/{key}`
- `POST /api/admin/projects/{project_id}/playbook/seed`
- `POST /api/admin/projects/{project_id}/backfill-branches`
- `GET|PATCH /api/admin/projects/{project_id}/integration-branch`
- `GET|PATCH /api/admin/projects/{project_id}/promotion-target`

### Deploy state routes

Mounted from `backend/app/routers/deploy.py`:

- `GET /deploy-state`
- `POST /deploy-state/pause`
- `POST /deploy-state/resume`
- `POST /deploy-state/reset-health`
- `POST /deploy-state/mark-stable`
- `POST /deploy-state/rollback`
- `GET /zone-health`

## Admin API

Access these via the admin UI (requires `ADMIN_API_KEY` or Firebase admin token).

### `/api/admin/*`

The admin surface is split across `admin.py` and `backend/app/routers/admin_*.py`.
Common route families include project CRUD, settings, analytics, digest/reporting,
health, notifications/messages, users, onboarding, scope measurement, pool-account
management, and operator/debug triggers. See the router modules for the exact paths.

> **New to Blueprint?** For an end-to-end walkthrough of installing the GitHub
> App on a target repository and creating + tuning a project, see
> **[ONBOARDING.md](ONBOARDING.md)**.

#### Project Onboarding

**`POST /api/admin/projects/onboard`** — Create a project for a GitHub repo,
auto-detect its build configuration, and seed branch defaults.
(`backend/app/routers/admin_onboard.py`, requires admin; rate-limited 10/min.)

Request body (`OnboardPayload`):

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `github_owner` | string | ✅ | GitHub org/user. Pattern `^[a-zA-Z0-9_.-]+$`. |
| `github_repo` | string | ✅ | Repo name. Pattern `^[a-zA-Z0-9_.-]+$`. |
| `name` | string | — | Display name. Defaults to `{owner}/{repo}`. |
| `build_config_override` | object | — | Merged on top of auto-detected `BuildConfig` (override wins). |
| `promotion_target` | string | — | Promotion/LTS branch. Defaults to the repo's default branch. |

Server-derived fields (NOT operator input):

- `integration_branch` and `promotion_target` default to the repo's **default
  branch** (resolved via `get_repo_default_branch`).
- `build_config` is **auto-detected** from the repo root + one subdirectory level.
- `task_branch_namespace` is **slugified** from the repo name.
- The three design-doc automation pipelines — `_auto_remediation`,
  `_issue_implementation`, and `_auto_review` — are **seeded OFF**
  (`enabled: false`).
- `autonomous_enabled` is **not** part of that seeding: it is a boolean column
  that defaults to `True` (`models.py`) and the onboard handler never sets it, so
  an onboarded project has `autonomous_enabled=True`. Disable it explicitly via
  `PATCH /projects/{project_id}/autonomous` if needed.

Token resolution & errors: the handler resolves an installation token via
`get_repo_installation_token(owner, repo)` (needs `GITHUB_APP_ID` /
`GITHUB_PRIVATE_KEY` and the App installed on the repo). If no token is
available it returns **`422 Unprocessable Entity`**
(`"No installation token available — cannot detect default branch."`); a GitHub
failure while resolving the default branch returns `502`.

Response: `{ "project_id", "build_config", "default_branch" }`.

#### Per-Project Branch & Build Config

All under `/api/admin/projects/{project_id}` (`admin_project_config.py`).
GET requires `member`; PATCH requires `owner`. Pass `null` to revert a field
to its default. Each response includes `is_default`.

- **`GET|PATCH /integration-branch`** — `{ "integration_branch": "<branch>"|null }`.
  PATCH validates the branch name and confirms it exists on the remote (422 if not).
- **`GET|PATCH /promotion-target`** — `{ "promotion_target": "<branch>"|null }`.
- **`GET|PATCH /task-branch-namespace`** — single branch segment (no `/`).
- **`GET|PATCH /task-branch-prefix`** — defaults to `{namespace}/`.
- **`GET|PATCH /staging-branch-prefixes`** — list of prefixes (default `["research/", "plan/"]`).
- **`GET|PATCH /approval-gate-config`** — `{ "approval_gate_config": { "infra_files": [...],
  "critical_files": [...] }, "is_default": bool }`. PATCH is a partial update (send either or
  both lists) and validates that every entry is a plausible repo-relative path string (no
  leading `/`, no `..` traversal, no shell metacharacters → 422). Writes take effect for
  agents immediately through the existing task-claim response seam (no agent-side change).
- **`PATCH /api/admin/projects/{project_id}`** (`ProjectUpdate`, owner-only for
  `build_config`) — updates the full `build_config` blob, including
  `stale_branch_prefixes`, `protected_branches`, and `enable_self_improvement`.
- **`GET|PATCH /auto-remediation`**, **`/issue-implementation`**, **`/auto-review`**
  — automation pipelines (all seeded `enabled: false`).
- **`PATCH /projects/{project_id}/autonomous`** — `{ "enabled": bool }` toggles
  `autonomous_enabled`.

---

## System & Diagnostics

Top-level operational endpoints defined directly on the app in
`backend/app/main.py` (not under a router prefix). Health and metrics endpoints
are unauthenticated so external monitors and load balancers can reach them; the
profile, detailed-health, and debug endpoints require a valid Firebase/admin
token.

### `GET /`

Unauthenticated liveness ping. Returns `{ "status": "online", "service": "Blueprint Orchestrator" }`.

### `GET /health`

Unauthenticated health check for external monitoring (Cloud Function, load
balancer). Non-leader instances always report healthy; on the leader, liveness is
gated on the orchestrator worker's in-cycle progress heartbeat. Returns
`{ "status", "is_leader", "revision", ... }`. Only a DB outage (or a genuinely
wedged worker) yields a `503` that removes the instance from the load balancer —
provider/external dependency degradation is surfaced but does not fail the check.

### `GET /metrics`

Prometheus-compatible metrics in the text exposition format
(`text/plain`). Unauthenticated by default; if the `METRICS_TOKEN` env var is
set, the request must include a matching `?token=<value>` query parameter
(`403` otherwise). Exposes orchestrator cycle age, active VM counts, consecutive
autonomous-failure counts, and related gauges.

### `GET /api/version`

Public version endpoint used by the frontend for compatibility checks. Returns
`{ "sha": "<git-sha>", "revision": "<alembic-revision>" }`.

### `GET /api/me`

Authenticated. Returns the calling user's profile:
`{ "user_id", "email", "role" }`.

### `GET /api/health/detailed`

Authenticated. Returns structured health status for all system components
(database with latency, providers, external dependencies, and a pipeline-health
snapshot): `{ "status", "components": {...}, "pipeline_health": {...} }`.

### `GET /api/debug/orchestrator`

Authenticated diagnostic endpoint that reports whether the orchestrator loop is
alive, including counts of `QUEUED` (non-approval-gated) and active
(`PROVISIONING`/`IN_PROGRESS`) tasks.

### `GET /api/admin/viewport-failures/download-url`

Authenticated. Generates a 1-hour signed GCS download URL for a viewport-failure
screenshot so the regression panel can display it without a per-task DB lookup.

**Query parameters:** `project_id` (uuid), `component` (string), `width` (int).

**Response:** `{ "url": "https://storage.googleapis.com/..." }`. Returns `503`
if artifact storage is unavailable. The object path mirrors what the agent
uploads via `POST /api/agent/viewport/failure-screenshot-upload-url`.

---

## Error Responses

All errors follow this format:

```json
{
  "detail": "Human-readable error message",
  "status": 400
}
```

Common HTTP status codes:
- `400` — Bad request (validation error)
- `401` — Unauthorized (missing or invalid auth)
- `403` — Forbidden (insufficient permissions)
- `404` — Not found (project/task doesn't exist)
- `429` — Rate limited
- `500` — Server error
- `503` — Service unavailable (DB down, etc.)

---

## Rate Limiting

Per-route limits are declared in the router code and vary by endpoint.
Notable examples: `/api/deploy` (5/min), `/api/projects` (30/min),
`/api/projects/{project_id}` and `/api/projects/{project_id}/tasks` (60/min),
`/api/agent/claim` (200/min), `/api/agent/heartbeat` (600/min),
`/api/agent/refresh-token` (120/min), and `/api/agent/credentials/rotate` (30/min).

---

## WebSocket Message Format

Every message the server pushes over `/api/projects/{project_id}/ws` is JSON with
a two-field envelope — an `event` discriminator and an `data` payload object
(shape varies by event):

```json
{
  "event": "<event_type>",
  "data": { ... }
}
```

Common `event` values consumed by the admin UI:
- `connected` — sent once on successful auth; `data` carries `{"project_id"}`
- `deploy_state` — initial hydration snapshot of current deploy state
- `agent_log` — a line of agent/task output
- `task_updated` — a task's status or fields changed
- `artifact_created` — a new artifact was produced
- `autonomous_activity` / `autonomous_paused` — autonomous-mode lifecycle events
- `pipeline_throttle` — pipeline throttling notice
- `pong` — keep-alive reply (the client may send `{"type": "ping"}`)

Additional `event` types are emitted for pipeline, release, and self-review
telemetry (e.g. `pr_merged`, `failure_alert`, `cycle_summary`,
`review_feedback`); clients should ignore unrecognized `event` values. The
broadcast envelope is built in `backend/app/broadcast.py`.

---

## Data Models

See [`docs/DATA_MODELS.md`](DATA_MODELS.md) for detailed schema of request/response objects.
