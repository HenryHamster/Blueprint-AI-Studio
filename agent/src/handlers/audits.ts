/**
 * Audit handlers — security, self-audit, and release audit task implementations.
 *
 * Provides three handler types:
 *
 * - **handleSecurityAudit**: Scans code for security vulnerabilities (injection,
 *   auth flaws, etc.) and emits a structured verdict with findings and fix recommendations.
 *
 * - **handleSelfAudit**: Evaluates system maturity and operational readiness,
 *   producing a maturity assessment and operating mode recommendations.
 *
 * - **handleReleaseAudit**: Examines release-candidate diffs for risky changes,
 *   migration issues, breaking changes, and deployment blockers. Spawns a child
 *   release_audit_challenge task for adversarial verification. Used in the
 *   qualify→promote gate to block unsafe releases.
 *
 * All audit handlers are structured-output producers that emit JSON verdicts
 * persisted as Artifacts and embedded in task context for downstream consumers.
 */

import fs from 'fs';
import { execFileSync } from 'child_process';
import type WebSocket from 'ws';
import {
  WORKSPACE, strVal, sanitize, sendLog, validateBranchName, readHandlerOutput,
  persistAndPostStatus, git, runClaudeCode, sendHeartbeatSync,
  setCurrentPromptFingerprint, resolveBaseBranch,
} from '../index.js';
import { prepareWorkspaceForRole } from '../workspacePrep.js';
import { DEFAULT_CLONE_DEPTH } from '../constants.js';
import { describeJsonExtractionResult, extractJsonArrayDetailed } from '../json-extract.js';
import {
  getTaskTypeHints, PLAYBOOK_GUIDANCE, FIRST_TASK_GUIDANCE, DEFAULT_TOOL_GUIDANCE,
  PARALLEL_AUDIT_GUIDANCE, READ_ONLY_BASH_WRITE_NOTE,
  readClaudeMd, buildContextPrefix, buildFileTree, configureAgentPermissions,
  writePlaybookHelper, writeMessagesHelper, writeDesignDocHelper, formatPlaybook, buildRetryContext, computePromptFingerprint, type PlaybookEntry,
} from '../prompt.js';
import { providerAutoInjectsClaudeMd } from '../providers/runners.js';
import {
  writeFetchHelper, buildFetchGuidance, PLAN_FETCH_VERBS,
} from './strategic-helpers.js';
import { resolveBuildConfig } from '../build-config.js';
import { buildPromptSizeTelemetry, setPromptSizeTelemetry } from '../telemetry.js';
import { repairStructuredOutput } from './structured-output-repair.js';

// Read the read-only mid-run helper guidance for an audit handler. project_id is
// set for all task types by the backend; when it is absent we provision NOTHING
// and advertise nothing, so a prompt never promises a helper that wasn't written.
// Returns the ./fetch guidance block (or '' when project_id is missing); the
// design-doc advertisement is appended to each prompt unconditionally because
// writeDesignDocHelper is provisioned alongside it whenever project_id is present.
export function provisionAuditFetchHelpers(
  taskId: string, task: Record<string, unknown>,
): string {
  const projectId = ((task.project_metadata as Record<string, unknown> | undefined)?.project_id as string | undefined) ?? '';
  if (!projectId) return '';
  writeFetchHelper(taskId, projectId, WORKSPACE, { verbs: PLAN_FETCH_VERBS });
  writeDesignDocHelper(taskId, WORKSPACE, { readOnly: true });
  return buildFetchGuidance(PLAN_FETCH_VERBS);
}

// Advertise the read-only ./design-doc CLI in an audit prompt. Gated on the same
// project_id as the helper write so we never advertise an unwritten helper.
export function designDocAdvertisement(task: Record<string, unknown>): string {
  const projectId = ((task.project_metadata as Record<string, unknown> | undefined)?.project_id as string | undefined) ?? '';
  return projectId
    ? '\n\nRead full design-doc section prose on demand with `./design-doc get <key>` (list keys with `./design-doc list`).'
    : '';
}

/** Returns true when the project has opted into parallel audit fan-out. */
export function isParallelAuditEnabled(task: Record<string, unknown>): boolean {
  const meta = task.project_metadata;
  if (meta && typeof meta === 'object' && !Array.isArray(meta)) {
    return (meta as Record<string, unknown>).enable_parallel_review === true;
  }
  return false;
}

// ── Candidate-SHA helpers (release qualifier) ────────────────────────────────

/** A full 40-char lowercase hex git commit SHA. */
const FULL_SHA_RE = /^[0-9a-f]{40}$/;

/**
 * Resolve the pinned candidate commit SHA for a qualifier task.
 *
 * The release qualifier (backend/app/release_qualifier.py) snapshots
 * `automain` HEAD as a full 40-char SHA and passes it as `candidate_sha`
 * into the `release_audit` and other SHA-pinned qualifier task payloads, so
 * the agent scores the EXACT pinned commit rather than whatever `automain`
 * has drifted to by the time the agent VM starts. The qualifier also stamps
 * `task.context.candidate_sha`; we accept that as a fallback so the handler
 * keeps working regardless of which field the backend populates.
 *
 * Returns the validated SHA, or null when no well-formed SHA is present.
 */
export function resolveCandidateSha(
  payload: Record<string, unknown>,
  task: Record<string, unknown>,
): string | null {
  const candidates: unknown[] = [payload.candidate_sha];
  const ctx = task.context;
  if (ctx && typeof ctx === 'object' && !Array.isArray(ctx)) {
    candidates.push((ctx as Record<string, unknown>).candidate_sha);
  }
  for (const raw of candidates) {
    if (typeof raw === 'string') {
      const sha = raw.trim().toLowerCase();
      if (FULL_SHA_RE.test(sha)) return sha;
    }
  }
  return null;
}

// ── Candidate-commit fetch + diff-range pre-flight (shared) ───────────────────
//
// handleReleaseAudit, handleReleaseAuditChallenge, and handleReleaseMergeGrade
// all clone the base branch with FULL history, fetch the EXACT pinned candidate
// commit object, then refuse to score an unresolvable three-dot diff range. The
// fetch + pre-flight verify logic is identical across all three, so it lives
// here and is imported by the other two handlers.

/** Outcome of fetching the pinned candidate commit + verifying the diff range. */
export interface CandidateDiffRangeResult {
  /** True when the candidate object was fetched into the local clone. */
  fetchOk: boolean;
  /** Last `git fetch` error message (empty when fetchOk). */
  fetchFailDetail: string;
  /** True when `${baseBranch}...<sha>` resolves (merge-base reachable). */
  diffRangeResolves: boolean;
  /** Why the range did not resolve (empty when diffRangeResolves). */
  diffRangeFailDetail: string;
}

// Bounded retry-with-backoff for the single network fetch of the pinned
// candidate commit. A qualification attempt is expensive and TERMINAL, so a
// transient GitHub I/O blip on this one fetch must NOT torch the whole attempt.
// Production 2026-06-26: an attempt finalized infra_failure with
// `audit_challenge_task_failed_diff_range_unresolvable`, yet the SAME candidate
// re-fetched and COMPLETED ~9 min later — the commit was always reachable; one
// fetch blip burned it. The candidate SHA is `automain` HEAD at snapshot time,
// reachable from a ref, so the fetch is deterministically retryable.
const CANDIDATE_FETCH_MAX_ATTEMPTS = 3;
// Linear backoff base (ms): waits 500ms then 1000ms between the three attempts.
const CANDIDATE_FETCH_BACKOFF_BASE_MS = 500;

function defaultFetchSleep(ms: number): Promise<void> {
  return new Promise<void>((resolve) => setTimeout(resolve, ms));
}

