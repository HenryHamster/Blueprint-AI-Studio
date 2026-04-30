#!/usr/bin/env bash
# AlloyDB → Cloud SQL migration runbook.
#
# This script reads "terraform output" — must be run from the workstation that holds
# Terraform local state (state is local-only per project_terraform_backend.md).
#
# This is interactive. Run it from a workstation with `gcloud` authenticated and
# IAP access. It walks through:
#   1. Provisioning a transient migrator VM inside the agent VPC
#   2. Pulling AlloyDB and Cloud SQL credentials from Secret Manager
#   3. pg_dump from AlloyDB → pg_restore to Cloud SQL
#   4. Verifying row counts match across a sample of tables
#   5. Updating the DATABASE_URL secret to the Cloud SQL connection string
#   6. Triggering a Cloud Run new revision so the change takes effect
#   7. Tearing down the migrator VM
#
# Pre-requisites:
#   - terraform apply -var="use_cloud_sql=true"   (Cloud SQL must already exist)
#   - You have run /api/admin/scale-down or `gcloud run services update --max-instances=0`
#     to drain in-flight traffic. The DB cutover itself is fast (<30s) but live writes
#     during the dump produce inconsistent restore data.
#
# Idempotency: each step checks state before acting. Re-running after a partial
# failure is safe.
#
# Rollback (if something goes wrong after the secret swap):
#   gcloud secrets versions list DATABASE_URL --limit=5
#   # Find the prior version (pre-swap), then:
#   gcloud secrets versions access <PRIOR_VERSION_NUMBER> --secret=DATABASE_URL > /tmp/prev.url
#   gcloud secrets versions add DATABASE_URL --data-file=/tmp/prev.url
#   gcloud run services update blueprint-orchestrator --region=us-central1
#   shred -u /tmp/prev.url  # cleanup local rollback file
#
# Post-cutover (after 24-48h stability window):
#   gcloud secrets versions list DATABASE_URL --project=$PROJECT_ID
#   # disable all versions before today's swap:
#   for v in <list-of-old-version-numbers>; do
#     gcloud secrets versions disable "$v" --secret=DATABASE_URL
#   done
#   gcloud secrets versions destroy ... --secret=alloydb-postgres-password   # after alloydb.tf removed
#
set -euo pipefail

MIGRATOR_VM_PROVISIONED="false"
DUMP_PATH="/tmp/blueprint-pgdump.dump"
DUMP_TMP="${DUMP_PATH}.tmp"

PROJECT_ID="${PROJECT_ID:-}"
REGION="${REGION:-us-central1}"
ZONE="${ZONE:-us-central1-a}"
MIGRATOR_VM="${MIGRATOR_VM:-blueprint-db-migrator}"
TERRAFORM_DIR="$(cd "$(dirname "$0")/terraform" && pwd)"

if [[ -z "$PROJECT_ID" ]]; then
  PROJECT_ID="$(gcloud config get-value project 2>/dev/null || true)"
  if [[ -z "$PROJECT_ID" ]]; then
    echo "ERROR: PROJECT_ID not set and gcloud has no active project." >&2
    exit 1
  fi
fi

step() { echo; echo "=== $1 ==="; }
confirm() {
  local prompt="$1"
  read -r -p "$prompt [y/N] " ans
  [[ "$ans" =~ ^[Yy]$ ]]
}

cleanup_on_exit() {
  local rc=$?
  # On abort (non-zero exit) with the orchestrator drained, restore Cloud Run
  # scaling so the operator isn't left with a 0-instance service. The success
  # path handles this in step 7b; the trap covers every other exit path.
  if [[ $rc -ne 0 && "${ORCHESTRATOR_DRAINED:-false}" == "true" ]]; then
    echo
    echo "=== Restoring Cloud Run scaling (abort path) ==="
    gcloud run services update blueprint-orchestrator \
      --region="$REGION" --max-instances=5 --min-instances=1 \
      --project="$PROJECT_ID" || \
      echo "WARNING: failed to restore scaling; run manually: gcloud run services update blueprint-orchestrator --region=$REGION --max-instances=5 --min-instances=1"
  fi
  if [[ "$MIGRATOR_VM_PROVISIONED" == "true" ]]; then
    echo
    echo "=== Cleanup ==="
    if [[ $rc -eq 0 ]] && confirm "Delete migrator VM ($MIGRATOR_VM)?"; then
      gcloud compute instances delete "$MIGRATOR_VM" --zone="$ZONE" -q --project="$PROJECT_ID" || true
    elif [[ $rc -ne 0 ]]; then
      echo "Migrator VM left running for debugging:"
      echo "  gcloud compute ssh $MIGRATOR_VM --zone=$ZONE --tunnel-through-iap"
      echo "  When done: gcloud compute instances delete $MIGRATOR_VM --zone=$ZONE -q"
    fi
  fi
}
trap cleanup_on_exit EXIT INT TERM

