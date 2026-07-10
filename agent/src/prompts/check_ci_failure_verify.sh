
# ── ci_failure closed-loop verify ───────────────────────────────────────────
# This is a CI-failure fix: verify the EXACT CI-failing pytest selector(s)
# pass locally BEFORE ./pr open can push. Targeted selectors (never the full
# suite — that OOMs the 8 GB agent VM); the harness memory guard exempts them.
# A still-failing target test fails this gate; ./pr open blocks until green.
CI_FAIL_SELECTORS=(@CI_FAIL_SELECTORS@)
if [ ${#CI_FAIL_SELECTORS[@]} -gt 0 ]; then
  if [ -f scripts/test-backend.sh ] && command -v docker >/dev/null 2>&1; then
    echo "[check] ci_failure verify: running ${#CI_FAIL_SELECTORS[@]} targeted CI-failing selector(s) via the sanctioned harness..."
    run "ci_failure selectors verify" bash -c 'set -o pipefail; bash scripts/test-backend.sh --postgres "$@" 2>&1 | tail -200' _ "${CI_FAIL_SELECTORS[@]}"
  else
    # No Docker → the sanctioned harness can't run here and a bare local pytest
    # would be the unfaithful SQLite fallback. Don't block on an infra artifact
    # (CI runs the authoritative Postgres suite on the PR after ./pr open); warn
    # and record a GATE-ENVIRONMENT marker ./pr surfaces after the gate.
    echo "[check] ci_failure verify: SKIPPED — Docker unavailable, can't run the sanctioned harness for the CI-failing selectors on this VM."
    echo "[check]   The targeted CI-failing tests were NOT verified locally; CI runs the authoritative Postgres suite on the PR."
    printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "ci_failure selector verify skipped — no Docker for the sanctioned harness; CI verifies the failing selectors on the PR" >> /tmp/check_gate_env_@TASK_ID@.txt
  fi
fi