/**
 * Fetch the EXACT pinned candidate commit (a real object, not just FETCH_HEAD)
 * with a bounded retry-with-backoff, then pre-flight-verify that the three-dot
 * diff range `${baseBranch}...<candidateSha>` resolves.
 *
 * Retry rationale: the fetch is a single network call. A transient failure used
 * to fail loud IMMEDIATELY with `diff_range_unresolvable`, terminally burning a
 * qualification attempt for an I/O blip. We retry ONLY the fetch — an
 * unresolvable range after a clean fetch is a genuine condition that MUST fail
 * loud (never retried/masked). After retries are exhausted the caller still
 * posts the fail-loud `diff_range_unresolvable` status (the safety property:
 * never audit/grade an unresolvable range).
 *
 * Pure of the fail-loud decision: returns a structured result; each caller posts
 * its own handler-specific FAILED status when `diffRangeResolves` is false.
 *
 * `opts` exists for tests (inject a no-op sleep, a fake fetch/verify, fewer
 * attempts); production callers pass none and get the real `execFileSync` git
 * fetch + the shared retry/backoff constants.
 */
export async function fetchCandidateCommitWithRetry(
  candidateSha: string,
  diffRange: string,
  ws: WebSocket | null,
  opts: {
    maxAttempts?: number;
    backoffBaseMs?: number;
    sleep?: (ms: number) => Promise<void>;
    fetchOnce?: (sha: string) => void;
    verifyDiffRange?: (sha: string, range: string) => void;
  } = {},
): Promise<CandidateDiffRangeResult> {
  const maxAttempts = Math.max(1, opts.maxAttempts ?? CANDIDATE_FETCH_MAX_ATTEMPTS);
  const backoffBaseMs = opts.backoffBaseMs ?? CANDIDATE_FETCH_BACKOFF_BASE_MS;
  const sleep = opts.sleep ?? defaultFetchSleep;
  // A SHA fetch leaves a real commit object behind, which `main...<sha>` can
  // resolve. A bare-branch fetch (`git fetch origin automain`) only updates
  // FETCH_HEAD and never a local ref — that is the original bug this guards.
  const fetchOnce = opts.fetchOnce ?? ((sha: string): void => {
    execFileSync('git', ['fetch', 'origin', sha], { cwd: WORKSPACE, stdio: ['ignore', 'pipe', 'pipe'] });
  });
  // `git rev-parse --verify <sha>^{commit}` errors when the SHA is not a
  // reachable commit object; `git rev-list --count <base>...<sha>` errors when
  // either endpoint or the merge-base is unreachable. Run both so a genuinely
  // unresolvable range fails loud, while a clean exit proves the symmetric-
  // difference range is fully resolvable.
  //
  // A count of 0 is ALSO unresolvable: an EMPTY symmetric difference (base and
  // candidate resolve to the same commit — e.g. the candidate IS the base branch
  // tip) means there is literally no diff to audit/grade. `git rev-list --count`
  // prints "0" and exits 0, so without this guard an empty range would be
  // treated as "resolves" and the handler would grade a blank surface. Fail
  // loud instead: never audit/grade an empty range.
  const verifyDiffRange = opts.verifyDiffRange ?? ((sha: string, range: string): void => {
    git(['rev-parse', '--verify', `${sha}^{commit}`]);
    const count = git(['rev-list', '--count', range]);
    if (parseInt(count, 10) === 0) {
      throw new Error(
        `diff range ${range} is EMPTY (0 commits) — base and candidate resolve ` +
        `to the same commit; refusing to audit/grade a blank surface`,
      );
    }
  });

  let fetchOk = false;
  let fetchFailDetail = '';
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    try {
      fetchOnce(candidateSha);
      fetchOk = true;
      fetchFailDetail = '';
      sendLog(ws, attempt > 1
        ? `Fetched candidate commit ${candidateSha.slice(0, 12)} successfully (attempt ${attempt}/${maxAttempts}).\n`
        : `Fetched candidate commit ${candidateSha.slice(0, 12)} successfully.\n`);
      break;
    } catch (e) {
      fetchFailDetail = e instanceof Error ? e.message : String(e);
      const willRetry = attempt < maxAttempts;
      sendLog(
        ws,
        `[WARNING] git fetch origin ${candidateSha.slice(0, 12)} failed ` +
        `(attempt ${attempt}/${maxAttempts})${willRetry ? ' — retrying' : ''}: ${fetchFailDetail}\n`,
      );
      if (willRetry) await sleep(backoffBaseMs * attempt);
    }
  }

  let diffRangeResolves = false;
  let diffRangeFailDetail = '';
  if (fetchOk) {
    try {
      verifyDiffRange(candidateSha, diffRange);
      diffRangeResolves = true;
      sendLog(ws, `Diff range ${diffRange} resolves — merge-base is reachable.\n`);
    } catch (e) {
      diffRangeFailDetail = e instanceof Error ? e.message : String(e);
      sendLog(ws, `[ERROR] Diff range ${diffRange} does not resolve: ${diffRangeFailDetail}\n`);
    }
  } else {
    diffRangeFailDetail =
      `git fetch origin ${candidateSha} failed after ${maxAttempts} attempt(s) ` +
      `(${fetchFailDetail || 'no detail'}); ${diffRange} cannot be resolved`;
  }

  return { fetchOk, fetchFailDetail, diffRangeResolves, diffRangeFailDetail };
}

// Read the audit progress markdown the agent writes for retry context.
// Returns the content prefixed with the standard analysis header so the reopen
// prompt includes prior analysis, or undefined when the file is missing/empty.
function readAuditProgress(taskId: string): string | undefined {
  const path = `/tmp/audit_progress_${taskId}.md`;
  try {
    if (fs.existsSync(path)) {
      const content = fs.readFileSync(path, 'utf-8').trim();
      if (content) return `## Prior Analysis\n\n${content}`;
    }
  } catch { /* ignore */ }
  return undefined;
}

