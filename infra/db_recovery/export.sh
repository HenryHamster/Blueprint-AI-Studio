#!/bin/sh
#
# Export the production Cloud SQL database to the independent recovery bucket.
#
# This script deliberately has no database credential, KMS permission, or
# Cloud Storage read permission. Cloud SQL performs the export server-side;
# its instance service account receives writer-only access in infra/recovery.
#
# Required environment:
#   PROJECT_ID, INSTANCE_NAME, DATABASE_NAME, BACKUP_BUCKET
# Optional environment:
#   POLL_SECONDS (default 15), MAX_POLL_ATTEMPTS (default 240)

set -eu

emit_event() {
  outcome="$1"
  message="$2"
  # Keep the record valid JSON even when gcloud returns quotes, backslashes,
  # newlines, or other control characters. Dropping unsupported characters is
  # deliberate: the raw gcloud stderr remains adjacent in Cloud Run logs.
  safe_message="$(printf '%s' "$message" | LC_ALL=C tr -cd '[:alnum:] _.,:;=/@()-')"
  printf '%s\n' "{\"event\":\"db_offhost_backup\",\"outcome\":\"${outcome}\",\"project_id\":\"${PROJECT_ID}\",\"instance\":\"${INSTANCE_NAME}\",\"message\":\"${safe_message}\"}"
}

fail() {
  emit_event "failure" "$1" >&2
  exit 1
}

require_env() {
  name="$1"
  eval "value=\${$name:-}"
  [ -n "$value" ] || fail "missing_${name}"
}

require_env PROJECT_ID
require_env INSTANCE_NAME
require_env DATABASE_NAME
require_env BACKUP_BUCKET

POLL_SECONDS="${POLL_SECONDS:-15}"
MAX_POLL_ATTEMPTS="${MAX_POLL_ATTEMPTS:-240}"

case "$POLL_SECONDS" in ''|*[!0-9]*) fail "invalid_POLL_SECONDS" ;; esac
case "$MAX_POLL_ATTEMPTS" in ''|*[!0-9]*) fail "invalid_MAX_POLL_ATTEMPTS" ;; esac
case "$BACKUP_BUCKET" in gs://*) ;; *) fail "backup_bucket_must_use_gs_uri" ;; esac
case "$BACKUP_BUCKET" in *' '*) fail "backup_bucket_contains_space" ;; esac
case "$PROJECT_ID" in *[!A-Za-z0-9-]*) fail "invalid_PROJECT_ID" ;; esac
case "$INSTANCE_NAME" in *[!A-Za-z0-9-]*) fail "invalid_INSTANCE_NAME" ;; esac
case "$DATABASE_NAME" in *[!A-Za-z0-9_-]*) fail "invalid_DATABASE_NAME" ;; esac

# A second Cloud SQL operation can fail or interfere with the export. Do not
# queue a competing operation; alert through the non-zero job result instead.
if ! active_operation="$(gcloud sql operations list \
  --project="$PROJECT_ID" \
  --instance="$INSTANCE_NAME" \
  --filter='status=RUNNING OR status=PENDING' \
  --limit=1 \
  --format='value(name)')"; then
  fail "active_operation_lookup_failed"
fi
[ -z "$active_operation" ] || fail "active_operation_${active_operation}"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
object_uri="${BACKUP_BUCKET%/}/cloudsql-exports/${INSTANCE_NAME}/${timestamp}-$$.sql.gz"

# --offload keeps the dump work out of the primary instance. --async lets us
# emit one explicit success/failure event after observing the operation rather
# than treating request acceptance as a completed backup.
if ! operation_name="$(gcloud sql export sql "$INSTANCE_NAME" "$object_uri" \
  --project="$PROJECT_ID" \
  --database="$DATABASE_NAME" \
  --offload \
  --async \
  --format='value(name)')"; then
  fail "export_request_failed"
fi
[ -n "$operation_name" ] || fail "export_operation_not_returned"

attempt=0
while [ "$attempt" -lt "$MAX_POLL_ATTEMPTS" ]; do
  if ! status="$(gcloud sql operations describe "$operation_name" \
    --project="$PROJECT_ID" \
    --format='value(status)')"; then
    fail "export_status_lookup_failed"
  fi
  case "$status" in
    DONE)
      if ! operation_error="$(gcloud sql operations describe "$operation_name" \
        --project="$PROJECT_ID" \
        --format='value(error.errors[0].message)')"; then
        fail "export_error_lookup_failed"
      fi
      [ -z "$operation_error" ] || fail "export_operation_failed_${operation_error}"
      emit_event "success" "operation=${operation_name};uri=${object_uri}"
      exit 0
      ;;
    RUNNING|PENDING)
      attempt=$((attempt + 1))
      sleep "$POLL_SECONDS"
      ;;
    *) fail "unexpected_operation_status_${status:-empty}" ;;
  esac
done

fail "export_operation_timed_out_${operation_name}"
