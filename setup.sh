#!/bin/bash
set -euo pipefail

# ─────────────────────────────────────────────────────────────────────────────
# Blueprint Orchestrator — GCP Setup
#
# Usage:
#   ./setup.sh            Full setup (infrastructure + backend + frontend)
#   ./setup.sh -f         Force rebuild + deploy backend (fixes broken Cloud Run)
#   ./setup.sh --phase2   Skip to Phase 2 (GitHub App + frontend)
# ─────────────────────────────────────────────────────────────────────────────

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; DIM='\033[2m'; NC='\033[0m'
info()  { echo -e "${CYAN}[INFO]${NC} $1"; }
ok()    { echo -e "${GREEN}[OK]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
fail()  { echo -e "${RED}[FAIL]${NC} $1"; exit 1; }

CONFIG_FILE=".setup-config"

# Save/load config so re-runs are instant (no Terraform state reads)
save_config() {
  cat > "$CONFIG_FILE" <<EOF
PROJECT_ID=$PROJECT_ID
REGION=$REGION
ZONE=$ZONE
ORCHESTRATOR_URL=$ORCHESTRATOR_URL
ORCHESTRATOR_SA=$ORCHESTRATOR_SA
ARTIFACT_BUCKET=$ARTIFACT_BUCKET
DB_IP=$DB_IP
DB_PW_SECRET=$DB_PW_SECRET
ADMIN_API_KEY=$ADMIN_API_KEY
ADMIN_EMAIL=${ADMIN_EMAIL:-}
ALERT_EMAIL=${ALERT_EMAIL:-}
BILLING_ACCOUNT_ID=${BILLING_ACCOUNT_ID:-}
EOF
}

load_config() {
  if [ -f "$CONFIG_FILE" ]; then
    source "$CONFIG_FILE"
    # Backward compatibility for .setup-config files written before alloydb.tf
    # was archived (when both AlloyDB and Cloud SQL paths existed).
    DB_IP="${DB_IP:-${ALLOYDB_IP:-}}"
    DB_PW_SECRET="${DB_PW_SECRET:-${ALLOYDB_PW_SECRET:-}}"
    return 0
  fi
  return 1
}

# ─── Force rebuild mode ────────────────────────────────────────────────────
if [ "${1:-}" = "-f" ]; then
  load_config || fail "No previous config found. Run ./setup.sh first."
  info "Force rebuilding and deploying backend..."
  cd backend
  # Wave F4 M6: pass _INITIAL_ADMIN_EMAIL through to cloudbuild.yaml
  # so the operator email is no longer hardcoded.
  gcloud builds submit --config=cloudbuild.yaml \
    --substitutions="_ALLOWED_ORIGINS=${ALLOWED_ORIGINS:-http://localhost:3000},_ORCHESTRATOR_URL=$ORCHESTRATOR_URL,_INITIAL_ADMIN_EMAIL=${ADMIN_EMAIL:-}" \
    --project="$PROJECT_ID"
  cd ..
  ok "Backend redeployed to $ORCHESTRATOR_URL"
  exit 0
fi

# ─── Phase 2 only mode ────────────────────────────────────────────────────
if [ "${1:-}" = "--phase2" ]; then
  load_config || fail "No previous config found. Run ./setup.sh first."
  info "Loaded config: $PROJECT_ID / $ORCHESTRATOR_URL"
  gcloud config set project "$PROJECT_ID" --quiet
  # Jump to Phase 2 (defined as a function below)
  SKIP_TO_PHASE2=true
fi

# ─── Phase 2 function ─────────────────────────────────────────────────────
run_phase2() {
  echo ""
  echo -e "${YELLOW}── Phase 2: GitHub App + Frontend ──${NC}"
  echo ""

  read -p "GitHub App ID: " GITHUB_APP_ID
  read -p "GitHub Client ID: " GITHUB_CLIENT_ID
  read -s -p "GitHub Client Secret: " GITHUB_CLIENT_SECRET
  echo ""
  read -r -p "GitHub App private key .pem file path: " GITHUB_KEY_FILE
  # Normalize Windows backslashes to forward slashes and strip quotes
  GITHUB_KEY_FILE=$(echo "$GITHUB_KEY_FILE" | tr '\\' '/' | tr -d '"')
  [ -f "$GITHUB_KEY_FILE" ] || fail "File not found: $GITHUB_KEY_FILE"
  read -p "GitHub webhook secret: " GITHUB_WEBHOOK_SECRET

  info "Creating GitHub secrets..."

  set_secret() {
    local name=$1 value=$2
    if gcloud secrets describe "$name" --project="$PROJECT_ID" &>/dev/null; then
      echo -n "$value" | gcloud secrets versions add "$name" --data-file=- --project="$PROJECT_ID" --quiet
    else
      echo -n "$value" | gcloud secrets create "$name" --data-file=- --project="$PROJECT_ID" --quiet
    fi
    # Wave F4 M2: per-secret IAM binding for orchestrator-sa removed.
    # iam.tf already grants `roles/secretmanager.admin` (and
    # `roles/secretmanager.secretAccessor`) project-wide on
    # orchestrator-sa, so per-secret secretAccessor bindings are
    # redundant. Migrator-sa still has per-secret bindings (see
    # iam.tf migrator_*_password_access) — those stay scoped.
  }

  set_secret "GITHUB_APP_ID" "$GITHUB_APP_ID"
  set_secret "GITHUB_CLIENT_ID" "$GITHUB_CLIENT_ID"
  set_secret "GITHUB_CLIENT_SECRET" "$GITHUB_CLIENT_SECRET"
  set_secret "GITHUB_PRIVATE_KEY" "$(cat "$GITHUB_KEY_FILE")"
  set_secret "GITHUB_WEBHOOK_SECRET" "$GITHUB_WEBHOOK_SECRET"
  ok "GitHub secrets created"

  # Re-deploy backend so it picks up the new secrets
  info "Re-deploying backend with GitHub secrets..."
  cd backend
  # Wave F4 M6: pass _INITIAL_ADMIN_EMAIL through to cloudbuild.yaml.
  gcloud builds submit --config=cloudbuild.yaml \
    --substitutions="_ALLOWED_ORIGINS=${ALLOWED_ORIGINS:-http://localhost:3000},_ORCHESTRATOR_URL=$ORCHESTRATOR_URL,_INITIAL_ADMIN_EMAIL=${ADMIN_EMAIL:-}" \
    --project="$PROJECT_ID" --quiet
  cd ..
  ok "Backend re-deployed"

  # ─── Frontend ─────────────────────────────────────────────────────────────

  echo ""
  echo -e "${DIM}Firebase config — needed for admin UI login. Get from Firebase Console > Project settings.${NC}"
  read -p "Firebase API key: " FIREBASE_API_KEY
  read -p "Firebase Auth domain [${PROJECT_ID}.firebaseapp.com]: " FIREBASE_AUTH_DOMAIN
  FIREBASE_AUTH_DOMAIN=${FIREBASE_AUTH_DOMAIN:-${PROJECT_ID}.firebaseapp.com}

  info "Building frontends..."
  cat > admin-ui/.env.production <<EOF
VITE_API_URL=$ORCHESTRATOR_URL
VITE_FIREBASE_API_KEY=$FIREBASE_API_KEY
VITE_FIREBASE_AUTH_DOMAIN=$FIREBASE_AUTH_DOMAIN
VITE_FIREBASE_PROJECT_ID=$PROJECT_ID
EOF

  npm install --quiet
  npm run build
  ok "Frontends built"

  if command -v firebase &>/dev/null; then
    info "Deploying to Firebase Hosting..."
    firebase hosting:sites:create blueprint-admin --project="$PROJECT_ID" 2>/dev/null || true
    firebase target:apply hosting launcher "$PROJECT_ID" 2>/dev/null || true
    firebase target:apply hosting admin blueprint-admin 2>/dev/null || true
    firebase deploy --only hosting --project="$PROJECT_ID"
    ok "Frontend deployed"
  else
    warn "Firebase CLI not found. Run 'npm install -g firebase-tools' then 'firebase deploy --only hosting'"
  fi

  echo ""
  echo -e "${GREEN}╔══════════════════════════════════════════════════════════════╗${NC}"
  echo -e "${GREEN}║                    Setup Complete!                            ║${NC}"
  echo -e "${GREEN}╚══════════════════════════════════════════════════════════════╝${NC}"
  echo ""
  echo -e "  Orchestrator:  ${CYAN}$ORCHESTRATOR_URL${NC}"
  echo -e "  Admin API Key: ${CYAN}$ADMIN_API_KEY${NC}"
  echo ""
  echo -e "  ${YELLOW}Final steps:${NC}"
  echo -e "  1. Grant yourself admin: firebase auth:set-custom-user-claims <UID> '{\"admin\":true}'"
  echo -e "  2. Open the admin UI → sign in → Settings → Connect GitHub"
  echo -e "  3. Settings → Add Max plan accounts (paste OAuth token from 'claude setup-token')"
  echo -e "  4. Launch your first task!"
  echo ""
}

# If --phase2, run it and exit
if [ "${SKIP_TO_PHASE2:-}" = "true" ]; then
  run_phase2
  exit 0
fi

# ═════════════════════════════════════════════════════════════════════════════
# PHASE 1: Infrastructure + Backend
# ═════════════════════════════════════════════════════════════════════════════

echo ""
echo -e "${CYAN}╔══════════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║       Blueprint Orchestrator — GCP Setup         ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "${YELLOW}── Phase 1: Infrastructure ──${NC}"
echo ""

SKIP_TERRAFORM=""

if load_config && [ -n "$ORCHESTRATOR_URL" ]; then
  echo -e "${DIM}Detected existing deployment: $PROJECT_ID ($ORCHESTRATOR_URL)${NC}"
  read -p "Use existing config? [Y/n]: " USE_EXISTING
  USE_EXISTING=${USE_EXISTING:-Y}
  if [[ "$USE_EXISTING" =~ ^[Yy] ]]; then
    info "Using existing config: $PROJECT_ID / $ORCHESTRATOR_URL"
    gcloud config set project "$PROJECT_ID" --quiet
    SKIP_TERRAFORM=true
  fi
fi

if [ -z "$SKIP_TERRAFORM" ]; then
  read -p "GCP Project ID: " PROJECT_ID
  [ -n "$PROJECT_ID" ] || fail "GCP Project ID is required"

  echo ""
  echo -e "${DIM}Press Enter to accept defaults for optional fields.${NC}"
  read -p "GCP Region [us-central1]: " REGION
  REGION=${REGION:-us-central1}
  ZONE="${REGION}-a"
  GITHUB_REPO="skip"

  # Wave F4 M6: INITIAL_ADMIN_EMAIL is read from operator input rather than
  # hardcoded in cloudbuild.yaml. The orchestrator uses this value to seed
  # the first AllowedUser row at boot so the operator can sign into the
  # admin UI without an existing admin to grant access.
  read -p "Admin email (will be seeded as first project admin) [optional, can skip]: " ADMIN_EMAIL
  ADMIN_EMAIL=${ADMIN_EMAIL:-}

  # ALERT_EMAIL drives Cloud Monitoring notification channel (used by Cloud SQL
  # CPU/disk/connections/backup-failure alerts AND by budget.tf's monthly
  # spend ceiling). Empty string disables both — keep that escape hatch for
  # operators who explicitly want no alerting.
  read -p "Alert email (Cloud Monitoring + budget alerts) [optional, can skip]: " ALERT_EMAIL
  ALERT_EMAIL=${ALERT_EMAIL:-}

  # BILLING_ACCOUNT_ID is required by budget.tf (budget alert at 50/90/100%
  # of $200/mo). Find via `gcloud billing accounts list`. Empty string
  # disables the budget resource (alerts still work if ALERT_EMAIL is set).
  if [ -n "$ALERT_EMAIL" ]; then
    read -p "GCP billing account ID (e.g. '01ABCD-234567-EFGH89') for budget alert [optional]: " BILLING_ACCOUNT_ID
    BILLING_ACCOUNT_ID=${BILLING_ACCOUNT_ID:-}
  else
    BILLING_ACCOUNT_ID=""
  fi

  echo ""
  info "Setting GCP project to $PROJECT_ID..."
  gcloud config set project "$PROJECT_ID" --quiet
  gcloud auth application-default set-quota-project "$PROJECT_ID" --quiet 2>/dev/null || true

  # ─── Enable APIs ──────────────────────────────────────────────────────────

  info "Enabling required GCP APIs..."
  # alloydb.googleapis.com removed 2026-05-02 (alloydb.tf archived). Re-add to
  # this list AND restore the alloydb resource files if scaling back to AlloyDB
  # — see infra/terraform/archive/README.md.
  # cloudbilling + billingbudgets enable budget.tf's google_billing_budget alert.
  gcloud services enable \
    compute.googleapis.com \
    run.googleapis.com \
    cloudbuild.googleapis.com \
    secretmanager.googleapis.com \
    artifactregistry.googleapis.com \
    sqladmin.googleapis.com \
    servicenetworking.googleapis.com \
    iam.googleapis.com \
    iamcredentials.googleapis.com \
    cloudbilling.googleapis.com \
    billingbudgets.googleapis.com \
    --quiet
  ok "APIs enabled"

  # ─── Quota check ──────────────────────────────────────────────────────────
  # MAX_CONCURRENT_VMS=10 (cloudbuild.yaml) × 2 vCPU per e2-standard-2 = 20 vCPU
  # needed in the primary region. Default new-project CPUS quota is 8 — would
  # cap concurrent agents at 4 with QUOTA_EXCEEDED on further provision attempts.
  CPUS_QUOTA=$(gcloud compute regions describe "$REGION" --project="$PROJECT_ID" --format="value(quotas[metric=CPUS].limit)" 2>/dev/null | head -1)
  if [ -n "$CPUS_QUOTA" ] && awk -v q="$CPUS_QUOTA" 'BEGIN{exit !(q < 20)}'; then
    warn "CPUS quota in $REGION is $CPUS_QUOTA vCPU; MAX_CONCURRENT_VMS=10 needs ≥20."
    warn "Provision attempts past your quota will hit QUOTA_EXCEEDED."
    warn "Request an increase: https://console.cloud.google.com/iam-admin/quotas?project=$PROJECT_ID"
    warn "Continuing anyway — you can apply now and request the bump in parallel."
  fi

  # ─── Terraform ────────────────────────────────────────────────────────────

  info "Running Terraform..."

  # GCS state bucket: bootstrap before init since provider.tf declares a GCS
  # backend. Bucket name follows the convention <project>-tf-state. Idempotent
  # — `gsutil mb` errors if the bucket exists, and we tolerate that.
  TF_STATE_BUCKET="${PROJECT_ID}-tf-state"
  if ! gsutil ls -b "gs://${TF_STATE_BUCKET}" >/dev/null 2>&1; then
    info "Creating Terraform state bucket gs://${TF_STATE_BUCKET}..."
    gsutil mb -p "$PROJECT_ID" -l "$REGION" "gs://${TF_STATE_BUCKET}" || fail "Failed to create state bucket"
    gsutil versioning set on "gs://${TF_STATE_BUCKET}" >/dev/null 2>&1 || warn "Could not enable versioning on state bucket"
  fi

  cd infra/terraform

  terraform init -input=false -backend-config="bucket=${TF_STATE_BUCKET}"

  # use_cloud_sql defaults true in variables.tf since alloydb.tf was archived.
  TF_VARS="-var=project_id=$PROJECT_ID -var=region=$REGION -var=zone=$ZONE"
  [ -n "$ALERT_EMAIL" ] && TF_VARS="$TF_VARS -var=alert_email=$ALERT_EMAIL"
  [ -n "$BILLING_ACCOUNT_ID" ] && TF_VARS="$TF_VARS -var=billing_account_id=$BILLING_ACCOUNT_ID"

  terraform apply -auto-approve $TF_VARS || warn "Terraform apply had errors (may be OK on re-run, continuing...)"

  ORCHESTRATOR_URL=$(terraform output -raw orchestrator_url 2>/dev/null || echo "")
  ARTIFACT_BUCKET=$(terraform output -raw artifact_bucket_name 2>/dev/null || echo "")
  ORCHESTRATOR_SA=$(terraform output -raw orchestrator_service_account 2>/dev/null || echo "")
  DB_IP=$(terraform output -raw cloud_sql_ip_address 2>/dev/null || echo "")
  DB_PW_SECRET=$(terraform output -raw cloud_sql_password_secret_id 2>/dev/null || echo "")
  echo "Using Cloud SQL at $DB_IP"

  if [ -z "$ORCHESTRATOR_URL" ]; then
    fail "Could not read orchestrator_url from Terraform outputs."
  fi

  ok "Infrastructure ready (URL: $ORCHESTRATOR_URL)"
  cd ../..
fi

# ─── Core Secrets ───────────────────────────────────────────────────────────

info "Setting up secrets in Secret Manager..."

urlencode() {
  # R3-W4 M7 (audit 2026-04-30): pipe via stdin instead of argv. The
  # cleartext password used to appear in /proc/<pid>/cmdline for the
  # python process lifetime, visible to any local user with /proc read
  # access. Reading from stdin keeps the secret off argv entirely.
  # `rstrip("\n")` strips the trailing newline that bash <<< adds.
  #
  # Use whichever python is available — on Windows `python3` doesn't
  # exist by default; the binary is just `python`. Falls back through
  # the common names so this works on Linux + Mac + Git-Bash.
  local py_bin
  if command -v python3 >/dev/null 2>&1; then py_bin=python3
  elif command -v python >/dev/null 2>&1; then py_bin=python
  elif command -v py >/dev/null 2>&1; then py_bin=py
  else fail "No python interpreter found (tried python3, python, py)"
  fi
  "$py_bin" -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.stdin.read().rstrip("\n"), safe=""))' <<< "$1"
}