// ── Handler: Security Audit (analyze codebase for vulnerabilities, output findings) ──
export async function handleSecurityAudit(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  const rawBaseBranch = resolveBaseBranch(payload, task);
  const baseBranch = validateBranchName(rawBaseBranch, 'base_branch');

  statusRef.value = `Cloning ${owner}/${repo} for security audit...`;
  sendLog(ws, `Starting security audit...\n`);
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  await prepareWorkspaceForRole({
    workspace: WORKSPACE, repoUrl: cloneUrl, githubToken: ghToken,
    branch: baseBranch, baseBranch, depth: DEFAULT_CLONE_DEPTH,
    // Audit is an independent verification role — never warm-resume onto a
    // lane workspace (gate integrity; lane pilot lane-izes only the fix side).
    allowLaneResume: false,
  });
  writePlaybookHelper(taskId);
  writeMessagesHelper(taskId);
  const secFetchGuidance = provisionAuditFetchHelpers(taskId, task);
  const disallowedTools = configureAgentPermissions('security_audit');

  const contextPrefix = buildContextPrefix(task);

  const claudeMd = readClaudeMd();
  const codebaseSummary = (claudeMd && !providerAutoInjectsClaudeMd())
    ? `## Codebase Instructions\n${claudeMd}\n\n`
    : '';

  const fileTreeSection = buildFileTree(ws);
  const secPlaybookSection = formatPlaybook(task.playbook as Record<string, PlaybookEntry | string> | undefined);
  const secRetryContext = buildRetryContext(task);
  const secFirstTaskSection = task.first_task_on_repo ? FIRST_TASK_GUIDANCE : '';

  const auditScope = strVal(payload.prompt) || strVal(payload.description);
  const auditScopeSection = auditScope ? `Audit focus: ${auditScope}\n\n` : '';

  const secCorePrompt = `You are a security audit agent. You have full access to this repository. Your job is to analyze the codebase for security vulnerabilities and produce structured findings.

Analyze the codebase for the following vulnerability categories:
1. OWASP Top 10 vulnerabilities
2. Hardcoded secrets, credentials, or API keys
3. SQL injection risks
4. Cross-site scripting (XSS) risks
5. Insecure dependencies (outdated packages with known CVEs)
6. Authentication and authorization issues
7. Unsafe file operations (path traversal, arbitrary file read/write)

Instructions:
1. Explore the codebase thoroughly. Read key files, understand the architecture, find patterns.
2. For each vulnerability found, assess its severity and provide a concrete fix prompt.
3. Output your findings as a JSON array as the VERY LAST thing in your response:

[
  {
    "title": "Short title (under 60 chars)",
    "description": "1-3 sentence description of the vulnerability",
    "severity": "critical",
    "file_path": "path/to/affected/file.py",
    "line_number": 42,
    "fix_prompt": "The exact prompt to give to a fixing agent. Must be self-contained and describe exactly what to change."
  }
]

Valid severity values: critical, high, medium, low

Rules:
- Only report genuine vulnerabilities, not theoretical ones
- Each finding must have a concrete, actionable fix_prompt
- Limit to 20 findings maximum, prioritized by severity
- For findings without a specific line number, use 0
- IMPORTANT: Write your findings JSON array to /tmp/security_findings_${taskId}.json ${READ_ONLY_BASH_WRITE_NOTE} (NOT in the repository). The file should contain ONLY the JSON array, no markdown wrapping.
- Also output the same JSON array as the final output in your logs.${designDocAdvertisement(task)}`;

  const secBuildConfig = resolveBuildConfig(task);
  const securityPromptSuffix = payload.prompt_suffix ? `\n\nAdditional instructions:\n${strVal(payload.prompt_suffix)}` : '';
  const secTypeHints = getTaskTypeHints(secBuildConfig);
  const secTypeHint = secTypeHints['security_audit'] ? '\n\n' + secTypeHints['security_audit'] : '';
  const secParallelGuidance = isParallelAuditEnabled(task) ? PARALLEL_AUDIT_GUIDANCE : '';
  const secSuffixBlock = secTypeHint + secFirstTaskSection + securityPromptSuffix + secParallelGuidance + PLAYBOOK_GUIDANCE + `\n\n${secFetchGuidance}${DEFAULT_TOOL_GUIDANCE}`;
  const securityPrompt = `${codebaseSummary}${secPlaybookSection}${secRetryContext}${contextPrefix}${fileTreeSection}${auditScopeSection}${secCorePrompt}${secSuffixBlock}`;
  const secTelemetry = buildPromptSizeTelemetry(securityPrompt, [
    ['claude_md', codebaseSummary],
    ['playbook', secPlaybookSection],
    ['retry_context', secRetryContext],
    ['context_prefix', contextPrefix],
    ['file_tree', fileTreeSection],
    ['audit_scope', auditScopeSection],
    ['core_prompt', secCorePrompt],
    ['suffix', secSuffixBlock],
  ]);
  setPromptSizeTelemetry(secTelemetry.prompt_size_bytes, secTelemetry.prompt_section_count, secTelemetry.prompt_section_sizes);
  statusRef.value = 'Running security audit...';
  const secFingerprint = computePromptFingerprint('security_audit', secBuildConfig);
  setCurrentPromptFingerprint(secFingerprint);
  sendHeartbeatSync(taskId, statusRef.value, { prompt_fingerprint: secFingerprint });
  const { exitCode, allOutput, lastResultText } = await runClaudeCode(securityPrompt, ws, statusRef, model, effort, disallowedTools);

  const findings = readHandlerOutput(taskId, 'security_findings', ws);
  let logs = findings !== null ? sanitize(JSON.stringify(findings)) : '';
  if (!logs) {
    const priorAnalysis = readAuditProgress(taskId);
    const priorAnalysisSection = priorAnalysis ? `${priorAnalysis}\n\n` : '';
    const repaired = await repairStructuredOutput<unknown[]>({
      mode: 'array',
      requiredKeys: ['severity'],
      schemaLabel: 'security_audit',
      first: { exitCode, allOutput, lastResultText },
      reopenPrompt: `${priorAnalysisSection}Your prior security audit response did not end with a parseable JSON findings array.
Output ONLY the JSON array now — each finding must include at least "severity" plus title, description, file_path, line_number, and fix_prompt. Use [] if there are no findings. No markdown, no code fences, no prose.`,
      ws,
      statusRef,
      model,
      effort,
      disallowedTools,
      retryStatus: 'Repairing security audit JSON...',
    });
    if (repaired.value !== null) {
      logs = sanitize(JSON.stringify(repaired.value));
    } else {
      logs = sanitize(lastResultText || allOutput.slice(-50_000));
    }
  }

  await persistAndPostStatus(taskId, {
    task_id: taskId,
    status: exitCode === 0 ? 'COMPLETED' : 'FAILED',
    ...(exitCode !== 0 ? { artifact_type: 'ERROR_LOG', failure_category: 'claude_code_failed' } : { artifact_type: 'SECURITY_REPORT' }),
    logs,
  });
}

