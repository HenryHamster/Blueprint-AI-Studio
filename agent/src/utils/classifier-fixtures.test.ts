/**
 * Behavioral regression net for classifyTestExecution — the no-CI merge gate's
 * test-output classifier.
 *
 * This suite runs the REAL classifier over the shared corpus at
 * backend/testing/__fixtures__/classifier_outputs.json and asserts the declared
 * label for every row. It is the behavioral counterpart to the backend
 * always-on shape guard (backend/testing/test_classifier_fixtures.py): the
 * backend guarantees the corpus can never be silently gutted in plain GitHub CI,
 * and this suite guarantees that a future change reintroducing an UNVERIFIED
 * fall-open (a genuine suite printing placeholder-looking text misread as
 * "no tests ran") fails here — even while the source-scan adjacency invariant
 * stays green.
 *
 * The fixture lives under backend/testing/__fixtures__/ so the backend guard is
 * always-on in plain GitHub CI; we read it via the established cross-package URL
 * pattern (see test-runners.test.ts).
 */

import { describe, it, expect } from 'vitest';
import { readFile } from 'node:fs/promises';
import { classifySetupFailure, classifyTestExecution } from './test-runners.js';

type ClassifierFixtureRow = {
  name: string;
  output: string;
  expected: 'ran' | 'no_tests';
  category: string;
  genuine_suite: boolean;
  setup_expected?: boolean;
};

const fixtures = JSON.parse(
  await readFile(
    new URL(
      '../../../backend/testing/__fixtures__/classifier_outputs.json',
      import.meta.url,
    ),
    'utf-8',
  ),
) as ClassifierFixtureRow[];

const EXPECTED_CATEGORIES = [
  'command-not-found',
  'missing-script',
  'no-test-specified',
  'make-target',
  'zero-test',
  'setup-missing-runtime',
  'setup-missing-dependency',
] as const;

describe('classifyTestExecution — shared fixture corpus (behavioral net)', () => {
  it('the shared fixture is non-empty', () => {
    expect(fixtures.length).toBeGreaterThan(0);
  });

  it('every fixture row classifies to its declared label', () => {
    const mismatches: string[] = [];
    for (const row of fixtures) {
      const actual = classifyTestExecution(row.output);
      if (actual !== row.expected) {
        mismatches.push(
          `${row.name} [${row.category}]: expected ${row.expected}, got ${actual}`,
        );
      }
    }
    expect(mismatches).toEqual([]);
  });

  it('fixture rows with setup expectations match the setup-failure classifier', () => {
    const rows = fixtures.filter((r) => typeof r.setup_expected === 'boolean');
    expect(rows.length).toBeGreaterThan(0);
    for (const row of rows) {
      expect(
        classifySetupFailure(row.output),
        `${row.name} must match setup_expected`,
      ).toBe(row.setup_expected);
    }
  });

  it('genuine suites containing placeholder-looking text still classify as "ran"', () => {
    const genuine = fixtures.filter((r) => r.genuine_suite);
    expect(genuine.length).toBeGreaterThan(0);
    for (const row of genuine) {
      expect(
        classifyTestExecution(row.output),
        `genuine suite ${row.name} must classify as ran`,
      ).toBe('ran');
    }
  });

  it('placeholder / empty outputs classify as "no_tests"', () => {
    const placeholder = fixtures.filter((r) => !r.genuine_suite);
    expect(placeholder.length).toBeGreaterThan(0);
    for (const row of placeholder) {
      expect(
        classifyTestExecution(row.output),
        `placeholder ${row.name} must classify as no_tests`,
      ).toBe('no_tests');
    }
  });

  it('every classifier phrase category is represented in the corpus', () => {
    const present = new Set(fixtures.map((r) => r.category));
    for (const category of EXPECTED_CATEGORIES) {
      expect(present.has(category), `missing category ${category}`).toBe(true);
    }
  });
});
