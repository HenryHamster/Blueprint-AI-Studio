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
  sendHeartbeatSync: vi.fn(),
  setCurrentPromptFingerprint: vi.fn(),
  snapshotToolTrace: vi.fn().mockReturnValue([]),
  extractInvestigatedFiles: vi.fn().mockReturnValue([
    'agent/src/handlers/panel-review.ts',
    'admin-ui/src/components/TaskDetailDrawerMeta.tsx',
  ]),
  resolveBaseBranch: vi.fn((payload: Record<string, unknown>) => payload.base_branch || 'main'),
  readHandlerOutput: vi.fn().mockReturnValue({
    overall_verdict: 'REQUEST_CHANGES',
    summary: 'Two blocking findings across the fixed review lenses.',
    lenses: {
      correctness: {
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
      },
      security: {
        verdict: 'APPROVE',
        summary: 'No security regressions were found.',
        issues: [],
      },
      code_quality: {
        verdict: 'APPROVE',
        summary: 'The workflow stays inside the existing review infrastructure.',
        issues: [],
      },
      test_coverage: {
        verdict: 'APPROVE',
        summary: 'Targeted tests cover the new workflow and UI rendering.',
        issues: [],
      },
    },
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
import { persistAndPostStatus, readHandlerOutput, runClaudeCode } from '../index.js';
import { expectCalledWith } from '../utils/typed-mock-call.js';
import { handlePanelReview, PANEL_REVIEW_LENSES } from './panel-review.js';

describe('handlePanelReview', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(execFileSync).mockReturnValue('abc123\trefs/heads/feature/panel-review\n' as never);
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

  it('runs the fixed-lens prompt and posts the structured verdict payload', async () => {
    await handlePanelReview(
      'task-panel-1',
      {
        pr_number: 42,
        branch: 'feature/panel-review',
        base_branch: 'automain',
        original_prompt: 'Run the multi-lens panel review.',
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

    const launchedPrompt = vi.mocked(runClaudeCode).mock.calls[0]?.[0] ?? '';
    expect(launchedPrompt).toContain('/tmp/panel_review_verdict_task-panel-1.json');
    expect(launchedPrompt).toContain('correctness');
    expect(launchedPrompt).toContain('security');
    expect(launchedPrompt).toContain('code_quality');
    expect(launchedPrompt).toContain('test_coverage');

    expectCalledWith(
      readHandlerOutput,
      'task-panel-1',
      'panel_review_verdict',
      null,
    );

    expectCalledWith(
      persistAndPostStatus,
      'task-panel-1',
      expect.objectContaining({
        task_id: 'task-panel-1',
        status: 'COMPLETED',
        changed_files: [
          'agent/src/handlers/panel-review.ts',
          'admin-ui/src/components/TaskDetailDrawerMeta.tsx',
        ],
      }),
    );

    const logs = vi.mocked(persistAndPostStatus).mock.calls[0]?.[1]?.logs;
    expect(typeof logs).toBe('string');
    const parsed = JSON.parse(logs as string) as Record<string, unknown>;
    expect(parsed.overall_verdict).toBe('REQUEST_CHANGES');
    expect(parsed.investigated_files).toEqual([
      'agent/src/handlers/panel-review.ts',
      'admin-ui/src/components/TaskDetailDrawerMeta.tsx',
    ]);
  });
});
