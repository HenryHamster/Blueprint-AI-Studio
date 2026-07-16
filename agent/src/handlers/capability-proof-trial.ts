/**
 * Capability proof trial handler — Slice 2 of the machine-verified
 * capability-proof earn path (design doc 84021e2d).
 *
 * A `capability_proof_trial` task runs ONLY a working capability's declared
 * convention-named covering tests as a TARGETED, bounded runner job — decoupled
 * from the merge gate — and reports a pass/fail verdict. The backend ingestion
 * completion handler (`_handle_capability_proof_trial_completion`) mints a genuine
 * `proven` named_test proof on a GREEN result and records NOTHING (never demotes)
 * on a RED one.
 *
 * This handler NEVER modifies code and opens no PR. It mirrors the
 * `acceptanceTest` result shape from the verify-tests gate and reuses its
 * `classifyAcceptanceTestRun` / `acceptanceTestRunPassed` classification so a
 * green determination is unambiguous (exit-zero AND the targeted tests actually
 * ran). A ref set that cannot be targeted cleanly (unknown extension, or a mix of
 * languages / packages that no single invocation can run) reports
 * `could_not_run: true` and mints nothing — fully fail-open.
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
  isDockerAvailable,
} from '../utils/test-runners.js';

// Default hard cap on covering test files per trial (mirrors the backend
// MAX_CAPABILITY_PROOF_TRIAL_TEST_FILES). The payload may carry a smaller
// max_test_files; the effective cap is the min of the two.
const DEFAULT_MAX_TEST_FILES = 10;
// Default wall-clock bound (ms) for the whole targeted run when the payload
// carries no wall_clock_seconds. Keeps a mis-declared covering surface from
// running unbounded on the runner.
const DEFAULT_WALL_CLOCK_MS = 900_000;

const TS_JS_EXTS = new Set(['ts', 'tsx', 'js', 'jsx']);

/** The bounded targeted-test result the handler reports back. Mirrors the
 *  verify-tests `acceptanceTest` block shape so the backend reuses the same
 *  green-determination predicate shape. */
export interface CapabilityProofTrialResult {
  type: 'capability_proof_trial';
  cap: string;
  head_sha: string | null;
  /** The SHA queued when the trial was enrolled; retained for drift visibility. */
  queued_head_sha: string | null;
  test_refs: string[];
  ran: boolean;
  passed: boolean;
  skipped_summary?: { total: number; skipped: number };
  could_not_run: boolean;
}

/** The file portion of a test ref (before any `::node` / `#Ln` selector). */
function refFilePart(ref: string): string {
  return ref.split('::')[0].split('#')[0].trim();
}

function refExt(ref: string): string {
  const file = refFilePart(ref);
  const base = file.split('/').pop() || file;
  const dot = base.lastIndexOf('.');
  return dot > 0 ? base.slice(dot + 1).toLowerCase() : '';
}

/** The workspace package a TS/JS ref belongs to (the top-level dir), or '' for
 *  a repo-root ref. Used to pick vitest's cwd and to reject a mix that no single
 *  `npx vitest run` invocation could target. */
function tsPackageDir(ref: string): string {
  const file = refFilePart(ref);
  const top = file.includes('/') ? file.split('/')[0] : '';
  // Known vitest package roots in this repo; anything else runs from repo root.
  return top === 'agent' || top === 'admin-ui' ? top : '';
}

/**
 * Build the TARGETED runner that runs ONLY the passed covering test refs, choosing
 * the command by test-ref extension (the same per-file targeting primitive the
 * verify-tests gate's `buildAcceptanceTestRunner` embodies):
 *   - Python (`.py`) refs → `bash scripts/test-backend.sh <pgFlag> testing/test_<name>.py ...`
 *     (paths normalized to the `backend/`-relative form the script expects).
 *     pgFlag is `--postgres` when Docker is available, `--no-postgres` otherwise —
 *     the same isDockerAvailable() fallback the verify-tests gate uses (test-runners.ts:445)
 *     so trials on Docker-less VMs do not trip the env-failure signature.
 *   - TS/JS refs → `npx vitest run <file> ...` from the owning package dir.
 *
 * Returns `null` when the refs cannot be targeted cleanly in a SINGLE invocation:
 * empty, an unknown extension, or a mix of languages / vitest packages. The caller
 * then reports `could_not_run: true` and mints nothing.
 *
 * `checkDockerAvailable` is injectable for testing (defaults to `isDockerAvailable`).
 */
