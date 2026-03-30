# Deployment Guide

## Architecture

- **Backend (Orchestrator):** Python/FastAPI on Cloud Run. Manages tasks, provisions agent VMs, serves API.
- **Frontend:** Two apps — root launcher (tldraw canvas) and admin-ui dashboard. Deployed to Firebase Hosting.
- **Agent VMs:** Ephemeral GCE instances running Claude Code. Built with Packer, provisioned on demand.
- **Database:** PostgreSQL (AlloyDB in production, Postgres container locally).
- **Auth:** Firebase Auth for users, JWT for agent VMs, ADMIN_API_KEY for programmatic access.
- **Claude Code Auth:** Max plan accounts (pooled, load-balanced) with API key as fallback.

---

## 1. Local Development

### Prerequisites

- Docker & Docker Compose
- Node.js 20+
- Python 3.11+
- A GitHub App (for repo access — see section 5)

### Start database

```bash
docker compose up db
```

### Run backend natively (required for LOCAL_MODE)

Docker runs the backend in an isolated container where the `agent/` directory isn't available. For local agent testing, run the backend directly:

```bash
cd backend
pip install -r requirements.txt

# Set required env vars (copy from .env.example)
export DATABASE_URL=postgresql+asyncpg://postgres:supersecret_blueprint_pass@localhost:5432/postgres
export AGENT_JWT_SECRET=local_dev_agent_secret
export WS_TICKET_SECRET=local_dev_ws_secret
export ADMIN_API_KEY=dev-admin-key
export LOCAL_MODE=true
export ORCHESTRATOR_URL=http://localhost:8080
export GITHUB_APP_ID=123456
export GITHUB_PRIVATE_KEY=dummy_key
export GITHUB_WEBHOOK_SECRET=dummy_secret

uvicorn app.main:app --host 0.0.0.0 --port 8080
```

### Run database migrations

```bash
cd backend
bash run_migrations.sh
```

### Install and start frontends

```bash
# From repo root
npm install
npm run dev
```

This starts the launcher at `:3000` and admin-ui at `:3001`.

### Prepare the agent (for LOCAL_MODE)

```bash
cd agent
npm install
```

When a task is created with `LOCAL_MODE=true`, the orchestrator spawns the agent as a local subprocess.

### Alternative: Docker-only (no local agent)

If you don't need LOCAL_MODE, docker-compose runs the backend in a container:

```bash
docker compose up --build
```

The backend will be at `http://localhost:8080`. Note: `LOCAL_MODE` won't work in this configuration.

---

## 2. GCP Deployment

### One-command setup

```bash
./setup.sh
```

This handles everything: Terraform, secrets, Docker build, Cloud Run deploy, migrations, Packer image, and frontend. It will prompt for your GCP project ID, GitHub App credentials, and region.

### Prerequisites

- GCP project with billing enabled
- `gcloud` CLI authenticated
- Firebase project linked to the GCP project
- GitHub App configured (see section 5)
- Terraform and Packer installed

### Infrastructure (Terraform)

Provisions VPC, Cloud NAT, IAM, Artifact Registry, Cloud Run, AlloyDB, and GCS bucket:

```bash
cd infra/terraform
terraform init
terraform plan -var="project_id=YOUR_PROJECT_ID" -var="github_repository=your-org/your-repo"
terraform apply -var="project_id=YOUR_PROJECT_ID" -var="github_repository=your-org/your-repo"
```

Note the outputs — you'll need `orchestrator_url`, `artifact_bucket_name`, and `alloydb_ip_address`.