DB_PASSWORD=$(gcloud secrets versions access latest --secret="$DB_PW_SECRET" --project="$PROJECT_ID")
ENCODED_DB_PASSWORD=$(urlencode "$DB_PASSWORD")
DATABASE_URL="postgresql+asyncpg://postgres:${ENCODED_DB_PASSWORD}@${DB_IP}:5432/postgres?ssl=require"
DB_PASSWORD=""
ENCODED_DB_PASSWORD=""

create_secret_if_missing() {
  local name=$1 value=$2
  if gcloud secrets describe "$name" --project="$PROJECT_ID" &>/dev/null; then
    echo "  $name already exists, skipping"
  else
    echo -n "$value" | gcloud secrets create "$name" --data-file=- --project="$PROJECT_ID" --quiet
    echo "  $name created"
  fi
  # Wave F4 M2: per-secret IAM binding for orchestrator-sa removed.
  # See `set_secret` above and iam.tf's project-wide grants — orchestrator-sa
  # already has secretmanager.admin + secretAccessor at the project level.
}

create_secret_if_missing "DATABASE_URL" "$DATABASE_URL"
create_secret_if_missing "AGENT_JWT_SECRET" "$(openssl rand -hex 32)"
create_secret_if_missing "WS_TICKET_SECRET" "$(openssl rand -hex 32)"
create_secret_if_missing "ADMIN_API_KEY" "$(openssl rand -hex 32)"

