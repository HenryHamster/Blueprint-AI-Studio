# ── size guardrail (shared archive baseline) ────────────────────────────────
# The tracked and generated task gates use the same static-release-guard entry
# point. It extracts the base with the shared git-archive helper and delegates
# per-file inherited/new/grown classification to check_size_limits.py. This
# keeps stale worktrees, raw checker output, and baseline policy out of the
# generated gate.
run "static size guards (task diff)" env TASK_ID="${TASK_ID:-${_VERIFY_TASK_ID:-}}" python3 scripts/static_release_guard.py \
  --size-only --base "$BASE" --repo-root "$WORKSPACE"
# ── end size guardrail ──────────────────────────────────────────────────────
