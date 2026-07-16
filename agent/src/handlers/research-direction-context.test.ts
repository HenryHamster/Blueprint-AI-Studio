import { describe, it, expect, vi } from 'vitest';

vi.mock('../index.js', () => ({
  normalizeClaudeConfigContent: vi.fn((_path: string, content: string) => content),
  strVal: (v: unknown, def = '') => (typeof v === 'string' ? v : def),
}));

import {
  formatCochangeHints,
  formatDecompositionOutcomes,
  formatPreviousDecisionOutcomes,
  formatPastRetirementCritiques,
  formatRetiredDirectionsSummary,
  formatRetiredDirectionGrounding,
} from './research-direction-context.js';

describe('formatRetiredDirectionGrounding', () => {
  it('returns empty string for non-object / empty grounding', () => {
    expect(formatRetiredDirectionGrounding(undefined)).toBe('');
    expect(formatRetiredDirectionGrounding(null)).toBe('');
    expect(formatRetiredDirectionGrounding([])).toBe('');
    expect(formatRetiredDirectionGrounding({})).toBe('');
  });

  it('renders classification, prior id, metrics and reason', () => {
    const out = formatRetiredDirectionGrounding({
      direction_id: 'd-prior',
      classification: 'abandoned',
      metrics: { prs_merged: 0, cycles: 4 },
      reason: 'stalled on flaky infra',
    });
    expect(out).toContain('d-prior');
    expect(out).toContain('[abandoned]');
    expect(out).toContain('prs_merged=0');
    expect(out).toContain('cycles=4');
    expect(out).toContain('stalled on flaky infra');
  });

  it('renders without metrics when metrics are absent', () => {
    const out = formatRetiredDirectionGrounding({ classification: 'landed' });
    expect(out).toContain('[landed]');
    expect(out).not.toContain('(');
  });
});

describe('formatCochangeHints', () => {
  it('returns empty string when direction context is undefined', () => {
    expect(formatCochangeHints(undefined)).toBe('');
  });

  it('returns empty string when cochange_hints is missing', () => {
    expect(formatCochangeHints({ other: 'data' })).toBe('');
  });

  it('returns empty string when cochange_hints is empty', () => {
    expect(formatCochangeHints({ cochange_hints: {} })).toBe('');
  });

  it('renders deduplicated pairs sorted by score (highest first)', () => {
    const ctx = {
      cochange_hints: {
        'a.py': { 'b.py': 1.5, 'c.py': 3.0 },
        'b.py': { 'a.py': 1.5 },
        'c.py': { 'a.py': 3.0 },
      },
    };
    const result = formatCochangeHints(ctx);
    expect(result).toContain('Co-Change Affinity Hints');
    const aBMatches = result.match(/a\.py.*b\.py/g) || [];
    const aCMatches = result.match(/a\.py.*c\.py/g) || [];
    expect(aBMatches.length).toBe(1);
    expect(aCMatches.length).toBe(1);
    expect(result.indexOf('c.py')).toBeLessThan(result.indexOf('b.py'));
    expect(result).toContain('affinity: 3');
    expect(result).toContain('affinity: 1.5');
  });

  it('skips non-numeric scores gracefully', () => {
    const ctx = {
      cochange_hints: {
        'a.py': { 'b.py': 'not-a-number' as unknown as number, 'c.py': 2.0 },
      },
    };
    const result = formatCochangeHints(ctx);
    expect(result).toContain('a.py');
    expect(result).toContain('c.py');
    expect(result).not.toContain('b.py');
  });
});

