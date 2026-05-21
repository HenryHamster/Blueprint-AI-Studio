import { describe, it, expect } from 'vitest';
import fs from 'fs';
import path from 'path';

describe('viewport test coverage invariant', () => {
  it('fails CI when a required component is missing all three viewport assertions at required widths', () => {
    const requiredPath = path.join(__dirname, 'viewport-required.json');
    const { requiredWidths, components } = JSON.parse(fs.readFileSync(requiredPath, 'utf-8')) as {
      requiredWidths: number[];
      components: string[];
    };

    const componentsDir = path.join(__dirname, '../components');
    const missing: string[] = [];

    // Assertion types that must be present at each required width.
    // A single expectFullViewportAt call counts as all three.
    const assertionTypes = ['overflow', 'text_truncation', 'touch_target'];

    for (const component of components) {
      const testFile = path.join(componentsDir, `${component}.viewport.test.tsx`);
      if (!fs.existsSync(testFile)) {
        for (const width of requiredWidths) {
          for (const type of assertionTypes) {
            missing.push(`${component} — no viewport test file (needs ${type} at ${width}px)`);
          }
        }
        continue;
      }
      const content = fs.readFileSync(testFile, 'utf-8');
      for (const width of requiredWidths) {
        // Check if expectFullViewportAt(width) is present — it counts as all three
        const hasFullViewport = new RegExp(`expectFullViewportAt\\s*\\(\\s*${width}[^\\d]`).test(content);

        if (hasFullViewport) {
          // Single expectFullViewportAt call satisfies all three assertion types
          continue;
        }

        // Otherwise, check for each assertion type individually
        const overflowRegex = new RegExp(`expectNoOverflowAt\\s*\\(\\s*${width}[^\\d]`);
        const truncationRegex = new RegExp(`expectNoTextTruncationAt\\s*\\(\\s*${width}[^\\d]`);
        const touchRegex = new RegExp(`expectMinTouchTargetAt\\s*\\(\\s*${width}[^\\d]`);

        if (!overflowRegex.test(content)) {
          missing.push(`${component} — missing overflow assertion at ${width}px in ${component}.viewport.test.tsx`);
        }
        if (!truncationRegex.test(content)) {
          missing.push(`${component} — missing text_truncation assertion at ${width}px in ${component}.viewport.test.tsx`);
        }
        if (!touchRegex.test(content)) {
          missing.push(`${component} — missing touch_target assertion at ${width}px in ${component}.viewport.test.tsx`);
        }
      }
    }

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
    const componentsDir = path.join(__dirname, '../components');
    const files = fs.readdirSync(componentsDir);

    // All .tsx component files (exclude test files)
    const components = files
      .filter(f => f.endsWith('.tsx') && !f.endsWith('.test.tsx') && !f.endsWith('.viewport.test.tsx'))
      .map(f => f.replace('.tsx', ''))
      .sort();

    // All .viewport.test.tsx files
    const viewportTests = files
      .filter(f => f.endsWith('.viewport.test.tsx'))
      .map(f => f.replace('.viewport.test.tsx', ''))
      .sort();

    // Allowlist: components that are exempt from the viewport-test invariant.
    // Each entry is a utility/internal component that is never rendered standalone
    // on an operator page — overflow is handled by the parent that composes it.
    const exemptFromViewportTest = new Set([
      // Internal decorator component for error isolation; not operator-facing.
      'ErrorBoundary',
      // Pure presentation: renders markdown blocks, used in help/documentation
      // panels where overflow is already handled by parent scroll container.
      'MarkdownContent',
      // Utility component: renders chip columns in KanbanBoard's table; layout
      // controlled entirely by parent, not rendering standalone in operator pages.
      'ColumnsPicker',
      // Toast notifications: positioned fixed/absolutely, exempt from viewport
      // flow overflow checks; positioned via Tailwind fixed positioning.
      'Toasts',
      // Internal mapper: generates DOM from regression data array; not used
      // standalone on operator pages, used within parent containers that handle layout.
      'catchupRows',
      // Internal mapper: generates chip DOM from phase data; not standalone,
      // layout controlled by parent container.
      'regressionPhaseChips',
    ]);

    const missingTests = components
      .filter(c => !viewportTests.includes(c) && !exemptFromViewportTest.has(c))
      .sort();

    const errorMessage =
      missingTests.length > 0
        ? `
The following operator components are missing *.viewport.test.tsx files.
Add a viewport test file for each (or add to the exemption list with justification):

${missingTests.map(c => `  admin-ui/src/components/${c}.viewport.test.tsx`).join('\n')}

If a component should be exempt from viewport testing, add it to the
exemptFromViewportTest Set in this file with a code comment justifying why.
        `.trim()
        : '';

    expect(missingTests, errorMessage).toEqual([]);
  });

  it('fails CI when an operator component lacks a 375px overflow assertion', () => {
    const componentsDir = path.join(__dirname, '../components');
    const files = fs.readdirSync(componentsDir);

    // All .tsx component files (exclude test files)
    const components = files
      .filter(f => f.endsWith('.tsx') && !f.endsWith('.test.tsx') && !f.endsWith('.viewport.test.tsx'))
      .map(f => f.replace('.tsx', ''))
      .sort();

    // Exempt components (same list as above)
    const exemptFromViewportTest = new Set([
      'ErrorBoundary',
      'MarkdownContent',
      'ColumnsPicker',
      'Toasts',
      'catchupRows',
      'regressionPhaseChips',
    ]);

    const missing: string[] = [];

    for (const component of components) {
      if (exemptFromViewportTest.has(component)) {
        continue;
      }

      const testFile = path.join(componentsDir, `${component}.viewport.test.tsx`);
      if (!fs.existsSync(testFile)) {
        continue; // Already caught by the previous test
      }

      const content = fs.readFileSync(testFile, 'utf-8');
      // Check if the test file asserts overflow at 375px
      const hasOverflowAssertion = new RegExp(`expectNoOverflowAt\\s*\\(\\s*375[^\\d]|expectFullViewportAt\\s*\\(\\s*375[^\\d]`).test(content);

      if (!hasOverflowAssertion) {
        missing.push(`${component} — missing 375px overflow assertion in ${component}.viewport.test.tsx`);
      }
    }

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
    const backendSrc = fs.readFileSync(
      path.join(__dirname, '../../../backend/app/failure_classification.py'),
      'utf-8',
    ).toLowerCase();

    // Each pattern is the lowercase function-call prefix emitted by that helper
    // in its thrown Error message (e.g. `expectNoOverflowAt(375) ...`).
    const patterns: string[] = [
      'expectnooverflowat(',
      'expectnotexttruncationat(',
      'expectmintouchtargetat(',
    ];

    const missing = patterns.filter(p => !backendSrc.includes(p));

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
    const budgetPath = path.join(__dirname, 'viewport-escape-hatch-budget.json');
    const { total: totalBudget, perComponent } = JSON.parse(fs.readFileSync(budgetPath, 'utf-8')) as {
      total: number;
      perComponent: Record<string, number>;
    };

    const componentsDir = path.join(__dirname, '../components');
    const files = fs.readdirSync(componentsDir).filter(
      f => f.endsWith('.tsx') && !f.endsWith('.test.tsx') && !f.endsWith('.viewport.test.tsx')
    );

    // Matches all three escape-hatch data attributes and eslint-disable viewport/* comments.
    const escapeHatchPattern =
      /data-viewport-allow-overflow|data-viewport-allow-truncation|data-viewport-allow-small-target|eslint-disable[^\n]*viewport/g;

    const violations: string[] = [];
    let grandTotal = 0;

    for (const file of files) {
      const component = file.replace('.tsx', '');
      const content = fs.readFileSync(path.join(componentsDir, file), 'utf-8');
      const count = (content.match(escapeHatchPattern) ?? []).length;
      grandTotal += count;

      const budget = perComponent[component];
      if (budget !== undefined && count > budget) {
        violations.push(
          `  ${component}: ${count} escape hatches (budget: ${budget}, over by ${count - budget})`
        );
      }
    }

    if (grandTotal > totalBudget) {
      violations.push(
        `  [total]: ${grandTotal} escape hatches (budget: ${totalBudget}, over by ${grandTotal - totalBudget})`
      );
    }

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
