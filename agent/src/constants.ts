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

// Self-review loop
export const MAX_SELF_REVIEW_ATTEMPTS = 1;

// Claude Code runner
export const CLAUDE_TIMEOUT_MS = 45 * 60 * 1000;
export const MAX_OUTPUT_BYTES = 10_000_000;
