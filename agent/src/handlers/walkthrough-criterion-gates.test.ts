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

const {
  computeGates,
  CRITERION_STEP_REQUIREMENTS,
  resolveCriterionResults,
  shouldEmitResolvedRows,
  WALKTHROUGH_FLOW_GROUPS,
  WALKTHROUGH_E2E_LEGACY_HARNESS_KEYS,
  WALKTHROUGH_E2E_SUBSTANTIVE_LEGACY_KEYS,
  WALKTHROUGH_META_CRITERION_KEY,
} = _require(
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
  resolveCriterionResults: (
    activeCriteria: Array<{ criterion_key: string; text: string }>,
    results: Array<{ name: string; verdict: string }>,
    shaValid: boolean,
  ) => Array<{ criterion_key: string; passed: boolean; notes: string }>;
  shouldEmitResolvedRows: (
    resolvedRows: Array<{ criterion_key: string; passed: boolean; notes: string }>,
  ) => boolean;
  WALKTHROUGH_FLOW_GROUPS: Array<{ flow: string; markers: string[]; steps: string[] }>;
  WALKTHROUGH_E2E_LEGACY_HARNESS_KEYS: string[];
  WALKTHROUGH_E2E_SUBSTANTIVE_LEGACY_KEYS: string[];
  WALKTHROUGH_META_CRITERION_KEY: string;
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

// ── Open-rubric resolution (resolveCriterionResults) ────────────────────────────

/** The frozen four-row report key contract (legacy harness output). */
const FOUR_LEGACY_KEYS = [
  'c-dfdc9d82',
  'c-f52fd975',
  'c-workday-session-lifecycle',
  'c-5214996f',
];

/** A rubric that re-minted every walkthrough criterion under fresh keys. */
const FRESH_RUBRIC = [
  { criterion_key: 'c-runner-42', text: 'Runner enrollment proves the daemon heartbeats' },
  { criterion_key: 'c-iframe-42', text: 'Iframe subresource token and project tools lifecycle' },
  { criterion_key: 'c-workday-42', text: 'Workday session lifecycle' },
];

/** The legacy v26 rubric verbatim (all four legacy criteria active). */
const LEGACY_RUBRIC = [
  { criterion_key: 'c-dfdc9d82', text: 'Runner enrollment' },
  { criterion_key: 'c-f52fd975', text: 'Iframe subresource token and project tools' },
  { criterion_key: 'c-workday-session-lifecycle', text: 'Workday session lifecycle' },
  { criterion_key: 'c-5214996f', text: 'Real walkthrough criterion' },
];

describe('resolveCriterionResults — open-rubric criteria resolution', () => {
  it('test_open_rubric_criteria_resolution_emits_fresh_keys', () => {
    const rows = resolveCriterionResults(FRESH_RUBRIC, makeResults(ALL_STEPS), true);
    // The report is keyed by the rubric's ACTIVE walkthrough-provable criteria,
    // not the retired v26 keys.
    expect(rows.map((r) => r.criterion_key)).toEqual([
      'c-runner-42',
      'c-iframe-42',
      'c-workday-42',
    ]);
    for (const row of rows) {
      expect(row.passed).toBe(true);
      expect(typeof row.notes).toBe('string');
      expect(row.notes.length).toBeGreaterThan(0);
    }
  });

  it('fresh keys: per-flow pass booleans track their flow gates', () => {
    const withoutHeartbeat = ALL_STEPS.filter((s) => s !== '1d.');
    const rows = resolveCriterionResults(FRESH_RUBRIC, makeResults(withoutHeartbeat), true);
    expect(rows.find((r) => r.criterion_key === 'c-runner-42')?.passed).toBe(false);
    expect(rows.find((r) => r.criterion_key === 'c-iframe-42')?.passed).toBe(true);
    expect(rows.find((r) => r.criterion_key === 'c-workday-42')?.passed).toBe(true);
  });

  it('legacy keys still resolve to the complete four-row legacy report', () => {
    const rows = resolveCriterionResults(LEGACY_RUBRIC, makeResults(ALL_STEPS), true);
    expect(rows.map((r) => r.criterion_key)).toEqual(FOUR_LEGACY_KEYS);
    expect(rows.every((r) => r.passed)).toBe(true);
  });

  it('meta c-5214996f passes only when every substantive sub-criterion passes in the same batch', () => {
    const rubric = [
      { criterion_key: 'c-dfdc9d82', text: 'Runner enrollment' },
      { criterion_key: 'c-f52fd975', text: 'Iframe subresource token' },
      { criterion_key: 'c-5214996f', text: 'Real walkthrough criterion' },
    ];
    const allPassRows = resolveCriterionResults(rubric, makeResults(ALL_STEPS), true);
    expect(allPassRows.find((r) => r.criterion_key === 'c-5214996f')?.passed).toBe(true);

    const withoutHeartbeat = resolveCriterionResults(
      rubric,
      makeResults(ALL_STEPS.filter((s) => s !== '1d.')),
      true,
    );
    expect(withoutHeartbeat.find((r) => r.criterion_key === 'c-5214996f')?.passed).toBe(false);
  });

  it('meta c-5214996f fails closed with zero substantive sub-criteria', () => {
    const rows = resolveCriterionResults(
      [{ criterion_key: 'c-5214996f', text: 'Real walkthrough criterion' }],
      makeResults(ALL_STEPS),
      true,
    );
    expect(rows).toHaveLength(1);
    expect(rows[0].criterion_key).toBe(WALKTHROUGH_META_CRITERION_KEY);
    expect(rows[0].passed).toBe(false);
  });

  it('all rows fail when shaValid is false, regardless of step verdicts', () => {
    const rows = resolveCriterionResults(FRESH_RUBRIC, makeResults(ALL_STEPS), false);
    expect(rows.length).toBeGreaterThan(0);
    expect(rows.every((r) => r.passed)).toBe(false);
  });

  it('omits active criteria whose text matches no flow marker and whose key is not legacy', () => {
    const rows = resolveCriterionResults(
      [{ criterion_key: 'c-compute-1', text: 'Compute viewable artifacts' }],
      makeResults(ALL_STEPS),
      true,
    );
    expect(rows).toHaveLength(0);
  });

  it('matches flow markers case-insensitively', () => {
    const rows = resolveCriterionResults(
      [{ criterion_key: 'c-runner-99', text: 'RUNNER ENROLLMENT end-to-end' }],
      makeResults(['1a.', '1b.', '1d.', '6a.', '6b.']),
      true,
    );
    expect(rows.map((r) => r.criterion_key)).toEqual(['c-runner-99']);
    expect(rows[0].passed).toBe(true);
  });

  it('exports the flow groups, legacy keys, and meta key the resolver mirrors', () => {
    // Mirrors backend/app/acceptance_criteria.py WALKTHROUGH_E2E_FLOW_MARKERS.
    expect(WALKTHROUGH_FLOW_GROUPS.map((g) => g.flow)).toEqual(['runner', 'iframe', 'workday']);
    expect(WALKTHROUGH_FLOW_GROUPS.find((g) => g.flow === 'runner')?.markers).toEqual([
      'runner enroll',
      'runner enrollment',
    ]);
    expect(WALKTHROUGH_FLOW_GROUPS.find((g) => g.flow === 'iframe')?.markers).toEqual([
      'iframe',
      'subresource token',
      'project tool',
    ]);
    expect(WALKTHROUGH_FLOW_GROUPS.find((g) => g.flow === 'workday')?.markers).toEqual([
      'workday',
    ]);
    // Mirrors backend WALKTHROUGH_E2E_LEGACY_HARNESS_KEYS + _WALKTHROUGH_META_CRITERION_KEY.
    expect(WALKTHROUGH_E2E_LEGACY_HARNESS_KEYS).toEqual([
      'c-dfdc9d82',
      'c-f52fd975',
      'c-5214996f',
    ]);
    // Mirrors backend _WALKTHROUGH_E2E_SUBSTANTIVE_LEGACY_RAW_KEYS: the meta key
    // is NOT a legacy signal, so it is absent from the substantive subset.
    expect(WALKTHROUGH_E2E_SUBSTANTIVE_LEGACY_KEYS).toEqual([
      'c-dfdc9d82',
      'c-f52fd975',
      'c-workday-session-lifecycle',
    ]);
    expect(WALKTHROUGH_META_CRITERION_KEY).toBe('c-5214996f');
  });

  it('fresh-substantive + active-meta rubric: emits the fresh rows AND the meta row', () => {
    // The exact shape from backend test_meta_criterion_gate_resolves_active_rubric:
    // substantive walkthrough criteria re-minted under fresh keys while the
    // canonical meta criterion c-5214996f stays active. The resolved report must
    // carry the fresh rows + the meta row — NOT the legacy four-row report.
    const freshWithMeta = [
      { criterion_key: 'c-fresh-runner-enroll', text: 'Runner enrollment validates credentials against the fleet' },
      { criterion_key: 'c-fresh-iframe-token', text: 'Project Tools iframe subresource token is minted and served' },
      { criterion_key: 'c-5214996f', text: 'Workday click-to-talk session lifecycle (re-walk)' },
    ];
    const rows = resolveCriterionResults(freshWithMeta, makeResults(ALL_STEPS), true);
    expect(rows.map((r) => r.criterion_key)).toEqual([
      'c-fresh-runner-enroll',
      'c-fresh-iframe-token',
      'c-5214996f',
    ]);
    expect(rows.every((r) => r.passed)).toBe(true);
    // The meta row is the conjunction: it fails when a fresh substantive row fails.
    const withoutHeartbeat = resolveCriterionResults(
      freshWithMeta,
      makeResults(ALL_STEPS.filter((s) => s !== '1d.')),
      true,
    );
    expect(withoutHeartbeat.find((r) => r.criterion_key === 'c-fresh-runner-enroll')?.passed).toBe(false);
    expect(withoutHeartbeat.find((r) => r.criterion_key === 'c-5214996f')?.passed).toBe(false);
  });
});

// ── writeJsonResult fallback decision (shouldEmitResolvedRows) ───────────────────

describe('shouldEmitResolvedRows — writeJsonResult fallback decision', () => {
  const FRESH_WITH_META = [
    { criterion_key: 'c-fresh-runner-enroll', text: 'Runner enrollment validates credentials against the fleet' },
    { criterion_key: 'c-fresh-iframe-token', text: 'Project Tools iframe subresource token is minted and served' },
    { criterion_key: 'c-5214996f', text: 'Workday click-to-talk session lifecycle (re-walk)' },
  ];

  it('emits resolved rows for a fresh-substantive + active-meta rubric (meta is NOT a legacy signal)', () => {
    const resolved = resolveCriterionResults(FRESH_WITH_META, makeResults(ALL_STEPS), true);
    expect(shouldEmitResolvedRows(resolved)).toBe(true);
  });

  it('emits resolved rows for a pure fresh rubric with no meta row', () => {
    const resolved = resolveCriterionResults(FRESH_RUBRIC, makeResults(ALL_STEPS), true);
    expect(shouldEmitResolvedRows(resolved)).toBe(true);
  });

  it('falls back to the legacy report when the rubric still carries a substantive legacy key', () => {
    const resolved = resolveCriterionResults(LEGACY_RUBRIC, makeResults(ALL_STEPS), true);
    expect(resolved.some((r) => WALKTHROUGH_E2E_SUBSTANTIVE_LEGACY_KEYS.includes(r.criterion_key))).toBe(true);
    expect(shouldEmitResolvedRows(resolved)).toBe(false);
  });

  it('falls back to the legacy report on an empty resolved batch (no walkthrough-provable criteria)', () => {
    expect(shouldEmitResolvedRows([])).toBe(false);
    expect(shouldEmitResolvedRows(null as unknown as never[])).toBe(false);
  });

  it('falls back when a resolved batch is entirely non-walkthrough criteria', () => {
    const resolved = resolveCriterionResults(
      [{ criterion_key: 'c-compute-1', text: 'Compute viewable artifacts' }],
      makeResults(ALL_STEPS),
      true,
    );
    expect(resolved).toHaveLength(0);
    expect(shouldEmitResolvedRows(resolved)).toBe(false);
  });
});
