import { describe, it, expect, vi, beforeEach } from 'vitest';

// ── Module mocks ──────────────────────────────────────────────────────────────
vi.mock('../index.js', () => ({
  WORKSPACE: '/tmp/workspace',
  sendLog: vi.fn(),
  runClaudeCode: vi.fn().mockResolvedValue({
    exitCode: 0, allOutput: '', lastResultText: '',
  }),
  readHandlerOutput: vi.fn().mockReturnValue(null),
  persistAndPostStatus: vi.fn().mockResolvedValue(undefined),
  strVal: (v: unknown, def = '') => (typeof v === 'string' ? v : def),
  sanitize: (s: string) => s,
  setCurrentPromptFingerprint: vi.fn(),
  sendHeartbeatSync: vi.fn(),
  apiGet: vi.fn().mockResolvedValue({ documents: [] }),
}));

vi.mock('../prompt.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../prompt.js')>();
  return {
    ...actual,
    buildContextPrefix: vi.fn().mockReturnValue(''),
    buildRetryContext: vi.fn().mockReturnValue(''),
    readClaudeMd: vi.fn().mockReturnValue(''),
    configureAgentPermissions: vi.fn().mockReturnValue(['Write', 'Edit', 'NotebookEdit']),
  };
});

vi.mock('fs', () => ({
  default: {
    readFileSync: vi.fn().mockReturnValue(''),
    existsSync: vi.fn().mockReturnValue(false),
  },
}));

// Deterministic git provenance: captureGitProvenance shells out via execSync.
vi.mock('child_process', () => ({
  execSync: vi.fn((cmd: string) =>
    Buffer.from(cmd.includes('--short') ? 'abc1234\n' : 'feat/wasm-branch\n'),
  ),
}));

// ── Imports under test ────────────────────────────────────────────────────────
import { handleWebResearch } from './web-research.js';
import { runClaudeCode, persistAndPostStatus, readHandlerOutput, apiGet } from '../index.js';
import { configureAgentPermissions } from '../prompt.js';
import { expectCalledWith } from '../utils/typed-mock-call.js';

const TASK_ID = 'task-web-research-1';
const WS = null;
const STATUS_REF = () => ({ value: '' });
const BASE_TASK = { title: 'Web Research', description: 'Investigate something', context: {} };

const VALID_DOCUMENT = {
  title: 'State of WebAssembly in 2026',
  summary: 'WebAssembly has matured into a portable compilation target.',
  body: '## Overview\n\nWasm is widely supported...',
  source_citations: ['https://webassembly.org/'],
  tags: ['wasm', 'web'],
  topic: 'webassembly',
};

function lastPostedPayload(): Record<string, unknown> {
  const calls = vi.mocked(persistAndPostStatus).mock.calls;
  return calls[calls.length - 1][1];
}

