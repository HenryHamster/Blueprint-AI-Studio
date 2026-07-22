#!/usr/bin/env bash
# batch-store-durations.sh — Regenerate backend/.test_durations using parallel workers
# sharing one Postgres instance (separate databases per worker).
#
# Avoids 3+ hour sequential wall clock by running tests in N parallel workers,
# each with its own Postgres database cloned from a migration-seeded template.
#
# Requirements: Docker with cached deps image and postgres:15.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/../backend"

TEST_ENCRYPTION_KEY="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="

# Resolve deps image
DEPS_IMAGE=""
resolve_deps_image() {
  local reqhash
  reqhash="$(cat requirements.txt Dockerfile | sha256sum | cut -c1-16)"
  local local_img="blueprint-backend-test-deps:$reqhash"
  if docker image inspect "$local_img" >/dev/null 2>&1; then
    DEPS_IMAGE="$local_img"; return 0
  fi
  echo "ERROR: no deps image" >&2; return 1
}
resolve_deps_image

# CPU-based parallelism: use 2 workers to avoid OOM on 8GB VM
N_WORKERS=2
echo "Using ${N_WORKERS} parallel workers"

BATCH_ID="dur-$$"
NETWORK="bn-${BATCH_ID}"
PG_CONTAINER="bp-${BATCH_ID}"
MERGE_DIR="/tmp/dm-${BATCH_ID}"
mkdir -p "$MERGE_DIR"

cleanup() {
  set +e
  for pid in $(jobs -p); do kill "$pid" 2>/dev/null; done
  docker rm -f "$PG_CONTAINER" &>/dev/null
  docker network rm "$NETWORK" &>/dev/null
  rm -rf "$MERGE_DIR" 2>/dev/null
}
trap cleanup EXIT

# --- Start shared Postgres ---
echo "=== Starting Postgres ==="
docker network create "$NETWORK" >/dev/null
docker run -d --rm --name "$PG_CONTAINER" --network "$NETWORK" \
  -e POSTGRES_USER=test -e POSTGRES_PASSWORD=test -e POSTGRES_DB=test_blueprint \
  postgres:15 >/dev/null
for _ in $(seq 1 30); do docker exec "$PG_CONTAINER" pg_isready -U test &>/dev/null && break; sleep 0.5; done
echo "Postgres ready"

# Base env (minus the DB name which we'll set per-worker)
ENV_PREFIX="-e GCP_PROJECT_ID=test-project -e LOCAL_MODE=true \
  -e GITHUB_PAT_ENCRYPTION_KEY=$TEST_ENCRYPTION_KEY \
  -e AGENT_JWT_SECRET=ci-test-secret-min-32-bytes-xxxxx \
  -e WS_TICKET_SECRET=ci-test-ws-secret-min-32-bytes-xx"

# Mount admin-ui and agent
ADMIN_MOUNT="" AGENT_MOUNT="" REPO_MOUNT=""
[ -d "$SCRIPT_DIR/../admin-ui/src" ] && ADMIN_MOUNT="-v $(cd "$SCRIPT_DIR/../admin-ui/src" && pwd):/admin-ui/src:ro"
[ -d "$SCRIPT_DIR/../agent/src" ] && AGENT_MOUNT="-v $(cd "$SCRIPT_DIR/../agent/src" && pwd):/agent/src:ro"
[ -d "$SCRIPT_DIR/.." ] && REPO_MOUNT="-v $(cd "$SCRIPT_DIR/.." && pwd):/repo:ro"

_run() {
  local db_name="${1:-test_blueprint}"; shift
  docker run --rm -v "$PWD:/app" $ADMIN_MOUNT $AGENT_MOUNT $REPO_MOUNT \
    --network "$NETWORK" \
    -e "DATABASE_URL=postgresql+asyncpg://test:test@${PG_CONTAINER}:5432/${db_name}" \
    -e "USE_TEST_POSTGRES=true" \
    $ENV_PREFIX "$DEPS_IMAGE" "$@"
}

# --- Create template database with migrations ---
echo "=== Creating template database ==="
docker exec -i "$PG_CONTAINER" psql -U test -d postgres -c "CREATE DATABASE test_blueprint_tmpl;" 2>/dev/null || true
_run "test_blueprint_tmpl" python3 -m alembic upgrade head
_run "test_blueprint_tmpl" python3 -c "from app.main import app; print(f'OK: {len(app.routes)} routes')"

# --- Create per-worker databases from template ---
for w in $(seq 1 $N_WORKERS); do
  docker exec -i "$PG_CONTAINER" psql -U test -d postgres \
    -c "CREATE DATABASE test_blueprint_w${w} TEMPLATE test_blueprint_tmpl;" 2>/dev/null || true
