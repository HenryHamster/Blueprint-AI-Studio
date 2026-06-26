/**
 * Release audit challenge handler — independent re-examination of audit findings.
 *
 * Spawned during the release qualification gate to independently verify release-audit
 * findings. Takes the parent audit's payload (base_branch, diff_range, candidate_sha)
 * and produces a structured verdict (issues found, severity assessments, recommended
 * actions) that can affirm or challenge the parent audit's conclusions.
 *
 * Used for adversarial review: if the parent audit missed critical issues or
 * over-flagged benign changes, the challenge handler surfaces those disagreements.
 * Verdicts are persisted on ReleaseVersion.last_qualification for merge-gating logic.
 */

import type WebSocket from 'ws';
import {
  WORKSPACE, strVal, sanitize, sendLog, validateBranchName, readHandlerOutput,
  persistAndPostStatus, runClaudeCode, sendHeartbeatSync,
  setCurrentPromptFingerprint, resolveBaseBranch,
} from '../index.js';
import { prepareWorkspaceForRole } from '../workspacePrep.js';
import {
  PLAYBOOK_GUIDANCE, FIRST_TASK_GUIDANCE, DEFAULT_TOOL_GUIDANCE, READ_ONLY_BASH_WRITE_NOTE,
  readClaudeMd, buildContextPrefix, buildFileTree, configureAgentPermissions,
  writePlaybookHelper, writeMessagesHelper, formatPlaybook, buildRetryContext,
  computePromptFingerprint, type PlaybookEntry,
} from '../prompt.js';
import { resolveBuildConfig } from '../build-config.js';
import { repairStructuredOutput } from './structured-output-repair.js';
import {
  resolveCandidateSha, provisionAuditFetchHelpers, designDocAdvertisement,
  fetchCandidateCommitWithRetry,
} from './audits.js';

// ── Release-audit-challenge contract ─────────────────────────────────────────
//
// The binding second-opinion artifact a `release_audit_challenge` task emits.
// A backend consumer (release_qualifier) reads this shape VERBATIM, so the keys
// and the verdict enum below are a stable contract — do not rename without
// changing the consumer.
//
//   {
//     "challenged_candidate_sha": "<40-hex>",
//     "verdicts": [
//       { "primary_finding_index": 1,
//         "severity": "high", "summary": "<matches a primary finding>",
//         "file": "<path|''>",
//         "verdict": "confirmed" | "refuted_false_positive" | "uncertain",
//         "reason": "<auditable >=20-char justification>" }
//     ]
//   }

/** Verdict enum a challenge entry may carry. */
export const CHALLENGE_VERDICTS = [
  'confirmed',
  'refuted_false_positive',
  'uncertain',
] as const;
export type ChallengeVerdict = (typeof CHALLENGE_VERDICTS)[number];

/**
 * Minimum non-whitespace length of a `reason` for a substantive
 * confirmed/refuted_false_positive verdict. Mirrors the backend audit gate's
 * `verification_note` requirement (_finding_is_verified) so a challenge can
 * never assert a false-positive exclusion without an auditable justification.
 */
export const MIN_CHALLENGE_REASON_LEN = 20;

/**
 * Severities the primary scoring would actually weigh (mirror of the backend
 * `_SEVERITIES_REQUIRING_VERIFICATION` set in release_scoring.py). Only findings
 * at these severities need a challenge verdict — low/info never deduct, so they
 * need no second opinion.
 */
export const CHALLENGE_SEVERITIES_REQUIRING_VERDICT = [
  'critical', 'blocker', 'high', 'medium', 'moderate', 'warning',
];

export interface ChallengeVerdictEntry {
  primary_finding_index?: number;
  severity: string;
  summary: string;
  file: string;
  verdict: ChallengeVerdict;
  reason: string;
}

export interface ChallengeArtifact {
  challenged_candidate_sha: string;
  verdicts: ChallengeVerdictEntry[];
}

type PrimaryFinding = { severity: string; summary: string; file: string };

type ChallengeArtifactShapeValidationResult =
  | { ok: true }
  | { ok: false; reason: 'parsed_object_schema_mismatch'; detail: string };

