#!/usr/bin/env bash
# Run backend tests in Docker (mirrors CI).
# Usage:
#   scripts/test-backend.sh                              # full suite, in-memory SQLite (fast)
#   scripts/test-backend.sh testing/test_agent.py        # specific file
#   scripts/test-backend.sh -k "test_name"               # specific test by name
#   scripts/test-backend.sh --standalone                 # standalone tests only (no DB fixtures)
#   scripts/test-backend.sh --postgres [...]             # use real Postgres sidecar (catches enum/JSONB/migration drift)
set -euo pipefail
cd "$(dirname "$0")/../backend"

# --- Argument parsing: extract --postgres without disturbing other args ---
USE_POSTGRES=0
ARGS=()
for arg in "$@"; do
  if [ "$arg" = "--postgres" ]; then
    USE_POSTGRES=1
  else
    ARGS+=("$arg")
  fi
done
if [ "${#ARGS[@]}" -gt 0 ]; then set -- "${ARGS[@]}"; else set --; fi

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

ENV_VARS="DATABASE_URL=postgresql+asyncpg://x:x@localhost/x \
  GCP_PROJECT_ID=test-project \
  LOCAL_MODE=true \
  AGENT_JWT_SECRET=ci-test-secret \
  WS_TICKET_SECRET=ci-test-secret"

_run() {
  # _run <cmd...> — runs inside Docker or directly, with env vars set
  if [ "$USE_DOCKER" = "1" ]; then
    docker run --rm $ENV_ARGS blueprint-backend-test "$@"
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
_run python3 -m pytest testing/ -q --tb=short

echo "=== All backend tests passed ==="
