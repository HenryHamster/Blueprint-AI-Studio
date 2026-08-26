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

# Windows/Git-Bash path resolution. Every function it defines is the identity
# on a non-MSYS host, so Linux and CI keep byte-for-byte the behaviour they
# have today; see the helper's header for the two silent-wrong-answer failures
# it exists to close.
#
# The guard fixtures build synthetic checkouts by copying scripts/ piecemeal,
# so the helper can legitimately be absent. Degrade to the identity there
# rather than refusing to run -- but only where the identity is *correct*. On
# an MSYS host it is not, and a missing helper is fatal rather than a run that
# quietly mounts VM-side directories and reports a missing report.
if [ -f ../scripts/posix-path.sh ]; then
  source ../scripts/posix-path.sh
else
  case "$(uname -s 2>/dev/null || echo unknown)" in
    MINGW* | MSYS* | CYGWIN*)
      echo "ERROR: scripts/posix-path.sh is missing; this host needs it to" >&2
      echo "       resolve report and workspace paths for the Docker daemon." >&2
      exit 1
      ;;
  esac
  blueprint_posix_path() { printf '%s\n' "${1-}"; }
  blueprint_docker_host_path() { printf '%s\n' "${1-}"; }
fi

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
INVENTORY_ONLY=0
ARGS=()
for arg in "$@"; do
  case "$arg" in
    --postgres) ;;  # default; no-op
    --sandbox-postgres) SANDBOX_POSTGRES=1 ;;
    --no-postgres) USE_POSTGRES=0 ;;
    --no-testmon) USE_TESTMON=0 ;;
    --require-testmon) REQUIRE_TESTMON=1 ;;
    --standalone-inventory) INVENTORY_ONLY=1 ;;
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

