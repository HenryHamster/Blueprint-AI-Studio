import { describe, it, expect, vi, beforeEach } from 'vitest';
import { expectCalledWith } from '../utils/typed-mock-call.js';

// ── Module mocks (mirror audit-challenge.test.ts) ────────────────────────────
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

// ── Imports under test ───────────────────────────────────────────────────────
import './index.js';
import {
  handleReleaseMergeGrade,
  normalizeMergeGradeOutput,
  validateMergeGradeShape,
} from './merge-grade.js';
import {
  persistAndPostStatus, readHandlerOutput, runClaudeCode, git,
} from '../index.js';
import { execFileSync } from 'child_process';
import { prepareWorkspaceForRole } from '../workspacePrep.js';
import { recordFormatMiss, recordFormatRepairAttempt, recordFormatRepairSuccess, getLlmTelemetry, resetLlmTelemetry } from '../telemetry.js';

// ── Shared test helpers ──────────────────────────────────────────────────────
const TASK_ID = 'task-grade-1';
const GH_TOKEN = 'gh-token';
const OWNER = 'owner';
const REPO = 'repo';
const WS = null;
const STATUS_REF = () => ({ value: '' });
const BASE_TASK = {} as Record<string, unknown>;

// A full 40-char hex SHA (the backend pins automain HEAD this way).
const CANDIDATE_SHA = 'a1b2c3d4e5f60718293a4b5c6d7e8f9012345678';
const DIFF_RANGE = `main...${CANDIDATE_SHA}`;
const VERSION_GOAL = 'Ship a persisted blind merge-grade gate that blocks stabilize→qualify.';
const RELEASE_PAYLOAD = {
  base_branch: 'main',
  candidate_sha: CANDIDATE_SHA,
  version_goal: VERSION_GOAL,
};

const VALID_GRADE = {
  graded_candidate_sha: CANDIDATE_SHA,
  covers_goal: false,
  coverage_score: 0.4,
  biggest_gap: 'No backend gate wiring is present in the merged diff — only the agent handler landed.',
  rationale: 'Read release_qualifier.py and the new handler: nothing reads the grade or blocks qualify.',
};

// ── Pure-function contract: validateMergeGradeShape ──────────────────────────
describe('validateMergeGradeShape — RELEASE_MERGE_GRADE schema gate', () => {
  it('accepts a well-formed grade with a non-empty biggest_gap and a coverage signal', () => {
    expect(validateMergeGradeShape(VALID_GRADE).ok).toBe(true);
  });

  it('accepts coverage_score alone as the coverage signal (no covers_goal boolean)', () => {
    const res = validateMergeGradeShape({ coverage_score: 0.9, biggest_gap: 'minor doc gap remains.' });
    expect(res.ok).toBe(true);
  });

  it('accepts a missing biggest_gap when covers_goal=true (goal fully met)', () => {
    const res = validateMergeGradeShape({ covers_goal: true, coverage_score: 1 });
    expect(res.ok).toBe(true);
  });

  it('accepts an empty biggest_gap when covers_goal=true', () => {
    const res = validateMergeGradeShape({ covers_goal: true, biggest_gap: '', coverage_score: 1 });
    expect(res.ok).toBe(true);
  });

  it('rejects a missing biggest_gap when covers_goal=false', () => {
    const res = validateMergeGradeShape({ covers_goal: false, coverage_score: 0 });
    expect(res.ok).toBe(false);
    if (!res.ok) {
      expect(res.reason).toBe('parsed_object_schema_mismatch');
      expect(res.detail).toContain('biggest_gap');
    }
  });

  it('rejects an empty / whitespace-only biggest_gap', () => {
    expect(validateMergeGradeShape({ covers_goal: false, biggest_gap: '' }).ok).toBe(false);
    expect(validateMergeGradeShape({ covers_goal: false, biggest_gap: '   ' }).ok).toBe(false);
  });

  it('rejects output with no coverage signal at all', () => {
    const res = validateMergeGradeShape({ biggest_gap: 'something is missing here.' });
    expect(res.ok).toBe(false);
    if (!res.ok) expect(res.detail).toContain('coverage');
  });

  it('rejects non-object input', () => {
    expect(validateMergeGradeShape(null).ok).toBe(false);
    expect(validateMergeGradeShape([VALID_GRADE]).ok).toBe(false);
    expect(validateMergeGradeShape('nope').ok).toBe(false);
  });
});

