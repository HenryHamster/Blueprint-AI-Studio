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

// Default challenge record the mocked runStrategicChallenge resolves with. Tests
// that care about the challenge override it per-case via mockResolvedValueOnce.
// Hoisted so it is initialized before the (hoisted) vi.mock factory references it.
const { DEFAULT_CHALLENGE_RECORD } = vi.hoisted(() => ({
  DEFAULT_CHALLENGE_RECORD: {
    challenge_version: 1,
    status: 'ok' as const,
    evidence_progress: [] as Array<{ direction_id: string; status: string; verdict: string; rationale: string }>,
    retirement_approvals: [] as Array<{ direction_id: string; verdict: string; rationale: string }>,
    retirement_critique_quality: { verdict: 'solid', rationale: '' },
    portfolio_shape_risk: { verdict: 'low', rationale: '' },
  },
}));

vi.mock('./strategic-helpers.js', () => ({
  writeStrategicHelpers: vi.fn(),
  // Mocked so the wiring tests do NOT spawn the real challenge runClaudeCode
  // pass — that keeps runClaudeCode.mock.calls[0] the executor prompt for the
  // existing executor-prompt assertions.
  runStrategicChallenge: vi.fn().mockResolvedValue(DEFAULT_CHALLENGE_RECORD),
  // Echo the record's status + approved pairs so wiring tests can assert the
  // section is built from the record runStrategicChallenge returned and is
  // threaded into the executor prompt.
  buildStrategicChallengeExecutorSection: (record: {
    status: string;
    evidence_progress: Array<{ direction_id: string; status: string; verdict: string }>;
    retirement_approvals?: Array<{ direction_id: string; verdict: string }>;
  }) => {
    const approved = record.evidence_progress
      .filter(e => e.verdict === 'approved' && (e.status === 'advancing' || e.status === 'met'))
      .map(e => `${e.direction_id}:${e.status}`)
      .join(',');
    const retirementApproved = (record.retirement_approvals || [])
      .filter(r => r.verdict === 'approved')
      .map(r => r.direction_id)
      .join(',');
    return `CHALLENGE_SECTION(status=${record.status};approved=${approved};retirements=${retirementApproved})`;
  },
}));

vi.mock('../utils/time.js', () => ({
  parseBackendTimestamp: () => null,
}));

vi.mock('../utils/design-doc-presence.js', () => ({
  reconcileDesignDocPresence: (doc: unknown) => doc,
  // Identity stub: the handler now reconciles the compact index. Return it
  // unchanged so the overview-rendering assertions see the payload's index.
  reconcileDesignDocIndex: (index: unknown) => index,
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
  buildOperatorOverridesSection: (o: Array<Record<string, unknown>>) =>
    Array.isArray(o) && o.length ? `OPERATOR_OVERRIDES(${o.length})` : '',
  buildRunningUpdateSupersededSection: (signal: Record<string, unknown> | null | undefined) =>
    signal && Number(signal.count) > 0 ? '\n_operator preserved running_update_\n' : '',
  buildUserPausedSection: () => '',
  buildVersionPhaseSection: () => '',
  buildMemoCitationsSection: () => '',
  resolveMemoHandles: () => [],
  buildEfficiencySnapshotSection: () => '',
  // Echo the authoritative active-total arg so wiring tests can assert the
  // handler threads payload.direction_counts.active through.
  buildPortfolioComposition: (
    _a: unknown,
    _b: unknown,
    _c: unknown,
    authoritativeActiveTotal?: number,
  ) => `PORTFOLIO(auth=${authoritativeActiveTotal})`,
  // Echo the rendered capability count so wiring tests can assert the handler
  // threads payload.available_capabilities through to the vocabulary section.
  buildAvailableCapabilitiesSection: (caps: Array<Record<string, unknown>> | undefined) =>
    Array.isArray(caps) && caps.length ? `AVAILABLE_CAPS(${caps.length})` : '',
  CAPABILITY_DECLARATION_DOC: 'CAP_DECL_DOC',
}));

