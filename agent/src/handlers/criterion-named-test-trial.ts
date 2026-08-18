/**
 * Criterion named-test trial handler — execution half of the post-merge
 * acceptance earn loop.
 *
 * A `criterion_named_test_trial` task executes ONLY a criterion's declared
 * covering tests at a pinned merged-integration revision and reports a
 * structured receipt. The backend enrollment (`criterion_named_test_trial`
 * task payload: criterion_key, criterion_text, test_refs, head_sha,
 * max_test_files, wall_clock_seconds) observes the exact revision the covering
 * tests shipped in; this handler pins the checkout to that revision so the run
 * exercises the merged code the enrollment statically proved, never a drifted
 * branch tip. The backend ingestion completion handler mints genuine `covered`
 * criterion evidence on a GREEN receipt and records NOTHING (fail-open) on a
 * non-green or unobservable one.
 *
 * This handler NEVER modifies code and opens no PR. It reuses the capability
 * proof trial's targeted runner and the verify-tests gate's
 * `classifyAcceptanceTestRun` / `acceptanceTestRunPassed` classification so a
 * green determination is unambiguous (exit-zero AND the targeted tests actually
 * ran). A ref set that cannot be targeted cleanly, or a pinned revision that
 * cannot be resolved, reports `could_not_run: true` and mints nothing.
 */

import type WebSocket from 'ws';
// NOTE: capability-proof-trial.js must be imported BEFORE ../index.js. The
// agent's index.ts re-exports the handlers registry (which imports this module),
// so in tests that mock ../index.js the mock factory's importOriginal load of
// the real index.ts would otherwise pull this module in with REAL index.js
// bindings. Importing the shared trial helpers first keeps this module (and its
// reuse of capability-proof-trial's execFileAsync/sendLog) on the mocked graph.
import {
  buildCapabilityProofTrialRunner,
  provisionNodeDepsForTrial,
  parseTrialSkippedSummary,
  isRefSetTargetable,
} from './capability-proof-trial.js';
import {
  WORKSPACE, sanitize, sendLog, persistAndPostStatus, execFileAsync,
  resolveBaseBranch, validateBranchName,
} from '../index.js';
import { setPromptSizeTelemetry } from '../telemetry.js';
import { prepareWorkspaceForRole } from '../workspacePrep.js';
import { DEFAULT_CLONE_DEPTH } from '../constants.js';
import {
  classifyAcceptanceTestRun, acceptanceTestRunPassed,
  matchesEnvironmentFailureSignature,
} from '../utils/test-runners.js';

// Default hard cap on covering test files per trial (mirrors the backend
// capability-proof bound). The payload may carry a smaller max_test_files; the
// effective cap is the min of the two.
const DEFAULT_MAX_TEST_FILES = 10;
// Default wall-clock bound (ms) for the whole targeted run when the payload
// carries no wall_clock_seconds. Keeps a mis-declared covering surface from
// running unbounded on the runner.
const DEFAULT_WALL_CLOCK_MS = 900_000;

/** The bounded targeted-test receipt the handler reports back. The backend
 *  ingestion parses this JSON from the completion logs; the `type` discriminator
 *  must match the enrollment task type exactly. */
export interface CriterionNamedTestTrialResult {
  type: 'criterion_named_test_trial';
  criterion_key: string;
  /** The SHA actually executed (git rev-parse HEAD at run time), or null when
   *  the checkout SHA could not be resolved. NEVER the queued SHA echoed back. */
  head_sha: string | null;
  /** The SHA queued when the trial was enrolled; retained for drift visibility. */
  queued_head_sha: string | null;
  test_refs: string[];
  ran: boolean;
  passed: boolean;
  skipped_summary?: { total: number; skipped: number };
  could_not_run: boolean;
  /** Stable orchestrator-authored retry identities. Additive for legacy tasks. */
  trial_identity?: string;
  test_identity?: string;
  precondition_identity?: string;
  /** Named absent precondition; never a passing/failing test verdict. */
  could_not_run_reason?: string;
}

