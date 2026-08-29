import { createHash, timingSafeEqual } from 'crypto';

// Keep this in lockstep with backend/app/strategic_memory_envelope.py.
// The backend's V2 core adds the bounded memory_v2 retrieval projection.
export const STRATEGIC_CONTEXT_ENVELOPE_VERSION = 2;
const SUPPORTED_STRATEGIC_CONTEXT_ENVELOPE_VERSIONS = new Set([1, 2]);
export const STRATEGIC_CONTEXT_ENVELOPE_MAX_BYTES = 512 * 1024;

export interface StrategicContextEnvelopeTransport {
  snapshot_id: string;
  canonical_envelope_b64: string;
  canonical_sha256: string;
  context_authority_generation: number;
  privacy_generation: number;
  transcript_sequence: number;
  ledger_revision: number;
  expires_at: string;
}

export interface StrategicContextEnvelope {
  version: 1 | 2;
  project_id: string;
  context_authority_generation: number;
  privacy_generation: number;
  transcript_sequence: number;
  ledger_revision: number;
  problem_model: unknown;
  decisions: unknown;
  memory: unknown;
  memory_v2?: unknown;
  recent_conversation: unknown[];
}

export interface ExpectedStrategicContextAuthority {
  contextAuthorityGeneration: number;
  privacyGeneration: number;
  minimumTranscriptSequence?: number;
  now?: Date;
}

export interface VerifiedStrategicContextEnvelope {
  snapshotId: string;
  sha256: string;
  canonicalBytes: Buffer;
  canonicalText: string;
  envelope: StrategicContextEnvelope;
}

function requireNonNegativeInteger(value: unknown, name: string): number {
  if (!Number.isSafeInteger(value) || Number(value) < 0) {
    throw new Error(`strategic context envelope has invalid ${name}`);
  }
  return Number(value);
}

function requireRecord(value: unknown): Record<string, unknown> {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('strategic context envelope must be a JSON object');
  }
  return value as Record<string, unknown>;
}

function decodeCanonicalBytes(value: string): Buffer {
  if (!value || !/^[A-Za-z0-9+/]*={0,2}$/.test(value) || value.length % 4 !== 0) {
    throw new Error('strategic context envelope has invalid base64');
  }
  const bytes = Buffer.from(value, 'base64');
  if (bytes.length === 0 || bytes.length > STRATEGIC_CONTEXT_ENVELOPE_MAX_BYTES) {
    throw new Error('strategic context envelope exceeds byte bounds');
  }
  if (bytes.toString('base64') !== value) {
    throw new Error('strategic context envelope base64 is not canonical');
  }
  return bytes;
}

function verifySha256(bytes: Buffer, expectedHex: string): void {
  if (!/^[0-9a-f]{64}$/.test(expectedHex)) {
    throw new Error('strategic context envelope has invalid sha256');
  }
  const actual = createHash('sha256').update(bytes).digest();
  const expected = Buffer.from(expectedHex, 'hex');
  if (!timingSafeEqual(actual, expected)) {
    throw new Error('strategic context envelope sha256 mismatch');
  }
}

/**
 * Verify persisted canonical bytes before either Fast or Workday consumes them.
 * Both consumers keep the same bytes/text; provider history is never authority.
 */
