import { describe, it, expect } from 'vitest';
import {
  checkRequiredComponentAssertions,
  checkAllComponentsHaveTests,
  check375pxOverflow,
  checkBackendPatterns,
  checkBackendEslintPattern,
  checkBackendExemptionMirror,
  checkEscapeHatchBudget,
  classifyEscapeHatchOverage,
  isBenignPlaywrightRouteRace,
  SINGLE_OPT_OUT_HEADROOM,
} from './viewport-checks.js';

describe('viewport runner unhandled-error filter', () => {
  it.each([
    'Route is already handled!',
    'route.fulfill: Route is already handled!',
  ])('accepts the known provider-owned duplicate fulfill race: %s', message => {
    expect(
      isBenignPlaywrightRouteRace({
        message,
        stack: 'at ../node_modules/@vitest/browser-playwright/dist/index.js:989:20',
      }),
    ).toBe(true);
  });

  it.each([
    'Route is already handled!',
    'route.fulfill: Route is already handled!',
  ])('accepts Vitest serialized Playwright provenance without a stack: %s', message => {
    expect(isBenignPlaywrightRouteRace({ message, log: [] })).toBe(true);
  });

  it.each([
    'Route is already handled!',
    'route.fulfill: Route is already handled!',
  ])('accepts the raw pre-serialization provider error: %s', message => {
    expect(isBenignPlaywrightRouteRace({ message })).toBe(true);
  });

  it.each([
    new Error('application rejection'),
    {
      message: 'different provider failure',
      stack: 'at ../node_modules/@vitest/browser-playwright/dist/index.js:989:20',
    },
    'route.fulfill: Route is already handled!',
  ])('rejects non-matching errors %#', (error) => {
    expect(isBenignPlaywrightRouteRace(error)).toBe(false);
  });
});

