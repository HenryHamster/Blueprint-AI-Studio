#!/usr/bin/env bash
# Cloud SQL DB password rotation runbook.
#
# Wave F4 H6: closes the gap where `terraform taint random_password.cloud_sql_db_password`
# rotated the `cloud-sql-postgres-password` secret but did NOT update the
# constructed `DATABASE_URL` secret that Cloud Run actually reads. After a
# bare taint, Cloud Run still booted with the OLD URL embedded into a stale
# `DATABASE_URL` version, so DB connections silently kept failing until the
# operator manually re-built the URL. This script does the full chain.
#
# Walk:
#   1. terraform taint random_password.cloud_sql_db_password
#   2. terraform apply -var="use_cloud_sql=true"  (regenerates pw + secret version)
#   3. Read NEW pw from Secret Manager
#   4. Read Cloud SQL private IP from terraform output
#   5. URL-encode pw and construct new postgresql+asyncpg URL
#   6. Add new DATABASE_URL secret version
#   7. Force Cloud Run to a new revision (re-pin current image)
#   8. Poll for the new revision to report Ready=True
#
# Pre-requisites:
#   - terraform apply -var="use_cloud_sql=true" (Cloud SQL must already exist)
#   - gcloud authenticated
#   - run from a workstation that holds Terraform local state
#
# Idempotency: each step checks state. Re-running after a partial failure is safe
# (terraform apply is no-op once the new version is rotated; secret version add
# is monotonic; gcloud run services update with the same image is a no-op revision
# bump but Cloud Run dedupes).
#
# Rollback (if backend errors appear after the swap):
#   gcloud secrets versions list DATABASE_URL --limit=5 --project=$PROJECT_ID
#   PRIOR_VERSION=<version-number before today>
#   gcloud secrets versions access $PRIOR_VERSION --secret=DATABASE_URL > /tmp/prev.url
#   gcloud secrets versions add DATABASE_URL --data-file=/tmp/prev.url --project=$PROJECT_ID
#   gcloud run services update blueprint-orchestrator --region=$REGION --project=$PROJECT_ID
#   shred -u /tmp/prev.url

set -euo pipefail

PROJECT_ID="${PROJECT_ID:-}"
REGION="${REGION:-us-central1}"
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

step "0. Sanity checks"
echo "Project: $PROJECT_ID"
echo "Region:  $REGION"
gcloud auth print-access-token > /dev/null || { echo "Not logged in: gcloud auth login"; exit 1; }
if ! confirm "Rotate the Cloud SQL DB password and refresh DATABASE_URL? Cloud Run will cycle to a new revision."; then
  echo "Aborting. Nothing changed."
  exit 0
fi

step "1. Taint random_password.cloud_sql_db_password"
terraform -chdir="$TERRAFORM_DIR" taint random_password.cloud_sql_db_password

step "2. terraform apply -var=\"use_cloud_sql=true\""
terraform -chdir="$TERRAFORM_DIR" apply -auto-approve -var="use_cloud_sql=true"

step "3. Read new password from Secret Manager"
CLOUDSQL_PW_SECRET="$(terraform -chdir="$TERRAFORM_DIR" output -raw cloud_sql_password_secret_id 2>/dev/null || true)"
if [[ -z "$CLOUDSQL_PW_SECRET" ]]; then
  echo "ERROR: cloud_sql_password_secret_id terraform output is empty. Is use_cloud_sql=true?" >&2
  exit 1
fi
NEW_PW="$(gcloud secrets versions access latest --secret="$CLOUDSQL_PW_SECRET" --project="$PROJECT_ID")"
if [[ -z "$NEW_PW" ]]; then
  echo "ERROR: failed to read latest version of $CLOUDSQL_PW_SECRET." >&2
  exit 1
fi

step "4. Read Cloud SQL private IP"
CLOUDSQL_IP="$(terraform -chdir="$TERRAFORM_DIR" output -raw cloud_sql_ip_address 2>/dev/null || true)"
if [[ -z "$CLOUDSQL_IP" ]]; then
  echo "ERROR: cloud_sql_ip_address terraform output is empty." >&2
  exit 1
fi
echo "Cloud SQL IP: $CLOUDSQL_IP"

step "5. URL-encode password and construct new DATABASE_URL"
# URL-encode for defense-in-depth even though override_special is URL-safe.
# Append ?ssl=require so asyncpg requests TLS (server-side enforced via
# ssl_mode=ENCRYPTED_ONLY).
# R3-W4 M7 (audit 2026-04-30): pipe via stdin instead of argv to keep the
# cleartext password out of /proc/<pid>/cmdline for the python process
# lifetime. `rstrip("\n")` strips the trailing newline that bash <<< adds.
ENCODED_PW="$(python3 -c 'import urllib.parse,sys;print(urllib.parse.quote(sys.stdin.read().rstrip("\n"),safe=""))' <<< "$NEW_PW")"
NEW_URL="postgresql+asyncpg://postgres:${ENCODED_PW}@${CLOUDSQL_IP}:5432/postgres?ssl=require"
# Clear plaintext password — must not be re-used after URL build.
NEW_PW=""
ENCODED_PW=""

step "6. Add new DATABASE_URL secret version"
if ! confirm "Add new DATABASE_URL secret version? (Cloud Run will pick it up on next revision.)"; then
  echo "Aborting before secret swap. The old DATABASE_URL still works against the rotated password ONLY if Cloud Run has not yet recycled — once it does, connections will fail. Re-run this script."
  exit 1
fi
printf '%s' "$NEW_URL" | gcloud secrets versions add DATABASE_URL --data-file=- --project="$PROJECT_ID"
echo "Added. Prior versions remain accessible for rollback:"
gcloud secrets versions list DATABASE_URL --project="$PROJECT_ID" --limit=3
NEW_URL=""

step "7. Force Cloud Run to pick up the new secret"
# Cloud Run pulls --set-secrets values at instance startup; re-pinning the
# current image forces a new revision without leaving any persistent
# rotation-only env vars in the revision YAML (env mutations would survive
# in `gcloud run revisions describe` forever).
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

step "8. Poll for new revision to become Ready"
for i in {1..24}; do
  REVISION="$(gcloud run services describe blueprint-orchestrator \
    --region="$REGION" --format='value(status.latestReadyRevisionName)' \
    --project="$PROJECT_ID")"
  STATUS="$(gcloud run revisions describe "$REVISION" \
    --region="$REGION" \
    --format='value(status.conditions[?(@.type=="Ready")].status)' \
    --project="$PROJECT_ID")"
  if [[ "$STATUS" == "True" ]]; then
    echo "Revision $REVISION ready."
    break
  fi
  echo "  attempt $i/24, status=$STATUS"
  sleep 5
done

step "Done"
cat <<DONE
DB password rotated. New revision is serving with the refreshed DATABASE_URL.

Verification:
  - curl \$ORCHESTRATOR_URL/healthz   should return 200
  - admin UI /tasks                  should load without DB errors
  - Cloud Run logs                   should be free of 'authentication failed' errors

Disable old DATABASE_URL versions after a stability window (24-48h):
  gcloud secrets versions list DATABASE_URL --project=$PROJECT_ID
  for v in <list-of-old-version-numbers>; do
    gcloud secrets versions disable "\$v" --secret=DATABASE_URL --project=$PROJECT_ID
  done
DONE
