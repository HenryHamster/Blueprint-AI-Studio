import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

// ── Module mocks ──────────────────────────────────────────────────────────────
vi.mock('../index.js', () => ({
  normalizeClaudeConfigContent: vi.fn((_path: string, content: string) => content),
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
    writePlaybookHelper: vi.fn(),
    writeMessagesHelper: vi.fn(),
  };
});

vi.mock('../telemetry.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../telemetry.js')>();
  return {
    ...actual,
    recordFormatMiss: vi.fn(),
    recordFormatRepairAttempt: vi.fn(),
    recordFormatRepairSuccess: vi.fn(),
  };
});

vi.mock('fs', () => ({
  default: {
    readFileSync: vi.fn().mockReturnValue(''),
    existsSync: vi.fn().mockReturnValue(false),
    writeFileSync: vi.fn(),
    appendFileSync: vi.fn(),
  },
}));

// Deterministic git provenance: captureGitProvenance shells out via execSync.
vi.mock('child_process', () => ({
  execSync: vi.fn((cmd: string) =>
    Buffer.from(cmd.includes('--short') ? 'abc1234\n' : 'feat/wasm-branch\n'),
  ),
}));

// ── Imports under test ────────────────────────────────────────────────────────
import fs from 'fs';
import { handleWebResearch, groundWebCitations } from './web-research.js';
import { runClaudeCode, persistAndPostStatus, readHandlerOutput, apiGet } from '../index.js';
import { configureAgentPermissions, writePlaybookHelper, writeMessagesHelper } from '../prompt.js';
import { expectCalledWith } from '../utils/typed-mock-call.js';
import { recordFormatMiss, recordFormatRepairAttempt, recordFormatRepairSuccess } from '../telemetry.js';

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
    });
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    vi.mocked(apiGet).mockResolvedValue({ documents: [] });
    // Default deterministic global fetch so the handler's web-citation grounding
    // step never reaches the real network (every probe sees a reachable 200).
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ status: 200 }));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
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
    // The serialized document carries the web-citation grounding stamp (the one
    // https citation probed reachable via the default fetch stub).
    const grounding = (parsed as Record<string, unknown>)._citation_grounding as Record<string, unknown>;
    expect(grounding).toMatchObject({ file_citations: 1, grounded: 1, checked: true, unresolved: [] });
    // No artifact_type on the success path — the backend completion handler is
    // the sole writer of the durable KnowledgeDocument.
    expect(payload.artifact_type).toBeUndefined();
  });

  it('falls back to parsing the document from output when the file is absent', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 0, allOutput: '',
      lastResultText: `Here is the document:\n${JSON.stringify(VALID_DOCUMENT)}`,
    });

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

  it('provisions messages and playbook helpers for the web_research workspace', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(VALID_DOCUMENT);

    await handleWebResearch(
      TASK_ID, { prompt: 'Research X' },
      BASE_TASK, 'tok', 'owner', 'repo', WS, STATUS_REF(),
    );

    // Both helper scripts must be provisioned so the agent can escalate
    // blockers and persist collective memory during a web research session.
    expectCalledWith(writePlaybookHelper, TASK_ID);
    expectCalledWith(writeMessagesHelper, TASK_ID);
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

  it('recovers knowledge document via bounded re-ask on clean-exit parse failure', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    // First run: clean exit but unparseable output.
    vi.mocked(runClaudeCode).mockResolvedValueOnce({
      exitCode: 0, allOutput: 'Research complete but forgot to emit JSON.', lastResultText: '',
    });
    // Repair run: good document JSON.
    vi.mocked(runClaudeCode).mockResolvedValueOnce({
      exitCode: 0,
      allOutput: JSON.stringify(VALID_DOCUMENT),
      lastResultText: JSON.stringify(VALID_DOCUMENT),
    });

    await handleWebResearch(
      TASK_ID, { prompt: 'Research the state of WebAssembly' },
      BASE_TASK, 'tok', 'owner', 'repo', WS, STATUS_REF(),
    );

    const payload = lastPostedPayload();
    expect(payload.status).toBe('COMPLETED');
    const parsed = JSON.parse(payload.logs as string) as typeof VALID_DOCUMENT;
    expect(parsed.title).toBe(VALID_DOCUMENT.title);
    expect(recordFormatRepairAttempt).toHaveBeenCalledTimes(1);
    expect(recordFormatRepairSuccess).toHaveBeenCalledTimes(1);
  });

  it('posts COMPLETED even when repair fails to recover the knowledge document', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    // Both runs return unparseable output on clean exit.
    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 0, allOutput: '', lastResultText: '',
    });

    await handleWebResearch(
      TASK_ID, { prompt: 'Research the state of WebAssembly' },
      BASE_TASK, 'tok', 'owner', 'repo', WS, STATUS_REF(),
    );

    // Still COMPLETED — web-research handler is lenient even on parse failure.
    const payload = lastPostedPayload();
    expect(payload.status).toBe('COMPLETED');
    expect(recordFormatRepairAttempt).toHaveBeenCalledTimes(1);
    expect(recordFormatRepairSuccess).not.toHaveBeenCalled();
  });

  it('marks the task FAILED when the inner session exits non-zero', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 1, allOutput: 'boom', lastResultText: '',
    });

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

