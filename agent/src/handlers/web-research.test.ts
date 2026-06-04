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

// ── Imports under test ────────────────────────────────────────────────────────
import { handleWebResearch } from './web-research.js';
import { runClaudeCode, persistAndPostStatus, readHandlerOutput } from '../index.js';
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
