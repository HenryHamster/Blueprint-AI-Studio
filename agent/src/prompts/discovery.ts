import fs from 'fs';
import { execFileSync } from 'child_process';
import type WebSocket from 'ws';
import { sendLog, WORKSPACE } from '../index.js';
import type { BuildConfig, FailureCategory } from '../index.js';
import { GREENFIELD_SOURCE_FILE_THRESHOLD } from '../constants.js';

// ── Cached git ls-files ──────────────────────────────────────────────────────
// Multiple functions in this module run `git ls-files`.  The output is
// identical for the lifetime of a task workspace, so cache it once.
let _cachedTrackedFiles: string[] | null = null;

function getTrackedFiles(): string[] {
  if (_cachedTrackedFiles === null) {
    try {
      _cachedTrackedFiles = execFileSync('git', ['ls-files'], { cwd: WORKSPACE, encoding: 'utf-8' })
        .split('\n')
        .filter(Boolean);
    } catch {
      _cachedTrackedFiles = [];
    }
  }
  return _cachedTrackedFiles;
}

/** Clear the ls-files cache so a fresh enumeration runs on the next call. */
export function clearTrackedFilesCache(): void { _cachedTrackedFiles = null; }

// ── Cached repo-map computation ──────────────────────────────────────────────
// buildRepoMap reads every tracked source file and parses imports — cheap to
// recompute only when HEAD moves, expensive to run on every prompt build.
interface RepoMapCacheEntry {
  headCommit: string;
  inDegree: Map<string, number>;
  fileImports: Map<string, string[]>;
}
const _repoMapCache = new Map<string, RepoMapCacheEntry>();

/** Clear the repo-map cache so the next buildRepoMap call recomputes. */
export function clearRepoMapCache(): void { _repoMapCache.clear(); }

export function buildFileTree(ws: WebSocket | null): string {
  try {
    // Get files ranked by recent git activity
    let recentFiles: string[] = [];
    try {
      const gitLog = execFileSync('git', ['log', '--pretty=format:', '--name-only', '-100'],
        { cwd: WORKSPACE, encoding: 'utf-8' });
      const counts: Record<string, number> = {};
      for (const f of gitLog.split('\n').filter(Boolean)) {
        counts[f] = (counts[f] ?? 0) + 1;
      }
      recentFiles = Object.entries(counts)
        .sort((a, b) => b[1] - a[1])
        .map(([f]) => f);
    } catch { /* fallback to unsorted */ }

    // Get all tracked files (cached — git ls-files output is stable per workspace session)
    const allFiles = getTrackedFiles()
      .filter(f => !f.includes('node_modules/') && !f.includes('dist/'));

    // Merge: recent files first, then remaining alphabetically
    const recentSet = new Set(recentFiles);
    const remaining = allFiles.filter(f => !recentSet.has(f)).sort();
    const sorted = [...recentFiles.filter(f => allFiles.includes(f)), ...remaining];
    const tree = sorted.slice(0, 150).map(f => './' + f).join('\n');

    return tree
      ? `## Repository File Tree (first 150 files, sorted by recent activity)\n\`\`\`\n${tree}\n\`\`\`\n\n`
      : '';
  } catch (e) {
    sendLog(ws, `Warning: file tree generation failed: ${String(e)}\n`);
    return '';
  }
}


// ── Greenfield (empty / near-empty repo) detection ──────────────────────────
// Config/scaffolding files that do NOT count as real source. A repo containing
// only these (plus dotfiles) is a fresh project to bootstrap, not a codebase to
// read and improve. Extension-based so it is language-agnostic.
const NON_SOURCE_EXT = /\.(md|markdown|json|ya?ml|toml|cfg|ini|txt|lock|lockb)$/i;
// Common config/manifest basenames with no telling extension.
const NON_SOURCE_BASENAMES = new Set([
  'license', 'notice', 'authors', 'codeowners', 'dockerfile', 'makefile',
  'procfile', '.gitignore', '.gitattributes', '.dockerignore', '.editorconfig',
  '.npmrc', '.nvmrc', '.prettierrc', '.eslintrc',
]);

/**
 * Counts the real source files in the cloned working tree, excluding
 * config/scaffolding (lockfiles, *.md/*.json/*.yml/*.toml, dotfiles, common
 * manifests). Reuses the same `git ls-files` enumeration as buildFileTree.
 * Returns null when the scan cannot complete, so callers can fall back to the
 * durable onboarding signal instead of misclassifying an existing repo.
 */
export function countSourceFiles(): number | null {
  try {
    // Deliberately bypasses getTrackedFiles() — countSourceFiles must
    // distinguish "scan error" (return null) from "empty repo" (return 0),
    // but getTrackedFiles() swallows errors and returns [].
    const out = execFileSync('git', ['ls-files'], { cwd: WORKSPACE, encoding: 'utf-8' });
    const files = out
      .split('\n')
      .filter(f => f && !f.includes('node_modules/') && !f.includes('dist/'));
    let count = 0;
    for (const f of files) {
      const base = (f.split('/').pop() || f).toLowerCase();
      // Dotfiles (e.g. .gitignore, .env) and dot-prefixed config are scaffolding.
      if (base.startsWith('.')) continue;
      if (NON_SOURCE_BASENAMES.has(base)) continue;
      if (NON_SOURCE_EXT.test(base)) continue;
      count++;
    }
    return count;
  } catch {
    return null;
  }
}

