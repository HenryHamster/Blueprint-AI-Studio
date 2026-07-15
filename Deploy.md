# Deployment Guide

## Quick Start (GCP)

```bash
./setup.sh
```

This runs in two phases:

**Phase 1** — Creates GCP infrastructure (VPC, AlloyDB, Cloud Run, IAM), builds + deploys the backend, runs DB migrations, and builds the Packer agent VM image. Prints your Cloud Run URL when done.

**Phase 2** — Prompts for GitHub App credentials and Firebase config, deploys the frontend. Run `./setup.sh --phase2` to re-run just this step.

**Other commands:**
- `./setup.sh -f` — Force rebuild + redeploy the backend (fixes broken Cloud Run revisions)
- `./setup.sh --phase2` — Skip to Phase 2 (instant, no Terraform)

---

## Prerequisites

- GCP project with billing enabled
- `gcloud` CLI authenticated (`gcloud auth login`)
- `terraform` and `packer` installed
- `firebase` CLI (`npm install -g firebase-tools`)
- Node.js 20+

## Required deploy secrets

Before deploying a current backend revision, create these Secret Manager secrets
once. `GITHUB_PAT_ENCRYPTION_KEY` must contain the live Fernet key already used
by the service, and `MIGRATION_APPLY_SCRATCH_DATABASE_URL` must contain the
scratch Postgres URL required by the migration-apply gate. Cloud Run validates
both secret bindings during deployment.

---

## GitHub App Setup

Create at https://github.com/settings/apps/new with these settings:

| Field | Value |
|-------|-------|
| Homepage URL | Your Cloud Run URL (printed after Phase 1) |
| Callback URL | `<CLOUD_RUN_URL>/api/github/oauth/callback` |
| Webhook URL | `<CLOUD_RUN_URL>/api/github/webhook` |
| Webhook secret | Any random string (must match what you give setup.sh) |

**Repository permissions:** Contents (R&W), Pull requests (R&W), Commit statuses (R&W), Issues (R&W), Checks (R)

**Subscribe to events:** Push, Pull request, Check runs

> **Note:** Existing GitHub App installations will need to accept the updated permissions before check_run webhooks are delivered.

After creating, grab: App ID, Client ID, Client Secret, private key .pem file.

---

## After Setup

1. **Grant yourself admin** — in Firebase Console > Authentication, find your UID after first sign-in, then:
   ```bash
   python -c "import firebase_admin; from firebase_admin import auth; firebase_admin.initialize_app(); auth.set_custom_user_claims('YOUR_UID', {'admin': True})"
   ```

2. **Open admin UI** — sign in with Google

3. **Connect GitHub** — Settings gear > "Connect GitHub" (one-time OAuth)

4. **Add Max plan accounts** — Settings gear > paste OAuth token (`sk-ant-oat01-...`) from `claude setup-token`

5. **Onboard a repository** — install the GitHub App on a target repo, then
   create the project. See **[docs/ONBOARDING.md](docs/ONBOARDING.md)** for the
   full per-repository walkthrough (App install + installation-token resolution,
   the onboard endpoint and its auto-derived fields, post-onboard branch/build
   tuning, and approval-gate config).

6. **Launch a task**

7. **Attach a local runner** (optional) — to run tasks on your own machine instead of / in addition to GCP VMs, see **[docs/runner-install.md](docs/runner-install.md)** for token persistence, systemd `--user` service setup, and the restart-on-crash runbook.

---

## Local Development

```bash
# Start database
docker compose up db

# Install deps
npm install && cd agent && npm install && cd ../backend && pip install -r requirements.txt && cd ..

# Run backend (LOCAL_MODE for agent testing)
cd backend
export DATABASE_URL=postgresql+asyncpg://postgres:supersecret_blueprint_pass@localhost:5432/postgres
export AGENT_JWT_SECRET=dev-jwt-secret WS_TICKET_SECRET=dev-ws-secret ADMIN_API_KEY=dev-admin-key
export LOCAL_MODE=true ORCHESTRATOR_URL=http://localhost:8080
export GITHUB_APP_ID=123456 GITHUB_PRIVATE_KEY=dummy GITHUB_WEBHOOK_SECRET=dummy
export GITHUB_CLIENT_ID=dummy GITHUB_CLIENT_SECRET=dummy
bash run_migrations.sh
uvicorn app.main:app --host 0.0.0.0 --port 8080 &
cd ..

# Start frontend (admin-ui :3001)
npm run dev
```

---

## Architecture

- **Backend:** FastAPI on Cloud Run — orchestrates tasks, provisions VMs, serves API
- **Agent VMs:** Ephemeral GCE instances running Claude Code (Packer-built image)
- **Database:** AlloyDB (PostgreSQL) with Alembic migrations
- **Auth:** Firebase Auth (admin UI), JWT (agent VMs), ADMIN_API_KEY (server-to-server)
- **Claude Auth:** Max plan accounts (pooled, load-balanced) with API key fallback
- **Frontend:** Firebase Hosting — admin-ui