step "0. Sanity checks"
echo "Project: $PROJECT_ID"
echo "Region:  $REGION"
echo "Zone:    $ZONE"
gcloud auth print-access-token > /dev/null || { echo "Not logged in: gcloud auth login"; exit 1; }

step "1. Pull connection details from terraform outputs"
ALLOYDB_IP="$(terraform -chdir="$TERRAFORM_DIR" output -raw alloydb_ip_address 2>/dev/null || true)"
ALLOYDB_PW_SECRET="$(terraform -chdir="$TERRAFORM_DIR" output -raw alloydb_password_secret_id 2>/dev/null || true)"
CLOUDSQL_IP="$(terraform -chdir="$TERRAFORM_DIR" output -raw cloud_sql_ip_address 2>/dev/null || true)"
CLOUDSQL_PW_SECRET="$(terraform -chdir="$TERRAFORM_DIR" output -raw cloud_sql_password_secret_id 2>/dev/null || true)"
VPC_NAME="$(terraform -chdir="$TERRAFORM_DIR" output -raw vpc_name 2>/dev/null || true)"
SUBNET_NAME="$(terraform -chdir="$TERRAFORM_DIR" output -raw subnet_name 2>/dev/null || true)"

if [[ -z "$ALLOYDB_IP" || -z "$CLOUDSQL_IP" ]]; then
  echo "ERROR: missing terraform outputs. Did you run 'terraform apply -var=\"use_cloud_sql=true\"'?" >&2
  echo "  alloydb_ip_address=$ALLOYDB_IP"
  echo "  cloud_sql_ip_address=$CLOUDSQL_IP"
  exit 1
fi
echo "AlloyDB IP:   $ALLOYDB_IP"
echo "Cloud SQL IP: $CLOUDSQL_IP"

# Note: Cloud SQL password is fetched here for URL construction in step 6.
# AlloyDB password is fetched only on the migrator VM (step 4) — never on this workstation.
CLOUDSQL_PASSWORD="$(gcloud secrets versions access latest --secret="$CLOUDSQL_PW_SECRET" --project="$PROJECT_ID")"

step "2. Drain Cloud Run before snapshot"
echo "Scaling blueprint-orchestrator to 0 instances to ensure consistent snapshot."
if confirm "Scale Cloud Run to 0 (recommended)?"; then
  gcloud run services update blueprint-orchestrator \
    --region="$REGION" --max-instances=0 --min-instances=0 \
    --project="$PROJECT_ID"
  ORCHESTRATOR_DRAINED="true"
else
  ORCHESTRATOR_DRAINED="false"
  echo "Skipping drain. Solo-dev mode acceptable, but live writes during dump may be lost."
fi

step "3. Provision migrator VM (transient, will be torn down at the end)"
if gcloud compute instances describe "$MIGRATOR_VM" --zone="$ZONE" >/dev/null 2>&1; then
  echo "Migrator VM already exists; reusing."
  MIGRATOR_VM_PROVISIONED="true"
else
  # --service-account points at the dedicated migrator-sa (provisioned by Wave F4
  # terraform). Defense-in-depth scopes:
  #   - cloud-platform.read-only: lets `gcloud secrets versions access` use
  #     instance metadata for ADC (the read-only scope is sufficient because
  #     accessing a secret VERSION is a "read" — adding/creating versions
  #     would require the broader scope and we never do that here).
  #   - userinfo.email: required by gcloud for OS Login and audit attribution.
  # The migrator-sa's IAM bindings further narrow effective capability to
  # secretAccessor on only the two DB password secrets.
  gcloud compute instances create "$MIGRATOR_VM" \
    --zone="$ZONE" \
    --machine-type=e2-small \
    --network="$VPC_NAME" \
    --subnet="$SUBNET_NAME" \
    --no-address \
    --image-family=debian-12 \
    --image-project=debian-cloud \
    --service-account="migrator-sa@${PROJECT_ID}.iam.gserviceaccount.com" \
    --scopes=https://www.googleapis.com/auth/cloud-platform.read-only,https://www.googleapis.com/auth/userinfo.email \
    --metadata=enable-oslogin=TRUE \
    --project="$PROJECT_ID"
  MIGRATOR_VM_PROVISIONED="true"
  echo "Waiting 20s for VM SSH to come up..."
  sleep 20
