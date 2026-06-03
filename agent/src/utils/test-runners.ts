/**
 * Test runner detection and project build configuration parsing.
 *
 * Detects available test runners (npm test, pytest, cargo test, etc.) via
 * auto-detection of package.json, pytest.ini, Cargo.toml, etc., or from
 * explicit project build configuration. Also parses build config for language,
 * build/lint/install commands, environment variables, and migration layout.
 */

import { execSync } from 'child_process';
import fs from 'fs';

const WORKSPACE = process.env.AGENT_WORKSPACE || '/tmp/workspace';

// ── Build configuration (from project settings, passed via claim response) ───
export interface DirectoryConfig {
  path: string;
  type: string; // 'python' | 'node' | 'go' | 'rust'
  install_cmd?: string;
  lint_cmd?: string;
  build_cmd?: string;
  test_cmd?: string;
}
/** Project-specific Alembic/SQLModel migration layout. Lets a non-Blueprint
 *  repo onboard the `migrate` handler without inheriting Blueprint's
 *  `backend/alembic/versions/` + `backend/app/main.py:EXPECTED_SCHEMA_REVISION`
 *  conventions. Every field is optional; unset fields fall back to the
 *  Blueprint defaults (see ``resolveMigrationLayout`` in
 *  ``agent/src/handlers/specialized.ts``) so the self-repo behaviour is
 *  byte-for-byte unchanged. Mirrors ``project.build_config.migration`` on the
 *  backend. */
export interface MigrationConfig {
  /** Directory holding alembic version files, relative to repo root.
   *  Default: ``<pythonDir>/alembic/versions``. */
  versions_dir?: string;
  /** Directory (or file) holding the SQLModel/SQLAlchemy models.
   *  Default: ``<pythonDir>/app/models``. */
  models_dir?: string;
  /** Working directory alembic commands run from (where alembic.ini lives).
   *  Default: ``<pythonDir>``. */
  alembic_cwd?: string;
  /** File declaring the schema-revision constant the startup gate checks.
   *  Default: ``<pythonDir>/app/main.py``. When empty, the schema-revision
   *  step is omitted from the migrate prompt entirely. */
  schema_revision_file?: string;
  /** Name of the schema-revision constant to bump.
   *  Default: ``EXPECTED_SCHEMA_REVISION``. */
  schema_revision_const?: string;
}
export interface BuildConfig {
  language?: string;
  directories?: DirectoryConfig[];
  env_vars?: Record<string, string>;
  git_user_name?: string;
  git_user_email?: string;
  enable_fresh_reviewer?: boolean;
  enable_parallel_review?: boolean;
  /** Project-specific migration layout (alembic versions dir, models dir,
   *  schema-revision file/constant). Unset → Blueprint defaults. */
  migration?: MigrationConfig;
  /** Namespace prefix for agent work branches (``{namespace}/{prefix}-{8hex}``).
   *  Defaults to ``"blueprint"`` so the self-repo's branch names are unchanged.
   *  Mirrors ``project.task_branch_namespace`` on the backend (resolved via
   *  ``shared_state.get_project_task_branch_namespace``). */
  task_branch_namespace?: string;
  /** Ordered staging-branch prefixes that identify research/plan rollup heads.
   *  Defaults to ``["research/", "plan/"]`` when unset. Mirrors
   *  ``project.staging_branch_prefixes`` on the backend (see
   *  ``backend/app/models.py``). */
  staging_branch_prefixes?: string[];
}

// ── Test runner detection (shared by testFixLoop and captureTestBaseline) ───
export interface TestRunner {
  cmd: string;
  args: string[];
  cwd: string;
  label: string;
}

/** Returns true if the runner invokes Docker or the backend test shell script. */
function isDockerRunner(runner: TestRunner): boolean {
  const cmdAndArgs = [runner.cmd, ...runner.args].join(' ');
  return cmdAndArgs.includes('docker') || cmdAndArgs.includes('scripts/test-backend.sh');
}

function isDockerAvailable(): boolean {
  try {
    execSync('docker version', { stdio: 'ignore', timeout: 2000 });
    return true;
  } catch {
    return false;
  }
}

