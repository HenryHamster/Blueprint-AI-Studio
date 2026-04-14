import { describe, it, expect, vi, beforeEach } from 'vitest';

// ── Module mocks ──────────────────────────────────────────────────────────────
vi.mock('../index.js', () => ({
  WORKSPACE: '/tmp/workspace',
  strVal: (v: unknown, fallback = '') => (typeof v === 'string' ? v : fallback),
  sanitize: (s: string) => s,
  sendLog: vi.fn(),
  log: vi.fn(),
  getAgentToken: vi.fn().mockReturnValue(''),
  apiPost: vi.fn(),
  apiGet: vi.fn(),
  persistAndPostStatus: vi.fn().mockResolvedValue(undefined),
  readHandlerOutput: vi.fn(),
  validateBranchName: vi.fn((b: string, _label: string) => b),
  git: vi.fn().mockReturnValue(''),
  setupWorkspace: vi.fn(),
  pushAndCreatePR: vi.fn(),
  runClaudeCode: vi.fn().mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' }),
  lintFixLoop: vi.fn(),
  testFixLoop: vi.fn(),
  cloneWithRetry: vi.fn().mockResolvedValue(undefined),
  sendHeartbeatSync: vi.fn(),
}));

vi.mock('../prompt.js', () => ({
  TASK_TYPE_HINTS: {},
  getTaskTypeHints: vi.fn().mockReturnValue({}),
  DEFAULT_TOOL_GUIDANCE: '',
  PLAYBOOK_GUIDANCE: '',
  readClaudeMd: vi.fn().mockReturnValue(''),
  buildContextPrefix: vi.fn().mockReturnValue(''),
  buildFileTree: vi.fn().mockReturnValue(''),
  discoverRelatedTests: vi.fn().mockReturnValue(''),
  configureAgentPermissions: vi.fn().mockReturnValue([]),
  writePlaybookHelper: vi.fn(),
  writeMessagesHelper: vi.fn(),
  formatPlaybook: vi.fn().mockReturnValue(''),
}));

vi.mock('fs', () => ({
  default: {
    readFileSync: vi.fn().mockReturnValue(''),
    writeFileSync: vi.fn(),
    existsSync: vi.fn().mockReturnValue(false),
    appendFileSync: vi.fn(),
    mkdirSync: vi.fn(),
    readdirSync: vi.fn().mockReturnValue([]),
    unlinkSync: vi.fn(),
  },
}));

vi.mock('child_process', () => ({
  execFileSync: vi.fn().mockReturnValue(Buffer.from('')),
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
import {
  handleSecurityAudit,
  handleSmoketest,
  handleDebug,
  handlePerf,
  handleInvestigate,
} from './specialized.js';
import { persistAndPostStatus, readHandlerOutput, runClaudeCode } from '../index.js';
import { execFileSync } from 'child_process';

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

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1] as Record<string, unknown>;
    expect(call.logs).toBe(JSON.stringify(findings));
    expect(call.artifact_type).toBe('SECURITY_REPORT');
  });

  it('falls back to lastResultText when it contains "severity" and readHandlerOutput returns null', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    const lastResultText = '[{"title":"XSS","severity":"high","fix_prompt":"..."}]';
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText });

    await handleSecurityAudit(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1] as Record<string, unknown>;
    expect(call.logs).toBe(lastResultText);
  });

  it('extracts JSON from allOutput via regex when lastResultText lacks "severity"', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    const jsonBlock = '[{"title":"Path Traversal","severity":"medium"}]';
    const allOutput = `Some preamble text\n${jsonBlock}\nSome trailing text`;
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput, lastResultText: 'no match here' });

    await handleSecurityAudit(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1] as Record<string, unknown>;
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

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1] as Record<string, unknown>;
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

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1] as Record<string, unknown>;
    expect(call.logs).toBe('no json content');
  });

  it('sets status COMPLETED and artifact_type SECURITY_REPORT on exitCode 0', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue([]);
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });

    await handleSecurityAudit(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1] as Record<string, unknown>;
    expect(call.status).toBe('COMPLETED');
    expect(call.artifact_type).toBe('SECURITY_REPORT');
  });

  it('sets status FAILED and artifact_type ERROR_LOG on non-zero exitCode', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 1, allOutput: 'err', lastResultText: '' });

    await handleSecurityAudit(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1] as Record<string, unknown>;
    expect(call.status).toBe('FAILED');
    expect(call.artifact_type).toBe('ERROR_LOG');
  });
});