function validateChallengeArtifactShape(raw: unknown): ChallengeArtifactShapeValidationResult {
  const hasOwn = (key: string): boolean =>
    Object.prototype.hasOwnProperty.call(raw as object, key);
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    return {
      ok: false,
      reason: 'parsed_object_schema_mismatch',
      detail: 'release audit challenge output was not a JSON object',
    };
  }
  const obj = raw as Record<string, unknown>;
  if (!hasOwn('challenged_candidate_sha')) {
    return {
      ok: false,
      reason: 'parsed_object_schema_mismatch',
      detail: 'release audit challenge output missed challenged_candidate_sha',
    };
  }
  if (typeof obj.challenged_candidate_sha !== 'string' || obj.challenged_candidate_sha.trim() === '') {
    return {
      ok: false,
      reason: 'parsed_object_schema_mismatch',
      detail: 'release audit challenge output had an invalid challenged_candidate_sha',
    };
  }
  if (!hasOwn('verdicts')) {
    return {
      ok: false,
      reason: 'parsed_object_schema_mismatch',
      detail: 'release audit challenge output missed verdicts',
    };
  }
  if (!Array.isArray(obj.verdicts)) {
    return {
      ok: false,
      reason: 'parsed_object_schema_mismatch',
      detail: 'release audit challenge output had a non-list verdicts field',
    };
  }
  return { ok: true };
}

function nonWhitespaceLen(s: string): number {
  let n = 0;
  for (const c of s) {
    if (!/\s/.test(c)) n++;
  }
  return n;
}

/**
 * Read optional primary audit findings passed in the task payload. The
 * challenge runs in parallel with the primary audit during qualification, so
 * these are usually absent at spawn time (the handler then self-derives the
 * higher-severity findings from the diff). When a future flow has the audit
 * report first, it can pass `primary_findings` and the handler judges THOSE.
 */
export function parsePrimaryFindings(payload: Record<string, unknown>): PrimaryFinding[] {
  const raw = payload.primary_findings;
  if (!Array.isArray(raw)) return [];
  const out: PrimaryFinding[] = [];
  for (const f of raw) {
    if (!f || typeof f !== 'object') continue;
    const o = f as Record<string, unknown>;
    const severity = strVal(o.severity || o.level).trim().toLowerCase() || 'unknown';
    const summary = strVal(o.summary || o.description || o.title).trim();
    if (!summary) continue;
    const file = strVal(o.file || o.file_path).trim();
    out.push({ severity, summary, file });
  }
  return out;
}

/**
 * Coerce an arbitrary parsed object into the stable RELEASE_AUDIT_CHALLENGE
 * contract shape. The `challenged_candidate_sha` is always forced to the pinned
 * candidate (never trusting whatever the LLM wrote), and each verdict is
 * validated against {@link CHALLENGE_VERDICTS}. A confirmed/refuted_false_positive
 * verdict whose `reason` is missing or shorter than
 * {@link MIN_CHALLENGE_REASON_LEN} non-whitespace chars is DOWNGRADED to
 * `uncertain` — the advisory output must never assert a substantive
 * confirmation/refutation without an auditable justification.
 */
export function normalizeChallengeOutput(
  raw: unknown, candidateSha: string, primaryFindings?: PrimaryFinding[],
): ChallengeArtifact {
  const out: ChallengeArtifact = { challenged_candidate_sha: candidateSha, verdicts: [] };
  if (!raw || typeof raw !== 'object') return out;
  const rawVerdicts = (raw as Record<string, unknown>).verdicts;
  if (!Array.isArray(rawVerdicts)) return out;
  for (const v of rawVerdicts) {
    if (!v || typeof v !== 'object') continue;
    const obj = v as Record<string, unknown>;
    const verdictRaw = strVal(obj.verdict).trim().toLowerCase();
    if (!(CHALLENGE_VERDICTS as readonly string[]).includes(verdictRaw)) continue;
    let verdict = verdictRaw as ChallengeVerdict;
    let severity = strVal(obj.severity).trim().toLowerCase() || 'unknown';
    const primaryFindingIndexRaw = obj.primary_finding_index;
    const primaryFindingIndex = (
      typeof primaryFindingIndexRaw === 'number' &&
      Number.isInteger(primaryFindingIndexRaw) &&
      primaryFindingIndexRaw > 0
    ) ? primaryFindingIndexRaw : undefined;
    let resolvedPrimaryFindingIndex: number | undefined;
    let file = strVal(obj.file || obj.file_path).trim();
    let summary = strVal(obj.summary || obj.description || obj.title).trim();
    if (
      primaryFindingIndex !== undefined &&
      primaryFindings !== undefined &&
      primaryFindingIndex <= primaryFindings.length
    ) {
      const primaryFinding = primaryFindings[primaryFindingIndex - 1];
      resolvedPrimaryFindingIndex = primaryFindingIndex;
      severity = primaryFinding.severity;
      summary = primaryFinding.summary;
      file = primaryFinding.file;
    }
    if (!summary) continue;
    const reason = strVal(obj.reason || obj.verification_note).trim();
    if (
      (verdict === 'confirmed' || verdict === 'refuted_false_positive') &&
      nonWhitespaceLen(reason) < MIN_CHALLENGE_REASON_LEN
    ) {
      verdict = 'uncertain';
    }
    const entry: ChallengeVerdictEntry = { severity, summary, file, verdict, reason };
    if (resolvedPrimaryFindingIndex !== undefined) entry.primary_finding_index = resolvedPrimaryFindingIndex;
    out.verdicts.push(entry);
  }
  return out;
}

