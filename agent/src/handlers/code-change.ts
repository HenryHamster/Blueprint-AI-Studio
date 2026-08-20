import * as fs from 'fs';
import type WebSocket from 'ws';
import {
  sanitize, sendLog, runClaudeCode, persistAndPostStatus, git,
  apiGet, gitAsync, rotateOAuthCredentialsIfChanged,
} from '../index.js';
import { writeGitTokenFile } from '../git-credential.js';
import type { BuildConfig } from '../index.js';
import { writeCodeChangeHelpers, SELF_REVIEW_PROMPT_VERSION } from '../prompt.js';
import {
  buildTaskCheckpointCommitMessage,
  parseTaskCheckpointCommitMessage,
} from '../task-checkpoint.js';
// buildPromptSizeTelemetry / setPromptSizeTelemetry are NOT called here —
// callers (standard.ts, review-fix.ts, specialized.ts) set richer
// per-section decompositions before invoking runCodeChangeTask.

// ── Code-change task runner ─────────────────────────────────────────────────
//
// 2026-05-04: replaces the wrapper-driven lint/test/push pipeline. Claude
// runs in ONE session with full task context, calls ./check itself in a loop
// to converge on a green build, then ./pr open + ./done to ship. The wrapper
// just runs Claude, watches for marker files, and posts terminal status.
//
// Three exit paths:
//   1. Claude calls ./done  → marker /tmp/done_<id> exists; ./done already
//      posted COMPLETED. Wrapper just exits.
//   2. Claude calls ./fail "reason" → /tmp/fail_<id> exists; ./fail posted
//      FAILED with reason. Wrapper exits.
//   3. Claude exits naturally without calling either:
//      a. /tmp/pr_url_<id> exists → Claude opened PR but didn't call ./done.
//         Treat as success; post COMPLETED with the PR URL.
//      b. No PR URL marker → task didn't ship. Post FAILED with allOutput tail
//         so the operator (and downstream investigation) can see what Claude
//         actually did before giving up.
//
// Wall-clock timeout (CLAUDE_TIMEOUT_MS_BY_EFFORT) and result-grace timer
// (CLAUDE_RESULT_GRACE_MS) still apply at the runClaudeCode level — those
// are the hard safety bounds.

export interface CodeChangeTaskOpts {
  taskId: string;
  prompt: string;
  /** Task-specific request/acceptance text, before repository and helper
   *  enrichment. Used by the one-shot continuation so prompt bounding cannot
   *  discard the actual task. Falls back to prompt for legacy callers. */
  taskPrompt?: string;
  ws: WebSocket | null;
  statusRef: { value: string };
  workBranch: string;
  baseBranch: string;
  owner: string;
  repo: string;
  /** When set, Claude is fixing an existing PR (review-fix path). ./pr open
   *  pushes to the existing PR's branch instead of creating a new one. */
  existingPrNumber?: number;
  /** Fix iteration number (1-based). When set alongside existingPrNumber, stamped
   *  on the SR outcome and written to the fix-SR context file so Claude scopes its
   *  self-review to THIS iteration's diff, not the cumulative PR diff. */
  fixIteration?: number;
  /** When true, ./check test runs the Docker backend test suite when changes
   *  touch backend/. Set when baseBranch === promotionTarget (or 'main' when
   *  no promotionTarget is known). */
  testsOnMainBranch: boolean;
  /** Per-project promotion target branch (e.g. 'automain', 'production', 'release/v2').
   *  When set, forwarded to writeCodeChangeHelpers so the generated ./check and ./pr
   *  scripts can name the actual LTS branch rather than assuming 'main'. */
  promotionTarget?: string;
  /** Diff summary captured pre-Claude — used as fallback if Claude exits
   *  without ./done|./fail. */
  baseCommit: string;
  model?: string;
  effort?: string;
  disallowedTools?: string[];
  /** Diff stats already collected; passed through to FAILED status if Claude
   *  exits without shipping. */
  fileChangeDetails?: { file: string; additions: number; deletions: number }[];
  /** Analysis plan passed through to FAILED status for retry context. */
  analysisContext?: string;
  /** Message to post when Claude exits cleanly with no diff (e.g. read-only
   *  task that decided no changes were needed). */
  noChangesMessage?: string;
  /** SR categories whose miss-rate is regressing for this task type.
   *  Forwarded to writeCodeChangeHelpers so ./done enforces a non-trivial
   *  `reasoning` field in check_results for each regressing category. */
  regressingCategories?: string[];
  /** Resolved project build configuration. Forwarded to writeCodeChangeHelpers
   *  so the generated ./check runs the project's configured per-directory
   *  commands instead of the hardcoded Blueprint blocks. */
  buildConfig?: BuildConfig;
  taskCapability?: Partial<Record<'agent_mode' | 'run_shape' | 'output_kind', unknown>>;
  /** Exact pytest node-id selectors CI reported failing (ci_failure fix only).
   *  Forwarded to writeCodeChangeHelpers so the generated ./check verifies
   *  these selectors pass LOCALLY before ./pr open can push — closing the
   *  fix→verify→push loop that previously shipped blind and re-failed in CI.
   *  Empty/undefined for non-ci_failure tasks. */
  ciFailureSelectors?: string[];
  /** A declared `visual` acceptance-evidence requirement (component + viewport
   *  width) for this task. Forwarded to writeCodeChangeHelpers so the generated
   *  ./check captures the component's viewport screenshot at the declared width
   *  on a passing run and uploads it through the signed-URL endpoint — making a
   *  declared visual evidence satisfiable. Best-effort / non-fatal; unset for
   *  tasks with no visual declaration (generated ./check is byte-identical). */
  visualAcceptance?: { component: string; width: number };
  /** When true, this task was auto-decomposed from a larger finding into a
   *  cluster sibling. Combined with relatedFiles + siblingTasks, enables the
   *  SCOPE_EMPTY pre-flight guard (see runCodeChangeTask). */
  autoDecomposed?: boolean;
  /** Files declared in the task scope. Used by the SCOPE_EMPTY guard. */
  relatedFiles?: string[];
  /** Sibling task summaries passed from project_context. Each entry has at
   *  minimum a status field; completed siblings expose their files list so
   *  the guard can check whether all relatedFiles are already covered. */
  siblingTasks?: Array<{ status: string; files?: string[]; title?: string; id?: string }>;
}

