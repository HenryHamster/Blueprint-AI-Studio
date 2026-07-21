# Blueprint AI Studio — Cross-Cutting Codebase Analysis

> Generated: 2026-04-13  
> Branch: automain  
> Scope: Full repository analysis across frontend, backend, agent, and infrastructure

---

## 1. Overall Architecture

Blueprint is an **autonomous Claude Code launcher platform** that manages the complete lifecycle of AI-driven software engineering tasks. It operates as a three-tier system:

```
┌─────────────────────────────────────────────────────────────────┐
│  Admin UI (React + TypeScript / Vite + TailwindCSS)             │
│  - Task creation & monitoring                                    │
│  - Real-time log streaming (WebSocket)                           │
│  - Deployment health dashboards                                  │
│  - User & project management                                     │
└─────────────────────┬───────────────────────────────────────────┘
                      │  Firebase Auth + REST APIs + WebSocket
┌─────────────────────▼───────────────────────────────────────────┐
│  Orchestrator (FastAPI / Python / SQLModel + PostgreSQL)         │
│  - Task queue management & polling                               │
│  - VM provisioning & lifecycle (GCP Compute Engine)             │
│  - Background workers (reaper, cleanup, monitoring)              │
│  - GitHub App integration                                        │
│  - Circuit breakers & deployment health tracking                 │
└─────────────────────┬───────────────────────────────────────────┘
                      │  JWT tokens + REST APIs
┌─────────────────────▼───────────────────────────────────────────┐
│  Agent VMs (TypeScript / Node.js — one VM per task)             │
│  - Clone repo + run Claude Code CLI                              │
│  - Heartbeat mechanism & log streaming                           │
│  - Git workflow (branch creation, PR submission)                 │
│  - 28 bespoke task handlers (create, refactor, plan, research, …) │
└─────────────────────────────────────────────────────────────────┘
```

**Operational modes:**
- **Production (GCP)**: VMs spin up on Google Compute Engine, no public IPs (Cloud NAT egress only), auto-destruct after 1 hour or heartbeat timeout (5 min).
- **Local mode**: Agents run as child processes (development / testing).

---

## 2. Key Technologies

| Layer | Primary Technologies |
|---|---|
| Frontend | React 19, TypeScript 5.9, Vite 6.2, TailwindCSS 4, Recharts, dnd-kit |
| Backend | Python, FastAPI 0.104.1, SQLModel (SQLAlchemy 2.0), asyncio, pytest-asyncio |
| Database | PostgreSQL 15 (production), SQLite (local/CI testing) |
| Agent | TypeScript, Node.js, tsx, WebSocket, Claude Code CLI subprocess |
| Auth | Firebase Admin SDK (user tokens), custom short-lived JWT (agent VMs) |
| Cloud | GCP Compute Engine, GCS, Secret Manager, Cloud Run, Cloud Trace |
| Infra | Terraform, Packer (VM images), Docker Compose (local dev) |
| Optional | Redis (distributed semaphore, rate limiting, webhook dedup) |

---

## 3. Component Communication

### Synchronous (REST)

```
Admin UI ↔ Orchestrator (Bearer token from Firebase)
  POST /api/deploy              — launch tasks
  GET  /api/projects/{id}/tasks — list live tasks
  POST /api/admin/directions    — steer autonomous behavior
  GET  /api/admin/*             — project / user management

Agent VMs ↔ Orchestrator (short-lived JWT)
  POST /api/agent/status        — task completion report
  POST /api/agent/heartbeat     — 60 s keep-alive
  POST /api/agent/spawn         — create child subtasks
  POST /api/agent/refresh-token — extend JWT
  POST /api/agent/artifacts/upload-url — signed GCS URL
  POST /api/agent/credentials/rotate   — provider secret refresh
  POST /api/agent/github/*      — GitHub API proxy
  POST /api/agent/vm-heartbeat  — warm-VM liveness
  POST /api/agent/next-role     — warm-VM handoff/backlog claim
```

### Asynchronous (WebSocket & events)

