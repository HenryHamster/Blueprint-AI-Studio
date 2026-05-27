import { describe, it, expect } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

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

function listProductionTsFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = path.join(dir, entry);
    const st = statSync(full);
    if (st.isDirectory()) {
      out.push(...listProductionTsFiles(full));
    } else if (
      entry.endsWith('.ts') &&
      !entry.endsWith('.test.ts') &&
      !entry.endsWith('.d.ts')
    ) {
      out.push(full);
    }
  }
  return out;
}

interface Pattern {
  name: string;
  re: RegExp;
}

// Each regex targets a BRANCH-DECISION use of a branch literal — equality
// checks, assignments to a `branch`/`baseBranch` variable, object-literal
// defaults keyed by such a name, or `||` / `??` fallback operators that
// short-circuit to the hardcoded value.
const PATTERNS: Pattern[] = [
  // === 'main' / !== 'main' / === 'automain' etc.
  { name: 'equality-main',     re: /(?:===|!==)\s*['"](?:main|automain)['"]/ },
  // branch = 'main' / baseBranch = 'main' (assignment — NOT == / === / !=)
  { name: 'assign-branch',     re: /\b(?:branch|baseBranch|baseRef|base_branch)\s*=(?!=)\s*['"](?:main|automain)['"]/ },
  // { baseBranch: 'main' } object-literal default
  { name: 'object-baseBranch', re: /\b(?:branch|baseBranch|base_branch|baseRef)\s*:\s*['"](?:main|automain)['"]/ },
  // foo || 'main' / foo ?? 'main' fallback operator
  { name: 'fallback-main',     re: /(?:\|\||\?\?)\s*['"](?:main|automain)['"]/ },
  // foo || 'blueprint' / foo ?? 'blueprint' (branch namespace fallback)
  { name: 'fallback-blueprint', re: /(?:\|\||\?\?)\s*['"]blueprint['"]/ },
];

// Allowlist of legitimate, non-regression occurrences in the current tree.
// Each entry: relative path + 1-based line number + matched pattern name.
// Keep this list narrow — one entry per offender, with a rationale comment
// — so future regressions surface immediately rather than being absorbed.
interface AllowEntry {
  path: string;   // relative to agent/src
  line: number;
  pattern: string;
  rationale: string;
}

const ALLOWLIST: AllowEntry[] = [
  {
    // The CANONICAL resolver. resolveBaseBranch() is the one place where a
    // hardcoded 'main' default is the documented behavior: payload →
    // project_metadata.integration_branch → fallback. Every other agent
    // call site must route through this helper.
    path: 'handlers/review-fix.ts',
    line: 340,
    pattern: 'fallback-main',
    rationale:
      "buildRollupReviewGuidance is downstream of resolveBaseBranch; the " +
      "`options.baseBranch || 'main'` is a defensive last-resort default " +
      "for a UI display string, not a branch-decision regression.",
  },
  {
    path: 'handlers/review-fix.ts',
    line: 781,
    pattern: 'fallback-main',
    rationale:
      "Local-variable defensive default downstream of resolveBaseBranch; " +
      "callers always supply a resolved baseBranch in the live code path.",
  },
];

interface Offender {
  path: string;
  line: number;
  pattern: string;
  snippet: string;
}

describe('hardcoded branch-literal regression guard (agent)', () => {
  const files = listProductionTsFiles(SRC_DIR);

  it('scans a meaningful number of production files (no-op guard)', () => {
    // Mirrors the defensive `expect(found.size).toBeGreaterThan(3)` idiom
    // in prompt-helper-refs.test.ts:53 — if the file walker / regex set
    // ever silently turns into a no-op, this fails immediately.
    expect(files.length).toBeGreaterThan(5);
  });

  it('contains no un-allowlisted branch-decision literals', () => {
    const offenders: Offender[] = [];

    for (const file of files) {
      const rel = path.relative(SRC_DIR, file);
      const raw = readFileSync(file, 'utf-8');
      const rawLines = raw.split('\n');

      // Per-line stripping that preserves line numbers exactly. Strips both
      // /* … */ block comments (including JSDoc `/** … */`) and `//` line
      // comments so doc-prose like `(or === 'main' when …)` cannot trip the
      // regex set below.
      let inBlockComment = false;
      for (let i = 0; i < rawLines.length; i++) {
        let line = rawLines[i];
        if (inBlockComment) {
          const endIdx = line.indexOf('*/');
          if (endIdx < 0) continue;
          line = line.slice(endIdx + 2);
          inBlockComment = false;
        }
        // Strip same-line /* … */ pairs.
        line = line.replace(/\/\*[^]*?\*\//g, '');
        // Detect dangling block-comment opener.
        const openIdx = line.indexOf('/*');
        if (openIdx >= 0) {
          line = line.slice(0, openIdx);
          inBlockComment = true;
        }
        // Strip line comments.
        const lineCommentIdx = line.indexOf('//');
        if (lineCommentIdx >= 0) line = line.slice(0, lineCommentIdx);

        if (!line.trim()) continue;

        for (const pat of PATTERNS) {
          if (pat.re.test(line)) {
            offenders.push({
              path: rel,
              line: i + 1,
              pattern: pat.name,
              snippet: line.trim().slice(0, 160),
            });
          }
        }
      }
    }

    // Remove allowlisted offenders.
    const allowKey = (a: { path: string; line: number; pattern: string }) =>
      `${a.path}::${a.line}::${a.pattern}`;
    const allowSet = new Set(ALLOWLIST.map(allowKey));
    const real = offenders.filter(o => !allowSet.has(allowKey(o)));

    if (real.length > 0) {
      const lines = real
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
    const stale: string[] = [];
    for (const entry of ALLOWLIST) {
      const abs = path.join(SRC_DIR, entry.path);
      const lines = readFileSync(abs, 'utf-8').split('\n');
      const line = lines[entry.line - 1] ?? '';
      const pat = PATTERNS.find(p => p.name === entry.pattern);
      if (!pat || !pat.re.test(line)) {
        stale.push(
          `  ${entry.path}:${entry.line} expected pattern [${entry.pattern}], ` +
            `found: ${line.trim().slice(0, 120)}`,
        );
      }
    }
    if (stale.length > 0) {
      throw new Error(
        'Stale ALLOWLIST entries (recorded line no longer matches recorded pattern):\n' +
          stale.join('\n') +
          '\nUpdate or remove the stale entries.',
      );
    }
  });
});
