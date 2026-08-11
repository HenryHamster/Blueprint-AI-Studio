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
#   scripts/test-backend.sh --sandbox-postgres           # private local Postgres; no Docker socket required
set -euo pipefail
cd "$(dirname "$0")/../backend"
source ../scripts/backend-test-route.sh

# The real upgrade chain contains fail-closed encrypted-data migrations. Keep a
# deterministic non-production Fernet key in the repo-owned harness so Alembic
# proves the whole chain without weakening migration behavior or relying on the
# pytest plaintext opt-in fixture (which runs only after migrations).
TEST_ENCRYPTION_KEY="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="

# --- Argument parsing ---
# --postgres: kept as a no-op flag for callers that pass it explicitly.
# --no-postgres: opt out (use in-memory SQLite for fast local dev).
# --no-testmon: opt out of warm-cache selection (run full suite).
# --require-testmon: opt out of the full-suite fallback when no cache exists.
USE_POSTGRES=1
USE_TESTMON=1
REQUIRE_TESTMON=0
SANDBOX_POSTGRES=0
ARGS=()
for arg in "$@"; do
  case "$arg" in
    --postgres) ;;  # default; no-op
    --sandbox-postgres) SANDBOX_POSTGRES=1 ;;
    --no-postgres) USE_POSTGRES=0 ;;
    --no-testmon) USE_TESTMON=0 ;;
    --require-testmon) REQUIRE_TESTMON=1 ;;
    *)
      case "$arg" in
        backend/*) ARGS+=("${arg#backend/}") ;;
        *) ARGS+=("$arg") ;;
      esac
      ;;
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
PG_DATA=""
PG_SOCKET=""
PG_BIN=""
VERIFICATION_OUTPUT_DIR=""
VERIFICATION_OUTPUT_DIR_OWNED=0
cleanup() {
  rm -f CLAUDE.md
  [ -n "$PG_CONTAINER" ] && docker rm -f "$PG_CONTAINER" &>/dev/null || true
  [ -n "$PG_NETWORK" ] && docker network rm "$PG_NETWORK" &>/dev/null || true
  if [ -n "$PG_BIN" ] && [ -n "$PG_DATA" ] && [ -d "$PG_DATA" ]; then
    "$PG_BIN/pg_ctl" -D "$PG_DATA" -m immediate stop &>/dev/null || true
  fi
  [ -n "$PG_DATA" ] && rm -rf "$PG_DATA" || true
  [ -n "$PG_SOCKET" ] && rm -rf "$PG_SOCKET" || true
  [ "$VERIFICATION_OUTPUT_DIR_OWNED" = "1" ] && [ -n "$VERIFICATION_OUTPUT_DIR" ] && rm -rf "$VERIFICATION_OUTPUT_DIR" || true
}
trap cleanup EXIT

# Copy root CLAUDE.md into the Docker build context so test_claude_md_* in
# test_invariants.py finds it at /app/CLAUDE.md inside the image — mirrors
# CI (.github/workflows/ci.yml already does this same cp before its own
# docker build). backend/CLAUDE.md is in .gitignore as a safety net.
if [ -f ../CLAUDE.md ]; then
  cp ../CLAUDE.md CLAUDE.md
fi

# --- Select the execution boundary. The authoring-verification path uses a
# private PostgreSQL process plus a baked read-only venv inside bwrap, never the
# host Docker socket. Ordinary developer/CI calls retain the Docker path. ---
if [ "$SANDBOX_POSTGRES" = "1" ]; then
  USE_DOCKER=0
  PYTHON="${BLUEPRINT_SANDBOX_PYTHON:-/opt/blueprint-backend-venv/bin/python}"
  [ -x "$PYTHON" ] || {
    echo "ERROR: --sandbox-postgres requires $PYTHON" >&2
    exit 2
  }
elif [ "$USE_POSTGRES" = "1" ] && _AUTO_POSTGRES_FLAG="$(select_backend_test_route 2>/dev/null)" && [ "$_AUTO_POSTGRES_FLAG" = "--sandbox-postgres" ]; then
  SANDBOX_POSTGRES=1
  USE_DOCKER=0
  PYTHON="${BLUEPRINT_SANDBOX_PYTHON:-/opt/blueprint-backend-venv/bin/python}"
  echo "=== Using off-image pgserver PostgreSQL sandbox ==="
elif command -v docker &>/dev/null && docker info >/dev/null 2>&1; then
  USE_DOCKER=1
else
  USE_DOCKER=0
  if [ "$USE_POSTGRES" = "1" ]; then
    echo "ERROR: --postgres requires a usable Docker daemon" >&2
    exit 2
  fi
  echo "=== Docker unavailable — running pytest directly ==="
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

  # Verify Python 3.11+ is available (required for asyncio.timeout)
  PYTHON_VERSION=$("$PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "unknown")
  if [ "$PYTHON_VERSION" != "unknown" ]; then
    MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
    MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)
    if [ "$MAJOR" -lt 3 ] || ([ "$MAJOR" -eq 3 ] && [ "$MINOR" -lt 11 ]); then
      # Try to find python3.11
      PYTHON_311=""
      for candidate in python3.11 python311 python3.11.* /usr/bin/python3.11 /usr/local/bin/python3.11; do
        if command -v "$candidate" &>/dev/null 2>&1; then
          PYTHON_311="$candidate"
          break
        fi
      done
      if [ -n "$PYTHON_311" ]; then
        echo "=== Using Python 3.11 (found $PYTHON_311; system python is $PYTHON_VERSION) ==="
        PYTHON="$PYTHON_311"
      else
        echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!" >&2
        echo "!!! WARNING: NON-AUTHORITATIVE ENVIRONMENT !!!" >&2
        echo "!!!" >&2
        echo "!!! System Python is $PYTHON_VERSION, but the backend requires Python 3.11+." >&2
        echo "!!! Tests will use $PYTHON_VERSION and will likely fail with asyncio.timeout" >&2
        echo "!!! AttributeError and other compatibility errors." >&2
        echo "!!!" >&2
        echo "!!! To run tests properly, either:" >&2
        echo "!!!   1. Install Docker (scripts/test-backend.sh will use the Docker image)" >&2
        echo "!!!   2. Install Python 3.11 on this system" >&2
        echo "!!!" >&2
        echo "!!! Proceeding anyway (test results may be misleading)..." >&2
        echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!" >&2
      fi
    fi
  fi
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
# The backend test image is two-stage: a `deps` stage (the apt shellcheck + pip
# layers — everything that only changes when requirements.txt / Dockerfile
# changes) and an `app` stage (= COPY . .). On a fresh agent VM a full
# `docker build` of the deps stage (base pull + pip install of ~37 packages)
# costs 1–19 min and runs ~2×/task — the dominant pre-PR gate cost. Skipping
# that build and bind-mounting the source over /app (mirroring CI exactly) saves
# ~15–18 min per invocation.
#
# We resolve the deps image in three tiers, fastest first:
#   1. LOCAL baked image: infra/packer/agent.pkr.hcl bakes
#      `blueprint-backend-test-deps:<reqhash>` into the VM image's docker store
#      at bake time, so on a booted agent VM this is a zero-network
#      `docker image inspect` hit — no pull, no build. This is the primary
#      fast path on agent VMs and is the reason this file exists.
#   2. GHCR pull: `ghcr.io/henryhamster/backend-test-deps:<reqhash>` (what CI
#      publishes). The package is PRIVATE, so an anonymous pull only succeeds
#      when GHCR_TOKEN is supplied; otherwise this tier is skipped fast. Useful
#      on dev machines / CI-adjacent hosts that have a token but no baked image.
#   3. Local `docker build --target deps` (the original behavior) — always
#      correct, just slow.
#
# reqhash = sha256(requirements.txt + Dockerfile)[:16] — IDENTICAL across the
# local baked tag (agent.pkr.hcl), the GHCR tag (ci.yml: `backend-deps`), and
# the computation below — so a requirements.txt/Dockerfile change cleanly misses
# all prebuilt tiers and falls through to a local build until the next CI push /
# packer bake catches up. No image can ever be silently stale.
#
# No public exposure: tier 1 needs no registry at all (the image lives only in
# the VM's local docker store), and tier 2 stays a PRIVATE GHCR package gated on
# GHCR_TOKEN. Nothing here makes any package public.
#
# Safety: this is strictly a speedup. If every tier misses (e.g. a same-PR
# requirements.txt change whose deps tag isn't baked/pushed yet) we WARN loudly
# and fall back to the exact `docker build` behavior below — so this path is
# never worse than today. The pull is wrapped in `timeout` so a
# slow-but-not-failing pull can't eat the test-runner budget before we decide to
# fall back. Set BLUEPRINT_NO_DEPS_IMAGE=1 to force the old local-build path.
DEPS_IMAGE=""          # non-empty ⇒ fast path: bind-mount source onto this image
IMAGE_PATH_TAKEN=""    # "baked-deps-image" | "pulled-deps-image" | "local-build" (logged for fleet observability)
GHCR_DEPS_REPO="ghcr.io/henryhamster/backend-test-deps"  # owner lowercased, mirrors CI
LOCAL_DEPS_REPO="blueprint-backend-test-deps"            # baked into the agent VM image by agent.pkr.hcl
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
  # Tier 1 — LOCAL baked image (agent.pkr.hcl bakes this into the VM image's
  # docker store at bake time). This is the primary fast path on agent VMs: a
  # zero-network `docker image inspect`, no pull, no build. Deterministic (an
  # explicit reqhash-tagged image, not docker-build-cache magic) and
  # reqhash-gated — a requirements.txt/Dockerfile change misses this tag and
  # falls through to the next tier. Set BLUEPRINT_NO_LOCAL_DEPS_IMAGE=1 to skip
  # (e.g. to force-exercise the GHCR/build path on a baked VM).
  local local_img="$LOCAL_DEPS_REPO:$reqhash"
  if [ -z "${BLUEPRINT_NO_LOCAL_DEPS_IMAGE:-}" ] \
     && docker image inspect "$local_img" >/dev/null 2>&1; then
    echo "=== Using locally baked deps image $local_img (no pull, no build) ===" >&2
    DEPS_IMAGE="$local_img"
    return 0
  fi

  local img="$GHCR_DEPS_REPO:$reqhash"

  # Tier 2 — GHCR pull. The package is PRIVATE, so a login is required: skip the
  # pull entirely when no GHCR_TOKEN is present (an anonymous pull of a private
  # package just fails after a network round-trip). A failed login is non-fatal —
  # we fall through to a local build.
  if [ -z "${GHCR_TOKEN:-}" ]; then
    echo "WARN: no GHCR_TOKEN and no baked deps image — falling back to local docker build" >&2
    return 1
  fi
  echo "${GHCR_TOKEN}" | docker login ghcr.io \
    -u "${GHCR_USER:-blueprint-ci}" --password-stdin >/dev/null 2>&1 \
    || echo "WARN: ghcr login failed — pull will likely fail, then we fall back" >&2

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
if [ "$USE_POSTGRES" = "1" ] && [ "$SANDBOX_POSTGRES" = "1" ]; then
  PG_BIN="$(resolve_pg_bindir)" || {
    echo "ERROR: --sandbox-postgres requires a PostgreSQL server bindir (pg_config on PATH, or /usr/lib/postgresql/*/bin with initdb)" >&2
    exit 2
  }
  PG_DATA=$(mktemp -d /tmp/blueprint-test-pg.XXXXXX)
  PG_SOCKET=$(mktemp -d /tmp/blueprint-test-pg-sock.XXXXXX)
  "$PG_BIN/initdb" -D "$PG_DATA" -A trust --no-locale --encoding=UTF-8 -U test >/dev/null
  "$PG_BIN/pg_ctl" -D "$PG_DATA" \
    -o "-F -c listen_addresses='' -k '$PG_SOCKET'" -w start >/dev/null
  "$PG_BIN/createdb" -h "$PG_SOCKET" -U test test_blueprint
  PG_DSN="postgresql+asyncpg://test@/test_blueprint?host=$PG_SOCKET"
  ENV_ARGS=""