// ── groundWebCitations (web-URL reachability grounding) ───────────────────────

// A DNS NXDOMAIN error as Node's fetch surfaces it (code on the error, or nested
// under `.cause`).
function dnsError(): Error {
  return Object.assign(new Error('getaddrinfo ENOTFOUND'), { code: 'ENOTFOUND' });
}

// Build a fake fetch that maps a URL to either a status (a response) or an Error
// (a transport failure). Control hosts (google/example) default to 200 so the
// connectivity guard passes unless a test overrides them.
function fakeFetch(map: (url: string, method: string) => { status: number } | Error) {
  return vi.fn((url: string | URL, opts?: { method?: string }) => {
    const u = String(url);
    const r = map(u, opts?.method ?? 'GET');
    if (r instanceof Error) return Promise.reject(r);
    return Promise.resolve(r as unknown as Response);
  }) as unknown as typeof globalThis.fetch;
}

const CONTROL_OK = (url: string): { status: number } | null =>
  url.includes('www.google.com') || url.includes('example.com') ? { status: 200 } : null;

// ── Bounded self-repair on a clean-exit-but-unparseable first response ────────
describe('handleWebResearch — bounded self-repair on format miss', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    vi.mocked(apiGet).mockResolvedValue({ documents: [] });
    // Reachable control + citation probes so grounding never hits the network.
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ status: 200 }));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('makes exactly one focused re-ask when exitCode=0 but no document extracts, and COMPLETES with the recovered document', async () => {
    vi.mocked(runClaudeCode).mockResolvedValueOnce({
      exitCode: 0, allOutput: 'I searched but never emitted the JSON document.', lastResultText: '',
    });
    vi.mocked(runClaudeCode).mockResolvedValueOnce({
      exitCode: 0, allOutput: '', lastResultText: JSON.stringify(VALID_DOCUMENT),
    });

    await handleWebResearch(
      TASK_ID, { prompt: 'Research the state of WebAssembly' },
      BASE_TASK, 'tok', 'owner', 'repo', WS, STATUS_REF(),
    );

    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(2);
    expect(vi.mocked(recordFormatMiss)).toHaveBeenCalledTimes(1);
    expect(vi.mocked(recordFormatRepairAttempt)).toHaveBeenCalledTimes(1);
    expect(vi.mocked(recordFormatRepairSuccess)).toHaveBeenCalledTimes(1);

    const payload = lastPostedPayload();
    expect(payload.status).toBe('COMPLETED');
    const parsed = JSON.parse(payload.logs as string) as typeof VALID_DOCUMENT;
    expect(parsed.title).toBe(VALID_DOCUMENT.title);
    expect((parsed as Record<string, unknown>)._citation_grounding).toBeDefined();
    expect(payload.artifact_type).toBeUndefined();
  });

  it('does NOT record repair success when the re-ask is also unparseable; falls back to raw output', async () => {
    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 0, allOutput: 'no json', lastResultText: 'raw fallback',
    });

    await handleWebResearch(
      TASK_ID, { prompt: 'Research X' },
      BASE_TASK, 'tok', 'owner', 'repo', WS, STATUS_REF(),
    );

    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(2);
    expect(vi.mocked(recordFormatRepairAttempt)).toHaveBeenCalledTimes(1);
    expect(vi.mocked(recordFormatRepairSuccess)).not.toHaveBeenCalled();
    expect(lastPostedPayload().status).toBe('COMPLETED');
  });

  it('skips the re-ask entirely (no format telemetry) when the run exited non-zero', async () => {
    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 1, allOutput: 'boom', lastResultText: '',
    });

    await handleWebResearch(
      TASK_ID, { prompt: 'Research X' },
      BASE_TASK, 'tok', 'owner', 'repo', WS, STATUS_REF(),
    );

    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(1);
    expect(vi.mocked(recordFormatMiss)).not.toHaveBeenCalled();
    expect(vi.mocked(recordFormatRepairAttempt)).not.toHaveBeenCalled();
    expect(lastPostedPayload().status).toBe('FAILED');
  });

  it('includes prior analysis in the repair prompt when a progress file exists', async () => {
    // First run: clean exit but no document.
    vi.mocked(runClaudeCode).mockResolvedValueOnce({
      exitCode: 0, allOutput: 'Research done but no JSON.', lastResultText: '',
    });
    // Repair run: valid document.
    vi.mocked(runClaudeCode).mockResolvedValueOnce({
      exitCode: 0, allOutput: '', lastResultText: JSON.stringify(VALID_DOCUMENT),
    });
    // Progress file exists with content.
    vi.mocked(fs.existsSync).mockImplementation((p: unknown) => {
      return String(p).includes(`web_research_progress_${TASK_ID}`) || false;
    });
    vi.mocked(fs.readFileSync).mockImplementation((p: unknown) => {
      if (String(p).includes(`web_research_progress_${TASK_ID}`)) {
        return 'Searched WebAssembly spec and found key performance data.';
      }
      return '';
    });

    await handleWebResearch(
      TASK_ID, { prompt: 'Research X' },
      BASE_TASK, 'tok', 'owner', 'repo', WS, STATUS_REF(),
    );

    const repairPrompt = vi.mocked(runClaudeCode).mock.calls[1][0];
    expect(repairPrompt).toContain('## Pre-Implementation Analysis');
    expect(repairPrompt).toContain('Searched WebAssembly spec and found key performance data.');
  });

  it('omits prior analysis in the repair prompt when the progress file is absent', async () => {
    vi.mocked(runClaudeCode).mockResolvedValueOnce({
      exitCode: 0, allOutput: 'Research done but no JSON.', lastResultText: '',
    });
    vi.mocked(runClaudeCode).mockResolvedValueOnce({
      exitCode: 0, allOutput: '', lastResultText: JSON.stringify(VALID_DOCUMENT),
    });
    // existsSync returns false for the progress file (absent).
    vi.mocked(fs.existsSync).mockReturnValue(false);

    await handleWebResearch(
      TASK_ID, { prompt: 'Research X' },
      BASE_TASK, 'tok', 'owner', 'repo', WS, STATUS_REF(),
    );

    const repairPrompt = vi.mocked(runClaudeCode).mock.calls[1][0];
    expect(repairPrompt).not.toContain('## Pre-Implementation Analysis');
    expect(repairPrompt).toContain('You did not emit the knowledge document JSON');
  });
});

