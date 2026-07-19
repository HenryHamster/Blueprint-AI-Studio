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

# Start a private, Unix-socket-only PostgreSQL sidecar inside the caller's
# isolation namespace. This is the same socketless boundary used by generated
# authoring checks: no host Docker socket or host data directory is exposed.
if [ "$PROBE_MODE" = "all" ] || [ "$PROBE_MODE" = "docker-postgres" ]; then
  POSTGRES_BIN="$(pg_config --bindir)"
  test -x "$POSTGRES_BIN/initdb"
  test -x "$POSTGRES_BIN/pg_ctl"
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
fi

echo "PROJECT_VERIFICATION_V1_OK"
