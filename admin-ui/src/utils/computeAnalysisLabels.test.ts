import { describe, it, expect } from 'vitest';
import { formatComputeAnalysisCauseLabel } from './computeAnalysisLabels';

describe('formatComputeAnalysisCauseLabel', () => {
  it('returns null when all inputs are absent', () => {
    expect(formatComputeAnalysisCauseLabel({})).toBeNull();
  });

  it('returns null when analysisMode is "llm" (the normal mode)', () => {
    expect(formatComputeAnalysisCauseLabel({ analysisMode: 'llm' })).toBeNull();
  });

  it('maps known unavailableCause values to their labels', () => {
    expect(
      formatComputeAnalysisCauseLabel({ unavailableCause: 'precondition-not-met' }),
    ).toBe('Missing credentials — analysis skipped');

    expect(
      formatComputeAnalysisCauseLabel({ unavailableCause: 'analysis-disabled' }),
    ).toBe('Analysis disabled');

    expect(
      formatComputeAnalysisCauseLabel({ unavailableCause: 'no-evidence' }),
    ).toBe('No evidence to analyze');

    expect(
      formatComputeAnalysisCauseLabel({ unavailableCause: 'llm-error' }),
    ).toBe('Backend / LLM error');

    expect(
      formatComputeAnalysisCauseLabel({ unavailableCause: 'citations-ungrounded' }),
    ).toBe('Ungrounded citations');
  });

  it('humanizes an unknown unavailableCause with title case and spaces', () => {
    expect(
      formatComputeAnalysisCauseLabel({ unavailableCause: 'some_future_reason' }),
    ).toBe('Some future reason');

    expect(
      formatComputeAnalysisCauseLabel({ unavailableCause: 'rate-limited-by-backend' }),
    ).toBe('Rate limited by backend');
  });

  it('falls back to analysisFallbackReason when unavailableCause is absent', () => {
    expect(
      formatComputeAnalysisCauseLabel({ analysisFallbackReason: 'llm-error' }),
    ).toBe('Backend / LLM error');
  });

  it('humanizes an unknown analysisFallbackReason', () => {
    expect(
      formatComputeAnalysisCauseLabel({ analysisFallbackReason: 'unknown_fallback_type' }),
    ).toBe('Unknown fallback type');
  });

  it('preferrs unavailableCause over analysisFallbackReason', () => {
    expect(
      formatComputeAnalysisCauseLabel({
        unavailableCause: 'analysis-disabled',
        analysisFallbackReason: 'llm-error',
      }),
    ).toBe('Analysis disabled');
  });

  it('returns the deterministic-fallback label for non-LLM analysis mode "fallback"', () => {
    expect(
      formatComputeAnalysisCauseLabel({ analysisMode: 'fallback' }),
    ).toBe('Deterministic fallback summary (analysis mode is not LLM) — measured only');
  });

  it('returns the deterministic-fallback label for non-LLM analysis mode "deterministic"', () => {
    expect(
      formatComputeAnalysisCauseLabel({ analysisMode: 'deterministic' }),
    ).toBe('Deterministic fallback summary (analysis mode is not LLM) — measured only');
  });

  it('returns a generic mode label for other non-LLM analysis modes', () => {
    expect(
      formatComputeAnalysisCauseLabel({ analysisMode: 'heuristic' }),
    ).toBe('Analysis mode: heuristic');
  });

  it('treats analysisMode case-insensitively (lowercases before comparison)', () => {
    expect(formatComputeAnalysisCauseLabel({ analysisMode: 'LLM' })).toBeNull();
    expect(formatComputeAnalysisCauseLabel({ analysisMode: 'Fallback' })).toBe(
      'Deterministic fallback summary (analysis mode is not LLM) — measured only',
    );
  });

  it('returns null for whitespace-only inputs', () => {
    expect(
      formatComputeAnalysisCauseLabel({ unavailableCause: '   ', analysisFallbackReason: '  ' }),
    ).toBeNull();
  });
});
