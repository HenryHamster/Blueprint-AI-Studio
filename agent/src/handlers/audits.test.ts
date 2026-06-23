import { describe, it, expect, vi, beforeEach } from 'vitest';

// ── Module mocks ──────────────────────────────────────────────────────────────
vi.mock('../index.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../index.js')>();
  return {
    ...actual,
    WORKSPACE: '/tmp/workspace',
    sendLog: vi.fn(),
    log: vi.fn(),
    getAgentToken: vi.fn().mockReturnValue(''),
    apiPost: vi.fn(),
    apiGet: vi.fn(),
    persistAndPostStatus: vi.fn().mockResolvedValue(undefined),
    readHandlerOutput: vi.fn(),
    validateBranchName: vi.fn((b: string, _label: string) => b),
    git: vi.fn().mockReturnValue(''),
    gitAsync: vi.fn().mockResolvedValue(''),
    setupWorkspace: vi.fn(),
    runClaudeCode: vi.fn().mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' }),
    cloneWithRetry: vi.fn().mockResolvedValue(undefined),
    sendHeartbeatSync: vi.fn(),
    listOpenPullRequestsForBranch: vi.fn().mockResolvedValue([]),
  };
});

vi.mock('../workspacePrep.js', () => ({
  prepareWorkspaceForRole: vi.fn().mockResolvedValue(undefined),
}));

vi.mock('../prompt.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../prompt.js')>();
  return {
    ...actual,
    getTaskTypeHints: vi.fn().mockReturnValue({}),
    readClaudeMd: vi.fn().mockReturnValue(''),
    buildContextPrefix: vi.fn().mockReturnValue(''),
    buildFileTree: vi.fn().mockReturnValue(''),
    discoverRelatedTests: vi.fn().mockReturnValue(''),
    configureAgentPermissions: vi.fn().mockReturnValue([]),
    writePlaybookHelper: vi.fn(),
    writeMessagesHelper: vi.fn(),
    writeCodeChangeHelpers: vi.fn(),
    formatPlaybook: vi.fn().mockReturnValue(''),
    buildRetryContext: vi.fn().mockReturnValue(''),
  };
});

vi.mock('../build-config.js', () => ({
  resolveBuildConfig: vi.fn().mockReturnValue(undefined),
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

// fs is consumed by both default + namespace imports across the modules under
// test. Mock both shapes so vitest doesn't reject either access pattern.
vi.mock('fs', () => {
  const stub = {
    readFileSync: vi.fn().mockReturnValue(''),
    writeFileSync: vi.fn(),
    existsSync: vi.fn().mockReturnValue(false),
    appendFileSync: vi.fn(),
    mkdirSync: vi.fn(),
    readdirSync: vi.fn().mockReturnValue([]),
    unlinkSync: vi.fn(),
  };
  return { ...stub, default: stub };
});

vi.mock('child_process', () => ({
  execFileSync: vi.fn().mockReturnValue(Buffer.from('')),
  execFile: vi.fn(),
  spawn: vi.fn().mockReturnValue({
    stderr: { on: vi.fn() },
    stdout: { on: vi.fn() },
    on: vi.fn(),
    kill: vi.fn(),
  }),
}));

vi.mock('ws', () => ({
  default: class MockWebSocket {
    static OPEN = 1;
    readyState = 0;
    on = vi.fn();
    send = vi.fn();
    close = vi.fn();
  },
}));

// ── Imports under test ────────────────────────────────────────────────────────
// Import handlers/index.ts first so audits.ts and structured-output-repair.ts
// bind to the mocked ../index.js path consistently during circular import setup.
import './index.js';
import {
  handleSecurityAudit,
  handleSelfAudit,
  handleReleaseAudit,
  resolveCandidateSha,
} from './audits.js';
import {
  persistAndPostStatus, readHandlerOutput, runClaudeCode, git,
} from '../index.js';
import { recordFormatRepairAttempt, recordFormatRepairSuccess } from '../telemetry.js';
import { execFileSync } from 'child_process';
import { prepareWorkspaceForRole } from '../workspacePrep.js';
import { READ_ONLY_BASH_WRITE_NOTE } from '../prompt.js';

// ── Shared test helpers ───────────────────────────────────────────────────────
const TASK_ID = 'task-abc123';
const GH_TOKEN = 'gh-token';
const OWNER = 'owner';
const REPO = 'repo';
const WS = null;
const STATUS_REF = () => ({ value: '' });
const BASE_TASK = { title: 'Test Task', description: '', context: {}, project_context: {} };
const BASE_PAYLOAD = { base_branch: 'main' };

// ── handleSecurityAudit — JSON extraction fallback ────────────────────────────
describe('handleSecurityAudit — JSON extraction fallback', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
  });

  it('uses readHandlerOutput result directly when it returns findings', async () => {
    const findings = [{ title: 'SQL Injection', severity: 'critical' }];
    vi.mocked(readHandlerOutput).mockReturnValue(findings);

    await handleSecurityAudit(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.logs).toBe(JSON.stringify(findings));
    expect(call.artifact_type).toBe('SECURITY_REPORT');
  });

  it('falls back to lastResultText when it contains "severity" and readHandlerOutput returns null', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    const lastResultText = '[{"title":"XSS","severity":"high","fix_prompt":"..."}]';
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText });

    await handleSecurityAudit(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.logs).toBe(lastResultText);
  });

  it('extracts JSON from allOutput via regex when lastResultText lacks "severity"', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    const jsonBlock = '[{"title":"Path Traversal","severity":"medium"}]';
    const allOutput = `Some preamble text\n${jsonBlock}\nSome trailing text`;
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput, lastResultText: 'no match here' });

    await handleSecurityAudit(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.logs).toBe(jsonBlock);
  });

  it('falls back to lastResultText when neither file nor regex match is found', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 0,
      allOutput: 'no json here',
      lastResultText: 'plain text result',
    });

    await handleSecurityAudit(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.logs).toBe('plain text result');
  });

  it('falls back to truncated allOutput when lastResultText is empty and no regex match', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 0,
      allOutput: 'no json content',
      lastResultText: '',
    });

    await handleSecurityAudit(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.logs).toBe('no json content');
  });

  it('repairs clean-exit unparseable output once and stores the repaired findings array', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    const repaired = [{ title: 'XSS', severity: 'high', fix_prompt: 'Escape output' }];
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: 'plain text', lastResultText: 'no json' })
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: JSON.stringify(repaired) });

    await handleSecurityAudit(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
    expect(call.artifact_type).toBe('SECURITY_REPORT');
    expect(call.logs).toBe(JSON.stringify(repaired));
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(2);
    const reopenPrompt = vi.mocked(runClaudeCode).mock.calls[1][0];
    expect(reopenPrompt).toContain('Output ONLY the JSON array now');
    expect(reopenPrompt).not.toContain('/tmp/');
    expect(reopenPrompt).not.toContain(READ_ONLY_BASH_WRITE_NOTE);
    expect(recordFormatRepairAttempt).toHaveBeenCalledTimes(1);
    expect(recordFormatRepairSuccess).toHaveBeenCalledTimes(1);
  });

  it('sets status COMPLETED and artifact_type SECURITY_REPORT on exitCode 0', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue([]);
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });

    await handleSecurityAudit(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
    expect(call.artifact_type).toBe('SECURITY_REPORT');
  });

  it('sets status FAILED and artifact_type ERROR_LOG on non-zero exitCode', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 1, allOutput: 'err', lastResultText: '' });

    await handleSecurityAudit(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.artifact_type).toBe('ERROR_LOG');
  });
});