describe('formatDecompositionOutcomes', () => {
  it('returns empty string when direction context is undefined', () => {
    expect(formatDecompositionOutcomes(undefined)).toBe('');
  });

  it('returns empty string when decomposition_outcomes is missing', () => {
    expect(formatDecompositionOutcomes({ other: 'x' })).toBe('');
  });

  it('returns empty string when prior_decomposition_count is zero', () => {
    expect(formatDecompositionOutcomes({
      decomposition_outcomes: { prior_decomposition_count: 0 },
    })).toBe('');
  });

  it('renders section with metrics when data is present', () => {
    const result = formatDecompositionOutcomes({
      decomposition_outcomes: {
        prior_decomposition_count: 4,
        avg_subtask_merge_rate: 0.75,
        avg_files_per_subtask: 3.5,
        max_files_in_subtask: 6,
        avg_dependency_depth: 2.0,
        max_dependency_depth: 3,
        high_cross_module_correlates_with_low_merge: false,
      },
    });
    expect(result).toContain('Prior Decomposition Outcomes');
    expect(result).toContain('Prior decompositions analyzed: 4');
    expect(result).toContain('Avg subtask merge rate: 75%');
    expect(result).toContain('Avg files per subtask: 3.5');
    expect(result).toContain('Max files in a single subtask: 6');
    expect(result).toContain('Max dependency depth: 3');
    expect(result).not.toContain('Actionable guidance');
  });

  it('includes "flatten dependency graph" hint when max_dependency_depth > 3', () => {
    const result = formatDecompositionOutcomes({
      decomposition_outcomes: {
        prior_decomposition_count: 2,
        avg_subtask_merge_rate: 0.5,
        max_dependency_depth: 5,
      },
    });
    expect(result).toContain('flatten the dependency graph');
  });

  it('includes "smaller subtasks" hint when avg_files_per_subtask is high and merge rate modest', () => {
    const result = formatDecompositionOutcomes({
      decomposition_outcomes: {
        prior_decomposition_count: 3,
        avg_subtask_merge_rate: 0.3,
        avg_files_per_subtask: 8.0,
      },
    });
    expect(result).toContain('prefer smaller subtasks');
  });

  it('includes cross-module correlation warning when flagged', () => {
    const result = formatDecompositionOutcomes({
      decomposition_outcomes: {
        prior_decomposition_count: 3,
        avg_subtask_merge_rate: 0.6,
        high_cross_module_correlates_with_low_merge: true,
      },
    });
    expect(result).toContain('Cross-module subtasks');
    expect(result).toContain('keep each subtask scoped to one module');
  });

  it('does not recommend smaller subtasks when merge rate is high despite many files', () => {
    const result = formatDecompositionOutcomes({
      decomposition_outcomes: {
        prior_decomposition_count: 3,
        avg_subtask_merge_rate: 0.9,
        avg_files_per_subtask: 8.0,
      },
    });
    expect(result).not.toContain('prefer smaller subtasks');
  });
});

describe('formatPreviousDecisionOutcomes', () => {
  it('returns empty string when input is undefined', () => {
    expect(formatPreviousDecisionOutcomes(undefined)).toBe('');
  });

  it('returns empty string when input is null', () => {
    expect(formatPreviousDecisionOutcomes(null)).toBe('');
  });

  it('returns empty string when created and retired are both empty', () => {
    expect(formatPreviousDecisionOutcomes({ cycle_id: 'sc-1', created: [], retired: [] })).toBe('');
  });

  it('renders created direction with status, cycles, merges, quality', () => {
    const out = formatPreviousDecisionOutcomes({
      cycle_id: 'sc-prev',
      phase2_completed_at: '2026-04-19T12:00:00Z',
      created: [
        {
          direction_id: 'd-abc',
          role: 'created',
          status: 'active',
          text: 'Improve foo',
          cycles_active: 3,
          prs_merged: 2,
          quality_score: 0.75,
          auto_paused: false,
          user_paused: false,
        },
      ],
      retired: [],
    });
    expect(out).toContain('## Decision Feedback');
    expect(out).toContain('cycle sc-prev');
    expect(out).toContain('d-abc');
    expect(out).toContain('"Improve foo"');
    expect(out).toContain('status=active');
    expect(out).toContain('3 cycles active');
    expect(out).toContain('2 PRs merged');
    expect(out).toContain('quality 0.75');
    expect(out).toContain('Directions you CREATED');
  });

  it('flags auto_paused and user_paused distinctly', () => {
    const out = formatPreviousDecisionOutcomes({
      created: [
        { direction_id: 'd-a', status: 'paused', auto_paused: true, user_paused: false, cycles_active: 1, prs_merged: 0, quality_score: 0 },
        { direction_id: 'd-b', status: 'paused', auto_paused: false, user_paused: true, cycles_active: 2, prs_merged: 0, quality_score: 0 },
      ],
      retired: [],
    });
    expect(out).toMatch(/d-a:.*AUTO_PAUSED/);
    expect(out).toMatch(/d-b:.*USER_PAUSED/);
    expect(out).not.toMatch(/d-a:.*USER_PAUSED/);
  });

  it('distinguishes missing retired directions from still-present ones', () => {
    const out = formatPreviousDecisionOutcomes({
      created: [],
      retired: [
        { direction_id: 'd-gone', role: 'retired', status: 'missing' },
        { direction_id: 'd-here', role: 'retired', status: 'retired', cycles_active: 5, prs_merged: 1, quality_score: 0.4 },
      ],
    });
    expect(out).toContain('Directions you RETIRED');
    expect(out).toContain('d-gone');
    expect(out).toContain('fully removed from direction list');
    expect(out).toContain('d-here');
    expect(out).toContain('status=retired');
  });

  it('handles malformed entries without throwing', () => {
    expect(() => formatPreviousDecisionOutcomes({
      created: [{}, { direction_id: 'd-partial' }],
      retired: [{ direction_id: null }],
    })).not.toThrow();
  });

  it('includes reflection prompt encouraging retrospective', () => {
    const out = formatPreviousDecisionOutcomes({
      created: [{ direction_id: 'd-x', status: 'active', cycles_active: 1, prs_merged: 0, quality_score: 0 }],
      retired: [],
    });
    expect(out.toLowerCase()).toContain('reflect');
  });
});