vi.mock('./strategic-sections.js', () => ({
  buildSystemMaturitySection: () => '',
  // Echo presence so wiring tests can assert the handler passes
  // payload.cycle_start_reconciliation through to this builder.
  buildCycleStartReconciliationSection: (recon: Record<string, unknown> | undefined) =>
    recon ? `RECON(${Object.keys(recon).length})` : '',
  buildPruneCandidatesSection: (prune: Record<string, unknown> | undefined) =>
    prune ? `PRUNE(${Object.keys(prune).length})` : '',
  buildModelHealthSection: () => '',
  buildDriftSection: () => '',
  buildOutcomeSection: () => '',
  buildClockAndRecentWorkSection: () => ({ clockHeader: '', recentWorkSection: '' }),
  buildConversationThreadSection: () => '',
  buildFeedbackSection: () => '',
  buildOpenMessagesText: () => '',
  // Echo the authoritative count args so wiring tests can assert the handler
  // threads payload.direction_counts through to these builders.
  buildDirSummaryText: (_d: unknown, authoritativeActiveCount?: number) =>
    `DIRSUMMARY(auth=${authoritativeActiveCount})`,
  buildDirHealthSummary: (
    _d: unknown,
    maxActiveDirections: number,
    authoritative?: { active: number; paused: number },
  ) => `DIRHEALTH(max=${maxActiveDirections},auth=${authoritative ? authoritative.active : undefined})`,
}));

import {
  renderStrategicPromptSuffix,
  buildStagedDirectionsSection,
  renderDesignDocIndexOverview,
  handleStrategic,
} from './strategic.js';
import { runClaudeCode, apiPost } from '../index.js';
import { VmConfigMissingError } from '../providers/runners.js';
import { runThinkerWithGate } from '../prompts/thinker.js';
import { runStrategicChallenge } from './strategic-helpers.js';

describe('buildStagedDirectionsSection', () => {
  it('returns empty string when nothing is staged', () => {
    expect(buildStagedDirectionsSection([])).toBe('');
    expect(buildStagedDirectionsSection(undefined as unknown as Array<Record<string, unknown>>)).toBe('');
  });

  it('renders staged entries with their target version and the no-defer framing', () => {
    const out = buildStagedDirectionsSection([
      { id: 'd-1', text: 'next-version feature', priority: 'high', staged_for_version: 4 },
    ]);
    expect(out).toContain('## Staged for next version');
    expect(out).toContain('d-1 [high] — next-version feature (auto-activates when v4 promotes)');
    expect(out).toContain('not a way to defer');
  });
});

describe('renderDesignDocIndexOverview', () => {
  it('returns empty string for an absent or section-less index', () => {
    expect(renderDesignDocIndexOverview(undefined)).toBe('');
    expect(renderDesignDocIndexOverview({})).toBe('');
    expect(renderDesignDocIndexOverview({ sections: [] })).toBe('');
  });

  it('renders systems/capabilities as a status catalog, not raw JSON', () => {
    const out = renderDesignDocIndexOverview({
      sections: [
        {
          key: 'systems/auth',
          status: 'mature',
          summary: 'Session + token auth',
          modules: ['backend/app/auth.py'],
        },
        {
          key: 'capabilities/login',
          status: 'working',
          summary: 'Users can log in',
        },
        { key: 'notes/strategic-context', summary: 'v5 stabilize' },
      ],
    });
    expect(out).toContain('### Systems');
    expect(out).toContain('`systems/auth`');
    expect(out).toContain('[mature]');
    expect(out).toContain('Session + token auth');
    expect(out).toContain('modules: backend/app/auth.py');
    expect(out).toContain('### Capabilities');
    expect(out).toContain('`capabilities/login`');
    expect(out).toContain('[working]');
    expect(out).toContain('### Other sections');
    expect(out).toContain('`notes/strategic-context`');
    // It is an overview, not a JSON dump.
    expect(out).not.toContain('"key":');
    expect(out).not.toContain('{');
    // Teaches on-demand fetch by key.
    expect(out).toContain('./design-doc get <key>');
    expect(out).toContain('./design-doc list');
  });

  it('surfaces a presence_warning on a downgraded entry', () => {
    const out = renderDesignDocIndexOverview({
      sections: [
        {
          key: 'systems/ghost',
          status: 'missing',
          modules: ['backend/app/ghost.py'],
          presence_warning: 'all referenced module file(s) are absent',
        },
      ],
    });
    expect(out).toContain('`systems/ghost`');
    expect(out).toContain('all referenced module file(s) are absent');
  });

  it('renders repo_overview inline bootstrap fields (tech_stack, directory_layout, first_steps)', () => {
    const out = renderDesignDocIndexOverview({
      sections: [
        {
          key: 'repo_overview',
          tech_stack: 'TypeScript, Node.js, Vitest',
          directory_layout: 'agent/ — worker; backend/ — API',
          first_steps_summary: 'cd agent && npm install && npm test',
        },
      ],
    });
    expect(out).toContain('`repo_overview`');
    expect(out).toContain('tech_stack: TypeScript, Node.js, Vitest');
    expect(out).toContain('directory_layout: agent/ — worker; backend/ — API');
    expect(out).toContain('first_steps: cd agent && npm install && npm test');
    // Should appear under Other sections (not systems/capabilities).
    expect(out).toContain('### Other sections');
    expect(out).not.toContain('### Systems');
  });
});

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

