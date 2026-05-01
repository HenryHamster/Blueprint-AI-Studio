#!/usr/bin/env bash
# Run backend tests in Docker (mirrors CI).
#
# Defaults: real Postgres sidecar + warm testmon cache (selecting only
# tests whose dependency hashes changed since the last main snapshot).
# Both default to ON because they catch real bugs CI would otherwise
# surface only after a push (Postgres) or save 5–7 min of redundant
# pytest collection (testmon). Opt out for fast local iteration.
#
# Usage:
#   scripts/test-backend.sh                              # full suite, Postgres + testmon if warm cache present
#   scripts/test-backend.sh testing/test_agent.py        # specific file
#   scripts/test-backend.sh -k "test_name"               # specific test by name
#   scripts/test-backend.sh --standalone                 # standalone tests only (no DB fixtures)
#   scripts/test-backend.sh --no-postgres                # SQLite (fast local; misses Postgres-only failures)
#   scripts/test-backend.sh --no-testmon                 # ignore warm cache, run full suite
#   scripts/test-backend.sh --require-testmon            # skip instead of running full suite when no warm cache exists
#   scripts/test-backend.sh --postgres                   # accepted for back-compat; default is already on
set -euo pipefail
cd "$(dirname "$0")/../backend"

# --- Argument parsing ---
# --postgres: kept as a no-op flag for callers that pass it explicitly.
# --no-postgres: opt out (use in-memory SQLite for fast local dev).
# --no-testmon: opt out of warm-cache selection (run full suite).
# --require-testmon: opt out of the full-suite fallback when no cache exists.
USE_POSTGRES=1
USE_TESTMON=1
REQUIRE_TESTMON=0
ARGS=()
for arg in "$@"; do
  case "$arg" in
    --postgres) ;;  # default; no-op
    --no-postgres) USE_POSTGRES=0 ;;
    --no-testmon) USE_TESTMON=0 ;;
    --require-testmon) REQUIRE_TESTMON=1 ;;
    *) ARGS+=("$arg") ;;
  esac
done
if [ "${#ARGS[@]}" -gt 0 ]; then set -- "${ARGS[@]}"; else set --; fi

# --- Fetch warm testmon cache from orphan branch (best-effort) ---
# CI on main refreshes the orphan testmon-cache branch with a vacuumed
# .testmondata. Pulling it lets pytest --testmon-forceselect skip tests
# whose dependency hashes are unchanged from the main snapshot.
# Falls back silently to the full suite when the branch doesn't exist
# or git can't reach origin (offline, no remote, etc.).
# Pass --no-testmon (or set TESTMON_NO_FETCH=1) to opt out.
TESTMON_ARGS=()
if [ "$USE_TESTMON" = "1" ] && [ -z "${TESTMON_NO_FETCH:-}" ]; then
  # MSYS_NO_PATHCONV: stop Git Bash on Windows from rewriting
  # `origin/testmon-cache:.testmondata` to `origin\testmon-cache;.testmondata`
  # (the colon + slashes look like a Windows path to MSYS, so it
  # "translates" them and git then can't resolve the ref). On Linux this
  # env var is a harmless no-op.
  if MSYS_NO_PATHCONV=1 git -C .. fetch --depth=1 origin testmon-cache 2>/dev/null; then
    if MSYS_NO_PATHCONV=1 git -C .. show origin/testmon-cache:.testmondata > .testmondata.tmp 2>/dev/null \
       && [ -s .testmondata.tmp ] \
       && head -c 15 .testmondata.tmp | grep -q "SQLite format 3"; then
      # Validated as an actual SQLite file (not a stray git error message
      # that happened to land in stdout — see the MSYS path-conv comment
      # above; the corruption can also come from a partial fetch).
      mv .testmondata.tmp .testmondata
    else
      rm -f .testmondata.tmp
    fi
  fi
  if [ -f .testmondata ]; then
    TESTMON_ARGS=(--testmon-forceselect)
  fi