ADMIN_API_KEY=$(gcloud secrets versions access latest --secret="ADMIN_API_KEY" --project="$PROJECT_ID")

ok "Core secrets ready"

# GitHub secrets — prompt for real values or skip (Cloud Run boot-path that
# reads these will fail loudly rather than silently use a literal "placeholder"
# value, which used to mask config bugs as "GitHub feature broken").
#
# If the secret already exists (re-run), we leave it alone — Phase 2 is the
# canonical path for setting real values, and we don't want to clobber a real
# value the operator already entered.
prompt_secret_or_skip() {
  local name="$1"
  local description="$2"
  local masked="${3:-false}"  # true = no echo (for client secrets / private keys)
  if gcloud secrets describe "$name" --project="$PROJECT_ID" &>/dev/null; then
    echo "  $name already exists, skipping"
    # Wave F4 M2: per-secret IAM binding removed (project-wide grant covers).
    return 0
  fi
  echo
  echo -e "  ${DIM}$description${NC}"
  local value=""
  if [[ "$masked" == "true" ]]; then
    read -r -s -p "  $name (or Enter to skip): " value
    echo
  else
    read -r -p "  $name (or Enter to skip): " value
  fi
  if [[ -z "$value" ]]; then
    # Cloud Run --set-secrets on cloudbuild.yaml validates that every named
    # secret exists at deploy time. If we leave $name uncreated, the next
    # `gcloud builds submit` step fails. Create a placeholder so deploy
    # validates; orchestrator code will fail loudly at runtime when GitHub
    # features are exercised, surfacing the unset state explicitly. Phase 2
    # overwrites with the real value via gcloud secrets versions add.
    echo -n "PLACEHOLDER_REPLACE_IN_PHASE2" | gcloud secrets create "$name" --data-file=- --project="$PROJECT_ID" --quiet
    echo -e "  ${YELLOW}Skipped: $name set to placeholder. Cloud Run boot will fail loudly when GitHub features are exercised.${NC}"
    echo -e "  ${DIM}Re-run './setup.sh --phase2' to populate all GitHub secrets with real values.${NC}"
    return 0
  fi
  echo -n "$value" | gcloud secrets create "$name" --data-file=- --project="$PROJECT_ID" --quiet
  echo "  $name created"
  # Wave F4 M2: per-secret IAM binding removed (project-wide grant covers).
}

