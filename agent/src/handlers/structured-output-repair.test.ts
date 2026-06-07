import { describe, it, expect, vi, beforeEach } from 'vitest';

// ── Module mocks ──────────────────────────────────────────────────────────────
// Stub the runner the same way memo.test.ts does: ../index.js re-exports
// runClaudeCode + sendLog, so mocking it lets us drive both the first run
// (passed in by the caller) and the bounded reopen run (invoked by the helper).
vi.mock('../index.js', () => ({
  sendLog: vi.fn(),
  runClaudeCode: vi.fn().mockResolvedValue({
    exitCode: 0, allOutput: '', lastResultText: '',
    codexTurnCompleted: false, stderrTail: '',
  }),
}));

// Spy on the telemetry verticals so we can assert miss / attempt / success
// fire exactly when the spec requires.
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
import { repairStructuredOutput, type RunResult } from './structured-output-repair.js';
import { runClaudeCode } from '../index.js';
import {
  recordFormatMiss, recordFormatRepairAttempt, recordFormatRepairSuccess,
} from '../telemetry.js';

// ── Shared helpers ────────────────────────────────────────────────────────────
const WS = null;
const STATUS_REF = () => ({ value: '' });
const REOPEN_PROMPT = 'Output ONLY the JSON object now. Schema: {"summary": "...", "reasoning": "..."}';

const VALID_OBJECT = JSON.stringify({
  summary: 'A concise summary.',
  reasoning: 'Detailed reasoning here.',
});

function makeFirst(overrides: Partial<RunResult>): RunResult {
  return {
    exitCode: 0, allOutput: '', lastResultText: '',
    codexTurnCompleted: true, stderrTail: '', ...overrides,
  };
}

function baseOpts(first: RunResult) {
  return {
    mode: 'object' as const,
    requiredKeys: ['summary', 'reasoning'],
    first,
    reopenPrompt: REOPEN_PROMPT,
    ws: WS,
    statusRef: STATUS_REF(),
  };
}

