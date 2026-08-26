#!/usr/bin/env bash
# store-backend-durations.sh — Regenerate backend/.test_durations for time-balanced CI shards.
#
# What it does:
#   Runs the full backend test suite with pytest-split's --store-durations flag inside
#   the same Docker+Postgres harness that scripts/test-backend.sh uses. pytest-split
#   writes per-test wall-clock timings to /app/.test_durations (the rootdir is /app,
#   i.e. backend/), which maps to backend/.test_durations on the host checkout.
#
# When to run:
#   - After adding or removing a large batch of tests
#   - When the freshness guard in backend/testing/test_test_durations_freshness.py
#     fails with a staleness warning
#   - Periodically (e.g. after merging a large feature branch) to keep time-balancing accurate
#
# After running:
#   1. Commit the updated backend/.test_durations to version control so CI shards
#      use time-based splitting.
#
# Requirements:
#   - Docker with access to the backend deps image
#   - A running Postgres instance (uses the same TEST_DB_URL as scripts/test-backend.sh)
#   - ~15-20 minutes (full suite run)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== Regenerating backend/.test_durations (time-balanced shard seeding) ==="
if [ "$#" -eq 0 ]; then
  echo "This runs the full backend test suite — expect 15-20 minutes."
else
  echo "This runs only the explicitly selected backend tests."
fi
echo ""

BACKEND_DIR="${DURATION_BACKEND_DIR:-$SCRIPT_DIR/../backend}"
TEST_BACKEND_SCRIPT="${DURATION_TEST_BACKEND_SCRIPT:-$SCRIPT_DIR/test-backend.sh}"
MERGE_SRC_DIR=""
PARTIAL_ROOT=""
EMPTY_SRC_DIR=""
cleanup() {
  [ -z "$EMPTY_SRC_DIR" ] || rm -rf "$EMPTY_SRC_DIR"
  [ -z "$PARTIAL_ROOT" ] || rm -rf "$PARTIAL_ROOT"
}
trap cleanup EXIT

if [ "$#" -eq 0 ]; then
  echo "=== Refresh mode: full testing/ duration inventory ==="
  # pytest-split writes .test_durations to the rootdir (/app inside Docker),
  # which is bind-mounted from backend/, so the file lands on the host at the
  # committed artifact path.
  "$TEST_BACKEND_SCRIPT" testing/ --store-durations
else
  echo "=== Refresh mode: targeted duration inventory ($*) ==="
  # Keep targeted output separate: pytest-split's direct artifact writer would
  # otherwise replace the complete baseline with only the selected tests.
  # A relative path is valid both from the backend checkout and from Docker's
  # /app backend mount.
  # Each invocation owns a distinct directory.  A stable directory caused
  # concurrent targeted refreshes to overwrite or delete each other's raw
  # measurement before merge_durations.py could consume it.
  PARTIAL_ROOT="$(mktemp -d "$BACKEND_DIR/.duration_refresh.XXXXXX")"
  PARTIAL_DIR="$PARTIAL_ROOT/target"
  mkdir -p "$PARTIAL_DIR"
  MERGE_SRC_DIR="$PARTIAL_ROOT"
  PARTIAL_PATH="$(basename "$PARTIAL_ROOT")/target/durations.json"
  "$TEST_BACKEND_SCRIPT" "$@" --store-durations \
    --durations-path "$PARTIAL_PATH"
fi

# pytest-split's raw --store-durations output charges fixture/setup cost
# (e.g. a Postgres cold-start billed to the first test of a batch) directly
# onto individual test entries. Route the freshly written file through
# merge_durations.py so it gets the same canonical duration policy the
# partial-recording path already applies -- OUTLIER_CAP plus the in-band
# setup-artifact strip from scripts/durations_quality.py -- and a full-suite
# regeneration cannot reintroduce the fixture-plateau pollution that
# backend/testing/test_test_durations_freshness.py rejects. A full-suite run
# produces no partial-recording files, so pass an empty source directory:
# merge() then reads backend/.test_durations as its baseline, sanitizes it,
# and rewrites it in place.
EMPTY_SRC_DIR="$(mktemp -d)"
if [ -z "$MERGE_SRC_DIR" ]; then
  MERGE_SRC_DIR="$EMPTY_SRC_DIR"
fi
# Individual measurement directories may run concurrently, but their final
# read-modify-write merge must be serialized so neither invocation loses the
# other's completed measurement.  The lock file is deliberately retained: it
# is only a flock handle and may have existed before this invocation.
flock "$BACKEND_DIR/.duration_refresh.lock" \
  python3 "$SCRIPT_DIR/merge_durations.py" "$MERGE_SRC_DIR" "$BACKEND_DIR/.test_durations"
