// ── Agent Constants ──────────────────────────────────────────────────────────
// All hardcoded numeric constants centralised here.
// Import what you need; do not re-declare locally.

// API helpers
export const API_TIMEOUT = 30_000;
export const API_MAX_RETRIES = 5;
export const LOGS_MAX_LENGTH = 450_000;
// Cap on per-attempt exponential backoff inside apiRequest.
export const API_RETRY_BACKOFF_MAX_MS = 30_000;

// Stale-result envelope (POST /api/agent/status retry persisted to disk
// when a POST fails — file survives VM kill, replayed on next startup).
export const STALE_RESULT_RETRY_BASE_MS = 30_000;
export const STALE_RESULT_RETRY_MAX_MS = 30 * 60_000;
export const STALE_RESULT_MAX_ATTEMPTS = 10;
export const STALE_RESULT_MAX_AGE_MS = 24 * 60 * 60 * 1000;
export const STDERR_TAIL_BYTES = 50_000;

// Subprocess lifecycle: grace window between SIGTERM and SIGKILL escalation.
export const PROCESS_KILL_GRACE_MS = 5_000;

// After Claude Code emits the structured `result` event (model finished its
// turn), the subprocess should exit promptly. If it doesn't, send SIGTERM after
// this grace window so the agent wrapper can proceed to commit/push/PR rather
// than burning the full CLAUDE_TIMEOUT_MS wall-clock budget.
//
// Observed 2026-05-04 on task 4ea9ff49: claude-code emitted result + completion
// summary, then hung. Wrapper waited 35 min (medium effort wall clock), threw
// ClaudeTimeoutError, task FAILED + requeued — work discarded with the VM
// because the post-Claude commit/push phase never ran. With this grace window
// the wrapper would have killed at +30s and proceeded to commit the diff.
export const CLAUDE_RESULT_GRACE_MS = 30_000;

// Mid-stream stall watchdog: if the claude-code subprocess stdout goes silent
// for this long while still alive (no data chunks received), force-kill via the
// same SIGTERM→SIGKILL escalation as resultGraceTimer. Resets on every chunk.
// Chosen to be well above any legitimate single tool call but well under the
// 20-60 min wall-clock budget so a hung tool call is caught quickly.
// flags.timedOut stays false so the post-Claude commit/push phase still runs.
// 2026-05-05: bumped 5 → 12 min. The backend Docker test runner is silent for
// 5-8 min during sidecar build + alembic + pytest (see TEST_RUNNER_DOCKER_TIMEOUT
// below); 5 min was firing inside legitimate test waits and killing agents
// mid-Bash with a complete diff but no shipped PR. Heartbeats fire on an
// independent 30s setInterval, so the reaper still sees the VM as alive.
export const CLAUDE_STREAM_STALL_MS = 12 * 60_000;

// Log/error message tail length used in status reports.
export const LOG_TAIL_BYTES = 5_000;

// Heartbeat
export const HEARTBEAT_INTERVAL = 30_000;

// Proactive agent-JWT refresh: fire at 75% of the 2h TTL (90 min) so the
// token is always renewed well before it expires. Matches AGENT_TOKEN_TTL_HOURS
// in backend/app/auth.py — if that value changes, adjust this accordingly.
export const TOKEN_PROACTIVE_REFRESH_MS = 90 * 60_000;

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

// Lint fix loop. 2026-05-03: bumped 3 → 5 once within-task fix-iteration
// history shipped (commit 02931339). Each retry now sees prior diff + failure
// tail in its prompt, so additional attempts are much more likely to pivot
// rather than repeat the same wrong fix. Cost of 2 extra attempts at worst is
// ~3-5 min of Claude time, paid for by converting CODE_ERROR failures into
// successes that don't need cross-VM remediation.
export const MAX_LINT_FIX_ATTEMPTS = 5;

// Frontend checks (admin-ui)
export const FRONTEND_NPM_TIMEOUT = 120_000;
export const FRONTEND_ESLINT_TIMEOUT = 60_000;
export const FRONTEND_BUILD_TIMEOUT = 120_000;
// Fresh VMs may need to apt-install Playwright deps and download Chromium
// before the viewport suite can start, which is materially slower than a build.
export const FRONTEND_PLAYWRIGHT_INSTALL_TIMEOUT = 600_000;

// Agent checks (agent/)
export const AGENT_NPM_TIMEOUT = 120_000;
export const AGENT_TSC_TIMEOUT = 60_000;

// Backend ruff checks
export const RUFF_TIMEOUT = 60_000;