describe('repairStructuredOutput', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 0, allOutput: '', lastResultText: VALID_OBJECT,
      codexTurnCompleted: true, stderrTail: '',
    });
  });

  it('returns the parsed value with no retry and no telemetry when the first attempt parses', async () => {
    const res = await repairStructuredOutput(
      baseOpts(makeFirst({ lastResultText: VALID_OBJECT })),
    );

    expect(res.value).toEqual({ summary: 'A concise summary.', reasoning: 'Detailed reasoning here.' });
    expect(res.missReason).toBeNull();
    expect(res.retry).toBeNull();
    // Clean first attempt → no reopen, no telemetry of any kind.
    expect(runClaudeCode).not.toHaveBeenCalled();
    expect(recordFormatMiss).not.toHaveBeenCalled();
    expect(recordFormatRepairAttempt).not.toHaveBeenCalled();
    expect(recordFormatRepairSuccess).not.toHaveBeenCalled();
  });

  it('prefers lastResultText over allOutput when both contain a parseable object', async () => {
    const lastResult = JSON.stringify({ summary: 'from-last', reasoning: 'last-text' });
    const all = JSON.stringify({ summary: 'from-all', reasoning: 'all-output' });
    const res = await repairStructuredOutput<{ summary: string }>(
      baseOpts(makeFirst({ lastResultText: lastResult, allOutput: all })),
    );

    expect(res.value?.summary).toBe('from-last');
    expect(runClaudeCode).not.toHaveBeenCalled();
  });

  it('rescues clean-exit unparseable output via bounded re-ask and records repair telemetry', async () => {
    // First run: clean exit (exitCode 0) but the FINAL output never includes
    // the JSON object. The reopen run then emits valid JSON.
    vi.mocked(runClaudeCode).mockResolvedValueOnce({
      exitCode: 0, allOutput: VALID_OBJECT, lastResultText: VALID_OBJECT,
      codexTurnCompleted: true, stderrTail: '',
    });

    const res = await repairStructuredOutput(
      baseOpts(makeFirst({
        exitCode: 0,
        allOutput: 'prose without any valid JSON object emitted',
        lastResultText: '',
      })),
    );

    // Exactly ONE reopen, and the continuation prompt re-states the schema.
    expect(vi.mocked(runClaudeCode).mock.calls.length).toBe(1);
    expect(vi.mocked(runClaudeCode).mock.calls[0][0]).toBe(REOPEN_PROMPT);
    // Telemetry: one miss (bucketed by the verdict reason), one attempt, one
    // success — in that order.
    expect(recordFormatMiss).toHaveBeenCalledTimes(1);
    expect(recordFormatRepairAttempt).toHaveBeenCalledTimes(1);
    expect(recordFormatRepairSuccess).toHaveBeenCalledTimes(1);
    // The miss is bucketed by the extraction verdict (no JSON markers present →
    // missing_required_key), NOT a re-derived label.
    expect(vi.mocked(recordFormatMiss).mock.calls[0][0]).toBe('missing_required_key');
    // The repaired value is returned, with the retry run surfaced for the caller.
    expect(res.value).toEqual({ summary: 'A concise summary.', reasoning: 'Detailed reasoning here.' });
    expect(res.retry?.exitCode).toBe(0);
  });

  it('records a miss + attempt but NOT success when the retry is still unparseable', async () => {
    vi.mocked(runClaudeCode).mockResolvedValueOnce({
      exitCode: 0, allOutput: 'still no json', lastResultText: 'nope',
      codexTurnCompleted: true, stderrTail: '',
    });

    const res = await repairStructuredOutput(
      baseOpts(makeFirst({ exitCode: 0, allOutput: 'no json first try', lastResultText: '' })),
    );

    expect(runClaudeCode).toHaveBeenCalledTimes(1);
    expect(recordFormatMiss).toHaveBeenCalledTimes(1);
    expect(recordFormatRepairAttempt).toHaveBeenCalledTimes(1);
    expect(recordFormatRepairSuccess).not.toHaveBeenCalled();
    expect(res.value).toBeNull();
    expect(res.retry).not.toBeNull();
  });

  it('does NOT reopen when the first run exited non-zero', async () => {
    const res = await repairStructuredOutput(
      baseOpts(makeFirst({ exitCode: 1, allOutput: 'crash banner', lastResultText: '' })),
    );

    // exitCode !== 0 → CLI crash; retrying the same setup wastes a slot.
    expect(runClaudeCode).not.toHaveBeenCalled();
    expect(recordFormatMiss).not.toHaveBeenCalled();
    expect(recordFormatRepairAttempt).not.toHaveBeenCalled();
    expect(res.value).toBeNull();
    expect(res.retry).toBeNull();
  });

  it('supports array mode with element required keys, including the repair path', async () => {
    const validArray = JSON.stringify([
      { id: 'a', title: 'first' },
      { id: 'b', title: 'second' },
    ]);
    vi.mocked(runClaudeCode).mockResolvedValueOnce({
      exitCode: 0, allOutput: validArray, lastResultText: validArray,
      codexTurnCompleted: true, stderrTail: '',
    });

    const res = await repairStructuredOutput<Array<{ id: string }>>({
      mode: 'array',
      requiredKeys: ['id', 'title'],
      first: makeFirst({ exitCode: 0, allOutput: 'no array yet', lastResultText: '' }),
      reopenPrompt: REOPEN_PROMPT,
      ws: WS,
      statusRef: STATUS_REF(),
    });

    expect(runClaudeCode).toHaveBeenCalledTimes(1);
    expect(recordFormatRepairSuccess).toHaveBeenCalledTimes(1);
    expect(Array.isArray(res.value)).toBe(true);
    expect(res.value?.[0].id).toBe('a');
  });

  it('buckets a malformed-JSON first attempt as malformed_json', async () => {
    // Candidate is brace-balanced and contains both required keys, but
    // JSON.parse throws (bareword value) → bucketed as malformed_json.
    const malformed = '{"summary": "ok", "reasoning": notquoted}';
    vi.mocked(runClaudeCode).mockResolvedValueOnce({
      exitCode: 0, allOutput: '', lastResultText: VALID_OBJECT,
      codexTurnCompleted: true, stderrTail: '',
    });

    await repairStructuredOutput(
      baseOpts(makeFirst({ exitCode: 0, lastResultText: malformed })),
    );

    expect(vi.mocked(recordFormatMiss).mock.calls[0][0]).toBe('malformed_json');
  });

  it('sets retryStatus on statusRef before the reopen run', async () => {
    vi.mocked(runClaudeCode).mockResolvedValueOnce({
      exitCode: 0, allOutput: VALID_OBJECT, lastResultText: VALID_OBJECT,
      codexTurnCompleted: true, stderrTail: '',
    });
    const statusRef = { value: 'initial' };

    await repairStructuredOutput({
      ...baseOpts(makeFirst({ exitCode: 0, lastResultText: '' })),
      statusRef,
      retryStatus: 'Repairing structured output...',
    });

    expect(statusRef.value).toBe('Repairing structured output...');
  });
});
