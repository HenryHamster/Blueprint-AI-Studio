import { describe, it, expect, vi, beforeEach } from 'vitest';
import { expectCalledWith } from '../utils/typed-mock-call.js';

// ── Module mocks (mirror audits.test.ts) ─────────────────────────────────────
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
    runClaudeCode: vi.fn().mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' }),
    sendHeartbeatSync: vi.fn(),
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
    configureAgentPermissions: vi.fn().mockReturnValue([]),
    writePlaybookHelper: vi.fn(),
    writeMessagesHelper: vi.fn(),
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
import './index.js';
import {
  handleReleaseAuditChallenge,
  normalizeChallengeOutput,
  parsePrimaryFindings,
  CHALLENGE_VERDICTS,
  MIN_CHALLENGE_REASON_LEN,
} from './audit-challenge.js';
import {
  persistAndPostStatus, readHandlerOutput, runClaudeCode, git,
} from '../index.js';
import { execFileSync } from 'child_process';
import { prepareWorkspaceForRole } from '../workspacePrep.js';
import { recordFormatMiss, recordFormatRepairAttempt, recordFormatRepairSuccess } from '../telemetry.js';

// ── Shared test helpers ───────────────────────────────────────────────────────
const TASK_ID = 'task-chal-1';
const GH_TOKEN = 'gh-token';
const OWNER = 'owner';
const REPO = 'repo';
const WS = null;
const STATUS_REF = () => ({ value: '' });
const BASE_TASK = {} as Record<string, unknown>;

// A full 40-char hex SHA (the backend pins automain HEAD this way).
const CANDIDATE_SHA = 'a1b2c3d4e5f60718293a4b5c6d7e8f9012345678';
const DIFF_RANGE = `main...${CANDIDATE_SHA}`;
const RELEASE_PAYLOAD = { base_branch: 'main', candidate_sha: CANDIDATE_SHA };

