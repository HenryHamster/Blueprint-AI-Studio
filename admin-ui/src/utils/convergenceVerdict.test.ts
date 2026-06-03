import { describe, expect, test } from 'vitest';
import { CONVERGENCE_VERDICT_RE, splitConvergenceVerdict } from './convergenceVerdict';

describe('splitConvergenceVerdict', () => {
  test('lifts the verdict sentence and returns the remaining prose', () => {
    const narrative =
      '3 tasks completed with 2 PRs opened. Review/fix churn is falling; the main blocker is cap exhausted (3/4 non-converged PR chains).';
    const { verdict, prose } = splitConvergenceVerdict(narrative);
    expect(verdict).toBe(
      'Review/fix churn is falling; the main blocker is cap exhausted (3/4 non-converged PR chains).',
    );
    expect(prose).toBe('3 tasks completed with 2 PRs opened.');
  });

  test('returns null verdict and unchanged prose when no verdict present', () => {
    const narrative = 'All quiet — no activity in the last 24h.';
    const { verdict, prose } = splitConvergenceVerdict(narrative);
    expect(verdict).toBeNull();
    expect(prose).toBe(narrative);
  });

  test('handles null/undefined narrative', () => {
    expect(splitConvergenceVerdict(null)).toEqual({ verdict: null, prose: '' });
    expect(splitConvergenceVerdict(undefined)).toEqual({ verdict: null, prose: '' });
  });

  test('regex matches rising and flat verdicts too', () => {
    expect('Review/fix churn is rising.'.match(CONVERGENCE_VERDICT_RE)?.[0]).toBe(
      'Review/fix churn is rising.',
    );
    expect('Review/fix churn is flat.'.match(CONVERGENCE_VERDICT_RE)?.[0]).toBe(
      'Review/fix churn is flat.',
    );
  });
});
