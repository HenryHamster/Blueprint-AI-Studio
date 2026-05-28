import { describe, it, expect } from 'vitest';
import { computeMaxFixIterations, formatTrajectorySection, formatDriftSection, verifyStepsOk } from './review-fix-history.js';

describe('computeMaxFixIterations — mirrors backend _max_review_iterations', () => {
  it('returns 8 for ci_failure fix_reason regardless of scope', () => {
    expect(computeMaxFixIterations({
      context: { scope: 'small', review_loop: { fix_reason: 'ci_failure' } },
    })).toBe(8);
    expect(computeMaxFixIterations({
      context: { scope: 'large', review_loop: { fix_reason: 'ci_failure' } },
    })).toBe(8);
  });

  it('returns 10 for small scope / non-ci_failure', () => {
    expect(computeMaxFixIterations({
      context: { scope: 'small', review_loop: { fix_reason: 'code_review' } },
    })).toBe(10);
    expect(computeMaxFixIterations({ context: { scope: 'small' } })).toBe(10);
  });

  it('returns 12 for medium scope', () => {
    expect(computeMaxFixIterations({ context: { scope: 'medium' } })).toBe(12);
  });

  it('returns 16 for large or unknown scope', () => {
    expect(computeMaxFixIterations({ context: { scope: 'large' } })).toBe(16);
    expect(computeMaxFixIterations({ context: { scope: 'xlarge' } })).toBe(16);
  });

  it('defaults missing scope to medium (matches backend ctx.get("scope", "medium"))', () => {
    expect(computeMaxFixIterations({ context: {} })).toBe(12);
    expect(computeMaxFixIterations({})).toBe(12);
  });

  it('handles task.context present but review_loop missing', () => {
    // Falls through to scope-based branch; no throw.
    expect(computeMaxFixIterations({ context: { scope: 'small' } })).toBe(10);
    expect(computeMaxFixIterations({ context: { scope: 'large' } })).toBe(16);
  });

  it('handles review_loop present but fix_reason missing', () => {
    // Optional-chain ensures fix_reason is undefined, not throwing.
    expect(computeMaxFixIterations({
      context: { scope: 'medium', review_loop: { iteration: 3 } },
    })).toBe(12);
  });

  it('prefers backend-provided max_iterations over scope-based default', () => {
    // Backend may reduce cap (~30%) for low-quality directions; agent must use it
    // so "iteration N of M" and FINAL-attempt warning match what backend enforces.
    expect(computeMaxFixIterations({
      context: { scope: 'medium', review_loop: { max_iterations: 8 } },
    })).toBe(8);
    // Backend value also wins over ci_failure default.
    expect(computeMaxFixIterations({
      context: { scope: 'small', review_loop: { fix_reason: 'ci_failure', max_iterations: 6 } },
    })).toBe(6);
    // Invalid backend values fall through to scope-based default.
    expect(computeMaxFixIterations({
      context: { scope: 'small', review_loop: { max_iterations: 0 } },
    })).toBe(10);
    expect(computeMaxFixIterations({
      context: { scope: 'small', review_loop: { max_iterations: 'bad' } },
    })).toBe(10);
  });

  it('does not throw on unusual (but typed) context shapes', () => {
    // Empty object is the realistic edge; the guard coerces to ctx={}.
    expect(() => computeMaxFixIterations({ context: {} })).not.toThrow();
    expect(() => computeMaxFixIterations({})).not.toThrow();
  });
});

