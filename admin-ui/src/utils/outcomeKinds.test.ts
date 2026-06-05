import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import {
  summarizeOutcomeKinds,
  hasNonCodeMergeOutcomes,
  mergeOutcomeKindBreakdowns,
  outcomeKindLabel,
  healthRateColorClass,
  OUTCOME_KIND_LABELS,
  OUTCOME_KIND_ORDER,
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

  it('labels the knowledge_document lane deliberately', () => {
    expect(outcomeKindLabel('knowledge_document')).toBe('knowledge');
  });

  it('labels the compute-experiment lane with deliberate vocabulary', () => {
    // Regression: compute_result is emitted by the backend
    // (task_outcomes.py KIND_COMPUTE_RESULT) and must not fall through to the
    // raw key.
    expect(outcomeKindLabel('compute_result')).toBe('compute');
  });
});

describe('knowledge_document outcome lane', () => {
  it('sorts the knowledge lane after verification but before unknown', () => {
    const rows = summarizeOutcomeKinds({
      unknown: { success: 1, neutral: 0, failure: 0 },
      knowledge_document: { success: 2, neutral: 0, failure: 0 },
      verification_verdict: { success: 1, neutral: 0, failure: 0 },
    });
    expect(rows.map(r => r.kind)).toEqual([
      'verification_verdict',
      'knowledge_document',
      'unknown',
    ]);
  });

  it('treats the knowledge lane as a generalized (non-code) success', () => {
    expect(
      hasNonCodeMergeOutcomes({
        code_merge: { success: 5, neutral: 0, failure: 0 },
        knowledge_document: { success: 1, neutral: 0, failure: 0 },
      }),
    ).toBe(true);
  });
});

describe('compute_result ordering', () => {
  it('sorts the compute lane ahead of read-only/unknown lanes', () => {
    const rows = summarizeOutcomeKinds({
      read_only_unclassified: { success: 0, neutral: 1, failure: 0 },
      compute_result: { success: 1, neutral: 0, failure: 0 },
      unknown: { success: 0, neutral: 0, failure: 1 },
    });
    expect(rows.map(r => r.kind)).toEqual([
      'compute_result',
      'read_only_unclassified',
      'unknown',
    ]);
    expect(rows[0]).toMatchObject({ label: 'compute', success: 1, terminal: 1 });
  });
});

// Drift guard: the frontend outcome-kind vocabulary must keep pace with the
// backend's emitted non-code lanes. If a future backend KIND_* is added
// without a matching frontend label + order entry, this fails — preventing the
// chip from silently rendering the raw backend key and sorting as "unknown".
describe('outcome-kind vocabulary parity with backend', () => {
  const backendPath = path.resolve(
    path.dirname(fileURLToPath(import.meta.url)),
    '../../../backend/app/task_outcomes.py',
  );
  const source = readFileSync(backendPath, 'utf8');
  const kindValues = [...source.matchAll(/^KIND_[A-Z_]+ = "([a-z_]+)"/gm)].map(m => m[1]);

  // code_merge is the legacy PR-merge lane handled by the existing PR metrics;
  // every other emitted lane is a generalized lane that needs a deliberate
  // label and a stable sort position.
  const generalizedLanes = kindValues.filter(v => v !== 'code_merge');

  it('parses the backend KIND_* constants', () => {
    expect(kindValues).toContain('code_merge');
    expect(kindValues).toContain('compute_result');
    expect(kindValues).toContain('knowledge_document');
  });

  it.each(generalizedLanes)('has a frontend label for %s', lane => {
    expect(OUTCOME_KIND_LABELS[lane]).toBeTruthy();
  });

  it.each(generalizedLanes)('has a stable sort position for %s', lane => {
    expect(OUTCOME_KIND_ORDER).toContain(lane);
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
