import fs from 'fs';
import type WebSocket from 'ws';
import {
  WORKSPACE, strVal, sanitize, sendLog, validateBranchName,
  persistAndPostStatus, resolveBaseBranch, execFileAsync,
} from '../index.js';
import { prepareWorkspaceForRole } from '../workspacePrep.js';
import { DEFAULT_CLONE_DEPTH, GENERIC_PROBE_TIMEOUT, DECLARED_BUILD_TIMEOUT, TEST_RUNNER_TIMEOUT } from '../constants.js';
import { resolveBuildConfig } from '../build-config.js';
import {
  detectTestRunners, detectAutoTestRunners, describeRunnerDirectory,
  detectAcceptanceProbes, classifyProbeOutcome, detectProvisioningSteps,
  buildWroteNewOutput,
  discoverDocumentedCommands, runnerFromCommand, MAX_DOCUMENTED_COMMANDS,
  type BuildConfig, type TestRunner, type AcceptanceProbe, type DocumentedCommand,
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
  // Set when SOME-but-not-all test runners failed setup/environment (a polyglot
  // repo where e.g. the Python suite ran but the TS runner's `npm ci` failed).
  // Distinct from setupFailed (ALL runners failed): a sibling runner ran, so the
  // suite is only PARTIALLY verified. The backend
  // (ci_resolution.classify_merge_test_verdict) must NOT treat this as a clean
  // pass / undetected fall-open — it fails the gate closed in the same spirit as
  // setupFailed while staying distinguishable in the artifact.
  partialSetupFailed?: boolean;
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
    // True when the probe ran the project's OWN declared ``build_cmd`` (the
    // operator's authoritative build) rather than a generic ecosystem compile
    // check. The backend surfaces declared-vs-generic wording from this flag.
    declared?: boolean;
    // ADDITIVE honesty signal, set ONLY for a PASSED declared build probe whose
    // working tree we could confidently observe before AND after the build: true
    // when the build exited 0 yet wrote NO new output to the fresh PR-head clone
    // (a likely no-op build — empty recipe / silent script / generator that
    // writes nowhere). Left UNSET when observation failed or was ambiguous (git
    // unavailable, tree unobservable) so the backend degrades to ``probe_passed``
    // rather than emitting the no-op reason on uncertainty.
    wrote_no_artifact?: boolean;
  };
  // Diff-aware probe of commands the PR INTRODUCES or DOCUMENTS — a newly added
  // package.json script, a newly added Makefile/justfile/Taskfile target, a
  // declared console-script entrypoint, or an allowlisted command in a touched
  // README/docs usage block. Runs REGARDLESS of whether a unit suite was
  // detected (a documented command must be exercised even when the repo already
  // has a passing suite). Mirrors the ``probe`` block shape so the backend can
  // reuse the SAME clear-failure detection (ran && !could_not_run && !passed).
  // ADDITIVE + CONSERVATIVE: absent when the diff introduces/documents nothing
  // runnable, when the base ref is unavailable / the diff can't be computed
  // (degrade to could-not-run fall-open → field omitted), and on every
  // setup-failure FAILED path. A clean command failure → outcome 'fail' (block);
  // a missing tool/dep/unprovisioned setup / spawn / timeout → 'could_not_run'
  // (fall-open); success → 'pass'. An older backend that ignores it sees
  // byte-for-byte today's gate.
  documentedCommands?: {
    ran: boolean;
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
  // Retry on EITHER an all-runners setup failure (setupFailed) or a partial
  // setup failure (partialSetupFailed): both mean a suite did not run because of
  // environment/setup breakage, which may be transient infra. Same bounded
  // budget for both.
  while ((loop.setupFailed || loop.partialSetupFailed) && setupRetries < MAX_VERIFY_SETUP_RETRIES) {
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
// Generic ecosystem probes (tsc, compileall, etc.) run under the cheap baseline
// so the gate stays fast. Declared build commands get a larger budget to allow
// realistic builds (bundling, compiling non-trivial binaries) to complete.
// A small per-run budget caps total probe processes.
const MAX_ACCEPTANCE_PROBES = 4;

// Bounded timeout for the cheap working-tree snapshot taken around a declared
// build probe. `git status` is fast even on a large clone; cap it so a wedged
// git invocation cannot stall the gate — on timeout/error we simply leave the
// no-artifact flag unset (degrade to probe_passed).
const WORKING_TREE_SNAPSHOT_TIMEOUT = 30_000;

// Snapshot the cloned working tree as a `git status --porcelain --ignored`
// string so a before/after diff around a DECLARED build probe can detect a
// no-op build that exits 0 yet writes nothing. `--ignored` is included so build
// output landing in a gitignored directory still counts as an artifact. Returns
// null when the tree cannot be confidently observed (git unavailable, not a
// repo, snapshot errored/timed out) — the caller then leaves wrote_no_artifact
// unset and the backend keeps today's probe_passed reason. CPU-cheap + bounded.
async function snapshotWorkingTree(cwd: string): Promise<string | null> {
  try {
    return await execFileAsync(
      'git', ['status', '--porcelain', '--ignored'],
      { cwd, timeoutMs: WORKING_TREE_SNAPSHOT_TIMEOUT },
    );
  } catch {
    return null;
  }
}

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
  // The no-op observation for the winning declared PASS probe: undefined =
  // not confidently observed (degrade to probe_passed); boolean = observed.
  let firstPassWroteNoArtifact: boolean | undefined;
  let firstCouldNotRun: AcceptanceProbe | null = null;
  for (const probe of probes) {
    let outcome: 'pass' | 'fail' | 'could_not_run';
    let output = '';
    // For a DECLARED build that could still WIN the probe verdict (no earlier
    // pass yet), snapshot the (already prepped) tree BEFORE the build runs so a
    // clean exit that wrote nothing can be distinguished from a real build.
    // Generic compile probes and already-superseded probes are untouched. A
    // failed snapshot leaves the observation unavailable (flag stays unset →
    // probe_passed).
    const before = probe.declared && firstPass === null
      ? await snapshotWorkingTree(probe.cwd)
      : null;
    const timeoutMs = probe.declared ? DECLARED_BUILD_TIMEOUT : GENERIC_PROBE_TIMEOUT;
    try {
      await execFileAsync(probe.cmd, probe.args, { cwd: probe.cwd, timeoutMs });
      outcome = 'pass';
    } catch (err: unknown) {
      const e = err as Error & { stdout?: string; stderr?: string; code?: string; status?: number | null };
      output = [e.stdout, e.stderr, e.message].filter(Boolean).join('\n');
      // Declared build commands are classified authoritatively: a clean non-zero
      // exit is a real build FAILURE (block), not a conservative could-not-run.
      outcome = classifyProbeOutcome(output, e, probe.declared);
    }
    sendLog(ws, `Acceptance probe [${probe.label}]: ${outcome}\n`);
    if (outcome === 'fail') {
      return {
        ran: true, ecosystem: probe.ecosystem, outcome,
        passed: false, could_not_run: false,
        output: output.slice(-VERIFY_TESTS_OUTPUT_LIMIT), label: probe.label,
        declared: Boolean(probe.declared),
      };
    }
    if (outcome === 'pass' && firstPass === null) {
      firstPass = probe;
      // Only a DECLARED build with BOTH snapshots observed yields a confident
      // no-op signal; anything else leaves the flag unset.
      if (probe.declared && before !== null) {
        const after = await snapshotWorkingTree(probe.cwd);
        if (after !== null) {
          const wroteSomething = buildWroteNewOutput(before, after);
          firstPassWroteNoArtifact = !wroteSomething;
          if (!wroteSomething) {
            sendLog(ws, `Acceptance probe [${probe.label}]: declared build exited cleanly but wrote no new output — possible no-op build.\n`);
          }
        }
      }
    }
    if (outcome === 'could_not_run' && firstCouldNotRun === null) firstCouldNotRun = probe;
  }
  if (firstPass !== null) {
    return {
      ran: true, ecosystem: firstPass.ecosystem, outcome: 'pass',
      passed: true, could_not_run: false, output: '', label: firstPass.label,
      declared: Boolean(firstPass.declared),
      ...(firstPassWroteNoArtifact !== undefined
        ? { wrote_no_artifact: firstPassWroteNoArtifact }
        : {}),
    };
  }
  const cnr = firstCouldNotRun as AcceptanceProbe;
  return {
    ran: true, ecosystem: cnr.ecosystem, outcome: 'could_not_run',
    passed: false, could_not_run: true, output: '', label: cnr.label,
    declared: Boolean(cnr.declared),
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
  if (probe.declared) {
    // Declared-build wording: we ran the operator's OWN declared build command.
    if (probe.outcome === 'fail') {
      note = `We ran your declared build command \`${probe.label}\` and it FAILED — the declared build did not succeed; blocking the merge.`;
    } else if (probe.outcome === 'pass') {
      note = probe.wrote_no_artifact === true
        ? `We ran your declared build command \`${probe.label}\` and it exited cleanly but wrote no artifact — possible no-op build (cheap goal-level signal; unit suite still absent/thin).`
        : `We ran your declared build command \`${probe.label}\` and it passed — your declared build succeeds (cheap goal-level signal; unit suite still absent/thin).`;
    } else {
      note = `We ran your declared build command \`${probe.label}\` but it could not run — missing deps / unprovisioned setup; falling open without a probe block.`;
    }
  } else if (probe.outcome === 'fail') {
    note = `Acceptance probe FAILED (${probe.ecosystem}: ${probe.label}) — the changed code does not compile/build; blocking the merge.`;
  } else if (probe.outcome === 'pass') {
    note = `Acceptance probe passed (${probe.ecosystem}: ${probe.label}) — the changed code still compiles/builds (cheap goal-level signal; unit suite still absent/thin).`;
  } else {
    note = `Acceptance probe could not run (${probe.ecosystem}: ${probe.label}) — missing deps / unprovisioned setup; falling open without a probe block.`;
  }
  return { ...base, probe, output: [base.output, note].filter(Boolean).join('\n') };
}

// ── Diff-aware documented / PR-introduced command probe ──────────────────────
// Cap on commands actually RUN (the discovery layer is already bounded to the
// same constant). Each runs ONCE under a bounded timeout, no retries.
const MAX_DOCUMENTED_COMMAND_RUNS = MAX_DOCUMENTED_COMMANDS;

// Compute the PR head's changed files + added lines against its base, discover
// the small allowlisted set of commands the PR INTRODUCES/DOCUMENTS, run each
// under a bounded timeout, and reduce to a single verdict mirroring the
// acceptance-probe block (fail-fast: a clean command FAILURE blocks; otherwise a
// PASS wins over a could-not-run; all-could-not-run degrades to could_not_run).
// Returns undefined when there is nothing to run (no discovered command) or the
// diff cannot be computed (base ref unavailable) — the field is then simply
// absent and the gate is byte-for-byte unchanged.
async function runDocumentedCommandsProbe(
  ws: WebSocket | null,
  baseBranch: string,
): Promise<VerifyTestsResult['documentedCommands']> {
  // Three-dot diff uses the merge-base, so only the PR's own additions count. If
  // the base ref is unavailable or the diff can't be computed, degrade silently.
  const baseRef = `origin/${baseBranch}`;
  let changedFiles: string[];
  let addedLines: string[];
  const addedLinesByFile: Record<string, string[]> = {};
  try {
    const namesOut = await execFileAsync('git', ['diff', '--name-only', `${baseRef}...HEAD`], { cwd: WORKSPACE, timeoutMs: GENERIC_PROBE_TIMEOUT });
    changedFiles = namesOut.split(/\r?\n/).map(s => s.trim()).filter(Boolean);
    const diffOut = await execFileAsync('git', ['diff', '--unified=0', `${baseRef}...HEAD`], { cwd: WORKSPACE, timeoutMs: GENERIC_PROBE_TIMEOUT });
    addedLines = [];
    // Attribute each added (``+``) line to the file its hunk belongs to, tracked
    // by the ``+++ b/<path>`` header, so discovery can judge "newly introduced"
    // per-manifest rather than against the whole PR's added lines.
    let currentFile: string | null = null;
    for (const raw of diffOut.split(/\r?\n/)) {
      const header = raw.match(/^\+\+\+ (?:b\/)?(.+)$/);
      if (header) {
        const path = header[1].trim();
        currentFile = path === '/dev/null' ? null : path;
        continue;
      }
      if (!raw.startsWith('+') || raw.startsWith('+++')) continue;
      const line = raw.slice(1).trim();
      if (!line) continue;
      addedLines.push(line);
      if (currentFile) (addedLinesByFile[currentFile] ??= []).push(line);
    }
  } catch {
    sendLog(ws, `Documented-command probe: could not compute PR diff against ${baseRef} — skipping (fall-open).\n`);
    return undefined;
  }
  if (changedFiles.length === 0) return undefined;

  const readFile = (relPath: string): string | null => {
    try { return fs.readFileSync(`${WORKSPACE}/${relPath}`, 'utf-8'); } catch { return null; }
  };
  const discovered = discoverDocumentedCommands({
    changedFiles, addedLines, addedLinesByFile, readFile, workspace: WORKSPACE, max: MAX_DOCUMENTED_COMMAND_RUNS,
  });
  if (discovered.length === 0) {
    sendLog(ws, 'Documented-command probe: PR introduces/documents no runnable command — skipping.\n');
    return undefined;
  }

  sendLog(ws, `Documented-command probe: running ${discovered.length} PR-introduced/documented command(s): ${discovered.map(d => d.label).join(', ')}\n`);

  let firstPass: DocumentedCommand | null = null;
  let firstCouldNotRun: DocumentedCommand | null = null;
  for (const cmd of discovered) {
    const runner = runnerFromCommand(cmd.command, cmd.cwd);
    const timeoutMs = cmd.heavy ? DECLARED_BUILD_TIMEOUT : GENERIC_PROBE_TIMEOUT;
    let outcome: 'pass' | 'fail' | 'could_not_run';
    let output = '';
    try {
      await execFileAsync(runner.cmd, runner.args, { cwd: runner.cwd, timeoutMs });
      outcome = 'pass';
    } catch (err: unknown) {
      const e = err as Error & { stdout?: string; stderr?: string; code?: string; status?: number | null };
      output = [e.stdout, e.stderr, e.message].filter(Boolean).join('\n');
      // Documented/introduced commands are classified authoritatively (like a
      // declared build): a CLEAN non-zero exit is a real FAILURE; a missing
      // tool/dep/unprovisioned-setup or spawn/timeout degrades to could_not_run.
      outcome = classifyProbeOutcome(output, e, true);
    }
    sendLog(ws, `Documented-command probe [${cmd.label}]: ${outcome}\n`);
    if (outcome === 'fail') {
      return {
        ran: true, outcome: 'fail', passed: false, could_not_run: false,
        output: output.slice(-VERIFY_TESTS_OUTPUT_LIMIT),
        label: `PR-introduced command failed: ${cmd.label}`,
      };
    }
    if (outcome === 'pass' && firstPass === null) firstPass = cmd;
    if (outcome === 'could_not_run' && firstCouldNotRun === null) firstCouldNotRun = cmd;
  }
  if (firstPass !== null) {
    return {
      ran: true, outcome: 'pass', passed: true, could_not_run: false,
      output: '', label: `PR-introduced command passed: ${firstPass.label}`,
    };
  }
  const cnr = firstCouldNotRun as DocumentedCommand;
  return {
    ran: true, outcome: 'could_not_run', passed: false, could_not_run: true,
    output: '', label: `PR-introduced command could not run: ${cnr.label}`,
  };
}

// Run the documented-command probe and fold its verdict into a result. A clear
// FAIL carries documentedCommands.passed=false (the backend flips this to a
// merge block); pass / could-not-run leave the result intact, annotating output
// for the operator. NEVER throws — any error degrades to the unchanged result.
async function attachDocumentedCommandsProbe(
  ws: WebSocket | null,
  base: VerifyTestsResult,
  baseBranch: string,
): Promise<VerifyTestsResult> {
  let block: VerifyTestsResult['documentedCommands'];
  try {
    block = await runDocumentedCommandsProbe(ws, baseBranch);
  } catch (e) {
    sendLog(ws, `Documented-command probe errored — skipping (fall-open): ${e instanceof Error ? e.message : String(e)}\n`);
    return base;
  }
  if (!block) return base;
  let note: string;
  if (block.outcome === 'fail') {
    note = `Documented/PR-introduced command probe FAILED — ${block.label}; blocking the merge.`;
  } else if (block.outcome === 'pass') {
    note = `Documented/PR-introduced command probe passed — ${block.label} (cheap goal-level signal).`;
  } else {
    note = `Documented/PR-introduced command probe could not run — ${block.label}; falling open without a block.`;
  }
  return { ...base, documentedCommands: block, output: [base.output, note].filter(Boolean).join('\n') };
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

  if (loop.setupFailed || loop.partialSetupFailed) {
    sendLog(ws, 'Replacement suite setup failed (fully or partially) after bounded retries — surfacing to caller.\n');
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

// ── Dependency provisioning (run before the suite AND the build probe) ───────
// Each install step runs under the bounded suite-runner timeout (deps install
// can take longer than a cheap compile probe but must stay bounded). A failure
// is retried the same bounded number of times the suite-setup path uses, then
// surfaced so the caller routes it through the EXISTING setup-failure path.
const PROVISION_TIMEOUT = TEST_RUNNER_TIMEOUT;

// Provision the project's declared / auto-detected dependencies into the cloned
// workspace BEFORE the detected suite and the declared build probe run. Returns
// a failure summary when provisioning could not complete (so the caller emits a
// setup failure — NEVER a silent pass / code-fix), or null on success / no-op.
async function provisionDependencies(
  ws: WebSocket | null,
  statusRef: { value: string },
  buildConfig: BuildConfig | undefined,
): Promise<string | null> {
  const steps = detectProvisioningSteps(buildConfig);
  if (steps.length === 0) return null;
  for (const step of steps) {
    for (let attempt = 0; ; attempt++) {
      statusRef.value = `Provisioning dependencies (${step.label})...`;
      sendLog(ws, `Provisioning dependencies: ${step.label}\n`);
      try {
        await execFileAsync(step.cmd, step.args, { cwd: step.cwd, timeoutMs: PROVISION_TIMEOUT });
        break;
      } catch (err: unknown) {
        const e = err as Error & { stdout?: string; stderr?: string };
        const msg = [e.stdout, e.stderr, e.message].filter(Boolean).join('\n');
        if (attempt < MAX_VERIFY_SETUP_RETRIES) {
          sendLog(ws, `Dependency provisioning failed (${step.label}) — retrying (${attempt + 1}/${MAX_VERIFY_SETUP_RETRIES})...\n`);
          continue;
        }
        return `Dependency provisioning failed: [${step.label}]\n${msg}`;
      }
    }
  }
  return null;
}

// Emit the canonical setup-failure verdict (FAILED + setupFailed/partialSetupFailed
// marker). Shared shape across the provisioning, suite, and replacement
// setup-failure paths so they all flow through the SAME merge-gate
// retry-before-fall-open semantics.
//
// Even though the detected suite could NOT run, we still run the cheap
// acceptance probe and carry its verdict on the artifact: a setup-failure
// fall-open otherwise merges UNVERIFIED with ZERO execution signal — the one
// situation the probe exists for. A clear compile/build FAILURE still blocks the
// merge (the backend redirects on a clear-failure probe); a probe that itself
// could not run (the same environment problem) stays distinguishable as
// could_not_run and keeps the fall-open.
async function postVerifySetupFailure(
  taskId: string, owner: string, repo: string,
  ws: WebSocket | null, summary: string,
  buildConfig: BuildConfig | undefined,
  markers: { setupFailed?: boolean; partialSetupFailed?: boolean } = { setupFailed: true },
): Promise<void> {
  const setupResult = await attachAcceptanceProbe(ws, buildConfig, {
    detected: false,
    passed: false,
    setupFailed: markers.setupFailed || undefined,
    partialSetupFailed: markers.partialSetupFailed || undefined,
    tests: [],
    output: summary.slice(-VERIFY_TESTS_OUTPUT_LIMIT),
  });
  const setupResultPath = `/tmp/verify_tests_result_${taskId}.json`;
  try {
    fs.writeFileSync(setupResultPath, JSON.stringify(setupResult, null, 2));
  } catch (e) {
    sendLog(ws, `[WARNING] Could not write ${setupResultPath}: ${e instanceof Error ? e.message : String(e)}\n`);
  }
  await persistAndPostStatus(taskId, {
    task_id: taskId,
    status: 'FAILED',
    artifact_type: 'MERGE_TEST_RESULT',
    logs: sanitize(JSON.stringify(setupResult)),
    repo_url: `https://github.com/${owner}/${repo}`,
  });
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

  // Provision declared / auto-detected dependencies BEFORE the suite and the
  // declared build probe so both run against a real environment. A provisioning
  // failure is an environment/setup failure: route it through the EXISTING
  // setup-failure path (FAILED + setupFailed marker → merge-gate retry), NOT the
  // code-fix loop and NEVER a silent pass.
  const provisionFailure = await provisionDependencies(ws, statusRef, buildConfig);
  if (provisionFailure) {
    sendLog(ws, `\nDependency provisioning failed after bounded retries — reporting FAILED so the merge gate retries the gate before falling open.\n`);
    statusRef.value = 'Running cheap acceptance probe (dependency provisioning failed)...';
    await postVerifySetupFailure(taskId, owner, repo, ws, provisionFailure, buildConfig);
    return;
  }

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

    if (loop.setupFailed || loop.partialSetupFailed) {
      // Setup still failing after the bounded retries. Either EVERY runner
      // failed setup (setupFailed) or SOME-but-not-all did (partialSetupFailed):
      // in both cases at least one suite genuinely could not run due to
      // infrastructure — NOT a no-tests repo. Post FAILED (keeping the
      // diagnostic artifact) so the orchestrator routes this through its
      // existing one-shot crash-retry path (re-spawn the verify task) before any
      // UNVERIFIED downgrade, instead of treating it as detected=false and
      // failing open immediately. The partial case stays distinguishable in the
      // artifact via partialSetupFailed so the backend never reads it as a clean
      // pass.
      sendLog(ws, `\nTest verification could not run the suite (setup failed after ${MAX_VERIFY_SETUP_RETRIES} retries) — reporting FAILED so the merge gate retries the gate before falling open.\n`);
      statusRef.value = 'Running cheap acceptance probe (suite setup failed)...';
      await postVerifySetupFailure(
        taskId, owner, repo, ws,
        loop.failureSummary ?? 'Test runner setup failed.',
        buildConfig,
        { setupFailed: loop.setupFailed || undefined, partialSetupFailed: loop.partialSetupFailed || undefined },
      );
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
        sendLog(ws, `\nStale-command recovery found a replacement suite, but it still failed setup after ${MAX_VERIFY_SETUP_RETRIES} retries — reporting FAILED so the merge gate retries instead of falling open.\n`);
        statusRef.value = 'Running cheap acceptance probe (replacement suite setup failed)...';
        await postVerifySetupFailure(
          taskId, owner, repo, ws,
          recovered.loop.failureSummary ?? 'Test runner setup failed.',
          buildConfig,
          { setupFailed: recovered.loop.setupFailed || undefined, partialSetupFailed: recovered.loop.partialSetupFailed || undefined },
        );
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

  // Diff-aware probe of commands the PR INTRODUCES or DOCUMENTS. Runs on the
  // COMPLETED path REGARDLESS of whether a unit suite was detected (the early
  // setup-failure FAILED paths return above and are intentionally skipped). It
  // is additive: when nothing runnable is introduced/documented, or the base ref
  // is unavailable, the field is simply absent and the verdict is unchanged.
  result = await attachDocumentedCommandsProbe(ws, result, baseBranch);

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
