import { createHash } from 'crypto';
import { describe, expect, it } from 'vitest';
import {
  buildCanonicalResidentContextFrame,
  buildFastResidentContextFrame,
  buildPrivateWorkdayContextFrame,
  consultationBelongsToEnvelope,
  verifyStrategicContextEnvelope,
  type StrategicContextEnvelopeTransport,
} from './strategic-context-envelope.js';

function transport(
  overrides: Partial<StrategicContextEnvelopeTransport> = {},
  envelopeVersion: number = 2,
) {
  const envelope = {
    version: envelopeVersion,
    project_id: '11111111-1111-4111-8111-111111111111',
    context_authority_generation: 4,
    privacy_generation: 2,
    transcript_sequence: 81,
    ledger_revision: 0,
    problem_model: { intent: { purpose: 'Work through the theory' } },
    decisions: [],
    memory: { active: [] },
    memory_v2: { evidence_cards: [], raw_suffix: [], memory_degraded: false },
    recent_conversation: [
      { sequence: 80, content: 'Earlier context that the warm child already saw.' },
      { sequence: 81, content: 'What if eta is smaller?' },
    ],
  };
  const bytes = Buffer.from(JSON.stringify(envelope), 'utf8');
  return {
    snapshot_id: 'snapshot-1',
    canonical_envelope_b64: bytes.toString('base64'),
    canonical_sha256: createHash('sha256').update(bytes).digest('hex'),
    context_authority_generation: 4,
    privacy_generation: 2,
    transcript_sequence: 81,
    ledger_revision: 0,
    expires_at: '2035-01-01T00:00:00.000Z',
    ...overrides,
  } satisfies StrategicContextEnvelopeTransport;
}

