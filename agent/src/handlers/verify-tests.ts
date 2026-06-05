import fs from 'fs';
import type WebSocket from 'ws';
import {
  WORKSPACE, strVal, sanitize, sendLog, validateBranchName,
  persistAndPostStatus, resolveBaseBranch, execFileAsync,
} from '../index.js';
import { prepareWorkspaceForRole } from '../workspacePrep.js';
import { DEFAULT_CLONE_DEPTH, TEST_BASELINE_TIMEOUT } from '../constants.js';
import { resolveBuildConfig } from '../build-config.js';
import {
  detectTestRunners, detectAutoTestRunners, describeRunnerDirectory,
  detectAcceptanceProbes, classifyProbeOutcome,
  type BuildConfig, type TestRunner, type AcceptanceProbe,
} from '../utils/test-runners.js';
import { testFixLoop } from '../utils/test-fix-loop.js';

// Cap the failing-test output we ship in the verdict. Mirrors the smoketest
// error-log tail size — enough to diagnose a failure without bloating the
// artifact (which is also subject to the backend payload-size budget).
const VERIFY_TESTS_OUTPUT_LIMIT = 16_000;

// Bounded in-handler retry for transient verify-time setup failures (dep
// install flake, runner spawn timeout). A setup failure means we could NOT run
// the suite — distinct from a genuine "no runnable suite" repo. We re-run the
// no-fix loop a few times before giving up so a single infrastructure blip does
// not silently skip a real suite and downgrade a tests-gated repo to UNVERIFIED.
const MAX_VERIFY_SETUP_RETRIES = 2;

interface VerifyTestsResult {
  detected: boolean;
  passed: boolean;
  tests: Array<{ name: string; status: string; message: string }>;
  output: string;
  // Set only when the suite could not run because setup/runner infrastructure
  // failed (vs. a genuine no-suite repo). The orchestrator reads this on a
  // FAILED verify task to distinguish a transient setup failure from a cold
  // clone crash when labeling an exhausted-retry UNVERIFIED merge.
  setupFailed?: boolean;
  // Set when the stored test command was STALE (it ran but executed no real
  // tests, or its command was gone) and auto-detection found a DIFFERENT, real
  // suite in the cloned workspace that actually executed. When true,
  // detected/passed/output reflect the REPLACEMENT suite's run. The backend
  // consumes this to converge the stored build_config onto the recovered suite.
  recovered?: boolean;
  // The per-directory configs (repo-root-relative path, language type,
  // reconstructed test command) of the replacement suite that actually ran.
  // Only populated alongside recovered=true.
  recovered_directories?: Array<{ path: string; type: string; test_cmd: string }>;
  // Machine-readable reason a detected=false verdict carries zero execution
  // signal, so a later backend change can surface "suite present but every test
  // skipped" (`all_skipped`) versus "detected suite ran no real tests"
  // (`no_real_tests`) versus "no detectable suite" (`no_suite`). Additive: only
  // set on the detected=false fall-open paths; never changes existing behaviour.
  noTestsReason?: 'all_skipped' | 'no_real_tests' | 'no_suite';
  // Cheap goal-level acceptance probe, run ONLY on the detected=false fall-open
  // paths (no suite / thin / all-skipped). Gives the no-CI merge gate at least
  // one honest execution signal — "does the changed code still compile/build?" —
  // when the unit suite produced none. CONSERVATIVE: a probe that could not run
  // (missing deps / unprovisioned setup) degrades to the existing fall-open and
  // never blocks; only a clear compile/build FAILURE blocks the merge. The
  // backend (ci_resolution.classify_merge_test_verdict) keys off
  // ran/passed/could_not_run to decide block-vs-fall-open. Additive: absent on
  // every detected=true verdict and the setup-failure FAILED path.
  probe?: {
    ran: boolean;
    ecosystem: string;
    outcome: 'pass' | 'fail' | 'could_not_run';
    passed: boolean;
    could_not_run: boolean;
    output: string;
    label: string;
  };
}

type TestFixLoopResult = Awaited<ReturnType<typeof testFixLoop>>;

type StaleCommandRecovery =
  | { kind: 'recovered'; result: VerifyTestsResult }
  | { kind: 'setupFailed'; loop: TestFixLoopResult }
  | null;

