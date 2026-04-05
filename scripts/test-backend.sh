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

echo "=== Building backend image ==="
docker build -t blueprint-backend-test . -q

echo "=== Verify app imports ==="
docker run --rm $ENV_ARGS blueprint-backend-test \
  python -c "from app.main import app; print(f'OK: {len(app.routes)} routes')"

if [ "${1:-}" = "--standalone" ]; then
  echo "=== Standalone tests (no DB) ==="
  docker run --rm $ENV_ARGS blueprint-backend-test \
    python -m pytest testing/test_security.py testing/test_pipeline.py testing/test_invariants.py -v --noconftest
  exit 0
fi

if [ $# -gt 0 ]; then
  echo "=== Running: pytest $* ==="
  docker run --rm $ENV_ARGS blueprint-backend-test \
    python -m pytest "$@"
  exit 0
fi

echo "=== Standalone tests ==="
docker run --rm $ENV_ARGS blueprint-backend-test \
  python -m pytest testing/test_security.py testing/test_pipeline.py testing/test_invariants.py -v --noconftest

echo "=== Full test suite ==="
docker run --rm $ENV_ARGS blueprint-backend-test \
  python -m pytest testing/ -q --tb=short

echo "=== All backend tests passed ==="
