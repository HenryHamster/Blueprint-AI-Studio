#!/usr/bin/env bash

# Shared backend-test capability selection.  This file is sourced by ./check,
# the backend harness, the generated verification fragment, and the startup
# probe so they cannot disagree about which PostgreSQL runtime a host has.

resolve_pg_bindir() {
  local bindir cand sandbox_python
  sandbox_python="${BLUEPRINT_SANDBOX_PYTHON:-/opt/blueprint-backend-venv/bin/python}"

  # Prefer the declared, self-contained wheel.  This is the off-image route:
  # it works when the Packer-created interpreter and system PostgreSQL are both
  # absent, while still using the same pg_ctl/initdb contract as the harness.
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

backend_test_sandbox_available() {
  local sandbox_python
  sandbox_python="${BLUEPRINT_SANDBOX_PYTHON:-/opt/blueprint-backend-venv/bin/python}"
  [ -x "$sandbox_python" ] || return 1
  "$sandbox_python" -c 'import pgserver, pytest' >/dev/null 2>&1 || return 1
  resolve_pg_bindir >/dev/null
}

# Keep this inventory aligned with the immutable Alembic chain. The embedded
# pgserver build must expose every contrib extension before it can be selected
# for a migration-backed test run; otherwise a missing extension is reported
# much later as an opaque Alembic failure.
required_postgres_extensions() {
  printf '%s\n' \
    'pg_trgm|backend/alembic/versions/0122_artifact_pr_url_trgm_index.py' \
    'pg_trgm|backend/alembic/versions/0145_knowledge_documents.py' \
    'pg_trgm|backend/alembic/versions/0529_add_task_search_trgm_indexes.py' \
    'pg_trgm|backend/alembic/versions/0714_repair_strategic_conversation_correctness.py'
}

# Verify the command's PostgreSQL server has the extension control file. The
# command is passed as argv so this works for both a local pgserver binary and
# `docker exec`; callers must supply connection arguments before this function
# appends the query arguments.
verify_required_postgres_extensions() {
  local extension migration result
  local -a psql_command=("$@")
  while IFS='|' read -r extension migration; do
    [ -n "$extension" ] || continue
    result="$("${psql_command[@]}" -tAc \
      "SELECT 1 FROM pg_available_extensions WHERE name = '$extension'" 2>&1)" || {
      echo "ERROR: PostgreSQL extension '$extension' required by $migration is unavailable in the selected server: $result" >&2
      return 1
    }
    if [ "$(printf '%s' "$result" | tr -d '[:space:]')" != "1" ]; then
      echo "ERROR: PostgreSQL extension '$extension' required by $migration is missing from the selected server (pg_available_extensions returned: $result)" >&2
      return 1
    fi
  done < <(required_postgres_extensions)
}

select_backend_test_route() {
  if backend_test_sandbox_available; then
    printf '%s\n' --sandbox-postgres
    return 0
  fi
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    printf '%s\n' --postgres
    return 0
  fi
  return 1
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  select_backend_test_route || {
    echo "ERROR: no usable PostgreSQL test runtime (pgserver sandbox or Docker)" >&2
    exit 2
  }
fi
