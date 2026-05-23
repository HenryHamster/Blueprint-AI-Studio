# Blueprint — Claude Code PVM Launcher

A platform for launching, monitoring, and scaling autonomous Claude Code instances on sandboxed GCP Virtual Machines. Assign a repo and a prompt, and Blueprint spins up an isolated VM, runs Claude Code, pushes a branch, and opens a PR.

Blueprint also supports **autonomous research mode**: a continuous loop where the orchestrator automatically generates and dispatches tasks based on configurable strategic directions, without manual intervention.

## Architecture

```
Launcher UI (React)   Admin UI (React)
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

- **Orchestrator** (`backend/`) — FastAPI backend deployed to Cloud Run. Manages projects, tasks, VM lifecycle, and autonomous research. Runs background workers:
  - `orchestrator_worker` — polls for QUEUED tasks every 10 s and provisions VMs
  - `reaper_service` — kills VMs that miss heartbeats (5-minute timeout, 30 s cadence)
  - `gcp_cleanup_service` — sweeps orphaned VMs every 15 min
  - `autonomous_research_worker` — spawns research tasks based on strategic directions

- **Admin UI** (`admin-ui/`) — React dashboard for monitoring agents, reviewing directions, configuring settings, and viewing analytics. Real-time log streaming via WebSocket.

- **Launcher UI** (root `src/`) — Lighter React frontend for creating new tasks and viewing progress. Runs on port 3000.

- **Agent** (`agent/`) — TypeScript process that runs inside each VM. Claims its task, clones the repo, runs `claude -p` with streaming output, handles the review-fix cycle, and creates a PR.

- **Infrastructure** (`infra/`) — Terraform for VPC, Cloud NAT, Cloud Run, AlloyDB, IAM. Packer for pre-baked agent VM images with Claude Code CLI.

## Quick Start (Local Mode)

Local mode runs agents as subprocesses instead of GCP VMs — no cloud setup needed for testing.

```bash
# 1. Copy and fill in environment variables
cp .env.example .env
# Set at minimum: ANTHROPIC_API_KEY, ADMIN_API_KEY, LOCAL_MODE=true

# 2. Start backend + database
docker-compose up -d

# 3. Start UIs (launcher on :3000, admin on :3001)
npm install
npm run dev

# 4. Open http://localhost:3001 and launch a task
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
| `LOCAL_MODE` | `true` to run agents as local subprocesses |
| `DATABASE_URL` | PostgreSQL connection string (asyncpg) |
| `GCP_PROJECT_ID` | GCP project for VM provisioning |
| `ORCHESTRATOR_URL` | URL agents use to reach the orchestrator |
| `MAX_CONCURRENT_VMS` | Maximum simultaneously running agent VMs (default 8) |
| `MAX_PROVISION_SLOTS` | Maximum concurrent VM provision operations (default 10) |

See `.env.example` for the full list.

## How It Works

1. Open the admin UI and click **Launch Task**
2. Enter a GitHub repo (`owner/repo`), base branch, and a prompt
3. The orchestrator creates a project + task, assigns a VM name
4. The orchestrator worker picks up the task and provisions a GCP VM (or local subprocess)
5. The agent boots, claims its task via JWT-authenticated API
6. It clones the repo, creates a working branch, and runs Claude Code
7. Logs stream back to the admin UI in real-time via WebSocket
8. On success: commits, pushes, creates a PR, reports COMPLETED
9. On failure: reports FAILED with error logs, retries up to 3 times
10. The VM self-destructs (1-hour hard TTL as safety net)

## Task Types

Blueprint supports 17 task types, each handled by a dedicated agent handler:

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

## Autonomous Research Mode

When enabled, Blueprint continuously generates and dispatches tasks without manual intervention:

