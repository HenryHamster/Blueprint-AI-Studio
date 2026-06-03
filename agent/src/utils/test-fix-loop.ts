/**
 * Test failure capture and baseline initialization for the fix-loop.
 *
 * Runs the project's test suite at task start to capture a baseline of
 * pre-existing test failures. The fix-loop uses this baseline to distinguish
 * regression failures (caused by the agent's changes) from pre-existing ones.
 */

import { execSync } from 'child_process';
import fs from 'fs';
import type WebSocket from 'ws';
import {
  WORKSPACE, sendLog, sendHeartbeatSync, execFileAsync, errorFingerprint, git,
} from '../index.js';
import { runClaudeCode } from '../providers/runners.js';
import {
  MAX_TEST_FIX_ATTEMPTS, LOG_TAIL_BYTES,
  TEST_RUNNER_TIMEOUT, TEST_RUNNER_DOCKER_TIMEOUT,
  TEST_BASELINE_TIMEOUT, TEST_BASELINE_DOCKER_TIMEOUT,
} from '../constants.js';
import {
  formatFixIterationHistory,
  type FixAttempt,
  READ_THE_FAILURE_MESSAGE,
  SIZE_GUARDRAIL_GUIDANCE,
  PRIOR_ATTEMPT_DIVERGENCE,
} from '../prompt.js';
import {
  type FixLoopExitReason,
  type FixLoopName,
  pushFixLoopEvent,
  fingerprintHash,
} from '../fix-loop-events.js';
import {
  type DiffLines,
  captureDiffLines, detectLoopDivergence,
} from './diff-detection.js';
import {
  type BuildConfig, type TestRunner,
  detectTestRunners, isDockerRunner,
  parseFailingTests, buildTargetedPytestRunners,
  classifyTestExecution,
} from './test-runners.js';

// Build + buffer a FixLoopEvent from one wrapper-loop iteration. Centralized
// so lintFixLoop and testFixLoop record identical-shaped events; the buffer
// drain on the next heartbeat flushes them to the backend.
function emitWrapperFixLoopEvent(opts: {
  loop: FixLoopName;
  iteration: number;
  errorMessage: string;
  exitReason: FixLoopExitReason;
  startedAt: string;
}): void {
  const fingerprint = errorFingerprint(opts.errorMessage);
  pushFixLoopEvent({
    loop: opts.loop,
    iteration: opts.iteration,
    fingerprint_hash: fingerprintHash(fingerprint),
    error_tail: opts.errorMessage.slice(-800),
    change_summary: captureFixIterationDiffStat(),
    exit_reason: opts.exitReason,
    source: 'agent_emitted',
    started_at: opts.startedAt,
    ended_at: new Date().toISOString(),
  });
}

/** Run lint checks in a loop, asking Claude to fix failures up to MAX_LINT_FIX_ATTEMPTS times. */
// Snapshot the cumulative working-tree change against task-start HEAD. Used
// by the within-task fix loops to give the next iteration's prompt visibility
// into what prior iterations actually modified — so it can pivot when the
// same approach has already been tried and failed.
function captureFixIterationDiffStat(): string {
  try {
    return execSync('git diff HEAD --stat --no-color', {
      cwd: WORKSPACE,
      encoding: 'utf-8',
      timeout: 5000,
    }).trim();
  } catch {
    return '(diff capture failed)';
  }
}

function buildFixLoopGuidanceSuffix(): string {
  return `\n\n${READ_THE_FAILURE_MESSAGE}\n${SIZE_GUARDRAIL_GUIDANCE}\n${PRIOR_ATTEMPT_DIVERGENCE}`;
}

function withRequiredTestmon(runner: TestRunner): TestRunner {
  const invokesBackendScript = [runner.cmd, ...runner.args].some(arg => arg.includes('scripts/test-backend.sh'));
  if (!invokesBackendScript || runner.args.includes('--require-testmon')) return runner;
  return {
    ...runner,
    args: [...runner.args, '--require-testmon'],
    label: `${runner.label} (testmon impact)`,
  };
}