echo
echo -e "${DIM}── GitHub secrets (optional now; required before GitHub features work) ──${NC}"
echo -e "${DIM}Press Enter at any prompt to skip; Phase 2 (./setup.sh --phase2) sets them all.${NC}"
prompt_secret_or_skip "GITHUB_APP_ID"         "GitHub App numeric ID (top of app settings page)"
prompt_secret_or_skip "GITHUB_CLIENT_ID"      "GitHub OAuth client ID (from App About section)"
prompt_secret_or_skip "GITHUB_CLIENT_SECRET"  "GitHub OAuth client secret (Generate a client secret)" "true"
prompt_secret_or_skip "GITHUB_PRIVATE_KEY"    "GitHub App private key (paste full PEM contents)" "true"
prompt_secret_or_skip "GITHUB_WEBHOOK_SECRET" "GitHub webhook secret (the value you set on the App)" "true"
# These boot-required secrets use the same placeholder path so Cloud Run's
# --set-secrets validation fails at deploy time only for a missing secret name.
# Replace placeholders with the real values before relying on the service.
prompt_secret_or_skip "GITHUB_PAT_ENCRYPTION_KEY" "Fernet key used to encrypt GitHub PATs" "true"
prompt_secret_or_skip "MIGRATION_APPLY_SCRATCH_DATABASE_URL" "Dedicated scratch Postgres URL for migration qualification" "true"