export function verifyStrategicContextEnvelope(
  transport: StrategicContextEnvelopeTransport,
  expected: ExpectedStrategicContextAuthority,
): VerifiedStrategicContextEnvelope {
  const bytes = decodeCanonicalBytes(transport.canonical_envelope_b64);
  verifySha256(bytes, transport.canonical_sha256);

  const expiresAt = new Date(transport.expires_at);
  if (!Number.isFinite(expiresAt.getTime())) {
    throw new Error('strategic context envelope has invalid expiry');
  }
  if (expiresAt.getTime() <= (expected.now ?? new Date()).getTime()) {
    throw new Error('strategic context envelope is expired');
  }

  let decoded: unknown;
  try {
    decoded = JSON.parse(bytes.toString('utf8'));
  } catch {
    throw new Error('strategic context envelope bytes are not valid UTF-8 JSON');
  }
  const raw = requireRecord(decoded);
  if (
    !Number.isSafeInteger(raw.version)
    || !SUPPORTED_STRATEGIC_CONTEXT_ENVELOPE_VERSIONS.has(Number(raw.version))
  ) {
    throw new Error('strategic context envelope version is unsupported');
  }
  if (typeof raw.project_id !== 'string' || raw.project_id.length === 0) {
    throw new Error('strategic context envelope has invalid project_id');
  }

  const authorityGeneration = requireNonNegativeInteger(
    raw.context_authority_generation,
    'context_authority_generation',
  );
  const privacyGeneration = requireNonNegativeInteger(
    raw.privacy_generation,
    'privacy_generation',
  );
  const transcriptSequence = requireNonNegativeInteger(
    raw.transcript_sequence,
    'transcript_sequence',
  );
  const ledgerRevision = requireNonNegativeInteger(raw.ledger_revision, 'ledger_revision');
  const transportAuthority = requireNonNegativeInteger(
    transport.context_authority_generation,
    'transport context_authority_generation',
  );
  const transportPrivacy = requireNonNegativeInteger(
    transport.privacy_generation,
    'transport privacy_generation',
  );
  const transportSequence = requireNonNegativeInteger(
    transport.transcript_sequence,
    'transport transcript_sequence',
  );
  const transportLedger = requireNonNegativeInteger(
    transport.ledger_revision,
    'transport ledger_revision',
  );

  if (
    authorityGeneration !== transportAuthority
    || privacyGeneration !== transportPrivacy
    || transcriptSequence !== transportSequence
    || ledgerRevision !== transportLedger
  ) {
    throw new Error('strategic context envelope metadata does not match canonical bytes');
  }
  if (
    authorityGeneration !== expected.contextAuthorityGeneration
    || privacyGeneration !== expected.privacyGeneration
  ) {
    throw new Error('strategic context envelope authority is stale');
  }
  if (
    expected.minimumTranscriptSequence !== undefined
    && transcriptSequence < expected.minimumTranscriptSequence
  ) {
    throw new Error('strategic context envelope transcript is stale');
  }
  if (!Array.isArray(raw.recent_conversation)) {
    // The backend's last-resort degraded core (`_truncated: true`) carries no
    // conversation at all; treat it as an empty transcript rather than a
    // malformed envelope — rejecting it livelocked the typed Fast lane, which
    // reclaimed the same over-budget envelope forever with no Workday
    // fallback. The canonical bytes/hash above stay untouched.
    if (raw._truncated === true) {
      raw.recent_conversation = [];
    } else {
      throw new Error('strategic context envelope recent_conversation must be an array');
    }
  }

  return {
    snapshotId: transport.snapshot_id,
    sha256: transport.canonical_sha256,
    canonicalBytes: bytes,
    canonicalText: bytes.toString('utf8'),
    envelope: raw as unknown as StrategicContextEnvelope,
  };
}

/** Render only the verified canonical context, without assigning an agent role. */
export function buildCanonicalResidentContextFrame(
  verified: VerifiedStrategicContextEnvelope,
  afterTranscriptSequence = 0,
): string {
  const canonicalContext = afterTranscriptSequence <= 0
    ? verified.canonicalText
    : JSON.stringify({
      ...verified.envelope,
      recent_conversation: verified.envelope.recent_conversation.filter((item) => {
        if (!item || typeof item !== 'object' || Array.isArray(item)) return false;
        const sequence = (item as Record<string, unknown>).sequence;
        return Number.isSafeInteger(sequence) && Number(sequence) > afterTranscriptSequence;
      }),
    });
  return [
    '## Canonical project context',
    canonicalContext,
  ].join('\n');
}

/**
 * Surface the latest canonical operator message for continuation turns.
 *
 * The complete verified transcript remains the authority. This small projection
 * keeps the request's stopping conditions salient when a long private result is
 * formatted later by a fresh foreground turn.
 */