// Minimal vitest JSON reporter shape — we only need the failed-test names.
// Full schema at https://vitest.dev/guide/reporters.html#json-reporter; these
// fields are stable across vitest 2.x/3.x.
interface VitestJsonReport {
  testResults?: Array<{
    name?: string;
    assertionResults?: Array<{
      fullName?: string;
      title?: string;
      status?: 'passed' | 'failed' | 'skipped' | 'pending' | 'todo';
    }>;
  }>;
}

/**
 * Extract failing test IDs from a vitest JSON reporter file. Returns the
 * fullName (or title) of every assertion with status="failed". Preferred
 * over stdout scanning because stdout can contain false-positive markers
 * from inside passing tests' mocked output.
 */
export function parseFailingTestsFromVitestJson(jsonText: string): Set<string> {
  const failing = new Set<string>();
  let parsed: VitestJsonReport;
  try {
    parsed = JSON.parse(jsonText) as VitestJsonReport;
  } catch {
    return failing;
  }
  for (const file of parsed.testResults ?? []) {
    for (const assertion of file.assertionResults ?? []) {
      if (assertion.status === 'failed') {
        const id = (assertion.fullName || assertion.title || '').trim();
        if (id) failing.add(id);
      }
    }
  }
  return failing;
}

function isVitestRunner(cwd: string): boolean {
  return fs.existsSync(`${cwd}/vitest.config.ts`) ||
    fs.existsSync(`${cwd}/vitest.config.js`) ||
    fs.existsSync(`${cwd}/vitest.config.mjs`);
}

/**
 * Run the test suite with a short timeout to capture which tests are already failing
 * before any code changes are made. Used to distinguish pre-existing failures from
 * newly-broken tests introduced by the agent.
 *
 * - Scope-limited to runners relevant to relatedFiles (avoids running full suite).
 * - Time-bounded by timeoutMs (default 60s) — returns null on timeout/error.
 * - Returns an empty Set if all tests pass (all future failures are new).
 * - Returns null to signal "skip baseline" — fall back to current behavior.
 */