elif [ "$USE_POSTGRES" = "1" ]; then
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
    -e GCE_METADATA_IP=127.0.0.1 \
    -e GCE_METADATA_HOST=127.0.0.1:9 \
    -e GCP_PROJECT_ID=test-project \
    -e LOCAL_MODE=true \
    -e GITHUB_PAT_ENCRYPTION_KEY=$TEST_ENCRYPTION_KEY \
    -e AGENT_JWT_SECRET=ci-test-secret-min-32-bytes-xxxxx \
    -e WS_TICKET_SECRET=ci-test-ws-secret-min-32-bytes-xx"
else
  ENV_ARGS="-e DATABASE_URL=postgresql+asyncpg://x:x@localhost/x \
    -e GCE_METADATA_IP=127.0.0.1 \
    -e GCE_METADATA_HOST=127.0.0.1:9 \
    -e GCP_PROJECT_ID=test-project \
    -e LOCAL_MODE=true \
    -e GITHUB_PAT_ENCRYPTION_KEY=$TEST_ENCRYPTION_KEY \
    -e AGENT_JWT_SECRET=ci-test-secret-min-32-bytes-xxxxx \
    -e WS_TICKET_SECRET=ci-test-ws-secret-min-32-bytes-xx"
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

# Mount the repo root read-only so the operator CLI test suite
# (scripts/test_blueprint_cli.py) can resolve REPO_ROOT to /repo and find
# the ./check and ./blueprint wrappers it tests for existence/syntax.
if [ "$USE_DOCKER" = "1" ] && [ -d .. ]; then
  MSYS_NO_PATHCONV=1 REPO_HOST_PATH="$(cd .. && pwd)"
  ENV_ARGS="$ENV_ARGS -v ${REPO_HOST_PATH}:/repo:ro"