// ── Pure-function contract: normalizeMergeGradeOutput ────────────────────────
describe('normalizeMergeGradeOutput — stable contract shape', () => {
  it('forces graded_candidate_sha to the pinned SHA regardless of LLM output', () => {
    const out = normalizeMergeGradeOutput({ ...VALID_GRADE, graded_candidate_sha: 'WRONG' }, CANDIDATE_SHA);
    expect(out.graded_candidate_sha).toBe(CANDIDATE_SHA);
  });

  it('clamps an out-of-range coverage_score into [0, 1]', () => {
    expect(normalizeMergeGradeOutput({ coverage_score: 1.7, biggest_gap: 'x' }, CANDIDATE_SHA).coverage_score).toBe(1);
    expect(normalizeMergeGradeOutput({ coverage_score: -3, biggest_gap: 'x' }, CANDIDATE_SHA).coverage_score).toBe(0);
  });

  it('derives covers_goal from coverage_score when the boolean is absent', () => {
    expect(normalizeMergeGradeOutput({ coverage_score: 0.8, biggest_gap: 'x' }, CANDIDATE_SHA).covers_goal).toBe(true);
    expect(normalizeMergeGradeOutput({ coverage_score: 0.2, biggest_gap: 'x' }, CANDIDATE_SHA).covers_goal).toBe(false);
  });

  it('derives coverage_score from covers_goal when the number is absent', () => {
    expect(normalizeMergeGradeOutput({ covers_goal: true, biggest_gap: 'x' }, CANDIDATE_SHA).coverage_score).toBe(1);
    expect(normalizeMergeGradeOutput({ covers_goal: false, biggest_gap: 'x' }, CANDIDATE_SHA).coverage_score).toBe(0);
  });

  it('trims biggest_gap and accepts reason as an alias for rationale', () => {
    const out = normalizeMergeGradeOutput(
      { covers_goal: false, biggest_gap: '  the gate is not wired.  ', reason: 'because nothing reads it' },
      CANDIDATE_SHA,
    );
    expect(out.biggest_gap).toBe('the gate is not wired.');
    expect(out.rationale).toBe('because nothing reads it');
  });

  it('returns a safe default shape for non-object input', () => {
    const out = normalizeMergeGradeOutput(null, CANDIDATE_SHA);
    expect(out).toEqual({
      graded_candidate_sha: CANDIDATE_SHA,
      covers_goal: false,
      coverage_score: 0,
      biggest_gap: '',
      rationale: '',
      // Goal-verification redesign: per-category grade defaults to empty / null.
      category_verdicts: [],
      residual_uncaptured_gap: null,
      // Direction scope discriminator defaults to '' (version-scoped path).
      direction_id: '',
    });
  });

  it('echoes the direction scope discriminator when supplied', () => {
    const out = normalizeMergeGradeOutput(
      { covers_goal: false, biggest_gap: 'objective not yet met' },
      CANDIDATE_SHA,
      'd-1a2b3c4d5e6f',
    );
    expect(out.direction_id).toBe('d-1a2b3c4d5e6f');
    // The version-scoped path (no direction) leaves it empty.
    expect(normalizeMergeGradeOutput({ covers_goal: true }, CANDIDATE_SHA).direction_id).toBe('');
  });

  it('coerces per-category verdicts + residual (goal-verification redesign)', () => {
    const out = normalizeMergeGradeOutput(
      {
        covers_goal: false,
        coverage_score: 0.4,
        biggest_gap: 'ui not wired',
        category_verdicts: [
          { category_key: 'c-1', status: 'met', citations: [{ kind: 'diff', ref: 'api.py:3' }] },
          { category_key: 'c-2', status: 'weird', gap: 'not done', citations: ['ui.tsx'] },
          { status: 'met' }, // no key → dropped
        ],
        residual_uncaptured_gap: 'no migration for the widget table',
      },
      CANDIDATE_SHA,
    );
    expect(out.category_verdicts).toEqual([
      { category_key: 'c-1', status: 'met', gap: null, citations: [{ kind: 'diff', ref: 'api.py:3' }] },
      // unknown status coerces to not_met; bare-string citation → diff kind
      { category_key: 'c-2', status: 'not_met', gap: 'not done', citations: [{ kind: 'diff', ref: 'ui.tsx' }] },
    ]);
    expect(out.residual_uncaptured_gap).toBe('no migration for the widget table');
  });
});