export async function captureTestBaseline(
  ws: WebSocket | null,
  statusRef: { value: string },
  buildConfig?: BuildConfig,
  relatedFiles?: string[],
  timeoutMs = TEST_BASELINE_TIMEOUT,
): Promise<Set<string> | null> {
  let runners = detectTestRunners(buildConfig);

  if (runners.length === 0) return null;

  // Scope-limit to runners relevant to the files being changed. This avoids
  // running the full backend Docker test suite when only agent files changed.
  if (relatedFiles && relatedFiles.length > 0) {
    const touchesAgent = relatedFiles.some(f => f.startsWith('agent/'));
    const touchesBackend = relatedFiles.some(f => f.startsWith('backend/'));
    if (touchesAgent || touchesBackend) {
      const filtered = runners.filter(r => {
        if (touchesAgent && (r.label.includes('agent') || r.cwd.includes('/agent'))) return true;
        if (touchesBackend && (r.label.includes('backend') || r.label.includes('pytest') || r.cwd.includes('/backend'))) return true;
        return false;
      });
      // Only apply the filter if it produced at least one runner
      if (filtered.length > 0) runners = filtered;
    }
  }

  sendLog(ws, `Capturing test baseline for: ${runners.map(r => r.label).join(', ')}\n`);
  statusRef.value = 'Capturing test baseline...';

  const baselineFailures = new Set<string>();
  const taskId = process.env.TASK_ID || '';

  for (const runner of runners) {
    // For vitest, request the JSON reporter so we can extract failing
    // test names authoritatively — the stdout parser historically missed
    // vitest's " FAIL" format, producing a "0 pre-existing failures"
    // baseline even when the suite had many. When JSON is available it's
    // the source of truth; stdout is a fallback for runners (pytest) that
    // don't emit a comparable JSON report at this runner-invocation layer.
    const useVitestJson = runner.cmd === 'npm' && isVitestRunner(runner.cwd);
    const jsonOutputPath = useVitestJson
      ? `/tmp/vitest-baseline-${Date.now()}-${Math.random().toString(36).slice(2, 8)}.json`
      : null;
    const runArgs = jsonOutputPath
      ? [...runner.args, '--', '--reporter=default', '--reporter=json', `--outputFile=${jsonOutputPath}`]
      : runner.args;

    try {
      sendHeartbeatSync(taskId, `Baseline test run [${runner.label}]...`);
      const runnerTimeout = isDockerRunner(runner) ? TEST_BASELINE_DOCKER_TIMEOUT : timeoutMs;
      await execFileAsync(runner.cmd, runArgs, {
        cwd: runner.cwd,
        timeoutMs: runnerTimeout,
      });
      // Runner exited cleanly — no failures
    } catch (err: unknown) {
      // execFileAsync sets killed=true and code='ETIMEDOUT' when the process is
      // terminated due to timeout; ENOENT-style errors (failure to spawn) also
      // surface with code. Both are infrastructure issues — can't capture
      // baseline, proceed without it.
      const isTimeout = err instanceof Error && (
        (err as Error & { killed?: boolean }).killed === true ||
        (err as NodeJS.ErrnoException).code === 'ETIMEDOUT'
      );
      if (isTimeout) {
        sendLog(ws, `Test baseline timed out for [${runner.label}] — proceeding without baseline.\n`);
        return null;
      }
      const msg = err instanceof Error ? err.message : String(err);
      // Prefer JSON reporter output when available — authoritative and
      // format-stable across vitest 2.x/3.x.
      let parsedFromJson = false;
      if (jsonOutputPath && fs.existsSync(jsonOutputPath)) {
        try {
          const jsonText = fs.readFileSync(jsonOutputPath, 'utf-8');
          const parsed = parseFailingTestsFromVitestJson(jsonText);
          if (parsed.size > 0) {
            parsed.forEach(f => baselineFailures.add(f));
            parsedFromJson = true;
          } else if (jsonText.includes('"numFailedTests":0') || jsonText.includes('"success":true')) {
            // JSON reporter says 0 failures but the process exited non-zero.
            // That's a non-test issue (setup error, runner crash); fall
            // through to stdout parser to surface whatever broke.
            parsedFromJson = false;
          } else {
            // JSON loaded but extraction returned empty — fall back to
            // stdout so we don't silently treat a broken suite as passing.
            parsedFromJson = false;
          }
        } catch {
          // JSON file unreadable/malformed — fall through to stdout.
        }
      }
      if (!parsedFromJson) {
        const parsed = parseFailingTests(msg);
        parsed.forEach(f => baselineFailures.add(f));
        // If BOTH paths came up empty despite the process exiting
        // non-zero, something's wrong — log loudly so an operator can
        // investigate rather than silently treating it as a clean baseline.
        if (baselineFailures.size === 0) {
          sendLog(
            ws,
            `WARNING: Test baseline for [${runner.label}] exited non-zero ` +
            `but neither JSON reporter nor stdout parser extracted any ` +
            `failures. Proceeding without baseline — treat any failures ` +
            `in the fix loop as potentially pre-existing.\n`,
          );
          return null;
        }
      }
    } finally {
      // Best-effort cleanup of the baseline JSON file.
      if (jsonOutputPath) {
        try { fs.unlinkSync(jsonOutputPath); } catch { /* ignore */ }
      }
    }
  }

  if (baselineFailures.size > 0) {
    sendLog(ws, `Baseline: ${baselineFailures.size} pre-existing failure(s) found — these will be excluded from test fix analysis.\n`);
  } else {
    sendLog(ws, 'Baseline captured: all tests passing before changes.\n');
  }

  return baselineFailures;
}

/**
 * Detect the project's test runner, run tests, and retry via Claude on failure.
 * When baselineFailures is provided, distinguishes pre-existing failures from
 * newly-broken tests and only counts new failures toward task FAILED status.
 * Returns whether tests passed and whether failures were detected (for commit note).
 */

/** Extracts plan-alignment-relevant sections ('Files to change', 'Interfaces to preserve',
 * 'Key dependencies', 'Callers to verify after implementation', 'Tests at risk of breaking')
 * from analysis context. Heading match is fuzzy (lowercased, key-term based) so minor wording
 * variations from the analysis agent (e.g. 'Files to modify') still produce complete section data. */
