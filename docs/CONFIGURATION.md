# Blueprint Configuration Guide

Complete reference for all environment variables, feature flags, and runtime configuration options.

---

## Table of Contents

1. [Required Configuration](#required-configuration)
2. [GCP Configuration](#gcp-configuration)
3. [Optional Configuration](#optional-configuration)
4. [Feature Flags](#feature-flags)
5. [Performance Tuning](#performance-tuning)
6. [Local Development](#local-development)
7. [Troubleshooting](#troubleshooting)

---

## Required Configuration

These environment variables **must** be set for the system to function.

### Authentication & Secrets

| Variable | Type | Description | Example |
|----------|------|-------------|---------|
| `ADMIN_API_KEY` | string | Shared API key for admin UI and server-to-server requests (min 32 chars) | `sk-blueprint-1234567890abcdef` |
| `AGENT_JWT_SECRET` | string | Secret to sign agent JWT tokens (min 32 chars) | `jwt-secret-1234567890abcdef` |
| `WS_TICKET_SECRET` | string | Secret for WebSocket ticket signing (min 32 chars) | `ws-secret-1234567890abcdef` |
| `ANTHROPIC_API_KEY` | string | Claude API key for agent VMs (or empty if using local Claude login) | `sk-ant-v1-...` |
| `VITE_ADMIN_API_KEY` | string | Frontend env var — **must match `ADMIN_API_KEY`** | Same as `ADMIN_API_KEY` |

### GitHub Configuration

| Variable | Type | Description | Example |
|----------|------|-------------|---------|
| `GITHUB_APP_ID` | string | GitHub App ID | `123456` |
| `GITHUB_PRIVATE_KEY` | string | GitHub App private key (PEM format, URL-encoded) | `-----BEGIN RSA...` |
| `GITHUB_WEBHOOK_SECRET` | string | Secret to verify webhook signatures | `my-webhook-secret` |
| `GITHUB_CLIENT_ID` | string | OAuth app client ID | `abc123...` |
| `GITHUB_CLIENT_SECRET` | string | OAuth app client secret | `xyz789...` |

### Database

| Variable | Type | Description | Example |
|----------|------|-------------|---------|
| `DATABASE_URL` | string | PostgreSQL connection string (async URL format) | `postgresql+asyncpg://user:pass@localhost:5432/blueprint` |

---

## GCP Configuration

Required for production (Cloud Run + Compute Engine).

### Project & Infrastructure

| Variable | Type | Default | Description | Example |
|----------|------|---------|-------------|---------|
| `GCP_PROJECT_ID` | string | — | GCP project ID | `my-blueprint-project` |
| `GCP_ZONE` | string | `us-central1-a` | Primary GCP zone for VMs | `us-central1-c` |
| `MACHINE_TYPE` | string | `e2-standard-2` | GCP machine type (2 vCPU, 8 GB RAM) | `e2-standard-4`, `n2-standard-2` |
| `VPC_NETWORK` | string | `agent-sandbox-vpc` | VPC network name | `custom-vpc-name` |
| `VPC_SUBNET` | string | `agent-sandbox-subnet` | Subnet within VPC | `custom-subnet-name` |
| `AGENT_IMAGE_FAMILY` | string | — | Packer-built agent image path | `projects/my-project/global/images/family/blueprint-agent` |
| `AGENT_SERVICE_ACCOUNT_EMAIL` | string | — | Service account for agent VMs (zero default permissions) | `agent-vm@my-project.iam.gserviceaccount.com` |

### Cloud Run & Networking

| Variable | Type | Default | Description | Example |
|----------|------|---------|-------------|---------|
| `ALLOWED_ORIGINS` | string | `http://localhost:3000,http://localhost:3001` | CORS allowed origins (comma-separated) | `https://example.com,https://admin.example.com` |
| `ORCHESTRATOR_URL` | string | `http://localhost:8080` | URL agents use to reach backend (for multi-region) | `https://orchestrator.example.com` |

---

## Optional Configuration

These variables are optional and have sensible defaults.

### Concurrency & Resource Management

| Variable | Type | Default | Description | Range |
|----------|------|---------|-------------|-------|
| `MAX_CONCURRENT_VMS` | int | `8` | Maximum simultaneous running VMs | 1-100 |
| `MAX_PROVISION_SLOTS` | int | `10` | Maximum concurrent VM provisioning operations | 1-50 |
| `TASK_TIMEOUT_MINUTES` | int | `60` | Hard timeout for any task (minutes) | 10-120 |
| `HEARTBEAT_TIMEOUT_SECONDS` | int | `300` | Agent heartbeat timeout before reaper marks task FAILED (seconds) | 60-600 |

### Token & Authentication Timeouts

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `AGENT_TOKEN_TTL_HOURS` | int | `1` | Standard agent JWT expiry (hours) |
| `AGENT_BOOTSTRAP_TOKEN_TTL_HOURS` | int | `8` | Extended TTL for cold-start agents (hours) |
| `WS_TICKET_EXPIRY_SECONDS` | int | `300` | WebSocket ticket validity (seconds) |

### Rate Limiting

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `RATE_LIMIT_WINDOW` | int | `60` | Rate limit window in seconds |
| `RATE_LIMIT_MAX_REQUESTS` | int | `100` | Maximum requests per window |

### Local Development

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `LOCAL_MODE` | boolean | `false` | Run agents as local subprocesses instead of GCP VMs |
| `DEBUG` | boolean | `false` | Enable debug logging |

### Monitoring & Observability

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `METRICS_TOKEN` | string | — | Token to protect `/metrics` endpoint. When set, requests must include `?token=<value>` matching this value (403 otherwise). When **unset**, `/metrics` is served **without authentication** (fail-open — see security note below). **⚠ Security:** Set this in any production or internet-exposed deployment — the endpoint exposes operational metrics (active VMs, queue depth, error rates) that should not be public. |
| `LOG_LEVEL` | string | `INFO` | Python logging level (DEBUG, INFO, WARNING, ERROR) |
| `ENABLE_PROFILING` | boolean | `false` | Enable Python profiling (use sparingly; performance impact) |

---

## Feature Flags

Runtime toggles for experimental or optional features.

### Autonomous Mode

Configured via the admin UI, but stored as a boolean column:

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `autonomous_enabled` | boolean | `true` | Enable autonomous research task spawning per project |

Disable globally:
```bash
curl -X POST https://orchestrator/api/admin/autonomous/pause-all \
  -H "Authorization: Bearer $ADMIN_API_KEY"
```

### Warm Pool

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `WARM_POOL_ENABLED` | boolean | `false` | Enable warm VM reuse from pre-provisioned pool |
| `WARM_POOL_SIZE` | int | `5` | Number of warm VMs to maintain in the pool |

### Pipeline Toggles

Stored in `runtime_config` (admin UI Settings):

- `circuit_breaker_enabled` — Halt new tasks if failure rate is high
- `auto_remediation_enabled` — Automatically spawn fix tasks for CI failures
- `anomaly_detection_enabled` — Monitor for unusual failure patterns

### Discovery Review Cycles

Cycle lineage and the review freeze are always enforced. Descendant credits do
not gate execution, and there is no shadow admission path.

Once every cohort task and its post-commit processing have settled, exactly one
packet-only `discovery_review` runs at `medium` effort. It returns one of
`advance`, `park`, `stop`, or `ask_operator`; there is no independent
confirmation fan-out. The packet contains a bounded parent outcome receipt with
compact semantic coverage and checksums for detailed bodies omitted from the
packet. Binding decisions must cite that exact receipt and are revalidated
against current cycle, cohort, and the frozen active-direction authority before
application. `advance` atomically records a bounded successor and a deferred
Strategic spawn request. `park`, `stop`, missing evidence, stale authority, or a
failed decision never cancel parallel work. Only actual operator boundaries and
failed-closed states wake the Resident with the exact cycle revision and
resolution context; the Resident does not run a duplicate discovery review.

---

## Performance Tuning

Guidelines for optimizing system performance.

### For Small Deployments (1-5 concurrent tasks)

```bash
MAX_CONCURRENT_VMS=2
MAX_PROVISION_SLOTS=3
MACHINE_TYPE=e2-standard-2
```

### For Medium Deployments (5-20 concurrent tasks)

```bash
MAX_CONCURRENT_VMS=10
MAX_PROVISION_SLOTS=15
MACHINE_TYPE=e2-standard-4      # or n2-standard-2
WARM_POOL_ENABLED=true
WARM_POOL_SIZE=3
```

### For Large Deployments (20+ concurrent tasks)

```bash
MAX_CONCURRENT_VMS=30
MAX_PROVISION_SLOTS=50
MACHINE_TYPE=n2-standard-4
WARM_POOL_ENABLED=true
WARM_POOL_SIZE=10
# Consider multi-region setup with per-region ORCHESTRATOR_URL
```

### Database Tuning

For PostgreSQL, consider:

- **Connection pooling**: `SQLALCHEMY_POOL_SIZE=10`, `SQLALCHEMY_MAX_OVERFLOW=20`
- **Indexes**: Ensure indexes on `(project_id, status)`, `(task_id, status)`, `created_at`
- **Vacuuming**: Auto-vacuum should be enabled; tune if task table grows large

---

## Local Development

### Minimal Setup

```bash
# .env file
export LOCAL_MODE=true
export ANTHROPIC_API_KEY=sk-ant-...
export ADMIN_API_KEY=dev-admin-key
export AGENT_JWT_SECRET=dev-jwt-32-chars-minimum
export WS_TICKET_SECRET=dev-ws-32-chars-minimum
export DATABASE_URL=postgresql+asyncpg://postgres:supersecret_blueprint_pass@localhost:5432/blueprint
export GITHUB_APP_ID=123456
export GITHUB_PRIVATE_KEY='-----BEGIN RSA PRIVATE KEY-----...'
export GITHUB_WEBHOOK_SECRET=dev-webhook-secret
export GITHUB_CLIENT_ID=dev-client-id
export GITHUB_CLIENT_SECRET=dev-client-secret
export ALLOWED_ORIGINS=http://localhost:3000,http://localhost:3001
```

### With Docker Compose

```bash
docker-compose up -d db    # Start PostgreSQL
cd backend && bash run_migrations.sh
cd ..
npm run dev               # Starts frontend (3000) and admin UI (3001)
# In another terminal:
cd backend
uvicorn app.main:app --reload --port 8080
```

### With GCP Emulator (for local GCP testing)

```bash
gcloud emulators firestore start   # or: gcloud emulators compute start (if available)
export GRPC_HOST=localhost:4001    # Point to emulator
```

---

## Troubleshooting

### "No installation token available" during onboarding

**Cause**: Blueprint's GitHub App is not installed on the target repository.

**Fix**:
1. Go to GitHub Settings → Installed GitHub Apps
2. Install "Blueprint" (or your app name)
3. Grant repository access
4. Retry onboarding

### "Cannot connect to database"

**Cause**: `DATABASE_URL` is incorrect or database is not running.

**Diagnosis**:
```bash
psql "$DATABASE_URL"  # or
python -c "from sqlalchemy import create_engine; create_engine('$DATABASE_URL').connect()"
```

**Fix**:
- Verify URL format: `postgresql+asyncpg://user:pass@host:port/dbname`
- Ensure PostgreSQL is running: `docker ps | grep postgres`
- Check credentials match your DB setup

### "Orchestrator loop stuck or not progressing"

**Cause**: Leader election issue or database transaction hung.

**Diagnosis**: Check `/api/debug/orchestrator` endpoint:
```bash
curl -H "Authorization: Bearer $ADMIN_API_KEY" https://orchestrator/api/debug/orchestrator
```

**Fix**:
```bash
# Restart orchestrator without restarting service
curl -X POST https://orchestrator/api/admin/restart-orchestrator \
  -H "Authorization: Bearer $ADMIN_API_KEY"
```

### "VMs fail to provision with quota error"

**Cause**: GCP quota exceeded or zone capacity exhausted.

**Fix**:
1. Check zone health: `/api/admin/zone-health`
2. Add more zones to `FALLBACK_ZONES` in `backend/app/compute.py`
3. Request quota increase from GCP Console
4. Reduce `MAX_CONCURRENT_VMS` as emergency measure

### "Agents can't clone private repos"

**Cause**: GitHub token lacks access or PAT isn't stored.

**Fix**:
1. Admin UI → Settings → Connect GitHub (OAuth flow)
2. Or: Admin UI → Max Plan Accounts → add OAuth token
3. Verify token has `repo` and `read:user` scopes

### Read-only metric retrieval from research/measurement VMs

Research and measurement VMs that run one-shot metric probes (e.g. `GET /api/admin/digest-latency-stats`) may not have `ADMIN_API_KEY` or `~/.blueprint-config.json`. They typically have:

- `ORCHESTRATOR_URL` — the orchestrator's public URL (set at VM launch time)
- `AGENT_SECRET_TOKEN` — the agent JWT minted by the orchestrator for this VM (also present in `/etc/agent.env`)

When `scripts/blueprint_api.py` (the shared ops helper) finds none of the normal admin-credential tiers (config files, `BLUEPRINT_API_URL`+`BLUEPRINT_API_KEY`, `gcloud`), it falls back to this pair automatically. The resulting config is flagged `read_only: True` in the returned dict.

**What this grants:** read access to non-mutating operator-metric endpoints that use the `get_metrics_reader` FastAPI dependency (see `METRICS_READER_ENDPOINTS` comment in `backend/app/auth.py`). Currently: `GET /api/admin/digest-latency-stats`.

**What this does NOT grant:** any admin or mutating access. Calls to endpoints protected by `get_current_user` or `require_admin` will return 403.

**Usage on a measurement VM:**
```bash
# Only ORCHESTRATOR_URL and AGENT_SECRET_TOKEN need to be set (already present in /etc/agent.env)
python3 scripts/blueprint_api.py list_tasks    # will 403 — admin endpoint
# Use the API helper directly for the allowed metrics path:
python3 -c "
from scripts.blueprint_api import api
import json
print(json.dumps(api('GET', '/api/admin/digest-latency-stats'), indent=2))
"
```

**Security note:** `AGENT_SECRET_TOKEN` is the VM's task-scoped JWT. Never log it or echo it. The `scripts/blueprint_api.py` helper never prints the token.

---

## Environment Variable Reference (Alphabetical)

```
ADMIN_API_KEY
AGENT_BOOTSTRAP_TOKEN_TTL_HOURS
AGENT_IMAGE_FAMILY
AGENT_JWT_SECRET
AGENT_SERVICE_ACCOUNT_EMAIL
AGENT_TOKEN_TTL_HOURS
ALLOWED_ORIGINS
ANTHROPIC_API_KEY
DATABASE_URL
DEBUG
ENABLE_PROFILING
GCP_PROJECT_ID
GCP_ZONE
GITHUB_APP_ID
GITHUB_CLIENT_ID
GITHUB_CLIENT_SECRET
GITHUB_PRIVATE_KEY
GITHUB_WEBHOOK_SECRET
HEARTBEAT_TIMEOUT_SECONDS
LOCAL_MODE
LOG_LEVEL
MACHINE_TYPE
MAX_CONCURRENT_VMS
MAX_PROVISION_SLOTS
METRICS_TOKEN
ORCHESTRATOR_URL
RATE_LIMIT_MAX_REQUESTS
RATE_LIMIT_WINDOW
TASK_TIMEOUT_MINUTES
VPC_NETWORK
VPC_SUBNET
VITE_ADMIN_API_KEY
WARM_POOL_ENABLED
WARM_POOL_SIZE
WS_TICKET_EXPIRY_SECONDS
WS_TICKET_SECRET
```
