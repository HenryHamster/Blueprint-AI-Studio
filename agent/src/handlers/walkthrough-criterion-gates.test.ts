/**
 * Regression tests for the walkthrough harness flow-to-criterion gate mapping.
 *
 * These tests pin which flow steps are required for each criterion to pass,
 * so that future harness edits cannot silently narrow a PASS gate.
 *
 * The gate logic lives in scripts/walkthrough/criterion-gates.cjs, which is
 * required by the live harness (v26-live-walkthrough.cjs). Testing the module
 * directly keeps the tests fast and environment-free.
 */

import { describe, it, expect } from 'vitest';
import { createRequire } from 'module';

const _require = createRequire(import.meta.url);

const { computeGates, CRITERION_STEP_REQUIREMENTS } = _require(
  '../../../scripts/walkthrough/criterion-gates.cjs',
) as {
  computeGates: (
    results: Array<{ name: string; verdict: string }>,
    shaValid: boolean,
  ) => {
    runnerEnrollmentPassed: boolean;
    iframeTokenPassed: boolean;
    workdaySessionPassed: boolean;
    walkthroughMetaPassed: boolean;
  };
  CRITERION_STEP_REQUIREMENTS: Record<string, string[]>;
};

/** Build a synthetic results array with the given step-name prefixes passing. */
function makeResults(passing: string[]): Array<{ name: string; verdict: string }> {
  return passing.map((p) => ({ name: `${p} synthetic step`, verdict: 'PASS' }));
}

/** All required steps for every criterion. */
const ALL_STEPS = ([] as string[]).concat(...Object.values(CRITERION_STEP_REQUIREMENTS));

// ── c-dfdc9d82: runner enrollment ─────────────────────────────────────────────

describe('c-dfdc9d82 runner enrollment gate', () => {
  it('fails when only 1a and 1b pass (missing 1d daemon heartbeat)', () => {
    const { runnerEnrollmentPassed } = computeGates(makeResults(['1a.', '1b.']), true);
    expect(runnerEnrollmentPassed).toBe(false);
  });

  it('fails when 1a, 1b, 1d pass but 6a (bad-token 401) is absent', () => {
    const { runnerEnrollmentPassed } = computeGates(makeResults(['1a.', '1b.', '1d.']), true);
    expect(runnerEnrollmentPassed).toBe(false);
  });

  it('fails when 1a, 1b, 1d, 6a pass but 6b (two-button UI) is absent', () => {
    const { runnerEnrollmentPassed } = computeGates(makeResults(['1a.', '1b.', '1d.', '6a.']), true);
    expect(runnerEnrollmentPassed).toBe(false);
  });

  it('passes when all five required steps pass: 1a, 1b, 1d, 6a, 6b', () => {
    const { runnerEnrollmentPassed } = computeGates(
      makeResults(['1a.', '1b.', '1d.', '6a.', '6b.']),
      true,
    );
    expect(runnerEnrollmentPassed).toBe(true);
  });

  it('CRITERION_STEP_REQUIREMENTS declares 1d, 6a, 6b for c-dfdc9d82', () => {
    const steps = CRITERION_STEP_REQUIREMENTS['c-dfdc9d82'];
    expect(steps).toContain('1d.');
    expect(steps).toContain('6a.');
    expect(steps).toContain('6b.');
  });
});

// ── c-f52fd975: iframe subresource token ──────────────────────────────────────

describe('c-f52fd975 iframe subresource token gate', () => {
  it('fails when only asset-token steps pass (2b, 2c, 2g) — tools lifecycle absent', () => {
    const { iframeTokenPassed } = computeGates(makeResults(['2b.', '2c.', '2g.']), true);
    expect(iframeTokenPassed).toBe(false);
  });

  it('fails when 2b, 2c, 2g, 7a-7d pass but 7e (no-Placement-dropdown) is absent', () => {
    const { iframeTokenPassed } = computeGates(
      makeResults(['2b.', '2c.', '2g.', '7a.', '7b.', '7c.', '7d.']),
      true,
    );
    expect(iframeTokenPassed).toBe(false);
  });

  it('passes when all eight required steps pass: 2b, 2c, 2g, 7a-7e', () => {
    const { iframeTokenPassed } = computeGates(
      makeResults(['2b.', '2c.', '2g.', '7a.', '7b.', '7c.', '7d.', '7e.']),
      true,
    );
    expect(iframeTokenPassed).toBe(true);
  });

  it('CRITERION_STEP_REQUIREMENTS declares all flow-7 steps for c-f52fd975', () => {
    const steps = CRITERION_STEP_REQUIREMENTS['c-f52fd975'];
    for (const s of ['7a.', '7b.', '7c.', '7d.', '7e.']) {
      expect(steps).toContain(s);
    }
  });

  it('CRITERION_STEP_REQUIREMENTS also declares 2b, 2c, 2g for c-f52fd975', () => {
    const steps = CRITERION_STEP_REQUIREMENTS['c-f52fd975'];
    expect(steps).toContain('2b.');
    expect(steps).toContain('2c.');
    expect(steps).toContain('2g.');
  });
});

// ── SHA validity gate ──────────────────────────────────────────────────────────

