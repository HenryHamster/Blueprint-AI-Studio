// SINGLE SOURCE OF TRUTH (frontend) for the no-CI UNVERIFIED-merge fall-open
// reason labels. Imported by every operator-facing surface that renders a
// reason key — the per-task Merge Test Gate panel (TaskDetailDrawerMeta) and the
// project merge-verification banner (MergeVerificationBanner) — so they can
// never disagree.
//
// The orchestrator writes the reason KEY (not the label) into
// review_loop["unverified_merge_reason"]; admin.py exposes it as
// task.unverified_merge_reason and the live gate runtime as
// merge_gate_runtime.last_unverified_reason. This map MUST mirror the backend
// label map _UNVERIFIED_REASON_LABELS (backend/app/alert_dispatch.py); the
// `test_unverified_reason_labels_frontend_backend_parity` invariant
// (backend/testing/test_invariants.py) fails CI if the two key sets drift, so a
// new reason key cannot ship without a label on both sides.
export const UNVERIFIED_REASON_LABELS: Record<string, string> = {
  no_detectable_test_command: 'no runnable test command could be detected',
  kill_switch_off: 'the no-CI test-gate is disabled (kill-switch off)',
  internal_staging_base: 'the PR targets an internal staging branch (CI is path-filtered there)',
  undetected_runtime: 'the detected test suite produced no usable verdict',
  suite_all_skipped: 'a test suite was present but every test was skipped (no real verification ran)',
  verify_setup_failed: 'the detected test suite exists but could not be run (setup/runner infrastructure failed after retries)',
  probe_passed: 'no unit suite ran; only a cheap acceptance probe (compile/build) passed — not real test verification',
  probe_could_not_run: 'no unit suite ran and the acceptance probe could not run (missing deps/unprovisioned setup)',
};

/**
 * Map a raw reason KEY to its operator-facing label. Falls back to a readable
 * de-tokenized form for an unknown/forward-compat key so the operator never
 * sees a raw snake_case token (e.g. `some_future_reason` → `some future reason`).
 */
export function humanizeUnverifiedReason(key: string): string {
  return (
    UNVERIFIED_REASON_LABELS[key] ??
    key.replace(/[_-]+/g, ' ').replace(/\s+/g, ' ').trim()
  );
}