// ── handlePerf — JSON extraction fallback ─────────────────────────────────────
describe('handlePerf — JSON extraction fallback', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
  });

  it('uses readHandlerOutput result directly when it returns findings', async () => {
    const findings = [{ area: 'database', severity: 'high', title: 'N+1 Query' }];
    vi.mocked(readHandlerOutput).mockReturnValue(findings);

    await handlePerf(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1] as Record<string, unknown>;
    expect(call.logs).toBe(JSON.stringify(findings));
    expect(call.artifact_type).toBe('PERF_REPORT');
  });

  it('falls back to lastResultText when it contains "severity" and readHandlerOutput returns null', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    const lastResultText = '[{"area":"api","severity":"medium","title":"Unbounded list"}]';
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText });

    await handlePerf(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1] as Record<string, unknown>;
    expect(call.logs).toBe(lastResultText);
  });

  it('extracts JSON from allOutput via regex when lastResultText lacks "severity"', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    const jsonBlock = '[{"area":"frontend","severity":"low","title":"Missing useMemo"}]';
    const allOutput = `Analysis complete\n${jsonBlock}\nEnd`;
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput, lastResultText: 'no keywords' });

    await handlePerf(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1] as Record<string, unknown>;
    expect(call.logs).toBe(jsonBlock);
  });

  it('sets status COMPLETED and artifact_type PERF_REPORT on exitCode 0', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue([]);

    await handlePerf(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1] as Record<string, unknown>;
    expect(call.status).toBe('COMPLETED');
    expect(call.artifact_type).toBe('PERF_REPORT');
  });

  it('sets status FAILED and artifact_type ERROR_LOG on non-zero exitCode', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 1, allOutput: 'crash', lastResultText: '' });

    await handlePerf(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1] as Record<string, unknown>;
    expect(call.status).toBe('FAILED');
    expect(call.artifact_type).toBe('ERROR_LOG');
  });
});

// ── handleSmoketest — URL health check result formatting ──────────────────────
describe('handleSmoketest — URL health checks', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
  });

  it('reports COMPLETED with SMOKETEST_RESULT when smoketestResult.passed is true and no URLs', async () => {
    const smoketestResult = { passed: true, tests: [{ name: 'startup', status: 'pass', message: '' }] };
    vi.mocked(readHandlerOutput).mockReturnValue(smoketestResult);

    await handleSmoketest(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1] as Record<string, unknown>;
    expect(call.status).toBe('COMPLETED');
    expect(call.artifact_type).toBe('SMOKETEST_RESULT');
  });

  it('reports FAILED when smoketestResult.passed is false', async () => {
    const smoketestResult = { passed: false, tests: [{ name: 'pytest', status: 'fail', message: 'AssertionError' }] };
    vi.mocked(readHandlerOutput).mockReturnValue(smoketestResult);

    await handleSmoketest(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1] as Record<string, unknown>;
    expect(call.status).toBe('FAILED');
    expect(call.artifact_type).toBe('SMOKETEST_RESULT');
  });

  it('appends passing URL health check results and keeps passed=true', async () => {
    const smoketestResult = { passed: true, tests: [] as Array<{ name: string; status: string; message: string }> };
    vi.mocked(readHandlerOutput).mockReturnValue(smoketestResult);
    // curl succeeds (no throw)
    vi.mocked(execFileSync).mockReturnValue(Buffer.from(''));

    await handleSmoketest(
      TASK_ID,
      { ...BASE_PAYLOAD, health_check_urls: ['https://example.com/health'] },
      BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1] as Record<string, unknown>;
    const stored = JSON.parse(call.logs as string) as { passed: boolean; tests: Array<{ name: string; status: string }> };
    expect(stored.passed).toBe(true);
    expect(stored.tests).toHaveLength(1);
    expect(stored.tests[0].name).toBe('HTTP GET https://example.com/health');
    expect(stored.tests[0].status).toBe('pass');
  });

  it('appends failing URL health check results and flips passed to false', async () => {
    const smoketestResult = { passed: true, tests: [] as Array<{ name: string; status: string; message: string }> };
    vi.mocked(readHandlerOutput).mockReturnValue(smoketestResult);
    // curl fails for this specific call
    vi.mocked(execFileSync).mockImplementation((cmd: unknown, args: unknown) => {
      const argsArr = args as string[];
      if (cmd === 'curl' || (Array.isArray(argsArr) && argsArr.includes('https://example.com/down'))) {
        const err = new Error('curl failed') as Error & { status: number };
        err.status = 22;
        throw err;
      }
      return Buffer.from('');
    });

    await handleSmoketest(
      TASK_ID,
      { ...BASE_PAYLOAD, health_check_urls: ['https://example.com/down'] },
      BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1] as Record<string, unknown>;
    const stored = JSON.parse(call.logs as string) as { passed: boolean; tests: Array<{ name: string; status: string; message: string }> };
    expect(stored.passed).toBe(false);
    expect(stored.tests[0].status).toBe('fail');
    expect(stored.tests[0].message).toContain('22');
  });

  it('falls back to ERROR_LOG when readHandlerOutput returns null or exitCode is non-zero', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 1, allOutput: 'fail output', lastResultText: '' });

    await handleSmoketest(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1] as Record<string, unknown>;
    expect(call.status).toBe('FAILED');
    expect(call.artifact_type).toBe('ERROR_LOG');
  });
});