// ── Handler behavior ─────────────────────────────────────────────────────────
describe('handleReleaseMergeGrade — pinned candidate + diff-range pre-flight', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
    vi.mocked(git).mockReturnValue('');
    vi.mocked(readHandlerOutput).mockReturnValue({ ...VALID_GRADE });
    vi.mocked(execFileSync).mockReturnValue(Buffer.from(''));
  });

  it('clones the base branch with FULL history (depth 0) and fetches the EXACT candidate SHA', async () => {
    await handleReleaseMergeGrade(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

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
    await handleReleaseMergeGrade(TASK_ID, { base_branch: 'main', version_goal: VERSION_GOAL }, taskWithCtx, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const fetchCalls = vi.mocked(execFileSync).mock.calls.filter(
      c => c[0] === 'git' && Array.isArray(c[1]) && (c[1] as string[])[0] === 'fetch',
    );
    expect(fetchCalls[0][1] as string[]).toEqual(['fetch', 'origin', CANDIDATE_SHA]);
  });

  it('interpolates main...<candidate_sha> and the version goal into the prompt', async () => {
    await handleReleaseMergeGrade(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(prompt).toContain(`git diff ${DIFF_RANGE}`);
    expect(prompt).toContain(CANDIDATE_SHA);
    expect(prompt).toContain(VERSION_GOAL);
  });

  it('accepts the legacy `goal` payload key as the version goal', async () => {
    await handleReleaseMergeGrade(
      TASK_ID, { base_branch: 'main', candidate_sha: CANDIDATE_SHA, goal: 'legacy goal text' },
      BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF(),
    );
    const prompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(prompt).toContain('legacy goal text');
  });

  it('lists the merged-PR window in the prompt when supplied', async () => {
    const payload = {
      ...RELEASE_PAYLOAD,
      merged_prs: [{ number: 11076, title: 'add merge-grade backend gate' }, '#11078 add fetch verb'],
    };
    await handleReleaseMergeGrade(TASK_ID, payload, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(prompt).toContain('#11076 add merge-grade backend gate');
    expect(prompt).toContain('#11078 add fetch verb');
  });

  it('is BLIND: never interpolates a strategist running_update into the prompt', async () => {
    // Even if the payload were polluted with a running-update narrative, the
    // handler must grade strictly against the merged diff and never surface it.
    const polluted = { ...RELEASE_PAYLOAD, running_update: 'SECRET STRATEGIST NARRATIVE OF PROGRESS' };
    await handleReleaseMergeGrade(TASK_ID, polluted, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    const prompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(prompt).not.toContain('SECRET STRATEGIST NARRATIVE OF PROGRESS');
    expect(prompt).not.toContain('running_update');
    // The blindness rule is stated affirmatively in the prompt.
    expect(prompt).toContain('BLINDNESS RULE');
  });

  it('FAILS LOUD with diff_range_unresolvable when no valid candidate_sha is supplied', async () => {
    await handleReleaseMergeGrade(TASK_ID, { base_branch: 'main', version_goal: VERSION_GOAL }, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(vi.mocked(prepareWorkspaceForRole)).not.toHaveBeenCalled();
    expect(vi.mocked(runClaudeCode)).not.toHaveBeenCalled();
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.artifact_type).toBe('ERROR_LOG');
    expect(call.failure_category).toBe('diff_range_unresolvable');
    expect(call.logs).toContain('candidate_sha');
  });

  it('FAILS LOUD with diff_range_unresolvable when candidate_sha is malformed', async () => {
    await handleReleaseMergeGrade(
      TASK_ID, { base_branch: 'main', candidate_sha: 'automain', version_goal: VERSION_GOAL }, BASE_TASK,
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
    await handleReleaseMergeGrade(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(vi.mocked(runClaudeCode)).not.toHaveBeenCalled();
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.failure_category).toBe('diff_range_unresolvable');
  });

  it('FAILS LOUD with diff_range_unresolvable when the range is EMPTY (0 commits)', async () => {
    // `git rev-list --count main...<sha>` prints "0" and exits 0 when base and
    // candidate resolve to the same commit (the direction-grade terminal-rollup
    // path IF the spawner had pinned the integration branch as base). The
    // pre-flight must treat a zero-count range as unresolvable and FAIL the
    // grade rather than grading a blank surface as not-met.
    vi.mocked(git).mockImplementation((args: string[]) => {
      if (args[0] === 'rev-parse') return CANDIDATE_SHA;
      if (args[0] === 'rev-list') return '0'; // EMPTY symmetric difference
      return '';
    });
    await handleReleaseMergeGrade(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(vi.mocked(runClaudeCode)).not.toHaveBeenCalled();
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.failure_category).toBe('diff_range_unresolvable');
    expect(call.logs).toContain(DIFF_RANGE);
    expect(call.logs).toContain('EMPTY');
  });

  it('FAILS LOUD with diff_range_unresolvable when the candidate fetch fails', async () => {
    vi.mocked(execFileSync).mockImplementation((_cmd: string, args?: readonly string[]) => {
      if (Array.isArray(args) && args[0] === 'fetch') throw new Error('fetch failed');
      return Buffer.from('');
    });
    await handleReleaseMergeGrade(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(vi.mocked(runClaudeCode)).not.toHaveBeenCalled();
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.failure_category).toBe('diff_range_unresolvable');
  });

  it('RETRIES a transient candidate fetch and proceeds to grade once it succeeds', async () => {
    // A single transient blip on `git fetch origin <sha>` must NOT torch the
    // qualification attempt: the fetch retries and the merge grade runs.
    let fetchAttempts = 0;
    vi.mocked(execFileSync).mockImplementation((_cmd: string, args?: readonly string[]) => {
      if (Array.isArray(args) && args[0] === 'fetch') {
        fetchAttempts += 1;
        if (fetchAttempts < 2) throw new Error('fatal: could not read from remote repository');
      }
      return Buffer.from('');
    });

    await handleReleaseMergeGrade(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(fetchAttempts).toBe(2); // retried once, then succeeded
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(1);
    const failedCall = vi.mocked(persistAndPostStatus).mock.calls.find(
      c => c[1].failure_category === 'diff_range_unresolvable',
    );
    expect(failedCall).toBeUndefined();
  });

  it('attempts the candidate fetch 3 times before failing loud (retries exhausted)', async () => {
    let fetchAttempts = 0;
    vi.mocked(execFileSync).mockImplementation((_cmd: string, args?: readonly string[]) => {
      if (Array.isArray(args) && args[0] === 'fetch') {
        fetchAttempts += 1;
        throw new Error('network down');
      }
      return Buffer.from('');
    });

    await handleReleaseMergeGrade(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(fetchAttempts).toBe(3);
    expect(vi.mocked(runClaudeCode)).not.toHaveBeenCalled();
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.failure_category).toBe('diff_range_unresolvable');
  });

  it('emits a normalized RELEASE_MERGE_GRADE artifact on success, pinning the SHA', async () => {
    // LLM wrote a wrong SHA + out-of-range score; the handler normalizes both.
    vi.mocked(readHandlerOutput).mockReturnValue({
      graded_candidate_sha: 'WRONG',
      covers_goal: false,
      coverage_score: 2,
      biggest_gap: 'The persisted gate is missing — nothing blocks qualify.',
      rationale: 'Read the diff; only the handler landed.',
    });
    await handleReleaseMergeGrade(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
    expect(call.artifact_type).toBe('RELEASE_MERGE_GRADE');
    const parsed = JSON.parse(call.logs as string) as {
      graded_candidate_sha: string; coverage_score: number; biggest_gap: string;
    };
    expect(parsed.graded_candidate_sha).toBe(CANDIDATE_SHA);
    expect(parsed.coverage_score).toBe(1);
    expect(parsed.biggest_gap).toBe('The persisted gate is missing — nothing blocks qualify.');
  });

  it('reopens once to repair a grade missing biggest_gap when goal not fully covered, then COMPLETES', async () => {
    // First handler-file read parses but is schema-invalid (covers_goal=false without biggest_gap) →
    // the bounded reopen path runs and the retry returns a valid grade.
    vi.mocked(readHandlerOutput).mockReturnValue({ covers_goal: false, coverage_score: 0.5 });
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: JSON.stringify({ covers_goal: false, coverage_score: 0.5 }),
        lastResultText: JSON.stringify({ covers_goal: false, coverage_score: 0.5 }),
      })
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '',
        lastResultText: JSON.stringify(VALID_GRADE),
      });

    await handleReleaseMergeGrade(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(2);
    // The first-run output parses but omits the required biggest_gap key when goal not fully covered,
    // so the extractor's ground-truth miss reason is missing_required_key.
    expectCalledWith(recordFormatMiss, 'missing_required_key', 'merge_grade');
    expectCalledWith(recordFormatRepairAttempt, 'merge_grade');
    expectCalledWith(recordFormatRepairSuccess, 'merge_grade');

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
    expect(call.artifact_type).toBe('RELEASE_MERGE_GRADE');
    const parsed = JSON.parse(call.logs as string) as { biggest_gap: string };
    expect(parsed.biggest_gap).toBe(VALID_GRADE.biggest_gap);
  });

  it('self-repairs once then FAILS unparseable when no JSON is ever produced', async () => {
    vi.mocked(readHandlerOutput).mockReturnValue(null);
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: 'no json here', lastResultText: 'still none' });

    await handleReleaseMergeGrade(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    // First run + exactly one bounded self-repair reopen.
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(2);
    const reopenPrompt = vi.mocked(runClaudeCode).mock.calls[1][0];
    expect(reopenPrompt).toContain('Output ONLY the JSON object now');
    expect(reopenPrompt).not.toContain('/tmp/');
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.artifact_type).toBe('ERROR_LOG');
    expect(call.failure_category).toBe('release_merge_grade_unparseable');
  });

  it('reads the live release_merge_grade artifact basename', async () => {
    await handleReleaseMergeGrade(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    expectCalledWith(readHandlerOutput, TASK_ID, 'release_merge_grade', WS);
  });

  it('records prompt size telemetry before calling runClaudeCode', async () => {
    resetLlmTelemetry();

    await handleReleaseMergeGrade(TASK_ID, RELEASE_PAYLOAD, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

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
