/**
 * Criterion coverage trial handler — machine-verified `covered` acceptance tier.
 *
 * A `criterion_coverage_trial` task runs ONLY a merged task's discovered covering
 * tests as a TARGETED, bounded runner job — no Claude invocation, no PR — and
 * reports a pass/fail verdict. The backend ingestion completion handler
 * (`_handle_criterion_coverage_trial_completion`) mints a `covered` tier on GREEN
 * and records NOTHING on RED (never demotes, fail-open).
 *
 * Reuses all shared primitives from capability-proof-trial.ts
 * (buildCapabilityProofTrialRunner, isRefSetTargetable, parseTrialSkippedSummary,
 * provisionNodeDepsForTrial) so behaviour is consistent across both trial types.
 */

import type WebSocket from 'ws';
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
import {
  buildCapabilityProofTrialRunner,
  isRefSetTargetable,
  parseTrialSkippedSummary,
  provisionNodeDepsForTrial,
} from './capability-proof-trial.js';

const DEFAULT_MAX_TEST_FILES = 10;
const DEFAULT_WALL_CLOCK_MS = 900_000;

/** Result shape reported back to the backend completion handler.
 *  Field names match the contract defined in
 *  `_handle_criterion_coverage_trial_completion` / `_parse_criterion_coverage_trial_result`. */
export interface CriterionCoverageTrialResult {
  type: 'criterion_coverage_trial';
  criterion_ids: string[];
  source_task_id: string | null;
  head_sha: string | null;
  queued_head_sha: string | null;
  test_refs: string[];
  ran: boolean;
  passed: boolean;
  could_not_run: boolean;
  skipped_summary?: { total: number; skipped: number };
}

export async function handleCriterionCoverageTrial(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  _model?: string, _effort?: string,
): Promise<void> {
  setPromptSizeTelemetry(0, 0, []);

  const criterionIds = Array.isArray(payload.criterion_ids)
    ? (payload.criterion_ids as unknown[]).filter((c): c is string => typeof c === 'string' && c.trim().length > 0)
    : [];
  const sourceTaskId = typeof payload.source_task_id === 'string' ? payload.source_task_id.trim() || null : null;
  const headSha = typeof payload.head_sha === 'string' ? payload.head_sha : null;
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

  const label = sourceTaskId ?? taskId;

  const report = async (result: CriterionCoverageTrialResult): Promise<void> => {
    await persistAndPostStatus(taskId, {
      task_id: taskId,
      status: 'COMPLETED',
      logs: sanitize(JSON.stringify(result)),
      repo_url: `https://github.com/${owner}/${repo}`,
    });
  };

  if (testRefs.length === 0 || !isRefSetTargetable(testRefs)) {
    const reason = testRefs.length === 0 ? 'no test refs' : 'refs not cleanly targetable';
    sendLog(ws, `Criterion coverage trial [${label}]: ${reason} — reporting could_not_run.\n`);
    await report({
      type: 'criterion_coverage_trial',
      criterion_ids: criterionIds, source_task_id: sourceTaskId,
      head_sha: headSha, queued_head_sha: headSha, test_refs: testRefs,
      ran: false, passed: false, could_not_run: true,
    });
    return;
  }

  const baseBranch = validateBranchName(resolveBaseBranch(payload, task), 'base_branch');
  statusRef.value = `Cloning ${owner}/${repo} @ ${baseBranch} for criterion coverage trial...`;
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  await prepareWorkspaceForRole({
    workspace: WORKSPACE, repoUrl: cloneUrl, githubToken: ghToken,
    branch: baseBranch, baseBranch, depth: DEFAULT_CLONE_DEPTH,
  });

  let executedHeadSha: string | null = null;
  try {
    const resolved = await execFileAsync('git', ['rev-parse', 'HEAD'], { cwd: WORKSPACE });
    executedHeadSha = resolved.trim() || null;
  } catch (err: unknown) {
    const message = err instanceof Error ? err.message : String(err);
    sendLog(ws, `Criterion coverage trial [${label}]: could not resolve checkout SHA: ${message}\n`);
  }

  const runner = buildCapabilityProofTrialRunner(testRefs, WORKSPACE);
  if (runner === null) {
    sendLog(ws, `Criterion coverage trial [${label}]: runner unexpectedly null post-clone — reporting could_not_run.\n`);
    await report({
      type: 'criterion_coverage_trial',
      criterion_ids: criterionIds, source_task_id: sourceTaskId,
      head_sha: executedHeadSha, queued_head_sha: headSha, test_refs: testRefs,
      ran: false, passed: false, could_not_run: true,
    });
    return;
  }

  if (runner.cmd === 'npx') {
    await provisionNodeDepsForTrial(runner.cwd, ws, label, wallClockMs);
  }

  statusRef.value = `Running covering tests for criterion trial [${label}]...`;
  sendLog(ws, `Criterion coverage trial [${label}]: ${runner.cmd} ${runner.args.join(' ')} (cwd=${runner.cwd})\n`);

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
    status = matchesEnvironmentFailureSignature(output)
      ? 'could_not_run'
      : classifyAcceptanceTestRun(output);
  }

  const couldNotRun = status === 'could_not_run';
  const ran = status === 'ran';
  const passed = acceptanceTestRunPassed(cleanExit, status);
  const skippedSummary = parseTrialSkippedSummary(output);
  sendLog(ws, `Criterion coverage trial [${label}]: status=${status} passed=${passed}\n`);

  await report({
    type: 'criterion_coverage_trial',
    criterion_ids: criterionIds, source_task_id: sourceTaskId,
    head_sha: executedHeadSha, queued_head_sha: headSha, test_refs: testRefs,
    ran, passed, could_not_run: couldNotRun,
    ...(skippedSummary ? { skipped_summary: skippedSummary } : {}),
  });
}