# ─── Build → Migrate → Deploy ───────────────────────────────────────────────
#
# Order matters here. The orchestrator's lifespan startup queries AllowedUser
# in `_seed_initial_admin` (see backend/app/lifecycle.py:28). If the table
# doesn't exist, the container crashes before binding port 8080, so the
# Cloud Run deploy times out with "container failed to start." Running
# alembic migrations BEFORE the first deploy creates the schema so startup
# succeeds.
#
# The cloudbuild.yaml file bundles build + push + deploy in one submit (used
# by CI workflows where migrations aren't part of the deploy path). For the
# initial-setup case here we split: build+push first (so the image exists for
# the migration job), then migrate, then deploy.

# Sanity check region
if ! echo "$REGION" | grep -qE '^[a-z]+-[a-z]+[0-9]+$'; then
  warn "Invalid REGION detected: '$REGION' — resetting to us-central1"
  REGION="us-central1"
  ZONE="${REGION}-a"
fi

IMAGE_TAG="${REGION}-docker.pkg.dev/${PROJECT_ID}/blueprint-repo/blueprint-orchestrator:latest"

info "Building and pushing backend image (no deploy yet)..."
cd backend
gcloud builds submit --tag="$IMAGE_TAG" --project="$PROJECT_ID" --quiet
cd ..
ok "Image built: $IMAGE_TAG"

