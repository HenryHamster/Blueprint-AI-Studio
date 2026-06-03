import { describe, it, expect, vi, beforeEach } from 'vitest';

// Capture the prompt the handler hands to the thinker (analysis) pass.
let capturedThinkerPrompt: string | undefined;
// Capture the params the handler passes to buildThinkerPrompt so tests can
// assert on derived values (problem_model recovery framing).
let capturedThinkerParams: Record<string, unknown> | undefined;

vi.mock('fs', () => ({
  default: {
    existsSync: vi.fn().mockReturnValue(false),
    readFileSync: vi.fn().mockReturnValue(''),
    writeFileSync: vi.fn(),
  },
}));

vi.mock('../index.js', () => ({
  WORKSPACE: '/tmp/workspace',
  strVal: (v: unknown, def = '') => (typeof v === 'string' ? v : def),
  sanitize: (s: string) => s,
  sendLog: vi.fn(),
  readHandlerOutput: vi.fn().mockReturnValue(null),
  persistAndPostStatus: vi.fn().mockResolvedValue(undefined),
  runClaudeCode: vi.fn().mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' }),
  apiPost: vi.fn().mockResolvedValue({}),
  resolveBaseBranch: () => 'main',
  setCurrentPromptFingerprint: vi.fn(),
  sendHeartbeatSync: vi.fn(),
}));

vi.mock('../workspacePrep.js', () => ({
  prepareWorkspaceForRole: vi.fn().mockResolvedValue(undefined),
}));

vi.mock('../prompt.js', () => ({
  buildContextPrefix: () => '',
  configureAgentPermissions: () => [],
  writePlaybookHelper: vi.fn(),
  writeMessagesHelper: vi.fn(),
}));

vi.mock('./strategic-helpers.js', () => ({
  writeStrategicHelpers: vi.fn(),
}));

vi.mock('../utils/time.js', () => ({
  parseBackendTimestamp: () => null,
}));

vi.mock('../utils/design-doc-presence.js', () => ({
  reconcileDesignDocPresence: (doc: unknown) => doc,
}));

vi.mock('./research-direction-context.js', () => ({
  formatPreviousDecisionOutcomes: () => '',
  formatPastRetirementCritiques: () => '',
  formatRetiredDirectionsSummary: () => '',
}));

vi.mock('../prompts/thinker.js', () => ({
  computeResearchPlanPromptFingerprint: () => 'fp',
  buildThinkerPrompt: (p: Record<string, unknown>) => {
    capturedThinkerParams = p;
    return 'BASE_THINKER_PROMPT';
  },
  // Capture the rendered prompt, then short-circuit so the executor pass never runs.
  runThinkerWithGate: vi.fn((prompt: string) => {
    capturedThinkerPrompt = prompt;
    return Promise.resolve({ ok: false });
  }),
}));

vi.mock('./research-plan.js', () => ({
  buildStrategicCompletionLogs: () => ({ logs: '', warning: '' }),
  readProgressArtifact: () => '',
  buildRejectedProposalsSection: () => '',
  buildRunningUpdateSupersededSection: (signal: Record<string, unknown> | null | undefined) =>
    signal && Number(signal.count) > 0 ? '\n_operator preserved running_update_\n' : '',
  buildUserPausedSection: () => '',
  buildVersionPhaseSection: () => '',
  buildMemoCitationsSection: () => '',
  resolveMemoHandles: () => [],
  buildEfficiencySnapshotSection: () => '',
  buildPortfolioComposition: () => '',
}));

vi.mock('./strategic-sections.js', () => ({
  buildSystemMaturitySection: () => '',
  buildCapabilityVerificationSection: () => '',
  buildDriftSection: () => '',
  buildOutcomeSection: () => '',
  buildClockAndRecentWorkSection: () => ({ clockHeader: '', recentWorkSection: '' }),
  buildConversationThreadSection: () => '',
  buildFeedbackSection: () => '',
  buildOpenMessagesText: () => '',
  buildDirSummaryText: () => '',
  buildDirHealthSummary: () => '',
}));

import { renderStrategicPromptSuffix, handleStrategic } from './strategic.js';