describe('handleWebResearch', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 0, allOutput: '', lastResultText: '',
    } as Awaited<ReturnType<typeof runClaudeCode>>);
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    vi.mocked(apiGet).mockResolvedValue({ documents: [] });
  });

  it('builds a web-first prompt with WebSearch/WebFetch and the document key cues', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(VALID_DOCUMENT);

    await handleWebResearch(
      TASK_ID, { prompt: 'Research the state of WebAssembly', topic: 'webassembly' },
      BASE_TASK, 'tok', 'owner', 'repo', WS, STATUS_REF(),
    );

    const prompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(prompt).toContain('WebSearch');
    expect(prompt).toContain('WebFetch');
    // Document schema cues the backend completion handler parses.
    for (const key of ['title', 'summary', 'body', 'source_citations', 'topic']) {
      expect(prompt).toContain(`"${key}"`);
    }
    // Read-only: the agent must write via Bash because the Write tool is disabled.
    expect(prompt).toContain(`/tmp/web_research_document_${TASK_ID}.json`);
  });

  it('renders the operational playbook (with provenance caveats) into the prompt', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(VALID_DOCUMENT);

    const taskWithPlaybook = {
      ...BASE_TASK,
      playbook: {
        'search-tip': {
          content: 'Prefer primary docs over secondary commentary.',
          effectiveness_score: 0.9, confirmed_count: 2, age_days: 1, stale: false,
        },
        'stale-fact': {
          content: 'A fact recorded on a divergent branch.',
          effectiveness_score: 0.8, confirmed_count: 1, age_days: 3, stale: false,
          provenance: { branch: 'feature/x', head_sha: 'abc1234', diverged: true },
        },
      },
    };

    await handleWebResearch(
      TASK_ID, { prompt: 'Research the state of WebAssembly' },
      taskWithPlaybook, 'tok', 'owner', 'repo', WS, STATUS_REF(),
    );

    const prompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(prompt).toContain('## Operational Playbook (active collective memory)');
    expect(prompt).toContain('Prefer primary docs over secondary commentary.');
    // Provenance divergence caveat surfaces for the divergent-branch entry — the
    // non-clone web_research path benefits from seeing possibly-stale repo facts.
    expect(prompt).toContain('⚠ different branch than yours');
  });

  it('omits the playbook section when no playbook is shipped', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(VALID_DOCUMENT);

    await handleWebResearch(
      TASK_ID, { prompt: 'Research X' },
      BASE_TASK, 'tok', 'owner', 'repo', WS, STATUS_REF(),
    );

    const prompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(prompt).not.toContain('## Operational Playbook (active collective memory)');
  });

  it('reads the document from the /tmp file and completes with the document JSON in logs', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(VALID_DOCUMENT);

    await handleWebResearch(
      TASK_ID, { prompt: 'Research the state of WebAssembly' },
      BASE_TASK, 'tok', 'owner', 'repo', WS, STATUS_REF(),
    );

    expectCalledWith(readHandlerOutput, TASK_ID, 'web_research_document', WS);
    const payload = lastPostedPayload();
    expect(payload.status).toBe('COMPLETED');
    const parsed = JSON.parse(payload.logs as string) as typeof VALID_DOCUMENT;
    expect(parsed.title).toBe(VALID_DOCUMENT.title);
    expect(parsed.body).toBe(VALID_DOCUMENT.body);
    expect(parsed.source_citations).toEqual(VALID_DOCUMENT.source_citations);
    // No artifact_type on the success path — the backend completion handler is
    // the sole writer of the durable KnowledgeDocument.
    expect(payload.artifact_type).toBeUndefined();
  });

  it('falls back to parsing the document from output when the file is absent', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 0, allOutput: '',
      lastResultText: `Here is the document:\n${JSON.stringify(VALID_DOCUMENT)}`,
    } as Awaited<ReturnType<typeof runClaudeCode>>);

    await handleWebResearch(
      TASK_ID, { prompt: 'Research the state of WebAssembly' },
      BASE_TASK, 'tok', 'owner', 'repo', WS, STATUS_REF(),
    );

    const payload = lastPostedPayload();
    expect(payload.status).toBe('COMPLETED');
    const parsed = JSON.parse(payload.logs as string) as typeof VALID_DOCUMENT;
    expect(parsed.title).toBe(VALID_DOCUMENT.title);
  });

  it('embeds branch/HEAD provenance INSIDE the emitted document (not as a sibling field)', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(VALID_DOCUMENT);

    await handleWebResearch(
      TASK_ID, { prompt: 'Research the state of WebAssembly' },
      BASE_TASK, 'tok', 'owner', 'repo', WS, STATUS_REF(),
    );

    const payload = lastPostedPayload();
    // Provenance must live INSIDE the parsed document — the backend extracts it
    // from the parsed `logs` document, not from a top-level status payload field.
    const parsed = JSON.parse(payload.logs as string) as Record<string, unknown>;
    expect(parsed.authoring_branch).toBe('feat/wasm-branch');
    expect(parsed.head_sha).toBe('abc1234');
    // And NOT as a sibling of logs (where the backend never reads it).
    expect(payload.authoring_branch).toBeUndefined();
    expect(payload.head_sha).toBeUndefined();
  });

  it('installs the read-only permission posture for the web_research type', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(VALID_DOCUMENT);

    await handleWebResearch(
      TASK_ID, { prompt: 'Research X' },
      BASE_TASK, 'tok', 'owner', 'repo', WS, STATUS_REF(),
    );

    expectCalledWith(configureAgentPermissions, 'web_research');
    // The disallowed tools returned by configureAgentPermissions must reach
    // runClaudeCode as the read-only blocklist (Write/Edit/NotebookEdit).
    const disallowed = vi.mocked(runClaudeCode).mock.calls[0][5] as string[];
    expect(disallowed).toContain('Write');
    expect(disallowed).toContain('Edit');
  });

  it('injects a Prior Knowledge section with prior-doc titles/ids and the build-on/cite instruction', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(VALID_DOCUMENT);
    vi.mocked(apiGet).mockResolvedValue({
      documents: [
        { id: 'doc-aaa', title: 'Earlier WebAssembly survey', summary: 'A prior look at wasm.', topic: 'webassembly', tags: ['wasm'] },
        { id: 'doc-bbb', title: 'Wasm toolchains', summary: 'Compilers and runtimes.', topic: 'webassembly', tags: ['wasm'] },
      ],
    });

    await handleWebResearch(
      TASK_ID, { prompt: 'Research the state of WebAssembly', topic: 'webassembly' },
      BASE_TASK, 'tok', 'owner', 'repo', WS, STATUS_REF(),
    );

    // Query uses the topic (preferred over prompt) and hits the agent endpoint.
    const calledPath = vi.mocked(apiGet).mock.calls[0][0];
    expect(calledPath).toContain('/api/agent/knowledge-documents');
    expect(calledPath).toContain(`q=${encodeURIComponent('webassembly')}`);

    const prompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(prompt).toContain('## Prior Knowledge');
    // Each retrieved document's title, summary, and id appear.
    expect(prompt).toContain('doc-aaa');
    expect(prompt).toContain('Earlier WebAssembly survey');
    expect(prompt).toContain('A prior look at wasm.');
    expect(prompt).toContain('doc-bbb');
    expect(prompt).toContain('Wasm toolchains');
    // Explicit build-on / supersede / cite instruction.
    expect(prompt).toContain('supersede');
    expect(prompt).toContain('cite');
  });

  it('omits the Prior Knowledge section when the fetch returns no documents', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(VALID_DOCUMENT);
    vi.mocked(apiGet).mockResolvedValue({ documents: [] });

    await handleWebResearch(
      TASK_ID, { prompt: 'Research the state of WebAssembly', topic: 'webassembly' },
      BASE_TASK, 'tok', 'owner', 'repo', WS, STATUS_REF(),
    );

    const prompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(prompt).not.toContain('## Prior Knowledge');
    const payload = lastPostedPayload();
    expect(payload.status).toBe('COMPLETED');
  });

  it('completes normally (no Prior Knowledge, no failure) when the knowledge fetch throws', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(VALID_DOCUMENT);
    vi.mocked(apiGet).mockRejectedValue(new Error('knowledge endpoint unreachable'));

    await handleWebResearch(
      TASK_ID, { prompt: 'Research the state of WebAssembly', topic: 'webassembly' },
      BASE_TASK, 'tok', 'owner', 'repo', WS, STATUS_REF(),
    );

    const prompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(prompt).not.toContain('## Prior Knowledge');
    const payload = lastPostedPayload();
    expect(payload.status).toBe('COMPLETED');
    const parsed = JSON.parse(payload.logs as string) as typeof VALID_DOCUMENT;
    expect(parsed.title).toBe(VALID_DOCUMENT.title);
  });

  it('marks the task FAILED when the inner session exits non-zero', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 1, allOutput: 'boom', lastResultText: '',
    } as Awaited<ReturnType<typeof runClaudeCode>>);

    await handleWebResearch(
      TASK_ID, { prompt: 'Research X' },
      BASE_TASK, 'tok', 'owner', 'repo', WS, STATUS_REF(),
    );

    const payload = lastPostedPayload();
    expect(payload.status).toBe('FAILED');
    expect(payload.failure_category).toBe('claude_code_failed');
    expect(payload.artifact_type).toBe('ERROR_LOG');
  });
});