fi

# The static release guard supplies a host-side temporary JUnit path. Docker
# containers cannot write to that path unless its directory is mounted, and
# the container also needs the remapped path in its environment. Keep the
# caller's filename so the host can parse the report after `docker run` exits.
if [ -n "${BLUEPRINT_STATIC_GUARD_JUNIT:-}" ]; then
  STATIC_GUARD_OUTPUT_DIR="$(dirname -- "$BLUEPRINT_STATIC_GUARD_JUNIT")"
  STATIC_GUARD_OUTPUT_NAME="$(basename -- "$BLUEPRINT_STATIC_GUARD_JUNIT")"
  [ -d "$STATIC_GUARD_OUTPUT_DIR" ] || {
    echo "ERROR: static guard JUnit output directory is unavailable: $STATIC_GUARD_OUTPUT_DIR" >&2
    exit 2
  }
  if [ "$USE_DOCKER" = "1" ]; then
    ENV_ARGS="$ENV_ARGS -v ${STATIC_GUARD_OUTPUT_DIR}:/static-guard-output"
    BLUEPRINT_STATIC_GUARD_JUNIT="/static-guard-output/${STATIC_GUARD_OUTPUT_NAME}"
    ENV_ARGS="$ENV_ARGS -e BLUEPRINT_STATIC_GUARD_JUNIT=$BLUEPRINT_STATIC_GUARD_JUNIT"
  fi
