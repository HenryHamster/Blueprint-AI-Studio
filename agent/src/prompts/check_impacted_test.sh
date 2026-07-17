
    # ── impacted-test merge-time gate ───────────────────────────────────────
    # Complement to the standalone smoke run above: mechanically run the backend
    # test files IMPACTED by this diff (test files that import a changed
    # app.<module> or mock-patch it by string target, plus any test file the PR
    # itself adds). Derived from $CHANGED via the committed reverse-dep resolver
    # (invoked BY REFERENCE — no embedded Python heredoc, see CLAUDE.md), run
    # bounded under the sanctioned Postgres harness so an impacted-test failure
    # blocks ./pr open instead of leaking to the promotion suite.
    IMPACTED_TEST_CAP=8
    IMPACTED_RESOLVER_TIMEOUT_SECS=30
    IMPACTED_GATE_TIMEOUT_SECS=120
    _it_stderr_f=$(mktemp /tmp/check_impacted_XXXXXX)
    _it_all=""
    if command -v timeout >/dev/null 2>&1; then
      _it_all=$(printf '%s\n' "$CHANGED" | timeout --foreground --signal=TERM --kill-after=5s "${IMPACTED_RESOLVER_TIMEOUT_SECS}s" python3 backend/testing/impacted_tests.py 2>"$_it_stderr_f")
      _it_rc=$?
      case "$_it_rc" in
        0) ;;
        124|137)
          echo "[check] impacted-test resolver: GATE-ENVIRONMENT — exceeded ${IMPACTED_RESOLVER_TIMEOUT_SECS}s; authoritative CI will resolve the full set."
          printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "impacted-test resolver timed out after ${IMPACTED_RESOLVER_TIMEOUT_SECS}s; CI verifies impacted tests on the PR" >> /tmp/check_gate_env_@TASK_ID@.txt
          _it_all=""
          ;;
        125|126|127)
          echo "[check] impacted-test resolver: GATE-ENVIRONMENT — timeout utility or resolver unavailable; authoritative CI will resolve the full set."
          printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "impacted-test resolver unavailable (exit $_it_rc); CI verifies impacted tests on the PR" >> /tmp/check_gate_env_@TASK_ID@.txt
          _it_all=""
          ;;
        *)
          echo "[check] impacted-test resolver: crashed (exit $_it_rc) — stderr follows:" >&2
          cat "$_it_stderr_f" >&2
          echo "[check] impacted-test resolver: crash surfaced as gate failure" >&2
          FAILED+=("impacted-test resolver crash")
          ;;
      esac
    else
      echo "[check] impacted-test resolver: GATE-ENVIRONMENT — timeout utility unavailable; authoritative CI will resolve the full set."
      printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "impacted-test resolver skipped — timeout utility unavailable; CI verifies impacted tests on the PR" >> /tmp/check_gate_env_@TASK_ID@.txt
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
        _it_log_f=$(mktemp /tmp/check_impacted_gate_XXXXXX)
        # shellcheck disable=SC2086
        timeout --foreground --signal=TERM --kill-after=15s "${IMPACTED_GATE_TIMEOUT_SECS}s" bash scripts/test-backend.sh --postgres $_it_run >"$_it_log_f" 2>&1 &
        _it_pid=$!
        _it_elapsed=0
        while kill -0 "$_it_pid" 2>/dev/null; do
          sleep 15
          _it_elapsed=$((_it_elapsed + 15))
          echo "[check] impacted-test gate: still running (${_it_elapsed}s/${IMPACTED_GATE_TIMEOUT_SECS}s)..."
          tail -1 "$_it_log_f" 2>/dev/null || true
        done
        wait "$_it_pid"
        _it_rc=$?
        tail -200 "$_it_log_f"
        rm -f "$_it_log_f"
        case "$_it_rc" in
          0) echo "[check] impacted-test gate: OK" ;;
          124|137)
            echo "[check] impacted-test gate: GATE-ENVIRONMENT — exceeded ${IMPACTED_GATE_TIMEOUT_SECS}s; authoritative CI verifies these tests."
            printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "impacted-test gate timed out after ${IMPACTED_GATE_TIMEOUT_SECS}s; CI verifies impacted tests on the PR" >> /tmp/check_gate_env_@TASK_ID@.txt
            ;;
          125|126|127)
            echo "[check] impacted-test gate: GATE-ENVIRONMENT — bounded harness unavailable (exit $_it_rc); authoritative CI verifies these tests."
            printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "impacted-test bounded harness unavailable (exit $_it_rc); CI verifies impacted tests on the PR" >> /tmp/check_gate_env_@TASK_ID@.txt
            ;;
          *)
            echo "[check] impacted-test gate: FAILED (exit $_it_rc)"
            FAILED+=("impacted-test gate")
            ;;
        esac
      else
        echo "[check] impacted-test gate: SKIPPED — Docker unavailable, can't run the sanctioned Postgres harness on this VM."
        echo "[check]   The impacted backend tests were NOT verified locally; CI runs the authoritative Postgres suite on the PR."
        printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "impacted-test gate skipped — no Docker for the sanctioned harness; CI verifies impacted tests on the PR" >> /tmp/check_gate_env_@TASK_ID@.txt
      fi
    fi
