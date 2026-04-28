// ── Agent Constants ──────────────────────────────────────────────────────────
// All hardcoded numeric constants centralised here.
// Import what you need; do not re-declare locally.

// API helpers
export const API_TIMEOUT = 30_000;
export const API_MAX_RETRIES = 5;
export const LOGS_MAX_LENGTH = 450_000;

// Heartbeat
export const HEARTBEAT_INTERVAL = 30_000;

// WebSocket log buffer
export const MAX_LOG_BUFFER = 1_000;

// Git clone
export const GIT_CLONE_TIMEOUT = 300_000;
export const CLONE_MAX_RETRIES = 3;
export const CLONE_RETRY_BASE_DELAY = 5_000;
export const DEFAULT_CLONE_DEPTH = 50;

// Force-with-lease push retry (concurrent-push recovery).
// maxRetries is "attempts AFTER the initial push" — default 2 gives 3 total
// attempts, which covers 3+ sibling agents racing the same branch.
export const PUSH_FORCE_WITH_LEASE_MAX_RETRIES = 2;
export const PUSH_FORCE_WITH_LEASE_RETRY_DELAY = 1_000;

// Conflict marker grep
export const CONFLICT_CHECK_TIMEOUT = 10_000;

// Lint fix loop
export const MAX_LINT_FIX_ATTEMPTS = 3;

// Frontend checks (admin-ui)
export const FRONTEND_NPM_TIMEOUT = 120_000;
export const FRONTEND_ESLINT_TIMEOUT = 60_000;
export const FRONTEND_BUILD_TIMEOUT = 120_000;

// Agent checks (agent/)
export const AGENT_NPM_TIMEOUT = 120_000;
export const AGENT_TSC_TIMEOUT = 60_000;

// Backend ruff checks
export const RUFF_TIMEOUT = 60_000;

// Test fix loop
export const MAX_TEST_FIX_ATTEMPTS = 2;
export const TEST_RUNNER_TIMEOUT = 180_000;
// Docker test runners (e.g. scripts/test-backend.sh --postgres) build the
// image, start a Postgres sidecar, run alembic upgrade, then execute the
// full pytest suite — typically 5–8 minutes end to end. The non-Docker
// budget is too tight to be useful here, so we branch in testFixLoop /
// captureTestBaseline at every isDockerRunner site.
export const TEST_RUNNER_DOCKER_TIMEOUT = 600_000;
export const TEST_BASELINE_TIMEOUT = 60_000;
export const TEST_BASELINE_DOCKER_TIMEOUT = 600_000;

// Self-review loop
export const MAX_SELF_REVIEW_ATTEMPTS = 2;

// Scope overflow detection — 2× the upper bound of each scope range
export const SCOPE_OVERFLOW_THRESHOLDS: Record<string, number> = {
  small: 6,   // small expects 1-3 files; warn at >6
  medium: 10, // medium expects 2-5 files; warn at >10
  large: 20,  // large expects 5+ files; warn at >20
};

// Pre-implementation analysis plan gate. If a medium-scope analysis plan
// proposes more files than this, the handler retries the analysis with a
// narrowing directive once, then fails fast rather than burning the
// implementation budget on a task that will hit SCOPE_OVERFLOW_THRESHOLDS.
export const MEDIUM_ANALYSIS_PLAN_FILE_LIMIT = 10;

// Claude Code runner
export const CLAUDE_TIMEOUT_MS = 45 * 60 * 1000;
export const MAX_OUTPUT_BYTES = 10_000_000;

// Effort-scaled subprocess timeout. Low-effort tasks that hang waste less VM
// time; max-effort deep analysis gets more room. Unknown/unset effort falls
// back to CLAUDE_TIMEOUT_MS.
export const CLAUDE_TIMEOUT_MS_BY_EFFORT: Record<string, number> = {
  low: 20 * 60 * 1000,
  medium: 35 * 60 * 1000,
  high: 45 * 60 * 1000,
  max: 60 * 60 * 1000,
};

export function claudeTimeoutForEffort(effort?: string): number {
  if (effort && CLAUDE_TIMEOUT_MS_BY_EFFORT[effort] !== undefined) {
    return CLAUDE_TIMEOUT_MS_BY_EFFORT[effort];
  }
  return CLAUDE_TIMEOUT_MS;
}
