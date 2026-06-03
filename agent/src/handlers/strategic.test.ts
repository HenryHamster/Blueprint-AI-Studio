import { describe, it, expect, vi, beforeEach } from 'vitest';

// Capture the prompt the handler hands to the thinker (analysis) pass.
let capturedThinkerPrompt: string | undefined;

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
  buildThinkerPrompt: () => 'BASE_THINKER_PROMPT',
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
