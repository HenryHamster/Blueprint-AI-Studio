import type WebSocket from 'ws';
import {
  DEFAULT_TOOL_GUIDANCE,
  WORKSPACE,
  buildContextPrefix,
  buildFileTree,
  buildPromptSizeTelemetry,
  computePromptFingerprint,
  configureAgentPermissions,
  git,
  persistAndPostStatus,
  prepareWorkspaceForRole,
  providerAutoInjectsClaudeMd,
  readClaudeMd,
  resolveBaseBranch,
  runClaudeCode,
  sanitize,
  sendHeartbeatSync,
  sendLog,
  setCurrentPromptFingerprint,
  setPromptSizeTelemetry,
  strVal,
  validateBranchName,
} from './discovery-review-deps.js';
import {
  repairStructuredOutput,
  type ParsedValueValidationResult,
} from './structured-output-repair.js';

export const DISCOVERY_VERDICTS = [
  'renew',
  'pivot',
  'expand',
  'branch',
  'fallback_pending',
  'park',
  'stop',
  'ask_operator',
  'inconclusive',
] as const;
export type DiscoveryVerdict = (typeof DISCOVERY_VERDICTS)[number];

export const DISCOVERY_PHASES = [
  'probe',
  'demonstrate',
  'productize',
  'release',
] as const;
export type DiscoveryPhase = (typeof DISCOVERY_PHASES)[number];

export interface DiscoveryRecommendedLimits {
  task_credit_limit: number | null;
  attempt_credit_limit: number | null;
  concurrency_limit: number | null;
}

export interface DiscoveryReviewResult {
  cycle_id: string;
  reviewed_revision: number;
  verdict: DiscoveryVerdict;
  confidence: number;
  decision_summary: string;
  goal_progress_assessment: string;
  evidence_citations: string[];
  contradictions: string[];
  next_bet: Record<string, unknown> | null;
  recommended_phase: DiscoveryPhase;
  recommended_limits: DiscoveryRecommendedLimits;
  operator_question: string | null;
}

interface DiscoveryReviewValidationContext {
  cycleId: string;
  expectedRevision: number;
  evidenceIds: ReadonlySet<string>;
  evidenceCount: number;
  requiredEvidenceOmitted: boolean;
}

const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const GIT_SHA_PATTERN = /^[0-9a-f]{7,40}$/i;
const DISCOVERY_RESULT_KEYS = [
  'cycle_id',
  'reviewed_revision',
  'verdict',
  'confidence',
  'decision_summary',
  'goal_progress_assessment',
  'evidence_citations',
  'contradictions',
  'next_bet',
  'recommended_phase',
  'recommended_limits',
  'operator_question',
] as const;
const LIMIT_KEYS = [
  'task_credit_limit',
  'attempt_credit_limit',
  'concurrency_limit',
] as const;

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === 'object' && !Array.isArray(value);
}

function hasExactKeys(
  obj: Record<string, unknown>,
  keys: readonly string[],
): boolean {
  const actual = Object.keys(obj).sort();
  const expected = [...keys].sort();
  return actual.length === expected.length
    && actual.every((key, index) => key === expected[index]);
}

function isNullablePositiveInteger(value: unknown): value is number | null {
  return value === null
    || (typeof value === 'number' && Number.isInteger(value) && value > 0);
}

/**
 * Validate the binding discovery verdict without coercing ambiguous model
 * output. Identity fields must match the frozen packet and every citation must
 * name an evidence item selected by the server.
 */
