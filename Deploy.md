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

## Required deploy and migration secrets

Before deploying a current backend revision, create
`GITHUB_PAT_ENCRYPTION_KEY` in Secret Manager with the live Fernet key already
used by the service. The same secret version must be bound to both the
`blueprint-orchestrator` service and the `blueprint-migrate` job. Migration 0609
validates the key before touching rows and refuses to stamp when it is missing
or invalid; `ALLOW_PLAINTEXT_CREDENTIALS` does not override that data-safety
rule. Never rotate an existing key without a separate credential re-encryption
procedure.

`MIGRATION_APPLY_SCRATCH_DATABASE_URL` is different: it is optional,
qualification-only infrastructure and is not a serving prerequisite. When the
Secret Manager object exists, `setup.sh` and `backend/cloudbuild.yaml` bind it
after the replace-all service deploy with `--update-secrets`. When it is absent,
Cloud Run still deploys and FastAPI still starts; release qualification returns
an actionable `migration_apply_check_unavailable` hold until a dedicated
PostgreSQL target whose user can create and drop databases is configured.

For installations managed outside `setup.sh`, bind the optional secret without
replacing the service's required secret list:

```bash
gcloud run services update blueprint-orchestrator \
  --region=us-central1 \
  --update-secrets=MIGRATION_APPLY_SCRATCH_DATABASE_URL=MIGRATION_APPLY_SCRATCH_DATABASE_URL:latest
```

The GitHub Actions deploy workflow updates `blueprint-migrate` with only
`--image` (no `--set-secrets`), which preserves existing secret bindings on
the job. `backend/cloudbuild.yaml` — which runs during the Cloud Build phase
that precedes the migration step — now binds `GITHUB_PAT_ENCRYPTION_KEY` to
the job via `--update-secrets` on every deploy. This guarantees the key is
present when `gcloud run jobs execute blueprint-migrate` fires. For
installations that manage deployments outside Cloud Build (e.g. direct
`gcloud` invocations without `cloudbuild.yaml`), bind the secret manually
before executing the job:

```bash
gcloud run jobs update blueprint-migrate \
  --region=us-central1 \
  --update-secrets=GITHUB_PAT_ENCRYPTION_KEY=GITHUB_PAT_ENCRYPTION_KEY:latest
```

The CI full-chain test job still requires a valid non-production Fernet key
injected at runtime; that is a separate GitHub Actions secret configuration.
The GitHub Actions deploy and CI workflows are managed separately from this
repository change; do not edit or deploy workflow files as part of a migration
hotfix.

## Narrow audited recovery

If qualification itself is broken, first call the read-only
`GET /api/admin/projects/{project_id}/promote/hotfix/preview` endpoint for the
exact candidate SHA. Only an eligible release-machinery-only diff may use
`POST /api/admin/projects/{project_id}/promote/hotfix`, with a non-empty
operator reason, the reviewed `scope_modules`, and
`ack_inert_until_deploy=true`. The lane remains owner-only, audited, CI-gated,
and subject to the merge-time migration-chain backstop.

The hotfix lane never admits migrations, `.github/workflows/**`, product code,
or an `EXPECTED_SCHEMA_REVISION` bump. Those changes must complete the normal
qualification and deploy path. Keep the release held when the preview is
ineligible or unavailable; do not substitute the whole-tree force-promotion
endpoint for this narrow recovery path.

### When integration has moved on (the usual case)

The preview above compares the promotion target against the whole integration
branch, so once integration has accumulated unrelated work since the last
promotion it reports every one of those files as offending — even when the
repair you want is a two-file machinery fix already sitting in that history.
Do not hand-cut a branch for this. Name the repair instead:

```bash
# preview exactly what would ship
GET  /api/admin/projects/{project_id}/promote/hotfix/preview?repair_commit_shas=<full-40-hex-sha>
# ship it
POST /api/admin/projects/{project_id}/promote/hotfix
     {"reason": "...", "scope_modules": [...], "ack_inert_until_deploy": true,
      "repair_commit_shas": ["<full-40-hex-sha>", ...]}
```

Each named commit is diffed against its own first parent, the union of those
diffs goes through the SAME fail-closed allowlist (a named commit that also
touches product code, a migration, a workflow, or `EXPECTED_SCHEMA_REVISION` is
still rejected and nothing is cut), and an eligible repair is materialized as a
new commit on top of the promotion target carrying only those files. SHAs must
be full 40-hex and at most ten may be named. CI is resolved against the commit
the repair was actually built and tested on, and the promotion PR then runs its
own CI on the narrow branch.

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
