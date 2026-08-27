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

# Host interpreter. Git Bash on Windows has no `python3`, so spelling it
# literally here failed the merge below with exit 127 AFTER a successful
# measurement run -- the tests passed, the artifact was never written, and the
# cleanup trap deleted the raw measurement. Resolved through the shared helper
# so this script and batch-store-durations.sh cannot drift apart. Fail closed
# when the helper is absent: there is no correct identity fallback for
# "which interpreter", and a wrong guess loses another measurement.
if [ ! -f "$SCRIPT_DIR/python-interpreter.sh" ]; then
  echo "ERROR: $SCRIPT_DIR/python-interpreter.sh is missing; this script needs" >&2
  echo "       it to resolve a host Python interpreter." >&2
  exit 1
fi
# shellcheck source=scripts/python-interpreter.sh
source "$SCRIPT_DIR/python-interpreter.sh"
PYTHON_BIN="$(blueprint_python_interpreter)"

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
# A targeted refresh costs a full Docker+Postgres test run -- minutes at best,
# twenty at worst -- and PARTIAL_ROOT is the ONLY copy of what it measured.
# Deleting it unconditionally meant any failure after the run (the merge step
# most of all) destroyed the measurement and left the operator nothing to
# retry but the whole run. Retain it on every failing exit and name the path.
RETAIN_PARTIAL=0
cleanup() {
  local status=$?
  [ -z "$EMPTY_SRC_DIR" ] || rm -rf "$EMPTY_SRC_DIR"
  if [ -n "$PARTIAL_ROOT" ] && [ "$RETAIN_PARTIAL" = "1" ] && [ "$status" -ne 0 ]; then
    echo "" >&2
    echo "The raw measurement was NOT deleted: $PARTIAL_ROOT" >&2
    echo "Re-run only the merge (no re-measurement needed) with:" >&2
    echo "  $PYTHON_BIN $SCRIPT_DIR/merge_durations.py $PARTIAL_ROOT $BACKEND_DIR/.test_durations" >&2
    echo "Delete the directory once the merge has landed." >&2
    return
  fi
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
  RETAIN_PARTIAL=1
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
# other's completed measurement.  That serialization now lives INSIDE
# merge_durations.py (see duration_artifact_lock there) rather than in a
# `flock(1)` wrapper around this line: flock is not present in Git Bash on
# Windows, so the wrapper turned every merge on that host into an exit-127
# after the tests had already run.  A lock taken in Python covers every caller
# of the merge on every platform and needs no external binary.
"$PYTHON_BIN" "$SCRIPT_DIR/merge_durations.py" "$MERGE_SRC_DIR" "$BACKEND_DIR/.test_durations"
# Merge landed: the raw measurement is now redundant and may be cleaned up.
RETAIN_PARTIAL=0
