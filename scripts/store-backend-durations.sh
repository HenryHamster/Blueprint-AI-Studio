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
echo "This runs the full backend test suite — expect 15-20 minutes."
echo ""

# Pass 'testing/ --store-durations' as explicit args so test-backend.sh runs
# pytest against the full testing/ directory with duration recording enabled.
# pytest-split writes .test_durations to the rootdir (/app inside Docker),
# which is bind-mounted from backend/, so the file lands at backend/.test_durations.
exec "$SCRIPT_DIR/test-backend.sh" testing/ --store-durations "$@"
