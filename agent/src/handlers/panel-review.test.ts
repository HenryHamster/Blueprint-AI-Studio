import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../index.js', () => ({
  normalizeClaudeConfigContent: vi.fn((_path: string, content: string) => content),
  WORKSPACE: '/tmp/workspace',
  strVal: (v: unknown, fallback = '') => (typeof v === 'string' ? v : fallback),
  sanitize: (value: string) => value,
  sendLog: vi.fn(),
  validateBranchName: vi.fn((branch: string) => branch),
  persistAndPostStatus: vi.fn().mockResolvedValue(undefined),
  git: vi.fn((args: string[]) => {
    if (args[0] === 'diff' && args[1] === '--name-only') {
      return 'agent/src/handlers/panel-review.ts\nadmin-ui/src/components/TaskDetailDrawerMeta.tsx\n';
    }
    if (args[0] === 'diff' && args[1] === '--shortstat') {
      return '2 files changed, 40 insertions(+), 3 deletions(-)';
    }
    return '';
  }),
  runClaudeCode: vi.fn().mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' }),
  // panel-review.ts calls matchesAuthFailure(output, PROVIDER) inside
  // throwIfLensAuthCrash on any non-zero lens exit. Default false (an ordinary
  // crash is a genuine review failure → fail closed); the dead-auth test
  // overrides this to true to exercise the AuthFailureError rethrow path.
  matchesAuthFailure: vi.fn(() => false),
  AuthFailureError: class AuthFailureError extends Error {},
  PROVIDER: 'codex',
  // Defaults to the native-subagent (parallel) path; serial-fallback tests
  // override this to false.
  providerSupportsNativeSubagents: vi.fn(() => true),
  sendHeartbeatSync: vi.fn(),
  setCurrentPromptFingerprint: vi.fn(),
  snapshotToolTrace: vi.fn().mockReturnValue([]),
  extractInvestigatedFiles: vi.fn().mockReturnValue([
    'agent/src/handlers/panel-review.ts',
    'admin-ui/src/components/TaskDetailDrawerMeta.tsx',
  ]),
  resolveBaseBranch: vi.fn((payload: Record<string, unknown>) => payload.base_branch || 'main'),
  // Each lens reads its OWN output file via readHandlerOutput(taskId, outputType, ws).
  // The mock returns a distinct per-lens verdict keyed on the outputType arg so
  // the test exercises genuine per-lens fan-out, not a single combined file.
  readHandlerOutput: vi.fn((_taskId: string, outputType: string) => {
    if (outputType === 'panel_review_lens_correctness') {
      return {
        verdict: 'REQUEST_CHANGES',
        summary: 'A caller compatibility issue remains.',
        issues: [
          {
            severity: 'major',
            description: 'Caller still expects the old payload shape.',
            file: 'agent/src/handlers/panel-review.ts',
            line: 120,
          },
        ],
      };
    }
    if (outputType === 'panel_review_lens_security') {
      return { verdict: 'APPROVE', summary: 'No security regressions were found.', issues: [] };
    }
    if (outputType === 'panel_review_lens_code_quality') {
      return {
        verdict: 'APPROVE',
        summary: 'The workflow stays inside the existing review infrastructure.',
        issues: [],
      };
    }
    if (outputType === 'panel_review_lens_test_coverage') {
      return {
        verdict: 'APPROVE',
        summary: 'Targeted tests cover the new workflow and UI rendering.',
        issues: [],
      };
    }
    return null;
  }),
}));

vi.mock('../workspacePrep.js', () => ({
  prepareWorkspaceForRole: vi.fn().mockResolvedValue(undefined),
}));

vi.mock('../telemetry.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../telemetry.js')>();
  return {
    ...actual,
    recordFormatMiss: vi.fn(),
    recordFormatRepairAttempt: vi.fn(),
    recordFormatRepairSuccess: vi.fn(),
    recordFormatRepairTokens: vi.fn(),
    recordFormatRepairMiss: vi.fn(),
    recordFormatRepairInfraFailure: vi.fn(),
  };
});

vi.mock('../prompt.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../prompt.js')>();
  return {
    ...actual,
    configureAgentPermissions: vi.fn().mockReturnValue(['Write', 'Edit', 'NotebookEdit']),
    formatPlaybook: vi.fn().mockReturnValue(''),
    formatProjectContext: vi.fn().mockReturnValue(''),
    readClaudeMd: vi.fn().mockReturnValue(''),
    writeMessagesHelper: vi.fn(),
    writePlaybookHelper: vi.fn(),
    computePromptFingerprint: vi.fn().mockReturnValue('panel-review-fp'),
  };
});

