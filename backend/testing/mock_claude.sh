#!/usr/bin/env bash
# Mock claude CLI for integration tests.
# Impersonates the claude binary so the agent subprocess can run without
# a real Anthropic account. Behavior is controlled by MOCK_CLAUDE_MODE.
set -euo pipefail

# ── Auth validation pattern ──────────────────────────────────────────────────
# The agent runs: claude -p 'respond with OK' --max-turns 1 --output-format stream-json ...
# Detect by --max-turns flag (only used during auth validation).
for arg in "$@"; do
  if [ "$arg" = "--max-turns" ]; then
    echo '{"type":"assistant","message":{"content":[{"type":"text","text":"OK"}]}}'
    echo '{"type":"result","result":"OK"}'
    exit 0
  fi
done

# ── Main execution pattern ───────────────────────────────────────────────────
# MOCK_CLAUDE_MODE may not be in our env (the agent builds a minimal env for
# the claude subprocess). Fall back to reading from a per-task file written by
# the test harness.  Using a per-task filename (keyed by TASK_ID) prevents
# concurrent tests from overwriting each other's mode file.
MODE="${MOCK_CLAUDE_MODE:-}"
TASK_ID="${TASK_ID:-unknown}"
if [ -z "$MODE" ] && [ -f "/tmp/mock_claude_mode_${TASK_ID}" ]; then
  MODE="$(cat "/tmp/mock_claude_mode_${TASK_ID}")"
fi
MODE="${MODE:-write}"

case "$MODE" in
  write)
    # Simulate creating a file in the workspace (cwd is the cloned repo)
    echo 'test content' > test_output.txt
    echo '{"type":"assistant","message":{"content":[{"type":"text","text":"Created test_output.txt"}]}}'
    echo '{"type":"result","result":"Task completed successfully"}'
    exit 0
    ;;

  readonly)
    # Write mock research findings to /tmp (agent reads this file)
    cat > "/tmp/research_findings_${TASK_ID}.json" <<'FINDINGS'
[
  {
    "title": "Mock finding: test coverage gap",
    "description": "Integration tests are missing for the agent lifecycle.",
    "severity": "medium",
    "file_path": "backend/testing/",
    "recommendation": "Add end-to-end lifecycle tests."
  }
]
FINDINGS
    echo '{"type":"assistant","message":{"content":[{"type":"text","text":"Research complete. Findings written."}]}}'
    echo '{"type":"result","result":"Research completed successfully"}'
    exit 0
    ;;

  fail)
    echo '{"type":"assistant","message":{"content":[{"type":"text","text":"Encountered an error during execution."}]}}'
    echo '{"type":"result","result":"Error: simulated failure"}'
    exit 1
    ;;

  *)
    echo "Unknown MOCK_CLAUDE_MODE: $MODE" >&2
    exit 2
    ;;
esac