fi
if [ -n "${BLUEPRINT_STATIC_GUARD_SELECTORS:-}" ] && [ "$USE_DOCKER" = "1" ]; then
  ENV_ARGS="$ENV_ARGS -e BLUEPRINT_STATIC_GUARD_SELECTORS=$BLUEPRINT_STATIC_GUARD_SELECTORS"
fi

ENV_VARS="DATABASE_URL=${PG_DSN:-postgresql+asyncpg://x:x@localhost/x} \
  USE_TEST_POSTGRES=$([ "$USE_POSTGRES" = "1" ] && echo true || echo false) \
  GCE_METADATA_IP=127.0.0.1 \
  GCE_METADATA_HOST=127.0.0.1:9 \
  GCP_PROJECT_ID=test-project \
  LOCAL_MODE=true \
  GITHUB_PAT_ENCRYPTION_KEY=$TEST_ENCRYPTION_KEY \
  AGENT_JWT_SECRET=ci-test-secret-min-32-bytes-xxxxx \
  WS_TICKET_SECRET=ci-test-ws-secret-min-32-bytes-xx"

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

# These are all roots containing test_*.py modules that the backend test gates
# must inspect for silent zero-test stubs. Keep this derived from the repository
# layout rather than maintaining a second per-file coverage roster.
CI_TEST_MODULE_ROOTS=(
  scripts
  backend/testing
)