describe('handleStrategic executor prompt: atomic staged create-direction', () => {
  it('documents create-direction with "staged": true as a single atomic op (no create-then-stage)', async () => {
    vi.mocked(runClaudeCode).mockClear();
    // Let the thinker gate pass so the executor prompt is actually built and
    // handed to runClaudeCode (its first arg is the executor prompt).
    vi.mocked(runThinkerWithGate).mockResolvedValueOnce({
      ok: true,
      analysisText: 'analysis',
    } as unknown as { ok: boolean; analysisText: string });
    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 0,
      allOutput: '',
      lastResultText: '',
    });

    const statusRef = { value: '' };
    await handleStrategic(
      'task-staged-exec',
      { project_name: 'Blueprint' },
      {},
      'tok',
      'owner',
      'repo',
      null,
      statusRef,
    );

    const executorPrompt = vi.mocked(runClaudeCode).mock.calls[0]?.[0];
    expect(executorPrompt).toBeDefined();
    expect(executorPrompt).toContain('"staged": true');
    expect(executorPrompt).toContain('one atomic op');
    // Steers AWAY from the old create-then-stage sequence for new ideas.
    expect(executorPrompt).toContain('Do NOT create-then-stage');
  });

  it('treats problem_model reconstruction as the last resort after bounded repair', async () => {
    vi.mocked(runClaudeCode).mockClear();
    vi.mocked(runThinkerWithGate).mockResolvedValueOnce({
      ok: true,
      analysisText: 'analysis',
    } as unknown as { ok: boolean; analysisText: string });
    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 0,
      allOutput: '',
      lastResultText: '',
    });

    const statusRef = { value: '' };
    await handleStrategic(
      'task-last-resort',
      { project_name: 'Blueprint' },
      {},
      'tok',
      'owner',
      'repo',
      null,
      statusRef,
    );

    const executorPrompt = vi.mocked(runClaudeCode).mock.calls[0]?.[0];
    expect(executorPrompt).toContain('if the file is still missing, empty, or the JSON is malformed after the bounded repair reopen');
  });
});

