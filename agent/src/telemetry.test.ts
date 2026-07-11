import { describe, it, expect, beforeEach } from 'vitest';
import {
  errorFingerprint,
  computePromptFingerprint,
  setCurrentPromptFingerprint,
  getCurrentPromptFingerprint,
  recordLlmCallStart,
  recordLlmTokens,
  recordNumTurns,
  recordViewportScreenshotDownload,
  recordToolResultBytes,
  getLlmTelemetry,
  resetLlmTelemetry,
  recordFormatMiss,
  recordFormatRepairMiss,
  recordFormatRepairAttempt,
  recordFormatRepairSuccess,
  recordFormatRepairTokens,
  getFormatComplianceTelemetry,
  resetFormatComplianceTelemetry,
  estimateTokensFromChars,
  recordResidentTurnLatency,
  getResidentTurnLatencyTelemetry,
  resetResidentTurnLatencyTelemetry,
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
    expect(getLlmTelemetry()).toEqual({
      llm_call_count: 0,
      total_input_tokens: 0,
      total_output_tokens: 0,
      total_cache_creation_input_tokens: 0,
      total_cache_read_input_tokens: 0,
      viewport_screenshot_forwarded_count: 0,
      num_turns: 0,
      total_tool_result_bytes: 0,
      prompt_size_bytes: null,
      prompt_section_count: null,
      prompt_section_sizes: null,
      provider_reported_usage: null,
    });
  });
  it('accumulates call counts and token totals', () => {
    recordLlmCallStart();
    recordLlmTokens(100, 50);
    recordLlmCallStart();
    recordLlmTokens(200, 75);
    expect(getLlmTelemetry()).toEqual({
      llm_call_count: 2,
      total_input_tokens: 300,
      total_output_tokens: 125,
      total_cache_creation_input_tokens: 0,
      total_cache_read_input_tokens: 0,
      viewport_screenshot_forwarded_count: 0,
      num_turns: 0,
      total_tool_result_bytes: 0,
      prompt_size_bytes: null,
      prompt_section_count: null,
      prompt_section_sizes: null,
      provider_reported_usage: null,
    });
  });
  it('ignores invalid token values (NaN, negative, Infinity)', () => {
    recordLlmTokens(Number.NaN, 50);   // NaN input ignored, 50 output added
    recordLlmTokens(-5, -10);           // negatives ignored
    recordLlmTokens(100, Number.POSITIVE_INFINITY); // 100 input added, Infinity output ignored
    expect(getLlmTelemetry()).toEqual({
      llm_call_count: 0,
      total_input_tokens: 100,
      total_output_tokens: 50,
      total_cache_creation_input_tokens: 0,
      total_cache_read_input_tokens: 0,
      viewport_screenshot_forwarded_count: 0,
      num_turns: 0,
      total_tool_result_bytes: 0,
      prompt_size_bytes: null,
      prompt_section_count: null,
      prompt_section_sizes: null,
      provider_reported_usage: null,
    });
  });
  it('resets all counters', () => {
    recordLlmCallStart();
    recordLlmTokens(500, 250);
    resetLlmTelemetry();
    expect(getLlmTelemetry()).toEqual({
      llm_call_count: 0,
      total_input_tokens: 0,
      total_output_tokens: 0,
      total_cache_creation_input_tokens: 0,
      total_cache_read_input_tokens: 0,
      viewport_screenshot_forwarded_count: 0,
      num_turns: 0,
      total_tool_result_bytes: 0,
      prompt_size_bytes: null,
      prompt_section_count: null,
      prompt_section_sizes: null,
      provider_reported_usage: null,
    });
  });
  it('breaks out cache-creation and cache-read while total_input_tokens stays the inclusive sum', () => {
    // inputTokens arg is the inclusive sum (uncached 100 + creation 40 + read 60 = 200).
    recordLlmTokens(200, 75, 40, 60);
    recordLlmTokens(150, 25, 10, 90); // inclusive sum 150 (uncached 50 + creation 10 + read 90)
    const tel = getLlmTelemetry();
    expect(tel.total_input_tokens).toBe(350);            // inclusive sum of all three classes
    expect(tel.total_cache_creation_input_tokens).toBe(50);  // 40 + 10
    expect(tel.total_cache_read_input_tokens).toBe(150);     // 60 + 90
    expect(tel.total_output_tokens).toBe(100);
    expect(tel.num_turns).toBe(0);                       // not set by recordLlmTokens
  });
  it('leaves cache counters at zero for 2-arg (text-mode) calls', () => {
    recordLlmTokens(100, 50);
    const tel = getLlmTelemetry();
    expect(tel.total_cache_creation_input_tokens).toBe(0);
    expect(tel.total_cache_read_input_tokens).toBe(0);
    expect(tel.num_turns).toBe(0);
  });
  it('ignores invalid cache token values (NaN, negative, Infinity)', () => {
    recordLlmTokens(100, 50, Number.NaN, -5);
    recordLlmTokens(100, 50, Number.POSITIVE_INFINITY, 30);
    const tel = getLlmTelemetry();
    expect(tel.total_cache_creation_input_tokens).toBe(0); // NaN and Infinity ignored
    expect(tel.total_cache_read_input_tokens).toBe(30);    // negative ignored, 30 added
  });
  it('resets the cache counters', () => {
    recordLlmTokens(200, 75, 40, 60);
    resetLlmTelemetry();
    const tel = getLlmTelemetry();
    expect(tel.total_cache_creation_input_tokens).toBe(0);
    expect(tel.total_cache_read_input_tokens).toBe(0);
  });
});

