# Blueprint API Reference

This document describes the public HTTP surface exposed by the Blueprint orchestrator.
Routes are grouped by router family; the implementation lives in `backend/app/main.py`
and the router modules under `backend/app/routers/`.

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

**Response:** `{ "task_id": "uuid", "project_id": "uuid", ... }`

**Status codes:**
- `200` — Task created and queued
- `429` — Rate limited (5 per minute)
- `400` — Invalid payload or missing required fields
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

Stream live task logs via WebSocket. Requires a `ws_ticket` query parameter obtained from `POST /api/projects/{project_id}/ws-ticket`.

**Connection flow:**
1. `POST /api/projects/{project_id}/ws-ticket` → get `{"ticket": "string", "expires_at": "ISO8601"}`
2. Connect to `ws://orchestrator/api/projects/{project_id}/ws?ws_ticket=<ticket>`
3. Receive JSON messages: `{"task_id": "uuid", "log": "string", "timestamp": "ISO8601"}`

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

Access these via the admin UI (requires `ADMIN_API_KEY` or Firebase admin token).

### `/api/admin/*`

The admin surface is split across `admin.py` and `backend/app/routers/admin_*.py`.
Common route families include project CRUD, settings, analytics, digest/reporting,
health, notifications/messages, users, onboarding, scope measurement, pool-account
management, and operator/debug triggers. See the router modules for the exact paths.

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

All WebSocket messages are JSON:

```json
{
  "type": "log|status|error|heartbeat",
  "task_id": "uuid",
  "timestamp": "ISO8601",
  "data": {...}
}
```

**Message types:**
- `log` — Task output log line
- `status` — Task status changed
- `error` — Error event
- `heartbeat` — Keep-alive ping

---

## Data Models

See [`docs/DATA_MODELS.md`](DATA_MODELS.md) for detailed schema of request/response objects.