export function condensedAnalysisSections(analysisContext: string): string {
  // Split at section boundaries (lines starting a new **[A-Z] heading)
  const sectionBreak = /(?=\n\*\*[A-Z])/;
  const sections = analysisContext.split(sectionBreak);
  const isWantedHeading = (section: string): boolean => {
    const headingMatch = section.match(/^\s*\*\*([^*\n]+)\*\*/);
    if (!headingMatch) return false;
    const h = headingMatch[1].toLowerCase();
    return (
      (h.includes('files') && (h.includes('change') || h.includes('modify') || h.includes('verify')))
      || (h.includes('interfaces') && h.includes('preserve'))
      || h.includes('dependencies')
      || (h.includes('callers') && h.includes('verify'))
      || (h.includes('tests') && h.includes('risk'))
    );
  };
  const wanted = sections.filter(isWantedHeading);
  if (wanted.length === 0) return analysisContext.slice(0, 800).trim();
  return wanted.map(s => s.trim()).join('\n\n');
}

export interface TestFixLoopOptions {
  targetPytestSelectors?: string[];
  runImpactAfterTargeted?: boolean;
  requireTestmonForImpact?: boolean;
  maxFixAttempts?: number;
  phaseLabel?: string;
  runnersOverride?: TestRunner[];
}

