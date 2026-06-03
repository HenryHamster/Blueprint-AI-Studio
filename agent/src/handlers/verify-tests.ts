import fs from 'fs';
import type WebSocket from 'ws';
import {
  WORKSPACE, strVal, sanitize, sendLog, validateBranchName,
  persistAndPostStatus, resolveBaseBranch,
} from '../index.js';
import { prepareWorkspaceForRole } from '../workspacePrep.js';
import { DEFAULT_CLONE_DEPTH } from '../constants.js';
import { resolveBuildConfig } from '../build-config.js';
import { detectTestRunners } from '../utils/test-runners.js';
import { testFixLoop } from '../utils/test-fix-loop.js';

// Cap the failing-test output we ship in the verdict. Mirrors the smoketest
// error-log tail size — enough to diagnose a failure without bloating the
// artifact (which is also subject to the backend payload-size budget).
const VERIFY_TESTS_OUTPUT_LIMIT = 16_000;

interface VerifyTestsResult {
  detected: boolean;
  passed: boolean;
  tests: Array<{ name: string; status: string; message: string }>;
  output: string;
}

// ── Handler: Verify Tests (run the repo's detected suite on a PR head) ───────
// Repo-side half of the no-CI merge gate. Clones the PR head branch, reuses the
// EXISTING test-runner detection (detectTestRunners) and execution+verdict
// logic (testFixLoop, run once with no fix loop), and posts a structured
// MERGE_TEST_RESULT artifact the backend merge gate can consult.
//
// Idempotent + self-contained: when no test command is detectable (or setup
// could not run the suite) the handler still SUCCEEDS with detected=false so
// the backend falls back to its current behavior rather than blocking the
// merge — absent-tests repos must still progress. A failing suite is a
// successful verification with a negative verdict (detected=true, passed=false),
// not a handler failure.
export async function handleVerifyTests(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  // The PR head branch is what we verify; base_branch is the clone fallback /
  // merge target like every other pipeline task.
  const headBranch = validateBranchName(strVal(payload.branch), 'branch');
  const baseBranch = validateBranchName(resolveBaseBranch(payload, task), 'base_branch');

  statusRef.value = `Cloning ${owner}/${repo} @ ${headBranch}...`;
  sendLog(ws, `Cloning ${owner}/${repo} @ ${headBranch} for test verification...\n`);
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  await prepareWorkspaceForRole({
    workspace: WORKSPACE, repoUrl: cloneUrl, githubToken: ghToken,
    branch: headBranch, baseBranch, depth: DEFAULT_CLONE_DEPTH,
  });

  const buildConfig = resolveBuildConfig(task);
  const runners = detectTestRunners(buildConfig);

  let result: VerifyTestsResult;
  if (runners.length === 0) {
    // No resolvable test_cmd — the backend will fall back. This is a SUCCESS:
    // an absent-tests repo must still progress through the merge gate.
    sendLog(ws, 'No test runner detected — reporting detected=false (backend will fall back).\n');
    result = {
      detected: false,
      passed: false,
      tests: [],
      output: 'No test runner detected for this repository; merge gate has no suite to run.',
    };
  } else {
    sendLog(ws, `Detected test runner(s): ${runners.map(r => r.label).join(', ')}\n`);
    statusRef.value = 'Running detected test suite...';
    // Single no-fix run: maxFixAttempts 0 means testFixLoop runs each runner
    // once and returns a verdict without ever invoking Claude to repair code.
    const loop = await testFixLoop(
      ws, statusRef, model, effort, buildConfig,
      undefined, undefined, undefined, { maxFixAttempts: 0 },
    );

    if (loop.setupFailed) {
      // Tests couldn't actually run (dep install / runner spawn failed). We
      // could not verify the change, so report detected=false and let the
      // backend fall back rather than blocking a legitimate merge on infra
      // noise.
      sendLog(ws, 'Test suite could not run (setup failed) — reporting detected=false.\n');
      result = {
        detected: false,
        passed: false,
        tests: [],
        output: (loop.failureSummary ?? 'Test runner setup failed.').slice(-VERIFY_TESTS_OUTPUT_LIMIT),
      };
    } else if (loop.noTestsExecuted) {
      // A runner ran but no real tests executed — the detected command was a
      // placeholder (e.g. the default `npm` "no test specified" script) or the
      // suite collected zero tests. This is effectively a no-detectable-tests
      // repo: report detected=false so the backend routes it to the documented
      // proceed-without-checks fallback rather than recording a hard failure
      // (placeholder exits non-zero) or a vacuous pass (empty suite exits zero).
      sendLog(ws, 'Detected suite ran no real tests (placeholder/empty) — reporting detected=false (backend will fall back).\n');
      result = {
        detected: false,
        passed: false,
        tests: [],
        output: 'Detected test command ran no real tests (placeholder or empty suite) — no real tests executed; falling back to proceed-without-checks.',
      };
    } else {
      const passed = loop.passed;
      const message = passed ? 'suite passed' : 'test failures detected';
      result = {
        detected: true,
        passed,
        tests: runners.map(r => ({
          name: r.label,
          status: passed ? 'pass' : 'fail',
          message: passed ? '' : message,
        })),
        output: passed ? '' : (loop.failureSummary ?? '').slice(-VERIFY_TESTS_OUTPUT_LIMIT),
      };
    }
  }

  // Write the result file (parity with smoketest's /tmp result file — useful
  // for operator inspection); the authoritative copy is the artifact logs.
  const resultPath = `/tmp/verify_tests_result_${taskId}.json`;
  try {
    fs.writeFileSync(resultPath, JSON.stringify(result, null, 2));
  } catch (e) {
    sendLog(ws, `[WARNING] Could not write ${resultPath}: ${e instanceof Error ? e.message : String(e)}\n`);
  }

  const summary = result.detected
    ? `Test verification ${result.passed ? 'PASSED' : 'FAILED'} (${runners.length} runner(s))`
    : 'Test verification skipped (no detectable suite)';
  sendLog(ws, `\n${summary}\n`);

  // Always COMPLETED: the handler's job is to produce a verdict, and it did.
  // A negative verdict (passed=false) lives in the artifact, not the task
  // status — the backend merge gate reads the MERGE_TEST_RESULT artifact.
  await persistAndPostStatus(taskId, {
    task_id: taskId,
    status: 'COMPLETED',
    artifact_type: 'MERGE_TEST_RESULT',
    logs: sanitize(JSON.stringify(result)),
    repo_url: `https://github.com/${owner}/${repo}`,
  });
}