describe('handleStrategic strategic challenge gate', () => {
  beforeEach(() => {
    vi.mocked(runClaudeCode).mockClear();
    vi.mocked(apiPost).mockClear();
    vi.mocked(runStrategicChallenge).mockReset();
    vi.mocked(runStrategicChallenge).mockResolvedValue(
      DEFAULT_CHALLENGE_RECORD as unknown as Awaited<ReturnType<typeof runStrategicChallenge>>,
    );
  });

  it('handleStrategic runs and persists a challenge before executor', async () => {
    // The challenge pass runs over the thinker analysis, the record is persisted
    // via the set_strategic_challenge op BEFORE the executor runs, and the
    // record's challenge-approved evidence-progress pairs reach the executor
    // prompt with the gate instruction.
    vi.mocked(runThinkerWithGate).mockResolvedValueOnce({
      ok: true,
      analysisText: 'ANALYSIS_UNDER_CHALLENGE',
    } as unknown as { ok: boolean; analysisText: string });

    const challengeRecord = {
      challenge_version: 1,
      status: 'ok' as const,
      evidence_progress: [
        { direction_id: 'd-7', status: 'advancing', verdict: 'approved', rationale: 'real evidence' },
        { direction_id: 'd-9', status: 'met', verdict: 'rejected', rationale: 'thin' },
      ],
      retirement_critique_quality: { verdict: 'solid', rationale: 'names the pattern' },
      portfolio_shape_risk: { verdict: 'elevated', rationale: 'concentrated' },
    };
    vi.mocked(runStrategicChallenge).mockResolvedValueOnce(
      challengeRecord as unknown as Awaited<ReturnType<typeof runStrategicChallenge>>,
    );
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });

    const statusRef = { value: '' };
    await handleStrategic(
      'task-challenge', { project_name: 'Blueprint' }, {}, 'tok', 'owner', 'repo', null, statusRef,
    );

    // The challenge ran over the thinker's analysis output.
    expect(vi.mocked(runStrategicChallenge)).toHaveBeenCalledTimes(1);
    const challengeArgs = vi.mocked(runStrategicChallenge).mock.calls[0][0];
    expect(challengeArgs.analysisText).toBe('ANALYSIS_UNDER_CHALLENGE');

    // The record was persisted via the new store op with the verbatim record.
    const storeCallIdx = vi.mocked(apiPost).mock.calls.findIndex(
      c => c[0] === '/api/agent/strategic'
        && (c[1] as { op?: string }).op === 'set_strategic_challenge',
    );
    expect(storeCallIdx).toBeGreaterThanOrEqual(0);
    const storeBody = vi.mocked(apiPost).mock.calls[storeCallIdx][1] as {
      task_id: string; op: string; data: unknown;
    };
    expect(storeBody.task_id).toBe('task-challenge');
    expect(storeBody.data).toBe(challengeRecord);

    // Persist happened BEFORE the executor's first runClaudeCode invocation.
    const storeOrder = vi.mocked(apiPost).mock.invocationCallOrder[storeCallIdx];
    const executorOrder = vi.mocked(runClaudeCode).mock.invocationCallOrder[0];
    expect(storeOrder).toBeLessThan(executorOrder);

    // The executor prompt carries the challenge section (only the APPROVED
    // advancing/met pair, d-7) plus the evidence-progress gate instruction.
    const executorPrompt = vi.mocked(runClaudeCode).mock.calls[0]?.[0];
    expect(executorPrompt).toContain('CHALLENGE_SECTION(status=ok;approved=d-7:advancing;retirements=)');
    expect(executorPrompt).toContain('Challenge gate');
    expect(executorPrompt).toContain('the independent challenge APPROVED');
  });

  it('persists a blocked/invalid challenge record (no approvals) and still reaches the executor', async () => {
    vi.mocked(runThinkerWithGate).mockResolvedValueOnce({
      ok: true,
      analysisText: 'analysis',
    } as unknown as { ok: boolean; analysisText: string });

    const blockedRecord = {
      challenge_version: 1,
      status: 'blocked' as const,
      evidence_progress: [],
      retirement_critique_quality: { verdict: 'not_assessed', rationale: 'challenge exited 1' },
      portfolio_shape_risk: { verdict: 'not_assessed', rationale: 'challenge exited 1' },
    };
    vi.mocked(runStrategicChallenge).mockResolvedValueOnce(
      blockedRecord as unknown as Awaited<ReturnType<typeof runStrategicChallenge>>,
    );
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });

    const statusRef = { value: '' };
    await handleStrategic(
      'task-blocked', { project_name: 'Blueprint' }, {}, 'tok', 'owner', 'repo', null, statusRef,
    );

    const storeCall = vi.mocked(apiPost).mock.calls.find(
      c => (c[1] as { op?: string }).op === 'set_strategic_challenge',
    );
    expect(storeCall).toBeDefined();
    const storeData = (storeCall?.[1] as { data: typeof blockedRecord } | undefined)?.data;
    expect(storeData?.status).toBe('blocked');

    // A blocked challenge approves nothing — the executor prompt must say so and
    // list no approved pairs.
    const executorPrompt = vi.mocked(runClaudeCode).mock.calls[0]?.[0];
    expect(executorPrompt).toContain('CHALLENGE_SECTION(status=blocked;approved=;retirements=)');
  });

  it('executor prompt carries retirement gate section wired from challenge record', async () => {
    vi.mocked(runThinkerWithGate).mockResolvedValueOnce({
      ok: true,
      analysisText: 'analysis',
    } as unknown as { ok: boolean; analysisText: string });

    const challengeWithRetirement = {
      ...DEFAULT_CHALLENGE_RECORD,
      retirement_approvals: [
        { direction_id: 'd-30', verdict: 'approved', rationale: 'real pattern' },
        { direction_id: 'd-31', verdict: 'rejected', rationale: 'shallow' },
      ],
    };
    vi.mocked(runStrategicChallenge).mockResolvedValueOnce(
      challengeWithRetirement as unknown as Awaited<ReturnType<typeof runStrategicChallenge>>,
    );
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });

    await handleStrategic(
      'task-ret-gate', { project_name: 'Blueprint' }, {}, 'tok', 'owner', 'repo', null, { value: '' },
    );

    const executorPrompt = vi.mocked(runClaudeCode).mock.calls[0]?.[0];
    // Mock echoes only approved retirements (d-30, not d-31).
    expect(executorPrompt).toContain('CHALLENGE_SECTION(status=ok;approved=;retirements=d-30)');
    // Static executor instruction mentions deferred response handling.
    expect(executorPrompt).toContain('"status": "deferred"');
    expect(executorPrompt).toContain('SOFT NON-ERROR OUTCOME');
  });

  it('fails closed when challenge persistence fails before executor', async () => {
    vi.mocked(runThinkerWithGate).mockResolvedValueOnce({
      ok: true,
      analysisText: 'analysis',
    } as unknown as { ok: boolean; analysisText: string });

    vi.mocked(runStrategicChallenge).mockResolvedValueOnce(
      DEFAULT_CHALLENGE_RECORD as unknown as Awaited<ReturnType<typeof runStrategicChallenge>>,
    );
    vi.mocked(apiPost).mockRejectedValueOnce(new Error('challenge store failed'));

    const statusRef = { value: '' };
    await expect(
      handleStrategic(
        'task-challenge-fail', { project_name: 'Blueprint' }, {}, 'tok', 'owner', 'repo', null, statusRef,
      ),
    ).rejects.toThrow('Failed to persist strategic challenge record: Error: challenge store failed');

    expect(vi.mocked(runClaudeCode)).not.toHaveBeenCalled();
  });
});

