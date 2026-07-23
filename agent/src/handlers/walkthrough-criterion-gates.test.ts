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
});