describe('viewport screenshot telemetry', () => {
  it('starts at zero', () => {
    expect(getLlmTelemetry().viewport_screenshot_forwarded_count).toBe(0);
  });
  it('accumulates across multiple download calls', () => {
    recordViewportScreenshotDownload(3);
    recordViewportScreenshotDownload(2);
    expect(getLlmTelemetry().viewport_screenshot_forwarded_count).toBe(5);
  });
  it('ignores zero-count calls', () => {
    recordViewportScreenshotDownload(0);
    expect(getLlmTelemetry().viewport_screenshot_forwarded_count).toBe(0);
  });
  it('resets with resetLlmTelemetry', () => {
    recordViewportScreenshotDownload(4);
    resetLlmTelemetry();
    expect(getLlmTelemetry().viewport_screenshot_forwarded_count).toBe(0);
  });
});

describe('num_turns telemetry', () => {
  it('starts at zero', () => {
    expect(getLlmTelemetry().num_turns).toBe(0);
  });
  it('accumulates turn counts from result events', () => {
    recordNumTurns(3);
    recordNumTurns(2);
    expect(getLlmTelemetry().num_turns).toBe(5);
  });
  it('ignores zero and negative values', () => {
    recordNumTurns(0);
    recordNumTurns(-5);
    expect(getLlmTelemetry().num_turns).toBe(0);
  });
  it('ignores invalid values (NaN, Infinity)', () => {
    recordNumTurns(Number.NaN);
    recordNumTurns(Number.POSITIVE_INFINITY);
    recordNumTurns(10);
    expect(getLlmTelemetry().num_turns).toBe(10);
  });
  it('resets with resetLlmTelemetry', () => {
    recordNumTurns(7);
    resetLlmTelemetry();
    expect(getLlmTelemetry().num_turns).toBe(0);
  });
  it('floors non-integer turn values', () => {
    recordNumTurns(3.9);
    recordNumTurns(2.1);
    expect(getLlmTelemetry().num_turns).toBe(5);
  });
});