// ── Pure-function contract: normalizeChallengeOutput ─────────────────────────
describe('normalizeChallengeOutput — stable RELEASE_AUDIT_CHALLENGE contract', () => {
  it('forces challenged_candidate_sha to the pinned SHA regardless of LLM output', () => {
    const out = normalizeChallengeOutput(
      { challenged_candidate_sha: 'WRONG', verdicts: [] },
      CANDIDATE_SHA,
    );
    expect(out.challenged_candidate_sha).toBe(CANDIDATE_SHA);
    expect(out.verdicts).toEqual([]);
  });

  it('keeps only verdicts in the enum and lowercases/normalizes fields', () => {
    const out = normalizeChallengeOutput(
      {
        verdicts: [
          { severity: 'High', summary: 'sql injection in auth', file: 'app/auth.py', verdict: 'CONFIRMED', reason: 'Read app/auth.py:60 — user_id reaches SQL unsanitised.' },
          { severity: 'medium', summary: 'bogus', file: '', verdict: 'not_a_verdict', reason: 'x' },
          { severity: 'high', summary: '', file: '', verdict: 'confirmed', reason: 'long enough reason text here for sure' },
        ],
      },
      CANDIDATE_SHA,
    );
    // The invalid-verdict entry and the empty-summary entry are dropped.
    expect(out.verdicts).toHaveLength(1);
    expect(out.verdicts[0]).toEqual({
      severity: 'high',
      summary: 'sql injection in auth',
      file: 'app/auth.py',
      verdict: 'confirmed',
      reason: 'Read app/auth.py:60 — user_id reaches SQL unsanitised.',
    });
    // Every emitted verdict carries a value from the stable enum.
    for (const v of out.verdicts) {
      expect(CHALLENGE_VERDICTS).toContain(v.verdict);
    }
  });

  it('downgrades confirmed/refuted_false_positive to uncertain when reason is too short', () => {
    const shortReason = 'too short'; // < MIN_CHALLENGE_REASON_LEN non-whitespace chars
    expect(shortReason.length).toBeLessThan(MIN_CHALLENGE_REASON_LEN);
    const out = normalizeChallengeOutput(
      {
        verdicts: [
          { severity: 'high', summary: 'confirmed but unjustified', file: 'a.py', verdict: 'confirmed', reason: shortReason },
          { severity: 'high', summary: 'refuted but unjustified', file: 'b.py', verdict: 'refuted_false_positive', reason: '' },
        ],
      },
      CANDIDATE_SHA,
    );
    expect(out.verdicts.map(v => v.verdict)).toEqual(['uncertain', 'uncertain']);
  });

  it('keeps a substantive confirmed verdict whose reason meets the length bar', () => {
    const reason = 'Verified at app/x.py:42 that the input is hardcoded — finding is genuine.';
    const out = normalizeChallengeOutput(
      { verdicts: [{ severity: 'critical', summary: 's', file: 'app/x.py', verdict: 'refuted_false_positive', reason }] },
      CANDIDATE_SHA,
    );
    expect(out.verdicts[0].verdict).toBe('refuted_false_positive');
    expect(out.verdicts[0].reason).toBe(reason);
  });

  it('drops primary_finding_index when no primaryFindings array is passed', () => {
    const out = normalizeChallengeOutput(
      {
        verdicts: [
          {
            primary_finding_index: 2,
            severity: 'high',
            summary: 'unsafe retry binding',
            file: 'app/x.py',
            verdict: 'confirmed',
            reason: 'Read app/x.py:42 and confirmed this retry binding issue is real.',
          },
          {
            primary_finding_index: '3',
            severity: 'high',
            summary: 'string index is ignored',
            file: 'app/y.py',
            verdict: 'confirmed',
            reason: 'Read app/y.py:42 and confirmed this issue is real too.',
          },
        ],
      },
      CANDIDATE_SHA,
    );
    expect(out.verdicts[0]).not.toHaveProperty('primary_finding_index');
    expect(out.verdicts[1]).not.toHaveProperty('primary_finding_index');
  });

  it('copies the bound primary summary even when the model omits summary text', () => {
    const primaryFindings = [
      { severity: 'high', summary: 'Unsafe retry binding in audit gate.', file: 'app/x.py' },
    ];
    const out = normalizeChallengeOutput(
      {
        verdicts: [
          {
            primary_finding_index: 1,
            severity: 'high',
            summary: '',
            file: 'app/x.py',
            verdict: 'confirmed',
            reason: 'Read app/x.py:42 and confirmed the binding issue is real.',
          },
        ],
      },
      CANDIDATE_SHA,
      primaryFindings,
    );
    expect(out.verdicts).toHaveLength(1);
    expect(out.verdicts[0].summary).toBe('Unsafe retry binding in audit gate.');
  });

  it('normalizes indexed primary summaries verbatim — overwrites model paraphrase with exact finding text', () => {
    const primaryFindings = [
      { severity: 'high', summary: 'Unsafe deserialization in upload handler.', file: 'app/upload.py' },
      { severity: 'medium', summary: 'SQL injection via user_id parameter.', file: 'app/auth.py' },
    ];
    const out = normalizeChallengeOutput(
      {
        verdicts: [
          {
            primary_finding_index: 1,
            severity: 'high',
            summary: 'unsafe deserialization exists in upload',  // paraphrased — must be overwritten
            file: 'app/upload.py',
            verdict: 'confirmed',
            reason: 'Read app/upload.py:42 and confirmed the deserialization issue is real.',
          },
          {
            primary_finding_index: 2,
            severity: 'medium',
            summary: 'sql injection via user parameter',  // paraphrased — must be overwritten
            file: 'app/auth.py',
            verdict: 'refuted_false_positive',
            reason: 'Read app/auth.py:60 — user_id is not user-controlled here, safely parameterized.',
          },
        ],
      },
      CANDIDATE_SHA,
      primaryFindings,
    );
    expect(out.verdicts[0].summary).toBe('Unsafe deserialization in upload handler.');
    expect(out.verdicts[1].summary).toBe('SQL injection via user_id parameter.');
  });

  it('copies bound primary severity, summary, and file over model drift', () => {
    const primary = { severity: 'high', summary: 'Unsafe retry binding in audit gate.', file: 'app/audit.py' };
    const out = normalizeChallengeOutput(
      {
        verdicts: [
          {
            primary_finding_index: 1,
            severity: 'low',
            summary: 'retry issue',
            file: 'wrong.py',
            verdict: 'refuted_false_positive',
            reason: 'Read app/audit.py:42 and confirmed the retry binding is a false positive.',
          },
        ],
      },
      CANDIDATE_SHA,
      [primary],
    );

    expect(out.verdicts[0]).toMatchObject({
      primary_finding_index: 1,
      severity: primary.severity,
      summary: primary.summary,
      file: primary.file,
    });
  });

  it('drops out-of-bounds primary_finding_index while leaving text available for backend matching', () => {
    const primaryFindings = [
      { severity: 'high', summary: 'Exact primary summary text.', file: 'app/x.py' },
    ];
    const out = normalizeChallengeOutput(
      {
        verdicts: [
          {
            primary_finding_index: 5,  // out of bounds (only 1 finding)
            severity: 'high',
            summary: 'independently derived summary',
            file: 'app/x.py',
            verdict: 'confirmed',
            reason: 'Read app/x.py:42 and confirmed this issue is genuine and present.',
          },
        ],
      },
      CANDIDATE_SHA,
      primaryFindings,
    );
    expect(out.verdicts[0].summary).toBe('independently derived summary');
    expect(out.verdicts[0]).not.toHaveProperty('primary_finding_index');
  });

  it('returns an empty contract for non-object / missing-verdicts input', () => {
    expect(normalizeChallengeOutput(null, CANDIDATE_SHA)).toEqual({ challenged_candidate_sha: CANDIDATE_SHA, verdicts: [] });
    expect(normalizeChallengeOutput({}, CANDIDATE_SHA)).toEqual({ challenged_candidate_sha: CANDIDATE_SHA, verdicts: [] });
    expect(normalizeChallengeOutput({ verdicts: 'nope' }, CANDIDATE_SHA)).toEqual({ challenged_candidate_sha: CANDIDATE_SHA, verdicts: [] });
  });
});

