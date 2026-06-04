import { describe, it, expect } from 'vitest';
import {
  summarizeOutcomeKinds,
  hasNonCodeMergeOutcomes,
  mergeOutcomeKindBreakdowns,
  outcomeKindLabel,
  healthRateColorClass,
} from './outcomeKinds';

describe('summarizeOutcomeKinds', () => {
  it('returns [] for null/undefined/empty input', () => {
    expect(summarizeOutcomeKinds(null)).toEqual([]);
    expect(summarizeOutcomeKinds(undefined)).toEqual([]);
    expect(summarizeOutcomeKinds({})).toEqual([]);
  });

  it('drops lanes with no terminal outcomes', () => {
    const rows = summarizeOutcomeKinds({
      code_merge: { success: 3, neutral: 0, failure: 1 },
      research_quality: { success: 0, neutral: 0, failure: 0 },
    });
    expect(rows.map(r => r.kind)).toEqual(['code_merge']);
    expect(rows[0]).toMatchObject({ label: 'code', success: 3, failure: 1, terminal: 4 });
  });

  it('orders by the canonical kind order, not insertion order', () => {
    const rows = summarizeOutcomeKinds({
      audit_findings: { success: 1, neutral: 0, failure: 0 },
      code_merge: { success: 2, neutral: 0, failure: 0 },
      research_quality: { success: 1, neutral: 0, failure: 0 },
    });
    expect(rows.map(r => r.kind)).toEqual(['code_merge', 'research_quality', 'audit_findings']);
  });

  it('coerces negative/fractional counts to non-negative integers', () => {
    const rows = summarizeOutcomeKinds({
      research_quality: { success: 2.9, neutral: -1, failure: 0 },
    });
    expect(rows[0]).toMatchObject({ success: 2, neutral: 0, failure: 0, terminal: 2 });
  });
});

describe('hasNonCodeMergeOutcomes', () => {
  it('is false when only code_merge has outcomes (legacy degrade)', () => {
    expect(hasNonCodeMergeOutcomes({ code_merge: { success: 5, neutral: 0, failure: 0 } })).toBe(false);
    expect(hasNonCodeMergeOutcomes(null)).toBe(false);
    expect(hasNonCodeMergeOutcomes({})).toBe(false);
  });

  it('is true when any generalized lane has terminal outcomes', () => {
    expect(
      hasNonCodeMergeOutcomes({
        code_merge: { success: 5, neutral: 0, failure: 0 },
        research_quality: { success: 1, neutral: 0, failure: 0 },
      }),
    ).toBe(true);
  });

  it('ignores empty generalized lanes', () => {
    expect(
      hasNonCodeMergeOutcomes({
        code_merge: { success: 5, neutral: 0, failure: 0 },
        audit_findings: { success: 0, neutral: 0, failure: 0 },
      }),
    ).toBe(false);
  });
});

describe('mergeOutcomeKindBreakdowns', () => {
  it('sums counts per kind across directions and ignores null entries', () => {
    const merged = mergeOutcomeKindBreakdowns([
      { code_merge: { success: 2, neutral: 0, failure: 1 } },
      null,
      { code_merge: { success: 3, neutral: 1, failure: 0 }, research_quality: { success: 4, neutral: 0, failure: 0 } },
    ]);
    expect(merged.code_merge).toEqual({ success: 5, neutral: 1, failure: 1 });
    expect(merged.research_quality).toEqual({ success: 4, neutral: 0, failure: 0 });
  });
});

describe('outcomeKindLabel', () => {
  it('maps known kinds and falls back to the raw key', () => {
    expect(outcomeKindLabel('verification_verdict')).toBe('verify');
    expect(outcomeKindLabel('some_future_kind')).toBe('some_future_kind');
  });
});

describe('healthRateColorClass', () => {
  it('uses neutral when the rate is null', () => {
    expect(healthRateColorClass(null)).toBe('text-neutral-500');
  });
  it('thresholds at 0.6 and 0.3', () => {
    expect(healthRateColorClass(0.8)).toBe('text-emerald-400');
    expect(healthRateColorClass(0.45)).toBe('text-amber-400');
    expect(healthRateColorClass(0.1)).toBe('text-red-400');
  });
});
