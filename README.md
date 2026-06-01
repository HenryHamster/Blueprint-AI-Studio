# Blueprint

Blueprint is a platform for launching, monitoring, and scaling autonomous Claude Code instances on sandboxed GCP Virtual Machines. Assign a repo and a prompt, and Blueprint provisions an isolated VM, runs Claude Code, pushes a branch, and opens a PR.

Blueprint also supports autonomous research mode: the orchestrator can generate and dispatch tasks from configurable strategic directions without manual intervention.

The runtime is split across three main surfaces:

- `backend/` - FastAPI orchestrator and background workers
- `admin-ui/` - React admin dashboard on port `3001`
- root `src/` - launcher UI on port `3000`

## Architecture

```
Launcher UI (React, root Vite app)   Admin UI (React workspace)
        \                  /
         \                /
    Orchestrator (FastAPI / Cloud Run)
              |
    +---------+---------+
    |                   |
Agent VMs          Background workers
(GCP Compute)      (orchestrator, reaper, cleanup)
    |
 Claude Code CLI
 (clone, edit, PR)
```

### Components

- **Orchestrator** (`backend/`) — FastAPI backend deployed to Cloud Run. Manages projects, tasks, VM lifecycle, and autonomous research. It registers the API routers under `backend/app/routers/` and runs background workers:
  - `orchestrator_worker` — polls for QUEUED tasks every 10 s and provisions VMs; it
    also drives autonomous-research spawning each cycle via `_check_idle_research()`
    (there is no separate autonomous-research worker loop)
  - `reaper_service` — kills VMs that miss heartbeats (5-minute timeout, 30 s cadence)
  - `gcp_cleanup_service` — sweeps orphaned VMs every 15 min

- **Admin UI** (`admin-ui/`) — React dashboard for monitoring agents, reviewing directions, configuring settings, and viewing analytics. It also consumes the WebSocket log stream.

- **Launcher UI** (root `src/`) — Root Vite app for creating new tasks and viewing progress. Runs on port 3000 in local dev.

- **Agent** (`agent/`) — TypeScript process that runs inside each VM. Claims its task, clones the repo, runs Claude Code with streaming output, handles the review-fix cycle, and creates a PR.

- **Infrastructure** (`infra/`) — Terraform for VPC, Cloud NAT, Cloud Run, AlloyDB, and IAM. Packer builds the pre-baked agent VM image.

## Quick Start (Local Mode)

Local mode runs agents as subprocesses instead of GCP VMs. You still need the backend, database, and frontends running locally.

```bash
# 1. Copy and fill in environment variables
cp .env.example .env
# Set at minimum: ANTHROPIC_API_KEY, ADMIN_API_KEY, LOCAL_MODE=true, DATABASE_URL

# 2. Start PostgreSQL
docker compose up -d db

# If you prefer containerized backend instead of uvicorn, `docker compose up -d` starts both `db` and `app`.

# 3. Install backend dependencies and run the API (separate terminal)
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8080

# 4. In another terminal, start the UIs (launcher on :3000, admin on :3001)
npm install
npm run dev

# 5. Open http://localhost:3000 to launch tasks, or http://localhost:3001 for the admin dashboard
```

## Production Setup (GCP)

See [Deploy.md](Deploy.md) for full Cloud Run + GCP deployment instructions.

```bash
# One-command setup (runs Terraform, builds agent image, deploys backend and frontend)
./setup.sh
```

## Configuration

| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | API key passed to agent VMs for Claude Code |
| `ADMIN_API_KEY` | Shared key for admin UI authentication |
| `AGENT_JWT_SECRET` | Secret used to sign short-lived agent JWTs |
| `WS_TICKET_SECRET` | Secret used to mint WebSocket admission tickets |
| `LOCAL_MODE` | `true` to run agents as local subprocesses |
| `DATABASE_URL` | PostgreSQL connection string used by the backend |
| `GITHUB_APP_ID` | GitHub App installation metadata used during auth flows |
| `GITHUB_PRIVATE_KEY` | GitHub App private key used to sign installation tokens |
| `GITHUB_WEBHOOK_SECRET` | Secret used to verify GitHub webhooks |
| `GCP_PROJECT_ID` | GCP project for VM provisioning |
| `ORCHESTRATOR_URL` | URL agents use to reach the orchestrator |
| `MAX_CONCURRENT_VMS` | Maximum simultaneously running agent VMs (default 8) |
| `MAX_PROVISION_SLOTS` | Maximum concurrent VM provision operations (default 10) |

See `.env.example` for the full list.

## How It Works

1. Open the launcher UI and click Launch Task
2. Enter a GitHub repo (`owner/repo`), base branch, and a prompt
3. The orchestrator creates a project and task, then assigns a VM name
4. The orchestrator worker picks up the queued task and provisions a GCP VM, or a local subprocess in `LOCAL_MODE=true`
5. The agent boots and claims its task via the JWT-authenticated API
6. It clones the repo, creates a working branch, and runs Claude Code
7. Logs stream back to the admin UI in real time via WebSocket
8. On success, it commits, pushes, creates a PR, and reports `COMPLETED`
9. On failure, it reports `FAILED` with logs and retries up to 3 times
10. The VM is reaped if it stops heartbeating or exceeds its hard TTL