1. **Strategic directions** define areas of focus (e.g., "Improve test coverage", "Reduce latency")
2. The **autonomous research worker** selects eligible directions and spawns `research` tasks
3. Research findings feed into a **strategic cycle**: `design_doc_update` → `strategic_review` → new directions
4. A **starvation handler** creates fallback directions when the pipeline runs low
5. Completed task deduplication prevents re-proposing work that was already done

Configure autonomous mode in the admin UI under Settings > Autonomous.

## Documentation

Comprehensive documentation is available in the `docs/` directory:

- **[API_REFERENCE.md](docs/API_REFERENCE.md)** — Complete HTTP endpoint reference with request/response schemas
- **[DATA_MODELS.md](docs/DATA_MODELS.md)** — Database schema, Pydantic DTOs, and data structure guide
- **[BACKEND_MODULES.md](docs/BACKEND_MODULES.md)** — Python module reference for `backend/app/` with responsibilities and key functions
- **[AGENT_HANDLERS.md](docs/AGENT_HANDLERS.md)** — TypeScript task handler implementations and workflows
- **[FRONTEND_COMPONENTS.md](docs/FRONTEND_COMPONENTS.md)** — React component guide with props and usage patterns
- **[CLAUDE.md](CLAUDE.md)** — Project-specific coding standards and file restrictions

---

## Development

### Testing

Run tests locally before committing:

```bash
# Backend (requires Docker for Postgres)
bash scripts/test-backend.sh                    # Full suite
bash scripts/test-backend.sh testing/test_foo.py  # Specific file

# Frontend (linting + unit tests)
cd admin-ui && npm run build && npx eslint .

# Agent (TypeScript)
cd agent && npx tsc --noEmit
```

Key test invariants:
- Module size guardrails (`backend/testing/module_size_limits.json`) — request permission to bump limits via test assertion message
- Schema revision (`EXPECTED_SCHEMA_REVISION` in `backend/app/main.py`) — must match latest Alembic migration
- Database migrations are immutable — new migrations must chain from the latest `down_revision`

### Adding Features

1. **Create a branch** from `main`
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

### Task fails with "clone_failed"

- Repo is private and agent token lacks access — grant OAuth token in admin settings
- Repo doesn't exist or URL is wrong — check `github_owner`/`github_repo` in task payload
- Network issue (rare in production) — reaper will auto-retry up to 3 times

### Task hangs (no logs after 5 minutes)

- Agent VM hung or network partitioned — reaper kills VMs after 5-minute heartbeat timeout
- Check agent logs in admin UI for the task — look for errors, timeouts, or blocked operations
- In production, VMs auto-destruct after 1 hour as safety net

### PR creation fails

- Agent doesn't have permission to create PRs in this repo — check OAuth token scopes (need `pull_requests: read/write`)
- PR title/body too long or contains unprintable characters — agent sanitizes before submission
- Target branch doesn't exist — specify correct `base_branch` in task

### Orchestrator won't start new tasks

- Circuit breaker tripped due to repeated failures — check project status in admin UI
- `MAX_CONCURRENT_VMS` limit reached — reduce concurrency or increase limit
- Database unreachable — check `DATABASE_URL` connection string

### Memory/CPU limits

- Default GCP VM is `e2-standard-2` (2 vCPU, 8 GB RAM) — sufficient for most tasks
- Very large repos or heavy analysis may require larger machine — set `MACHINE_TYPE` in `.env`
- In local mode, agent runs as subprocess — limited by host machine resources

---

## Contributing

Contributions are welcome. Please:

1. **Read [CLAUDE.md](CLAUDE.md)** for coding standards and restrictions
2. **Open an issue** for bugs or feature requests
3. **Follow the test guidelines** — all code paths must have tests
4. **Update documentation** when adding new features or APIs

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
- **Concurrency** — See [docs/concurrency.md](docs/concurrency.md) for row-locking and async patterns
- **Analysis** — See [docs/codebase-analysis.md](docs/codebase-analysis.md) for cross-cutting architecture overview
