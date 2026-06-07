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

# --- Memory guard: refuse the full Docker+Postgres suite on memory-constrained
# hosts (Blueprint's agent VM is e2-standard-2 = 8 GB and cannot fit the
# build + sidecar + pytest peak alongside the agent + Claude subprocess —
# tasks 53ef061b / b150b4d6 / b15b6813 all OOM'd here 2026-05-05). The
# guard exits with a clear message instead of letting the OOM-killer fire
# silently. CI and 16 GB+ dev machines pass through.
# Bypass with ALLOW_LOW_MEM=1 (or pass --standalone, or specify test files).
if [ -r /proc/meminfo ] && [ -z "${ALLOW_LOW_MEM:-}" ]; then
  AVAIL_MB=$(awk '/^MemAvailable:/ {print int($2/1024); exit}' /proc/meminfo 2>/dev/null || echo 99999)
  IS_FULL_SUITE=1
  for arg in "$@"; do
    case "$arg" in
      --standalone|-k|*.py|*::*) IS_FULL_SUITE=0 ;;
    esac
  done
  if [ "$IS_FULL_SUITE" = "1" ] && [ "$AVAIL_MB" -lt 4096 ]; then
    echo "ERROR: Available memory ${AVAIL_MB}MB is below 4GB — full suite will likely OOM." >&2
    echo "  Use --standalone for a lightweight pass (3 invariant files, no DB)," >&2
    echo "  or pass a specific test file (testing/test_<x>.py), or set ALLOW_LOW_MEM=1." >&2
    exit 2
  fi
fi

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

# --- Docker Hub rate-limit resilience --------------------------------------
# Agent VMs share egress and routinely hit Docker Hub's unauthenticated pull
# rate limit, which kills the docker build below (FROM python:3.11-slim) and
# fails the mandatory PR gate repo-wide regardless of the diff. Same recovery
# as CI (ci.yml, #7434): retry the pull, then fall back to mirror.gcr.io
# (Google's public pull-through cache of Docker Hub library images — it rides
# a different network path) and retag so FROM / docker run resolve the image
# locally without re-pulling. A failed pull is non-fatal when the image is
# already in the local cache.
ensure_image() {
  local image="$1" src i ok=
  for src in "$image" "mirror.gcr.io/library/$image"; do
    for i in 1 2 3; do
      if docker pull "$src" >/dev/null 2>&1; then ok=1; break; fi
      echo "WARN: $src pull failed (attempt $i/3) — retrying" >&2
      sleep $((i * 10))
    done
    if [ -n "$ok" ]; then
      [ "$src" = "$image" ] || docker tag "$src" "$image"
      return 0
    fi
  done
  if docker image inspect "$image" >/dev/null 2>&1; then
    echo "WARN: could not pull $image — using locally cached copy" >&2
    return 0
  fi
  echo "ERROR: could not pull $image from Docker Hub or mirror.gcr.io" >&2
  return 1
}

# --- Prebuilt deps image fast path -----------------------------------------
# CI publishes a deps-only image (the apt + pip layers — everything that only
# changes when requirements.txt / Dockerfile changes) to GHCR, keyed by a hash
# of those two files (see .github/workflows/ci.yml: the `backend-deps` job and
# the shards' "Resolve backend deps image" step). On agent VMs a full
# `docker build` (base pull + pip install of ~37 packages) costs 1–19 min and
# runs ~2×/task — the dominant pre-PR gate cost. Pulling the cached deps image
# and bind-mounting the source over /app (mirroring CI exactly) skips that
# build entirely, saving ~15–18 min per invocation.
#
# Auth: the GHCR package is public, so an ANONYMOUS `docker pull` is the primary
# path — no token plumbing required. An optional GHCR_TOKEN env enables a
# private-package fallback (login before pull) without changing the happy path.
#
# Safety: this is strictly a speedup. If the pull fails or the tag doesn't exist
# (e.g. a same-PR requirements.txt change whose deps tag CI hasn't pushed yet),
# we WARN loudly and fall back to the exact `docker build` behavior below — so
# this path is never worse than today. The pull is wrapped in `timeout` so a
# slow-but-not-failing pull can't eat the test-runner budget before we decide to
# fall back. Set BLUEPRINT_NO_DEPS_IMAGE=1 to force the old local-build path.
DEPS_IMAGE=""          # non-empty ⇒ fast path: bind-mount source onto this image
IMAGE_PATH_TAKEN=""    # "pulled-deps-image" | "local-build" (logged for fleet observability)
GHCR_DEPS_REPO="ghcr.io/henryhamster/backend-test-deps"  # owner lowercased, mirrors CI
DEPS_PULL_TIMEOUT="${BLUEPRINT_DEPS_PULL_TIMEOUT:-180}"   # seconds; cap a slow pull

