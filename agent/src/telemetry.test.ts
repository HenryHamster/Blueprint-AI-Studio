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
  estimateTokensFromChars,
} from './telemetry.js';

beforeEach(() => {
  resetLlmTelemetry();
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