fi

# Inline remote script. Sent via heredoc through gcloud ssh.
#
# Escaping convention inside this heredoc (unquoted REMOTE):
#   $ALLOYDB_IP, $CLOUDSQL_IP, $DUMP_PATH, $DUMP_TMP   — LOCAL-expanded (filled in here)
#   \$ALLOYDB_PASSWORD, \$CLOUDSQL_PASSWORD, \$PGPASS_FILE, \$tbl, \$alloy, \$cloud,
#   \$REAL_ERRORS, \$RESTORE_LOG, \$EXPECTED_TABLES, \$exists  — REMOTE-expanded on the VM
#   \$(...)  — REMOTE command substitution
#   \${VAR}  — REMOTE expansion with braces
# No password values appear anywhere in this script string — they are fetched
# on the VM via Secret Manager.
REMOTE_SCRIPT=$(cat <<REMOTE
set -euo pipefail

# Wave F4 H7: libpq defaults to sslmode=prefer (downgrade if peer rejects TLS),
# which on a private VPC link is fine for confidentiality but defeats the
# server-side ssl_mode=ENCRYPTED_ONLY contract. Force REQUIRE so any psql/
# pg_dump/pg_restore call below errors out instead of silently downgrading.
export PGSSLMODE=require

sudo apt-get update -qq
sudo apt-get install -y -qq postgresql-client

# Fetch passwords on the VM via Secret Manager (never embed in argv)
ALLOYDB_PASSWORD=\$(gcloud secrets versions access latest --secret=alloydb-postgres-password --project="$PROJECT_ID")
CLOUDSQL_PASSWORD=\$(gcloud secrets versions access latest --secret=cloud-sql-postgres-password --project="$PROJECT_ID")

# Use PGPASSFILE — never put passwords in env or argv
PGPASS_FILE=\$(mktemp)
chmod 600 "\$PGPASS_FILE"

# Wave F4 M1: single consolidated trap covering both PGPASS_FILE and
# RESTORE_LOG. Pre-fix this was a chained trap (one set on PGPASS_FILE
# at this point, then re-set later before pg_restore to also include
# RESTORE_LOG) which made the cleanup invariant brittle — any new file
# added in between would also need a third re-set. RESTORE_LOG is
# created lazily below; cleanup_remote tolerates its absence via the
# rm -f fallback.
RESTORE_LOG=""
cleanup_remote() {
  if [[ -n "\${PGPASS_FILE:-}" && -f "\$PGPASS_FILE" ]]; then
    shred -u "\$PGPASS_FILE" 2>/dev/null || rm -f "\$PGPASS_FILE"
  fi
  if [[ -n "\${RESTORE_LOG:-}" && -f "\$RESTORE_LOG" ]]; then
    shred -u "\$RESTORE_LOG" 2>/dev/null || rm -f "\$RESTORE_LOG"
  fi
}
trap cleanup_remote EXIT INT TERM

# Format: hostname:port:database:username:password
echo "$ALLOYDB_IP:5432:postgres:postgres:\${ALLOYDB_PASSWORD}" >  "\$PGPASS_FILE"
echo "$CLOUDSQL_IP:5432:postgres:postgres:\${CLOUDSQL_PASSWORD}" >> "\$PGPASS_FILE"
export PGPASSFILE="\$PGPASS_FILE"

# Pre-flight: verify expected tables exist on AlloyDB
EXPECTED_TABLES="task project alloweduser maxplanaccount projectmessage taskhistory"
echo "Verifying expected tables exist on AlloyDB..."
for tbl in \$EXPECTED_TABLES; do
  exists=\$(psql -h "$ALLOYDB_IP" -U postgres -d postgres -tAc "SELECT to_regclass('public.\$tbl')")
  if [[ -z "\$exists" || "\$exists" == "" ]]; then
    echo "WARNING: table \$tbl not found on AlloyDB. Continuing — your schema may differ."
  fi
done

# Dump (write to .tmp, mv on success)
rm -f "$DUMP_PATH" "$DUMP_TMP"
echo "Dumping AlloyDB → $DUMP_TMP..."
pg_dump -h "$ALLOYDB_IP" -U postgres -d postgres -Fc --no-owner --no-acl > "$DUMP_TMP"
mv "$DUMP_TMP" "$DUMP_PATH"
echo "Dump size: \$(du -h "$DUMP_PATH" | cut -f1)"

# Restore — capture stderr and parse for real errors.
# Wave F4 M1: assign RESTORE_LOG to the variable that the consolidated trap
# already references; no per-call trap re-set needed.
echo "Restoring to Cloud SQL..."
RESTORE_LOG=\$(mktemp)
pg_restore -h "$CLOUDSQL_IP" -U postgres -d postgres \
  --clean --if-exists --no-owner --no-acl \
  "$DUMP_PATH" 2> "\$RESTORE_LOG" || true

# Allow benign warnings: "does not exist, skipping" (--if-exists), "must be owner of" with --no-owner.
# Block on real errors:
REAL_ERRORS=\$(grep -E "^pg_restore: error:" "\$RESTORE_LOG" | grep -vE "(does not exist, skipping|must be (member of|owner of))" | wc -l || true)
if [[ "\$REAL_ERRORS" -gt 0 ]]; then
  echo "ERROR: pg_restore reported \$REAL_ERRORS non-benign errors. First 20 lines:"
  grep -E "^pg_restore: error:" "\$RESTORE_LOG" | head -20
  exit 1
fi

# Row count comparison with correct table names
echo
echo "Row count comparison (alloydb / cloudsql):"
for tbl in \$EXPECTED_TABLES; do
  alloy=\$(psql -h "$ALLOYDB_IP" -U postgres -d postgres -tAc "SELECT COUNT(*) FROM \$tbl" 2>/dev/null || echo "?")
  cloud=\$(psql -h "$CLOUDSQL_IP" -U postgres -d postgres -tAc "SELECT COUNT(*) FROM \$tbl" 2>/dev/null || echo "?")
  printf "  %-20s alloydb=%s cloudsql=%s\n" "\$tbl" "\$alloy" "\$cloud"
done

echo
echo "Alembic version:"
psql -h "$ALLOYDB_IP" -U postgres -d postgres -tAc "SELECT version_num FROM alembic_version" 2>/dev/null || echo "  alloydb: missing"
psql -h "$CLOUDSQL_IP" -U postgres -d postgres -tAc "SELECT version_num FROM alembic_version" 2>/dev/null || echo "  cloudsql: missing"

# Shred dump file before VM teardown
shred -u "$DUMP_PATH" 2>/dev/null || rm -f "$DUMP_PATH"
REMOTE
)