export function validateDiscoveryReviewShape(
  raw: unknown,
  context: DiscoveryReviewValidationContext,
): ParsedValueValidationResult {
  if (!isPlainObject(raw) || !hasExactKeys(raw, DISCOVERY_RESULT_KEYS)) {
    return {
      ok: false,
      reason: 'parsed_object_schema_mismatch',
      detail: 'discovery review must be an object with exactly the required top-level keys',
    };
  }
  if (raw.cycle_id !== context.cycleId
      || raw.reviewed_revision !== context.expectedRevision) {
    return {
      ok: false,
      reason: 'parsed_object_schema_mismatch',
      detail: 'cycle_id or reviewed_revision did not match the frozen review packet',
    };
  }
  if (typeof raw.verdict !== 'string'
      || !(DISCOVERY_VERDICTS as readonly string[]).includes(raw.verdict)) {
    return {
      ok: false,
      reason: 'parsed_object_schema_mismatch',
      detail: 'verdict was outside the discovery verdict vocabulary',
    };
  }
  if (typeof raw.confidence !== 'number'
      || !Number.isFinite(raw.confidence)
      || raw.confidence < 0
      || raw.confidence > 1) {
    return {
      ok: false,
      reason: 'parsed_object_schema_mismatch',
      detail: 'confidence must be a finite number from 0 through 1',
    };
  }
  if (typeof raw.decision_summary !== 'string' || !raw.decision_summary.trim()
      || typeof raw.goal_progress_assessment !== 'string'
      || !raw.goal_progress_assessment.trim()) {
    return {
      ok: false,
      reason: 'parsed_object_schema_mismatch',
      detail: 'decision_summary and goal_progress_assessment must be non-empty strings',
    };
  }
  if (!Array.isArray(raw.evidence_citations)
      || raw.evidence_citations.some(
        value => typeof value !== 'string' || !context.evidenceIds.has(value),
      )
      || new Set(raw.evidence_citations).size !== raw.evidence_citations.length) {
    return {
      ok: false,
      reason: 'parsed_object_schema_mismatch',
      detail: 'evidence_citations must be unique IDs from the frozen evidence packet',
    };
  }
  if (context.evidenceCount === 0
      && raw.verdict !== 'inconclusive'
      && raw.verdict !== 'ask_operator') {
    return {
      ok: false,
      reason: 'parsed_object_schema_mismatch',
      detail: 'a packet with no evidence can only support inconclusive or ask_operator',
    };
  }
  if (context.requiredEvidenceOmitted
      && raw.verdict !== 'inconclusive'
      && raw.verdict !== 'ask_operator') {
    return {
      ok: false,
      reason: 'parsed_object_schema_mismatch',
      detail: 'a packet with omitted required evidence can only support inconclusive or ask_operator',
    };
  }
  if (context.evidenceCount > 0
      && raw.evidence_citations.length === 0
      && raw.verdict !== 'inconclusive'
      && raw.verdict !== 'ask_operator') {
    return {
      ok: false,
      reason: 'parsed_object_schema_mismatch',
      detail: 'a consequential verdict must cite at least one packet evidence ID',
    };
  }
  if (!Array.isArray(raw.contradictions)
      || raw.contradictions.some(
        value => typeof value !== 'string' || !value.trim(),
      )) {
    return {
      ok: false,
      reason: 'parsed_object_schema_mismatch',
      detail: 'contradictions must be an array of non-empty strings',
    };
  }
  if (raw.next_bet !== null && !isPlainObject(raw.next_bet)) {
    return {
      ok: false,
      reason: 'parsed_object_schema_mismatch',
      detail: 'next_bet must be an object or null',
    };
  }
  if (typeof raw.recommended_phase !== 'string'
      || !(DISCOVERY_PHASES as readonly string[]).includes(raw.recommended_phase)) {
    return {
      ok: false,
      reason: 'parsed_object_schema_mismatch',
      detail: 'recommended_phase was outside the discovery phase vocabulary',
    };
  }
  const recommendedLimits = raw.recommended_limits;
  if (!isPlainObject(recommendedLimits)
      || !hasExactKeys(recommendedLimits, LIMIT_KEYS)
      || LIMIT_KEYS.some(
        key => !isNullablePositiveInteger(recommendedLimits[key]),
      )) {
    return {
      ok: false,
      reason: 'parsed_object_schema_mismatch',
      detail: 'recommended_limits must contain exactly three nullable positive-integer limits',
    };
  }
  if (raw.operator_question !== null
      && (typeof raw.operator_question !== 'string'
        || !raw.operator_question.trim())) {
    return {
      ok: false,
      reason: 'parsed_object_schema_mismatch',
      detail: 'operator_question must be a non-empty string or null',
    };
  }
  if (raw.verdict === 'ask_operator'
      && (typeof raw.operator_question !== 'string'
        || !raw.operator_question.trim())) {
    return {
      ok: false,
      reason: 'parsed_object_schema_mismatch',
      detail: 'ask_operator requires a concrete operator_question',
    };
  }
  return { ok: true };
}