export function detectTestRunners(buildConfig?: BuildConfig): TestRunner[] {
  const { existsSync, readFileSync } = fs;
  const runners: TestRunner[] = [];

  // --- BuildConfig-based detection (takes precedence over auto-detection) ---
  if (buildConfig?.directories) {
    for (const dir of buildConfig.directories) {
      if (dir.test_cmd) {
        const cwd = dir.path ? `${WORKSPACE}/${dir.path}` : WORKSPACE;
        const parts = dir.test_cmd.split(/\s+/);
        runners.push({ cmd: parts[0], args: parts.slice(1), cwd, label: `${dir.test_cmd} (${dir.path || 'root'})` });
      }
    }
  }

  // --- Auto-detection (only if buildConfig produced no runners) ---
  // TS doesn't track .push() mutations, so use a Boolean accessor that always reads the
  // current length to avoid spurious "always-true" narrowing on the literal-empty array.
  const noRunners = (): boolean => runners.length === 0;
  if (noRunners()) {
    const pkgPath = `${WORKSPACE}/package.json`;
    const pytestIniPath = `${WORKSPACE}/pytest.ini`;
    const pyprojectPath = `${WORKSPACE}/pyproject.toml`;
    const cargoPath = `${WORKSPACE}/Cargo.toml`;

    if (existsSync(pkgPath)) {
      try {
        const pkg = JSON.parse(readFileSync(pkgPath, 'utf-8')) as { scripts?: { test?: string } };
        if (pkg.scripts?.test) {
          runners.push({ cmd: 'npm', args: ['test'], cwd: WORKSPACE, label: 'npm test (root)' });
        }
      } catch { /* malformed package.json — skip */ }
    }

    if (noRunners()) {
      if (existsSync(pytestIniPath)) {
        runners.push({ cmd: 'python3', args: ['-m', 'pytest', '-x', '-q'], cwd: WORKSPACE, label: 'pytest (root)' });
      } else if (existsSync(pyprojectPath)) {
        try {
          if (readFileSync(pyprojectPath, 'utf-8').includes('pytest')) {
            runners.push({ cmd: 'python3', args: ['-m', 'pytest', '-x', '-q'], cwd: WORKSPACE, label: 'pytest (root pyproject)' });
          }
        } catch { /* unreadable — skip */ }
      }
    }

    if (noRunners() && existsSync(cargoPath)) {
      runners.push({ cmd: 'cargo', args: ['test'], cwd: WORKSPACE, label: 'cargo test (root)' });
    }

    // --- Subdirectory detection (only if no root-level runner found) ---
    if (noRunners()) {
      const backendPytestIni = `${WORKSPACE}/backend/pytest.ini`;
      const backendPyproject = `${WORKSPACE}/backend/pyproject.toml`;
      const backendTestScript = `${WORKSPACE}/scripts/test-backend.sh`;
      const agentPkg = `${WORKSPACE}/agent/package.json`;

      // --postgres makes test-backend.sh stand up a Postgres sidecar so the
      // local run matches CI. Without it, the script defaults to in-memory
      // SQLite and Postgres-only failures (enum/JSONB/migration drift) only
      // surface on CI, after a push, burning a full review-fix iteration.
      // When Docker is unavailable, fall back to --no-postgres so tests still
      // run locally via the in-memory SQLite path rather than hard-failing.
      const pgFlag = isDockerAvailable() ? '--postgres' : '--no-postgres';
      if (existsSync(backendPytestIni)) {
        if (existsSync(backendTestScript)) {
          runners.push({ cmd: 'bash', args: ['scripts/test-backend.sh', pgFlag], cwd: WORKSPACE, label: 'pytest (backend)' });
        } else {
          runners.push({ cmd: 'python3', args: ['-m', 'pytest', '-x', '-q'], cwd: `${WORKSPACE}/backend`, label: 'pytest (backend)' });
        }
      } else if (existsSync(backendPyproject)) {
        try {
          if (readFileSync(backendPyproject, 'utf-8').includes('pytest')) {
            if (existsSync(backendTestScript)) {
              runners.push({ cmd: 'bash', args: ['scripts/test-backend.sh', pgFlag], cwd: WORKSPACE, label: 'pytest (backend pyproject)' });
            } else {
              runners.push({ cmd: 'python3', args: ['-m', 'pytest', '-x', '-q'], cwd: `${WORKSPACE}/backend`, label: 'pytest (backend pyproject)' });
            }
          }
        } catch { /* unreadable — skip */ }
      }

      if (existsSync(agentPkg)) {
        try {
          const pkg = JSON.parse(readFileSync(agentPkg, 'utf-8')) as { scripts?: { test?: string } };
          if (pkg.scripts?.test) {
            runners.push({ cmd: 'npm', args: ['test'], cwd: `${WORKSPACE}/agent`, label: 'npm test (agent)' });
          }
        } catch { /* malformed package.json — skip */ }
      }
    }
  }

  return runners;
}

/**
 * Classify whether a test runner's output indicates that real tests actually
 * executed, or that the command was a placeholder / collected zero tests.
 *
 * Used by the no-CI merge gate: a repo whose only "test" command is the default
 * scaffolded placeholder (`npm` "no test specified"), or whose suite collects
 * zero tests, is effectively a repo with no detectable tests — it should route
 * to the documented proceed-without-checks fallback rather than being recorded
 * as a hard failure (placeholder exits non-zero) or a vacuous pass (empty suite
 * exits zero).
 *
 * CONSERVATIVE BY DESIGN: returns 'no_tests' ONLY on a positive signature match.
 * Anything else — including a genuine failure like "1 failed" or "2 passed",
 * empty output, or an unrecognized format — defaults to 'ran' so that a real
 * verdict is never misread as "no tests".
 */