vi.mock('../build-config.js', () => ({
  resolveBuildConfig: vi.fn().mockReturnValue({
    directories: [
      { path: 'agent', type: 'node', lint_cmd: 'npx eslint .', build_cmd: 'npm run build' },
      { path: 'admin-ui', type: 'node', lint_cmd: 'npx eslint .', build_cmd: 'npm run build' },
    ],
  }),
}));

vi.mock('child_process', () => ({
  execFileSync: vi.fn().mockReturnValue('abc123\trefs/heads/feature/panel-review\n'),
}));

import { execFileSync } from 'child_process';
import {
  AuthFailureError,
  matchesAuthFailure,
  persistAndPostStatus,
  providerSupportsNativeSubagents,
  readHandlerOutput,
  runClaudeCode,
} from '../index.js';
import { recordFormatMiss, getLlmTelemetry, resetLlmTelemetry } from '../telemetry.js';
import { handlePanelReview, PANEL_REVIEW_LENSES, synthesizeBlindReviewVerdict } from './panel-review.js';
import { expectCalledWith } from '../utils/typed-mock-call.js';

const PAYLOAD = {
  pr_number: 42,
  branch: 'feature/panel-review',
  base_branch: 'automain',
  original_prompt: 'Run the multi-lens panel review.',
};

async function runHandler() {
  await handlePanelReview(
    'task-panel-1',
    { ...PAYLOAD },
    { project_metadata: {}, playbook: {}, context: {} },
    'gh-token',
    'owner',
    'repo',
    null,
    { value: '' },
    'gpt-5',
    'medium',
  );
}

