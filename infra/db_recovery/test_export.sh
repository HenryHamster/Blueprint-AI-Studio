#!/usr/bin/env bash
# Unit tests for export.sh. They run without GCP credentials by substituting a
# small gcloud executable which records every invocation.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXPORT_SCRIPT="$SCRIPT_DIR/export.sh"
TMPDIR_TEST="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_TEST"' EXIT

assert_contains() {
  local haystack="$1" needle="$2"
  [[ "$haystack" == *"$needle"* ]] || {
    echo "expected output to contain: $needle" >&2
    echo "$haystack" >&2
    return 1
  }
}

assert_valid_json_event() {
  local output="$1"
  printf '%s\n' "$output" | python3 -c '
import json, sys
events = [json.loads(line) for line in sys.stdin if line.startswith("{")]
assert events and events[-1]["event"] == "db_offhost_backup"
'
}

make_fake_gcloud() {
  local mode="$1"
  mkdir -p "$TMPDIR_TEST/bin"
  cat > "$TMPDIR_TEST/bin/gcloud" <<'EOF'
#!/bin/sh
set -eu
printf '%s\n' "$*" >> "$GCLOUD_CALLS"
case "${GCLOUD_MODE}:${1:-}:${2:-}:${3:-}" in
  success:sql:operations:list|describe-error:sql:operations:list|export-fail:sql:operations:list|status-fail:sql:operations:list|hostile-error:sql:operations:list) exit 0 ;;
  list-fail:sql:operations:list) echo 'list unavailable' >&2; exit 1 ;;
  active:sql:operations:list) printf '%s\n' 'already-running'; exit 0 ;;
  success:sql:export:sql) printf '%s\n' 'operation-1'; exit 0 ;;
  describe-error:sql:export:sql) printf '%s\n' 'operation-1'; exit 0 ;;
  status-fail:sql:export:sql|hostile-error:sql:export:sql) printf '%s\n' 'operation-1'; exit 0 ;;
  export-fail:sql:export:sql) echo 'request rejected' >&2; exit 1 ;;
  success:sql:operations:describe)
    case "$*" in *'error.errors[0].message'*) exit 0 ;; *) printf '%s\n' 'DONE'; exit 0 ;; esac
    ;;
  describe-error:sql:operations:describe)
    case "$*" in *'error.errors[0].message'*) printf '%s\n' 'permission denied'; exit 0 ;; *) printf '%s\n' 'DONE'; exit 0 ;; esac
    ;;
  hostile-error:sql:operations:describe)
    case "$*" in
      *'error.errors[0].message'*) printf 'permission \"denied\"\\next\nsecond line'; exit 0 ;;
      *) printf '%s\n' 'DONE'; exit 0 ;;
    esac
    ;;
  status-fail:sql:operations:describe) echo 'status unavailable' >&2; exit 1 ;;
esac
echo "unexpected gcloud invocation: $*" >&2
exit 64
EOF
  chmod +x "$TMPDIR_TEST/bin/gcloud"
  export GCLOUD_MODE="$mode"
  export GCLOUD_CALLS="$TMPDIR_TEST/gcloud-${mode}.calls"
  : > "$GCLOUD_CALLS"
}

run_export() {
  PATH="$TMPDIR_TEST/bin:$PATH" \
    PROJECT_ID='source-project' \
    INSTANCE_NAME='blueprint-cloudsql-a1b2' \
    DATABASE_NAME='postgres' \
    BACKUP_BUCKET='gs://recovery-project-db-backups' \
    POLL_SECONDS=0 \
    MAX_POLL_ATTEMPTS=2 \
    /bin/sh "$EXPORT_SCRIPT"
}

make_fake_gcloud success
success_output="$(run_export)"
assert_contains "$success_output" '"outcome":"success"'
assert_contains "$success_output" 'gs://recovery-project-db-backups/cloudsql-exports/'
assert_valid_json_event "$success_output"
if grep -q 'storage' "$GCLOUD_CALLS"; then
  echo "export script must not read or mutate Cloud Storage directly" >&2
  exit 1
fi

make_fake_gcloud active
if active_output="$(run_export 2>&1)"; then
  echo "active Cloud SQL operation should fail the backup job" >&2
  exit 1
fi
assert_contains "$active_output" 'active_operation_already-running'
if grep -q 'sql export sql' "$GCLOUD_CALLS"; then
  echo "export must not be requested while another Cloud SQL operation is active" >&2
  exit 1
fi

make_fake_gcloud describe-error
if error_output="$(run_export 2>&1)"; then
  echo "failed Cloud SQL export should fail the job" >&2
  exit 1
fi
assert_contains "$error_output" 'export_operation_failed_permission denied'
assert_valid_json_event "$error_output"

make_fake_gcloud list-fail
if list_fail_output="$(run_export 2>&1)"; then
  echo "failed operation lookup should fail the backup job" >&2
  exit 1
fi
assert_contains "$list_fail_output" 'active_operation_lookup_failed'
assert_valid_json_event "$list_fail_output"

make_fake_gcloud export-fail
if export_fail_output="$(run_export 2>&1)"; then
  echo "failed export request should fail the backup job" >&2
  exit 1
fi
assert_contains "$export_fail_output" 'export_request_failed'
assert_valid_json_event "$export_fail_output"

make_fake_gcloud status-fail
if status_fail_output="$(run_export 2>&1)"; then
  echo "failed operation status lookup should fail the backup job" >&2
  exit 1
fi
assert_contains "$status_fail_output" 'export_status_lookup_failed'
assert_valid_json_event "$status_fail_output"

make_fake_gcloud hostile-error
if hostile_output="$(run_export 2>&1)"; then
  echo "terminal export error should fail the backup job" >&2
  exit 1
fi
assert_contains "$hostile_output" '"outcome":"failure"'
assert_valid_json_event "$hostile_output"

echo "export.sh tests passed"
