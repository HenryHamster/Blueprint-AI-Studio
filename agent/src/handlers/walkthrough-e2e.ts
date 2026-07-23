/**
 * walkthrough_e2e handler — autonomous live-stack walkthrough evidence lane.
 *
 * Clones the repo at the pinned `candidate_sha`, runs the live-stack Playwright
 * walkthrough harness (`scripts/walkthrough/v26-live-walkthrough.cjs`), parses
 * the machine-readable JSON result, and POSTs SHA-pinned per-criterion evidence
 * to the backend ingestion endpoint (`POST ..../walkthrough-e2e-evidence`).
 *
 * This is the agent half of the Autonomous Evidence Lane: the backend emits
 * `walkthrough_e2e` tasks spawned by the release qualifier; this handler
 * executes the walkthrough against the live stack and posts covered-tier
 * machine-verified CriterionEvidence.
 */

import { execFileSync } from 'child_process';
import type WebSocket from 'ws';
import {
  WORKSPACE,
  strVal,
  sanitize,
  sanitizeError,
  sendLog,
  readHandlerOutput,
  persistAndPostStatus,
  apiPost,
  resolveBaseBranch,
} from '../index.js';
import { prepareWorkspaceForRole } from '../workspacePrep.js';
import { resolveCandidateSha } from './specialized.js';
import { CandidateStackController } from './walkthrough-stack.js';

/**
 * Map a per-criterion result entry to the shape expected by the
 * WalkthroughE2EEvidenceRequest endpoint.
 */
function normalizeResultEntry(r: Record<string, unknown>): {
  criterion_key: string;
  passed: boolean;
  notes: string | null;
} {
  return {
    criterion_key: typeof r.criterion_key === 'string' ? r.criterion_key : '',
    passed: r.passed === true,
    notes: typeof r.notes === 'string' ? r.notes : null,
  };
}

/**
 * Validate the walkthrough JSON result has the expected shape.
 */
function isValidWalkthroughResult(value: unknown): value is {
  git_sha: string;
  run_timestamp: string;
  results: Array<{ criterion_key: string; passed: boolean; notes?: string }>;
} {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const v = value as Record<string, unknown>;
  return typeof v.git_sha === 'string'
    && typeof v.run_timestamp === 'string'
    && Array.isArray(v.results);
}

