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
ALLOYDB_IP=$ALLOYDB_IP
ALLOYDB_PW_SECRET=$ALLOYDB_PW_SECRET
ADMIN_API_KEY=$ADMIN_API_KEY
EOF
}

load_config() {
  if [ -f "$CONFIG_FILE" ]; then
    source "$CONFIG_FILE"
    return 0
  fi
  return 1
}

# ─── Force rebuild mode ────────────────────────────────────────────────────
if [ "${1:-}" = "-f" ]; then
  load_config || fail "No previous config found. Run ./setup.sh first."
  info "Force rebuilding and deploying backend..."
  cd backend
  gcloud builds submit --config=cloudbuild.yaml \
    --substitutions="_ALLOWED_ORIGINS=*,_ORCHESTRATOR_URL=$ORCHESTRATOR_URL" \
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
    gcloud secrets add-iam-policy-binding "$name" \
      --member="serviceAccount:$ORCHESTRATOR_SA" \
      --role="roles/secretmanager.secretAccessor" \
      --project="$PROJECT_ID" --quiet 2>/dev/null || true
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
  gcloud builds submit --config=cloudbuild.yaml \
    --substitutions="_ALLOWED_ORIGINS=*,_ORCHESTRATOR_URL=$ORCHESTRATOR_URL" \
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
  echo -e "  3. Settings → Add Max plan accounts (paste ~/.claude/credentials.json)"
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

  echo ""
  info "Setting GCP project to $PROJECT_ID..."
  gcloud config set project "$PROJECT_ID" --quiet
  gcloud auth application-default set-quota-project "$PROJECT_ID" --quiet 2>/dev/null || true

  # ─── Enable APIs ──────────────────────────────────────────────────────────

  info "Enabling required GCP APIs..."
  gcloud services enable \
    compute.googleapis.com \
    run.googleapis.com \
    cloudbuild.googleapis.com \
    secretmanager.googleapis.com \
    artifactregistry.googleapis.com \
    alloydb.googleapis.com \
    servicenetworking.googleapis.com \
    iam.googleapis.com \
    iamcredentials.googleapis.com \
    --quiet
  ok "APIs enabled"

  # ─── Terraform ────────────────────────────────────────────────────────────

  info "Running Terraform..."
  cd infra/terraform

  terraform init -input=false

  TF_VARS="-var=project_id=$PROJECT_ID -var=region=$REGION -var=zone=$ZONE"

  terraform apply -auto-approve $TF_VARS || warn "Terraform apply had errors (may be OK on re-run, continuing...)"

  ORCHESTRATOR_URL=$(terraform output -raw orchestrator_url 2>/dev/null || echo "")
  ARTIFACT_BUCKET=$(terraform output -raw artifact_bucket_name 2>/dev/null || echo "")
  ORCHESTRATOR_SA=$(terraform output -raw orchestrator_service_account 2>/dev/null || echo "")
  ALLOYDB_IP=$(terraform output -raw alloydb_ip_address 2>/dev/null || echo "")
  ALLOYDB_PW_SECRET=$(terraform output -raw alloydb_password_secret_id 2>/dev/null || echo "")

  if [ -z "$ORCHESTRATOR_URL" ]; then
    fail "Could not read orchestrator_url from Terraform outputs."
  fi

  ok "Infrastructure ready (URL: $ORCHESTRATOR_URL)"
  cd ../..
fi

# ─── Core Secrets ───────────────────────────────────────────────────────────

info "Setting up secrets in Secret Manager..."

ALLOYDB_PASSWORD=$(gcloud secrets versions access latest --secret="$ALLOYDB_PW_SECRET" --project="$PROJECT_ID")
DATABASE_URL="postgresql+asyncpg://postgres:${ALLOYDB_PASSWORD}@${ALLOYDB_IP}:5432/postgres"

create_secret_if_missing() {
  local name=$1 value=$2
  if gcloud secrets describe "$name" --project="$PROJECT_ID" &>/dev/null; then
    echo "  $name already exists, skipping"
  else
    echo -n "$value" | gcloud secrets create "$name" --data-file=- --project="$PROJECT_ID" --quiet
    echo "  $name created"
  fi
  gcloud secrets add-iam-policy-binding "$name" \
    --member="serviceAccount:$ORCHESTRATOR_SA" \
    --role="roles/secretmanager.secretAccessor" \
    --project="$PROJECT_ID" --quiet 2>/dev/null || true
}

create_secret_if_missing "DATABASE_URL" "$DATABASE_URL"
create_secret_if_missing "AGENT_JWT_SECRET" "$(openssl rand -hex 32)"
create_secret_if_missing "WS_TICKET_SECRET" "$(openssl rand -hex 32)"
create_secret_if_missing "ADMIN_API_KEY" "$(openssl rand -hex 32)"

ADMIN_API_KEY=$(gcloud secrets versions access latest --secret="ADMIN_API_KEY" --project="$PROJECT_ID")

ok "Core secrets ready"

# Create placeholder GitHub secrets so Cloud Run deploy doesn't fail
for PLACEHOLDER_SECRET in GITHUB_APP_ID GITHUB_CLIENT_ID GITHUB_CLIENT_SECRET GITHUB_PRIVATE_KEY GITHUB_WEBHOOK_SECRET; do
  create_secret_if_missing "$PLACEHOLDER_SECRET" "placeholder"
done

# ─── Build & Deploy Backend ─────────────────────────────────────────────────

info "Building and deploying backend to Cloud Run..."
cd backend
gcloud builds submit --config=cloudbuild.yaml \
  --substitutions="_ALLOWED_ORIGINS=*,_ORCHESTRATOR_URL=$ORCHESTRATOR_URL" \
  --project="$PROJECT_ID" --quiet
cd ..
ok "Backend deployed"

# ─── Run Migrations ─────────────────────────────────────────────────────────

# Sanity check region
if ! echo "$REGION" | grep -qE '^[a-z]+-[a-z]+[0-9]+$'; then
  warn "Invalid REGION detected: '$REGION' — resetting to us-central1"
  REGION="us-central1"
  ZONE="${REGION}-a"
fi

info "Running database migrations (region: $REGION)..."
MIGRATION_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/blueprint-repo/blueprint-orchestrator:latest"

if gcloud run jobs describe run-migrations --region="$REGION" --project="$PROJECT_ID" &>/dev/null; then
  gcloud run jobs update run-migrations --image="$MIGRATION_IMAGE" --region="$REGION" --project="$PROJECT_ID" --quiet
else
  gcloud run jobs create run-migrations \
    --image="$MIGRATION_IMAGE" --region="$REGION" \
    --service-account="$ORCHESTRATOR_SA" \
    --set-secrets="DATABASE_URL=DATABASE_URL:latest" \
    --vpc-egress=all-traffic --network=agent-sandbox-vpc --subnet=agent-sandbox-subnet \
    --command=alembic --args="upgrade,head" \
    --project="$PROJECT_ID" --quiet
fi

gcloud run jobs execute run-migrations --region="$REGION" --project="$PROJECT_ID" --wait --quiet
ok "Migrations complete"

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