```
Orchestrator → Admin UI (real-time log streaming)
  POST /api/projects/{project_id}/ws-ticket — mint a short-lived ticket
  WS /api/projects/{project_id}/ws          — send {"ticket": "..."} first
  Messages: {"event": "...", "data": {...}}
  Secrets are redacted before broadcast.

Internal background services (asyncio tasks, crash-monitored):
  orchestrator_worker  — 15 s polling loop (configurable via ORCHESTRATOR_HARD_WATCHDOG_POLL_SECONDS)
  reaper               — ~60 s cycle with exponential backoff on errors
  gcp_cleanup          — 300 s VM reconciliation
  deployment_monitor   — Cloud Run health observer
  background_monitor   — crash-and-restart supervisor
```

### External integrations

```
Orchestrator ↔ GitHub (REST + webhook)
  OAuth token exchange (user → installation ID)
  Branch creation, PR submission, PR reviews
  Commit status updates (CI check markers)
  Webhook ingestion (push, PR, check_run events)

Orchestrator ↔ GCP
  Compute Engine: VM provisioning, zone-aware scheduling, auto-delete
  Secret Manager: GitHub tokens, Claude OAuth tokens
  Cloud Storage:  artifacts (screenshots, reports, logs)
```

---

## 4. Authentication & Authorization

### User authentication (frontend → backend)
- **Firebase** ID tokens verified by Firebase Admin SDK.
- **Allowlist**: `AllowedUser` table gates access (email + role: `admin` / `user`).
- **Admin API Key**: `ADMIN_API_KEY` env var for programmatic access.
- **WebSocket**: Temporary ticket issued per project access check and sent as the first WebSocket message, so it does not appear in browser URL history.

### Agent VM authentication
- **Short-lived JWT**: Standard 1-hour tokens signed with `AGENT_JWT_SECRET`, with an 8-hour bootstrap token for cold-start agents before their first successful refresh.
- **Claims**: `task_id`, `vm_instance_id`, `task_type` (endpoint-scoped).
- **Revocation**: Tokens blacklisted (in-memory `_terminated_task_ids` dict) when task reaches a terminal state; 9-hour TTL (> 8-hour JWT lifetime).
- **Refresh**: `/api/agent/refresh-token` endpoint for within-VM refresh.

### Project-level RBAC
- `ProjectMember`: many-to-many (`user_id`, `project_id`, `role`: `owner` / `member` / `viewer`).
- Role hierarchy: `owner > member > viewer`.
- All task/project queries verify project membership before granting access.

---

## 5. Data Models & Schema

### Core tables (31 migrations total)

| Table | Purpose |
|---|---|
| `AllowedUser` | Access control allowlist; Firebase UID, role, last_login |
| `Project` | Multi-project + autonomous research; status, GitHub repo, directions, vision, design_doc |
| `ProjectMember` | Collaboration RBAC; project_id, user_id, role |
| `Task` | Flexible task execution; JSONB payloads + context, status state machine, depends_on DAG |
| `TaskHistory` | Immutable audit trail; action, timestamp, details |
| `Artifact` | Agent outputs; git commit, PR URL, GCS URI, screenshot |
| `Alert` | System & project-level warnings; severity, dismissal |
| `ProjectMessage` | Async agent↔user message board; question, feedback, blocker |
| `DeploymentState` | Singleton; Cloud Run revision, circuit breaker states, zone health, runtime config |
| `MaxPlanAccount` | Claude OAuth token pooling; rate_limit_state JSONB |
| `UserIntegration` | GitHub installation ID, PAT |

### Task status state machine

```
QUEUED
  → PROVISIONING   (orchestrator assigns VM)
  → IN_PROGRESS    (agent claims task + starts running)
  → EVALUATING     (plan task waiting for child subtasks)
  → COMPLETED      (success)
  → FAILED         (error or timeout)
  → CANCELLED      (user or orchestrator abort)
```

### Notable JSONB columns
- `Task.payloads`: list of work-item dicts (one per execution attempt); accessed via `task.first_payload`.
- `Task.context`: shared memory during execution (parent refs, codebase context).
- `Task.depends_on`: task dependency DAG.
- `DeploymentState.circuit_breaker_states`: per-project breaker state, persisted across restarts.
- `DeploymentState.zone_health_state`: per-zone availability + cooldown tracking.

---

## 6. Task Processing & Agent Dispatch