// Test fix loop. 2026-05-03: bumped 2 → 3 alongside MAX_LINT_FIX_ATTEMPTS.
// Test failures are typically more semantically rich than lint, so the
// iteration-history feedback should help even faster than for lint.
export const MAX_TEST_FIX_ATTEMPTS = 3;
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

// Divergence detection thresholds for lint/test fix loops.
// These are intentionally conservative — false-positive abandons waste more
// than burning 1-2 extra iterations. Tune as data accumulates.
// Jaccard: two consecutive error pairs must both be ≥ threshold to trigger.
// 0.8 (not 0.7) to avoid false-positives from boilerplate tokens shared across
// all failures of a given runner (e.g. "[npm test (root)]:" prefix inflates
// Jaccard similarity for short errors that actually differ semantically).
export const FIX_LOOP_JACCARD_THRESHOLD = 0.8;
// Diff revert: fraction of prior iteration's added lines that must vanish in
// the current iteration before flagging as a revert.
export const FIX_LOOP_DIFF_REVERT_THRESHOLD = 0.8;

// Check-runaway watchdog thresholds for Claude-driven code-change flows.
// Intentionally lenient vs. wrapper-driven loops — Claude has more context per
// attempt, so false-positive kills are more expensive. Tune after observing
// dashboard data. Set DISABLE_CHECK_RUNAWAY_WATCHDOG=1 to disable entirely.
export const MAX_CLAUDE_CHECK_INVOCATIONS = 10; // hard ceiling: kill regardless of pattern
export const CHECK_RUNAWAY_MIN_INVOCATIONS = 6; // minimum calls before consecutive-failure check
export const CHECK_RUNAWAY_CONSECUTIVE_FAILURES = 3; // last-N failures triggers kill

// Scope overflow detection — 2× the upper bound of each scope range
export const SCOPE_OVERFLOW_THRESHOLDS: Record<string, number> = {
  small: 6,   // small expects 1-3 files; warn at >6
  medium: 10, // medium expects 2-5 files; warn at >10
  large: 20,  // large expects 5+ files; warn at >20
};

// Parallel review fan-out thresholds. False positive (extra fan-out) costs a
// little extra subagent overhead; false negative (no fan-out on a big rollup)
// costs review wall-clock. Keep defaults conservative — easier to widen later.
export const PARALLEL_REVIEW_THRESHOLDS = {
  minNonTestFiles: 4,
  minDistinctModules: 2,
} as const;

// Shared test-path classifier used by both fan-out helpers.
function isTestPath(p: string): boolean {
  return (
    /(^|\/)testing\//.test(p) ||
    /\.test\.[tj]sx?$/.test(p) ||
    /\.spec\.[tj]sx?$/.test(p) ||
    /(^|\/)tests?\//.test(p) ||
    /(^|\/)__tests__\//.test(p) ||
    /_test\.py$/.test(p) ||
    /(^|\/)test_[^/]+\.py$/.test(p)
  );
}

// Returns true when a review should fan out per-module investigation subagents.
// Trivial diffs and single-module changes always return false — subagent
// overhead dominates there. buildConfig is required to identify "module
// boundaries"; without it we default to false (conservative).
export function shouldFanOutReview(
  changedFilePaths: string[],
  buildConfig?: { directories?: { path: string }[] },
): boolean {
  const dirs = buildConfig?.directories;
  if (!dirs || dirs.length === 0) return false;
  const nonTest = changedFilePaths.filter(p => !isTestPath(p));
  if (nonTest.length < PARALLEL_REVIEW_THRESHOLDS.minNonTestFiles) return false;
  const matched = new Set<string>();
  for (const p of nonTest) {
    for (const d of dirs) {
      const prefix = d.path.endsWith('/') ? d.path : d.path + '/';
      if (p === d.path || p.startsWith(prefix)) {
        matched.add(d.path);
        break;
      }
    }
  }
  return matched.size >= PARALLEL_REVIEW_THRESHOLDS.minDistinctModules;
}

// Parallel fix fan-out thresholds. Conservative defaults: fan-out only when
// the diff is genuinely multi-module OR the reviewer raised many issues across
// multiple files. Small diffs and single-module fixes always stay serial.
export const PARALLEL_FIX_THRESHOLDS = {
  minNonTestFiles: 4,
  minDistinctModules: 2,
  minReviewerIssues: 6,
  minIssueFiles: 2,
} as const;