describe('handlePanelReview', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(execFileSync).mockReturnValue('abc123\trefs/heads/feature/panel-review\n');
    // clearAllMocks clears call records but NOT implementations, so explicitly
    // restore the two mocks that individual tests mutate, keeping the default
    // path (native sub-agent / parallel) and a clean exit between tests.
    vi.mocked(providerSupportsNativeSubagents).mockReturnValue(true);
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
    // mockReturnValue survives clearAllMocks, so the dead-auth test's override
    // would leak; restore the default (an ordinary crash is not an auth failure).
    vi.mocked(matchesAuthFailure).mockReturnValue(false);
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ title: 'Panel review PR', body: 'Review this carefully.' }),
    });
  });

  it('declares the fixed backend lens contract in code', () => {
    expect(PANEL_REVIEW_LENSES.map(lens => lens.id)).toEqual([
      'correctness',
      'security',
      'code_quality',
      'test_coverage',
    ]);
  });

  it('parallel path: fans the four lenses out from ONE orchestrating pass', async () => {
    await runHandler();

    // Native sub-agent fan-out → a SINGLE orchestrating runClaudeCode pass.
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(1);

    // The single orchestrating prompt instructs parallel fan-out and embeds
    // EVERY lens's focus and its lens-specific output path, bounded to exactly
    // the four declared lenses.
    const prompt = vi.mocked(runClaudeCode).mock.calls[0]?.[0] ?? '';
    expect(prompt).toContain('Task tool');
    expect(prompt).toContain('SINGLE message');
    for (const lens of PANEL_REVIEW_LENSES) {
      expect(prompt).toContain(lens.focus);
      expect(prompt).toContain(`/tmp/panel_review_lens_${lens.id}_task-panel-1.json`);
    }

    // The handler still reads each lens's own output file back deterministically.
    for (const lens of PANEL_REVIEW_LENSES) {
      expectCalledWith(
        readHandlerOutput,
        'task-panel-1',
        `panel_review_lens_${lens.id}`,
        null,
      );
    }
  });

  it('serial fallback (no native sub-agents): one independent pass per lens', async () => {
    vi.mocked(providerSupportsNativeSubagents).mockReturnValue(false);

    await runHandler();

    // Four lenses → four independent runClaudeCode passes, in sequence.
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(PANEL_REVIEW_LENSES.length);

    // Each launched prompt is scoped to exactly ONE lens: it contains that
    // lens's unique focus string and its lens-specific output path, and does
    // NOT contain the other lenses' focus strings.
    PANEL_REVIEW_LENSES.forEach((lens, index) => {
      const prompt = vi.mocked(runClaudeCode).mock.calls[index]?.[0] ?? '';
      expect(prompt).toContain(lens.focus);
      expect(prompt).toContain(`/tmp/panel_review_lens_${lens.id}_task-panel-1.json`);
      for (const other of PANEL_REVIEW_LENSES) {
        if (other.id !== lens.id) expect(prompt).not.toContain(other.focus);
      }
    });

    // Each lens reads its own output file.
    for (const lens of PANEL_REVIEW_LENSES) {
      expectCalledWith(
        readHandlerOutput,
        'task-panel-1',
        `panel_review_lens_${lens.id}`,
        null,
      );
    }
  });

  it('synthesizes one aggregated verdict that still passes the gate shape', async () => {
    await runHandler();

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
    expect(call.changed_files).toEqual([
      'agent/src/handlers/panel-review.ts',
      'admin-ui/src/components/TaskDetailDrawerMeta.tsx',
    ]);

    const logs = call.logs;
    expect(typeof logs).toBe('string');
    const parsed = JSON.parse(logs as string) as Record<string, unknown>;
    // overall REQUEST_CHANGES because the correctness lens requested changes.
    expect(parsed.overall_verdict).toBe('REQUEST_CHANGES');
    // All four lens keys present in the aggregated object.
    expect(Object.keys(parsed.lenses as Record<string, unknown>).sort()).toEqual(
      ['code_quality', 'correctness', 'security', 'test_coverage'],
    );
    // Each lens carries a non-empty summary (the backend gate requires this).
    for (const lens of PANEL_REVIEW_LENSES) {
      const lensVerdict = (parsed.lenses as Record<string, { summary: string }>)[lens.id];
      expect(lensVerdict.summary.length).toBeGreaterThan(0);
    }
    // The correctness lens's issue is carried into the aggregated issue list.
    expect(parsed.issues).toEqual([
      expect.objectContaining({
        description: 'Caller still expects the old payload shape.',
        lens: 'correctness',
      }),
    ]);
    expect(parsed.investigated_files).toEqual([
      'agent/src/handlers/panel-review.ts',
      'admin-ui/src/components/TaskDetailDrawerMeta.tsx',
    ]);
  });

  it('parallel path: fails closed when any lens produces no usable verdict', async () => {
    // security lens returns null → repair is attempted, then fail closed. Under the
    // parallel path all lenses launch from ONE orchestrating pass; after repair the
    // panel still fails closed because the security lens verdict remains unusable.
    vi.mocked(readHandlerOutput).mockImplementation((_taskId: string, outputType: string) => {
      if (outputType === 'panel_review_lens_correctness') {
        return { verdict: 'APPROVE', summary: 'Correctness is fine.', issues: [] };
      }
      return null;
    });

    await runHandler();

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.artifact_type).toBe('ERROR_LOG');
    expect(call.failure_category).toBe('panel_review_lens_failed');
    // 1 orchestrating pass + 1 repair reopen for the first failing lens (security).
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(2);
  });

  it('serial fallback: stops at the first unusable lens', async () => {
    vi.mocked(providerSupportsNativeSubagents).mockReturnValue(false);
    vi.mocked(readHandlerOutput).mockImplementation((_taskId: string, outputType: string) => {
      if (outputType === 'panel_review_lens_correctness') {
        return { verdict: 'APPROVE', summary: 'Correctness is fine.', issues: [] };
      }
      return null;
    });

    await runHandler();

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.artifact_type).toBe('ERROR_LOG');
    expect(call.failure_category).toBe('panel_review_lens_failed');
    // Serial fan-out: correctness pass + security pass + security repair reopen = 3.
    // Repair is attempted before failing closed, but security readHandlerOutput
    // still returns null after repair so the panel fails at the security lens.
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(3);
  });

  it('parallel path: fails closed when the orchestrating pass exits non-zero', async () => {
    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 1,
      allOutput: 'crash',
      lastResultText: '',
    });

    await runHandler();

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.failure_category).toBe('panel_review_lens_failed');
    // Fails before reading any lens file — one pass, no synthesis.
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(1);
  });

  it('serial fallback: fails closed when a lens pass exits non-zero', async () => {
    vi.mocked(providerSupportsNativeSubagents).mockReturnValue(false);
    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 1,
      allOutput: 'crash',
      lastResultText: '',
    });

    await runHandler();

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.failure_category).toBe('panel_review_lens_failed');
  });

  it('serial fallback: malformed lens verdict triggers repair and records format miss telemetry', async () => {
    vi.mocked(providerSupportsNativeSubagents).mockReturnValue(false);

    // Security lens: runClaudeCode returns non-JSON text so repairStructuredOutput
    // has a source to try (non-empty allOutput → tryExtract records no_json_start
    // → recordFormatMiss is called). On the repair reopen, the file is valid.
    let runClaudeCodeCallCount = 0;
    vi.mocked(runClaudeCode).mockImplementation(() => {
      runClaudeCodeCallCount++;
      // Second call is the security lens pass — return non-JSON text so the
      // extraction layer records a format miss before reopening.
      const allOutput = runClaudeCodeCallCount === 2 ? 'lens review complete (no json)' : '';
      return Promise.resolve({ exitCode: 0, allOutput, lastResultText: '' });
    });

    // Security lens: first readHandlerOutput returns null; after the repair reopen
    // (third runClaudeCode call), return a valid verdict so the panel completes.
    let securityReadCount = 0;
    vi.mocked(readHandlerOutput).mockImplementation((_taskId: string, outputType: string) => {
      if (outputType === 'panel_review_lens_correctness') {
        return { verdict: 'APPROVE', summary: 'Correctness is fine.', issues: [] };
      }
      if (outputType === 'panel_review_lens_security') {
        securityReadCount++;
        if (securityReadCount === 1) return null; // first pass: unusable verdict
        return { verdict: 'APPROVE', summary: 'No security regressions after repair.', issues: [] };
      }
      if (outputType === 'panel_review_lens_code_quality') {
        return { verdict: 'APPROVE', summary: 'Code quality fine.', issues: [] };
      }
      if (outputType === 'panel_review_lens_test_coverage') {
        return { verdict: 'APPROVE', summary: 'Test coverage fine.', issues: [] };
      }
      return null;
    });

    await runHandler();

    // Panel should complete successfully (verdict recovered via repair).
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');

    // repairStructuredOutput records a format miss when text extraction fails
    // (no_json_start for non-JSON allOutput). Verifies the telemetry is not swallowed.
    expect(vi.mocked(recordFormatMiss)).toHaveBeenCalled();

    // Serial path: 4 lens passes + 1 repair reopen for the security lens = 5 total.
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(PANEL_REVIEW_LENSES.length + 1);
  });

  it('parallel path: malformed lens verdict triggers repair and records format miss telemetry', async () => {
    // providerSupportsNativeSubagents returns true (default) — parallel path.
    // First runClaudeCode call: orchestrating pass returns non-JSON allOutput so
    // repairStructuredOutput records a format miss (no_json_start) when the security
    // lens is null. Second call: repair reopen returns success exit.
    let runClaudeCodeCallCount = 0;
    vi.mocked(runClaudeCode).mockImplementation(() => {
      runClaudeCodeCallCount++;
      // First call is the orchestrating pass — return non-JSON text so the extraction
      // layer records a format miss when the security lens verdict is missing.
      const allOutput = runClaudeCodeCallCount === 1 ? 'orchestrating review complete (no json)' : '';
      return Promise.resolve({ exitCode: 0, allOutput, lastResultText: '' });
    });

    // Security lens: first readHandlerOutput returns null; after the repair reopen
    // (second runClaudeCode call), return a valid verdict so the panel completes.
    let securityReadCount = 0;
    vi.mocked(readHandlerOutput).mockImplementation((_taskId: string, outputType: string) => {
      if (outputType === 'panel_review_lens_correctness') {
        return { verdict: 'APPROVE', summary: 'Correctness is fine.', issues: [] };
      }
      if (outputType === 'panel_review_lens_security') {
        securityReadCount++;
        if (securityReadCount === 1) return null; // first read: unusable verdict
        return { verdict: 'APPROVE', summary: 'No security regressions after repair.', issues: [] };
      }
      if (outputType === 'panel_review_lens_code_quality') {
        return { verdict: 'APPROVE', summary: 'Code quality fine.', issues: [] };
      }
      if (outputType === 'panel_review_lens_test_coverage') {
        return { verdict: 'APPROVE', summary: 'Test coverage fine.', issues: [] };
      }
      return null;
    });

    await runHandler();

    // Panel should complete successfully (verdict recovered via repair).
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');

    // repairStructuredOutput records a format miss when text extraction fails
    // (no_json_start for non-JSON allOutput). Verifies the telemetry is not swallowed.
    expect(vi.mocked(recordFormatMiss)).toHaveBeenCalled();

    // Parallel path: 1 orchestrating pass + 1 repair reopen for the security lens = 2 total.
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(2);
  });

  it('serial fallback: a lens pass crashing on dead provider auth rethrows AuthFailureError, never failing the panel closed', async () => {
    // A dead Codex OAuth account (refresh_token_reused) made a lens pass exit
    // non-zero with an auth-failure signature. That reviewer is UNAVAILABLE
    // (infra), not a review verdict — it must route to the retryable
    // AuthFailureError path (account disabled + task requeued), NOT record a
    // terminal panel_review_lens_failed that strands a mergeable gated PR.
    vi.mocked(providerSupportsNativeSubagents).mockReturnValue(false);
    vi.mocked(matchesAuthFailure).mockReturnValue(true);
    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 1,
      allOutput: 'refresh token already used, please sign in again',
      lastResultText: '',
    });

    await expect(runHandler()).rejects.toBeInstanceOf(AuthFailureError);
    // Must NOT have failed the panel closed — the gate stays open for a requeue.
    expect(vi.mocked(persistAndPostStatus)).not.toHaveBeenCalled();
  });

  describe('challenge mode', () => {
    const PRIMARY_VERDICT = 'REQUEST_CHANGES';
    const PRIMARY_ISSUES = [
      { severity: 'critical', description: 'Missing null check on user input', file: 'src/auth.ts', line: 42 },
      { severity: 'major', description: 'Unhandled promise rejection in handler', file: 'src/handler.ts', line: 88 },
    ];

    // Restore the full readHandlerOutput implementation before each challenge test,
    // because a prior test (serial fallback: stops at first unusable lens) overrides
    // it to return null for all lenses except correctness, and clearAllMocks() does
    // not reset mock implementations — only call tracking.
    beforeEach(() => {
      vi.mocked(readHandlerOutput).mockImplementation((_taskId: string, outputType: string) => {
        if (outputType === 'panel_review_lens_correctness') {
          return { verdict: 'REQUEST_CHANGES', summary: 'A caller compatibility issue remains.', issues: [{ severity: 'major', description: 'Caller still expects the old payload shape.', file: 'agent/src/handlers/panel-review.ts', line: 120 }] };
        }
        if (outputType === 'panel_review_lens_security') {
          return { verdict: 'APPROVE', summary: 'No security regressions were found.', issues: [] };
        }
        if (outputType === 'panel_review_lens_code_quality') {
          return { verdict: 'APPROVE', summary: 'The workflow stays inside the existing review infrastructure.', issues: [] };
        }
        if (outputType === 'panel_review_lens_test_coverage') {
          return { verdict: 'APPROVE', summary: 'Targeted tests cover the new workflow and UI rendering.', issues: [] };
        }
        return null;
      });
    });

    async function runChallengeHandler() {
      await handlePanelReview(
        'task-challenge-1',
        {
          ...PAYLOAD,
          is_challenge: true,
          primary_verdict: PRIMARY_VERDICT,
          primary_issues: PRIMARY_ISSUES,
        },
        { project_metadata: {}, playbook: {}, context: {} },
        'gh-token',
        'owner',
        'repo',
        null,
        { value: '' },
        'gpt-5',
        'medium',
      );
    }

    it('challenge run references the primary verdict and issues', async () => {
      await runChallengeHandler();

      // One orchestrating pass launched for the parallel path.
      expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(1);

      const prompt = vi.mocked(runClaudeCode).mock.calls[0]?.[0] ?? '';

      // The primary verdict must appear in the per-lens body blocks.
      expect(prompt).toContain(PRIMARY_VERDICT);

      // Each primary issue description must appear so the lens can confirm or dispute.
      for (const issue of PRIMARY_ISSUES) {
        expect(prompt).toContain(issue.description);
      }

      // The challenge-mode section header must be present.
      expect(prompt).toContain('Challenge Mode: Second-Opinion Pass');

      // The confirm/dispute instruction must be present.
      expect(prompt).toContain('CONFIRM');
      expect(prompt).toContain('DISPUTE');
    });

    it('challenge mode: serial fallback includes primary verdict and issues in each per-lens prompt', async () => {
      vi.mocked(providerSupportsNativeSubagents).mockReturnValue(false);

      await runChallengeHandler();

      // Each of the four individual lens prompts should contain the primary verdict.
      expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(PANEL_REVIEW_LENSES.length);
      for (let i = 0; i < PANEL_REVIEW_LENSES.length; i++) {
        const prompt = vi.mocked(runClaudeCode).mock.calls[i]?.[0] ?? '';
        expect(prompt).toContain(PRIMARY_VERDICT);
        expect(prompt).toContain('Missing null check on user input');
        expect(prompt).toContain('Challenge Mode: Second-Opinion Pass');
      }
    });

    it('challenge mode absent: lens prompt has no challenge section (backward compatible)', async () => {
      await runHandler();

      const prompt = vi.mocked(runClaudeCode).mock.calls[0]?.[0] ?? '';
      expect(prompt).not.toContain('Challenge Mode');
      expect(prompt).not.toContain('INDEPENDENT CHALLENGER');
      // Standard lens content still present.
      for (const lens of PANEL_REVIEW_LENSES) {
        expect(prompt).toContain(lens.focus);
      }
    });

    it('challenge mode: synthesized verdict still emits the required backend shape', async () => {
      await runChallengeHandler();

      const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
      expect(call.status).toBe('COMPLETED');

      const parsed = JSON.parse(call.logs as string) as Record<string, unknown>;
      // Shape required by the backend reconciler must be unchanged.
      expect(parsed).toHaveProperty('overall_verdict');
      expect(parsed).toHaveProperty('summary');
      expect(parsed).toHaveProperty('issues');
      expect(parsed).toHaveProperty('lenses');
      expect(Object.keys(parsed.lenses as Record<string, unknown>).sort()).toEqual(
        ['code_quality', 'correctness', 'security', 'test_coverage'],
      );
    });
  });
});