fi
if [ "$REQUIRE_TESTMON" = "1" ] && [ "${#TESTMON_ARGS[@]}" -eq 0 ]; then
  echo "=== Warm testmon cache unavailable — skipping impact coverage instead of running full suite ==="
  exit 0
fi
# When USE_TESTMON=0 we just don't append the flag; the plugin sits inert
# without an explicit --testmon* arg, so a leftover .testmondata on disk
# has no effect on the run and is preserved for the next opt-in.

# --- Cleanup: combined trap for CLAUDE.md copy + Postgres sidecar ---
PG_CONTAINER=""
PG_NETWORK=""
cleanup() {
  rm -f CLAUDE.md
  [ -n "$PG_CONTAINER" ] && docker rm -f "$PG_CONTAINER" &>/dev/null || true
  [ -n "$PG_NETWORK" ] && docker network rm "$PG_NETWORK" &>/dev/null || true
}
trap cleanup EXIT

# Copy root CLAUDE.md into the Docker build context so test_claude_md_* in
# test_invariants.py finds it at /app/CLAUDE.md inside the image — mirrors
# CI (.github/workflows/ci.yml already does this same cp before its own
# docker build). backend/CLAUDE.md is in .gitignore as a safety net.
if [ -f ../CLAUDE.md ]; then
  cp ../CLAUDE.md CLAUDE.md
fi

# --- Detect Docker; --postgres requires it ---
if command -v docker &>/dev/null; then
  USE_DOCKER=1