describe('formatTrajectorySection — fix-prompt iteration arc renderer', () => {
  it('returns empty when history is empty', () => {
    expect(formatTrajectorySection([])).toBe('');
  });

  it('returns empty when no entries carry verdict info (older backends)', () => {
    const out = formatTrajectorySection([
      { iteration: 1, feedback: 'foo' },
      { iteration: 2, feedback: 'bar' },
    ]);
    expect(out).toBe('');
  });

  it('renders APPROVE and REQUEST_CHANGES entries with diff signature', () => {
    const out = formatTrajectorySection([
      { iteration: 1, feedback: '', verdict: 'APPROVE', changed_files: ['a.py', 'b.py'] },
      { iteration: 2, feedback: 'review feedback', verdict: 'REQUEST_CHANGES', changed_files: ['main.py'] },
    ]);
    expect(out).toContain('## PR trajectory');
    expect(out).toContain('iter 1: APPROVE');
    expect(out).toContain('iter 2: REQUEST_CHANGES');
    expect(out).toContain('a.py, b.py');
    expect(out).toContain('main.py');
  });

  it('truncates long file lists with a +N more suffix', () => {
    const manyFiles = Array.from({ length: 12 }, (_, i) => `f${i}.py`);
    const out = formatTrajectorySection([
      { iteration: 1, feedback: '', verdict: 'APPROVE', changed_files: manyFiles },
    ]);
    expect(out).toMatch(/f0\.py.*f7\.py.*\+4 more/s);
  });
});

describe('formatDriftSection — drift signal renderer', () => {
  it('returns empty when no drift', () => {
    expect(formatDriftSection({ drift_in: [], drift_out: [] })).toBe('');
    expect(formatDriftSection(undefined)).toBe('');
  });

  it('renders drift_in with first_appeared + flag_count metadata', () => {
    const out = formatDriftSection({
      drift_in: [
        { file: 'main.py', first_appeared_iteration: 2, flag_count: 3 },
        { file: 'other.py' }, // bare entry — no metadata
      ],
      drift_out: ['test_reaper.py'],
    });
    expect(out).toContain('main.py');
    expect(out).toContain('first appeared iter 2');
    expect(out).toContain('flagged 3x');
    // Bare entry should not show metadata.
    expect(out).toMatch(/- other\.py(?!.*first appeared)/s);
    expect(out).toContain('test_reaper.py');
  });

  it('frames drift as informational with explicit "scope expansion is sometimes correct" guidance', () => {
    const out = formatDriftSection({
      drift_in: [{ file: 'foo.py' }],
      drift_out: [],
    });
    // Must NOT be directive — agent should retain judgment.
    expect(out).toContain('informational — not a directive');
    // Must explicitly acknowledge justified scope expansion (the user-flagged
    // concern that drift signaling shouldn't push the agent to reflexively revert).
    expect(out).toContain('scope expansion IS sometimes the right call');
    expect(out).toContain('caller-impact');
    expect(out).toContain('discovered-dep');
    expect(out).toContain('cluster-boundary correction');
  });
});

describe('verifyStepsOk — sycophancy guard for fix_agent_responses', () => {
  it('rejects entries with no verify steps', () => {
    expect(verifyStepsOk({
      claim: 'this is fine',
      verify: [],
    })).toBe(false);
  });

  it('rejects entries where every verify step is non-mechanical (sycophancy patterns)', () => {
    expect(verifyStepsOk({
      claim: 'I disagree',
      verify: ['trust me, I tested it', "I'm confident this works"],
    })).toBe(false);
  });

  it('accepts entries with at least one mechanical verify step even if other steps are non-mechanical', () => {
    expect(verifyStepsOk({
      claim: 'caller-impact justification',
      verify: [
        'I re-read the code',  // non-mechanical
        'grep -n "newApi" backend/app/auth.py',  // mechanical
      ],
    })).toBe(true);
  });

  it('accepts purely mechanical verify steps', () => {
    expect(verifyStepsOk({
      claim: 'fix is in helper file',
      verify: ['cat backend/app/helpers.py | head -20', 'pytest backend/testing/test_helpers.py::test_X'],
    })).toBe(true);
  });

  it('rejects entries with empty claim', () => {
    expect(verifyStepsOk({
      claim: '',
      verify: ['grep "foo" file.py'],
    })).toBe(false);
  });
});
