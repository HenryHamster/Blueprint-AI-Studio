#!/bin/bash
# check-viewport.sh — run admin-ui viewport suite with Docker fallback for Chromium bootstrap failures.
# Exits 0 on success or unavailable VM substrate, 1 on real test assertion failures.

VPORT_ARGS=(
  VITE_API_URL=http://localhost:8080
  VITE_FIREBASE_API_KEY=ci
  VITE_FIREBASE_AUTH_DOMAIN=ci.firebaseapp.com
  VITE_FIREBASE_PROJECT_ID=ci
)

BOOTSTRAP_PATTERNS="libatk|libxcomposite|libgbm|libnss3|libasound|cannot open shared object file|executable doesn|host system is missing|please install|npx playwright install|run: playwright install|missing browser|missing chromium|eacces.*unlink.*vite|permission denied.*unlink"

cd /tmp/workspace/admin-ui

# Try native run first
OUT=$(env "${VPORT_ARGS[@]}" npm run test:viewport 2>&1)
STATUS=$?
echo "$OUT"
if [ "$STATUS" -eq 0 ]; then
  # Confirm tests actually ran and passed
  if echo "$OUT" | grep -qE "Tests +[0-9]+ passed"; then
    exit 0
  fi
  echo "[check] admin-ui viewport: test suite exited 0 but no test assertions found"
  exit 1
fi

# Native failed — check if it's a Chromium bootstrap failure
if ! echo "$OUT" | grep -qiE "$BOOTSTRAP_PATTERNS"; then
  echo "[check] admin-ui viewport suite requires Playwright Chromium. If this VM is missing it, run: cd admin-ui && npx playwright install --with-deps chromium"
  exit 1
fi

echo "[check] admin-ui viewport: Chromium bootstrap failure detected."

# Try Docker fallback (matches agent harness runViewportSuiteWithFallback behavior)
if ! docker info >/dev/null 2>&1; then
  echo "VIEWPORT_CAPTURE_SKIPPED: Chromium bootstrap failure detected, Docker unavailable; CI frontend-viewport enforces browser verification"
  exit 0
fi

PLAYWRIGHT_IMAGE=mcr.microsoft.com/playwright:v1.59.1-jammy
echo "[check] Retrying viewport suite in Docker ($PLAYWRIGHT_IMAGE)..."
DOCKER_OUT=$(docker run --rm \
  --user "$(id -u):$(id -g)" \
  -v /tmp/workspace:/work \
  -w /work/admin-ui \
  -e VITE_API_URL=http://localhost:8080 \
  -e VITE_FIREBASE_API_KEY=ci \
  -e VITE_FIREBASE_AUTH_DOMAIN=ci.firebaseapp.com \
  -e VITE_FIREBASE_PROJECT_ID=ci \
  "$PLAYWRIGHT_IMAGE" npm run test:viewport 2>&1)
DOCKER_STATUS=$?
echo "$DOCKER_OUT"

if [ "$DOCKER_STATUS" -eq 0 ]; then
  # Confirm tests actually ran and passed
  if echo "$DOCKER_OUT" | grep -qE "Tests +[0-9]+ passed"; then
    echo "[check] admin-ui viewport passed in Docker."
    exit 0
  fi
  echo "[check] admin-ui viewport: Docker run exited 0 but no test assertions found"
  exit 1
fi

# Docker also failed — check whether any test ASSERTIONS failed (vs file-import errors only)
if echo "$DOCKER_OUT" | grep -qE "Tests +[0-9]+ failed"; then
  echo "[check] admin-ui viewport: Docker run has test assertion failures — blocking."
  exit 1
fi

# Docker failed without test assertions — check if bootstrap errors persisted into Docker
if echo "$DOCKER_OUT" | grep -qiE "$BOOTSTRAP_PATTERNS"; then
  echo "VIEWPORT_CAPTURE_SKIPPED: Chromium bootstrap failure persists in Docker fallback; CI frontend-viewport enforces browser verification"
  exit 0
fi

# Docker run failed without detected test assertions or bootstrap errors
echo "[check] admin-ui viewport Docker run failed (exit $DOCKER_STATUS) without recognized test assertions or bootstrap errors — blocking."
exit 1
