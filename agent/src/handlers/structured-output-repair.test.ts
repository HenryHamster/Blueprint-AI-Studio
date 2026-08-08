import { describe, it, expect, vi, beforeEach } from 'vitest';

// ── Module mocks ──────────────────────────────────────────────────────────────
// Stub the runner the same way memo.test.ts does: ../index.js re-exports
// runClaudeCode + sendLog, so mocking it lets us drive both the first run
// (passed in by the caller) and the bounded reopen run (invoked by the helper).
vi.mock('../index.js', () => ({
  normalizeClaudeConfigContent: vi.fn((_path: string, content: string) => content),
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
    recordFormatRepairMiss: vi.fn(),
    recordFormatRepairAttempt: vi.fn(),
    recordFormatRepairSuccess: vi.fn(),
    recordFormatRepairInfraFailure: vi.fn(),
    recordFormatRepairTokens: vi.fn(),
    getLlmTelemetry: vi.fn().mockReturnValue({
      llm_call_count: 0, total_input_tokens: 0, total_output_tokens: 0,
      total_cache_creation_input_tokens: 0, total_cache_read_input_tokens: 0,
      viewport_screenshot_forwarded_count: 0, num_turns: 0, total_tool_result_bytes: 0,
      prompt_size_bytes: null, prompt_section_count: null, prompt_section_sizes: null,
    }),
  };
});

