import { describe, it, expect, vi, beforeEach } from 'vitest';

// ── Hoisted shared mocks ───────────────────────────────────────────────────────
// runClaudeCode is shared across both the ../index.js and ../providers/runners.js
// mock factories so that calls from audits.ts AND from structured-output-repair.ts
// (which may resolve its import through the re-export chain) are tracked by the
// same vi.fn() instance and controlled by the same mockResolvedValueOnce calls.
const { sharedRunClaudeCode } = vi.hoisted(() => ({
  sharedRunClaudeCode: vi.fn().mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' }),
}));

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
    readHandlerOutput: vi.fn().mockReturnValue(null),
    validateBranchName: vi.fn((b: string, _label: string) => b),
    git: vi.fn().mockReturnValue(''),
    gitAsync: vi.fn().mockResolvedValue(''),
    setupWorkspace: vi.fn(),
    runClaudeCode: sharedRunClaudeCode,
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

// Backstop: prevent the real runProviderAgent/spawn path from being reached
// regardless of module-resolution order. structured-output-repair.ts imports
// runClaudeCode from '../index.js' (mocked above as sharedRunClaudeCode), but a
// stale-cache edge case can leave the runners.ts re-export binding unintercepted.
// Using sharedRunClaudeCode here ensures that if the resolution falls through to
// runners.ts, it still calls the same tracked vi.fn() as all other callers.
vi.mock('../providers/runners.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../providers/runners.js')>();
  return {
    ...actual,
    runClaudeCode: sharedRunClaudeCode,
    runProviderAgent: vi.fn().mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' }),
    provisionAmbientHelpers: vi.fn(),
  };
});

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
  handleSelfAudit,
  handleReleaseAudit,
} from './audits.js';
import {
  persistAndPostStatus, readHandlerOutput, runClaudeCode, git,
} from '../index.js';
import { execFileSync } from 'child_process';
import { recordFormatMiss, recordFormatRepairAttempt, recordFormatRepairSuccess } from '../telemetry.js';

// ── Shared test helpers ───────────────────────────────────────────────────────
const TASK_ID = 'task-reopen-test';
const GH_TOKEN = 'gh-token';
const OWNER = 'owner';
const REPO = 'repo';
const WS = null;
const STATUS_REF = () => ({ value: '' });
const BASE_TASK = { title: 'Test Task', description: '', context: {}, project_context: {} };
const BASE_PAYLOAD = { base_branch: 'main' };

const CANDIDATE_SHA = 'a1b2c3d4e5f60718293a4b5c6d7e8f9012345678';
const RELEASE_PAYLOAD = { base_branch: 'main', candidate_sha: CANDIDATE_SHA };

// ── handleSecurityAudit — bounded self-repair ─────────────────────────────────
describe('handleSecurityAudit — bounded self-repair on format miss', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(readHandlerOutput).mockReturnValue(null);
  });

  it('makes exactly one focused re-ask when exitCode=0 but output has no JSON array', async () => {
    // First call: clean exit but no findings JSON
    vi.mocked(runClaudeCode).mockResolvedValueOnce({ exitCode: 0, allOutput: 'Some analysis here.', lastResultText: '' });
    // Second call (repair): also no JSON — repair fails
    vi.mocked(runClaudeCode).mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '' });

    await handleSecurityAudit(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(2);
    expect(vi.mocked(recordFormatMiss)).toHaveBeenCalledTimes(1);
    expect(vi.mocked(recordFormatRepairAttempt)).toHaveBeenCalledTimes(1);
  });

  it('records repair success and posts COMPLETED when re-ask returns parseable findings', async () => {
    const repairedFindings = '[{"title":"SQL injection","severity":"high","description":"...","file_path":"app.py","line_number":10,"fix_prompt":"..."}]';
    // First call: clean exit, no JSON
    vi.mocked(runClaudeCode).mockResolvedValueOnce({ exitCode: 0, allOutput: 'Analysis without JSON.', lastResultText: '' });
    // Second call (repair): valid findings array
    vi.mocked(runClaudeCode).mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: repairedFindings });

    await handleSecurityAudit(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(2);
    expect(vi.mocked(recordFormatRepairSuccess)).toHaveBeenCalledTimes(1);
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
    expect(call.artifact_type).toBe('SECURITY_REPORT');
    const stored = JSON.parse(call.logs as string) as Array<{ severity: string }>;
    expect(Array.isArray(stored)).toBe(true);
    expect(stored[0].severity).toBe('high');
  });

  it('does NOT call recordFormatRepairSuccess when repair also fails; preserves fallback path', async () => {
    // Both calls: clean exit but no parseable JSON
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: 'no json', lastResultText: 'raw fallback' });

    await handleSecurityAudit(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(vi.mocked(recordFormatRepairSuccess)).not.toHaveBeenCalled();
    // Falls back to raw output but still COMPLETED (handler's existing fallback behavior)
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
    expect(call.logs).toBe('raw fallback');
  });

  it('skips repair and does NOT call format telemetry when exitCode is non-zero', async () => {
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 1, allOutput: 'crash', lastResultText: '' });

    await handleSecurityAudit(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(1);
    expect(vi.mocked(recordFormatMiss)).not.toHaveBeenCalled();
    expect(vi.mocked(recordFormatRepairAttempt)).not.toHaveBeenCalled();
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
  });

  it('includes prior allOutput content in the reopen prompt when audit progress file exists', async () => {
    const { readFileSync, existsSync } = await import('fs');
    const priorAnalysisContent = 'Found potential SQL injection in auth.py line 42. Reviewing pattern...';
    vi.mocked(existsSync).mockReturnValue(true);
    vi.mocked(readFileSync).mockReturnValue(priorAnalysisContent);

    // First call: clean exit, no JSON
    vi.mocked(runClaudeCode).mockResolvedValueOnce({ exitCode: 0, allOutput: 'Analysis here.', lastResultText: '' });
    // Second call (repair): valid findings
    const repairedFindings = '[{"title":"SQL injection","severity":"high","description":"...","file_path":"app.py","line_number":10,"fix_prompt":"..."}]';
    vi.mocked(runClaudeCode).mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: repairedFindings });

    await handleSecurityAudit(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    // Verify the second call (repair) includes prior analysis in the reopenPrompt
    const repairCall = vi.mocked(runClaudeCode).mock.calls[1];
    expect(repairCall[0]).toContain(priorAnalysisContent);
    expect(repairCall[0]).toContain('## Prior Analysis');
  });
});

