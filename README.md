# Blueprint

Blueprint is a platform for launching, monitoring, and scaling autonomous Claude Code instances on sandboxed GCP virtual machines. Assign a repo and a prompt, and Blueprint provisions an isolated VM, runs Claude Code, pushes a branch, and opens a PR.

Blueprint also supports autonomous research mode: the orchestrator can generate and dispatch tasks from configurable strategic directions without manual intervention.

The runtime is split across two main surfaces:

- `backend/` - FastAPI orchestrator and background workers
- `admin-ui/` - React admin dashboard

## Architecture

```
Admin UI (React workspace)
        |
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

- **Orchestrator** (`backend/`) — FastAPI backend deployed to Cloud Run. Manages projects, tasks, VM lifecycle, and autonomous research. It registers the API routers under `backend/app/routers/` and runs background workers for orchestration, reaping, cleanup, digest precompute, secret reconciliation, and pipeline alerts:
  - `orchestrator_worker` — polls for QUEUED tasks every 15 s (configurable via `ORCHESTRATOR_HARD_WATCHDOG_POLL_SECONDS`) and provisions VMs; it also drives autonomous-research spawning each cycle via `_check_idle_research()` (there is no separate autonomous-research worker loop)
  - `reaper_service` — kills VMs that miss heartbeats (5-minute timeout, ~60 s cycle with exponential backoff on errors)
  - `gcp_cleanup_service` — sweeps orphaned VMs every 15 min
  - `secret_reconciliation_service` / `pipeline_alert_scan_service` / `digest_precompute_service` — keep runtime secrets, operator alerts, and digest cache state in sync

- **Admin UI** (`admin-ui/`) — React dashboard for monitoring agents, reviewing directions, configuring settings, and viewing analytics. It also consumes the project WebSocket log stream.

- **Agent** (`agent/`) — TypeScript process that runs inside each VM. Claims its task, clones the repo, runs Claude Code with streaming output, handles the review-fix cycle, and creates a PR.

- **Infrastructure** (`infra/`) — Terraform for VPC, Cloud NAT, Cloud Run, AlloyDB, and IAM. Packer builds the pre-baked agent VM image.

## Quick Start (Local Mode)

Local mode runs agents as subprocesses instead of GCP VMs. You still need PostgreSQL, the backend API, and the admin UI running locally.

```bash
# 1. Copy and fill in environment variables
cp .env.example .env
# Set at minimum: ADMIN_API_KEY, VITE_ADMIN_API_KEY, LOCAL_MODE=true, DATABASE_URL.
# Set ANTHROPIC_API_KEY if you want agents to use the Anthropic API fallback
# instead of a local Claude login / Max-plan account.

# 2. Start PostgreSQL
docker compose up -d db

# 3. Apply backend migrations
cd backend && bash run_migrations.sh

# If you prefer the containerized backend instead of uvicorn, `docker compose up -d`
# starts both `db` and `app`.

# 4. Install backend dependencies and run the API (separate terminal)
cd backend
pip install -r requirements.txt
export DATABASE_URL=postgresql+asyncpg://postgres:supersecret_blueprint_pass@localhost:5432/postgres
export ADMIN_API_KEY=dev-admin-key
export AGENT_JWT_SECRET=dev-jwt-secret
export WS_TICKET_SECRET=dev-ws-ticket-secret
export LOCAL_MODE=true
uvicorn app.main:app --reload --port 8080

# 5. In another terminal, start the UI
npm install
npm run dev

# 6. Open http://localhost:3001 for the admin dashboard
```

## Production Setup (GCP)

See [Deploy.md](Deploy.md) for full Cloud Run + GCP deployment instructions.

```bash
# One-command setup (runs Terraform, builds agent image, deploys backend and frontend)
./setup.sh
```

## Configuration

### Required Environment Variables

| Variable | Description | Example |
|---|---|---|
| `ADMIN_API_KEY` | Shared API key for admin UI and server-to-server requests (must also set `VITE_ADMIN_API_KEY` to same value) | `my-secret-admin-key` |
| `ANTHROPIC_API_KEY` | Claude API key passed to agent VMs for Claude Code (or leave empty if using local Claude login) | `sk-ant-...` |
| `AGENT_JWT_SECRET` | Secret used to sign short-lived agent JWTs (min 32 chars, used internally) | `my-secret-jwt-key` |
| `WS_TICKET_SECRET` | Secret used to mint WebSocket authentication tickets (min 32 chars) | `my-secret-ws-key` |
| `DATABASE_URL` | PostgreSQL connection string: `postgresql+asyncpg://user:pass@host:port/dbname` | See Quick Start section |
| `GITHUB_APP_ID` | GitHub App ID (see Deploy.md section 5 for setup) | `123456` |
| `GITHUB_PRIVATE_KEY` | GitHub App private key in PEM format (multiline, URL-escaped) | `-----BEGIN RSA PRIVATE KEY-----...` |
| `GITHUB_WEBHOOK_SECRET` | Secret used to verify GitHub webhook signatures | `my-webhook-secret` |