export async function handleCriterionNamedTestTrial(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  _model?: string, _effort?: string,
): Promise<void> {
  // No Claude invocation — this is a programmatic targeted-test run.
  setPromptSizeTelemetry(0, 0, []);

  const criterionKey = typeof payload.criterion_key === 'string' ? payload.criterion_key.trim() : '';
  const headSha = typeof payload.head_sha === 'string' && payload.head_sha.trim()
    ? payload.head_sha.trim()
    : null;
  const rawRefs = Array.isArray(payload.test_refs)
    ? (payload.test_refs as unknown[]).filter((r): r is string => typeof r === 'string' && r.trim().length > 0)
    : [];
  const maxFiles = typeof payload.max_test_files === 'number' && payload.max_test_files > 0
    ? Math.min(payload.max_test_files, DEFAULT_MAX_TEST_FILES)
    : DEFAULT_MAX_TEST_FILES;
  const testRefs = rawRefs.slice(0, maxFiles);
  const wallClockMs = typeof payload.wall_clock_seconds === 'number' && payload.wall_clock_seconds > 0
    ? payload.wall_clock_seconds * 1000
    : DEFAULT_WALL_CLOCK_MS;
  const identityReceipt = {
    ...(typeof payload.trial_identity === 'string' && payload.trial_identity.trim()
      ? { trial_identity: payload.trial_identity.trim() } : {}),
    ...(typeof payload.test_identity === 'string' && payload.test_identity.trim()
      ? { test_identity: payload.test_identity.trim() } : {}),
    ...(typeof payload.precondition_identity === 'string' && payload.precondition_identity.trim()
      ? { precondition_identity: payload.precondition_identity.trim() } : {}),
  };

  const report = async (result: CriterionNamedTestTrialResult): Promise<void> => {
    await persistAndPostStatus(taskId, {
      task_id: taskId,
      status: 'COMPLETED',
      logs: sanitize(JSON.stringify({ ...result, ...identityReceipt })),
      repo_url: `https://github.com/${owner}/${repo}`,
    });
  };

  // Pre-clone targetability check (no Docker probe — isDockerAvailable() only runs
  // when the workspace is ready and we are about to execute, not here).
  if (!criterionKey || testRefs.length === 0 || !isRefSetTargetable(testRefs)) {
    sendLog(ws, `Criterion named-test trial: ${!criterionKey ? 'no criterion_key' : testRefs.length === 0 ? 'no test refs' : 'refs not cleanly targetable'} — reporting could_not_run.\n`);
    await report({
      type: 'criterion_named_test_trial',
      criterion_key: criterionKey,
      // No checkout happened: never echo the queued candidate as executed.
      head_sha: null, queued_head_sha: headSha, test_refs: testRefs,
      ran: false, passed: false, could_not_run: true,
      could_not_run_reason: !criterionKey
        ? 'criterion_identity_missing'
        : testRefs.length === 0
          ? 'test_identity_missing'
          : 'test_target_unavailable',
    });
    return;
  }

  // Clone the repo at the base branch, then (optionally) pin to the exact
  // merged-integration revision the enrollment observed.
  const baseBranch = validateBranchName(resolveBaseBranch(payload, task), 'base_branch');
  statusRef.value = `Cloning ${owner}/${repo} @ ${baseBranch} for criterion named-test trial...`;
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  await prepareWorkspaceForRole({
    workspace: WORKSPACE, repoUrl: cloneUrl, githubToken: ghToken,
    branch: baseBranch, baseBranch, depth: DEFAULT_CLONE_DEPTH,
  });

  // Pin to the declared merged-integration revision when the payload carries one.
  // The base-branch clone is shallow (depth-limited) and may not contain the
  // pinned SHA, so fetch it before checking out. A pin that cannot be resolved
  // is fail-open: report could_not_run rather than running the covering tests at
  // the WRONG revision.
  if (headSha) {
    try {
      await execFileAsync('git', ['fetch', 'origin', headSha], { cwd: WORKSPACE });
      sendLog(ws, `Criterion named-test trial [${criterionKey}]: fetched pinned SHA ${headSha}\n`);
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : String(err);
      sendLog(ws, `Criterion named-test trial [${criterionKey}]: git fetch origin ${headSha} failed — ${message.slice(0, 300)}\n`);
    }
    try {
      await execFileAsync('git', ['checkout', headSha], { cwd: WORKSPACE });
      sendLog(ws, `Criterion named-test trial [${criterionKey}]: checked out pinned SHA ${headSha}\n`);
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : String(err);
      sendLog(ws, `Criterion named-test trial [${criterionKey}]: pinned SHA ${headSha} could not be resolved — reporting could_not_run (${message.slice(0, 300)})\n`);
      await report({
        type: 'criterion_named_test_trial',
        criterion_key: criterionKey,
        // No checkout happened — nothing executed, so no executed SHA exists.
        head_sha: null, queued_head_sha: headSha, test_refs: testRefs,
        ran: false, passed: false, could_not_run: true,
        could_not_run_reason: 'candidate_unavailable',
      });
      return;
    }
  }

  // Proof provenance must name the checkout that ACTUALLY ran the tests, never
  // the queued SHA. A failed identity lookup is indeterminate, not permission to
  // echo the queued SHA; test execution and its classification still proceed.
  let executedHeadSha: string | null = null;
  try {
    const resolved = await execFileAsync('git', ['rev-parse', 'HEAD'], { cwd: WORKSPACE });
    executedHeadSha = resolved.trim() || null;
  } catch (err: unknown) {
    const message = err instanceof Error ? err.message : String(err);
    sendLog(ws, `Criterion named-test trial [${criterionKey}]: could not resolve checkout SHA: ${message}\n`);
  }

  // Build the runner post-clone so the Docker probe reflects execution-time state.
  const runner = buildCapabilityProofTrialRunner(testRefs, WORKSPACE);
  if (runner === null) {
    // Safety: isRefSetTargetable passed pre-clone but runner is null now — treat as
    // untargetable to avoid a confusing could_not_run classification.
    sendLog(ws, `Criterion named-test trial [${criterionKey}]: runner unexpectedly null post-clone — reporting could_not_run.\n`);
    await report({
      type: 'criterion_named_test_trial',
      criterion_key: criterionKey,
      head_sha: executedHeadSha, queued_head_sha: headSha, test_refs: testRefs,
      ran: false, passed: false, could_not_run: true,
      could_not_run_reason: 'test_target_unavailable',
    });
    return;
  }

  // For TS/JS tests, provision committed dependencies before invoking vitest.
  // Python tests use scripts/test-backend.sh which manages its own environment.
  // Fail-open: an install failure is logged but never blocks the test attempt —
  // on warm VMs node_modules survive git checkout so the run can still succeed.
  if (runner.cmd === 'npx') {
    await provisionNodeDepsForTrial(runner.cwd, ws, criterionKey, wallClockMs);
  }

  statusRef.value = `Running covering tests for criterion '${criterionKey}'...`;
  sendLog(ws, `Criterion named-test trial [${criterionKey}]: ${runner.cmd} ${runner.args.join(' ')} (cwd=${runner.cwd})\n`);

  let output = '';
  let cleanExit = false;
  let status: 'ran' | 'skipped' | 'absent' | 'could_not_run';
  try {
    output = await execFileAsync(runner.cmd, runner.args, { cwd: runner.cwd, timeoutMs: wallClockMs });
    cleanExit = true;
    status = classifyAcceptanceTestRun(output);
  } catch (err: unknown) {
    const e = err as Error & { stdout?: string; stderr?: string };
    output = [e.stdout, e.stderr, e.message].filter(Boolean).join('\n');
    // An environment/setup failure means we could NOT confidently observe the
    // run — degrade to could_not_run (mint nothing) rather than mis-reading a
    // missing runtime as a real red result.
    status = matchesEnvironmentFailureSignature(output)
      ? 'could_not_run'
      : classifyAcceptanceTestRun(output);
  }

  const couldNotRun = status === 'could_not_run';
  const ran = status === 'ran';
  const passed = acceptanceTestRunPassed(cleanExit, status);
  const skippedSummary = parseTrialSkippedSummary(output);
  sendLog(ws, `Criterion named-test trial [${criterionKey}]: status=${status} passed=${passed}\n`);

  await report({
    type: 'criterion_named_test_trial',
    criterion_key: criterionKey,
    head_sha: executedHeadSha, queued_head_sha: headSha, test_refs: testRefs,
    ran, passed, could_not_run: couldNotRun,
    ...(couldNotRun ? { could_not_run_reason: 'execution_environment_unavailable' } : {}),
    ...(skippedSummary ? { skipped_summary: skippedSummary } : {}),
  });
}