/** Render the primary findings (if any) as a numbered list for the prompt. */
function formatPrimaryFindingsSection(findings: PrimaryFinding[]): string {
  if (findings.length === 0) {
    return (
      'No primary audit findings were supplied in your payload. Independently ' +
      'identify the higher-severity issues in this diff yourself (those at ' +
      `severity ${CHALLENGE_SEVERITIES_REQUIRING_VERDICT.join('/')}), then judge ` +
      'each one with a verdict. When no primary findings were supplied, omit `primary_finding_index` ' +
      'from each verdict entry — the backend can fuzzy-match it by content.\n\n'
    );
  }
  const lines = findings.map((f, i) =>
    `${i + 1}. [${f.severity}] ${f.summary}${f.file ? ` (file: ${f.file})` : ''}`,
  );
  return (
    'The primary release audit reported these higher-severity findings. ' +
    'Independently re-investigate EACH against the real diff and the full repo, ' +
    'and emit one verdict per finding:\n' + lines.join('\n') + '\n\n' +
    'IMPORTANT — index and verbatim summary requirement: for each verdict, set ' +
    '`primary_finding_index` to the 1-based number from this list (1 for the first ' +
    'finding, 2 for the second, etc.), and copy that finding\'s `summary` text EXACTLY ' +
    'into the `summary` field and its listed severity EXACTLY into the `severity` field — ' +
    'verbatim: same words, punctuation, capitalization, and severity label; do not paraphrase. ' +
    'The backend uses `primary_finding_index` first, then falls back to exact string ' +
    'matching on `summary` to bind verdicts to findings; its `summary` must be copied EXACTLY from that list. ' +
    'Do not paraphrase the `summary` on a primary-findings retry — paraphrasing breaks ' +
    'the binding and causes the qualification to be held.\n\n'
  );
}