**Post-deploy: Set ORCHESTRATOR_URL** (chicken-and-egg — Cloud Run URL isn't known until first deploy):

```bash
# Get the Cloud Run URL from the first deploy
ORCH_URL=$(terraform output -raw orchestrator_url)

# Re-apply with the URL
terraform apply -var="project_id=YOUR_PROJECT_ID" -var="orchestrator_url=$ORCH_URL"
```

### Create secrets in Secret Manager

```bash
PROJECT_ID=YOUR_PROJECT_ID

# Required secrets
echo -n "postgresql+asyncpg://postgres:PASSWORD@ALLOYDB_IP:5432/postgres" | \
  gcloud secrets create DATABASE_URL --data-file=- --project=$PROJECT_ID

gcloud secrets create AGENT_JWT_SECRET --data-file=<(openssl rand -hex 32) --project=$PROJECT_ID
gcloud secrets create WS_TICKET_SECRET --data-file=<(openssl rand -hex 32) --project=$PROJECT_ID
gcloud secrets create ADMIN_API_KEY --data-file=<(openssl rand -hex 32) --project=$PROJECT_ID

# GitHub App secrets (see section 5)
echo -n "YOUR_APP_ID" | gcloud secrets create GITHUB_APP_ID --data-file=- --project=$PROJECT_ID
gcloud secrets create GITHUB_PRIVATE_KEY --data-file=path/to/private-key.pem --project=$PROJECT_ID
echo -n "YOUR_WEBHOOK_SECRET" | gcloud secrets create GITHUB_WEBHOOK_SECRET --data-file=- --project=$PROJECT_ID
```

### Agent VM Image (Packer)

Build the GCE image with Node.js, Claude Code CLI, and the agent code pre-installed:

```bash
cd infra/packer
export GCP_PROJECT_ID=YOUR_PROJECT_ID
packer init .
packer build agent.pkr.hcl
```

This creates an image in the `blueprint-agent` family.

### Backend (Cloud Run)

**Option A: GitHub Actions (recommended)**

Push to `main` with changes in `backend/`. The workflow at `.github/workflows/deploy.yml` builds, pushes to Artifact Registry, and deploys to Cloud Run using Workload Identity Federation.

Required GitHub secrets:
- `GCP_PROJECT_ID`
- `WIF_PROVIDER_NAME` — Workload Identity Provider resource name (from Terraform output)
- `WIF_SERVICE_ACCOUNT_EMAIL` — SA the runner impersonates (from Terraform output)

**Option B: Cloud Build trigger**

```bash
cd backend
gcloud builds submit --config=cloudbuild.yaml \
  --substitutions=_ALLOWED_ORIGINS="https://your-domain.com",_ORCHESTRATOR_URL="https://blueprint-orchestrator-HASH.run.app"
```

**Option C: Manual deploy**

```bash
cd backend
gcloud run deploy blueprint-orchestrator \
  --source . \
  --region us-central1 \
  --service-account orchestrator-sa@YOUR_PROJECT_ID.iam.gserviceaccount.com
```

### Run migrations in production

```bash
# Connect to AlloyDB via Cloud SQL Proxy or direct VPC access
export DATABASE_URL=postgresql+asyncpg://postgres:PASSWORD@ALLOYDB_IP:5432/postgres
cd backend && alembic upgrade head
```

### Frontend (Firebase Hosting)

Two apps are deployed as separate Firebase Hosting sites:

**First-time setup:**
```bash
firebase hosting:sites:create blueprint-admin
firebase target:apply hosting launcher blueprint-orchestrator
firebase target:apply hosting admin blueprint-admin
```

**Configure admin-ui build vars:**
```bash
# Edit admin-ui/.env.production
VITE_API_URL=https://blueprint-orchestrator-HASH.run.app
VITE_ADMIN_API_KEY=your_admin_api_key
```

**Build & deploy:**
```bash
npm install
npm run build
firebase deploy --only hosting
```

---

## 3. Secrets (Secret Manager)

| Secret | Description |
|--------|-------------|
| `DATABASE_URL` | PostgreSQL connection string (`postgresql+asyncpg://...`) |
| `AGENT_JWT_SECRET` | Signs JWTs for agent VM authentication |
| `WS_TICKET_SECRET` | Signs short-lived WebSocket auth tickets |
| `GITHUB_APP_ID` | GitHub App ID for repo access |
| `GITHUB_PRIVATE_KEY` | GitHub App private key (PEM) |
| `GITHUB_WEBHOOK_SECRET` | GitHub webhook HMAC secret |
| `ANTHROPIC_API_KEY` | *(Optional)* Fallback API key if not using Max plan |
| `ADMIN_API_KEY` | Programmatic admin access (used by admin-ui) |

---

## 4. Environment Variables

Non-secret env vars set on the Cloud Run service:

| Variable | Default | Description |
|----------|---------|-------------|
| `GCP_PROJECT_ID` | — | GCP project ID |
| `GCP_ZONE` | `us-central1-a` | Zone for agent VMs |
| `VPC_NETWORK` | `agent-sandbox-vpc` | VPC for agent VMs |
| `VPC_SUBNET` | `agent-sandbox-subnet` | Subnet for agent VMs |
| `AGENT_SERVICE_ACCOUNT_EMAIL` | — | Restricted SA attached to agent VMs |
| `ORCHESTRATOR_URL` | — | Public URL of the orchestrator (agent VMs call back to this) |
| `ARTIFACT_BUCKET_NAME` | `blueprint-agent-artifacts` | GCS bucket for payloads and credentials |
| `ALLOWED_ORIGINS` | — | CORS origins (comma-separated) |
| `LOCAL_MODE` | `false` | `true` to spawn agents as local subprocesses |
| `MACHINE_TYPE` | `e2-standard-2` | GCE machine type for agent VMs |
| `AGENT_IMAGE_FAMILY` | `blueprint-agent` | GCE image family for agent VMs |
| `MAX_CONCURRENT_VMS` | `5` | Max simultaneous agent VMs (1-30, adjustable via admin UI) |

---

## 5. GitHub App Setup

1. Create a GitHub App at https://github.com/settings/apps/new
2. Set permissions: **Contents** (read & write), **Pull requests** (read & write)
3. Generate a private key and note the App ID
4. Install the app on the repos/orgs you want agents to access
5. Store `GITHUB_APP_ID`, `GITHUB_PRIVATE_KEY`, and `GITHUB_WEBHOOK_SECRET` in Secret Manager
6. Set the webhook URL to `https://YOUR_CLOUD_RUN_URL/api/github/webhook`

---

## 6. Max Plan Account Pool

The system uses Claude Max plan accounts as the primary auth for Claude Code. Multiple accounts can be pooled for load balancing.

### Setup

1. On your machine, log in to each Max plan account:
   ```bash
   claude login
   ```
2. Copy the credentials file:
   ```bash
   cat ~/.claude/credentials.json
   ```
3. In the admin UI (Settings gear icon), paste the credentials JSON and give the account a name
4. Repeat for each account you want in the pool

The orchestrator automatically assigns the account with the fewest active tasks to each new VM. Credentials are uploaded to GCS, downloaded by the VM, then the GCS copy is deleted.

If no Max plan accounts are configured, the system falls back to `ANTHROPIC_API_KEY` if set.

---

## 7. How Agent VMs Work

1. User creates a task via admin-ui (repo + prompt)
2. Orchestrator selects a Max plan account from the pool (least active tasks)
3. Orchestrator uploads credentials to GCS and creates a GCE VM with a startup script
4. VM boots, downloads credentials from GCS (then deletes the GCS copy), starts the agent
5. Agent claims the task, clones the repo, runs Claude Code, pushes a branch, creates a PR
6. Agent reports completion with the PR URL as an artifact
7. Orchestrator releases the Max plan account slot and deletes the VM
8. A 1-hour self-destruct timer on the VM acts as a safety net