// ── handleSelfAudit — findings parsing and artifact emission ─────────────────
describe('handleSelfAudit — findings ingestion', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
  });

  it('persists findings and emits SELF_AUDIT_REPORT when readHandlerOutput returns a structured object', async () => {
    const findings = {
      findings: [{
        system_id: 'sys-auth',
        score: 72,
        components: { test_coverage: 80, friction_density: 65, claim_consistency: 75, failure_mix: 70 },
        top_issues: ['Missing integration tests'],
        recommended_mode: 'fundamentals',
        evidence: [{ file: 'backend/app/auth.py', summary: 'no tests' }],
      }],
    };
    vi.mocked(readHandlerOutput).mockReturnValue(findings);

    await handleSelfAudit(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
    expect(call.artifact_type).toBe('SELF_AUDIT_REPORT');
    expect(call.logs).toBe(JSON.stringify(findings));
  });

  it('extracts JSON object from allOutput when readHandlerOutput returns null', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    const jsonBlock = '{"findings":[{"system_id":"sys-x","score":50,"recommended_mode":"audit"}]}';
    const allOutput = `preamble\n${jsonBlock}\ntrailer`;
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput, lastResultText: 'no signal' });

    await handleSelfAudit(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    // We re-stringify after parsing, so just verify the resulting JSON has the same shape
    const stored = JSON.parse(call.logs as string) as { findings: Array<{ system_id: string }> };
    expect(stored.findings).toHaveLength(1);
    expect(stored.findings[0].system_id).toBe('sys-x');
    expect(call.artifact_type).toBe('SELF_AUDIT_REPORT');
  });

  it('falls back to wrapping a bare JSON array in {findings: [...]}', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    const arrBlock = '[{"system_id":"sys-y","score":80,"recommended_mode":"ship"}]';
    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 0, allOutput: `noise ${arrBlock} more noise`, lastResultText: '',
    });

    await handleSelfAudit(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    const stored = JSON.parse(call.logs as string) as { findings: Array<{ system_id: string }> };
    expect(stored.findings).toHaveLength(1);
    expect(stored.findings[0].system_id).toBe('sys-y');
  });

  it('repairs self-audit object output and preserves bare-array wrapping after the retry', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    const repaired = [{ system_id: 'sys-z', score: 60, recommended_mode: 'fundamentals' }];
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: 'not json', lastResultText: '' })
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: JSON.stringify(repaired) });

    await handleSelfAudit(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    const stored = JSON.parse(call.logs as string) as { findings: Array<{ system_id: string }> };
    expect(call.status).toBe('COMPLETED');
    expect(call.artifact_type).toBe('SELF_AUDIT_REPORT');
    expect(stored.findings[0].system_id).toBe('sys-z');
    const reopenPrompt = vi.mocked(runClaudeCode).mock.calls[1][0];
    expect(reopenPrompt).toContain('Output ONLY the JSON object now');
    expect(reopenPrompt).not.toContain('/tmp/');
    expect(reopenPrompt).not.toContain(READ_ONLY_BASH_WRITE_NOTE);
    expect(recordFormatRepairAttempt).toHaveBeenCalledTimes(1);
    expect(recordFormatRepairSuccess).not.toHaveBeenCalled();
  });

  it('sets status FAILED and ERROR_LOG on non-zero exitCode', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 1, allOutput: 'crash', lastResultText: '' });

    await handleSelfAudit(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.artifact_type).toBe('ERROR_LOG');
    expect(call.failure_category).toBe('claude_code_failed');
  });

  it('embeds focus_systems list into the prompt when provided', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue({ findings: [] });

    await handleSelfAudit(
      TASK_ID,
      { ...BASE_PAYLOAD, focus_systems: ['sys-auth', 'sys-billing'], max_systems: 4 },
      BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );

    const promptArg = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(promptArg).toContain('sys-auth');
    expect(promptArg).toContain('sys-billing');
    expect(promptArg).toContain('Audit ONLY these systems');
  });
});

