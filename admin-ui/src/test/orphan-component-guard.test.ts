import { describe, it, expect } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

// Reachability guard: every admin-ui/src/components/*.tsx file must be
// transitively imported from main.tsx (the app entry point) OR appear in
// ORPHAN_ALLOWLIST. Guards against dead components accumulating silently
// because each carries its own test files that keep build/lint/vitest green.
//
// Mirrors the allowlist discipline in branch-literal-regression.test.ts.

const SRC_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const COMPONENTS_DIR = path.join(SRC_DIR, 'components');

// Components explicitly allowed to be unreachable from the entry point.
// Add entries here with a one-line rationale when a file serves as a
// library export or is composed exclusively by an external consumer.
const ORPHAN_ALLOWLIST = new Set<string>([
  // Legacy metrics shell retained while its useful diagnostics are migrated
  // into the intentional Runtime/Project deep-monitoring surfaces.
  'MetricsDashboard.tsx',
  'LaneDiagPanel.tsx',
  // New fleet-health dashboard card — wired into the operator dashboard by a
  // follow-up consumer (DashboardHome or FleetDetailDrawer). Allowlisted here
  // so the API endpoint and component can land together without blocking on
  // the dashboard wiring, which requires a parallel useProjectMetrics hook
  // update.
  'FleetHealthCard.tsx',
  // Whiteboard tool — reachable from main.tsx via import './appTools'
  // (side-effect import) then registerToolKind, but the import-graph walker's
  // regex only tracks `from` and `import()` patterns, not bare side-effect
  // imports. The component is not dead; it is registered dynamically.
  'WhiteboardTool.tsx',
]);

// ---------------------------------------------------------------------------
// Import-graph walker
// ---------------------------------------------------------------------------

function extractImports(file: string): string[] {
  let src: string;
  try { src = readFileSync(file, 'utf-8'); } catch { return []; }
  const out: string[] = [];
  // Matches: import/export ... from '...' and dynamic import('...')
  const re = /(?:(?:import|export)\b[^'"]*?\bfrom\s+|import\s*\()['"]([^'"]+)['"]/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(src)) !== null) out.push(m[1]);
  return out;
}

const EXT_CANDIDATES = ['', '.ts', '.tsx', '/index.ts', '/index.tsx'];

function resolveRelative(from: string, imp: string): string | null {
  if (!imp.startsWith('.')) return null; // skip node_modules
  const base = path.resolve(path.dirname(from), imp);
  for (const ext of EXT_CANDIDATES) {
    const candidate = base + ext;
    try {
      const stat = statSync(candidate);
      if (stat.isFile()) return candidate;
    } catch { /* skip */ }
  }
  return null;
}

function buildReachableSet(entry: string): Set<string> {
  const visited = new Set<string>();
  const queue: string[] = [entry];
  while (queue.length) {
    const cur = queue.pop()!;
    if (visited.has(cur)) continue;
    visited.add(cur);
    for (const imp of extractImports(cur)) {
      const resolved = resolveRelative(cur, imp);
      if (resolved && !visited.has(resolved)) queue.push(resolved);
    }
  }
  return visited;
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('orphaned-component reachability guard (admin-ui)', () => {
  const entry = path.join(SRC_DIR, 'main.tsx');
  const reachable = buildReachableSet(entry);

  it('import walker visits a meaningful number of source files (no-op guard)', () => {
    // Fails immediately if the walker or entry path is silently broken.
    expect(reachable.size).toBeGreaterThan(10);
  });

  it('every components/*.tsx file is reachable from main.tsx or explicitly allowlisted', () => {
    const allComponents = readdirSync(COMPONENTS_DIR).filter(
      f =>
        f.endsWith('.tsx') &&
        !f.endsWith('.test.tsx') &&
        !f.endsWith('.viewport.test.tsx'),
    );

    // Defensive guard: directory walk found at least some components.
    expect(allComponents.length).toBeGreaterThan(5);

    const orphans = allComponents.filter(f => {
      const abs = path.join(COMPONENTS_DIR, f);
      return !reachable.has(abs) && !ORPHAN_ALLOWLIST.has(f);
    });

    if (orphans.length > 0) {
      const lines = orphans.map(f => `  admin-ui/src/components/${f}`).join('\n');
      throw new Error(
        `Unreachable component(s) not in ORPHAN_ALLOWLIST:\n${lines}\n\n` +
          'Options:\n' +
          '  1. Import the component from App.tsx or another reachable file.\n' +
          '  2. Delete the file if it is genuinely dead code.\n' +
          '  3. Add the component filename (e.g. "FooPanel.tsx") to ORPHAN_ALLOWLIST\n' +
          '     in admin-ui/src/test/orphan-component-guard.test.ts with a rationale.',
      );
    }

    expect(orphans).toEqual([]);
  });
});
