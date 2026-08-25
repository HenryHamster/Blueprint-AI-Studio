import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('./discovery-review-deps.js', () => ({
  buildPromptSizeTelemetry: vi.fn().mockReturnValue({
    prompt_size_bytes: 0,
    prompt_section_count: 0,
    prompt_section_sizes: {},
  }),
  cleanupPacketOnlyWorkspace: vi.fn(),
  computePromptFingerprint: vi.fn().mockReturnValue('discovery-review-fp'),
  persistAndPostStatus: vi.fn(),
  preparePacketOnlyWorkspace: vi.fn().mockReturnValue(
    '/tmp/blueprint-discovery-review/task',
  ),
  runIsolatedTextOnlyMemory: vi.fn(),
  sanitize: vi.fn((value: string) => value),
  sendHeartbeatSync: vi.fn(),
  sendLog: vi.fn(),
  setCurrentPromptFingerprint: vi.fn(),
  setPromptSizeTelemetry: vi.fn(),
  strVal: vi.fn((value: unknown, fallback: string = ''): string =>
    typeof value === 'string' ? value : fallback),
}));

import {
  cleanupPacketOnlyWorkspace,
  persistAndPostStatus,
  preparePacketOnlyWorkspace,
  runIsolatedTextOnlyMemory,
} from './discovery-review-deps.js';
import {
  buildDiscoveryReviewPrompt,
  handleDiscoveryReview,
  normalizeDiscoveryReviewResult,
  validateDiscoveryReviewShape,
} from './discovery-review.js';

const CYCLE_ID = '123e4567-e89b-42d3-a456-426614174000';
const EVIDENCE_ID = '123e4567-e89b-42d3-a456-426614174001';

function payload(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    type: 'discovery_review',
    cycle_id: CYCLE_ID,
    expected_revision: 4,
    direction_key: 'd-useful-product',
    intent_snapshot: { goal: 'demonstrate user value' },
    bet_snapshot: { claim: 'the interaction is useful' },
    evidence_packet: [{
      evidence_id: EVIDENCE_ID,
      semantic_role: 'parent_outcome',
      source_kind: 'server_receipt',
      checksum: 'b'.repeat(64),
    }],
    evidence_packet_manifest: { required_evidence_omitted: false },
    packet_checksum: 'c'.repeat(64),
    // Legacy backend fields are deliberately ignored by the packet-only agent.
    candidate_sha: 'a'.repeat(40),
    review_round: 2,
    confirmation_proposal: { action: 'stop' },
    ...overrides,
  };
}

function validAdvance(
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    cycle_id: CYCLE_ID,
    reviewed_revision: 4,
    action: 'advance',
    summary: 'The exact cohort receipt supports one bounded measurement bet.',
    evidence_citations: [EVIDENCE_ID],
    next_bet: {
      mode: 'measure',
      claim: 'The interaction reduces time to a verified result.',
      decision_question: 'Does median completion time fall below ten minutes?',
      success_evidence: 'A pinned experiment receipt reports median time below ten minutes.',
      retirement_condition: 'Two sound runs miss the threshold or one sound run meets it.',
    },
    park_reason: null,
    resume_condition: null,
    operator_question: null,
    ...overrides,
  };
}

const validationContext = {
  cycleId: CYCLE_ID,
  expectedRevision: 4,
  evidenceIds: new Set([EVIDENCE_ID]),
  evidenceCount: 1,
  decisionReceiptIds: new Set([EVIDENCE_ID]),
  requiredEvidenceOmitted: false,
};

