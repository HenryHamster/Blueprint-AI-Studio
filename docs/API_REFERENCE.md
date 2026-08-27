# Blueprint API Reference

This document describes the public HTTP surface exposed by the Blueprint orchestrator.
Routes are grouped by router family; the implementation lives in `backend/app/main.py`
and the router modules under `backend/app/routers/`.

The API surface is split into a few families:

- Public project/task launch and status routes under `/api/*`
- Agent lifecycle, warm handoff, GitHub proxy, and artifact upload routes under `/api/agent/*`
- Self-hosted runner polling and compute-job endpoints under `/api/runner/*`
- Admin settings, diagnostics, analytics, onboarding, and project-configuration routes under `/api/admin/*`
- GitHub OAuth and webhook routes under `/api/github/*`

## Authentication

- **Admin UI & User Endpoints**: Bearer token from Firebase Auth in `Authorization: Bearer <firebase_token>` header
- **Agent VM Endpoints**: Custom JWT in `Authorization: Bearer <agent_jwt>` header, scoped to the task ID. The default TTL is **1 hour** (`AGENT_TOKEN_TTL_HOURS`); the agent refreshes proactively at ~45 min via `POST /api/agent/refresh-token`. Cold-start agents that have not yet proven they can refresh receive a longer **8-hour bootstrap** token (`AGENT_BOOTSTRAP_TOKEN_TTL_HOURS`) that is rotated back down to the 1-hour TTL on the first successful refresh.
- **Server-to-Server**: the `ADMIN_API_KEY` value is sent as a bearer token in the `Authorization: Bearer <ADMIN_API_KEY>` header (the same header as the Firebase token). A request whose bearer token matches `ADMIN_API_KEY` is granted admin access (`get_current_user` in `backend/app/auth.py`).
- **Read-only metric endpoints**: a small set of non-mutating operator-metric endpoints (see table below) additionally accept a valid agent JWT as a `Authorization: Bearer <agent_jwt>` credential via the `get_metrics_reader` dependency. This grants **read-only** access to those specific endpoints only — it does not grant admin or mutating access. Research/measurement VMs that have `ORCHESTRATOR_URL` and `AGENT_SECRET_TOKEN` but no `ADMIN_API_KEY` can use this path.

| Endpoint | Auth accepted |
|----------|--------------|
| `GET /api/admin/digest-latency-stats` | `ADMIN_API_KEY` **or** valid agent JWT |

---

## System Endpoints

These routes are defined directly in `backend/app/main.py` rather than in a router module.

### `GET /`

Unauthenticated liveness ping. Returns `{ "status": "online", "service": "Blueprint Orchestrator" }`.

### `GET /health`

Unauthenticated health check for external monitoring. The response includes the current revision and whether the instance is the elected leader.

### `GET /metrics`

Prometheus text metrics (text/plain exposition format). Returns `403 Forbidden` if `METRICS_TOKEN` is configured but the request doesn't include a valid `?token=<value>` query parameter. When `METRICS_TOKEN` is unset, the endpoint also returns `403` (fail-closed security design). Exposes orchestrator cycle age, active VM counts, consecutive autonomous-failure counts, and related gauges.

### `GET /api/version`

Public compatibility check used by the frontend. Returns the git SHA and Alembic revision.

### `GET /api/me`

Authenticated user profile lookup. Returns the caller's `user_id`, `email`, and `role`.

### `GET /api/health/detailed`

Authenticated diagnostic snapshot with database latency, provider health, external dependency checks, and pipeline-health state.

### `GET /api/debug/orchestrator`

Authenticated debug endpoint that reports whether the orchestrator loop is alive and how many tasks are queued or active.

### `GET /api/admin/viewport-failures/download-url`

Authenticated helper used by the admin UI to fetch a signed download URL for a viewport-failure screenshot.

---

## Projects API

### `POST /api/deploy`

Launch a new task on a repository.

**Request body:**
```json
{
  "name": "string",                    // Project name
  "payloads": [{"key": "value"}],      // Task-specific parameters (see below)
  "github_owner": "string | null",     // GitHub repo owner (optional)
  "github_repo": "string | null"       // GitHub repo name (optional)
}
```

Each entry in `payloads` must include a `type` field (e.g. `"create"`, `"fix"`, `"refactor"`, `"qa"`, `"migrate"`, `"deps"`, `"docs"`) and type-specific fields such as `prompt`.

**`acceptance_evidence` (required for code-producing task types)**

For `type` values `create`, `fix`, `refactor`, `qa`, `migrate`, `deps`, and `docs`, each payload entry **must** include an `acceptance_evidence` object. Payloads that omit it are rejected with `422`. The object has a `kind` discriminator and kind-specific fields:

| `kind` | Required additional fields | Description |
|--------|---------------------------|-------------|
| `named_test` | `test_path` (path to a real test file, optionally with an inline node id as `path::test_fn`) | A test that exercises the changed seam end-to-end. The node id may be given inline in `test_path` (`backend/testing/test_auth.py::test_rate_limiter_locks_account`) or separately in `test_id`; only the path part is checked against the test-file naming rules. |
| `observable_behavior` | `description` (≥10 non-trivial characters) | A concise human-readable description of a behavior that should be exercised. |
| `downstream_artifact` | `artifact_id` (non-trivial string) | An artifact ID a later task consumes. |
| `visual` | `component` (non-trivial string), `viewport_width` (int 1–4096) | Before/after viewport screenshot pair proving a frontend change. `assertion_type` is optional. |
| `coverage_delta` | `threshold` (float 0–100) | Minimum coverage percentage the merged PR must achieve. |

Trivial placeholders (`"test"`, `"pass"`, `"n/a"`, `"assert True"`, etc.) are rejected at validation time.

