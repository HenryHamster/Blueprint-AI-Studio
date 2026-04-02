# Blueprint — Claude Code PVM Launcher

A personal tool for launching, monitoring, and scaling autonomous Claude Code instances on sandboxed GCP Virtual Machines. Assign a repo and a prompt, and Blueprint spins up an isolated VM, runs Claude Code, pushes a branch, and opens a PR.

## Architecture

```
Admin UI (React)  -->  Orchestrator (FastAPI/Cloud Run)  -->  Agent VMs (GCP Compute Engine)
     |                        |                                      |
  Launch tasks           Queue & provision                    Run Claude Code CLI
  View live logs         Manage lifecycle                     Clone repo, push PR
  Monitor costs          Reap orphans                         Self-destruct on completion
```

### Components

- **Orchestrator** (`backend/`) — FastAPI backend deployed to Cloud Run. Manages projects, tasks, and VM lifecycle. Runs three background workers:
  - `orchestrator_worker` — polls for QUEUED tasks, provisions VMs
  - `reaper_service` — kills VMs that miss heartbeats (5 min timeout)
  - `gcp_cleanup_service` — sweeps orphaned VMs every 15 min

- **Admin UI** (`admin-ui/`) — React dashboard for launching tasks and monitoring agents. Real-time log streaming via WebSocket.

- **Agent** (`agent/`) — TypeScript process that runs inside each VM. Claims its task, clones the repo, runs `claude -p` with streaming output, commits changes, and creates a PR.

- **Infrastructure** (`infra/`) — Terraform for VPC, Cloud NAT, Cloud Run, AlloyDB, IAM. Packer for pre-baked agent VM images with Claude Code CLI.

## Quick Start (Local Mode)

Local mode runs agents as subprocesses instead of GCP VMs — no cloud setup needed for testing.

```bash
# 1. Copy and fill in environment variables
cp .env.example .env
# Set at minimum: ANTHROPIC_API_KEY, ADMIN_API_KEY, LOCAL_MODE=true

# 2. Start backend + database
docker-compose up -d

# 3. Start admin UI
cd admin-ui && npm run dev

# 4. Open http://localhost:3001 and launch a task
```

## Production Setup (GCP)

See [Deploy.md](Deploy.md) for full Cloud Run + GCP deployment instructions.

```bash
# Build agent VM image
cd infra/packer && ./build.sh

# Deploy infrastructure
cd infra/terraform && terraform apply

# Deploy backend
cd backend && gcloud builds submit
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

See `.env.example` for the full list.

## How It Works

1. You open the admin UI and click **Launch Task**
2. Enter a GitHub repo (`owner/repo`), base branch, and a prompt
3. The orchestrator creates a project + task, assigns a VM name
4. The orchestrator worker picks up the task and provisions a GCP VM (or local subprocess)
5. The agent boots, claims its task via JWT-authenticated API
6. It clones the repo, creates a working branch, and runs Claude Code
7. Logs stream back to the admin UI in real-time via WebSocket
8. On success: commits, pushes, creates a PR, reports COMPLETED
9. On failure: reports FAILED with error logs, retries up to 3 times
10. The VM self-destructs (1-hour hard TTL as safety net)

## Safety

- VMs have no public IP — outbound only via Cloud NAT
- Agent service account has zero default GCP permissions
- Short-lived JWTs scoped to individual tasks
- 1-hour hard TTL + heartbeat-based reaper + orphan cleanup
- GitHub tokens refreshed before each push operation

## Changelog

### 2026-04-01
- Security and robustness audit completed with 23 fixes applied