describe('groundWebCitations', () => {
  it('stamps all reachable URLs as grounded with an empty unresolved list', async () => {
    const doc: Record<string, unknown> = {
      source_citations: ['https://a.example.org/x', 'https://b.example.org/y'],
    };
    const fetchImpl = fakeFetch((url) => CONTROL_OK(url) ?? { status: 200 });

    await groundWebCitations(doc, fetchImpl);

    expect(doc._citation_grounding).toEqual({
      file_citations: 2, grounded: 2, checked: true, unresolved: [],
    });
  });

  it('lists a definitively-dead (404/410) URL as unresolved', async () => {
    const dead = 'https://a.example.org/missing';
    const live = 'https://b.example.org/ok';
    const doc: Record<string, unknown> = { source_citations: [dead, live] };
    const fetchImpl = fakeFetch((url) => {
      const c = CONTROL_OK(url);
      if (c) return c;
      return url === dead ? { status: 404 } : { status: 200 };
    });

    await groundWebCitations(doc, fetchImpl);

    expect(doc._citation_grounding).toEqual({
      file_citations: 2, grounded: 1, checked: true, unresolved: [dead],
    });
  });

  it('marks a DNS-resolution failure (ENOTFOUND) as unresolved', async () => {
    const dead = 'https://nonexistent.invalid/page';
    const doc: Record<string, unknown> = { source_citations: [dead] };
    const fetchImpl = fakeFetch((url) => CONTROL_OK(url) ?? dnsError());

    await groundWebCitations(doc, fetchImpl);

    expect(doc._citation_grounding).toEqual({
      file_citations: 1, grounded: 0, checked: true, unresolved: [dead],
    });
  });

  it('treats bot-hostile statuses (403/429/5xx) as grounded, not unresolved', async () => {
    const cites = ['https://h1/forbidden', 'https://h2/ratelimited', 'https://h3/down'];
    const doc: Record<string, unknown> = { source_citations: cites };
    const statuses: Record<string, number> = {
      'https://h1/forbidden': 403, 'https://h2/ratelimited': 429, 'https://h3/down': 503,
    };
    const fetchImpl = fakeFetch((url) => CONTROL_OK(url) ?? { status: statuses[url] });

    await groundWebCitations(doc, fetchImpl);

    expect(doc._citation_grounding).toEqual({
      file_citations: 3, grounded: 3, checked: true, unresolved: [],
    });
  });

  it('ignores file:/path-style citations (not counted toward file_citations)', async () => {
    const doc: Record<string, unknown> = {
      source_citations: ['file:backend/app/x.py', 'docs/notes.md', 'https://web.example/only'],
    };
    const fetchImpl = fakeFetch((url) => CONTROL_OK(url) ?? { status: 200 });

    await groundWebCitations(doc, fetchImpl);

    // Only the single https citation is checked; non-http refs are design's domain.
    expect(doc._citation_grounding).toEqual({
      file_citations: 1, grounded: 1, checked: true, unresolved: [],
    });
  });

  it('falls back to GET when HEAD is unsupported (405) and grounds on the GET status', async () => {
    const url = 'https://head-hostile.example/resource';
    const doc: Record<string, unknown> = { source_citations: [url] };
    const fetchImpl = fakeFetch((u, method) => {
      const c = CONTROL_OK(u);
      if (c) return c;
      return method === 'HEAD' ? { status: 405 } : { status: 200 };
    });

    await groundWebCitations(doc, fetchImpl);

    expect(doc._citation_grounding).toEqual({
      file_citations: 1, grounded: 1, checked: true, unresolved: [],
    });
  });

  it('degrades to checked:false when there is no outbound connectivity (never false-fails)', async () => {
    const doc: Record<string, unknown> = {
      source_citations: ['https://a.example/x', 'https://b.example/y'],
    };
    // EVERY request (including the control hosts) throws a transport error.
    const fetchImpl = fakeFetch(() => new Error('ECONNREFUSED'));

    await groundWebCitations(doc, fetchImpl);

    expect(doc._citation_grounding).toEqual({
      file_citations: 2, grounded: 0, checked: false, unresolved: [],
    });
  });

  it('stamps checked:true with empty counts when there are no web citations', async () => {
    const doc: Record<string, unknown> = { source_citations: ['file:only/local.py'] };
    const fetchImpl = fakeFetch((url) => CONTROL_OK(url) ?? { status: 200 });

    await groundWebCitations(doc, fetchImpl);

    expect(doc._citation_grounding).toEqual({
      file_citations: 0, grounded: 0, checked: true, unresolved: [],
    });
  });

  it('caps the number of probed citations and does not count over-cap URLs', async () => {
    const cites = Array.from({ length: 15 }, (_, i) => `https://h${i}.example/p`);
    const doc: Record<string, unknown> = { source_citations: cites };
    const fetchImpl = fakeFetch((url) => CONTROL_OK(url) ?? { status: 200 });

    await groundWebCitations(doc, fetchImpl);

    const grounding = doc._citation_grounding as { file_citations: number; grounded: number };
    expect(grounding.file_citations).toBe(12);
    expect(grounding.grounded).toBe(12);
  });
});
