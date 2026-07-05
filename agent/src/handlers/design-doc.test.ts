import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

// ── Module mocks ──────────────────────────────────────────────────────────────
vi.mock('../index.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../index.js')>();
  return {
    ...actual,
    WORKSPACE: '/tmp/workspace',
    sendLog: vi.fn(),
    apiGet: vi.fn(),
    persistAndPostStatus: vi.fn().mockResolvedValue(undefined),
    validateBranchName: vi.fn((b: string, _label: string) => b),
    runClaudeCode: vi.fn().mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' }),
    cloneWithRetry: vi.fn().mockResolvedValue(undefined),
  };
});

vi.mock('../workspacePrep.js', () => ({
  prepareWorkspaceForRole: vi.fn().mockResolvedValue(undefined),
}));

vi.mock('../prompt.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../prompt.js')>();
  return {
    ...actual,
    readClaudeMd: vi.fn().mockReturnValue(''),
    buildContextPrefix: vi.fn().mockReturnValue(''),
    buildFileTree: vi.fn().mockReturnValue(''),
    configureAgentPermissions: vi.fn().mockReturnValue([]),
    writePlaybookHelper: vi.fn(),
    writeMessagesHelper: vi.fn(),
    excludeAgentHelpers: vi.fn(),
    buildRetryContext: vi.fn().mockReturnValue(''),
  };
});

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
  execFile: vi.fn(),
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
const PROJECT_ID = 'proj-1234';
const BASE_TASK = {
  title: 'Test Task', description: '', context: {}, project_context: {},
  project_metadata: { project_id: PROJECT_ID },
};
const PROJECT_NAME = 'TestProject';
const BASE_PAYLOAD = { base_branch: 'main', project_name: PROJECT_NAME };

