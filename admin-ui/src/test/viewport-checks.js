// Shared ESM helper: viewport coverage invariant checks.
// Imported by both scripts/check-viewport-coverage.js (Node script) and
// src/test/viewport-coverage.test.ts (vitest jsdom suite).
// Path resolution uses import.meta.url so the module is cwd-independent.
import { fileURLToPath } from 'url';
import path from 'path';
import fs from 'fs';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const COMPONENTS_DIR = path.join(__dirname, '../components');
const REQUIRED_JSON = path.join(__dirname, 'viewport-required.json');
const BUDGET_JSON = path.join(__dirname, 'viewport-escape-hatch-budget.json');
const BACKEND_PY = path.join(__dirname, '../../../backend/app/failure_classification.py');
const VIEWPORT_DEBT_PY = path.join(__dirname, '../../../backend/app/viewport_debt.py');

// Canonical eslint-disable pattern for viewport escape-hatch counting.
// Must stay in sync with _ESLINT_VIEWPORT_RE in backend/app/viewport_debt.py
// so the count shown in the Viewport Debt panel matches the count CI enforces.
export const ESLINT_VIEWPORT_PATTERN = 'eslint-disable[^\\n]*viewport';

// Single source of truth for viewport-test exemptions.
// Components listed here are internal/utility and never rendered standalone on
// an operator page — overflow is handled by the parent that composes them.
// Keep in sync with the Python mirror in backend/app/viewport_debt.py
// (_EXEMPT_FROM_VIEWPORT_TEST) so the operator Viewport Debt panel
// "untested" count matches what the CI gate enforces.
export const EXEMPT_FROM_VIEWPORT_TEST = new Set([
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

/**
 * Invariant 1: every component in viewport-required.json has all three
 * viewport assertions at each required width, or a single expectFullViewportAt call.
 * Returns human-readable violation strings (empty = pass).
 */
export function checkRequiredComponentAssertions() {
  const { requiredWidths, components } = JSON.parse(fs.readFileSync(REQUIRED_JSON, 'utf-8'));
  const assertionTypes = ['overflow', 'text_truncation', 'touch_target'];
  const violations = [];

  for (const component of components) {
    const testFile = path.join(COMPONENTS_DIR, `${component}.viewport.test.tsx`);
    if (!fs.existsSync(testFile)) {
      for (const width of requiredWidths) {
        for (const type of assertionTypes) {
          violations.push(`${component} — no viewport test file (needs ${type} at ${width}px)`);
        }
      }
      continue;
    }
    const content = fs.readFileSync(testFile, 'utf-8');
    for (const width of requiredWidths) {
      const hasFullViewport = new RegExp(`expectFullViewportAt\\s*\\(\\s*${width}[^\\d]`).test(content);
      if (hasFullViewport) continue;

      const overflowRegex = new RegExp(`expectNoOverflowAt\\s*\\(\\s*${width}[^\\d]`);
      const truncationRegex = new RegExp(`expectNoTextTruncationAt\\s*\\(\\s*${width}[^\\d]`);
      const touchRegex = new RegExp(`expectMinTouchTargetAt\\s*\\(\\s*${width}[^\\d]`);

      if (!overflowRegex.test(content)) {
        violations.push(`${component} — missing overflow assertion at ${width}px in ${component}.viewport.test.tsx`);
      }
      if (!truncationRegex.test(content)) {
        violations.push(`${component} — missing text_truncation assertion at ${width}px in ${component}.viewport.test.tsx`);
      }
      if (!touchRegex.test(content)) {
        violations.push(`${component} — missing touch_target assertion at ${width}px in ${component}.viewport.test.tsx`);
      }
    }
  }
  return violations;
}

/**
 * Invariant 2: every operator component (.tsx in src/components/) has a
 * *.viewport.test.tsx file or is in the exemption list.
 * Returns component names without viewport tests (empty = pass).
 */
export function checkAllComponentsHaveTests() {
  const files = fs.readdirSync(COMPONENTS_DIR);
  const components = files
    .filter(f => f.endsWith('.tsx') && !f.endsWith('.test.tsx') && !f.endsWith('.viewport.test.tsx'))
    .map(f => f.replace('.tsx', ''))
    .sort();
  const viewportTests = new Set(
    files
      .filter(f => f.endsWith('.viewport.test.tsx'))
      .map(f => f.replace('.viewport.test.tsx', ''))
  );
  return components
    .filter(c => !viewportTests.has(c) && !EXEMPT_FROM_VIEWPORT_TEST.has(c))
    .sort();
}

/**
 * Invariant 3: every non-exempt operator component asserts horizontal overflow
 * at 375px (via expectNoOverflowAt or expectFullViewportAt).
 * Returns human-readable violation strings (empty = pass).
 */
export function check375pxOverflow() {
  const files = fs.readdirSync(COMPONENTS_DIR);
  const components = files
    .filter(f => f.endsWith('.tsx') && !f.endsWith('.test.tsx') && !f.endsWith('.viewport.test.tsx'))
    .map(f => f.replace('.tsx', ''))
    .sort();

  const violations = [];
  for (const component of components) {
    if (EXEMPT_FROM_VIEWPORT_TEST.has(component)) continue;
    const testFile = path.join(COMPONENTS_DIR, `${component}.viewport.test.tsx`);
    if (!fs.existsSync(testFile)) continue; // caught by invariant 2
    const content = fs.readFileSync(testFile, 'utf-8');
    const has375Overflow =
      /expectNoOverflowAt\s*\(\s*375[^\d]|expectFullViewportAt\s*\(\s*375[^\d]/.test(content);
    if (!has375Overflow) {
      violations.push(
        `${component} — missing 375px overflow assertion in ${component}.viewport.test.tsx`
      );
    }
  }
  return violations;
}

/**
 * Invariant 4: the three viewport helper call-name patterns must still appear
 * (lowercased) in backend/app/failure_classification.py (_VIEWPORT_PATTERNS).
 * Returns missing pattern strings (empty = pass).
 */
export function checkBackendPatterns() {
  const backendSrc = fs.readFileSync(BACKEND_PY, 'utf-8').toLowerCase();
  return [
    'expectnooverflowat(',
    'expectnotexttruncationat(',
    'expectmintouchtargetat(',
  ].filter(p => !backendSrc.includes(p));
}

/**
 * Invariant 5: the backend admin_digest.py _ESLINT_VIEWPORT_RE must embed the
 * canonical ESLINT_VIEWPORT_PATTERN so operator-visible counts equal CI counts.
 * Returns human-readable violation strings (empty = pass).
 */
export function checkBackendEslintPattern() {
  const src = fs.readFileSync(VIEWPORT_DEBT_PY, 'utf-8');
  if (!src.includes(ESLINT_VIEWPORT_PATTERN)) {
    return [
      `backend/app/viewport_debt.py _ESLINT_VIEWPORT_RE does not contain` +
      ` the canonical pattern "${ESLINT_VIEWPORT_PATTERN}".` +
      ` Update it to: re.compile(r"${ESLINT_VIEWPORT_PATTERN}")` +
      ` so the Viewport Debt panel shows counts that match the CI gate.`,
    ];
  }
  return [];
}

/**
 * Invariant 6: the Python mirror of EXEMPT_FROM_VIEWPORT_TEST in
 * backend/app/viewport_debt.py (_EXEMPT_FROM_VIEWPORT_TEST) must contain
 * exactly the same component names as the JS canonical set. Drift here
 * silently reintroduces the backend-vs-CI untested_count mismatch that
 * motivated #5752 / #5759.
 * Returns human-readable violation strings (empty = pass).
 */
export function checkBackendExemptionMirror() {
  const src = fs.readFileSync(VIEWPORT_DEBT_PY, 'utf-8');
  // Capture the frozenset literal body so we only parse what's intended as
  // the exemption list (not random "ErrorBoundary" strings in comments).
  const m = src.match(/_EXEMPT_FROM_VIEWPORT_TEST[^=]*=\s*frozenset\(\s*\[([\s\S]*?)\]\s*\)/);
  if (!m) {
    return [
      `backend/app/viewport_debt.py: could not locate _EXEMPT_FROM_VIEWPORT_TEST frozenset literal.` +
      ` Expected: _EXEMPT_FROM_VIEWPORT_TEST: frozenset[str] = frozenset([...]).` +
      ` Keep its members in sync with EXEMPT_FROM_VIEWPORT_TEST in viewport-checks.js.`,
    ];
  }
  const pyMembers = new Set(
    Array.from(m[1].matchAll(/["']([A-Za-z_][A-Za-z0-9_]*)["']/g)).map(x => x[1])
  );
  const jsMembers = EXEMPT_FROM_VIEWPORT_TEST;

  const violations = [];
  for (const name of jsMembers) {
    if (!pyMembers.has(name)) {
      violations.push(
        `"${name}" is in EXEMPT_FROM_VIEWPORT_TEST (viewport-checks.js) but missing from` +
        ` _EXEMPT_FROM_VIEWPORT_TEST in backend/app/viewport_debt.py — add it there so the` +
        ` Viewport Debt panel untested_count matches CI.`
      );
    }
  }
  for (const name of pyMembers) {
    if (!jsMembers.has(name)) {
      violations.push(
        `"${name}" is in _EXEMPT_FROM_VIEWPORT_TEST (backend/app/viewport_debt.py) but missing` +
        ` from EXEMPT_FROM_VIEWPORT_TEST in admin-ui/src/test/viewport-checks.js — add it there` +
        ` so CI exempts the same components the backend panel does.`
      );
    }
  }
  return violations;
}

/**
 * Invariant 7: per-component and total viewport escape-hatch debt stays within
 * the limits in viewport-escape-hatch-budget.json.
 * Returns human-readable violation strings (empty = pass).
 */
export function checkEscapeHatchBudget() {
  const { total: totalBudget, perComponent } = JSON.parse(fs.readFileSync(BUDGET_JSON, 'utf-8'));
  const files = fs.readdirSync(COMPONENTS_DIR).filter(
    f => f.endsWith('.tsx') && !f.endsWith('.test.tsx') && !f.endsWith('.viewport.test.tsx')
  );
  // Matches all three escape-hatch data attributes and eslint-disable viewport/* comments.
  // Uses ESLINT_VIEWPORT_PATTERN (canonical) so counts match what admin_digest.py reports.
  const escapeHatchPattern = new RegExp(
    `data-viewport-allow-overflow|data-viewport-allow-truncation|data-viewport-allow-small-target|${ESLINT_VIEWPORT_PATTERN}`,
    'g'
  );

  const violations = [];
  let grandTotal = 0;

  for (const file of files) {
    const component = file.replace('.tsx', '');
    const content = fs.readFileSync(path.join(COMPONENTS_DIR, file), 'utf-8');
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

  return violations;
}