step "4. Run dump/restore on the migrator VM"
gcloud compute ssh "$MIGRATOR_VM" --zone="$ZONE" --tunnel-through-iap --command="$REMOTE_SCRIPT" --project="$PROJECT_ID"

step "5. Inspect the row counts above. Do they match?"
if ! confirm "Row counts match (within tolerance for any in-flight writes)?"; then
  echo "Aborting before secret swap. Migrator VM left running for debugging:"
  echo "  gcloud compute ssh $MIGRATOR_VM --zone=$ZONE --tunnel-through-iap"
  exit 1
fi

echo
echo "NOTE: Currently-running Cloud Run revisions hold the old DATABASE_URL in memory."
echo "They will continue serving from AlloyDB until the new revision boots and they cycle out."
echo "If you scaled to 0 in step 2, this is moot — only the new revision will exist."
echo

step "6. Update DATABASE_URL secret"
# URL-encode the password for defense-in-depth even though override_special is now URL-safe.
# Append ?ssl=require for asyncpg TLS hint (server-side enforced via ssl_mode=ENCRYPTED_ONLY).
# R3-W4 M7 (audit 2026-04-30): pipe via stdin instead of argv to keep the
# cleartext password out of /proc/<pid>/cmdline for the python process
# lifetime. `rstrip("\n")` strips the trailing newline that bash <<< adds.
ENCODED_PW=$(python3 -c 'import urllib.parse,sys;print(urllib.parse.quote(sys.stdin.read().rstrip("\n"),safe=""))' <<< "$CLOUDSQL_PASSWORD")
NEW_URL="postgresql+asyncpg://postgres:${ENCODED_PW}@${CLOUDSQL_IP}:5432/postgres?ssl=require"
# Clear plaintext password — must not be re-used after URL build.
CLOUDSQL_PASSWORD=""
echo "New URL points to Cloud SQL ($CLOUDSQL_IP)."
if ! confirm "Add new DATABASE_URL secret version?"; then
  echo "Aborting; AlloyDB still authoritative."
  exit 0