export async function handleWalkthroughE2e(
  taskId: string,
  payload: Record<string, unknown>,
  task: Record<string, unknown>,
  ghToken: string,
  owner: string,
  repo: string,
  ws: WebSocket | null,
  statusRef: { value: string },
  _model?: string,
  _effort?: string,
): Promise<void> {
  const rawBaseBranch = resolveBaseBranch(payload, task);
  const candidateSha = resolveCandidateSha(payload, task);
  const qualificationAttemptId = strVal(payload.qualification_attempt_id) || undefined;
  // project_id is populated on task.project_metadata by the backend for every
  // task type (the qualifier spawner does not thread it through the payload);
  // fall back to payload.project_id for out-of-band/manual dispatch.
  const projectMetadata = (task.project_metadata as Record<string, unknown> | undefined) ?? {};
  const projectId = strVal(projectMetadata.project_id) || strVal(payload.project_id);

  if (!projectId) throw new Error('project_id is required in walkthrough_e2e payload');
  if (!candidateSha) throw new Error('candidate_sha is required in walkthrough_e2e payload');

  // ── Clone (full history — candidate SHA may be older than shallow depth) ──
  statusRef.value = `Cloning ${owner}/${repo} for walkthrough_e2e...`;
  sendLog(
    ws,
    `Cloning ${owner}/${repo} @ ${rawBaseBranch}, pinning candidate ${candidateSha.slice(0, 12)}...\n`,
  );
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  await prepareWorkspaceForRole({
    workspace: WORKSPACE,
    repoUrl: cloneUrl,
    githubToken: ghToken,
    branch: rawBaseBranch,
    baseBranch: rawBaseBranch,
    depth: 0, // full history required to reach the pinned SHA
  });

  // ── Pin to candidate SHA (mirrors handleSmoketest) ────────────────────────
  sendLog(ws, `Fetching + checking out candidate commit ${candidateSha.slice(0, 12)}...\n`);
  try {
    execFileSync('git', ['fetch', 'origin', candidateSha], {
      cwd: WORKSPACE,
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    execFileSync('git', ['checkout', '--detach', candidateSha], {
      cwd: WORKSPACE,
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    sendLog(ws, `Checked out candidate commit ${candidateSha.slice(0, 12)} (detached HEAD).\n`);
  } catch (e) {
    const detail = sanitizeError(e);
    sendLog(ws, `[ERROR] Could not check out candidate commit ${candidateSha}: ${detail}\n`);
    await persistAndPostStatus(taskId, {
      task_id: taskId,
      status: 'FAILED',
      logs: sanitize(
        `walkthrough_e2e aborted: could not check out the pinned candidate ` +
        `commit ${candidateSha}. The candidate must be the exact commit the ` +
        `release qualifier snapshotted; running the walkthrough against a ` +
        `different commit would produce meaningless evidence. Detail: ${detail}`,
      ),
      failure_category: 'clone_failed',
    });
    return;
  }

  // ── Boot an ephemeral candidate-SHA stack (FAIL-CLOSED) ────────────────────
  // The harness MUST exercise the candidate commit, not the deployed engine.
  // Boot a throwaway backend + migrated DB + served frontend built from THIS
  // checked-out tree, then drive the harness against it. If the stack cannot be
  // booted / migrated / health-checked, we fail closed: post ZERO evidence and
  // FAIL the task — never silently fall back to ORCHESTRATOR_URL. The
  // controller's teardown is guaranteed on every exit path below.
  statusRef.value = 'Booting candidate-SHA stack...';
  sendLog(ws, `Booting ephemeral candidate stack @ ${candidateSha.slice(0, 12)}...\n`);
  const stackController = new CandidateStackController({
    workspace: WORKSPACE,
    candidateSha,
    adminToken: process.env.ADMIN_API_KEY || undefined,
    log: (m) => { sendLog(ws, m); },
  });

  let stack;
  try {
    stack = await stackController.boot();
  } catch (e) {
    const detail = sanitizeError(e);
    sendLog(ws, `[ERROR] Candidate stack failed to boot: ${detail}\n`);
    // Fail closed: release any partially-provisioned resources and post NO
    // evidence so the criterion stays uncovered and the qualifier gate closed.
    await stackController.teardown();
    await persistAndPostStatus(taskId, {
      task_id: taskId,
      status: 'FAILED',
      artifact_type: 'ERROR_LOG',
      logs: sanitize(
        `walkthrough_e2e aborted: the candidate-SHA stack could not be booted, ` +
        `migrated, or health-checked at commit ${candidateSha}. Failing closed ` +
        `with zero evidence — the walkthrough never ran against a genuine ` +
        `candidate engine, so no SHA-pinned coverage may be minted. Detail: ${detail}`,
      ),
      failure_category: 'candidate_stack_boot_failed',
    });
    return;
  }

  try {
    // ── Run the walkthrough harness against the CANDIDATE stack ──────────────
    statusRef.value = 'Running live-stack walkthrough...';
    sendLog(ws, `Running live-stack walkthrough harness against ${stack.backendUrl}...\n`);

    const resultFile = `/tmp/walkthrough_result_${taskId}.json`;
    let harnessFailed = false;
    try {
      const harnessEnv: Record<string, string> = {
        ...(process.env as Record<string, string>),
        // Drive the harness at the LOCAL candidate stack — NOT ORCHESTRATOR_URL.
        WALKTHROUGH_ADMIN_TOKEN: stack.adminToken,
        WALKTHROUGH_BACKEND_URL: stack.backendUrl,
        WALKTHROUGH_FRONTEND_URL: stack.frontendUrl,
        WALKTHROUGH_PROJECT_ID: projectId,
        GIT_SHA: candidateSha,
        WALKTHROUGH_RESULT_FILE: resultFile,
      };
      execFileSync('node', ['scripts/walkthrough/v26-live-walkthrough.cjs'], {
        cwd: WORKSPACE,
        env: harnessEnv,
        encoding: 'utf-8',
        stdio: ['ignore', 'pipe', 'pipe'],
        timeout: 300_000, // 5 minutes
      });
      sendLog(ws, 'Walkthrough harness completed.\n');
    } catch (e) {
      harnessFailed = true;
      sendLog(ws, `[ERROR] Walkthrough harness exited with error: ${sanitizeError(e)}\n`);
      // Fall through — the harness may still have written a partial result file.
    }

    // ── Read the JSON result ─────────────────────────────────────────────────
    const rawResult = readHandlerOutput(taskId, 'walkthrough_result', ws);
    const walkthroughResult = isValidWalkthroughResult(rawResult) ? rawResult : null;

    if (!walkthroughResult) {
      const detail = harnessFailed
        ? 'walkthrough_e2e harness failed and no parseable result file was written'
        : 'walkthrough_e2e result file missing or unparseable';
      sendLog(ws, `[ERROR] ${detail}\n`);
      await persistAndPostStatus(taskId, {
        task_id: taskId,
        status: 'FAILED',
        artifact_type: 'ERROR_LOG',
        logs: sanitize(detail),
        failure_category: harnessFailed ? 'walkthrough_harness_failed' : 'walkthrough_result_unparseable',
      });
      return;
    }

    // ── Post per-criterion evidence to the REAL orchestrator ingestion endpoint ──
    // apiPost targets ORCHESTRATOR_URL (the live orchestrator), NOT the local
    // candidate stack: the harness EXERCISES the candidate stack, but the
    // evidence is RECORDED against the live release version. exercised_engine_sha
    // carries the SHA the candidate stack actually served — the seam a backend
    // guard enforces against candidate_sha.
    const results = (walkthroughResult.results as unknown[]).map((r) =>
      normalizeResultEntry(r as Record<string, unknown>),
    );

    const evidenceBody: Record<string, unknown> = {
      candidate_sha: candidateSha,
      exercised_engine_sha: stack.exercisedEngineSha,
      results: results.map((r) => ({
        criterion_key: r.criterion_key,
        passed: r.passed,
        notes: r.notes,
      })),
    };
    if (qualificationAttemptId) {
      evidenceBody.qualification_attempt_id = qualificationAttemptId;
    }

    let evidenceSummary = '';
    try {
      const resp = (await apiPost(
        `/api/admin/projects/${projectId}/acceptance-criteria/walkthrough-e2e-evidence`,
        evidenceBody,
      )) as Record<string, unknown>;
      const recorded = typeof resp.recorded === 'number' ? resp.recorded : Number(resp.recorded) || 0;
      const skipped = typeof resp.skipped_not_passed === 'number' ? resp.skipped_not_passed : Number(resp.skipped_not_passed) || 0;
      evidenceSummary = `recorded=${recorded}, skipped_not_passed=${skipped}`;
      sendLog(ws, `Walkthrough evidence posted: ${evidenceSummary}\n`);
    } catch (e) {
      sendLog(ws, `[ERROR] Failed to post walkthrough evidence: ${sanitizeError(e)}\n`);
      // Evidence posting failure is non-fatal for the task — the result is still
      // captured in the WALKTHROUGH_E2E_REPORT artifact.
    }

    // ── Persist final status ─────────────────────────────────────────────────
    // Post COMPLETED regardless of pass/fail — the evidence itself records which
    // criteria passed. Negative results are still valid machine-verified evidence.
    await persistAndPostStatus(taskId, {
      task_id: taskId,
      status: 'COMPLETED',
      artifact_type: 'WALKTHROUGH_E2E_REPORT',
      logs: JSON.stringify({ ...walkthroughResult, exercised_engine_sha: stack.exercisedEngineSha }),
      repo_url: `https://github.com/${owner}/${repo}`,
    });
  } finally {
    // Guaranteed teardown of the ephemeral stack on every exit path.
    await stackController.teardown();
  }
}
