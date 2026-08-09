#!/usr/bin/env bash
set -euo pipefail

# Earn the version-1 project verification contract from functional behavior.
# The caller chooses the execution boundary (Packer and startup run this as the
# production `agent` user; the fleet runner launches it inside its agent bwrap
# plan).  Nothing here treats an installed binary, cached image, or host daemon
# as proof.

COMMAND_TIMEOUT_SECONDS="${BLUEPRINT_VERIFICATION_COMMAND_TIMEOUT_SECONDS:-30}"
READY_TIMEOUT_SECONDS="${BLUEPRINT_VERIFICATION_READY_TIMEOUT_SECONDS:-3}"
PROBE_ID="${BLUEPRINT_VERIFICATION_PROBE_ID:-$$}"
PROBE_MODE="${1:-all}"
POSTGRES_DATA="/tmp/blueprint-verification-v1-pg-${PROBE_ID}"
POSTGRES_SOCKET="/tmp/blueprint-verification-v1-sock-${PROBE_ID}"
RUFF_INPUT=""
POSTGRES_BIN=""

cleanup() {
  if [ -n "$POSTGRES_BIN" ] && [ -d "$POSTGRES_DATA" ]; then
    timeout "$COMMAND_TIMEOUT_SECONDS" "$POSTGRES_BIN/pg_ctl" \
      -D "$POSTGRES_DATA" -m immediate stop >/dev/null 2>&1 || true
  fi
  rm -rf "$POSTGRES_DATA" "$POSTGRES_SOCKET"
  if [ -n "$RUFF_INPUT" ]; then
    rm -f "$RUFF_INPUT"
  fi
}
trap cleanup EXIT

if [ "$PROBE_MODE" != "all" ] && [ "$PROBE_MODE" != "ruff" ] && \
   [ "$PROBE_MODE" != "docker-postgres" ]; then
  echo "usage: $0 [all|ruff|docker-postgres]" >&2
  exit 2
fi

if [ "$PROBE_MODE" = "all" ] || [ "$PROBE_MODE" = "ruff" ]; then
  RUFF_INPUT="$(mktemp /tmp/blueprint-verification-ruff.XXXXXX.py)"
  printf 'verification_value = 1\n' >"$RUFF_INPUT"
  timeout "$COMMAND_TIMEOUT_SECONDS" python3 -m ruff check --isolated --no-cache "$RUFF_INPUT"
fi

# Resolve the same PostgreSQL binaries as the backend route selector.  This
# copy is intentional: the probe is copied by itself into Packer and bwrap
# workspaces, where sibling repo scripts are not present.
resolve_pg_bindir() {
  local bindir cand sandbox_python
  sandbox_python="${BLUEPRINT_SANDBOX_PYTHON:-/opt/blueprint-backend-venv/bin/python}"
  if [ -x "$sandbox_python" ]; then
    bindir="$("$sandbox_python" -c 'from pgserver._commands import POSTGRES_BIN_PATH; print(POSTGRES_BIN_PATH)' 2>/dev/null || true)"
    if [ -n "$bindir" ] && [ -x "$bindir/initdb" ]; then
      printf '%s\n' "$bindir"
      return 0
    fi
  fi
  if command -v pg_config >/dev/null 2>&1; then
    bindir="$(pg_config --bindir 2>/dev/null || true)"
    if [ -n "$bindir" ] && [ -x "$bindir/initdb" ]; then
      printf '%s\n' "$bindir"
      return 0
    fi
    return 1
  fi
  for cand in $(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -n 1); do
    if [ -x "$cand/initdb" ]; then
      printf '%s\n' "$cand"
      return 0
    fi
  done
  return 1
}

# Start a private, Unix-socket-only PostgreSQL sidecar inside the caller's
# isolation namespace. This is the same socketless boundary used by generated
# authoring checks: no host Docker socket or host data directory is exposed.
if [ "$PROBE_MODE" = "all" ] || [ "$PROBE_MODE" = "docker-postgres" ]; then
  # This probe's result is the input to the dispatch gate
  # (runner_eligibility.py keys authoring dispatch off the docker_postgres
  # probe), so keep this resolver's precedence aligned with the shared route
  # selector.
  POSTGRES_BIN="$(resolve_pg_bindir)" || {
    echo "Sandbox PostgreSQL verification failed: no usable PostgreSQL server bindir" >&2
    exit 1
  }
  test -x "$POSTGRES_BIN/pg_ctl"
  # The `check` gate selects --sandbox-postgres only when the selected
  # interpreter can import both declared runtime packages. This also covers
  # off-image hosts using BLUEPRINT_SANDBOX_PYTHON.
  SANDBOX_PYTHON="${BLUEPRINT_SANDBOX_PYTHON:-/opt/blueprint-backend-venv/bin/python}"
  if ! timeout "$COMMAND_TIMEOUT_SECONDS" "$SANDBOX_PYTHON" -c 'import pgserver, pytest' \
    >/dev/null 2>&1; then
    echo "Sandbox PostgreSQL verification failed: baked sandbox interpreter $SANDBOX_PYTHON cannot import pytest" >&2
    exit 1
  fi

  mkdir -p "$POSTGRES_SOCKET"
  timeout "$COMMAND_TIMEOUT_SECONDS" "$POSTGRES_BIN/initdb" \
    -D "$POSTGRES_DATA" -A trust --no-locale -U postgres >/dev/null
  timeout "$COMMAND_TIMEOUT_SECONDS" "$POSTGRES_BIN/pg_ctl" \
    -D "$POSTGRES_DATA" \
    -o "-F -c listen_addresses='' -k '$POSTGRES_SOCKET'" \
    -w start >/dev/null

  if ! timeout "$READY_TIMEOUT_SECONDS" "$POSTGRES_BIN/pg_isready" \
    -h "$POSTGRES_SOCKET" -U postgres >/dev/null; then
    echo "Sandbox PostgreSQL verification failed: sidecar did not become ready" >&2
    exit 1
  fi

  query_result="$(
    timeout "$COMMAND_TIMEOUT_SECONDS" "$POSTGRES_BIN/psql" \
      -h "$POSTGRES_SOCKET" -U postgres -tAc 'SELECT 1'
  )"
  if [ "$(printf '%s' "$query_result" | tr -d '[:space:]')" != "1" ]; then
    echo "Sandbox PostgreSQL verification failed: SELECT 1 returned an unexpected result" >&2
    exit 1
  fi

  # The v1 receipt must prove the same contrib surface that the immutable
  # migration chain requires. pgserver's base wheel starts PostgreSQL but does
  # not install pg_trgm; fail here with the migration that first needs it,
  # before a runner can advertise a misleading verification capability.
  extension="pg_trgm"
  migration="backend/alembic/versions/0122_artifact_pr_url_trgm_index.py"
  extension_result="$(
    timeout "$COMMAND_TIMEOUT_SECONDS" "$POSTGRES_BIN/psql" \
      -h "$POSTGRES_SOCKET" -U postgres -d postgres -tAc \
      "SELECT 1 FROM pg_available_extensions WHERE name = '$extension'" 2>&1
  )" || {
    echo "Sandbox PostgreSQL verification failed: extension '$extension' required by $migration is unavailable: $extension_result" >&2
    exit 1
  }
  if [ "$(printf '%s' "$extension_result" | tr -d '[:space:]')" != "1" ]; then
    echo "Sandbox PostgreSQL verification failed: extension '$extension' required by $migration is missing (pg_available_extensions returned: $extension_result)" >&2
    exit 1
  fi
fi

echo "PROJECT_VERIFICATION_V1_OK"