export function buildCapabilityProofTrialRunner(
  testRefs: string[],
  workspace: string = WORKSPACE,
  checkDockerAvailable: () => boolean = isDockerAvailable,
): { cmd: string; args: string[]; cwd: string } | null {
  const refs = testRefs.filter(r => typeof r === 'string' && r.trim()).map(r => r.trim());
  if (refs.length === 0) return null;

  const exts = [...new Set(refs.map(refExt))];
  // A mix of languages, or any unrecognized extension, cannot be targeted cleanly.
  let lang: 'py' | 'ts' | null = null;
  if (exts.every(e => e === 'py')) lang = 'py';
  else if (exts.every(e => TS_JS_EXTS.has(e))) lang = 'ts';
  if (lang === null) return null;

  if (lang === 'py') {
    // scripts/test-backend.sh expects `backend/`-relative paths (e.g.
    // `testing/test_foo.py`); strip a leading `backend/` from each ref.
    // Pass --postgres when Docker is available so tests run against a real PG
    // sidecar (matching CI); fall back to --no-postgres on Docker-less VMs so
    // the trial does not trip the env-failure signature and mint nothing.
    const pgFlag = checkDockerAvailable() ? '--postgres' : '--no-postgres';
    const args = ['scripts/test-backend.sh', pgFlag];
    for (const ref of refs) {
      args.push(ref.startsWith('backend/') ? ref.slice('backend/'.length) : ref);
    }
    return { cmd: 'bash', args, cwd: workspace };
  }

  // TS/JS: all refs must live in the SAME vitest package so one `npx vitest run`
  // can execute them; a cross-package mix is not cleanly targetable.
  const pkgs = new Set(refs.map(tsPackageDir));
  if (pkgs.size !== 1) return null;
  const pkg = [...pkgs][0];
  const cwd = pkg ? `${workspace}/${pkg}` : workspace;
  const args = ['vitest', 'run'];
  for (const ref of refs) {
    args.push(pkg && ref.startsWith(`${pkg}/`) ? ref.slice(pkg.length + 1) : ref);
  }
  return { cmd: 'npx', args, cwd };
}

/** Best-effort pass/skip summary parsed from pytest / vitest output. Returns
 *  undefined when counts could not be reliably parsed — the backend then treats
 *  the run as verifying (it relies on the authoritative ran/passed flags), rather
 *  than fabricating a fake all-skipped or zero summary. */
export function parseTrialSkippedSummary(
  output: string,
): { total: number; skipped: number } | undefined {
  if (typeof output !== 'string' || !output) return undefined;
  const num = (re: RegExp): number | null => {
    const m = output.match(re);
    return m ? parseInt(m[1], 10) : null;
  };
  const passed = num(/(\d+)\s+passed/i);
  const skipped = num(/(\d+)\s+skipped/i);
  const failed = num(/(\d+)\s+failed/i);
  if (passed === null && skipped === null && failed === null) return undefined;
  const p = passed ?? 0;
  const s = skipped ?? 0;
  const f = failed ?? 0;
  return { total: p + s + f, skipped: s };
}

export async function handleCapabilityProofTrial(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  _model?: string, _effort?: string,
): Promise<void> {
  // No Claude invocation — this is a programmatic targeted-test run.
  setPromptSizeTelemetry(0, 0, []);

  const cap = typeof payload.cap === 'string' ? payload.cap.trim() : '';
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

  const report = async (result: CapabilityProofTrialResult): Promise<void> => {
    await persistAndPostStatus(taskId, {
      task_id: taskId,
      status: 'COMPLETED',
      logs: sanitize(JSON.stringify(result)),
      repo_url: `https://github.com/${owner}/${repo}`,
    });
  };

  // A trial with no cap or no targetable refs is a no-op (mints nothing).
  const runner = buildCapabilityProofTrialRunner(testRefs, WORKSPACE);
  if (!cap || testRefs.length === 0 || runner === null) {
    sendLog(ws, `Capability proof trial: ${!cap ? 'no capability' : testRefs.length === 0 ? 'no test refs' : 'refs not cleanly targetable'} — reporting could_not_run.\n`);
    await report({
      type: 'capability_proof_trial',
      // This path has no checkout, so it intentionally reports only the queued
      // identity rather than claiming it was executed.
      cap, head_sha: headSha, queued_head_sha: headSha, test_refs: testRefs,
      ran: false, passed: false, could_not_run: true,
    });
    return;
  }

  // Clone the repo at the base branch so the covering tests run at HEAD.
  const baseBranch = validateBranchName(resolveBaseBranch(payload, task), 'base_branch');
  statusRef.value = `Cloning ${owner}/${repo} @ ${baseBranch} for capability proof trial...`;
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  await prepareWorkspaceForRole({
    workspace: WORKSPACE, repoUrl: cloneUrl, githubToken: ghToken,
    branch: baseBranch, baseBranch, depth: DEFAULT_CLONE_DEPTH,
  });

  // The base branch can advance after this trial is queued. Proof provenance
  // must name the checkout that ran the tests, never the queued branch head.
  // A failed identity lookup is indeterminate, not permission to echo the
  // queued SHA; test execution and its classification still proceed normally.
  let executedHeadSha: string | null = null;
  try {
    const resolved = await execFileAsync('git', ['rev-parse', 'HEAD'], { cwd: WORKSPACE });
    executedHeadSha = resolved.trim() || null;
  } catch (err: unknown) {
    const message = err instanceof Error ? err.message : String(err);
    sendLog(ws, `Capability proof trial [${cap}]: could not resolve checkout SHA: ${message}\n`);
  }

  statusRef.value = `Running covering tests for capability '${cap}'...`;
  sendLog(ws, `Capability proof trial [${cap}]: ${runner.cmd} ${runner.args.join(' ')} (cwd=${runner.cwd})\n`);

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
  sendLog(ws, `Capability proof trial [${cap}]: status=${status} passed=${passed}\n`);

  await report({
    type: 'capability_proof_trial',
    cap, head_sha: executedHeadSha, queued_head_sha: headSha, test_refs: testRefs,
    ran, passed, could_not_run: couldNotRun,
    ...(skippedSummary ? { skipped_summary: skippedSummary } : {}),
  });
}