describe('handleDesignDocUpdate', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
    vi.mocked(apiGet).mockResolvedValue({ design_doc: { summary: 'x' } });
  });

  it('writes the ./design-doc helper script to WORKSPACE with mode 0o755', async () => {
    await handleDesignDocUpdate(
      TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );

    const helperCall = vi.mocked(fs.writeFileSync).mock.calls.find(
      c => typeof c[0] === 'string' && c[0].endsWith('/design-doc'),
    );
    if (!helperCall) throw new Error('design-doc helper script was not written');
    const content = helperCall[1] as string;
    expect(content.startsWith('#!/bin/bash')).toBe(true);
    expect(content).toContain('design-doc get|set|delete|list');
    // set/delete are durable under 429/5xx contention via a bounded retry loop.
    expect(content).toContain('_post_with_retry');
    expect(content).toContain('"$http_code" = "429"');
    expect(helperCall[2]).toEqual({ mode: 0o755 });
  });

  it('writes the ./fetch helper and advertises design-doc fetch guidance when project_id exists', async () => {
    await handleDesignDocUpdate(
      TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );

    const fetchCall = vi.mocked(fs.writeFileSync).mock.calls.find(
      c => typeof c[0] === 'string' && c[0] === '/tmp/workspace/fetch',
    );
    if (!fetchCall) throw new Error('fetch helper script was not written');
    const content = fetchCall[1] as string;
    expect(content).toContain('Usage: fetch direction|playbook|gap|message|section|knowledge [id]');
    expect(content).toContain('messages/${ID:-open}');
    expect(content).toContain('design-doc/$ID');

    const prompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(prompt).toContain('MID-RUN FETCH (./fetch)');
    expect(prompt).toContain('`./fetch section <key>`');
    expect(prompt).toContain('`./fetch message [ref]`');
  });

  it('reports COMPLETED when runClaudeCode exits 0 and project has design_doc pages', async () => {
    vi.mocked(apiGet).mockResolvedValue({
      design_doc: { summary: '"proj"', 'systems/sys-auth': {} },
    });

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
    vi.mocked(apiGet).mockResolvedValue({ design_doc: {} });

    await handleDesignDocUpdate(
      TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );

    expect(runClaudeCode).toHaveBeenCalledTimes(2);
    const retryPrompt = vi.mocked(runClaudeCode).mock.calls[1][0];
    expect(retryPrompt).toContain('You MUST call ./design-doc set');
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
  });

  it('fetches design_doc from the project detail endpoint, not the project list', async () => {
    vi.mocked(apiGet).mockResolvedValue({ design_doc: { summary: 'x' } });

    await handleDesignDocUpdate(
      TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );

    expect(apiGet).toHaveBeenCalledWith(`/api/admin/projects/${PROJECT_ID}`);
  });

  it('skips the post-check (no spurious retry) when project_metadata has no project_id', async () => {
    vi.mocked(apiGet).mockResolvedValue({ design_doc: {} });
    const taskWithoutProjectId = { title: 'Test Task', description: '', context: {} };

    await handleDesignDocUpdate(
      TASK_ID, BASE_PAYLOAD, taskWithoutProjectId, GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );

    // No project_id -> post-check is skipped entirely: no detail fetch, no retry.
    expect(apiGet).not.toHaveBeenCalled();
    expect(runClaudeCode).toHaveBeenCalledTimes(1);
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
  });

  it('omits ./fetch provisioning and guidance when project_metadata has no project_id', async () => {
    vi.mocked(apiGet).mockResolvedValue({ design_doc: {} });
    const taskWithoutProjectId = { title: 'Test Task', description: '', context: {} };

    await handleDesignDocUpdate(
      TASK_ID, BASE_PAYLOAD, taskWithoutProjectId, GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );

    const fetchCall = vi.mocked(fs.writeFileSync).mock.calls.find(
      c => typeof c[0] === 'string' && c[0] === '/tmp/workspace/fetch',
    );
    expect(fetchCall).toBeUndefined();
    const prompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(prompt).not.toContain('MID-RUN FETCH (./fetch)');
    expect(prompt).not.toContain('`./fetch section <key>`');
  });

  it('reports FAILED with failure_category "claude_code_failed" when runClaudeCode exits non-zero', async () => {
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 1, allOutput: 'err', lastResultText: '' });
    vi.mocked(apiGet).mockResolvedValue({ design_doc: {} });

    await handleDesignDocUpdate(
      TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );

    // Non-zero exit skips the retry (retry only triggers when exitCode === 0)
    expect(runClaudeCode).toHaveBeenCalledTimes(1);
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.failure_category).toBe('claude_code_failed');
  });

  // ── Structural invariants (design_doc_update = research/planning class) ──────
  // These pin the guidance blocks present in the prompt so a future PR that
  // accidentally drops one causes a test failure before merging.

  async function capturePrompt(): Promise<string> {
    await handleDesignDocUpdate(
      TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );
    return vi.mocked(runClaudeCode).mock.calls[0][0];
  }

  it('includes DEFAULT_TOOL_GUIDANCE (structural invariant: design_doc_update class)', async () => {
    const prompt = await capturePrompt();
    expect(prompt).toContain('Use Grep/Glob for code search');
  });

  it('includes PLAYBOOK_GUIDANCE (structural invariant: design_doc_update class)', async () => {
    const prompt = await capturePrompt();
    expect(prompt).toContain('./playbook set');
  });

  it('does not include CODE_CHANGE_HELPER_GUIDANCE (design_doc_update must not push code)', async () => {
    const prompt = await capturePrompt();
    expect(prompt).not.toContain('Task lifecycle — you own it end-to-end');
  });

  it('contains task-type fingerprint: Do NOT create files in the repo', async () => {
    const prompt = await capturePrompt();
    expect(prompt).toContain('Do NOT create files in the repo');
  });

  it('contains YOUR ONLY OUTPUT MECHANISM section header (load-bearing contract)', async () => {
    const prompt = await capturePrompt();
    expect(prompt).toContain('YOUR ONLY OUTPUT MECHANISM');
  });

  it('contains ./design-doc set invocation pattern in the prompt body', async () => {
    const prompt = await capturePrompt();
    expect(prompt).toContain('./design-doc set');
  });

  it('contains Current pages section for existing-state context', async () => {
    const prompt = await capturePrompt();
    expect(prompt).toContain('Current pages');
  });

  // ── design_doc index shape rendering (backend ships { sections: [...] }) ─────
  // The backend converted the inline design_doc to a compact navigation index.
  // The handler must render the index's `sections` array as the Current pages
  // list — surfacing the real section keys — not treat the whole payload as a
  // flat map (which would yield one bogus page literally named `sections`).

  async function capturePromptWith(payload: Record<string, unknown>): Promise<string> {
    await handleDesignDocUpdate(
      TASK_ID, { ...BASE_PAYLOAD, ...payload }, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );
    return vi.mocked(runClaudeCode).mock.calls[0][0];
  }

  it('renders index sections as Current pages (real keys, not a bogus `sections` page)', async () => {
    const prompt = await capturePromptWith({
      design_doc: {
        sections: [
          { key: 'systems/sys-auth', status: 'functional', summary: 'Auth subsystem', modules: ['backend/app/auth.py'] },
          { key: 'capabilities/testing', summary: 'Unit tests only', presence_warning: 'no integration tests' },
          { key: 'notes/observations', summary: 'misc notes' },
        ],
      },
    });
    expect(prompt).toContain('systems/sys-auth');
    expect(prompt).toContain('capabilities/testing');
    expect(prompt).toContain('notes/observations');
    expect(prompt).toContain('status: functional');
    expect(prompt).toContain('⚠ no integration tests');
    // 3 real sections, not a single page named `sections`
    expect(prompt).toContain('3 pages exist');
    expect(prompt).not.toMatch(/^\s*sections:/m);
  });

  it('advertises `./design-doc get` so the curator can read full prose before editing', async () => {
    const prompt = await capturePromptWith({
      design_doc: { sections: [{ key: 'systems/sys-auth', summary: 'Auth' }] },
    });
    expect(prompt).toContain('./design-doc get');
  });

  it('falls back to flat key->value rendering when design_doc is a legacy map', async () => {
    const prompt = await capturePromptWith({
      design_doc: { summary: '"proj"', 'systems/sys-auth': { status: 'functional' } },
    });
    expect(prompt).toContain('systems/sys-auth');
    expect(prompt).toContain('2 pages exist');
  });
});

describe('handleDesignDocUpdate prompt determinism', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-01-01T00:00:00Z'));
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
    vi.mocked(apiGet).mockResolvedValue({ design_doc: { summary: 'x' } });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('produces identical prompts for identical inputs (no Date.now/Math.random leaks)', async () => {
    const invoke = async () => {
      await handleDesignDocUpdate(
        TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
      );
      return vi.mocked(runClaudeCode).mock.calls[0][0];
    };
    const prompt1 = await invoke();
    vi.clearAllMocks();
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
    vi.mocked(apiGet).mockResolvedValue({ design_doc: { summary: 'x' } });
    const prompt2 = await invoke();
    expect(prompt1).toBe(prompt2);
  });
});