// ── handleSelfAudit — bounded self-repair ─────────────────────────────────────
describe('handleSelfAudit — bounded self-repair on format miss', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(readHandlerOutput).mockReturnValue(null);
  });

  it('makes exactly one focused re-ask when exitCode=0 but output has no findings JSON', async () => {
    vi.mocked(runClaudeCode).mockResolvedValueOnce({ exitCode: 0, allOutput: 'Detailed analysis.', lastResultText: '' });
    vi.mocked(runClaudeCode).mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '' });

    await handleSelfAudit(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(2);
    expect(vi.mocked(recordFormatMiss)).toHaveBeenCalledTimes(1);
    expect(vi.mocked(recordFormatRepairAttempt)).toHaveBeenCalledTimes(1);
  });

  it('records repair success and posts COMPLETED when re-ask returns parseable findings object', async () => {
    // system_id is nested inside findings elements, not a top-level own property.
    // The extractor uses requiredKeys ['findings'] so a correctly-formed
    // {"findings":[{"system_id":...}]} must succeed without a top-level system_id.
    const repairedObj = '{"findings":[{"system_id":"sys-auth","score":72,"components":{"test_coverage":80,"friction_density":65,"claim_consistency":75,"failure_mix":70},"top_issues":["Missing tests"],"recommended_mode":"fundamentals","evidence":[{"file":"auth.py","summary":"..."}]}]}';
    vi.mocked(runClaudeCode).mockResolvedValueOnce({ exitCode: 0, allOutput: 'Analysis...', lastResultText: '' });
    vi.mocked(runClaudeCode).mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: repairedObj });

    await handleSelfAudit(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(vi.mocked(recordFormatRepairSuccess)).toHaveBeenCalledTimes(1);
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
    expect(call.artifact_type).toBe('SELF_AUDIT_REPORT');
    const stored = JSON.parse(call.logs as string) as { findings: Array<{ system_id: string }> };
    expect(stored.findings[0].system_id).toBe('sys-auth');
  });

  it('does NOT call recordFormatRepairSuccess when repair also fails; falls back to raw output', async () => {
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: 'no json', lastResultText: 'raw text' });

    await handleSelfAudit(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(vi.mocked(recordFormatRepairSuccess)).not.toHaveBeenCalled();
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
    expect(call.logs).toBe('raw text');
  });

  it('skips repair when exitCode is non-zero', async () => {
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 1, allOutput: '', lastResultText: '' });

    await handleSelfAudit(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(1);
    expect(vi.mocked(recordFormatRepairAttempt)).not.toHaveBeenCalled();
  });

  it('includes prior allOutput content in the reopen prompt when audit progress file exists', async () => {
    const { readFileSync, existsSync } = await import('fs');
    const priorAnalysisContent = 'Auditing sys-auth: test coverage 85%, friction 60%...';
    vi.mocked(existsSync).mockReturnValue(true);
    vi.mocked(readFileSync).mockReturnValue(priorAnalysisContent);

    // First call: clean exit, no JSON
    vi.mocked(runClaudeCode).mockResolvedValueOnce({ exitCode: 0, allOutput: 'Analysis...', lastResultText: '' });
    // Second call (repair): valid findings
    const repairedObj = '{"system_id":"sys-auth","findings":[{"system_id":"sys-auth","score":72,"components":{"test_coverage":80,"friction_density":65,"claim_consistency":75,"failure_mix":70},"top_issues":["Missing tests"],"recommended_mode":"fundamentals","evidence":[{"file":"auth.py","summary":"..."}]}]}';
    vi.mocked(runClaudeCode).mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: repairedObj });

    await handleSelfAudit(TASK_ID, BASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    // Verify the repair call includes prior analysis in the reopenPrompt
    const repairCall = vi.mocked(runClaudeCode).mock.calls[1];
    expect(repairCall[0]).toContain(priorAnalysisContent);
    expect(repairCall[0]).toContain('## Prior Analysis');
  });
});