/**
 * True when the working tree has effectively no source — only config and
 * scaffolding — so the system should bootstrap the foundation from the project
 * goal rather than assume an existing codebase. Gated by a single threshold
 * constant (GREENFIELD_SOURCE_FILE_THRESHOLD).
 *
 * Cross-language coupling: the NON_SOURCE_EXT / NON_SOURCE_BASENAMES sets and
 * GREENFIELD_SOURCE_FILE_THRESHOLD that define "real source" here must stay
 * aligned with the backend's durable empty-repo classifier
 * (backend/app/build_config_detection.py). If you change what counts as source
 * on one side, change it on the other or the durable onboarding signal and this
 * live prompt-time signal will disagree again.
 */
export function detectGreenfield(): boolean {
  const sourceFileCount = countSourceFiles();
  if (sourceFileCount === null) return false;
  return sourceFileCount <= GREENFIELD_SOURCE_FILE_THRESHOLD;
}

/**
 * Single greenfield predicate that BOTH the research and plan handlers consult,
 * so the durable onboarding gate and the prompt-time gate can never disagree.
 *
 * Reconciles two signals:
 *  - the DURABLE onboarding signal `task.repo_is_empty` (persisted as
 *    `build_config.repo_is_empty` from the shallow onboarding scan), and
 *  - the LIVE full-clone `detectGreenfield()` scan of the working tree.
 *
 * The DURABLE signal is authoritative whenever it is explicitly present:
 *  - `repo_is_empty === true`  → greenfield (short-circuit; never read the tree).
 *  - `repo_is_empty === false` → NOT greenfield, even if the live scan currently
 *    sees an empty tree. A durable "non-empty" reading paired with an empty live
 *    tree means the clone failed to deliver content (partial/wrong/shallow
 *    checkout), NOT that the repo is genuinely greenfield — `verifyCloneDelivered`
 *    catches that disagreement and fails loudly before we get here. Treating the
 *    empty tree as greenfield would scaffold into a repo that actually has code.
 *
 * Only when the durable signal is ABSENT (or a non-boolean value) do we consult
 * the live `detectGreenfield()` scan, which catches lightly-scaffolded repos the
 * shallow onboarding scan may have missed and projects onboarded before the
 * durable signal existed. The two signals can therefore never silently resolve
 * to greenfield when they disagree.
 */
export function isGreenfieldRepo(task: Record<string, unknown>): boolean {
  if (task.repo_is_empty === true) return true;
  if (task.repo_is_empty === false) return false;
  return detectGreenfield();
}

/**
 * Positively confirms that the clone actually delivered the expected working
 * tree BEFORE any greenfield conclusion is allowed. A clone can exit 0 yet leave
 * an empty or near-empty tree — a partial clone, a wrong/empty checkout, or a
 * shallow fetch whose objects never materialized. Without this check
 * `countSourceFiles()` returns 0 (not null) and the agent wrongly enters
 * bootstrap mode, scaffolding into a repo that already has code.
 *
 * Two positive confirmations, both throwing a `clone_failed`-tagged error (which
 * the role loop classifies as a PROVISION_FAILURE and retries / fails loudly,
 * rather than silently proceeding as greenfield):
 *  1. HEAD resolves to a real commit, and — when the expected branch is known —
 *     matches `origin/<branch>` (so a wrong or partial checkout is caught).
 *  2. When the durable signal says the repo is NOT empty (`repo_is_empty ===
 *     false`) but the live tree is empty/near-empty, the clone did not deliver
 *     the expected content — treat it as a clone problem, not greenfield.
 *
 * The check is intentionally conservative about FALSE positives: if HEAD cannot
 * be read at all we throw (an unusable workspace), but a missing `origin/<branch>`
 * ref or an unreadable `git ls-files` (countSourceFiles → null) is NOT treated as
 * a clone failure on its own — only an explicit durable/live disagreement is.
 */
export function verifyCloneDelivered(task: Record<string, unknown>, branch?: string): void {
  const cloneFailure = (message: string): Error & { failureCategory: FailureCategory } => {
    const err = new Error(message) as Error & { failureCategory: FailureCategory };
    err.failureCategory = 'clone_failed';
    return err;
  };

  // (1a) HEAD must resolve to a real commit — a usable checkout exists.
  let head: string | null = null;
  try {
    head = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: WORKSPACE, encoding: 'utf-8' }).trim();
  } catch {
    head = null;
  }
  if (!head) {
    throw cloneFailure('clone_failed: working tree has no resolvable HEAD after clone — the clone did not deliver a checkout');
  }

  // (1b) When we know the expected branch, HEAD must match its remote tracking
  // ref. A mismatch means a wrong or partial checkout. A MISSING origin ref is
  // not treated as a failure here (the durable check below is the real net).
  if (branch) {
    let originHead: string | null = null;
    try {
      originHead = execFileSync('git', ['rev-parse', '--verify', `origin/${branch}`],
        { cwd: WORKSPACE, encoding: 'utf-8' }).trim();
    } catch {
      originHead = null;
    }
    if (originHead && originHead !== head) {
      throw cloneFailure(`clone_failed: HEAD (${head.slice(0, 12)}) does not match origin/${branch} (${originHead.slice(0, 12)}) — partial or wrong checkout`);
    }
  }

  // (2) Durable signal says the repo is NOT empty, but the live tree is
  // empty/near-empty → the clone did not deliver content. countSourceFiles()
  // returning null means the scan could not run; we do NOT claim a clone failure
  // on a failed scan (only on a confirmed empty tree paired with a non-empty
  // durable signal).
  if (task.repo_is_empty === false) {
    const sourceFileCount = countSourceFiles();
    if (sourceFileCount !== null && sourceFileCount <= GREENFIELD_SOURCE_FILE_THRESHOLD) {
      throw cloneFailure(`clone_failed: durable onboarding signal says the repo is NOT empty, but the cloned tree has only ${sourceFileCount} source file(s) — the clone did not deliver the expected content`);
    }
  }
}