# Try to pull the prebuilt deps image. On success sets DEPS_IMAGE and returns 0;
# on any failure leaves DEPS_IMAGE empty and returns 1 so the caller falls back
# to a local build. Never exits the script (callers handle the fallback).
resolve_deps_image() {
  if [ -n "${BLUEPRINT_NO_DEPS_IMAGE:-}" ]; then
    echo "=== BLUEPRINT_NO_DEPS_IMAGE set — using local docker build ===" >&2
    return 1
  fi
  if ! command -v sha256sum >/dev/null 2>&1; then
    echo "WARN: sha256sum unavailable — cannot key the deps image, using local docker build" >&2
    return 1
  fi
  # reqhash = sha256(requirements.txt + Dockerfile)[:16] — must match CI's
  # `cat backend/requirements.txt backend/Dockerfile | sha256sum | cut -c1-16`.
  # We run from the backend dir, so the files are alongside us.
  local reqhash
  reqhash="$(cat requirements.txt Dockerfile | sha256sum | cut -c1-16)"
  if [ -z "$reqhash" ]; then
    echo "WARN: could not compute deps hash — using local docker build" >&2
    return 1
  fi
  local img="$GHCR_DEPS_REPO:$reqhash"

  # Optional private-package fallback: log in if a token is provided. The
  # primary (public-package) path needs no login. A failed login is non-fatal —
  # the anonymous pull below may still succeed, and if not we fall back.
  if [ -n "${GHCR_TOKEN:-}" ]; then
    echo "${GHCR_TOKEN}" | docker login ghcr.io \
      -u "${GHCR_USER:-blueprint-ci}" --password-stdin >/dev/null 2>&1 \
      || echo "WARN: ghcr login failed — trying anonymous pull" >&2
  fi

  echo "=== Pulling prebuilt deps image $img (timeout ${DEPS_PULL_TIMEOUT}s) ===" >&2
  if timeout "$DEPS_PULL_TIMEOUT" docker pull "$img" >/dev/null 2>&1; then
    DEPS_IMAGE="$img"
    return 0
  fi
  echo "WARN: deps image $img unavailable (pull failed, missing tag, or timeout)" >&2
  echo "WARN: falling back to local docker build — slower but identical result" >&2
  return 1
}

# --- Configure DATABASE_URL / ENV_ARGS based on --postgres ---
if [ "$USE_POSTGRES" = "1" ]; then
  PG_NETWORK="blueprint-test-net-$$"
  PG_CONTAINER="blueprint-test-pg-$$"
  PG_DSN="postgresql+asyncpg://test:test@${PG_CONTAINER}:5432/test_blueprint"

  echo "=== Starting Postgres sidecar (${PG_CONTAINER}) ==="
  ensure_image postgres:15
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

# Mount agent/src read-only so test_invariants.py's SR category parity
# check can parse the live prompt.ts instead of relying on the static
# AGENT_SELF_REVIEW_CATEGORIES mirror. The mount target /agent/src is
# one of the candidate paths in `_resolve_prompt_ts_path()`.
if [ "$USE_DOCKER" = "1" ] && [ -d ../agent/src ]; then
  MSYS_NO_PATHCONV=1 AGENT_SRC_HOST_PATH="$(cd ../agent/src && pwd)"
  ENV_ARGS="$ENV_ARGS -v ${AGENT_SRC_HOST_PATH}:/agent/src:ro"
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
    if [ -n "$DEPS_IMAGE" ]; then
      # Fast path: the deps image has no source baked in (it's the `deps`
      # stage), so bind-mount the backend source over /app — exactly as the
      # CI shards do. $PWD is the backend dir here (we cd'd at the top), so
      # /app/CLAUDE.md (cp'd above), the agent/admin-ui mounts in $ENV_ARGS,
      # alembic, and `app.*` all resolve identically to the built image.
      MSYS_NO_PATHCONV=1 docker run --rm -v "$PWD:/app" $ENV_ARGS "$DEPS_IMAGE" "$@"
    else
      MSYS_NO_PATHCONV=1 docker run --rm $ENV_ARGS blueprint-backend-test "$@"
    fi
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
  # Fast path: pull CI's prebuilt deps image and bind-mount the source (set in
  # _run). Falls back loudly to the local build on any failure — never worse
  # than today. The chosen path is logged so fleet fallback-rate is observable.
  if resolve_deps_image; then
    IMAGE_PATH_TAKEN="pulled-deps-image"
    echo "=== Using prebuilt deps image (skipping local build): $DEPS_IMAGE ==="
  else
    IMAGE_PATH_TAKEN="local-build"
    echo "=== Building backend image ==="
    ensure_image python:3.11-slim
    docker build -t blueprint-backend-test . -q
  fi
  echo "=== test-backend image path: ${IMAGE_PATH_TAKEN} ==="
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
