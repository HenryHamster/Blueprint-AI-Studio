import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../index.js', () => ({
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
  persistAndPostStatus,
  providerSupportsNativeSubagents,
  readHandlerOutput,
  runClaudeCode,
} from '../index.js';
import { handlePanelReview, PANEL_REVIEW_LENSES } from './panel-review.js';
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
    vi.mocked(execFileSync).mockReturnValue('abc123\trefs/heads/feature/panel-review\n' as never);
    // clearAllMocks clears call records but NOT implementations, so explicitly
    // restore the two mocks that individual tests mutate, keeping the default
    // path (native sub-agent / parallel) and a clean exit between tests.
    vi.mocked(providerSupportsNativeSubagents).mockReturnValue(true);
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ title: 'Panel review PR', body: 'Review this carefully.' }),
    }) as unknown as typeof fetch;
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
    // security lens returns null → fail closed, no silent APPROVE. Under the
    // parallel path all lenses launch from ONE pass; synthesis still fails
    // closed because one lens verdict is unusable.
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
    // Single orchestrating pass (no serial short-circuit in the parallel path).
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(1);
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
    // Serial fan-out short-circuits at the first unusable lens (correctness ok,
    // security null) — only two passes ran.
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(2);
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
});