### Orchestrator worker loop (10 s cadence)
1. Poll `QUEUED` tasks ordered by priority + `created_at`.
2. Check dependency DAG (skip if parent not `COMPLETED`/`MERGED`).
3. Check file conflicts (abort early before VM provisioning).
4. Acquire distributed semaphore (Redis `SETNX`, max `runtime_max_concurrent_vms`).
5. Zone-aware VM provisioning (round-robin GCP zones, skip cooled-down zones).
6. Inject short-lived JWT into GCP VM startup metadata.
7. Monitor `EVALUATING` tasks (watch child subtasks; transition to `COMPLETED` when all children terminal).

### Reaper (30 s cadence)
- `PROVISIONING > 15 min` → `FAILED` (VM boot hung).
- `IN_PROGRESS` with no heartbeat for > 5 min → `FAILED` (agent crash).
- Calls `delete_agent_vm()` for GCP cleanup on failure.

### GCP cleanup service (300 s cadence)
- Lists all `agent-vm-*` VMs in GCP.
- Deletes any VM with no matching `PROVISIONING`/`IN_PROGRESS` task record.
- Caches live VM count for concurrency checks.

### Task handlers (28 types)

| Category | Handlers |
|---|---|
| Core | `create`, `refactor`, `docs`, `qa`, `deps`, `migrate`, `debug`, `perf`, `investigate` |
| Review/Fix | `review`, `fix`, `release_audit`, `release_audit_challenge`, `release_merge_grade` |
| Planning & Research | `plan`, `research`, `web_research`, `strategic_review`, `design_doc_update` |
| Auditing & Testing | `security_audit`, `smoketest`, `verify_tests`, `self_audit` |
| Structured Output | `memo`, `compute_experiment`, `repo_bootstrap`, `design` |

**Plan decomposition flow:**
1. Clone repo; run Claude Code with decomposition prompt.
2. Parse JSON subtask array from Claude output.
3. `POST /api/agent/spawn` for each subtask.
4. Orchestrator marks parent `EVALUATING`.
5. All children reach terminal state → parent → `COMPLETED`.

---

## 7. Error Handling & Resilience

### Circuit breaker (per-project, Hystrix-inspired)

```
States: CLOSED → OPEN → HALF_OPEN → CLOSED
- 3 consecutive task failures → OPEN (block new autonomous tasks)
- After cooldown (30 min, exponential, max 2 h) → HALF_OPEN
- One test task succeeds → CLOSED; fails → OPEN (longer cooldown)
- State persisted in DeploymentState (survives restarts)
```

### Zone health tracking (GCP)

```
Per-zone failure reasons: QUOTA_EXCEEDED, ZONE_UNAVAILABLE, …
- 2+ consecutive failures → skip zone for 5 min (exponential, max 2 h)
- Persisted in DeploymentState
```

### Background task crash recovery

```
_monitor_background_tasks() — 30 s loop
  For each asyncio background task:
    if task.done() (unexpected):
      log exception → alert all projects
      restart with exponential backoff (1 s, 2 s, 4 s, …)
```

### Rate limiting & backoff
- API: 5 requests / minute per IP (slowapi; Redis-backed when available).
- GitHub API: respect 429 `Retry-After`; back off when < 10 calls remaining.
- GCP provision: distributed semaphore (max 6 concurrent).
- Agent retries: max 5 attempts with exponential backoff.

### JWT revocation
- In-memory `_terminated_task_ids` dict; O(1) check before DB query.
- 9-hour TTL (longer than 8-hour JWT lifetime); eviction of stale entries.

---

## 8. Security Design

### Prompt injection scanning (`app/security.py`, 516 LOC)
- 100+ regex patterns for known injection attempts.
- Fuzzy matching for obfuscated variants.
- Base64 and URL-decode passes before scanning.
- Applied to all user-supplied task descriptions before dispatch.

### Secret redaction in logs
- Regex patterns: `sk-ant-*`, `ghp_*`, `ghs_*`, `github_pat_*`, OAuth tokens, PEM keys.
- Applied in orchestrator before WebSocket broadcast.
- Applied in agent before log transmission.

### Network isolation
- Production VMs: no public IPs; egress-only via Cloud NAT.
- Agent↔Orchestrator: HTTPS only; JWT scoped per task.
- WebSocket tickets: 1-minute expiry, prevents replay.

### Least-privilege
- Agent JWT claims include `task_type` for endpoint-level authorization.
- GitHub tokens scoped to installation (not personal PAT in production).
- GCS signed URLs expire quickly; agents never hold permanent storage credentials.

