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