describe('tool-result byte telemetry', () => {
  it('starts at zero', () => {
    expect(getLlmTelemetry().total_tool_result_bytes).toBe(0);
  });
  it('accumulates across multiple record calls', () => {
    recordToolResultBytes(1000);
    recordToolResultBytes(500);
    expect(getLlmTelemetry().total_tool_result_bytes).toBe(1500);
  });
  it('ignores zero and negative values', () => {
    recordToolResultBytes(0);
    recordToolResultBytes(-100);
    expect(getLlmTelemetry().total_tool_result_bytes).toBe(0);
  });
  it('ignores invalid values (NaN, Infinity)', () => {
    recordToolResultBytes(Number.NaN);
    recordToolResultBytes(Number.POSITIVE_INFINITY);
    recordToolResultBytes(200);
    expect(getLlmTelemetry().total_tool_result_bytes).toBe(200);
  });
  it('resets with resetLlmTelemetry', () => {
    recordToolResultBytes(999);
    resetLlmTelemetry();
    expect(getLlmTelemetry().total_tool_result_bytes).toBe(0);
  });
  it('floors non-integer byte values', () => {
    recordToolResultBytes(100.9);
    recordToolResultBytes(50.1);
    expect(getLlmTelemetry().total_tool_result_bytes).toBe(150);
  });
});

describe('format-compliance telemetry', () => {
  it('starts at zero', () => {
    expect(getFormatComplianceTelemetry()).toEqual({
      format_miss_by_reason: {},
      format_repair_miss_by_reason: {},
      format_error_total: 0,
      format_repair_attempts: 0,
      format_repair_successes: 0,
      format_repair_infra_failures: 0,
      format_repair_input_tokens: 0,
      format_repair_output_tokens: 0,
      format_miss_by_schema: {},
      format_repair_attempts_by_schema: {},
      format_repair_successes_by_schema: {},
      format_repair_infra_failures_by_schema: {},
      format_repair_input_tokens_by_schema: {},
      format_repair_output_tokens_by_schema: {},
      format_repair_miss_by_schema: {},
      format_miss_by_schema_and_reason: {},
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
      format_repair_miss_by_reason: {},
      format_error_total: 1,
      format_repair_attempts: 1,
      format_repair_successes: 1,
      format_repair_infra_failures: 0,
      format_repair_input_tokens: 0,
      format_repair_output_tokens: 0,
      format_miss_by_schema: {},
      format_repair_attempts_by_schema: {},
      format_repair_successes_by_schema: {},
      format_repair_infra_failures_by_schema: {},
      format_repair_input_tokens_by_schema: {},
      format_repair_output_tokens_by_schema: {},
      format_repair_miss_by_schema: {},
      format_miss_by_schema_and_reason: {},
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
      format_repair_miss_by_reason: {},
      format_error_total: 4,
      format_repair_attempts: 0,
      format_repair_successes: 0,
      format_repair_infra_failures: 0,
      format_repair_input_tokens: 0,
      format_repair_output_tokens: 0,
      format_miss_by_schema: {},
      format_repair_attempts_by_schema: {},
      format_repair_successes_by_schema: {},
      format_repair_infra_failures_by_schema: {},
      format_repair_input_tokens_by_schema: {},
      format_repair_output_tokens_by_schema: {},
      format_repair_miss_by_schema: {},
      format_miss_by_schema_and_reason: {},
    });
  });

  it('snapshot is a copy — mutating it does not affect internal state', () => {
    recordFormatMiss('no_json_start');
    const snap = getFormatComplianceTelemetry();
    snap.format_miss_by_reason.no_json_start = 999;
    snap.format_error_total = 999;
    expect(getFormatComplianceTelemetry()).toEqual({
      format_miss_by_reason: { no_json_start: 1 },
      format_repair_miss_by_reason: {},
      format_error_total: 1,
      format_repair_attempts: 0,
      format_repair_successes: 0,
      format_repair_infra_failures: 0,
      format_repair_input_tokens: 0,
      format_repair_output_tokens: 0,
      format_miss_by_schema: {},
      format_repair_attempts_by_schema: {},
      format_repair_successes_by_schema: {},
      format_repair_infra_failures_by_schema: {},
      format_repair_input_tokens_by_schema: {},
      format_repair_output_tokens_by_schema: {},
      format_repair_miss_by_schema: {},
      format_miss_by_schema_and_reason: {},
    });
  });

  it('reset clears all format-compliance counters', () => {
    recordFormatMiss('unbalanced_json');
    recordFormatRepairAttempt();
    recordFormatRepairSuccess();
    resetFormatComplianceTelemetry();
    expect(getFormatComplianceTelemetry()).toEqual({
      format_miss_by_reason: {},
      format_repair_miss_by_reason: {},
      format_error_total: 0,
      format_repair_attempts: 0,
      format_repair_successes: 0,
      format_repair_infra_failures: 0,
      format_repair_input_tokens: 0,
      format_repair_output_tokens: 0,
      format_miss_by_schema: {},
      format_repair_attempts_by_schema: {},
      format_repair_successes_by_schema: {},
      format_repair_infra_failures_by_schema: {},
      format_repair_input_tokens_by_schema: {},
      format_repair_output_tokens_by_schema: {},
      format_repair_miss_by_schema: {},
      format_miss_by_schema_and_reason: {},
    });
  });

  it('recordFormatRepairTokens accumulates input and output tokens', () => {
    recordFormatRepairTokens(500, 200);
    recordFormatRepairTokens(300, 100);
    const tel = getFormatComplianceTelemetry();
    expect(tel.format_repair_input_tokens).toBe(800);
    expect(tel.format_repair_output_tokens).toBe(300);
  });

  it('recordFormatRepairTokens ignores invalid values (NaN, negative, Infinity)', () => {
    recordFormatRepairTokens(Number.NaN, 100);
    recordFormatRepairTokens(-50, -20);
    recordFormatRepairTokens(200, Number.POSITIVE_INFINITY);
    const tel = getFormatComplianceTelemetry();
    expect(tel.format_repair_input_tokens).toBe(200);
    expect(tel.format_repair_output_tokens).toBe(100);
  });

  it('getFormatComplianceTelemetry starts with zero repair tokens', () => {
    const tel = getFormatComplianceTelemetry();
    expect(tel.format_repair_input_tokens).toBe(0);
    expect(tel.format_repair_output_tokens).toBe(0);
  });

  it('resetFormatComplianceTelemetry clears repair token counters', () => {
    recordFormatRepairTokens(1000, 500);
    resetFormatComplianceTelemetry();
    const tel = getFormatComplianceTelemetry();
    expect(tel.format_repair_input_tokens).toBe(0);
    expect(tel.format_repair_output_tokens).toBe(0);
  });
});

