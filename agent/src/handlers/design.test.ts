import { describe, it, expect, vi, beforeEach } from 'vitest';

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
  validateBranchName: (b: string) => b,
  resolveBaseBranch: () => 'main',
  setCurrentPromptFingerprint: vi.fn(),
  sendHeartbeatSync: vi.fn(),
  apiGet: vi.fn().mockResolvedValue({ documents: [] }),
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
    writeMessagesHelper: vi.fn(),
    configureAgentPermissions: vi.fn().mockReturnValue(['Write', 'Edit', 'NotebookEdit']),
    verifyCloneDelivered: vi.fn(),
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
    // handleDesign now provisions the read-only ./design-doc and ./fetch
    // helpers (writeDesignDocHelper / writeFetchHelper), which write files; the
    // helper exclusion path appends to .git/info/exclude. Mock the writers so
    // provisioning is a no-op rather than throwing on the bare fs mock.
    writeFileSync: vi.fn(),
    appendFileSync: vi.fn(),
  },
}));

// Deterministic git provenance: captureGitProvenance shells out via execSync.
vi.mock('child_process', () => ({
  execSync: vi.fn((cmd: string) =>
    Buffer.from(cmd.includes('--short') ? 'def5678\n' : 'design/reaper-branch\n'),
  ),
}));

vi.mock('../telemetry.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../telemetry.js')>();
  return {
    ...actual,
    recordFormatMiss: vi.fn(),
    recordFormatRepairAttempt: vi.fn(),
    recordFormatRepairSuccess: vi.fn(),
  };
});