// ── Test Convention Extraction ───────────────────────────────────────────────
// Cache: dirKey → convention summary string. Populated once per task invocation.
const _testConventionCache = new Map<string, string>(); // @per-role

export function clearTestConventionCache(): void { _testConventionCache.clear(); }

/**
 * Scan test files in the given directory and extract conventions:
 * fixtures, mock libraries, setup patterns, assertion style.
 * Returns a compact summary (<=500 chars). Cached per dirKey.
 */
export function extractTestConventions(dirKey: string, testFiles: string[]): string {
  const cached = _testConventionCache.get(dirKey);
  if (cached !== undefined) return cached;

  const fixtures = new Set<string>();
  const mockImports = new Set<string>();
  const setupPatterns = new Set<string>();
  const assertionStyles = new Set<string>();

  for (const tf of testFiles.slice(0, 6)) {
    const full = `${WORKSPACE}/${tf}`;
    if (!fs.existsSync(full)) continue;
    try {
      const lines = fs.readFileSync(full, 'utf-8').split('\n').slice(0, 120);
      for (const line of lines) {
        // Python fixture decorators (names extracted below via full-content regex)
        // Python mock imports
        if (line.match(/from\s+unittest\.mock\s+import/)) {
          const imported = line.match(/import\s+(.+)/)?.[1].trim();
          if (imported) mockImports.add(`unittest.mock: ${imported.slice(0, 50)}`);
        }
        if (line.match(/from\s+pytest_mock/)) mockImports.add('pytest_mock');
        // JS/TS mock imports
        if (line.match(/vi\.fn\(\)|vi\.mock\(|vitest/)) mockImports.add('vitest (vi.fn/vi.mock)');
        if (line.match(/jest\.fn\(\)|jest\.mock\(/)) mockImports.add('jest');
        // Setup patterns
        if (line.match(/beforeEach\s*\(/)) setupPatterns.add('beforeEach');
        if (line.match(/afterEach\s*\(/)) setupPatterns.add('afterEach');
        if (line.match(/beforeAll\s*\(/)) setupPatterns.add('beforeAll');
        if (line.match(/def\s+setup(?:_method)?\s*\(/)) setupPatterns.add('setUp');
        if (line.match(/@pytest\.fixture/)) setupPatterns.add('@pytest.fixture');
        if (line.match(/@pytest_asyncio\.fixture/)) setupPatterns.add('@pytest_asyncio.fixture');
        // Assertion styles
        if (line.match(/expect\s*\(.*\)\.(toBe|toEqual|toHaveBeenCalled)/)) assertionStyles.add('expect().toBe/toEqual');
        if (line.match(/\bassert\s+/)) assertionStyles.add('assert');
        if (line.match(/pytest\.raises\s*\(/)) assertionStyles.add('pytest.raises');
        if (line.match(/assert_called(?:_once)?_with/)) assertionStyles.add('mock.assert_called_with');
        if (line.match(/AsyncMock\s*\(/)) mockImports.add('AsyncMock');
        if (line.match(/MagicMock\s*\(/)) mockImports.add('MagicMock');
      }

      // Fixture names: scan for @pytest.fixture + next def
      const fullContent = lines.join('\n');
      const fixturePattern = /@(?:pytest_asyncio\.fixture|pytest\.fixture)[^\n]*\n(?:async )?def\s+(\w+)/g;
      let fm: RegExpExecArray | null;
      while ((fm = fixturePattern.exec(fullContent)) !== null) {
        fixtures.add(fm[1]);
      }
    } catch { /* skip */ }
  }

  const parts: string[] = [];
  if (fixtures.size > 0) parts.push(`Fixtures: ${[...fixtures].slice(0, 6).join(', ')}`);
  if (mockImports.size > 0) parts.push(`Mocks: ${[...mockImports].slice(0, 4).join(', ')}`);
  if (setupPatterns.size > 0) parts.push(`Setup: ${[...setupPatterns].join(', ')}`);
  if (assertionStyles.size > 0) parts.push(`Assertions: ${[...assertionStyles].join(', ')}`);

  const summary = parts.length > 0 ? parts.join(' | ') : '';
  const result = summary.slice(0, 500);
  _testConventionCache.set(dirKey, result);
  return result;
}

export function discoverRelatedTests(ws: WebSocket | null, relatedFiles: string[], buildConfig?: BuildConfig): string {
  if (relatedFiles.length === 0) return '';

  try {
    const testEntries: string[] = [];
    const seen = new Set<string>();
    // Track test files per directory key for convention extraction
    const testFilesByDir = new Map<string, string[]>();

    const extractTestNames = (content: string): string[] => {
      const names: string[] = [];
      for (const line of content.split('\n')) {
        const m = line.match(/^\s*(?:def (test_\w+)|it\(['"`]([^'"`]+)['"`]|test\(['"`]([^'"`]+)['"`]|describe\(['"`]([^'"`]+)['"`])/);
        if (m) names.push(m[1] || m[2] || m[3] || m[4]);
      }
      return names;
    };

    const processTestFile = (testPath: string) => {
      const full = `${WORKSPACE}/${testPath}`;
      if (seen.has(testPath) || !fs.existsSync(full)) return;
      seen.add(testPath);
      try {
        const content = fs.readFileSync(full, 'utf-8').split('\n').slice(0, 80).join('\n');
        const names = extractTestNames(content);
        if (names.length > 0) {
          testEntries.push(`- ${testPath}: ${names.slice(0, 10).join(', ')}`);
        } else {
          testEntries.push(`- ${testPath}`);
        }
        // Track by directory key for convention extraction
        const dirKey = testPath.startsWith('backend/') ? 'backend'
          : testPath.startsWith('agent/') ? 'agent'
          : testPath.startsWith('admin-ui/') ? 'admin-ui'
          : testPath.split('/')[0];
        let bucket = testFilesByDir.get(dirKey);
        if (!bucket) {
          bucket = [];
          testFilesByDir.set(dirKey, bucket);
        }
        bucket.push(testPath);
      } catch { /* skip unreadable */ }
    };

    // Build a lookup from directory prefix → directory type using buildConfig if provided
    const dirConfigs = buildConfig?.directories ?? [];


    // Phase 1: Convention-based discovery (one pass over related files — uses
    // cached ls-files for directory scans instead of spawning per-directory
    // child processes).
    for (const relFile of relatedFiles) {
      let discoveredByConvention = false;

      if (dirConfigs.length > 0) {
        for (const dir of dirConfigs) {
          const dirPrefix = dir.path.replace(/\/$/, '');
          if (!relFile.startsWith(dirPrefix + '/')) continue;
          discoveredByConvention = true;

          if (dir.type === 'python' && relFile.endsWith('.py')) {
            // Look for test_<module>.py in common test directories
            const base = relFile.replace(new RegExp(`^${dirPrefix}/`), '').replace(/\.py$/, '');
            const moduleName = base.split('/').pop() || base;
            for (const testDir of [`${dirPrefix}/testing`, `${dirPrefix}/tests`, `${dirPrefix}/test`]) {
              processTestFile(`${testDir}/test_${moduleName}.py`);
            }
          } else if ((dir.type === 'node') && relFile.match(/\.(tsx?|jsx?)$/) && !relFile.includes('.test.')) {
            const testPath = relFile.replace(/\.(tsx?|jsx?)$/, (ext) => `.test${ext}`);
            processTestFile(testPath);
            // Scan this directory for test files (from cached ls-files, no child process)
            try {
              const prefix = dirPrefix + '/';
              const nodeTestFiles = getTrackedFiles()
                .filter(f => f.startsWith(prefix) && (f.includes('.test.') || f.includes('.spec.')));
              for (const tf of nodeTestFiles) processTestFile(tf);
            } catch { /* skip */ }
          } else {
            // Unknown type: fall through to grep-based discovery (phase 2 below)
            discoveredByConvention = false;
          }
        }
      } else {
        // Hardcoded Blueprint conventions
        if (relFile.startsWith('backend/') && relFile.endsWith('.py')) {
          const base = relFile.replace(/^backend\/app\//, '').replace(/\.py$/, '');
          const candidate = `backend/testing/test_${base.split('/').pop()}.py`;
          processTestFile(candidate);
          discoveredByConvention = true;
        } else if (relFile.match(/^admin-ui\/.*\.(tsx?|jsx?)$/) && !relFile.includes('.test.')) {
          const testPath = relFile.replace(/\.(tsx?|jsx?)$/, (ext) => `.test${ext}`);
          processTestFile(testPath);
          discoveredByConvention = true;
        } else if (relFile.startsWith('agent/')) {
          // Search for any test files in agent/ (from cached ls-files)
          try {
            const agentTestFiles = getTrackedFiles()
              .filter(f => f.startsWith('agent/') && (f.includes('.test.') || f.includes('test_') || f.includes('/tests/')));
            for (const tf of agentTestFiles) processTestFile(tf);
          } catch { /* skip */ }
          discoveredByConvention = true;
        }
      }
      void discoveredByConvention;
    }

    // Phase 2: Batched grep-based discovery — collect all module names and run
    // a single `git grep` instead of one per related file (N+1 → 1).
    const moduleNamesForGrep = relatedFiles
      .map(f => f.split('/').pop()?.replace(/\.(tsx?|jsx?|py)$/, '') || '')
      .filter(Boolean);
    if (moduleNamesForGrep.length > 0) {
      try {
        // Escape regex-special chars and join into a single alternation
        const escaped = moduleNamesForGrep.map(n => n.replace(/[.+*?^${}()|[\]\\]/g, '\\$&'));
        const combined = escaped.join('|');
        const grepResult = execFileSync('git', ['grep', '-lE', combined, '--', '*test*'],
          { cwd: WORKSPACE, encoding: 'utf-8' });
        for (const tf of grepResult.split('\n').filter(Boolean)) {
          processTestFile(tf);
        }
      } catch { /* no matches */ }
    }

    // Phase 3: Batched import-based discovery — collect Python dotted-paths and
    // TS stems, then run at most 2 greps instead of N × 2.
    const pyImportPatterns: string[] = [];
    const tsImportStems: string[] = [];
    for (const relFile of relatedFiles) {
      if (relFile.endsWith('.py')) {
        const pyModulePath = relFile
          .replace(/^backend\//, '')
          .replace(/\.py$/, '')
          .replace(/\//g, '.');
        pyImportPatterns.push(`(from ${pyModulePath} import|import ${pyModulePath})`);
      } else if (relFile.match(/\.(tsx?|jsx?)$/) && !relFile.includes('.test.')) {
        const stem = relFile.split('/').pop()?.replace(/\.(tsx?|jsx?)$/, '') || '';
        if (stem) tsImportStems.push(stem.replace(/[.+*?^${}()|[\]\\]/g, '\\$&'));
      }
    }
    if (pyImportPatterns.length > 0) {
      try {
        const combined = pyImportPatterns.join('|');
        const pyGrepResult = execFileSync('git', ['grep', '-lE', combined, '--', '*test*'],
          { cwd: WORKSPACE, encoding: 'utf-8' });
        for (const tf of pyGrepResult.split('\n').filter(Boolean)) {
          processTestFile(tf);
        }
      } catch { /* no matches */ }
    }
    if (tsImportStems.length > 0) {
      try {
        const stemPattern = tsImportStems.length === 1
          ? tsImportStems[0]
          : `(${tsImportStems.join('|')})`;
        const tsImportPattern = `from ['"][^'"]*/${stemPattern}(\\.js|\\.ts|\\.tsx)?['"]`;
        const tsGrepResult = execFileSync('git', ['grep', '-lE', tsImportPattern, '--', '*test*'],
          { cwd: WORKSPACE, encoding: 'utf-8' });
        for (const tf of tsGrepResult.split('\n').filter(Boolean)) {
          processTestFile(tf);
        }
      } catch { /* no matches */ }
    }

    if (testEntries.length === 0) return '';

    // Extract test conventions per directory type
    const conventionLines: string[] = [];
    for (const [dirKey, files] of testFilesByDir) {
      const conventions = extractTestConventions(dirKey, files);
      if (conventions) conventionLines.push(`${dirKey}: ${conventions}`);
    }
    const conventionsSection = conventionLines.length > 0
      ? `## Test Conventions\n${conventionLines.join('\n')}\n\n`
      : '';

    const output = `## Related Tests\nThese tests cover files you'll be modifying:\n${testEntries.join('\n')}\n\n${conventionsSection}`;
    return output.slice(0, 3000);
  } catch (e) {
    sendLog(ws, `Warning: related test discovery failed: ${String(e)}\n`);
    return '';
  }
}

// ── Module interface extraction ──────────────────────────────────────────────

/**
 * Extracts top-level exported symbols from a Python source file.
 * Prefers __all__ when present; otherwise scans for top-level def/class.
 */
function extractPythonExports(content: string): string[] {
  // If __all__ is defined, use it as the authoritative public surface
  const allMatch = content.match(/__all__\s*=\s*\[([\s\S]*?)\]/);
  if (allMatch) {
    return [...allMatch[1].matchAll(/['"](\w+)['"]/g)].map(m => m[1]).slice(0, 15);
  }

  const results: string[] = [];
  for (const line of content.split('\n')) {
    // Top-level functions (no leading indentation)
    const funcMatch = line.match(/^def (\w+)\(([^)]*)\)/);
    const classMatch = line.match(/^class (\w+)/);
    if (funcMatch) {
      const params = funcMatch[2].length > 50 ? funcMatch[2].slice(0, 47) + '...' : funcMatch[2];
      results.push(`def ${funcMatch[1]}(${params})`);
    } else if (classMatch) {
      results.push(`class ${classMatch[1]}`);
    }
  }
  return results;
}

/**
 * Extracts export declarations from a TypeScript/JavaScript source file.
 */
function extractTypeScriptExports(content: string): string[] {
  const results: string[] = [];
  for (const line of content.split('\n')) {
    let m: RegExpMatchArray | null;
    if ((m = line.match(/^export\s+(?:default\s+)?(?:async\s+)?function\s+(\w+)\s*(?:<[^>]*>)?\s*\(([^)]*)\)/))) {
      const params = m[2].length > 50 ? m[2].slice(0, 47) + '...' : m[2];
      results.push(`function ${m[1]}(${params})`);
    } else if ((m = line.match(/^export\s+(?:abstract\s+)?class\s+(\w+)/))) {
      results.push(`class ${m[1]}`);
    } else if ((m = line.match(/^export\s+interface\s+(\w+)/))) {
      results.push(`interface ${m[1]}`);
    } else if ((m = line.match(/^export\s+type\s+(\w+)/))) {
      results.push(`type ${m[1]}`);
    } else if ((m = line.match(/^export\s+(?:const|let)\s+(\w+)/))) {
      results.push(`const ${m[1]}`);
    }
  }
  return results;
}

/**
 * Scans each file in relatedFiles to extract its public API surface (exported
 * functions/classes/types) and which other files import from it.  Returns a
 * concise markdown section for injection into the agent prompt, capped at ~2KB.
 *
 * Only runs for create/refactor tasks with scope=medium or scope=large so that
 * small tasks are not burdened with irrelevant context.
 */
export function extractModuleInterfaces(ws: WebSocket | null, relatedFiles: string[]): string {
  if (relatedFiles.length === 0) return '';

  try {
    const CAP = 2000;
    const entries: string[] = [];
    const processedFiles: string[] = [];
    const fileCallers = new Map<string, string[]>();

    for (const relFile of relatedFiles) {
      // Skip test files — their exports are not caller-facing APIs
      if (relFile.includes('.test.') || relFile.includes('/test_') || relFile.includes('/tests/')) continue;

      const isPython = relFile.endsWith('.py');
      const isTS = /\.(ts|tsx|js|jsx)$/.test(relFile);
      if (!isPython && !isTS) continue;

      let content: string;
      try {
        const fullPath = `${WORKSPACE}/${relFile}`;
        if (!fs.existsSync(fullPath)) continue;
        content = fs.readFileSync(fullPath, 'utf-8');
      } catch { continue; }

      const exports = isPython ? extractPythonExports(content) : extractTypeScriptExports(content);
      if (exports.length === 0) continue;

      // Find files that import this module via git grep
      const moduleName = relFile.split('/').pop()?.replace(/\.(tsx?|jsx?|py)$/, '') || '';
      let callers: string[] = [];
      if (moduleName) {
        try {
          const grepOut = execFileSync(
            'git', ['grep', '-rl', moduleName, '--', '*.ts', '*.tsx', '*.py', '*.js', '*.jsx'],
            { cwd: WORKSPACE, encoding: 'utf-8', timeout: 5000 }
          );
          callers = grepOut.split('\n')
            .filter(f => f && f !== relFile && !f.includes('.test.') && !f.includes('/test_'))
            .slice(0, 5);
        } catch { /* no matches — not an error */ }
      }

      const exportStr = exports.slice(0, 12).join(', ');
      let entry = `**${relFile}** exports: ${exportStr}`;
      if (callers.length > 0) {
        entry += `\n  Imported by: ${callers.join(', ')}`;
      }
      entries.push(entry);
      processedFiles.push(relFile);
      fileCallers.set(relFile, callers);
    }

    if (entries.length === 0) return '';

    let body = `## Module Interfaces\nPublic API and callers for files you are modifying. Update callers when changing signatures.\n\n${entries.join('\n\n')}\n\n`;

    // ── Suggested Change Order ───────────────────────────────────────────────
    // Analyze import dependencies between related files and topologically sort.
    if (processedFiles.length > 1) {
      try {
        const pSet = new Set(processedFiles);
        const dependsOn = new Map(processedFiles.map(f => [f, new Set<string>()]));
        for (const [file, callers] of fileCallers)
          for (const caller of callers) {
            const callerDeps = dependsOn.get(caller);
            if (callerDeps && pSet.has(caller)) callerDeps.add(file);
          }

        if ([...dependsOn.values()].some(d => d.size > 0)) {
          const consumers = new Map(processedFiles.map(f => [f, new Set<string>()]));
          for (const [f, deps] of dependsOn)
            for (const dep of deps) {
              const depConsumers = consumers.get(dep);
              if (depConsumers) depConsumers.add(f);
            }

          const inDeg = new Map(processedFiles.map(f => [f, dependsOn.get(f)?.size ?? 0]));
          const queue = processedFiles.filter(f => inDeg.get(f) === 0);
          const order: string[] = [];
          while (queue.length > 0) {
            const f = queue.shift();
            if (f === undefined) break;
            order.push(f);
            const fConsumers = consumers.get(f);
            if (!fConsumers) continue;
            for (const c of fConsumers) {
              inDeg.set(c, (inDeg.get(c) ?? 0) - 1);
              if (inDeg.get(c) === 0) queue.push(c);
            }
          }
          if (order.length === processedFiles.length) {
            const lines = order.map((f, i) => {
              const deps = dependsOn.get(f);
              const note = deps && deps.size > 0
                ? ` (imports from ${[...deps].map(d => d.split('/').pop() || d).join(', ')})`
                : ' (no dependencies on other changed files)';
              return `${i + 1}. ${f.split('/').pop() ?? f}${note}`;
            });
            body += `## Suggested Change Order\nBased on import dependencies between files you are modifying:\n${lines.join('\n')}\n\nModify foundational files first, then update their consumers.\n\n`;
          }
        }
      } catch { /* dependency analysis failed — skip silently */ }
    }

    return body.slice(0, CAP);
  } catch (e) {
    sendLog(ws, `Warning: module interface extraction failed: ${String(e)}\n`);
    return '';
  }
}

/**
 * Scans all tracked Python/TypeScript source files to build a language-aware
 * repo map: ranks modules by import-graph centrality (in-degree — how many
 * other tracked files import them), lists each hub's exported symbols, and
 * returns a compact markdown section capped at 3 000 chars.
 *
 * When opts.relatedFiles is provided, modules directly adjacent (imported by
 * or importing from) those files are surfaced before global hubs. When
 * opts.taskPrompt is provided, a keyword-match tiebreaker boosts modules
 * whose basename appears in the prompt.
 *
 * Fail-soft: returns '' on any error so the caller never blocks on missing
 * tooling. The natural stdlib/third-party filter comes for free — only modules
 * whose basename matches a tracked file appear in the output.
 */
export function buildRepoMap(ws: WebSocket | null, opts?: { taskPrompt?: string; relatedFiles?: string[] }): string {
  const CAP = 3000;
  try {
    const allFiles = getTrackedFiles()
      .filter(f =>
        /\.(py|ts|tsx|js|jsx|mjs)$/.test(f) &&
        !f.includes('node_modules/') &&
        !f.includes('/dist/') &&
        !f.includes('.test.') &&
        !f.includes('.spec.') &&
        !f.includes('/test_') &&
        !f.includes('/testing/') &&
        !f.includes('/tests/')
      );

    if (allFiles.length === 0) return '';

    const fileSet = new Set(allFiles);

    // ── Repo-map cache: reuse the expensive inDegree/fileImports computation
    // when HEAD hasn't moved.  The ranking pass below is cheap and always
    // recomputed so taskPrompt / relatedFiles stay fresh.
    let headCommit = '';
    try {
      headCommit = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: WORKSPACE, encoding: 'utf-8' }).trim();
    } catch { /* leave empty — cache miss */ }
    const cacheEntry = headCommit ? _repoMapCache.get(WORKSPACE) : undefined;

    // inDegree is keyed on full file paths to avoid cross-package basename collisions
    let inDegree: Map<string, number>;
    // fileImports tracks each file's resolved imports for task-adjacency computation
    let fileImports: Map<string, string[]>;

    if (cacheEntry && cacheEntry.headCommit === headCommit) {
      inDegree = new Map(cacheEntry.inDegree);
      fileImports = new Map(cacheEntry.fileImports);
    } else {
      inDegree = new Map<string, number>();
      fileImports = new Map<string, string[]>();

      for (const file of allFiles) {
        let lines: string[];
        try {
          lines = fs.readFileSync(`${WORKSPACE}/${file}`, 'utf-8').split('\n').slice(0, 100);
        } catch { continue; }

        const isPython = file.endsWith('.py');
        const fileDir = file.split('/').slice(0, -1).join('/');
        const importsForFile: string[] = [];
        for (const line of lines) {
          let resolved: string | null = null;

          if (!isPython) {
            // Only resolve relative imports — package imports are not tracked files
            const m = line.match(/from\s+['"]([^'"]+)['"]/);
            if (m && m[1].startsWith('.')) {
              const spec = m[1].replace(/\.(js|ts|tsx|jsx|mjs)$/, '');
              const joined = fileDir ? fileDir + '/' + spec : spec;
              const segments = joined.split('/');
              const normalized: string[] = [];
              for (const seg of segments) {
                if (seg === '..') normalized.pop();
                else if (seg !== '.') normalized.push(seg);
              }
              const candidateStem = normalized.join('/');
              for (const ext of ['.ts', '.tsx', '.js', '.jsx']) {
                if (fileSet.has(candidateStem + ext)) { resolved = candidateStem + ext; break; }
              }
              if (!resolved && fileSet.has(candidateStem)) resolved = candidateStem;
            }
          } else {
            // Python: resolve dotted module path to a unique tracked file
            let dotted: string | null = null;
            const m1 = line.match(/^from\s+\.*([\w.]+)\s+import\s+/);
            if (m1) dotted = m1[1];
            else { const m2 = line.match(/^import\s+([\w.]+)/); if (m2) dotted = m2[1]; }
            if (dotted) {
              const asPath = dotted.split('.').join('/') + '.py';
              const matches = allFiles.filter(f => f === asPath || f.endsWith('/' + asPath));
              // Drop ambiguous matches — only attribute when exactly one file matches
              if (matches.length === 1) resolved = matches[0];
            }
          }

          if (resolved) {
            inDegree.set(resolved, (inDegree.get(resolved) ?? 0) + 1);
            importsForFile.push(resolved);
          }
        }
        fileImports.set(file, importsForFile);
      }

      // Cache the expensive computation keyed by HEAD commit
      if (headCommit) {
        _repoMapCache.set(WORKSPACE, { headCommit, inDegree, fileImports });
      }
    }

    if (inDegree.size === 0) return '';

    // ── Task-scoped ranking ──────────────────────────────────────────────────
    // When relatedFiles are provided, modules that share a direct import edge
    // with those files (either as dependencies or consumers) are promoted to
    // appear before global hubs so the map is relevant to the current task.
    const taskRelatedSet = new Set(
      (opts?.relatedFiles ?? []).filter(f => fileSet.has(f))
    );
    const taskAdjacent = new Set<string>();
    if (taskRelatedSet.size > 0) {
      // What relatedFiles import → their direct dependencies
      for (const relFile of taskRelatedSet) {
        for (const imported of fileImports.get(relFile) ?? []) {
          if (!taskRelatedSet.has(imported)) taskAdjacent.add(imported);
        }
      }
      // Files that import from relatedFiles → their direct consumers
      for (const [file, imports] of fileImports) {
        if (taskRelatedSet.has(file)) continue;
        if (imports.some(imp => taskRelatedSet.has(imp))) {
          taskAdjacent.add(file);
        }
      }
    }

    // Keyword tiebreaker: files whose basename matches a word in the task prompt
    const promptKeywords = new Set(
      (opts?.taskPrompt ?? '').toLowerCase().match(/\b[a-z]\w{3,}\b/g) ?? []
    );
    const keywordScore = (file: string): number => {
      if (promptKeywords.size === 0) return 0;
      const base = file.split('/').pop()?.replace(/\.(py|ts|tsx|js|jsx|mjs)$/, '').toLowerCase() ?? '';
      return promptKeywords.has(base) ? 0.5 : 0;
    };

    // Merge task-adjacent files into candidates even when their in-degree is 0,
    // so direct consumers of relatedFiles appear in the map regardless of
    // how many other modules import them.
    const candidates = new Map<string, number>(inDegree);
    for (const f of taskAdjacent) {
      if (!candidates.has(f)) candidates.set(f, 0);
    }

    const hubs = [...candidates.entries()]
      .sort((a, b) => {
        const aAdj = taskAdjacent.has(a[0]) ? 1 : 0;
        const bAdj = taskAdjacent.has(b[0]) ? 1 : 0;
        if (aAdj !== bAdj) return bAdj - aAdj;
        return (b[1] + keywordScore(b[0])) - (a[1] + keywordScore(a[0]));
      })
      .slice(0, 20)
      .map(([file]) => file);

    const sections: string[] = [];
    for (const relFile of hubs) {
      let content: string;
      try {
        content = fs.readFileSync(`${WORKSPACE}/${relFile}`, 'utf-8');
      } catch { continue; }

      const exports = relFile.endsWith('.py')
        ? extractPythonExports(content)
        : extractTypeScriptExports(content);
      if (exports.length === 0) continue;

      const count = inDegree.get(relFile) ?? 0;
      sections.push(`**${relFile}** (imported by ${count}): ${exports.slice(0, 10).join(', ')}`);
    }

    if (sections.length === 0) return '';

    const body = `## Repo Map (most-imported modules)\n${sections.join('\n')}\n\n`;
    return body.slice(0, CAP);
  } catch (e) {
    sendLog(ws, `Warning: repo map generation failed: ${String(e)}\n`);
    return '';
  }
}

/**
 * Finds production source files that import from the given related_files.
 * Returns a compact markdown section listing each changed module alongside the
 * consumers that import from it (with the specific symbols they import), capped
 * at ~2000 chars.  Only production code is included — test files are excluded
 * because those are already surfaced by discoverRelatedTests.
 *
 * Only meaningful for create/refactor tasks with scope=medium or scope=large.
 */
export function discoverAffectedConsumers(ws: WebSocket | null, relatedFiles: string[]): string {
  if (relatedFiles.length === 0) return '';

  try {
    const CAP = 2000;
    const entries: string[] = [];

    for (const relFile of relatedFiles) {
      // Skip test files — test consumers are handled by discoverRelatedTests
      if (relFile.includes('.test.') || relFile.includes('/test_') || relFile.includes('/tests/')) continue;

      const isPython = relFile.endsWith('.py');
      const isTS = /\.(ts|tsx|js|jsx)$/.test(relFile);
      if (!isPython && !isTS) continue;

      const moduleName = relFile.split('/').pop()?.replace(/\.(tsx?|jsx?|py)$/, '') || '';
      if (!moduleName) continue;

      // Find production source files that mention this module name
      let consumerFiles: string[] = [];
      try {
        const grepOut = execFileSync(
          'git', ['grep', '-rl', moduleName, '--',
            ...(isPython ? ['*.py'] : ['*.ts', '*.tsx', '*.js', '*.jsx']),
          ],
          { cwd: WORKSPACE, encoding: 'utf-8', timeout: 5000 }
        );
        consumerFiles = grepOut.split('\n').filter(f =>
          f &&
          f !== relFile &&
          !f.includes('.test.') &&
          !f.includes('/test_') &&
          !f.includes('/testing/') &&
          !f.includes('/tests/')
        ).slice(0, 6);
      } catch { /* no matches — not an error */ }

      if (consumerFiles.length === 0) continue;

      // For each consumer, extract which symbols it imports from this module
      const consumerDetails: Array<{ file: string; imports: string[]; usageCounts: Map<string, number> }> = [];
      for (const consumerFile of consumerFiles) {
        const fullPath = `${WORKSPACE}/${consumerFile}`;
        if (!fs.existsSync(fullPath)) continue;

        try {
          const allLines = fs.readFileSync(fullPath, 'utf-8').split('\n');
          const importLines = allLines.slice(0, 80);
          const bodyLines = allLines.slice(80, 280);
          const imports: string[] = [];

          for (const line of importLines) {
            if (isPython) {
              // from .module_name import X, Y  /  from app.module_name import X
              const m = line.match(new RegExp(`from\\s+\\S*${moduleName}\\s+import\\s+(.+)`));
              if (m) {
                const names = m[1].split(',')
                  .map(s => s.trim().replace(/[()#\\].*/g, '').trim())
                  .filter(s => s && /^\w+$/.test(s));
                imports.push(...names);
              }
            } else {
              // import { X, Y } from './module-name' or './module-name.js'
              const namedMatch = line.match(
                new RegExp(`import\\s+\\{([^}]+)\\}\\s+from\\s+['"][^'"]*${moduleName}[^'"]*['"]`)
              );
              if (namedMatch) {
                const names = namedMatch[1].split(',')
                  .map(s => s.trim().split(/\s+as\s+/)[0].trim())
                  .filter(s => s && /^\w+$/.test(s));
                imports.push(...names);
              }
              // import DefaultExport from './module-name' or './module-name.js'
              const defaultMatch = line.match(
                new RegExp(`import\\s+(\\w+)\\s+from\\s+['"][^'"]*${moduleName}[^'"]*['"]`)
              );
              if (defaultMatch) {
                imports.push(defaultMatch[1]);
              }
            }
          }

          const uniqueImports = [...new Set(imports)].slice(0, 8);
          const usageCounts = new Map<string, number>();
          for (const sym of uniqueImports) {
            // Symbols are validated as /^\w+$/, so they are safe to embed in a regex
            const wordRe = new RegExp(`\\b${sym}\\b`, 'g');
            let count = 0;
            for (const line of bodyLines) {
              const matches = line.match(wordRe);
              if (matches) count += matches.length;
            }
            usageCounts.set(sym, count);
          }

          consumerDetails.push({ file: consumerFile, imports: uniqueImports, usageCounts });
        } catch {
          consumerDetails.push({ file: consumerFile, imports: [], usageCounts: new Map() });
        }
      }

      if (consumerDetails.length === 0) continue;

      const consumerLines = consumerDetails.map(c => {
        const importStr = c.imports.length > 0
          ? ` (imports: ${c.imports.map(s => `${s} used ${c.usageCounts.get(s) ?? 0}x`).join(', ')})`
          : '';
        return `  - ${c.file}${importStr}`;
      });
      entries.push(`**${relFile}** is imported by:\n${consumerLines.join('\n')}`);
    }

    if (entries.length === 0) return '';

    const body = `## Affected Consumers\nProduction files that import from your changed modules. Verify they remain compatible after your changes.\n\n${entries.join('\n\n')}\n\n`;
    return body.slice(0, CAP);
  } catch (e) {
    sendLog(ws, `Warning: affected consumer discovery failed: ${String(e)}\n`);
    return '';
  }
}
