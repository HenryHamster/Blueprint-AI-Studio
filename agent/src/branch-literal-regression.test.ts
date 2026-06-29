import { describe, it, expect } from 'vitest';
import path from 'path';
import { fileURLToPath } from 'url';
import { spawnSync } from 'child_process';

// Regression guard: production agent code must NOT hardcode branch decisions
// against the literal 'main', 'automain', or a hardcoded 'blueprint' branch
// namespace. Branch resolution must flow through resolveBaseBranch() (in
// agent/src/index.ts) which honors task.project_metadata.integration_branch
// per-project — hardcoded literals silently mis-target non-Blueprint repos
// whose integration_branch is e.g. 'master' or 'develop'.
//
// Mirrors backend/testing/test_invariants.py TestBaseBranchFallback +
// TestStagingBranchClassification, and the source-walking pattern in
// agent/src/prompt-helper-refs.test.ts.

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
  const result = spawnSync('python3', [SCANNER, '--surface', 'agent', '--root', SRC_DIR, '--json'], {
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

describe('hardcoded branch-literal regression guard (agent)', () => {
  const scan = runScanner();

  it('scans a meaningful number of production files (no-op guard)', () => {
    // Mirrors the defensive `expect(found.size).toBeGreaterThan(3)` idiom
    // in prompt-helper-refs.test.ts:53 — if the file walker / regex set
    // ever silently turns into a no-op, this fails immediately.
    expect(scan.files_scanned).toBeGreaterThan(5);
  });

  it('contains no un-allowlisted branch-decision literals', () => {
    if (scan.offenders.length > 0) {
      const lines = scan.offenders
        .map(o => `  ${o.path}:${o.line} [${o.pattern}]: ${o.snippet}`)
        .join('\n');
      throw new Error(
        'Found hardcoded branch-literal(s) in agent/src/ production code.\n' +
          'Branch resolution MUST route through resolveBaseBranch() in\n' +
          'agent/src/index.ts (which consults task.project_metadata.integration_branch)\n' +
          'so non-Blueprint projects with integration_branch != "main" work correctly.\n' +
          'Offenders:\n' +
          lines +
          '\n\nIf this is a legitimate display/UI fallback downstream of resolveBaseBranch,\n' +
          'add a narrow entry to ALLOWLIST in this file with a one-line rationale.',
      );
    }
  });

  it('every ALLOWLIST entry still matches its recorded line (no rot)', () => {
    // Guards against allowlist rot: if production code is refactored and
    // the allowlisted literal moves or disappears, this test surfaces the
    // stale entry so it gets cleaned up — rather than silently exempting
    // a different (potentially real) offender from the scan.
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