// ── Imports under test ────────────────────────────────────────────────────────
import fs from 'fs';
import { handleDesign, groundDesignCitations, buildCitationRepairPrompt } from './design.js';
import { runClaudeCode, persistAndPostStatus, readHandlerOutput, apiGet } from '../index.js';
import { buildFileTree, configureAgentPermissions, verifyCloneDelivered, writePlaybookHelper, writeMessagesHelper } from '../prompt.js';
import { prepareWorkspaceForRole } from '../workspacePrep.js';
import { expectCalledWith } from '../utils/typed-mock-call.js';
import { recordFormatMiss, recordFormatRepairAttempt, recordFormatRepairSuccess, getLlmTelemetry, resetLlmTelemetry } from '../telemetry.js';

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
    });
    vi.mocked(verifyCloneDelivered).mockImplementation(() => undefined);
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    // Citations ground by default so tests not targeting the grounding-repair
    // reopen never trigger it incidentally.
    vi.mocked(fs.existsSync).mockReturnValue(true);
  });

  it('clones the repo and builds a codebase-grounded prompt with file-citation cues', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(VALID_DOCUMENT);

    await handleDesign(
      TASK_ID, { prompt: 'Design how X should work', topic: 'reaper' },
      BASE_TASK, 'tok', 'owner', 'repo', WS, STATUS_REF(),
    );

    // requires_code: the design handler clones the repo (unlike web_research).
    expect(prepareWorkspaceForRole).toHaveBeenCalledTimes(1);
    expect(verifyCloneDelivered).toHaveBeenCalledWith(BASE_TASK, 'main');
    expect(vi.mocked(prepareWorkspaceForRole).mock.invocationCallOrder[0])
      .toBeLessThan(vi.mocked(verifyCloneDelivered).mock.invocationCallOrder[0]);
    expect(vi.mocked(verifyCloneDelivered).mock.invocationCallOrder[0])
      .toBeLessThan(vi.mocked(runClaudeCode).mock.invocationCallOrder[0]);

    const prompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    // Codebase-grounded, file-citation-first guidance.
    expect(prompt).toContain('GROUNDING your design in the ACTUAL code');
    expect(prompt).toContain('FILE references are the default');
    // The grounding contract is explicit: citations are machine-checked, so the
    // agent is told to emit clean repo-relative paths and verify them first.
    expect(prompt).toContain('MACHINE-CHECKED');
    // Document schema cues the backend completion handler parses.
    for (const key of ['title', 'summary', 'body', 'source_citations', 'topic']) {
      expect(prompt).toContain(`"${key}"`);
    }
    // Read-only: the agent must write via Bash because the Write tool is disabled.
    expect(prompt).toContain(`/tmp/design_document_${TASK_ID}.json`);
  });

  it('rejects_clone_disagreement_before_model_invocation', async () => {
    const cloneFailure = Object.assign(
      new Error('clone_failed: cloned tree disagrees with durable non-empty signal'),
      { failureCategory: 'clone_failed' },
    );
    vi.mocked(verifyCloneDelivered).mockImplementation(() => { throw cloneFailure; });

    await expect(handleDesign(
      TASK_ID, { prompt: 'Design how X should work' },
      { ...BASE_TASK, repo_is_empty: false }, 'tok', 'owner', 'repo', WS, STATUS_REF(),
    )).rejects.toMatchObject({ message: cloneFailure.message, failureCategory: 'clone_failed' });

    expect(verifyCloneDelivered).toHaveBeenCalledWith(expect.objectContaining({ repo_is_empty: false }), 'main');
    expect(buildFileTree).not.toHaveBeenCalled();
    expect(runClaudeCode).not.toHaveBeenCalled();
  });

  it('renders the operational playbook (with provenance caveats) into the prompt', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(VALID_DOCUMENT);

    const taskWithPlaybook = {
      ...BASE_TASK,
      playbook: {
        'build-cmd': {
          content: 'Run the docker test harness via scripts/test-backend.sh.',
          effectiveness_score: 0.9, confirmed_count: 2, age_days: 1, stale: false,
        },
        'stale-fact': {
          content: 'A fact recorded on a divergent branch.',
          effectiveness_score: 0.8, confirmed_count: 1, age_days: 3, stale: false,
          provenance: { branch: 'feature/x', head_sha: 'abc1234', diverged: true },
        },
      },
    };

    await handleDesign(
      TASK_ID, { prompt: 'Design how X should work' },
      taskWithPlaybook, 'tok', 'owner', 'repo', WS, STATUS_REF(),
    );

    const prompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(prompt).toContain('## Operational Playbook (active collective memory)');
    expect(prompt).toContain('Run the docker test harness via scripts/test-backend.sh.');
    // Provenance divergence caveat surfaces for the divergent-branch entry.
    expect(prompt).toContain('⚠ different branch than yours');
  });

  it('omits the playbook section when no playbook is shipped', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(VALID_DOCUMENT);

    await handleDesign(
      TASK_ID, { prompt: 'Design how X should work' },
      BASE_TASK, 'tok', 'owner', 'repo', WS, STATUS_REF(),
    );

    const prompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(prompt).not.toContain('## Operational Playbook (active collective memory)');
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

  it('provisions messages helper alongside playbook helper for the design workspace', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(VALID_DOCUMENT);

    await handleDesign(
      TASK_ID, { prompt: 'Design X' },
      BASE_TASK, 'tok', 'owner', 'repo', WS, STATUS_REF(),
    );

    // Both helpers must be provisioned so the agent can escalate blockers and
    // persist collective memory during a codebase-grounded design session.
    expectCalledWith(writePlaybookHelper, TASK_ID);
    expectCalledWith(writeMessagesHelper, TASK_ID);
  });

  it('injects a Prior Knowledge section with prior-doc ids/titles/summaries when docs exist', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(VALID_DOCUMENT);
    vi.mocked(apiGet).mockResolvedValue({
      documents: [
        { id: 'doc-aaa', title: 'Earlier reaper design', summary: 'A prior sweep design.', topic: 'reaper' },
        { id: 'doc-bbb', title: 'Blocked-task lifecycle', summary: 'How tasks block.' },
      ],
    });

    await handleDesign(
      TASK_ID, { prompt: 'Design how X should work', topic: 'reaper' },
      BASE_TASK, 'tok', 'owner', 'repo', WS, STATUS_REF(),
    );

    // Query uses the topic (preferred over prompt) and hits the agent endpoint.
    const calledPath = vi.mocked(apiGet).mock.calls[0][0];
    expect(calledPath).toContain('/api/agent/knowledge-documents');
    expect(calledPath).toContain(`q=${encodeURIComponent('reaper')}`);

    const prompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(prompt).toContain('## Prior Knowledge');
    expect(prompt).toContain('doc-aaa');
    expect(prompt).toContain('Earlier reaper design');
    expect(prompt).toContain('A prior sweep design.');
    expect(prompt).toContain('doc-bbb');
    // Explicit build-on / supersede / cite instruction.
    expect(prompt).toContain('supersede');
    expect(prompt).toContain('cite');
  });

  it('omits the Prior Knowledge section when the fetch returns no documents', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(VALID_DOCUMENT);
    vi.mocked(apiGet).mockResolvedValue({ documents: [] });

    await handleDesign(
      TASK_ID, { prompt: 'Design how X should work', topic: 'reaper' },
      BASE_TASK, 'tok', 'owner', 'repo', WS, STATUS_REF(),
    );

    const prompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(prompt).not.toContain('## Prior Knowledge');
    expect(lastPostedPayload().status).toBe('COMPLETED');
  });

  it('completes normally (no Prior Knowledge, no failure) when the knowledge fetch throws', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(VALID_DOCUMENT);
    vi.mocked(apiGet).mockRejectedValue(new Error('knowledge endpoint unreachable'));

    await handleDesign(
      TASK_ID, { prompt: 'Design how X should work', topic: 'reaper' },
      BASE_TASK, 'tok', 'owner', 'repo', WS, STATUS_REF(),
    );

    const prompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(prompt).not.toContain('## Prior Knowledge');
    expect(lastPostedPayload().status).toBe('COMPLETED');
  });

  it('recovers design document via bounded re-ask on clean-exit parse failure', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    // First run: clean exit but unparseable output.
    vi.mocked(runClaudeCode).mockResolvedValueOnce({
      exitCode: 0, allOutput: 'Analysis complete but forgot to emit JSON.', lastResultText: '',
    });
    // Repair run: good document JSON.
    vi.mocked(runClaudeCode).mockResolvedValueOnce({
      exitCode: 0,
      allOutput: JSON.stringify(VALID_DOCUMENT),
      lastResultText: JSON.stringify(VALID_DOCUMENT),
    });

    await handleDesign(
      TASK_ID, { prompt: 'Design how X should work' },
      BASE_TASK, 'tok', 'owner', 'repo', WS, STATUS_REF(),
    );

    const payload = lastPostedPayload();
    expect(payload.status).toBe('COMPLETED');
    const parsed = JSON.parse(payload.logs as string) as typeof VALID_DOCUMENT;
    expect(parsed.title).toBe(VALID_DOCUMENT.title);
    expect(recordFormatRepairAttempt).toHaveBeenCalledTimes(1);
    expect(recordFormatRepairSuccess).toHaveBeenCalledTimes(1);
  });

  it('posts COMPLETED even when repair fails to recover the design document', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    // Both runs return unparseable output on clean exit.
    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 0, allOutput: '', lastResultText: '',
    });

    await handleDesign(
      TASK_ID, { prompt: 'Design how X should work' },
      BASE_TASK, 'tok', 'owner', 'repo', WS, STATUS_REF(),
    );

    // Still COMPLETED — design handler is lenient even on parse failure.
    const payload = lastPostedPayload();
    expect(payload.status).toBe('COMPLETED');
    expect(recordFormatRepairAttempt).toHaveBeenCalledTimes(1);
    expect(recordFormatRepairSuccess).not.toHaveBeenCalled();
  });

  it('surfaces a citation-less / empty output for the gate to reject (FAILED on non-zero exit)', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 1, allOutput: 'boom', lastResultText: '',
    });

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
    // existsSync false → the file citation resolves to nothing (this also
    // exercises the repair reopen, whose retry re-reads the same document and
    // keeps the truthful zero-grounded stamp).
    vi.mocked(fs.existsSync).mockReturnValue(false);
    vi.mocked(readHandlerOutput).mockReturnValue({ ...VALID_DOCUMENT });

    await handleDesign(
      TASK_ID, { prompt: 'Design how X should work' },
      BASE_TASK, 'tok', 'owner', 'repo', WS, STATUS_REF(),
    );

    const payload = lastPostedPayload();
    const parsed = JSON.parse(payload.logs as string) as Record<string, unknown>;
    expect(parsed._citation_grounding).toEqual({
      file_citations: 1, grounded: 0, checked: true,
      unresolved: ['file:backend/app/reaper_sweeps.py'],
    });
  });

  it('records prompt size telemetry before calling runClaudeCode', async () => {
    resetLlmTelemetry();
    vi.mocked(readHandlerOutput).mockReturnValue(VALID_DOCUMENT);

    await handleDesign(
      TASK_ID, { prompt: 'Design how X should work' },
      BASE_TASK, 'tok', 'owner', 'repo', WS, STATUS_REF(),
    );

    const telemetry = getLlmTelemetry();
    // prompt_size_bytes must reflect the real assembled design prompt.
    expect(telemetry.prompt_size_bytes).toBeGreaterThan(0);
    expect(telemetry.prompt_section_count).toBeGreaterThan(0);
    expect(telemetry.prompt_section_sizes).not.toBeNull();
    expect((telemetry.prompt_section_sizes ?? []).length).toBeGreaterThan(0);
    for (const s of (telemetry.prompt_section_sizes ?? [])) {
      expect(s.bytes).toBeGreaterThan(0);
      expect(typeof s.name).toBe('string');
    }
  });
});