describe('handleStrategic operator_overrides wiring', () => {
  beforeEach(() => {
    capturedThinkerParams = undefined;
  });

  it('threads payload.operator_overrides into the thinker params', async () => {
    const payload: Record<string, unknown> = {
      project_name: 'Blueprint',
      operator_overrides: [
        { document: 'running_update', field: 'running_update', agent_value: 'a', at: '2026-06-01' },
        { document: 'vision', field: 'vision.goal', agent_value: 'b', operator_value: 'c', at: '2026-06-02' },
      ],
    };
    const statusRef = { value: '' };
    await handleStrategic('task-ov', payload, {}, 'tok', 'owner', 'repo', null, statusRef);

    const params = capturedThinkerParams;
    expect(params?.operatorOverridesSection).toBe('OPERATOR_OVERRIDES(2)');
  });

  it('passes empty section when operator_overrides absent', async () => {
    const payload: Record<string, unknown> = { project_name: 'Blueprint' };
    const statusRef = { value: '' };
    await handleStrategic('task-no-ov', payload, {}, 'tok', 'owner', 'repo', null, statusRef);

    const params = capturedThinkerParams;
    expect(params?.operatorOverridesSection).toBe('');
  });
});

describe('handleStrategic cycle_start_reconciliation wiring', () => {
  beforeEach(() => {
    capturedThinkerParams = undefined;
  });

  it('threads payload.cycle_start_reconciliation into the thinker params', async () => {
    const payload: Record<string, unknown> = {
      project_name: 'Blueprint',
      cycle_start_reconciliation: {
        capability_contradictions: [],
        orphaned_gaps: [{ gap_id: 'g-1', name: 'x' }],
        ungrounded_directions: [],
        direction_pipeline: [],
        counts: { active_directions: 3 },
      },
    };
    const statusRef = { value: '' };
    await handleStrategic('task-recon', payload, {}, 'tok', 'owner', 'repo', null, statusRef);

    const params = capturedThinkerParams;
    expect(params?.cycleStartReconciliationSection).toBe('RECON(5)');
  });

  it('passes an empty section when cycle_start_reconciliation is absent', async () => {
    const payload: Record<string, unknown> = { project_name: 'Blueprint' };
    const statusRef = { value: '' };
    await handleStrategic('task-recon-empty', payload, {}, 'tok', 'owner', 'repo', null, statusRef);

    expect(capturedThinkerParams?.cycleStartReconciliationSection).toBe('');
  });
});

