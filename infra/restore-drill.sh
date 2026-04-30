#!/usr/bin/env bash
# Quarterly Cloud SQL restore drill.
#
# Phase F2 of the database remediation plan (MEDIUM M-O3). The drill exists to
# prove the documented RTO/RPO targets in docs/db-operations.md are real:
# we restore a recent backup into a transient instance, confirm the schema
# migrates cleanly, sanity-count rows, and tear the test instance down.
#
# This script is interactive (uses `confirm()` like migrate-alloydb-to-cloudsql.sh).
# Run it from a workstation with `gcloud` authenticated and IAP access.
#
# Walks through:
#   1. Pull live instance / project info from terraform outputs (or env)
#   2. List recent backups; pick the latest (or accept operator-supplied id)
#   3. Restore into a transient `blueprint-restore-test` instance
#   4. Run `alembic check` against the restored DB (no pending migrations)
#   5. Sample row counts on key tables (task, project, alloweduser)
#   6. Destroy the test instance
#   7. Append the result to docs/db-restore-drill-log.md (creates if missing)
#
# Idempotency: each step checks state before acting. Re-running after a
# partial failure is safe.

set -euo pipefail

PROJECT_ID="${PROJECT_ID:-}"
REGION="${REGION:-us-central1}"
RESTORE_INSTANCE="${RESTORE_INSTANCE:-blueprint-restore-test}"
TERRAFORM_DIR="$(cd "$(dirname "$0")/terraform" && pwd)"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DRILL_LOG="${REPO_ROOT}/docs/db-restore-drill-log.md"

DRILL_START_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
RESTORE_INSTANCE_PROVISIONED="false"
ALEMBIC_RESULT="not-run"
ROW_COUNTS_RAW=""
ANOMALIES=""

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
  if [[ "$RESTORE_INSTANCE_PROVISIONED" == "true" ]]; then
    echo
    echo "=== Cleanup ==="
    if [[ $rc -eq 0 ]] && confirm "Delete restore-test instance ($RESTORE_INSTANCE)?"; then
      gcloud sql instances delete "$RESTORE_INSTANCE" -q --project="$PROJECT_ID" || true
    elif [[ $rc -ne 0 ]]; then
      echo "Restore-test instance left running for debugging:"
      echo "  $RESTORE_INSTANCE"
      echo "When done: gcloud sql instances delete $RESTORE_INSTANCE -q --project=$PROJECT_ID"
    fi
  fi
}
trap cleanup_on_exit EXIT INT TERM

step "0. Sanity checks"
echo "Project:          $PROJECT_ID"
echo "Region:           $REGION"
echo "Restore instance: $RESTORE_INSTANCE"
gcloud auth print-access-token > /dev/null || { echo "Not logged in: gcloud auth login"; exit 1; }

step "1. Identify the live Cloud SQL instance"
LIVE_INSTANCE="${LIVE_INSTANCE:-}"
if [[ -z "$LIVE_INSTANCE" ]]; then
  LIVE_INSTANCE="$(terraform -chdir="$TERRAFORM_DIR" output -raw cloud_sql_instance_name 2>/dev/null || true)"
fi
if [[ -z "$LIVE_INSTANCE" ]]; then
  echo "ERROR: could not determine live Cloud SQL instance name." >&2
  echo "Set LIVE_INSTANCE env var or add a cloud_sql_instance_name terraform output." >&2
  exit 1
fi
echo "Live instance: $LIVE_INSTANCE"

step "2. List recent backups"
gcloud sql backups list --instance="$LIVE_INSTANCE" --project="$PROJECT_ID" --limit=10