// ── Bounded self-repair on a clean-exit-but-unparseable first response ────────
describe('handleDesign — bounded self-repair on format miss', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    // Recovered documents ground cleanly — these tests target the FORMAT
    // repair, not the citation-grounding repair.
    vi.mocked(fs.existsSync).mockReturnValue(true);
  });

  it('makes exactly one focused re-ask when exitCode=0 but no document extracts, and COMPLETES with the recovered document', async () => {
    // First call: clean exit, narrative but no document JSON.
    vi.mocked(runClaudeCode).mockResolvedValueOnce({
      exitCode: 0, allOutput: 'I explored the reaper but forgot the JSON.', lastResultText: '',
    });
    // Second call (repair): valid document JSON in lastResultText.
    vi.mocked(runClaudeCode).mockResolvedValueOnce({
      exitCode: 0, allOutput: '', lastResultText: JSON.stringify(VALID_DOCUMENT),
    });

    await handleDesign(
      TASK_ID, { prompt: 'Design how X should work' },
      BASE_TASK, 'tok', 'owner', 'repo', WS, STATUS_REF(),
    );

    // Exactly one re-ask (two runClaudeCode invocations total — strictly bounded).
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(2);
    expect(vi.mocked(recordFormatMiss)).toHaveBeenCalledTimes(1);
    expect(vi.mocked(recordFormatRepairAttempt)).toHaveBeenCalledTimes(1);
    expect(vi.mocked(recordFormatRepairSuccess)).toHaveBeenCalledTimes(1);

    const payload = lastPostedPayload();
    expect(payload.status).toBe('COMPLETED');
    const parsed = JSON.parse(payload.logs as string) as typeof VALID_DOCUMENT;
    expect(parsed.title).toBe(VALID_DOCUMENT.title);
    // Recovered document path still grounds citations for the backend gate.
    expect((parsed as Record<string, unknown>)._citation_grounding).toBeDefined();
    // Success path omits artifact_type (backend is the sole KnowledgeDocument writer).
    expect(payload.artifact_type).toBeUndefined();
  });

  it('does NOT record repair success when the re-ask is also unparseable; falls back to raw output (still bounded to one re-ask)', async () => {
    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 0, allOutput: 'no json', lastResultText: 'raw fallback',
    });

    await handleDesign(
      TASK_ID, { prompt: 'Design X' },
      BASE_TASK, 'tok', 'owner', 'repo', WS, STATUS_REF(),
    );

    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(2);
    expect(vi.mocked(recordFormatMiss)).toHaveBeenCalledTimes(1);
    expect(vi.mocked(recordFormatRepairAttempt)).toHaveBeenCalledTimes(1);
    expect(vi.mocked(recordFormatRepairSuccess)).not.toHaveBeenCalled();
    // exitCode=0 → still COMPLETED with the raw fallback for the backend gate.
    expect(lastPostedPayload().status).toBe('COMPLETED');
  });

  it('skips the re-ask entirely (no format telemetry) when the run exited non-zero', async () => {
    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 1, allOutput: 'boom', lastResultText: '',
    });

    await handleDesign(
      TASK_ID, { prompt: 'Design X' },
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
      exitCode: 0, allOutput: 'Analysis done but no JSON.', lastResultText: '',
    });
    // Repair run: valid document.
    vi.mocked(runClaudeCode).mockResolvedValueOnce({
      exitCode: 0, allOutput: '', lastResultText: JSON.stringify(VALID_DOCUMENT),
    });
    // Progress file exists with content.
    vi.mocked(fs.existsSync).mockImplementation((p: unknown) => {
      return String(p).includes(`design_progress_${TASK_ID}`) || true;
    });
    vi.mocked(fs.readFileSync).mockImplementation((p: unknown) => {
      if (String(p).includes(`design_progress_${TASK_ID}`)) {
        return 'Explored the reaper module and identified key files.';
      }
      return '';
    });

    await handleDesign(
      TASK_ID, { prompt: 'Design X' },
      BASE_TASK, 'tok', 'owner', 'repo', WS, STATUS_REF(),
    );

    const repairPrompt = vi.mocked(runClaudeCode).mock.calls[1][0];
    expect(repairPrompt).toContain('## Pre-Implementation Analysis');
    expect(repairPrompt).toContain('Explored the reaper module and identified key files.');
  });

  it('omits prior analysis in the repair prompt when the progress file is absent', async () => {
    vi.mocked(runClaudeCode).mockResolvedValueOnce({
      exitCode: 0, allOutput: 'Analysis done but no JSON.', lastResultText: '',
    });
    vi.mocked(runClaudeCode).mockResolvedValueOnce({
      exitCode: 0, allOutput: '', lastResultText: JSON.stringify(VALID_DOCUMENT),
    });
    // existsSync returns true for workspace paths (citation grounding) but false for progress file.
    vi.mocked(fs.existsSync).mockImplementation((p: unknown) => {
      return !String(p).includes(`design_progress_${TASK_ID}`);
    });

    await handleDesign(
      TASK_ID, { prompt: 'Design X' },
      BASE_TASK, 'tok', 'owner', 'repo', WS, STATUS_REF(),
    );

    const repairPrompt = vi.mocked(runClaudeCode).mock.calls[1][0];
    expect(repairPrompt).not.toContain('## Pre-Implementation Analysis');
    expect(repairPrompt).toContain('You did not emit the design document JSON');
  });
});