export function latestCanonicalOperatorRequest(
  verified: VerifiedStrategicContextEnvelope,
): string | null {
  let latest: { sequence: number; content: string } | null = null;
  for (const item of verified.envelope.recent_conversation) {
    if (!item || typeof item !== 'object' || Array.isArray(item)) continue;
    const row = item as Record<string, unknown>;
    if (row.from_type !== 'user' && row.from_type !== 'operator') continue;
    if (!Number.isSafeInteger(row.sequence) || typeof row.content !== 'string') continue;
    const content = row.content.trim();
    if (!content) continue;
    const sequence = Number(row.sequence);
    if (latest === null || sequence > latest.sequence) latest = { sequence, content };
  }
  return latest?.content ?? null;
}

/**
 * The foreground role is conversational, not a second coding agent. It may
 * request a durable Workday consultation while continuing the public turn.
 */
export function buildFastResidentContextFrame(
  verified: VerifiedStrategicContextEnvelope,
  afterTranscriptSequence = 0,
): string {
  return [
    buildCanonicalResidentContextFrame(verified, afterTranscriptSequence),
    '',
    '## Foreground role',
    'You are the public foreground voice of one Strategic system and its conversation router, not an implementation agent.',
    'Reply naturally and directly to every operator message using the context above.',
    '',
    '## Operator instruction fidelity',
    'The active operator turn is authoritative for scope, negative constraints, decision ownership, and the requested stopping point. Answer that request without widening it into a new planning turn.',
    'Honor explicit constraints such as "read-only", "no mutation", "no operator decision is needed", "do not retry", and "do not propose alternatives" in this immediate reply as well as in any later Workday continuation.',
    'Do not add alternatives, choices, blockers, recommendations, retries, or requests for an operator decision that the active turn excludes. Never turn a settled terminal instruction into a menu of ways around it, suggest changing immutable state, or reopen a conclusion the operator asked you to verify and carry forward.',
    'Requesting a private Workday consultation does not relax these constraints. This immediate reply and the later receipt-bound continuation are distinct public roles, and both must preserve the same operator request. Use the private request only to collect the missing evidence or receipts.',
    'Preserve every literal the operator explicitly asks you to report or verify, including full digests, commit SHAs, ids, criterion keys, revisions, counts, byte counts, and quoted text. Do not abbreviate, ellipsize, round, summarize, or replace a requested exact value in either public role. If an exact value awaits private verification, say that it is pending instead of publishing a shortened substitute.',
    'If canonical context genuinely conflicts with the active operator turn, state the exact conflict and stop. Otherwise carry the requested conclusion and stopping point through unchanged.',
    '',
    'Do not use repository, shell, browser, or infrastructure tools.',
    'The canonical decisions object and, only on a continuation, the separately labeled server-projected current_live_work_authority are the only claim-time authorities for the role contract, Resident activity, and managed-project state. Use exact durable ids and states from active_directions, active_tasks, recent_terminal_tasks, result_correlated_tasks, receipt_correlated_directions, receipt_correlated_tasks, receipt_correlated_documents, release_deployments, result_correlated_releases, result_correlated_documents, current_project_state_authority, or consultation_mutation_receipts instead of inferring state from conversation or Workday prose. A result_correlation candidate comes from Workday prose only to request a bounded lookup; only a returned canonical row supplies current state authority.',
    'Their managed-work arrays are bounded: when collection_metadata says has_more, an unlisted id or state is unknown rather than proof that no such direction, task, release, or deployment exists or has reached a particular state. Request canonical verification before making an absence claim.',
    'Distinguish configured private Workday policy from current actability. Continuous mode sets role_contract.private_workday_autonomy_configured, but say Workday can act now only when role_contract.private_workday_autonomy is enabled; paused, disabled, unavailable, or unknown means it cannot currently be claimed active. project_autonomous_enabled describes separate project loops, not private Workday actability. Never generalize a private-mode fact to the whole project: already-dispatched tasks and other autonomous loops may continue.',
    'Never claim that you will edit or delete code, configuration, branches, files, deployments, or other managed project state yourself.',
    'Route execution requests through a Workday consultation so supported Blueprint task tooling owns the change; distinguish proposed or delegated work from verified completion.',
    'A Workday-requested receipt proves only that a private consultation was created. A receipt-bound Workday result is investigatory and correlation evidence, not managed-state authority. A result-correlated row permits only a statement that Workday mentioned exact id X and canonical X currently has state Y; the mention does not prove X was caused by, belongs to, or completed this request. Claim a managed direction, task, execution start, blocker, completion, or relationship to this request only when exact claim-time state plus a server-projected matching direction_id, strategic_work_order_id, or consultation_mutation_receipt proves that relationship. An entity receipt with effect created may prove Workday created or dispatched its exact canonical direction, task, or memory document; updated may prove the exact canonical entity was updated by this consultation. Replayed proves only that the operation resolved to existing work, and deferred proves only that a spawn request was accepted for later admission. Current state, blockage, and completion still require the exact canonical entity row. An unrelated terminal or merely mentioned item proves nothing about this request.',
    'A terminal coding task proves only that task outcome. Claim that an exact deployment or release is serving only when release_deployments or result_correlated_releases has serving_status serving_confirmed and canonical served evidence for the exact promoted revision or SHA at issue; otherwise say it is unknown and request verification.',
    'Do not claim that Fast or Workday recorded, wrote, updated, or persisted a vision, problem model, memory, note, configuration, or other managed state unless consultation_mutation_receipts contains an exact receipt matching this packet, Workday claim, task, session, worker generation and incarnation, and the current canonical target revision or digest. result_correlated_documents and current_project_state_authority may prove only current document existence, current problem-model revision or digest, or current running-update digest. Comparing an original snapshot with current authority may prove that state changed between those snapshots, not who changed it. Without an exact receipt, state only the current canonical fact, understanding, proposal, or unverified authorship report.',
    'When fresh or bulky evidence is needed, request a Workday consultation and say what is missing.',
    'Workday investigation does not replace this reply and must never create a competing public answer.',
  ].join('\n');
}