info "Running database migrations (region: $REGION)..."
if gcloud run jobs describe blueprint-migrate --region="$REGION" --project="$PROJECT_ID" &>/dev/null; then
  gcloud run jobs update blueprint-migrate --image="$IMAGE_TAG" --region="$REGION" --project="$PROJECT_ID" --quiet
else
  gcloud run jobs create blueprint-migrate \
    --image="$IMAGE_TAG" --region="$REGION" \
    --service-account="$ORCHESTRATOR_SA" \
    --set-secrets="DATABASE_URL=DATABASE_URL:latest" \
    --vpc-egress=all-traffic --network=agent-sandbox-vpc --subnet=agent-sandbox-subnet \
    --command=alembic --args="upgrade,head" \
    --project="$PROJECT_ID" --quiet
fi

gcloud run jobs execute blueprint-migrate --region="$REGION" --project="$PROJECT_ID" --wait --quiet
ok "Migrations complete"

info "Deploying backend to Cloud Run..."
# Mirrors cloudbuild.yaml's deploy step (kept inline so the bootstrap order
# is build → migrate → deploy). The deploy-parity test reads the machine-readable
# required-runtime-env manifest and both lists below to prevent boot-env drift.
gcloud run deploy blueprint-orchestrator \
  --image="$IMAGE_TAG" \
  --region="$REGION" \
  --service-account="$ORCHESTRATOR_SA" \
  --set-env-vars="GCP_PROJECT_ID=$PROJECT_ID,GCP_ZONE=${ZONE},VPC_NETWORK=agent-sandbox-vpc,VPC_SUBNET=agent-sandbox-subnet,AGENT_SERVICE_ACCOUNT_EMAIL=agent-naked-sa@${PROJECT_ID}.iam.gserviceaccount.com,ARTIFACT_BUCKET_NAME=${PROJECT_ID}-agent-artifacts,ALLOWED_ORIGINS=${ALLOWED_ORIGINS:-http://localhost:3000},ORCHESTRATOR_URL=${ORCHESTRATOR_URL},LOCAL_MODE=false,MAX_CONCURRENT_VMS=10,MAX_PROVISION_SLOTS=2,MACHINE_TYPE=n2-standard-2,INITIAL_ADMIN_EMAIL=${ADMIN_EMAIL:-},VM_PROVISIONING_MODEL=STANDARD,DISABLED_REGIONS=us-east4;us-west1,CLOUD_RUN_JOB_NAME=projects/${PROJECT_ID}/locations/${REGION}/jobs/blueprint-agent-job" \
  --set-secrets="DATABASE_URL=DATABASE_URL:latest,AGENT_JWT_SECRET=AGENT_JWT_SECRET:latest,WS_TICKET_SECRET=WS_TICKET_SECRET:latest,GITHUB_APP_ID=GITHUB_APP_ID:latest,GITHUB_CLIENT_ID=GITHUB_CLIENT_ID:latest,GITHUB_CLIENT_SECRET=GITHUB_CLIENT_SECRET:latest,GITHUB_PRIVATE_KEY=GITHUB_PRIVATE_KEY:latest,GITHUB_WEBHOOK_SECRET=GITHUB_WEBHOOK_SECRET:latest,ADMIN_API_KEY=ADMIN_API_KEY:latest,METRICS_TOKEN=METRICS_TOKEN:latest,GITHUB_PAT_ENCRYPTION_KEY=GITHUB_PAT_ENCRYPTION_KEY:latest,MIGRATION_APPLY_SCRATCH_DATABASE_URL=MIGRATION_APPLY_SCRATCH_DATABASE_URL:latest" \
  --no-cpu-throttling --cpu-boost \
  --vpc-egress=all-traffic --network=agent-sandbox-vpc --subnet=agent-sandbox-subnet \
  --project="$PROJECT_ID" --quiet