done
echo "Worker databases created"

# --- Partition test files among workers ---
TEST_FILES=(testing/test_*.py)
echo "Total test files: ${#TEST_FILES[@]}"

# Round-robin assignment to workers
declare -a WORKER_FILES
for w in $(seq 1 $N_WORKERS); do WORKER_FILES[$w]=""; done
idx=0
for f in "${TEST_FILES[@]}"; do
  w=$(( (idx % N_WORKERS) + 1 ))
  WORKER_FILES[$w]="${WORKER_FILES[$w]} $f"
  idx=$((idx + 1))
done

echo "Files per worker:"
for w in $(seq 1 $N_WORKERS); do
  count=$(echo "${WORKER_FILES[$w]}" | wc -w)
  echo "  Worker ${w}: ${count} files"
done

# --- Run workers in parallel ---
SRUN_DIR="$MERGE_DIR/runs"
mkdir -p "$SRUN_DIR"
START_WALL=$(date +%s)

for w in $(seq 1 $N_WORKERS); do
  (
    WORKER_DIR="$SRUN_DIR/w${w}"
    mkdir -p "$WORKER_DIR"
    BATCH_NUM=0
    TOTAL=0
    BATCH_FILES=""
    files=(${WORKER_FILES[$w]})

    for f in "${files[@]}"; do
      BATCH_FILES="$BATCH_FILES $f"
      BATCH_NUM=$((BATCH_NUM + 1))

      # Run in small sub-batches of 8 files to avoid timeout accumulation
      if [ $((BATCH_NUM % 8)) -eq 0 ] || [ "$BATCH_NUM" -eq "${#files[@]}" ]; then
        if [ -n "$BATCH_FILES" ]; then
          DUR_FILE="$WORKER_DIR/dur_${BATCH_NUM}.json"
          TIMEOUT=$((300 + $(echo "$BATCH_FILES" | wc -w) * 60))

          # Redirect stdout to pipe to avoid flooding main output
          _run "test_blueprint_w${w}" python3 -m pytest $BATCH_FILES \
            --store-durations --durations-path "/app/.test_durations_w${w}_${BATCH_NUM}" \
            -o "timeout=${TIMEOUT}" -q --tb=line 2>&1 | tail -2 || true

          SRC_FILE="$PWD/.test_durations_w${w}_${BATCH_NUM}"
          if [ -f "$SRC_FILE" ]; then
            cp "$SRC_FILE" "$DUR_FILE"
            rm -f "$SRC_FILE"
            N=$(python3 -c "import json; print(len(json.load(open('$DUR_FILE'))))" 2>/dev/null || echo "0")
            TOTAL=$((TOTAL + N))
            ELAPSED=$(( $(date +%s) - START_WALL ))
            echo "  [W${w} batch ${BATCH_NUM}] ${N} durations (cumulative ${TOTAL}, ${ELAPSED}s wall)"
          else
            echo "  [W${w} batch ${BATCH_NUM}] no durations (retrying individually...)"
            # Retry each file individually
            for single_f in $BATCH_FILES; do
              single_base=$(basename "$single_f" .py)
              SF_DUR="$WORKER_DIR/sf_${BATCH_NUM}_${single_base}.json"
              _run "test_blueprint_w${w}" python3 -m pytest "$single_f" \
                --store-durations --durations-path "/app/.test_durations_sf_w${w}_${BATCH_NUM}_${single_base}" \
                -o "timeout=300" -q --tb=line 2>&1 | tail -1 || true
              SF_SRC="$PWD/.test_durations_sf_w${w}_${BATCH_NUM}_${single_base}"
              if [ -f "$SF_SRC" ]; then
                cp "$SF_SRC" "$SF_DUR"
                rm -f "$SF_SRC"
                N=$(python3 -c "import json; print(len(json.load(open('$SF_DUR'))))" 2>/dev/null || echo "0")
                echo "    [W${w}] ${single_f}: ${N} durations"
                TOTAL=$((TOTAL + N))
              fi
            done
          fi
          BATCH_FILES=""
        fi
      fi
    done

    # Final summary for this worker
    echo "  [W${w}] DONE: ${TOTAL} total durations"
  ) &
done

# Wait for all workers
echo "Waiting for ${N_WORKERS} workers..."
wait
echo "All workers completed"

# --- Merge all duration files ---
# Delegates to scripts/merge_durations.py (shared outlier cap + quality guards from
# scripts/durations_quality.py) so the merge logic lives in one lintable, testable place.
echo ""; echo "=== Merging ==="
python3 "$SCRIPT_DIR/merge_durations.py" "$SRUN_DIR" "$PWD/.test_durations"

echo "=== Done ==="