## Task Types

Blueprint supports 20 task types, each handled by a dedicated agent handler:

| Type | Description |
|---|---|
| `create` | Implement a new feature from a prompt |
| `refactor` | Refactor existing code |
| `review` | Review a PR and leave comments |
| `fix` | Fix issues identified in a review |
| `plan` | Generate an implementation plan |
| `research` | Research a codebase area and produce findings |
| `strategic_review` | Generate strategic directions based on research findings |
| `design_doc_update` | Update the project design document |
| `security_audit` | Audit code for security issues |
| `qa` | Write or run quality assurance tests |
| `deps` | Update dependencies |
| `docs` | Write or update documentation |
| `smoketest` | Run smoke tests against a deployment |
| `migrate` | Write a database migration |
| `debug` | Investigate and diagnose a bug |
| `perf` | Profile and improve performance |
| `investigate` | Open-ended investigation of an issue |
| `self_audit` | Audit system maturity and recommend operating mode |
| `release_audit` | Audit the release qualifier diff |
| `memo` | Produce a structured reasoning artifact without opening a PR |

## Autonomous Research Mode

When enabled, Blueprint continuously generates and dispatches tasks without manual intervention:

1. **Strategic directions** define areas of focus (e.g., "Improve test coverage", "Reduce latency")
2. The **autonomous research worker** selects eligible directions and spawns `research` tasks
3. Research findings feed into a **strategic cycle**: `design_doc_update` → `strategic_review` → new directions
4. A **starvation handler** creates fallback directions when the pipeline runs low
5. Completed task deduplication prevents re-proposing work that was already done

Configure autonomous mode in the admin UI under Settings > Autonomous.

## Backend Entry Points

- `backend/app/main.py` wires the FastAPI app, middleware, router registration, startup/shutdown hooks, and schema revision checks.
- `backend/app/routers/projects.py` owns the public task/project launch surface under `/api/*`.
- `backend/app/routers/agent.py` and `backend/app/routers/agent_warm_continuity.py` own the agent lifecycle and warm handoff endpoints under `/api/agent/*`.
- `backend/app/routers/admin_*.py` provide the admin dashboard, analytics, settings, diagnostics, and project configuration endpoints under `/api/admin/*` plus a few related internal paths.
- `backend/app/orchestrator.py`, `backend/app/reaper.py`, and `backend/app/services.py` host the long-running background loops.

## Documentation

Comprehensive documentation is available in the `docs/` directory:

### Core Architecture & Design
- **[ARCHITECTURE_OVERVIEW.md](docs/codebase-analysis.md)** — High-level system design and component interactions
- **[BACKEND_MODULES.md](docs/BACKEND_MODULES.md)** — Python module reference for `backend/app/` with responsibilities and key functions

### Task Execution & Lifecycle
- **[TASK_LIFECYCLE.md](docs/TASK_LIFECYCLE.md)** — Task state machine, transitions, and lifecycle callbacks
- **[PAYLOADS_AND_SCHEMAS.md](docs/PAYLOADS_AND_SCHEMAS.md)** — Task payload structure and validation for all task types

### API & Integration
- **[API_REFERENCE.md](docs/API_REFERENCE.md)** — Complete HTTP endpoint reference with request/response schemas
- **[DATA_MODELS.md](docs/DATA_MODELS.md)** — Database schema, Pydantic DTOs, and data structure guide
- **[AGENT_HANDLERS.md](docs/AGENT_HANDLERS.md)** — TypeScript task handler implementations and workflows

### Frontend & UI
- **[FRONTEND_COMPONENTS.md](docs/FRONTEND_COMPONENTS.md)** — React component guide with props and usage patterns

### Operations & Debugging
- **[DEBUG_GUIDE.md](docs/DEBUG_GUIDE.md)** — Troubleshooting failed tasks and interpreting logs
- **[DEVELOPMENT_GUIDE.md](docs/DEVELOPMENT_GUIDE.md)** — Local development setup and workflow
- **[ONBOARDING.md](docs/ONBOARDING.md)** — Bringing a new repository under Blueprint management

### Standards & Guidelines
- **[CLAUDE.md](CLAUDE.md)** — Project-specific coding standards and file restrictions

---

## Development

### Testing

Run tests locally before committing:

```bash
# Backend (requires Docker for Postgres)
bash scripts/test-backend.sh                     # Full suite
bash scripts/test-backend.sh testing/test_foo.py  # Specific file

# Frontend
npm run build
cd admin-ui && npm run test:viewport

# Agent (TypeScript)
cd agent && npm run build && npm run lint
```

Key test invariants:
- Module size guardrails (`backend/testing/module_size_limits.json`) — request permission to bump limits via test assertion message
- Schema revision (`EXPECTED_SCHEMA_REVISION` in `backend/app/main.py`) — must match the latest Alembic migration
- Database migrations are immutable — new migrations must chain from the latest `down_revision`
- Use `./check` for the fast repo gate; use `./check test` for the lightweight backend smoke pass

