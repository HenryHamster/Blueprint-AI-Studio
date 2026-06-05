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
  validateBranchName: (b: string) => b,
  resolveBaseBranch: () => 'main',
  setCurrentPromptFingerprint: vi.fn(),
  sendHeartbeatSync: vi.fn(),
}));

vi.mock('../workspacePrep.js', () => ({
  prepareWorkspaceForRole: vi.fn().mockResolvedValue(undefined),
}));

vi.mock('../prompt.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../prompt.js')>();
  return {
    ...actual,
    buildContextPrefix: vi.fn().mockReturnValue(''),
    buildRetryContext: vi.fn().mockReturnValue(''),
    buildFileTree: vi.fn().mockReturnValue(''),
    readClaudeMd: vi.fn().mockReturnValue(''),
    writePlaybookHelper: vi.fn(),
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
    Buffer.from(cmd.includes('--short') ? 'def5678\n' : 'design/reaper-branch\n'),
  ),
}));

// ── Imports under test ────────────────────────────────────────────────────────
import fs from 'fs';
import { handleDesign, groundDesignCitations } from './design.js';
import { runClaudeCode, persistAndPostStatus, readHandlerOutput } from '../index.js';
import { configureAgentPermissions } from '../prompt.js';
import { prepareWorkspaceForRole } from '../workspacePrep.js';
import { expectCalledWith } from '../utils/typed-mock-call.js';

const TASK_ID = 'task-design-1';
const WS = null;
const STATUS_REF = () => ({ value: '' });
const BASE_TASK = { title: 'Design', description: 'Design how X should work', context: {} };

const VALID_DOCUMENT = {
  title: 'Design: how the reaper should sweep blocked tasks',
  summary: 'A codebase-grounded design for the blocked-task sweep.',
  body: '## Current state\n\nThe reaper sweeps in reaper_sweeps.py...',
  source_citations: ['file:backend/app/reaper_sweeps.py'],
  tags: ['reaper', 'design'],
  topic: 'reaper',
};

function lastPostedPayload(): Record<string, unknown> {
  const calls = vi.mocked(persistAndPostStatus).mock.calls;
  return calls[calls.length - 1][1];
}

