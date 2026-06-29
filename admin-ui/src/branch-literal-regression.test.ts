import { describe, it, expect } from 'vitest';
import path from 'path';
import { fileURLToPath } from 'url';
import { spawnSync } from 'child_process';

// Regression guard: production admin-UI code must NOT hardcode branch
// decisions against the literal 'main', 'automain', or a 'blueprint'
// branch namespace. Per-project branch values flow in from the backend
// (project.integration_branch / promotion_target / task_branch_namespace)
// and must be honored — hardcoded literals mislabel non-Blueprint repos
// in the UI.
//
// Sister of agent/src/branch-literal-regression.test.ts. Mirrors the
// allow-list discipline used in backend/testing/test_invariants.py
// (TestBaseBranchFallback / TestStagingBranchClassification).

const SRC_DIR = path.dirname(fileURLToPath(import.meta.url));
const SCANNER = path.join(SRC_DIR, '..', '..', 'backend', 'app', 'branch_literal_compliance.py');

interface Offender {
  surface: string;
  path: string;
  line: number;
  pattern: string;
  snippet: string;
}

interface ScanResult {
  surface: string;
  files_scanned: number;
  offenders: Offender[];
  stale_allowlist: Array<{ path: string; line: number; pattern: string; found: string }>;
}

function runScanner(): ScanResult {
  const result = spawnSync('python3', [SCANNER, '--surface', 'admin-ui', '--root', SRC_DIR, '--json'], {
    encoding: 'utf-8',
  });
  if (result.status !== 0) {
    throw new Error(`branch literal scanner failed:\n${result.stderr}\n${result.stdout}`);
  }
  const parsed = JSON.parse(result.stdout) as unknown;
  if (!Array.isArray(parsed) || parsed.length !== 1) {
    throw new Error(`branch literal scanner returned unexpected JSON:\n${result.stdout}`);
  }
  return parsed[0] as ScanResult;
}

describe('hardcoded branch-literal regression guard (admin-ui)', () => {
  const scan = runScanner();

  it('scans a meaningful number of production files (no-op guard)', () => {
    // Defensive guard — if the walker / regex set ever silently turns
    // into a no-op, this fails immediately. Mirrors prompt-helper-refs
    // .test.ts:53 in the agent layer.
    expect(scan.files_scanned).toBeGreaterThan(5);
  });

  it('contains no un-allowlisted branch-decision literals', () => {
    if (scan.offenders.length > 0) {
      const lines = scan.offenders
        .map(o => `  ${o.path}:${o.line} [${o.pattern}]: ${o.snippet}`)
        .join('\n');
      throw new Error(
        'Found hardcoded branch-literal(s) in admin-ui/src/ production code.\n' +
          'Branch values must honor the per-project resolved value\n' +
          '(project.integration_branch / promotion_target / task_branch_namespace)\n' +
          'fetched from the backend — hardcoded literals mis-target non-Blueprint repos.\n' +
          'Offenders:\n' +
          lines +
          '\n\nIf this is a legitimate placeholder/display fallback that already prefers\n' +
          'the resolved value, add a narrow entry to ALLOWLIST in this file with a\n' +
          'one-line rationale.',
      );
    }
  });

  it('every ALLOWLIST entry still matches its recorded line (no rot)', () => {
    const stale = scan.stale_allowlist.map(
      entry =>
        `  ${entry.path}:${entry.line} expected pattern [${entry.pattern}], ` +
        `found: ${entry.found}`,
    );
    if (stale.length > 0) {
      throw new Error(
        'Stale ALLOWLIST entries (recorded line no longer matches recorded pattern):\n' +
          stale.join('\n') +
          '\nUpdate or remove the stale entries.',
      );
    }
  });
});