# --- Cleanup: combined trap for Postgres sidecar and temporary outputs ---
PG_CONTAINER=""
PG_NETWORK=""
PG_DATA=""
PG_SOCKET=""
PG_BIN=""
VERIFICATION_OUTPUT_DIR=""
VERIFICATION_OUTPUT_DIR_OWNED=0
VERIFICATION_HOST_JUNIT=""
DOCKER_ARGS=()
PYTHON_REWRITE_ENABLED=0
ALEMBIC="alembic"
cleanup() {
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

# Do not stage CLAUDE.md into backend/.  The harness can run from a read-only
# checkout (including the /repo bind mount used by CI).  In Docker, the repo
# root is mounted at /repo; outside Docker, test_invariants.py resolves the
# checkout root directly.  Neither path requires mutating the backend tree.

# --- Select the execution boundary. The authoring-verification path uses a
# private PostgreSQL process plus a baked read-only venv inside bwrap, never the
# host Docker socket. Ordinary developer/CI calls retain the Docker path. ---
if [ "$SANDBOX_POSTGRES" = "1" ]; then
  USE_DOCKER=0
  PYTHON="${BLUEPRINT_SANDBOX_PYTHON:-/opt/blueprint-backend-venv/bin/python}"
  PYTHON_REWRITE_ENABLED=1
  [ -x "$PYTHON" ] || {
    echo "ERROR: --sandbox-postgres requires $PYTHON" >&2
    exit 2
  }
elif [ "$USE_POSTGRES" = "1" ] && _AUTO_POSTGRES_FLAG="$(select_backend_test_route 2>/dev/null)" && [ "$_AUTO_POSTGRES_FLAG" = "--sandbox-postgres" ]; then
  SANDBOX_POSTGRES=1
  USE_DOCKER=0
  PYTHON="${BLUEPRINT_SANDBOX_PYTHON:-/opt/blueprint-backend-venv/bin/python}"
  PYTHON_REWRITE_ENABLED=1
  echo "=== Using off-image pgserver PostgreSQL sandbox ==="
elif command -v docker &>/dev/null && docker info >/dev/null 2>&1; then
  USE_DOCKER=1
elif [ "$INVENTORY_ONLY" = "1" ]; then
  USE_DOCKER=0
  echo "=== Standalone corpus inventory (no test runtime) ==="
else
  USE_DOCKER=0
  if [ "$USE_POSTGRES" = "1" ]; then
    echo "ERROR: --postgres requires a usable Docker daemon" >&2
    exit 2
  fi
  echo "=== Docker unavailable — running pytest directly ==="
  # Ensure pip-installed binaries are on PATH
  export PATH="$HOME/.local/bin:$PATH"
  # An explicit interpreter is an authority boundary for roster/invariant
  # probes. Do not silently replace it with a host-cached venv: that makes the
  # same checkout collect different tests depending on runner residue.
  EXPLICIT_BACKEND_TEST_PYTHON=0
  if [ -n "${BLUEPRINT_BACKEND_TEST_PYTHON:-}" ]; then
    export PYTHON="$BLUEPRINT_BACKEND_TEST_PYTHON"
    EXPLICIT_BACKEND_TEST_PYTHON=1
  fi
  # Install dependencies if needed (use uv if pip is unavailable)
  VENV=/tmp/blueprint-backend-venv
  # Preserve an explicitly selected interpreter.  Read-only harness probes
  # use this to inject a recorder without requiring an app checkout.
  if [ -z "${PYTHON:-}" ] && [ -d "$VENV" ]; then
    export PYTHON="$VENV/bin/python"
    PYTHON_REWRITE_ENABLED=1
  fi
  if ! "${PYTHON:-python3}" -c "import fastapi" &>/dev/null; then
    if [ "$EXPLICIT_BACKEND_TEST_PYTHON" = "1" ]; then
      echo "ERROR: explicit BLUEPRINT_BACKEND_TEST_PYTHON cannot import backend dependencies" >&2
      exit 2
    fi
    echo "=== Installing dependencies ==="
    if python3 -m pip install -q -r requirements.txt 2>&1 | tail -5; then
      true
    elif command -v uv &>/dev/null || (curl -LsSf https://astral.sh/uv/install.sh | sh && export PATH="$HOME/.local/bin:$PATH"); then
      [ -d "$VENV" ] || uv venv "$VENV" -q
      uv pip install --python "$VENV/bin/python" -q -r requirements.txt
      uv pip install --python "$VENV/bin/python" -q "setuptools<81"
      export PYTHON="$VENV/bin/python"
      PYTHON_REWRITE_ENABLED=1
    fi
  fi
  PYTHON="${PYTHON:-python3}"

  # Verify Python 3.11+ is available (required for asyncio.timeout)
  PYTHON_VERSION=$("$PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "unknown")
  if [ -z "$PYTHON_VERSION" ] || [ "$PYTHON_VERSION" = "unknown" ]; then
    if [ "$EXPLICIT_BACKEND_TEST_PYTHON" = "1" ]; then
      echo "ERROR: explicit BLUEPRINT_BACKEND_TEST_PYTHON did not report a Python version" >&2
      exit 2
    fi
  else
    MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
    MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)
    if [ "$MAJOR" -lt 3 ] || ([ "$MAJOR" -eq 3 ] && [ "$MINOR" -lt 11 ]); then
      if [ "$EXPLICIT_BACKEND_TEST_PYTHON" = "1" ]; then
        echo "ERROR: explicit BLUEPRINT_BACKEND_TEST_PYTHON is $PYTHON_VERSION; Python 3.11+ is required" >&2
        exit 2
      fi
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
        PYTHON_REWRITE_ENABLED=1
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

if [ "$PYTHON_REWRITE_ENABLED" = "1" ]; then
  ALEMBIC="$(dirname "$PYTHON")/alembic"
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

# --- Configure DATABASE_URL / Docker argv based on --postgres ---
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
  DOCKER_ARGS=()
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

  DOCKER_ARGS=(
    --network "$PG_NETWORK"
    -e "DATABASE_URL=$PG_DSN"
    -e USE_TEST_POSTGRES=true
    -e GCE_METADATA_IP=127.0.0.1
    -e GCE_METADATA_HOST=127.0.0.1:9
    -e GCP_PROJECT_ID=test-project
    -e LOCAL_MODE=true
    -e "GITHUB_PAT_ENCRYPTION_KEY=$TEST_ENCRYPTION_KEY"
    -e AGENT_JWT_SECRET=ci-test-secret-min-32-bytes-xxxxx
    -e WS_TICKET_SECRET=ci-test-ws-secret-min-32-bytes-xx
  )
else
  DOCKER_ARGS=(
    -e DATABASE_URL=postgresql+asyncpg://x:x@localhost/x
    -e GCE_METADATA_IP=127.0.0.1
    -e GCE_METADATA_HOST=127.0.0.1:9
    -e GCP_PROJECT_ID=test-project
    -e LOCAL_MODE=true
    -e "GITHUB_PAT_ENCRYPTION_KEY=$TEST_ENCRYPTION_KEY"
    -e AGENT_JWT_SECRET=ci-test-secret-min-32-bytes-xxxxx
    -e WS_TICKET_SECRET=ci-test-ws-secret-min-32-bytes-xx
  )
fi

# --- Keep the container's writes out of, and off, the host checkout ---
#
# _run bind-mounts the host checkout at /app READ-WRITE (the deps image bakes
# no source), and the image has no USER directive, so by default pytest runs
# as root and writes root-owned __pycache__/, .pytest_cache/ and .testmondata
# straight back into the workspace. Nothing fails while that happens. The next
# thing to touch the workspace is what dies: actions/checkout and `git clean`
# report "EACCES: permission denied, unlink .../.pytest_cache/.gitignore" and
# the job is over in seconds, before a single test runs. On a self-hosted
# runner _work/<repo>/<repo> is shared between workflows, so one poisoned run
# breaks every later job on that runner and the same SHA passes or fails
# depending on which runner picks it up. Locally and on agent VMs the same
# droppings make `git clean` silently unable to clean.
#
# PYTHONDONTWRITEBYTECODE stops the largest source of them being created at
# all (this is what .github/workflows/ci.yml already does with the same
# image), and --user makes anything still written land owned by the caller.
# Both, because either alone leaves a hole: bytecode is not the only thing
# pytest writes, and --user cannot help a lane that does not pass it.
DOCKER_ARGS+=(-e PYTHONDONTWRITEBYTECODE=1)

# The workspace bind-mount source has the same host-path requirement as the
# report directories: `pwd` answers the MSYS rendering, and a checkout that
# lives under the Windows temp directory (which is what the base-tree
# verification lane extracts into) renders as /tmp/... -- a path Docker
# Desktop binds to a VM-side directory instead of rejecting, so /app would
# be empty and every selector would "fail" as a collection error.
WORKSPACE_HOST_PATH="$(blueprint_docker_host_path "$PWD")"

# Run the container as the invoking uid/gid. Root stays root (uid 0 in, uid 0
# out) so a root-owned CI checkout is unaffected; HOME is redirected because
# it defaults to "/", which a non-root uid cannot write. Fails open: if `id`
# cannot answer, the run proceeds exactly as it does today rather than not at
# all.
DOCKER_USER_ARGS=()
if _invoking_uid="$(id -u 2>/dev/null)" && _invoking_gid="$(id -g 2>/dev/null)" \
   && [ -n "$_invoking_uid" ] && [ -n "$_invoking_gid" ]; then
  DOCKER_USER_ARGS=(--user "${_invoking_uid}:${_invoking_gid}" -e HOME=/tmp)
  # USER too: --user injects a uid the image's /etc/passwd does not know, so
  # getpass.getuser() raises KeyError: 'getpwuid(): uid not found: 197609'
  # (the MSYS mapping of a Windows account). That is exactly how 3 cases in
  # scripts/test_blueprint_runner.py and 1 in scripts/test_local_runner.py
  # fail in the scripts test lane on Windows. CI cannot see it: a root-owned
  # checkout sends uid 0 in, and getpwuid(0) resolves. The only two consumers
  # (_install_user in blueprint_runner.py / blueprint_node.py) prefer $USER,
  # and under root the exported value is "root" -- what they already got.
  if _invoking_user="$(id -un 2>/dev/null)" && [ -n "$_invoking_user" ]; then
    DOCKER_USER_ARGS+=(-e "USER=${_invoking_user}")
  fi
fi

# Mount admin-ui/src read-only so test_invariants.py's frontend size
# guardrails can actually run inside Docker. Without this they silently
# skip (admin-ui isn't in the backend Dockerfile build context) and
# breaches accumulate unnoticed (R6 finding). The mount target
# /admin-ui/src is one of the candidate paths in
# `_resolve_admin_ui_src_root()`. CI runs without this mount; the
# guardrails fail loudly there until the workflow is updated similarly.
if [ "$USE_DOCKER" = "1" ] && [ -d ../admin-ui/src ]; then
  ADMIN_UI_HOST_PATH="$(blueprint_docker_host_path "$(cd ../admin-ui/src && pwd)")"
  DOCKER_ARGS+=(-v "${ADMIN_UI_HOST_PATH}:/admin-ui/src:ro")
fi

# Mount agent/src read-only so test_invariants.py's SR category parity
# check can parse the live prompt.ts instead of relying on the static
# AGENT_SELF_REVIEW_CATEGORIES mirror. The mount target /agent/src is
# one of the candidate paths in `_resolve_prompt_ts_path()`.
if [ "$USE_DOCKER" = "1" ] && [ -d ../agent/src ]; then
  MSYS_NO_PATHCONV=1 AGENT_SRC_HOST_PATH="$(blueprint_docker_host_path "$(cd ../agent/src && pwd)")"
  DOCKER_ARGS+=(-v "${AGENT_SRC_HOST_PATH}:/agent/src:ro")
fi

# Mount the repo root read-only so the operator CLI test suite
# (scripts/test_blueprint_cli.py) can resolve REPO_ROOT to /repo and find
# the ./check and ./blueprint wrappers it tests for existence/syntax.
if [ "$USE_DOCKER" = "1" ] && [ -d .. ]; then
  MSYS_NO_PATHCONV=1 REPO_HOST_PATH="$(blueprint_docker_host_path "$(cd .. && pwd)")"
  DOCKER_ARGS+=(-v "${REPO_HOST_PATH}:/repo:ro")
fi

# The static release guard supplies a host-side temporary JUnit path. Docker
# containers cannot write to that path unless its directory is mounted, and
# the container also needs the remapped path in its environment. Keep the
# caller's filename so the host can parse the report after `docker run` exits.
if [ -n "${BLUEPRINT_STATIC_GUARD_JUNIT:-}" ]; then
  # The caller may hand us a native Windows path. Split the POSIX rendering
  # of it, never the raw value: `cd` into a raw Windows path succeeds and
  # then reports the location through the MSYS mount table, which turns
  # C:\Users\...\Temp\g into /tmp/g -- a path the Docker daemon cannot
  # bind-mount from the host.
  STATIC_GUARD_JUNIT_POSIX="$(blueprint_posix_path "$BLUEPRINT_STATIC_GUARD_JUNIT")"
  STATIC_GUARD_OUTPUT_DIR="$(dirname -- "$STATIC_GUARD_JUNIT_POSIX")"
  STATIC_GUARD_OUTPUT_NAME="$(basename -- "$STATIC_GUARD_JUNIT_POSIX")"
  [ -d "$STATIC_GUARD_OUTPUT_DIR" ] || {
    echo "ERROR: static guard JUnit output directory is unavailable: $STATIC_GUARD_OUTPUT_DIR" >&2
    exit 2
  }
  if [ "$USE_DOCKER" = "1" ]; then
    STATIC_GUARD_OUTPUT_DIR="$(cd "$STATIC_GUARD_OUTPUT_DIR" && pwd -P)"
    # A bind-mount source has to be a host path. `pwd -P` can legitimately
    # answer an MSYS-virtual one (mktemp lives under /tmp), and Docker
    # Desktop silently binds a VM-side directory for those instead of
    # failing, so the report never lands where the caller reads it.
    DOCKER_ARGS+=(-v "$(blueprint_docker_host_path "$STATIC_GUARD_OUTPUT_DIR"):/static-guard-output")
    BLUEPRINT_STATIC_GUARD_JUNIT="/static-guard-output/${STATIC_GUARD_OUTPUT_NAME}"
    DOCKER_ARGS+=(-e "BLUEPRINT_STATIC_GUARD_JUNIT=$BLUEPRINT_STATIC_GUARD_JUNIT")
  fi
fi
if [ -n "${BLUEPRINT_STATIC_GUARD_SELECTORS:-}" ] && [ "$USE_DOCKER" = "1" ]; then
  DOCKER_ARGS+=(-e "BLUEPRINT_STATIC_GUARD_SELECTORS=$BLUEPRINT_STATIC_GUARD_SELECTORS")
fi
if [ -n "${BLUEPRINT_MANDATORY_VERIFICATION_SELECTORS:-}" ] && [ "$USE_DOCKER" = "1" ]; then
  DOCKER_ARGS+=(-e "BLUEPRINT_MANDATORY_VERIFICATION_SELECTORS=$BLUEPRINT_MANDATORY_VERIFICATION_SELECTORS")
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
if [ -n "${BLUEPRINT_MANDATORY_VERIFICATION_SELECTORS:-}" ]; then
  ENV_VARS="$ENV_VARS BLUEPRINT_MANDATORY_VERIFICATION_SELECTORS=$BLUEPRINT_MANDATORY_VERIFICATION_SELECTORS"
fi

# Hermetic Blueprint credentials for the scripts test lane.
#
# Several scripts/test_*.py drive `blueprint.main()` for real, and blueprint.py's
# `from blueprint_api import (...)` re-binding means a double installed on
# `blueprint` never intercepts the copy resolved inside blueprint_api. The real
# load_config() then runs, and its search path ends at the repo-root and
# ~/.blueprint-config.json -- so on a credentialed machine this harness issued
# live authenticated requests to the PRODUCTION orchestrator. CI containers hold
# no config file, which is exactly why CI cannot catch it. Every pytest call
# below passes --noconftest, so scripts/conftest.py is skipped by construction
# and the guarantee must arrive through the environment. See scripts/conftest.py
# for the full rationale and the tier ordering this relies on;
# backend/testing/test_scripts_lane_hermetic_credentials.py pins these values.
BLUEPRINT_HERMETIC_API_URL="https://scripts-lane.blueprint.invalid"
BLUEPRINT_HERMETIC_API_KEY="hermetic-scripts-lane-not-a-credential"
ENV_VARS="$ENV_VARS BLUEPRINT_CONFIG= BLUEPRINT_API_URL=$BLUEPRINT_HERMETIC_API_URL BLUEPRINT_API_KEY=$BLUEPRINT_HERMETIC_API_KEY"
DOCKER_ARGS+=(
  -e "BLUEPRINT_CONFIG="
  -e "BLUEPRINT_API_URL=$BLUEPRINT_HERMETIC_API_URL"
  -e "BLUEPRINT_API_KEY=$BLUEPRINT_HERMETIC_API_KEY"
)

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
      # /app/CLAUDE.md (cp'd above), the agent/admin-ui mounts in DOCKER_ARGS,
      # alembic, and `app.*` all resolve identically to the built image.
      MSYS_NO_PATHCONV=1 docker run --rm "${DOCKER_USER_ARGS[@]}" -v "${WORKSPACE_HOST_PATH}:/app" "${DOCKER_ARGS[@]}" "$DEPS_IMAGE" "$@"
    else
      MSYS_NO_PATHCONV=1 docker run --rm "${DOCKER_USER_ARGS[@]}" "${DOCKER_ARGS[@]}" blueprint-backend-test "$@"
    fi
  else
    # Replace leading "python3" only when the harness selected the venv.
    # An explicit PYTHON is also used by read-only probes that deliberately
    # exercise the literal python3 subprocess command.
    if [ "$PYTHON_REWRITE_ENABLED" = "1" ] && [ "$1" = "python3" ]; then
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
  infra/db_recovery
  infra/livekit/tests
)

# infra/db_recovery and infra/livekit/tests hold pure static-assertion tests
# (path reads + string asserts against checked-in IaC/config text, no Docker/
# DB/cloud dependency) that ran in no lane at all before this: they are outside
# CI_TEST_MODULE_ROOTS's old coverage and outside backend/testing/, so nothing
# executed them and nothing flagged that. Unlike the scripts/ roster they need
# no capability exemption list -- every discovered file always runs.
INFRA_TEST_ROOTS=(
  infra/db_recovery
  "infra/livekit/tests"
)

# Discover the scripts test corpus at runtime. This is intentionally a glob,
# not a second per-file roster: a newly added scripts/test_*.py is picked up by
# the standalone lane and by backend/testing/test_scripts_test_lane.py without
# an unrelated list edit.
CI_SCRIPT_TEST_DISCOVERY_GLOB="test_*.py"

# Entries in this list need a capability that the Docker-less standalone lane
# does not provide. They remain in the CI collection and are collected when the
# Docker capability is present; the reason makes a local omission reviewable.
#
# test_blueprint_cli.py is deliberately NOT here. Its own module docstring
# states the whole file mocks blueprint_api.api, so no network or operator
# credentials are required, and every subprocess call the CLI itself makes
# (docker, alembic, systemctl) is monkeypatched per-test. It ran fine in the
# Docker-less CI lane before #17295 added a blanket exemption for it with the
# reason "requires Blueprint operator API configuration and control-plane
# network" — that reason does not match the file's design and no test in it
# calls a real API or Docker daemon. Keep it in the roster; if a specific test
# genuinely needs a capability this lane lacks, exempt that one test with
# pytest.mark.skipif, not the whole file.
#
# test_local_runner.py is no longer here either. It carried the reason
# "requires an enrolled local runner and its host services", but its 648 tests
# import local_runner in-process and mock the host: no test enrolls a runner,
# and the handful that genuinely touched the host (a real `codex --version`
# probe, a real HTTP POST to api.example.com, a saved ~/.blueprint token, a
# clean git checkout) were each scoped or stubbed per-test rather than by
# exempting the file. The blanket exemption is what let the file drift 20-red
# and 49s long while reporting nothing; it now runs in 9s.
CI_SCRIPT_TEST_EXEMPTIONS=(
  "test_blueprint_node.py|requires a root-owned system Python and systemd deployment host"
  "test_local_postgres_cutover_integration.py|requires a Docker daemon and two real PostgreSQL containers"
)

STANDALONE_SCRIPT_TEST_ARGS=()
STANDALONE_RELEASE_TEST_ARGS=()
if [ "$INVENTORY_ONLY" = "1" ]; then
  # Inventory mode runs outside the container, so inspect this checkout even
  # when the eventual test run would use the /repo Docker mount.
  SCRIPT_TEST_ROOT=../scripts
elif [ "$USE_DOCKER" = "1" ]; then
  SCRIPT_TEST_ROOT=/repo/scripts
else
  # Docker-less local runs are rooted at the checkout, not the /repo bind mount.
  SCRIPT_TEST_ROOT=../scripts
fi
# Expand the discovered script corpus by capability. Every discovered entry is collected in
# the standalone lane unless its written exemption names a capability absent
# from this invocation. CI is deliberately not part of this decision: a
# Docker-less CI-marked run has the same capabilities as any other Docker-less
# run, and the discovery tripwire below makes omissions fail closed.
for script_path in "$SCRIPT_TEST_ROOT"/$CI_SCRIPT_TEST_DISCOVERY_GLOB; do
  [ -f "$script_path" ] || continue
  script_test="${script_path##*/}"
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

# Discover the infra test corpus the same way: a glob, not a second per-file
# roster. Every discovered test_*.py under an INFRA_TEST_ROOTS entry always
# runs -- these files are static-assertion-only, so there is no capability
# axis to exempt against.
STANDALONE_INFRA_TEST_ARGS=()
for infra_root in "${INFRA_TEST_ROOTS[@]}"; do
  # Inventory mode runs outside the container, so it must inspect this
  # checkout even when the eventual test run would use the /repo mount --
  # the same reason SCRIPT_TEST_ROOT special-cases it above. Without this
  # the glob resolves to a nonexistent /repo path, the infra corpus drops
  # out of the inventory silently, and the release guard that diffs this
  # inventory between trees stops seeing infra test deletions.
  if [ "$USE_DOCKER" = "1" ] && [ "$INVENTORY_ONLY" != "1" ]; then
    infra_test_root="/repo/$infra_root"
  else
    infra_test_root="../$infra_root"
  fi
  for infra_test_path in "$infra_test_root"/$CI_SCRIPT_TEST_DISCOVERY_GLOB; do
    [ -f "$infra_test_path" ] || continue
    STANDALONE_INFRA_TEST_ARGS+=("$infra_test_path")
  done
done

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

if [ "${1:-}" = "--standalone" ]; then
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
    testing/test_wire_timestamp_zone.py
    testing/test_event_type_invariants.py
    testing/test_salvage_bundle_standalone.py
    testing/test_test_durations_freshness.py
    testing/test_durations_measurability.py
    testing/test_test_estate_ranked_queue.py
    testing/test_shard_balance.py
    testing/test_conversational_workday_protection.py
    testing/test_vitest_durations_freshness.py
    testing/test_test_imports_checker.py
    testing/test_scripts_lane_hermetic_credentials.py
    )
    # /repo is a Docker bind-mount; skip on Docker-less VMs where it does not exist.
    # The scripts roster is explicitly maintained above and checked by
    # backend/testing/test_ci_compliance_gate.py. It runs from the /repo mount in
    # CI and from ../scripts on Docker-less checkouts.
    STANDALONE_ARGS+=("${STANDALONE_SCRIPT_TEST_ARGS[@]}")
    # This exit-proof release suite covers durable handoff, serving confirmation,
    # and receipt retry behavior that pytest backend shards do not collect.
    STANDALONE_ARGS+=("${STANDALONE_RELEASE_TEST_ARGS[@]}")
    STANDALONE_ARGS+=("${STANDALONE_INFRA_TEST_ARGS[@]}")
  fi
