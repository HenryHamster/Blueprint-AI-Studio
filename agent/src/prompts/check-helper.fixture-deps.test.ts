import { describe, it, expect } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { FIXTURE_SHARED_FILES, testingImportClosure } from './check-helper.fixture-deps.js';

const PRODUCTION_ROOT = path.resolve(process.cwd(), '..');
const TESTING_IMPORT = /^[ \t]*(?:from|import)[ \t]+testing\.([A-Za-z_][A-Za-z0-9_]*)/gm;

/**
 * The staging contract these assertions defend.
 *
 * `check-helper.test.ts` copies a fixed set of production files into a
 * stripped fixture checkout and runs the real `check_size_limits.py` there.
 * Those runtime scenarios are gated on `POSIX_SHIM_SANDBOX_SUPPORTED`, so on
 * a Windows dev host all 84 of them skip and an incomplete file set is
 * invisible until CI. That is exactly how #17910 shipped: it added
 * `from testing.repo_scripts_path import resolve_scripts_dir_from_env` to
 * `check_size_limits.py`, every dev host stayed green, and the agent lane
 * went red on CI with `ModuleNotFoundError: No module named
 * 'testing.repo_scripts_path'` — visible only inside a captured
 * `[static guard]` log, under an assertion that read `expected 1 to be +0`.
 *
 * These tests run everywhere, including Windows, so the manifest is checked
 * on the host that would otherwise skip past the breakage.
 */
describe('check fixture dependency manifest', () => {
  const stagedPython = () => [
    ...FIXTURE_SHARED_FILES,
    ...testingImportClosure(fs, path, PRODUCTION_ROOT, FIXTURE_SHARED_FILES.filter(f => f.endsWith('.py'))),
  ].filter(f => f.startsWith('backend/testing/') && f.endsWith('.py'));

  it('names only files that exist in the checkout', () => {
    const missing = [...FIXTURE_SHARED_FILES].filter(
      relative => !fs.existsSync(path.join(PRODUCTION_ROOT, relative)),
    );
    expect(missing, 'FIXTURE_SHARED_FILES entry has no production file; copyFileSync would throw').toEqual([]);
  });

  it('stages every backend/testing module the staged guards import', () => {
    const staged = stagedPython();
    // Non-vacuity: an empty scan would satisfy the loop below trivially.
    expect(staged.length, 'no backend/testing sources staged — the scan below proves nothing').toBeGreaterThan(0);

    const unstaged: string[] = [];
    for (const relative of staged) {
      const source = fs.readFileSync(path.join(PRODUCTION_ROOT, relative), 'utf8');
      for (const match of source.matchAll(TESTING_IMPORT)) {
        const dependency = `backend/testing/${match[1]}.py`;
        if (!staged.includes(dependency) && fs.existsSync(path.join(PRODUCTION_ROOT, dependency))) {
          unstaged.push(`${relative} -> ${dependency}`);
        }
      }
    }
    expect(unstaged, 'staged guard imports a backend/testing module the fixture does not copy').toEqual([]);
  });

  it('derives the intra-testing import check_size_limits.py actually has today', () => {
    // A literal pin, deliberately: it is the import that broke CI, and it
    // keeps the walker honest if someone rewrites the regex.
    const closure = testingImportClosure(
      fs, path, PRODUCTION_ROOT, ['backend/testing/check_size_limits.py'],
    );
    expect(closure).toContain('backend/testing/repo_scripts_path.py');
  });

  it('ignores testing.* named in a comment or docstring rather than imported', () => {
    const sandbox = fs.mkdtempSync(path.join(fs.realpathSync(os.tmpdir()), 'fixdeps-'));
    fs.mkdirSync(path.join(sandbox, 'backend', 'testing'), { recursive: true });
    fs.writeFileSync(
      path.join(sandbox, 'backend', 'testing', 'entry.py'),
      '"""See from testing.ghost import thing for history."""\n# import testing.ghost\nx = 1\n',
    );
    fs.writeFileSync(path.join(sandbox, 'backend', 'testing', 'ghost.py'), 'y = 1\n');
    expect(testingImportClosure(fs, path, sandbox, ['backend/testing/entry.py'])).toEqual([]);
  });
});
