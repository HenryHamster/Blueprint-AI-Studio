# Blueprint API Reference

This document describes all publicly exposed HTTP endpoints in the Blueprint orchestrator.

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

## GitHub Proxy Endpoints

Agent VMs use these to call GitHub APIs without storing credentials locally.

### `POST /api/agent/github/pull-requests`

Create a pull request.

**Request body:**
```json
{
  "task_id": "uuid",
  "title": "string",
  "body": "string",
  "head": "branch_name",
  "base": "main|target_branch"
}
```

**Response:** `{ "url": "https://github.com/owner/repo/pull/123", "number": 123 }`

---

### `POST /api/agent/github/commit-status`

Update commit status (pending, success, failure).

**Request body:**
```json
{
  "task_id": "uuid",
  "sha": "commit_hash",
  "state": "pending|success|failure|error",
  "description": "string",
  "context": "continuous-integration/task-name"
}
```

---

### `POST /api/agent/github/pr-comment`

Add a comment to a PR.

**Request body:**
```json
{
  "task_id": "uuid",
  "pr_number": 123,
  "body": "string"
}
```

---

## Admin API

Access these via the admin UI (requires `ADMIN_API_KEY` or Firebase admin token).

### `GET /api/admin/projects`

List all projects with summary stats.

**Query parameters:**
- `limit`: Default 20
- `offset`: Pagination offset

---

### `POST /api/admin/directions`

Create or update a strategic direction (autonomous research mode).

**Request body:**
```json
{
  "project_id": "uuid",
  "title": "string",
  "description": "string",
  "mode": "active|paused|completed",
  "priority": 0-100
}
```

---

### `GET /api/admin/health`

Orchestrator health check.

**Response:**
```json
{
  "status": "healthy|degraded|down",
  "vm_count": 5,
  "queued_tasks": 10,
  "failed_tasks": 2,
  "uptime_seconds": 86400,
  "database": "connected|disconnected"
}
```

---

### `GET /api/admin/analytics`

Aggregated metrics across all projects.

**Response includes:**
- Total tasks by status and type
- Success rate by task type
- Average completion time
- Failure distribution
- Cost breakdown (on-demand vs. spot VMs)

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

- `/api/deploy`: 5 requests per minute per user
- `/api/agent/*`: 100 requests per minute per task
- `/api/admin/*`: 60 requests per minute

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