describe('format-compliance telemetry — repair miss by schema', () => {
  it('recordFormatRepairMiss with schemaLabel fills format_repair_miss_by_schema', () => {
    recordFormatRepairMiss('no_json_start', 'thinker');
    recordFormatRepairMiss('malformed_json', 'thinker');
    recordFormatRepairMiss('unbalanced_json', 'security_audit');
    const tel = getFormatComplianceTelemetry();
    expect(tel.format_repair_miss_by_schema).toEqual({ thinker: 2, security_audit: 1 });
    expect(tel.format_repair_miss_by_reason).toEqual({ no_json_start: 1, malformed_json: 1, unbalanced_json: 1 });
  });

  it('calls without schemaLabel do not create per-schema entries but still update format_repair_miss_by_reason', () => {
    recordFormatRepairMiss('malformed_json');
    const tel = getFormatComplianceTelemetry();
    expect(tel.format_repair_miss_by_reason).toEqual({ malformed_json: 1 });
    expect(tel.format_repair_miss_by_schema).toEqual({});
  });

  it('reset clears format_repair_miss_by_schema', () => {
    recordFormatRepairMiss('no_json_start', 'thinker');
    resetFormatComplianceTelemetry();
    expect(getFormatComplianceTelemetry().format_repair_miss_by_schema).toEqual({});
  });

  it('format_repair_miss_by_schema snapshot is a copy — mutating it does not affect internal state', () => {
    recordFormatRepairMiss('unbalanced_json', 'memo');
    const snap = getFormatComplianceTelemetry();
    (snap.format_repair_miss_by_schema as Record<string, number>)['memo'] = 999;
    expect(getFormatComplianceTelemetry().format_repair_miss_by_schema).toEqual({ memo: 1 });
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
    recordFormatMiss('unbalanced_json', 'security_audit');
    recordFormatRepairAttempt('security_audit');
    recordFormatRepairSuccess('security_audit');
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

  it('recordFormatRepairMiss with schemaLabel buckets into format_repair_miss_by_schema', () => {
    recordFormatRepairMiss('unbalanced_json', 'thinker');
    recordFormatRepairMiss('missing_required_key', 'thinker');
    recordFormatRepairMiss('no_json_start', 'memo');
    const tel = getFormatComplianceTelemetry();
    expect(tel.format_repair_miss_by_reason).toEqual({ unbalanced_json: 1, missing_required_key: 1, no_json_start: 1 });
    expect(tel.format_repair_miss_by_schema).toEqual({ thinker: 2, memo: 1 });
  });

  it('recordFormatRepairMiss without schemaLabel does not create per-schema entry but still updates reason bucket', () => {
    recordFormatRepairMiss('malformed_json');
    const tel = getFormatComplianceTelemetry();
    expect(tel.format_repair_miss_by_reason).toEqual({ malformed_json: 1 });
    expect(tel.format_repair_miss_by_schema).toEqual({});
  });

  it('reset clears format_repair_miss_by_schema', () => {
    recordFormatRepairMiss('unbalanced_json', 'thinker');
    resetFormatComplianceTelemetry();
    expect(getFormatComplianceTelemetry().format_repair_miss_by_schema).toEqual({});
  });

  it('format_repair_miss_by_schema snapshot is a copy — mutating it does not affect internal state', () => {
    recordFormatRepairMiss('unbalanced_json', 'thinker');
    const snap = getFormatComplianceTelemetry();
    (snap.format_repair_miss_by_schema as Record<string, number>)['thinker'] = 999;
    expect(getFormatComplianceTelemetry().format_repair_miss_by_schema).toEqual({ thinker: 1 });
  });
});

describe('format-compliance telemetry — repair token counters', () => {
  it('starts at zero for repair token fields', () => {
    const tel = getFormatComplianceTelemetry();
    expect(tel.format_repair_input_tokens).toBe(0);
    expect(tel.format_repair_output_tokens).toBe(0);
    expect(tel.format_repair_input_tokens_by_schema).toEqual({});
    expect(tel.format_repair_output_tokens_by_schema).toEqual({});
  });

  it('accumulates repair token deltas', () => {
    recordFormatRepairTokens(1000, 500);
    recordFormatRepairTokens(2000, 800);
    const tel = getFormatComplianceTelemetry();
    expect(tel.format_repair_input_tokens).toBe(3000);
    expect(tel.format_repair_output_tokens).toBe(1300);
  });

  it('floors non-integer token deltas', () => {
    recordFormatRepairTokens(100.9, 50.7);
    const tel = getFormatComplianceTelemetry();
    expect(tel.format_repair_input_tokens).toBe(100);
    expect(tel.format_repair_output_tokens).toBe(50);
  });

  it('ignores non-positive and non-finite deltas', () => {
    recordFormatRepairTokens(0, 0);
    recordFormatRepairTokens(-10, -5);
    recordFormatRepairTokens(Number.NaN, Number.POSITIVE_INFINITY);
    const tel = getFormatComplianceTelemetry();
    expect(tel.format_repair_input_tokens).toBe(0);
    expect(tel.format_repair_output_tokens).toBe(0);
  });

  it('buckets repair tokens by schema when schemaLabel is provided', () => {
    recordFormatRepairTokens(1000, 400, 'memo');
    recordFormatRepairTokens(500, 200, 'design');
    recordFormatRepairTokens(300, 100, 'memo');
    const tel = getFormatComplianceTelemetry();
    expect(tel.format_repair_input_tokens).toBe(1800);
    expect(tel.format_repair_output_tokens).toBe(700);
    expect(tel.format_repair_input_tokens_by_schema).toEqual({ memo: 1300, design: 500 });
    expect(tel.format_repair_output_tokens_by_schema).toEqual({ memo: 500, design: 200 });
  });

  it('calls without schemaLabel do not create per-schema entries', () => {
    recordFormatRepairTokens(500, 250);
    const tel = getFormatComplianceTelemetry();
    expect(tel.format_repair_input_tokens).toBe(500);
    expect(tel.format_repair_output_tokens).toBe(250);
    expect(tel.format_repair_input_tokens_by_schema).toEqual({});
    expect(tel.format_repair_output_tokens_by_schema).toEqual({});
  });

  it('reset clears repair token counters and per-schema maps', () => {
    recordFormatRepairTokens(1000, 500, 'security_audit');
    resetFormatComplianceTelemetry();
    const tel = getFormatComplianceTelemetry();
    expect(tel.format_repair_input_tokens).toBe(0);
    expect(tel.format_repair_output_tokens).toBe(0);
    expect(tel.format_repair_input_tokens_by_schema).toEqual({});
    expect(tel.format_repair_output_tokens_by_schema).toEqual({});
  });

  it('token snapshot is a copy — mutating it does not affect internal state', () => {
    recordFormatRepairTokens(600, 300, 'thinker');
    const snap = getFormatComplianceTelemetry();
    (snap.format_repair_input_tokens_by_schema as Record<string, number>)['thinker'] = 999;
    expect(getFormatComplianceTelemetry().format_repair_input_tokens_by_schema).toEqual({ thinker: 600 });
  });
});

describe('format-compliance telemetry — cross-product (schema × reason)', () => {
  it('records format miss cross-product by schema and reason', () => {
    // Two schemas, multiple reasons — the cross-product must be independent per cell.
    recordFormatMiss('missing_required_key', 'research_plan');
    recordFormatMiss('unbalanced_json', 'research_plan');
    recordFormatMiss('missing_required_key', 'research_plan');
    recordFormatMiss('no_json_start', 'security_audit');
    recordFormatMiss('unbalanced_json', 'security_audit');

    const tel = getFormatComplianceTelemetry();

    // Global aggregates are unaffected.
    expect(tel.format_error_total).toBe(5);
    expect(tel.format_miss_by_reason).toEqual({
      missing_required_key: 2,
      unbalanced_json: 2,
      no_json_start: 1,
    });
    expect(tel.format_miss_by_schema).toEqual({ research_plan: 3, security_audit: 2 });

    // Cross-product localizes each reason to its handler.
    expect(tel.format_miss_by_schema_and_reason).toEqual({
      research_plan: { missing_required_key: 2, unbalanced_json: 1 },
      security_audit: { no_json_start: 1, unbalanced_json: 1 },
    });
  });

  it('calls without schemaLabel do not appear in format_miss_by_schema_and_reason', () => {
    recordFormatMiss('malformed_json');
    recordFormatMiss('unbalanced_json', 'memo');
    const tel = getFormatComplianceTelemetry();
    expect(tel.format_miss_by_schema_and_reason).toEqual({ memo: { unbalanced_json: 1 } });
  });

  it('format_miss_by_schema_and_reason snapshot is a copy — mutating it does not affect internal state', () => {
    recordFormatMiss('no_json_start', 'design');
    const snap = getFormatComplianceTelemetry();
    (snap.format_miss_by_schema_and_reason as Record<string, Record<string, number>>)['design']['no_json_start'] = 999;
    expect(getFormatComplianceTelemetry().format_miss_by_schema_and_reason).toEqual({
      design: { no_json_start: 1 },
    });
  });

  it('reset clears format_miss_by_schema_and_reason', () => {
    recordFormatMiss('unbalanced_json', 'memo');
    resetFormatComplianceTelemetry();
    expect(getFormatComplianceTelemetry().format_miss_by_schema_and_reason).toEqual({});
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

describe('resident turn latency telemetry', () => {
  beforeEach(() => {
    resetResidentTurnLatencyTelemetry();
  });

  it('returns null when no turns recorded', () => {
    expect(getResidentTurnLatencyTelemetry()).toBeNull();
  });

  it('accumulates operator turns and computes mean/max', () => {
    recordResidentTurnLatency({ kind: 'operator', turnMs: 100, ttftMs: 50, interrupted: false, toolCallCount: 2, toolCallDurationMs: 30, meanInterTokenMs: 10 });
    recordResidentTurnLatency({ kind: 'operator', turnMs: 300, ttftMs: 150, interrupted: false, toolCallCount: 1, toolCallDurationMs: 80, meanInterTokenMs: 20 });
    const tel = getResidentTurnLatencyTelemetry();
    if (!tel) throw new Error('expected non-null telemetry');
    expect(tel.operator.count).toBe(2);
    expect(tel.operator.mean_turn_ms).toBe(200); // (100+300)/2
    expect(tel.operator.max_turn_ms).toBe(300);
    expect(tel.operator.mean_ttft_ms).toBe(100); // (50+150)/2
    expect(tel.operator.interrupted_count).toBe(0);
    expect(tel.operator.total_tool_calls).toBe(3);
    expect(tel.operator.total_tool_call_duration_ms).toBe(110); // 30+80
    expect(tel.operator.mean_inter_token_ms).toBe(15); // (10+20)/2
    expect(tel.autonomous.count).toBe(0);
  });

  it('accumulates autonomous turns separately', () => {
    recordResidentTurnLatency({ kind: 'autonomous', turnMs: 200, ttftMs: null, interrupted: true, toolCallCount: 0, toolCallDurationMs: 0, meanInterTokenMs: null });
    const tel = getResidentTurnLatencyTelemetry();
    if (!tel) throw new Error('expected non-null telemetry');
    expect(tel.autonomous.count).toBe(1);
    expect(tel.autonomous.mean_turn_ms).toBe(200);
    expect(tel.autonomous.mean_ttft_ms).toBeNull(); // no ttft recorded
    expect(tel.autonomous.interrupted_count).toBe(1);
    expect(tel.operator.count).toBe(0);
  });

  it('mean_ttft_ms denominator excludes tool-only turns with null ttft', () => {
    recordResidentTurnLatency({ kind: 'operator', turnMs: 100, ttftMs: 40, interrupted: false, toolCallCount: 0, toolCallDurationMs: 0, meanInterTokenMs: null });
    recordResidentTurnLatency({ kind: 'operator', turnMs: 100, ttftMs: null, interrupted: false, toolCallCount: 5, toolCallDurationMs: 60, meanInterTokenMs: null });
    const tel = getResidentTurnLatencyTelemetry();
    if (!tel) throw new Error('expected non-null telemetry');
    // Only 1 turn had ttft; mean should be 40, not 40/2=20
    expect(tel.operator.mean_ttft_ms).toBe(40);
    expect(tel.operator.count).toBe(2);
  });

  it('drops non-finite and negative turnMs values', () => {
    recordResidentTurnLatency({ kind: 'operator', turnMs: Number.NaN, ttftMs: null, interrupted: false, toolCallCount: 0, toolCallDurationMs: 0, meanInterTokenMs: null });
    recordResidentTurnLatency({ kind: 'operator', turnMs: -50, ttftMs: null, interrupted: false, toolCallCount: 0, toolCallDurationMs: 0, meanInterTokenMs: null });
    recordResidentTurnLatency({ kind: 'operator', turnMs: Number.POSITIVE_INFINITY, ttftMs: null, interrupted: false, toolCallCount: 0, toolCallDurationMs: 0, meanInterTokenMs: null });
    const tel = getResidentTurnLatencyTelemetry();
    if (!tel) throw new Error('expected non-null telemetry');
    // count is incremented but turnMs stats are 0 since all invalid
    expect(tel.operator.count).toBe(3);
    expect(tel.operator.mean_turn_ms).toBe(0); // totalTurnMs=0, count=3 → 0
    expect(tel.operator.max_turn_ms).toBe(0);
  });

  it('resets all stats to zero', () => {
    recordResidentTurnLatency({ kind: 'operator', turnMs: 500, ttftMs: 200, interrupted: true, toolCallCount: 3, toolCallDurationMs: 100, meanInterTokenMs: 25 });
    resetResidentTurnLatencyTelemetry();
    expect(getResidentTurnLatencyTelemetry()).toBeNull();
  });
});