export function normalizeDiscoveryReviewResult(
  raw: Record<string, unknown>,
  cycleId: string,
  expectedRevision: number,
): DiscoveryReviewResult {
  return {
    ...(raw as unknown as DiscoveryReviewResult),
    // Never trust model-authored identity even after validation. The frozen
    // server packet remains the authority for optimistic-concurrency checks.
    cycle_id: cycleId,
    reviewed_revision: expectedRevision,
  };
}

function reviewContractError(payload: Record<string, unknown>): string | null {
  const cycleId = strVal(payload.cycle_id).trim();
  const expectedRevision = payload.expected_revision;
  if (!UUID_PATTERN.test(cycleId)) return 'cycle_id is missing or malformed';
  if (typeof expectedRevision !== 'number'
      || !Number.isInteger(expectedRevision)
      || expectedRevision < 0) {
    return 'expected_revision must be a non-negative integer';
  }
  if (!Array.isArray(payload.evidence_packet)) {
    return 'evidence_packet must be an array';
  }
  const candidateSha = strVal(payload.candidate_sha).trim();
  if (candidateSha && !GIT_SHA_PATTERN.test(candidateSha)) {
    return 'candidate_sha must be a 7-40 character hexadecimal git commit';
  }
  return null;
}

export function buildDiscoveryReviewPrompt(
  payload: Record<string, unknown>,
  task: Record<string, unknown>,
  resolvedCandidateSha: string | null,
): string {
  const cycleId = strVal(payload.cycle_id);
  const revision = payload.expected_revision as number;
  const packet = Array.isArray(payload.evidence_packet)
    ? payload.evidence_packet
    : [];
  const packetJson = JSON.stringify(packet, null, 2);
  const candidateStatement = resolvedCandidateSha
    ? (
      `The workspace is detached at the exact pinned candidate commit ` +
      `\`${resolvedCandidateSha}\`. Do not inspect or grade any other commit.`
    )
    : (
      'This cycle has no candidate commit. Judge only the frozen intent, bet, ' +
      'and evidence packet; do not invent code evidence.'
    );
  const claudeMd = readClaudeMd();
  const claudeMdPrefix = claudeMd && !providerAutoInjectsClaudeMd()
    ? `## Codebase instructions\n${claudeMd}\n\n`
    : '';
  return `${claudeMdPrefix}${buildContextPrefix(task, { suppressTaskLists: true })}
# Discovery cycle decision review

You are the independent decision reviewer for one completed, bounded discovery
cycle. This is a binding evidence review, not another research turn and not an
implementation-planning task.

${candidateStatement}

## Frozen decision packet

- cycle_id: \`${cycleId}\`
- expected_revision: ${revision}
- direction_key: \`${strVal(payload.direction_key)}\`
- current_phase: \`${strVal(payload.phase)}\`
- outcome_kind: \`${strVal(payload.outcome_kind)}\`
- packet_checksum: \`${strVal(payload.packet_checksum)}\`
- mechanism_fingerprint: \`${strVal(payload.mechanism_fingerprint) || '(none)'}\`
- review_round: ${String(payload.review_round ?? 1)}
- prior_review_task_id: \`${strVal(payload.prior_review_task_id) || '(none)'}\`

${payload.review_round === 2 ? `Canonical proposal to confirm or reject:
\`\`\`json
${JSON.stringify(payload.confirmation_proposal ?? {}, null, 2)}
\`\`\`

Independently judge that proposal against the frozen evidence. If it is
supported, reproduce its verdict, next_bet, recommended_phase, and
recommended_limits exactly in your output. If it is not supported, emit the
different decision you believe the evidence warrants; disagreement will be
sent for adjudication.

` : ''}
Intent snapshot:
\`\`\`json
${JSON.stringify(payload.intent_snapshot ?? {}, null, 2)}
\`\`\`

Bet snapshot:
\`\`\`json
${JSON.stringify(payload.bet_snapshot ?? {}, null, 2)}
\`\`\`

Evidence packet (the only IDs allowed in evidence_citations):
\`\`\`json
${packetJson}
\`\`\`

Server evidence manifest:
\`\`\`json
${JSON.stringify(payload.evidence_packet_manifest ?? {}, null, 2)}
\`\`\`

${buildFileTree(null)}

## Review method

1. Judge progress against the frozen intent and bet, not against a newly
   invented objective and not against a Strategist progress narrative.
2. Use only packet evidence for binding claims. Repository inspection may help
   interpret a pinned item, but it does not create a new citable evidence item.
3. Explicitly identify conflicting evidence and missing proof. Absence of
   evidence is not evidence of success.
4. Pick the smallest defensible next decision. Do not spawn tasks, continue
   research, edit files, or turn speculative possibilities into product work.
5. If the packet cannot support a decision, return \`inconclusive\`. Use
   \`ask_operator\` only when a specific human choice is genuinely required.

Verdict meanings:
- \`renew\`: repeat the same bet with a justified bounded change.
- \`pivot\`: change the bet while keeping the direction.
- \`expand\`: evidence earns a broader or later-phase investment.
- \`branch\`: evidence supports a distinct parallel bet.
- \`fallback_pending\`: primary path failed but the frozen fallback deserves review.
- \`park\`: retain the direction without spending another cycle now.
- \`stop\`: evidence argues against further work on this direction.
- \`ask_operator\`: a concrete operator decision is required.
- \`inconclusive\`: the packet is insufficient or materially contradictory.

## Required output

Output ONLY one JSON object with exactly these keys:

\`\`\`json
{
  "cycle_id": "${cycleId}",
  "reviewed_revision": ${revision},
  "verdict": "renew|pivot|expand|branch|fallback_pending|park|stop|ask_operator|inconclusive",
  "confidence": 0.0,
  "decision_summary": "short decision and why",
  "goal_progress_assessment": "what demonstrably changed relative to the frozen intent",
  "evidence_citations": ["evidence UUIDs from the packet only"],
  "contradictions": ["material conflicts or missing-proof statements"],
  "next_bet": null,
  "recommended_phase": "probe|demonstrate|productize|release",
  "recommended_limits": {
    "task_credit_limit": null,
    "attempt_credit_limit": null,
    "concurrency_limit": null
  },
  "operator_question": null
}
\`\`\`

Limits must be positive integers or null. \`next_bet\` must be an object or
null. A consequential verdict must cite at least one packet evidence UUID.
An empty evidence packet can only support \`inconclusive\` or \`ask_operator\`.
If the server evidence manifest says \`required_evidence_omitted: true\`, the
verdict can only be \`inconclusive\` or \`ask_operator\`.
${DEFAULT_TOOL_GUIDANCE}`;
}