// ── Imports under test ────────────────────────────────────────────────────────
import { repairStructuredOutput, type RunResult } from './structured-output-repair.js';
import { runClaudeCode } from '../index.js';
import {
  recordFormatMiss, recordFormatRepairMiss, recordFormatRepairAttempt, recordFormatRepairSuccess,
  recordFormatRepairInfraFailure, recordFormatRepairTokens, getLlmTelemetry,
  getFormatComplianceTelemetry, resetFormatComplianceTelemetry,
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

    // Exactly ONE reopen, and the continuation prompt contains the caller's schema restatement.
    expect(vi.mocked(runClaudeCode).mock.calls.length).toBe(1);
    const sentPrompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(sentPrompt).toContain(REOPEN_PROMPT);
    // The miss reason for text without the required key substrings is missing_required_key,
    // and the preamble names the specific missing keys.
    expect(sentPrompt).toContain('missing required keys');
    expect(sentPrompt).toContain('"summary"');
    // Telemetry: one miss (bucketed by the verdict reason), one attempt, one
    // success — in that order.
    expect(recordFormatMiss).toHaveBeenCalledTimes(1);
    expect(recordFormatRepairAttempt).toHaveBeenCalledTimes(1);
    expect(recordFormatRepairSuccess).toHaveBeenCalledTimes(1);
    // The miss is bucketed by the extraction verdict (no quoted key substrings →
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

  it('reopens once when parsed output fails schema validation and records schema-mismatch telemetry', async () => {
    const invalidButParseable = JSON.stringify({
      summary: 'present',
      reasoning: 'present',
      verdicts: 'not-a-list',
    });
    const repaired = JSON.stringify({
      summary: 'A concise summary.',
      reasoning: 'Detailed reasoning here.',
      verdicts: [],
    });
    vi.mocked(runClaudeCode).mockResolvedValueOnce({
      exitCode: 0, allOutput: repaired, lastResultText: repaired,
      codexTurnCompleted: true, stderrTail: '',
    });

    const res = await repairStructuredOutput({
      ...baseOpts(makeFirst({ exitCode: 0, lastResultText: invalidButParseable })),
      validateParsedValue: (value) => {
        if (
          value &&
          typeof value === 'object' &&
          !Array.isArray(value) &&
          Array.isArray((value as Record<string, unknown>).verdicts)
        ) {
          return { ok: true };
        }
        return {
          ok: false,
          reason: 'parsed_object_schema_mismatch',
          detail: 'verdicts must be a list',
        };
      },
    });

    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(1);
    expect(recordFormatMiss).toHaveBeenCalledTimes(1);
    expect(vi.mocked(recordFormatMiss).mock.calls[0][0]).toBe('parsed_object_schema_mismatch');
    expect(recordFormatRepairAttempt).toHaveBeenCalledTimes(1);
    expect(recordFormatRepairSuccess).toHaveBeenCalledTimes(1);
    const sentPrompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(sentPrompt).toContain('parsed as JSON');
    expect(res.value).toEqual({
      summary: 'A concise summary.',
      reasoning: 'Detailed reasoning here.',
      verdicts: [],
    });
    expect(res.retry?.exitCode).toBe(0);
  });

  it('surfaces the validateParsedValue detail (not the static requiredKeys list) in the reopen preamble', async () => {
    // requiredKeys only hard-gates extraction on 'summary' — 'verdict' is
    // conditionally required (only when not_a_regression), so it deliberately
    // is NOT in requiredKeys and is instead enforced by validateParsedValue.
    // The preamble must tell the model about the ACTUAL missing field
    // (verdict) rather than falling back to naming only the static
    // requiredKeys ('summary'), which would mislead the retry.
    const missingVerdict = JSON.stringify({ summary: 'present, but no verdict field' });
    const repaired = JSON.stringify({ summary: 'present', verdict: 'real_regression' });
    vi.mocked(runClaudeCode).mockResolvedValueOnce({
      exitCode: 0, allOutput: repaired, lastResultText: repaired,
      codexTurnCompleted: true, stderrTail: '',
    });

    const res = await repairStructuredOutput({
      mode: 'object',
      requiredKeys: ['summary'],
      first: makeFirst({ exitCode: 0, lastResultText: missingVerdict }),
      reopenPrompt: REOPEN_PROMPT,
      ws: WS,
      statusRef: STATUS_REF(),
      validateParsedValue: (value) => {
        const obj = value as Record<string, unknown>;
        if (typeof obj.verdict === 'string') return { ok: true };
        return {
          ok: false,
          reason: 'parsed_object_missing_key',
          detail: 'missing required fields: verdict',
        };
      },
    });

    const sentPrompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(sentPrompt).toContain('missing required fields: verdict');
    expect(sentPrompt).not.toContain('missing required keys: "summary"');
    expect(res.value).toEqual({ summary: 'present', verdict: 'real_regression' });
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

  // ── reopen prompt is tailored to the extraction miss reason ──────────────────

  it('no_json_start: preamble tells agent it emitted no JSON', async () => {
    vi.mocked(runClaudeCode).mockResolvedValueOnce({
      exitCode: 0, allOutput: VALID_OBJECT, lastResultText: VALID_OBJECT,
      codexTurnCompleted: true, stderrTail: '',
    });
    // Text contains the required keys as quoted substrings (so extractor passes
    // the key-presence check) but has no `{` → no_json_start.
    const noJsonText = 'Please provide values for "summary" and "reasoning" fields.';

    await repairStructuredOutput(
      baseOpts(makeFirst({ exitCode: 0, lastResultText: noJsonText, allOutput: '' })),
    );

    const sentPrompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(sentPrompt).toContain('no JSON object or array');
    expect(sentPrompt).toContain(REOPEN_PROMPT);
    expect(vi.mocked(recordFormatMiss).mock.calls[0][0]).toBe('no_json_start');
  });

  it('missing_required_key: preamble names the specific unmet required keys', async () => {
    vi.mocked(runClaudeCode).mockResolvedValueOnce({
      exitCode: 0, allOutput: VALID_OBJECT, lastResultText: VALID_OBJECT,
      codexTurnCompleted: true, stderrTail: '',
    });
    // Text has no quoted key substrings → missing_required_key.
    const missingKeyText = 'Here is some output without the required fields.';

    await repairStructuredOutput(
      baseOpts(makeFirst({ exitCode: 0, lastResultText: missingKeyText, allOutput: '' })),
    );

    const sentPrompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(sentPrompt).toContain('missing required keys');
    expect(sentPrompt).toContain('"summary"');
    expect(sentPrompt).toContain('"reasoning"');
    expect(sentPrompt).toContain(REOPEN_PROMPT);
    expect(vi.mocked(recordFormatMiss).mock.calls[0][0]).toBe('missing_required_key');
  });

  // ── schemaLabel threading ─────────────────────────────────────────────────────

  it('passes schemaLabel to all three telemetry calls when provided', async () => {
    vi.mocked(runClaudeCode).mockResolvedValueOnce({
      exitCode: 0, allOutput: VALID_OBJECT, lastResultText: VALID_OBJECT,
      codexTurnCompleted: true, stderrTail: '',
    });

    await repairStructuredOutput({
      ...baseOpts(makeFirst({ exitCode: 0, allOutput: 'prose without any valid JSON object emitted', lastResultText: '' })),
      schemaLabel: 'design',
    });

    expect(vi.mocked(recordFormatMiss).mock.calls[0]).toEqual(['missing_required_key', 'design']);
    expect(vi.mocked(recordFormatRepairAttempt).mock.calls[0]).toEqual(['design']);
    expect(vi.mocked(recordFormatRepairSuccess).mock.calls[0]).toEqual(['design']);
  });

  it('omits schemaLabel from telemetry calls when not provided', async () => {
    vi.mocked(runClaudeCode).mockResolvedValueOnce({
      exitCode: 0, allOutput: VALID_OBJECT, lastResultText: VALID_OBJECT,
      codexTurnCompleted: true, stderrTail: '',
    });

    await repairStructuredOutput(
      baseOpts(makeFirst({ exitCode: 0, allOutput: 'prose without any valid JSON object emitted', lastResultText: '' })),
    );

    // schemaLabel is undefined — telemetry functions receive it but their
    // internal guard (`if (schemaLabel !== undefined)`) skips the per-schema bucket.
    expect(vi.mocked(recordFormatMiss).mock.calls[0]).toEqual(['missing_required_key', undefined]);
    expect(vi.mocked(recordFormatRepairAttempt).mock.calls[0]).toEqual([undefined]);
    expect(vi.mocked(recordFormatRepairSuccess).mock.calls[0]).toEqual([undefined]);
  });

  it('does not record a repair success when validateParsedValue rejects the retry value', async () => {
    // Retry produces parseable JSON but validateParsedValue rejects it —
    // recordFormatRepairSuccess must NOT fire even though the JSON parsed.
    const parseableButRejected = JSON.stringify({ summary: 'ok', reasoning: 'ok' });
    vi.mocked(runClaudeCode).mockResolvedValueOnce({
      exitCode: 0, allOutput: parseableButRejected, lastResultText: parseableButRejected,
      codexTurnCompleted: true, stderrTail: '',
    });

    const res = await repairStructuredOutput({
      ...baseOpts(makeFirst({ exitCode: 0, lastResultText: '', allOutput: 'no json' })),
      validateParsedValue: () => ({
        ok: false,
        reason: 'parsed_object_schema_mismatch',
        detail: 'sentinel rejection for test',
      }),
    });

    expect(runClaudeCode).toHaveBeenCalledTimes(1);
    expect(recordFormatRepairAttempt).toHaveBeenCalledTimes(1);
    // The retry parsed but validateParsedValue rejected it — success must NOT be recorded.
    expect(recordFormatRepairSuccess).not.toHaveBeenCalled();
    expect(res.value).toBeNull();
    expect(res.retry).not.toBeNull();
  });

  it('parsed_array_invalid_value: preamble surfaces the validateParsedValue detail (not missing required keys)', async () => {
    vi.mocked(runClaudeCode).mockResolvedValueOnce({
      exitCode: 0, allOutput: VALID_OBJECT, lastResultText: VALID_OBJECT,
      codexTurnCompleted: true, stderrTail: '',
    });
    const validArrayButBadValues = JSON.stringify([{ summary: 'ok', reasoning: 'ok' }]);

    await repairStructuredOutput({
      ...baseOpts(makeFirst({ exitCode: 0, lastResultText: validArrayButBadValues })),
      mode: 'array',
      requiredKeys: ['summary', 'reasoning'],
      validateParsedValue: () => ({
        ok: false,
        reason: 'parsed_array_invalid_value',
        detail: 'unrecognized verdict strings: uncertain',
      }),
    });

    const sentPrompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    // The validator's specific detail is surfaced verbatim (more actionable
    // than the generic fallback) since a detail was supplied.
    expect(sentPrompt).toContain('unrecognized verdict strings: uncertain');
    expect(sentPrompt).not.toContain('missing required keys');
    expect(sentPrompt).toContain(REOPEN_PROMPT);
    expect(vi.mocked(recordFormatMiss).mock.calls[0][0]).toBe('parsed_array_invalid_value');
  });

  it('malformed_json: preamble tells agent the JSON was incomplete or malformed', async () => {
    vi.mocked(runClaudeCode).mockResolvedValueOnce({
      exitCode: 0, allOutput: VALID_OBJECT, lastResultText: VALID_OBJECT,
      codexTurnCompleted: true, stderrTail: '',
    });
    // Contains both keys and `{}`  but JSON.parse throws (bareword value) →
    // JsonExtractionError → bucketed as malformed_json.
    const malformedText = '{"summary": "ok", "reasoning": notquoted}';

    await repairStructuredOutput(
      baseOpts(makeFirst({ exitCode: 0, lastResultText: malformedText, allOutput: '' })),
    );

    const sentPrompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(sentPrompt).toContain('incomplete or malformed JSON');
    expect(sentPrompt).toContain(REOPEN_PROMPT);
    expect(vi.mocked(recordFormatMiss).mock.calls[0][0]).toBe('malformed_json');
  });

  // ── infra-failure classification (reopen exitCode !== 0) ─────────────────────

  it('adopts valid output and records only infra failure (not success) when reopen exitCode is nonzero', async () => {
    // Reopen run exits non-zero (e.g. filesystem warning) but emits parseable
    // output. The recovered value MUST be returned, but repair success must NOT
    // be recorded. The backend formula excludes infra-failure attempts from the
    // denominator; recording success here too would inflate the rate above 1.0.
    vi.mocked(runClaudeCode).mockResolvedValueOnce({
      exitCode: 1, allOutput: VALID_OBJECT, lastResultText: VALID_OBJECT,
      codexTurnCompleted: false, stderrTail: 'process crashed',
    });

    const res = await repairStructuredOutput({
      ...baseOpts(makeFirst({ exitCode: 0, allOutput: 'no json here', lastResultText: '' })),
      schemaLabel: 'design',
    });

    expect(runClaudeCode).toHaveBeenCalledTimes(1);
    expect(recordFormatRepairAttempt).toHaveBeenCalledTimes(1);
    expect(recordFormatRepairInfraFailure).toHaveBeenCalledTimes(1);
    expect(vi.mocked(recordFormatRepairInfraFailure).mock.calls[0]).toEqual(['design']);
    // Crash recovery must NOT increment the success counter.
    expect(recordFormatRepairSuccess).not.toHaveBeenCalled();
    expect(res.value).toEqual({ summary: 'A concise summary.', reasoning: 'Detailed reasoning here.' });
    expect(res.retry?.exitCode).toBe(1);
  });

  it('crash-then-recover does not inflate self_repair_success_rate above 1.0', async () => {
    // Simulate two tasks: one clean success (exitCode=0) and one crash-recover
    // (exitCode=1 but valid output). The backend rate = successes / (attempts -
    // infra_failures). With the fix: successes=1, attempts=2, infra_failures=1
    // => rate = 1/(2-1) = 1.0, bounded. Without the fix: successes=2 => rate > 1.
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({
        // Task 1: clean reopen success
        exitCode: 0, allOutput: VALID_OBJECT, lastResultText: VALID_OBJECT,
        codexTurnCompleted: true, stderrTail: '',
      })
      .mockResolvedValueOnce({
        // Task 2: crash but parseable output
        exitCode: 1, allOutput: VALID_OBJECT, lastResultText: VALID_OBJECT,
        codexTurnCompleted: false, stderrTail: 'oom',
      });

    await repairStructuredOutput(
      baseOpts(makeFirst({ exitCode: 0, allOutput: 'no json', lastResultText: '' })),
    );
    vi.clearAllMocks();
    await repairStructuredOutput(
      baseOpts(makeFirst({ exitCode: 0, allOutput: 'no json', lastResultText: '' })),
    );

    // Task 2: infra failure recorded, success NOT recorded
    expect(recordFormatRepairInfraFailure).toHaveBeenCalledTimes(1);
    expect(recordFormatRepairSuccess).not.toHaveBeenCalled();
  });

  it('records only infra failure (not success) when reopen exitCode is nonzero and output is unparseable', async () => {
    vi.mocked(runClaudeCode).mockResolvedValueOnce({
      exitCode: 1, allOutput: '', lastResultText: '',
      codexTurnCompleted: false, stderrTail: 'oom',
    });

    const res = await repairStructuredOutput({
      ...baseOpts(makeFirst({ exitCode: 0, allOutput: 'no json here', lastResultText: '' })),
      schemaLabel: 'design',
    });

    expect(recordFormatRepairInfraFailure).toHaveBeenCalledTimes(1);
    expect(recordFormatRepairSuccess).not.toHaveBeenCalled();
    expect(res.value).toBeNull();
    expect(res.retry?.exitCode).toBe(1);
  });

  // ── recordFormatRepairMiss: reopen failure reason tracking ────────────────────

  it('records repair miss reason (missing_required_key) when reopen still yields no parseable value', async () => {
    vi.mocked(runClaudeCode).mockResolvedValueOnce({
      exitCode: 0, allOutput: 'still no json', lastResultText: 'nope',
      codexTurnCompleted: true, stderrTail: '',
    });

    await repairStructuredOutput(
      baseOpts(makeFirst({ exitCode: 0, allOutput: 'no json first try', lastResultText: '' })),
    );

    expect(recordFormatRepairMiss).toHaveBeenCalledTimes(1);
    expect(vi.mocked(recordFormatRepairMiss).mock.calls[0][0]).toBe('missing_required_key');
  });

  it('records repair miss reason (parsed_object_schema_mismatch) when validateParsedValue rejects the retry value', async () => {
    const parseableButRejected = JSON.stringify({ summary: 'ok', reasoning: 'ok' });
    vi.mocked(runClaudeCode).mockResolvedValueOnce({
      exitCode: 0, allOutput: parseableButRejected, lastResultText: parseableButRejected,
      codexTurnCompleted: true, stderrTail: '',
    });

    await repairStructuredOutput({
      ...baseOpts(makeFirst({ exitCode: 0, lastResultText: '', allOutput: 'no json' })),
      validateParsedValue: () => ({
        ok: false,
        reason: 'parsed_object_schema_mismatch',
        detail: 'sentinel rejection for test',
      }),
    });

    expect(recordFormatRepairMiss).toHaveBeenCalledTimes(1);
    expect(vi.mocked(recordFormatRepairMiss).mock.calls[0][0]).toBe('parsed_object_schema_mismatch');
  });

  it('does NOT record repair miss when reopen succeeds', async () => {
    vi.mocked(runClaudeCode).mockResolvedValueOnce({
      exitCode: 0, allOutput: VALID_OBJECT, lastResultText: VALID_OBJECT,
      codexTurnCompleted: true, stderrTail: '',
    });

    const res = await repairStructuredOutput(
      baseOpts(makeFirst({ exitCode: 0, allOutput: 'no json first try', lastResultText: '' })),
    );

    expect(recordFormatRepairSuccess).toHaveBeenCalledTimes(1);
    expect(recordFormatRepairMiss).not.toHaveBeenCalled();
    expect(res.value).not.toBeNull();
  });

  it('passes schemaLabel to recordFormatRepairMiss when reopen fails', async () => {
    vi.mocked(runClaudeCode).mockResolvedValueOnce({
      exitCode: 0, allOutput: 'still no json', lastResultText: 'nope',
      codexTurnCompleted: true, stderrTail: '',
    });

    await repairStructuredOutput({
      ...baseOpts(makeFirst({ exitCode: 0, allOutput: 'no json', lastResultText: '' })),
      schemaLabel: 'thinker',
    });

    expect(vi.mocked(recordFormatRepairMiss).mock.calls[0]).toEqual(['missing_required_key', 'thinker']);
  });

  it('does NOT record repair miss on infra failure (exitCode !== 0) even when reopen output is unparseable', async () => {
    vi.mocked(runClaudeCode).mockResolvedValueOnce({
      exitCode: 1, allOutput: '', lastResultText: '',
      codexTurnCompleted: false, stderrTail: 'oom',
    });

    await repairStructuredOutput(
      baseOpts(makeFirst({ exitCode: 0, allOutput: 'no json', lastResultText: '' })),
    );

    expect(recordFormatRepairInfraFailure).toHaveBeenCalledTimes(1);
    expect(recordFormatRepairMiss).not.toHaveBeenCalled();
  });

  it('format_repair_miss_by_reason field is present in getFormatComplianceTelemetry snapshot', () => {
    const snap = getFormatComplianceTelemetry();
    expect(Object.prototype.hasOwnProperty.call(snap, 'format_repair_miss_by_reason')).toBe(true);
    expect(typeof snap.format_repair_miss_by_reason).toBe('object');
  });

  it('resetFormatComplianceTelemetry clears format_repair_miss_by_reason', () => {
    resetFormatComplianceTelemetry();
    const snap = getFormatComplianceTelemetry();
    expect(snap.format_repair_miss_by_reason).toEqual({});
  });

  it('carries the first pass output into the reopen prompt as bounded context', async () => {
    // Simulates a merge-grade first pass: the agent produced reasoning prose but
    // never emitted the required JSON keys. The reopen must include that prior
    // output so the agent can reformat rather than re-derive from scratch.
    const firstPassOutput = [
      'I reviewed the PR and found that the changes address the stated goal.',
      'The implementation follows existing patterns in the codebase.',
      'Overall assessment: the merge looks safe with only minor concerns.',
    ].join('\n');
    vi.mocked(runClaudeCode).mockResolvedValueOnce({
      exitCode: 0, allOutput: VALID_OBJECT, lastResultText: VALID_OBJECT,
      codexTurnCompleted: true, stderrTail: '',
    });

    await repairStructuredOutput(
      baseOpts(makeFirst({ exitCode: 0, lastResultText: firstPassOutput, allOutput: '' })),
    );

    const sentPrompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    // The first pass output must appear verbatim in the reopen prompt.
    expect(sentPrompt).toContain(firstPassOutput);
    // The caller's schema restatement must still be present.
    expect(sentPrompt).toContain(REOPEN_PROMPT);
    // The miss-aware preamble must also be present.
    expect(sentPrompt).toContain('missing required keys');
  });

  it('truncates prior output to the bounded tail when first pass output exceeds the limit', async () => {
    // 4001-char first pass — reopen prompt must carry only the last 4000 chars.
    const longOutput = 'x'.repeat(4001);
    vi.mocked(runClaudeCode).mockResolvedValueOnce({
      exitCode: 0, allOutput: VALID_OBJECT, lastResultText: VALID_OBJECT,
      codexTurnCompleted: true, stderrTail: '',
    });

    await repairStructuredOutput(
      baseOpts(makeFirst({ exitCode: 0, lastResultText: longOutput, allOutput: '' })),
    );

    const sentPrompt = vi.mocked(runClaudeCode).mock.calls[0][0];
    // The leading character must be trimmed; the tail (last 4000 x's) must survive.
    expect(sentPrompt).not.toContain('x'.repeat(4001));
    expect(sentPrompt).toContain('x'.repeat(4000));
    expect(sentPrompt).toContain(REOPEN_PROMPT);
  });

  it('does NOT record infra failure when reopen exitCode is zero but output is unparseable', async () => {
    vi.mocked(runClaudeCode).mockResolvedValueOnce({
      exitCode: 0, allOutput: 'still no json', lastResultText: 'nope',
      codexTurnCompleted: true, stderrTail: '',
    });

    await repairStructuredOutput(
      baseOpts(makeFirst({ exitCode: 0, allOutput: 'no json first try', lastResultText: '' })),
    );

    expect(recordFormatRepairInfraFailure).not.toHaveBeenCalled();
    expect(recordFormatRepairSuccess).not.toHaveBeenCalled();
  });

  it('records infra failure without schemaLabel when none provided', async () => {
    vi.mocked(runClaudeCode).mockResolvedValueOnce({
      exitCode: 2, allOutput: '', lastResultText: '',
      codexTurnCompleted: false, stderrTail: 'oom',
    });

    await repairStructuredOutput(
      baseOpts(makeFirst({ exitCode: 0, allOutput: 'no json', lastResultText: '' })),
    );

    expect(recordFormatRepairInfraFailure).toHaveBeenCalledTimes(1);
    expect(vi.mocked(recordFormatRepairInfraFailure).mock.calls[0]).toEqual([undefined]);
    expect(recordFormatRepairSuccess).not.toHaveBeenCalled();
  });

  it('records repair token delta via recordFormatRepairTokens with schemaLabel when the reopen run consumes tokens', async () => {
    // Simulate getLlmTelemetry returning different snapshots before and after the reopen run.
    vi.mocked(getLlmTelemetry)
      .mockReturnValueOnce({ llm_call_count: 3, total_input_tokens: 500, total_output_tokens: 200, total_cache_creation_input_tokens: 0, total_cache_read_input_tokens: 0, viewport_screenshot_forwarded_count: 0, num_turns: 0, total_tool_result_bytes: 0, prompt_size_bytes: null, prompt_section_count: null, prompt_section_sizes: null })
      .mockReturnValueOnce({ llm_call_count: 4, total_input_tokens: 900, total_output_tokens: 450, total_cache_creation_input_tokens: 0, total_cache_read_input_tokens: 0, viewport_screenshot_forwarded_count: 0, num_turns: 0, total_tool_result_bytes: 0, prompt_size_bytes: null, prompt_section_count: null, prompt_section_sizes: null });

    vi.mocked(runClaudeCode).mockResolvedValueOnce({
      exitCode: 0, allOutput: VALID_OBJECT, lastResultText: VALID_OBJECT,
      codexTurnCompleted: true, stderrTail: '',
    });

    await repairStructuredOutput({
      ...baseOpts(makeFirst({ exitCode: 0, allOutput: 'no json first try', lastResultText: '' })),
      schemaLabel: 'thinker',
    });

    // getLlmTelemetry must be called before and after the reopen run.
    expect(getLlmTelemetry).toHaveBeenCalledTimes(2);
    // recordFormatRepairTokens must receive the delta (input: 400, output: 250) and schemaLabel.
    expect(recordFormatRepairTokens).toHaveBeenCalledTimes(1);
    expect(vi.mocked(recordFormatRepairTokens).mock.calls[0]).toEqual([400, 250, 'thinker']);
  });
});