// ── handleReleaseAudit — bounded self-repair ──────────────────────────────────
describe('handleReleaseAudit — bounded self-repair on format miss', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    vi.mocked(git).mockReturnValue('');
    vi.mocked(execFileSync).mockReturnValue(Buffer.from(''));
  });

  it('makes exactly one focused re-ask when exitCode=0 but output has no audit JSON', async () => {
    vi.mocked(runClaudeCode).mockResolvedValueOnce({ exitCode: 0, allOutput: 'Analysis only.', lastResultText: '' });
    vi.mocked(runClaudeCode).mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '' });

    await handleReleaseAudit(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(2);
    expect(vi.mocked(recordFormatMiss)).toHaveBeenCalledTimes(1);
    expect(vi.mocked(recordFormatRepairAttempt)).toHaveBeenCalledTimes(1);
  });

  it('records repair success, posts COMPLETED (not FAILED), when re-ask returns parseable report', async () => {
    const repairedReport = '{"audit_score":0.9,"test_erosion":{"deleted_test_files":[],"assertion_delta":5,"test_count_delta":1},"findings":[]}';
    vi.mocked(runClaudeCode).mockResolvedValueOnce({ exitCode: 0, allOutput: 'Detailed analysis.', lastResultText: '' });
    vi.mocked(runClaudeCode).mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: repairedReport });

    await handleReleaseAudit(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(vi.mocked(recordFormatRepairSuccess)).toHaveBeenCalledTimes(1);
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    // The handler must NOT mark the task FAILED when the re-ask recovers
    expect(call.status).toBe('COMPLETED');
    expect(call.artifact_type).toBe('RELEASE_AUDIT_REPORT');
    const stored = JSON.parse(call.logs as string) as { audit_score: number; findings: unknown[] };
    expect(stored.audit_score).toBe(0.9);
    expect(stored.findings).toEqual([]);
  });

  it('preserves FAILED/release_audit_report_unparseable when repair also fails', async () => {
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: 'no json', lastResultText: 'raw tail' });

    await handleReleaseAudit(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(vi.mocked(recordFormatRepairSuccess)).not.toHaveBeenCalled();
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.failure_category).toBe('release_audit_report_unparseable');
    expect(call.logs).toContain('raw tail');
  });

  it('skips repair when exitCode is non-zero; no format telemetry emitted', async () => {
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 1, allOutput: '', lastResultText: '' });

    await handleReleaseAudit(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(1);
    expect(vi.mocked(recordFormatMiss)).not.toHaveBeenCalled();
    expect(vi.mocked(recordFormatRepairAttempt)).not.toHaveBeenCalled();
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.failure_category).toBe('claude_code_failed');
  });

  it('includes prior allOutput content in the reopen prompt when audit progress file exists', async () => {
    const { readFileSync, existsSync } = await import('fs');
    const priorAnalysisContent = 'Reviewed diff: 5 files changed, 150 additions. Test erosion: no deleted tests. Security: no injection risks found.';
    vi.mocked(existsSync).mockReturnValue(true);
    vi.mocked(readFileSync).mockReturnValue(priorAnalysisContent);

    // First call: clean exit, no JSON
    vi.mocked(runClaudeCode).mockResolvedValueOnce({ exitCode: 0, allOutput: 'Detailed analysis.', lastResultText: '' });
    // Second call (repair): valid report
    const repairedReport = '{"audit_score":0.9,"test_erosion":{"deleted_test_files":[],"assertion_delta":5,"test_count_delta":1},"findings":[]}';
    vi.mocked(runClaudeCode).mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: repairedReport });

    await handleReleaseAudit(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    // Verify the repair call includes prior analysis in the reopenPrompt
    const repairCall = vi.mocked(runClaudeCode).mock.calls[1];
    expect(repairCall[0]).toContain(priorAnalysisContent);
    expect(repairCall[0]).toContain('## Prior Analysis');
  });
});