fi

# This is the one machine-readable view of the corpus that this harness will
# execute. The release guard compares it between trees to detect deleted,
# renamed, or otherwise omitted corpus members without maintaining a second
# per-file roster. Normalize mount paths back to checkout-relative identities so
# Docker and direct execution produce the same inventory.
if [ "$INVENTORY_ONLY" = "1" ]; then
  [ "${1:-}" = "--standalone" ] || {
    echo "ERROR: --standalone-inventory requires --standalone" >&2
    exit 2
  }
  echo "BLUEPRINT_STANDALONE_CORPUS_V1"
  for corpus_entry in "${STANDALONE_ARGS[@]}"; do
    case "$corpus_entry" in
      /repo/*) canonical_entry="${corpus_entry#/repo/}" ;;
      # Any checkout-relative entry (scripts/, infra/) normalizes to the
      # same repo-relative identity the /repo mount produces, so the two
      # execution modes emit one comparable inventory.
      ../*) canonical_entry="${corpus_entry#../}" ;;
      testing/*) canonical_entry="backend/$corpus_entry" ;;
      *) canonical_entry="$corpus_entry" ;;
    esac
    printf 'BLUEPRINT_STANDALONE_CORPUS_ENTRY=%s\n' "$canonical_entry"
  done
  echo "BLUEPRINT_STANDALONE_CORPUS_END"
  exit 0
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
    # --target deps, not the default final `app` stage. CI builds and runs
    # the tests against `deps`; `app` is the production image and lacks git
    # and the test Fernet key, so the old default-stage build silently
    # disagreed with CI -- testing/test_vitest_durations_freshness.py fails
    # locally on a missing `git` binary while passing in CI, which is
    # exactly the "identical result" the fallback banner above promises.
    docker build --target deps -t blueprint-backend-test . -q
    # `deps` bakes no source, so route this image through the same
    # bind-mount path the prebuilt images use rather than the legacy
    # baked-source invocation in _run().
    DEPS_IMAGE="blueprint-backend-test"
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
  _run "$ALEMBIC" upgrade head
fi

echo "=== Verify app imports ==="
_run python3 -c "from app.main import app; print(f'OK: {len(app.routes)} routes')"

if [ "${1:-}" = "--standalone" ]; then
  echo "=== Standalone tests (no DB) ==="
  STANDALONE_JUNIT_ARGS=()
  if [ -n "${BLUEPRINT_STATIC_GUARD_JUNIT:-}" ]; then
    STANDALONE_JUNIT_ARGS+=("--junitxml=${BLUEPRINT_STATIC_GUARD_JUNIT}")
  fi
  _run python3 -m pytest "${STANDALONE_ARGS[@]}" -v --noconftest \
    --timeout=300 --timeout-method=signal "${STANDALONE_JUNIT_ARGS[@]}"
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
    verification_ordinary_selector_count=0
    verification_mandatory_selector_count=0
    verification_mandatory_selectors=()
    if [ -n "${BLUEPRINT_MANDATORY_VERIFICATION_SELECTORS:-}" ]; then
      verification_mandatory_tmp=$(mktemp /tmp/blueprint-mandatory-selectors.XXXXXX)
      if ! printf '%s' "$BLUEPRINT_MANDATORY_VERIFICATION_SELECTORS" | base64 --decode >"$verification_mandatory_tmp"; then
        rm -f "$verification_mandatory_tmp"
        echo "ERROR: malformed mandatory verification selector allowlist" >&2
        exit 2
      fi
      mapfile -t verification_mandatory_selectors <"$verification_mandatory_tmp"
      rm -f "$verification_mandatory_tmp"
    fi
    verification_is_mandatory() {
      local candidate="$1" mandatory
      for mandatory in "${verification_mandatory_selectors[@]}"; do
        [ "$candidate" = "$mandatory" ] && return 0
      done
      return 1
    }
    for selector in "$@"; do
      case "$selector" in
        testing/test_*.py|testing/test_*.py::*)
          verification_selector_count=$((verification_selector_count + 1))
          if verification_is_mandatory "$selector"; then
            verification_mandatory_selector_count=$((verification_mandatory_selector_count + 1))
          else
            verification_ordinary_selector_count=$((verification_ordinary_selector_count + 1))
          fi
          ;;
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
    [ "$verification_ordinary_selector_count" -le 8 ] || {
      echo "ERROR: verification evidence is bounded to at most 8 ordinary targeted selectors" >&2
      exit 2
    }
    [ "$verification_mandatory_selector_count" -eq "${#verification_mandatory_selectors[@]}" ] || {
      echo "ERROR: every mandatory verification selector must be carried exactly once" >&2
      exit 2
    }
    rm -f "$BLUEPRINT_VERIFICATION_RESULT_FILE"
    if [ -n "${BLUEPRINT_VERIFICATION_JUNIT:-}" ]; then
      VERIFICATION_JUNIT_POSIX="$(blueprint_posix_path "$BLUEPRINT_VERIFICATION_JUNIT")"
      VERIFICATION_OUTPUT_DIR="$(dirname -- "$VERIFICATION_JUNIT_POSIX")"
      VERIFICATION_JUNIT_NAME="$(basename -- "$VERIFICATION_JUNIT_POSIX")"
      [ -d "$VERIFICATION_OUTPUT_DIR" ] || {
        echo "ERROR: verification JUnit output directory is unavailable: $VERIFICATION_OUTPUT_DIR" >&2
        exit 2
      }
      VERIFICATION_OUTPUT_DIR="$(cd "$VERIFICATION_OUTPUT_DIR" && pwd -P)"
    else
      VERIFICATION_OUTPUT_DIR=$(mktemp -d /tmp/blueprint-backend-verification.XXXXXX)
      VERIFICATION_OUTPUT_DIR_OWNED=1
      VERIFICATION_JUNIT_NAME=""
    fi
    VERIFICATION_HOST_JUNIT="$VERIFICATION_OUTPUT_DIR/${VERIFICATION_JUNIT_NAME:-junit.xml}"
    if [ "$USE_DOCKER" = "1" ]; then
      DOCKER_ARGS+=(-v "$(blueprint_docker_host_path "$VERIFICATION_OUTPUT_DIR"):/verification-output")
      if [ -n "$VERIFICATION_JUNIT_NAME" ]; then
        verification_junit="/verification-output/${VERIFICATION_JUNIT_NAME}"
        DOCKER_ARGS+=(-e "BLUEPRINT_VERIFICATION_JUNIT=$verification_junit")
      else
        verification_junit=/verification-output/junit.xml
      fi
    else
      verification_junit="$VERIFICATION_HOST_JUNIT"
    fi
  _run python3 -m pytest "$@" --junitxml="$verification_junit"
    verification_tmp="${BLUEPRINT_VERIFICATION_RESULT_FILE}.$$"
    python3 -c 'import datetime,json,sys,xml.etree.ElementTree as ET; junit,out,*selectors=sys.argv[1:]; root=ET.parse(junit).getroot(); suites=[root] if root.tag.endswith("testsuite") else list(root); tests=sum(int(s.attrib.get("tests",0)) for s in suites); skipped=sum(int(s.attrib.get("skipped",0)) for s in suites); failures=sum(int(s.attrib.get("failures",0))+int(s.attrib.get("errors",0)) for s in suites); assert tests>0 and skipped<tests and failures==0, f"unacceptable pytest result tests={tests} skipped={skipped} failures={failures}"; payload={"version":1,"harness":"scripts/test-backend.sh","selectors":selectors,"targeted":True,"use_test_postgres":True,"postgres_sidecar":True,"tests":tests,"skipped":skipped,"failures":failures,"exit_code":0,"created_at":datetime.datetime.now(datetime.timezone.utc).isoformat()}; open(out,"w",encoding="utf-8").write(json.dumps(payload,sort_keys=True)+"\n")' "$VERIFICATION_HOST_JUNIT" "$verification_tmp" "$@"
    mv -f "$verification_tmp" "$BLUEPRINT_VERIFICATION_RESULT_FILE"
  else
    if [ -n "${BLUEPRINT_VERIFICATION_JUNIT:-}" ]; then
      VERIFICATION_JUNIT_POSIX="$(blueprint_posix_path "$BLUEPRINT_VERIFICATION_JUNIT")"
      VERIFICATION_OUTPUT_DIR="$(dirname -- "$VERIFICATION_JUNIT_POSIX")"
      VERIFICATION_JUNIT_NAME="$(basename -- "$VERIFICATION_JUNIT_POSIX")"
      [ -d "$VERIFICATION_OUTPUT_DIR" ] || {
        echo "ERROR: verification JUnit output directory is unavailable: $VERIFICATION_OUTPUT_DIR" >&2
        exit 2
      }
      VERIFICATION_OUTPUT_DIR="$(cd "$VERIFICATION_OUTPUT_DIR" && pwd -P)"
      if [ "$USE_DOCKER" = "1" ]; then
        DOCKER_ARGS+=(-v "$(blueprint_docker_host_path "$VERIFICATION_OUTPUT_DIR"):/verification-output")
        verification_junit="/verification-output/${VERIFICATION_JUNIT_NAME}"
        DOCKER_ARGS+=(-e "BLUEPRINT_VERIFICATION_JUNIT=$verification_junit")
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
_run python3 -m pytest testing/test_security.py testing/test_pipeline.py testing/test_invariants.py testing/test_impacted_tests.py testing/test_audit_finding_class_guards.py testing/test_admin_auth_invariant.py testing/test_release_promotion_metric_manifest.py testing/test_dependency_deprecations.py testing/test_websocket_emitter_inventory.py testing/test_operator_workbench_contracts.py::test_websocket_emitter_inventory_covers_current_emitters /repo/scripts/test_blueprint_cli.py /repo/scripts/test_blueprint_compute_local.py /repo/scripts/test_blueprint_mcp.py /repo/scripts/test_hermes_operator_cli.py "${STANDALONE_RELEASE_TEST_ARGS[@]}" testing/test_test_durations_freshness.py testing/test_durations_measurability.py testing/test_shard_balance.py testing/test_vitest_durations_freshness.py -v --noconftest

echo "=== Full test suite ==="
if [ "${#TESTMON_ARGS[@]}" -gt 0 ]; then
  echo "(warm testmon cache present — selecting only tests with changed dependencies)"
fi
_run python3 -m pytest testing/ -q --tb=short "${TESTMON_ARGS[@]}"

echo "=== All backend tests passed ==="
