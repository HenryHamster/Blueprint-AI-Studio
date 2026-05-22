import { describe, it, expect } from 'vitest';
import {
  checkRequiredComponentAssertions,
  checkAllComponentsHaveTests,
  check375pxOverflow,
  checkBackendPatterns,
  checkEscapeHatchBudget,
} from './viewport-checks.js';

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