**Example payload entry with `acceptance_evidence`:**
```json
{
  "type": "create",
  "prompt": "Add a login rate-limiter that locks an account after 5 failed attempts.",
  "acceptance_evidence": {
    "kind": "named_test",
    "test_path": "backend/testing/test_auth.py::test_rate_limiter_locks_account"
  }
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
- `422` — Invalid task payload (failed `validate_task_payload`), including missing or malformed `acceptance_evidence` on code-producing task types
- `401` — Unauthorized

---

## Admin Onboarding and Project Configuration

The admin onboarding and project-config routers live under `backend/app/routers/admin_onboard.py` and `backend/app/routers/admin_project_config.py`.

### `POST /api/admin/projects/onboard`

Create a project for a GitHub repository, auto-detect build config, and seed branch defaults.

**Request body:** `OnboardPayload`

Key fields:
- `github_owner`, `github_repo` are required.
- `name` defaults to `{owner}/{repo}`.
- `build_config_override` is merged on top of auto-detected values.
- `promotion_target` overrides the default promotion branch.

**Response:** `{ "project_id", "build_config", "default_branch" }`

### `GET|PATCH /api/admin/projects/{project_id}/integration-branch`

Read or update the autonomous-spawn trunk for the project. `PATCH` validates the branch on the remote and returns `422` if it does not exist.

### `GET|PATCH /api/admin/projects/{project_id}/promotion-target`

Read or update the promotion/LTS target branch.

### `GET|PATCH /api/admin/projects/{project_id}/task-branch-namespace`

Read or update the leading branch namespace used for agent task branches.

### `GET|PATCH /api/admin/projects/{project_id}/task-branch-prefix`

Read or update the per-project task branch prefix.

### `GET|PATCH /api/admin/projects/{project_id}/staging-branch-prefixes`

Read or update the branch prefixes that route into staging/rollup flows.

### `GET|PATCH /api/admin/projects/{project_id}/full-suite-drift-sentinel`

Read or update the standing full-suite CI drift sentinel (canonical and ON by default, stored in `build_config`).
Each reaper sweep dispatches ci.yml on the integration branch for every open version and alerts + spawns
harvest remediation when a concluded run is red. PATCH body: `{ "full_suite_drift_sentinel_enabled": true|false }`.
Pass `false` to explicitly disable the sentinel (temporary rollback path); the key's absence is the canonical
default (enabled). Response includes `full_suite_drift_sentinel_enabled` and `is_default`.

### `GET|PATCH /api/admin/projects/{project_id}/approval-gate-config`

Read or update the file-path approval gate config. The stored value is a `{ "infra_files": [...], "critical_files": [...] }` object.

### `GET|PATCH /api/admin/projects/{project_id}/auto-remediation`
### `GET|PATCH /api/admin/projects/{project_id}/issue-implementation`
### `GET|PATCH /api/admin/projects/{project_id}/auto-review`

Read or update the per-project automation pipelines seeded from the design doc.

### `PATCH /api/admin/projects/{project_id}/autonomous`

Toggle the project's `autonomous_enabled` flag.

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

Cancel a project and its active work. Marks the project `CANCELLED`, cancels
project tasks in `QUEUED`, `PROVISIONING`, `IN_PROGRESS`, `EVALUATING`, or
`PARKED`, and deletes VMs only for cancelled tasks that have a
`vm_instance_id`. This is a project-wide owner action.

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

### `GET /api/agent/task-capabilities`

Return the task capability contract used by agents and operator surfaces. The
agent uses each entry's `agent_mode` to derive read-only vs. write permissions
at startup; `agent/src/taskCapabilities.ts` contains the bespoke dispatch list.

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

The `payload` object is validated by `validate_task_payload`. For code-producing `child_task_type` values (`create`, `fix`, `refactor`, `qa`, `migrate`, `deps`, `docs`), the payload **must** include an `acceptance_evidence` object. See the [acceptance_evidence reference](#acceptance_evidence-required-for-code-producing-task-types) under `POST /api/deploy` for the full `kind` vocabulary and field requirements. Omitting `acceptance_evidence` on a code-producing spawn returns `422`.

**Response:** `{ "child_task_id": "uuid" }`

---

### `POST /api/agent/refresh-token`

Issue a fresh JWT carrying the standard 1-hour TTL (`AGENT_TOKEN_TTL_HOURS`).
A successful refresh is also the downgrade point for bootstrap tokens: an agent
holding the wider 8-hour bootstrap grant is rotated back to the 1-hour TTL here,
since calling this endpoint proves it is refresh-capable.

**Request body:**
```json
{
  "task_id": "uuid"
}
```

**Response:** `{ "token": "new.jwt.token", "token_expires_at": "ISO8601" }`

---

### `POST /api/agent/credentials/rotate`

Propagate a refreshed provider secret for the current task/account.

**Request body:**
```json
{ "secret": "string" }
```

**Response:**
```json
{ "rotated": true }
```

When the current provider/auth mode does not support agent-side rotation, the endpoint returns `{ "rotated": false, "reason": "..." }`.

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

### `GET /api/agent/artifacts/download-url`

Get a signed GCS URL for an existing artifact. The response model is
`ArtifactDownloadResponse`.

---

### `POST /api/agent/viewport/failure-screenshot-upload-url`

Get a signed upload URL for viewport-failure screenshots.

---

### `POST /api/agent/compute-jobs`

Create a compute job for the current task.

### `GET /api/agent/compute-jobs/{job_id}`

Fetch the current status and result metadata for an agent-launched compute job.

### `POST /api/agent/vm-heartbeat`

VM-scoped liveness for warm-VM continuity.

---

### `POST /api/agent/next-role`

Warm-VM handoff endpoint that either claims a pinned task or exits with no work.

### `GET /api/agent/projects/{project_id}/directions/{direction_id}`

Fetch a single direction for agent-side strategic work.

### `GET /api/agent/projects/{project_id}/playbook/{key:path}`

Read a project playbook entry.

### `GET /api/agent/projects/{project_id}/gaps/{gap_id}`

Fetch a specific gap entry for the agent research workflow.

---

### Agent Operational Endpoints

These four endpoints (`backend/app/routers/admin_agent_ops.py`) let an agent
persist operational state to its project during a task. All require the agent
JWT, and all reject the request with `403 Task ID mismatch` unless the
`task_id` in the body matches the token's `sub` claim.

#### `POST /api/agent/design-doc`

Read or mutate a key in the project's design-doc (a JSONB document keyed by
page, e.g. `systems/sys-vm-lifecycle`). Rate limit: **60/minute**.

Read ops (`list`, `get`) are open to all agent types; write ops (`set`,
`delete`) are restricted to `design_doc_update` and `strategic_review` agents
(`403` otherwise).

**Request body:**
```json
{
  "task_id": "uuid",
  "op": "list | get | set | delete",
  "key": "string | null",
  "value": "any | null"
}
```

- Keys are max 100 chars and may not start with `_` (reserved for internal
  metadata such as `_meta` and `_file_to_system`).
- `set` enforces a 50 KB per-value cap and a 500 KB total-document cap (both
  return `400` on overflow). Setting or deleting a `systems/*` key atomically
  rebuilds the `_file_to_system` reverse index.

**Responses:**
- `list` → `{ "keys": ["systems/...", ...] }` (internal `_`-prefixed keys hidden)
- `get` → `{ "key": "...", "value": ... }` (`404` if the key is absent)
- `set`/`delete` → `{ "status": "ok", "op": "...", "key": "...", "revision": <int> }`

#### `POST /api/agent/playbook`

Read or write the shared operational playbook (persisted as `playbook/*` keys
in the same design-doc JSONB). Any agent task type may call this. Rate limit:
**120/minute**. `delete` is rejected with `403`.

**Request body:**
```json
{
  "task_id": "uuid",
  "op": "get | set | list",
  "key": "string | null",
  "value": "string | null",
  "applicable_types": ["create", "fix", "..."]
}
```

- `key` must match `^[a-zA-Z0-9][a-zA-Z0-9\-]{0,49}$` (alphanumeric + hyphens,
  max 50 chars, no `_` prefix).
- `value` is plain text, max 2 KB.
- `applicable_types`, when set, must all be valid task types; the entry then
  only surfaces for those task types.
- Anti-poisoning: until the writing task reaches `COMPLETED`, its entry carries
  a `pending_source` marker and is invisible to other tasks. The total playbook
  is capacity-bounded; least-useful entries are auto-archived to make room.

**Responses:**
- `list` → `{ "status": "ok", "op": "list", "keys": [...] }`
- `get` → `{ "status": "ok", "op": "get", "entries": { "<key>": { "content", "updated_at", "source_task_id" } } }`
- `set` → `{ "status": "ok", "op": "set", "key": "...", "revision": <int> }`

#### `POST /api/agent/annotate`

Leave a comment, issue, vision suggestion, or size-breach signal. Stored as a
`ProjectMessage` so strategic/design-doc agents pick it up. Rate limit:
**30/minute**. Capped at **10 annotations per task** (`429` beyond that).

**Request body:**
```json
{
  "task_id": "uuid",
  "annotation_type": "comment | issue | vision | size_breach",
  "content": "string",
  "target_page": "string | null",
  "severity": "low | medium | high | null",
  "file_path": "string | null",
  "current_size": 0,
  "change_summary": "string | null"
}
```

- `comment`/`issue`/`vision` require non-empty `content` (max 2000 chars);
  `severity` (issues) must be `low`/`medium`/`high`; `target_page` (comments)
  max 100 chars.
- `size_breach` is a programmatic signal from a size-guardrail test failure and
  instead requires `file_path`, a positive `current_size`, and a
  `change_summary` (max 500 chars). The server computes a policy-driven
  `new_limit` and returns it for the agent to apply to `module_size_limits.json`.

**Response:** `{ "status": "ok", "annotation_id": "uuid", "new_limit": <int>? }`
(`new_limit` only present for `size_breach`).

#### `POST /api/agent/strategic`

Incremental strategic-review operations against the project (vision, directions,
problem-model, messages, task spawning, and more). Rate limit: **60/minute**.
The data payload is capped at 100 KB.

Message ops (`create_message`, `resolve_message`) are available to all agent
types; every other op is restricted to `strategic_review` agents (`403`
otherwise).

**Request body:**
```json
{
  "task_id": "uuid",
  "op": "set_vision | create_direction | update_direction | retire_direction | stage_direction | approve_direction | reject_direction | activate_direction | resolve_discovery | create_message | resolve_message | spawn_task | cancel_task | watch_task | ack_watch | cancel_watch | dispose_completion_debt | consume_knowledge_followup | dismiss_knowledge_followup | set_problem_model | patch_problem_model | replace_problem_model | set_running_update | reflect | pending_confirm | resolve_pending_confirm | set_retirement_critiques | set_evidence_progress | set_acceptance_criteria | record_finding_resolution | set_memory | set_session_summary | report_autonomous_finding | consume_knowledge | dismiss_knowledge | request_qualification | force_promote | approve_activation | requeue_task | reset_iteration_cap | set_task_priority | unhold_direction",
  "data": { }
}
```

The `data` shape and response vary per op (each returns `{ "status": "ok", "op": "...", ... }`
with op-specific fields, and may include a `warnings` array when input was
truncated). Notable behaviors:

- `create_direction` requires `text`, `success_evidence`, and
  `retirement_condition`; returns `409` if the active-direction cap
  (`max_active_directions`, default 5) is reached.
- `resolve_discovery` resolves the exact `cycle_id` and `cycle_revision` named
  by a discovery operator event for `direction_id`. `action=continue` requires
  a complete `next_bet`; `action=park` pauses pending stronger evidence and
  `action=close` stops the direction, with both omitting `next_bet`. Stale or
  conflicting resolutions are rejected while an identical replay is idempotent.
- `spawn_task` durably dispatches `research`, `web_research`, `design`, `plan`,
  `compute_experiment`, `create`, `refactor`, or `qa` directly. `fix` is also
  accepted when `pr_number` and `branch` are supplied. Direction association is
  optional. The request is idempotent and automatically watches children unless
  `watch: false` or the work is direction-linked research. That research enters a bounded
  discovery cycle instead, so its cheap review filters routine completion before
  Resident attention; callers may set `watch: true` for an exceptional raw-result
  interrupt. The endpoint returns HTTP 202 with a durable deferred receipt only when
  the project-wide abuse circuit breaker is reached.
- `cancel_task` requires a reason plus exactly one of `target_task_id` or
  `request_id`. A task can be cancelled only in its early lifecycle; a durable
  spawn request can be cancelled only while deferred, which prevents later
  auto-drain from creating the task.
- `watch_task` registers a durable, one-shot terminal-state notification for a
  task in the same project. Its strict `data` object requires `target_task_id`
  (UUID), and optionally accepts `purpose` (up to 1000 characters) and
  `deadline_at` (ISO-8601 datetime). Repeating the same owner/target pair
  replays the existing watch.
- `ack_watch` requires `watch_id` plus `disposition` (`reported`, `acted`,
  `no_action`, or `superseded`), with an optional `note`; only a fired or
  expired watch can be acknowledged. `cancel_watch` requires only `watch_id`.
  Both operations are same-project and idempotent.
- `reject_direction` tombstones a `proposed` direction and cancels any QUEUED
  tasks tied to it.
- `patch_problem_model` is the normal safe write. It requires
  `{base_revision, patch}` and preserves omitted fields. It accepts optional
  `transient_actions` and `dry_run`; stale revisions return HTTP 409.
- `replace_problem_model` is reserved for bootstrap/recovery and requires
  `{base_revision, model, confirm_replace: true}`. Use `dry_run: true` to
  preview its diff first.
- `set_problem_model` remains a compatibility alias and now uses safe patch
  semantics. It lifts out transient `version_phase_decision` /
  `qualify_request` siblings before validating the durable problem model.
- `record_finding_resolution` writes a per-finding re-qualification resolution
  ledger row for the current open release version. Required `data` fields:
  `finding_key` (string, ≤ 64 chars), `state` (`resolved` | `stale`). Optional:
  `severity`, `note`, `resolving_ref`, `verified_against_sha`. Returns `409`
  when no open version exists.
- `set_memory` records a durable behavioral preference or strategic insight that
  persists across session restarts. Required `data` fields: `key` (string, ≤ 200
  chars) and `content` (string, ≤ 2000 chars). Entries are capped at 50; oldest
  are evicted first.

Pipeline **action** verbs. Each re-enters the same server-side implementation
its operator HTTP lever calls, so behavior cannot drift between the two. All
require a non-empty `reason`, and all report the REASON an action did not happen
rather than asserting success.

- `force_promote` promotes the open release version while **skipping the release
  audit/qualification gate — and only that gate**. It does **NOT** skip CI: the
  release branch is still cut, the `release-vN` → promotion-target PR is still
  opened, and the promotion target's required branch-protection checks must still
  go green before anything merges. Optional `candidate_sha` (7–40 hex, resolved
  server-side) defaults to the current integration-branch HEAD. Two-phase by
  design — the first call cuts the PR and returns a waiting status (commonly
  `promotion_check_wait`); call again once CI is green to land it. Pre-promote
  no-op guards (`no_open_version`, `no_credentials`, `ci_not_green`,
  `promotion_check_wait`, …) come back as `promoted: false` with the raw
  `promotion_status`, not as an HTTP error.
- `requeue_task` re-drives one same-project task back to `QUEUED`. This is also
  the **retry** verb. Requires `target_task_id`; optional `guidance` is the
  one-shot retry instruction appended to the requeued prompt (defaults to
  `reason`). Retryable source statuses are `FAILED`, `CANCELLED`, `BLOCKED`,
  `EVALUATING`, `PARKED`; anything else returns `409` naming the current status.
  Does **NOT** re-run a failed release qualification (use `request_qualification`)
  and does **NOT** unpark a review-fix loop (use `reset_iteration_cap`).
- `reset_iteration_cap` is the **unpark** verb for a review-fix chain: a chain
  parked for operator disposition is released by exactly this operation. It
  archives the FAILED review/fix tasks on the PR, zeroes the per-chain iteration
  counter, stamps the reset marker that also releases the merge gate's
  exact-target respawn budget, and clears the abandon-candidate marker. It does
  **NOT** re-spawn anything (`respawned: false`) and never touches the PR.
  Returns `400` when the task carries no `review_loop.pr_number` /
  `rollup_pr_number`, `409` while a failed review/fix task still has a pending
  post-commit continuation.
- `set_task_priority` reprioritises one same-project task by rewriting
  `Task.priority`, the integer the dispatch loop orders by. `priority` accepts
  `high` | `medium` | `low` (100 | 50 | 0) or an explicit integer 0–100. It does
  **NOT** dispatch the task, raise a concurrency cap, or clear a dependency or
  landing gate (`dispatched: false`) — priority only decides order among rows
  that are already eligible. To reprioritise a *direction*, use
  `update_direction` with `priority`.
- `unhold_direction` clears a direction's consecutive-failure hold so it
  dispatches work again; idempotent, returning `was_held: false` when the
  direction was already healthy. It does **NOT** change the direction's status,
  reset saturation, re-activate a retired or staged direction (use
  `activate_direction`), or spawn anything (`spawned: false`).

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
- `GET /api/agent/projects/{project_id}/design-doc/{key:path}` — a single
  design-doc entry by key.
- `GET /api/agent/projects/{project_id}/problem_model` and
  `GET /api/agent/projects/{project_id}/problem-model` — the project's problem
  model; both spellings are supported.
- `GET /api/agent/projects/{project_id}/messages/{ref}` — fetch one project
  message by reference.
- `GET /api/agent/projects/{project_id}/qualification` — current qualification
  context for strategic/release work.
- `GET /api/agent/projects/{project_id}/gaps/{gap_id}` — a single problem-model
  gap (active or archived) with full history/past_moves/evidence.

### Agent knowledge and build-config helpers

- `GET /api/agent/knowledge-documents` and
  `GET /api/agent/knowledge-documents/{document_id}` — retrieve knowledge
  documents available to the current task.
- `PATCH /api/agent/projects/{project_id}/build-config/commands` — update
  discovered build/test command metadata for a project.
- `POST /api/agent/build-config-discovery` — submit build-config discovery
  observations from a bootstrap or fix-loop run.

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

## Runner API

Self-hosted runners use these endpoints to pull tasks, report heartbeats, and execute compute jobs. Authentication uses the runner's bearer token (`Authorization: Bearer <runner_token>`), resolved via `get_current_runner` in `backend/app/auth.py`. All routes are under `/api/runner`.

### `POST /api/runner/next-task`

Pull the oldest task pinned to this runner (status `PROVISIONING`, `vm_instance_id = 'runner:<id>'`). Returns `204 No Content` when no task is waiting.

Uses `SELECT ... FOR UPDATE SKIP LOCKED` to prevent concurrent polls from double-claiming. On a hit, `started_at` is stamped to guard against dispatch storms and a task-scoped agent JWT is minted. The task remains in `PROVISIONING` — the agent binary calls `POST /api/agent/claim` to perform the `PROVISIONING → IN_PROGRESS` transition (same contract as GCP VMs).

If the LLM account pool is exhausted, returns `503 Service Unavailable` with `{"pool_exhausted": true}` and releases the dispatch marker so the task stays claimable.

**Response (200):**
```json
{
  "task_id": "uuid",
  "agent_token": "jwt-string",
  "vm_instance_id": "runner:<runner-id>",
  "payload": [...],
  "task_type": "create",
  "capability_slice": [...],
  "credentials": {
    "env": {"PROVIDER": "...", "AUTH_MODE": "..."},
    "files": [{"path": "...", "content": "...", "mode": "..."}]
  }
}
```

`credentials` is `null` when no pool account is assigned (runner falls back to host environment variables).

**Status codes:** `200` task returned, `204` no task, `429` rate limited, `503` pool exhausted.

---

### `POST /api/runner/decline-task`

Release a previously-dispatched task back to `QUEUED`. Runners call this when the isolation backend cannot sandbox the task type (e.g., no bwrap support for write tasks on a read-only docker runner). Without this, the task strands in `PROVISIONING` until the stale-runner sweep fires.

**Request body:**
```json
{"task_id": "uuid"}
```

**Response:** `{"ok": true}`

Idempotent — returns `ok` even when the task is not found or is no longer `PROVISIONING`.

---

### `POST /api/runner/heartbeat`

Refresh the runner's `last_seen_at` and report its current load. Re-arms an inactive runner (status is set back to `active` on every authenticated heartbeat).

**Request body:**
```json
{
  "active_task_ids": ["uuid", ...],
  "active_compute_job_ids": ["uuid", ...],
  "agent_available": true,
  "supported_agent_task_types": ["create", "review", ...],
  "hostname": "my-machine",
  "version": "1.2.3",
  "isolation_backends": {"bwrap_ok": true, "docker_usable": true},
  "python_runtime_usable": true,
  "python_runtime": {"usable": true, "reason": null, "interpreter": "/opt/blueprint/compute-runtime/venv/bin/python", "python_version": "3.11.9", "pip_version": "24.0", "provisioner": "venv", "probed_at": "2026-08-24T12:00:00Z"},
  "agent_backend": "bwrap",
  "compute_backend": "docker",
  "has_gpu": false,
  "gpu_count": 0,
  "gpu_names": [],
  "last_successful_job_at": "2026-07-10T12:00:00Z",
  "consecutive_job_failures": 0,
  "disk_free_bytes": 10737418240,
  "cpu_load_avg": 0.5,
  "memory_free_bytes": 4294967296,
  "gpu_utilization_pct": 0
}
```

All fields except `active_task_ids` and `active_compute_job_ids` are optional — older runners omit them and continue heartbeating without change.

`supported_agent_task_types` is a runner-declared capability floor. The orchestrator intersects it with the operator-configured `task_types` ceiling when dispatching. This field is distinct from operator config and never overwrites `task_types`.

`python_runtime` reports whether this host can give an **imageless (bwrap) compute job** a Python interpreter with a working pip, and is persisted into `capabilities` only from genuine daemon heartbeats (`active_work_report: true`). A `--check` preflight probes without provisioning, so its verdict is not authoritative and neither writes nor clears the stored report. String fields are trimmed to 300 characters.

Placement reads the NESTED report, not the flat `python_runtime_usable` bool: the runner derives that bool as `bool((report or {}).get("usable"))`, so a runner whose boot-time provisioner has not finished yet sends `false` while being perfectly capable. A runner is dropped from imageless `compute_experiment` candidates **only** when the nested report proves it unfit (`"usable": false`); absent or unreadable evidence means unknown and does not exclude.

**Response:**
```json
{
  "ok": true,
  "last_seen_at": "2026-07-10T12:00:00.000000",
  "enrolled_max_concurrent": 2,
  "version_hint": "git-sha"
}
```

**Status codes:** `200` updated, `429` rate limited.

---

### `POST /api/runner/update-report`

Report the outcome of a runner self-update. Creates an INFO alert on success and a WARNING alert on failure, broadcasting it to the operator console immediately.

**Request body:**
```json
{
  "status": "success",
  "old_version": "1.0.0",
  "new_version": "1.1.0"
}
```

`status` must be `"success"` or `"failure"`. Version fields are optional.

**Response:** `{"ok": true}`

---

### `POST /api/runner/next-compute-job`

Pull the oldest `STARTING` runner-subprocess compute job pinned to this runner. Returns `204 No Content` when none is available. Atomically transitions the job `STARTING → RUNNING` on a hit.

Scoped to the runner's project and the specific runner pin set by the orchestrator (prevents GPU experiments from being claimed by non-GPU runners).

**Response (200):**
```json
{
  "job_id": "uuid",
  "command": "python3 train.py",
  "env": {"KEY": "value"},
  "cwd": "/workspace",
  "setup_step": null,
  "container_image": null,
  "container_registry_auth": null,
  "runtime_deadline_seconds": 3600,
  "cancel_requested": false,
  "repo_clone": {"owner": "org", "repo": "name", "ref": "main"},
  "repo_token": "ghp_..."
}
```

`repo_token` is a freshly-minted GitHub installation token (not stored in the DB); it is present only when `repo_clone` is set.

**Status codes:** `200` job returned, `204` no job, `429` rate limited.

---

### `POST /api/runner/compute-job/{job_id}/report`

Report execution status for a claimed compute job. Stamps `exec_status`, `exit_code`, `result_metrics`, `command_log`, and `terminal_reason` into `polling_metadata` for the monitor's `poll_status`/finalize path. Does **not** finalize the job — the monitor owns that transition so artifact-linking and delivery run on the normal path.

Idempotent: if the job is already in a terminal state (`SUCCEEDED`, `FAILED`, `CANCELLED`, `TIMED_OUT`), the update is skipped but `cancel_requested: true` is returned so the runner can observe the signal.

**Request body:**
```json
{
  "exec_status": "running",
  "exit_code": null,
  "result_metrics": {"loss": 0.42, "accuracy": 0.95},
  "command_log": {"text": "Epoch 1/10..."},
  "terminal_reason": null
}
```

`exec_status` must be `"running"`, `"succeeded"`, or `"failed"`. All fields except `exec_status` are optional. `command_log.text` is truncated server-side to 64 KiB. `result_metrics` accepts numeric values; `"arms"` key may be a dict or non-empty list; strings and non-finite floats are dropped.

`terminal_reason` is the runner's structured cause for a failure the **command** did not cause — a host-capability gap, a runner-side deadline, a watchdog kill (for example `"runner provisioning failure: no usable python runtime on this host"`, sent alongside the reserved exit code `123`). Accepted only on a terminal report, trimmed to 500 characters, stamped as `polling_metadata["runner_terminal_reason"]`, and preferred by `poll_status` over the untyped `"compute job failed"` default so `classify_compute_terminal_reason` bills the fault to the platform (`PROVISION_FAILURE`, budget-exempt) instead of to the experiment's author (`CODE_ERROR`).

**Response:** `{"ok": true, "cancel_requested": false}`

**Status codes:** `200` stamped, `404` job not found or wrong runner, `429` rate limited.

---

### `POST /api/runner/compute-job/{job_id}/artifact`

Upload a durable artifact tarball for a claimed compute job. The body must be `multipart/form-data` with:
- `archive` (file field) — a `.tar.gz` archive (≤ 25 MiB)
- `file_count` (form field) — integer count of files in the archive
- `total_bytes` (form field) — uncompressed size in bytes

The server stores the archive via `compute_backends.store_runner_artifact` and links the descriptor into `polling_metadata['runner_artifact']` for the monitor/finalize path. Scoped to the pinned runner only — runner A cannot upload to a job claimed by runner B.

**Response:** `{"ok": true, "runner_artifact": {...}}`

**Status codes:** `200` stored, `400` missing `archive` field, `404` job not found, `413` exceeds 25 MiB, `429` rate limited.

---

## Admin API

Access these via the admin UI (requires `ADMIN_API_KEY` or Firebase admin token).

### `/api/admin/*`

The admin surface is split across `admin.py` and `backend/app/routers/admin_*.py`
(plus a handful of feature modules — `viewport_debt.py`, `sr_diagnostics_endpoints.py`,
`anomaly_detection.py`, `efficiency_snapshot.py`, `residual_case_endpoints.py` — whose
routers are mounted under the same `/api/admin` prefix). Every route below requires a
valid Firebase admin token (or `ADMIN_API_KEY`); project-scoped routes additionally
enforce a per-project role (`member` for reads, `owner` for mutations) via
`@require_project_access` / `@require_role`. The tables below enumerate the full surface,
grouped by domain. Deep-dives for onboarding and branch/build config follow.

#### Projects & Lifecycle (`admin_projects.py`)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/admin/projects` | List the caller's projects with lightweight columns and redacted config. |
| GET | `/api/admin/projects/{project_id}` | Return the full project record including all JSONB detail columns. |
| PATCH | `/api/admin/projects/{project_id}` | Apply operator updates while preserving project-owned branch metadata. |
| DELETE | `/api/admin/projects/{project_id}` | Delete a project and cascade to tasks/artifacts/history. |
| POST | `/api/admin/projects/{project_id}/detect-build-config` | Re-run repo detection, preserving operator-owned overrides. |
| POST | `/api/admin/projects/{project_id}/running-update/acknowledge` | Acknowledge the project's `running_update` read-marker. |
| GET | `/api/admin/projects/{project_id}/directions/task-counts` | Return per-direction task counts for the project. |
| PATCH | `/api/admin/projects/{project_id}/autonomous` | Toggle the project's `autonomous_enabled` flag. |
| POST | `/api/admin/autonomous/pause-all` | Disable autonomous mode on all projects at once. |
| GET | `/api/admin/autonomous-status` | Return autonomous-mode status across projects. |

#### Directions & Strategic Cycle (`directions.py`)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/admin/projects/{project_id}/directions` | List the project's strategic directions. |
| POST | `/api/admin/projects/{project_id}/directions` | Append a new direction. |
| PUT | `/api/admin/projects/{project_id}/directions` | Replace the entire directions array for a project. |
| PATCH | `/api/admin/projects/{project_id}/directions/{direction_id}` | Update a direction's fields. |
| DELETE | `/api/admin/projects/{project_id}/directions/{direction_id}` | Remove a direction. |
| PATCH | `/api/admin/projects/{project_id}/directions/reorder` | Reorder the project's directions. |
| POST | `/api/admin/projects/{project_id}/directions/{direction_id}/research` | Spawn a research task for a direction. |
| POST | `/api/admin/projects/{project_id}/directions/{direction_id}/unhold` | Clear a direction's consecutive-failure hold so new tasks dispatch again. |
| POST | `/api/admin/projects/{project_id}/strategic-cycle` | Manually trigger a strategic cycle. |
| GET | `/api/admin/projects/{project_id}/messages` | List messages for a project. |
| POST | `/api/admin/projects/{project_id}/messages` | Create a message from the user to the autonomous system. |

#### Strategic Memory Repair (`admin_strategic_memory.py`)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/admin/projects/{project_id}/memory/repairs/fast-claim-redactions` | Owner-only, two-phase repair for receipts falsely redacted by the Fast claim-metadata regression. `mode=dry_run` returns only an exact candidate count and digest; `mode=apply` additionally requires that reviewed count/digest, an idempotent operation ID, the buggy and currently served revisions, and the identical timestamp window. |

Operators should use `blueprint ops repair-fast-claim-redactions dry-run|apply`
instead of constructing this request directly. The CLI requires timezone-aware
window bounds and makes `--yes` mandatory for apply; the endpoint re-locks and
recomputes the candidate set before restoring any receipt.

#### Project Automation & Config (`admin_project_config.py`)

See **[Per-Project Branch & Build Config](#per-project-branch--build-config)** below for the
branch-routing endpoints. The remaining config endpoints:

| Method | Path | Description |
|--------|------|-------------|
| GET / PATCH | `/api/admin/projects/{project_id}/auto-remediation` | Read / update the auto-remediation pipeline config (partial update). |
| GET / PATCH | `/api/admin/projects/{project_id}/issue-implementation` | Read / update the issue-implementation pipeline config. |
| GET / PATCH | `/api/admin/projects/{project_id}/auto-review` | Read / update the auto-review pipeline config. |
| GET / PATCH | `/api/admin/projects/{project_id}/broadcast-limit` | Read / set the per-project broadcast limit (null = global default). |
| PATCH | `/api/admin/projects/{project_id}/playbook/{key:path}` | Apply an operation to a playbook entry: promote, demote, or archive. |
| POST | `/api/admin/projects/{project_id}/backfill-branches` | Re-run the `integration_branch` / `promotion_target` backfill for one project. |
| GET | `/api/admin/cost-summary` | Estimated VM cost summary by provider over a rolling window (`days`). |

#### Users & Membership (`admin_users.py`)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/admin/projects/{project_id}/members` | List members of a project (any member/viewer). |
| POST | `/api/admin/projects/{project_id}/members` | Add a member to a project (owner-only). |
| PATCH | `/api/admin/projects/{project_id}/members/{member_id}` | Update a member's role (owner-only; cannot change owner's own role). |
| DELETE | `/api/admin/projects/{project_id}/members/{member_id}` | Remove a member (owner-only, except self-removal). |
| GET | `/api/admin/users` | List platform users. |
| POST | `/api/admin/users` | Create a platform user. |
| PATCH | `/api/admin/users/{user_id}` | Update a platform user. |
| DELETE | `/api/admin/users/{user_id}` | Remove a platform user. |

#### Settings & Providers (`admin_settings.py`)

| Method | Path | Description |
|--------|------|-------------|
| GET / PATCH | `/api/admin/settings` | Read / update global runtime settings. |
| GET | `/api/admin/providers` | Return the available LLM provider catalog. |
| GET | `/api/admin/github/repos` | List repositories accessible via the user's GitHub App installation. |
| GET | `/api/admin/github/branches` | List branches for a repo accessible via the GitHub App installation. |
| POST | `/api/admin/restart-orchestrator` | Restart the orchestrator background task without restarting the service. |
| POST | `/api/admin/github-quota/refresh` | Force an immediate probe of GitHub's `/rate_limit` endpoint. |
| POST | `/api/admin/github-quota/reset-tracking` | Clear the in-memory GitHub quota tracking state. |
| POST | `/api/admin/repair-state` | Post-rollback state audit and repair (stuck tasks, orphaned parents, stale evaluating). |
| POST | `/api/admin/memory/tracemalloc/start` | Enable tracemalloc to diagnose Cloud Run OOM. |
| POST | `/api/admin/memory/tracemalloc/stop` | Disable tracemalloc. |
| GET | `/api/admin/memory/snapshot` | Return top-N allocations plus RSS for OOM debugging. |

#### Deploy State & Zone Health (`deploy.py`)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/admin/deploy-state` | Get current deployment state including monitoring status and health. |
| POST | `/api/admin/deploy-state/pause` | Manually pause the pipeline (set `max_concurrent_vms=0`). |
| POST | `/api/admin/deploy-state/resume` | Resume the pipeline, restoring the pre-pause concurrency. |
| POST | `/api/admin/deploy-state/reset-health` | Force-reset health state and resume (emergency escape hatch). |
| POST | `/api/admin/deploy-state/mark-stable` | Manually mark the current deploy as stable (skip the monitoring window). |
| POST | `/api/admin/deploy-state/rollback` | One-click rollback: shift Cloud Run traffic to `last_known_good_revision`. |
| GET | `/api/admin/zone-health` | Get GCP zone health status for VM provisioning. |

#### Notifications, Alerts & Messages (`admin_notifications.py`)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/admin/alerts` | List active operator alerts. |
| POST | `/api/admin/alerts/{alert_id}/dismiss` | Dismiss a single alert. |
| POST | `/api/admin/alerts/dismiss-all` | Dismiss all visible alerts. |
| GET | `/api/admin/notification-dispatch/stats` | Webhook delivery stats (success/failure counts, retry queue depth). |
| GET | `/api/admin/messages` | List project messages, optionally filtered by project and status. |
| POST | `/api/admin/messages` | Create a message (feedback, bug report, note). |
| PATCH | `/api/admin/messages/{message_id}` | Resolve a message with an optional response. |
| DELETE | `/api/admin/messages/{message_id}` | Delete a message. |
| GET / PUT | `/api/admin/messages/last-seen` | Read / upsert the operator's last-seen timestamp for the messages divider. |
| GET / PUT | `/api/admin/messages/noise-prefs` | Read / upsert messages-panel noise-control preferences. |
| GET / PUT | `/api/admin/messages/saved-views` | Read / upsert the operator's saved message views. |
| GET / PUT | `/api/admin/messages/temp-mutes` | Read / upsert temporary event-type mutes (expired entries dropped). |
| GET / PUT | `/api/admin/messages/mute-window` | Read / upsert the do-not-disturb mute window. |
| GET / PUT | `/api/admin/last-visit-timestamp` | Read / persist the per-operator last-visit timestamp (cross-device home baseline). |

#### Playbook (`admin_notifications.py`)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/admin/projects/{project_id}/playbook` | List all playbook entries for a project with full metadata. |
| PUT | `/api/admin/projects/{project_id}/playbook` | Create or update a playbook entry. |
| DELETE | `/api/admin/projects/{project_id}/playbook/{key}` | Delete a playbook entry by key. |
| POST | `/api/admin/projects/{project_id}/playbook/seed` | Seed default operational knowledge (skips entries that already exist). |

#### Analytics & Cost (`admin_analytics.py`, `efficiency_snapshot.py`)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/admin/direction-auto-pause-stats` | Aggregate direction auto-pause/recovery stats over a rolling window. |
| GET | `/api/admin/task-cost-breakdown` | Spend breakdowns by task type, direction, and most expensive tasks. |
| GET | `/api/admin/task-duration-stats` | p50 per task type, p50/p90 per scope, p50 per phase (last 7 days). |
| GET | `/api/admin/failing-task-types` | Task types currently gated by the per-type failure-rate guard. |
| GET | `/api/admin/open-prs` | All open PRs from GitHub cross-referenced with linked tasks. |
| GET | `/api/admin/projects/{project_id}/efficiency-axes` | Per-axis current/baseline/drift/history for the project's efficiency baseline. |
| GET | `/api/admin/projects/{project_id}/llm-cost-breakdown` | Per-provider and per-model LLM cost/token breakdown over a window. |

#### Compute Jobs (`admin_compute_jobs.py`)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/admin/projects/{project_id}/compute-jobs` | List compute jobs for a project, optionally including recently finished jobs. |
| POST | `/api/admin/compute-jobs/{job_id}/kill` | Request termination of an active compute job. |
| POST | `/api/admin/compute-jobs/{job_id}/analyze` | Trigger analysis for a compute job's output. |

#### Pipeline Observability & Convergence (`admin_pipeline_observability.py`)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/admin/provider-cost-summary` | Per-provider/account cost and success metrics for a rolling window. |
| GET | `/api/admin/drift-snapshot` | Per-direction drift signals for a project. |
| GET | `/api/admin/fix-loop-convergence` | Aggregate fix-loop telemetry across recently terminal tasks. |
| GET | `/api/admin/fix-loop-convergence-trend` | Per-day buckets of fix-loop convergence/divergence rates. |
| GET | `/api/admin/fix-loop-diverged-tasks` | Tasks that exited a fix loop with a specific reason in the window. |
| GET | `/api/admin/outer-loop-convergence` | Aggregate outer review↔fix PR-cycle convergence health. |
| GET | `/api/admin/outer-loop-convergence-trend` | Per-day buckets of outer-loop convergence. |
| GET | `/api/admin/pipeline-regressions` | Active pipeline regression signals with autonomous-loop response status. |
| GET | `/api/admin/pipeline-efficiency` | VM cold-start latency, warm reuse rate, and review iterations. |
| GET | `/api/admin/projects/{project_id}/warm-reuse-funnel` | Rolling warm-VM backlog reuse funnel counters. |
| GET | `/api/admin/digest-latency-stats` | Per-instance rolling p50/p95 latency for `/api/admin/digest`. Accepts `ADMIN_API_KEY` **or** a valid agent JWT (read-only; see Authentication section). |

#### Diagnostics & Debug (`admin_debug.py`, `admin_health.py`, `admin_orchestrator_errors.py`, `anomaly_detection.py`)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/admin/diagnostics` | Aggregate pipeline diagnostics across all projects. |
| GET | `/api/admin/projects/{project_id}/diagnostics` | Structured health snapshot for a single project's pipeline. |
| GET | `/api/admin/projects/{project_id}/autonomy-debug` | Why autonomy is (or isn't) spawning for a project. |
| GET | `/api/admin/projects/{project_id}/strategic-debug` | At-a-glance status for the project's strategic/architecture cycle. |
| GET | `/api/admin/projects/{project_id}/system-maturity` | Per-system maturity rows, ascending by score. |
| GET | `/api/admin/projects/{project_id}/recent-findings` | Recent research findings with actionability grades. |
| GET | `/api/admin/tasks/{task_id}/fix-loop-history` | Fix-loop telemetry persisted on a task. |
| POST | `/api/admin/projects/{project_id}/circuit-breaker/reset` | Reset a project's circuit breaker to CLOSED. |
| POST | `/api/admin/anomaly-throttle/restore` | Clear the active anomaly auto-throttle and restore stashed concurrency. |
| GET | `/api/admin/health-overview` | Unified health overview with top actionable issues. |
| GET | `/api/admin/diagnostics/null-branch-config` | Diagnose projects with unset/invalid branch config. |
| GET | `/api/admin/warm-vm-state` | Snapshot of active VMs and warm-pin chain state. |
| GET | `/api/admin/pr-behind-detector-stats` | In-process counts of `pr_is_not_behind` outcomes by call site. |
| GET | `/api/admin/recent-orchestrator-errors` | Aggregate ERROR-severity orchestrator logs from Cloud Logging. |
| POST | `/api/admin/orchestrator-errors/acknowledge` | Acknowledge an orchestrator error bucket until new occurrences arrive. |
| POST | `/api/admin/orchestrator-errors/acknowledge-all` | Acknowledge all visible orchestrator error buckets. |
| DELETE | `/api/admin/orchestrator-errors/acknowledged` | Clear all acknowledgments so hidden errors reappear. |
| GET | `/api/admin/anomaly-tracker/snapshot` | Read-only snapshot of `shared_state.persistent_anomalies`. |

#### Self-Review Diagnostics (`sr_diagnostics_endpoints.py`)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/admin/projects/{project_id}/sr-missed-retrospectives` | Tasks where SR passed a category human review later flagged. |
| GET | `/api/admin/projects/{project_id}/sr-unhandled-edges-retrospectives` | Tasks with agent-flagged unhandled edge cases. |
| GET | `/api/admin/projects/{project_id}/sr-alert-drilldown` | Tasks in the current SR alert window that missed the given categories. |
| GET | `/api/admin/projects/{project_id}/sr-blind-spots` | Aggregate `blind_spots` from recent self-review outcomes. |
| GET | `/api/admin/projects/{project_id}/sr-recent-misses` | Most recent tasks where SR missed a human-caught category. |
| GET | `/api/admin/projects/{project_id}/direction-sr-breakdown` | Per-direction SR catch-rate breakdown. |
| POST | `/api/admin/projects/{project_id}/sr-investigation` | Spawn an operator-triggered SR investigation task for a category. |
| GET | `/api/admin/projects/{project_id}/sr-investigation-task-ids` | Map of category → task_id for open SR investigations. |
| GET | `/api/admin/projects/{project_id}/sr_catch_rate/health` | Persisted SR catch-rate snapshot for ops/audit/CI. |
| GET | `/api/admin/projects/{project_id}/prompt-fingerprint-analytics` | Group recent code-change outcomes by prompt fingerprint. |
| GET | `/api/admin/projects/{project_id}/research-actionability` | Actionability grade distribution + merge outcome by grade tier. |
| GET | `/api/admin/projects/{project_id}/findings-by-source` | Findings (across directions) whose `source_refs` cite a given ref. |
| GET | `/api/admin/projects/{project_id}/directions/{direction_id}/evicted-findings` | Findings evicted from a direction's active context. |

#### Scope Measurement (`admin_scope_measurement.py`)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/admin/scope-baselines` | Per-scope merge-rate baselines from historical terminal tasks. |
| GET | `/api/admin/medium-scope-measurement` | Medium-scope merge rate with pre/post investment split. |
| GET | `/api/admin/first-attempt-measurement` | Historical first-attempt approval rate with optional pre/post split. |

#### Release Version & Qualification (`admin_release_version.py`)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/admin/projects/{project_id}/version-lifecycle` | Release version lifecycle state for a project. |
| POST | `/api/admin/projects/{project_id}/version-phase` | Set the phase of the open release version. |
| GET | `/api/admin/projects/{project_id}/audit-findings` | Latest qualification's audit findings. |
| GET | `/api/admin/projects/{project_id}/qualification-verdicts` | Recent `QualificationVerdict` rows, newest-first. |
| GET | `/api/admin/projects/{project_id}/retirement-critiques` | Recent retirement critiques across `StrategicCycle` rows. |

#### Max-Plan Pool Accounts (`admin_pool_accounts.py`)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/admin/max-plan-accounts/health` | Per-account health summary (state, 24h completions/RL events, score). |
| GET | `/api/admin/max-plan-accounts/provider-model-breakdown` | Rolling 7-day per-(provider, model) success-rate breakdown. |
| POST | `/api/admin/max-plan-accounts` | Add an LLM account record. |
| PATCH | `/api/admin/max-plan-accounts/{account_id}` | Update an LLM account's fields. |
| DELETE | `/api/admin/max-plan-accounts/{account_id}` | Remove an LLM account. |
| POST | `/api/admin/max-plan-accounts/{account_id}/reset-rate-limits` | Clear all rate-limit state (`disabled_models`) for an account. |
| POST | `/api/admin/max-plan-accounts/{account_id}/regression-quarantine/clear` | Clear a `kind='regression'` disabled-model quarantine entry. |
| POST | `/api/admin/max-plan-accounts/{account_id}/refresh-models` | Probe the provider's list-models endpoint and cache availability. |

#### Residual Remediation Cases (`residual_case_endpoints.py`)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/admin/residual-fingerprints` | Top-N CODE_ERROR fingerprint clusters with no matching classifier. |
| GET | `/api/admin/residual-cases` | List residual remediation cases. |
| POST | `/api/admin/residual-cases/{fingerprint}/ignore` | Mark a residual fingerprint IGNORED for a project. |
| POST | `/api/admin/residual-cases/{fingerprint}/unignore` | Reactivate a previously IGNORED residual fingerprint. |
| POST | `/api/admin/residual-cases/{fingerprint}/unsuspend` | Re-enable a SUSPENDED residual fingerprint case. |
| POST | `/api/admin/residual-fingerprints/clear` | Wipe the in-memory residual fingerprint store. |

#### Activity, Digest & Viewport Debt (`admin_activity.py`, `admin_digest.py`, `viewport_debt.py`)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/admin/activity` | Reverse-chronological list of recent system events. |
| GET | `/api/admin/digest` | Catch-up digest of activity within the requested time window. |
| GET | `/api/admin/projects/{project_id}/viewport-debt` | Per-component viewport opt-out debt summary. |

#### Operator Triggers (`admin_operator_triggers.py`)

Manual operator escape hatches for the autonomous pipeline.

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/admin/projects/{project_id}/self-audit` | Enqueue a `self_audit` task for the project. |
| POST | `/api/admin/projects/{project_id}/qualify` | Manually trigger the release qualifier for the open version. |
| POST | `/api/admin/projects/{project_id}/clear-wave-deps` | Clear only stale/legacy wave serialization edges on QUEUED tasks; structural prerequisites are preserved. |
| POST | `/api/admin/tasks` | Create a task manually. Auto-creates a project for the repo if one does not exist. Requires `acceptance_evidence` for code-producing task types. |
| PATCH | `/api/admin/tasks/{task_id}` | Mutate an existing task in place — drives single-task Retry (`status → QUEUED`), Cancel/Dismiss/Reject/card cancel (`status → CANCELLED`), and Edit (`payloads[0].prompt`) actions. Does not create a new task. |
| GET | `/api/admin/tasks/{task_id}/cancel-impact` | Preview cancellation impact for confirmations: active child cascade count, waiting dependent count, VM cleanup signals, and PR cleanup hints. |
| POST | `/api/admin/tasks/bulk-action` | Bulk approve, retry, or cancel selected tasks. |
| POST | `/api/admin/tasks/{task_id}/archive` | Move a terminal task out of the active board into the archive view (status unchanged). |
| POST | `/api/admin/tasks/{task_id}/unarchive` | Restore an archived task to the active board. |
| POST | `/api/admin/tasks/{task_id}/rollup` | Manually trigger the rollup PR for an EVALUATING parent task. |
| GET | `/api/admin/projects/{project_id}/findings` | Structured research findings for a project, with claims and sources. |

Several `admin_*` routers declare no prefix of their own and are mounted as
sub-routers under a parent that supplies the `/api/admin` prefix (e.g.
`admin_pipeline_observability` and `admin_release_version` are included by
`admin_digest`'s router; `admin_operator_triggers` is included by `admin.py`'s
router). The full paths below already include that prefix.

#### Operator Triggers

Manual operator actions that kick off pipeline work
(`backend/app/routers/admin_operator_triggers.py`). All require admin access.

- **`POST /api/admin/tasks`** — create a task manually (rate-limited 60/min). The request body is the `CreateTaskPayload` model. Key fields:

  | Field | Type | Required | Notes |
  |-------|------|----------|-------|
  | `task_type` | string | ✅ | Any valid task type (e.g. `create`, `fix`, `refactor`, `qa`, `migrate`, `deps`, `docs`, `research`, `plan`, …). |
  | `github_owner` | string | ✅ | GitHub org/user. |
  | `github_repo` | string | ✅ | Repository name. |
  | `prompt` | string | ✅ for most types | Human-readable task description. |
  | `acceptance_evidence` | object | **Required for code-producing types** | Proof-of-completion declaration. Required when `task_type` is `create`, `fix`, `refactor`, `qa`, `migrate`, `deps`, or `docs`. See the [acceptance_evidence reference](#acceptance_evidence-required-for-code-producing-task-types) under `POST /api/deploy` for the full shape. |
  | `base_branch` | string | — | Branch to base the task off (defaults to integration branch). |
  | `branch` | string | — | Pre-existing branch to use instead of creating a new one. |
  | `model` | string | — | LLM model override. |
  | `effort` | string | — | `low` / `medium` / `high` / `max`. |
  | `idempotency_key` | string | — | Alphanumeric key (max 255 chars) to dedupe retried requests — returns the existing task if one with the same key is already active. |
  | `hotfix` | bool | — | `true` + `base_branch="main"` routes a plan task through the expedited hotfix path. |
  | `work_class` | string | — | `exploratory` / `confirmatory` / `productization` — research task planning envelope. |
  | `stop_or_pivot_condition` | string | — | Research stop/pivot criterion (max 4000 chars). |

  The payload is validated by `validate_task_payload` with `require_acceptance_evidence=True`. Omitting `acceptance_evidence` on a code-producing task type returns `422` with a message naming the accepted `kind` values.

  **Response:** `{ "task_id": "uuid", "project_id": "uuid", "status": "QUEUED" }`

- **`POST /api/admin/projects/{project_id}/self-audit`** — spawn a self-audit task.
- **`POST /api/admin/projects/{project_id}/qualify`** — trigger a release-qualification run.
- **`POST /api/admin/projects/{project_id}/clear-wave-deps`** — clear only stale/legacy wave serialization edges on QUEUED tasks while PRESERVING structural prerequisites (research prerequisites, blocked-need resolution, sibling foundations). Returns `cleared`, `preserved`, and `total_queued`.
- **`PATCH /api/admin/tasks/{task_id}`** — single-task mutation. **Retry** sends `{"status": "QUEUED"}` (re-queues the same task — no new task row), **Cancel/Dismiss/Reject/card cancel** send `{"status": "CANCELLED"}`, and **Edit** sends `{"payloads": [{"prompt": "..."}]}`. Status transitions, cascade cancellation, and requeue are applied in `admin_task_mutations.modify_task_apply`.
- **`POST /api/admin/tasks/bulk-action`** — body: `{"action": "approve" | "retry" | "cancel", "task_ids": ["..."]}`. Returns `results`, `success_count`, and `error_count`.
- **`GET /api/admin/tasks/{task_id}/cancel-impact`** — read-only confirmation helper. Returns authoritative project-scoped counts for direct active children that would cascade cancel, waiting dependents with `depends_on` edges, target/child VM cleanup signals, and conservative PR cleanup hints.
- Cancellation via `PATCH status → CANCELLED` or bulk `action=cancel` sets the selected task terminal, clears approval, cascades `CANCELLED` to direct active children (`parent_task_id == task.id`, status `QUEUED`/`PROVISIONING`/`IN_PROGRESS`/`EVALUATING`), and runs best-effort orphan PR cleanup where task context identifies an open PR. VM/account cleanup runs only for cancelled rows that have a `vm_instance_id` or account lease. Queued, blocked, and parked tasks may have no provisioned VM to terminate. Waiting dependents may be stranded; use `/cancel-impact` for full project-scoped counts before presenting exact impact.
- **`POST /api/admin/tasks/{task_id}/archive`** / **`/unarchive`** — move a terminal task out of (or back into) the active board without changing its status.
- **`POST /api/admin/tasks/{task_id}/rollup`** — force a rollup of the given task's branch.
- **`GET /api/admin/projects/{project_id}/findings`** — list audit/research findings.

#### Pipeline Observability

Read-only metrics powering the pipeline-health dashboards
(`backend/app/routers/admin_pipeline_observability.py`). All are `GET`.

- **`/api/admin/provider-cost-summary`** — per-provider cost rollup.
- **`/api/admin/drift-snapshot`** — current drift snapshot.
- **`/api/admin/fix-loop-convergence`** and **`/fix-loop-convergence-trend`** — inner fix-loop churn metrics.
- **`/api/admin/outer-loop-convergence`** and **`/outer-loop-convergence-trend`** — outer review/fix-loop churn metrics.
- **`/api/admin/fix-loop-diverged-tasks`** — tasks whose fix loop is not converging.
- **`/api/admin/pipeline-regressions`** — detected pipeline regressions.
- **`/api/admin/projects/{project_id}/warm-reuse-funnel`** — warm-VM reuse funnel for a project.
- **`/api/admin/pipeline-efficiency`** — aggregate pipeline-efficiency metrics.

#### Release Version Lifecycle

Per-project release/version lifecycle and qualification reporting
(`backend/app/routers/admin_release_version.py`).

- **`GET /api/admin/projects/{project_id}/version-lifecycle`** — version lifecycle state.
- **`POST /api/admin/projects/{project_id}/version-phase`** — advance the version phase.
- **`GET /api/admin/projects/{project_id}/qualification-verdicts`** — qualification verdict history.
- **`GET /api/admin/projects/{project_id}/audit-findings`** — release-audit findings.
- **`GET /api/admin/projects/{project_id}/retirement-critiques`** — version-retirement critiques.

#### Evergreen Backlog (`admin_evergreen.py`)

Read/write surface for the evergreen backlog auto-fill mechanism — a ledger of findings that should be periodically revisited as code evolves.

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/admin/evergreen/backlog` | Enroll a backlog candidate (deduped by `finding_key`). |
| GET | `/api/admin/evergreen/backlog` | List evergreen backlog items for a project. |
| GET | `/api/admin/evergreen/backlog-summary` | Per-status count summary of backlog items. |
| GET | `/api/admin/evergreen/fill-observability` | Bounded decision counters for the fill process. |

**Authentication:** Admin required.

#### System State (`admin_state.py`)

Canonical system-state read endpoint for CLI and UI health panels.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/admin/state` | System state snapshot (deploy + fleet; fleet-only requires admin; project-scoped view optionally includes version block). 15s cache TTL. |

**Query parameters:**
- `project_id` (optional UUID) — If supplied, includes project version state; requires at least viewer role on the project.

#### Warm Pool Management (`admin_warm_pool.py`)

Manual operator control for the warm-VM pool (autonomously maintained by `warm_pool_maintainer`). These endpoints complement the autonomous bootstrap and enforce the same budget ceilings and fleet constraints.

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/admin/projects/{project_id}/warm-pool/seed` | Top the project's warm pool up to its configured target size. |
| POST | `/api/admin/projects/{project_id}/warm-pool/drain` | Retire all live warm VMs for the project. |
| POST | `/api/admin/projects/{project_id}/warm-pool/enable` | Enable warm pooling for this project (scoped allowlist), establish target, seed initial members. |
| POST | `/api/admin/projects/{project_id}/warm-pool/disable` | Drain the project and remove from allowlist; flips master switch off only if allowlist becomes empty. |
| GET | `/api/admin/projects/{project_id}/warm-pool/status` | Current pool state (size, capacity, allowlist, enabled flag). |

**Authentication:** Admin required for all warm-pool operations.

**Response fields (additive-only contract):** `seeded`, `drained`, `recycled`, `reason`, `project_id`, `enabled`, `project_allowlist`.

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

Prometheus text metrics (text/plain exposition format). Returns `403 Forbidden` if `METRICS_TOKEN` is configured but the request doesn't include a valid `?token=<value>` query parameter. When `METRICS_TOKEN` is unset, the endpoint also returns `403` (fail-closed security design). Exposes orchestrator cycle age, active VM counts, consecutive autonomous-failure counts, and related gauges.

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