export function classifyTestExecution(output: string): 'ran' | 'no_tests' {
  const lower = output.toLowerCase();
  // npm placeholder: the default `package.json` test script prints
  // "Error: no test specified" and exits 1 without running anything.
  if (lower.includes('no test specified')) return 'no_tests';
  // pytest: "no tests ran in 0.01s" / "collected 0 items".
  if (lower.includes('no tests ran')) return 'no_tests';
  if (lower.includes('collected 0 items')) return 'no_tests';
  // vitest: "No test files found, exiting with code 1".
  if (lower.includes('no test files found')) return 'no_tests';
  return 'ran';
}

/**
 * Parse failing test identifiers from test runner output.
 * Returns a Set of stable test IDs (e.g. "path/test_foo.py::test_bar" for pytest,
 * "test description" for vitest) for baseline comparison.
 */
export function parseFailingTests(output: string): Set<string> {
  const failing = new Set<string>();
  for (const line of output.split('\n')) {
    const trimmed = line.trim();
    // pytest: "FAILED backend/testing/test_foo.py::test_bar - AssertionError: ..."
    if (trimmed.startsWith('FAILED ')) {
      const id = trimmed.slice('FAILED '.length).split(' - ')[0].trim();
      if (id) failing.add(id);
    }
    // vitest file-level failure line: " FAIL  src/foo.test.ts > suite > test"
    // (the default reporter emits one per failing file/test, leading space
    // trims away). This was silently missing from the prior parser —
    // vitest does NOT use "FAILED" (the pytest word) and doesn't always
    // use ✕ markers for per-test failures, so a vitest baseline run
    // with real failures was reporting 0.
    //
    // Tightened to `FAIL` + 2+ spaces + non-space body to avoid false
    // positives from test-output lines like `FAIL reason: expected...`
    // (tests that verify failure-formatting can emit FAIL as part of
    // their own stdout). JSON reporter remains the primary path — this
    // is only a fallback.
    const vitestFailMatch = trimmed.match(/^FAIL\s{2,}(\S.*)$/);
    if (vitestFailMatch) failing.add(vitestFailMatch[1]);
    // vitest: "✕ test description" or "× test description" or "✗ test description".
    // Fallback only — prefer parseFailingTestsFromVitestJson when available,
    // since stdout scanning mis-classifies simulated failure output inside
    // passing tests (a test that verifies failure-formatting logic can print
    // a mocked "✕ foo" line that this regex matches even though the real
    // test passed).
    const vitestMatch = trimmed.match(/^[✕✗×⨯]\s+(.+)$/u);
    if (vitestMatch) failing.add(vitestMatch[1].trim());
  }
  return failing;
}

export function normalizePytestSelectorForRunner(selector: string, runner?: TestRunner): string {
  const normalized = selector.trim().replace(/\\/g, '/');
  if (!normalized) return '';
  const invokesBackendScript = Boolean(
    runner && [runner.cmd, ...runner.args].some(arg => arg.includes('scripts/test-backend.sh'))
  );
  const cwd = runner?.cwd.replace(/\\/g, '/') || '';
  const runsFromBackend = invokesBackendScript || cwd.endsWith('/backend');
  if (runsFromBackend && normalized.startsWith('backend/')) {
    return normalized.slice('backend/'.length);
  }
  if (!runsFromBackend && normalized.startsWith('testing/')) {
    return `backend/${normalized}`;
  }
  return normalized;
}

export function extractPytestSelectorsFromOutput(output: string): string[] {
  const selectors = new Set<string>();
  for (const id of parseFailingTests(output)) {
    const normalized = id.trim().replace(/\\/g, '/');
    if (/^(backend\/)?testing\/.+\.py(?:::|$)/.test(normalized)) {
      selectors.add(normalized);
    }
  }

  const selectorPattern = /\b((?:backend\/)?testing\/[A-Za-z0-9_./-]+\.py(?:::[^\s\r\n]+)?)/g;
  for (const line of output.split('\n')) {
    if (!/\b(?:FAILED|ERROR)\b|::error\b/i.test(line)) continue;
    for (const match of line.matchAll(selectorPattern)) {
      selectors.add(match[1].trim().replace(/\\/g, '/'));
    }
  }

  return Array.from(selectors);
}

function acceptsPytestSelectors(runner: TestRunner): boolean {
  const cmdAndArgs = [runner.cmd, ...runner.args].join(' ');
  return cmdAndArgs.includes('pytest') || cmdAndArgs.includes('scripts/test-backend.sh');
}

function withPytestSelectors(runner: TestRunner, selectors: string[]): TestRunner | null {
  if (!acceptsPytestSelectors(runner)) return null;
  const normalized = Array.from(new Set(
    selectors
      .map(selector => normalizePytestSelectorForRunner(selector, runner))
      .filter(Boolean)
  ));
  if (normalized.length === 0) return null;
  return {
    ...runner,
    args: [...runner.args, ...normalized],
    label: `${runner.label} (targeted CI failures)`,
  };
}

export function buildTargetedPytestRunners(runners: TestRunner[], selectors: string[]): TestRunner[] {
  return runners
    .map(runner => withPytestSelectors(runner, selectors))
    .filter((runner): runner is TestRunner => runner !== null);
}

// isDockerRunner is used by callers in index.ts via re-export
export { isDockerRunner };
