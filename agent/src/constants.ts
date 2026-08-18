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
// 1.5-6 h wall-clock backstop so a hung tool call is caught quickly.
// flags.timedOut stays false so the post-Claude commit/push phase still runs.
// 2026-05-05: bumped 5 → 12 min. The backend Docker test runner is silent for
// 5-8 min during sidecar build + alembic + pytest (see TEST_RUNNER_DOCKER_TIMEOUT
// below); 5 min was firing inside legitimate test waits and killing agents
// mid-Bash with a complete diff but no shipped PR. Heartbeats fire on an
// independent 30s setInterval, so the reaper still sees the VM as alive.
export const CLAUDE_STREAM_STALL_MS = 12 * 60_000;

// Max time the model may go without ANY stream-json progress signal — a content
// event (assistant/result) OR an incremental partial-message delta
// (content_block_delta text / thinking_delta, emitted by --include-partial-messages)
// — while NO tool is in flight, before the call is treated as a silent provider
// hang. With partial messages the model emits a delta every few seconds while
// actively generating/thinking, so a slow-but-working opus turn keeps this fresh
// and never trips; only a genuinely hung call (a real 429/529 the SDK retries
// silently, emitting ZERO model deltas) goes stale and fires. Such a hang
// otherwise rides to the wall-clock backstop and is misclassified
// AGENT_TIMEOUT (incident 2026-05-29). On trigger, the agent reports the hang
// (rate_limited only when a confirmed rate-limit message is present, else the
// transient no_progress_timeout) so the orchestrator holds/reroutes instead of
// burning the retry budget. Bumped 12→15 min as defense-in-depth so a brief
// inter-delta gap (large thinking block, big tool-result re-ingest) has more
// slack. Env-overridable for ops tuning.
export const LLM_NO_PROGRESS_STALL_MS =
  Number(process.env.LLM_NO_PROGRESS_STALL_MS) || 15 * 60_000;

// Log/error message tail length used in status reports.
export const LOG_TAIL_BYTES = 5_000;

// Heartbeat
export const HEARTBEAT_INTERVAL = 30_000;

// Deadline-escape for the heartbeat in-flight mutex. If a heartbeat POST stays
// outstanding longer than this, the next tick treats it as wedged and reclaims
// the slot so liveness can't be latched silent by a single hung request. Set
// above the worst-case LEGIT hold so a merely-slow cycle doesn't spuriously
// reclaim: a fully-stalling 409 retry chain (a fast 409, then two body-stalling
// retry POSTs each bounded to ~2×HEARTBEAT_API_TIMEOUT by the apiRequest
// body-phase re-arm, plus the 5s/10s inter-retry backoffs) is ~100s. And well
// below the reaper's 5-minute (300s) no-heartbeat cutoff, so a genuine wedge is
// reclaimed (≤150s + one 30s tick) with ample margin before a false reap.
export const HEARTBEAT_INFLIGHT_DEADLINE = 150_000;

// Proactive agent-JWT refresh timing.
// The agent decodes the token's real exp claim and schedules refresh at
// FRACTION of remaining lifetime, clamped to [FLOOR, CEILING]. This removes
// the hardcoded TTL mirror and makes refresh robust to any future TTL change.
export const TOKEN_REFRESH_FRACTION = 0.75;
export const TOKEN_REFRESH_FLOOR_MS = 3 * 60_000;    // 3 min minimum
export const TOKEN_REFRESH_CEILING_MS = 55 * 60_000; // 55 min maximum

// Bootstrap JWT detection: a remaining lifetime above this threshold
// (75 min) indicates a long bootstrap token rather than a normal short TTL
// (~60 min). When detected, the agent fires an early confirmation refresh
// after BOOTSTRAP_CONFIRMATION_DELAY_MS so it promptly downgrades to the
// short TTL without waiting for the normal proactive cadence.
export const BOOTSTRAP_TOKEN_THRESHOLD_MS = 75 * 60_000; // 75 min
export const BOOTSTRAP_CONFIRMATION_DELAY_MS = 30_000;   // 30 s

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