describe('renderStrategicPromptSuffix', () => {
  it('renders prompt_suffix as an "Additional instructions" block', () => {
    const suffix = 'DEPLOY FRAMING: integration-branch landings are not yet live.\n'
      + 'STABILIZE: open a targeted audit for the last qualification findings.';
    const out = renderStrategicPromptSuffix({ prompt_suffix: suffix });
    expect(out).toBe(`\n\nAdditional instructions:\n${suffix}`);
  });

  it('returns empty string when prompt_suffix is absent', () => {
    expect(renderStrategicPromptSuffix({})).toBe('');
    expect(renderStrategicPromptSuffix({ prompt_suffix: '' })).toBe('');
  });
});

describe('handleStrategic problem_model recovery framing', () => {
  beforeEach(() => {
    capturedThinkerParams = undefined;
  });

  it('dropped-but-nonempty: packer dropped problem_model → steer to fetch, not bootstrap', async () => {
    const payload: Record<string, unknown> = {
      project_name: 'Blueprint',
      // problem_model absent from payload (dropped under byte budget)
      _packer_meta: {
        dropped: [{ key: 'problem_model', reason: 'exceeds remaining budget even after summarize' }],
        trimmed: [],
      },
    };
    const statusRef = { value: '' };
    await handleStrategic('task-d', payload, {}, 'tok', 'owner', 'repo', null, statusRef);

    const params = capturedThinkerParams;
    expect(params).toBeDefined();
    expect(params?.problemModelRecovery).toBe('fetch');
    const section = String(params?.problemModelSection);
    // Must NOT present this as a first cycle...
    expect(section).not.toContain('this is the first cycle');
    // ...and must steer to fetch-the-full-model-then-edit.
    expect(section).toContain('./fetch problem-model');
    expect(section).toContain('EXISTS but was not shipped in full');
  });

  it('trimmed/summarized: packer summarized problem_model → steer to fetch even with partial JSON', async () => {
    const payload: Record<string, unknown> = {
      project_name: 'Blueprint',
      problem_model: { intent: { purpose: 'partial' } },
      _packer_meta: {
        dropped: [],
        trimmed: [{ key: 'problem_model', kind: 'summarized' }],
      },
    };
    const statusRef = { value: '' };
    await handleStrategic('task-t', payload, {}, 'tok', 'owner', 'repo', null, statusRef);

    const params = capturedThinkerParams;
    expect(params?.problemModelRecovery).toBe('fetch');
    const section = String(params?.problemModelSection);
    expect(section).toContain('./fetch problem-model');
    expect(section).not.toContain('this is the first cycle');
  });

  it('true-empty: no packer drop/trim and empty model → bootstrap framing (first cycle)', async () => {
    const payload: Record<string, unknown> = {
      project_name: 'Blueprint',
      // no problem_model, no packer meta naming it
    };
    const statusRef = { value: '' };
    await handleStrategic('task-e', payload, {}, 'tok', 'owner', 'repo', null, statusRef);

    const params = capturedThinkerParams;
    expect(params?.problemModelRecovery).toBe('empty');
    const section = String(params?.problemModelSection);
    expect(section).toContain('this is the first cycle');
    expect(section).not.toContain('./fetch problem-model');
  });

  it('present: full model shipped → present framing, no fetch steering', async () => {
    const payload: Record<string, unknown> = {
      project_name: 'Blueprint',
      problem_model: { intent: { purpose: 'real' }, capabilities: [], gaps: [] },
    };
    const statusRef = { value: '' };
    await handleStrategic('task-p', payload, {}, 'tok', 'owner', 'repo', null, statusRef);

    const params = capturedThinkerParams;
    expect(params?.problemModelRecovery).toBe('present');
    const section = String(params?.problemModelSection);
    expect(section).toContain('```json');
    expect(section).not.toContain('this is the first cycle');
    expect(section).not.toContain('EXISTS but was not shipped in full');
  });
});

describe('handleStrategic running_update_superseded wiring', () => {
  beforeEach(() => {
    capturedThinkerParams = undefined;
  });

  it('wires a running_update_superseded section when the payload signal is present', async () => {
    const payload: Record<string, unknown> = {
      project_name: 'Blueprint',
      running_update_superseded: { count: 2, most_recent_at: '2026-05-20T09:30:00Z' },
    };
    const statusRef = { value: '' };
    await handleStrategic('task-rus', payload, {}, 'tok', 'owner', 'repo', null, statusRef);

    expect(String(capturedThinkerParams?.runningUpdateSupersededSection)).toContain(
      'operator preserved running_update'
    );
  });

  it('passes an empty section when no superseded signal is present', async () => {
    const payload: Record<string, unknown> = { project_name: 'Blueprint' };
    const statusRef = { value: '' };
    await handleStrategic('task-rus-empty', payload, {}, 'tok', 'owner', 'repo', null, statusRef);

    expect(capturedThinkerParams?.runningUpdateSupersededSection).toBe('');
  });
});