/** Build the private investigator frame used by a durable Workday consultation. */
export function buildPrivateWorkdayContextFrame(
  verified: VerifiedStrategicContextEnvelope,
  afterTranscriptSequence = 0,
): string {
  return [
    buildCanonicalResidentContextFrame(verified, afterTranscriptSequence),
    '',
    '## Private Workday role',
    'Treat the private question as the complete assignment and prioritize it over generic exploration.',
    'When the question explicitly requests a supported direct strategic operation, execute that operation in this Workday turn before any broader investigation. Do not delegate it, inspect unrelated repository, direction, or task state, return a plan, or wait for another turn.',
    'For a requested direct mutation, use only the minimum bounded sequence: read the live operation schema if it is unfamiliar, fetch the canonical target and required precondition, apply the exact requested mutation through ./strategic, then fetch the same canonical target for readback. Preserve unrelated fields and use the question\'s exact evidence, revision, and idempotency constraints. Attempt the mutation early enough to finish this sequence inside the current turn.',
    'After an exact successful mutation receipt and matching canonical readback, return those exact identifiers and revisions immediately. On refusal, applied=false, missing receipt, mismatched readback, or a real blocker, stop and report the exact result instead of spending the turn on more exploration.',
    'When the question requests implementation, research, or other work that must run as a task, inspect existing directions and tasks, then delegate through supported Blueprint task tooling. Report exact direction/task/watch ids and current states, or the exact blocker; do not pretend the foreground strategist made the change.',
    'For an investigatory question with no requested direct operation, use repository, shell, browser, infrastructure, and Blueprint tools as needed to answer it.',
    'Return an evidence-grounded internal summary; do not attempt a public reply.',
  ].join('\n');
}

export interface DurableWorkdayConsultation {
  packet_id: string;
  turn_claim_id: string;
  input_revision: number;
  context_snapshot_id: string;
  context_sha256: string;
  question: string;
  status: 'pending' | 'running' | 'answered' | 'cancelled';
  continuation_sequence: number;
}

export function consultationBelongsToEnvelope(
  consultation: DurableWorkdayConsultation,
  verified: VerifiedStrategicContextEnvelope,
): boolean {
  return (
    consultation.context_snapshot_id === verified.snapshotId
    && consultation.context_sha256 === verified.sha256
  );
}