// ── handleReleaseAudit — pinned-candidate clone + diff-range pre-flight ───────
//
// C2/C4: a release audit must score ONE PINNED candidate commit. The bare
// branch name `automain` does NOT resolve as a revision after cloning `main`
// (`git fetch origin automain` only updates FETCH_HEAD, never a local ref), so
// a `main...automain` range can never resolve and the audit would ALWAYS fail.
// The fix: the backend passes a full 40-char `candidate_sha` in the task
// payload; the handler (1) clones `main` with FULL history (depth 0), (2)
// fetches the EXACT candidate commit (`git fetch origin <sha>`), (3) pre-flight
// `git rev-parse --verify <sha>^{commit}` + `git rev-list --count
// main...<sha>` and FAILS LOUD if the range does not resolve.
describe('handleReleaseAudit — pinned candidate + diff-range pre-flight', () => {
  // A full 40-char hex SHA (the backend pins automain HEAD this way).
  const CANDIDATE_SHA = 'a1b2c3d4e5f60718293a4b5c6d7e8f9012345678';
  const DIFF_RANGE = `main...${CANDIDATE_SHA}`;
  const RELEASE_PAYLOAD = { base_branch: 'main', candidate_sha: CANDIDATE_SHA };

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
    // git() mock returns '' (success) by default — rev-parse + rev-list pass.
    vi.mocked(git).mockReturnValue('');
    vi.mocked(readHandlerOutput).mockReturnValue({
      audit_score: 1,
      test_erosion: { deleted_test_files: [], assertion_delta: 0, test_count_delta: 0 },
      findings: [],
    });
    // execFileSync (child_process) — git fetch succeeds by default.
    vi.mocked(execFileSync).mockReturnValue(Buffer.from(''));
  });

  it('clones main with FULL history (depth 0), not a shallow DEFAULT_CLONE_DEPTH clone', async () => {
    await handleReleaseAudit(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(vi.mocked(prepareWorkspaceForRole)).toHaveBeenCalledTimes(1);
    const opts = vi.mocked(prepareWorkspaceForRole).mock.calls[0][0];
    expect(opts.branch).toBe('main');
    expect(opts.baseBranch).toBe('main');
    // depth 0 → full clone (no --depth flag). A shallow clone would leave the
    // merge-base unreachable for a long-diverged candidate.
    expect(opts.depth).toBe(0);
  });

  it('fetches the EXACT candidate SHA — not the bare `automain` branch name', async () => {
    await handleReleaseAudit(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const fetchCalls = vi.mocked(execFileSync).mock.calls.filter(
      c => c[0] === 'git' && Array.isArray(c[1]) && (c[1] as string[])[0] === 'fetch',
    );
    expect(fetchCalls.length).toBeGreaterThan(0);
    const fetchArgs = fetchCalls[0][1] as string[];
    // `git fetch origin <candidate_sha>` — a SHA fetch leaves a real commit
    // object behind, which `main...<sha>` can resolve. A bare-branch fetch
    // (`git fetch origin automain`) only updates FETCH_HEAD and is the bug.
    expect(fetchArgs).toEqual(['fetch', 'origin', CANDIDATE_SHA]);
    expect(fetchArgs).not.toContain('automain');
  });

  it('reads candidate_sha from task.context as a fallback when absent from the payload', async () => {
    // The backend may stamp candidate_sha under task.context; the handler must
    // still resolve it (the contract is "from the payload", context is a
    // defensive fallback so the handler keeps working either way).
    const payloadNoSha = { base_branch: 'main' };
    const taskWithCtx = { ...BASE_TASK, context: { candidate_sha: CANDIDATE_SHA } };

    await handleReleaseAudit(TASK_ID, payloadNoSha, taskWithCtx, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const fetchCalls = vi.mocked(execFileSync).mock.calls.filter(
      c => c[0] === 'git' && Array.isArray(c[1]) && (c[1] as string[])[0] === 'fetch',
    );
    expect((fetchCalls[0][1] as string[])).toEqual(['fetch', 'origin', CANDIDATE_SHA]);
    // The agent runs exactly once: the describe-level beforeEach stubs
    // readHandlerOutput with a valid report, so the report parses on the first
    // pass and no bounded self-repair re-ask fires.
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(1);
  });

  it('runs the agent when the diff range resolves (rev-parse + rev-list succeed)', async () => {
    vi.mocked(git).mockImplementation((args: string[]) => {
      if (args[0] === 'rev-list') return '137'; // range resolves: 137 commits
      return '';
    });

    await handleReleaseAudit(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    // Pre-flight verified the candidate SHA is a reachable commit object.
    const revParseCall = vi.mocked(git).mock.calls.find(c => c[0][0] === 'rev-parse');
    expect(revParseCall).toBeDefined();
    expect(revParseCall?.[0]).toContain(`${CANDIDATE_SHA}^{commit}`);
    // Pre-flight ran `git rev-list --count main...<candidate_sha>`.
    const revListCall = vi.mocked(git).mock.calls.find(c => c[0][0] === 'rev-list');
    expect(revListCall).toBeDefined();
    expect(revListCall?.[0]).toContain('--count');
    expect(revListCall?.[0]).toContain(DIFF_RANGE);
    // Agent runs exactly once: readHandlerOutput is stubbed with a valid report
    // (describe beforeEach), so the report parses and no self-repair re-ask fires.
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(1);
  });

  it('interpolates main...<candidate_sha> into the agent prompt — never the bare automain name', async () => {
    await handleReleaseAudit(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const prompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    // The diff commands the agent is told to run must use the pinned SHA.
    expect(prompt).toContain(`git diff ${DIFF_RANGE}`);
    expect(prompt).toContain(`git diff --stat ${DIFF_RANGE}`);
    expect(prompt).toContain(CANDIDATE_SHA);
    // The prompt must NOT instruct the agent to diff the bare branch name.
    expect(prompt).not.toContain('git diff main...automain');
  });

  it('FAILS LOUD with diff_range_unresolvable when no valid candidate_sha is supplied', async () => {
    // Missing candidate_sha is a real contract bug — there is no commit to
    // diff. The handler must not fall back to the bare `automain` name.
    await handleReleaseAudit(TASK_ID, { base_branch: 'main' }, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(vi.mocked(prepareWorkspaceForRole)).not.toHaveBeenCalled();
    expect(vi.mocked(runClaudeCode)).not.toHaveBeenCalled();
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.artifact_type).toBe('ERROR_LOG');
    expect(call.failure_category).toBe('diff_range_unresolvable');
    expect(call.logs).toContain('candidate_sha');
  });

  it('FAILS LOUD with diff_range_unresolvable when candidate_sha is malformed (not 40-char hex)', async () => {
    // A truncated SHA, branch name, or garbage value must be rejected — only a
    // full 40-char hex SHA is a valid pinned candidate.
    await handleReleaseAudit(
      TASK_ID, { base_branch: 'main', candidate_sha: 'automain' }, BASE_TASK,
      GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );

    expect(vi.mocked(runClaudeCode)).not.toHaveBeenCalled();
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.failure_category).toBe('diff_range_unresolvable');
    // The rejected value is surfaced for debuggability.
    expect(call.logs).toContain('automain');
  });

  it('FAILS LOUD with diff_range_unresolvable when git rev-list does not resolve the range', async () => {
    // Merge-base unreachable → `git rev-list --count main...<sha>` errors.
    vi.mocked(git).mockImplementation((args: string[]) => {
      if (args[0] === 'rev-list') {
        throw new Error(`fatal: Invalid symmetric difference expression ${DIFF_RANGE}`);
      }
      return '';
    });

    await handleReleaseAudit(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    // The agent must NOT run — it could misread a diff error as "empty diff"
    // and emit a false audit_score:1.0.
    expect(vi.mocked(runClaudeCode)).not.toHaveBeenCalled();
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.artifact_type).toBe('ERROR_LOG');
    expect(call.failure_category).toBe('diff_range_unresolvable');
    expect(call.logs).toContain(DIFF_RANGE);
    expect(call.logs).toContain('merge-base');
  });

  it('FAILS LOUD with diff_range_unresolvable when rev-parse cannot verify the candidate commit', async () => {
    // The SHA fetch reported success but the object is not actually a reachable
    // commit → `git rev-parse --verify <sha>^{commit}` errors. Fail loud.
    vi.mocked(git).mockImplementation((args: string[]) => {
      if (args[0] === 'rev-parse') {
        throw new Error('fatal: Needed a single revision');
      }
      return '';
    });

    await handleReleaseAudit(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(vi.mocked(runClaudeCode)).not.toHaveBeenCalled();
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.failure_category).toBe('diff_range_unresolvable');
  });

  it('FAILS LOUD when the candidate-SHA fetch fails', async () => {
    // The candidate commit cannot be fetched → range cannot resolve. The
    // handler must fail loud, not run the agent.
    vi.mocked(execFileSync).mockImplementation((_cmd: unknown, args: unknown) => {
      const a = args as string[];
      if (Array.isArray(a) && a[0] === 'fetch') {
        throw new Error('fatal: could not read from remote repository');
      }
      return Buffer.from('');
    });

    await handleReleaseAudit(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(vi.mocked(runClaudeCode)).not.toHaveBeenCalled();
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.failure_category).toBe('diff_range_unresolvable');
  });

  it('does not run the rev-list pre-flight at all when the candidate fetch failed', async () => {
    vi.mocked(execFileSync).mockImplementation((_cmd: unknown, args: unknown) => {
      const a = args as string[];
      if (Array.isArray(a) && a[0] === 'fetch') throw new Error('network down');
      return Buffer.from('');
    });

    await handleReleaseAudit(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    // No rev-list call — the fetch already failed so there is nothing to check.
    const revListCall = vi.mocked(git).mock.calls.find(c => c[0][0] === 'rev-list');
    expect(revListCall).toBeUndefined();
  });

  it('emits RELEASE_AUDIT_REPORT preserving the audit_score/test_erosion artifact contract on success', async () => {
    // The qualifier's _score_audit reads audit_score + test_erosion.{deleted_test_files,
    // assertion_delta, test_count_delta}. The handler must pass that shape through.
    const report = {
      audit_score: 0.82,
      test_erosion: { deleted_test_files: [], assertion_delta: 9, test_count_delta: 2 },
      findings: [{ severity: 'medium', summary: 'x', file: 'a.py' }],
    };
    vi.mocked(readHandlerOutput).mockReturnValue(report);

    await handleReleaseAudit(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
    expect(call.artifact_type).toBe('RELEASE_AUDIT_REPORT');
    const stored = JSON.parse(call.logs as string) as typeof report;
    expect(stored.audit_score).toBe(0.82);
    expect(stored.test_erosion.deleted_test_files).toEqual([]);
    expect(stored.test_erosion.assertion_delta).toBe(9);
    expect(stored.test_erosion.test_count_delta).toBe(2);
  });

  it('fails loudly with release_audit_report_unparseable when the report cannot be parsed', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: 'no json object here',
        lastResultText: 'raw tail fallback text',
      })
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: 'still no json object',
        lastResultText: 'still raw text',
      });

    await handleReleaseAudit(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.artifact_type).toBe('ERROR_LOG');
    expect(call.failure_category).toBe('release_audit_report_unparseable');
    expect(call.logs).toContain('raw tail fallback text');
    expect(recordFormatRepairAttempt).toHaveBeenCalledTimes(1);
    expect(recordFormatRepairSuccess).not.toHaveBeenCalled();
  });

  it('release audit recovers from clean-exit parse failure via bounded re-ask instead of failing', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    vi.mocked(execFileSync).mockImplementation((_cmd: unknown, args: unknown) => {
      const a = args as string[];
      if (Array.isArray(a) && a[0] === 'diff' && a[1] === '--name-status') return '';
      if (Array.isArray(a) && a[0] === 'diff') return '+ expect(value).toBe(true)\n';
      return Buffer.from('');
    });
    const repaired = {
      audit_score: 0.91,
      test_erosion: { deleted_test_files: [], assertion_delta: 4, test_count_delta: 1 },
      findings: [],
    };
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: 'release audit complete but prose only',
        lastResultText: 'raw tail fallback text',
      })
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '',
        lastResultText: JSON.stringify(repaired),
      });

    await handleReleaseAudit(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    const stored = JSON.parse(call.logs as string) as typeof repaired & { computed_test_erosion?: unknown };
    expect(call.status).toBe('COMPLETED');
    expect(call.artifact_type).toBe('RELEASE_AUDIT_REPORT');
    expect(call.failure_category).toBeUndefined();
    expect(stored.audit_score).toBe(0.91);
    expect(stored.findings).toEqual([]);
    expect(stored.computed_test_erosion).toBeDefined();
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(2);
    expect(vi.mocked(runClaudeCode).mock.calls[1][0]).toContain('Output ONLY the JSON object now');
    expect(vi.mocked(runClaudeCode).mock.calls[1][0]).not.toContain('/tmp/');
    expect(recordFormatRepairAttempt).toHaveBeenCalledTimes(1);
    expect(recordFormatRepairSuccess).toHaveBeenCalledTimes(1);
  });

  it('prompt includes verification_note in the findings JSON schema', async () => {
    await handleReleaseAudit(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(prompt).toContain('verification_note');
  });

  it('prompt instructs agent to read full-repo context before recording a high/medium/critical finding', async () => {
    await handleReleaseAudit(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    // The prompt must require whole-repo verification, not just diff inspection.
    expect(prompt).toMatch(/substantiation required/i);
    expect(prompt).toMatch(/read the relevant unchanged files/i);
  });

  describe('release_audit prompt false-positive disclaimers', () => {
    it('guards the hardcoded-constant-is-not-injection disclaimer', async () => {
      await handleReleaseAudit(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
      const prompt = vi.mocked(runClaudeCode).mock.calls[0][0];
      // This disclaimer protects against the false positive: a hardcoded value in the
      // source flowing to a sink (e.g. hardcoded SQL fragment reaching a query) is NOT
      // injection if the hardcoded value is the only possible input.
      expect(prompt).toContain('A hardcoded constant reaching a sink is NOT injection.');
    });

    it('guards the per-request-check-is-not-replay-gap disclaimer', async () => {
      await handleReleaseAudit(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
      const prompt = vi.mocked(runClaudeCode).mock.calls[0][0];
      // This disclaimer protects against the false positive: a per-request check
      // (e.g. token validation) is NOT a token-replay vulnerability when the token
      // is genuinely validated on each request.
      expect(prompt).toContain('A per-request DB check is NOT a replay gap.');
    });

    it('guards the exception-guarded-comparison-is-not-type-error disclaimer', async () => {
      await handleReleaseAudit(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
      const prompt = vi.mocked(runClaudeCode).mock.calls[0][0];
      // This disclaimer protects against the false positive: a comparison operation
      // guarded by an exception handler (e.g. try/except or if type check) is NOT
      // a TypeError vulnerability.
      expect(prompt).toContain('A comparison that is exception-guarded is NOT a TypeError.');
    });

    it('guards the verification-note exclusion contract for unsubstantiated findings', async () => {
      await handleReleaseAudit(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
      const prompt = vi.mocked(runClaudeCode).mock.calls[0][0];
      // This contract is critical: high/medium/critical findings without a substantive
      // verification_note are silently excluded from the score. If this contract is
      // dropped, unsubstantiated findings could cause false-positive audit_score collapses.
      expect(prompt).toContain('Findings at severity high/medium/critical WITHOUT a substantive `verification_note` are silently excluded from the score');
    });

    it('guards the substantiation requirement and whole-repo context guidance', async () => {
      await handleReleaseAudit(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
      const prompt = vi.mocked(runClaudeCode).mock.calls[0][0];
      // The substantiation rule must remain: before recording high/medium/critical findings,
      // the agent must read unchanged files to check for mitigations.
      expect(prompt).toContain('Before recording any finding at severity high, medium, or critical, read the relevant unchanged files in the full repository');
      expect(prompt).toContain('check for existing mitigations');
    });
  });

  // ── TEST_PATH_RE gap-1: root-level test directories ────────────────────────
  describe('computed_test_erosion — TEST_PATH_RE root-level path detection', () => {
    beforeEach(() => vi.clearAllMocks());

    it('detects a deleted file under a root-level testing/ directory (no leading slash)', async () => {
      // "testing/test_foo.py" has no leading slash — the old regex required one.
      vi.mocked(execFileSync).mockImplementation((_cmd: unknown, args: unknown) => {
        const a = args as string[];
        if (Array.isArray(a) && a[0] === 'diff' && a[1] === '--name-status') {
          return 'D\ttesting/test_foo.py\n';
        }
        if (Array.isArray(a) && a[0] === 'diff') return '';
        return Buffer.from('');
      });
      vi.mocked(readHandlerOutput).mockReturnValue({
        audit_score: 1,
        test_erosion: { deleted_test_files: [], assertion_delta: 0, test_count_delta: 0 },
        findings: [],
      });

      await handleReleaseAudit(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

      const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
      const stored = JSON.parse(call.logs as string) as { computed_test_erosion?: { deleted_test_files: string[] } };
      expect(stored.computed_test_erosion?.deleted_test_files).toContain('testing/test_foo.py');
    });

    it('detects a deleted file under a root-level __tests__/ directory (no leading slash)', async () => {
      vi.mocked(execFileSync).mockImplementation((_cmd: unknown, args: unknown) => {
        const a = args as string[];
        if (Array.isArray(a) && a[0] === 'diff' && a[1] === '--name-status') {
          return 'D\t__tests__/foo.test.ts\n';
        }
        if (Array.isArray(a) && a[0] === 'diff') return '';
        return Buffer.from('');
      });
      vi.mocked(readHandlerOutput).mockReturnValue({
        audit_score: 1,
        test_erosion: { deleted_test_files: [], assertion_delta: 0, test_count_delta: 0 },
        findings: [],
      });

      await handleReleaseAudit(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

      const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
      const stored = JSON.parse(call.logs as string) as { computed_test_erosion?: { deleted_test_files: string[] } };
      expect(stored.computed_test_erosion?.deleted_test_files).toContain('__tests__/foo.test.ts');
    });
  });

  // ── Gap-2: it(/describe( double-counting ──────────────────────────────────
  describe('computed_test_erosion — no double-counting of it(/describe( lines', () => {
    beforeEach(() => vi.clearAllMocks());

    it('an added it( line increments test_count_delta but not assertion_delta', async () => {
      vi.mocked(execFileSync).mockImplementation((_cmd: unknown, args: unknown) => {
        const a = args as string[];
        if (Array.isArray(a) && a[0] === 'diff' && a[1] === '--name-status') return '';
        if (Array.isArray(a) && a[0] === 'diff') {
          // A single added it( line — should only count toward testCountDelta.
          return "+  it('does something', () => {\n";
        }
        return Buffer.from('');
      });
      vi.mocked(readHandlerOutput).mockReturnValue({
        audit_score: 1,
        test_erosion: { deleted_test_files: [], assertion_delta: 0, test_count_delta: 0 },
        findings: [],
      });

      await handleReleaseAudit(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

      const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
      const stored = JSON.parse(call.logs as string) as {
        computed_test_erosion?: { assertion_delta: number; test_count_delta: number };
      };
      // it( is a test-function declaration, not an assertion.
      expect(stored.computed_test_erosion?.test_count_delta).toBe(1);
      expect(stored.computed_test_erosion?.assertion_delta).toBe(0);
    });
  });

  // ── Gap-3: authorized deletion lines excluded from graded penalty ──────────
  describe('computed_test_erosion — deleted-file lines excluded from delta', () => {
    beforeEach(() => vi.clearAllMocks());

    it('lines removed from a deleted test file do not penalize assertion_delta or test_count_delta', async () => {
      // The deleted file contains assertion/test lines that would make delta negative
      // if counted — but those lines belong to the hard-blocker dimension, not the
      // graded-penalty dimension.
      const deletedFileDiff = [
        'diff --git a/testing/test_gone.py b/testing/test_gone.py',
        '--- a/testing/test_gone.py',
        '+++ /dev/null',
        '-def test_something():',
        '-    assert result == 42',
        '-    assert other == True',
      ].join('\n') + '\n';

      vi.mocked(execFileSync).mockImplementation((_cmd: unknown, args: unknown) => {
        const a = args as string[];
        if (Array.isArray(a) && a[0] === 'diff' && a[1] === '--name-status') {
          return 'D\ttesting/test_gone.py\n';
        }
        if (Array.isArray(a) && a[0] === 'diff') return deletedFileDiff;
        return Buffer.from('');
      });
      vi.mocked(readHandlerOutput).mockReturnValue({
        audit_score: 1,
        test_erosion: { deleted_test_files: [], assertion_delta: 0, test_count_delta: 0 },
        findings: [],
      });

      await handleReleaseAudit(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

      const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
      const stored = JSON.parse(call.logs as string) as {
        computed_test_erosion?: {
          deleted_test_files: string[];
          assertion_delta: number;
          test_count_delta: number;
        };
      };
      // The file must be detected as deleted.
      expect(stored.computed_test_erosion?.deleted_test_files).toContain('testing/test_gone.py');
      // But its removed lines must NOT feed the graded penalty.
      expect(stored.computed_test_erosion?.assertion_delta).toBe(0);
      expect(stored.computed_test_erosion?.test_count_delta).toBe(0);
    });

    it('within-file test removals (not wholly-deleted file) still penalize delta', async () => {
      // A live file that had some tests removed — these should still count.
      const partialDiff = [
        'diff --git a/testing/test_live.py b/testing/test_live.py',
        '--- a/testing/test_live.py',
        '+++ b/testing/test_live.py',
        '-def test_old():',
        '-    assert value == 1',
      ].join('\n') + '\n';

      vi.mocked(execFileSync).mockImplementation((_cmd: unknown, args: unknown) => {
        const a = args as string[];
        if (Array.isArray(a) && a[0] === 'diff' && a[1] === '--name-status') return '';
        if (Array.isArray(a) && a[0] === 'diff') return partialDiff;
        return Buffer.from('');
      });
      vi.mocked(readHandlerOutput).mockReturnValue({
        audit_score: 1,
        test_erosion: { deleted_test_files: [], assertion_delta: 0, test_count_delta: 0 },
        findings: [],
      });

      await handleReleaseAudit(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

      const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
      const stored = JSON.parse(call.logs as string) as {
        computed_test_erosion?: { assertion_delta: number; test_count_delta: number };
      };
      expect(stored.computed_test_erosion?.test_count_delta).toBe(-1);
      expect(stored.computed_test_erosion?.assertion_delta).toBe(-1);
    });
  });
});

// ── resolveCandidateSha — payload/context SHA resolution + validation ─────────
describe('resolveCandidateSha', () => {
  const FULL_SHA = 'a1b2c3d4e5f60718293a4b5c6d7e8f9012345678';

  it('reads a full 40-char SHA from the payload', () => {
    expect(resolveCandidateSha({ candidate_sha: FULL_SHA }, {})).toBe(FULL_SHA);
  });

  it('falls back to task.context.candidate_sha when the payload lacks it', () => {
    expect(resolveCandidateSha({}, { context: { candidate_sha: FULL_SHA } })).toBe(FULL_SHA);
  });

  it('prefers the payload SHA over the context SHA', () => {
    const other = 'ffffffffffffffffffffffffffffffffffffffff';
    expect(
      resolveCandidateSha({ candidate_sha: FULL_SHA }, { context: { candidate_sha: other } }),
    ).toBe(FULL_SHA);
  });

  it('lowercases and trims an uppercase / padded SHA', () => {
    expect(resolveCandidateSha({ candidate_sha: `  ${FULL_SHA.toUpperCase()}  ` }, {})).toBe(FULL_SHA);
  });

  it('returns null for a short SHA, a branch name, or garbage', () => {
    expect(resolveCandidateSha({ candidate_sha: 'a1b2c3d' }, {})).toBeNull();
    expect(resolveCandidateSha({ candidate_sha: 'automain' }, {})).toBeNull();
    expect(resolveCandidateSha({ candidate_sha: 'z'.repeat(40) }, {})).toBeNull(); // non-hex
    expect(resolveCandidateSha({ candidate_sha: '' }, {})).toBeNull();
  });

  it('returns null when neither payload nor context carries a SHA', () => {
    expect(resolveCandidateSha({}, {})).toBeNull();
    expect(resolveCandidateSha({}, { context: {} })).toBeNull();
    expect(resolveCandidateSha({ candidate_sha: 123 }, { context: 'not-an-object' })).toBeNull();
  });
});

// ── Release audit JSON schema contract ───────────────────────────────────────
// Guards the shape contract between the agent prompt and backend score parser.
// If the prompt is rewritten to document 'issues'/'level', these tests break.
describe('handleReleaseAudit — JSON schema field name contract', () => {
  const CANDIDATE_SHA = 'a1b2c3d4e5f60718293a4b5c6d7e8f9012345678';
  const RELEASE_PAYLOAD = { base_branch: 'main', candidate_sha: CANDIDATE_SHA };

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(git).mockReturnValue('');
    vi.mocked(execFileSync).mockReturnValue(Buffer.from(''));
    vi.mocked(readHandlerOutput).mockReturnValue({
      audit_score: 1,
      test_erosion: { deleted_test_files: [], assertion_delta: 0, test_count_delta: 0 },
      findings: [],
    });
  });

  /** Capture the prompt string passed to runClaudeCode by handleReleaseAudit. */
  async function captureReleaseAuditPrompt(): Promise<string> {
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
    await handleReleaseAudit(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const calls = vi.mocked(runClaudeCode).mock.calls;
    if (calls.length === 0) throw new Error('runClaudeCode was not invoked');
    return calls[calls.length - 1][0];
  }

  it('prompt documents "findings" as the top-level array key', async () => {
    const prompt = await captureReleaseAuditPrompt();
    expect(prompt).toContain('"findings"');
  });

  it('prompt documents "severity" as the finding severity field', async () => {
    const prompt = await captureReleaseAuditPrompt();
    expect(prompt).toContain('"severity"');
  });

  it('prompt JSON schema block does not use "issues" as the findings key', async () => {
    const prompt = await captureReleaseAuditPrompt();
    // Extract the fenced JSON block containing audit_score to scope the check.
    const match = prompt.match(/```json\s*([\s\S]*?)```/);
    const block = match ? match[1] : prompt;
    expect(block).not.toContain('"issues"');
  });

  it('prompt JSON schema block does not use "level" as the severity field', async () => {
    const prompt = await captureReleaseAuditPrompt();
    const match = prompt.match(/```json\s*([\s\S]*?)```/);
    const block = match ? match[1] : prompt;
    expect(block).not.toContain('"level"');
  });
});

// ── READ_ONLY_BASH_WRITE_NOTE — prompt inclusion guards ──────────────────────
// Each read-only audit handler must instruct the agent to use Bash to write its
// /tmp output file because Write/Edit tools are disabled. These tests assert the
// shared note appears in the main prompt for each handler, preventing silent
// regression when prompts are refactored.
describe('read-only audit handlers — Bash write note present in prompt', () => {
  const CANDIDATE_SHA = 'a1b2c3d4e5f60718293a4b5c6d7e8f9012345678';

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(git).mockReturnValue('');
    vi.mocked(execFileSync).mockReturnValue(Buffer.from(''));
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
    vi.mocked(readHandlerOutput).mockReturnValue(null);
  });

  it('handleSecurityAudit prompt contains READ_ONLY_BASH_WRITE_NOTE', async () => {
    await handleSecurityAudit(TASK_ID, { base_branch: 'main' }, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(prompt).toContain(READ_ONLY_BASH_WRITE_NOTE);
  });

  it('handleSelfAudit prompt contains READ_ONLY_BASH_WRITE_NOTE', async () => {
    await handleSelfAudit(TASK_ID, { base_branch: 'main' }, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(prompt).toContain(READ_ONLY_BASH_WRITE_NOTE);
  });

  it('handleReleaseAudit prompt contains READ_ONLY_BASH_WRITE_NOTE', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue({
      audit_score: 1,
      test_erosion: { deleted_test_files: [], assertion_delta: 0, test_count_delta: 0 },
      findings: [],
    });
    await handleReleaseAudit(
      TASK_ID, { base_branch: 'main', candidate_sha: CANDIDATE_SHA },
      BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );
    const prompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(prompt).toContain(READ_ONLY_BASH_WRITE_NOTE);
  });
});

// ── audit handlers use distinct schema labels ─────────────────────────────────
describe('audit handlers use distinct schema labels', () => {
  const CANDIDATE_SHA = 'a1b2c3d4e5f60718293a4b5c6d7e8f9012345678';

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('handleSecurityAudit passes security_audit schema label on repair', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    const repaired = [{ title: 'XSS', severity: 'high', fix_prompt: 'Escape output' }];
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: 'not json', lastResultText: '' })
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: JSON.stringify(repaired) });

    await handleSecurityAudit(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(recordFormatRepairAttempt).toHaveBeenCalledWith('security_audit');
  });

  it('handleSelfAudit passes self_audit schema label on repair', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    const repaired = [{ system_id: 'sys-z', score: 60, recommended_mode: 'fundamentals' }];
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: 'not json', lastResultText: '' })
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: JSON.stringify(repaired) });

    await handleSelfAudit(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(recordFormatRepairAttempt).toHaveBeenCalledWith('self_audit');
  });

  it('handleReleaseAudit passes release_audit schema label on repair', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    vi.mocked(execFileSync).mockImplementation((_cmd: unknown, args: unknown) => {
      const a = args as string[];
      if (Array.isArray(a) && a[0] === 'diff' && a[1] === '--name-status') return '';
      if (Array.isArray(a) && a[0] === 'diff') return '+ expect(value).toBe(true)\n';
      return Buffer.from('');
    });
    const repaired = {
      audit_score: 0.91,
      test_erosion: { deleted_test_files: [], assertion_delta: 0, test_count_delta: 0 },
      findings: [],
    };
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: 'not json', lastResultText: '' })
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: JSON.stringify(repaired) });

    await handleReleaseAudit(TASK_ID, { base_branch: 'main', candidate_sha: CANDIDATE_SHA }, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(recordFormatRepairAttempt).toHaveBeenCalledWith('release_audit');
  });
});