/** Read Claude's self-review JSON and ensure prompt_version is stamped.
 *  For fix tasks, also stamps fix_iteration so the SR outcome carries
 *  iteration context for the catch-rate dashboard and reviewer priming. */
function readSelfReviewOutcome(taskId: string, fixIteration?: number): Record<string, unknown> | null {
  const path = `/tmp/self_review_${taskId}.json`;
  try {
    if (!fs.existsSync(path)) return null;
    const raw = fs.readFileSync(path, 'utf-8');
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    if (!('prompt_version' in parsed)) {
      parsed.prompt_version = SELF_REVIEW_PROMPT_VERSION;
    }
    if (fixIteration !== undefined) {
      parsed.fix_iteration = fixIteration;
    }
    return parsed;
  } catch {
    return null;
  }
}

// Mirrors computeRegressionRiskHeuristic from standard.ts — kept local to
// avoid circular import (standard.ts already imports runCodeChangeTask here).
function heuristicRegressionRisk(diff: string): { passed: boolean; issue: string } {
  const lines = diff.split('\n');
  let srcChanged = 0, testChanged = 0, removedExports = 0;
  let currentFile = '';
  for (const line of lines) {
    if (line.startsWith('diff --git ')) { currentFile = line.replace(/^diff --git a\/\S+ b\//, ''); continue; }
    if (line.startsWith('--- ') || line.startsWith('+++ ')) continue;
    const isTest =
      /\/(test|spec|__tests__)\//.test(currentFile) ||
      /\.(test|spec)\.(ts|tsx|js|py)$/.test(currentFile) ||
      /(?:^|\/)test_[^/]+\.py$/.test(currentFile);
    if (/^[+-]/.test(line)) { if (isTest) testChanged++; else srcChanged++; }
    if (line.startsWith('-') &&
        /^-\s*export\s+(?:default\s+)?(?:async\s+)?(?:function|class|const|let|var|type|interface|enum)\s+\w/.test(line)) {
      removedExports++;
    }
  }
  const signals: string[] = [];
  if (srcChanged >= 80 && testChanged === 0) signals.push(`+${srcChanged} source lines / 0 test lines`);
  if (removedExports > 0) signals.push(`${removedExports} export(s) removed`);
  const tag = '[heuristic-derived]';
  if (signals.length === 0) return { passed: true, issue: `${tag} no high-risk signals (${srcChanged} src / ${testChanged} test lines)` };
  return { passed: false, issue: `${tag} ${signals.join('; ')}` };
}

/** Inject a heuristic regression_risk entry when no baseline file exists.
 *  Always returns an object (never null) so the backend can track prompt_version
 *  for every completed task, even when Claude didn't write the SR file. */
export function augmentSelfReview(
  selfReview: Record<string, unknown> | null,
  taskId: string,
  baseCommit: string,
): Record<string, unknown> {
  // Ensure prompt_version is always stamped so compute_sr_catch_rate_trends can
  // bucket by prompt regime even when Claude skipped the self-review step.
  const sr: Record<string, unknown> = selfReview ?? { verdict: 'unknown', check_results: {}, prompt_version: SELF_REVIEW_PROMPT_VERSION };
  if (!('prompt_version' in sr)) sr.prompt_version = SELF_REVIEW_PROMPT_VERSION;
  if (fs.existsSync(`/tmp/test_baseline_${taskId}.json`)) return sr;
  const checkResults = (sr.check_results ?? {}) as Record<string, unknown>;
  if ('regression_risk' in checkResults) return sr;
  try {
    const diff = git(['diff', `${baseCommit}..HEAD`]);
    sr.check_results = { ...checkResults, regression_risk: heuristicRegressionRisk(diff) };
  } catch { /* git unavailable — leave check_results unchanged */ }
  return sr;
}

/** Write a fix-task self-review context file before Claude runs.
 *  Records the per-iteration base commit so Claude scopes scope-drift and
 *  regression_risk checks to THIS iteration's diff, not the cumulative PR diff.
 *  The file is also cleaned up in the stale marker sweep on each run. */
function writeFixSRContext(taskId: string, baseCommit: string, prNumber: number, fixIteration: number): void {
  try {
    fs.writeFileSync(
      `/tmp/fix_sr_context_${taskId}.json`,
      JSON.stringify({ is_fix_task: true, fix_base_commit: baseCommit, pr_number: prNumber, fix_iteration: fixIteration, required_sr_categories: ['regression_risk', 'scope-drift'] }, null, 2),
      { mode: 0o644 },
    );
  } catch { /* non-fatal */ }
}

function workspaceHasChanges(baseCommit: string): boolean {
  try {
    return git(['status', '--porcelain']).length > 0 || git(['rev-parse', 'HEAD']) !== baseCommit;
  } catch {
    return false;
  }
}

function readRemoteBranchSha(workBranch: string): string | null {
  const ref = `refs/heads/${workBranch}`;
  const output = git(['ls-remote', '--heads', 'origin', ref]).trim();
  if (!output) return null;

  const matches = output.split('\n').map(line => line.trim()).filter(Boolean);
  if (matches.length !== 1) {
    throw new Error(`Expected one remote checkpoint ref for ${ref}, got ${matches.length}`);
  }
  const [sha, remoteRef] = matches[0].split(/\s+/, 2);
  if (remoteRef !== ref || !/^[0-9a-f]{40}(?:[0-9a-f]{24})?$/i.test(sha)) {
    throw new Error(`Malformed remote checkpoint ref returned for ${ref}`);
  }
  return sha;
}

/** Persist work that reached the fail-closed path without a terminal marker.
 *
 * The provenance-marked commit is intentionally pushed to the canonical task
 * branch without opening a PR or completing the task. A fresh retry recognizes
 * this marker in prepareTaskWorkBranch and restores its diff onto the current
 * base. force-with-lease permits a second failed retry to replace the previous
 * checkpoint while refusing to clobber an unexpected concurrent branch update.
 * The lease is pinned to the SHA observed before the first push. It is never
 * refreshed to a competing remote tip: after an ambiguous push failure we only
 * accept remote==local (the response was lost) or remote==the original lease
 * (safe to retry). Every other state fails closed.
 */
async function checkpointUnshippedWork(
  opts: Pick<CodeChangeTaskOpts, 'taskId' | 'workBranch' | 'baseCommit'>,
  ws: WebSocket | null,
): Promise<{ branch: string; sha: string } | null> {
  try {
    // A generated ./fail|./blocked helper may have committed and pushed before
    // its status request lost a response. Reuse that exact checkpoint rather
    // than stacking another provenance commit on every recovery pass.
    let existingCheckpoint = false;
    try {
      const workspaceDirty = git(['status', '--porcelain']).length > 0;
      const metadata = parseTaskCheckpointCommitMessage(
        git(['show', '-s', '--format=%B', 'HEAD']),
        opts.taskId,
      );
      existingCheckpoint = !workspaceDirty && metadata?.baseCommit === opts.baseCommit;
    } catch { /* create a fresh provenance commit below */ }
    if (!existingCheckpoint) {
      git(['add', '-A']);
      const message = buildTaskCheckpointCommitMessage(opts.taskId, opts.baseCommit);
      const [subject, ...bodyLines] = message.split('\n');
      git(['commit', '--allow-empty', '-m', subject, '-m', bodyLines.join('\n').trim()]);
    }
    // Refresh here: a long session may outlive its installation token.
    try {
      const fresh = await apiGet(
        `/api/agent/github/token?task_id=${encodeURIComponent(opts.taskId)}&force_refresh=true`,
      ) as { data?: { token?: string } };
      if (fresh.data?.token) writeGitTokenFile(fresh.data.token);
    } catch (error) {
      // The credential already in the helper file may still be valid. Preserve
      // best-effort checkpointing instead of turning refresh availability into
      // a new way to lose the worktree.
      sendLog(
        ws,
        `[wrapper] WARNING: checkpoint token refresh failed; trying the existing credential: ` +
        `${error instanceof Error ? error.message : String(error)}\n`,
      );
    }
    const localSha = git(['rev-parse', 'HEAD']);
    const expectedRemoteSha = readRemoteBranchSha(opts.workBranch);
    if (expectedRemoteSha === localSha) {
      sendLog(
        ws,
        `[wrapper] Existing checkpoint ${localSha.slice(0, 12)} is already durable on ` +
        `${opts.workBranch}; not pushing it again.\n`,
      );
      return { branch: opts.workBranch, sha: localSha };
    }

    const remoteRef = `refs/heads/${opts.workBranch}`;
    const lease = `--force-with-lease=${remoteRef}:${expectedRemoteSha ?? ''}`;
    let pushError: unknown;
    for (let attempt = 1; attempt <= 3; attempt += 1) {
      try {
        await gitAsync([
          'push', lease, 'origin', `HEAD:${remoteRef}`,
        ]);
        pushError = undefined;
        break;
      } catch (error) {
        pushError = error;
        try {
          const observedRemoteSha = readRemoteBranchSha(opts.workBranch);
          if (observedRemoteSha === localSha) {
            // The remote write happened and only the client response was lost.
            pushError = undefined;
            break;
          }
          if (observedRemoteSha !== expectedRemoteSha) {
            throw new Error(
              `Checkpoint branch ${opts.workBranch} changed concurrently ` +
              `(expected ${expectedRemoteSha ?? 'absent'}, observed ${observedRemoteSha ?? 'absent'}); ` +
              'refusing to overwrite it.',
            );
          }
        } catch (reconcileError) {
          // A definite competing SHA is terminal. A remote-read failure remains
          // retryable because the unchanged explicit lease still prevents a
          // later push from overwriting either an accepted or competing write.
          if (reconcileError instanceof Error && reconcileError.message.includes('changed concurrently')) {
            throw reconcileError;
          }
          pushError = reconcileError;
        }
        if (attempt < 3) {
          await new Promise(resolve => setTimeout(resolve, attempt * 1_000));
        }
      }
    }
    if (pushError !== undefined) {
      if (pushError instanceof Error) throw pushError;
      throw new Error('Checkpoint push failed with a non-Error rejection');
    }
    sendLog(
      ws,
      `[wrapper] Preserved unshipped work on durable checkpoint branch ${opts.workBranch}; ` +
      'the next retry will restore it before running.\n',
    );
    return { branch: opts.workBranch, sha: localSha };
  } catch (error) {
    sendLog(
      ws,
      `[wrapper] WARNING: failed to persist unshipped-work checkpoint: ` +
      `${error instanceof Error ? error.message : String(error)}\n`,
    );
    return null;
  }
}

/**
 * Inspect the terminal marker files (./done, ./fail, ./blocked, PR-URL) and
 * finalize the task when one is present.
 *
 * Returns 'handled' when the caller should return immediately — either a clean
 * ./done|./fail|./blocked marker was found (the bash helper already posted the
 * terminal status), or a well-formed PR-URL marker was finalised to COMPLETED
 * on Claude's behalf. Returns 'unshipped' when no valid terminal signal exists
 * (no markers, or a malformed PR-URL marker) and the caller should continue to
 * the forced-continuation / fail-closed path.
 *
 * `note` is appended to operator log/status lines so the post-forced-
 * continuation inspection is distinguishable from the first one in the logs.
 */
async function inspectTerminalMarkers(
  opts: CodeChangeTaskOpts,
  ws: WebSocket | null,
  note: string,
): Promise<'handled' | 'unshipped'> {
  const { taskId } = opts;
  const doneMarker = `/tmp/done_${taskId}`;
  const failMarker = `/tmp/fail_${taskId}`;
  const blockedMarker = `/tmp/blocked_${taskId}`;
  const prUrlFile = `/tmp/pr_url_${taskId}`;

  if (fs.existsSync(doneMarker)) {
    sendLog(ws, `[wrapper] ./done marker present${note} — Claude posted COMPLETED. Exiting cleanly.\n`);
    return 'handled';
  }
  if (fs.existsSync(failMarker)) {
    // The generated helper checkpoints before posting status or writing this
    // marker. Re-checkpointing here would stack redundant commits after the
    // terminal transition and is too late to protect an immediate retry.
    sendLog(ws, `[wrapper] ./fail marker present${note} — Claude checkpointed pending work and posted FAILED. Exiting cleanly.\n`);
    return 'handled';
  }
  if (fs.existsSync(blockedMarker)) {
    sendLog(ws, `[wrapper] ./blocked marker present${note} — Claude checkpointed pending work and posted BLOCKED_PRECONDITION. Exiting cleanly.\n`);
    return 'handled';
  }

  // Claude exited without calling ./done|./fail|./blocked. Did it at least open
  // a PR? If yes, treat as success — Claude probably hung post-result-event
  // before getting to ./done; we just complete the handshake on its behalf.
  //
  // Shape-validate the marker file before trusting it. ./pr open / ./pr close /
  // verify-mode all write a canonical pull URL; any other shape means Claude
  // (or some other code path) wrote the file directly. Production incident
  // 2026-05-06: Claude on a retry whose ./check gate was blocked by a
  // missing-libatk viewport failure improvised by writing a /compare/ URL
  // here, the wrapper trusted it, and downstream PR-merge verifiers crashed
  // 36×/cycle on int(last-path-segment) of `task-cb7bb07e`. Treat malformed
  // contents as task-did-not-ship.
  if (fs.existsSync(prUrlFile)) {
    const prUrl = fs.readFileSync(prUrlFile, 'utf-8').trim();
    const PR_URL_RE = /^https:\/\/github\.com\/[^/\s]+\/[^/\s]+\/pull\/\d+$/;
    if (!PR_URL_RE.test(prUrl)) {
      sendLog(ws, `[wrapper] /tmp/pr_url_${taskId} contains malformed PR URL: ${JSON.stringify(prUrl)}. Treating as task-did-not-ship.\n`);
      return 'unshipped';
    }
    sendLog(ws, `[wrapper] PR URL marker present (${prUrl}) but no ./done marker${note}. Posting COMPLETED on Claude's behalf.\n`);
    // Capture the diff so review-pipeline scope analysis, direction credit,
    // and rollup management get real changed_files instead of an empty list.
    let changedFiles: string[] = [];
    let diffComputed = false;
    try {
      const out = git(['diff', '--name-only', `${opts.baseCommit}..HEAD`]);
      changedFiles = out.split('\n').filter(Boolean).slice(0, 50);
      diffComputed = true;
    } catch { /* git may be in odd state — best effort */ }
    // Zero-effective-diff guard (defense-in-depth for the empty-diff-at-merge
    // incident). A rebased/superseded branch can carry a PR-URL marker yet net
    // to ZERO effective diff vs base. Posting COMPLETED on Claude's behalf here
    // would mask that no-op as a shipped success. When the diff was computed
    // successfully and is genuinely empty, fall through to the fail-closed path
    // instead so the task records an explicit unshipped disposition. Only trip
    // on a confirmed-empty diff — a git error leaves diffComputed=false and
    // preserves the prior best-effort COMPLETED behavior.
    if (diffComputed && changedFiles.length === 0) {
      sendLog(ws, `[wrapper] PR URL marker present (${prUrl}) but ${opts.baseCommit}..HEAD nets to ZERO effective diff${note}. Treating as task-did-not-ship (no COMPLETED post).\n`);
      return 'unshipped';
    }
    const selfReview = augmentSelfReview(
      readSelfReviewOutcome(taskId, opts.existingPrNumber !== undefined ? (opts.fixIteration ?? 1) : undefined),
      taskId,
      opts.baseCommit,
    );
    await persistAndPostStatus(taskId, {
      task_id: taskId,
      status: 'COMPLETED',
      artifact_type: 'PULL_REQUEST',
      pr_url: prUrl,
      logs: `Task completed. Claude opened PR but did not call ./done — wrapper finalised${note}.`,
      ...(changedFiles.length > 0 ? { changed_files: changedFiles } : {}),
      self_review_outcome: selfReview,
    });
    return 'handled';
  }

  return 'unshipped';
}

export async function runCodeChangeTask(opts: CodeChangeTaskOpts): Promise<void> {
  const { taskId, prompt, ws, statusRef, model, effort, disallowedTools } = opts;

  // Stale marker cleanup: if a prior attempt on this VM left /tmp/done_<id>
  // or /tmp/fail_<id> or /tmp/pr_url_<id> behind (e.g. agent crashed mid-
  // status post; warm-pool retry on same VM), the wrapper would short-
  // circuit on the stale marker and skip Claude entirely — or post a fake
  // COMPLETED. Wipe before every run.
  for (const f of [
    `/tmp/done_${taskId}`,
    `/tmp/fail_${taskId}`,
    `/tmp/blocked_${taskId}`,
    `/tmp/pr_url_${taskId}`,
    `/tmp/base_commit_${taskId}`,
    `/tmp/fix_responses_${taskId}.json`,
    `/tmp/self_review_${taskId}.json`,
    `/tmp/fix_sr_context_${taskId}.json`,
    `/tmp/task_${taskId}_result.json`,
    `/tmp/test_baseline_${taskId}.json`,
    `/tmp/execution_capability_receipt_${taskId}`,
  ]) {
    try { fs.unlinkSync(f); } catch { /* not present — fine */ }
  }

  // ── SCOPE_EMPTY pre-flight (auto-decomposed cluster siblings) ────────────
  // When all declared related_files were already shipped by completed sibling
  // cluster PRs, exit cleanly without consuming the retry budget.
  if (opts.autoDecomposed && (opts.relatedFiles ?? []).length > 0 && Array.isArray(opts.siblingTasks)) {
    const guardFiles = opts.relatedFiles as string[];
    const coveredFiles = new Set<string>();
    const coveringTitles: string[] = [];
    for (const sibling of opts.siblingTasks) {
      if (sibling.status !== 'COMPLETED') continue;
      const sFiles = sibling.files ?? [];
      if (sFiles.some(f => guardFiles.includes(f))) {
        for (const f of sFiles) coveredFiles.add(f);
        const title = sibling.title ?? sibling.id ?? 'sibling task';
        if (!coveringTitles.includes(title)) coveringTitles.push(title);
      }
    }
    if (coveredFiles.size > 0 && guardFiles.every(f => coveredFiles.has(f))) {
      const msg = `SCOPE_EMPTY: All declared related_files [${guardFiles.join(', ')}] were already covered by completed sibling tasks (${coveringTitles.join(', ')}). Exiting cleanly — no retry needed.`;
      sendLog(ws, `[ScopeGuard] ${msg}\n`);
      await persistAndPostStatus(taskId, {
        task_id: taskId,
        status: 'BLOCKED_PRECONDITION',
        artifact_type: 'ERROR_LOG',
        logs: msg,
        failure_category: 'SCOPE_EMPTY',
      });
      return;
    }
  }

  // Persist baseCommit so ./done can compute the changed_files diff for
  // its enriched payload (review-pipeline scope analysis, direction
  // credit, rollup management all read task.context._pr_changed_files).
  try {
    fs.writeFileSync(`/tmp/base_commit_${taskId}`, opts.baseCommit, { mode: 0o644 });
  } catch (e) {
    sendLog(ws, `[wrapper] Failed to write base_commit marker: ${e instanceof Error ? e.message : String(e)}\n`);
    // Non-fatal — ./done's enrichment is optional.
  }

  // 1a. For fix tasks, write a context file that tells Claude what base commit
  //     to use when computing scope-drift and regression_risk in its SR. This
  //     ensures SR reflects THIS iteration's diff, not the cumulative PR diff.
  if (opts.existingPrNumber !== undefined) {
    writeFixSRContext(taskId, opts.baseCommit, opts.existingPrNumber, opts.fixIteration ?? 1);
  }

  // 1. Write helpers (./check, ./pr, ./done, ./fail). Existing helpers
  //    (./playbook, ./messages, ./note, etc.) are written by the caller.
  writeCodeChangeHelpers({
    taskId,
    workBranch: opts.workBranch,
    baseBranch: opts.baseBranch,
    owner: opts.owner,
    repo: opts.repo,
    existingPrNumber: opts.existingPrNumber,
    testsOnMainBranch: opts.testsOnMainBranch,
    promotionTarget: opts.promotionTarget,
    regressingCategories: opts.regressingCategories,
    buildConfig: opts.buildConfig,
    taskCapability: opts.taskCapability,
    ciFailureSelectors: opts.ciFailureSelectors,
    visualAcceptance: opts.visualAcceptance,
  });

  // 2. Run Claude with the full task prompt. Claude is responsible for
  //    making edits, running ./check until green, and calling ./pr open
  //    + ./done to ship.
  const { exitCode, allOutput } = await runClaudeCode(
    prompt, ws, statusRef, model, effort, disallowedTools, true,
  );

  // 2b. Rotate OAuth credentials if Claude refreshed its tokens during
  //     the session. This used to live in applyFinalStatusPrerequisites
  //     (only fired on the TS persistAndPostStatus path); the bash ./done
  //     bypasses it, so we run it here regardless of who posts the
  //     terminal status. The rotation function is idempotent (SHA-256 hash
  //     compare against the at-claim-time hash) and safe to call even if
  //     ./done already finished — it's a no-op when credentials haven't
  //     changed. Critical for OpenAI/Codex OAuth users only; no-op for
  //     ANTHROPIC_API_KEY auth.
  try {
    await rotateOAuthCredentialsIfChanged(taskId);
  } catch (e) {
    sendLog(ws, `[wrapper] OAuth rotation failed: ${e instanceof Error ? e.message : String(e)}\n`);
    // Don't fail the task — Claude's work is done, the rotation only
    // affects the NEXT task on this account. Surfaced in logs for
    // operator triage.
  }

  // 3. Inspect marker files written by ./done|./fail|./pr to determine
  //    terminal status without re-querying GitHub.
  if (await inspectTerminalMarkers(opts, ws, '') === 'handled') return;

  // No PR opened and no ./done|./fail|./blocked marker. Did Claude make any
  // changes?
  let hasChanges = workspaceHasChanges(opts.baseCommit);

  // ── FORCED TERMINAL-VERB CONTINUATION BACKSTOP (2026-06-06) ──────────────
  // A passive end_turn — Claude ended its final turn with a plain assistant
  // message and NO tool call ("I've scheduled a check. Waiting for the gate to
  // complete.") — falsely assumes a monitor will resume the session. There is
  // none: runClaudeCode is a single-shot SDK query invoked exactly once above.
  // When the subprocess exits 0 with an uncommitted/HEAD-advanced diff but no
  // terminal marker, the work would be discarded as "did not ship". The prompt
  // guard (SHARED_LIFECYCLE_REMAINDER) is soft prose the model can ignore; this
  // is the deterministic, model-independent backstop. Issue exactly ONE forced
  // continuation that DEMANDS a terminal verb, then re-inspect the markers. Cap
  // at one attempt to avoid loops. We deliberately do NOT auto-run ./pr open or
  // auto-complete without a model turn — the repo requires an affirmative
  // terminal verb (see the SAFEGUARD comment below), so the continuation must
  // produce a real ./pr open + ./done|./fail|./blocked or the task fails closed.
  if (exitCode === 0 && hasChanges) {
    sendLog(ws, '[wrapper] passive end_turn with uncommitted diff — issuing one forced terminal-verb continuation\n');
    let workingTreeState = '<unavailable>';
    let changedFiles = '<unavailable>';
    try {
      workingTreeState = sanitize(git(['status', '--short']).trim()).replace(/\s+/g, ' ').slice(0, 1200) || '<clean>';
      changedFiles = git(['diff', '--name-only', opts.baseCommit]).split('\n')
        .map(file => sanitize(file.trim()).slice(0, 200))
        .filter(Boolean)
        .slice(0, 25)
        .join(', ') || '<none>';
    } catch {
      // The continuation still has task/routing identity if local inspection fails.
    }
    const sanitizedPrompt = sanitize(opts.taskPrompt ?? opts.prompt).trim();
    const originalTask = sanitizedPrompt.length > 8_000
      ? `${sanitizedPrompt.slice(0, 4_000)}\n...[bounded middle omitted]...\n${sanitizedPrompt.slice(-4_000)}`
      : sanitizedPrompt || '<unavailable>';
    const priorOutputTail = sanitize(allOutput.slice(-1500)).replace(/\s+/g, ' ').trim() || '<none>';
    const continuationState = `Original task:\n${originalTask}\n\nTask state: task_id=${opts.taskId}; repo=${opts.owner}/${opts.repo}; work_branch=${opts.workBranch}; base=${opts.baseBranch}@${opts.baseCommit}; existing_pr=${opts.existingPrNumber ?? 'none'}; changed_files=${changedFiles}; working_tree=${workingTreeState}; prior_output_tail=${priorOutputTail}.`;
    const continuationPrompt =
      continuationState + ' ' +
      'Your previous turn ended with a text message and no tool call, so the run was about ' +
      'to be discarded. THERE IS NO MONITOR OR BACKGROUND PROCESS THAT WILL RESUME YOU. You ' +
      'must re-read the original task, inspect the current repository state, and finish every ' +
      'required validation step. Only if the work is complete and the required checks pass, ' +
      'run ./pr open --title "<title>" --body "<body>" then ./done. If validation fails or the ' +
      'work cannot be completed, call ./fail "<reason>"; if blocked upstream call ./blocked ' +
      '"<reason>". Do not publish before validating, and do not reply with prose — invoke an ' +
      'appropriate terminal verb.';
    try {
      await runClaudeCode(continuationPrompt, ws, statusRef, model, effort, disallowedTools, true);
    } catch (e) {
      sendLog(ws, `[wrapper] forced continuation run failed: ${e instanceof Error ? e.message : String(e)}\n`);
      // Non-fatal — fall through to re-inspect markers (none will exist) and
      // then to the fail-closed path below.
    }
    // Re-inspect exactly as the existing code does. Only if there is STILL no
    // terminal marker / no PR do we fall through and fail the task.
    if (await inspectTerminalMarkers(opts, ws, ' after forced continuation') === 'handled') return;
    // The continuation may have added, committed, or reverted work. Checkpoint
    // exactly the state that will be reported incomplete, not the pre-continuation
    // snapshot that merely triggered this one capped recovery turn.
    hasChanges = workspaceHasChanges(opts.baseCommit);
  }

  // SAFEGUARD (2026-06-01): a bare no-diff clean exit is NOT auto-completed.
  //
  // The previous behaviour completed ANY no-diff clean exit when
  // `noChangesMessage` was set — a passive "no diff ⇒ COMPLETED" rule. That let
  // a task that produced no diff because the agent gave up, errored before
  // editing, or simply stopped early masquerade as a legitimate "nothing to do"
  // success. The canonical no-op completion is now an AFFIRMATIVE signal:
  // `./done --already-done "<reason>"` (done-helper.ts), which posts COMPLETED
  // (no PR) with a verified reason and writes /tmp/done_<id> — handled by the
  // marker check above (line ~321). Reaching here means the agent did NOT make
  // that affirmation, so a no-diff exit is treated as `incomplete` and FAILS:
  // the retry/operator then sees the agent never shipped or signalled. This is
  // the false-completion guard required by the redundant-no-op work — the only
  // path to a no-PR COMPLETED is the explicit ./done --already-done verb.
  if (!hasChanges && exitCode === 0 && opts.noChangesMessage) {
    sendLog(
      ws,
      '[wrapper] No diff and no ./done|./fail|./blocked. The agent did not affirmatively ' +
      'signal "already done" via ./done --already-done — treating as incomplete (NOT a ' +
      'silent COMPLETED).\n',
    );
  }

  // Anything else is a failure: Claude exited without shipping. Capture the
  // output tail so the operator and any downstream investigation can see
  // what actually happened (the lint-output-capture commit 0a35f0b8 is for
  // wrapper-driven lint loops; this captures the Claude session output).
  const reason = exitCode === 0
    ? (!hasChanges
        ? 'Claude exited with no diff and without calling ./done (incl. ./done --already-done), ' +
          './fail, or ./blocked. If the deliverable already exists, the agent must affirmatively ' +
          'call ./done --already-done "<reason>"; a bare no-diff exit is treated as incomplete.'
        : 'Claude exited without calling ./done, ./fail, or opening a PR. Task did not ship.')
    : `Claude subprocess exited with code ${exitCode} without shipping a PR.`;
  const logs = `${reason}\n\n--- Claude output tail ---\n${sanitize(allOutput.slice(-10_000))}`;
  sendLog(ws, `[wrapper] ${reason}\n`);
  let checkpointReceipt: { branch: string; sha: string } | null = null;
  if (hasChanges) {
    checkpointReceipt = await checkpointUnshippedWork(opts, ws);
    if (!checkpointReceipt) {
      // Posting a retryable terminal transition now would release this runner
      // while its disposable worktree still owns the only copy of the diff.
      // Keep the task non-terminal so recovery cannot race from an empty clone.
      sendLog(
        ws,
        '[wrapper] Refusing to post retryable FAILED status because every durable ' +
        'checkpoint path failed; preserving task ownership for operator recovery.\n',
      );
      return;
    }
  }
  await persistAndPostStatus(taskId, {
    task_id: taskId,
    status: 'FAILED',
    artifact_type: 'ERROR_LOG',
    logs,
    failure_category: exitCode === 0 ? 'incomplete' : 'claude_code_failed',
    ...(opts.fileChangeDetails && opts.fileChangeDetails.length > 0
      ? { file_change_details: opts.fileChangeDetails.slice(0, 50) } : {}),
    ...(opts.analysisContext ? { analysis_plan: opts.analysisContext.slice(0, 5000) } : {}),
    ...(checkpointReceipt ? {
      commit_hash: checkpointReceipt.sha,
      storage_uri: `git-ref:refs/heads/${checkpointReceipt.branch}`,
    } : {}),
  });
}