describe('SHA validity guard', () => {
  it('all criteria fail when shaValid is false, regardless of step verdicts', () => {
    const gates = computeGates(makeResults(ALL_STEPS), false);
    expect(gates.runnerEnrollmentPassed).toBe(false);
    expect(gates.iframeTokenPassed).toBe(false);
    expect(gates.workdaySessionPassed).toBe(false);
    expect(gates.walkthroughMetaPassed).toBe(false);
  });

  it('all criteria fail on empty results (no steps ran)', () => {
    const gates = computeGates([], true);
    expect(gates.runnerEnrollmentPassed).toBe(false);
    expect(gates.iframeTokenPassed).toBe(false);
    expect(gates.workdaySessionPassed).toBe(false);
    expect(gates.walkthroughMetaPassed).toBe(false);
  });
});

// ── c-workday-session-lifecycle ────────────────────────────────────────────────

describe('c-workday-session-lifecycle gate', () => {
  it('fails when only 5b and 5c pass (missing 5d, 5e, 5f)', () => {
    const { workdaySessionPassed } = computeGates(makeResults(['5b.', '5c.']), true);
    expect(workdaySessionPassed).toBe(false);
  });

  it('fails when 5b-5d pass but 5e (clean stop) is absent', () => {
    const { workdaySessionPassed } = computeGates(makeResults(['5b.', '5c.', '5d.']), true);
    expect(workdaySessionPassed).toBe(false);
  });

  it('fails when 5b-5e pass but 5f (click-to-talk) is absent', () => {
    const { workdaySessionPassed } = computeGates(
      makeResults(['5b.', '5c.', '5d.', '5e.']),
      true,
    );
    expect(workdaySessionPassed).toBe(false);
  });

  it('passes when all five required steps pass: 5b, 5c, 5d, 5e, 5f', () => {
    const { workdaySessionPassed } = computeGates(
      makeResults(['5b.', '5c.', '5d.', '5e.', '5f.']),
      true,
    );
    expect(workdaySessionPassed).toBe(true);
  });

  it('CRITERION_STEP_REQUIREMENTS declares all session steps for c-workday-session-lifecycle', () => {
    const steps = CRITERION_STEP_REQUIREMENTS['c-workday-session-lifecycle'];
    for (const s of ['5b.', '5c.', '5d.', '5e.', '5f.']) {
      expect(steps).toContain(s);
    }
  });

  it('workday session fails when shaValid is false even with all steps passing', () => {
    const { workdaySessionPassed } = computeGates(
      makeResults(['5b.', '5c.', '5d.', '5e.', '5f.']),
      false,
    );
    expect(workdaySessionPassed).toBe(false);
  });
});

// ── Flow-to-criterion registry completeness ────────────────────────────────────

describe('CRITERION_STEP_REQUIREMENTS registry completeness', () => {
  it('registers exactly the three known walkthrough criterion keys', () => {
    const keys = Object.keys(CRITERION_STEP_REQUIREMENTS);
    expect(keys).toContain('c-dfdc9d82');
    expect(keys).toContain('c-f52fd975');
    expect(keys).toContain('c-workday-session-lifecycle');
    expect(keys).toHaveLength(3);
  });

  it('every registered criterion has at least one non-empty step requirement string', () => {
    for (const [, steps] of Object.entries(CRITERION_STEP_REQUIREMENTS)) {
      expect(Array.isArray(steps)).toBe(true);
      expect(steps.length).toBeGreaterThan(0);
      for (const s of steps) {
        expect(typeof s).toBe('string');
        expect(s.length).toBeGreaterThan(0);
      }
    }
  });

  it('ALL_STEPS aggregates at least one step per criterion (no orphaned criterion)', () => {
    for (const [, steps] of Object.entries(CRITERION_STEP_REQUIREMENTS)) {
      const hasOverlap = steps.some((s) => ALL_STEPS.includes(s));
      expect(hasOverlap).toBe(true);
    }
  });
});

// ── Meta criterion ─────────────────────────────────────────────────────────────

describe('meta criterion (c-5214996f)', () => {
  it('passes only when all three sub-criteria pass', () => {
    const { walkthroughMetaPassed } = computeGates(makeResults(ALL_STEPS), true);
    expect(walkthroughMetaPassed).toBe(true);
  });

  it('fails when runner enrollment fails (missing 1d) even if others pass', () => {
    const withoutHeartbeat = ALL_STEPS.filter((s) => s !== '1d.');
    const { walkthroughMetaPassed } = computeGates(makeResults(withoutHeartbeat), true);
    expect(walkthroughMetaPassed).toBe(false);
  });

  it('fails when iframe token fails (missing 7e) even if others pass', () => {
    const withoutNoPlacement = ALL_STEPS.filter((s) => s !== '7e.');
    const { walkthroughMetaPassed } = computeGates(makeResults(withoutNoPlacement), true);
    expect(walkthroughMetaPassed).toBe(false);
  });

  it('fails when workday session fails (missing 5f) even if enrollment and iframe pass', () => {
    const withoutClickToTalk = ALL_STEPS.filter((s) => s !== '5f.');
    const { walkthroughMetaPassed } = computeGates(makeResults(withoutClickToTalk), true);
    expect(walkthroughMetaPassed).toBe(false);
  });
});
