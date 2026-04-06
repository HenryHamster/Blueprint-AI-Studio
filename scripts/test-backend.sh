#!/usr/bin/env bash
# Run backend tests in Docker (mirrors CI).
# Usage:
#   scripts/test-backend.sh                       # full CI suite (import check + all tests)
#   scripts/test-backend.sh testing/test_agent.py  # specific file
#   scripts/test-backend.sh -k "test_name"         # specific test by name
#   scripts/test-backend.sh --standalone           # standalone tests only (no DB fixtures)
set -euo pipefail
cd "$(dirname "$0")/../backend"

ENV_ARGS="-e DATABASE_URL=postgresql+asyncpg://x:x@localhost/x \
  -e GCP_PROJECT_ID=test-project \
  -e LOCAL_MODE=true \
  -e AGENT_JWT_SECRET=ci-test-secret \
  -e WS_TICKET_SECRET=ci-test-secret"

ENV_VARS="DATABASE_URL=postgresql+asyncpg://x:x@localhost/x \
  GCP_PROJECT_ID=test-project \
  LOCAL_MODE=true \
  AGENT_JWT_SECRET=ci-test-secret \
  WS_TICKET_SECRET=ci-test-secret"

# Detect whether Docker is available; fall back to running pytest directly.
if command -v docker &>/dev/null; then
  USE_DOCKER=1
else
  USE_DOCKER=0
  echo "=== Docker not found — running pytest directly ==="
  # Ensure pip-installed binaries are on PATH
  export PATH="$HOME/.local/bin:$PATH"
  # Install dependencies if needed
  if ! python3 -c "import fastapi" &>/dev/null; then
    echo "=== Installing dependencies ==="
    python3 -m pip install -q -r requirements.txt 2>&1 | tail -5 || true
  fi
fi

_run() {
  # _run <cmd...> — runs inside Docker or directly, with env vars set
  if [ "$USE_DOCKER" = "1" ]; then
    docker run --rm $ENV_ARGS blueprint-backend-test "$@"
  else
    env $ENV_VARS PYTHONPATH="$(pwd)" "$@"
  fi
}

if [ "$USE_DOCKER" = "1" ]; then
  echo "=== Building backend image ==="
  docker build -t blueprint-backend-test . -q
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