// Returns true when a fix iteration should fan out per-slice investigation
// subagents. Two independent axes can trigger fan-out:
//   (a) multi-module diff: >=minNonTestFiles non-test files across >=minDistinctModules
//       build_config directories
//   (b) many reviewer issues: >=minReviewerIssues issues spanning >=minIssueFiles
//       distinct files
// buildConfig is optional — without it only the issue-count axis can trigger.
export function shouldFanOutFix(
  changedFilePaths: string[],
  reviewIssueCount?: number,
  reviewIssueFiles?: string[],
  buildConfig?: { directories?: { path: string }[] },
): boolean {
  // Issue-cluster axis: many reviewer issues across multiple files
  if (
    reviewIssueCount !== undefined &&
    reviewIssueCount >= PARALLEL_FIX_THRESHOLDS.minReviewerIssues &&
    (reviewIssueFiles?.length ?? 0) >= PARALLEL_FIX_THRESHOLDS.minIssueFiles
  ) {
    return true;
  }
  // Module axis: large multi-module diff
  const dirs = buildConfig?.directories;
  if (!dirs || dirs.length === 0) return false;
  const nonTest = changedFilePaths.filter(p => !isTestPath(p));
  if (nonTest.length < PARALLEL_FIX_THRESHOLDS.minNonTestFiles) return false;
  const matched = new Set<string>();
  for (const p of nonTest) {
    for (const d of dirs) {
      const prefix = d.path.endsWith('/') ? d.path : d.path + '/';
      if (p === d.path || p.startsWith(prefix)) {
        matched.add(d.path);
        break;
      }
    }
  }
  return matched.size >= PARALLEL_FIX_THRESHOLDS.minDistinctModules;
}

// Pre-implementation analysis plan gate. If a medium-scope analysis plan
// proposes more files than this, the handler retries the analysis with a
// narrowing directive once, then fails fast rather than burning the
// implementation budget on a task that will hit SCOPE_OVERFLOW_THRESHOLDS.
export const MEDIUM_ANALYSIS_PLAN_FILE_LIMIT = 10;

// Mid-stream stall detection: if a tool_use event is in-flight for longer
// than this without a corresponding tool-result (user) event, the subprocess
// is considered stalled and will be SIGTERMed.
export const TOOL_USE_STALL_TIMEOUT_MS = 30 * 60_000;

// Claude Code runner
export const CLAUDE_TIMEOUT_MS = 45 * 60 * 1000;
export const MAX_OUTPUT_BYTES = 10_000_000;

// Stream-json drain: bound event-loop blocking inside drainJsonChunks. Yield
// after every WITHIN_CHUNK_YIELD_LINES parsed lines OR every WITHIN_CHUNK_YIELD_MS
// of wall time, whichever fires first. Tuned so heartbeat (30s) and SIGTERM
// signal handlers stay deliverable even during heavy test-backend.sh stdout.
// Lowering YIELD_LINES below 50 increases yield overhead without tangible
// benefit on healthy VMs; raising YIELD_MS above 50 risks crossing the
// detectable lag threshold (5s) on adversarial single-line megablobs.
export const WITHIN_CHUNK_YIELD_LINES = 50;
export const WITHIN_CHUNK_YIELD_MS = 50;

// Heartbeat-specific API params, overriding the global API_TIMEOUT / API_MAX_RETRIES.
// Heartbeats are best-effort liveness signals — if the orchestrator is slow
// or returning 5xx during a Cloud Run cutover, retrying for the full 30s × 5
// chain (~3min) holds the in-flight mutex and starves subsequent setInterval
// ticks. Tighter timeouts + fewer retries keep the mutex turning over so
// the orchestrator can see fresh heartbeats once it recovers.
// Production incident 2026-05-04: cutover-driven 5xx run blocked heartbeats
// for 15+ minutes with the stock retry chain.
export const HEARTBEAT_API_TIMEOUT = 10_000;
export const HEARTBEAT_API_MAX_RETRIES = 2;

// Effort-scaled subprocess timeout. Low-effort tasks that hang waste less VM
// time; max-effort deep analysis gets more room. Unknown/unset effort falls
// back to CLAUDE_TIMEOUT_MS.
export const CLAUDE_TIMEOUT_MS_BY_EFFORT: Record<string, number | undefined> = {
  low: 20 * 60 * 1000,
  medium: 35 * 60 * 1000,
  high: 45 * 60 * 1000,
  max: 60 * 60 * 1000,
};

export function claudeTimeoutForEffort(effort?: string): number {
  if (effort) {
    const v = CLAUDE_TIMEOUT_MS_BY_EFFORT[effort];
    if (v !== undefined) return v;
  }
  return CLAUDE_TIMEOUT_MS;
}
