
# ── merge-resolution closed-loop verify ─────────────────────────────────────
# This fix resolved a base-branch merge conflict. Verify the affected backend
# test files pass under Postgres BEFORE ./pr open can push — a silent drop or a
# code/test vintage skew from the resolution would otherwise surface only in CI.
MERGE_VERIFY_CAP=8
_mv_all=$(
  {
    printf '%s\n' "$CHANGED" | grep -E '^backend/testing/test_[A-Za-z0-9_]+\.py$'
    printf '%s\n' "$CHANGED" | grep -E '^backend/app/[A-Za-z0-9_]+\.py$' \
      | sed -E 's|^backend/app/([A-Za-z0-9_]+)\.py$|backend/testing/test_\1.py|'
  } | sort -u | grep -v '^$' | while read -r _mv_f; do [ -f "$_mv_f" ] && printf '%s\n' "$_mv_f"; done
)
if [ -n "$_mv_all" ]; then
  _mv_count=$(printf '%s\n' "$_mv_all" | grep -c '^' || true)
  if [ "$_mv_count" -gt "$MERGE_VERIFY_CAP" ]; then
    echo "[check] merge-resolution verify: $_mv_count affected test file(s); running first $MERGE_VERIFY_CAP under Postgres, CI verifies the rest:"
    printf '%s\n' "$_mv_all" | tail -n +$((MERGE_VERIFY_CAP + 1)) | sed 's|^|[check]   deferred-to-CI: |'
  fi
  # Strip the backend/ prefix: scripts/test-backend.sh runs from backend/ and
  # expects testing/test_x.py selectors.
  _mv_run=$(printf '%s\n' "$_mv_all" | head -n "$MERGE_VERIFY_CAP" | sed 's|^backend/||')
  if [ -f scripts/test-backend.sh ] && command -v docker >/dev/null 2>&1; then
    echo "[check] merge-resolution verify: running affected backend test file(s) via the sanctioned Postgres harness..."
    # shellcheck disable=SC2086
    run "merge-resolution verify" bash -c 'set -o pipefail; bash scripts/test-backend.sh --postgres "$@" 2>&1 | tail -200' _ $_mv_run
  else
    echo "[check] merge-resolution verify: SKIPPED — Docker unavailable, can't run the sanctioned Postgres harness on this VM."
    echo "[check]   The affected backend tests were NOT verified locally; CI runs the authoritative Postgres suite on the PR."
    printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "merge-resolution verify skipped — no Docker for the sanctioned harness; CI verifies affected tests on the PR" >> /tmp/check_gate_env_@TASK_ID@.txt
  fi
fi