describe('research outcome decision contract', () => {
  it('accepts only the exact evidence-bound schema', () => {
    expect(
      validateDiscoveryReviewShape(validAdvance(), validationContext),
    ).toEqual({ ok: true });
    expect(
      validateDiscoveryReviewShape(
        validAdvance({ extra: true }),
        validationContext,
      ).ok,
    ).toBe(false);
    expect(
      validateDiscoveryReviewShape(
        validAdvance({ action: 'inconclusive' }),
        validationContext,
      ).ok,
    ).toBe(false);
    expect(
      validateDiscoveryReviewShape(
        validAdvance({ evidence_citations: ['unknown-id'] }),
        validationContext,
      ).ok,
    ).toBe(false);
  });

  it('requires a complete, bounded next bet for advance', () => {
    expect(
      validateDiscoveryReviewShape(
        validAdvance({ next_bet: { mode: 'measure', claim: 'partial' } }),
        validationContext,
      ).ok,
    ).toBe(false);
    expect(
      validateDiscoveryReviewShape(
        validAdvance({
          next_bet: {
            ...(validAdvance().next_bet as Record<string, unknown>),
            mode: 'release',
          },
        }),
        validationContext,
      ).ok,
    ).toBe(false);
    expect(
      validateDiscoveryReviewShape(
        validAdvance({ park_reason: 'also park' }),
        validationContext,
      ).ok,
    ).toBe(false);
  });

  it('requires binding actions to cite the current parent outcome receipt', () => {
    expect(
      validateDiscoveryReviewShape(
        validAdvance(),
        { ...validationContext, decisionReceiptIds: new Set<string>() },
      ).ok,
    ).toBe(false);
  });

  it('requires park reason and a concrete resume condition', () => {
    const parked = validAdvance({
      action: 'park',
      next_bet: null,
      park_reason: 'The current approach has no decision-bearing evidence.',
      resume_condition: 'Resume when the dependency exposes a stable API.',
    });
    expect(validateDiscoveryReviewShape(parked, validationContext)).toEqual({
      ok: true,
    });
    expect(
      validateDiscoveryReviewShape(
        { ...parked, resume_condition: null },
        validationContext,
      ).ok,
    ).toBe(false);
  });

  it('requires stop to carry no competing action payload', () => {
    const stopped = validAdvance({
      action: 'stop',
      next_bet: null,
    });
    expect(validateDiscoveryReviewShape(stopped, validationContext)).toEqual({
      ok: true,
    });
    expect(
      validateDiscoveryReviewShape(
        { ...stopped, operator_question: 'Stop?' },
        validationContext,
      ).ok,
    ).toBe(false);
  });

  it('turns empty or incomplete evidence into a concrete operator question', () => {
    const askOperator = validAdvance({
      action: 'ask_operator',
      evidence_citations: [],
      next_bet: null,
      operator_question: 'Should this direction wait for a live experiment receipt?',
    });
    const emptyContext = {
      ...validationContext,
      evidenceIds: new Set<string>(),
      evidenceCount: 0,
    };
    expect(
      validateDiscoveryReviewShape(askOperator, emptyContext),
    ).toEqual({ ok: true });
    expect(
      validateDiscoveryReviewShape(
        validAdvance(),
        { ...validationContext, requiredEvidenceOmitted: true },
      ).ok,
    ).toBe(false);
    expect(
      validateDiscoveryReviewShape(
        { ...askOperator, operator_question: null },
        emptyContext,
      ).ok,
    ).toBe(false);
  });

  it('refuses to escalate a question the packet already authorises answering', () => {
    // Direction d-1210b978 cycle 81ba8010 asked the operator for permission to
    // re-dispatch a verification task it was already authorised to dispatch,
    // and sat blocked for ~16 hours. ask_operator is for preference, authority,
    // scope, and priority calls only -- never for acquiring evidence.
    const askOperator = validAdvance({
      action: 'ask_operator',
      evidence_citations: [],
      next_bet: null,
      operator_question:
        'Should we re-dispatch a direct verification task before grading?',
    });
    const bindingContext = {
      ...validationContext,
      bindingDecisionAllowed: true,
      escalationRepairable: true,
    };
    const rejected = validateDiscoveryReviewShape(askOperator, bindingContext);
    expect(rejected.ok).toBe(false);
    expect(rejected.detail).toContain('ask_operator is not');
    // The same packet still accepts every disposition the reviewer can execute.
    expect(
      validateDiscoveryReviewShape(validAdvance(), bindingContext),
    ).toEqual({ ok: true });
    // A second insistence is accepted, not failed: the server converts it into
    // a bounded evidence bet, and failing here would strand the direction too.
    expect(
      validateDiscoveryReviewShape(askOperator, {
        ...bindingContext,
        escalationRepairable: false,
      }),
    ).toEqual({ ok: true });
    // A packet that does not grant binding authority keeps ask_operator.
    expect(
      validateDiscoveryReviewShape(askOperator, {
        ...bindingContext,
        bindingDecisionAllowed: false,
        requiredEvidenceOmitted: true,
      }),
    ).toEqual({ ok: true });
  });

  it('requires exact cycle identity and forces it during normalization', () => {
    expect(
      validateDiscoveryReviewShape(
        validAdvance({ cycle_id: '123e4567-e89b-42d3-a456-426614174099' }),
        validationContext,
      ).ok,
    ).toBe(false);
    const normalized = normalizeDiscoveryReviewResult(
      validAdvance({
        cycle_id: '123e4567-e89b-42d3-a456-426614174099',
        reviewed_revision: 999,
      }),
      CYCLE_ID,
      4,
    );
    expect(normalized.cycle_id).toBe(CYCLE_ID);
    expect(normalized.reviewed_revision).toBe(4);
  });
});