async function failDiscoveryReview(
  taskId: string,
  failureCategory: string,
  message: string,
  ws: WebSocket | null,
): Promise<void> {
  sendLog(ws, `[ERROR] ${message}\n`);
  await persistAndPostStatus(taskId, {
    task_id: taskId,
    status: 'FAILED',
    artifact_type: 'ERROR_LOG',
    failure_category: failureCategory,
    logs: sanitize(message),
  });
}

function discoveryReviewWorkspaceError(expectedHeadSha: string): string | null {
  try {
    const actualHeadSha = git(['rev-parse', 'HEAD']).trim().toLowerCase();
    if (actualHeadSha !== expectedHeadSha) {
      return `review checkout HEAD changed from ${expectedHeadSha} to ${actualHeadSha || '(empty)'}`;
    }
    const dirty = git([
      'status',
      '--porcelain=v1',
      '--untracked-files=all',
    ]).trim();
    if (dirty) {
      return `review checkout was modified:\n${dirty.slice(0, 4_000)}`;
    }
  } catch (error) {
    return `review checkout integrity could not be verified (${String(error)})`;
  }
  return null;
}

export async function handleDiscoveryReview(
  taskId: string,
  payload: Record<string, unknown>,
  task: Record<string, unknown>,
  ghToken: string,
  owner: string,
  repo: string,
  ws: WebSocket | null,
  statusRef: { value: string },
  model?: string,
  effort?: string,
): Promise<void> {
  const contractError = reviewContractError(payload);
  if (contractError) {
    await failDiscoveryReview(
      taskId,
      'discovery_review_contract_invalid',
      `Discovery review aborted: ${contractError}.`,
      ws,
    );
    return;
  }

  const cycleId = strVal(payload.cycle_id);
  const expectedRevision = payload.expected_revision as number;
  const candidateSha = strVal(payload.candidate_sha).trim() || null;
  const baseBranch = validateBranchName(
    resolveBaseBranch(payload, task),
    'base_branch',
  );

  statusRef.value = `Preparing frozen discovery review ${cycleId.slice(0, 8)}...`;
  const cloneUrl =
    `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  await prepareWorkspaceForRole({
    workspace: WORKSPACE,
    repoUrl: cloneUrl,
    githubToken: ghToken,
    branch: baseBranch,
    baseBranch,
    depth: candidateSha ? 0 : 1,
    allowLaneResume: false,
  });

  let resolvedCandidateSha: string | null = null;
  if (candidateSha) {
    try {
      git(['fetch', '--no-tags', 'origin', candidateSha]);
      git(['checkout', '--detach', candidateSha]);
      resolvedCandidateSha = git(['rev-parse', 'HEAD']).toLowerCase();
      if (!/^[0-9a-f]{40}$/.test(resolvedCandidateSha)
          || !resolvedCandidateSha.startsWith(candidateSha.toLowerCase())) {
        throw new Error(
          `resolved HEAD ${resolvedCandidateSha || '(empty)'} does not match ${candidateSha}`,
        );
      }
    } catch (error) {
      await failDiscoveryReview(
        taskId,
        'discovery_review_candidate_unresolvable',
        `Discovery review aborted: pinned candidate ${candidateSha} could not be checked out (${String(error)}).`,
        ws,
      );
      return;
    }
  }
  const reviewHeadSha = git(['rev-parse', 'HEAD']).trim().toLowerCase();

  const disallowedTools = configureAgentPermissions('discovery_review');
  const prompt = buildDiscoveryReviewPrompt(payload, task, resolvedCandidateSha);
  const fingerprint = computePromptFingerprint('discovery_review');
  setCurrentPromptFingerprint(fingerprint);
  const promptTelemetry = buildPromptSizeTelemetry(prompt, [
    ['evidence_packet', JSON.stringify(payload.evidence_packet ?? [])],
    [
      'evidence_packet_manifest',
      JSON.stringify(payload.evidence_packet_manifest ?? {}),
    ],
    ['intent_snapshot', JSON.stringify(payload.intent_snapshot ?? {})],
    ['bet_snapshot', JSON.stringify(payload.bet_snapshot ?? {})],
  ]);
  setPromptSizeTelemetry(
    promptTelemetry.prompt_size_bytes,
    promptTelemetry.prompt_section_count,
    promptTelemetry.prompt_section_sizes,
  );
  sendHeartbeatSync(taskId, statusRef.value, {
    prompt_fingerprint: fingerprint,
    discovery_cycle_id: cycleId,
    discovery_review_revision: expectedRevision,
  });

  statusRef.value = `Reviewing frozen evidence for cycle ${cycleId.slice(0, 8)}...`;
  sendLog(
    ws,
    `Starting discovery review for cycle ${cycleId}, revision ${expectedRevision}, ` +
      `${(payload.evidence_packet as unknown[]).length} evidence item(s).\n`,
  );
  const first = await runClaudeCode(
    prompt,
    ws,
    statusRef,
    model,
    effort,
    disallowedTools,
  );
  const firstWorkspaceError = discoveryReviewWorkspaceError(reviewHeadSha);
  if (firstWorkspaceError) {
    await failDiscoveryReview(
      taskId,
      'discovery_review_workspace_modified',
      `Discovery review aborted: ${firstWorkspaceError}.`,
      ws,
    );
    return;
  }

  const evidenceIds = new Set<string>();
  for (const item of payload.evidence_packet as unknown[]) {
    if (isPlainObject(item) && typeof item.evidence_id === 'string') {
      evidenceIds.add(item.evidence_id);
    }
  }
  const validationContext: DiscoveryReviewValidationContext = {
    cycleId,
    expectedRevision,
    evidenceIds,
    evidenceCount: evidenceIds.size,
    requiredEvidenceOmitted: (
      isPlainObject(payload.evidence_packet_manifest)
      && payload.evidence_packet_manifest.required_evidence_omitted === true
    ),
  };
  const repaired = await repairStructuredOutput<Record<string, unknown>>({
    mode: 'object',
    requiredKeys: [...DISCOVERY_RESULT_KEYS],
    first,
    reopenPrompt:
      `Output ONLY the complete discovery review JSON object. Use cycle_id ` +
      `"${cycleId}" and reviewed_revision ${expectedRevision}. Include exactly ` +
      `these keys: ${DISCOVERY_RESULT_KEYS.join(', ')}. Evidence citations may ` +
      `only use these IDs: ${[...evidenceIds].join(', ') || '(none)'}. No ` +
      `markdown, code fences, or prose.`,
    ws,
    statusRef,
    model,
    effort,
    disallowedTools,
    run: runClaudeCode,
    retryStatus: 'Repairing discovery review verdict JSON...',
    schemaLabel: 'discovery_review',
    validateParsedValue: value =>
      validateDiscoveryReviewShape(value, validationContext),
  });
  const repairedWorkspaceError = discoveryReviewWorkspaceError(reviewHeadSha);
  if (repairedWorkspaceError) {
    await failDiscoveryReview(
      taskId,
      'discovery_review_workspace_modified',
      `Discovery review aborted: ${repairedWorkspaceError}.`,
      ws,
    );
    return;
  }

  if (repaired.value === null) {
    const failedRun = repaired.retry ?? first;
    await failDiscoveryReview(
      taskId,
      first.exitCode !== 0
        ? 'claude_code_failed'
        : 'discovery_review_unparseable',
      `Discovery review produced no valid binding verdict after one structured-output repair. ` +
        `${failedRun.lastResultText || failedRun.allOutput.slice(-20_000)}`,
      ws,
    );
    return;
  }

  const result = normalizeDiscoveryReviewResult(
    repaired.value,
    cycleId,
    expectedRevision,
  );
  await persistAndPostStatus(taskId, {
    task_id: taskId,
    status: 'COMPLETED',
    artifact_type: 'DISCOVERY_REVIEW',
    logs: sanitize(JSON.stringify(result)),
  });
}