describe('formatPastRetirementCritiques', () => {
  it('returns empty string for empty array', () => {
    expect(formatPastRetirementCritiques([])).toBe('');
  });

  it('returns empty string for non-array input', () => {
    expect(formatPastRetirementCritiques(null)).toBe('');
    expect(formatPastRetirementCritiques(undefined)).toBe('');
    expect(formatPastRetirementCritiques('string')).toBe('');
  });

  it('renders items with addressed, pattern, and lesson fields', () => {
    const out = formatPastRetirementCritiques([
      {
        direction_id: 'd-ret-1',
        what_it_addressed: 'frontend perf',
        pattern_that_produced_it: 'too broad',
        lesson: 'scope to one component',
      },
    ]);
    expect(out).toContain('Lessons from past retired directions');
    expect(out).toContain('d-ret-1');
    expect(out).toContain('frontend perf');
    expect(out).toContain('too broad');
    expect(out).toContain('scope to one component');
  });

  it('renders no_eligible_retirements sentinel', () => {
    const out = formatPastRetirementCritiques([{ no_eligible_retirements: true }]);
    expect(out).toContain('no eligible retired directions');
  });

  it('skips items with no relevant fields', () => {
    const out = formatPastRetirementCritiques([{ direction_id: 'd-empty' }]);
    expect(out).toBe('');
  });
});

describe('formatRetiredDirectionsSummary', () => {
  it('returns empty string for empty array', () => {
    expect(formatRetiredDirectionsSummary([])).toBe('');
  });

  it('returns empty string for non-array input', () => {
    expect(formatRetiredDirectionsSummary(null)).toBe('');
    expect(formatRetiredDirectionsSummary(undefined)).toBe('');
  });

  it('renders retired directions with cycles, merged, reason', () => {
    const out = formatRetiredDirectionsSummary([
      {
        id: 'd-old-1',
        text: 'Migrate to gRPC',
        status: 'auto_completed',
        cycles: 4,
        prs_merged: 2,
        retirement_reason: 'work completed',
        dominant_taxonomy: 'backend',
        cost_per_merge: 3.5,
      },
    ]);
    expect(out).toContain('Recently retired directions');
    expect(out).toContain('d-old-1');
    expect(out).toContain('Migrate to gRPC');
    expect(out).toContain('4 cycles');
    expect(out).toContain('2 merged');
    expect(out).toContain('work completed');
    expect(out).toContain('dominant: backend');
    expect(out).toContain('$3.5/merge');
  });

  it('renders failed_approaches when present', () => {
    const out = formatRetiredDirectionsSummary([
      {
        id: 'd-f',
        text: 'Foo',
        status: 'retired',
        cycles: 1,
        prs_merged: 0,
        failed_approaches: ['approach A', 'approach B'],
      },
    ]);
    expect(out).toContain('Failed approaches: approach A; approach B');
  });

  it('caps failed_approaches at 3', () => {
    const out = formatRetiredDirectionsSummary([
      {
        id: 'd-g',
        text: 'Bar',
        status: 'retired',
        cycles: 1,
        prs_merged: 0,
        failed_approaches: ['a1', 'a2', 'a3', 'a4', 'a5'],
      },
    ]);
    expect(out).toContain('a1; a2; a3');
    expect(out).not.toContain('a4');
  });

  it('renders retirement_outcome when present', () => {
    const out = formatRetiredDirectionsSummary([
      {
        id: 'd-outcome',
        text: 'Some direction',
        status: 'auto_completed',
        cycles: 3,
        prs_merged: 1,
        retirement_outcome: 'all acceptance criteria met',
      },
    ]);
    expect(out).toContain('outcome: all acceptance criteria met');
  });

  it('renders both retirement_reason and retirement_outcome when both present', () => {
    const out = formatRetiredDirectionsSummary([
      {
        id: 'd-both',
        text: 'Direction with both',
        status: 'retired',
        cycles: 2,
        prs_merged: 0,
        retirement_reason: 'stalled due to infra',
        retirement_outcome: 'no progress made',
      },
    ]);
    expect(out).toContain('reason: stalled due to infra');
    expect(out).toContain('outcome: no progress made');
  });
});