### Adding Features

1. **Create a branch** from the integration branch (typically `automain`)
2. **Implement changes** following [CLAUDE.md](CLAUDE.md) patterns
3. **Run tests** (`./check` or `bash scripts/test-backend.sh` — see CLAUDE.md for details)
4. **Self-review** — check for correctness, security, test coverage
5. **Push** and open a PR

For substantial changes affecting multiple systems (backend + frontend + agent), open the PR to enable CI. See [Deploy.md](Deploy.md) for deployment.

### Local Development

```bash
# Set up environment
cp .env.example .env
# Edit .env: set LOCAL_MODE=true, ANTHROPIC_API_KEY, ADMIN_API_KEY

# Start services
docker compose up -d db        # PostgreSQL
cd backend && bash run_migrations.sh
cd .. && npm install
npm run dev                    # Starts launcher UI (3000) and admin UI (3001)

# In another terminal, start backend
cd backend
uvicorn app.main:app --reload --port 8080
```

Local mode runs agents as subprocess instead of GCP VMs. Full CI suite runs on PR; `./check` is a quick smoke test.

---

## Safety

- VMs have no public IP — outbound only via Cloud NAT
- Agent service account has zero default GCP permissions
- Short-lived JWTs scoped to individual tasks (2-hour expiry)
- 1-hour hard TTL + heartbeat-based reaper + orphan cleanup
- GitHub tokens refreshed before each push operation
- Prompt injection scanning on PR content (100+ patterns)
- Per-project circuit breaker to halt runaway failure loops

---

## Troubleshooting

For detailed debugging guidance, see **[DEBUG_GUIDE.md](docs/DEBUG_GUIDE.md)**, which covers:
- Common failure categories and how to diagnose them
- Viewing task logs and interpreting error messages
- Understanding failure classification
- GCP VM inspection
- Circuit breaker recovery
- Performance profiling

### Quick Fixes for Common Issues

**Task fails with "clone_failed"**
- Repo is private and agent token lacks access — grant OAuth token in Settings > GitHub
- Repo doesn't exist or URL is wrong — verify `github_owner`/`github_repo` in task
- Invalid GitHub token — reconnect OAuth in Settings

**Task hangs (no logs after 5 minutes)**
- Agent VM unresponsive — reaper kills VMs after 5-minute heartbeat timeout
- Check **Logs** tab in task details for errors or timeouts
- VMs auto-destruct after 1 hour as safety net

**PR creation fails**
- Insufficient permissions — verify GitHub token has `pull_requests: read/write` scope
- Branch doesn't exist — specify correct `base_branch` in task payload
- Network timeout — retry task

**Orchestrator won't start new tasks**
- Circuit breaker tripped (too many failures) — click **Reset** in Project settings
- Concurrency limit reached — increase `MAX_CONCURRENT_VMS` or wait for tasks to complete
- Database unreachable — check `DATABASE_URL` connection string and database health

**Fix loop stuck (task not progressing)**
- Max iterations reached (default 5) — issue may be too complex; try simpler scope
- Agent misunderstood the problem — check review findings in parent task
- Missing tool or dependency — check VM logs, may need to rebuild agent image

**Token expired or invalid**
- GitHub token invalid — Settings > GitHub > **Reconnect**
- Max plan account credentials invalid — Settings > re-authenticate
- Agent JWT expired — rarely happens; restart the task

See **[DEBUG_GUIDE.md](docs/DEBUG_GUIDE.md)** for comprehensive troubleshooting steps and additional scenarios.

---

## Contributing

Contributions are welcome. Please:

1. **Read [CONTRIBUTING.md](CONTRIBUTING.md)** for environment setup and the contribution workflow
2. **Read [CLAUDE.md](CLAUDE.md)** for coding standards and restrictions
3. **Open an issue** for bugs or feature requests
4. **Follow the test guidelines** — all code paths must have tests
5. **Update documentation** when adding new features or APIs

For significant architectural changes, open an issue to discuss approach first.

---

## License

See [LICENSE](LICENSE) file.

---

## Support

For issues, questions, or feedback:
- **GitHub Issues** — bug reports, feature requests
- **Discussions** — questions, ideas, architecture discussions
- **Internal Slack** (if applicable) — for Blueprint team coordination

---

## Related Documentation

- **Deployment** — See [Deploy.md](Deploy.md) for GCP Cloud Run + Terraform setup
- **Database** — See [docs/db-operations.md](docs/db-operations.md) for manual DB queries and operations
- **Deferred DB work** — See [docs/db-deferrals.md](docs/db-deferrals.md) for items the database audit deliberately deferred, with re-evaluation triggers
- **Concurrency** — See [docs/concurrency.md](docs/concurrency.md) for row-locking and async patterns
- **Analysis** — See [docs/codebase-analysis.md](docs/codebase-analysis.md) for cross-cutting architecture overview