---

## 9. Testing Approach

### Backend (pytest)
- `asyncio_mode = "auto"` throughout.
- In-memory SQLite (local dev, fast); PostgreSQL (CI, schema validation).
- Fixtures: `db_session` (per-test cleanup), `http_client` (AsyncClient + dependency overrides), `mock_gcp`, `mock_github`.
- Key test files:

| File | Coverage |
|---|---|
| `test_api.py` | Core REST endpoints |
| `test_agent.py` | Agent claim/status/spawn lifecycle |
| `test_background.py` | Reaper, cleanup, orchestrator loop |
| `test_security.py` | 100+ injection patterns |
| `test_autonomous_research.py` | Research spawning + plan decomposition |
| `test_pipeline_health.py` | Circuit breaker, zone health |
| `test_pipeline_integration.py` | End-to-end integration scenarios |

```bash
# Run all tests
bash scripts/test-backend.sh

# Specific file
bash scripts/test-backend.sh testing/test_agent.py

# Standalone (no DB)
bash scripts/test-backend.sh --standalone
```

### Frontend
- `npm run build` (type checking + asset bundling).
- `npx eslint .` (lint rules).
- No unit tests; build + linting only.

### Agent
- `npx tsc --noEmit` (TypeScript type checking).
- No runtime tests; linting + type safety only.

---

## 10. Notable Design Patterns

### Push-model task assignment
Orchestrator assigns `vm_instance_id` to a task *before* the VM boots, then JWT is scoped to that `task_id`. Prevents race conditions where multiple VMs claim the same task.

### Flexible JSONB payloads
`Task.payloads` is a list of dicts (one per execution); no schema enforced at the ORM layer. Accessed uniformly via `task.first_payload`. Supports 28 handler types without schema migrations.

### Monolithic single-file agent
`agent/src/index.ts` (~8 000 LOC) intentionally keeps all state in one process per VM. Handlers are modular functions imported from `handlers/`. Simplifies state sharing and VM lifecycle.

### Status log as bounded audit trail
`Task.status_log` is auto-appended on every status + message change via a SQLAlchemy event listener; capped at 90 entries. Used by the UI for timeline display and by diagnostics.

### Semaphore-based concurrency
- Local: `asyncio.Semaphore` (per-instance guard).
- Distributed: Redis sorted set with 10-min TTL auto-reclaim.
- Graceful fallback to local semaphore when Redis is unavailable.

### Deployment state singleton
`DeploymentState` is a single row in Postgres that acts as a distributed config store: current Cloud Run revision, circuit breaker states, zone health, and runtime overrides (max VMs, max retries). Survives restarts; admin-configurable at runtime.

### Autonomous research framework
- **Strategic agent**: generates long-term vision and design docs.
- **Tactical agents**: execute specific research / improvement tasks.
- **Direction board**: users steer via `Project.directions` (JSONB list).
- **Anomaly detection**: persistent anomaly tracking across cycles.

---

## 11. Technical Debt & Improvement Opportunities

### High priority

1. **Monolithic files** — `backend/app/main.py` (~2 400 LOC, ceiling 2 500) and `agent/src/index.ts` are hard to navigate and have hard line/function ceilings enforced by `test_invariants.py`. Substantial decomposition has already occurred (routers extracted, task lifecycle split). Further extraction must respect the ceilings.

2. **Missing payload schema validation** — `Task.payloads` is raw JSONB; handlers assume fields exist without null checks. Adding Pydantic schemas per handler type (`CreatePayload`, `ResearchPayload`, …) would catch malformed tasks at ingestion time.

3. **Error context loss in orchestrator loop** — broad `except Exception` handlers catch all errors; only a string message is stored. Adding structured `error_class` / `reason_code` fields would enable better circuit breaker logic and alerting.

### Medium priority

4. **Incomplete type coverage** — `app/github.py` suppresses `warn_return_any`; test files disable `DTZ`/`BLE` Ruff rules broadly. Tightening types would surface latent bugs.

5. **Rate limiter fairness** — per-IP limits penalize shared corporate NAT IPs. A per-authenticated-user override for known internal IPs would improve ergonomics.

6. **Handler registration** — `handlers/index.ts` must be manually updated when adding a new handler. A registry pattern or directory-based auto-discovery would reduce friction.

