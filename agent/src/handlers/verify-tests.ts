/**
 * Verify tests handler — run test suite on a PR and report pass/fail.
 *
 * Checks out a PR branch, auto-detects the build config and test runners,
 * executes the test suite, captures results, and posts a structured pass/fail
 * verdict as a comment on the PR.
 *
 * Does not modify code; used for continuous integration checks and to validate
 * that PRs don't break existing tests. Useful for human-reviewed PRs or to gate
 * merges on test success before entering the merge queue.
 */

import fs from 'fs';
import type WebSocket from 'ws';
import {
  WORKSPACE, strVal, sanitize, sendLog, validateBranchName,
  persistAndPostStatus, resolveBaseBranch, execFileAsync,
} from '../index.js';
import { setPromptSizeTelemetry } from '../telemetry.js';
import { prepareWorkspaceForRole } from '../workspacePrep.js';
import { DEFAULT_CLONE_DEPTH, GENERIC_PROBE_TIMEOUT, DECLARED_BUILD_TIMEOUT, TEST_RUNNER_TIMEOUT } from '../constants.js';
import { resolveBuildConfig } from '../build-config.js';
import {
  detectTestRunners, detectAutoTestRunners, describeRunnerDirectory,
  detectAcceptanceProbes, classifyProbeOutcome, detectProvisioningSteps,
  buildWroteNewOutput,
  discoverDocumentedCommands, runnerFromCommand, MAX_DOCUMENTED_COMMANDS,
  discoverNewlyDisabledTests, MAX_DISABLED_TEST_MARKERS,
  buildAcceptanceTestRunner, classifyAcceptanceTestRun, acceptanceTestRunPassed, matchesEnvironmentFailureSignature,
  type BuildConfig, type TestRunner, type AcceptanceProbe, type DocumentedCommand,
  type DeclaredAcceptanceTest,
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
  // Best-effort pass/skip/total coverage summary parsed from the runner output
  // on the path where a real suite was DETECTED and RAN (both the passed and
  // failed branches). Lets the backend / task drawer surface a green PASS that
  // exercised only a tiny fraction of its suite (e.g. "5 passed, 1500 skipped")
  // instead of an indistinguishable "PASSED". ADDITIVE + visibility-only: a high
  // skip ratio never changes detected/passed and never blocks the merge. Omitted
  // entirely when the counts could not be reliably parsed (extractTestCoverage
  // returned null) and on every detected=false / setup-failure path, so older
  // consumers that ignore it are byte-for-byte unaffected.
  coverage?: { passed: number; skipped: number; total: number; line_coverage_pct?: number; changed_lines_coverage_pct?: number };
  // Cheap goal-level acceptance probe. Attached on TWO paths:
  //   - the detected=false fall-open paths (no suite / thin / all-skipped) and
  //     the setup-failure path, where it may carry EITHER the operator's declared
  //     build_cmd OR a generic ecosystem compile check (tsc/compileall/…); and
  //   - the detected=true + PASSED path, where it carries the operator's DECLARED
  //     build_cmd ONLY (no generic compile probes) — a green unit suite must not
  //     mask a broken declared build (bundled frontends, tests that run against
  //     source without the build step). Gives the no-CI merge gate at least one
  //     honest "does the deliverable still build?" signal even when a passing
  //     suite produced none for the declared build. CONSERVATIVE: a probe that
  //     could not run (missing deps / unprovisioned setup) degrades to a fall-open
  //     and never blocks; only a clear compile/build FAILURE blocks the merge. The
  //     backend (ci_resolution.classify_merge_test_verdict) keys off
  //     declared/ran/passed/could_not_run to decide block-vs-fall-open. Additive:
  //     absent on a detected=true verdict with no declared build_cmd and on the
  //     setup-failure FAILED path's no-probe case.
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
    // ADDITIVE honesty signal mirroring ``probe.wrote_no_artifact`` for the
    // declared-build path: set ONLY for a PASSED, BUILD-INTENT documented command
    // (the discovery layer's ``heavy`` flag) whose working tree we could
    // confidently observe before AND after the run — true when the command exited
    // 0 yet wrote NO new output (a likely no-op build/generate). Left UNSET for
    // non-build commands (lint/test/typecheck correctly produce no artifact), when
    // the tree was unobservable, and on every other outcome — so the backend
    // degrades to ``documented_command_passed`` rather than emitting the no-op
    // reason on uncertainty.
    wrote_no_artifact?: boolean;
  };
  // Diff-aware VISIBILITY signal for the inverse anti-pattern of the documented-
  // command probe: a PR that makes a red suite green by DISABLING tests rather
  // than fixing them. Counts the test-skip/disable markers (``it.skip``,
  // ``@pytest.mark.skip``, ``t.Skip(``, ``#[ignore]``, ``@Disabled`` …) the PR's
  // OWN diff NEWLY adds to TEST files — pre-existing legitimate skips are
  // ignored. VISIBILITY-FIRST + ADDITIVE: it NEVER changes ``passed``/the merge
  // decision; it only surfaces "this PR disables N test(s)" to the operator.
  // Populated ONLY when at least one newly-added marker is found; OMITTED
  // entirely when the count is 0 OR the diff against the base ref cannot be
  // computed — so a PR that introduces no new skips serializes byte-for-byte as
  // today and an older backend that ignores the field is unaffected.
  disabledTests?: {
    count: number;
    markers: Array<{ file: string; marker: string; line: string }>;
  };
  // Declared acceptance-test probe. Attached ONLY when the implementation task
  // declared, at plan time, a named acceptance test (``acceptance_evidence`` with
  // ``kind="test"``) — the test that should exercise the seam end-to-end. The
  // gate runs that SPECIFIC test (targeted) and reports whether it actually
  // executed in the PR's run. A declared test that is ABSENT (not found) or
  // SKIPPED (collected but every match skipped/deselected/xfail'd) did NOT prove
  // the claim — the backend (ci_resolution.acceptance_test_is_clear_failure)
  // flips this to a HIGH-precedence merge BLOCK, mirroring how
  // compute_experiment fails closed when its declared metric is absent. A check
  // that itself ``could_not_run`` (no targetable pytest suite / environment
  // failure) degrades to a fall-open carrying the operator-visible
  // ``acceptance_test_not_run`` reason rather than a hard block. ADDITIVE +
  // CONSERVATIVE: omitted entirely when no acceptance test was declared (or the
  // declared kind is not ``test``), so a PR with no declaration serializes
  // byte-for-byte as today and an older backend that ignores the field is
  // unaffected.
  acceptanceTest?: {
    declared: boolean;
    descriptor: string;
    selector?: string;
    // The declared test was found/collected (true for ``ran``/``skipped``,
    // false for ``absent``).
    present: boolean;
    // The declared test actually executed (true ONLY for ``ran``).
    ran: boolean;
    status: 'ran' | 'skipped' | 'absent' | 'could_not_run';
    // The targeted run was GREEN — true ONLY when the runner exited zero AND the
    // classified status is ``ran``. Because the run targets ONLY the declared
    // test, an exit-zero ``ran`` proves the declared test is passing; any thrown/
    // non-zero exit (a failing test), or a ``skipped``/``absent``/``could_not_run``
    // status, yields false. Additive: a backend that does not read it is
    // unaffected; the sibling backend change consumes it to mint ``proven``.
    passed: boolean;
  };
}

