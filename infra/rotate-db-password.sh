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
#   1. terraform taint 'random_password.cloud_sql_db_password[0]'
#   2. terraform apply -var="use_cloud_sql=true"  (regenerates pw + secret version)
#   3. Read NEW pw from Secret Manager
#   4. Read Cloud SQL private IP from terraform output
#   5. Verify the rotated pw authenticates against Cloud SQL (live SELECT 1)
#   6. URL-encode pw and construct new postgresql+asyncpg URL
#   7. Add new DATABASE_URL secret version  (POINT OF NO RETURN — operator confirm)
#   8. Force Cloud Run to a new revision (re-pin current image)
#   9. Poll for the new revision to report Ready=True
#
# Failure handling: ERR trap (set immediately below) covers steps 1-6 with a
# manual-recovery message that explains how to complete the rotation if
# terraform apply or the live-auth check fails. The trap is disarmed once
# the operator confirms the DATABASE_URL secret-version add (step 7) — past
# that the rotation is committed and any error is a "retry the swap"
# situation, not a "the rotation broke things" situation.
#
# EXPECTED BEHAVIOR DURING ROTATION:
# Between step 7 (DATABASE_URL secret-version add) and step 9 (new revision
# Ready), the OLD Cloud Run revision continues serving with connections
# holding the OLD password. After step 2's terraform apply rotates the SQL
# user, those connections fail auth on next pre-ping (~9 min via
# pool_recycle=540) or next reconnect. pool_pre_ping=True in
# backend/app/database.py prevents hard failure, but expect elevated request
# latency for up to ~9 minutes during rollover. This is by design; Cloud Run
# gracefully drains old revisions while the new revision serves.
#
# Pre-requisites:
#   - terraform apply -var="use_cloud_sql=true" (Cloud SQL must already exist)
#   - gcloud authenticated
#   - gcloud's cloud_sql_proxy component installed (bundled by default; if
#     missing run `gcloud components install cloud_sql_proxy`). Step 5
#     uses `gcloud sql connect --use-cloud-sql-proxy` to reach the
#     private-IP-only instance.
#   - workstation has private connectivity to the VPC (typically via VPN
#     or by running this from a bastion / Cloud Shell session that's
#     peered with `agent_vpc`). The Cloud SQL Auth Proxy can negotiate
#     authentication, but the proxied TCP still has to land on the
#     private IP.
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

# R4-W4 H8 (audit 2026-04-29): if any step fails after the taint has been
# accepted but before terraform apply succeeds, the operator is left with
# state that says "regenerate" but on-disk SQL user / DATABASE_URL secret
# untouched. Print explicit recovery instructions so the operator doesn't
# layer a panic-fix on top of a half-rotation.
trap 'rc=$?; echo; echo "ROTATION FAILED at line $LINENO (exit $rc). Manual recovery:"; \
      echo "  1. Run: terraform -chdir=$TERRAFORM_DIR apply -auto-approve -var=\"use_cloud_sql=true\""; \
      echo "     (idempotent — completes the rotation if state is partial)"; \
      echo "  2. Verify the new password is live: gcloud secrets versions access latest \\"; \
      echo "     --secret=cloud-sql-postgres-password --project=$PROJECT_ID"; \
      echo "     should match what the SQL user authenticates with."; \
      echo "  3. DO NOT add a new DATABASE_URL secret version until step 2 succeeds and SQL auth works."; \
      echo "  4. After auth confirmed, re-run this script — taint is idempotent on a fresh password,"; \
      echo "     and the secret-version-add steps below have a final confirm gate."; \
      exit $rc' ERR

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
# R4-W4 H7 (audit 2026-04-29): the resource is declared with `count = var.use_cloud_sql ? 1 : 0`
# in cloud_sql.tf, so the address is `random_password.cloud_sql_db_password[0]`.
# The bare-name taint command was broken-on-arrival ("resource not found").
# Quote the brackets so the shell doesn't try to glob them.
terraform -chdir="$TERRAFORM_DIR" taint 'random_password.cloud_sql_db_password[0]'

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