fi
printf '%s' "$NEW_URL" | gcloud secrets versions add DATABASE_URL --data-file=- --project="$PROJECT_ID"
echo "Added. Prior version remains accessible for rollback:"
gcloud secrets versions list DATABASE_URL --project="$PROJECT_ID" --limit=3

step "7. Force Cloud Run to pick up the new secret"
# Cloud Run pulls --set-secrets values at instance startup; we need a new revision.
# Re-deploy by pinning the current image — this forces a new revision without
# leaving any persistent migration-only env vars in the revision YAML
# (mutating env vars would survive in `gcloud run revisions describe` forever).
CURRENT_IMAGE="$(gcloud run services describe blueprint-orchestrator \
  --region="$REGION" --project="$PROJECT_ID" \
  --format='value(spec.template.spec.containers[0].image)')"
if [[ -z "$CURRENT_IMAGE" ]]; then
  echo "ERROR: could not read current Cloud Run image; aborting redeploy." >&2
  exit 1
fi
gcloud run services update blueprint-orchestrator \
  --region="$REGION" \
  --image="$CURRENT_IMAGE" \
  --project="$PROJECT_ID"
# Defensive cleanup: if a prior run of this script (pre-fix) left the env var
# behind, strip it now. Safe no-op if the var is absent.
gcloud run services update blueprint-orchestrator \
  --region="$REGION" \
  --remove-env-vars=MIGRATION_REDEPLOY_TIMESTAMP \
  --project="$PROJECT_ID" 2>/dev/null || true

if [[ "${ORCHESTRATOR_DRAINED:-false}" == "true" ]]; then
  step "7b. Restore Cloud Run scaling"
  gcloud run services update blueprint-orchestrator \
    --region="$REGION" --max-instances=5 --min-instances=1 \
    --project="$PROJECT_ID"
fi

step "8. Smoke-test the new DB"
echo "Polling for new revision to become ready..."
for i in {1..24}; do
  REVISION=$(gcloud run services describe blueprint-orchestrator --region="$REGION" --format='value(status.latestReadyRevisionName)' --project="$PROJECT_ID")
  STATUS=$(gcloud run revisions describe "$REVISION" --region="$REGION" --format='value(status.conditions[?(@.type=="Ready")].status)' --project="$PROJECT_ID")
  if [[ "$STATUS" == "True" ]]; then
    echo "Revision $REVISION ready."
    break
  fi
  echo "  attempt $i/24, status=$STATUS"
  sleep 5
done

ORCH_URL="$(terraform -chdir="$TERRAFORM_DIR" output -raw orchestrator_url 2>/dev/null || true)"
if [[ -n "$ORCH_URL" ]]; then
  echo "Health endpoint: $ORCH_URL/healthz"
  curl -sS -o /dev/null -w "HTTP %{http_code} (%{time_total}s)\n" "$ORCH_URL/healthz" || true
fi
echo "If admin UI loads tasks, the cutover succeeded."

step "Done"
cat <<DONE
Cutover complete. Next steps when you're confident things are stable (24-48h):
  1. Remove infra/terraform/alloydb.tf
  2. terraform apply  (destroys AlloyDB cluster — saves ~\$15/day)
  3. Optionally: gcloud secrets delete alloydb-postgres-password
  4. Disable old DATABASE_URL versions:
     gcloud secrets versions list DATABASE_URL --project=$PROJECT_ID
     # for each version-number prior to today's swap:
     gcloud secrets versions disable <N> --secret=DATABASE_URL

Rollback (if backend errors appear within the window):
  gcloud secrets versions list DATABASE_URL --limit=5 --project=$PROJECT_ID
  PRIOR_VERSION=<the version-number before today>
  gcloud secrets versions access \$PRIOR_VERSION --secret=DATABASE_URL > /tmp/prev.url
  gcloud secrets versions add DATABASE_URL --data-file=/tmp/prev.url
  gcloud run services update blueprint-orchestrator --region=$REGION \\
    --update-env-vars="ROLLBACK_TIMESTAMP=\$(date -u +%Y%m%dT%H%M%SZ)"
  shred -u /tmp/prev.url  # cleanup local rollback file
DONE