BACKUP_ID="${BACKUP_ID:-}"
if [[ -z "$BACKUP_ID" ]]; then
  BACKUP_ID="$(gcloud sql backups list --instance="$LIVE_INSTANCE" --project="$PROJECT_ID" --limit=1 --format='value(id)' || true)"
  if [[ -z "$BACKUP_ID" ]]; then
    echo "ERROR: no backups found for $LIVE_INSTANCE." >&2
    exit 1
  fi
  echo
  echo "Latest backup id: $BACKUP_ID"
  if ! confirm "Use the latest backup?"; then
    read -r -p "Enter backup-id to restore: " BACKUP_ID
    if [[ -z "$BACKUP_ID" ]]; then
      echo "ERROR: empty backup id." >&2
      exit 1
    fi
  fi
fi
echo "Selected backup: $BACKUP_ID"

step "3. Restore backup into transient instance ($RESTORE_INSTANCE)"
if gcloud sql instances describe "$RESTORE_INSTANCE" --project="$PROJECT_ID" >/dev/null 2>&1; then
  echo "Restore-test instance already exists; skipping create."
  RESTORE_INSTANCE_PROVISIONED="true"
else
  if ! confirm "Create $RESTORE_INSTANCE and restore backup $BACKUP_ID into it? (~10 min)"; then
    echo "Aborting before restore."
    exit 1
  fi
  # Cloud SQL requires the destination instance to exist before restoring into it.
  # Wave F4 M8: read the live instance's tier dynamically rather than
  # hardcoding `db-g1-small`. After a tier upgrade on the live instance,
  # a hardcoded smaller tier silently shrinks the test instance — a backup
  # restore that succeeds at the smaller tier proves nothing about the live
  # tier's capacity to absorb the same workload. Falling back to db-g1-small
  # only if the describe fails (e.g. live instance not yet provisioned).
  LIVE_TIER="$(gcloud sql instances describe "$LIVE_INSTANCE" \
    --project="$PROJECT_ID" \
    --format='value(settings.tier)' 2>/dev/null || echo '')"
  if [[ -z "$LIVE_TIER" ]]; then
    echo "WARN: could not read live tier from $LIVE_INSTANCE; falling back to db-g1-small." >&2
    LIVE_TIER="db-g1-small"
  fi
  echo "Using tier=$LIVE_TIER for $RESTORE_INSTANCE (matches live instance)."
  gcloud sql instances create "$RESTORE_INSTANCE" \
    --database-version=POSTGRES_15 \
    --tier="$LIVE_TIER" \
    --region="$REGION" \
    --availability-type=ZONAL \
    --no-backup \
    --project="$PROJECT_ID"
  RESTORE_INSTANCE_PROVISIONED="true"
  echo "Waiting for restore-test instance to be runnable..."
  for i in {1..30}; do
    state="$(gcloud sql instances describe "$RESTORE_INSTANCE" --project="$PROJECT_ID" --format='value(state)' || true)"
    if [[ "$state" == "RUNNABLE" ]]; then
      break
    fi
    echo "  attempt $i/30, state=$state"
    sleep 10
  done

  echo "Restoring backup $BACKUP_ID into $RESTORE_INSTANCE..."
  gcloud sql backups restore "$BACKUP_ID" \
    --restore-instance="$RESTORE_INSTANCE" \
    --backup-instance="$LIVE_INSTANCE" \
    --project="$PROJECT_ID" \
    -q
fi

step "4. Run alembic check against the restored DB"
echo "Operator action: connect to $RESTORE_INSTANCE (e.g. via Cloud SQL Auth Proxy"
echo "or a transient migrator VM in the agent VPC) and run:"
echo
echo "  cd backend && alembic check"
echo
echo "Expected outcome: 'No new upgrade operations detected.'"
if confirm "Did 'alembic check' pass with no pending migrations?"; then
  ALEMBIC_RESULT="pass"
else
  # Wave F4 M7: a failed alembic check means the restored DB cannot serve
  # production traffic — the migration history is broken. Continuing the
  # drill silently and recording "fail" in the log files away the failure
  # without alerting the operator that the documented RTO has been
  # invalidated. Fail loudly: append the outcome to the log first (so the
  # audit trail is complete), THEN exit non-zero.
  ALEMBIC_RESULT="fail"
  ANOMALIES="${ANOMALIES}alembic check failed; "
  DRILL_END_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  if [[ ! -f "$DRILL_LOG" ]]; then
    cat > "$DRILL_LOG" <<'HEADER'