// Node dependency preinstall for review-prep (`npm ci`) in preinstallNodeDeps.
// The only per-check timeout the agent process applies directly; the actual
// lint/typecheck/build/test check STEPS run inside the model-driven `./check`
// bash script (bounded by the stream-stall + wall-clock guards above) or the
// acceptance-probe runner in verify-tests.ts (GENERIC_PROBE_TIMEOUT /
// DECLARED_BUILD_TIMEOUT), so no separate eslint/tsc/ruff/build timeout
// constants are consumed here.
export const FRONTEND_NPM_TIMEOUT = 120_000;

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

// Acceptance probe timeouts
// Generic ecosystem probes (tsc/compileall/go build/cargo check) use the baseline.
export const GENERIC_PROBE_TIMEOUT = TEST_BASELINE_TIMEOUT;
// Declared build commands (operator's authoritative build_cmd) get more time.
// Comparable to test runners but not Docker (a real build > cheap compile-check,
// but < full suite with sidecar overhead). Allows realistic builds to complete.
export const DECLARED_BUILD_TIMEOUT = TEST_RUNNER_TIMEOUT;

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

// Scope overflow detection — a generous multiple of each scope's expected
// range. These exist to catch RUNAWAY scope (a task quietly rewriting half the
// repo), not to squeeze organic growth: modern implementing agents handle
// multi-file changes well, and punishing a task for touching the callers and
// consumers its contract change requires just trains the pipeline to ship
// half-wired fragments. Keep in sync with backend SCOPE_FILE_BUDGETS
// (backend/app/spawn_calibration.py) and _SCOPE_OVERFLOW_THRESHOLDS
// (backend/app/retry_prompt.py).
export const SCOPE_OVERFLOW_THRESHOLDS: Record<string, number> = {
  small: 8,   // small expects a handful of files; warn at >8
  medium: 16, // medium expects up to ~a dozen files; warn at >16
  large: 32,  // large is cross-component; warn at >32
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

// Greenfield detection: a cloned working tree with at most this many real
// source files (config/scaffolding excluded — lockfiles, *.md/*.json/*.yml,
// dotfiles, manifests) is treated as an empty/near-empty repo. The plan and
// research agents then enter "bootstrap the foundation from the goal" mode
// instead of assuming there is an existing codebase to read and improve.
export const GREENFIELD_SOURCE_FILE_THRESHOLD = 8;

// Pre-implementation analysis plan gate. If a medium-scope analysis plan
// proposes more files than this, the handler retries the analysis with a
// narrowing directive once, then fails fast rather than burning the
// implementation budget on a task that will hit SCOPE_OVERFLOW_THRESHOLDS.
export const MEDIUM_ANALYSIS_PLAN_FILE_LIMIT = 16;

// Maximum characters of analysis text injected into the implementation prompt.
// Prevents unbounded growth from large pre-implementation analysis results.
// When truncation fires, a clearly-marked notice is appended and a warning is
// sent via sendLog so the agent knows the plan was cut.
export const ANALYSIS_CONTEXT_CHAR_CAP = 16_000;

// Mid-stream stall detection: if a tool_use event is in-flight for longer
// than this WITHOUT EMITTING ANY SUB-PROGRESS (no further stream-json event of
// any kind under it — including nested sub-agent assistant/tool/result events),
// the subprocess is considered stalled and will be SIGTERMed. The watchdog
// resets on any sub-progress event, so this budget is "max silence within a
// single tool call", not "max total tool duration" — a legitimately long tool
// (a backend test run, a workflow/Task sub-agent fan-out) that keeps streaming
// stays alive indefinitely; only a tool that goes truly silent times out.
export const TOOL_USE_STALL_TIMEOUT_MS = 30 * 60_000;

// Tools that fan out into long-running sub-agents / sub-workflows (the built-in
// Claude Code Task/Agent/Workflow tool and any future custom workflow tool an
// agent authors) legitimately run far longer than an ordinary tool call while
// streaming sub-agent progress events. Their in-flight silence budget is the
// larger of TOOL_USE_STALL_TIMEOUT_MS and this value so a deep fan-out is never
// false-killed mid-flight. Still bounded so a genuinely hung workflow that
// stops emitting sub-progress is eventually caught. Env-overridable for tuning.
export const WORKFLOW_TOOL_STALL_TIMEOUT_MS =
  Number(process.env.WORKFLOW_TOOL_STALL_TIMEOUT_MS) || 45 * 60_000;

// Tool names (lowercased) that spawn long-running sub-agents / sub-workflows.
// Matched case-insensitively as either an exact name or a substring so future
// MCP-namespaced or custom workflow tools (e.g. "mcp__workflow__run") are
// covered without an allowlist edit. Used to grant the longer in-flight silence
// budget above and to treat their nested sub-agent events as legitimate progress.
export const WORKFLOW_TOOL_NAME_PATTERNS: readonly string[] = [
  'task',
  'agent',
  'workflow',
] as const;

export function isWorkflowToolName(name: string | undefined): boolean {
  if (!name) return false;
  const lower = name.toLowerCase();
  return WORKFLOW_TOOL_NAME_PATTERNS.some(p => lower === p || lower.includes(p));
}

// Claude Code runner
export const CLAUDE_TIMEOUT_MS = 4 * 60 * 60 * 1000;
export const MAX_OUTPUT_BYTES = 10_000_000;
// Structured stream modes (claude-stream-json / codex-jsonl) wrap every token
// in a JSON event envelope, so raw stream bytes overstate model output by
// 20-100x — a fast provider legitimately passes MAX_OUTPUT_BYTES mid-task
// (deepseek-v4-flash: ~57 turns / ~25k output tokens hit 10MB in 5 minutes).
// Past the cap those runs keep only a rolling tail of this size instead of
// being killed; runaway is still bounded by the stall/no-progress watchdogs
// and the run timeout.
export const OUTPUT_TAIL_AFTER_CAP_BYTES = 2_000_000;

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

// Effort-scaled subprocess wall-clock ceiling. This is a RUNAWAY BACKSTOP, not
// the stuck-detector: a genuinely hung run is caught far earlier by the stall
// guards (CLAUDE_STREAM_STALL_MS, LLM_NO_PROGRESS_STALL_MS,
// TOOL_USE_STALL_TIMEOUT_MS — all fire on *silence*, in ≤45 min). The wall
// clock is the only guard that kills an actively-progressing run, and tool
// time counts against it: a create task's `./check test` gate (Docker image
// build + standalone pytest on a 2-vCPU VM) alone can eat 20+ min. At the old
// 20–60 min caps that regularly executed finished work (task 705668f7,
// 2026-06-07: killed mid-gate at minute 35 with the diff complete, ~30 min of
// work lost and redone from scratch). Sized so the ceiling comfortably exceeds
// worst-case legitimate gate time and only a pathological busy-loop — output
// flowing, never converging — can reach it. Unknown/unset effort falls back
// to CLAUDE_TIMEOUT_MS.
export const CLAUDE_TIMEOUT_MS_BY_EFFORT: Record<string, number | undefined> = {
  low: 90 * 60 * 1000,
  medium: 3 * 60 * 60 * 1000,
  high: 4 * 60 * 60 * 1000,
  max: 6 * 60 * 60 * 1000,
};

export function claudeTimeoutForEffort(effort?: string): number {
  if (effort) {
    const v = CLAUDE_TIMEOUT_MS_BY_EFFORT[effort];
    if (v !== undefined) return v;
  }
  return CLAUDE_TIMEOUT_MS;
}
