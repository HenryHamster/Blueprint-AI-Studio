#!/usr/bin/env node
/**
 * Shrink-only baseline gate for admin-ui test-file type errors.
 *
 * Usage:
 *   node scripts/typecheck-tests-baseline.mjs          # check mode (default)
 *   node scripts/typecheck-tests-baseline.mjs --update # regenerate baseline
 *
 * Exits 0 when no errors exceed the baseline; non-zero on any new or increased error.
 * Decreasing or removing errors is always allowed (shrink-only).
 *
 * Baseline format: { "errors": { "<file>": { "<TScode: message>": <count> } } }
 * Keyed by (file, error-code + message), NOT line/col, so inserting lines
 * elsewhere does not churn the baseline.
 */

import { execSync } from 'node:child_process';
import { readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(__dirname, '..');
const BASELINE_PATH = join(ROOT, 'tsconfig.test.baseline.json');
const TSC_CONFIG = join(ROOT, 'tsconfig.test.json');

const UPDATE_MODE = process.argv.includes('--update');

/** Run tsc and return raw output (stderr + stdout merged). */
function runTsc() {
  const tsc = join(ROOT, 'node_modules/.bin/tsc');
  try {
    execSync(`"${tsc}" -p "${TSC_CONFIG}" --noEmit`, {
      cwd: ROOT,
      stdio: ['ignore', 'pipe', 'pipe'],
      encoding: 'utf8',
    });
    return '';
  } catch (err) {
    // tsc exits non-zero when there are errors; that's expected.
    return (err.stdout ?? '') + (err.stderr ?? '');
  }
}

/** Parse tsc output into { file -> { "TScode: message" -> count } }. */
function parseErrors(output) {
  // Line format: src/path/to/file.tsx(10,5): error TS2322: message text
  const re = /^(src\/[^(]+)\(\d+,\d+\): error (TS\d+: .+)$/gm;
  const result = {};
  let m;
  while ((m = re.exec(output)) !== null) {
    const [, file, key] = m;
    if (!result[file]) result[file] = {};
    result[file][key] = (result[file][key] ?? 0) + 1;
  }
  return result;
}

/** Load baseline from disk. Returns empty errors on missing file. */
function loadBaseline() {
  try {
    return JSON.parse(readFileSync(BASELINE_PATH, 'utf8'));
  } catch {
    return { errors: {} };
  }
}

/** Write baseline to disk. */
function saveBaseline(errors) {
  const data = { errors };
  writeFileSync(BASELINE_PATH, JSON.stringify(data, null, 2) + '\n', 'utf8');
}

/** Compare current errors against baseline. Returns array of violation strings. */
function findViolations(current, baseline) {
  const violations = [];
  for (const [file, keys] of Object.entries(current)) {
    const baselineFile = baseline[file] ?? {};
    for (const [key, count] of Object.entries(keys)) {
      const baselineCount = baselineFile[key] ?? 0;
      if (count > baselineCount) {
        violations.push(
          `  ${file}\n    ${key}\n    count: ${count} (baseline: ${baselineCount})`
        );
      }
    }
  }
  return violations;
}

// --- Main ---

const output = runTsc();
const current = parseErrors(output);

if (UPDATE_MODE) {
  saveBaseline(current);
  const totalErrors = Object.values(current).reduce(
    (sum, keys) => sum + Object.values(keys).reduce((s, c) => s + c, 0),
    0
  );
  const fileCount = Object.keys(current).length;
  console.log(`Baseline updated: ${totalErrors} errors across ${fileCount} files → ${BASELINE_PATH}`);
  process.exit(0);
}

// Check mode
const { errors: baseline } = loadBaseline();
const violations = findViolations(current, baseline);

if (violations.length > 0) {
  console.error('');
  console.error('typecheck-tests: NEW or INCREASED type errors detected in test files.');
  console.error('These errors are not covered by the shrink-only baseline.');
  console.error('Fix the errors, or run `node scripts/typecheck-tests-baseline.mjs --update`');
  console.error('to update the baseline if this is intentional growth.');
  console.error('');
  console.error(`Violations (${violations.length}):`);
  for (const v of violations) {
    console.error(v);
  }
  console.error('');
  process.exit(1);
}

// Count how many errors are still baselined (informational)
const totalErrors = Object.values(current).reduce(
  (sum, keys) => sum + Object.values(keys).reduce((s, c) => s + c, 0),
  0
);
if (totalErrors > 0) {
  const fileCount = Object.keys(current).length;
  console.log(
    `typecheck-tests: ${totalErrors} pre-existing baselined errors in ${fileCount} files — no new errors. ✓`
  );
} else {
  console.log('typecheck-tests: no errors in test files. ✓');
}
process.exit(0);
