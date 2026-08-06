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

# Resolve a usable PostgreSQL server bindir. Prefers `pg_config --bindir` when
# it is on PATH and points at a real server (images with libpq-dev /
# postgresql-client install pg_config on PATH). When pg_config is absent from
# PATH, falls back to the highest-versioned /usr/lib/postgresql/*/bin that
# actually holds initdb (an image with only the server packages still has the
# server binaries there). Prints the bindir and returns 0; returns 1 when
# neither exists — a pg_config that exists but points at a non-server dir stays
# fail-closed rather than scanning. A pg_config that EXISTS but exits non-zero
# (a broken wrapper) must not falsely deny the capability: the guarded
# substitution above captures whatever it prints and the bindir check keeps the
# decision fail-closed. Keep this helper byte-identical across check,
# scripts/test-backend.sh, the generated check_backend_verification.sh fragment
# and this probe — mirrored paths must not diverge again.
resolve_pg_bindir() {
  local bindir cand
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
  POSTGRES_BIN="$(resolve_pg_bindir)" || {
    echo "Sandbox PostgreSQL verification failed: no usable PostgreSQL server bindir" >&2
    exit 1
  }
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
