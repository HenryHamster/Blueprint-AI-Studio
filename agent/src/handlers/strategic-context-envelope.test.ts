import { createHash } from 'crypto';
import { describe, expect, it } from 'vitest';
import {
  buildFastResidentContextFrame,
  consultationBelongsToEnvelope,
  verifyStrategicContextEnvelope,
  type StrategicContextEnvelopeTransport,
} from './strategic-context-envelope.js';

function transport(overrides: Partial<StrategicContextEnvelopeTransport> = {}) {
  const envelope = {
    version: 2,
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