describe('handleStrategic authoritative direction_counts wiring', () => {
  beforeEach(() => {
    capturedThinkerParams = undefined;
  });

  it('anchors direction views on direction_counts when directions_summary is empty', async () => {
    // directions_summary dropped under budget; direction_counts (critical) carries
    // the true active count + real ceiling.
    const payload: Record<string, unknown> = {
      project_name: 'Blueprint',
      directions_summary: [],
      direction_counts: { active: 12, paused: 1, total: 13, max_active: 15 },
      max_active_directions: 15,
    };
    const statusRef = { value: '' };
    await handleStrategic('task-dc', payload, {}, 'tok', 'owner', 'repo', null, statusRef);

    const p = capturedThinkerParams;
    // The handler threads the authoritative active count (12) and the real
    // ceiling (15) into all three direction builders.
    expect(String(p?.dirHealthSummary)).toBe('DIRHEALTH(max=15,auth=12)');
    expect(String(p?.portfolioCompositionText)).toBe('PORTFOLIO(auth=12)');
    expect(String(p?.dirSummaryText)).toBe('DIRSUMMARY(auth=12)');
  });

  it('falls back to undefined authoritative count + default ceiling when direction_counts absent', async () => {
    const payload: Record<string, unknown> = {
      project_name: 'Blueprint',
      directions_summary: [],
    };
    const statusRef = { value: '' };
    await handleStrategic('task-dc-absent', payload, {}, 'tok', 'owner', 'repo', null, statusRef);

    const p = capturedThinkerParams;
    expect(String(p?.dirHealthSummary)).toBe('DIRHEALTH(max=10,auth=undefined)');
    expect(String(p?.portfolioCompositionText)).toBe('PORTFOLIO(auth=undefined)');
    expect(String(p?.dirSummaryText)).toBe('DIRSUMMARY(auth=undefined)');
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

describe('handleStrategic design_doc index rendering', () => {
  beforeEach(() => {
    capturedThinkerParams = undefined;
  });

  it('renders the design_doc index as an architecture overview (not raw JSON) into designDocJson', async () => {
    const payload: Record<string, unknown> = {
      project_name: 'Blueprint',
      design_doc: {
        sections: [
          {
            key: 'systems/auth',
            status: 'mature',
            summary: 'Session + token auth',
            modules: ['backend/app/auth.py'],
          },
          { key: 'capabilities/login', status: 'working', summary: 'Users can log in' },
        ],
      },
    };
    const statusRef = { value: '' };
    await handleStrategic('task-dd', payload, {}, 'tok', 'owner', 'repo', null, statusRef);

    const rendered = String(capturedThinkerParams?.designDocJson);
    // Architecture catalog form, not a JSON dump of the index object.
    expect(rendered).toContain('### Systems');
    expect(rendered).toContain('`systems/auth`');
    expect(rendered).toContain('[mature]');
    expect(rendered).toContain('### Capabilities');
    expect(rendered).not.toContain('"sections"');
    expect(rendered).not.toContain('"key":');
    // Teaches on-demand fetch by key.
    expect(rendered).toContain('./design-doc get <key>');
    expect(rendered).toContain('./design-doc list');
  });

  it('does not crash and renders empty designDocJson on the new shape when sections are absent', async () => {
    const payload: Record<string, unknown> = {
      project_name: 'Blueprint',
      design_doc: { sections: [] },
    };
    const statusRef = { value: '' };
    await handleStrategic('task-dd-empty', payload, {}, 'tok', 'owner', 'repo', null, statusRef);

    expect(capturedThinkerParams).toBeDefined();
    expect(capturedThinkerParams?.designDocJson).toBe('');
  });

  it('derives strategicContext from the notes/strategic-context index entry with a fetch hint', async () => {
    const payload: Record<string, unknown> = {
      project_name: 'Blueprint',
      design_doc: {
        sections: [
          { key: 'notes/strategic-context', summary: 'v5 stabilize — harden toward qualification' },
        ],
      },
    };
    const statusRef = { value: '' };
    await handleStrategic('task-sc', payload, {}, 'tok', 'owner', 'repo', null, statusRef);

    const ctx = String(capturedThinkerParams?.strategicContext);
    expect(ctx).toContain('v5 stabilize — harden toward qualification');
    // Cross-cycle running context is not silently lost — a fetch hint remains.
    expect(ctx).toContain('./design-doc get notes/strategic-context');
  });

  it('emits empty strategicContext when no notes/strategic-context entry exists', async () => {
    const payload: Record<string, unknown> = {
      project_name: 'Blueprint',
      design_doc: { sections: [{ key: 'systems/auth', status: 'mature' }] },
    };
    const statusRef = { value: '' };
    await handleStrategic('task-sc-none', payload, {}, 'tok', 'owner', 'repo', null, statusRef);

    expect(capturedThinkerParams?.strategicContext).toBe('');
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

  it('threads available_capabilities into the thinker availableCapabilitiesSection param', async () => {
    const statusRef = { value: '' };
    await handleStrategic(
      'task-3',
      {
        project_name: 'Blueprint',
        available_capabilities: [
          { name: 'seeded-eval-harness', status: 'working' },
          { name: 'payment-core', status: 'planned' },
        ],
      },
      {}, 'tok', 'owner', 'repo', null, statusRef,
    );
    const section = capturedThinkerParams?.availableCapabilitiesSection as string;
    // The vocabulary section renders (mock echoes the count) and carries the
    // uniform provides_capability / prerequisite declaration doc.
    expect(section).toContain('AVAILABLE_CAPS(2)');
    expect(section).toContain('CAP_DECL_DOC');
  });

  it('passes an empty availableCapabilitiesSection when no capabilities are supplied', async () => {
    const statusRef = { value: '' };
    await handleStrategic(
      'task-4', { project_name: 'Blueprint' }, {}, 'tok', 'owner', 'repo', null, statusRef,
    );
    expect(capturedThinkerParams?.availableCapabilitiesSection).toBe('');
  });
});

describe('handleStrategic infra-error propagation (vm_config_missing)', () => {
  it('propagates a vm_config_missing death from the required-calls retry instead of swallowing it', async () => {
    // The swallowing retry-catch previously turned a vm_config_missing death into a
    // [WARNING] + a misclassified COMPLETED/claude_code_failed status — the warm VM
    // then stranded into vm_unresponsive. The fix rethrows VmConfigMissingError so it
    // reaches runRoleOnce's classifier for a clean PROVISION_FAILURE requeue.
    vi.mocked(runClaudeCode).mockReset();
    vi.mocked(runThinkerWithGate).mockResolvedValueOnce({
      ok: true,
      analysisText: 'analysis',
    } as unknown as { ok: boolean; analysisText: string });
    // Executor (call 0) returns exit 0 with no required-call evidence → triggers the
    // required-calls retry (call 1), which dies vm_config_missing.
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '' })
      .mockRejectedValueOnce(
        new VmConfigMissingError(
          'provider config file missing on VM (boot/retarget materialization lost)',
        ),
      );

    const statusRef = { value: '' };
    await expect(
      handleStrategic('task-vmcm', { project_name: 'Blueprint' }, {}, 'tok', 'owner', 'repo', null, statusRef),
    ).rejects.toBeInstanceOf(VmConfigMissingError);

    // Restore the shared default so later tests are unaffected.
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
  });
});