# This is the executing roster for scripts/test_*.py. Keep every file either
# here or in CI_SCRIPT_TEST_EXEMPTIONS below: backend/testing's CI-compliance
# tripwire enumerates scripts/ on disk, rejects unclassified tests, and rejects
# classified files that contain no pytest- or unittest-discoverable test. The
# harness is the path used by scripts/static_release_guard.py; the workflow's
# existing runner-script job covers a subset independently.
CI_SCRIPT_TEST_ROSTER=(
  test_audit_tasks_prs.py
  test_blueprint_cli.py
  test_blueprint_livekit_cli.py
  test_blueprint_mcp.py
  test_blueprint_node.py
  test_blueprint_runner.py
  test_bootstrap_local_app_release.py
  test_frontend_release_manifest.py
  test_gcs_create_only_upload.py
  test_github_actions_runner.py
  test_github_actions_vm.py
  test_guard_gate_left_shift.py
  test_hermes_operator_cli.py
  test_live_source_rehearsal_export.py
  test_local_api_service.py
  test_local_app_release.py
  test_local_control_plane_services.py
  test_local_postgres_backup_service.py
  test_local_postgres_cutover.py
  test_local_postgres_cutover_integration.py
  test_local_postgres_rehearsal.py
  test_local_postgres_service.py
  test_local_runner.py
  test_local_runner_livekit_guard.py
  test_migrate_container_storage.py
  test_runner_isolation.py
  test_tailscale_funnel_service.py
  test_viewport_manifest.py
  test_walkthrough_stack.py
)

# Entries in this list need a capability that the Docker-less standalone lane
# does not provide. They remain in the CI roster and are collected when the
# Docker capability is present; the reason makes a local omission reviewable.
CI_SCRIPT_TEST_EXEMPTIONS=(
  "test_blueprint_cli.py|requires Blueprint operator API configuration and control-plane network"
  "test_blueprint_node.py|requires a root-owned system Python and systemd deployment host"
  "test_local_postgres_cutover_integration.py|requires a Docker daemon and two real PostgreSQL containers"
  "test_local_runner.py|requires an enrolled local runner and its host services"
)

STANDALONE_SCRIPT_TEST_ARGS=()
STANDALONE_RELEASE_TEST_ARGS=()
if [ "$USE_DOCKER" = "1" ]; then
  SCRIPT_TEST_ROOT=/repo/scripts
else
  # Docker-less local runs are rooted at the checkout, not the /repo bind mount.
  SCRIPT_TEST_ROOT=../scripts
fi
# Expand the script corpus by capability. Every roster entry is collected in
# the standalone lane unless its written exemption names a capability absent
# from this invocation. CI is deliberately not part of this decision: a
# Docker-less CI-marked run has the same capabilities as any other Docker-less
# run, and the roster tripwire below makes omissions fail closed.
for script_test in "${CI_SCRIPT_TEST_ROSTER[@]}"; do
  script_test_exempt=0
  for exemption in "${CI_SCRIPT_TEST_EXEMPTIONS[@]}"; do
    [ "${exemption%%|*}" = "$script_test" ] || continue
    script_test_exempt=1
    break
  done
  if [ "$script_test_exempt" = "1" ] && [ "$USE_DOCKER" != "1" ]; then
    continue
  fi
  [ "$script_test" = "test_bootstrap_local_app_release.py" ] && continue
  STANDALONE_SCRIPT_TEST_ARGS+=("$SCRIPT_TEST_ROOT/$script_test")
done
if [ "$USE_DOCKER" = "1" ]; then
  STANDALONE_RELEASE_TEST_ARGS+=("$SCRIPT_TEST_ROOT/test_bootstrap_local_app_release.py")
elif [ -f "$SCRIPT_TEST_ROOT/test_bootstrap_local_app_release.py" ]; then
  STANDALONE_RELEASE_TEST_ARGS+=("$SCRIPT_TEST_ROOT/test_bootstrap_local_app_release.py")