async function runVerifyLoop(
  ws: WebSocket | null,
  statusRef: { value: string },
  model: string | undefined,
  effort: string | undefined,
  buildConfig: BuildConfig | undefined,
  runnersOverride?: TestRunner[],
  retryStatusLabel = 'detected suite',
): Promise<TestFixLoopResult> {
  let loop = await testFixLoop(
    ws, statusRef, model, effort, buildConfig,
    undefined, undefined, undefined,
    { maxFixAttempts: 0, runnersOverride },
  );

  let setupRetries = 0;
  while (loop.setupFailed && setupRetries < MAX_VERIFY_SETUP_RETRIES) {
    setupRetries += 1;
    sendLog(ws, `Test runner setup failed (transient infra) — retrying ${retryStatusLabel} (${setupRetries}/${MAX_VERIFY_SETUP_RETRIES})...\n`);
    statusRef.value = `Retrying ${retryStatusLabel} after setup failure (${setupRetries}/${MAX_VERIFY_SETUP_RETRIES})...`;
    loop = await testFixLoop(
      ws, statusRef, model, effort, buildConfig,
      undefined, undefined, undefined,
      { maxFixAttempts: 0, runnersOverride },
    );
  }

  return loop;
}

// ── Cheap acceptance probe (goal-level fall-open signal) ─────────────────────
// Each probe runs under the bounded baseline timeout the suite-baseline run uses
// so the gate stays cheap (seconds, not a full build pipeline). A small per-run
// budget caps total probe processes.
const ACCEPTANCE_PROBE_TIMEOUT = TEST_BASELINE_TIMEOUT;
const MAX_ACCEPTANCE_PROBES = 4;

// Run the detected acceptance probe(s) for the cloned workspace and reduce them
// to a single verdict. Fail-fast: a clear compile/build FAILURE on ANY probe
// blocks (returns immediately). Otherwise a single clean PASS wins over a
// could-not-run; an all-could-not-run set degrades to could_not_run. Returns
// undefined when no probeable ecosystem is present (no probe signal at all).
async function runAcceptanceProbe(
  ws: WebSocket | null,
  buildConfig: BuildConfig | undefined,
): Promise<VerifyTestsResult['probe']> {
  const probes = detectAcceptanceProbes(buildConfig).slice(0, MAX_ACCEPTANCE_PROBES);
  if (probes.length === 0) {
    sendLog(ws, 'Acceptance probe: no probeable ecosystem detected — no goal-level probe signal.\n');
    return undefined;
  }
  let firstPass: AcceptanceProbe | null = null;
  let firstCouldNotRun: AcceptanceProbe | null = null;
  for (const probe of probes) {
    let outcome: 'pass' | 'fail' | 'could_not_run';
    let output = '';
    try {
      await execFileAsync(probe.cmd, probe.args, { cwd: probe.cwd, timeoutMs: ACCEPTANCE_PROBE_TIMEOUT });
      outcome = 'pass';
    } catch (err: unknown) {
      const e = err as Error & { stdout?: string; stderr?: string; code?: string; status?: number | null };
      output = [e.stdout, e.stderr, e.message].filter(Boolean).join('\n');
      outcome = classifyProbeOutcome(output, e);
    }
    sendLog(ws, `Acceptance probe [${probe.label}]: ${outcome}\n`);
    if (outcome === 'fail') {
      return {
        ran: true, ecosystem: probe.ecosystem, outcome,
        passed: false, could_not_run: false,
        output: output.slice(-VERIFY_TESTS_OUTPUT_LIMIT), label: probe.label,
      };
    }
    if (outcome === 'pass' && firstPass === null) firstPass = probe;
    if (outcome === 'could_not_run' && firstCouldNotRun === null) firstCouldNotRun = probe;
  }
  if (firstPass !== null) {
    return {
      ran: true, ecosystem: firstPass.ecosystem, outcome: 'pass',
      passed: true, could_not_run: false, output: '', label: firstPass.label,
    };
  }
  const cnr = firstCouldNotRun as AcceptanceProbe;
  return {
    ran: true, ecosystem: cnr.ecosystem, outcome: 'could_not_run',
    passed: false, could_not_run: true, output: '', label: cnr.label,
  };
}

// Run the acceptance probe and fold its verdict into a detected=false fall-open
// result. A clear probe FAIL keeps detected=false but carries probe.passed=false
// (the backend flips this to a merge block); pass/could-not-run leave the
// fall-open intact, only annotating output for the operator.
async function attachAcceptanceProbe(
  ws: WebSocket | null,
  buildConfig: BuildConfig | undefined,
  base: VerifyTestsResult,
): Promise<VerifyTestsResult> {
  const probe = await runAcceptanceProbe(ws, buildConfig);
  if (!probe) return base;
  let note: string;
  if (probe.outcome === 'fail') {
    note = `Acceptance probe FAILED (${probe.ecosystem}: ${probe.label}) — the changed code does not compile/build; blocking the merge.`;
  } else if (probe.outcome === 'pass') {
    note = `Acceptance probe passed (${probe.ecosystem}: ${probe.label}) — the changed code still compiles/builds (cheap goal-level signal; unit suite still absent/thin).`;
  } else {
    note = `Acceptance probe could not run (${probe.ecosystem}: ${probe.label}) — missing deps / unprovisioned setup; falling open without a probe block.`;
  }
  return { ...base, probe, output: [base.output, note].filter(Boolean).join('\n') };
}

