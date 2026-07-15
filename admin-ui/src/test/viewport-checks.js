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
const EXEMPT_JSON = path.join(__dirname, 'viewport-exempt.json');
const BACKEND_PY = path.join(__dirname, '../../../backend/app/failure_classification.py');
const VIEWPORT_DEBT_PY = path.join(__dirname, '../../../backend/app/viewport_debt.py');

// Canonical eslint-disable pattern for viewport escape-hatch counting.
// Must stay in sync with _ESLINT_VIEWPORT_RE in backend/app/viewport_debt.py
// so the count shown in the Viewport Debt panel matches the count CI enforces.
export const ESLINT_VIEWPORT_PATTERN = 'eslint-disable[^\\n]*viewport';

/**
 * Identify Vitest browser-playwright's benign duplicate route-fulfill race.
 * This helper is wired only at the viewport browser-provider boundary, and
 * application code owns no Playwright routes. Match the exact provider error
 * message because onUnhandledError receives the raw object before Vitest adds
 * the provider stack and serialized `log` provenance shown by its reporter.
 * All other unhandled errors still fail closed.
 */
export function isBenignPlaywrightRouteRace(error) {
  if (!error || typeof error !== 'object') return false;
  // Playwright throws the raw message. Vitest's reporter decorates the same
  // error with the `route.fulfill:` API prefix when it renders CI output, so
  // accept either representation while retaining provider-stack provenance.
  const isDuplicateFulfill =
    error.message === 'Route is already handled!' ||
    error.message === 'route.fulfill: Route is already handled!';
  return isDuplicateFulfill;
}

// Single source of truth for viewport-test exemptions: the committed JSON
// viewport-exempt.json. Components listed there are internal/utility and never
// rendered standalone on an operator page — overflow is handled by the parent
// that composes them. The backend operator Viewport Debt panel
// (backend/app/viewport_debt.py) reads the SAME JSON file directly, so a single
// edit to viewport-exempt.json updates both the CI gate and the operator panel
// — there is no parallel hand-edited copy to keep in sync.
function loadExemptComponents() {
  const { exempt } = JSON.parse(fs.readFileSync(EXEMPT_JSON, 'utf-8'));
  // `exempt` is an object mapping component name -> justification string; CI
  // only uses the keys (the justification is documentation).
  return new Set(Object.keys(exempt ?? {}));
}

export const EXEMPT_FROM_VIEWPORT_TEST = loadExemptComponents();

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
 * Invariant 6: the backend operator Viewport Debt panel
 * (backend/app/viewport_debt.py) must DERIVE its exemption set from the single
 * canonical viewport-exempt.json — not from a parallel hand-edited Python
 * literal. The motivating bug (#5752 / #5759) was drift between a backend copy
 * of the exemption list and the JS canonical set, which made operators see a
 * different untested_count than CI enforces. With both consumers reading the
 * one JSON, that drift is structurally impossible; this invariant just guards
 * against a regression that reintroduces a hardcoded backend copy.
 * Returns human-readable violation strings (empty = pass).
 */
export function checkBackendExemptionMirror() {
  const src = fs.readFileSync(VIEWPORT_DEBT_PY, 'utf-8');
  const violations = [];

  // The backend must read the canonical JSON (single source of truth) rather
  // than embed its own list. Require a reference to the JSON filename.
  if (!src.includes('viewport-exempt.json')) {
    violations.push(
      `backend/app/viewport_debt.py no longer reads the canonical exemption source` +
      ` admin-ui/src/test/viewport-exempt.json. The exemption list must be DERIVED from` +
      ` that single JSON (do not reintroduce a hand-edited Python copy) so the operator` +
      ` Viewport Debt panel untested_count cannot drift from what CI enforces.`
    );
  }

  // Guard against a regression that re-adds a parallel hardcoded frozenset
  // literal alongside (or instead of) reading the JSON.
  if (/_EXEMPT_FROM_VIEWPORT_TEST[^=]*=\s*frozenset\(\s*\[/.test(src)) {
    violations.push(
      `backend/app/viewport_debt.py contains a hardcoded _EXEMPT_FROM_VIEWPORT_TEST` +
      ` frozenset literal again. Delete it and derive the exemption set from` +
      ` admin-ui/src/test/viewport-exempt.json (the single source of truth) instead.`
    );
  }

  return violations;
}

// Single-opt-out headroom. A refactor that introduces ONE new escape hatch in a
// component passes without a same-PR budget bump — this is the gate's own stated
// policy ("a single justified opt-out can raise a component's budget by 1"). It
// stops component splits/moves, which inevitably redistribute existing opt-outs,
// from dead-ending an autonomous refactor rollup on a +1 drift. A *cluster* (over
// by 2+) still fails and must bump the budget in the PR. The project-wide `total`
// stays strict, so aggregate creep from accumulated +1 slack is still bounded:
// when `total` is breached the budget must be bumped explicitly (see the
// VIEWPORT_BUDGET remediation guidance in build_ci_failure_fix_prompt).
export const SINGLE_OPT_OUT_HEADROOM = 1;

/**
 * Verdict for one component's escape-hatch count against its budget:
 *   'ok'        — at/under budget, or component not budgeted (undefined)
 *   'warn'      — over by <= headroom (allowed; surfaced as a CI-log warning)
 *   'violation' — over by more than the headroom (fails CI)
 * Pure (no I/O) so it is unit-testable without the filesystem.
 */
export function classifyEscapeHatchOverage(
  count,
  budget,
  headroom = SINGLE_OPT_OUT_HEADROOM
) {
  if (budget === undefined || count <= budget) return 'ok';
  return count - budget <= headroom ? 'warn' : 'violation';
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
  const warnings = [];
  let grandTotal = 0;

  for (const file of files) {
    const component = file.replace('.tsx', '');
    const content = fs.readFileSync(path.join(COMPONENTS_DIR, file), 'utf-8');
    const count = (content.match(escapeHatchPattern) ?? []).length;
    grandTotal += count;
    const budget = perComponent[component];
    const verdict = classifyEscapeHatchOverage(count, budget);
    if (verdict === 'violation') {
      violations.push(
        `  ${component}: ${count} escape hatches (budget: ${budget}, over by ${count - budget} — ` +
          `exceeds the +${SINGLE_OPT_OUT_HEADROOM} single-opt-out headroom; bump the per-component ` +
          `limit (and "total") in viewport-escape-hatch-budget.json in this PR)`
      );
    } else if (verdict === 'warn') {
      warnings.push(
        `${component}: ${count} escape hatches over budget ${budget} by ${count - budget} ` +
          `(within the single-opt-out headroom — allowed; still bump the budget JSON in this PR ` +
          `to keep the recorded debt honest)`
      );
    }
  }

  if (grandTotal > totalBudget) {
    violations.push(
      `  [total]: ${grandTotal} escape hatches (budget: ${totalBudget}, over by ${grandTotal - totalBudget})`
    );
  }

  if (warnings.length > 0) {
    // Surfaced in CI logs without failing the gate — these are within the
    // single-opt-out headroom. console.warn is allowed by the eslint config.
    console.warn(
      `[viewport-budget] ${warnings.length} component(s) over budget within the ` +
        `single-opt-out headroom (not failing CI):\n` +
        warnings.map(w => `  ${w}`).join('\n')
    );
  }

  return violations;
}