step "5. Verify the rotated password authenticates against Cloud SQL"
# R4-W4 H8 (audit 2026-04-29): the prior version of this script jumped from
# "secret value read" straight to "DATABASE_URL secret-version add". If
# terraform apply rotated the random_password and updated cloud-sql-postgres-password
# but the Cloud SQL user UPDATE failed silently (e.g. the google_sql_user
# resource hit an API hiccup), we'd ship a DATABASE_URL that no longer
# authenticates and Cloud Run would crashloop on the next revision.
#
# Strategy: try a lightweight `SELECT 1` from this workstation through the
# Cloud SQL Auth Proxy (`gcloud sql connect --use-cloud-sql-proxy`). If it
# succeeds we know the rotated password is actually live on the SQL user.
# If it fails, the ERR trap fires with recovery instructions and we never
# write the new DATABASE_URL version.
#
# R5-W3 A (audit 2026-04-30): the Cloud SQL instance is `ipv4_enabled = false`
# (see cloud_sql.tf), so the default `gcloud sql connect` path (which
# whitelists the operator's public IPv4 and connects over public IP) is
# broken-by-design — there is no public IP on this instance. The
# `--use-cloud-sql-proxy` flag tunnels via the Cloud SQL Auth Proxy
# (private IP-aware) and works against a private-only instance. The
# operator's gcloud already bundles the proxy binary, so this introduces
# no new tooling dependency.
CLOUDSQL_INSTANCE="$(terraform -chdir="$TERRAFORM_DIR" output -raw cloud_sql_instance_name 2>/dev/null || true)"
if [[ -z "$CLOUDSQL_INSTANCE" ]]; then
  echo "WARNING: cloud_sql_instance_name terraform output is empty. Skipping live-auth verification."
  echo "         (The rotation may still be valid, but if it isn't, the DATABASE_URL swap below will be irreversible without rollback.)"
  if ! confirm "Continue without live-auth check?"; then
    exit 1
  fi
else
  echo "Verifying rotated password against $CLOUDSQL_INSTANCE via Cloud SQL Auth Proxy..."
  # PGPASSWORD is the standard libpq env var; gcloud sql connect honors it
  # and forwards to psql. Avoid argv exposure of the password.
  # --use-cloud-sql-proxy: required because the instance is private-IP only
  # (ipv4_enabled = false in cloud_sql.tf). Without this flag, gcloud
  # attempts a direct public-IPv4 connection that cannot exist.
  if ! PGPASSWORD="$NEW_PW" gcloud sql connect "$CLOUDSQL_INSTANCE" \
        --user=postgres --database=postgres --quiet --project="$PROJECT_ID" \
        --use-cloud-sql-proxy \
        <<< "SELECT 1;" >/dev/null 2>&1; then
    echo "ERROR: the rotated password does NOT authenticate against $CLOUDSQL_INSTANCE." >&2
    echo "       Possible causes:" >&2
    echo "         - terraform apply succeeded on the random_password resource but the" >&2
    echo "           google_sql_user UPDATE failed (re-run terraform apply)." >&2
    echo "         - The Cloud SQL Auth Proxy can't reach the instance's private IP" >&2
    echo "           from this workstation (check VPN / private-services access)." >&2
    echo "         - gcloud lacks the cloud_sql_proxy bundled component (run" >&2
    echo "           'gcloud components install cloud_sql_proxy')." >&2
    echo "       NOT writing a new DATABASE_URL secret version — old one still authoritative." >&2
    exit 1
  fi
  echo "Live-auth check passed."
fi

step "6. URL-encode password and construct new DATABASE_URL"
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

step "7. Add new DATABASE_URL secret version"
# R4-W4 H8 (audit 2026-04-29): point-of-no-return gate. Terraform apply
# has succeeded AND the rotated password is verified to authenticate. From
# this point forward, writing a new DATABASE_URL secret version is the
# operator-driven irreversible commit. Disable the ERR trap here so a
# transient `gcloud secrets versions add` failure surfaces a different
# message (the rotation succeeded; only the secret swap failed, retry that
# specific step manually).
if ! confirm "POINT OF NO RETURN: Add new DATABASE_URL secret version? Cloud Run will read the new URL on the next revision boot."; then
  echo "Aborting before DATABASE_URL swap. The rotated password is live on the SQL user;"
  echo "the OLD DATABASE_URL will continue to authenticate. To finish the rotation later,"
  echo "re-run this script — it will detect state is partial and complete the swap."
  trap - ERR  # operator-driven abort, not a script failure
  exit 0
fi
trap - ERR  # post-confirm: failures here are not "rotation broke", they're "secret-add retry"
printf '%s' "$NEW_URL" | gcloud secrets versions add DATABASE_URL --data-file=- --project="$PROJECT_ID"
echo "Added. Prior versions remain accessible for rollback:"
gcloud secrets versions list DATABASE_URL --project="$PROJECT_ID" --limit=3
NEW_URL=""

step "8. Force Cloud Run to pick up the new secret"
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

step "9. Poll for new revision to become Ready"
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
