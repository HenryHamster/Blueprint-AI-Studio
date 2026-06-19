import { describe, it, expect, beforeEach } from 'vitest';
import {
  errorFingerprint,
  computePromptFingerprint,
  setCurrentPromptFingerprint,
  getCurrentPromptFingerprint,
  recordLlmCallStart,
  recordLlmTokens,
  getLlmTelemetry,
  resetLlmTelemetry,
  recordFormatMiss,
  recordFormatRepairAttempt,
  recordFormatRepairSuccess,
  getFormatComplianceTelemetry,
  resetFormatComplianceTelemetry,
  estimateTokensFromChars,
} from './telemetry.js';
import { extractJsonObjectDetailed } from './json-extract.js';

beforeEach(() => {
  resetLlmTelemetry();
  resetFormatComplianceTelemetry();
  setCurrentPromptFingerprint(null);
});

describe('errorFingerprint', () => {
  it('collapses whitespace and trims', () => {
    expect(errorFingerprint('  foo\n\tbar  ')).toBe('foo bar');
  });
  it('truncates to 2000 chars', () => {
    expect(errorFingerprint('x'.repeat(3000)).length).toBe(2000);
  });
  it('normalizes different whitespace to same fingerprint', () => {
    expect(errorFingerprint('err:\n  at foo')).toBe(errorFingerprint('err:  at   foo'));
  });
});

describe('computePromptFingerprint', () => {
  it('returns 12-hex-char SHA-256 prefix', () => {
    const fp = computePromptFingerprint('hello world');
    expect(fp).toMatch(/^[0-9a-f]{12}$/);
  });
  it('same input produces same fingerprint', () => {
    expect(computePromptFingerprint('abc')).toBe(computePromptFingerprint('abc'));
  });
  it('different input produces different fingerprint', () => {
    expect(computePromptFingerprint('abc')).not.toBe(computePromptFingerprint('xyz'));
  });
});

describe('setCurrentPromptFingerprint / getCurrentPromptFingerprint', () => {
  it('round-trips a fingerprint value', () => {
    setCurrentPromptFingerprint('abc123');
    expect(getCurrentPromptFingerprint()).toBe('abc123');
  });
  it('returns null after reset to null', () => {
    setCurrentPromptFingerprint('fp');
    setCurrentPromptFingerprint(null);
    expect(getCurrentPromptFingerprint()).toBeNull();
  });
});

describe('LLM telemetry', () => {
  it('starts at zero', () => {
    expect(getLlmTelemetry()).toEqual({ llm_call_count: 0, total_input_tokens: 0, total_output_tokens: 0 });
  });
  it('accumulates call counts and token totals', () => {
    recordLlmCallStart();
    recordLlmTokens(100, 50);
    recordLlmCallStart();
    recordLlmTokens(200, 75);
    expect(getLlmTelemetry()).toEqual({ llm_call_count: 2, total_input_tokens: 300, total_output_tokens: 125 });
  });
  it('ignores invalid token values (NaN, negative, Infinity)', () => {
    recordLlmTokens(Number.NaN, 50);   // NaN input ignored, 50 output added
    recordLlmTokens(-5, -10);           // negatives ignored
    recordLlmTokens(100, Number.POSITIVE_INFINITY); // 100 input added, Infinity output ignored
    expect(getLlmTelemetry()).toEqual({ llm_call_count: 0, total_input_tokens: 100, total_output_tokens: 50 });
  });
  it('resets all counters', () => {
    recordLlmCallStart();
    recordLlmTokens(500, 250);
    resetLlmTelemetry();
    expect(getLlmTelemetry()).toEqual({ llm_call_count: 0, total_input_tokens: 0, total_output_tokens: 0 });
  });
});