// ── Handler: Release Audit Challenge ─────────────────────────────────────────
//
// An independent adversarial second opinion on a release_audit's higher-severity
// findings. Like handleReleaseAudit it clones the base branch with FULL history,
// fetches the PINNED candidate SHA, and fails loud (diff_range_unresolvable) if
// the candidate / merge-base diff range cannot be resolved — an unaudited
// challenge is worse than none. It then re-judges each higher-severity finding
// and emits a BINDING RELEASE_AUDIT_CHALLENGE artifact. A backend consumer
// (release_qualifier) uses the verdicts as a fail-closed gate: verified
// higher-severity findings must be challenger-confirmed to deduct from the audit
// sub-score, and missing/uncertain verdicts HOLD the qualification attempt.
export async function handleReleaseAuditChallenge(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  // CRITICAL: clone base_branch (the reference branch), never the candidate's
  // own branch — the challenge judges the incoming diff by the base's standards.
  const rawBaseBranch = resolveBaseBranch(payload, task);
  const baseBranch = validateBranchName(rawBaseBranch, 'base_branch');

  // The challenge MUST score one pinned candidate commit (same contract as the
  // release_audit). A missing/malformed candidate_sha is a real contract bug —
  // fail loud rather than diff a non-resolvable range.
  const candidateSha = resolveCandidateSha(payload, task);
  if (!candidateSha) {
    const rawSeen = strVal(payload.candidate_sha) || strVal(
      (task.context && typeof task.context === 'object' && !Array.isArray(task.context)
        ? (task.context as Record<string, unknown>).candidate_sha
        : undefined),
    );
    sendLog(ws, `[ERROR] Release audit challenge aborted: no valid candidate_sha in task payload.\n`);
    await persistAndPostStatus(taskId, {
      task_id: taskId,
      status: 'FAILED',
      artifact_type: 'ERROR_LOG',
      failure_category: 'diff_range_unresolvable',
      logs: sanitize(
        `Release audit challenge aborted: the task payload carries no valid ` +
        `candidate_sha. The release qualifier must pin the candidate commit by ` +
        `passing a full 40-char commit SHA as payload.candidate_sha. Without it ` +
        `there is no commit to diff against ${baseBranch}. ` +
        `Saw: ${rawSeen ? `"${rawSeen}"` : '(absent)'}`,
      ),
    });
    return;
  }
  // Three-dot (merge-base) diff of the pinned candidate vs the base branch.
  const diffRange = `${baseBranch}...${candidateSha}`;

  statusRef.value = `Cloning ${owner}/${repo} (${baseBranch}) for release audit challenge...`;
  sendLog(ws, `Starting release audit challenge — cloning ${baseBranch}, candidate ${candidateSha.slice(0, 12)}...\n`);
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  // A FULL clone (depth 0) is required: on a long-diverged candidate a shallow
  // clone leaves the merge-base unreachable and the three-dot diff is wrong.
  await prepareWorkspaceForRole({
    workspace: WORKSPACE, repoUrl: cloneUrl, githubToken: ghToken,
    branch: baseBranch, baseBranch, depth: 0,
  });

  // Fetch the EXACT candidate commit so git diff can compute the merge-base
  // diff (do NOT check it out — the working tree stays on baseBranch). A SHA
  // fetch leaves a real commit object behind where a bare-branch fetch only
  // updates FETCH_HEAD. Bounded retry-with-backoff so a transient GitHub I/O
  // blip on this one fetch does not torch the whole qualification attempt;
  // pre-flight-verify the diff range and fail loud below if it stays
  // unresolvable. Mirrors handleReleaseAudit's diff_range_unresolvable guard.
  sendLog(ws, `Fetching candidate commit ${candidateSha.slice(0, 12)} (full history) for diff computation...\n`);
  const { diffRangeResolves, diffRangeFailDetail } =
    await fetchCandidateCommitWithRetry(candidateSha, diffRange, ws);

  if (!diffRangeResolves) {
    sendLog(ws, `Aborting release audit challenge: cannot compute ${diffRange}.\n`);
    await persistAndPostStatus(taskId, {
      task_id: taskId,
      status: 'FAILED',
      artifact_type: 'ERROR_LOG',
      failure_category: 'diff_range_unresolvable',
      logs: sanitize(
        `Release audit challenge aborted: diff range ${diffRange} does not ` +
        `resolve. The candidate commit ${candidateSha} could not be fetched, or ` +
        `the merge-base of ${baseBranch} and the candidate is unreachable. ` +
        `Detail: ${diffRangeFailDetail || '(no detail)'}`,
      ),
    });
    return;
  }

  writePlaybookHelper(taskId);
  writeMessagesHelper(taskId);
  const challengeFetchGuidance = provisionAuditFetchHelpers(taskId, task);
  const disallowedTools = configureAgentPermissions('release_audit_challenge');

  const contextPrefix = buildContextPrefix(task);
  const claudeMd = readClaudeMd();
  const codebaseSummary = claudeMd ? `## Codebase Instructions\n${claudeMd}\n\n` : '';
  const fileTreeSection = buildFileTree(ws);
  const playbookSection = formatPlaybook(task.playbook as Record<string, PlaybookEntry | string> | undefined);
  const retryContext = buildRetryContext(task);
  const firstTaskSection = task.first_task_on_repo ? FIRST_TASK_GUIDANCE : '';

  const primaryFindings = parsePrimaryFindings(payload);
  const primaryFindingsSection = formatPrimaryFindingsSection(primaryFindings);

  const versionGoal = typeof payload.version_goal === 'string' && payload.version_goal.length > 0
    ? payload.version_goal
    : null;
  const versionGoalSection = versionGoal
    ? `\n## Version Goal\n\n${versionGoal}\n\nThis is the strategic objective the version aimed to deliver. As optional metadata (does NOT affect the \`verdicts\` structure), you may add a \`goal_alignment\` object to your JSON output if you disagree with or wish to amend the primary audit's assessment. Use the same canonical schema:\n- \`state\` (string): one of \`"aligned"\`, \`"shortfall"\`, or \`"not_applicable"\`.\n- \`assessed_grade\` (float 0.0–1.0): degree of alignment.\n- \`biggest_gap\` (string or null): one sentence on the most significant gap if state is \`"shortfall"\`; null otherwise.\n- \`reasoning_summary\` (string): 1–3 sentences explaining your assessment.\n\nOmit \`goal_alignment\` entirely if the primary audit did not include it or you have no meaningful disagreement.\n\n`
    : '';

  const challengePrompt = `${codebaseSummary}${playbookSection}${retryContext}${contextPrefix}${fileTreeSection}You are a release-audit CHALLENGER — an independent, adversarial second opinion. A primary release-audit agent already reviewed the diff range \`${diffRange}\` and produced findings. Your job is NOT to repeat that audit, but to skeptically RE-JUDGE its higher-severity findings against the real code. \`${candidateSha}\` is the PINNED release candidate commit; it has already been fetched into this clone on \`${baseBranch}\`, so the diff range resolves.

## Your task

1. **Obtain the diff.** Run \`git diff ${diffRange}\` (and \`git diff --stat ${diffRange}\`, \`git log ${diffRange} --oneline\`) to see exactly what the candidate \`${candidateSha}\` changes relative to \`${baseBranch}\`. Always use the pinned SHA \`${candidateSha}\`, never the bare branch name.

2. **Re-judge each higher-severity finding.** ${primaryFindingsSection}For EACH finding you judge, READ the relevant unchanged files in the full repository (not just the diff) to check for existing mitigations (guards, exception handlers, per-request checks, hardcoded constants at the call site). Then assign exactly one verdict:
   - \`confirmed\` — you independently reproduced the issue against the real code; it is a genuine problem.
   - \`refuted_false_positive\` — you investigated and the issue does NOT hold once you account for an existing mitigation (e.g. a guard, check, or constraint already present at the call site neutralises the reported problem).
   - \`uncertain\` — you could not confidently confirm or refute it from the available code.
${versionGoalSection}
3. **Emit results.** Write your verdicts as a JSON object to \`/tmp/release_audit_challenge_${taskId}.json\` with EXACTLY this structure:

\`\`\`json
{
  "challenged_candidate_sha": "${candidateSha}",
  "verdicts": [
    {
      "primary_finding_index": 1,
      "severity": "high",
      "summary": "EXACT primary finding summary text when primary_findings were supplied; otherwise one sentence describing the finding",
      "file": "path/to/affected/file.py",
      "verdict": "confirmed",
      "reason": "<auditable justification citing the specific files/lines you read, e.g. '<file>:<lines> — the finding holds because …'>"
    }
  ]
}
\`\`\`

Field semantics:
- \`challenged_candidate_sha\`: the pinned candidate SHA — use exactly \`${candidateSha}\`.
- \`verdicts\`: one entry per higher-severity finding you judged. Each entry has \`severity\` (critical|blocker|high|medium|moderate|warning), \`summary\` (when primary findings were listed above, copy the exact text from the numbered list character-for-character — the backend uses exact string matching to bind verdicts to findings, so paraphrasing breaks the binding; when deriving findings independently, one descriptive sentence), \`file\` (file path or empty string if cross-cutting), \`verdict\` (one of: confirmed | refuted_false_positive | uncertain), and \`reason\`.
- \`reason\`: an auditable justification (≥ 20 non-whitespace characters) citing the specific files/lines you read. It is REQUIRED for \`confirmed\` and \`refuted_false_positive\`; a verdict of those types WITHOUT a substantive reason will be downgraded to \`uncertain\`.

Rules:
- Only judge findings the primary scoring would weigh — severity ${CHALLENGE_SEVERITIES_REQUIRING_VERDICT.join('/')}. Low/info findings do NOT need a verdict; omit them.
- Read the actual code before each verdict — do NOT speculate.
- IMPORTANT: Write ONLY the JSON object to \`/tmp/release_audit_challenge_${taskId}.json\` ${READ_ONLY_BASH_WRITE_NOTE} — no markdown, no code fences, no extra text.
- Also output the same JSON object as the final output in your logs.
- Do NOT modify any files in the repository.${designDocAdvertisement(task)}`;

  const buildConfig = resolveBuildConfig(task);
  const promptSuffix = payload.prompt_suffix ? `\n\nAdditional instructions:\n${strVal(payload.prompt_suffix)}` : '';
  statusRef.value = 'Running release audit challenge...';
  const challengeFingerprint = computePromptFingerprint('release_audit_challenge', buildConfig);
  setCurrentPromptFingerprint(challengeFingerprint);
  sendHeartbeatSync(taskId, statusRef.value, { prompt_fingerprint: challengeFingerprint });
  const { exitCode, allOutput, lastResultText } = await runClaudeCode(
    challengePrompt + firstTaskSection + promptSuffix + PLAYBOOK_GUIDANCE + `\n\n${challengeFetchGuidance}${DEFAULT_TOOL_GUIDANCE}`,
    ws, statusRef, model, effort, disallowedTools,
  );

  const handlerOutput = readHandlerOutput(taskId, 'release_audit_challenge', ws) as Record<string, unknown> | null;

  let logs = '';
  let challengeUnparseable = false;
  if (handlerOutput !== null && validateChallengeArtifactShape(handlerOutput).ok) {
    logs = sanitize(JSON.stringify(normalizeChallengeOutput(handlerOutput, candidateSha, primaryFindings)));
  } else {
    const reopenPrompt = primaryFindings.length > 0
      ? `${primaryFindingsSection}Your prior release audit challenge response did not end with a parseable JSON verdict object.\nOutput ONLY the JSON object now — with top-level "challenged_candidate_sha" and "verdicts". Each verdict must include "severity", "summary", "file", "verdict" (one of confirmed | refuted_false_positive | uncertain), and "reason". If the original prompt supplied a numbered primary finding list, also include "primary_finding_index", copy the listed primary finding summary exactly into "summary", and copy its listed severity exactly into "severity". Use {"challenged_candidate_sha": "${candidateSha}", "verdicts": []} if there is nothing to challenge. No markdown, no code fences, no prose.`
      : `${lastResultText || ''}\n${allOutput || ''}\n\nYour prior release audit challenge response did not end with a parseable JSON verdict object.
Output ONLY the JSON object now — with top-level "challenged_candidate_sha" and "verdicts". Each verdict must include "severity", "summary", "file", "verdict" (one of confirmed | refuted_false_positive | uncertain), and "reason". If the original prompt supplied a numbered primary finding list, also include "primary_finding_index", copy the listed primary finding summary exactly into "summary", and copy its listed severity exactly into "severity". Use {"challenged_candidate_sha": "${candidateSha}", "verdicts": []} if there is nothing to challenge. No markdown, no code fences, no prose.`;
    const repaired = await repairStructuredOutput<Record<string, unknown>>({
      mode: 'object',
      requiredKeys: ['challenged_candidate_sha', 'verdicts'],
      first: { exitCode, allOutput, lastResultText },
      reopenPrompt,
      ws,
      statusRef,
      model,
      effort,
      disallowedTools,
      retryStatus: 'Repairing release audit challenge JSON...',
      schemaLabel: 'audit_challenge',
      validateParsedValue: validateChallengeArtifactShape,
    });
    if (repaired.value !== null) {
      logs = sanitize(JSON.stringify(normalizeChallengeOutput(repaired.value, candidateSha, primaryFindings)));
    } else {
      challengeUnparseable = exitCode === 0;
      logs = sanitize(lastResultText || allOutput.slice(-50_000));
    }
  }

  await persistAndPostStatus(taskId, {
    task_id: taskId,
    status: challengeUnparseable || exitCode !== 0 ? 'FAILED' : 'COMPLETED',
    ...(challengeUnparseable
      ? { artifact_type: 'ERROR_LOG', failure_category: 'release_audit_challenge_unparseable' }
      : exitCode !== 0
        ? { artifact_type: 'ERROR_LOG', failure_category: 'claude_code_failed' }
        : { artifact_type: 'RELEASE_AUDIT_CHALLENGE' }),
    logs,
  });
}
