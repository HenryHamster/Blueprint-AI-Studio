
    # ── impacted-test merge-time gate ───────────────────────────────────────
    # Complement to the standalone smoke run above: mechanically run the backend
    # test files IMPACTED by this diff (test files that import a changed
    # app.<module> or mock-patch it by string target, plus any test file the PR
    # itself adds). Derived from $CHANGED via the committed reverse-dep resolver
    # (invoked BY REFERENCE — no embedded Python heredoc, see CLAUDE.md), run
    # bounded under the sanctioned Postgres harness so an impacted-test failure
    # blocks ./pr open instead of leaking to the promotion suite.
    IMPACTED_TEST_CAP=8
    _it_stderr_f=$(mktemp /tmp/check_impacted_XXXXXX)
    _it_all=$(printf '%s\n' "$CHANGED" | python3 backend/testing/impacted_tests.py 2>"$_it_stderr_f")
    _it_rc=$?
    if [ "$_it_rc" -ne 0 ]; then
      echo "[check] impacted-test resolver: crashed (exit $_it_rc) — stderr follows:" >&2
      cat "$_it_stderr_f" >&2
      echo "[check] impacted-test resolver: crash surfaced as gate failure" >&2
      FAILED+=("impacted-test resolver crash")
    fi
    rm -f "$_it_stderr_f"
    if [ -n "$_it_all" ]; then
      _it_count=$(printf '%s\n' "$_it_all" | grep -c '^' || true)
      if [ "$_it_count" -gt "$IMPACTED_TEST_CAP" ]; then
        echo "[check] impacted-test gate: $_it_count impacted test file(s); running first $IMPACTED_TEST_CAP under Postgres, CI verifies the rest:"
        printf '%s\n' "$_it_all" | tail -n +$((IMPACTED_TEST_CAP + 1)) | sed 's|^|[check]   deferred-to-CI: |'
      fi
      # Strip the backend/ prefix: scripts/test-backend.sh runs from backend/ and
      # expects testing/test_x.py selectors.
      _it_run=$(printf '%s\n' "$_it_all" | head -n "$IMPACTED_TEST_CAP" | sed 's|^backend/||')
      if [ -f scripts/test-backend.sh ] && command -v docker >/dev/null 2>&1; then
        echo "[check] impacted-test gate: running impacted backend test file(s) via the sanctioned Postgres harness..."
        # shellcheck disable=SC2086
        run "impacted-test gate" bash -c 'set -o pipefail; bash scripts/test-backend.sh --postgres "$@" 2>&1 | tail -200' _ $_it_run
      else
        echo "[check] impacted-test gate: SKIPPED — Docker unavailable, can't run the sanctioned Postgres harness on this VM."
        echo "[check]   The impacted backend tests were NOT verified locally; CI runs the authoritative Postgres suite on the PR."
        printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "impacted-test gate skipped — no Docker for the sanctioned harness; CI verifies impacted tests on the PR" >> /tmp/check_gate_env_@TASK_ID@.txt
      fi
    fi
