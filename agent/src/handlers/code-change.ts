import * as fs from 'fs';
import type WebSocket from 'ws';
import {
  sanitize, sendLog, runClaudeCode, persistAndPostStatus, git,
  rotateOAuthCredentialsIfChanged,
} from '../index.js';
import { writeCodeChangeHelpers, SELF_REVIEW_PROMPT_VERSION } from '../prompt.js';

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
  ws: WebSocket | null;
  statusRef: { value: string };
  workBranch: string;
  baseBranch: string;
  owner: string;
  repo: string;
  /** When set, Claude is fixing an existing PR (review-fix path). ./pr open
   *  pushes to the existing PR's branch instead of creating a new one. */
  existingPrNumber?: number;
  /** When true, ./check test runs the Docker backend test suite when changes
   *  touch backend/. Set when baseBranch === 'main'. */
  testsOnMainBranch: boolean;
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
  /**
   * Called when SR has categories=["incomplete_review"] and no reviewer_findings yet.
   * Implementations run a fresh-reviewer pass with incompleteSrRecovery=true so
   * check_results are stamped with origin="reviewer_recovery_after_incomplete_sr".
   * Only invoked from the PR-URL fallback path so the recovery findings are captured
   * in the COMPLETED status payload. Guard callers with freshReviewerAlreadyRan when
   * they also run a fresh-reviewer pass unconditionally to avoid double-firing.
   */
  incompleteReviewRecoveryFn?: () => Promise<void>;
}

/** Read Claude's self-review JSON and ensure prompt_version is stamped. */
function readSelfReviewOutcome(taskId: string): Record<string, unknown> | null {
  const path = `/tmp/self_review_${taskId}.json`;
  try {
    if (!fs.existsSync(path)) return null;
    const raw = fs.readFileSync(path, 'utf-8');
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    if (!('prompt_version' in parsed)) {
      parsed.prompt_version = SELF_REVIEW_PROMPT_VERSION;
    }
    return parsed;
  } catch {
    return null;
  }
}

/**
 * Fires incompleteReviewRecoveryFn when the SR carries incomplete_review in
 * categories and no reviewer_findings have been set yet. No-op when recoveryFn
 * is undefined or when conditions are not met. Exported for unit testing.
 */
export async function maybeRunIncompleteReviewRecovery(
  taskId: string,
  ws: WebSocket | null,
  recoveryFn: (() => Promise<void>) | undefined,
): Promise<void> {
  if (!recoveryFn) return;
  const sr = readSelfReviewOutcome(taskId);
  const cats = Array.isArray(sr?.categories) ? sr.categories as string[] : [];
  const hasFindings = Array.isArray(sr?.reviewer_findings) && (sr.reviewer_findings as unknown[]).length > 0;
  if (cats.includes('incomplete_review') && !hasFindings) {
    sendLog(ws, '[FreshReview] SR reports incomplete_review — auto-triggering recovery pass.\n');
    await recoveryFn();
  }
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
    `/tmp/pr_url_${taskId}`,
    `/tmp/base_commit_${taskId}`,
    `/tmp/fix_responses_${taskId}.json`,
    `/tmp/self_review_${taskId}.json`,
    `/tmp/task_${taskId}_result.json`,
    `/tmp/test_baseline_${taskId}.json`,
  ]) {
    try { fs.unlinkSync(f); } catch { /* not present — fine */ }
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
  const doneMarker = `/tmp/done_${taskId}`;
  const failMarker = `/tmp/fail_${taskId}`;
  const prUrlFile = `/tmp/pr_url_${taskId}`;

  if (fs.existsSync(doneMarker)) {
    sendLog(ws, '[wrapper] ./done marker present — Claude posted COMPLETED. Exiting cleanly.\n');
    return;
  }
  if (fs.existsSync(failMarker)) {
    sendLog(ws, '[wrapper] ./fail marker present — Claude posted FAILED. Exiting cleanly.\n');
    return;
  }

  // Claude exited without calling ./done|./fail.
  // Did it at least open a PR? If yes, treat as success — Claude probably
  // hung post-result-event before getting to ./done; we just complete the
  // handshake on its behalf.
  if (fs.existsSync(prUrlFile)) {
    const prUrl = fs.readFileSync(prUrlFile, 'utf-8').trim();
    // Run incomplete-review recovery before reading the SR for the status post so
    // any fresh-reviewer findings are included in the COMPLETED payload.
    await maybeRunIncompleteReviewRecovery(taskId, ws, opts.incompleteReviewRecoveryFn);
    sendLog(ws, `[wrapper] PR URL marker present (${prUrl}) but no ./done marker. Posting COMPLETED on Claude's behalf.\n`);
    // Capture the diff so review-pipeline scope analysis, direction credit,
    // and rollup management get real changed_files instead of an empty list.
    let changedFiles: string[] = [];
    try {
      const out = git(['diff', '--name-only', `${opts.baseCommit}..HEAD`]);
      changedFiles = out.split('\n').filter(Boolean).slice(0, 50);
    } catch { /* git may be in odd state — best effort */ }
    const selfReview = readSelfReviewOutcome(taskId);
    await persistAndPostStatus(taskId, {
      task_id: taskId,
      status: 'COMPLETED',
      artifact_type: 'PULL_REQUEST',
      pr_url: prUrl,
      logs: 'Task completed. Claude opened PR but did not call ./done — wrapper finalised.',
      ...(changedFiles.length > 0 ? { changed_files: changedFiles } : {}),
      ...(selfReview ? { self_review_outcome: selfReview } : {}),
    });
    return;
  }

  // No PR opened. Did Claude make any changes?
  let hasChanges = false;
  try {
    const diff = git(['status', '--porcelain']);
    const head = git(['rev-parse', 'HEAD']);
    hasChanges = diff.length > 0 || head !== opts.baseCommit;
  } catch { /* git may be in odd state */ }

  if (!hasChanges && exitCode === 0 && opts.noChangesMessage) {
    // Edge case: Claude legitimately decided no changes were needed (e.g. a
    // docs task where the codebase is already documented). Treat as success.
    sendLog(ws, '[wrapper] No changes detected and Claude exited cleanly. Posting COMPLETED.\n');
    await persistAndPostStatus(taskId, {
      task_id: taskId, status: 'COMPLETED', logs: opts.noChangesMessage,
    });
    return;
  }

  // Anything else is a failure: Claude exited without shipping. Capture the
  // output tail so the operator and any downstream investigation can see
  // what actually happened (the lint-output-capture commit 0a35f0b8 is for
  // wrapper-driven lint loops; this captures the Claude session output).
  const reason = exitCode === 0
    ? 'Claude exited without calling ./done, ./fail, or opening a PR. Task did not ship.'
    : `Claude subprocess exited with code ${exitCode} without shipping a PR.`;
  const logs = `${reason}\n\n--- Claude output tail ---\n${sanitize(allOutput.slice(-10_000))}`;
  sendLog(ws, `[wrapper] ${reason}\n`);
  await persistAndPostStatus(taskId, {
    task_id: taskId,
    status: 'FAILED',
    artifact_type: 'ERROR_LOG',
    logs,
    failure_category: exitCode === 0 ? 'incomplete' : 'claude_code_failed',
    ...(opts.fileChangeDetails && opts.fileChangeDetails.length > 0
      ? { file_change_details: opts.fileChangeDetails.slice(0, 50) } : {}),
    ...(opts.analysisContext ? { analysis_plan: opts.analysisContext.slice(0, 5000) } : {}),
  });
}