### GCP Configuration (Required for Production)

| Variable | Description | Default | Example |
|---|---|---|---|
| `GCP_PROJECT_ID` | GCP project ID for VM provisioning | — | `my-blueprint-project` |
| `GCP_ZONE` | GCP zone for agent VMs | `us-central1-a` | `us-central1-c` |
| `MACHINE_TYPE` | GCP machine type for agent VMs | `e2-standard-2` | `e2-standard-4`, `n2-standard-2` |
| `VPC_NETWORK` | GCP VPC network name for agents | `agent-sandbox-vpc` | custom network name |
| `VPC_SUBNET` | GCP subnet name within VPC | `agent-sandbox-subnet` | custom subnet name |
| `AGENT_IMAGE_FAMILY` | GCP image family path for prebuilt agent image | — | `projects/my-project/global/images/family/blueprint-agent` |
| `AGENT_SERVICE_ACCOUNT_EMAIL` | Service account email for agent VMs (zero default permissions) | — | `vpm-worker@my-project.iam.gserviceaccount.com` |

### Optional Configuration

| Variable | Description | Default |
|---|---|---|
| `LOCAL_MODE` | Run agents as local subprocesses (for development) instead of GCP VMs | `false` |
| `ORCHESTRATOR_URL` | URL agents use to reach the orchestrator backend (for production/multi-region setup) | `http://localhost:8080` |
| `ALLOWED_ORIGINS` | Comma-separated CORS allowed origins (for frontend) | `http://localhost:3000,http://localhost:3001` |
| `MAX_CONCURRENT_VMS` | Maximum simultaneously running agent VMs | `8` |
| `MAX_PROVISION_SLOTS` | Maximum concurrent VM provision operations | `10` |
| `VITE_ADMIN_API_KEY` | Frontend environment variable (must match `ADMIN_API_KEY`) | — |

See `.env.example` for the complete template with all variables.

## How It Works

1. Open the admin UI and create a new task
2. Enter a GitHub repo (`owner/repo`), base branch, and a prompt
3. The orchestrator creates a project and task, then assigns a VM name
4. The orchestrator worker picks up the queued task and provisions a GCP VM, or a local subprocess in `LOCAL_MODE=true`
5. The agent boots and claims its task via the JWT-authenticated API
6. It clones the repo, creates a working branch, and runs Claude Code
7. Logs stream back to the admin UI in real time through a ticket-authenticated WebSocket
8. On success, it commits, pushes, creates a PR, and reports `COMPLETED`
9. On failure, it reports `FAILED` with logs and retries up to 3 times
10. The VM is reaped if it stops heartbeating or exceeds its hard TTL

## Task Types

Blueprint's agent currently has bespoke handlers for 27 task types. The canonical list lives in `agent/src/taskCapabilities.ts`; runtime read/write permissions are loaded from `GET /api/agent/task-capabilities`.

| Type | Description |
|---|---|
| `create` | Implement a new feature from a prompt |
| `refactor` | Refactor existing code |
| `review` | Review a PR and leave comments |
| `fix` | Fix issues identified in a review |
| `verify_tests` | Clone a PR head, run the detected test suite, and post a pass/fail verdict |
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
| `release_audit_challenge` | Independently re-examine release-audit findings and emit structured verdicts |
| `memo` | Produce a structured reasoning artifact without opening a PR |
| `compute_experiment` | Launch a budgeted compute job and monitor it to a terminal state |
| `web_research` | Web-first research that produces a cited knowledge document |
| `design` | Codebase-grounded design work that produces a durable design document |
| `repo_bootstrap` | Inspect a newly onboarded repository and produce bootstrap metadata for setup, build, and environment guidance |

## Autonomous Research Mode

When enabled, Blueprint continuously generates and dispatches tasks without manual intervention:

1. **Strategic directions** define areas of focus (e.g., "Improve test coverage", "Reduce latency")
2. The orchestrator worker's autonomous-research step selects eligible directions and spawns `research` tasks during the normal dispatch loop
3. Research findings feed into a **strategic cycle**: `design_doc_update` → `strategic_review` → new directions
4. A **starvation handler** creates fallback directions when the pipeline runs low
5. Completed task deduplication prevents re-proposing work that was already done

Configure autonomous mode in the admin UI under Settings > Autonomous.

## Backend Entry Points