// ── handleDebug — report file reading and status mapping ─────────────────────
describe('handleDebug — report reading and status mapping', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
  });

  it('sets status COMPLETED and artifact_type DEBUG_RESULT when debugResult is found', async () => {
    const debugResult = {
      root_cause: 'Missing null check at line 42',
      affected_files: ['src/utils.ts'],
      fix_prompt: 'Add null check',
      confidence: 'high',
    };
    vi.mocked(readHandlerOutput).mockReturnValue(debugResult);

    await handleDebug(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1] as Record<string, unknown>;
    expect(call.status).toBe('COMPLETED');
    expect(call.artifact_type).toBe('DEBUG_RESULT');
    expect(call.logs).toBe(JSON.stringify(debugResult));
  });

  it('sets status FAILED and artifact_type ERROR_LOG when debugResult is null', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: 'no report written', lastResultText: '' });

    await handleDebug(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1] as Record<string, unknown>;
    expect(call.status).toBe('FAILED');
    expect(call.artifact_type).toBe('ERROR_LOG');
  });
});

// ── handleInvestigate — report file reading and status mapping ────────────────
describe('handleInvestigate — report reading and status mapping', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
  });

  it('sets status COMPLETED and artifact_type INVESTIGATION_REPORT on exitCode 0 with result', async () => {
    const investigationResult = {
      summary: 'Found issue in auth middleware',
      root_cause: 'Token not invalidated on logout',
      affected_files: [{ path: 'backend/app/auth.py', issue: 'missing invalidation' }],
      recommended_fix: 'Call invalidate() in logout handler',
      severity: 'high',
      confidence: 'high',
    };
    vi.mocked(readHandlerOutput).mockReturnValue(investigationResult);

    await handleInvestigate(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1] as Record<string, unknown>;
    expect(call.status).toBe('COMPLETED');
    expect(call.artifact_type).toBe('INVESTIGATION_REPORT');
    expect(call.logs).toBe(JSON.stringify(investigationResult));
  });

  it('sets status FAILED with parse_failed category when investigation_report file is missing', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);

    await handleInvestigate(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1] as Record<string, unknown>;
    expect(call.status).toBe('FAILED');
    expect(call.failure_category).toBe('parse_failed');
  });

  it('sets status FAILED and artifact_type ERROR_LOG on non-zero exitCode with result', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue({ summary: 'partial', root_cause: '', affected_files: [], recommended_fix: '', severity: 'low', confidence: 'low' });
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 1, allOutput: '', lastResultText: '' });

    await handleInvestigate(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1] as Record<string, unknown>;
    expect(call.status).toBe('FAILED');
    expect(call.artifact_type).toBe('ERROR_LOG');
  });
});

// ── Handler statusRef updates ─────────────────────────────────────────────────
describe('handler statusRef.value updates', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
  });

  it('handleSecurityAudit sets statusRef before and after runClaudeCode', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue([]);
    const statusRef = STATUS_REF();

    await handleSecurityAudit(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, statusRef);

    // statusRef should have been mutated during the run
    // After runClaudeCode it's still 'Running security audit...'
    expect(statusRef.value).toBe('Running security audit...');
  });

  it('handlePerf sets "Running performance analysis..." status', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue([]);
    const statusRef = STATUS_REF();

    await handlePerf(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, statusRef);

    expect(statusRef.value).toBe('Running performance analysis...');
  });

  it('handleSmoketest sets "Running smoke tests..." status', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue({ passed: true, tests: [] });
    const statusRef = STATUS_REF();

    await handleSmoketest(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, statusRef);

    expect(statusRef.value).toBe('Running smoke tests...');
  });

  it('handleDebug sets "Analyzing failure..." status', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue({ root_cause: '', affected_files: [], fix_prompt: '', confidence: 'low' });
    const statusRef = STATUS_REF();

    await handleDebug(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, statusRef);

    expect(statusRef.value).toBe('Analyzing failure...');
  });

  it('handleInvestigate sets "Running investigation..." status', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue({ summary: '', root_cause: '', affected_files: [], recommended_fix: '', severity: 'low', confidence: 'low' });
    const statusRef = STATUS_REF();

    await handleInvestigate(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, statusRef);

    expect(statusRef.value).toBe('Running investigation...');
  });
});