describe('parsePrimaryFindings', () => {
  it('parses findings supporting both summary/description and file/file_path keys', () => {
    const out = parsePrimaryFindings({
      primary_findings: [
        { severity: 'High', summary: 'A', file: 'a.py' },
        { level: 'medium', description: 'B', file_path: 'b.py' },
        { severity: 'low', summary: '' }, // dropped: empty summary
        'garbage', // dropped: not an object
      ],
    });
    expect(out).toEqual([
      { severity: 'high', summary: 'A', file: 'a.py' },
      { severity: 'medium', summary: 'B', file: 'b.py' },
    ]);
  });

  it('returns [] when no primary_findings are supplied', () => {
    expect(parsePrimaryFindings({})).toEqual([]);
    expect(parsePrimaryFindings({ primary_findings: 'x' })).toEqual([]);
  });
});

// ── Handler behavior ─────────────────────────────────────────────────────────
describe('handleReleaseAuditChallenge — pinned candidate + diff-range pre-flight', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
    vi.mocked(git).mockReturnValue('');
    vi.mocked(readHandlerOutput).mockReturnValue({
      challenged_candidate_sha: CANDIDATE_SHA,
      verdicts: [
        { severity: 'high', summary: 'sql injection', file: 'app/auth.py', verdict: 'confirmed', reason: 'Read app/auth.py:60 — user_id reaches SQL unsanitised, real.' },
      ],
    });
    vi.mocked(execFileSync).mockReturnValue(Buffer.from(''));
  });

  it('clones the base branch with FULL history (depth 0) and fetches the EXACT candidate SHA', async () => {
    await handleReleaseAuditChallenge(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const opts = vi.mocked(prepareWorkspaceForRole).mock.calls[0][0];
    expect(opts.branch).toBe('main');
    expect(opts.baseBranch).toBe('main');
    expect(opts.depth).toBe(0);

    const fetchCalls = vi.mocked(execFileSync).mock.calls.filter(
      c => c[0] === 'git' && Array.isArray(c[1]) && (c[1] as string[])[0] === 'fetch',
    );
    expect(fetchCalls.length).toBeGreaterThan(0);
    expect(fetchCalls[0][1] as string[]).toEqual(['fetch', 'origin', CANDIDATE_SHA]);
  });

  it('reads candidate_sha from task.context as a fallback when absent from the payload', async () => {
    const taskWithCtx = { ...BASE_TASK, context: { candidate_sha: CANDIDATE_SHA } };
    await handleReleaseAuditChallenge(TASK_ID, { base_branch: 'main' }, taskWithCtx, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const fetchCalls = vi.mocked(execFileSync).mock.calls.filter(
      c => c[0] === 'git' && Array.isArray(c[1]) && (c[1] as string[])[0] === 'fetch',
    );
    expect(fetchCalls[0][1] as string[]).toEqual(['fetch', 'origin', CANDIDATE_SHA]);
  });

  it('interpolates main...<candidate_sha> into the prompt — never the bare automain name', async () => {
    await handleReleaseAuditChallenge(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(prompt).toContain(`git diff ${DIFF_RANGE}`);
    expect(prompt).toContain(CANDIDATE_SHA);
    expect(prompt).not.toContain('git diff main...automain');
  });

  it('lists supplied primary findings in the prompt for re-judgement', async () => {
    const payload = {
      ...RELEASE_PAYLOAD,
      primary_findings: [{ severity: 'high', summary: 'unsafe deserialization', file: 'app/x.py' }],
    };
    await handleReleaseAuditChallenge(TASK_ID, payload, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(prompt).toContain('unsafe deserialization');
    expect(prompt).toContain('app/x.py');
  });

  it('requires exact summary copying and primary_finding_index when primary_findings are supplied', async () => {
    const payload = {
      ...RELEASE_PAYLOAD,
      primary_findings: [
        {
          severity: 'high',
          summary: 'Unsafe deserialization in upload handler.',
          file: 'app/upload.py',
        },
      ],
    };
    await handleReleaseAuditChallenge(TASK_ID, payload, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(prompt).toContain('`primary_finding_index` to the 1-based number from this list');
    expect(prompt).toContain('copy that finding\'s `summary` text EXACTLY');
    expect(prompt).toContain('its listed severity EXACTLY into the `severity` field');
    expect(prompt).toContain('verbatim: same words, punctuation, capitalization, and severity label; do not paraphrase');
    expect(prompt).toContain('its `summary` must be copied EXACTLY from that list');
    expect(prompt).toContain('Do not paraphrase the `summary` on a primary-findings retry');
    expect(prompt).toContain('1. [high] Unsafe deserialization in upload handler. (file: app/upload.py)');
  });

  it('allows independently derived summaries when no primary_findings are supplied', async () => {
    await handleReleaseAuditChallenge(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(prompt).toContain('No primary audit findings were supplied in your payload');
    expect(prompt).toContain('Independently identify the higher-severity issues in this diff yourself');
    expect(prompt).toContain('When no primary findings were supplied, omit `primary_finding_index`');
    expect(prompt).toContain('the backend can fuzzy-match it by content');
  });

  it('FAILS LOUD with diff_range_unresolvable when no valid candidate_sha is supplied', async () => {
    await handleReleaseAuditChallenge(TASK_ID, { base_branch: 'main' }, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(vi.mocked(prepareWorkspaceForRole)).not.toHaveBeenCalled();
    expect(vi.mocked(runClaudeCode)).not.toHaveBeenCalled();
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.artifact_type).toBe('ERROR_LOG');
    expect(call.failure_category).toBe('diff_range_unresolvable');
    expect(call.logs).toContain('candidate_sha');
  });

  it('FAILS LOUD with diff_range_unresolvable when candidate_sha is malformed', async () => {
    await handleReleaseAuditChallenge(
      TASK_ID, { base_branch: 'main', candidate_sha: 'automain' }, BASE_TASK,
      GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.failure_category).toBe('diff_range_unresolvable');
  });

  it('FAILS LOUD with diff_range_unresolvable when git rev-list does not resolve the range', async () => {
    vi.mocked(git).mockImplementation((args: string[]) => {
      if (args[0] === 'rev-list') throw new Error('bad revision');
      return '';
    });
    await handleReleaseAuditChallenge(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(vi.mocked(runClaudeCode)).not.toHaveBeenCalled();
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.failure_category).toBe('diff_range_unresolvable');
  });

  it('emits a normalized RELEASE_AUDIT_CHALLENGE artifact on success, pinning the SHA', async () => {
    // LLM wrote a wrong SHA + an out-of-enum verdict; the handler normalizes both.
    vi.mocked(readHandlerOutput).mockReturnValue({
      challenged_candidate_sha: 'WRONG',
      verdicts: [
        { severity: 'high', summary: 'real bug', file: 'a.py', verdict: 'confirmed', reason: 'Read a.py:10 and confirmed the bug is genuinely present.' },
        { severity: 'medium', summary: 'junk', file: '', verdict: 'maybe', reason: 'x' },
      ],
    });
    await handleReleaseAuditChallenge(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
    expect(call.artifact_type).toBe('RELEASE_AUDIT_CHALLENGE');
    const parsed = JSON.parse(call.logs as string) as {
      challenged_candidate_sha: string;
      verdicts: Array<{ verdict: string }>;
    };
    expect(parsed.challenged_candidate_sha).toBe(CANDIDATE_SHA);
    expect(parsed.verdicts).toHaveLength(1);
    expect(parsed.verdicts[0].verdict).toBe('confirmed');
  });

  it('keeps a valid empty challenge artifact as COMPLETED without reopening', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue({
      challenged_candidate_sha: CANDIDATE_SHA,
      verdicts: [],
    });

    await handleReleaseAuditChallenge(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(1);
    expect(recordFormatMiss).not.toHaveBeenCalled();
    expect(recordFormatRepairAttempt).not.toHaveBeenCalled();
    expect(recordFormatRepairSuccess).not.toHaveBeenCalled();

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
    expect(call.artifact_type).toBe('RELEASE_AUDIT_CHALLENGE');
    expect(JSON.parse(call.logs as string)).toEqual({
      challenged_candidate_sha: CANDIDATE_SHA,
      verdicts: [],
    });
  });

  it.each([
    [
      'missing top-level keys',
      { verdicts: [] },
      'missing_required_key',
      '{"verdicts":[]}',
    ],
    [
      'non-list verdicts field',
      { challenged_candidate_sha: CANDIDATE_SHA, verdicts: 'oops' },
      'parsed_object_schema_mismatch',
      JSON.stringify({ challenged_candidate_sha: CANDIDATE_SHA, verdicts: 'oops' }),
    ],
  ] as const)('repairs schema-wrong audit_challenge output for %s', async (_, invalidHandlerOutput, expectedMissReason, firstRunJson) => {
    vi.mocked(readHandlerOutput).mockReturnValue(invalidHandlerOutput as Record<string, unknown>);
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: firstRunJson,
        lastResultText: firstRunJson,
      })
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '',
        lastResultText: JSON.stringify({ challenged_candidate_sha: CANDIDATE_SHA, verdicts: [] }),
      });

    await handleReleaseAuditChallenge(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(2);
    expectCalledWith(recordFormatMiss, expectedMissReason, 'audit_challenge');
    expectCalledWith(recordFormatRepairAttempt, 'audit_challenge');
    expectCalledWith(recordFormatRepairSuccess, 'audit_challenge');

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
    expect(call.artifact_type).toBe('RELEASE_AUDIT_CHALLENGE');
    expect(JSON.parse(call.logs as string)).toEqual({
      challenged_candidate_sha: CANDIDATE_SHA,
      verdicts: [],
    });
  });

  it('self-repairs once then FAILS unparseable when no JSON is ever produced', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: 'no json here', lastResultText: 'still none' });

    await handleReleaseAuditChallenge(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    // First run + exactly one bounded self-repair reopen.
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(2);
    const reopenPrompt = vi.mocked(runClaudeCode).mock.calls[1][0];
    expect(reopenPrompt).toContain('Output ONLY the JSON object now');
    expect(reopenPrompt).not.toContain('/tmp/');
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.artifact_type).toBe('ERROR_LOG');
    expect(call.failure_category).toBe('release_audit_challenge_unparseable');
  });

  it('includes the primary findings list in the repair prompt when reopening', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: 'no json here', lastResultText: 'still none' })
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: JSON.stringify({ challenged_candidate_sha: CANDIDATE_SHA, verdicts: [] }) });

    await handleReleaseAuditChallenge(TASK_ID, {
      ...RELEASE_PAYLOAD,
      primary_findings: [
        { severity: 'high', summary: 'Unsafe deserialization in upload handler.', file: 'app/upload.py' },
      ],
    }, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const repairPrompt = vi.mocked(runClaudeCode).mock.calls[1][0];
    expect(repairPrompt).toContain('1. [high] Unsafe deserialization in upload handler. (file: app/upload.py)');
    expect(repairPrompt).toContain('copy the listed primary finding summary exactly into "summary"');
  });

  it('audit-challenge repair reopen includes prior output when no primary findings provided', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    const priorText = 'some attempted JSON that failed to parse properly';
    const priorAllOutput = 'extra context from the first run attempt';
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: priorAllOutput, lastResultText: priorText })
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: JSON.stringify({ challenged_candidate_sha: CANDIDATE_SHA, verdicts: [] }) });

    await handleReleaseAuditChallenge(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(2);
    const repairPrompt = vi.mocked(runClaudeCode).mock.calls[1][0];
    expect(repairPrompt).toContain(priorText);
    expect(repairPrompt).toContain(priorAllOutput);
    expect(repairPrompt).toContain('Your prior release audit challenge response did not end with a parseable JSON verdict object');
  });

  it('self-repair records audit_challenge schema telemetry and pins output basename', async () => {
    // Force the repair path: readHandlerOutput returns null, first run has no JSON,
    // repair run returns valid JSON → exercises all three schema-dimensioned telemetry calls.
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: 'no json here', lastResultText: 'no json either' })
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '',
        lastResultText: JSON.stringify({ challenged_candidate_sha: CANDIDATE_SHA, verdicts: [] }),
      });

    await handleReleaseAuditChallenge(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    // readHandlerOutput must use the live artifact basename (not a _REPORT variant)
    expectCalledWith(readHandlerOutput, TASK_ID, 'release_audit_challenge', WS);

    // All three schema-dimensioned telemetry calls must carry 'audit_challenge'
    expectCalledWith(recordFormatMiss, expect.any(String), 'audit_challenge');
    expectCalledWith(recordFormatRepairAttempt, 'audit_challenge');
    expectCalledWith(recordFormatRepairSuccess, 'audit_challenge');

    // Successful repair must emit the correct artifact type
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.artifact_type).toBe('RELEASE_AUDIT_CHALLENGE');
    expect(call.status).toBe('COMPLETED');
  });
});