// ── Citation-grounding repair: warn the agent and let it fix its citations ────
describe('handleDesign — citation-grounding repair reopen', () => {
  // Decorated/malformed citations (the real-world failure mode: backticks,
  // appended descriptions) never resolve; clean repo-relative paths do.
  const UNGROUNDED_DOCUMENT = {
    ...VALID_DOCUMENT,
    source_citations: ['`backend/app/reaper_sweeps.py` (sweep loop)'],
  };
  const CORRECTED_DOCUMENT = {
    ...VALID_DOCUMENT,
    source_citations: ['backend/app/reaper_sweeps.py'],
  };

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 0, allOutput: '', lastResultText: '',
    });
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    // Only clean (undecorated) paths exist in the mocked working tree.
    vi.mocked(fs.existsSync).mockImplementation(
      (p) => !String(p).includes('`') && !String(p).includes('('),
    );
  });

  it('reopens once with the unresolved citations verbatim and adopts the corrected document', async () => {
    vi.mocked(readHandlerOutput)
      .mockReturnValueOnce({ ...UNGROUNDED_DOCUMENT })
      .mockReturnValueOnce({ ...CORRECTED_DOCUMENT });

    await handleDesign(
      TASK_ID, { prompt: 'Design how X should work' },
      BASE_TASK, 'tok', 'owner', 'repo', WS, STATUS_REF(),
    );

    // Exactly one reopen (two runClaudeCode invocations total — strictly bounded).
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(2);
    // The reopen prompt echoes the offending citation exactly as written, so
    // the agent can see WHICH format/path it got wrong.
    const reopenPrompt = vi.mocked(runClaudeCode).mock.calls[1][0];
    expect(reopenPrompt).toContain('NONE of its file citations resolve');
    expect(reopenPrompt).toContain('`backend/app/reaper_sweeps.py` (sweep loop)');
    expect(reopenPrompt).toContain(`/tmp/design_document_${TASK_ID}.json`);

    const payload = lastPostedPayload();
    expect(payload.status).toBe('COMPLETED');
    const parsed = JSON.parse(payload.logs as string) as Record<string, unknown>;
    expect(parsed.source_citations).toEqual(['backend/app/reaper_sweeps.py']);
    expect(parsed._citation_grounding).toEqual({
      file_citations: 1, grounded: 1, checked: true, unresolved: [],
    });
  });

  it('adopts a corrected document from the retry output when the /tmp file was not rewritten', async () => {
    // The /tmp file keeps yielding the original ungrounded document; the
    // corrected JSON arrives only in the retry response text.
    vi.mocked(readHandlerOutput).mockReturnValue({ ...UNGROUNDED_DOCUMENT });
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({
        exitCode: 0, allOutput: '', lastResultText: '',
      })
      .mockResolvedValueOnce({
        exitCode: 0, allOutput: '', lastResultText: JSON.stringify(CORRECTED_DOCUMENT),
      });

    await handleDesign(
      TASK_ID, { prompt: 'Design how X should work' },
      BASE_TASK, 'tok', 'owner', 'repo', WS, STATUS_REF(),
    );

    const parsed = JSON.parse(lastPostedPayload().logs as string) as Record<string, unknown>;
    expect(parsed.source_citations).toEqual(['backend/app/reaper_sweeps.py']);
    expect(parsed._citation_grounding).toEqual({
      file_citations: 1, grounded: 1, checked: true, unresolved: [],
    });
  });

  it('keeps the original document (truthful zero-grounded stamp) when the reopen fixes nothing', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue({ ...UNGROUNDED_DOCUMENT });

    await handleDesign(
      TASK_ID, { prompt: 'Design how X should work' },
      BASE_TASK, 'tok', 'owner', 'repo', WS, STATUS_REF(),
    );

    // Still strictly bounded: one reopen, no retry loop.
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(2);
    const payload = lastPostedPayload();
    expect(payload.status).toBe('COMPLETED');
    const parsed = JSON.parse(payload.logs as string) as Record<string, unknown>;
    expect(parsed._citation_grounding).toEqual({
      file_citations: 1, grounded: 0, checked: true,
      unresolved: ['`backend/app/reaper_sweeps.py` (sweep loop)'],
    });
  });

  it('does not reopen when at least one file citation grounds', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue({ ...CORRECTED_DOCUMENT });

    await handleDesign(
      TASK_ID, { prompt: 'Design how X should work' },
      BASE_TASK, 'tok', 'owner', 'repo', WS, STATUS_REF(),
    );

    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(1);
  });

  it('does not reopen for a web-URL-only document (no file citations to ground)', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue({
      ...VALID_DOCUMENT, source_citations: ['https://example.com/spec'],
    });

    await handleDesign(
      TASK_ID, { prompt: 'Design how X should work' },
      BASE_TASK, 'tok', 'owner', 'repo', WS, STATUS_REF(),
    );

    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(1);
  });

  it('skips the reopen when the first run exited non-zero', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue({ ...UNGROUNDED_DOCUMENT });
    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 1, allOutput: 'boom', lastResultText: '',
    });

    await handleDesign(
      TASK_ID, { prompt: 'Design X' },
      BASE_TASK, 'tok', 'owner', 'repo', WS, STATUS_REF(),
    );

    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(1);
    expect(lastPostedPayload().status).toBe('FAILED');
  });
});