describe('handleDesign', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 0, allOutput: '', lastResultText: '',
    } as Awaited<ReturnType<typeof runClaudeCode>>);
    vi.mocked(readHandlerOutput).mockReturnValue(null);
  });

  it('clones the repo and builds a codebase-grounded prompt with file-citation cues', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(VALID_DOCUMENT);

    await handleDesign(
      TASK_ID, { prompt: 'Design how X should work', topic: 'reaper' },
      BASE_TASK, 'tok', 'owner', 'repo', WS, STATUS_REF(),
    );

    // requires_code: the design handler clones the repo (unlike web_research).
    expect(prepareWorkspaceForRole).toHaveBeenCalledTimes(1);

    const prompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    // Codebase-grounded, file-citation-first guidance.
    expect(prompt).toContain('GROUNDING your design in the ACTUAL code');
    expect(prompt).toContain('FILE references are the default');
    // Document schema cues the backend completion handler parses.
    for (const key of ['title', 'summary', 'body', 'source_citations', 'topic']) {
      expect(prompt).toContain(`"${key}"`);
    }
    // Read-only: the agent must write via Bash because the Write tool is disabled.
    expect(prompt).toContain(`/tmp/design_document_${TASK_ID}.json`);
  });

  it('reads the document from the /tmp file and completes with the document JSON in logs', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(VALID_DOCUMENT);

    await handleDesign(
      TASK_ID, { prompt: 'Design how X should work' },
      BASE_TASK, 'tok', 'owner', 'repo', WS, STATUS_REF(),
    );

    expectCalledWith(readHandlerOutput, TASK_ID, 'design_document', WS);
    const payload = lastPostedPayload();
    expect(payload.status).toBe('COMPLETED');
    const parsed = JSON.parse(payload.logs as string) as typeof VALID_DOCUMENT;
    expect(parsed.title).toBe(VALID_DOCUMENT.title);
    expect(parsed.source_citations).toEqual(VALID_DOCUMENT.source_citations);
    // No artifact_type on the success path — the backend completion handler is
    // the sole writer of the durable KnowledgeDocument.
    expect(payload.artifact_type).toBeUndefined();
  });

  it('installs the read-only permission posture for the design type', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(VALID_DOCUMENT);

    await handleDesign(
      TASK_ID, { prompt: 'Design X' },
      BASE_TASK, 'tok', 'owner', 'repo', WS, STATUS_REF(),
    );

    expectCalledWith(configureAgentPermissions, 'design');
    const disallowed = vi.mocked(runClaudeCode).mock.calls[0][5] as string[];
    expect(disallowed).toContain('Write');
    expect(disallowed).toContain('Edit');
  });

  it('surfaces a citation-less / empty output for the gate to reject (FAILED on non-zero exit)', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 1, allOutput: 'boom', lastResultText: '',
    } as Awaited<ReturnType<typeof runClaudeCode>>);

    await handleDesign(
      TASK_ID, { prompt: 'Design X' },
      BASE_TASK, 'tok', 'owner', 'repo', WS, STATUS_REF(),
    );

    const payload = lastPostedPayload();
    expect(payload.status).toBe('FAILED');
    expect(payload.failure_category).toBe('claude_code_failed');
    expect(payload.artifact_type).toBe('ERROR_LOG');
  });

  it('embeds branch/HEAD provenance INSIDE the emitted document (not as a sibling field)', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue({ ...VALID_DOCUMENT });

    await handleDesign(
      TASK_ID, { prompt: 'Design how X should work' },
      BASE_TASK, 'tok', 'owner', 'repo', WS, STATUS_REF(),
    );

    const payload = lastPostedPayload();
    // Provenance must live INSIDE the parsed document — the backend extracts it
    // from the parsed `logs` document, not from a top-level status payload field.
    const parsed = JSON.parse(payload.logs as string) as Record<string, unknown>;
    expect(parsed.authoring_branch).toBe('design/reaper-branch');
    expect(parsed.head_sha).toBe('def5678');
    // And NOT as a sibling of logs (where the backend never reads it).
    expect(payload.authoring_branch).toBeUndefined();
    expect(payload.head_sha).toBeUndefined();
  });

  it('stamps _citation_grounding on the emitted document for the backend gate', async () => {
    // existsSync false (module default) → file citation resolves to nothing.
    vi.mocked(readHandlerOutput).mockReturnValue({ ...VALID_DOCUMENT });

    await handleDesign(
      TASK_ID, { prompt: 'Design how X should work' },
      BASE_TASK, 'tok', 'owner', 'repo', WS, STATUS_REF(),
    );

    const payload = lastPostedPayload();
    const parsed = JSON.parse(payload.logs as string) as Record<string, unknown>;
    expect(parsed._citation_grounding).toEqual({
      file_citations: 1, grounded: 0, checked: true,
    });
  });
});

describe('groundDesignCitations', () => {
  it('grounds every file citation that resolves against the working tree', () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    const doc: Record<string, unknown> = {
      source_citations: ['file:backend/app/a.py', 'agent/src/b.ts'],
    };
    groundDesignCitations(doc, '/tmp/workspace');
    expect(doc._citation_grounding).toEqual({
      file_citations: 2, grounded: 2, checked: true,
    });
  });

  it('reports zero grounded when no file citation resolves', () => {
    vi.mocked(fs.existsSync).mockReturnValue(false);
    const doc: Record<string, unknown> = {
      source_citations: ['file:made/up/path.py', 'hallucinated/x.ts'],
    };
    groundDesignCitations(doc, '/tmp/workspace');
    expect(doc._citation_grounding).toEqual({
      file_citations: 2, grounded: 0, checked: true,
    });
  });

  it('ignores web URLs and accepts a real+proposed mix (>=1 grounded)', () => {
    vi.mocked(fs.existsSync).mockImplementation(
      (p) => String(p).includes('real.py'),
    );
    const doc: Record<string, unknown> = {
      source_citations: [
        'https://example.com/ref',
        'file:backend/real.py',
        'backend/proposed/new.py',
      ],
    };
    groundDesignCitations(doc, '/tmp/workspace');
    // The web URL is not counted; one of two file refs resolves.
    expect(doc._citation_grounding).toEqual({
      file_citations: 2, grounded: 1, checked: true,
    });
  });
});
