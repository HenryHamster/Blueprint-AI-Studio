import { describe, it, expect, vi, beforeEach } from 'vitest';

// ── Module mocks ──────────────────────────────────────────────────────────────
vi.mock('../index.js', () => ({
  WORKSPACE: '/tmp/workspace',
  strVal: (v: unknown, fallback = '') => (typeof v === 'string' ? v : fallback),
  sendLog: vi.fn(),
  apiGet: vi.fn(),
  persistAndPostStatus: vi.fn().mockResolvedValue(undefined),
  validateBranchName: vi.fn((b: string, _label: string) => b),
  runClaudeCode: vi.fn().mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' }),
  cloneWithRetry: vi.fn().mockResolvedValue(undefined),
}));

vi.mock('../prompt.js', () => ({
  DEFAULT_TOOL_GUIDANCE: '',
  FIRST_TASK_GUIDANCE: '',
  readClaudeMd: vi.fn().mockReturnValue(''),
  buildContextPrefix: vi.fn().mockReturnValue(''),
  buildFileTree: vi.fn().mockReturnValue(''),
  configureAgentPermissions: vi.fn().mockReturnValue([]),
  writePlaybookHelper: vi.fn(),
  writeMessagesHelper: vi.fn(),
  excludeAgentHelpers: vi.fn(),
  buildRetryContext: vi.fn().mockReturnValue(''),
}));

vi.mock('fs', () => ({
  default: {
    readFileSync: vi.fn().mockReturnValue(''),
    writeFileSync: vi.fn(),
    existsSync: vi.fn().mockReturnValue(false),
    appendFileSync: vi.fn(),
  },
}));

vi.mock('child_process', () => ({
  execFileSync: vi.fn().mockReturnValue(Buffer.from('')),
}));

// ── Imports under test ────────────────────────────────────────────────────────
import { handleDesignDocUpdate } from './design-doc.js';
import { apiGet, persistAndPostStatus, runClaudeCode } from '../index.js';
import fs from 'fs';

// ── Shared test helpers ───────────────────────────────────────────────────────
const TASK_ID = 'task-abc123';
const GH_TOKEN = 'gh-token';
const OWNER = 'owner';
const REPO = 'repo';
const WS = null;
const STATUS_REF = () => ({ value: '' });
const BASE_TASK = { title: 'Test Task', description: '', context: {}, project_context: {} };
const PROJECT_NAME = 'TestProject';
const BASE_PAYLOAD = { base_branch: 'main', project_name: PROJECT_NAME };

describe('handleDesignDocUpdate', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
    vi.mocked(apiGet).mockResolvedValue([{ name: PROJECT_NAME, design_doc: { summary: 'x' } }]);
  });

  it('writes the ./design-doc helper script to WORKSPACE with mode 0o755', async () => {
    await handleDesignDocUpdate(
      TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );

    const helperCall = vi.mocked(fs.writeFileSync).mock.calls.find(
      c => typeof c[0] === 'string' && c[0] === '/tmp/workspace/design-doc',
    );
    if (!helperCall) throw new Error('design-doc helper script was not written');
    const content = helperCall[1] as string;
    expect(content.startsWith('#!/bin/bash')).toBe(true);
    expect(content).toContain('design-doc get|set|delete|list');
    expect(helperCall[2]).toEqual({ mode: 0o755 });
  });

  it('reports COMPLETED when runClaudeCode exits 0 and project has design_doc pages', async () => {
    vi.mocked(apiGet).mockResolvedValue([
      { name: PROJECT_NAME, design_doc: { summary: '"proj"', 'systems/sys-auth': {} } },
    ]);

    await handleDesignDocUpdate(
      TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );

    // Only one runClaudeCode call — no retry when pages exist
    expect(runClaudeCode).toHaveBeenCalledTimes(1);
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
    expect(call.failure_category).toBeUndefined();
  });

  it('triggers a retry with "You MUST call ./design-doc set" when exitCode 0 but zero pages saved', async () => {
    vi.mocked(apiGet).mockResolvedValue([{ name: PROJECT_NAME, design_doc: {} }]);

    await handleDesignDocUpdate(
      TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );

    expect(runClaudeCode).toHaveBeenCalledTimes(2);
    const retryPrompt = vi.mocked(runClaudeCode).mock.calls[1][0];
    expect(retryPrompt).toContain('You MUST call ./design-doc set');
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
  });

  it('reports FAILED with failure_category "claude_code_failed" when runClaudeCode exits non-zero', async () => {
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 1, allOutput: 'err', lastResultText: '' });
    vi.mocked(apiGet).mockResolvedValue([{ name: PROJECT_NAME, design_doc: {} }]);

    await handleDesignDocUpdate(
      TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );

    // Non-zero exit skips the retry (retry only triggers when exitCode === 0)
    expect(runClaudeCode).toHaveBeenCalledTimes(1);
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.failure_category).toBe('claude_code_failed');
  });
});