else
  USE_DOCKER=0
  if [ "$USE_POSTGRES" = "1" ]; then
    echo "ERROR: --postgres requires Docker" >&2
    exit 2
  fi
  echo "=== Docker not found — running pytest directly ==="
  # Ensure pip-installed binaries are on PATH
  export PATH="$HOME/.local/bin:$PATH"
  # Install dependencies if needed (use uv if pip is unavailable)
  VENV=/tmp/blueprint-backend-venv
  [ -d "$VENV" ] && export PYTHON="$VENV/bin/python"
  if ! "${PYTHON:-python3}" -c "import fastapi" &>/dev/null; then
    echo "=== Installing dependencies ==="
    if python3 -m pip install -q -r requirements.txt 2>&1 | tail -5; then
      true
    elif command -v uv &>/dev/null || (curl -LsSf https://astral.sh/uv/install.sh | sh && export PATH="$HOME/.local/bin:$PATH"); then
      [ -d "$VENV" ] || uv venv "$VENV" -q
      uv pip install --python "$VENV/bin/python" -q -r requirements.txt
      uv pip install --python "$VENV/bin/python" -q "setuptools<81"
      export PYTHON="$VENV/bin/python"
    fi
  fi
  PYTHON="${PYTHON:-python3}"
fi

# --- Configure DATABASE_URL / ENV_ARGS based on --postgres ---
if [ "$USE_POSTGRES" = "1" ]; then
  PG_NETWORK="blueprint-test-net-$$"
  PG_CONTAINER="blueprint-test-pg-$$"
  PG_DSN="postgresql+asyncpg://test:test@${PG_CONTAINER}:5432/test_blueprint"

  echo "=== Starting Postgres sidecar (${PG_CONTAINER}) ==="
  docker network create "$PG_NETWORK" >/dev/null
  docker run -d --rm --name "$PG_CONTAINER" --network "$PG_NETWORK" \
    -e POSTGRES_USER=test \
    -e POSTGRES_PASSWORD=test \
    -e POSTGRES_DB=test_blueprint \
    postgres:15 >/dev/null

  ENV_ARGS="--network $PG_NETWORK \
    -e DATABASE_URL=$PG_DSN \
    -e USE_TEST_POSTGRES=true \
    -e GCP_PROJECT_ID=test-project \
    -e LOCAL_MODE=true \
    -e AGENT_JWT_SECRET=ci-test-secret \
    -e WS_TICKET_SECRET=ci-test-secret"
else
  ENV_ARGS="-e DATABASE_URL=postgresql+asyncpg://x:x@localhost/x \
    -e GCP_PROJECT_ID=test-project \
    -e LOCAL_MODE=true \
    -e AGENT_JWT_SECRET=ci-test-secret \
    -e WS_TICKET_SECRET=ci-test-secret"
fi

# Mount admin-ui/src read-only so test_invariants.py's frontend size
# guardrails can actually run inside Docker. Without this they silently
# skip (admin-ui isn't in the backend Dockerfile build context) and
# breaches accumulate unnoticed (R6 finding). The mount target
# /admin-ui/src is one of the candidate paths in
# `_resolve_admin_ui_src_root()`. CI runs without this mount; the
# guardrails fail loudly there until the workflow is updated similarly.
if [ "$USE_DOCKER" = "1" ] && [ -d ../admin-ui/src ]; then
  ADMIN_UI_HOST_PATH="$(cd ../admin-ui/src && pwd)"
  ENV_ARGS="$ENV_ARGS -v ${ADMIN_UI_HOST_PATH}:/admin-ui/src:ro"
fi

ENV_VARS="DATABASE_URL=postgresql+asyncpg://x:x@localhost/x \
  GCP_PROJECT_ID=test-project \
  LOCAL_MODE=true \
  AGENT_JWT_SECRET=ci-test-secret \
  WS_TICKET_SECRET=ci-test-secret"

_run() {
  # _run <cmd...> — runs inside Docker or directly, with env vars set
  if [ "$USE_DOCKER" = "1" ]; then
    # MSYS_NO_PATHCONV=1: stop Git Bash on Windows from translating
    # the host path in `-v /d/path:/admin-ui/src` to a Windows-style
    # path before passing it to docker.exe (the colon + leading slash
    # look like a Windows drive ref to MSYS).
    MSYS_NO_PATHCONV=1 docker run --rm $ENV_ARGS blueprint-backend-test "$@"
  else
    # Replace leading "python3" with $PYTHON if set (venv interpreter)
    if [ -n "${PYTHON:-}" ] && [ "$1" = "python3" ]; then
      shift; env $ENV_VARS PYTHONPATH="$(pwd)" "$PYTHON" "$@"
    else
      env $ENV_VARS PYTHONPATH="$(pwd)" "$@"
    fi
  fi
}

if [ "$USE_DOCKER" = "1" ]; then
  echo "=== Building backend image ==="
  docker build -t blueprint-backend-test . -q
fi

# --- Postgres needs migrations applied before tests ---
if [ "$USE_POSTGRES" = "1" ]; then
  echo "=== Waiting for Postgres ==="
  for _ in $(seq 1 30); do
    if docker exec "$PG_CONTAINER" pg_isready -U test &>/dev/null; then break; fi
    sleep 0.5
  done
  echo "=== Running alembic migrations ==="
  _run alembic upgrade head
fi

echo "=== Verify app imports ==="
_run python3 -c "from app.main import app; print(f'OK: {len(app.routes)} routes')"

if [ "${1:-}" = "--standalone" ]; then
  echo "=== Standalone tests (no DB) ==="
  _run python3 -m pytest testing/test_security.py testing/test_pipeline.py testing/test_invariants.py -v --noconftest
  exit 0
fi

if [ $# -gt 0 ]; then
  echo "=== Running: pytest $* ==="
  _run python3 -m pytest "$@"
  exit 0
fi

echo "=== Standalone tests ==="
_run python3 -m pytest testing/test_security.py testing/test_pipeline.py testing/test_invariants.py -v --noconftest

echo "=== Full test suite ==="
if [ "${#TESTMON_ARGS[@]}" -gt 0 ]; then
  echo "(warm testmon cache present — selecting only tests with changed dependencies)"
fi
_run python3 -m pytest testing/ -q --tb=short "${TESTMON_ARGS[@]}"

echo "=== All backend tests passed ==="
