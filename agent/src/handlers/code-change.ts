import * as fs from 'fs';
import type WebSocket from 'ws';
import {
  sanitize, sendLog, runClaudeCode, persistAndPostStatus, git,
} from '../index.js';
import { writeCodeChangeHelpers } from '../prompt.js';

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
}

export async function runCodeChangeTask(opts: CodeChangeTaskOpts): Promise<void> {
  const { taskId, prompt, ws, statusRef, model, effort, disallowedTools } = opts;

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
    prompt, ws, statusRef, model, effort, disallowedTools,
  );

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
    sendLog(ws, `[wrapper] PR URL marker present (${prUrl}) but no ./done marker. Posting COMPLETED on Claude's behalf.\n`);
    await persistAndPostStatus(taskId, {
      task_id: taskId,
      status: 'COMPLETED',
      artifact_type: 'PULL_REQUEST',
      pr_url: prUrl,
      logs: 'Task completed. Claude opened PR but did not call ./done — wrapper finalised.',
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