# Database restore-drill log

Append-only log of quarterly restore drills run via `infra/restore-drill.sh`.
Each entry captures: drill date, backup id used, alembic check outcome, row
counts, anomalies, and total wall time. The log is the audit trail proving
the RTO/RPO targets in `docs/db-operations.md` are real, not aspirational.

HEADER
  fi
  {
    echo "## Drill ${DRILL_START_TS}"
    echo
    echo "- Operator project: ${PROJECT_ID}"
    echo "- Live instance: ${LIVE_INSTANCE}"
    echo "- Restore-test instance: ${RESTORE_INSTANCE}"
    echo "- Backup id: ${BACKUP_ID}"
    echo "- Started: ${DRILL_START_TS}"
    echo "- Finished: ${DRILL_END_TS}"
    echo "- Alembic check: ${ALEMBIC_RESULT}"
    echo "- Row counts: (skipped — alembic failed)"
    echo "- Anomalies: ${ANOMALIES}"
    echo
  } >> "$DRILL_LOG"
  echo "ERROR: alembic check failed against the restored DB." >&2
  echo "       The documented RTO/RPO targets in docs/db-operations.md are NOT achievable" >&2
  echo "       until the migration history is fixed. Drill outcome appended to $DRILL_LOG." >&2
  exit 1
fi

step "5. Sample row counts on key tables"
echo "From the same connection, run:"
echo
echo "  SELECT 'task'        AS tbl, count(*) FROM task UNION ALL"
echo "  SELECT 'project'     AS tbl, count(*) FROM project UNION ALL"
echo "  SELECT 'alloweduser' AS tbl, count(*) FROM alloweduser;"
echo
read -r -p "Paste the output (or a one-line summary, then press Enter): " ROW_COUNTS_RAW

step "6. Destroy the restore-test instance"
echo "Cleanup runs automatically on exit (you'll be prompted)."

step "7. Record drill outcome"
DRILL_END_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
if [[ ! -f "$DRILL_LOG" ]]; then
  cat > "$DRILL_LOG" <<'HEADER'
# Database restore-drill log

Append-only log of quarterly restore drills run via `infra/restore-drill.sh`.
Each entry captures: drill date, backup id used, alembic check outcome, row
counts, anomalies, and total wall time. The log is the audit trail proving
the RTO/RPO targets in `docs/db-operations.md` are real, not aspirational.

HEADER
fi

{
  echo "## Drill ${DRILL_START_TS}"
  echo
  echo "- Operator project: ${PROJECT_ID}"
  echo "- Live instance: ${LIVE_INSTANCE}"
  echo "- Restore-test instance: ${RESTORE_INSTANCE}"
  echo "- Backup id: ${BACKUP_ID}"
  echo "- Started: ${DRILL_START_TS}"
  echo "- Finished: ${DRILL_END_TS}"
  echo "- Alembic check: ${ALEMBIC_RESULT}"
  echo "- Row counts: ${ROW_COUNTS_RAW:-(none recorded)}"
  echo "- Anomalies: ${ANOMALIES:-none}"
  echo
} >> "$DRILL_LOG"

echo "Drill outcome appended to $DRILL_LOG"

step "Done"
cat <<DONE
Drill complete.

Summary:
  Backup:        $BACKUP_ID
  Alembic:       $ALEMBIC_RESULT
  Anomalies:     ${ANOMALIES:-none}
  Wall clock:    $DRILL_START_TS → $DRILL_END_TS
  Log appended:  $DRILL_LOG

Next steps:
  - Schedule the next drill ~3 months from today.
  - If alembic check failed or row counts looked off, investigate before the
    next drill. The restored instance has been (or will be) destroyed; capture
    findings in $DRILL_LOG before they fade.
DONE