describe('persisted Strategic context envelope', () => {
  it('gives Fast and Workday the exact same verified bytes after restart', () => {
    const persisted = transport();
    const first = verifyStrategicContextEnvelope(persisted, {
      contextAuthorityGeneration: 4,
      privacyGeneration: 2,
      now: new Date('2030-01-01T00:00:00.000Z'),
    });
    const restarted = verifyStrategicContextEnvelope(
      JSON.parse(JSON.stringify(persisted)) as StrategicContextEnvelopeTransport,
      {
        contextAuthorityGeneration: 4,
        privacyGeneration: 2,
        now: new Date('2030-01-01T00:00:00.000Z'),
      },
    );

    expect(restarted.canonicalBytes.equals(first.canonicalBytes)).toBe(true);
    expect(restarted.sha256).toBe(first.sha256);
    expect(buildFastResidentContextFrame(first)).toContain('Reply naturally and directly');
    expect(buildFastResidentContextFrame(first)).not.toContain('example reply');
    const delta = buildFastResidentContextFrame(first, 80);
    expect(delta).toContain('What if eta is smaller?');
    expect(delta).not.toContain('Earlier context that the warm child already saw.');
  });

  it('accepts the backend degraded core as an empty transcript instead of livelocking Fast', () => {
    // The backend's last-resort over-budget core omits recent_conversation
    // entirely (`_truncated: true`). Rejecting it made every typed Fast claim
    // rebuild the same over-budget envelope and defer forever (live wedge,
    // 2026-08-18). It must verify as an empty conversation.
    const truncatedCore = {
      version: 2,
      project_id: '11111111-1111-4111-8111-111111111111',
      context_authority_generation: 4,
      privacy_generation: 2,
      transcript_sequence: 81,
      ledger_revision: 0,
      _truncated: true,
      _sha256: 'a'.repeat(64),
      _bytes: 999_999,
    };
    const bytes = Buffer.from(JSON.stringify(truncatedCore), 'utf8');
    const verified = verifyStrategicContextEnvelope(transport({
      canonical_envelope_b64: bytes.toString('base64'),
      canonical_sha256: createHash('sha256').update(bytes).digest('hex'),
    }), {
      contextAuthorityGeneration: 4,
      privacyGeneration: 2,
      now: new Date('2030-01-01T00:00:00.000Z'),
    });
    expect(verified.envelope.recent_conversation).toEqual([]);
    // Canonical bytes stay the exact persisted bytes, untouched by the repair.
    expect(verified.canonicalBytes.equals(bytes)).toBe(true);

    // A normal (non-degraded) envelope without a conversation array is still
    // malformed and must keep failing verification.
    const malformed = { ...truncatedCore } as Record<string, unknown>;
    delete malformed._truncated;
    const malformedBytes = Buffer.from(JSON.stringify(malformed), 'utf8');
    expect(() => verifyStrategicContextEnvelope(transport({
      canonical_envelope_b64: malformedBytes.toString('base64'),
      canonical_sha256: createHash('sha256').update(malformedBytes).digest('hex'),
    }), {
      contextAuthorityGeneration: 4,
      privacyGeneration: 2,
      now: new Date('2030-01-01T00:00:00.000Z'),
    })).toThrow('recent_conversation must be an array');
  });

  it('keeps canonical context role-neutral and isolates private Workday instructions', () => {
    const verified = verifyStrategicContextEnvelope(transport(), {
      contextAuthorityGeneration: 4,
      privacyGeneration: 2,
      now: new Date('2030-01-01T00:00:00.000Z'),
    });

    const canonical = buildCanonicalResidentContextFrame(verified, 80);
    expect(canonical).toContain('## Canonical project context');
    expect(canonical).toContain('What if eta is smaller?');
    expect(canonical).not.toContain('Earlier context that the warm child already saw.');
    expect(canonical).not.toContain('## Foreground role');
    expect(canonical).not.toContain('## Private Workday role');

    const workday = buildPrivateWorkdayContextFrame(verified, 80);
    expect(workday).toContain(canonical);
    expect(workday).toContain('## Private Workday role');
    expect(workday).toContain('repository, shell, browser, infrastructure, and Blueprint tools');
    expect(workday).toContain('Treat the private question as the complete assignment');
    expect(workday).toContain('execute that operation in this Workday turn before any broader investigation');
    expect(workday).toContain('read the live operation schema if it is unfamiliar');
    expect(workday).toContain('apply the exact requested mutation through ./strategic');
    expect(workday).toContain('fetch the same canonical target for readback');
    expect(workday).toContain('exact successful mutation receipt and matching canonical readback');
    expect(workday).toContain('missing receipt, mismatched readback, or a real blocker');
    expect(workday).toContain('implementation, research, or other work that must run as a task');
    expect(workday).not.toContain('inspect existing directions and tasks first');
    expect(workday).not.toContain('## Foreground role');
    expect(workday).not.toContain('Do not use repository, shell, browser, or infrastructure tools.');
    expect(workday).not.toContain('request a Workday consultation');
  });

  it('preserves the public Fast foreground frame', () => {
    const verified = verifyStrategicContextEnvelope(transport(), {
      contextAuthorityGeneration: 4,
      privacyGeneration: 2,
      now: new Date('2030-01-01T00:00:00.000Z'),
    });

    expect(buildFastResidentContextFrame(verified)).toBe([
      buildCanonicalResidentContextFrame(verified),
      '',
      '## Foreground role',
      'You are the public foreground voice of one Strategic system and its conversation router, not an implementation agent.',
      'Reply naturally and directly to every operator message using the context above.',
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
    ].join('\n'));
  });

  it('accepts still-valid V1 packets during rollout and rejects unknown versions', () => {
    const expected = {
      contextAuthorityGeneration: 4,
      privacyGeneration: 2,
      now: new Date('2030-01-01T00:00:00.000Z'),
    };
    expect(verifyStrategicContextEnvelope(transport({}, 1), expected).envelope.version).toBe(1);
    expect(() => verifyStrategicContextEnvelope(transport({}, 3), expected)).toThrow(
      /version is unsupported/,
    );
  });

  it('fails closed on hash, authority, privacy, transcript, and expiry drift', () => {
    const expected = {
      contextAuthorityGeneration: 4,
      privacyGeneration: 2,
      minimumTranscriptSequence: 81,
      now: new Date('2030-01-01T00:00:00.000Z'),
    };
    expect(() => verifyStrategicContextEnvelope(
      transport({ canonical_sha256: '0'.repeat(64) }),
      expected,
    )).toThrow(/sha256 mismatch/);
    expect(() => verifyStrategicContextEnvelope(transport(), {
      ...expected,
      contextAuthorityGeneration: 5,
    })).toThrow(/authority is stale/);
    expect(() => verifyStrategicContextEnvelope(transport(), {
      ...expected,
      privacyGeneration: 3,
    })).toThrow(/authority is stale/);
    expect(() => verifyStrategicContextEnvelope(transport(), {
      ...expected,
      minimumTranscriptSequence: 82,
    })).toThrow(/transcript is stale/);
    expect(() => verifyStrategicContextEnvelope(
      transport({ expires_at: '2029-01-01T00:00:00.000Z' }),
      expected,
    )).toThrow(/expired/);
  });

  it('binds durable Workday consultations to the same canonical snapshot', () => {
    const verified = verifyStrategicContextEnvelope(transport(), {
      contextAuthorityGeneration: 4,
      privacyGeneration: 2,
      now: new Date('2030-01-01T00:00:00.000Z'),
    });
    expect(consultationBelongsToEnvelope({
      packet_id: 'packet-1',
      turn_claim_id: 'claim-1',
      input_revision: 1,
      context_snapshot_id: verified.snapshotId,
      context_sha256: verified.sha256,
      question: 'Check the derivation.',
      status: 'pending',
      continuation_sequence: 0,
    }, verified)).toBe(true);
  });
});
