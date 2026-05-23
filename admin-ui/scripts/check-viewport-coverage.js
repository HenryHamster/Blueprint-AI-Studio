#!/usr/bin/env node
// CI gate: runs all five viewport-coverage invariants defined in
// src/test/viewport-checks.js and exits non-zero if any fail.
// Invoked by `npm run test:viewport` before the browser-mode vitest run.
import {
  checkRequiredComponentAssertions,
  checkAllComponentsHaveTests,
  check375pxOverflow,
  checkBackendPatterns,
  checkEscapeHatchBudget,
} from '../src/test/viewport-checks.js';

let hasErrors = false;

// Invariant 1: required components have all three assertions at required widths
const inv1 = checkRequiredComponentAssertions();
if (inv1.length > 0) {
  console.error(
    '[viewport] Required components missing one or more viewport assertions at required widths:'
  );
  console.error(
    '  Each component must assert all three: horizontal overflow, silent text truncation,'
  );
  console.error('  and minimum touch target (or a single expectFullViewportAt call).');
  for (const v of inv1) console.error(`  ${v}`);
  hasErrors = true;
}

// Invariant 2: every operator component has a viewport test or is exempted
const inv2 = checkAllComponentsHaveTests();
if (inv2.length > 0) {
  console.error('[viewport] Operator components missing *.viewport.test.tsx files:');
  console.error(
    '  Add a viewport test or add to the exemption list in src/test/viewport-checks.js:'
  );
  for (const c of inv2) console.error(`  admin-ui/src/components/${c}.viewport.test.tsx`);
  hasErrors = true;
}

// Invariant 3: every non-exempt component asserts 375px overflow
const inv3 = check375pxOverflow();
if (inv3.length > 0) {
  console.error('[viewport] Components missing 375px horizontal-overflow assertion:');
  console.error(
    '  Use expectNoOverflowAt(375, ...) or expectFullViewportAt(375, ...) in each viewport test.'
  );
  for (const v of inv3) console.error(`  ${v}`);
  hasErrors = true;
}

// Invariant 4: backend _VIEWPORT_PATTERNS match viewport helper call-name prefixes
const inv4 = checkBackendPatterns();
if (inv4.length > 0) {
  console.error('[viewport] Backend _VIEWPORT_PATTERNS out of sync with viewport helper names:');
  console.error('  Update _VIEWPORT_PATTERNS in backend/app/failure_classification.py to match:');
  for (const p of inv4) console.error(`  "${p}" not found in failure_classification.py`);
  hasErrors = true;
}

// Invariant 5: escape-hatch debt within per-component and total budget
const inv5 = checkEscapeHatchBudget();
if (inv5.length > 0) {
  console.error('[viewport] Viewport escape-hatch debt exceeds budget:');
  console.error(
    '  Bump the limit in admin-ui/src/test/viewport-escape-hatch-budget.json in the same PR.'
  );
  for (const v of inv5) console.error(v);
  hasErrors = true;
}

if (!hasErrors) {
  console.log('[viewport] All viewport coverage invariants pass');
}

process.exit(hasErrors ? 1 : 0);
