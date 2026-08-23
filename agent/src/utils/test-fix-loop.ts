/**
 * Test failure capture and baseline initialization for the fix-loop.
 *
 * Runs the project's test suite at task start to capture a baseline of
 * pre-existing test failures. The fix-loop uses this baseline to distinguish
 * regression failures (caused by the agent's changes) from pre-existing ones.
 */

import { execFileSync, execSync } from 'child_process';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import type WebSocket from 'ws';
import {
  WORKSPACE, sendLog, sendHeartbeatSync, execFileAsync, errorFingerprint, git, apiPost,
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
  detectTestRunners, isDockerRunner, describeRunnerDirectory,
  buildTargetedPytestRunners,
  classifySetupFailure, classifyTestExecution, matchesEnvironmentFailureSignature,
  extractTestCoverage, extractLineCoverage, extractChangedLineCoverage, runnerFromCommand,
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

type BuildDiscoveryEntry = {
  path: string;
  type: string;
  test_cmd: string;
  install_cmd?: string;
  executed_successfully: true;
};

function toBuildDiscoveryEntry(runner: TestRunner): BuildDiscoveryEntry {
  const descriptor = describeRunnerDirectory(runner);
  const entry: BuildDiscoveryEntry = {
    path: descriptor.path || '.',
    type: descriptor.type,
    test_cmd: descriptor.test_cmd,
    executed_successfully: true,
  };
  if (runner.installCmd && runner.installCmd.trim()) {
    entry.install_cmd = runner.installCmd.trim();
  }
  return entry;
}

async function postBuildDiscoveryReport(
  ws: WebSocket | null,
  buildConfig: BuildConfig | undefined,
  runners: TestRunner[],
): Promise<void> {
  if ((buildConfig?.directories ?? []).length > 0) return;
  if (runners.length === 0) return;

  const discovered_directories: BuildDiscoveryEntry[] = [];
  const seen = new Set<string>();
  for (const runner of runners) {
    const entry = toBuildDiscoveryEntry(runner);
    const key = [
      entry.path,
      entry.type,
      entry.test_cmd,
      entry.install_cmd ?? '',
    ].join('\u0000');
    if (seen.has(key)) continue;
    seen.add(key);
    discovered_directories.push(entry);
    if (discovered_directories.length >= 50) break;
  }
  if (discovered_directories.length === 0) return;

  try {
    // We only reach here after actually executing runnable test suites from a
    // full clone, so the repo is authoritatively NOT empty. Report that live
    // verdict so the backend can re-detect when the durable repo_is_empty signal
    // disagrees (e.g. a greenfield repo that has since gained real source).
    await apiPost('/api/agent/build-config-discovery', {
      discovered_directories,
      observed_repo_is_empty: false,
    });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    sendLog(ws, `WARNING: build-config discovery report failed: ${msg.slice(0, 500)}\n`);
  }
}

export function matchesTransientSetupFailure(message: string): boolean {
  const lower = message.toLowerCase();
  const transportSignals = [
    'connection refused',
    'spurious network error',
    'temporary failure in name resolution',
    'network is unreachable',
    'could not resolve host',
    'registry timeout',
    'unreachable',
    'timed out',
    'timeout',
    'eai_again',
    'econnreset',
    'connection reset',
  ];
  if (transportSignals.some(signature => lower.includes(signature))) return true;
  return false;
}

function isInfrastructureSetupFailure(
  err: unknown,
  message: string,
  { includeTransientSetupFailures = false }: { includeTransientSetupFailures?: boolean } = {},
): boolean {
  const code = err instanceof Error ? (err as NodeJS.ErrnoException).code : undefined;
  if (code === 'ENOENT') return true;
  if (classifySetupFailure(message) || matchesEnvironmentFailureSignature(message)) return true;
  if (includeTransientSetupFailures && matchesTransientSetupFailure(message)) return true;
  return false;
}

/**
 * Detect the stable signature of a module size-guardrail breach in test output.
 *
 * A size breach is the one failure class with a single canonical, policy-defined
 * remediation (bump the cap via the shared checker) — never a code refactor and
 * never a weakened test. Detecting it deterministically lets the fix loop apply
 * that remediation directly instead of handing the decision to LLM judgment,
 * which has historically prescribed a code-extraction refactor against the
 * explicit instruction (losing a full strategic cycle on a documented run).
 *
 * Three structured forms are recognized — two cap-breach forms and the
 * complementary missing-cap (coverage meta-test) form:
 *  - the test_invariants breach assertion: "<path> has <N> lines (limit: <M>)."
 *  - the fast local checker breach line: "[breach] <section>/<rel>: <N> lines (cap: <M>)"
 *  - the missing-cap coverage failure: "<path> has <N> lines (threshold: <M>)" /
 *    "NO cap entry" (test_*_no_uncapped_large_file and the checker's [uncapped]
 *    line). Its single remediation is to ADD a cap entry — `check_size_limits.py
 *    --fix` now does this deterministically, same as a breach bump.
 * Any form must be anchored by a size-guardrail token so an unrelated assertion
 * that merely mentions "lines (limit: N)" cannot trigger a cap bump.
 */
export function matchesSizeBreachSignature(text: string): boolean {
  if (!text) return false;
  const assertionForm = /\bhas\s+\d+\s+lines\s+\(limit:\s*\d+\)/i.test(text);
  const checkerForm = /\blines\s+\(cap:\s*\d+\)/i.test(text);
  const thresholdForm = /\blines\s+\(threshold:\s*\d+\)/i.test(text);
  const missingCapForm = /\bNO cap entry\b/i.test(text);
  if (!assertionForm && !checkerForm && !thresholdForm && !missingCapForm) return false;
  return (
    /module_size_limits\.json/i.test(text) ||
    /size guardrail/i.test(text) ||
    /size breach/i.test(text)
  );
}

/** The size-limit sections `check_size_limits.py --repair-target` accepts. */
const SIZE_SECTION = 'backend|agent|frontend|testing|scripts';

/** `[breach] agent/prompt.ts: …` / `[uncapped] agent/prompts/huge.ts: …` */
const EXPLICIT_SIZE_TARGET_RE = new RegExp(
  String.raw`\[(?:breach|uncapped)\]\s*((?:${SIZE_SECTION})\/[A-Za-z0-9._/-]+)`,
  'gi',
);

/**
 * The aggregated missing-cap header emitted by
 * test_invariants._missing_cap_message and agent/src/moduleSize.contract.test.ts:
 * "N source file(s) exceed 1500 lines with NO cap entry under '<section>' in".
 * The section is bound in the HEADER — it never reappears after the offenders —
 * so the header must be parsed before the per-offender lines below it.
 */
const MISSING_CAP_HEADER_RE = new RegExp(
  String.raw`NO cap entry under\s+['"](${SIZE_SECTION})['"]`,
  'gi',
);

/** One offender line of an aggregated block: "  - prompts/huge.ts has 1700 lines (threshold: 1500)" */
const MISSING_CAP_OFFENDER_RE =
  /^[ \t]*-[ \t]+([A-Za-z0-9][A-Za-z0-9._/-]*)\s+has\s+\d+\s+lines\s+\(threshold:\s*\d+\)/gm;

/** Single-file invariant form: "<rel> has <N> lines (limit: <M>)" … "under '<section>'". */
const INVARIANT_SIZE_TARGET_RE = new RegExp(
  String.raw`(?:^|\s)([A-Za-z0-9][A-Za-z0-9._/-]*)\s+has\s+\d+\s+lines\s+` +
  String.raw`\((?:limit|threshold):\s*\d+\)[\s\S]*?under\s+['"](${SIZE_SECTION})['"]`,
  'gi',
);

/**
 * Consume every aggregated missing-cap block in `detail`, returning one target
 * per offender line plus the residual text with those blocks blanked out. The
 * residual keeps the single-file invariant scan from re-attributing an
 * aggregated block's offenders to a different section named further down a
 * concatenated runner detail.
 */
function consumeAggregatedMissingCapTargets(
  detail: string,
): { targets: string[]; residual: string } {
  const targets: string[] = [];
  const headers = Array.from(detail.matchAll(MISSING_CAP_HEADER_RE));
  if (headers.length === 0) return { targets, residual: detail };

  let residual = '';
  let cursor = 0;
  headers.forEach((header, index) => {
    const start = header.index;
    const blockStart = start + header[0].length;
    const blockEnd = headers[index + 1]?.index ?? detail.length;
    const block = detail.slice(blockStart, blockEnd);
    for (const offender of block.matchAll(MISSING_CAP_OFFENDER_RE)) {
      targets.push(`${header[1].toLowerCase()}/${offender[1]}`);
    }
    residual += detail.slice(cursor, start);
    // Blank the consumed block, preserving newlines so the residual's remaining
    // line-anchored and `(?:^|\s)` matches keep their original boundaries.
    residual += detail.slice(start, blockEnd).replace(/[^\n]/g, ' ');
    cursor = blockEnd;
  });
  residual += detail.slice(cursor);
  return { targets, residual };
}

/**
 * Return checker repair selectors only for canonically formatted size findings.
 * A structured comparator has already established that these details are new;
 * malformed size diagnostics deliberately produce null so automatic mutation
 * fails closed rather than guessing a file from raw runner output.
 *
 * Every offender in a detail is collected, not just the first: the missing-cap
 * form is aggregated (one test failure listing every uncapped file), so
 * repairing one file per iteration would exhaust maxFixAttempts on a multi-file
 * breach.
 */
export function extractSizeRepairTargets(details: Iterable<string>): string[] | null {
  const targets = new Set<string>();
  for (const detail of details) {
    if (!matchesSizeBreachSignature(detail)) continue;
    const found: string[] = [];
    for (const match of detail.matchAll(EXPLICIT_SIZE_TARGET_RE)) found.push(match[1]);
    const aggregated = consumeAggregatedMissingCapTargets(detail);
    found.push(...aggregated.targets);
    for (const match of aggregated.residual.matchAll(INVARIANT_SIZE_TARGET_RE)) {
      found.push(`${match[2].toLowerCase()}/${match[1]}`);
    }
    // A detail that carries the size signature but yields no canonical target is
    // malformed — fail the whole batch closed rather than guessing.
    if (found.length === 0) return null;
    for (const target of found) {
      if (target.includes('..') || target.endsWith('/')) return null;
      targets.add(target);
    }
  }
  return Array.from(targets).sort();
}

/**
 * Run the canonical, target-scoped size-cap repair from the repo root. The
 * checker still owns the bump, governing-source rewrite, ledger, and chronic
 * refusal policies; this loop merely supplies the comparator-attributed target.
 */
async function runDeterministicSizeCapBump(
  ws: WebSocket | null,
  repairTargets: readonly string[],
): Promise<boolean> {
  if (repairTargets.length === 0) return false;
  sendLog(
    ws,
    'Attributed size-guardrail breach detected — running deterministic cap repair ' +
    '(python3 backend/testing/check_size_limits.py --fix --repair-target …) instead of an LLM refactor.\n',
  );
  try {
    const out = await execFileAsync(
      'python3',
      [
        'backend/testing/check_size_limits.py', '--fix',
        ...repairTargets.flatMap(target => ['--repair-target', target]),
      ],
      { cwd: WORKSPACE, timeoutMs: TEST_RUNNER_TIMEOUT },
    );
    sendLog(ws, `${out}\n`);
    // The checker prints "No breaches to fix" when nothing changed — treat that
    // as a no-op so the caller falls back to the Claude fix path.
    if (/No breaches to fix/i.test(out)) return false;
    return true;
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    sendLog(ws, `WARNING: deterministic size-cap bump failed: ${msg.slice(0, 500)}\n`);
    return false;
  }
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

export interface StructuredTestFailure {
  route: string;
  testcase: string;
  kind: 'failure' | 'error';
  detail: string;
}

export interface TestFailureBaseline {
  records: StructuredTestFailure[];
}

type StructuredAttribution = {
  blocking: boolean;
  new_failures: Record<string, string>;
  suppressed_failures: Record<string, { detail: string; reason: string }>;
};

// Full schema at https://vitest.dev/guide/reporters.html#json-reporter; these
// fields are stable across Vitest 2.x/3.x and carry the full comparator key.
interface VitestJsonReport {
  testResults?: Array<{
    name?: string;
    assertionResults?: Array<{
      fullName?: string;
      title?: string;
      status?: 'passed' | 'failed' | 'skipped' | 'pending' | 'todo';
      failureMessages?: string[];
    }>;
  }>;
}

function stableVitestRoute(name: string): string {
  if (!name) return '<vitest>';
  const normalized = name.replace(/\\/g, '/');
  const workspace = WORKSPACE.replace(/\\/g, '/');
  return normalized.startsWith(`${workspace}/`)
    ? normalized.slice(workspace.length + 1)
    : path.isAbsolute(normalized) ? path.basename(normalized) : normalized;
}

export function parseStructuredVitestFailures(jsonText: string): StructuredTestFailure[] | null {
  let parsed: VitestJsonReport;
  try {
    parsed = JSON.parse(jsonText) as VitestJsonReport;
  } catch {
    return null;
  }
  const records: StructuredTestFailure[] = [];
  for (const file of parsed.testResults ?? []) {
    for (const assertion of file.assertionResults ?? []) {
      if (assertion.status !== 'failed') continue;
      const testcase = (assertion.fullName || assertion.title || '').trim();
      if (!testcase) return null;
      records.push({
        route: stableVitestRoute(file.name ?? ''),
        testcase,
        kind: 'failure',
        detail: (assertion.failureMessages ?? []).join('\n').trim(),
      });
    }
  }
  return records;
}

/**
 * Extract failing test IDs from a vitest JSON reporter file. Returns the
 * fullName (or title) of every assertion with status="failed". Preferred
 * over stdout scanning because stdout can contain false-positive markers
 * from inside passing tests' mocked output.
 */
export function parseFailingTestsFromVitestJson(jsonText: string): Set<string> {
  const failing = new Set<string>();
  parseStructuredVitestFailures(jsonText)?.forEach(record => failing.add(record.testcase));
  return failing;
}

function isPytestRunner(runner: TestRunner): boolean {
  return [runner.cmd, ...runner.args].some(arg =>
    arg.includes('pytest') || arg.includes('scripts/test-backend.sh'),
  );
}

function usesVerificationJunitContract(runner: TestRunner): boolean {
  // scripts/test-backend.sh runs pytest in Docker for the normal backend
  // verification path. Its BLUEPRINT_VERIFICATION_JUNIT contract mounts the
  // report directory and translates the host path for the container; a direct
  // host /tmp --junitxml argument is not visible from that container.
  return [runner.cmd, ...runner.args].some(arg => arg.includes('scripts/test-backend.sh'));
}

type StructuredReportInvocation = {
  reportOutputPath: string | null;
  runArgs: string[];
  env?: NodeJS.ProcessEnv;
  useVitestJson: boolean;
};

function structuredReportInvocation(runner: TestRunner, phase: 'baseline' | 'current'): StructuredReportInvocation {
  const useVitestJson = runner.cmd === 'npm' && isVitestRunner(runner.cwd);
  const usePytestJunit = !useVitestJson && isPytestRunner(runner);
  const reportOutputPath = useVitestJson
    ? `/tmp/vitest-${phase}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}.json`
    : usePytestJunit ? `/tmp/pytest-${phase}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}.xml` : null;
  const harnessAwareJunit = usePytestJunit && usesVerificationJunitContract(runner);
  const runArgs = useVitestJson && reportOutputPath
    ? [...runner.args, '--', '--reporter=default', '--reporter=json', `--outputFile=${reportOutputPath}`]
    : usePytestJunit && reportOutputPath && !harnessAwareJunit
      ? [...runner.args, `--junitxml=${reportOutputPath}`]
      : runner.args;

  return {
    reportOutputPath,
    runArgs,
    env: harnessAwareJunit && reportOutputPath
      ? { ...process.env, BLUEPRINT_VERIFICATION_JUNIT: reportOutputPath }
      : undefined,
    useVitestJson,
  };
}

function junitRecords(reportPath: string, treeRoot: string): StructuredTestFailure[] | null {
  try {
    const output = execFileSync(
      'python3',
      [canonicalComparatorPath(), '--junit-records', reportPath, treeRoot],
      { cwd: WORKSPACE, encoding: 'utf-8', timeout: 30_000 },
    );
    const records = JSON.parse(output) as unknown;
    return Array.isArray(records) ? records as StructuredTestFailure[] : null;
  } catch {
    return null;
  }
}

export function compareStructuredFailures(
  current: StructuredTestFailure[], baseline: StructuredTestFailure[],
): StructuredAttribution | null {
  try {
    const output = execFileSync('python3', [canonicalComparatorPath()], {
      cwd: WORKSPACE,
      encoding: 'utf-8',
      input: JSON.stringify({ current, baseline }),
      timeout: 30_000,
    });
    const result = JSON.parse(output) as Partial<StructuredAttribution>;
    if (typeof result.blocking !== 'boolean' || !result.new_failures || !result.suppressed_failures) return null;
    return result as StructuredAttribution;
  } catch {
    return null;
  }
}

export function canonicalComparatorPath(
  sourceDirectory = path.dirname(fileURLToPath(import.meta.url)),
): string {
  // The backend source tree is not present on baked agents: Packer and the
  // container image copy agent/src to /opt/agent/src. Keep the parity-checked
  // canonical attributor in that packaged tree, rather than falling back to a
  // workspace checkout whose contents are the task under verification.
  return path.resolve(
    sourceDirectory,
    '../_agent_scripts/fixgate_attribution.py',
  );
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
 * - Returns an empty structured record set if all tests pass.
 * - Returns null when the baseline cannot be proved from a structured report.
 */
export async function captureTestBaseline(
  ws: WebSocket | null,
  statusRef: { value: string },
  buildConfig?: BuildConfig,
  relatedFiles?: string[],
  timeoutMs = TEST_BASELINE_TIMEOUT,
): Promise<TestFailureBaseline | null> {
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

  const baselineRecords: StructuredTestFailure[] = [];
  const taskId = process.env.TASK_ID || '';
  const successfulRunners: TestRunner[] = [];

  for (const runner of runners) {
    // A baseline is only meaningful after the runner's locked dependencies
    // exist. Fresh task clones intentionally omit node_modules; invoking npm
    // test first turns a missing Vitest binary into a false repository failure.
    // Use the same package-manager-aware setup contract as the post-change
    // test loop, and never invoke the suite when setup fails.
    const setupCommand = describeRunnerSetupCommand(runner);
    if (setupCommand) {
      sendHeartbeatSync(taskId, `Baseline setup [${runner.label}]...`);
      sendLog(ws, `Preparing baseline runner [${runner.label}]: ${setupCommand.label}\n`);
      try {
        await execFileAsync(setupCommand.cmd, setupCommand.args, {
          cwd: runner.cwd,
          timeoutMs: TEST_RUNNER_TIMEOUT,
        });
      } catch (setupErr) {
        const msg = setupErr instanceof Error ? setupErr.message : String(setupErr);
        sendLog(
          ws,
          `Test baseline setup failed for [${runner.label}] — suite was not executed.\n` +
          `${msg.slice(0, 500)}\n`,
        );
        await postBuildDiscoveryReport(ws, buildConfig, successfulRunners);
        return null;
      }
    }
    // For vitest, request the JSON reporter so we can extract failing
    // test names authoritatively — the stdout parser historically missed
    // vitest's " FAIL" format, producing a "0 pre-existing failures"
    // baseline even when the suite had many. When JSON is available it's
    // the source of truth; stdout is a fallback for runners (pytest) that
    // don't emit a comparable JSON report at this runner-invocation layer.
    const { reportOutputPath, runArgs, env, useVitestJson } = structuredReportInvocation(runner, 'baseline');

    try {
      sendHeartbeatSync(taskId, `Baseline test run [${runner.label}]...`);
      const runnerTimeout = isDockerRunner(runner) ? TEST_BASELINE_DOCKER_TIMEOUT : timeoutMs;
      await execFileAsync(runner.cmd, runArgs, {
        cwd: runner.cwd,
        timeoutMs: runnerTimeout,
        env,
      });
      // Runner exited cleanly — no failures
      successfulRunners.push(runner);
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
        await postBuildDiscoveryReport(ws, buildConfig, successfulRunners);
        return null;
      }
      const msg = err instanceof Error ? err.message : String(err);
      // Fail closed when a failing supported runner cannot provide the
      // occurrence records the route/key/detail comparator needs.  Do not
      // revive the old stdout/name-only baseline as a fallback.
      let records: StructuredTestFailure[] | null = null;
      if (reportOutputPath && fs.existsSync(reportOutputPath)) {
        try {
          records = useVitestJson
            ? parseStructuredVitestFailures(fs.readFileSync(reportOutputPath, 'utf-8'))
            : junitRecords(reportOutputPath, runner.cwd);
        } catch {
          records = null;
        }
      }
      if (records === null || records.length === 0) {
        sendLog(ws, `Test baseline for [${runner.label}] exited non-zero but did not produce structured failure records. Baseline is unverified.\n${msg.slice(0, 500)}\n`);
        return null;
      }
      baselineRecords.push(...records);
    } finally {
      if (reportOutputPath) {
        try { fs.unlinkSync(reportOutputPath); } catch { /* ignore */ }
      }
    }
  }

  await postBuildDiscoveryReport(ws, buildConfig, successfulRunners);

  if (baselineRecords.length > 0) {
    sendLog(ws, `Baseline: ${baselineRecords.length} pre-existing failure occurrence(s) found — these will be excluded from test fix analysis.\n`);
  } else {
    sendLog(ws, 'Baseline captured: all tests passing before changes.\n');
  }

  return { records: baselineRecords };
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

/**
 * Render the per-runner setup/environment failures recorded during one attempt
 * into a single operator-facing block (capped to keep the verdict artifact under
 * the backend payload budget). Used to surface partial-setup failures — a runner
 * whose environment broke (npm ci / pip / go mod / cargo fetch / runner timeout /
 * missing-runtime signature) never executed its suite, so a sibling runner's
 * clean pass must NOT be reported as an unqualified pass.
 */
function formatSetupErrors(setupErrors: Array<{ label: string; message: string }>): string {
  return setupErrors
    .map(e => `[${e.label}] ${e.message}`)
    .join('\n\n')
    .slice(0, 2000);
}

export interface TestFixLoopOptions {
  targetPytestSelectors?: string[];
  runImpactAfterTargeted?: boolean;
  requireTestmonForImpact?: boolean;
  maxFixAttempts?: number;
  phaseLabel?: string;
  runnersOverride?: TestRunner[];
  /** Map of repo-relative file path → number of changed/added lines, used to
   *  compute diff-aware changed-line coverage (``changed_lines_coverage_pct``)
   *  alongside the total ``line_coverage_pct``.  Only used when coverage
   *  instrumentation (pytest-cov) is active. */
  changedLineCounts?: Record<string, number>;
}

function describeRunnerSetupCommand(runner: TestRunner): { cmd: string; args: string[]; label: string } | null {
  if (runner.installCmd) {
    return {
      ...runnerFromCommand(runner.installCmd, runner.cwd),
      label: runner.installCmd,
    };
  }
  if (runner.cmd === 'npm' && !fs.existsSync(`${runner.cwd}/node_modules`)) {
    // Detect which Node package manager to use based on lockfile presence
    const hasYarnLock = fs.existsSync(`${runner.cwd}/yarn.lock`);
    const hasPnpmLock = fs.existsSync(`${runner.cwd}/pnpm-lock.yaml`);

    if (hasYarnLock) {
      return {
        cmd: 'yarn',
        args: ['install', '--frozen-lockfile'],
        label: 'yarn install --frozen-lockfile',
      };
    }
    if (hasPnpmLock) {
      return {
        cmd: 'pnpm',
        args: ['install', '--frozen-lockfile'],
        label: 'pnpm install --frozen-lockfile',
      };
    }
    // Default to npm ci (for package-lock.json or as final fallback)
    return {
      cmd: 'npm',
      args: ['ci'],
      label: 'npm ci',
    };
  }
  return null;
}

export async function testFixLoop(
  ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string, buildConfig?: BuildConfig,
  baselineFailures?: TestFailureBaseline,
  relatedFiles?: string[],
  analysisContext?: string,
  options: TestFixLoopOptions = {},
): Promise<{ passed: boolean; testFailuresDetected: boolean; setupFailed?: boolean; partialSetupFailed?: boolean; timedOut?: boolean; verificationUnverified?: boolean; failureSummary?: string; noTestsExecuted?: boolean; noTestsReason?: 'all_skipped' | 'no_real_tests'; coverage?: { passed: number; skipped: number; total: number }; line_coverage_pct?: number; changed_lines_coverage_pct?: number }> {
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
    const failures: Array<{ label: string; message: string; structuredRecords?: StructuredTestFailure[] | null }> = [];
    // Track setup errors (dep install, spawn timeout) separately from test
    // failures. If NO runner actually gets to execute tests, we can't
    // claim tests passed — the result must propagate as a setup failure
    // so the caller doesn't push unverified code.
    const setupErrors: Array<{ label: string; message: string }> = [];
    // Wall-clock timeouts / SIGTERM-kills are tracked SEPARATELY from setup
    // errors. A runner that blows the wall clock ran an unbounded/oversized suite
    // — retrying it just times out again (it is NOT a transient infra blip). When
    // a timeout is the ONLY reason nothing ran, the caller must degrade to a
    // non-blocking "gate could not run" fall-open (timedOut), NOT the retried
    // setupFailed path that wedges the merge gate.
    const timeoutErrors: Array<{ label: string; message: string }> = [];
    let ranAnyRunner = false;
    // Accumulate each runner's raw combined output (pass output + real-failure
    // output) so a best-effort pass/skip/total coverage summary can be parsed
    // BEFORE the per-runner output is discarded. Reset per attempt so coverage
    // reflects the run actually being reported. Setup/timeout/env errors route
    // to setupErrors and are intentionally excluded (no real suite output).
    const runnerOutputs: string[] = [];
    // Track whether ANY runner's output indicates a real test actually
    // executed (vs. a placeholder command or a zero-test collection). Used to
    // distinguish "a suite ran and produced a verdict" from "no real tests
    // executed" so the no-CI merge gate can route the latter to the documented
    // proceed-without-checks fallback instead of a hard fail / vacuous pass.
    let anyRealTestExecuted = false;
    // Track whether any runner's output was classified as an ALL-SKIPPED run
    // (collected tests but executed none — all skipped/deselected/ignored)
    // vs. a plain no-real-tests case (placeholder / zero-collection), so the
    // no-execution verdict can distinguish "suite present but every test
    // skipped" from "no real tests".
    let sawAllSkipped = false;
    const taskId = process.env.TASK_ID || '';
    for (const runner of runners) {
      // Setup step: run any declared bootstrap/install command before the
      // suite, otherwise preserve the historical npm ci bootstrap for clean
      // Node worktrees. A failure HERE is not a fixable test failure.
      const setupCommand = describeRunnerSetupCommand(runner);
      if (setupCommand) {
        const setupDescription = runner.installCmd
          ? `Running install/bootstrap for ${runner.label}: ${runner.installCmd}`
          : `Installing dependencies for ${runner.label}...`;
        sendLog(ws, `${setupDescription}\n`);
        try {
          await execFileAsync(setupCommand.cmd, setupCommand.args, {
            cwd: runner.cwd,
            timeoutMs: TEST_RUNNER_TIMEOUT,
          });
        } catch (installErr) {
          const msg = installErr instanceof Error ? installErr.message : String(installErr);
          if (isInfrastructureSetupFailure(installErr, msg, { includeTransientSetupFailures: true })) {
            sendLog(
              ws,
              `WARNING: Runner setup failed for [${runner.label}] — ` +
              `infrastructure/setup error, not a fixable test failure. Skipping runner.\n` +
              `${msg.slice(0, 500)}\n`,
            );
            setupErrors.push({ label: runner.label, message: `${setupCommand.label} failed: ${msg}` });
            continue;
          }
          sendLog(
            ws,
            `Runner setup failed for [${runner.label}] — treating this as a repository/bootstrap failure ` +
            `so the verify flow stays red instead of falling open.\n${msg.slice(0, 500)}\n`,
          );
          failures.push({ label: runner.label, message: `${setupCommand.label} failed:\n${msg}` });
          ranAnyRunner = true;
          continue;
        }
      }
      // Python pre-install: install declared deps before running the suite.
      // poetry.lock → poetry install --no-root; requirements.txt → pip install.
      // Treat install failure as a setup error so it routes to setupFailed
      // rather than the code-fix loop.
      if (
        (runner.cmd === 'python3' || runner.cmd === 'python' || runner.cmd === 'pytest' || runner.cmd === 'tox' || runner.cmd === 'nox') &&
        (fs.existsSync(`${runner.cwd}/poetry.lock`) || fs.existsSync(`${runner.cwd}/requirements.txt`))
      ) {
        const usePoetry = fs.existsSync(`${runner.cwd}/poetry.lock`);
        const installCmd = usePoetry ? 'poetry' : 'pip';
        const installArgs = usePoetry
          ? ['install', '--no-root']
          : ['install', '-r', 'requirements.txt'];
        sendLog(ws, `Installing Python dependencies for ${runner.label}...\n`);
        try {
          await execFileAsync(installCmd, installArgs, { cwd: runner.cwd, timeoutMs: TEST_RUNNER_TIMEOUT });
        } catch (installErr) {
          const msg = installErr instanceof Error ? installErr.message : String(installErr);
          if (isInfrastructureSetupFailure(installErr, msg, { includeTransientSetupFailures: true })) {
            sendLog(
              ws,
              `WARNING: Python dependency install failed for [${runner.label}] — ` +
              `infrastructure/setup error. Skipping runner.\n${msg.slice(0, 500)}\n`,
            );
            setupErrors.push({ label: runner.label, message: `${installCmd} install failed: ${msg}` });
            continue;
          }
          sendLog(
            ws,
            `Python dependency install failed for [${runner.label}] — treating this as a repository ` +
            `dependency failure instead of a setup fallback.\n${msg.slice(0, 500)}\n`,
          );
          failures.push({ label: runner.label, message: `${installCmd} install failed:\n${msg}` });
          ranAnyRunner = true;
          continue;
        }
      }
      // Go pre-install: download module graph before running tests.
      if (runner.cmd === 'go' && fs.existsSync(`${runner.cwd}/go.mod`)) {
        sendLog(ws, `Downloading Go modules for ${runner.label}...\n`);
        try {
          await execFileAsync('go', ['mod', 'download'], { cwd: runner.cwd, timeoutMs: TEST_RUNNER_TIMEOUT });
        } catch (installErr) {
          const msg = installErr instanceof Error ? installErr.message : String(installErr);
          if (isInfrastructureSetupFailure(installErr, msg, { includeTransientSetupFailures: true })) {
            sendLog(
              ws,
              `WARNING: Go module download failed for [${runner.label}] — ` +
              `infrastructure/setup error. Skipping runner.\n${msg.slice(0, 500)}\n`,
            );
            setupErrors.push({ label: runner.label, message: `go mod download failed: ${msg}` });
            continue;
          }
          sendLog(
            ws,
            `Go module download failed for [${runner.label}] — treating this as a repository/module failure.\n` +
            `${msg.slice(0, 500)}\n`,
          );
          failures.push({ label: runner.label, message: `go mod download failed:\n${msg}` });
          ranAnyRunner = true;
          continue;
        }
      }
      // Rust pre-install: fetch dependencies before running tests.
      if (runner.cmd === 'cargo' && fs.existsSync(`${runner.cwd}/Cargo.toml`)) {
        sendLog(ws, `Fetching Rust dependencies for ${runner.label}...\n`);
        try {
          await execFileAsync('cargo', ['fetch', '--manifest-path', `${runner.cwd}/Cargo.toml`], { cwd: runner.cwd, timeoutMs: TEST_RUNNER_TIMEOUT });
        } catch (installErr) {
          const msg = installErr instanceof Error ? installErr.message : String(installErr);
          if (isInfrastructureSetupFailure(installErr, msg, { includeTransientSetupFailures: true })) {
            sendLog(
              ws,
              `WARNING: Rust dependency fetch failed for [${runner.label}] — ` +
              `infrastructure/setup error. Skipping runner.\n${msg.slice(0, 500)}\n`,
            );
            setupErrors.push({ label: runner.label, message: `cargo fetch failed: ${msg}` });
            continue;
          }
          sendLog(
            ws,
            `Rust dependency fetch failed for [${runner.label}] — treating this as a repository dependency failure.\n` +
            `${msg.slice(0, 500)}\n`,
          );
          failures.push({ label: runner.label, message: `cargo fetch failed:\n${msg}` });
          ranAnyRunner = true;
          continue;
        }
      }
      // For vitest runners, request the JSON reporter alongside default
      // (human-readable) output — the JSON file is the authoritative source
      // of failed-test IDs, stdout is kept for operator diagnosis.
      const { reportOutputPath, runArgs, env, useVitestJson } = structuredReportInvocation(runner, 'current');

      try {
        sendHeartbeatSync(taskId, `Running tests [${runner.label}]...`);
        const runnerTimeout = isDockerRunner(runner) ? TEST_RUNNER_DOCKER_TIMEOUT : TEST_RUNNER_TIMEOUT;
        const passOutput = await execFileAsync(runner.cmd, runArgs, { cwd: runner.cwd, timeoutMs: runnerTimeout, env });
        ranAnyRunner = true;
        runnerOutputs.push(passOutput);
        // A clean exit only counts as a real verdict if real tests ran. An
        // empty/zero-test suite that exits 0 must not be recorded as a
        // verified pass.
        const passClass = classifyTestExecution(passOutput);
        if (passClass === 'ran') anyRealTestExecuted = true;
        else if (passClass === 'all_skipped') sawAllSkipped = true;
        sendLog(ws, `Tests passed [${runner.label}].\n`);
      } catch (testErr: unknown) {
        const errno = testErr instanceof Error
          ? (testErr as NodeJS.ErrnoException).code
          : undefined;
        // Route a TRUE wall-clock kill to the bounded fall-open (timedOut) lane.
        // Narrow to errno==='ETIMEDOUT' ONLY — execFileAsync sets that code
        // whenever its wall-clock termTimer fired (even when it escalates
        // SIGTERM→SIGKILL: the `timedOut` branch wins over the SIGKILL/EOOM one),
        // so a genuine timeout ALWAYS carries ETIMEDOUT. `killed` alone is too
        // broad: execFileAsync sets killed=true for ANY signal-terminated process
        // — an OOM kill (code 'EOOM'/SIGKILL), a native SIGSEGV/SIGABRT crash, or
        // an external SIGTERM. Those are NOT wall-clock timeouts and must stay on
        // the fail-closed setupErrors lane (retryable) rather than falling open as
        // timedOut and merging unverified/broken code.
        const isRunnerTimeout = errno === 'ETIMEDOUT';
        if (isRunnerTimeout) {
          const msg = testErr instanceof Error ? testErr.message : String(testErr);
          sendLog(ws, `WARNING: Test runner [${runner.label}] hit the wall clock — the suite is unbounded/oversized; recording a timeout (not a transient setup failure to retry).\n`);
          timeoutErrors.push({ label: runner.label, message: `runner timeout: ${msg}` });
          continue;
        }
        const msg = testErr instanceof Error ? testErr.message : String(testErr);
        // Route only true VM/runtime failures to setupErrors. Repo dependency
        // regressions (missing package/module/import after the suite starts)
        // stay on the failure lane so verify blocks instead of downgrading to
        // setupFailed/UNVERIFIED.
        if (isInfrastructureSetupFailure(testErr, msg)) {
          sendLog(
            ws,
            `WARNING: Test runner [${runner.label}] hit an environment failure ` +
            `(missing runtime / shared library / spawn ENOENT) — not a fixable code failure. ` +
            `Skipping runner.\n${msg.slice(0, 500)}\n`,
          );
          setupErrors.push({ label: runner.label, message: `environment failure: ${msg}` });
          continue;
        }
        // Non-timeout, non-env-failure, non-zero exit = the runner ran the
        // suite and at least one test failed. Record the failure.
        ranAnyRunner = true;
        runnerOutputs.push(msg);
        // A non-zero exit from a placeholder command (e.g. the default `npm`
        // "no test specified" script) is NOT a real test failure. Only count
        // this as a real test executing when the output isn't a no-tests
        // signature.
        const failClass = classifyTestExecution(msg);
        if (failClass === 'ran') anyRealTestExecuted = true;
        else if (failClass === 'all_skipped') sawAllSkipped = true;
        let structuredRecords: StructuredTestFailure[] | null = null;
        if (reportOutputPath && fs.existsSync(reportOutputPath)) {
          try {
            structuredRecords = useVitestJson
              ? parseStructuredVitestFailures(fs.readFileSync(reportOutputPath, 'utf-8'))
              : junitRecords(reportOutputPath, runner.cwd);
          } catch {
            structuredRecords = null;
          }
        }
        failures.push({ label: runner.label, message: msg, structuredRecords });
      } finally {
        if (reportOutputPath) {
          try { fs.unlinkSync(reportOutputPath); } catch { /* already gone */ }
        }
      }
    }

    // Best-effort coverage summary (pass/skip/total) parsed from the combined
    // runner output of THIS attempt. Conservative: extractTestCoverage returns
    // null (→ undefined here) for ambiguous/unparseable output, in which case
    // the field is simply omitted from the verdict. Visibility only — never
    // changes pass/fail or detected.
    const coverage = extractTestCoverage(runnerOutputs.join('\n')) ?? undefined;
    // Line/statement coverage percentage from pytest-cov terminal output.
    // Populated only when the test runner includes coverage instrumentation
    // (--cov-report=term-missing). When absent (no instrumentation active),
    // line_coverage_pct is undefined and the backend falls back to the
    // test-count-based coverage computation.
    const lineCoveragePct = extractLineCoverage(runnerOutputs.join('\n')) ?? undefined;
    // Diff-aware changed-line coverage: weighted per-file coverage by changed-line
    // count per file.  Only computed when the PR diff data is available (passed via
    // options.changedLineCounts) and coverage instrumentation is active.
    const changedLineCoveragePct = (options.changedLineCounts && lineCoveragePct !== undefined)
      ? extractChangedLineCoverage(runnerOutputs.join('\n'), options.changedLineCounts) ?? undefined
      : undefined;

    // Partial-setup detection: at least one runner ran AND at least one OTHER
    // runner failed setup/environment. The failed runner's suite never
    // executed, so any "pass" here is only over the runners that ran — we must
    // not report an unqualified clean pass. setupSummary carries the broken
    // runners' errors; setupNote appends them to the test-failure summary on the
    // real-failure exits so they are surfaced alongside genuine failures.
    // A runner that never executed its suite — because of a genuine
    // environment/setup error OR a wall-clock timeout — is a "blocked" runner.
    // Both count toward the partial-setup signal (a sibling ran clean, but this
    // one produced no verdict, so we must not report an unqualified pass).
    const blockedRunnerErrors = [...setupErrors, ...timeoutErrors];
    const partialSetupFailed = ranAnyRunner && blockedRunnerErrors.length > 0;
    const setupSummary = blockedRunnerErrors.length > 0 ? formatSetupErrors(blockedRunnerErrors) : '';
    const setupNote = setupSummary
      ? `\n\nPartial setup failure — the following runner(s) never executed (environment/setup error); their suites are UNVERIFIED:\n${setupSummary}`
      : '';

    const reportableFailures = failures.filter(
      failure => classifyTestExecution(failure.message) === 'ran',
    );
    const placeholderFailures = failures.filter(
      failure => classifyTestExecution(failure.message) !== 'ran',
    );

    if (reportableFailures.length === 0) {
      // "No failures" is only a pass if we actually ran at least one
      // runner. If every runner hit setup errors (npm ci / spawn
      // timeout), tests didn't run — we can't claim they passed, and
      // there's nothing for Claude to fix. Surface as a distinct
      // setupFailed state so the caller fails the task with an infra
      // category instead of merging unverified code.
      if (!ranAnyRunner && blockedRunnerErrors.length > 0) {
        // A WALL-CLOCK kill of the whole suite is not a transient setup blip:
        // retrying re-times-out and wedges the merge gate. When the ONLY reason
        // nothing ran was a timeout (no genuine environment/setup error), surface
        // a distinct ``timedOut`` state so the caller degrades to a non-blocking
        // "gate could not run" fall-open instead of the retried setupFailed path.
        if (setupErrors.length === 0 && timeoutErrors.length > 0) {
          sendLog(ws, `Tests could not run — the suite exceeded the wall clock (no environment/setup error). Reporting a bounded "gate could not run" state (no retry).\n${setupSummary}\n`);
          return { passed: false, testFailuresDetected: false, timedOut: true, failureSummary: setupSummary };
        }
        sendLog(ws, `Tests could not run — setup failed for all runners. Not invoking fix loop.\n${setupSummary}\n`);
        return { passed: false, testFailuresDetected: false, setupFailed: true, failureSummary: setupSummary };
      }
      // Partial setup failure: some runner(s) ran clean, but at least one
      // sibling runner failed setup and never executed its suite. Reporting a
      // clean pass here would silently merge with an entire suite unverified.
      // Surface a distinct partialSetupFailed signal (passed:false) so the
      // caller drives the same bounded setup-retry the all-failed case uses and,
      // once exhausted, fails the gate closed rather than falling open.
      if (partialSetupFailed) {
        sendLog(ws, `Tests passed on the runners that ran, but ${blockedRunnerErrors.length} runner(s) never executed (setup error / wall-clock timeout) — not reporting a clean pass.\n${setupSummary}\n`);
        return { passed: false, testFailuresDetected: false, partialSetupFailed: true, failureSummary: setupSummary };
      }
      if (!anyRealTestExecuted && placeholderFailures.length > 0) {
        const failureSummary = placeholderFailures
          .map(failure => `[${failure.label}]:\n${failure.message}`)
          .join('\n\n')
          .slice(-8000);
        sendLog(ws, 'No real tests executed — only placeholder/empty-suite failures were observed.\n');
        return { passed: false, testFailuresDetected: true, failureSummary, noTestsExecuted: true };
      }
      emitWrapperFixLoopEvent({
        loop: 'test', iteration: attempt + 1, errorMessage: '',
        exitReason: 'passed', startedAt: iterStartedAt,
      });
      sendLog(ws, 'All tests passed.\n');
      // noTestsExecuted: we ran at least one runner but none of them showed a
      // real test executing — the "pass" is vacuous (empty/zero-test suite or
      // an all-skipped suite). noTestsReason distinguishes the two.
      const noTestsExecuted = ranAnyRunner && !anyRealTestExecuted;
      return {
        passed: true, testFailuresDetected: false, noTestsExecuted,
        noTestsReason: noTestsExecuted ? (sawAllSkipped ? 'all_skipped' : 'no_real_tests') : undefined,
        coverage,
        line_coverage_pct: lineCoveragePct,
        changed_lines_coverage_pct: changedLineCoveragePct,
      };
    }

    // Serialize each failure for operator-facing output. This same format
    // fed the old pattern matcher; it's still used as a fallback when we
    // don't have structured IDs (non-vitest runners).
    const renderFailure = (f: { label: string; message: string }) => `[${f.label}]:\n${f.message}`;

    // When baseline is available, let the canonical Python comparator decide
    // route + testcase + kind + normalized-detail attribution.  A missing
    // report/comparator is unverified; never fall back to test names.
    let actionableFailureDetails: string[] | null = null;
    let preExistingNote = '';

    if (baselineFailures) {
      const allRecords = reportableFailures.flatMap(failure => failure.structuredRecords ?? []);
      if (allRecords.length === 0 || reportableFailures.some(failure => failure.structuredRecords === null)) {
        return {
          passed: false,
          testFailuresDetected: false,
          verificationUnverified: true,
          failureSummary: 'Test runner failed but did not produce a structured failure report for baseline attribution.',
        };
      }
      const attribution = compareStructuredFailures(allRecords, baselineFailures.records);
      if (attribution === null) {
        return {
          passed: false,
          testFailuresDetected: false,
          verificationUnverified: true,
          failureSummary: 'Canonical structured baseline comparator was unavailable or returned an invalid result.',
        };
      }

      if (!attribution.blocking) {
        // Every detected failure existed before our changes — task is not responsible
        sendLog(ws, `All ${allRecords.length} test failure occurrence(s) are pre-existing and were not caused by this task.\n`);
        // ...but if a sibling runner failed setup, a suite never ran — surface
        // the partial-setup signal instead of an unqualified clean pass.
        if (partialSetupFailed) {
          sendLog(ws, `However, ${blockedRunnerErrors.length} runner(s) never executed (setup error / wall-clock timeout) — reporting partial setup failure.\n${setupSummary}\n`);
          return { passed: false, testFailuresDetected: false, partialSetupFailed: true, failureSummary: setupSummary };
        }
        return { passed: true, testFailuresDetected: false };
      }

      preExistingNote = `\n\nNote: structured baseline attribution suppressed ${Object.keys(attribution.suppressed_failures).length} inherited failure occurrence(s).`;
      actionableFailureDetails = Object.values(attribution.new_failures);
      if (Object.keys(attribution.suppressed_failures).length > 0) {
        // Keep inherited diagnostics visible to operators, but never feed them
        // into cap mutation or the LLM's actionable-failure prompt.
        sendLog(ws, `Reporting ${Object.keys(attribution.suppressed_failures).length} inherited failure occurrence(s); excluding them from task repair decisions.\n`);
      }
    }

    const combinedFailures = actionableFailureDetails
      ? actionableFailureDetails.join('\n\n')
      : reportableFailures.map(renderFailure).join('\n\n');
    const fingerprint = errorFingerprint(combinedFailures);
    // Early exit: byte-identical consecutive failure (existing behaviour).
    if (fingerprintHistory.length > 0 && fingerprint === fingerprintHistory[fingerprintHistory.length - 1]) {
      emitWrapperFixLoopEvent({
        loop: 'test', iteration: attempt + 1, errorMessage: combinedFailures,
        exitReason: 'early_unchanged', startedAt: iterStartedAt,
      });
      sendLog(ws, `Test failures unchanged from previous attempt — exiting early (persistent, unfixable by Claude).\n`);
      const failureSummary = combinedFailures.slice(-8000) + setupNote;
      return { passed: false, testFailuresDetected: true, failureSummary, partialSetupFailed: partialSetupFailed || undefined, line_coverage_pct: lineCoveragePct, changed_lines_coverage_pct: changedLineCoveragePct, coverage };
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
      const failureSummary = combinedFailures.slice(-8000) + setupNote;
      return { passed: false, testFailuresDetected: true, failureSummary, partialSetupFailed: partialSetupFailed || undefined, line_coverage_pct: lineCoveragePct, changed_lines_coverage_pct: changedLineCoveragePct, coverage };
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

      // Deterministic size-guardrail remediation takes precedence over the LLM
      // fix path. A size breach has a stable, machine-detectable signature and a
      // single policy-defined fix (bump the cap via the shared checker). Resolve
      // it here, before the model gets a vote, so the outcome no longer depends
      // on the model resisting its refactor bias. On success we re-run the suite
      // to verify the breach cleared without spending an LLM fix attempt; the
      // prompt-level SIZE_GUARDRAIL_GUIDANCE remains the fallback for the no-op
      // case (e.g. a breach the checker can't resolve).
      const repairTargets = actionableFailureDetails === null
        ? null
        : extractSizeRepairTargets(actionableFailureDetails);
      if (repairTargets && repairTargets.length > 0) {
        const bumped = await runDeterministicSizeCapBump(ws, repairTargets);
        if (bumped) {
          git(['add', '-A']);
          prevDiff = currDiff;
          currDiff = captureDiffLines();
          continue;
        }
        sendLog(ws, 'Deterministic size-cap bump made no change — falling back to the Claude fix path.\n');
      } else if (actionableFailureDetails !== null && repairTargets === null) {
        sendLog(ws, 'Attributed size diagnostic had no safe repair target — skipping deterministic cap mutation.\n');
      }

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
      const failureSummary = combinedFailures.slice(-8000) + setupNote;
      // noTestsExecuted: the only "failures" came from placeholder commands
      // (e.g. `npm` "no test specified") — no real test executed, so the
      // focused-verification fail should route to the proceed-without-checks
      // fallback rather than blocking the merge.
      const noTestsExecuted = ranAnyRunner && !anyRealTestExecuted;
      return {
        passed: false, testFailuresDetected: true, failureSummary, noTestsExecuted,
        noTestsReason: noTestsExecuted ? (sawAllSkipped ? 'all_skipped' : 'no_real_tests') : undefined,
        partialSetupFailed: partialSetupFailed || undefined,
        coverage,
        line_coverage_pct: lineCoveragePct,
        changed_lines_coverage_pct: changedLineCoveragePct,
      };
    }
  }
  return { passed: false, testFailuresDetected: true };
}