- `backend/app/main.py` wires the FastAPI app, middleware, router registration, startup/shutdown hooks, and schema revision checks.
- `backend/app/routers/projects.py` owns task/project launch, project listing, and the project WebSocket/status surface under `/api/deploy`, `/api/projects`, and related paths.
- `backend/app/routers/deploy.py` owns admin deploy-state operations under `/api/admin/deploy-state/*`.
- `backend/app/routers/agent.py`, `backend/app/routers/agent_status.py`, `backend/app/routers/agent_credentials.py`, `backend/app/routers/agent_github.py`, `backend/app/routers/agent_strategic_fetch.py`, and `backend/app/routers/agent_warm_continuity.py` own agent lifecycle, auth, and handoff endpoints.
- `backend/app/routers/admin_*.py` provide the admin dashboard, analytics, settings, diagnostics, warm-pool, and project configuration endpoints under `/api/admin/*`.
- `backend/app/orchestrator.py`, `backend/app/reaper.py`, `backend/app/warm_pool_maintainer.py`, and `backend/app/services.py` host the long-running background loops.

## Documentation

Comprehensive documentation is available in the `docs/` directory:

### Core Architecture & Design
- **[codebase-analysis.md](docs/codebase-analysis.md)** — High-level system design, component interactions, and architecture overview
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
- **[CONFIGURATION.md](docs/CONFIGURATION.md)** — Complete environment variable reference and configuration guide
- **[TESTING_GUIDE.md](docs/TESTING_GUIDE.md)** — Testing strategies and best practices for backend, frontend, and agent
- **[DEBUG_GUIDE.md](docs/DEBUG_GUIDE.md)** — Troubleshooting failed tasks and interpreting logs
- **[DEVELOPMENT_GUIDE.md](docs/DEVELOPMENT_GUIDE.md)** — Local development setup and workflow
- **[ONBOARDING.md](docs/ONBOARDING.md)** — Canonical user, project, collaborator, and GitHub onboarding guide

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
- Use `./blueprint status` (or `./blueprint --help`) to query the operator CLI; `./blueprint` is a thin bash wrapper that forwards to `python3 scripts/blueprint.py`. Run `bash scripts/install-cli.sh` once after cloning to symlink `blueprint` and `blueprint-runner` into `~/.local/bin` so they work as bare commands from any directory. On Windows / PowerShell, use `.\blueprint.ps1 status`; the launcher finds the repo virtualenv or an installed Python 3 interpreter.
- Developer-hosted coding agents can use Blueprint's typed, project-scoped, read-only MCP tools for orientation and diagnosis instead of reading API keys or assembling raw REST calls. Blueprint-provisioned task workers keep using their existing task-scoped helpers and token; they are not given a reusable service credential. See [Coding-agent access to Blueprint](docs/CODING_AGENT_ACCESS.md) for credential bootstrap, Codex/stdio configuration, and the CLI fallback. Trusted external operators can instead use the non-owner, revocable [project-operator CLI profile](docs/HERMES_OPERATOR_ACCESS.md), whose exact project mutations remain separate from MCP and platform administration.

### Adding Features

1. **Create a branch** from the project's integration branch (for Blueprint's own repo, this is `automain`; for other projects, check the project settings)
2. **Implement changes** following [CLAUDE.md](CLAUDE.md) patterns
3. **Run tests** (`./check` or `bash scripts/test-backend.sh` — see CLAUDE.md for details)
4. **Self-review** — check for correctness, security, test coverage
5. **Push** and open a PR

For substantial changes affecting multiple systems (backend + frontend + agent), open the PR to enable CI. See [Deploy.md](Deploy.md) for deployment. See [docs/DATA_MODELS.md](docs/DATA_MODELS.md#project) for integration branch configuration details.

### Local Development

```bash
# Set up environment
cp .env.example .env
# Edit .env: set LOCAL_MODE=true, ANTHROPIC_API_KEY, ADMIN_API_KEY

# Start services
docker compose up -d db        # PostgreSQL
cd backend && bash run_migrations.sh
cd .. && npm install
npm run dev                    # Starts admin UI (3001)

# In another terminal, start backend
cd backend
uvicorn app.main:app --reload --port 8080
```

Local mode runs agents as subprocess instead of GCP VMs. Full CI suite runs on PR; `./check` is a quick smoke test.

---

## Safety

- VMs have no public IP — outbound only via Cloud NAT
- Agent service account has zero default GCP permissions
- Short-lived JWTs scoped to individual tasks (1-hour standard expiry; 8-hour bootstrap token for cold starts)
- Task timeout and VM self-destruct safeguards plus heartbeat-based reaper and orphan cleanup
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
- Circuit breaker tripped (too many failures) — click the **Autonomy** indicator in the top bar to expand the status popover, then click **Reset Circuit Breaker**
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
