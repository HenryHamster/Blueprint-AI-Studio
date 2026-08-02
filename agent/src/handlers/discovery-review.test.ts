import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('./discovery-review-deps.js', () => {
  return {
    DEFAULT_TOOL_GUIDANCE: '',
    WORKSPACE: '/tmp/discovery-review-test',
    buildContextPrefix: vi.fn().mockReturnValue(''),
    buildPromptSizeTelemetry: vi.fn().mockReturnValue({
      prompt_size_bytes: 0,
      prompt_section_count: 0,
      prompt_section_sizes: {},
    }),
    computePromptFingerprint: vi.fn().mockReturnValue('discovery-review-fp'),
    configureAgentPermissions: vi.fn().mockReturnValue([
      'Write',
      'Edit',
      'NotebookEdit',
    ]),
    git: vi.fn(),
    persistAndPostStatus: vi.fn(),
    preparePacketOnlyWorkspace: vi.fn(),
    prepareWorkspaceForRole: vi.fn().mockResolvedValue(undefined),
    providerAutoInjectsClaudeMd: vi.fn().mockReturnValue(false),
    readClaudeMd: vi.fn().mockReturnValue(''),
    resolveBaseBranch: vi.fn().mockReturnValue('automain'),
    runClaudeCode: vi.fn(),
    sanitize: vi.fn((value: string) => value),
    sendHeartbeatSync: vi.fn(),
    sendLog: vi.fn(),
    setCurrentPromptFingerprint: vi.fn(),
    setPromptSizeTelemetry: vi.fn(),
    strVal: vi.fn((value: unknown, fallback: string = ''): string =>
      typeof value === 'string' ? value : fallback),
    validateBranchName: vi.fn((value: string) => value),
  };
});

import {
  git,
  persistAndPostStatus,
  preparePacketOnlyWorkspace,
  prepareWorkspaceForRole,
  runClaudeCode,
} from './discovery-review-deps.js';
import {
  buildDiscoveryReviewPrompt,
  handleDiscoveryReview,
  normalizeDiscoveryReviewResult,
  validateDiscoveryReviewShape,
} from './discovery-review.js';

const CYCLE_ID = '123e4567-e89b-42d3-a456-426614174000';
const EVIDENCE_ID = '123e4567-e89b-42d3-a456-426614174001';
const CANDIDATE_SHA = 'a'.repeat(40);

function payload(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    type: 'discovery_review',
    cycle_id: CYCLE_ID,
    expected_revision: 4,
    direction_key: 'd-useful-product',
    intent_snapshot: { goal: 'demonstrate user value' },
    bet_snapshot: { claim: 'the interaction is useful' },
    outcome_kind: 'mixed',
    phase: 'demonstrate',
    evidence_packet: [{
      evidence_id: EVIDENCE_ID,
      semantic_role: 'result',
      source_kind: 'artifact',
      checksum: 'b'.repeat(64),
    }],
    packet_checksum: 'c'.repeat(64),
    candidate_sha: CANDIDATE_SHA,
    mechanism_fingerprint: 'd'.repeat(64),
    review_round: 1,
    prior_review_task_id: null,
    ...overrides,
  };
}

function validResult(
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    cycle_id: CYCLE_ID,
    reviewed_revision: 4,
    verdict: 'expand',
    confidence: 0.82,
    decision_summary: 'The bounded demonstration supports expansion.',
    goal_progress_assessment: 'The result demonstrates the frozen outcome.',
    evidence_citations: [EVIDENCE_ID],
    contradictions: [],
    next_bet: { claim: 'test the interaction with more users' },
    recommended_phase: 'productize',
    recommended_limits: {
      task_credit_limit: 3,
      attempt_credit_limit: 5,
      concurrency_limit: 2,
    },
    operator_question: null,
    ...overrides,
  };
}

const validationContext = {
  cycleId: CYCLE_ID,
  expectedRevision: 4,
  evidenceIds: new Set([EVIDENCE_ID]),
  evidenceCount: 1,
  requiredEvidenceOmitted: false,
};

