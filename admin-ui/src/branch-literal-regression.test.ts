import { describe, it, expect } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

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

function listProductionSourceFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = path.join(dir, entry);
    const st = statSync(full);
    if (st.isDirectory()) {
      out.push(...listProductionSourceFiles(full));
    } else if (
      /\.(ts|tsx)$/.test(entry) &&
      !/\.test\.(ts|tsx)$/.test(entry) &&
      !/\.spec\.(ts|tsx)$/.test(entry) &&
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

// Branch-decision patterns only — equality checks, assignments to a branch-y
// variable, object-literal defaults keyed by such a name, and `||`/`??`
// fallback operators that short-circuit to the hardcoded value.
const PATTERNS: Pattern[] = [
  { name: 'equality-main',      re: /(?:===|!==)\s*['"](?:main|automain)['"]/ },
  { name: 'assign-branch',      re: /\b(?:branch|baseBranch|baseRef|base_branch|integrationBranch|integration_branch|promotionTarget|promotion_target)\s*=(?!=)\s*['"](?:main|automain)['"]/ },
  { name: 'object-baseBranch',  re: /\b(?:branch|baseBranch|base_branch|baseRef|integrationBranch|integration_branch|promotionTarget|promotion_target)\s*:\s*['"](?:main|automain)['"]/ },
  { name: 'fallback-main',      re: /(?:\|\||\?\?)\s*['"](?:main|automain)['"]/ },
  { name: 'fallback-blueprint', re: /(?:\|\||\?\?)\s*['"]blueprint['"]/ },
];

interface AllowEntry {
  path: string;   // relative to admin-ui/src
  line: number;
  pattern: string;
  rationale: string;
}

// Narrow, justified exemptions. UI display fallbacks that already prefer
// the resolved per-project value and only use the literal as the
// placeholder/visible-default are allowed; branch-decision logic is not.
const ALLOWLIST: AllowEntry[] = [
  {
    path: 'components/NewTaskModal.tsx',
    line: 216,
    pattern: 'fallback-main',
    rationale:
      "<input placeholder={integrationBranch || 'main'}> — the resolved " +
      "per-project integrationBranch is preferred; 'main' is only shown " +
      "as the input placeholder when integrationBranch is not yet known.",
  },
  {
    path: 'components/ProjectBuildConfigSection.tsx',
    line: 612,
    pattern: 'fallback-blueprint',
    rationale:
      "Display-only fallback inside a <code> sample of the task-branch " +
      "namespace; the user-typed taskBranchNamespace is preferred, and " +
      "'blueprint' is only shown as the historical default in the example.",
  },
];

interface Offender {
  path: string;
  line: number;
  pattern: string;
  snippet: string;
}

describe('hardcoded branch-literal regression guard (admin-ui)', () => {
  const files = listProductionSourceFiles(SRC_DIR);

  it('scans a meaningful number of production files (no-op guard)', () => {
    // Defensive guard — if the walker / regex set ever silently turns
    // into a no-op, this fails immediately. Mirrors prompt-helper-refs
    // .test.ts:53 in the agent layer.
    expect(files.length).toBeGreaterThan(5);
  });

  it('contains no un-allowlisted branch-decision literals', () => {
    const offenders: Offender[] = [];

    for (const file of files) {
      // Normalize to forward slashes so the allowKey (and the printed
      // offender path) match the ALLOWLIST entries on every platform —
      // path.relative yields backslashes on Windows, which otherwise made
      // every allowlisted entry look un-allowlisted in local dev.
      const rel = path.relative(SRC_DIR, file).split(path.sep).join('/');
      const raw = readFileSync(file, 'utf-8');
      const rawLines = raw.split('\n');

      // Per-line strip of /* … */ blocks (incl. JSDoc) and `//` line
      // comments, preserving line numbers so allowlist entries are stable.
      let inBlockComment = false;
      for (let i = 0; i < rawLines.length; i++) {
        let line = rawLines[i];
        if (inBlockComment) {
          const endIdx = line.indexOf('*/');
          if (endIdx < 0) continue;
          line = line.slice(endIdx + 2);
          inBlockComment = false;
        }
        line = line.replace(/\/\*[^]*?\*\//g, '');
        const openIdx = line.indexOf('/*');
        if (openIdx >= 0) {
          line = line.slice(0, openIdx);
          inBlockComment = true;
        }
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

    const allowKey = (a: { path: string; line: number; pattern: string }) =>
      `${a.path}::${a.line}::${a.pattern}`;
    const allowSet = new Set(ALLOWLIST.map(allowKey));
    const real = offenders.filter(o => !allowSet.has(allowKey(o)));

    if (real.length > 0) {
      const lines = real
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