// ── Prompt size telemetry — panel review records prompt size before runClaudeCode ─
describe('handlePanelReview — prompt size telemetry', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(execFileSync).mockReturnValue('abc123\trefs/heads/feature/panel-review\n');
    vi.mocked(providerSupportsNativeSubagents).mockReturnValue(true);
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
    vi.mocked(matchesAuthFailure).mockReturnValue(false);
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ title: 'Panel review PR', body: 'Review this carefully.' }),
    });
    resetLlmTelemetry();
  });

  it('parallel path records prompt size telemetry before calling runClaudeCode', async () => {
    await handlePanelReview(
      'task-panel-1',
      { ...PAYLOAD },
      { project_metadata: {}, playbook: {}, context: {} },
      'gh-token', 'owner', 'repo', null, { value: '' }, 'gpt-5', 'medium',
    );

    const telemetry = getLlmTelemetry();
    expect(telemetry.prompt_size_bytes).toBeGreaterThan(0);
    expect(telemetry.prompt_section_count).toBeGreaterThan(0);
    expect(telemetry.prompt_section_sizes).not.toBeNull();
    expect((telemetry.prompt_section_sizes ?? []).length).toBeGreaterThan(0);
    for (const s of (telemetry.prompt_section_sizes ?? [])) {
      expect(s.bytes).toBeGreaterThanOrEqual(0);
      expect(typeof s.name).toBe('string');
    }
    // At least the core_prompt section must be non-empty.
    const nonEmpty = (telemetry.prompt_section_sizes ?? []).filter(s => s.bytes > 0);
    expect(nonEmpty.length).toBeGreaterThan(0);
  });

  it('serial fallback path records prompt size telemetry before calling runClaudeCode', async () => {
    vi.mocked(providerSupportsNativeSubagents).mockReturnValue(false);

    await handlePanelReview(
      'task-panel-1',
      { ...PAYLOAD },
      { project_metadata: {}, playbook: {}, context: {} },
      'gh-token', 'owner', 'repo', null, { value: '' }, 'gpt-5', 'medium',
    );

    const telemetry = getLlmTelemetry();
    // The serial path calls runClaudeCode per lens; the last lens call's
    // telemetry should be stored.
    expect(telemetry.prompt_size_bytes).toBeGreaterThan(0);
    expect(telemetry.prompt_section_count).toBeGreaterThan(0);
    expect(telemetry.prompt_section_sizes).not.toBeNull();
    expect((telemetry.prompt_section_sizes ?? []).length).toBeGreaterThan(0);
    for (const s of (telemetry.prompt_section_sizes ?? [])) {
      expect(s.bytes).toBeGreaterThanOrEqual(0);
      expect(typeof s.name).toBe('string');
    }
    // At least the core_prompt section must be non-empty.
    const nonEmpty = (telemetry.prompt_section_sizes ?? []).filter(s => s.bytes > 0);
    expect(nonEmpty.length).toBeGreaterThan(0);
  });
});