describe('discovery review verdict contract', () => {
  it('accepts only the exact evidence-bound schema', () => {
    expect(
      validateDiscoveryReviewShape(validResult(), validationContext),
    ).toEqual({ ok: true });
    expect(
      validateDiscoveryReviewShape(
        validResult({ extra: true }),
        validationContext,
      ).ok,
    ).toBe(false);
    expect(
      validateDiscoveryReviewShape(
        validResult({ evidence_citations: ['unknown-id'] }),
        validationContext,
      ).ok,
    ).toBe(false);
    expect(
      validateDiscoveryReviewShape(
        validResult({ confidence: 1.1 }),
        validationContext,
      ).ok,
    ).toBe(false);
    expect(
      validateDiscoveryReviewShape(
        validResult({ next_bet: { blob: 'x'.repeat(9_000) } }),
        validationContext,
      ).ok,
    ).toBe(false);
  });

  it('fails closed on unsupported empty-packet verdicts', () => {
    const emptyContext = {
      ...validationContext,
      evidenceIds: new Set<string>(),
      evidenceCount: 0,
    };
    expect(
      validateDiscoveryReviewShape(
        validResult({ evidence_citations: [] }),
        emptyContext,
      ).ok,
    ).toBe(false);
    expect(
      validateDiscoveryReviewShape(
        validResult({
          verdict: 'inconclusive',
          evidence_citations: [],
          next_bet: null,
        }),
        emptyContext,
      ),
    ).toEqual({ ok: true });
  });

  it('fails closed on binding verdicts when required evidence was omitted', () => {
    expect(
      validateDiscoveryReviewShape(
        validResult(),
        { ...validationContext, requiredEvidenceOmitted: true },
      ).ok,
    ).toBe(false);
  });

  it('requires exact cycle identity and a question for ask_operator', () => {
    expect(
      validateDiscoveryReviewShape(
        validResult({ cycle_id: '123e4567-e89b-42d3-a456-426614174099' }),
        validationContext,
      ).ok,
    ).toBe(false);
    expect(
      validateDiscoveryReviewShape(
        validResult({ verdict: 'ask_operator', operator_question: null }),
        validationContext,
      ).ok,
    ).toBe(false);
  });

  it('forces identity from the frozen payload during normalization', () => {
    const normalized = normalizeDiscoveryReviewResult(
      validResult({
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

describe('discovery review prompt and handler', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(git).mockImplementation(args => {
      if (args[0] === 'rev-parse') return CANDIDATE_SHA;
      return '';
    });
  });

  it('pins the decision and exposes only packet evidence IDs', () => {
    const prompt = buildDiscoveryReviewPrompt(
      payload(),
      {},
      CANDIDATE_SHA,
    );
    expect(prompt).toContain(CANDIDATE_SHA);
    expect(prompt).toContain(EVIDENCE_ID);
    expect(prompt).toContain('only IDs allowed in evidence_citations');
    expect(prompt).toContain('not another research turn');
    expect(prompt).toContain('Output ONLY one JSON object');
  });

  it('bounds decision snapshots even when the prompt helper is called directly', () => {
    const prompt = buildDiscoveryReviewPrompt(
      payload({ intent_snapshot: { goal: 'x'.repeat(20_000) } }),
      {},
      CANDIDATE_SHA,
    );

    expect(prompt).toContain('"local_guard": true');
    expect(prompt).not.toContain('x'.repeat(9_000));
    expect(prompt.length).toBeLessThan(18_000);
  });

  it('uses the resolved lower-tier model and effort for an ordinary review', async () => {
    vi.mocked(runClaudeCode).mockResolvedValueOnce({
      exitCode: 0,
      allOutput: JSON.stringify(validResult()),
      lastResultText: JSON.stringify(validResult()),
    });
    const statusRef = { value: '' };

    await handleDiscoveryReview(
      'task-routine-tier',
      payload({ effort: 'medium' }),
      {},
      'token',
      'owner',
      'repo',
      null,
      statusRef,
      'gpt-5.6-terra',
      'medium',
    );

    expect(runClaudeCode).toHaveBeenCalledOnce();
    expect(runClaudeCode).toHaveBeenCalledWith(
      expect.any(String),
      null,
      statusRef,
      'gpt-5.6-terra',
      'medium',
      ['Write', 'Edit', 'NotebookEdit'],
    );
  });

  it('makes confirmation disagreement an explicit operator handoff', () => {
    const prompt = buildDiscoveryReviewPrompt(
      payload({
        review_round: 2,
        effort: 'max',
        prior_review_task_id: '123e4567-e89b-42d3-a456-426614174099',
        confirmation_proposal: {
          verdict: 'expand',
          next_bet: { claim: 'expand the bounded bet' },
          recommended_phase: 'productize',
          recommended_limits: {
            task_credit_limit: 3,
            attempt_credit_limit: 5,
            concurrency_limit: 2,
          },
        },
      }),
      {},
      CANDIDATE_SHA,
    );

    expect(prompt).toMatch(/fail\s+closed/);
    expect(prompt).toContain('operator for adjudication');
  });

  it('performs one structured repair and persists the repaired verdict', async () => {
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: 'not json',
        lastResultText: 'not json',
      })
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: JSON.stringify(validResult()),
        lastResultText: JSON.stringify(validResult()),
      });

    await handleDiscoveryReview(
      'task-1',
      payload(),
      {},
      'token',
      'owner',
      'repo',
      null,
      { value: '' },
    );

    expect(prepareWorkspaceForRole).toHaveBeenCalledWith(
      expect.objectContaining({ depth: 1 }),
    );
    expect(git).toHaveBeenCalledWith([
      'fetch',
      '--depth=1',
      '--no-tags',
      'origin',
      CANDIDATE_SHA,
    ]);
    expect(runClaudeCode).toHaveBeenCalledTimes(2);
    expect(persistAndPostStatus).toHaveBeenLastCalledWith(
      'task-1',
      expect.objectContaining({
        status: 'COMPLETED',
        artifact_type: 'DISCOVERY_REVIEW',
      }),
    );
  });

  it('rejects an oversized server snapshot before preparing a workspace', async () => {
    await handleDiscoveryReview(
      'task-oversized-contract',
      payload({ intent_snapshot: { goal: 'x'.repeat(20_000) } }),
      {},
      'token',
      'owner',
      'repo',
      null,
      { value: '' },
    );

    expect(prepareWorkspaceForRole).not.toHaveBeenCalled();
    expect(preparePacketOnlyWorkspace).not.toHaveBeenCalled();
    expect(runClaudeCode).not.toHaveBeenCalled();
    expect(persistAndPostStatus).toHaveBeenLastCalledWith(
      'task-oversized-contract',
      expect.objectContaining({
        status: 'FAILED',
        failure_category: 'discovery_review_contract_invalid',
      }),
    );
  });

  it('fails closed after one unsuccessful repair', async () => {
    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 0,
      allOutput: 'still not json',
      lastResultText: 'still not json',
    });

    await handleDiscoveryReview(
      'task-2',
      payload({ candidate_sha: null }),
      {},
      'token',
      'owner',
      'repo',
      null,
      { value: '' },
    );

    expect(prepareWorkspaceForRole).not.toHaveBeenCalled();
    expect(preparePacketOnlyWorkspace).toHaveBeenCalledWith(
      '/tmp/discovery-review-test',
    );
    expect(runClaudeCode).toHaveBeenCalledTimes(2);
    expect(persistAndPostStatus).toHaveBeenLastCalledWith(
      'task-2',
      expect.objectContaining({
        status: 'FAILED',
        failure_category: 'discovery_review_unparseable',
      }),
    );
  });

  it('rejects a verdict when shell access modified the pinned checkout', async () => {
    vi.mocked(git).mockImplementation(args => {
      if (args[0] === 'rev-parse') return CANDIDATE_SHA;
      if (args[0] === 'status') return ' M src/product.ts';
      return '';
    });
    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 0,
      allOutput: JSON.stringify(validResult()),
      lastResultText: JSON.stringify(validResult()),
    });

    await handleDiscoveryReview(
      'task-dirty',
      payload(),
      {},
      'token',
      'owner',
      'repo',
      null,
      { value: '' },
    );

    expect(runClaudeCode).toHaveBeenCalledTimes(1);
    expect(persistAndPostStatus).toHaveBeenLastCalledWith(
      'task-dirty',
      expect.objectContaining({
        status: 'FAILED',
        failure_category: 'discovery_review_workspace_modified',
      }),
    );
  });
});