7. **Alembic migration naming** — files use numeric prefixes only (e.g., `0024_`). Semantic suffixes (e.g., `0024_add_project_members`) would make `git log` diffs more readable.

8. **Distributed integration tests** — CI uses SQLite + mocked GCP. A test tier against real PostgreSQL + Redis would catch distributed-semaphore and connection-pool edge cases.

### Already addressed (previously listed as debt)

- **WebSocket heartbeat** — ping/pong heartbeats implemented; `ws.close(4001)` on pong timeout triggers reconnect.
- **GCS artifact retention** — 7-day auto-delete lifecycle rule in Terraform (`infra/terraform/iam.tf`).
- **Dependency DAG cycle detection** — DAG is now validated at creation to prevent silent deadlocks.

---

## 12. Key File Reference

| File | Purpose |
|---|---|
| `backend/app/models.py` | SQLModel data models, enums, state machines |
| `backend/app/main.py` | FastAPI app entry point; mounts all routers (~2 400 LOC, ceiling 2 500) |
| `backend/app/auth.py` | Firebase auth, agent JWT, WebSocket ticket issuance |
| `backend/app/orchestrator.py` | Orchestrator worker loop (polling, provisioning, monitoring) |
| `backend/app/orchestrator_filters.py` | Task eligibility and conflict filtering for the orchestrator |
| `backend/app/reaper.py` | Orphaned VM detection & recovery |
| `backend/app/compute.py` | GCP Compute API wrapper; zone-aware scheduling |
| `backend/app/github.py` | GitHub API client (httpx, retry logic, rate limiting) |
| `backend/app/security.py` | Prompt injection detection (100+ patterns, fuzzy match, base64) |
| `backend/app/shared_state.py` | Shared globals (runtime_config, circuit_breakers, metrics) |
| `backend/app/lifecycle.py` | Startup/shutdown hooks, background task crash monitoring |
| `backend/app/task_lifecycle.py` | Core task state transitions and post-commit flow (~1 200 LOC, ceiling 1 200) |
| `backend/app/task_completion.py` | Task completion handling and evaluation |
| `backend/app/research_completion.py` | Research task completion + ghost-merge dedup filter |
| `backend/app/strategic_completion.py` | Strategic cycle completion (direction create/retire/vision) |
| `backend/app/review_pipeline.py` | Review-fix iteration logic; caps per task size |
| `backend/app/rollup_management.py` | Rollup PR creation and retry (~700 LOC, effectively frozen) |
| `backend/app/autonomous_research.py` | Autonomous research spawning, strategic cycles, direction health |
| `backend/app/direction_lifecycle.py` | Direction pruning, auto-pause, auto-recovery |
| `backend/app/deploy_health.py` | Deployment health monitoring and webhook notifications |
| `backend/app/failure_classification.py` | On-read failure class computation (no DB storage) |
| `backend/app/routers/agent.py` | Agent lifecycle endpoints (claim, status, spawn, GitHub proxy) |
| `backend/app/routers/projects.py` | Deploy, status, WebSocket endpoints |
| `backend/app/routers/directions.py` | Direction CRUD endpoints |
| `backend/app/routers/admin_analytics.py` | Analytics and digest endpoints |
| `backend/app/routers/admin_settings.py` | Runtime configuration endpoints |
| `backend/app/admin.py` | Admin panel endpoints (users, tasks, manual create) |
| `admin-ui/src/useAppState.ts` | Master state hook (tasks, projects, metrics, WS logs) |
| `admin-ui/src/hooks/useLogViewer.ts` | WebSocket log streaming |
| `admin-ui/src/hooks/useDeploymentState.ts` | Deployment monitoring + rollback |
| `admin-ui/src/components/KanbanBoard.tsx` | Task status columns (drag-drop) |
| `agent/src/index.ts` | Main agent process, API client, task execution (monolith) |
| `agent/src/prompt.ts` | Prompt building, context assembly |
| `agent/src/handlers/research-plan.ts` | Plan decomposition + research report handlers |
| `backend/testing/conftest.py` | Pytest fixtures + DB setup |
| `backend/testing/test_agent.py` | Agent claim/status/spawn endpoint tests |
| `backend/testing/test_security.py` | Injection detection pattern tests |