export async function testFixLoop(
  ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string, buildConfig?: BuildConfig,
  baselineFailures?: Set<string>,
  relatedFiles?: string[],
  analysisContext?: string,
  options: TestFixLoopOptions = {},
): Promise<{ passed: boolean; testFailuresDetected: boolean; setupFailed?: boolean; failureSummary?: string; noTestsExecuted?: boolean }> {
  let runners = options.runnersOverride ?? detectTestRunners(buildConfig);
  const targetPytestSelectors = Array.from(new Set(options.targetPytestSelectors ?? []));

  // Scope-limit to runners relevant to the files being changed. Same logic as
  // captureTestBaseline — avoids running the full backend Docker test suite
  // when only agent files changed.
  if (!options.runnersOverride && targetPytestSelectors.length === 0 && relatedFiles && relatedFiles.length > 0) {
    const touchesAgent = relatedFiles.some(f => f.startsWith('agent/'));
    const touchesBackend = relatedFiles.some(f => f.startsWith('backend/'));
    if (touchesAgent || touchesBackend) {
      const filtered = runners.filter(r => {
        if (touchesAgent && (r.label.includes('agent') || r.cwd.includes('/agent'))) return true;
        if (touchesBackend && (r.label.includes('backend') || r.label.includes('pytest') || r.cwd.includes('/backend'))) return true;
        return false;
      });
      if (filtered.length > 0) runners = filtered;
    }
  }

  if (runners.length === 0) {
    sendLog(ws, 'No test runner detected — skipping test step.\n');
    return { passed: true, testFailuresDetected: false };
  }

  sendLog(ws, `Detected test runner(s): ${runners.map(r => r.label).join(', ')}\n`);

  if (!options.runnersOverride && targetPytestSelectors.length > 0) {
    const targetedRunners = buildTargetedPytestRunners(runners, targetPytestSelectors);
    if (targetedRunners.length > 0) {
      sendLog(ws, `Running targeted CI failure tests first: ${targetPytestSelectors.slice(0, 8).join(', ')}${targetPytestSelectors.length > 8 ? ` (+${targetPytestSelectors.length - 8} more)` : ''}\n`);
      const targetedResult = await testFixLoop(
        ws, statusRef, model, effort, buildConfig,
        undefined, relatedFiles, analysisContext,
        {
          ...options,
          targetPytestSelectors: [],
          runImpactAfterTargeted: false,
          phaseLabel: 'Targeted CI failure verification',
          runnersOverride: targetedRunners,
        },
      );
      if (!targetedResult.passed || !options.runImpactAfterTargeted) {
        return targetedResult;
      }
      sendLog(ws, 'Targeted CI failure tests passed; running impact coverage with the detected test runner(s).\n');
      const impactRunners = options.requireTestmonForImpact
        ? runners.map(runner => withRequiredTestmon(runner))
        : runners;
      return testFixLoop(
        ws, statusRef, model, effort, buildConfig,
        baselineFailures, relatedFiles, analysisContext,
        {
          ...options,
          targetPytestSelectors: [],
          runImpactAfterTargeted: false,
          phaseLabel: 'Impact coverage',
          runnersOverride: impactRunners,
        },
      );
    }
    sendLog(ws, 'No pytest-compatible runner found for targeted CI failures — falling back to detected test runner(s).\n');
  }

  const fingerprintHistory: string[] = [];
  const errorTextHistory: string[] = [];
  const prevAttempts: FixAttempt[] = [];
  let prevDiff: DiffLines | null = null;
  let currDiff: DiffLines | null = null;
  const maxFixAttempts = Math.max(0, options.maxFixAttempts ?? MAX_TEST_FIX_ATTEMPTS);
  const totalAttempts = maxFixAttempts + 1;
  const phasePrefix = options.phaseLabel ? `${options.phaseLabel}: ` : '';
  for (let attempt = 0; attempt <= maxFixAttempts; attempt++) {
    const iterStartedAt = new Date().toISOString();
    statusRef.value = attempt === 0 ? `${phasePrefix}Running test suite...` : `${phasePrefix}Re-running tests (attempt ${attempt + 1}/${totalAttempts})...`;
    sendLog(ws, `${phasePrefix}Running tests (attempt ${attempt + 1}/${totalAttempts})...\n`);

    // structuredIds is populated for vitest runners from the JSON reporter
    // output file. When present, baseline comparison uses it as the source
    // of truth for WHICH tests failed, bypassing stdout pattern matching
    // (which mis-classifies mocked failure output inside passing tests).
    const failures: Array<{ label: string; message: string; structuredIds?: Set<string> }> = [];
    // Track setup errors (dep install, spawn timeout) separately from test
    // failures. If NO runner actually gets to execute tests, we can't
    // claim tests passed — the result must propagate as a setup failure
    // so the caller doesn't push unverified code.
    const setupErrors: Array<{ label: string; message: string }> = [];
    let ranAnyRunner = false;
    // Track whether ANY runner's output indicates a real test actually
    // executed (vs. a placeholder command or a zero-test collection). Used to
    // distinguish "a suite ran and produced a verdict" from "no real tests
    // executed" so the no-CI merge gate can route the latter to the documented
    // proceed-without-checks fallback instead of a hard fail / vacuous pass.
    let anyRealTestExecuted = false;
    const taskId = process.env.TASK_ID || '';
    for (const runner of runners) {
      // Setup step: ensure node_modules exist for npm runners. A failure
      // HERE is NOT a test failure — it's dep-install breakage (registry
      // flake, lockfile drift, disk, postinstall crash). The Claude fix
      // loop can't meaningfully repair these; skip this runner but record
      // the error so we can report "tests couldn't run" when every runner
      // fails setup.
      if (runner.cmd === 'npm' && !fs.existsSync(`${runner.cwd}/node_modules`)) {
        sendLog(ws, `Installing dependencies for ${runner.label}...\n`);
        try {
          await execFileAsync('npm', ['ci'], { cwd: runner.cwd, timeoutMs: TEST_RUNNER_TIMEOUT });
        } catch (installErr) {
          const msg = installErr instanceof Error ? installErr.message : String(installErr);
          sendLog(
            ws,
            `WARNING: Dependency install failed for [${runner.label}] — ` +
            `setup error, not a fixable test failure. Skipping runner.\n` +
            `${msg.slice(0, 500)}\n`,
          );
          setupErrors.push({ label: runner.label, message: `npm ci failed: ${msg}` });
          continue;
        }
      }
      // For vitest runners, request the JSON reporter alongside default
      // (human-readable) output — the JSON file is the authoritative source
      // of failed-test IDs, stdout is kept for operator diagnosis.
      const useVitestJson = runner.cmd === 'npm' && isVitestRunner(runner.cwd);
      const jsonOutputPath = useVitestJson
        ? `/tmp/vitest-${Date.now()}-${Math.random().toString(36).slice(2, 8)}.json`
        : null;
      const runArgs = jsonOutputPath
        ? [...runner.args, '--', '--reporter=default', '--reporter=json', `--outputFile=${jsonOutputPath}`]
        : runner.args;

      try {
        sendHeartbeatSync(taskId, `Running tests [${runner.label}]...`);
        const runnerTimeout = isDockerRunner(runner) ? TEST_RUNNER_DOCKER_TIMEOUT : TEST_RUNNER_TIMEOUT;
        const passOutput = await execFileAsync(runner.cmd, runArgs, { cwd: runner.cwd, timeoutMs: runnerTimeout });
        ranAnyRunner = true;
        // A clean exit only counts as a real verdict if real tests ran. An
        // empty/zero-test suite that exits 0 must not be recorded as a
        // verified pass.
        if (classifyTestExecution(passOutput) === 'ran') anyRealTestExecuted = true;
        sendLog(ws, `Tests passed [${runner.label}].\n`);
      } catch (testErr: unknown) {
        // If the runner timed out (killed by the process-group SIGTERM/SIGKILL
        // chain in execFileAsync) or the OS could not spawn the process
        // (ETIMEDOUT), treat as a setup error — a Docker build timeout or
        // spawn failure is not a fixable code failure, but we also can't
        // claim tests passed.
        const isRunnerTimeout = testErr instanceof Error && (
          (testErr as Error & { killed?: boolean }).killed === true ||
          (testErr as NodeJS.ErrnoException).code === 'ETIMEDOUT'
        );
        if (isRunnerTimeout) {
          const msg = testErr instanceof Error ? testErr.message : String(testErr);
          sendLog(ws, `WARNING: Test runner [${runner.label}] timed out — skipping.\n`);
          setupErrors.push({ label: runner.label, message: `runner timeout: ${msg}` });
          continue;
        }
        // Non-timeout non-zero exit = the runner actually ran the suite
        // and at least one test failed. Record the failure.
        ranAnyRunner = true;
        const msg = testErr instanceof Error ? testErr.message : String(testErr);
        // A non-zero exit from a placeholder command (e.g. the default `npm`
        // "no test specified" script) is NOT a real test failure. Only count
        // this as a real test executing when the output isn't a no-tests
        // signature.
        if (classifyTestExecution(msg) === 'ran') anyRealTestExecuted = true;
        let structuredIds: Set<string> | undefined;
        if (jsonOutputPath && fs.existsSync(jsonOutputPath)) {
          try {
            const jsonText = fs.readFileSync(jsonOutputPath, 'utf-8');
            structuredIds = parseFailingTestsFromVitestJson(jsonText);
          } catch {
            // Fall back to stdout scanning via parseFailingTests downstream.
          }
        }
        failures.push({ label: runner.label, message: msg, structuredIds });
      } finally {
        // Don't leave per-run temp files behind (cheap, but they accumulate).
        if (jsonOutputPath) {
          try { fs.unlinkSync(jsonOutputPath); } catch { /* already gone */ }
        }
      }
    }

    const reportableFailures = anyRealTestExecuted
      ? failures.filter(failure => classifyTestExecution(failure.message) === 'ran')
      : failures;

    if (reportableFailures.length === 0) {
      // "No failures" is only a pass if we actually ran at least one
      // runner. If every runner hit setup errors (npm ci / spawn
      // timeout), tests didn't run — we can't claim they passed, and
      // there's nothing for Claude to fix. Surface as a distinct
      // setupFailed state so the caller fails the task with an infra
      // category instead of merging unverified code.
      if (!ranAnyRunner && setupErrors.length > 0) {
        const summary = setupErrors
          .map(e => `[${e.label}] ${e.message}`)
          .join('\n\n')
          .slice(0, 2000);
        sendLog(ws, `Tests could not run — setup failed for all runners. Not invoking fix loop.\n${summary}\n`);
        return { passed: false, testFailuresDetected: false, setupFailed: true, failureSummary: summary };
      }
      emitWrapperFixLoopEvent({
        loop: 'test', iteration: attempt + 1, errorMessage: '',
        exitReason: 'passed', startedAt: iterStartedAt,
      });
      sendLog(ws, 'All tests passed.\n');
      // noTestsExecuted: we ran at least one runner but none of them showed a
      // real test executing — the "pass" is vacuous (empty/zero-test suite).
      return { passed: true, testFailuresDetected: false, noTestsExecuted: ranAnyRunner && !anyRealTestExecuted };
    }

    // Serialize each failure for operator-facing output. This same format
    // fed the old pattern matcher; it's still used as a fallback when we
    // don't have structured IDs (non-vitest runners).
    const renderFailure = (f: { label: string; message: string }) => `[${f.label}]:\n${f.message}`;

    // When baseline is available, separate new failures from pre-existing ones.
    // A failure block is pre-existing when all parsed test IDs within it appear
    // in the baseline. Unparseable failures (size=0) are treated as new (safe default).
    let fixableFailures: Array<{ label: string; message: string; structuredIds?: Set<string> }> = reportableFailures;
    let preExistingNote = '';

    if (baselineFailures) {
      const newFailures: typeof failures = [];
      const preExistingFailures: typeof failures = [];

      for (const failure of reportableFailures) {
        // Structured IDs (from vitest JSON reporter) win over stdout parsing.
        // Fall back to parseFailingTests only when JSON wasn't available.
        const failureIds = failure.structuredIds ?? parseFailingTests(failure.message);
        const isAllPreExisting = failureIds.size > 0 &&
          Array.from(failureIds).every(id => baselineFailures.has(id));
        if (isAllPreExisting) {
          preExistingFailures.push(failure);
        } else {
          newFailures.push(failure);
        }
      }

      if (newFailures.length === 0) {
        // Every detected failure existed before our changes — task is not responsible
        sendLog(ws, `All ${preExistingFailures.length} test failure(s) are pre-existing and were not caused by this task.\n`);
        return { passed: true, testFailuresDetected: false };
      }

      fixableFailures = newFailures;
      if (preExistingFailures.length > 0) {
        preExistingNote = `\n\nNote: The following test failures existed BEFORE your changes — do not attempt to fix these:\n${preExistingFailures.map(renderFailure).join('\n\n').slice(0, 1500)}`;
      }
    }

    const combinedFailures = fixableFailures.map(renderFailure).join('\n\n');
    const fingerprint = errorFingerprint(combinedFailures);
    // Early exit: byte-identical consecutive failure (existing behaviour).
    if (fingerprintHistory.length > 0 && fingerprint === fingerprintHistory[fingerprintHistory.length - 1]) {
      emitWrapperFixLoopEvent({
        loop: 'test', iteration: attempt + 1, errorMessage: combinedFailures,
        exitReason: 'early_unchanged', startedAt: iterStartedAt,
      });
      sendLog(ws, `Test failures unchanged from previous attempt — exiting early (persistent, unfixable by Claude).\n`);
      const failureSummary = combinedFailures.slice(-8000);
      return { passed: false, testFailuresDetected: true, failureSummary };
    }
    fingerprintHistory.push(fingerprint);
    errorTextHistory.push(combinedFailures);
    // Divergence detection: oscillation, Jaccard-similar errors, diff revert.
    const divergence = detectLoopDivergence(fingerprintHistory, errorTextHistory, prevDiff, currDiff);
    if (divergence !== null) {
      emitWrapperFixLoopEvent({
        loop: 'test', iteration: attempt + 1, errorMessage: combinedFailures,
        exitReason: divergence, startedAt: iterStartedAt,
      });
      sendLog(ws, `Test fix loop divergence detected (${divergence}) — abandoning to avoid wasted iterations.\n`);
      const failureSummary = combinedFailures.slice(-8000);
      return { passed: false, testFailuresDetected: true, failureSummary };
    }
    // On retry (attempt >= 1), record what the previous Claude call
    // actually changed + the test failures that persisted, so the next
    // iteration's prompt can pivot if the same approach already failed.
    if (attempt >= 1) {
      prevAttempts.push({
        attemptNumber: attempt,
        diffStat: captureFixIterationDiffStat(),
        errorTail: combinedFailures.slice(-800),
      });
    }
    if (attempt < maxFixAttempts) {
      emitWrapperFixLoopEvent({
        loop: 'test', iteration: attempt + 1, errorMessage: combinedFailures,
        exitReason: 'in_progress', startedAt: iterStartedAt,
      });
      const preExistingHint = preExistingNote ? ' (pre-existing failures excluded)' : '';
      sendLog(ws, `${phasePrefix}Tests failed (attempt ${attempt + 1}/${totalAttempts})${preExistingHint}:\n${combinedFailures.slice(-LOG_TAIL_BYTES)}\nClaude will attempt to fix...\n`);
      const analysisSections = analysisContext ? condensedAnalysisSections(analysisContext) : '';
      const analysisPrefix = analysisSections ? `## Module Analysis\n\n${analysisSections}\n\n` : '';
      // Broader, investigation-first prompt. The previous "These tests
      // broke because of your changes" framing made Claude presume
      // self-caused regressions and miss pre-existing failures on main
      // (missing mock exports, stale fixtures, outdated imports) that
      // are just as valid to fix. Give it agency to consider either.
      //
      // The explicit "pre-existing bugs are fair game" language is
      // load-bearing: Claude Code's default disposition is to leave
      // pre-existing issues alone unless told otherwise. This task says
      // otherwise.
      const guidance = (
        `Tests are failing. Investigate and fix them.\n\n` +
        `Scope note (important): Claude Code's default is to avoid ` +
        `touching "unrelated" pre-existing bugs. That default DOES NOT ` +
        `apply here. On this task, pre-existing failures on main are ` +
        `fair game to fix. Edit test mocks, fixtures, imports, or ` +
        `implementation code wherever the root cause actually lives.\n\n` +
        `Causes to consider:\n` +
        `- Code your changes introduced (fix the code, or the test if ` +
        `it asserts the wrong thing).\n` +
        `- Pre-existing issues on main nobody has fixed yet: missing ` +
        `mock exports (e.g. "No 'foo' export is defined on the mock" ` +
        `→ add 'foo' to the mock), stale fixtures, outdated imports, ` +
        `recently-renamed functions the tests still reference.\n` +
        `- Flakiness or environmental issues (note them; don't chase).\n\n` +
        `Do NOT silence, skip, or weaken assertions to make tests pass. ` +
        `Do NOT revert your task's intent unless the test was correctly ` +
        `catching a real bug.`
      );
      // Slice from the tail: test output puts setup/boilerplate at the
      // head and the actual failure line at the end. 5000 chars is
      // enough to carry a diagnostic traceback or a handful of failed
      // assertions without drowning the agent in noise.
      const historySection = prevAttempts.length > 0 ? `${formatFixIterationHistory(prevAttempts)}\n\n` : '';
      const guidanceSuffix = attempt >= 1 ? buildFixLoopGuidanceSuffix() : '';
      const fixPrompt = baselineFailures
        ? `${historySection}${analysisPrefix}${guidance}\n\nFailures to fix:\n\n${combinedFailures.slice(-LOG_TAIL_BYTES)}${preExistingNote.slice(-1500)}${guidanceSuffix}`
        : `${historySection}${analysisPrefix}${guidance}\n\nAll test failures (baseline skipped — pre-existing failures are also fair game on this task):\n\n${combinedFailures.slice(-LOG_TAIL_BYTES)}${guidanceSuffix}`;
      await runClaudeCode(fixPrompt, ws, statusRef, model, effort);
      git(['add', '-A']);
      prevDiff = currDiff;
      currDiff = captureDiffLines();
    } else {
      emitWrapperFixLoopEvent({
        loop: 'test', iteration: attempt + 1, errorMessage: combinedFailures,
        exitReason: 'max_attempts', startedAt: iterStartedAt,
      });
      const exhaustedMessage = maxFixAttempts === 0
        ? `${phasePrefix}Tests failed during focused verification.\n`
        : `WARNING: ${phasePrefix}Tests still failing after ${maxFixAttempts} fix attempts. Proceeding with commit.\n`;
      sendLog(ws, exhaustedMessage);
      const failureSummary = combinedFailures.slice(-8000);
      // noTestsExecuted: the only "failures" came from placeholder commands
      // (e.g. `npm` "no test specified") — no real test executed, so the
      // focused-verification fail should route to the proceed-without-checks
      // fallback rather than blocking the merge.
      return { passed: false, testFailuresDetected: true, failureSummary, noTestsExecuted: ranAnyRunner && !anyRealTestExecuted };
    }
  }
  return { passed: false, testFailuresDetected: true };
}
