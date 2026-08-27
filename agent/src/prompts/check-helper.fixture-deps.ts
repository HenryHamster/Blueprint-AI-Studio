/**
 * Which production files a stripped `check` fixture checkout has to carry.
 *
 * `check-helper.test.ts` stages an isolated tree and runs the REAL
 * `backend/testing/check_size_limits.py` inside it, so that script's own
 * imports have to travel with it or the guard dies on an import error the
 * moment it is invoked.
 */

/** Minimal `node:fs` surface this module needs, so callers can pass the
 *  un-mocked module the test resolves via `vi.importActual`. */
export interface FixtureFs {
  existsSync(path: string): boolean;
  readFileSync(path: string, encoding: 'utf8'): string;
}

/** Minimal `node:path` surface, same rationale as {@link FixtureFs}. */
export interface FixturePath {
  join(...segments: string[]): string;
}

/**
 * Shared guard sources every scenario needs, independent of imports.
 *
 * Data files and cross-package dependencies (`app.size_limit_policy`, the
 * `scripts/` receipt writers) are listed by hand because no import statement
 * in `backend/testing` names them as a `testing.*` module.
 */
export const FIXTURE_SHARED_FILES: readonly string[] = [
  'scripts/backend_verification_guard.py', 'scripts/verification_receipt.py',
  'scripts/guard_suppression_receipt.py', 'scripts/static_release_guard.py',
  'backend/testing/fixgate_attribution.py', 'backend/testing/impacted_tests.py',
  'backend/testing/drift_sentinel_surfaces.json', 'backend/testing/check_size_limits.py',
  'backend/testing/module_size_limits.json', 'backend/testing/cap_bump_history.json',
  'backend/app/__init__.py', 'backend/app/size_limit_policy.py',
  'backend/app/size_limit_justification.py',
];

/** `from testing.<mod> import ...` / `import testing.<mod>`, at statement
 *  position so a mention inside a docstring or comment does not match. */
const TESTING_IMPORT = /^[ \t]*(?:from|import)[ \t]+testing\.([A-Za-z_][A-Za-z0-9_]*)/gm;

/**
 * Every `backend/testing` module reachable by intra-package import from
 * `entries`, excluding the entries themselves.
 *
 * Listing these by hand rots silently in one direction only. #17910 added
 * `from testing.repo_scripts_path import resolve_scripts_dir_from_env` to
 * `check_size_limits.py` without touching the fixture's file list, and the
 * agent lane went red on CI with `ModuleNotFoundError: No module named
 * 'testing.repo_scripts_path'` buried inside a captured `[static guard]`
 * log — the assertion itself read only `expected 1 to be +0`. Deriving the
 * closure makes the next such import free instead of a promotion blocker.
 *
 * A regex is sufficient and safe here: this walk only ever ADDS files to a
 * fixture. If it under-matches, the fixture is exactly as incomplete as a
 * hand-written list and the test fails loudly the same way it does today —
 * it can never turn a real failure into a silent pass.
 */
export function testingImportClosure(
  fs: FixtureFs, path: FixturePath, productionRoot: string, entries: readonly string[],
): string[] {
  const found = new Set<string>();
  const seen = new Set<string>(entries);
  const queue = [...entries];
  while (queue.length > 0) {
    const relative = queue.shift() as string;
    const absolute = path.join(productionRoot, relative);
    if (!fs.existsSync(absolute)) continue;
    for (const match of fs.readFileSync(absolute, 'utf8').matchAll(TESTING_IMPORT)) {
      const dependency = `backend/testing/${match[1]}.py`;
      if (seen.has(dependency)) continue;
      seen.add(dependency);
      if (!fs.existsSync(path.join(productionRoot, dependency))) continue;
      found.add(dependency);
      queue.push(dependency);
    }
  }
  return [...found].sort();
}