// Computed-once view of the PR head's diff against its base: changed files plus
// the added (``+``) lines, attributed per-file. SHARED by the documented-command
// probe and the newly-disabled-tests probe so the gate runs the `git diff`
// passes ONCE. Null when the diff cannot be computed (base ref unavailable).
interface PrDiff {
  changedFiles: string[];
  addedLines: string[];
  addedLinesByFile: Record<string, string[]>;
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
  changedLineCounts?: Record<string, number>,
): Promise<TestFixLoopResult> {
  let loop = await testFixLoop(
    ws, statusRef, model, effort, buildConfig,
    undefined, undefined, undefined,
    { maxFixAttempts: 0, runnersOverride, changedLineCounts },
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
      { maxFixAttempts: 0, runnersOverride, changedLineCounts },
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
  opts: { declaredOnly?: boolean } = {},
): Promise<VerifyTestsResult['probe']> {
  // declaredOnly restricts the run to the operator's OWN declared build_cmd
  // probe(s) — used on the passing-suite path where a green suite already
  // exercised the source, so only the authoritative declared build is worth
  // adding (never the generic tsc/compileall/go-build/cargo-check probes).
  let candidates = detectAcceptanceProbes(buildConfig);
  if (opts.declaredOnly) candidates = candidates.filter(p => p.declared);
  const probes = candidates.slice(0, MAX_ACCEPTANCE_PROBES);
  if (probes.length === 0) {
    if (!opts.declaredOnly) {
      sendLog(ws, 'Acceptance probe: no probeable ecosystem detected — no goal-level probe signal.\n');
    }
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

// Run a DECLARED-build-only acceptance probe on the detected=true + PASSED path
// and fold its verdict into the result via the existing ``probe`` field. Unlike
// attachAcceptanceProbe (the fall-open path), this filters to the operator's OWN
// declared ``build_cmd`` — generic ecosystem compile probes (tsc/compileall/go
// build/cargo check) are NOT run, because a green suite already exercised the
// source; we only add the authoritative "does the deliverable build" claim. The
// backend reads probe.passed=false on a CLEAR declared-build failure and flips
// the gate to a block. CONSERVATIVE + ADDITIVE: no declared build_cmd → no probe
// → the pass is byte-for-byte unchanged; a could-not-run declared build (missing
// deps / unprovisioned setup) falls open without blocking; only a clear declared-
// build FAILURE blocks. NEVER throws — any error degrades to the unchanged pass.
async function attachDeclaredBuildProbe(
  ws: WebSocket | null,
  buildConfig: BuildConfig | undefined,
  base: VerifyTestsResult,
): Promise<VerifyTestsResult> {
  let probe: VerifyTestsResult['probe'];
  try {
    probe = await runAcceptanceProbe(ws, buildConfig, { declaredOnly: true });
  } catch (e) {
    sendLog(ws, `Declared build probe errored — skipping (suite passed): ${e instanceof Error ? e.message : String(e)}\n`);
    return base;
  }
  if (!probe) return base;
  let note: string;
  if (probe.outcome === 'fail') {
    note = `We ran your declared build command \`${probe.label}\` and it FAILED — the unit suite passed but the declared build did not succeed; blocking the merge.`;
  } else if (probe.outcome === 'pass') {
    note = probe.wrote_no_artifact === true
      ? `We ran your declared build command \`${probe.label}\` and it exited cleanly but wrote no artifact — possible no-op build (unit suite passed).`
      : `We ran your declared build command \`${probe.label}\` and it passed — your declared build succeeds (unit suite passed).`;
  } else {
    note = `We ran your declared build command \`${probe.label}\` but it could not run — missing deps / unprovisioned setup; not blocking (unit suite passed).`;
  }
  return { ...base, probe, output: [base.output, note].filter(Boolean).join('\n') };
}

// ── Diff-aware documented / PR-introduced command probe ──────────────────────
// Cap on commands actually RUN (the discovery layer is already bounded to the
// same constant). Each runs ONCE under a bounded timeout, no retries.
const MAX_DOCUMENTED_COMMAND_RUNS = MAX_DOCUMENTED_COMMANDS;

// Filter detected runners to only those in directories touched by the PR diff.
// Root runners (cwd == workspace root) always run. Falls back to all runners
// when diff is null (unavailable) or when every runner would be filtered out.
function filterRunnersByDiff(
  runners: TestRunner[],
  diff: PrDiff | null,
  ws: WebSocket | null,
): TestRunner[] {
  if (!diff || diff.changedFiles.length === 0) return runners;
  const changedDirs = new Set(diff.changedFiles.map(f => f.split('/')[0]));
  // Use the env var directly so this works correctly even when the module-level
  // WORKSPACE constant has been overridden in test environments.
  const wsRoot = process.env.AGENT_WORKSPACE || WORKSPACE;
  const filtered = runners.filter(r => {
    const relDir = r.cwd.startsWith(wsRoot + '/') ? r.cwd.slice(wsRoot.length + 1) : '';
    // Root runners (path '') always run; directory runners run only when touched.
    return relDir === '' || changedDirs.has(relDir.split('/')[0]);
  });
  if (filtered.length === 0) return runners;
  const skipped = runners.filter(r => !filtered.includes(r));
  if (skipped.length > 0) {
    sendLog(ws, `Diff-aware runner filter: skipping ${skipped.map(r => r.label).join(', ')} (no files changed in their directories).\n`);
  }
  return filtered;
}

// Compute the PR head's changed files + added lines against its base ONCE, so
// both diff-aware probes (documented-command + newly-disabled-tests) share a
// single pair of `git diff` passes. Three-dot diff uses the merge-base, so only
// the PR's own additions count. Returns null when the base ref is unavailable or
// the diff can't be computed — callers then degrade silently (omit their field).
async function computePrDiff(
  ws: WebSocket | null,
  baseBranch: string,
): Promise<PrDiff | null> {
  const baseRef = `origin/${baseBranch}`;
  const addedLinesByFile: Record<string, string[]> = {};
  try {
    // Early health check: verify git and base ref are available before computing full diff.
    // Fails fast if the repo is not properly initialized (e.g., shallow clone without origin).
    await execFileAsync('git', ['rev-parse', baseRef], { cwd: WORKSPACE, timeoutMs: GENERIC_PROBE_TIMEOUT });

    const namesOut = await execFileAsync('git', ['diff', '--name-only', `${baseRef}...HEAD`], { cwd: WORKSPACE, timeoutMs: GENERIC_PROBE_TIMEOUT });
    const changedFiles = namesOut.split(/\r?\n/).map(s => s.trim()).filter(Boolean);
    const diffOut = await execFileAsync('git', ['diff', '--unified=0', `${baseRef}...HEAD`], { cwd: WORKSPACE, timeoutMs: GENERIC_PROBE_TIMEOUT });
    const addedLines: string[] = [];
    // Attribute each added (``+``) line to the file its hunk belongs to, tracked
    // by the ``+++ b/<path>`` header, so discovery can judge "newly introduced"
    // per-file rather than against the whole PR's added lines.
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
    return { changedFiles, addedLines, addedLinesByFile };
  } catch {
    sendLog(ws, `Diff-aware probe: could not compute PR diff against ${baseRef} — skipping (fall-open).\n`);
    return null;
  }
}

// Discover the small allowlisted set of commands the PR INTRODUCES/DOCUMENTS from
// the precomputed diff, run each under a bounded timeout, and reduce to a single
// verdict mirroring the acceptance-probe block (fail-fast: a clean command
// FAILURE blocks; otherwise a PASS wins over a could-not-run; all-could-not-run
// degrades to could_not_run). Returns undefined when there is nothing to run (no
// discovered command) — the field is then simply absent and the gate unchanged.
async function runDocumentedCommandsProbe(
  ws: WebSocket | null,
  diff: PrDiff,
): Promise<VerifyTestsResult['documentedCommands']> {
  const { changedFiles, addedLines, addedLinesByFile } = diff;
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
  // The no-op observation for the winning PASS command: undefined = not
  // confidently observed (degrade to documented_command_passed); boolean =
  // observed. Only a BUILD-INTENT (``heavy``) command can set it.
  let firstPassWroteNoArtifact: boolean | undefined;
  let firstCouldNotRun: DocumentedCommand | null = null;
  for (const cmd of discovered) {
    const runner = runnerFromCommand(cmd.command, cmd.cwd);
    const timeoutMs = cmd.heavy ? DECLARED_BUILD_TIMEOUT : GENERIC_PROBE_TIMEOUT;
    // For a BUILD-INTENT command that could still WIN the verdict (no earlier
    // pass yet), snapshot the tree BEFORE it runs so a clean exit that wrote
    // nothing is distinguishable from a real build — mirroring the declared-build
    // no-op detection. Non-build commands (``heavy`` false: lint/test/typecheck/
    // ``--help``) correctly produce no artifact and are NEVER snapshotted. A
    // failed snapshot leaves the observation unavailable (flag stays unset).
    const before = cmd.heavy && firstPass === null
      ? await snapshotWorkingTree(runner.cwd)
      : null;
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
    if (outcome === 'pass' && firstPass === null) {
      firstPass = cmd;
      // Only a BUILD-INTENT command with BOTH snapshots observed yields a
      // confident no-op signal; anything else leaves the flag unset.
      if (cmd.heavy && before !== null) {
        const after = await snapshotWorkingTree(runner.cwd);
        if (after !== null) {
          const wroteSomething = buildWroteNewOutput(before, after);
          firstPassWroteNoArtifact = !wroteSomething;
          if (!wroteSomething) {
            sendLog(ws, `Documented-command probe [${cmd.label}]: build command exited cleanly but wrote no new output — possible no-op build.\n`);
          }
        }
      }
    }
    if (outcome === 'could_not_run' && firstCouldNotRun === null) firstCouldNotRun = cmd;
  }
  if (firstPass !== null) {
    return {
      ran: true, outcome: 'pass', passed: true, could_not_run: false,
      output: '', label: `PR-introduced command passed: ${firstPass.label}`,
      ...(firstPassWroteNoArtifact !== undefined
        ? { wrote_no_artifact: firstPassWroteNoArtifact }
        : {}),
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
  diff: PrDiff | null,
): Promise<VerifyTestsResult> {
  if (!diff) return base;
  let block: VerifyTestsResult['documentedCommands'];
  try {
    block = await runDocumentedCommandsProbe(ws, diff);
  } catch (e) {
    sendLog(ws, `Documented-command probe errored — skipping (fall-open): ${e instanceof Error ? e.message : String(e)}\n`);
    return base;
  }
  if (!block) return base;
  let note: string;
  if (block.outcome === 'fail') {
    note = `Documented/PR-introduced command probe FAILED — ${block.label}; blocking the merge.`;
  } else if (block.outcome === 'pass') {
    note = block.wrote_no_artifact === true
      ? `Documented/PR-introduced command probe ran but wrote no artifact — ${block.label}; possible no-op build (cheap goal-level signal; not confident verification).`
      : `Documented/PR-introduced command probe passed — ${block.label} (cheap goal-level signal).`;
  } else {
    note = `Documented/PR-introduced command probe could not run — ${block.label}; falling open without a block.`;
  }
  return { ...base, documentedCommands: block, output: [base.output, note].filter(Boolean).join('\n') };
}

// ── Diff-aware newly-disabled-tests probe (VISIBILITY-ONLY) ──────────────────
// Surface, but NEVER block on, the inverse anti-pattern of the documented-command
// probe: a PR that turns a red suite green by DISABLING tests. Reuses the SAME
// precomputed diff — no extra `git diff` passes. ADDITIVE: the field is populated
// ONLY when the PR's own added lines introduce at least one test-skip/disable
// marker in a TEST file, and OMITTED when none are found OR the diff is
// unavailable. It does NOT touch ``passed``/``outcome`` or the merge decision, so
// a PR that introduces no new skips serializes byte-for-byte as today. NEVER
// throws — any error degrades to the unchanged result.
function attachDisabledTests(
  ws: WebSocket | null,
  base: VerifyTestsResult,
  diff: PrDiff | null,
): VerifyTestsResult {
  if (!diff) return base;
  let markers: ReturnType<typeof discoverNewlyDisabledTests>;
  try {
    markers = discoverNewlyDisabledTests({
      changedFiles: diff.changedFiles,
      addedLinesByFile: diff.addedLinesByFile,
      max: MAX_DISABLED_TEST_MARKERS,
    });
  } catch (e) {
    sendLog(ws, `Newly-disabled-tests probe errored — skipping: ${e instanceof Error ? e.message : String(e)}\n`);
    return base;
  }
  if (markers.length === 0) return base;
  sendLog(ws, `Newly-disabled-tests probe: PR newly disables ${markers.length} test(s) — surfacing for operator visibility (does NOT block).\n`);
  return { ...base, disabledTests: { count: markers.length, markers } };
}

// ── Declared acceptance-test probe ───────────────────────────────────────────
// Read the optional plan-time acceptance-test declaration off the task context.
// Returns a normalized {descriptor, selector?} ONLY for a well-formed
// ``kind="test"`` declaration; null for an absent / malformed / non-test
// declaration (mirroring the backend ``parse_acceptance_evidence`` shape, read
// defensively). NEVER throws.
function parseDeclaredAcceptanceTest(
  task: Record<string, unknown>,
): DeclaredAcceptanceTest | null {
  const ctx = (task as { context?: unknown }).context;
  if (!ctx || typeof ctx !== 'object') return null;
  const ev = (ctx as Record<string, unknown>).acceptance_evidence;
  if (!ev || typeof ev !== 'object') return null;
  const e = ev as Record<string, unknown>;
  if (e.kind !== 'test') return null;
  const descriptor = typeof e.descriptor === 'string' ? e.descriptor.trim() : '';
  if (!descriptor) return null;
  const selector =
    typeof e.selector === 'string' && e.selector.trim() ? e.selector.trim() : undefined;
  return { descriptor, selector };
}

// Run the declared acceptance test (if any) and fold the verdict into the
// result via the additive ``acceptanceTest`` block. Self-contained: it detects
// the runner, runs ONLY the declared test (targeted), and classifies whether it
// executed. A clear ABSENT/SKIPPED is carried on the block for the backend to
// flip into a merge BLOCK; an environment failure or an un-targetable selector
// degrades to ``could_not_run`` (fall-open). NEVER throws — any error degrades
// to the unchanged result. Skipped entirely when no acceptance test was
// declared, so a PR with no declaration is byte-for-byte unchanged.
async function attachAcceptanceTestProbe(
  ws: WebSocket | null,
  buildConfig: BuildConfig | undefined,
  task: Record<string, unknown>,
  base: VerifyTestsResult,
): Promise<VerifyTestsResult> {
  let declared: DeclaredAcceptanceTest | null;
  try {
    declared = parseDeclaredAcceptanceTest(task);
  } catch {
    return base;
  }
  if (!declared) return base;
  const label = declared.selector || declared.descriptor;

  let runner: TestRunner | null = null;
  try {
    runner = buildAcceptanceTestRunner(declared, detectTestRunners(buildConfig));
  } catch {
    runner = null;
  }
  const blockBase = {
    declared: true,
    descriptor: declared.descriptor,
    ...(declared.selector ? { selector: declared.selector } : {}),
  };
  if (!runner) {
    sendLog(ws, `Declared acceptance test [${label}]: no targetable suite — could not run the check (fall-open).\n`);
    const note = `Declared acceptance test \`${label}\` could not be targeted (no runnable suite to execute it) — cannot confirm it ran; falling open as degraded.`;
    return {
      ...base,
      acceptanceTest: { ...blockBase, present: false, ran: false, status: 'could_not_run', passed: false },
      output: [base.output, note].filter(Boolean).join('\n'),
    };
  }

  let output = '';
  let status: 'ran' | 'skipped' | 'absent' | 'could_not_run';
  // Whether the targeted runner invocation completed WITHOUT throwing (exit 0).
  // A thrown/non-zero exit (a failing test, or an environment failure) leaves
  // this false, so the pass determination can never be minted from a red run.
  let cleanExit = false;
  try {
    output = await execFileAsync(runner.cmd, runner.args, { cwd: runner.cwd, timeoutMs: TEST_RUNNER_TIMEOUT });
    cleanExit = true;
    status = classifyAcceptanceTestRun(output);
  } catch (err: unknown) {
    const e = err as Error & { stdout?: string; stderr?: string };
    output = [e.stdout, e.stderr, e.message].filter(Boolean).join('\n');
    // An environment/setup failure (missing runtime, OOM, docker daemon, …) means
    // we could NOT confidently observe whether the test ran — degrade to
    // could_not_run rather than mis-reading a missing tool as an absent test.
    status = matchesEnvironmentFailureSignature(output)
      ? 'could_not_run'
      : classifyAcceptanceTestRun(output);
  }
  const ran = status === 'ran';
  // Because the run targets ONLY the declared test, an exit-zero run classified
  // ``ran`` means the declared test is GREEN. Any thrown/non-zero exit or a
  // non-``ran`` status yields false (see acceptanceTestRunPassed).
  const passed = acceptanceTestRunPassed(cleanExit, status);
  const present = status !== 'absent';
  sendLog(ws, `Declared acceptance test [${label}]: ${status}\n`);
  let note: string;
  if (status === 'ran') {
    note = `Declared acceptance test \`${label}\` ran — confirmed present and executed.`;
  } else if (status === 'skipped') {
    note = `Declared acceptance test \`${label}\` was SKIPPED — the promised test did not actually execute; blocking the merge.`;
  } else if (status === 'absent') {
    note = `Declared acceptance test \`${label}\` is ABSENT — the promised test was not found in the PR's run; blocking the merge.`;
  } else {
    note = `Declared acceptance test \`${label}\` could not be run — cannot confirm it ran; falling open as degraded.`;
  }
  return {
    ...base,
    acceptanceTest: { ...blockBase, present, ran, status, passed },
    output: [base.output, note].filter(Boolean).join('\n'),
  };
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
      ...(loop.coverage || loop.line_coverage_pct !== undefined || loop.changed_lines_coverage_pct !== undefined ? {
        coverage: {
          ...(loop.coverage ? loop.coverage : { passed: 0, skipped: 0, total: 0 }),
          ...(loop.line_coverage_pct !== undefined ? { line_coverage_pct: loop.line_coverage_pct } : {}),
          ...(loop.changed_lines_coverage_pct !== undefined ? { changed_lines_coverage_pct: loop.changed_lines_coverage_pct } : {}),
        },
      } : {}),
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
  // No direct Claude invocation at this handler level — record a zero-byte
  // prompt so the task type reports a non-null prompt_size_bytes in its
  // api_call_summary. (testFixLoop internally may invoke Claude for fix
  // attempts with maxFixAttempts>0, but this handler always passes 0.)
  setPromptSizeTelemetry(0, 0, []);

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
  // earlyPrDiff is set only on the detected-suite path (where we need it to
  // filter runners). On the no-suite path it stays null and the shared diff
  // computation at the bottom of the function covers both probes.
  let earlyPrDiff: PrDiff | null = null;
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
    // Compute the PR diff early so we can filter runners to only the directories
    // touched by this PR. Fall back to all runners when the diff is unavailable.
    earlyPrDiff = await computePrDiff(ws, baseBranch);
    const activeRunners = filterRunnersByDiff(runners, earlyPrDiff, ws);
    sendLog(ws, `Detected test runner(s): ${activeRunners.map(r => r.label).join(', ')}\n`);
    statusRef.value = 'Running detected test suite...';
    // Compute changed-line counts from the PR diff for diff-aware coverage
    // measurement.  Null-safe: without the diff, total-coverage is used.
    const changedLineCounts = earlyPrDiff
      ? Object.fromEntries(
          Object.entries(earlyPrDiff.addedLinesByFile).map(
            ([f, lines]) => [f, lines.length],
          ),
        )
      : undefined;
    // Single no-fix run: maxFixAttempts 0 means testFixLoop runs each runner
    // once and returns a verdict without ever invoking Claude to repair code.
    const loop = await runVerifyLoop(
      ws, statusRef, model, effort, buildConfig,
      activeRunners,
      'detected suite',
      changedLineCounts,
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
        tests: activeRunners.map(r => ({
          name: r.label,
          status: passed ? 'pass' : 'fail',
          message: passed ? '' : message,
        })),
        // On the passed branch output is intentionally discarded; coverage was
        // parsed from the raw runner output inside the loop BEFORE it was
        // dropped, so the partial-skip signal survives. Omitted when null.
        ...(loop.coverage || loop.line_coverage_pct !== undefined || loop.changed_lines_coverage_pct !== undefined ? {
          coverage: {
            ...(loop.coverage ? loop.coverage : { passed: 0, skipped: 0, total: 0 }),
            ...(loop.line_coverage_pct !== undefined ? { line_coverage_pct: loop.line_coverage_pct } : {}),
            ...(loop.changed_lines_coverage_pct !== undefined ? { changed_lines_coverage_pct: loop.changed_lines_coverage_pct } : {}),
          },
        } : {}),
        output: passed ? '' : (loop.failureSummary ?? '').slice(-VERIFY_TESTS_OUTPUT_LIMIT),
      };
      // A green suite must not mask a broken DECLARED build. On the passing
      // path, still exercise the operator's OWN declared build_cmd as a
      // goal-level acceptance probe (declared-only — no generic compile probes);
      // the backend blocks the merge only on a CLEAR declared-build failure.
      if (passed) {
        statusRef.value = 'Running declared build probe (suite passed)...';
        result = await attachDeclaredBuildProbe(ws, buildConfig, result);
      }
    }
  }

  // Diff-aware probes of what the PR's OWN diff INTRODUCES. Both run on the
  // COMPLETED path REGARDLESS of whether a unit suite was detected (the early
  // setup-failure FAILED paths return above and are intentionally skipped). The
  // PR diff is computed ONCE and shared by both probes. Each is additive: when
  // the diff introduces nothing relevant, or the base ref is unavailable, the
  // field is simply absent and the verdict is unchanged.
  const prDiff = earlyPrDiff ?? await computePrDiff(ws, baseBranch);
  result = await attachDocumentedCommandsProbe(ws, result, prDiff);
  result = attachDisabledTests(ws, result, prDiff);
  // Declared acceptance-test probe: when the implementation task promised a named
  // test, confirm that SPECIFIC test actually ran in the PR. Runs on every
  // completed path (the early setup-failure FAILED returns above skip it); a
  // no-op when no acceptance test was declared.
  statusRef.value = 'Confirming declared acceptance test ran...';
  result = await attachAcceptanceTestProbe(ws, buildConfig, task, result);

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