describe('packet-only decision prompt and handler', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(preparePacketOnlyWorkspace).mockReturnValue(
      '/tmp/blueprint-discovery-review/task',
    );
  });

  it('contains only the frozen decision packet and total action vocabulary', () => {
    // This packet grants binding authority, so the vocabulary is exactly the
    // dispositions the reviewer can execute itself.
    const prompt = buildDiscoveryReviewPrompt(payload());
    expect(prompt).toContain(EVIDENCE_ID);
    expect(prompt).toContain('only IDs allowed in evidence_citations');
    expect(prompt).toContain('"action": "advance|park|stop"');
    expect(prompt).not.toContain('advance|park|stop|ask_operator');
    expect(prompt).toContain('`ask_operator` is NOT available for this packet');
    expect(prompt).toContain('There is no inconclusive or null action');
    expect(prompt).toContain('not inspect a repository');
    expect(prompt).not.toContain('a'.repeat(40));
    expect(prompt).not.toContain('confirmation_proposal');
    expect(prompt).not.toContain('recommended_phase');
    expect(prompt).not.toContain('recommended_limits');
  });

  it('offers ask_operator only when the packet withholds binding authority', () => {
    const blocked = buildDiscoveryReviewPrompt(payload({
      evidence_packet_manifest: {
        required_evidence_omitted: true,
        binding_decision_allowed: false,
      },
    }));
    expect(blocked).toContain('"action": "advance|park|stop|ask_operator"');
    expect(blocked).toContain('a real\n  human choice is needed');
    expect(blocked).not.toContain('`ask_operator` is NOT available');
    // An empty packet has no receipt to bind a decision to either.
    const empty = buildDiscoveryReviewPrompt(payload({ evidence_packet: [] }));
    expect(empty).toContain('"action": "advance|park|stop|ask_operator"');
  });

  it('bounds decision snapshots when the prompt helper is called directly', () => {
    const prompt = buildDiscoveryReviewPrompt(
      payload({ intent_snapshot: { goal: 'x'.repeat(20_000) } }),
    );
    expect(prompt).toContain('"local_guard": true');
    expect(prompt).not.toContain('x'.repeat(9_000));
    expect(prompt.length).toBeLessThan(30_000);
  });

  it('does not re-flag a server-bounded snapshot as locally truncated', () => {
    // The server bounds bet_snapshot field-aware to 8,000 chars of *compact*
    // JSON, which indents to more than that. Wrapping such a snapshot in the
    // local guard would tell the reviewer its authority had been cut when
    // every field was in fact intact -- the false signal that stranded
    // direction d-f502e1a5 at ask_operator.
    const bet: Record<string, unknown> = {
      mode: 'research',
      claim: 'c'.repeat(1_500),
      decision_question: 'q'.repeat(4_000),
      success_evidence: 'The stranded-direction count reaches zero.',
      retirement_condition: 'Retire after two flat generations.',
      snapshot_truncated: true,
      snapshot_shortened_keys: ['direction'],
      snapshot_elided_keys: [],
      original_checksum: 'a'.repeat(64),
      direction: { id: 'd-f502e1a5', text: 'd'.repeat(1_000) },
      alternatives: Array.from(
        { length: 60 },
        (_unused, index) => `alternative ${index}`,
      ),
    };
    const compact = JSON.stringify(bet);
    expect(compact.length).toBeLessThanOrEqual(8_000);
    expect(JSON.stringify(bet, null, 2).length).toBeGreaterThan(8_000);

    const prompt = buildDiscoveryReviewPrompt(payload({ bet_snapshot: bet }));
    expect(prompt).toContain(compact);
    expect(prompt).not.toContain('"local_guard": true');
  });

  it('uses one configurable routine model route in isolated text-only memory', async () => {
    vi.mocked(runIsolatedTextOnlyMemory).mockResolvedValueOnce({
      exitCode: 0,
      allOutput: JSON.stringify(validAdvance()),
      lastResultText: JSON.stringify(validAdvance()),
    });
    const statusRef = { value: '' };

    await handleDiscoveryReview(
      'task-routine-tier',
      payload(),
      {},
      'unused-token',
      'unused-owner',
      'unused-repo',
      null,
      statusRef,
      'gpt-5.6-terra',
      'medium',
    );

    expect(preparePacketOnlyWorkspace).toHaveBeenCalledWith('task-routine-tier');
    expect(runIsolatedTextOnlyMemory).toHaveBeenCalledOnce();
    expect(runIsolatedTextOnlyMemory).toHaveBeenCalledWith(
      expect.any(String),
      null,
      statusRef,
      'gpt-5.6-terra',
      'medium',
      {
        cwd: '/tmp/blueprint-discovery-review/task',
        timeoutMs: 300_000,
        maxOutputBytes: 200_000,
      },
    );
    expect(cleanupPacketOnlyWorkspace).toHaveBeenCalledWith(
      '/tmp/blueprint-discovery-review/task',
    );
  });

  it('defaults the routine effort to medium without a confirmation branch', async () => {
    vi.mocked(runIsolatedTextOnlyMemory).mockResolvedValueOnce({
      exitCode: 0,
      allOutput: JSON.stringify(validAdvance()),
      lastResultText: JSON.stringify(validAdvance()),
    });
    const statusRef = { value: '' };

    await handleDiscoveryReview(
      'task-default-tier',
      payload(),
      {},
      'unused-token',
      'unused-owner',
      'unused-repo',
      null,
      statusRef,
    );

    expect(runIsolatedTextOnlyMemory).toHaveBeenCalledWith(
      expect.any(String),
      null,
      statusRef,
      undefined,
      'medium',
      {
        cwd: '/tmp/blueprint-discovery-review/task',
        timeoutMs: 300_000,
        maxOutputBytes: 200_000,
      },
    );
  });

  it('performs one structured repair and persists the repaired action', async () => {
    vi.mocked(runIsolatedTextOnlyMemory)
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: 'not json',
        lastResultText: 'not json',
      })
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: JSON.stringify(validAdvance()),
        lastResultText: JSON.stringify(validAdvance()),
      });

    await handleDiscoveryReview(
      'task-repair',
      payload(),
      {},
      'unused-token',
      'unused-owner',
      'unused-repo',
      null,
      { value: '' },
    );

    expect(preparePacketOnlyWorkspace).toHaveBeenCalledOnce();
    expect(runIsolatedTextOnlyMemory).toHaveBeenCalledTimes(2);
    const repairPrompt = vi.mocked(runIsolatedTextOnlyMemory).mock.calls[1]?.[0];
    expect(repairPrompt).toContain('demonstrate user value');
    expect(repairPrompt).toContain(EVIDENCE_ID);
    expect(repairPrompt).toContain('Re-evaluate the same frozen packet above');
    expect(persistAndPostStatus).toHaveBeenLastCalledWith(
      'task-repair',
      expect.objectContaining({
        status: 'COMPLETED',
        artifact_type: 'DISCOVERY_REVIEW',
      }),
    );
    const persisted = vi.mocked(persistAndPostStatus).mock.calls.at(-1)?.[1];
    expect(persisted?.logs).toContain('"action":"advance"');
  });

  it('rejects an oversized contract before preparing the packet workspace', async () => {
    await handleDiscoveryReview(
      'task-oversized-contract',
      payload({ intent_snapshot: { goal: 'x'.repeat(20_000) } }),
      {},
      'unused-token',
      'unused-owner',
      'unused-repo',
      null,
      { value: '' },
    );

    expect(preparePacketOnlyWorkspace).not.toHaveBeenCalled();
    expect(runIsolatedTextOnlyMemory).not.toHaveBeenCalled();
    expect(persistAndPostStatus).toHaveBeenLastCalledWith(
      'task-oversized-contract',
      expect.objectContaining({
        status: 'FAILED',
        failure_category: 'discovery_review_contract_invalid',
      }),
    );
  });

  it('fails closed after one unsuccessful repair', async () => {
    vi.mocked(runIsolatedTextOnlyMemory).mockResolvedValue({
      exitCode: 0,
      allOutput: 'still not json',
      lastResultText: 'still not json',
    });

    await handleDiscoveryReview(
      'task-invalid',
      payload(),
      {},
      'unused-token',
      'unused-owner',
      'unused-repo',
      null,
      { value: '' },
    );

    expect(runIsolatedTextOnlyMemory).toHaveBeenCalledTimes(2);
    expect(persistAndPostStatus).toHaveBeenLastCalledWith(
      'task-invalid',
      expect.objectContaining({
        status: 'FAILED',
        failure_category: 'discovery_review_unparseable',
      }),
    );
    expect(cleanupPacketOnlyWorkspace).toHaveBeenCalledWith(
      '/tmp/blueprint-discovery-review/task',
    );
  });
});