fi

STATIC_GUARD_SELECTORS_ARGS=()
if [ -n "${BLUEPRINT_STATIC_GUARD_SELECTORS:-}" ]; then
  while IFS= read -r selector; do
    [ -n "$selector" ] || continue
    case "$selector" in
      scripts/*)
        if [ "$USE_DOCKER" = "1" ]; then
          selector="/repo/$selector"
        else
          selector="../$selector"
        fi
        ;;
    esac
    STATIC_GUARD_SELECTORS_ARGS+=("$selector")
  done < <(printf '%s\n' "$BLUEPRINT_STATIC_GUARD_SELECTORS" | base64 --decode)
  [ "${#STATIC_GUARD_SELECTORS_ARGS[@]}" -gt 0 ] || {
    echo "ERROR: static guard selector list is empty or invalid" >&2
    exit 2
  }
fi

if [ "$USE_DOCKER" = "1" ]; then
  # Fast path: resolve a prebuilt deps image (local baked → GHCR pull) and
  # bind-mount the source (set in _run). Falls back loudly to the local build on
  # any failure — never worse than today. The chosen path is logged so fleet
  # fallback-rate is observable.
  if resolve_deps_image; then
    # Distinguish the baked (tier 1) vs pulled (tier 2) path for observability:
    # the locally baked image carries the LOCAL_DEPS_REPO prefix (no registry
    # host), the GHCR one carries the ghcr.io/... repo.
    case "$DEPS_IMAGE" in
      "$LOCAL_DEPS_REPO":*) IMAGE_PATH_TAKEN="baked-deps-image" ;;
      *)                    IMAGE_PATH_TAKEN="pulled-deps-image" ;;
    esac
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
  if [ "$SANDBOX_POSTGRES" = "1" ]; then
    "$PG_BIN/pg_isready" -h "$PG_SOCKET" -U test
  else
    for _ in $(seq 1 30); do
      if docker exec "$PG_CONTAINER" pg_isready -U test &>/dev/null; then break; fi
      sleep 0.5
    done
  fi
  echo "=== Checking migration-required PostgreSQL extensions ==="
  if [ "$SANDBOX_POSTGRES" = "1" ]; then
    verify_required_postgres_extensions "$PG_BIN/psql" -h "$PG_SOCKET" -U test -d test_blueprint
  else
    verify_required_postgres_extensions docker exec "$PG_CONTAINER" psql -U test -d test_blueprint
  fi
  echo "=== Running alembic migrations ==="
  _run python3 -m alembic upgrade head
fi

echo "=== Verify app imports ==="
_run python3 -c "from app.main import app; print(f'OK: {len(app.routes)} routes')"

if [ "${1:-}" = "--standalone" ]; then
  echo "=== Standalone tests (no DB) ==="
  if [ "${#STATIC_GUARD_SELECTORS_ARGS[@]}" -gt 0 ]; then
    STANDALONE_ARGS=("${STATIC_GUARD_SELECTORS_ARGS[@]}")
  else
    STANDALONE_ARGS=(
    testing/test_security.py
    testing/test_pipeline.py
    testing/test_invariants.py
    testing/test_impacted_tests.py
    testing/test_audit_finding_class_guards.py
    testing/test_admin_auth_invariant.py
    testing/test_release_promotion_metric_manifest.py
    testing/test_dependency_deprecations.py
    testing/test_websocket_emitter_inventory.py
    testing/test_operator_workbench_contracts.py::test_websocket_emitter_inventory_covers_current_emitters
    testing/test_monthly_budget_alerts.py
    testing/test_migration_chain.py
    testing/test_message_audience.py
    testing/test_event_type_invariants.py
    testing/test_salvage_bundle_standalone.py
    testing/test_test_durations_freshness.py
    testing/test_shard_balance.py
    testing/test_conversational_workday_protection.py
    testing/test_vitest_durations_freshness.py
    )
    # /repo is a Docker bind-mount; skip on Docker-less VMs where it does not exist.
    # The scripts roster is explicitly maintained above and checked by
    # backend/testing/test_ci_compliance_gate.py. It runs from the /repo mount in
    # CI and from ../scripts on Docker-less checkouts.
    STANDALONE_ARGS+=("${STANDALONE_SCRIPT_TEST_ARGS[@]}")
    # This exit-proof release suite covers durable handoff, serving confirmation,
    # and receipt retry behavior that pytest's backend shards do not collect.
    STANDALONE_ARGS+=("${STANDALONE_RELEASE_TEST_ARGS[@]}")
  fi
  STANDALONE_JUNIT_ARGS=()
  if [ -n "${BLUEPRINT_STATIC_GUARD_JUNIT:-}" ]; then
    STANDALONE_JUNIT_ARGS+=("--junitxml=${BLUEPRINT_STATIC_GUARD_JUNIT}")
  fi
  _run python3 -m pytest "${STANDALONE_ARGS[@]}" -v --noconftest \
    --timeout=30 --timeout-method=signal "${STANDALONE_JUNIT_ARGS[@]}"
  exit 0
fi

if [ $# -gt 0 ]; then
  echo "=== Running: pytest $* ==="
  if [ -n "${BLUEPRINT_VERIFICATION_RESULT_FILE:-}" ]; then
    # Opt-in authoring evidence mode. The caller must select real test files or
    # nodes, use Postgres, and explicitly attest USE_TEST_POSTGRES=true. JUnit
    # proves that pytest collected and executed at least one non-skipped test;
    # an exit-zero no-collection/all-skipped path cannot mint the result file.
    [ "$USE_POSTGRES" = "1" ] || {
      echo "ERROR: verification evidence requires the Postgres harness" >&2
      exit 2
    }
    [ "$(printf '%s' "${USE_TEST_POSTGRES:-}" | tr '[:upper:]' '[:lower:]')" = "true" ] || {
      echo "ERROR: verification evidence requires USE_TEST_POSTGRES=true" >&2
      exit 2
    }
    verification_selector_count=0
    for selector in "$@"; do
      case "$selector" in
        testing/test_*.py|testing/test_*.py::*) verification_selector_count=$((verification_selector_count + 1)) ;;
        *)
          echo "ERROR: unsupported verification pytest argument: $selector" >&2
          exit 2
          ;;
      esac
    done
    [ "$verification_selector_count" -gt 0 ] || {
      echo "ERROR: verification evidence requires a targeted testing/test_*.py file or node" >&2
      exit 2
    }
    [ "$verification_selector_count" -le 8 ] || {
      echo "ERROR: verification evidence is bounded to at most 8 targeted selectors" >&2
      exit 2
    }
    rm -f "$BLUEPRINT_VERIFICATION_RESULT_FILE"
    if [ -n "${BLUEPRINT_VERIFICATION_JUNIT:-}" ]; then
      VERIFICATION_OUTPUT_DIR="$(dirname -- "$BLUEPRINT_VERIFICATION_JUNIT")"
      VERIFICATION_JUNIT_NAME="$(basename -- "$BLUEPRINT_VERIFICATION_JUNIT")"
      [ -d "$VERIFICATION_OUTPUT_DIR" ] || {
        echo "ERROR: verification JUnit output directory is unavailable: $VERIFICATION_OUTPUT_DIR" >&2
        exit 2
      }
    else
      VERIFICATION_OUTPUT_DIR=$(mktemp -d /tmp/blueprint-backend-verification.XXXXXX)
      VERIFICATION_OUTPUT_DIR_OWNED=1
      VERIFICATION_JUNIT_NAME=""
    fi
    if [ "$USE_DOCKER" = "1" ]; then
      ENV_ARGS="$ENV_ARGS -v ${VERIFICATION_OUTPUT_DIR}:/verification-output"
      if [ -n "$VERIFICATION_JUNIT_NAME" ]; then
        verification_junit="/verification-output/${VERIFICATION_JUNIT_NAME}"
        ENV_ARGS="$ENV_ARGS -e BLUEPRINT_VERIFICATION_JUNIT=$verification_junit"
      else
        verification_junit=/verification-output/junit.xml
      fi
    else
      if [ -n "$VERIFICATION_JUNIT_NAME" ]; then
        verification_junit="$VERIFICATION_OUTPUT_DIR/$VERIFICATION_JUNIT_NAME"
      else
        verification_junit="$VERIFICATION_OUTPUT_DIR/junit.xml"
      fi
    fi
  _run python3 -m pytest "$@" --junitxml="$verification_junit"
    verification_tmp="${BLUEPRINT_VERIFICATION_RESULT_FILE}.$$"
    python3 -c 'import datetime,json,sys,xml.etree.ElementTree as ET; junit,out,*selectors=sys.argv[1:]; root=ET.parse(junit).getroot(); suites=[root] if root.tag.endswith("testsuite") else list(root); tests=sum(int(s.attrib.get("tests",0)) for s in suites); skipped=sum(int(s.attrib.get("skipped",0)) for s in suites); failures=sum(int(s.attrib.get("failures",0))+int(s.attrib.get("errors",0)) for s in suites); assert tests>0 and skipped<tests and failures==0, f"unacceptable pytest result tests={tests} skipped={skipped} failures={failures}"; payload={"version":1,"harness":"scripts/test-backend.sh","selectors":selectors,"targeted":True,"use_test_postgres":True,"postgres_sidecar":True,"tests":tests,"skipped":skipped,"failures":failures,"exit_code":0,"created_at":datetime.datetime.now(datetime.timezone.utc).isoformat()}; open(out,"w",encoding="utf-8").write(json.dumps(payload,sort_keys=True)+"\n")' "$VERIFICATION_OUTPUT_DIR/junit.xml" "$verification_tmp" "$@"
    mv -f "$verification_tmp" "$BLUEPRINT_VERIFICATION_RESULT_FILE"
  else
    if [ -n "${BLUEPRINT_VERIFICATION_JUNIT:-}" ]; then
      VERIFICATION_OUTPUT_DIR="$(dirname -- "$BLUEPRINT_VERIFICATION_JUNIT")"
      VERIFICATION_JUNIT_NAME="$(basename -- "$BLUEPRINT_VERIFICATION_JUNIT")"
      [ -d "$VERIFICATION_OUTPUT_DIR" ] || {
        echo "ERROR: verification JUnit output directory is unavailable: $VERIFICATION_OUTPUT_DIR" >&2
        exit 2
      }
      if [ "$USE_DOCKER" = "1" ]; then
        ENV_ARGS="$ENV_ARGS -v ${VERIFICATION_OUTPUT_DIR}:/verification-output"
        verification_junit="/verification-output/${VERIFICATION_JUNIT_NAME}"
        ENV_ARGS="$ENV_ARGS -e BLUEPRINT_VERIFICATION_JUNIT=$verification_junit"
      else
        verification_junit="$BLUEPRINT_VERIFICATION_JUNIT"
      fi
      _run python3 -m pytest "$@" --junitxml="$verification_junit"
    else
      _run python3 -m pytest "$@"
    fi
  fi
  exit 0
fi

echo "=== Standalone tests ==="
_run python3 -m pytest testing/test_security.py testing/test_pipeline.py testing/test_invariants.py testing/test_impacted_tests.py testing/test_audit_finding_class_guards.py testing/test_admin_auth_invariant.py testing/test_release_promotion_metric_manifest.py testing/test_dependency_deprecations.py testing/test_websocket_emitter_inventory.py testing/test_operator_workbench_contracts.py::test_websocket_emitter_inventory_covers_current_emitters /repo/scripts/test_blueprint_cli.py /repo/scripts/test_blueprint_mcp.py /repo/scripts/test_hermes_operator_cli.py "${STANDALONE_RELEASE_TEST_ARGS[@]}" testing/test_test_durations_freshness.py testing/test_shard_balance.py testing/test_vitest_durations_freshness.py -v --noconftest

echo "=== Full test suite ==="
if [ "${#TESTMON_ARGS[@]}" -gt 0 ]; then
  echo "(warm testmon cache present — selecting only tests with changed dependencies)"
fi
_run python3 -m pytest testing/ -q --tb=short "${TESTMON_ARGS[@]}"

echo "=== All backend tests passed ==="