describe('synthesizeBlindReviewVerdict', () => {
  it('returns APPROVE when 2/3 reviewers approve (majority threshold)', () => {
    const result = synthesizeBlindReviewVerdict(
      {
        blind_reviewer_1: { verdict: 'APPROVE', summary: 'LGTM', issues: [] },
        blind_reviewer_2: { verdict: 'APPROVE', summary: 'Looks good', issues: [] },
        blind_reviewer_3: { verdict: 'REQUEST_CHANGES', summary: 'Minor nit', issues: [{ description: 'Fix the comment', severity: 'minor', file: 'a.ts', line: 5 }] },
      },
      ['a.ts', 'b.ts'],
    );
    expect(result.overall_verdict).toBe('APPROVE');
    expect(result.investigated_files).toEqual(['a.ts', 'b.ts']);
    expect(result.summary).toContain('Reviewer 1 [APPROVE]');
    expect(result.summary).toContain('Reviewer 2 [APPROVE]');
    expect(result.summary).toContain('Reviewer 3 [REQUEST_CHANGES]');
    expect(result.summary).toContain('2/3');
    expect(result.issues).toHaveLength(1);
    expect(result.issues[0].description).toBe('Fix the comment');
  });

  it('returns REQUEST_CHANGES when only 1/3 approve (below majority threshold)', () => {

    const result = synthesizeBlindReviewVerdict(
      {
        blind_reviewer_1: { verdict: 'APPROVE', summary: 'Looks OK', issues: [] },
        blind_reviewer_2: { verdict: 'REQUEST_CHANGES', summary: 'Bug found', issues: [{ description: 'Off-by-one error', severity: 'major', file: 'b.ts', line: 42 }] },
        blind_reviewer_3: { verdict: 'REQUEST_CHANGES', summary: 'Needs rework', issues: [{ description: 'Missing error handling', severity: 'major', file: 'c.ts', line: 10 }] },
      },
      ['b.ts', 'c.ts'],
    );
    expect(result.overall_verdict).toBe('REQUEST_CHANGES');
    expect(result.issues).toHaveLength(2);
  });

  it('handles empty reviewer set gracefully', () => {

    const result = synthesizeBlindReviewVerdict({}, []);
    // With 0 reviewers: ceil(0 * 2/3) = 0 threshold, 0 >= 0 → APPROVE (vacuous majority).
    expect(result.overall_verdict).toBe('APPROVE');
    expect(result.issues).toEqual([]);
    expect(result.investigated_files).toEqual([]);
  });

  it('returns APPROVE when 1/1 reviewer approves', () => {

    const result = synthesizeBlindReviewVerdict(
      {
        blind_reviewer_1: { verdict: 'APPROVE', summary: 'Ship it', issues: [] },
      },
      ['a.ts'],
    );
    expect(result.overall_verdict).toBe('APPROVE');
    expect(result.summary).toContain('1/1');
  });

  it('deduplicates issues with same description across reviewers', () => {

    const result = synthesizeBlindReviewVerdict(
      {
        blind_reviewer_1: { verdict: 'REQUEST_CHANGES', summary: 'Bug', issues: [{ description: 'Null pointer risk', severity: 'critical', file: 'd.ts', line: 1 }] },
        blind_reviewer_2: { verdict: 'REQUEST_CHANGES', summary: 'Same bug', issues: [{ description: 'Null pointer risk', severity: 'critical', file: 'd.ts', line: 1 }] },
      },
      ['d.ts'],
    );
    expect(result.issues).toHaveLength(1);
    expect(result.issues[0].description).toBe('Null pointer risk');
  });

  it('caps investigated_files at 50', () => {
    const manyFiles = Array.from({ length: 100 }, (_, i) => `file-${i}.ts`);
    const result = synthesizeBlindReviewVerdict(
      {
        blind_reviewer_1: { verdict: 'APPROVE', summary: 'ok', issues: [] },
      },
      manyFiles,
    );
    expect(result.investigated_files).toHaveLength(50);
  });
});