describe('viewport test coverage invariant', () => {
  it('fails CI when a required component is missing all three viewport assertions at required widths', () => {
    const missing = checkRequiredComponentAssertions();

    const errorMessage =
      missing.length > 0
        ? `
The following required components are missing one or more viewport assertions at required widths.
Each component must assert all three: horizontal overflow, silent text truncation, and minimum touch target.
A single expectFullViewportAt(width, ...) call satisfies all three.

${missing.map(m => `  ${m}`).join('\n')}

For components with no interactive controls, the touch-target assertion may use a scoped selector
or a vacuous call like: expectMinTouchTargetAt(375, async () => {...}, { selector: 'none' })

Required widths come from admin-ui/src/test/viewport-required.json.
        `.trim()
        : '';

    expect(missing, errorMessage).toEqual([]);
  });

  it('fails CI when a new operator component lacks a *.viewport.test.tsx file', () => {
    const missingTests = checkAllComponentsHaveTests();

    const errorMessage =
      missingTests.length > 0
        ? `
The following operator components are missing *.viewport.test.tsx files.
Add a viewport test file for each (or add to the exemption list with justification):

${missingTests.map(c => `  admin-ui/src/components/${c}.viewport.test.tsx`).join('\n')}

If a component should be exempt from viewport testing, add it to the
exemptFromViewportTest Set in admin-ui/src/test/viewport-checks.js with a code comment justifying why.
        `.trim()
        : '';

    expect(missingTests, errorMessage).toEqual([]);
  });

  it('fails CI when an operator component lacks a 375px overflow assertion', () => {
    const missing = check375pxOverflow();

    const errorMessage =
      missing.length > 0
        ? `
The following operator components are missing a 375px horizontal-overflow assertion.
Every non-exempt component must assert that it renders without overflow at 375px mobile width.
Use expectNoOverflowAt(375, ...) or expectFullViewportAt(375, ...) in each viewport test.

${missing.map(m => `  ${m}`).join('\n')}
        `.trim()
        : '';

    expect(missing, errorMessage).toEqual([]);
  });

  it('python _VIEWPORT_PATTERNS strings match exported viewport helper names', () => {
    // backend/app/failure_classification.py routes viewport-suite failures to
    // VIEWPORT_REGRESSION by lowercasing the error message and checking for these
    // substrings.  If a helper is renamed in viewport.ts without updating the Python
    // patterns, viewport failures silently dissolve into CODE_ERROR residuals instead
    // of routing to VIEWPORT_REGRESSION — misclassification that undermines the
    // self-healing loop's diagnosis quality.
    //
    // Cross-language contract: kept in sync with _VIEWPORT_PATTERNS in
    // backend/app/failure_classification.py.  Verified by
    // TestCrossLanguageClassificationContracts in backend/testing/test_failure_classification.py.
    const missing = checkBackendPatterns();

    const errorMessage =
      missing.length > 0
        ? `
The following viewport helper name patterns are no longer found in
backend/app/failure_classification.py (_VIEWPORT_PATTERNS).
Either update _VIEWPORT_PATTERNS to match the current viewport.ts export names,
or update viewport.ts to keep the original names:

${missing.map(p => `  "${p}"`).join('\n')}
        `.trim()
        : '';

    expect(missing, errorMessage).toEqual([]);
  });

  it('backend admin_digest.py eslint-disable regex matches the canonical CI gate pattern', () => {
    // The Viewport Debt panel counts eslint-disable viewport/* opt-outs using
    // _ESLINT_VIEWPORT_RE in admin_digest.py.  It must use the same pattern as
    // checkEscapeHatchBudget (ESLINT_VIEWPORT_PATTERN) so operators see counts
    // that exactly match what CI enforces.
    const violations = checkBackendEslintPattern();

    const errorMessage =
      violations.length > 0
        ? `
${violations.join('\n')}
        `.trim()
        : '';

    expect(violations, errorMessage).toEqual([]);
  });

  it('backend viewport_debt.py derives its exemption set from the single canonical viewport-exempt.json', () => {
    // The operator Viewport Debt panel computes untested_count from the
    // exemption set in backend/app/viewport_debt.py. That set must be DERIVED
    // from the one canonical source (admin-ui/src/test/viewport-exempt.json),
    // the same file CI reads — not from a parallel hand-edited Python literal.
    // A backend copy that drifts from the JS canonical set is the exact bug
    // #5752 / #5759 addressed; reading the one JSON makes that drift
    // structurally impossible. This guards against a regression that
    // reintroduces a hardcoded backend copy.
    const violations = checkBackendExemptionMirror();

    const errorMessage =
      violations.length > 0
        ? `
${violations.join('\n')}
        `.trim()
        : '';

    expect(violations, errorMessage).toEqual([]);
  });

  it('fails CI when escape-hatch debt exceeds the per-component or total budget', () => {
    const violations = checkEscapeHatchBudget();

    const errorMessage =
      violations.length > 0
        ? `
Viewport escape-hatch debt exceeds the budget in admin-ui/src/test/viewport-escape-hatch-budget.json.
Escape hatches are: data-viewport-allow-overflow, data-viewport-allow-truncation,
data-viewport-allow-small-target, and eslint-disable viewport/* comments.

${violations.join('\n')}

To fix: bump the per-component limit (and "total") in viewport-escape-hatch-budget.json in the same PR
that adds the new escape hatch. The budget is a visible, reviewable signal — not a ratchet.
A single justified opt-out can raise a component's budget by 1; a cluster requires a larger bump.
        `.trim()
        : '';

    expect(violations, errorMessage).toEqual([]);
  });
});

describe('escape-hatch single-opt-out headroom (classifyEscapeHatchOverage)', () => {
  // A refactor that splits/moves a component inevitably redistributes existing
  // escape hatches. The headroom lets a single new opt-out through (surfaced as
  // a CI-log warning) so the rollup doesn't dead-end; a cluster still fails and
  // must bump the budget JSON in the PR (see build_ci_failure_fix_prompt).
  it('is ok at or under budget', () => {
    expect(classifyEscapeHatchOverage(0, 1)).toBe('ok');
    expect(classifyEscapeHatchOverage(1, 1)).toBe('ok');
  });

  it('treats an unlisted component (undefined budget) as ok', () => {
    expect(classifyEscapeHatchOverage(5, undefined)).toBe('ok');
  });

  it('warns (does NOT fail) when over by exactly the single-opt-out headroom', () => {
    expect(SINGLE_OPT_OUT_HEADROOM).toBe(1);
    expect(classifyEscapeHatchOverage(2, 1)).toBe('warn');
    expect(classifyEscapeHatchOverage(4, 3)).toBe('warn');
  });

  it('is a violation when over by more than the headroom (a cluster)', () => {
    expect(classifyEscapeHatchOverage(3, 1)).toBe('violation');
    expect(classifyEscapeHatchOverage(10, 5)).toBe('violation');
  });
});