// ── Stale-command self-heal ──────────────────────────────────────────────────
// When the stored build_config test command turns out to be STALE — it ran but
// executed no real tests (placeholder / zero-collection / missing script /
// dropped Makefile target) — re-run detection IGNORING the stored config and,
// if a DIFFERENT real suite is present in the already-cloned workspace, run THAT
// and use its verdict. CONSERVATIVE: returns a recovered verdict ONLY when a
// replacement suite genuinely executed real tests. A placeholder/empty suite or
// no replacement at all returns null so the caller keeps its detected=false
// fall-open behaviour, but a replacement-suite setup failure is surfaced so the
// caller can route it through the same FAILED/setupFailed retry semantics as
// the primary detected-suite path. Zero GitHub-API cost — auto-detection
// inspects only the local filesystem.
async function recoverFromStaleCommand(
  ws: WebSocket | null, statusRef: { value: string },
  model: string | undefined, effort: string | undefined,
  buildConfig: BuildConfig | undefined, storedRunners: TestRunner[],
): Promise<StaleCommandRecovery> {
  const autoRunners = detectAutoTestRunners();
  // Never re-invoke a stored runner we already know is dead: drop any
  // auto-detected runner whose command+args+cwd matches a stored one.
  const sameRunner = (a: TestRunner, b: TestRunner): boolean =>
    a.cmd === b.cmd && a.cwd === b.cwd &&
    a.args.length === b.args.length && a.args.every((arg, i) => arg === b.args[i]);
  const replacement = autoRunners.filter(
    cand => !storedRunners.some(stored => sameRunner(cand, stored)),
  );
  if (replacement.length === 0) {
    sendLog(ws, 'Stale stored command, but auto-detection found no DIFFERENT replacement suite — keeping detected=false fall-open.\n');
    return null;
  }

  sendLog(ws, `Stored test command appears stale — auto-detected replacement suite(s): ${replacement.map(r => r.label).join(', ')}\n`);
  statusRef.value = 'Running auto-detected replacement suite (stale-command recovery)...';
  const loop = await runVerifyLoop(
    ws, statusRef, model, effort, buildConfig,
    replacement,
    'replacement suite',
  );

  if (loop.setupFailed) {
    sendLog(ws, 'Replacement suite setup failed after bounded retries — surfacing setupFailed to caller.\n');
    return { kind: 'setupFailed', loop };
  }

  // Only a REAL run counts as recovery. Another placeholder/empty suite
  // (noTestsExecuted) is NOT a recovery.
  if (loop.noTestsExecuted) {
    sendLog(ws, 'Replacement suite did not execute real tests (placeholder/empty) — keeping detected=false fall-open.\n');
    return null;
  }

  const passed = loop.passed;
  const message = passed ? 'suite passed' : 'test failures detected';
  sendLog(ws, `Stale-command recovery: replacement suite ${passed ? 'PASSED' : 'FAILED'} — reporting recovered=true.\n`);
  return {
    kind: 'recovered',
    result: {
      detected: true,
      passed,
      recovered: true,
      recovered_directories: replacement.map(describeRunnerDirectory),
      tests: replacement.map(r => ({
        name: r.label,
        status: passed ? 'pass' : 'fail',
        message: passed ? '' : message,
      })),
      output: passed ? '' : (loop.failureSummary ?? '').slice(-VERIFY_TESTS_OUTPUT_LIMIT),
    },
  };
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
    statusRef.value = 'Running cheap acceptance probe (no suite detected)...';
    result = await attachAcceptanceProbe(ws, buildConfig, {
      detected: false,
      passed: false,
      tests: [],
      output: 'No test runner detected for this repository; merge gate has no suite to run.',
      noTestsReason: 'no_suite',
    });
  } else {
    sendLog(ws, `Detected test runner(s): ${runners.map(r => r.label).join(', ')}\n`);
    statusRef.value = 'Running detected test suite...';
    // Single no-fix run: maxFixAttempts 0 means testFixLoop runs each runner
    // once and returns a verdict without ever invoking Claude to repair code.
    const loop = await runVerifyLoop(
      ws, statusRef, model, effort, buildConfig,
      undefined,
      'detected suite',
    );

    if (loop.setupFailed) {
      // Setup still failing after the bounded retries. We genuinely could not
      // run an existing suite due to infrastructure — NOT a no-tests repo.
      // Post FAILED (keeping the diagnostic artifact) so the orchestrator
      // routes this through its existing one-shot crash-retry path (re-spawn
      // the verify task) before any UNVERIFIED downgrade, instead of treating
      // it as detected=false and failing open immediately.
      const setupResult: VerifyTestsResult = {
        detected: false,
        passed: false,
        setupFailed: true,
        tests: [],
        output: (loop.failureSummary ?? 'Test runner setup failed.').slice(-VERIFY_TESTS_OUTPUT_LIMIT),
      };
      const setupResultPath = `/tmp/verify_tests_result_${taskId}.json`;
      try {
        fs.writeFileSync(setupResultPath, JSON.stringify(setupResult, null, 2));
      } catch (e) {
        sendLog(ws, `[WARNING] Could not write ${setupResultPath}: ${e instanceof Error ? e.message : String(e)}\n`);
      }
      sendLog(ws, `\nTest verification could not run the suite (setup failed after ${MAX_VERIFY_SETUP_RETRIES} retries) — reporting FAILED so the merge gate retries the gate before falling open.\n`);
      await persistAndPostStatus(taskId, {
        task_id: taskId,
        status: 'FAILED',
        artifact_type: 'MERGE_TEST_RESULT',
        logs: sanitize(JSON.stringify(setupResult)),
        repo_url: `https://github.com/${owner}/${repo}`,
      });
      return;
    } else if (loop.noTestsExecuted) {
      // A runner ran but no real tests executed — the stored command was a
      // placeholder (e.g. the default `npm` "no test specified" script), the
      // suite collected zero tests, OR the stored command itself is gone
      // (missing script / dropped Makefile target — funnelled here by
      // classifyTestExecution). Before falling open, try to SELF-HEAL: the
      // stored command may simply be STALE while the cloned repo still carries
      // a real, runnable suite at a different path/command. Re-detect ignoring
      // the stored config and run that replacement if found.
      const recovered = await recoverFromStaleCommand(ws, statusRef, model, effort, buildConfig, runners);
      if (recovered?.kind === 'recovered') {
        result = recovered.result;
      } else if (recovered?.kind === 'setupFailed') {
        const setupResult: VerifyTestsResult = {
          detected: false,
          passed: false,
          setupFailed: true,
          tests: [],
          output: (recovered.loop.failureSummary ?? 'Test runner setup failed.').slice(-VERIFY_TESTS_OUTPUT_LIMIT),
        };
        const setupResultPath = `/tmp/verify_tests_result_${taskId}.json`;
        try {
          fs.writeFileSync(setupResultPath, JSON.stringify(setupResult, null, 2));
        } catch (e) {
          sendLog(ws, `[WARNING] Could not write ${setupResultPath}: ${e instanceof Error ? e.message : String(e)}\n`);
        }
        sendLog(ws, `\nStale-command recovery found a replacement suite, but it still failed setup after ${MAX_VERIFY_SETUP_RETRIES} retries — reporting FAILED so the merge gate retries instead of falling open.\n`);
        await persistAndPostStatus(taskId, {
          task_id: taskId,
          status: 'FAILED',
          artifact_type: 'MERGE_TEST_RESULT',
          logs: sanitize(JSON.stringify(setupResult)),
          repo_url: `https://github.com/${owner}/${repo}`,
        });
        return;
      } else {
        // No real replacement suite — this is effectively a no-detectable-tests
        // repo: report detected=false so the backend routes it to the
        // documented proceed-without-checks fallback rather than recording a
        // hard failure (placeholder exits non-zero) or a vacuous pass (empty
        // suite exits zero).
        const allSkipped = loop.noTestsReason === 'all_skipped';
        sendLog(ws, `Detected suite ran no real tests (${allSkipped ? 'all tests skipped' : 'placeholder/empty'}) and no replacement found — reporting detected=false (backend will fall back).\n`);
        statusRef.value = 'Running cheap acceptance probe (suite ran no real tests)...';
        result = await attachAcceptanceProbe(ws, buildConfig, {
          detected: false,
          passed: false,
          tests: [],
          output: allSkipped
            ? 'Detected test suite collected tests but executed none — every test was skipped/deselected/ignored; no real tests executed; falling back to proceed-without-checks.'
            : 'Detected test command ran no real tests (placeholder or empty suite) — no real tests executed; falling back to proceed-without-checks.',
          noTestsReason: allSkipped ? 'all_skipped' : 'no_real_tests',
        });
      }
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
    ? `Test verification ${result.passed ? 'PASSED' : 'FAILED'} (${result.tests.length} runner(s)${result.recovered ? ', stale-command recovery' : ''})`
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
