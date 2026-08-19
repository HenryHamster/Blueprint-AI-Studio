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

vi.mock('../workspacePrep.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../workspacePrep.js')>();
  return { ...actual, prepareWorkspaceForRole: vi.fn().mockResolvedValue(undefined) };
});

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

vi.mock('../build-config.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../build-config.js')>();
  return { ...actual, resolveBuildConfig: vi.fn().mockReturnValue(undefined) };
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
  fetchCandidateCommitWithRetry,
} from './audits.js';
import {
  persistAndPostStatus, readHandlerOutput, runClaudeCode, git,
} from '../index.js';
import { recordFormatRepairAttempt, recordFormatRepairSuccess, getLlmTelemetry, resetLlmTelemetry } from '../telemetry.js';
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
  interface AuditReport {
    audit_scope: {
      mode: string;
      effective_base_sha: string | null;
      diff_range: string;
      goal_category_diff_range: string;
      changed_paths: string[];
      fallback_reason: string | null;
    };
    findings: Array<{ blob_sha: string; [key: string]: unknown }>;
    [key: string]: unknown;
  }

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

  it('uses a verified prior clean candidate as the incremental range and explains the scope', async () => {
    const auditBaseSha = '0123456789abcdef0123456789abcdef01234567';
    vi.mocked(execFileSync).mockImplementation((_cmd: unknown, args: unknown) => {
      const a = args as string[];
      if (a[0] === 'diff' && a[1] === '--name-status') return 'M\tbackend/app/main.py\n';
      if (a[0] === 'diff') return '';
      return '';
    });

    await handleReleaseAudit(
      TASK_ID, { ...RELEASE_PAYLOAD, audit_base_sha: auditBaseSha }, BASE_TASK,
      GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );

    const deltaRange = `${auditBaseSha}..${CANDIDATE_SHA}`;
    const prompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(prompt).toContain(`git diff ${deltaRange}`);
    // The goal/category grading range is the FULL version delta even though the
    // code-quality review is scoped to the incremental delta (the backend pins
    // goal_category_diff_range in the payload; the handler falls back to the
    // full base_branch...candidate range when the field is absent).
    expect(prompt).not.toContain(`git diff ${DIFF_RANGE}`);
    const call = vi.mocked(persistAndPostStatus).mock.calls.at(-1)?.[1];
    const report = JSON.parse(call?.logs as string) as AuditReport;
    expect(report.audit_scope).toMatchObject({
      mode: 'incremental',
      effective_base_sha: auditBaseSha,
      diff_range: deltaRange,
      goal_category_diff_range: DIFF_RANGE,
      changed_paths: ['backend/app/main.py'],
      fallback_reason: null,
    });
  });

  it('grades goal alignment and acceptance categories over the FULL version delta even in incremental mode', async () => {
    const auditBaseSha = '0123456789abcdef0123456789abcdef01234567';
    const VERSION_GOAL = 'STABILIZE v28 — make the grader read the full version delta';
    const categories = [
      { category_key: 'goal_alignment', text: 'The candidate advances the version goal' },
      { category_key: 'ship_safety', text: 'Promotion stays safe' },
    ];
    vi.mocked(execFileSync).mockImplementation((_cmd: unknown, args: unknown) => {
      const a = args as string[];
      if (a[0] === 'diff' && a[1] === '--name-status') return 'M\tbackend/app/main.py\n';
      if (a[0] === 'diff') return '';
      return '';
    });

    await handleReleaseAudit(
      TASK_ID, {
        ...RELEASE_PAYLOAD,
        audit_base_sha: auditBaseSha,
        version_goal: VERSION_GOAL,
        categories,
      }, BASE_TASK,
      GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );

    const prompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    const deltaRange = `${auditBaseSha}..${CANDIDATE_SHA}`;
    // The code-quality review stays scoped to the incremental delta.
    expect(prompt).toContain(`git diff ${deltaRange}`);
    // The goal/category grading is explicitly pinned to the FULL version delta
    // (the whole range the promotion PR would carry), not the incremental one.
    expect(prompt).toContain(`FULL version delta (\`git diff ${DIFF_RANGE}\`)`);
    expect(prompt).toContain(`git log ${DIFF_RANGE} --oneline`);
    const call = vi.mocked(persistAndPostStatus).mock.calls.at(-1)?.[1];
    const report = JSON.parse(call?.logs as string) as AuditReport;
    expect(report.audit_scope).toMatchObject({
      mode: 'incremental',
      effective_base_sha: auditBaseSha,
      diff_range: deltaRange,
      goal_category_diff_range: DIFF_RANGE,
    });
  });

  it('grades goal alignment and acceptance categories over a payload-pinned goal_category_diff_range', async () => {
    // The backend explicitly pins the full version delta as
    // payload.goal_category_diff_range; the handler must use THAT range for the
    // goal/category sections even when the code-quality range narrows.
    const auditBaseSha = '0123456789abcdef0123456789abcdef01234567';
    const explicitFullRange = 'main...fedcba98765432100123456789abcdef01234567';
    const VERSION_GOAL = 'Grade the whole version';
    const categories = [{ category_key: 'goal_alignment', text: 'Advances the goal' }];
    vi.mocked(execFileSync).mockImplementation((_cmd: unknown, args: unknown) => {
      const a = args as string[];
      if (a[0] === 'diff' && a[1] === '--name-status') return 'M\tbackend/app/main.py\n';
      if (a[0] === 'diff') return '';
      return '';
    });

    await handleReleaseAudit(
      TASK_ID, {
        ...RELEASE_PAYLOAD,
        audit_base_sha: auditBaseSha,
        goal_category_diff_range: explicitFullRange,
        version_goal: VERSION_GOAL,
        categories,
      }, BASE_TASK,
      GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );

    const prompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    const deltaRange = `${auditBaseSha}..${CANDIDATE_SHA}`;
    expect(prompt).toContain(`git diff ${deltaRange}`);
    expect(prompt).toContain(`FULL version delta (\`git diff ${explicitFullRange}\`)`);
    expect(prompt).not.toContain(`git diff ${DIFF_RANGE}`);
    const call = vi.mocked(persistAndPostStatus).mock.calls.at(-1)?.[1];
    const report = JSON.parse(call?.logs as string) as AuditReport;
    expect(report.audit_scope.goal_category_diff_range).toBe(explicitFullRange);
    expect(report.audit_scope.diff_range).toBe(deltaRange);
  });

  it('emits the acceptance-category contract when no version goal is set', async () => {
    // A version with an ACTIVE acceptance-criteria rubric but NO version goal
    // string must still be graded per-category: the backend always ships
    // payload.categories, and the qualifier's goal-verification gate IS the
    // acceptance contract.  The category section must not be dropped just
    // because versionGoalSection is empty.
    const categories = [
      { category_key: 'goal_alignment', text: 'The candidate advances the version goal' },
      { category_key: 'ship_safety', text: 'Promotion stays safe' },
    ];
    vi.mocked(execFileSync).mockImplementation((_cmd: unknown, args: unknown) => {
      const a = args as string[];
      if (a[0] === 'diff' && a[1] === '--name-status') return 'M\tbackend/app/main.py\n';
      if (a[0] === 'diff') return '';
      return '';
    });

    await handleReleaseAudit(
      TASK_ID, {
        ...RELEASE_PAYLOAD,
        categories,
        // no version_goal supplied
      }, BASE_TASK,
      GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );

    const prompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    // The category contract is emitted even without a goal string...
    expect(prompt).toContain('Acceptance categories:');
    expect(prompt).toContain('- goal_alignment: The candidate advances the version goal');
    expect(prompt).toContain('- ship_safety: Promotion stays safe');
    // ...grading the FULL version delta (the whole range the promotion PR would
    // carry), with the per-category verdict requirements the backend gates on.
    expect(prompt).toContain(`FULL version delta (\`git diff ${DIFF_RANGE}\`)`);
    expect(prompt).toContain('Emit one `category_verdicts` entry for every supplied key');
    expect(prompt).toContain('`status` (`met` | `partial` | `not_met`)');
    expect(prompt).toContain('residual_uncaptured_gap');
    // The version-goal section is absent — there is no goal string to grade.
    expect(prompt).not.toContain('## Version Goal');
    expect(prompt).not.toContain('Emit `covers_goal`');
    // The report still emits the audit artifact normally.
    const call = vi.mocked(persistAndPostStatus).mock.calls.at(-1)?.[1];
    expect(call?.status).toBe('COMPLETED');
    expect(call?.artifact_type).toBe('RELEASE_AUDIT_REPORT');
  });

  it('repair re-ask requests category_verdicts when categories exist without a version goal', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    const categories = [{ category_key: 'goal_alignment', text: 'Advances the goal' }];
    vi.mocked(execFileSync).mockImplementation((_cmd: unknown, args: unknown) => {
      const a = args as string[];
      if (a[0] === 'diff' && a[1] === '--name-status') return 'M\tbackend/app/main.py\n';
      if (a[0] === 'diff') return '';
      return '';
    });
    const repaired = {
      audit_score: 0.91,
      test_erosion: { deleted_test_files: [], assertion_delta: 0, test_count_delta: 0 },
      category_verdicts: [
        { category_key: 'goal_alignment', status: 'met', citations: [{ kind: 'diff', ref: 'main.py:1' }] },
      ],
      residual_uncaptured_gap: null,
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

    await handleReleaseAudit(
      TASK_ID, {
        ...RELEASE_PAYLOAD,
        categories,
      }, BASE_TASK,
      GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );

    const reopenPrompt = vi.mocked(runClaudeCode).mock.calls[1][0];
    // The repair re-ask must still demand the per-category section even though
    // there is no version goal (no covers_goal/coverage_score clause).
    expect(reopenPrompt).toContain('Output ONLY the JSON object now');
    expect(reopenPrompt).toContain('"category_verdicts" and "residual_uncaptured_gap" from the acceptance-category review');
    expect(reopenPrompt).not.toContain('covers_goal');
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
  });

  it('falls back to the full range when the requested baseline is not an ancestor', async () => {
    const auditBaseSha = '0123456789abcdef0123456789abcdef01234567';
    vi.mocked(execFileSync).mockImplementation((_cmd: unknown, args: unknown) => {
      const a = args as string[];
      if (a[0] === 'merge-base') throw new Error('not an ancestor');
      if (a[0] === 'diff' && a[1] === '--name-status') return '';
      if (a[0] === 'diff') return '';
      return '';
    });

    await handleReleaseAudit(
      TASK_ID, { ...RELEASE_PAYLOAD, audit_base_sha: auditBaseSha }, BASE_TASK,
      GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );

    expect(vi.mocked(runClaudeCode).mock.calls[0][0]).toContain(`git diff ${DIFF_RANGE}`);
    const call = vi.mocked(persistAndPostStatus).mock.calls.at(-1)?.[1];
    const report = JSON.parse(call?.logs as string) as AuditReport;
    expect(report.audit_scope.mode).toBe('full');
    expect(report.audit_scope.effective_base_sha).toBeNull();
    expect(report.audit_scope.fallback_reason).toContain('not an ancestor');
  });

  it('attaches the candidate blob SHA to path findings', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue({
      audit_score: 0.98,
      findings: [{ severity: 'low', summary: 'cache issue', file: 'backend/app/cache.py' }],
    });
    vi.mocked(execFileSync).mockImplementation((_cmd: unknown, args: unknown) => {
      const a = args as string[];
      if (a[0] === 'rev-parse' && a[1] === `${CANDIDATE_SHA}:backend/app/cache.py`) {
        return `${'c'.repeat(40)}\n`;
      }
      if (a[0] === 'diff') return '';
      return '';
    });

    await handleReleaseAudit(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls.at(-1)?.[1];
    const report = JSON.parse(call?.logs as string) as AuditReport;
    expect(report.findings[0].blob_sha).toBe('c'.repeat(40));
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

  it('RETRIES a transient candidate fetch and proceeds to audit once it succeeds', async () => {
    // A single transient GitHub I/O blip on `git fetch origin <sha>` must NOT
    // torch the qualification attempt: the fetch is retried and the audit runs.
    let fetchAttempts = 0;
    vi.mocked(execFileSync).mockImplementation((_cmd: unknown, args: unknown) => {
      const a = args as string[];
      if (Array.isArray(a) && a[0] === 'fetch') {
        fetchAttempts += 1;
        if (fetchAttempts < 2) throw new Error('fatal: could not read from remote repository');
      }
      return Buffer.from('');
    });

    await handleReleaseAudit(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    // The transient first attempt was retried; the second succeeded.
    expect(fetchAttempts).toBe(2);
    const fetchCalls = vi.mocked(execFileSync).mock.calls.filter(
      c => c[0] === 'git' && Array.isArray(c[1]) && (c[1] as string[])[0] === 'fetch',
    );
    expect(fetchCalls.length).toBe(2);
    // The agent ran — the blip did not fail the audit loud.
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(1);
    const failedCall = vi.mocked(persistAndPostStatus).mock.calls.find(
      c => c[1].failure_category === 'diff_range_unresolvable',
    );
    expect(failedCall).toBeUndefined();
  });

  it('attempts the candidate fetch 3 times before failing loud (retries exhausted)', async () => {
    // The fail-loud diff_range_unresolvable safety property survives the retry:
    // after all bounded attempts blip out, the audit still refuses to run.
    let fetchAttempts = 0;
    vi.mocked(execFileSync).mockImplementation((_cmd: unknown, args: unknown) => {
      const a = args as string[];
      if (Array.isArray(a) && a[0] === 'fetch') {
        fetchAttempts += 1;
        throw new Error('network down');
      }
      return Buffer.from('');
    });

    await handleReleaseAudit(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(fetchAttempts).toBe(3);
    expect(vi.mocked(runClaudeCode)).not.toHaveBeenCalled();
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.failure_category).toBe('diff_range_unresolvable');
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

  // ── Gap-3 (corrected): a wholly-deleted test file's lines DO count ─────────
  // The exclusion here used to be "the hard-blocker gate already owns those
  // files" — but that gate is fail-open (only an affirmative genuine_erosion
  // LLM tag blocks), so any deleted test file the auditor doesn't flag was
  // invisible to BOTH the hard blocker and the graded penalty. A mass test
  // deletion must take the (capped) graded penalty like any other removal;
  // a genuine split/rename nets ~0 because its additions offset the removal.
  describe('computed_test_erosion — deleted-file lines count toward delta', () => {
    beforeEach(() => vi.clearAllMocks());

    it('lines removed from a wholly-deleted test file DO penalize assertion_delta and test_count_delta', async () => {
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
      // Its removed lines feed the graded penalty like any other removal:
      // 1 removed `def test_something(` and 2 removed assertions.
      expect(stored.computed_test_erosion?.assertion_delta).toBe(-2);
      expect(stored.computed_test_erosion?.test_count_delta).toBe(-1);
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

  // ── co-deletion classification — deterministic dead_code_removal ───────────
  describe('computed_test_erosion — co-deletion classifications', () => {
    beforeEach(() => vi.clearAllMocks());

    it('emits dead_code_removal classification when test and subject are both D-lines (frontend .tsx)', async () => {
      vi.mocked(execFileSync).mockImplementation((_cmd: unknown, args: unknown) => {
        const a = args as string[];
        if (Array.isArray(a) && a[0] === 'diff' && a[1] === '--name-status') {
          return 'D\tadmin-ui/src/components/Foo.tsx\nD\tadmin-ui/src/components/Foo.test.tsx\n';
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
      const stored = JSON.parse(call.logs as string) as {
        computed_test_erosion?: {
          deleted_test_files: string[];
          deleted_test_file_classifications?: { path: string; classification: string; rationale: string }[];
        };
      };
      const cls = stored.computed_test_erosion?.deleted_test_file_classifications;
      expect(cls).toBeDefined();
      const entry = cls?.find(c => c.path === 'admin-ui/src/components/Foo.test.tsx');
      expect(entry?.classification).toBe('dead_code_removal');
    });

    it('emits dead_code_removal for viewport test when subject tsx is also deleted', async () => {
      vi.mocked(execFileSync).mockImplementation((_cmd: unknown, args: unknown) => {
        const a = args as string[];
        if (Array.isArray(a) && a[0] === 'diff' && a[1] === '--name-status') {
          return 'D\tadmin-ui/src/components/Bar.tsx\nD\tadmin-ui/src/components/Bar.viewport.test.tsx\n';
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
      const stored = JSON.parse(call.logs as string) as {
        computed_test_erosion?: {
          deleted_test_file_classifications?: { path: string; classification: string }[];
        };
      };
      const entry = stored.computed_test_erosion?.deleted_test_file_classifications?.find(
        c => c.path === 'admin-ui/src/components/Bar.viewport.test.tsx'
      );
      expect(entry?.classification).toBe('dead_code_removal');
    });

    it('emits NO classification when test is deleted but subject tsx is still present', async () => {
      vi.mocked(execFileSync).mockImplementation((_cmd: unknown, args: unknown) => {
        const a = args as string[];
        if (Array.isArray(a) && a[0] === 'diff' && a[1] === '--name-status') {
          return 'D\tadmin-ui/src/components/Foo.test.tsx\n';
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
      const stored = JSON.parse(call.logs as string) as {
        computed_test_erosion?: {
          deleted_test_file_classifications?: { path: string; classification: string }[];
        };
      };
      const cls = stored.computed_test_erosion?.deleted_test_file_classifications;
      const entry = cls?.find(c => c.path === 'admin-ui/src/components/Foo.test.tsx');
      expect(entry).toBeUndefined();
    });

    it('emits dead_code_removal for backend test when subject module is co-deleted', async () => {
      vi.mocked(execFileSync).mockImplementation((_cmd: unknown, args: unknown) => {
        const a = args as string[];
        if (Array.isArray(a) && a[0] === 'diff' && a[1] === '--name-status') {
          return 'D\ttesting/test_bar.py\nD\tapp/bar.py\n';
        }
        if (Array.isArray(a) && a[0] === 'diff') return '';
        return Buffer.from('');
      });
      vi.mocked(readHandlerOutput).mockReturnValue({
        audit_score: 1,
        test_erosion: { deleted_test_files: [], assertion_delta: 0, test_count_count: 0 },
        findings: [],
      });

      await handleReleaseAudit(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

      const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
      const stored = JSON.parse(call.logs as string) as {
        computed_test_erosion?: {
          deleted_test_file_classifications?: { path: string; classification: string }[];
        };
      };
      const entry = stored.computed_test_erosion?.deleted_test_file_classifications?.find(
        c => c.path === 'testing/test_bar.py'
      );
      expect(entry?.classification).toBe('dead_code_removal');
    });
  });

  // ── relocation classification — a genuine test-file split, not erosion ────
  describe('computed_test_erosion — relocation (split/rename) classification', () => {
    beforeEach(() => vi.clearAllMocks());

    it('classifies a wholly-deleted test file as dead_code_removal when its test identifiers relocate into a newly-added file, even though its subject module is unchanged', async () => {
      // Mirrors the real 43bfe173 split: test_strategic_spawning.py deleted,
      // its subject app/strategic_spawning.py NOT deleted (present/unchanged),
      // and its tests relocated verbatim into a newly-added sibling file.
      const nameStatus = [
        'D\tbackend/testing/test_strategic_spawning.py',
        'A\tbackend/testing/test_strategic_spawning_capability_prediction.py',
      ].join('\n') + '\n';
      const diffText = [
        'diff --git a/backend/testing/test_strategic_spawning.py b/backend/testing/test_strategic_spawning.py',
        '--- a/backend/testing/test_strategic_spawning.py',
        '+++ /dev/null',
        '-def test_capability_prediction_scores_high_confidence():',
        '-    assert score > 0.9',
        '-def test_capability_prediction_rejects_stale_input():',
        '-    assert rejected is True',
        'diff --git a/backend/testing/test_strategic_spawning_capability_prediction.py b/backend/testing/test_strategic_spawning_capability_prediction.py',
        '--- /dev/null',
        '+++ b/backend/testing/test_strategic_spawning_capability_prediction.py',
        '+def test_capability_prediction_scores_high_confidence():',
        '+    assert score > 0.9',
        '+def test_capability_prediction_rejects_stale_input():',
        '+    assert rejected is True',
      ].join('\n') + '\n';

      vi.mocked(execFileSync).mockImplementation((_cmd: unknown, args: unknown) => {
        const a = args as string[];
        if (Array.isArray(a) && a[0] === 'diff' && a[1] === '--name-status') return nameStatus;
        if (Array.isArray(a) && a[0] === 'diff') return diffText;
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
          deleted_test_file_classifications?: { path: string; classification: string; rationale: string }[];
        };
      };
      const entry = stored.computed_test_erosion?.deleted_test_file_classifications?.find(
        c => c.path === 'backend/testing/test_strategic_spawning.py'
      );
      expect(entry?.classification).toBe('dead_code_removal');
      expect(entry?.rationale).toContain('2/2');
    });

    it('does NOT auto-clear the same deleted file when the replacement files are absent from the diff', async () => {
      // Same source-side deletion, but no newly-added file carries the
      // relocated identifiers (e.g. the split commit itself is missing from
      // this diff range) — must NOT be deterministically cleared.
      const nameStatus = 'D\tbackend/testing/test_strategic_spawning.py\n';
      const diffText = [
        'diff --git a/backend/testing/test_strategic_spawning.py b/backend/testing/test_strategic_spawning.py',
        '--- a/backend/testing/test_strategic_spawning.py',
        '+++ /dev/null',
        '-def test_capability_prediction_scores_high_confidence():',
        '-    assert score > 0.9',
        '-def test_capability_prediction_rejects_stale_input():',
        '-    assert rejected is True',
      ].join('\n') + '\n';

      vi.mocked(execFileSync).mockImplementation((_cmd: unknown, args: unknown) => {
        const a = args as string[];
        if (Array.isArray(a) && a[0] === 'diff' && a[1] === '--name-status') return nameStatus;
        if (Array.isArray(a) && a[0] === 'diff') return diffText;
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
          deleted_test_file_classifications?: { path: string; classification: string }[];
        };
      };
      const cls = stored.computed_test_erosion?.deleted_test_file_classifications;
      const entry = cls?.find(c => c.path === 'backend/testing/test_strategic_spawning.py');
      expect(entry).toBeUndefined();
    });
  });
});

// ── handleReleaseAudit — reviewed_paths per-path verdicts ────────────────────
describe('handleReleaseAudit — reviewed_paths per-path verdicts', () => {
  const CANDIDATE_SHA = 'a1b2c3d4e5f60718293a4b5c6d7e8f9012345678';
  const RELEASE_PAYLOAD = { base_branch: 'main', candidate_sha: CANDIDATE_SHA };

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
    vi.mocked(git).mockReturnValue('');
  });

  it('all changed paths are "clean" when findings list is empty', async () => {
    vi.mocked(execFileSync).mockImplementation((_cmd: unknown, args: unknown) => {
      const a = args as string[];
      if (a[0] === 'diff' && a[1] === '--name-status') return 'M\tbackend/app/foo.py\nA\tbackend/app/bar.py\n';
      if (a[0] === 'diff') return '';
      return Buffer.from('');
    });
    vi.mocked(readHandlerOutput).mockReturnValue({
      audit_score: 1,
      test_erosion: { deleted_test_files: [], assertion_delta: 0, test_count_delta: 0 },
      findings: [],
    });

    await handleReleaseAudit(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls.at(-1)?.[1];
    const report = JSON.parse(call?.logs as string) as { reviewed_paths: Record<string, string> };
    expect(report.reviewed_paths).toEqual({
      'backend/app/foo.py': 'clean',
      'backend/app/bar.py': 'clean',
    });
  });

  it('path matching a finding file is marked "finding"; other changed paths remain "clean"', async () => {
    vi.mocked(execFileSync).mockImplementation((_cmd: unknown, args: unknown) => {
      const a = args as string[];
      if (a[0] === 'diff' && a[1] === '--name-status') return 'M\tbackend/app/auth.py\nM\tbackend/app/models.py\n';
      if (a[0] === 'diff') return '';
      return Buffer.from('');
    });
    vi.mocked(readHandlerOutput).mockReturnValue({
      audit_score: 0.5,
      test_erosion: { deleted_test_files: [], assertion_delta: 0, test_count_delta: 0 },
      findings: [{ severity: 'high', summary: 'SQL injection', file: 'backend/app/auth.py', verification_note: 'line 42' }],
    });

    await handleReleaseAudit(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls.at(-1)?.[1];
    const report = JSON.parse(call?.logs as string) as { reviewed_paths: Record<string, string> };
    expect(report.reviewed_paths['backend/app/auth.py']).toBe('finding');
    expect(report.reviewed_paths['backend/app/models.py']).toBe('clean');
  });

  it('a finding whose file is not in changed_paths is absent from reviewed_paths', async () => {
    vi.mocked(execFileSync).mockImplementation((_cmd: unknown, args: unknown) => {
      const a = args as string[];
      if (a[0] === 'diff' && a[1] === '--name-status') return 'M\tbackend/app/foo.py\n';
      if (a[0] === 'diff') return '';
      return Buffer.from('');
    });
    vi.mocked(readHandlerOutput).mockReturnValue({
      audit_score: 0.5,
      test_erosion: { deleted_test_files: [], assertion_delta: 0, test_count_delta: 0 },
      // Cross-cutting finding with empty file — must not pollute reviewed_paths
      findings: [{ severity: 'medium', summary: 'cross-cutting concern', file: '' }],
    });

    await handleReleaseAudit(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls.at(-1)?.[1];
    const report = JSON.parse(call?.logs as string) as { reviewed_paths: Record<string, string> };
    expect(Object.keys(report.reviewed_paths)).toEqual(['backend/app/foo.py']);
    expect(report.reviewed_paths['backend/app/foo.py']).toBe('clean');
  });

  it('incremental run: reviewed_paths keys match audit_scope.changed_paths (two-dot scope)', async () => {
    const auditBaseSha = '0123456789abcdef0123456789abcdef01234567';
    vi.mocked(execFileSync).mockImplementation((_cmd: unknown, args: unknown) => {
      const a = args as string[];
      if (a[0] === 'diff' && a[1] === '--name-status') return 'M\tagent/src/handlers/audits.ts\n';
      if (a[0] === 'diff') return '';
      return Buffer.from('');
    });
    vi.mocked(readHandlerOutput).mockReturnValue({
      audit_score: 1,
      test_erosion: { deleted_test_files: [], assertion_delta: 0, test_count_delta: 0 },
      findings: [],
    });

    await handleReleaseAudit(
      TASK_ID, { ...RELEASE_PAYLOAD, audit_base_sha: auditBaseSha }, BASE_TASK,
      GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );

    const call = vi.mocked(persistAndPostStatus).mock.calls.at(-1)?.[1];
    const report = JSON.parse(call?.logs as string) as {
      reviewed_paths: Record<string, string>;
      audit_scope: { mode: string; changed_paths: string[] };
    };
    expect(report.audit_scope.mode).toBe('incremental');
    expect(Object.keys(report.reviewed_paths).sort()).toEqual([...report.audit_scope.changed_paths].sort());
  });

  it('fallback: fallback_reason carries "ancestor_check_failed:" prefix on merge-base failure', async () => {
    const auditBaseSha = '0123456789abcdef0123456789abcdef01234567';
    vi.mocked(execFileSync).mockImplementation((_cmd: unknown, args: unknown) => {
      const a = args as string[];
      if (a[0] === 'merge-base') throw new Error('not an ancestor');
      if (a[0] === 'diff' && a[1] === '--name-status') return 'M\tbackend/app/main.py\nM\tbackend/app/auth.py\n';
      if (a[0] === 'diff') return '';
      return Buffer.from('');
    });
    vi.mocked(readHandlerOutput).mockReturnValue({
      audit_score: 1,
      test_erosion: { deleted_test_files: [], assertion_delta: 0, test_count_delta: 0 },
      findings: [],
    });

    await handleReleaseAudit(
      TASK_ID, { ...RELEASE_PAYLOAD, audit_base_sha: auditBaseSha }, BASE_TASK,
      GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );

    const call = vi.mocked(persistAndPostStatus).mock.calls.at(-1)?.[1];
    const report = JSON.parse(call?.logs as string) as {
      audit_scope: { mode: string; fallback_reason: string; changed_paths: string[] };
      reviewed_paths: Record<string, string>;
    };
    expect(report.audit_scope.mode).toBe('full');
    expect(report.audit_scope.fallback_reason).toContain('ancestor_check_failed:');
    expect(report.audit_scope.fallback_reason).toContain('not an ancestor');
    // reviewed_paths must cover the full changed set for this fallback run
    expect(Object.keys(report.reviewed_paths).sort()).toEqual([...report.audit_scope.changed_paths].sort());
  });

  it('fallback: fallback_reason carries "cat_file_failed:" prefix on cat-file failure', async () => {
    const auditBaseSha = '0123456789abcdef0123456789abcdef01234567';
    vi.mocked(execFileSync).mockImplementation((_cmd: unknown, args: unknown) => {
      const a = args as string[];
      if (a[0] === 'cat-file') throw new Error('bad object a1b2c3');
      if (a[0] === 'diff' && a[1] === '--name-status') return 'M\tbackend/app/main.py\n';
      if (a[0] === 'diff') return '';
      return Buffer.from('');
    });
    vi.mocked(readHandlerOutput).mockReturnValue({
      audit_score: 1,
      test_erosion: { deleted_test_files: [], assertion_delta: 0, test_count_delta: 0 },
      findings: [],
    });

    await handleReleaseAudit(
      TASK_ID, { ...RELEASE_PAYLOAD, audit_base_sha: auditBaseSha }, BASE_TASK,
      GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );

    const call = vi.mocked(persistAndPostStatus).mock.calls.at(-1)?.[1];
    const report = JSON.parse(call?.logs as string) as {
      audit_scope: { fallback_reason: string };
    };
    expect(report.audit_scope.fallback_reason).toContain('cat_file_failed:');
    expect(report.audit_scope.fallback_reason).toContain('bad object a1b2c3');
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

// ── fetchCandidateCommitWithRetry — bounded retry on the candidate fetch ──────
//
// The shared helper all three release-qualifier audit/grade handlers use. A
// transient GitHub I/O blip on the single `git fetch origin <sha>` must NOT
// torch the (terminal) qualification attempt — it is retried with backoff —
// while a diff range that is genuinely unresolvable after a CLEAN fetch must
// still fail loud (never retried/masked: the safety property). Driven directly
// with injected sleep/fetch/verify so the retry mechanics are exercised without
// real timers or git.
describe('fetchCandidateCommitWithRetry — bounded candidate-fetch retry', () => {
  const CANDIDATE_SHA = 'a1b2c3d4e5f60718293a4b5c6d7e8f9012345678';
  const DIFF_RANGE = `main...${CANDIDATE_SHA}`;

  it('retries a transient fetch failure and then succeeds (no fail-loud)', async () => {
    let attempts = 0;
    const sleep = vi.fn().mockResolvedValue(undefined);
    const fetchOnce = vi.fn(() => {
      attempts += 1;
      if (attempts < 3) throw new Error('fatal: could not read from remote repository');
    });
    const verifyDiffRange = vi.fn();

    const res = await fetchCandidateCommitWithRetry(CANDIDATE_SHA, DIFF_RANGE, null, {
      sleep, fetchOnce, verifyDiffRange,
    });

    expect(fetchOnce).toHaveBeenCalledTimes(3); // 2 transient + 1 success
    expect(sleep).toHaveBeenCalledTimes(2);     // backoff between the 3 attempts
    expect(res.fetchOk).toBe(true);
    expect(res.diffRangeResolves).toBe(true);
    expect(verifyDiffRange).toHaveBeenCalledWith(CANDIDATE_SHA, DIFF_RANGE);
  });

  it('uses increasing (linear) backoff between attempts', async () => {
    const sleep = vi.fn().mockResolvedValue(undefined);
    const fetchOnce = vi.fn(() => { throw new Error('transient'); });

    await fetchCandidateCommitWithRetry(CANDIDATE_SHA, DIFF_RANGE, null, {
      maxAttempts: 3, backoffBaseMs: 100, sleep, fetchOnce, verifyDiffRange: vi.fn(),
    });

    expect(sleep).toHaveBeenNthCalledWith(1, 100);
    expect(sleep).toHaveBeenNthCalledWith(2, 200);
  });

  it('fails loud only AFTER exhausting all retries', async () => {
    const sleep = vi.fn().mockResolvedValue(undefined);
    const fetchOnce = vi.fn(() => { throw new Error('network down'); });
    const verifyDiffRange = vi.fn();

    const res = await fetchCandidateCommitWithRetry(CANDIDATE_SHA, DIFF_RANGE, null, {
      maxAttempts: 3, sleep, fetchOnce, verifyDiffRange,
    });

    expect(fetchOnce).toHaveBeenCalledTimes(3);     // exhausted every attempt
    expect(verifyDiffRange).not.toHaveBeenCalled(); // fetch never succeeded → no verify
    expect(res.fetchOk).toBe(false);
    expect(res.diffRangeResolves).toBe(false);
    expect(res.diffRangeFailDetail).toContain('3 attempt');
    expect(res.diffRangeFailDetail).toContain('network down');
  });

  it('does NOT retry a clean fetch whose diff range is genuinely unresolvable', async () => {
    // Safety property: a successful fetch leaving the merge-base unreachable is a
    // GENUINE unresolvable range — it must fail loud, never be retried/masked.
    const sleep = vi.fn().mockResolvedValue(undefined);
    const fetchOnce = vi.fn(); // succeeds on the first attempt
    const verifyDiffRange = vi.fn(() => { throw new Error('fatal: bad revision main...<sha>'); });

    const res = await fetchCandidateCommitWithRetry(CANDIDATE_SHA, DIFF_RANGE, null, {
      sleep, fetchOnce, verifyDiffRange,
    });

    expect(fetchOnce).toHaveBeenCalledTimes(1); // fetch succeeded → not retried
    expect(sleep).not.toHaveBeenCalled();
    expect(res.fetchOk).toBe(true);
    expect(res.diffRangeResolves).toBe(false);  // genuine unresolvable → fail loud
    expect(res.diffRangeFailDetail).toContain('bad revision');
  });

  it('FAILS LOUD when the diff range resolves to ZERO commits (empty range)', async () => {
    // `git rev-list --count <base>...<candidate>` prints "0" and exits 0 when the
    // symmetric difference is EMPTY (base == candidate). Without this guard the
    // range would be treated as 'resolves' and the handler would audit/grade a
    // blank surface. The default verifyDiffRange must fail loud instead.
    vi.mocked(git).mockImplementation((args: string[]) => {
      if (args[0] === 'rev-parse') return CANDIDATE_SHA;
      if (args[0] === 'rev-list') return '0'; // EMPTY — no commits in range
      return '';
    });
    const sleep = vi.fn().mockResolvedValue(undefined);

    const res = await fetchCandidateCommitWithRetry(CANDIDATE_SHA, DIFF_RANGE, null, { sleep });

    expect(res.fetchOk).toBe(true);
    expect(res.diffRangeResolves).toBe(false);
    expect(res.diffRangeFailDetail).toContain('EMPTY');
    expect(res.diffRangeFailDetail).toContain(DIFF_RANGE);
  });
});

// ── Prompt size telemetry — audit handlers record prompt size before runClaudeCode ─
describe('audit handlers — prompt size telemetry', () => {
  const CANDIDATE_SHA = 'a1b2c3d4e5f60718293a4b5c6d7e8f9012345678';

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(git).mockReturnValue('');
    vi.mocked(execFileSync).mockReturnValue(Buffer.from(''));
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
    resetLlmTelemetry();
  });

  it('test_security_audit_sets_prompt_size_telemetry', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue([{ title: 'SQL Injection', severity: 'critical' }]);

    await handleSecurityAudit(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const telemetry = getLlmTelemetry();
    expect(telemetry.prompt_size_bytes).toBeGreaterThan(0);
    expect(telemetry.prompt_section_count).toBeGreaterThan(0);
    expect(telemetry.prompt_section_sizes).not.toBeNull();
    expect((telemetry.prompt_section_sizes ?? []).length).toBeGreaterThan(0);
    for (const s of (telemetry.prompt_section_sizes ?? [])) {
      expect(s.bytes).toBeGreaterThan(0);
      expect(typeof s.name).toBe('string');
    }
  });

  it('test_self_audit_sets_prompt_size_telemetry', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue({ findings: [] });

    await handleSelfAudit(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const telemetry = getLlmTelemetry();
    expect(telemetry.prompt_size_bytes).toBeGreaterThan(0);
    expect(telemetry.prompt_section_count).toBeGreaterThan(0);
    expect(telemetry.prompt_section_sizes).not.toBeNull();
    expect((telemetry.prompt_section_sizes ?? []).length).toBeGreaterThan(0);
    for (const s of (telemetry.prompt_section_sizes ?? [])) {
      expect(s.bytes).toBeGreaterThan(0);
      expect(typeof s.name).toBe('string');
    }
  });

  it('test_release_audit_sets_prompt_size_telemetry', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue({
      audit_score: 1,
      test_erosion: { deleted_test_files: [], assertion_delta: 0, test_count_delta: 0 },
      findings: [],
    });

    await handleReleaseAudit(
      TASK_ID, { base_branch: 'main', candidate_sha: CANDIDATE_SHA },
      BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );

    const telemetry = getLlmTelemetry();
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