describe('buildCitationRepairPrompt', () => {
  it('lists each unresolved citation verbatim and targets the document path', () => {
    const prompt = buildCitationRepairPrompt(
      ['`a/b.py` (loop)', '/tmp/workspace/c/d.py'], '/tmp/design_document_t1.json',
    );
    expect(prompt).toContain('- `a/b.py` (loop)');
    expect(prompt).toContain('- /tmp/workspace/c/d.py');
    expect(prompt).toContain('/tmp/design_document_t1.json');
    // The fix is constrained to the citations array.
    expect(prompt).toContain('Fix ONLY the source_citations array');
  });

  it('caps the echoed list and reports the overflow count', () => {
    const unresolved = Array.from({ length: 25 }, (_, i) => `made/up/path${i}.py`);
    const prompt = buildCitationRepairPrompt(unresolved, '/tmp/doc.json');
    expect(prompt).toContain('- made/up/path19.py');
    expect(prompt).not.toContain('- made/up/path20.py');
    expect(prompt).toContain('(+5 more)');
  });

  it('on greenfield steers to web citations instead of fixing to real file paths', () => {
    const nonGreenfield = buildCitationRepairPrompt(['src/does/not/exist.ts'], '/tmp/doc.json', false);
    // Default (mature repo) branch tells the agent to point at a real file.
    expect(nonGreenfield).toContain('repository-relative path of a real file');

    const greenfield = buildCitationRepairPrompt(['src/does/not/exist.ts'], '/tmp/doc.json', true);
    // Greenfield branch must NOT ask for a real file path (impossible on an
    // empty repo) — it must steer to external/web citations.
    expect(greenfield).not.toContain('repository-relative path of a real file');
    expect(greenfield).toContain('REMOVE the invented file paths');
    expect(greenfield).toMatch(/https:\/\//);
    expect(greenfield).toContain('At least one https:// citation is required');
  });
});

describe('groundDesignCitations', () => {
  it('grounds every file citation that resolves against the working tree', () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    const doc: Record<string, unknown> = {
      source_citations: ['file:backend/app/a.py', 'agent/src/b.ts'],
    };
    groundDesignCitations(doc, '/tmp/workspace');
    // Fully grounded — no unresolved citations.
    expect(doc._citation_grounding).toEqual({
      file_citations: 2, grounded: 2, checked: true, unresolved: [],
    });
  });

  it('reports zero grounded and lists every unresolved citation', () => {
    vi.mocked(fs.existsSync).mockReturnValue(false);
    const doc: Record<string, unknown> = {
      source_citations: ['file:made/up/path.py', 'hallucinated/x.ts'],
    };
    groundDesignCitations(doc, '/tmp/workspace');
    expect(doc._citation_grounding).toEqual({
      file_citations: 2, grounded: 0, checked: true,
      // Both file refs are absent — captured as written for operator visibility.
      unresolved: ['file:made/up/path.py', 'hallucinated/x.ts'],
    });
  });

  it('ignores web URLs and lists only the unresolved ref in a real+proposed mix', () => {
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
    // The web URL is not counted; one of two file refs resolves, the other is
    // surfaced in `unresolved` so operators see exactly which path is absent.
    expect(doc._citation_grounding).toEqual({
      file_citations: 2, grounded: 1, checked: true,
      unresolved: ['backend/proposed/new.py'],
    });
  });
});