describe('format-compliance telemetry', () => {
  it('starts at zero', () => {
    expect(getFormatComplianceTelemetry()).toEqual({
      format_miss_by_reason: {},
      format_error_total: 0,
      format_repair_attempts: 0,
      format_repair_successes: 0,
      format_repair_infra_failures: 0,
      format_miss_by_schema: {},
      format_repair_attempts_by_schema: {},
      format_repair_successes_by_schema: {},
      format_repair_infra_failures_by_schema: {},
    });
  });

  it('records a malformed-then-repaired sequence driven by real extraction verdicts', () => {
    // Initial FINAL output: schema keys present but the JSON object is truncated
    // (unbalanced) — the extraction layer is the GROUND TRUTH for the reason.
    const malformed = '{"summary": "did the thing", "reasoning": "because"';
    const firstVerdict = extractJsonObjectDetailed(malformed, 'summary', 'reasoning');
    expect(firstVerdict.ok).toBe(false);
    if (firstVerdict.ok) throw new Error('expected miss');
    expect(firstVerdict.reason).toBe('unbalanced_json');

    // Memo-handler self-repair path: record the verdict's reason, the attempt,
    // then re-extract the repaired output and record success.
    recordFormatMiss(firstVerdict.reason);
    recordFormatRepairAttempt();

    const repaired = '{"summary": "did the thing", "reasoning": "because", "citations": [], "tags": []}';
    const retryVerdict = extractJsonObjectDetailed(repaired, 'summary', 'reasoning');
    expect(retryVerdict.ok).toBe(true);
    recordFormatRepairSuccess();

    expect(getFormatComplianceTelemetry()).toEqual({
      format_miss_by_reason: { unbalanced_json: 1 },
      format_error_total: 1,
      format_repair_attempts: 1,
      format_repair_successes: 1,
      format_repair_infra_failures: 0,
      format_miss_by_schema: {},
      format_repair_attempts_by_schema: {},
      format_repair_successes_by_schema: {},
      format_repair_infra_failures_by_schema: {},
    });
  });

  it('buckets distinct reason classes separately and totals format errors', () => {
    recordFormatMiss('missing_required_key');
    recordFormatMiss('unbalanced_json');
    recordFormatMiss('unbalanced_json');
    recordFormatMiss('malformed_json');
    expect(getFormatComplianceTelemetry()).toEqual({
      format_miss_by_reason: {
        missing_required_key: 1,
        unbalanced_json: 2,
        malformed_json: 1,
      },
      format_error_total: 4,
      format_repair_attempts: 0,
      format_repair_successes: 0,
      format_repair_infra_failures: 0,
      format_miss_by_schema: {},
      format_repair_attempts_by_schema: {},
      format_repair_successes_by_schema: {},
      format_repair_infra_failures_by_schema: {},
    });
  });

  it('snapshot is a copy — mutating it does not affect internal state', () => {
    recordFormatMiss('no_json_start');
    const snap = getFormatComplianceTelemetry();
    snap.format_miss_by_reason.no_json_start = 999;
    snap.format_error_total = 999;
    expect(getFormatComplianceTelemetry()).toEqual({
      format_miss_by_reason: { no_json_start: 1 },
      format_error_total: 1,
      format_repair_attempts: 0,
      format_repair_successes: 0,
      format_repair_infra_failures: 0,
      format_miss_by_schema: {},
      format_repair_attempts_by_schema: {},
      format_repair_successes_by_schema: {},
      format_repair_infra_failures_by_schema: {},
    });
  });

  it('reset clears all format-compliance counters', () => {
    recordFormatMiss('unbalanced_json');
    recordFormatRepairAttempt();
    recordFormatRepairSuccess();
    resetFormatComplianceTelemetry();
    expect(getFormatComplianceTelemetry()).toEqual({
      format_miss_by_reason: {},
      format_error_total: 0,
      format_repair_attempts: 0,
      format_repair_successes: 0,
      format_repair_infra_failures: 0,
      format_miss_by_schema: {},
      format_repair_attempts_by_schema: {},
      format_repair_successes_by_schema: {},
      format_repair_infra_failures_by_schema: {},
    });
  });
});

describe('format-compliance telemetry — schema dimension', () => {
  it('records_format_compliance_by_schema_without_losing_totals', () => {
    // This is the named acceptance test: schema-bucketed events must accumulate
    // in the per-schema maps without losing the existing aggregate counters.
    recordFormatMiss('unbalanced_json', 'memo');
    recordFormatMiss('no_json_start', 'design');
    recordFormatMiss('unbalanced_json', 'memo');
    recordFormatRepairAttempt('memo');
    recordFormatRepairAttempt('design');
    recordFormatRepairSuccess('memo');

    const tel = getFormatComplianceTelemetry();

    // Aggregate counters are preserved.
    expect(tel.format_error_total).toBe(3);
    expect(tel.format_repair_attempts).toBe(2);
    expect(tel.format_repair_successes).toBe(1);
    expect(tel.format_miss_by_reason).toEqual({ unbalanced_json: 2, no_json_start: 1 });

    // Per-schema buckets accumulate correctly.
    expect(tel.format_miss_by_schema).toEqual({ memo: 2, design: 1 });
    expect(tel.format_repair_attempts_by_schema).toEqual({ memo: 1, design: 1 });
    expect(tel.format_repair_successes_by_schema).toEqual({ memo: 1 });
  });

  it('calls without schemaLabel do not create per-schema entries but still update aggregates', () => {
    recordFormatMiss('malformed_json');
    recordFormatRepairAttempt();
    recordFormatRepairSuccess();

    const tel = getFormatComplianceTelemetry();
    expect(tel.format_error_total).toBe(1);
    expect(tel.format_repair_attempts).toBe(1);
    expect(tel.format_repair_successes).toBe(1);
    expect(tel.format_miss_by_schema).toEqual({});
    expect(tel.format_repair_attempts_by_schema).toEqual({});
    expect(tel.format_repair_successes_by_schema).toEqual({});
  });

  it('reset clears per-schema maps', () => {
    recordFormatMiss('unbalanced_json', 'audit');
    recordFormatRepairAttempt('audit');
    recordFormatRepairSuccess('audit');
    resetFormatComplianceTelemetry();

    const tel = getFormatComplianceTelemetry();
    expect(tel.format_miss_by_schema).toEqual({});
    expect(tel.format_repair_attempts_by_schema).toEqual({});
    expect(tel.format_repair_successes_by_schema).toEqual({});
  });

  it('per-schema snapshot is a copy — mutating it does not affect internal state', () => {
    recordFormatMiss('no_json_start', 'web_research');
    const snap = getFormatComplianceTelemetry();
    (snap.format_miss_by_schema as Record<string, number>)['web_research'] = 999;

    expect(getFormatComplianceTelemetry().format_miss_by_schema).toEqual({ web_research: 1 });
  });
});

describe('estimateTokensFromChars', () => {
  it('returns ceil(chars/4)', () => {
    expect(estimateTokensFromChars(0)).toBe(0);
    expect(estimateTokensFromChars(1)).toBe(1);
    expect(estimateTokensFromChars(4)).toBe(1);
    expect(estimateTokensFromChars(5)).toBe(2);
    expect(estimateTokensFromChars(400)).toBe(100);
    expect(estimateTokensFromChars(1000)).toBe(250);
  });
});