ok "Backend deployed"

# ─── Packer Agent Image ────────────────────────────────────────────────────

info "Building agent VM image with Packer..."
cd infra/packer
export GCP_PROJECT_ID="$PROJECT_ID"
packer init .
packer build -var="project_id=$PROJECT_ID" -var="zone=$ZONE" agent.pkr.hcl
cd ../..
ok "Agent VM image built"

# ─── Save config for re-runs ───────────────────────────────────────────────

save_config
ok "Config saved to $CONFIG_FILE"

# ═════════════════════════════════════════════════════════════════════════════
# PHASE 1 COMPLETE
# ═════════════════════════════════════════════════════════════════════════════

echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║  Phase 1 complete! Your orchestrator is live.                ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  Orchestrator URL:  ${CYAN}$ORCHESTRATOR_URL${NC}"
echo -e "  Admin API Key:     ${CYAN}$ADMIN_API_KEY${NC}"
echo ""
echo -e "${YELLOW}  Now create your GitHub App at: https://github.com/settings/apps/new${NC}"
echo ""
echo "  Use these URLs when creating the app:"
echo -e "    Callback URL:  ${CYAN}${ORCHESTRATOR_URL}/api/github/oauth/callback${NC}"
echo -e "    Webhook URL:   ${CYAN}${ORCHESTRATOR_URL}/api/github/webhook${NC}"
echo ""
echo "  Required permissions: Contents (R&W), Pull requests (R&W),"
echo "  Commit statuses (R&W), Issues (R&W)"
echo ""
echo "  After creating the app, you'll need:"
echo "    - App ID (top of app settings page)"
echo "    - Client ID (About section)"
echo "    - Client Secret (click 'Generate a client secret')"
echo "    - Private key .pem file (click 'Generate a private key')"
echo "    - Webhook secret (the one you set)"
echo ""

echo "Press Enter when your GitHub App is created, or Ctrl+C to finish later."
echo -e "${DIM}(Re-run Phase 2 anytime with: ./setup.sh --phase2)${NC}"
read -p ""

run_phase2