// ── Handler: Self Audit (score system maturity, output findings JSON) ────────
export async function handleSelfAudit(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  const rawBaseBranch = resolveBaseBranch(payload, task);
  const baseBranch = validateBranchName(rawBaseBranch, 'base_branch');

  statusRef.value = `Cloning ${owner}/${repo} for self audit...`;
  sendLog(ws, `Starting self audit...\n`);
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  await prepareWorkspaceForRole({
    workspace: WORKSPACE, repoUrl: cloneUrl, githubToken: ghToken,
    branch: baseBranch, baseBranch, depth: DEFAULT_CLONE_DEPTH,
    // Audit is an independent verification role — never warm-resume onto a
    // lane workspace (gate integrity; lane pilot lane-izes only the fix side).
    allowLaneResume: false,
  });
  writePlaybookHelper(taskId);
  writeMessagesHelper(taskId);
  const selfAuditFetchGuidance = provisionAuditFetchHelpers(taskId, task);
  const disallowedTools = configureAgentPermissions('self_audit');

  const contextPrefix = buildContextPrefix(task);
  const claudeMd = readClaudeMd();
  const codebaseSummary = (claudeMd && !providerAutoInjectsClaudeMd()) ? `## Codebase Instructions\n${claudeMd}\n\n` : '';
  const fileTreeSection = buildFileTree(ws);
  const playbookSection = formatPlaybook(task.playbook as Record<string, PlaybookEntry | string> | undefined);
  const retryContext = buildRetryContext(task);
  const firstTaskSection = task.first_task_on_repo ? FIRST_TASK_GUIDANCE : '';

  const focusSystemsRaw = Array.isArray(payload.focus_systems) ? (payload.focus_systems as unknown[]) : [];
  const focusSystems = focusSystemsRaw.filter((s): s is string => typeof s === 'string' && s.length > 0);
  const maxSystemsRaw = typeof payload.max_systems === 'number' && Number.isFinite(payload.max_systems)
    ? Math.floor(payload.max_systems)
    : 3;
  const maxSystems = Math.max(1, Math.min(maxSystemsRaw, 10));

  const focusSection = focusSystems.length > 0
    ? `Audit ONLY these systems (use these exact system_id values in output): ${JSON.stringify(focusSystems)}\n\n`
    : `No focus_systems provided. Pick up to ${maxSystems} systems to audit by reading the project design_doc and direction system_id values (round-robin across systems not recently audited). Use the discovered system_id verbatim in output.\n\n`;

  const taskCtx = task.context && typeof task.context === 'object' ? task.context as Record<string, unknown> : {};
  const driftCtxRaw = taskCtx.drift_context;
  let driftSection = '';
  if (driftCtxRaw && typeof driftCtxRaw === 'object') {
    const entries = Object.entries(driftCtxRaw as Record<string, unknown>);
    if (entries.length > 0) {
      const fmt = (v: unknown, fallback: string): string => {
        if (v === undefined || v === null) return fallback;
        if (typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean') return String(v);
        return JSON.stringify(v);
      };
      const lines = entries.map(([sid, sig]) => {
        const s = sig as Record<string, unknown>;
        return `  - ${sid}: claimed=${fmt(s.claimed_changes_count, '?')}, merged=${fmt(s.actually_merged_prs, '?')}, consistency=${fmt(s.claim_shipped_consistency, '?')}, over_claim=${fmt(s.over_claim_flag, 'false')}, stalled=${fmt(s.stalled_despite_churn_flag, 'false')}`;
      });
      driftSection = `## Drift Scanner Findings\nThe following systems were flagged by the drift scanner before this audit was dispatched. For each, reconcile what the pipeline *claimed* to ship vs what was *actually merged*:\n${lines.join('\n')}\n\n`;
    }
  }

  const selfAuditCorePrompt = `You are a self-audit agent. You have full read access to this repository. Your job is to score the maturity of one or more systems on four objective signals and output a structured findings report.

For each system, score four components (each 0-100, where 100 is healthy and 0 is broken):

1. **test_coverage** — Are there tests covering the system's core paths? Look at tests directories, count test files mentioning the system, check whether key modules have direct test coverage. Low score: little or no test coverage. High score: each module has direct, runnable tests.

2. **friction_density** — How often does work in this system get stuck? Inspect recent task history (titles, status_log, retry counts in payloads/context) and recent git history. Low score: many recurring failures, retries, or aborts in this area. High score: tasks in this system flow through without recurring blockers.

3. **claim_consistency** — Does what shipped actually match what was claimed? Compare recent task summaries / PR descriptions against the diffs that were merged. Low score: PR titles/summaries do not match what the diff actually does (drift). High score: claims and shipped diffs are consistent.

4. **failure_mix** — What is the failure-class mix in this area? Look at recent FAILED tasks scoped to this system. Low score: heavy CODE_ERROR / parse_failed / auto-remediation churn. High score: most failures are recoverable / transient.

Then assign an overall \`score\` (0-100) and a \`recommended_mode\` based on the worst components:
- \`audit\` — score < 40 in any component, system is silently broken; pause feature work and investigate
- \`fundamentals\` — score 40-70, system is functioning but accumulating friction; prioritize structural cleanup over new features
- \`ship\` — score >= 70, system is healthy; continue feature work
- \`pause\` — pipeline-wide hold (use sparingly when the audit cannot make a confident assessment)

Output a single JSON object as the VERY LAST thing in your response with this exact shape:

{
  "findings": [
    {
      "system_id": "exact-system-id-from-design-doc",
      "score": 72,
      "components": {
        "test_coverage": 80,
        "friction_density": 65,
        "claim_consistency": 75,
        "failure_mix": 70
      },
      "top_issues": [
        "One short sentence per issue, max 5 issues"
      ],
      "recommended_mode": "fundamentals",
      "evidence": [
        {"file": "path/to/file.py", "summary": "What this file shows about the system"}
      ]
    }
  ]
}

Rules:
- Every score MUST be an integer in [0, 100]
- recommended_mode MUST be one of: audit, fundamentals, ship, pause
- Limit findings to ${maxSystems} systems unless focus_systems was provided (then use all focus_systems)
- Each finding must cite at least one evidence file from the actual repo
- IMPORTANT: Write the JSON object to /tmp/self_audit_findings_${taskId}.json ${READ_ONLY_BASH_WRITE_NOTE} (NOT in the repository). The file should contain ONLY the JSON object, no markdown wrapping.
- Also output the same JSON object as the final output in your logs.${designDocAdvertisement(task)}`;

  const buildConfig = resolveBuildConfig(task);
  const promptSuffix = payload.prompt_suffix ? `\n\nAdditional instructions:\n${strVal(payload.prompt_suffix)}` : '';
  const typeHints = getTaskTypeHints(buildConfig);
  const typeHint = typeHints['self_audit'] ? '\n\n' + typeHints['self_audit'] : '';
  const selfAuditParallelGuidance = isParallelAuditEnabled(task) ? PARALLEL_AUDIT_GUIDANCE : '';
  const selfAuditSuffixBlock = typeHint + firstTaskSection + promptSuffix + selfAuditParallelGuidance + PLAYBOOK_GUIDANCE + `\n\n${selfAuditFetchGuidance}${DEFAULT_TOOL_GUIDANCE}`;
  const selfAuditPrompt = `${codebaseSummary}${playbookSection}${retryContext}${contextPrefix}${fileTreeSection}${driftSection}${focusSection}${selfAuditCorePrompt}${selfAuditSuffixBlock}`;
  const selfAuditTelemetry = buildPromptSizeTelemetry(selfAuditPrompt, [
    ['claude_md', codebaseSummary],
    ['playbook', playbookSection],
    ['retry_context', retryContext],
    ['context_prefix', contextPrefix],
    ['file_tree', fileTreeSection],
    ['core_prompt', selfAuditCorePrompt],
    ['suffix', selfAuditSuffixBlock],
  ]);
  setPromptSizeTelemetry(selfAuditTelemetry.prompt_size_bytes, selfAuditTelemetry.prompt_section_count, selfAuditTelemetry.prompt_section_sizes);
  statusRef.value = 'Running self audit...';
  const selfAuditFingerprint = computePromptFingerprint('self_audit', buildConfig);
  setCurrentPromptFingerprint(selfAuditFingerprint);
  sendHeartbeatSync(taskId, statusRef.value, { prompt_fingerprint: selfAuditFingerprint });
  const { exitCode, allOutput, lastResultText } = await runClaudeCode(selfAuditPrompt, ws, statusRef, model, effort, disallowedTools);

  // Parse findings: prefer structured file output, then a {findings:[...]} object
  // anywhere in the output, then a bare array of findings (wrapped on our side).
  const handlerOutput = readHandlerOutput(taskId, 'self_audit_findings', ws) as { findings?: unknown[] } | unknown[] | null;
  let logs = '';
  if (handlerOutput !== null) {
    logs = sanitize(JSON.stringify(handlerOutput));
  } else {
    const priorAnalysis = readAuditProgress(taskId);
    const priorAnalysisSection = priorAnalysis ? `${priorAnalysis}\n\n` : '';
    const objRepair = await repairStructuredOutput<Record<string, unknown>>({
      mode: 'object',
      requiredKeys: ['findings'],
      schemaLabel: 'self_audit',
      first: { exitCode, allOutput, lastResultText },
      reopenPrompt: `${priorAnalysisSection}Your prior self-audit response did not end with a parseable JSON object.
Output ONLY the JSON object now — with top-level "findings": [...] and each finding carrying "system_id", score, components, top_issues, recommended_mode, and evidence. No markdown, no code fences, no prose.`,
      ws,
      statusRef,
      model,
      effort,
      disallowedTools,
      retryStatus: 'Repairing self audit JSON...',
    });
    if (objRepair.value !== null) {
      logs = sanitize(JSON.stringify(objRepair.value));
    } else {
      const retryText = objRepair.retry
        ? `${objRepair.retry.lastResultText || ''}\n${objRepair.retry.allOutput || ''}\n`
        : '';
      const haystack = `${retryText}${lastResultText || ''}\n${allOutput || ''}`;
      const arrExtract = extractJsonArrayDetailed(haystack, 'system_id');
      if (arrExtract.ok) {
        logs = sanitize(JSON.stringify({ findings: arrExtract.value }));
      } else {
        sendLog(ws, `[WARNING] Could not extract self audit findings JSON: ${describeJsonExtractionResult(arrExtract)}\n`);
        logs = sanitize(lastResultText || allOutput.slice(-50_000));
      }
    }
  }

  await persistAndPostStatus(taskId, {
    task_id: taskId,
    status: exitCode === 0 ? 'COMPLETED' : 'FAILED',
    ...(exitCode !== 0 ? { artifact_type: 'ERROR_LOG', failure_category: 'claude_code_failed' } : { artifact_type: 'SELF_AUDIT_REPORT' }),
    logs,
  });
}

// ── Handler: Release Audit (review main...<candidate_sha> diff against main's standards) ──
export async function handleReleaseAudit(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  // CRITICAL: always clone base_branch (main), never automain. The audit must
  // judge the incoming changes by main's standards, not the new code's standards.
  const rawBaseBranch = resolveBaseBranch(payload, task);
  const baseBranch = validateBranchName(rawBaseBranch, 'base_branch');

  // CRITICAL (C2/C4): the qualifier must score one PINNED candidate commit, not
  // live `automain`. The bare branch name `automain` does NOT resolve as a
  // revision after cloning `main` — `git fetch origin automain` only updates
  // FETCH_HEAD, never a local `automain` ref — so a `main...automain` range can
  // never resolve and the audit would ALWAYS fail "diff_range_unresolvable".
  // The backend pins the candidate by passing a full 40-char `candidate_sha`
  // (automain HEAD at snapshot time) in the task payload; we fetch that exact
  // commit object and diff `main...<candidate_sha>`, which always resolves.
  const candidateSha = resolveCandidateSha(payload, task);
  if (!candidateSha) {
    // No pinned candidate ⇒ nothing safe to audit. Fail loud rather than fall
    // back to the bare `automain` name (which cannot resolve) or to live HEAD
    // (which is not the snapshotted candidate). A missing candidate_sha is a
    // real contract bug between the qualifier and the agent.
    const rawSeen = strVal(payload.candidate_sha) || strVal(
      (task.context && typeof task.context === 'object' && !Array.isArray(task.context)
        ? (task.context as Record<string, unknown>).candidate_sha
        : undefined),
    );
    sendLog(ws, `[ERROR] Release audit aborted: no valid candidate_sha in task payload.\n`);
    await persistAndPostStatus(taskId, {
      task_id: taskId,
      status: 'FAILED',
      artifact_type: 'ERROR_LOG',
      failure_category: 'diff_range_unresolvable',
      logs: sanitize(
        `Release audit aborted: the task payload carries no valid candidate_sha. ` +
        `The release qualifier must pin the candidate commit by passing a ` +
        `full 40-char commit SHA as payload.candidate_sha. ` +
        `Without it there is no commit to diff against ${baseBranch}. ` +
        `Saw: ${rawSeen ? `"${rawSeen}"` : '(absent)'}`,
      ),
    });
    return;
  }
  // The full LTS range is always the fail-closed fallback. A prior clean audit
  // may request a smaller direct delta, but that hint is trusted only after
  // proving the old candidate is an ancestor of this pinned candidate.
  const fullDiffRange = `${baseBranch}...${candidateSha}`;
  let diffRange = fullDiffRange;
  const requestedAuditBaseSha = typeof payload.audit_base_sha === 'string'
    && /^[0-9a-f]{40}$/i.test(payload.audit_base_sha.trim())
    ? payload.audit_base_sha.trim()
    : null;
  // The FULL version delta for goal-alignment and acceptance-category grading.
  // The backend pins it explicitly (payload.goal_category_diff_range) so it
  // survives the incremental narrowing of `diffRange` below; absent on older
  // payloads we fall back to the full base_branch...candidate range.  Goal and
  // category verdicts have NO scoped carry-forward (unlike code-quality
  // findings, which merge unchanged paths from the prior audit fact), so they
  // must always span the full version delta the promotion PR would carry.
  const goalCategoryDiffRange = typeof payload.goal_category_diff_range === 'string'
    && payload.goal_category_diff_range.trim().length > 0
    ? payload.goal_category_diff_range.trim()
    : fullDiffRange;
  let auditScopeMode: 'full' | 'incremental' = 'full';
  let auditScopeFallbackReason: string | null = requestedAuditBaseSha
    ? 'incremental_base_not_verified'
    : null;

  statusRef.value = `Cloning ${owner}/${repo} (${baseBranch}) for release audit...`;
  sendLog(ws, `Starting release audit — cloning ${baseBranch} (reference branch), candidate ${candidateSha.slice(0, 12)}...\n`);
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  // CRITICAL: a release audit runs `git diff main...<candidate_sha>` (three-dot
  // = merge-base diff). On an LTS integration trunk the candidate routinely
  // diverges from `main` by far more than DEFAULT_CLONE_DEPTH commits, so a
  // shallow clone leaves the merge-base commit unreachable and `git diff`
  // either errors or produces a wrong diff. A FULL clone (depth 0) is the only
  // safe option — the merge-base must be reachable for the three-dot diff.
  await prepareWorkspaceForRole({
    workspace: WORKSPACE, repoUrl: cloneUrl, githubToken: ghToken,
    branch: baseBranch, baseBranch, depth: 0,
    // Release audit is an independent verification role — never warm-resume
    // onto a lane workspace (gate integrity; pilot lane-izes only the fix side).
    allowLaneResume: false,
  });

  // Fetch the EXACT candidate commit so git diff can compute the merge-base
  // diff — but do NOT check it out. The working tree stays on baseBranch
  // (main). GitHub permits fetching a SHA reachable from a ref, and the
  // candidate_sha is `automain` HEAD at snapshot time, so it is reachable.
  // After the fetch the SHA is a local object and `main...<candidate_sha>`
  // resolves. (`git fetch origin <sha>` works where `git fetch origin
  // automain` does not: a SHA fetch leaves a real commit object behind, a
  // bare-branch fetch only updates FETCH_HEAD.)
  // Fetch the EXACT candidate commit (bounded retry-with-backoff) and
  // pre-flight-verify the three-dot diff range resolves before invoking the
  // agent. A successful-but-incomplete fetch can still leave the merge-base
  // unreachable; if it is, `git diff ${diffRange}` errors and the agent can
  // misread that as an "empty diff" and emit audit_score: 1.0 — a FALSE
  // strong-promote signal to the release qualifier. The retry stops a transient
  // GitHub I/O blip from torching the whole qualification attempt; an
  // unresolvable range after a clean fetch still fails loud below.
  sendLog(ws, `Fetching candidate commit ${candidateSha.slice(0, 12)} (full history) for diff computation (staying on ${baseBranch})...\n`);
  const { diffRangeResolves, diffRangeFailDetail } =
    await fetchCandidateCommitWithRetry(candidateSha, fullDiffRange, ws);

  if (!diffRangeResolves) {
    // Never let the agent run an unaudited audit. A diff range that does not
    // resolve makes every downstream audit_score meaningless — post a FAILED
    // status with a clear failure category so the qualifier records a blocker
    // rather than a false audit_score: 1.0.
    sendLog(ws, `Aborting release audit: cannot compute ${fullDiffRange} — refusing to emit an unaudited audit_score.\n`);
    await persistAndPostStatus(taskId, {
      task_id: taskId,
      status: 'FAILED',
      artifact_type: 'ERROR_LOG',
      failure_category: 'diff_range_unresolvable',
      logs: sanitize(
        `Release audit aborted: diff range ${fullDiffRange} does not resolve. ` +
        `The candidate commit ${candidateSha} could not be fetched, or the ` +
        `merge-base of ${baseBranch} and the candidate is unreachable, so ` +
        `git diff would error or produce a wrong diff. ` +
        `Detail: ${diffRangeFailDetail || '(no detail)'}`,
      ),
    });
    return;
  }

  if (requestedAuditBaseSha) {
    // Track which verification stage we reached so fallback_reason carries a
    // machine-readable prefix that the backend can interpret without parsing
    // the raw git error message.
    let fallbackCategory = 'cat_file_failed';
    try {
      execFileSync('git', ['cat-file', '-e', `${requestedAuditBaseSha}^{commit}`], {
        cwd: WORKSPACE, stdio: ['ignore', 'ignore', 'pipe'],
      });
      fallbackCategory = 'ancestor_check_failed';
      execFileSync('git', ['merge-base', '--is-ancestor', requestedAuditBaseSha, candidateSha], {
        cwd: WORKSPACE, stdio: ['ignore', 'ignore', 'pipe'],
      });
      const candidateDelta = `${requestedAuditBaseSha}..${candidateSha}`;
      // Force git to parse both endpoints/range before selecting it. An empty
      // delta is valid; a missing object or malformed range throws.
      execFileSync('git', ['rev-list', '--count', candidateDelta], {
        cwd: WORKSPACE, stdio: ['ignore', 'ignore', 'pipe'],
      });
      diffRange = candidateDelta;
      auditScopeMode = 'incremental';
      auditScopeFallbackReason = null;
      sendLog(ws, `Using incremental audit range ${diffRange}; prior candidate was clean and is an ancestor.\n`);
    } catch (e) {
      const rawMsg = e instanceof Error ? e.message.slice(0, 500) : String(e).slice(0, 500);
      auditScopeFallbackReason = `${fallbackCategory}: ${rawMsg}`;
      diffRange = fullDiffRange;
      sendLog(ws, `[WARNING] Incremental audit base ${requestedAuditBaseSha.slice(0, 12)} is not usable; falling back to full range ${fullDiffRange}.\n`);
    }
  }

  // Deterministically compute test erosion from the actual diff so the
  // hard-blocker (deleted test files) and sub-score deltas are not dependent
  // on the LLM spotting every change in a potentially large diff.
  type ComputedTestErosion = {
    deleted_test_files: string[];
    assertion_delta: number;
    test_count_delta: number;
    deleted_test_file_classifications?: { path: string; classification: string; rationale: string }[];
  };
  let computedTestErosion: ComputedTestErosion | null = null;
  let auditedChangedPaths: string[] = [];
  try {
    // Matches test paths at any depth: root-level (testing/foo.py) or nested
    // (src/testing/foo.py), plus .test. / .spec. file markers.
    const TEST_PATH_RE = /(^|\/)(__tests__|tests|testing)\/|\.test\.|\.spec\./;

    const nameStatus = execFileSync('git', ['diff', '--name-status', diffRange], {
      cwd: WORKSPACE, encoding: 'utf-8', stdio: ['ignore', 'pipe', 'pipe'],
    });
    // All deleted files (test and non-test) — used for co-deletion subject checks.
    const allDeletedFiles = new Set<string>(
      nameStatus
        .split('\n')
        .filter((line: string) => line.startsWith('D\t'))
        .map((line: string) => line.slice(2).trim())
        .filter((p: string) => p.length > 0)
    );
    auditedChangedPaths = [...new Set(
      nameStatus.split('\n').flatMap((line: string) => {
        const fields = line.trim().split('\t');
        return fields.length >= 2 ? fields.slice(1) : [];
      }).filter((p: string) => p.length > 0)
    )].sort();
    const deletedTestFiles = [...allDeletedFiles].filter((p: string) => TEST_PATH_RE.test(p));

    // Derive the subject source file path(s) for a given test file path.
    // Returns candidate paths; caller checks which (if any) are co-deleted.
    const deriveSubjectPaths = (testPath: string): string[] => {
      const lastSlash = testPath.lastIndexOf('/');
      const dir = lastSlash >= 0 ? testPath.slice(0, lastSlash + 1) : '';
      const base = lastSlash >= 0 ? testPath.slice(lastSlash + 1) : testPath;

      // Frontend: X.test.tsx / X.viewport.test.tsx / X.test.ts / X.viewport.test.ts
      // -> subject X.tsx and X.ts in the same directory.
      const frontendMatch = base.match(/^(.+?)(?:\.viewport)?\.test\.(tsx?)$/);
      if (frontendMatch) {
        const stem = frontendMatch[1];
        return [`${dir}${stem}.tsx`, `${dir}${stem}.ts`];
      }

      // Backend: testing/test_foo.py -> app/foo.py
      // Replace the 'testing' directory segment with 'app'.
      const backendMatch = base.match(/^test_(.+)\.py$/);
      if (backendMatch) {
        const module = backendMatch[1];
        const appDir = dir.replace(/^(.*)testing\/$/, '$1app/');
        if (appDir !== dir) {
          return [`${appDir}${module}.py`];
        }
      }

      // __tests__/foo.test.ts -> ../foo.ts (parent of __tests__ directory).
      if (dir.endsWith('__tests__/')) {
        const tsMatch = base.match(/^(.+)\.test\.(tsx?)$/);
        if (tsMatch) {
          const stem = tsMatch[1];
          const parentDir = dir.slice(0, dir.length - '__tests__/'.length);
          return [`${parentDir}${stem}.ts`, `${parentDir}${stem}.tsx`];
        }
      }

      return [];
    };

    // Classify each deleted test file deterministically: if ANY derived subject
    // is also a D-line in the diff, it is unambiguously dead_code_removal.
    // Files whose subject cannot be derived or is still present are left unclassified
    // so the LLM can assess them (fail-closed for those cases).
    const deterministicClassifications: { path: string; classification: string; rationale: string }[] = [];
    for (const testPath of deletedTestFiles) {
      const subjects = deriveSubjectPaths(testPath);
      const coDeleted = subjects.find(s => allDeletedFiles.has(s));
      if (coDeleted) {
        deterministicClassifications.push({
          path: testPath,
          classification: 'dead_code_removal',
          rationale: `Subject ${coDeleted} is also a D-line in git diff --name-status; co-deletion confirms dead code removal.`,
        });
      }
    }

    const diffText = execFileSync('git', ['diff', diffRange], {
      cwd: WORKSPACE, encoding: 'utf-8', stdio: ['ignore', 'pipe', 'pipe'],
      maxBuffer: 50 * 1024 * 1024,
    });

    // ASSERTION_RE must NOT include it(/describe( — those are test-function
    // declarations counted by TEST_FN_RE. Overlapping both regexes would
    // double-count every JS/TS test declaration.
    const ASSERTION_RE = /\b(assert[\s(]|expect\(|assertEqual|assertRaises)/;
    const TEST_FN_RE = /\b(def test_|test\(|it\(|describe\()/;

    // Build a set of deleted test file paths for O(1) lookup while iterating
    // diff lines. Lines removed from a wholly-deleted test file are owned by
    // the hard-blocker gate (deleted_test_files), not by the graded penalty,
    // so exclude them from delta counting to avoid double-penalizing authorized
    // dead-code deletions.
    const deletedTestFileSet = new Set(deletedTestFiles);
    let currentFile = '';
    let assertionDelta = 0;
    let testCountDelta = 0;
    for (const line of diffText.split('\n')) {
      // Track which file the current hunk belongs to.
      if (line.startsWith('diff --git ')) {
        // Format: "diff --git a/<path> b/<path>" — extract the b-side path.
        const m = line.match(/^diff --git a\/.+ b\/(.+)$/);
        currentFile = m ? m[1] : '';
        continue;
      }
      // Skip delta counting for lines in wholly-deleted test files — the
      // hard-blocker gate already owns those files.
      if (deletedTestFileSet.has(currentFile)) continue;

      if (line.startsWith('+') && !line.startsWith('+++')) {
        if (ASSERTION_RE.test(line)) assertionDelta++;
        if (TEST_FN_RE.test(line)) testCountDelta++;
      } else if (line.startsWith('-') && !line.startsWith('---')) {
        if (ASSERTION_RE.test(line)) assertionDelta--;
        if (TEST_FN_RE.test(line)) testCountDelta--;
      }
    }

    computedTestErosion = {
      deleted_test_files: deletedTestFiles,
      assertion_delta: assertionDelta,
      test_count_delta: testCountDelta,
      ...(deterministicClassifications.length > 0 && {
        deleted_test_file_classifications: deterministicClassifications,
      }),
    };
    sendLog(ws, `[AUDIT] Computed test erosion: ${deletedTestFiles.length} deleted test file(s), ${deterministicClassifications.length} deterministic classification(s), assertion_delta=${assertionDelta}, test_count_delta=${testCountDelta}\n`);
  } catch (e) {
    sendLog(ws, `[WARNING] Could not compute deterministic test erosion: ${e instanceof Error ? e.message : String(e)}\n`);
  }

  writePlaybookHelper(taskId);
  writeMessagesHelper(taskId);
  const releaseAuditFetchGuidance = provisionAuditFetchHelpers(taskId, task);
  const disallowedTools = configureAgentPermissions('release_audit');

  const contextPrefix = buildContextPrefix(task);
  const claudeMd = readClaudeMd();
  const codebaseSummary = (claudeMd && !providerAutoInjectsClaudeMd()) ? `## Codebase Instructions\n${claudeMd}\n\n` : '';
  const fileTreeSection = buildFileTree(ws);
  const playbookSection = formatPlaybook(task.playbook as Record<string, PlaybookEntry | string> | undefined);
  const retryContext = buildRetryContext(task);
  const firstTaskSection = task.first_task_on_repo ? FIRST_TASK_GUIDANCE : '';

  const versionGoal = typeof payload.version_goal === 'string' && payload.version_goal.length > 0
    ? payload.version_goal
    : null;
  const categories = Array.isArray(payload.categories)
    ? payload.categories.filter((row): row is { category_key: string; text: string } => (
        !!row && typeof row === 'object'
        && typeof (row as Record<string, unknown>).category_key === 'string'
        && typeof (row as Record<string, unknown>).text === 'string'
      ))
    : [];
  const categoryContractSection = categories.length > 0
    ? `\nAssess each acceptance category below against the FULL version delta (\`git diff ${goalCategoryDiffRange}\`), NOT the narrowed code-quality range — every supplied criterion grades the whole version the promotion PR would carry. Emit one \`category_verdicts\` entry for every supplied key, with \`status\` (\`met\` | \`partial\` | \`not_met\`), a concrete \`gap\` (null when met), and diff-grounded \`citations\` (file paths or commit/PR references). Also emit \`residual_uncaptured_gap\` for any important goal gap not represented by a category, else null.\n\nAcceptance categories:\n${categories.map((row) => `- ${row.category_key}: ${row.text}`).join('\n')}\n`
    : '';
  const versionGoalSection = versionGoal
    ? `\n## Version Goal\n\n${versionGoal}\n\nIn addition to the standard quality checks below (which review the code-quality range \`${diffRange}\`), assess whether the FULL version delta (\`git diff ${goalCategoryDiffRange}\` / \`git log ${goalCategoryDiffRange} --oneline\`) plausibly delivers the version's strategic goal stated above — do NOT limit the goal verdict to the narrowed code-quality range. Emit \`covers_goal\` (boolean), \`coverage_score\` (float 0.0–1.0), \`biggest_gap\` (string or null), and \`goal_alignment\` with \`state\` (\`aligned\` | \`shortfall\` | \`not_applicable\`), \`assessed_grade\`, \`biggest_gap\`, and a diff-grounded \`reasoning_summary\`. These fields are additional to \`audit_score\`, \`test_erosion\`, and \`findings\`.\n${categoryContractSection}\n`
    : '';

  const buildConfig = resolveBuildConfig(task);
  // Only emit the migration-revision-divergence carve-out when the project has a
  // migrations layout configured; most projects have none, so the rule would be
  // noise (and bake in a stack they don't use).
  const migrationDivergenceRule = buildConfig?.migration
    ? `\n- Authorized migration revision-identity divergences are owned by a separate deterministic gate, NOT the audit. If the diff renumbers or reuses a migration revision id that is an authorized, reconciled divergence, do NOT raise a finding for the revision-identity/renumber itself — the qualifier's migration-identity gate evaluates it and deploy-time reconciliation handles it. You SHOULD still flag genuinely broken migration BODIES (bad DDL, missing/incorrect downgrade, destructive re-apply, data-loss risk) on those same files.`
    : '';

  const releaseAuditCorePrompt = `You are a release audit agent. You have been cloned onto \`${baseBranch}\` — the last known-good LTS branch — and your job is to review the incoming ${auditScopeMode === 'incremental' ? 'changed-path delta since the last clean audit' : 'full candidate diff'} range \`${diffRange}\` against \`${baseBranch}\`'s standards. \`${candidateSha}\` is the PINNED release candidate commit (a snapshot of the integration branch); it has already been fetched into this clone, so the diff range resolves.

IMPORTANT: You are running on \`${baseBranch}\` intentionally. You must judge the incoming changes by \`${baseBranch}\`'s standards, NOT by the new code's standards. This asymmetry is the entire point of the audit.

## Your task

1. **Obtain the diff.** Run: \`git diff ${diffRange}\` to see what the candidate commit \`${candidateSha}\` adds relative to \`${baseBranch}\`. Also run \`git diff --stat ${diffRange}\` for a file summary and \`git log ${diffRange} --oneline\` for the commit list. Do NOT use the bare name \`automain\` — it is not a resolvable revision in this clone; always diff against the pinned SHA \`${candidateSha}\`.

2. **Assess test erosion.** Identify:
   - Any test files deleted in the diff (files under \`testing/\`, \`tests/\`, \`__tests__/\`, \`*.test.*\`, \`*.spec.*\` that appear as deleted).
   - Net change in assertion count: search for \`assert \`, \`expect(\`, \`assertEqual\`, \`assertRaises\`, \`it(\`, \`describe(\` in the diff's added/removed lines.
   - Net change in test function/case count: count added vs removed \`def test_\`, \`test(\`, \`it(\`, \`describe(\` patterns.

3. **Review code quality** against \`${baseBranch}\`'s conventions (read CLAUDE.md for standards). Focus on:
   - Correctness and logic errors introduced
   - Security regressions (new hardcoded secrets, unsafe operations, auth bypasses)
   - Architecture drift (patterns inconsistent with the existing codebase conventions)
   - Test coverage gaps for new behavior
   - Documentation / comment accuracy
${versionGoalSection}
4. **Score and emit results.** Write your findings as a JSON object to \`/tmp/release_audit_report_${taskId}.json\` with EXACTLY this structure:

\`\`\`json
{
  "audit_score": 0.85,
  "covers_goal": true,
  "coverage_score": 0.92,
  "biggest_gap": null,
  "category_verdicts": [],
  "residual_uncaptured_gap": null,
  "test_erosion": {
    "deleted_test_files": [],
    "assertion_delta": 12,
    "test_count_delta": 3
  },
  "deleted_test_file_classifications": [
    {
      "path": "testing/test_foo.py",
      "classification": "dead_code_removal",
      "rationale": "The subject module foo.py was also removed in this diff (see \`D testing/test_foo.py\` and \`D app/foo.py\`); no live callers remain."
    }
  ],
  "findings": [
    {
      "severity": "high",
      "summary": "One sentence describing the issue",
      "file": "path/to/affected/file.py",
      "verification_note": "Read app/auth.py lines 45-62 — req.user_id is user-controlled and reaches the SQL call at line 60 with no sanitisation."
    }
  ]
}
\`\`\`

Field semantics:
- \`audit_score\`: float [0, 1] — holistic cross-check of your overall assessment (1.0 = no issues found, 0.0 = critical problems). The backend derives the authoritative gating score from the \`findings\` list, so focus on producing accurate, well-categorised findings rather than computing this number precisely.
- \`test_erosion.deleted_test_files\`: list of test file paths deleted in the diff. An empty list is good.
- \`test_erosion.assertion_delta\`: integer — positive means more assertions added than removed (good), negative means erosion.
- \`test_erosion.test_count_delta\`: integer — positive means more tests added than removed (good), negative means erosion.
- \`deleted_test_file_classifications\`: classify EACH file in \`test_erosion.deleted_test_files\`. One object per deleted test file with \`path\` (the exact path string from \`deleted_test_files\`), \`classification\` (\`dead_code_removal\` | \`genuine_erosion\`), and \`rationale\` (one sentence citing the diff evidence you read). Classification rules:
  - \`dead_code_removal\`: the test file's subject code (the module/function/component it exercised) is ALSO deleted in this same diff (a \`D\` line in \`git diff --name-status\`), OR there are zero remaining callers/imports of that subject anywhere in the candidate. Requires affirmative evidence — read the diff and confirm the subject is also gone before using this classification.
  - \`genuine_erosion\`: the subject code is demonstrably STILL PRESENT and unchanged, and the test file was removed specifically to stop failing tests from blocking a build. This is the rare exception and requires clear evidence — read the diff and confirm the subject is present and unchanged before using it.
  - The gate is **fail-open**: the ONLY blocking outcome is an explicit \`genuine_erosion\` classification. \`dead_code_removal\`, absent (no classification provided), and ambiguous classifications all pass — a co-deleted or dead-code test removal must never block promotion. When uncertain, read the full candidate file list in the repository to determine whether the subject still exists — do NOT classify as \`genuine_erosion\` without clear affirmative evidence that the subject is still present and unchanged.
  - When \`deleted_test_files\` is empty, emit \`[]\` (or omit the field).
- \`findings\`: each entry has \`severity\` (critical|high|medium|low), \`summary\` (one sentence), \`file\` (file path or empty string if cross-cutting), and \`verification_note\` (required for high/medium/critical — cite the specific file and line you read and why the issue is real, ≥ 20 non-whitespace characters). Findings at severity high/medium/critical WITHOUT a substantive \`verification_note\` are silently excluded from the score — they do not deduct points. Drop or downgrade to low/info any finding you cannot substantiate.

Rules:
- Read the actual diff — do NOT speculate about code you haven't read.
- IMPORTANT — substantiation required: Before recording any finding at severity high, medium, or critical, read the relevant unchanged files in the full repository (not just the diff) to check for existing mitigations (guards, exception handlers, per-request checks, hardcoded constants at the call site). Confirm no existing mitigation neutralizes the issue before recording it. Record what you read and why the issue is real in \`verification_note\`. A hardcoded constant reaching a sink is NOT injection. A per-request DB check is NOT a replay gap. A comparison that is exception-guarded is NOT a TypeError. Only record the finding if you can substantiate it after reading the whole-repo context.${migrationDivergenceRule}
- If \`git diff ${diffRange}\` produces no output (empty diff), set \`audit_score: 1.0\`, \`findings: []\`, and note it in a single low-severity finding.
- IMPORTANT: Write ONLY the JSON object to \`/tmp/release_audit_report_${taskId}.json\` ${READ_ONLY_BASH_WRITE_NOTE} — no markdown, no code fences, no extra text.
- Also output the same JSON object as the final output in your logs.
- Do NOT modify any files in the repository.${designDocAdvertisement(task)}`;

  const promptSuffix = payload.prompt_suffix ? `\n\nAdditional instructions:\n${strVal(payload.prompt_suffix)}` : '';
  const typeHints = getTaskTypeHints(buildConfig);
  const typeHint = typeHints['release_audit'] ? '\n\n' + typeHints['release_audit'] : '';
  const releaseAuditParallelGuidance = isParallelAuditEnabled(task) ? PARALLEL_AUDIT_GUIDANCE : '';
  const releaseAuditSuffixBlock = typeHint + firstTaskSection + promptSuffix + releaseAuditParallelGuidance + PLAYBOOK_GUIDANCE + `\n\n${releaseAuditFetchGuidance}${DEFAULT_TOOL_GUIDANCE}`;
  const releaseAuditPrompt = `${codebaseSummary}${playbookSection}${retryContext}${contextPrefix}${fileTreeSection}${releaseAuditCorePrompt}${releaseAuditSuffixBlock}`;
  const releaseAuditTelemetry = buildPromptSizeTelemetry(releaseAuditPrompt, [
    ['claude_md', codebaseSummary],
    ['playbook', playbookSection],
    ['retry_context', retryContext],
    ['context_prefix', contextPrefix],
    ['file_tree', fileTreeSection],
    ['core_prompt', releaseAuditCorePrompt],
    ['suffix', releaseAuditSuffixBlock],
  ]);
  setPromptSizeTelemetry(releaseAuditTelemetry.prompt_size_bytes, releaseAuditTelemetry.prompt_section_count, releaseAuditTelemetry.prompt_section_sizes);
  statusRef.value = 'Running release audit...';
  const releaseAuditFingerprint = computePromptFingerprint('release_audit', buildConfig);
  setCurrentPromptFingerprint(releaseAuditFingerprint);
  sendHeartbeatSync(taskId, statusRef.value, { prompt_fingerprint: releaseAuditFingerprint });
  const { exitCode, allOutput, lastResultText } = await runClaudeCode(
    releaseAuditPrompt,
    ws, statusRef, model, effort, disallowedTools,
  );

  const handlerOutput = readHandlerOutput(taskId, 'release_audit_report', ws) as {
    audit_score?: number;
    test_erosion?: { deleted_test_files?: unknown[]; assertion_delta?: number; test_count_delta?: number };
    computed_test_erosion?: { deleted_test_files: string[]; assertion_delta: number; test_count_delta: number; deleted_test_file_classifications?: { path: string; classification: string; rationale: string }[] };
    deleted_test_file_classifications?: { path: string; classification: string; rationale: string }[];
    findings?: unknown[];
  } | null;

  const enrichAuditOutput = (output: Record<string, unknown>): Record<string, unknown> => {
    const rawFindings = Array.isArray(output.findings) ? output.findings : null;
    const findings = rawFindings === null ? output.findings : rawFindings.map((raw) => {
      if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return raw as Record<string, unknown>;
      const finding = { ...(raw as Record<string, unknown>) };
      const path = typeof finding.file === 'string' ? finding.file.trim() : '';
      if (path) {
        try {
          finding.blob_sha = execFileSync('git', ['rev-parse', `${candidateSha}:${path}`], {
            cwd: WORKSPACE, encoding: 'utf-8', stdio: ['ignore', 'pipe', 'pipe'],
          }).trim();
        } catch {
          // Cross-cutting/deleted/renamed paths have no candidate blob. The
          // finding remains usable, but cannot be reused by exact blob identity.
        }
      }
      return finding;
    });

    // Build explicit per-path review verdicts grounded in the diff scope actually
    // reviewed. Every changed path starts as 'clean'; any path that has at least
    // one finding against it is upgraded to 'finding'. Paths outside the reviewed
    // scope (not in auditedChangedPaths) are absent — this lets the backend
    // distinguish "reviewed and cleared" from "not in scope / carried forward".
    const reviewedPaths: Record<string, 'clean' | 'finding'> = {};
    for (const p of auditedChangedPaths) {
      reviewedPaths[p] = 'clean';
    }
    if (rawFindings !== null) {
      for (const f of (findings as Record<string, unknown>[])) {
        const fpath = typeof f.file === 'string' ? f.file.trim() : '';
        if (fpath && Object.prototype.hasOwnProperty.call(reviewedPaths, fpath)) {
          reviewedPaths[fpath] = 'finding';
        }
      }
    }

    return {
      ...output,
      ...(rawFindings === null ? {} : { findings }),
      ...(computedTestErosion === null ? {} : { computed_test_erosion: computedTestErosion }),
      reviewed_paths: reviewedPaths,
      audit_scope: {
        mode: auditScopeMode,
        requested_base_sha: requestedAuditBaseSha,
        effective_base_sha: auditScopeMode === 'incremental' ? requestedAuditBaseSha : null,
        candidate_sha: candidateSha,
        diff_range: diffRange,
        goal_category_diff_range: goalCategoryDiffRange,
        changed_paths: auditedChangedPaths,
        fallback_reason: auditScopeFallbackReason,
      },
    };
  };

  // Inject computed_test_erosion into the artifact so the backend can use it
  // as the authoritative signal rather than the LLM's self-reported test_erosion.
  // deleted_test_file_classifications (the per-file dead_code_removal /
  // genuine_erosion disposition) is carried through verbatim from the parsed
  // handler output so it reaches release_scoring._score_audit, which retired the
  // hardcoded AUTHORIZED_TEST_DELETIONS allowlist in favour of this signal.
  const enrichedOutput = handlerOutput !== null
    ? enrichAuditOutput(handlerOutput)
    : handlerOutput;

  let logs = '';
  let releaseAuditUnparseable = false;
  if (enrichedOutput !== null) {
    logs = sanitize(JSON.stringify(enrichedOutput));
  } else {
    const priorAnalysis = readAuditProgress(taskId);
    const priorAnalysisSection = priorAnalysis ? `${priorAnalysis}\n\n` : '';
    const repaired = await repairStructuredOutput<Record<string, unknown>>({
      mode: 'object',
      requiredKeys: ['audit_score', 'findings'],
      schemaLabel: 'release_audit',
      first: { exitCode, allOutput, lastResultText },
      reopenPrompt: `${priorAnalysisSection}Your prior release review response did not end with a parseable JSON report object.
Output ONLY the JSON object now — with top-level "audit_score", "test_erosion", and "findings".${versionGoal ? ' Also include "covers_goal", "coverage_score", "biggest_gap", "category_verdicts", and "residual_uncaptured_gap" from the requested holistic goal review.' : ''} Each finding must include "severity", "summary", "file", and "verification_note". No markdown, no code fences, no prose.`,
      ws,
      statusRef,
      model,
      effort,
      disallowedTools,
      retryStatus: 'Repairing release audit JSON...',
    });
    if (repaired.value !== null) {
      const enrichedExtracted = enrichAuditOutput(repaired.value);
      logs = sanitize(JSON.stringify(enrichedExtracted));
    } else {
      releaseAuditUnparseable = exitCode === 0;
      logs = sanitize(lastResultText || allOutput.slice(-50_000));
    }
  }

  await persistAndPostStatus(taskId, {
    task_id: taskId,
    status: releaseAuditUnparseable || exitCode !== 0 ? 'FAILED' : 'COMPLETED',
    ...(releaseAuditUnparseable
      ? { artifact_type: 'ERROR_LOG', failure_category: 'release_audit_report_unparseable' }
      : exitCode !== 0
        ? { artifact_type: 'ERROR_LOG', failure_category: 'claude_code_failed' }
        : { artifact_type: 'RELEASE_AUDIT_REPORT' }),
    logs,
  });
}