describe('handleStrategic per-gap memory pre-trim wiring', () => {
  beforeEach(() => {
    capturedThinkerParams = undefined;
  });

  it('extracts gap_ids from problem_model._gap_memory_trimmed and passes them to the thinker', async () => {
    const payload: Record<string, unknown> = {
      project_name: 'Blueprint',
      problem_model: {
        intent: { purpose: 'real' },
        gaps: [{ id: 'g-long-lived' }, { id: 'g-fresh' }],
        // The backend pre-trim marker: per-gap append-only memory shortened
        // to a recent window before the byte-budget packer ran.
        _gap_memory_trimmed: [
          { gap_id: 'g-long-lived', fields: { history: { shown: 3, total: 41 } } },
        ],
      },
    };
    const statusRef = { value: '' };
    await handleStrategic('task-gm', payload, {}, 'tok', 'owner', 'repo', null, statusRef);

    const params = capturedThinkerParams;
    expect(params).toBeDefined();
    // The model is still 'present' — the pre-trim is independent of packer drops.
    expect(params?.problemModelRecovery).toBe('present');
    expect(params?.gapMemoryTrimmedGapIds).toEqual(['g-long-lived']);
  });

  it('passes an empty list when no gap memory was trimmed', async () => {
    const payload: Record<string, unknown> = {
      project_name: 'Blueprint',
      problem_model: { intent: { purpose: 'real' }, gaps: [{ id: 'g-1' }] },
    };
    const statusRef = { value: '' };
    await handleStrategic('task-gm-empty', payload, {}, 'tok', 'owner', 'repo', null, statusRef);

    expect(capturedThinkerParams?.gapMemoryTrimmedGapIds).toEqual([]);
  });

  it('drops entries with missing gap_id from the trimmed list', async () => {
    const payload: Record<string, unknown> = {
      project_name: 'Blueprint',
      problem_model: {
        intent: { purpose: 'real' },
        _gap_memory_trimmed: [
          { gap_id: 'g-a', fields: {} },
          { fields: {} }, // malformed: no gap_id
          { gap_id: 'g-b', fields: {} },
        ],
      },
    };
    const statusRef = { value: '' };
    await handleStrategic('task-gm-mal', payload, {}, 'tok', 'owner', 'repo', null, statusRef);

    expect(capturedThinkerParams?.gapMemoryTrimmedGapIds).toEqual(['g-a', 'g-b']);
  });
});

describe('handleStrategic prompt_suffix wiring', () => {
  beforeEach(() => {
    capturedThinkerPrompt = undefined;
  });

  it('appends backend prompt_suffix content to the rendered thinker prompt', async () => {
    const suffix = 'STABILIZE GUIDANCE: last qualification flagged audit finding F-123.';
    const payload: Record<string, unknown> = {
      project_name: 'Blueprint',
      prompt_suffix: suffix,
    };
    const statusRef = { value: '' };

    await handleStrategic(
      'task-1', payload, {}, 'tok', 'owner', 'repo', null, statusRef,
    );

    expect(capturedThinkerPrompt).toBeDefined();
    // The analysis-pass content is preserved...
    expect(capturedThinkerPrompt).toContain('BASE_THINKER_PROMPT');
    // ...and the backend-supplied guidance reaches the rendered prompt.
    expect(capturedThinkerPrompt).toContain('Additional instructions:');
    expect(capturedThinkerPrompt).toContain(suffix);
  });

  it('leaves the thinker prompt unchanged when prompt_suffix is absent', async () => {
    const statusRef = { value: '' };
    await handleStrategic(
      'task-2', { project_name: 'Blueprint' }, {}, 'tok', 'owner', 'repo', null, statusRef,
    );
    expect(capturedThinkerPrompt).toBe('BASE_THINKER_PROMPT');
  });
});
