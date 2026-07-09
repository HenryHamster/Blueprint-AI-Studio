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
  /** Agent-confirmed commands (persisted by repo_bootstrap write-back). */
  confirmed_test_cmd?: string;
  confirmed_build_cmd?: string;
  confirmed_install_cmd?: string;
  confirmed_lint_cmd?: string;
  /** True when the confirmed commands are stale (re-detection ran after confirmation). */
  commands_stale?: boolean;
}

/**
 * Returns the effective test command for a directory: the confirmed command when
 * it is non-empty and not stale, otherwise the detected test_cmd.
 */
export function effectiveTestCmd(dir: DirectoryConfig): string | undefined {
  if (dir.commands_stale !== true && dir.confirmed_test_cmd) return dir.confirmed_test_cmd;
  return dir.test_cmd;
}

/**
 * Returns the effective build command for a directory: the confirmed command when
 * it is non-empty and not stale, otherwise the detected build_cmd.
 */
export function effectiveBuildCmd(dir: DirectoryConfig): string | undefined {
  if (dir.commands_stale !== true && dir.confirmed_build_cmd) return dir.confirmed_build_cmd;
  return dir.build_cmd;
}

/**
 * Returns the effective install command for a directory: the confirmed command when
 * it is non-empty and not stale, otherwise the detected install_cmd.
 */
export function effectiveInstallCmd(dir: DirectoryConfig): string | undefined {
  if (dir.commands_stale !== true && dir.confirmed_install_cmd) return dir.confirmed_install_cmd;
  return dir.install_cmd;
}

/**
 * Returns the effective lint command for a directory: the confirmed command when
 * it is non-empty and not stale, otherwise the detected lint_cmd.
 */
export function effectiveLintCmd(dir: DirectoryConfig): string | undefined {
  if (dir.commands_stale !== true && dir.confirmed_lint_cmd) return dir.confirmed_lint_cmd;
  return dir.lint_cmd;
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
  installCmd?: string;
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

function manifestDefinesTestScript(text: string): boolean {
  try {
    const parsed = JSON.parse(text) as { scripts?: { test?: string } };
    return Boolean(parsed.scripts?.test);
  } catch {
    return false;
  }
}

function textDefinesTestTarget(text: string): boolean {
  return /^\s*test\b[^:\n]*:/m.test(text);
}

/** Strict Makefile-only `test` target check. `make test` only ever runs an
 *  EXACT top-level `test` target — a Makefile whose only target is
 *  `test-integration:` makes `make test` fail with "No rule to make target
 *  test", and an indented / leading-whitespace target is a recipe line, not a
 *  target. So the prefix-tolerant `textDefinesTestTarget` (correct for
 *  just/Taskfile) OVER-detects here. Mirrors the backend onboarding detector's
 *  `_MAKEFILE_TEST_TARGET_RE = ^test\s*:` (admin_onboard.py): line-anchored with
 *  no leading whitespace, only optional whitespace between `test` and `:`. */
function textDefinesMakeTestTarget(text: string): boolean {
  return /^test[ \t]*:/m.test(text);
}

function fileMentionsPytest(path: string): boolean {
  try {
    return fs.readFileSync(path, 'utf-8').includes('pytest');
  } catch {
    return false;
  }
}

// Shell metacharacters that require a real shell to interpret. A command
// containing any of these (operators, redirections, substitutions, globs,
// backslash escapes) cannot be run by execFile after a naive whitespace split —
// it must round-trip through `bash -c`. Quote characters alone do NOT need a
// shell: a quoted argument like `-k "not slow"` is handled by the tokenizer.
const SHELL_OPERATORS_RE = /[&;|$`<>(){}*?\\]/;

/** Quote-aware tokenizer for an operator-configured test command. Splits on
 *  unquoted whitespace while preserving single/double-quoted segments as a
 *  single argument (with the surrounding quotes stripped), so
 *  `pytest -k "not slow"` → ['pytest', '-k', 'not slow']. Unbalanced quotes
 *  fall back to a plain whitespace split (the bash -c branch already handles
 *  anything genuinely shell-shaped). */
function tokenizeCommand(cmd: string): string[] {
  const tokens: string[] = [];
  let current = '';
  let quote: '"' | "'" | null = null;
  let started = false;
  for (const ch of cmd) {
    if (quote) {
      if (ch === quote) { quote = null; } else { current += ch; }
      continue;
    }
    if (ch === '"' || ch === "'") { quote = ch; started = true; continue; }
    if (/\s/.test(ch)) {
      if (started) { tokens.push(current); current = ''; started = false; }
      continue;
    }
    current += ch;
    started = true;
  }
  if (started) tokens.push(current);
  if (quote !== null) return cmd.split(/\s+/).filter(Boolean);
  return tokens;
}

/** Build a safe execFile argv from an operator-configured shell command.
 *
  *  - A command needing shell features (operators, substitutions, redirections,
 *    globs, backslash escapes) is wrapped as `{cmd:'bash', args:['-c', cmd]}` so
  *    the operator's own command runs verbatim under a shell. The runner is
  *    executed via execFile (NOT a shell), so the command string is passed as a
 *    single argv element and is never re-interpreted — injection-safe.
 *  - A plain command is tokenized quote-aware so `pytest -k "not slow"` yields
 *    `{cmd:'pytest', args:['-k','not slow']}`. Plain commands keep their
 *    historical `{cmd,args}` shape byte-for-byte, so pytest-selector targeting
 *    and family-parity invariants are unchanged. */
export function runnerFromCommand(command: string, cwd: string): { cmd: string; args: string[]; cwd: string } {
  if (SHELL_OPERATORS_RE.test(command)) {
    return { cmd: 'bash', args: ['-c', command], cwd };
  }
  const tokens = tokenizeCommand(command);
  if (tokens.length === 0) return { cmd: 'bash', args: ['-c', command], cwd };
  return { cmd: tokens[0], args: tokens.slice(1), cwd };
}

// Backward-compatible alias for older internal callers that still use the
// prior helper name.
export const runnerFromTestCmd = runnerFromCommand;

export function detectTestRunners(buildConfig?: BuildConfig): TestRunner[] {
  const { existsSync, readFileSync, readdirSync } = fs;
  const runners: TestRunner[] = [];

  // --- BuildConfig-based detection (takes precedence over auto-detection) ---
  if (buildConfig?.directories) {
    for (const dir of buildConfig.directories) {
      const testCmd = effectiveTestCmd(dir);
      if (testCmd) {
        const cwd = dir.path ? `${WORKSPACE}/${dir.path}` : WORKSPACE;
        runners.push({
          ...runnerFromCommand(testCmd, cwd),
          label: `${testCmd} (${dir.path || 'root'})`,
          installCmd: effectiveInstallCmd(dir),
        });
      }
    }
  }

  // --- Auto-detection (only if buildConfig produced no runners) ---
  // TS doesn't track .push() mutations, so use a Boolean accessor that always reads the
  // current length to avoid spurious "always-true" narrowing on the literal-empty array.
  const noRunners = (): boolean => runners.length === 0;

  // Shared manifest→runner mapping for the test-runner families that diverge
  // from the core python/node/go/rust set: Ruby, Gradle, Maven, Elixir, .NET,
  // and PHP. Mirrors the backend onboarding detector
  // (admin_onboard.py::_detect_fallback_in_listing) byte-for-byte so the no-CI
  // merge gate runs the SAME command the backend recorded for the repo.
  // Invoked from BOTH the repo-root detection and the nested
  // services/apps/packages/src scan so the two sides stay at family parity.
  // First match wins; CONSERVATIVE — a runner is emitted only when the real
  // manifest is actually present. Returns null when no recognized extra-family
  // manifest is found. Defensive against fs errors (treated as "absent").
  const { existsSync: _exists, readdirSync: _readdir } = fs;
  const detectExtraFamilyRunner = (absDir: string, label: string): TestRunner | null => {
    const has = (name: string): boolean => {
      try { return _exists(`${absDir}/${name}`); } catch { return false; }
    };
    // Ruby: Gemfile + Rakefile → `bundle exec rake test`.
    if (has('Gemfile') && has('Rakefile')) {
      return { cmd: 'bundle', args: ['exec', 'rake', 'test'], cwd: absDir, label: `bundle exec rake test (${label})` };
    }
    // Gradle: build.gradle / build.gradle.kts → `./gradlew test`.
    if (has('build.gradle') || has('build.gradle.kts')) {
      return { cmd: './gradlew', args: ['test'], cwd: absDir, label: `./gradlew test (${label})` };
    }
    // Maven: pom.xml → `mvn test`.
    if (has('pom.xml')) {
      return { cmd: 'mvn', args: ['test'], cwd: absDir, label: `mvn test (${label})` };
    }
    // Elixir: mix.exs → `mix test`.
    if (has('mix.exs')) {
      return { cmd: 'mix', args: ['test'], cwd: absDir, label: `mix test (${label})` };
    }
    // .NET: any *.sln / *.csproj at this dir → `dotnet test`. The project file
    // name is arbitrary, so glob the directory listing (name-based, like the
    // backend's `n.endswith('.sln') or n.endswith('.csproj')`).
    let dotnetEntries: string[] = [];
    try {
      const entries = _readdir(absDir);
      if (Array.isArray(entries)) dotnetEntries = entries;
    } catch { /* unreadable — skip */ }
    if (dotnetEntries.some(n => typeof n === 'string' && (n.endsWith('.sln') || n.endsWith('.csproj')))) {
      return { cmd: 'dotnet', args: ['test'], cwd: absDir, label: `dotnet test (${label})` };
    }
    // PHP: phpunit.xml / phpunit.xml.dist → `vendor/bin/phpunit`.
    if (has('phpunit.xml') || has('phpunit.xml.dist')) {
      return { cmd: 'vendor/bin/phpunit', args: [], cwd: absDir, label: `vendor/bin/phpunit (${label})` };
    }
    // Composer: composer.json declaring a real `scripts.test` → `composer test`.
    if (has('composer.json')) {
      try {
        if (manifestDefinesTestScript(readFileSync(`${absDir}/composer.json`, 'utf-8'))) {
          return { cmd: 'composer', args: ['test'], cwd: absDir, label: `composer test (${label})` };
        }
      } catch { /* unreadable — skip */ }
    }
    // Swift Package Manager: Package.swift → `swift test`.
    if (has('Package.swift')) {
      return { cmd: 'swift', args: ['test'], cwd: absDir, label: `swift test (${label})` };
    }
    // Scala/sbt: build.sbt → `sbt test`.
    if (has('build.sbt')) {
      return { cmd: 'sbt', args: ['test'], cwd: absDir, label: `sbt test (${label})` };
    }
    // Bazel: WORKSPACE or WORKSPACE.bazel → `bazel test //...`.
    if (has('WORKSPACE') || has('WORKSPACE.bazel')) {
      return { cmd: 'bazel', args: ['test', '//...'], cwd: absDir, label: `bazel test //... (${label})` };
    }
    // Haskell/Cabal: any *.cabal file → `cabal test all`. Reuses dotnetEntries
    // (already-read directory listing) to glob by extension, like the .NET check above.
    if (dotnetEntries.some(n => typeof n === 'string' && n.endsWith('.cabal'))) {
      return { cmd: 'cabal', args: ['test', 'all'], cwd: absDir, label: `cabal test all (${label})` };
    }
    // Dart/Flutter: pubspec.yaml → `flutter test`.
    if (has('pubspec.yaml')) {
      return { cmd: 'flutter', args: ['test'], cwd: absDir, label: `flutter test (${label})` };
    }
    return null;
  };

  const detectGenericFallbackRunner = (absDir: string, label: string): TestRunner | null => {
    const has = (name: string): boolean => {
      try { return _exists(`${absDir}/${name}`); } catch { return false; }
    };
    const tryTextManifest = (name: string): boolean => {
      try {
        return textDefinesTestTarget(readFileSync(`${absDir}/${name}`, 'utf-8'));
      } catch {
        return false;
      }
    };
    const tryMakeManifest = (name: string): boolean => {
      try {
        return textDefinesMakeTestTarget(readFileSync(`${absDir}/${name}`, 'utf-8'));
      } catch {
        return false;
      }
    };
    // Makefile with a real top-level `test:` target → `make test`. Uses the
    // strict matcher (exact `test:`, no prefix/indent) so it agrees with the
    // backend onboarding detector; `make test` only runs an exact target.
    for (const makefileName of ['Makefile', 'makefile', 'GNUmakefile']) {
      if (has(makefileName) && tryMakeManifest(makefileName)) {
        return { cmd: 'make', args: ['test'], cwd: absDir, label: `make test (${label}/${makefileName})` };
      }
    }
    // just / Taskfile support mirrors the backend content-guarded fallback:
    // only emit when the manifest actually declares a runnable `test` target.
    for (const justfileName of ['Justfile', 'justfile']) {
      if (has(justfileName) && tryTextManifest(justfileName)) {
        return { cmd: 'just', args: ['test'], cwd: absDir, label: `just test (${label}/${justfileName})` };
      }
    }
    for (const taskfileName of ['Taskfile.yml', 'Taskfile.yaml']) {
      if (has(taskfileName) && tryTextManifest(taskfileName)) {
        return { cmd: 'task', args: ['test'], cwd: absDir, label: `task test (${label}/${taskfileName})` };
      }
    }
    if (has('tox.ini')) {
      return { cmd: 'tox', args: [], cwd: absDir, label: `tox (${label})` };
    }
    if (has('noxfile.py')) {
      return { cmd: 'nox', args: [], cwd: absDir, label: `nox (${label})` };
    }
    for (const script of ['test.sh', 'run-tests.sh', 'run_tests.sh', 'scripts/test.sh']) {
      if (has(script)) {
        return { cmd: 'bash', args: [script], cwd: absDir, label: `bash ${script} (${label})` };
      }
    }
    return null;
  };

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
      } else if (existsSync(pyprojectPath) && fileMentionsPytest(pyprojectPath)) {
        runners.push({ cmd: 'python3', args: ['-m', 'pytest', '-x', '-q'], cwd: WORKSPACE, label: 'pytest (root pyproject)' });
      } else if (existsSync(`${WORKSPACE}/setup.cfg`) && fileMentionsPytest(`${WORKSPACE}/setup.cfg`)) {
        runners.push({ cmd: 'python3', args: ['-m', 'pytest', '-x', '-q'], cwd: WORKSPACE, label: 'pytest (root setup.cfg)' });
      }
    }

    if (noRunners() && existsSync(cargoPath)) {
      runners.push({ cmd: 'cargo', args: ['test'], cwd: WORKSPACE, label: 'cargo test (root)' });
    }

    // Go at repo root: go.mod → `go test ./...`. Mirrors the backend core
    // detector, which recognizes go.mod at the repository root (the agent's
    // nested scan already covered go.mod, but root-level Go was missing).
    if (noRunners() && existsSync(`${WORKSPACE}/go.mod`)) {
      runners.push({ cmd: 'go', args: ['test', './...'], cwd: WORKSPACE, label: 'go test (root)' });
    }

    // Additional divergent families at repo root (Ruby/Gradle/Maven/Elixir/
    // .NET/PHP). Brings the agent runtime detector to family parity with the
    // backend onboarding detector so a standard repo in one of these ecosystems
    // runs its REAL suite instead of falling through to an UNVERIFIED merge.
    if (noRunners()) {
      const rootExtra = detectExtraFamilyRunner(WORKSPACE, 'root');
      if (rootExtra) runners.push(rootExtra);
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
        if (fileMentionsPytest(backendPyproject)) {
          if (existsSync(backendTestScript)) {
            runners.push({ cmd: 'bash', args: ['scripts/test-backend.sh', pgFlag], cwd: WORKSPACE, label: 'pytest (backend pyproject)' });
          } else {
            runners.push({ cmd: 'python3', args: ['-m', 'pytest', '-x', '-q'], cwd: `${WORKSPACE}/backend`, label: 'pytest (backend pyproject)' });
          }
        }
      } else if (existsSync(`${WORKSPACE}/backend/setup.cfg`) && fileMentionsPytest(`${WORKSPACE}/backend/setup.cfg`)) {
        if (existsSync(backendTestScript)) {
          runners.push({ cmd: 'bash', args: ['scripts/test-backend.sh', pgFlag], cwd: WORKSPACE, label: 'pytest (backend setup.cfg)' });
        } else {
          runners.push({ cmd: 'python3', args: ['-m', 'pytest', '-x', '-q'], cwd: `${WORKSPACE}/backend`, label: 'pytest (backend setup.cfg)' });
        }
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

    // --- Generic external-repo fallbacks (only if no Blueprint-specific or
    //     manifest-based runner was found above). These let an external no-CI
    //     repo whose stored build_config is empty still run its real suite.
    //     Ordering is deterministic: Makefile → just/Taskfile → tox.ini →
    //     noxfile.py → test scripts; the first match wins.
    if (noRunners()) {
      const genericRoot = detectGenericFallbackRunner(WORKSPACE, 'root');
      if (genericRoot) runners.push(genericRoot);
    }

    // --- Nested monorepo / external-layout detection (only if no root-level or
    //     Blueprint-specific runner was found above). Brings the agent runtime
    //     fallback to TRUE parity with the backend onboarding detector
    //     (admin_onboard.py::_detect_build_config), which BFS-walks EVERY
    //     subdirectory of the repo — not a fixed container allowlist. We mirror
    //     that with a generic depth-bounded directory walk (see below), so an
    //     external no-CI repo whose suites live under ANY top-level directory —
    //     services/, apps/, packages/ AND server/, web/, api/, app/ (singular),
    //     tools/, or a project-specific monorepo container name — runs its REAL
    //     suite instead of falling through to an UNVERIFIED merge. Inspects ONLY
    //     the already-cloned local workspace, so there is zero GitHub-API cost.
    //     CONSERVATIVE: a runner is emitted only when a real manifest/test
    //     entrypoint is actually present. Multiple sibling packages each emit
    //     their own runner.
    //
    //     NOTE: the Blueprint self-repo (root/backend/agent) always produces a
    //     runner above, so noRunners() is false here and this scan never fires
    //     for the self-repo — its detection result is byte-for-byte unchanged.
    if (noRunners()) {
      // Emit a runner for a single package directory (relative to WORKSPACE) if
      // it carries a recognized manifest. Mirrors the backend manifest→command
      // mapping: python→`python3 -m pytest`, node→`npm test`, go→`go test ./...`,
      // rust→`cargo test`. First match wins; no bare `pytest` is ever emitted
      // (honoring the C19 bare-pytest guard). Defensive against fs errors.
      const pushRunnerForPackageDir = (relPath: string): void => {
        const abs = `${WORKSPACE}/${relPath}`;
        try {
          // Python: pytest.ini, or pyproject.toml / setup.cfg that actually
          // mention pytest. A bare setup.py is a packaging signal only and
          // must not claim a runnable suite.
          if (existsSync(`${abs}/pytest.ini`)) {
            runners.push({ cmd: 'python3', args: ['-m', 'pytest', '-x', '-q'], cwd: abs, label: `pytest (${relPath})` });
            return;
          }
          if (existsSync(`${abs}/pyproject.toml`) && fileMentionsPytest(`${abs}/pyproject.toml`)) {
            runners.push({ cmd: 'python3', args: ['-m', 'pytest', '-x', '-q'], cwd: abs, label: `pytest (${relPath} pyproject)` });
            return;
          }
          if (existsSync(`${abs}/setup.cfg`) && fileMentionsPytest(`${abs}/setup.cfg`)) {
            runners.push({ cmd: 'python3', args: ['-m', 'pytest', '-x', '-q'], cwd: abs, label: `pytest (${relPath} setup.cfg)` });
            return;
          }
          // Node: package.json declaring a real `scripts.test` (conservative —
          // an absent test script must fall back, not block, per the no-CI gate).
          if (existsSync(`${abs}/package.json`)) {
            try {
              if (manifestDefinesTestScript(readFileSync(`${abs}/package.json`, 'utf-8'))) {
                runners.push({ cmd: 'npm', args: ['test'], cwd: abs, label: `npm test (${relPath})` });
                return;
              }
            } catch { /* malformed package.json — skip */ }
          }
          // Go: go.mod → `go test ./...`.
          if (existsSync(`${abs}/go.mod`)) {
            runners.push({ cmd: 'go', args: ['test', './...'], cwd: abs, label: `go test (${relPath})` });
            return;
          }
          // Rust: Cargo.toml → `cargo test`.
          if (existsSync(`${abs}/Cargo.toml`)) {
            runners.push({ cmd: 'cargo', args: ['test'], cwd: abs, label: `cargo test (${relPath})` });
            return;
          }
          // Divergent families (Ruby/Gradle/Maven/Elixir/.NET/PHP) — shared
          // mapping with the root detection above and the backend detector.
          const extra = detectExtraFamilyRunner(abs, relPath);
          if (extra) {
            runners.push(extra);
            return;
          }
          const generic = detectGenericFallbackRunner(abs, relPath);
          if (generic) {
            runners.push(generic);
            return;
          }
        } catch { /* defensive: any fs error — skip this package */ }
      };

      // Generic depth-bounded directory walk over the local workspace, mirroring
      // the backend onboarding BFS bounds (admin_onboard.py:
      // _BUILD_CONFIG_SCAN_MAX_DEPTH=2, _BUILD_CONFIG_SCAN_MAX_DIRECTORIES=40).
      // Every top-level directory is treated BOTH as a candidate package
      // directory (depth 1) AND as a container scanned one level deeper
      // (depth 2) — exactly the dirs the backend walks. This replaces the old
      // fixed allowlist (services/apps/packages/libs/modules/crates/cmd/pkg/src),
      // which silently missed suites under any other top-level dir.
      //
      // Well-known vendored / dependency / build directories that exist only in
      // the local clone (node_modules, vendor, .venv, venv, target, dist, build)
      // and every dotdir (.git, .github, .pytest_cache, …) are skipped so a large
      // external repo cannot blow up the scan or emit spurious runners. The scan
      // is bounded to SCAN_MAX_DIRECTORIES inspected package dirs to match the
      // backend directory cap. First-match-wins per dir; no bare `pytest` is ever
      // emitted (C19); every fs access is defensive.
      const SKIP_DIRS = new Set([
        'node_modules', 'vendor', '.venv', 'venv', 'target', 'dist', 'build',
      ]);
      const skipDir = (name: string): boolean => name.startsWith('.') || SKIP_DIRS.has(name);
      const SCAN_MAX_DIRECTORIES = 40;
      let inspectedDirs = 0;
      const inspectPackageDir = (relPath: string): void => {
        if (inspectedDirs >= SCAN_MAX_DIRECTORIES) return;
        inspectedDirs++;
        pushRunnerForPackageDir(relPath);
      };

      let topLevelEntries: string[];
      try {
        topLevelEntries = readdirSync(WORKSPACE);
      } catch { topLevelEntries = []; /* unreadable workspace — skip nested scan */ }
      if (Array.isArray(topLevelEntries)) {
        for (const entry of topLevelEntries) {
          if (typeof entry !== 'string' || skipDir(entry)) continue;
          if (inspectedDirs >= SCAN_MAX_DIRECTORIES) break;
          // depth 1: the top-level entry may itself be a package directory.
          inspectPackageDir(entry);
          // depth 2: scan its immediate children as package dirs too. A
          // top-level file (not a dir) makes readdirSync throw → caught → skip.
          let children: string[];
          try {
            children = readdirSync(`${WORKSPACE}/${entry}`);
          } catch { continue; /* not a directory / unreadable — skip */ }
          if (!Array.isArray(children)) continue;
          for (const child of children) {
            if (typeof child !== 'string' || skipDir(child)) continue;
            if (inspectedDirs >= SCAN_MAX_DIRECTORIES) break;
            inspectPackageDir(`${entry}/${child}`);
          }
        }
      }
    }
  }

  return runners;
}

/**
 * Auto-detect test runners against the cloned workspace ONLY, ignoring any
 * stored project build_config.
 *
 * This is exactly the auto-detection fallback path that `detectTestRunners`
 * runs when a stored build_config produces zero runners — surfaced as a named
 * entry point so the no-CI merge gate can ask "what would we detect if there
 * were no stored test command?" in order to self-heal a STALE stored command
 * (renamed/removed suite, dropped Makefile target, deleted npm `test` script).
 *
 * Inspects only the local filesystem (the already-cloned workspace), so there
 * is zero GitHub-API cost. `detectTestRunners` behaviour for every existing
 * caller is byte-for-byte unchanged — this is purely additive.
 */
export function detectAutoTestRunners(): TestRunner[] {
  return detectTestRunners(undefined);
}

// True when the directory contains any *.sln / *.csproj — the glob signal the
// .NET suite detector (detectExtraFamilyRunner) keys off. The project file name
// is arbitrary, so the directory listing is globbed name-based rather than
// checking a fixed manifest. Defensive against fs errors (treated as "absent").
function hasDotnetProject(absDir: string): boolean {
  try {
    const entries = fs.readdirSync(absDir);
    return Array.isArray(entries) && entries.some(
      n => typeof n === 'string' && (n.endsWith('.sln') || n.endsWith('.csproj')),
    );
  } catch {
    return false;
  }
}

// ── Dependency provisioning (no-CI verify gate) ──────────────────────────────
// Before the no-CI verify gate runs the detected suite OR the declared build
// probe, the project's dependencies must be installed — otherwise both fail to
// RUN for environment reasons and the gate falls open UNVERIFIED even though the
// code is fine. A provisioning step is a single install command run under a
// bounded timeout; a failure routes through the EXISTING setup-failure path.
export interface ProvisioningStep {
  cmd: string;
  args: string[];
  cwd: string;
  label: string;
  // True when this step runs the operator's OWN declared ``install_cmd`` rather
  // than an auto-detected fallback (currently the pyproject-only ``pip install .``).
  declared?: boolean;
}

/**
 * Detect dependency-provisioning steps the no-CI verify gate must run BEFORE the
 * detected suite and the declared build probe, so both execute against a
 * provisioned environment instead of a missing-dependency one.
 *
 * Two sources, in precedence order:
 *  1) The operator's declared ``install_cmd`` per build_config directory — the
 *     authoritative provisioning step. Wrapped exactly like ``test_cmd``
 *     (bash -c for shell operators, quote-aware tokenize otherwise) and run via
 *     execFile (no shell interpolation).
 *  2) A pyproject.toml-only Python project (no committed ``poetry.lock`` and no
 *     ``requirements.txt``) — the common modern layout the per-runner hardcoded
 *     installs (poetry.lock / requirements.txt) miss entirely. Provisioned with
 *     ``pip install .`` so the declared package + its dependencies are present
 *     before pytest collects. A directory that already declares its own
 *     ``install_cmd`` is NOT given this fallback (the operator's command wins).
 *  3) Extra-ecosystem manifests whose suite command does NOT self-install deps —
 *     Ruby (Gemfile → ``bundle install``), Gradle (build.gradle[.kts] →
 *     ``./gradlew dependencies``), Maven (pom.xml → ``mvn dependency:go-offline``),
 *     Elixir (mix.exs → ``mix deps.get``), PHP (composer.json → ``composer
 *     install``). Detected from the SAME signals the suite detector uses, skipped
 *     when the operator declared an install_cmd for that directory.
 *  4) Node/JS projects (package.json) — node_modules install previously fired
 *     ONLY in the per-runner suite loop and ONLY for a literal ``npm`` command,
 *     so a ``yarn``/``pnpm``/direct-``vitest`` suite and the cheap goal-level
 *     probes ran unprovisioned. Provisioned ONCE here per Node directory, with
 *     the committed lockfile selecting the package manager (package-lock.json →
 *     ``npm ci``, yarn.lock → ``yarn install --frozen-lockfile``, pnpm-lock.yaml
 *     → ``pnpm install --frozen-lockfile``; bare package.json → ``npm install``).
 *     Skipped when the operator declared an install_cmd for that directory.
 *
 * Deduped by ``cwd``; inspects ONLY the local filesystem (zero GitHub-API cost).
 */
export function detectProvisioningSteps(buildConfig?: BuildConfig): ProvisioningStep[] {
  const exists = (p: string): boolean => {
    try { return fs.existsSync(p); } catch { return false; }
  };
  const steps: ProvisioningStep[] = [];
  const seenCwds = new Set<string>();
  const add = (step: ProvisioningStep): void => {
    if (seenCwds.has(step.cwd)) return;
    seenCwds.add(step.cwd);
    steps.push(step);
  };

  // 1) Operator-declared install commands take precedence.
  const declaredCwds = new Set<string>();
  if (buildConfig?.directories) {
    for (const dir of buildConfig.directories) {
      const installCmd = (dir.install_cmd || '').trim();
      if (!installCmd) continue;
      const abs = dir.path ? `${WORKSPACE}/${dir.path}` : WORKSPACE;
      declaredCwds.add(abs);
      const runner = runnerFromTestCmd(installCmd, abs);
      add({ cmd: runner.cmd, args: runner.args, cwd: abs, label: `${installCmd} (${dir.path || 'root'})`, declared: true });
    }
  }

  // 2) pyproject-only Python auto-provision. Only when no lockfile/requirements.txt
  //    is present (those are handled by the per-runner installs) and the directory
  //    did not already declare its own install_cmd.
  const pyprojectOnly = (absDir: string): boolean =>
    exists(`${absDir}/pyproject.toml`) &&
    !exists(`${absDir}/poetry.lock`) &&
    !exists(`${absDir}/requirements.txt`);
  const addPyprojectAuto = (absDir: string, label: string): void => {
    if (declaredCwds.has(absDir) || !pyprojectOnly(absDir)) return;
    add({ cmd: 'pip', args: ['install', '.'], cwd: absDir, label: `pip install . (${label})` });
  };
  if (buildConfig?.directories) {
    for (const dir of buildConfig.directories) {
      if (dir.type !== 'python') continue;
      addPyprojectAuto(dir.path ? `${WORKSPACE}/${dir.path}` : WORKSPACE, dir.path || 'root');
    }
  }
  // Repo root covers the auto-detected (no build_config) case.
  addPyprojectAuto(WORKSPACE, 'root');

  // 2b) requirements.txt Python auto-provision. A project that declares its
  //     dependencies via requirements.txt (and common variants: requirements-dev.txt,
  //     requirements/base.txt) has no existing pre-provisioning path — the only
  //     requirements.txt install in the codebase fires inside the per-runner test-suite
  //     loop, which is never reached for the cheap goal-level probes or when the suite
  //     is absent/thin. Without this, the documented-command probe runs unprovisioned,
  //     hits an ImportError, and classifies as "could not run" — silently falling open.
  //     First matching requirements file wins; a dir with a declared install_cmd is
  //     skipped (operator's command wins); deduped by cwd like the other auto steps.
  const REQ_FILES = ['requirements.txt', 'requirements-dev.txt', 'requirements/base.txt'];
  const addRequirementsTxtAuto = (absDir: string, label: string): void => {
    if (declaredCwds.has(absDir)) return;
    for (const reqFile of REQ_FILES) {
      if (exists(`${absDir}/${reqFile}`)) {
        add({ cmd: 'pip', args: ['install', '-r', reqFile], cwd: absDir, label: `pip install -r ${reqFile} (${label})` });
        return;
      }
    }
  };
  if (buildConfig?.directories) {
    for (const dir of buildConfig.directories) {
      if (dir.type !== 'python') continue;
      addRequirementsTxtAuto(dir.path ? `${WORKSPACE}/${dir.path}` : WORKSPACE, dir.path || 'root');
    }
  }
  // Repo root covers the auto-detected (no build_config) case.
  addRequirementsTxtAuto(WORKSPACE, 'root');

  // 2c) Poetry Python auto-provision. A project with a committed poetry.lock
  //     must run `poetry install` before tests/probes to install dependencies
  //     AND the project's own package (with console-script entrypoints). Without
  //     this, the documented-command probe runs unprovisioned, hits missing-dependency
  //     or command-not-found, and classifies as "could not run" — silently falling open.
  //     Skipped when a declared install_cmd is present (operator's command wins);
  //     deduped by cwd like the other auto steps.
  const addPoetryAuto = (absDir: string, label: string): void => {
    if (declaredCwds.has(absDir)) return;
    if (!exists(`${absDir}/pyproject.toml`) || !exists(`${absDir}/poetry.lock`)) return;
    add({ cmd: 'poetry', args: ['install'], cwd: absDir, label: `poetry install (${label})` });
  };
  if (buildConfig?.directories) {
    for (const dir of buildConfig.directories) {
      if (dir.type !== 'python') continue;
      addPoetryAuto(dir.path ? `${WORKSPACE}/${dir.path}` : WORKSPACE, dir.path || 'root');
    }
  }
  // Repo root covers the auto-detected (no build_config) case.
  addPoetryAuto(WORKSPACE, 'root');

  // 2d) Pipenv Python auto-provision. A project with a Pipfile must run
  //     `pipenv install --system` before tests/probes to install dependencies
  //     into the ambient interpreter (not a virtualenv). Without this, the
  //     documented-command probe runs unprovisioned, hits missing-dependency or
  //     command-not-found, and classifies as "could not run" — silently falling
  //     open. Skipped when a declared install_cmd is present (operator's command
  //     wins); deduped by cwd like the other auto steps.
  const addPipenvAuto = (absDir: string, label: string): void => {
    if (declaredCwds.has(absDir)) return;
    if (!exists(`${absDir}/Pipfile`)) return;
    add({ cmd: 'pipenv', args: ['install', '--system'], cwd: absDir, label: `pipenv install --system (${label})` });
  };
  if (buildConfig?.directories) {
    for (const dir of buildConfig.directories) {
      if (dir.type !== 'python') continue;
      addPipenvAuto(dir.path ? `${WORKSPACE}/${dir.path}` : WORKSPACE, dir.path || 'root');
    }
  }
  // Repo root covers the auto-detected (no build_config) case.
  addPipenvAuto(WORKSPACE, 'root');

  // 2e) Bare setup.py/setup.cfg Python auto-provision. A project with setup.py or
  //     setup.cfg but NO pyproject.toml must run `pip install .` before tests/probes to
  //     install dependencies AND the project's own package (with console-script entrypoints).
  //     Without this, the documented-command probe runs unprovisioned, hits missing-dependency
  //     or command-not-found, and classifies as "could not run" — silently falling open.
  //     Skipped when a declared install_cmd or any OTHER provisioning step already fired
  //     (operator's command wins, deduped by cwd).
  const bareSetupPy = (absDir: string): boolean =>
    !exists(`${absDir}/pyproject.toml`) &&
    !exists(`${absDir}/requirements.txt`) &&
    !exists(`${absDir}/requirements-dev.txt`) &&
    !exists(`${absDir}/requirements/base.txt`) &&
    !exists(`${absDir}/poetry.lock`) &&
    !exists(`${absDir}/Pipfile`) &&
    (exists(`${absDir}/setup.py`) || exists(`${absDir}/setup.cfg`));
  const addBareSetupAuto = (absDir: string, label: string): void => {
    if (declaredCwds.has(absDir) || !bareSetupPy(absDir)) return;
    add({ cmd: 'pip', args: ['install', '.'], cwd: absDir, label: `pip install . (${label})` });
  };
  if (buildConfig?.directories) {
    for (const dir of buildConfig.directories) {
      if (dir.type !== 'python') continue;
      addBareSetupAuto(dir.path ? `${WORKSPACE}/${dir.path}` : WORKSPACE, dir.path || 'root');
    }
  }
  // Repo root covers the auto-detected (no build_config) case.
  addBareSetupAuto(WORKSPACE, 'root');

  // 3) Extra-ecosystem auto-provision (Ruby/Gradle/Maven/Elixir/PHP). The suite
  //    detector LAUNCHES these runners (detectExtraFamilyRunner) but their test
  //    commands (`bundle exec rake test`, `./gradlew test`, `mvn test`,
  //    `mix test`, `composer test`) do NOT install their own dependencies. Without
  //    a pre-install step the suite runs against an UNPROVISIONED workspace and
  //    fails on dependency resolution — which the env-failure classifier
  //    deliberately treats as a REAL red test (blocking the merge AND spawning a
  //    code-fix agent against code that isn't broken). Auto-provision from the
  //    SAME local manifest signals the suite detector keys off, skipping any dir
  //    that already declared its own install_cmd (the operator's command wins) and
  //    any cwd already provisioned above (deduped by `add`). First match wins per
  //    dir; mirrors detectExtraFamilyRunner's detection order. A provisioning
  //    FAILURE flows through the existing setup-failure path exactly like a failed
  //    declared install, pipenv/poetry install, or pip install — never a blocking test failure.
  const addExtraFamilyAuto = (absDir: string, label: string): void => {
    if (declaredCwds.has(absDir)) return;
    // Ruby: Gemfile → `bundle install`.
    if (exists(`${absDir}/Gemfile`)) {
      add({ cmd: 'bundle', args: ['install'], cwd: absDir, label: `bundle install (${label})` });
      return;
    }
    // Gradle: build.gradle / build.gradle.kts → prefetch dependencies so the
    // suite's first run does not fail on initial dependency download.
    if (exists(`${absDir}/build.gradle`) || exists(`${absDir}/build.gradle.kts`)) {
      add({ cmd: './gradlew', args: ['dependencies', '--no-daemon'], cwd: absDir, label: `./gradlew dependencies (${label})` });
      return;
    }
    // Maven: pom.xml → resolve all dependencies for an offline-ready clean run.
    if (exists(`${absDir}/pom.xml`)) {
      add({ cmd: 'mvn', args: ['-B', 'dependency:go-offline'], cwd: absDir, label: `mvn dependency:go-offline (${label})` });
      return;
    }
    // Elixir: mix.exs → `mix deps.get`.
    if (exists(`${absDir}/mix.exs`)) {
      add({ cmd: 'mix', args: ['deps.get'], cwd: absDir, label: `mix deps.get (${label})` });
      return;
    }
    // .NET: any *.sln / *.csproj at this dir → `dotnet restore`. The project file
    // name is arbitrary, so glob the directory listing (name-based, like the suite
    // detector's detectExtraFamilyRunner) rather than checking a fixed manifest.
    // Without a restore step the suite/probe run against an unprovisioned NuGet
    // cache and fail on first-run dependency resolution.
    if (hasDotnetProject(absDir)) {
      add({ cmd: 'dotnet', args: ['restore'], cwd: absDir, label: `dotnet restore (${label})` });
      return;
    }
    // PHP: composer.json → `composer install`.
    if (exists(`${absDir}/composer.json`)) {
      add({ cmd: 'composer', args: ['install'], cwd: absDir, label: `composer install (${label})` });
      return;
    }
    // Go: go.mod → `go mod download` (pre-fetch modules so the suite and
    // acceptance probe run provisioned). Without this, a repo with no unit suite
    // (or a thin/skipped suite) runs its `go build` probe unprovisioned and fails
    // on first-download, silently downgrading to could_not_run.
    if (exists(`${absDir}/go.mod`)) {
      add({ cmd: 'go', args: ['mod', 'download'], cwd: absDir, label: `go mod download (${label})` });
      return;
    }
    // Rust: Cargo.toml → `cargo fetch` (pre-fetch crates). Without this, a repo
    // with no unit suite (or a thin/skipped suite) runs its `cargo check` probe
    // unprovisioned and fails on first-download, silently downgrading to
    // could_not_run.
    if (exists(`${absDir}/Cargo.toml`)) {
      add({ cmd: 'cargo', args: ['fetch'], cwd: absDir, label: `cargo fetch (${label})` });
      return;
    }
    // Dart/Flutter: pubspec.yaml → `flutter pub get`. Swift/sbt/Bazel/Cabal
    // self-fetch on first run so they need no explicit pre-install step.
    if (exists(`${absDir}/pubspec.yaml`)) {
      add({ cmd: 'flutter', args: ['pub', 'get'], cwd: absDir, label: `flutter pub get (${label})` });
      return;
    }
  };
  if (buildConfig?.directories) {
    for (const dir of buildConfig.directories) {
      addExtraFamilyAuto(dir.path ? `${WORKSPACE}/${dir.path}` : WORKSPACE, dir.path || 'root');
    }
  }
  // Repo root covers the auto-detected (no build_config) case.
  addExtraFamilyAuto(WORKSPACE, 'root');

  // 4) Node/JS auto-provision. Node is the most common external-repo ecosystem,
  //    yet node_modules install previously lived ONLY in the per-runner suite
  //    loop and fired ONLY when the resolved test command was literally `npm`.
  //    So a suite invoked as `yarn test` / `pnpm test` / a direct `vitest`/`jest`
  //    never got deps, and the cheap goal-level probes (acceptance type-check +
  //    documented/PR-introduced command) ran with no node_modules and silently
  //    degraded to "could not run". Provision node_modules ONCE here so BOTH the
  //    suite (regardless of which test command was detected) AND the probes run
  //    against a provisioned environment. The committed lockfile selects the
  //    package manager exactly like the per-runner backstop in test-fix-loop.ts:
  //    yarn.lock → `yarn install --frozen-lockfile`, pnpm-lock.yaml →
  //    `pnpm install --frozen-lockfile`, package-lock.json → `npm ci`; a bare
  //    package.json with no lockfile falls back to `npm install` (npm ci REQUIRES
  //    a lockfile and would hard-fail). Skips any dir that declared its own
  //    install_cmd (operator's command wins) and any cwd already provisioned
  //    above (deduped by `add`). A provisioning FAILURE flows through the existing
  //    setup-failure path — never a blocking test failure. The per-runner npm
  //    fallback stays as a harmless idempotent backstop (it no-ops once
  //    node_modules is present).
  const addNodeAuto = (absDir: string, label: string): void => {
    if (declaredCwds.has(absDir)) return;
    if (!exists(`${absDir}/package.json`)) return;
    if (exists(`${absDir}/yarn.lock`)) {
      add({ cmd: 'yarn', args: ['install', '--frozen-lockfile'], cwd: absDir, label: `yarn install --frozen-lockfile (${label})` });
      return;
    }
    if (exists(`${absDir}/pnpm-lock.yaml`)) {
      add({ cmd: 'pnpm', args: ['install', '--frozen-lockfile'], cwd: absDir, label: `pnpm install --frozen-lockfile (${label})` });
      return;
    }
    if (exists(`${absDir}/package-lock.json`)) {
      add({ cmd: 'npm', args: ['ci'], cwd: absDir, label: `npm ci (${label})` });
      return;
    }
    // Bare package.json, no committed lockfile: npm ci would fail, so npm install.
    add({ cmd: 'npm', args: ['install'], cwd: absDir, label: `npm install (${label})` });
  };
  if (buildConfig?.directories) {
    for (const dir of buildConfig.directories) {
      addNodeAuto(dir.path ? `${WORKSPACE}/${dir.path}` : WORKSPACE, dir.path || 'root');
    }
  }
  // Repo root covers the auto-detected (no build_config) case.
  addNodeAuto(WORKSPACE, 'root');

  return steps;
}

// ── Cheap acceptance probe (no-CI merge gate goal-level check) ───────────────
// When the no-CI merge gate finds no unit suite (or the suite is thin / fully
// skipped), the PR ships with ZERO execution signal. The acceptance probe is a
// bounded, CPU-cheap, goal-level check of the most basic claim every code
// change makes: that the code still loads / compiles / builds. It is NOT an
// environment fixer (that is the greenfield setup-repair direction) and NOT a
// full build pipeline — it runs in seconds under the same bounded-timeout
// discipline the suite already uses. Language is detected from the SAME signals
// the suite detector uses.
export interface AcceptanceProbe {
  cmd: string;
  args: string[];
  cwd: string;
  // The ecosystem bucket the probe belongs to (used for dedup + the operator
  // display label). ``'build'`` is the catch-all for an operator-declared
  // ``build_cmd`` whose directory ``type`` is none of python/node/go/rust
  // (e.g. ``other``).
  ecosystem: 'python' | 'node' | 'go' | 'rust' | 'dotnet' | 'gradle' | 'maven' | 'elixir' | 'build';
  label: string;
  // True when this probe runs the project's OWN declared ``build_cmd`` (the
  // operator's authoritative "does this deliverable build" claim) rather than a
  // generic per-ecosystem compile check. A declared build is classified
  // authoritatively (a clean non-zero exit is a real FAILURE, see
  // ``classifyProbeOutcome``); generic probes stay conservative (fall-open).
  declared?: boolean;
}

/**
 * Compare two `git status --porcelain --ignored` snapshots (each the raw stdout
 * string of one observation) and report whether the AFTER snapshot contains at
 * least one status line not present BEFORE — i.e. the build wrote something
 * (a new untracked/ignored file, or a modification to a tracked one).
 *
 * Used by the no-CI merge gate's DECLARED build probe to tell a genuine build
 * apart from a no-op one (an empty Makefile recipe, a script that silently does
 * nothing) that exits 0 yet emits no artifact. A bare "tree is empty" check
 * would false-positive on pre-existing warm-VM cache directories (node_modules,
 * .vitest_cache, .pnpm-store, .testmondata) that survive `git clean -fdx` and
 * already show up as ignored entries; a before/after DIFF ignores those because
 * they appear identically in BOTH snapshots. Returns false only when AFTER
 * introduces no new line relative to BEFORE (the build wrote nothing new).
 */
export function buildWroteNewOutput(before: string, after: string): boolean {
  const lines = (s: string): Set<string> =>
    new Set(
      s
        .split('\n')
        .map(line => line.replace(/\r$/, ''))
        .filter(line => line.trim().length > 0),
    );
  const beforeLines = lines(before);
  for (const line of lines(after)) {
    if (!beforeLines.has(line)) return true;
  }
  return false;
}

// Hard cap on probes emitted so a large monorepo cannot fan out an unbounded
// set of probe processes. The handler additionally slices to a small per-run
// budget (see verify-tests.ts).
const ACCEPTANCE_PROBE_MAX = 6;

/**
 * Detect cheap, goal-level acceptance probes for the cloned workspace's
 * ecosystem(s). Mirrors the suite detector's manifest signals but emits a
 * *compile/build* command rather than a test command:
 *
 *   - Python → ``python3 -m compileall`` (byte-compile; never imports, so a
 *     missing runtime dependency cannot make it fail — only a real SyntaxError
 *     does). Vendored/build trees are excluded so a large repo stays bounded.
 *   - Node/TS → ``npx --no-install tsc --noEmit`` (no-emit type-check), emitted
 *     ONLY when a tsconfig.json is present. ``--no-install`` fails fast
 *     (could-not-run) when tsc is absent instead of triggering a network
 *     install. A plain-JS package has nothing cheap+safe to "compile" (requiring
 *     its entrypoint could boot a server) so none is emitted.
 *   - Go → ``go build ./...`` (compiles every package without running tests).
 *   - Rust → ``cargo check`` (type/borrow-check without producing a binary).
 *
 * CONSERVATIVE + bounded: probes are deduped by ``ecosystem:cwd``, capped at
 * ``ACCEPTANCE_PROBE_MAX``, and the auto-scan is depth-1 with the same
 * vendored/dot-dir skip list as the suite detector. Inspects ONLY the local
 * filesystem (zero GitHub-API cost).
 */
export function detectAcceptanceProbes(buildConfig?: BuildConfig): AcceptanceProbe[] {
  const { existsSync, readdirSync } = fs;
  const probes: AcceptanceProbe[] = [];
  const seen = new Set<string>();
  // Directories whose own declared ``build_cmd`` ran as the authoritative probe.
  // The generic ecosystem probe(s) and the depth-1 auto-scan are suppressed for
  // these cwds so the operator's declared build is the preferred goal-level check.
  const declaredCwds = new Set<string>();

  const has = (absDir: string, name: string): boolean => {
    try { return existsSync(`${absDir}/${name}`); } catch { return false; }
  };
  const add = (probe: AcceptanceProbe): void => {
    if (probes.length >= ACCEPTANCE_PROBE_MAX) return;
    const key = `${probe.ecosystem}:${probe.cwd}`;
    if (seen.has(key)) return;
    seen.add(key);
    probes.push(probe);
  };

  // Map a build_config directory ``type`` to its probe ecosystem bucket. A type
  // with no dedicated language bucket (``other`` / unknown) collapses to the
  // ``'build'`` catch-all — the probe still runs the declared command verbatim.
  const ecosystemForType = (type?: string): AcceptanceProbe['ecosystem'] => {
    switch (type) {
      case 'python': return 'python';
      case 'node': return 'node';
      case 'go': return 'go';
      case 'rust': return 'rust';
      default: return 'build';
    }
  };

  // 0) Operator-declared build commands take precedence. Any build_config
  //    directory that declares its own ``build_cmd`` runs THAT as the
  //    authoritative goal-level probe (the operator's "does this deliverable
  //    build" claim), and suppresses the generic ecosystem probe(s) for the same
  //    directory. Commands may carry shell features (`&&`, `--features x`,
  //    quotes) so they are wrapped exactly like ``test_cmd`` (bash -c for shell
  //    operators, quote-aware tokenize otherwise) and run via execFile — no shell
  //    interpolation.
  if (buildConfig?.directories) {
    for (const dir of buildConfig.directories) {
      const buildCmd = (effectiveBuildCmd(dir) || '').trim();
      if (!buildCmd) continue;
      const abs = dir.path ? `${WORKSPACE}/${dir.path}` : WORKSPACE;
      const runner = runnerFromTestCmd(buildCmd, abs);
      add({
        ecosystem: ecosystemForType(dir.type),
        cmd: runner.cmd,
        args: runner.args,
        cwd: abs,
        label: `${buildCmd} (${dir.path || 'root'})`,
        declared: true,
      });
      declaredCwds.add(abs);
    }
  }

  // Emit every ecosystem probe a directory supports. ``typeHint`` is the stored
  // build_config directory ``type`` (python|node|go|rust|other) when available.
  const probeDir = (absDir: string, label: string, typeHint?: string): void => {
    // A directory with its own declared build_cmd already emitted the
    // authoritative probe above — never add a competing generic probe for it.
    if (declaredCwds.has(absDir)) return;
    if (
      typeHint === 'python' ||
      has(absDir, 'pyproject.toml') || has(absDir, 'setup.py') ||
      has(absDir, 'setup.cfg') || has(absDir, 'pytest.ini')
    ) {
      add({
        ecosystem: 'python',
        cmd: 'python3',
        // -x EXCLUDES vendored/build trees (regex over the file path). Passed as
        // a single argv element via execFile (no shell), so no escape pass.
        args: ['-m', 'compileall', '-q', '-x', '(\\.venv|venv|node_modules|\\.git|build|dist)', absDir],
        cwd: absDir,
        label: `python3 -m compileall (${label})`,
      });
    }
    if (has(absDir, 'tsconfig.json')) {
      add({
        ecosystem: 'node',
        cmd: 'npx',
        args: ['--no-install', 'tsc', '--noEmit'],
        cwd: absDir,
        label: `tsc --noEmit (${label})`,
      });
    }
    if (has(absDir, 'go.mod')) {
      add({
        ecosystem: 'go',
        cmd: 'go',
        args: ['build', './...'],
        cwd: absDir,
        label: `go build ./... (${label})`,
      });
    }
    if (has(absDir, 'Cargo.toml')) {
      add({
        ecosystem: 'rust',
        cmd: 'cargo',
        args: ['check', '--quiet'],
        cwd: absDir,
        label: `cargo check (${label})`,
      });
    }
    // .NET → `dotnet build` (compiles every project without running tests).
    // Glob-detected (*.sln/*.csproj) like the suite detector, since the project
    // file name is arbitrary. `--nologo` keeps output terse for the classifier.
    if (hasDotnetProject(absDir)) {
      add({
        ecosystem: 'dotnet',
        cmd: 'dotnet',
        args: ['build', '--nologo'],
        cwd: absDir,
        label: `dotnet build (${label})`,
      });
    }
    // Compiled extra-family ecosystems the suite detector also launches. Each
    // runs the CHEAPEST compile-only task (compile/classes, not a full build or
    // test run), keyed off the same manifest the suite detector + provisioning
    // use. A genuine compile error classifies as a real FAILURE; a missing
    // toolchain / unprovisioned deps degrades to could_not_run (fall-open).
    //
    // Gradle: build.gradle[.kts] → `./gradlew classes` (compile main sources,
    // no tests, no daemon — matches the provisioning step's `--no-daemon`).
    if (has(absDir, 'build.gradle') || has(absDir, 'build.gradle.kts')) {
      add({
        ecosystem: 'gradle',
        cmd: './gradlew',
        args: ['classes', '--no-daemon'],
        cwd: absDir,
        label: `gradlew classes (${label})`,
      });
    }
    // Maven: pom.xml → `mvn -o -B test-compile` (offline compile of main+test
    // sources; -o never hits the network so unfetched deps fall open, not block).
    if (has(absDir, 'pom.xml')) {
      add({
        ecosystem: 'maven',
        cmd: 'mvn',
        args: ['-o', '-B', 'test-compile'],
        cwd: absDir,
        label: `mvn -o test-compile (${label})`,
      });
    }
    // Elixir: mix.exs → `mix compile` (compiles the project without running the
    // ExUnit suite).
    if (has(absDir, 'mix.exs')) {
      add({
        ecosystem: 'elixir',
        cmd: 'mix',
        args: ['compile'],
        cwd: absDir,
        label: `mix compile (${label})`,
      });
    }
  };

  // 1) Stored build_config directories carry an explicit language type hint.
  if (buildConfig?.directories) {
    for (const dir of buildConfig.directories) {
      const abs = dir.path ? `${WORKSPACE}/${dir.path}` : WORKSPACE;
      probeDir(abs, dir.path || 'root', dir.type);
    }
  }

  // 2) Auto-detect at the repo root + one level of subdirectories so an external
  //    repo with no stored build_config still gets a probe. Bounded depth-1,
  //    capped, skipping vendored/dot dirs.
  probeDir(WORKSPACE, 'root');
  const SKIP_DIRS = new Set(['node_modules', 'vendor', '.venv', 'venv', 'target', 'dist', 'build']);
  const skip = (name: string): boolean => name.startsWith('.') || SKIP_DIRS.has(name);
  let entries: string[] = [];
  try { entries = readdirSync(WORKSPACE); } catch { entries = []; }
  if (Array.isArray(entries)) {
    let inspected = 0;
    for (const entry of entries) {
      if (typeof entry !== 'string' || skip(entry)) continue;
      if (inspected >= 40 || probes.length >= ACCEPTANCE_PROBE_MAX) break;
      inspected++;
      probeDir(`${WORKSPACE}/${entry}`, entry);
    }
  }

  return probes;
}

/**
 * Classify an acceptance-probe run into ``pass`` | ``fail`` | ``could_not_run``.
 *
 * ``exitErr`` is null when the probe exited 0 (``pass``). Otherwise it is the
 * rejected ``execFileAsync`` error (carrying ``.code`` / ``.stdout`` / etc.).
 *
 * CONSERVATIVE BY DESIGN — to avoid false merge blocks, a GENERIC probe's
 * non-zero exit maps to ``fail`` ONLY on an unambiguous compile/build-error
 * signature. Spawn/timeout failures, missing-tool / missing-dependency /
 * unprovisioned-setup signatures, and any unrecognized non-zero exit all degrade
 * to ``could_not_run`` so the gate falls open rather than hard-blocking.
 *
 * ``declared`` widens step (3) ONLY: when the probe runs the project's OWN
 * declared ``build_cmd``, the operator declared it authoritative, so a CLEAN
 * non-zero exit (NOT one of the missing-tool/dep/setup signatures and NOT a
 * spawn/timeout) is a REAL build FAILURE → ``fail`` (block the merge). The
 * missing-tool/dep/setup degrades (step 1) and the spawn/timeout degrades still
 * map to ``could_not_run`` for declared commands too — a build that needs
 * uninstalled deps must fall open, not block. Generic-probe semantics
 * (``declared=false``, the default) are byte-for-byte unchanged.
 */
export function classifyProbeOutcome(
  output: string,
  exitErr: (Error & { code?: string; status?: number | null }) | null,
  declared = false,
): 'pass' | 'fail' | 'could_not_run' {
  if (exitErr == null) return 'pass';
  // Spawn / timeout / OOM — the probe could not execute to a verdict.
  const code = exitErr.code;
  if (code === 'ENOENT' || code === 'ETIMEDOUT' || code === 'EOOM') return 'could_not_run';
  const lower = `${output}\n${exitErr.message}`.toLowerCase();

  // (1) Missing-tool / missing-dependency / unprovisioned-setup signatures →
  //     could_not_run (degrade to fall-open; NEVER hard-block on these). Checked
  //     BEFORE the failure signatures so e.g. a tsc "Cannot find module" run
  //     (TS2307, prints `error TS…`) is treated as unprovisioned deps, not a
  //     real type failure.
  const couldNotRunSignatures = [
    'command not found',
    'could not determine executable',
    'is not recognized as an internal or external',
    'permission denied',
    'no module named',
    'modulenotfounderror',
    'cannot find module',
    'cannot find type definition file',
    'cannot find package',
    'no required module provides package',
    'missing go.sum entry',
    'go: downloading',
    'failed to load source',
    'no matching package named',
    'failed to download',
    'spurious network error',
    'could not find `cargo.toml`',
  ];
  if (couldNotRunSignatures.some(sig => lower.includes(sig))) return 'could_not_run';

  // (2) Unambiguous compile/build FAILURE signatures → fail (block the gate).
  const failureSignatures = [
    'syntaxerror',        // python compileall
    'indentationerror',
    'taberror',
    'error ts',           // tsc: "error TS2322: ..."
    'error[e',            // rustc: "error[E0425]: ..."
    'undefined:',         // go build: "undefined: Foo"
    'error cs',           // C# (dotnet build): "error CS0103: ..."
    'compilation error',  // Maven javac / Gradle-Kotlin: "COMPILATION ERROR"
    'compilation failed', // Gradle javac: "Compilation failed; see the compiler error output"
    'compileerror',       // Elixir: "** (CompileError) ..."
  ];
  if (failureSignatures.some(sig => lower.includes(sig))) return 'fail';

  // (3) Non-zero exit we cannot positively attribute to a clean compile error.
  //     - GENERIC probe: CONSERVATIVE default could_not_run, so an unrecognized
  //       environment failure never hard-blocks a merge.
  //     - DECLARED build_cmd: the operator declared this build authoritative, so
  //       a clean non-zero exit (not a missing-tool/dep/setup or spawn/timeout
  //       degrade above) is a REAL build FAILURE → fail (block the merge).
  return declared ? 'fail' : 'could_not_run';
}

/**
 * Map a detected `TestRunner` back to a project build_config directory entry
 * (repo-root-relative path, language type, reconstructed test command) so the
 * backend can converge a stale stored config onto the suite that actually ran.
 * Used to populate `recovered_directories` on the MERGE_TEST_RESULT verdict.
 */
export function describeRunnerDirectory(runner: TestRunner): { path: string; type: string; test_cmd: string } {
  // Relative path from repo root: strip the WORKSPACE prefix; root → ''.
  let path = runner.cwd;
  if (path === WORKSPACE) {
    path = '';
  } else if (path.startsWith(`${WORKSPACE}/`)) {
    path = path.slice(WORKSPACE.length + 1);
  }
  // A `bash -c '<cmd>'` runner wraps an operator-configured command that needed
  // shell features (see runnerFromTestCmd). Reconstruct the ORIGINAL command so
  // a recovered directory entry round-trips back to what the operator wrote,
  // not the `bash -c …` wrapper. runnerKindFromCmd then keys off the unwrapped
  // first token instead of collapsing every wrapped suite to 'other'.
  if (runner.cmd === 'bash' && runner.args.length === 2 && runner.args[0] === '-c') {
    const original = runner.args[1];
    const firstToken = tokenizeCommand(original)[0] ?? original;
    return { path, type: runnerKindFromCmd(firstToken), test_cmd: original };
  }
  return { path, type: runnerKindFromCmd(runner.cmd), test_cmd: [runner.cmd, ...runner.args].join(' ') };
}

/** Derive a coarse language/runner kind from a runner's command.
 *
 *  The returned value MUST stay within the BuildConfigDirectoryEntry.type enum
 *  (`^(python|node|go|rust|other)$` in backend/app/schemas.py) — a recovered
 *  directory entry is appended verbatim to build_config and re-validated by the
 *  backend (and by a later operator PATCH /build_config), so an out-of-enum
 *  value (make/shell/unknown) would fail BuildConfigSchema validation and force
 *  the raw-store fallback, persisting a schema-invalid config. Anything without
 *  a dedicated language bucket (make targets, `bash`/`sh` script suites, or an
 *  unrecognized command) collapses to the catch-all `other`. */
function runnerKindFromCmd(cmd: string): string {
  switch (cmd) {
    case 'python3':
    case 'python':
    case 'pytest':
    case 'tox':
    case 'nox':
      return 'python';
    case 'npm':
    case 'yarn':
    case 'pnpm':
      return 'node';
    case 'go':
      return 'go';
    case 'cargo':
      return 'rust';
    default:
      // make targets, bash/sh script suites, and any unrecognized command have
      // no dedicated language bucket — map to the schema-valid catch-all.
      return 'other';
  }
}

/**
 * Detect environment/infrastructure failure signatures in test runner output.
 *
 * Returns true when the output shows a setup or infrastructure failure —
 * missing shared library, missing system runtime, spawn ENOENT, the dash/sh
 * "<tool>: not found" missing-executable phrasing, the Docker daemon being
 * unavailable on the gate VM, or an out-of-memory kill (exit 137 / SIGKILL /
 * signal 9) — rather than a genuine test failure. Repo dependency / packaging
 * regressions are
 * intentionally EXCLUDED here: a bare `ModuleNotFoundError`, `Cannot find
 * module`, or missing Go package/module line from the suite itself must stay on
 * the red-suite lane so the no-CI gate blocks broken repo changes instead of
 * downgrading them to setupFailed/UNVERIFIED. CONSERVATIVE: returns false
 * whenever real test failure evidence is present (FAILED/error-count lines), so
 * a test that quotes an env phrase as assertion data stays a real run.
 * Patterns are checked per-line so a phrase mid-line in an assertion diff never
 * triggers a misroute.
 *
 * Mirrors backend/app/failure_layer_classifier.py _AGENT_VM_PATTERNS — keep
 * the two in sync. The cross-language contract is pinned by the CI source-scan
 * in backend/testing/test_invariants.py.
 */
export function matchesEnvironmentFailureSignature(output: string): boolean {
  if (!output) return false;
  const trimmedLines = output.split(/\r?\n/).map(line => line.trim());
  // Real-failure evidence guard — same logic as classifyTestExecution's
  // hasRealFailureEvidence. When a FAILED/error-count line is present the env
  // phrase was quoted inside assertion data, not an infrastructure error.
  const hasRealFailureEvidence = trimmedLines.some(line => (
    /^FAILED\s+\S+/.test(line) ||
    /^FAIL\s{2,}\S+/.test(line) ||
    /^[✕✗×⨯]\s+\S+/.test(line) ||
    /\b[1-9]\d*\s+failed\b/i.test(line) ||
    /\b[1-9]\d*\s+failures?\b/i.test(line) ||
    /^--- FAIL:/.test(line) ||
    /^FAIL\t\S/.test(line) ||
    /\bFailed:\s+[1-9]\d*\b/i.test(line) ||
    /\bFailures:\s+[1-9]\d*\b/i.test(line) ||
    /\bErrors:\s+[1-9]\d*\b/i.test(line) ||
    /\b[1-9]\d*\s+errors?\b/i.test(line)
  ));
  if (hasRealFailureEvidence) return false;
  // Environment failure patterns — checked per-line.
  // Mirrors _AGENT_VM_PATTERNS in backend/app/failure_layer_classifier.py.
  return trimmedLines.some(line => (
    // Shared-library load errors (.so not installed on the gate VM)
    /error while loading shared librar/i.test(line) ||
    /cannot open shared object file/i.test(line) ||
    /no such file or directory.*\.so\b/i.test(line) ||
    /libatk[-\w]*\.so/i.test(line) ||
    /missing.*system.*librar/i.test(line) ||
    // Spawn failure: binary not found (OS could not exec the process)
    /\bspawn\b.+\bENOENT\b/i.test(line) ||
    // System runtimes not on PATH (VM-level, not test-runner-level)
    /\bnode:\s+command not found/i.test(line) ||
    /\bpython3?:\s+command not found/i.test(line) ||
    // dash/sh phrase the missing-executable diagnostic as "<tool>: not found"
    // (Debian's /bin/sh is dash). Anchored to the shell prefix so it can only
    // be a shell missing-binary error, never a repo dependency error
    // ("No module named …") or a test assertion that merely contains the words.
    /^(?:\/(?:usr\/)?bin\/)?(?:sh|bash|dash|ash):\s*(?:\d+:\s*)?\S+:\s+not found\s*$/i.test(line) ||
    // Docker daemon unavailable on the gate VM — repos whose test command shells
    // out to Docker cannot run, but that is the VM's missing daemon, not the code.
    /cannot connect to the docker daemon/i.test(line) ||
    /is the docker daemon running/i.test(line) ||
    /docker daemon is not running/i.test(line) ||
    // Out-of-memory kills: the OOM killer reaps the runner (exit 137 / signal 9 /
    // SIGKILL) with no test-failure summary. Surface forms vary per ecosystem;
    // all are VM resource exhaustion, never a repo assertion failure.
    /^killed\s*$/i.test(line) ||
    /\bsignal:\s*killed\b/i.test(line) ||
    /\bsigkill\b/i.test(line) ||
    /\bsignal 9\b/i.test(line) ||
    /\b(?:exit(?:ed with)?|exit code|exit status|code|status)\s+137\b/i.test(line) ||
    /\boomkilled\b/i.test(line) ||
    /out of memory:\s*kill/i.test(line) ||
    /javascript heap out of memory/i.test(line) ||
    /java\.lang\.outofmemoryerror/i.test(line) ||
    /cannot allocate memory/i.test(line) ||
    // Disk/quota/space failures (host resource exhaustion on the gate VM)
    /no space left on device/i.test(line) ||
    /disk quota exceeded/i.test(line) ||
    /filesystem (is )?full/i.test(line) ||
    /enospc/i.test(line) ||
    // Permission denied / access failures (read-only fs, insufficient perms)
    /permission denied/i.test(line) ||
    /eacces/i.test(line) ||
    /read-only file system/i.test(line) ||
    /erofs/i.test(line)
  ));
}

/**
 * Detect bootstrap/setup failures that mean the verify VM is missing the tool
 * needed to even attempt dependency installation or suite startup.
 *
 * This is deliberately narrower than repo dependency resolution: missing `pip`,
 * `npm`, `poetry`, or the language runtime itself is infrastructure, while a
 * missing repo package (`sqlalchemy`, `typescript`, a Go module) is a genuine
 * project failure that must block the gate.
 */
export function classifySetupFailure(output: string): boolean {
  if (!output) return false;
  const trimmedLines = output.split(/\r?\n/).map(line => line.trim());
  const missingToolPatterns = [
    /^(?:bash|sh):(?:\s+line\s+\d+:)?\s*(?:python(?:3)?|go|cargo|node|npm|pnpm|yarn|pip|poetry):\s+command not found\s*$/i,
    /^(?:python(?:3)?|go|cargo|node|npm|pnpm|yarn|pip|poetry):\s+command not found\s*$/i,
    /^zsh:\s+command not found:\s+(?:python(?:3)?|go|cargo|node|npm|pnpm|yarn|pip|poetry)\s*$/i,
    /^ModuleNotFoundError:\s+No module named ['"]pip['"]\s*$/i,
    /^ImportError:\s+No module named ['"]pip['"]\s*$/i,
  ];
  return trimmedLines.some(line => missingToolPatterns.some(pattern => pattern.test(line)));
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
 * Recognizes missing-script signatures (package.json has no test script, or a
 * target does not exist) as a no-tests case so the gate falls back to
 * proceed-without-checks rather than blocking the merge on an invocation error.
 *
 * Returns 'all_skipped' for a run that COLLECTED tests but executed NONE
 * because every test was skipped / deselected / ignored / filtered — the same
 * honest no-real-tests-executed case as 'no_tests' (callers treat both as "not
 * a real run"), but distinguished so the verdict can surface "suite present but
 * every test skipped" versus "no detectable suite / zero collection".
 *
 * CONSERVATIVE BY DESIGN: returns 'no_tests'/'all_skipped' ONLY on a positive
 * signature match. Anything else — including a genuine failure like "1 failed"
 * or "2 passed", empty output, or an unrecognized format — defaults to 'ran' so
 * that a real verdict is never misread as "no tests".
 */
export function classifyTestExecution(output: string): 'ran' | 'no_tests' | 'all_skipped' {
  const lower = output.toLowerCase();
  const lines = output.split(/\r?\n/);
  const trimmedLines = lines.map(line => line.trim());
  // npm placeholder: the default `package.json` test script prints
  // "Error: no test specified" and exits 1 without running anything. Anchor to
  // the runner-emitted line shape so a real suite that merely asserts on the
  // literal "Error: no test specified" as data (e.g. `AssertionError: expected
  // 'Error: no test specified' in output`) stays a real run.
  // The runner detection (detectExtraFamilyRunner + the root/nested scans) can
  // launch MANY ecosystems beyond pytest/vitest/Rust — Go, Ruby/Rake, Gradle,
  // Maven, Elixir, .NET, PHPUnit. Each reports a real failure with its own
  // summary shape, and the guard MUST recognize every one of them: a genuine
  // failure that ALSO quotes a placeholder-looking line (a captured child
  // process's "command not found", an assertion diff printing "no test
  // specified", a sub-invocation's "no tests ran in 0.01s") would otherwise be
  // misread as "no tests ran" and merged UNVERIFIED. Treat "a family the gate
  // can launch" and "its failure signature is recognized" as ONE invariant
  // (mirrored by the CI-enforced source-scan in test_invariants.py). Keep every
  // signal LINE-ANCHORED and, for count summaries, REQUIRE a non-zero count so a
  // zero-count summary ("0 failures", "Failed: 0", "Failures: 0") is never
  // mistaken for a failure.
  const hasRealFailureEvidence = trimmedLines.some(line => (
    /^FAILED\s+\S+/.test(line) ||
    /^FAIL\s{2,}\S+/.test(line) ||
    /^[✕✗×⨯]\s+\S+/.test(line) ||
    /^AssertionError:/i.test(line) ||
    /^Traceback \(most recent call last\):/i.test(line) ||
    /\b[1-9]\d*\s+failed\b/i.test(line) ||
    /\b[1-9]\d*\s+errors?\b/i.test(line) ||
    /\btest result:\s+ok\.\s+[1-9]\d*\s+passed;\s+[1-9]\d*\s+failed\b/i.test(line) ||
    // Go: verbose per-test failure marker and the tab-separated package-fail
    // summary line ("FAIL\texample.com/pkg\t0.03s"). The bare-2-space FAIL rule
    // above does not match the tab form.
    /^--- FAIL:/.test(line) ||
    /^FAIL\t\S/.test(line) ||
    // .NET (`dotnet test`): "Failed!  - Failed:     N, Passed: ..." — non-zero.
    /\bFailed:\s+[1-9]\d*\b/i.test(line) ||
    // Elixir/Ruby/RSpec count summaries: "1 test, 1 failure",
    // "3 examples, 1 failure", "1 runs, .., 1 failures, .." — non-zero.
    /\b[1-9]\d*\s+failures?\b/i.test(line) ||
    // PHPUnit/Maven: "Tests: .., Assertions: .., Failures: N" /
    // "Tests run: .., Failures: N, .." — non-zero.
    /\bFailures:\s+[1-9]\d*\b/i.test(line) ||
    // ERROR-outcome counts (a test ERROR — an exception/setup/collection error —
    // is reported with a count SEPARATE from assertion FAILURES). A run whose
    // only failure signal is a non-zero error count is still real-failure
    // evidence and must not fall open to no_tests.
    // Maven/Gradle/JUnit/PHPUnit: "Tests run: X, Failures: 0, Errors: N" — non-zero.
    /\bErrors:\s+[1-9]\d*\b/i.test(line)
  ));
  const noTestSpecified = /^(?:npm (?:ERR!|error)\s+)?error:\s*no test specified\s*$/i;
  if (!hasRealFailureEvidence && trimmedLines.some(line => noTestSpecified.test(line))) return 'no_tests';
  // npm/yarn/pnpm: missing test script in package.json.
  //   npm: 'Error: Missing script: "test"' or 'npm ERR! Missing script: "test"'
  //   yarn: 'error Command "test" not found.'  (handled by missingCommand below)
  //   pnpm: 'ERR!  No matching version found for test'
  // Anchor to the runner-emitted line shape (`Error:` / `npm ERR!` at the
  // front, plus the quoted script name) so a failing test that quotes the full
  // diagnostic line as assertion data stays a real run.
  const missingScript = /^(?:(?:npm (?:ERR!|error)\s+)?(?:error:\s*)?)missing script:\s*["']test["'](?:\s|$)/i;
  if (!hasRealFailureEvidence && trimmedLines.some(line => missingScript.test(line))) return 'no_tests';
  // Shell / package-manager missing-runner messages put the contiguous phrase
  // "command not found" in specific runner-shaped lines:
  //   bash/sh: "bash: pytest: command not found"
  //   zsh:     "zsh: command not found: pytest"
  //   pnpm:    "error: command not found: test"
  //   yarn:    'error Command "test" not found.'
  // Match only those line shapes so a failing suite that QUOTES the literal
  // phrase "command not found" (or that merely contains `command` and `not
  // found` elsewhere on the line) stays a real run.
  // Runtime commands (`python3`, `go`, `cargo`, `node`) are EXCLUDED here: a
  // missing runtime is a setup failure, not a placeholder/no-tests signal.
  const missingCommandPatterns = [
    /^(?:bash|sh):\s+(?!python(?:3)?\b|go\b|cargo\b|node\b)\S+:\s+command not found\s*$/i,
    /^zsh:\s+command not found:\s+(?!python(?:3)?\b|go\b|cargo\b|node\b)\S+\s*$/i,
    /^error:\s+command not found:\s+(?!python(?:3)?\b|go\b|cargo\b|node\b)\S+\s*$/i,
    /^error\s+command\s+["'][^"']+["']\s+not\s+found\.?\s*$/i,
  ];
  if (!hasRealFailureEvidence && trimmedLines.some(line => missingCommandPatterns.some(pattern => pattern.test(line)))) return 'no_tests';
  // make: "make: *** No rule to make target 'test'". Anchor to the make
  // diagnostic prefix (`make:`/`make[N]:` + `***`) so a test asserting on the
  // bare phrase "No rule to make target" as data stays a real run.
  const noRuleToMake = /^make(?:\[\d+\])?:\s*\*\*\*\s*no rule to make target\b/i;
  if (!hasRealFailureEvidence && trimmedLines.some(line => noRuleToMake.test(line))) return 'no_tests';
  // pytest: the summary line "no tests ran in 0.01s". Anchor to the
  // `in <duration>s` summary form so a test quoting the bare phrase "no tests
  // ran" as assertion data stays a real run.
  const pytestNoTestsRan = /^no tests ran in\s+[\d.]+\s*s\s*$/i;
  if (!hasRealFailureEvidence && trimmedLines.some(line => pytestNoTestsRan.test(line))) return 'no_tests';
  // pytest: a line-leading "collected 0 items" header. Line-anchored so an
  // assertion quoting the phrase mid-line stays a real run.
  if (!hasRealFailureEvidence && trimmedLines.some(line => /^collected 0 items\b/i.test(line))) return 'no_tests';
  // vitest: "No test files found, exiting with code 1". Line-anchored so an
  // assertion quoting the bare phrase "no test files" mid-line stays a real run.
  if (!hasRealFailureEvidence && trimmedLines.some(line => /^no test files found\b/i.test(line))) return 'no_tests';
  // ── All-skipped / fully-skipped run (collected tests but executed NONE) ──
  // A suite that COLLECTED tests but ran NONE of them because every test was
  // skipped / deselected / ignored / filtered (e.g. an integration suite gated
  // on an env var or a service the cold verify VM has not provisioned)
  // exercises ZERO real tests — the SAME honest non-verification case as a
  // zero-collection run, and must NOT be recorded as a verified pass.
  //
  // STRICTLY conservative: classify all_skipped ONLY when there is zero
  // real-pass AND zero real-failure evidence AND at least one
  // skip/deselect/ignore/filter signal. A run where even one test passed or
  // failed ("5 passed, 1500 skipped") keeps its real signal and stays 'ran'.
  // hasRealPassEvidence mirrors hasRealFailureEvidence: line-anchored, non-zero.
  const hasRealPassEvidence = trimmedLines.some(line => (
    /\b[1-9]\d*\s+passed\b/i.test(line) ||   // pytest / vitest / Rust "N passed"
    /\bPassed:\s+[1-9]\d*\b/i.test(line) ||  // .NET `dotnet test` "Passed: N"
    /^--- PASS:/.test(line)                  // Go verbose per-test pass marker
  ));
  if (!hasRealFailureEvidence && !hasRealPassEvidence) {
    // Mocha/Jasmine: "N passing, M pending" where N=0 and M>0 is all-skipped.
    // Check this before standalone skip patterns since pending is the skip token.
    // May be on the same line or separate lines.
    {
      const passingM = trimmedLines.find(line => /\b(\d+)\s+passing\b/i.test(line))?.match(/\b(\d+)\s+passing\b/i);
      const pendingM = trimmedLines.find(line => /\b(\d+)\s+pending\b/i.test(line))?.match(/\b(\d+)\s+pending\b/i);
      if (passingM && pendingM) {
        const passing = Number(passingM[1]);
        const pending = Number(pendingM[1]);
        if (passing === 0 && pending > 0) return 'all_skipped';
      }
    }
    // Standalone skip tokens: the family reports any passing test as its OWN
    // "N passed" / "Passed: N" / "--- PASS:" token (already excluded above), so
    // a non-zero skip/deselect/ignore/filter count here is decisive.
    //   pytest "5 skipped" / "3 deselected"; vitest "5 skipped"; Rust "N ignored"
    //   / "N filtered out"; Go verbose "--- SKIP:".
    const standaloneSkip = trimmedLines.some(line => (
      /\b[1-9]\d*\s+skipped\b/i.test(line) ||
      /\b[1-9]\d*\s+deselected\b/i.test(line) ||
      /\b[1-9]\d*\s+ignored\b/i.test(line) ||
      /\b[1-9]\d*\s+filtered out\b/i.test(line) ||
      /^\s*--- SKIP:/.test(line)
    ));
    // Aggregate families report a TOTAL count (not a separate pass token), so a
    // bare "Skipped: N" could co-exist with passes counted only in the total.
    // Classify all-skip ONLY when total === skip on the SAME line (zero pass/
    // fail already guaranteed by the guards above), so "Tests run: 5, .., Skipped:
    // 3" (2 passed) stays 'ran' while "Tests run: 5, .., Skipped: 5" is all-skip.
    // The (totalRe, skipRe) family table is shared between the all-skip check
    // (total === skip) and the multi-class pass guard (total > skip) below.
    const AGGREGATE_FAMILIES: Array<[RegExp, RegExp]> = [
      // Ruby/RSpec: "5 examples, 0 failures, 5 pending"
      [/\b(\d+)\s+examples?\b/i, /\b(\d+)\s+pending\b/i],
      // Ruby/Minitest: "5 runs, .., 0 failures, 0 errors, 5 skips"
      [/\b(\d+)\s+runs?\b/i, /\b(\d+)\s+skips\b/i],
      // Elixir/ExUnit: "5 tests, 0 failures, 5 skipped" / ".., 5 excluded"
      [/\b(\d+)\s+tests?\b/i, /\b(\d+)\s+(?:skipped|excluded)\b/i],
      // Gradle/Maven (Surefire/JUnit): "Tests run: 5, Failures: 0, Errors: 0, Skipped: 5"
      [/Tests run:\s*(\d+)/i, /Skipped:\s*(\d+)/i],
      // .NET: "Passed: 0, Failed: 0, Skipped: 5, Total: 5"
      [/\bTotal:\s*(\d+)/i, /Skipped:\s*(\d+)/i],
      // PHPUnit: "OK, but incomplete, skipped, or risky tests!" → "Tests: 5, .., Skipped: 5"
      [/\bTests:\s*(\d+)/i, /Skipped:\s*(\d+)/i],
      // NOTE: Mocha/Jasmine is handled separately above via the special "0 passing, N pending" check.
    ];
    // Compare the total and skip counts on the SAME line across all aggregate
    // families; `cmp` selects all-skip (===) vs has-passes (>).
    const aggregateMatch = (cmp: (total: number, skip: number) => boolean): boolean =>
      AGGREGATE_FAMILIES.some(([totalRe, skipRe]) =>
        trimmedLines.some(line => {
          const t = line.match(totalRe);
          const s = line.match(skipRe);
          return Boolean(t && s && Number(t[1]) > 0 && cmp(Number(t[1]), Number(s[1])));
        }),
      );
    const allSkipByTotal = aggregateMatch((total, skip) => total === skip);
    // Multi-class / multi-project guard: Maven/Surefire/Gradle (and dotnet test)
    // print ONE aggregate summary line PER class/project. A sibling class that
    // PASSED ("Tests run: 3, .., Skipped: 0") shows total > skip on its own line
    // but — for the Surefire family — emits NO "N passed" token, so it evades
    // hasRealPassEvidence above. Since we are already inside !hasRealFailureEvidence
    // (zero failures/errors anywhere), any line with total > skip means real tests
    // passed. Treat that as pass evidence so one fully-skipped class can never
    // short-circuit the scan and flip a run with passing siblings to all_skipped.
    const aggregatePass = aggregateMatch((total, skip) => total > skip);
    if ((standaloneSkip || allSkipByTotal) && !aggregatePass) return 'all_skipped';
  }
  // Go: `go test` on an empty package reports `? pkg [no test files]`.
  // The default reporter also emits `ok pkg ...` lines when real tests ran,
  // so only classify as no_tests when every observed package line is empty or
  // explicitly filtered out with `[no tests to run]`.
  {
    const goResults = lines.filter(line => /^\s*(?:\?\s+\S+\s+\[no test files\]|ok\s+\S+)/i.test(line));
    if (goResults.length > 0) {
      if (hasRealFailureEvidence) return 'ran';
      const sawRealGoPass = goResults.some(line => /^ok\s+\S+/.test(line) && !/\[no tests to run\]/i.test(line));
      if (sawRealGoPass) return 'ran';
      return 'no_tests';
    }
  }
  // test body executing. Anchor to the runner-emitted forms (Go's
  // "testing: warning: no tests to run" / a standalone "[no tests to run]")
  // so a test asserting on the literal bracketed phrase as data stays a real
  // run. Bracketed Go package summaries are already handled by the `goResults`
  // branch above.
  if (!hasRealFailureEvidence && trimmedLines.some(line => /^testing:\s*warning:\s*no tests to run\b/i.test(line))) return 'no_tests';
  if (!hasRealFailureEvidence && trimmedLines.some(line => /^\[no tests to run\]$/i.test(line))) return 'no_tests';
  // Rust: zero-test runs emit a zero-count summary. Require the summary and
  // the `running 0 tests` header, while excluding any non-zero run counts so
  // mixed suites still count as real verification.
  if (
    !hasRealFailureEvidence &&
    lower.includes('running 0 tests') &&
    lower.includes('test result: ok. 0 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out') &&
    !/\brunning [1-9]\d* test(s)?\b/.test(lower)
  ) {
    return 'no_tests';
  }
  return 'ran';
}

/**
 * Best-effort parse of pass / skip / total counts from test-runner output.
 *
 * Companion to ``classifyTestExecution`` for the no-CI merge gate: where the
 * classifier only decides ran / no_tests / all_skipped, this keeps the ACTUAL
 * counts so a green PASS that exercised a tiny fraction of its suite (e.g.
 * "5 passed, 1500 skipped") is no longer indistinguishable from a fully
 * exercised pass. Reuses the SAME token/regex families the classifier
 * understands:
 *   - Standalone-token families (pytest, vitest, Rust, Go verbose) print
 *     discrete "N passed" / "N skipped|deselected|ignored|filtered out" tokens
 *     and Go verbose "--- PASS:" / "--- SKIP:" markers; counts are SUMMED across
 *     lines / projects (one summary per class/project — exactly how the
 *     classifier reasons about multi-class runs).
 *   - Aggregate families (RSpec, Minitest, ExUnit, Surefire/Maven, .NET,
 *     PHPUnit) print a single summary line carrying a TOTAL plus a skip count;
 *     passed is derived as total - skipped - failures - errors.
 * ``skipped`` is the UNION of skipped / deselected / ignored / filtered-out /
 * pending / excluded — the same set the classifier folds together.
 *
 * CONSERVATIVE BY DESIGN: returns null when the output carries no recognizable
 * count tokens, when a detected family's counts are internally inconsistent
 * (skipped > total), or when two aggregate families disagree on the total.
 * Callers treat null as "no coverage signal" and omit the field entirely, so an
 * ambiguous parse never invents numbers.
 */
export function extractTestCoverage(
  output: string,
): { passed: number; skipped: number; total: number } | null {
  if (!output) return null;
  const trimmedLines = output.split(/\r?\n/).map(line => line.trim());

  // First count match on a line, or 0. (RegExp.match returns only the first
  // occurrence, which is what every per-class summary line carries.)
  const countOnLine = (line: string, patterns: RegExp[]): number => {
    for (const p of patterns) {
      const m = line.match(p);
      if (m) return Number(m[1]);
    }
    return 0;
  };

  // ── Mocha/Jasmine special handling (passing + pending + failing = total) ────
  // Mocha reports "N passing, M pending, K failing" where passing/pending/failing
  // are the COUNTS of each category, not a total. So we need explicit addition.
  // Tokens may be on the same line or separate lines.
  {
    const passingM = trimmedLines.find(line => /\b(\d+)\s+passing\b/i.test(line))?.match(/\b(\d+)\s+passing\b/i);
    const pendingM = trimmedLines.find(line => /\b(\d+)\s+pending\b/i.test(line))?.match(/\b(\d+)\s+pending\b/i);
    const failingM = trimmedLines.find(line => /\b(\d+)\s+failing\b/i.test(line))?.match(/\b(\d+)\s+failing\b/i);
    if (passingM && pendingM) {
      const passed = Number(passingM[1]);
      const skipped = Number(pendingM[1]);
      const failed = failingM ? Number(failingM[1]) : 0;
      const total = passed + skipped + failed;
      if (total > 0) return { passed, skipped, total };
    }
  }

  // ── Aggregate families (single summary line carrying a TOTAL) ──────────────
  // Each entry is [totalRe, skipRe]; mirrors AGGREGATE_FAMILIES in
  // classifyTestExecution. A family fires only on lines carrying BOTH tokens,
  // and counts are summed across all such lines (one per class/project).
  // NOTE: Mocha/Jasmine is handled separately above via mochaMatch, not here.
  const AGGREGATE_FAMILIES: Array<[RegExp, RegExp]> = [
    [/\b(\d+)\s+examples?\b/i, /\b(\d+)\s+pending\b/i],            // Ruby/RSpec
    [/\b(\d+)\s+runs?\b/i, /\b(\d+)\s+skips\b/i],                  // Ruby/Minitest
    [/\b(\d+)\s+tests?\b/i, /\b(\d+)\s+(?:skipped|excluded)\b/i],  // Elixir/ExUnit
    [/Tests run:\s*(\d+)/i, /Skipped:\s*(\d+)/i],                  // Surefire/Maven
    [/\bTotal:\s*(\d+)/i, /Skipped:\s*(\d+)/i],                    // .NET
    [/\bTests:\s*(\d+)/i, /Skipped:\s*(\d+)/i],                    // PHPUnit
  ];
  const failPatterns = [/\b(?:Failures?|Failed):\s*(\d+)\b/i, /\b(\d+)\s+(?:failures?|failing)\b/i];
  const errPatterns = [/\bErrors?:\s*(\d+)\b/i, /\b(\d+)\s+errors?\b/i];
  const aggregateResults: Array<{ passed: number; skipped: number; total: number }> = [];
  for (const [totalRe, skipRe] of AGGREGATE_FAMILIES) {
    const familyLines = trimmedLines.filter(line => totalRe.test(line) && skipRe.test(line));
    if (familyLines.length === 0) continue;
    let total = 0, skipped = 0, failed = 0, errored = 0;
    for (const line of familyLines) {
      const t = line.match(totalRe);
      const s = line.match(skipRe);
      if (t) total += Number(t[1]);
      if (s) skipped += Number(s[1]);
      failed += countOnLine(line, failPatterns);
      errored += countOnLine(line, errPatterns);
    }
    if (total <= 0 || skipped > total) continue; // inconsistent — ignore family
    aggregateResults.push({ passed: Math.max(0, total - skipped - failed - errored), skipped, total });
  }
  if (aggregateResults.length > 0) {
    // Ambiguous when two families disagree on the total.
    if (new Set(aggregateResults.map(r => r.total)).size > 1) return null;
    return aggregateResults[0];
  }

  // ── Standalone-token families (pytest / vitest / Rust / Go verbose) ────────
  // Exclude vitest/jest FILE- or SUITE-level summary lines so their "N passed"
  // counts (files/suites, not tests) are not double-counted into the totals.
  const countLines = trimmedLines.filter(line => !/^Test (?:Files\b|Suites:)/i.test(line));
  const sum = (re: RegExp): number => countLines.reduce((acc, line) => {
    const m = line.match(re);
    return m ? acc + Number(m[1]) : acc;
  }, 0);
  const passed =
    sum(/\b(\d+)\s+passed\b/i) +
    countLines.filter(line => /^--- PASS:/.test(line)).length;
  const skipped =
    sum(/\b(\d+)\s+skipped\b/i) +
    sum(/\b(\d+)\s+deselected\b/i) +
    sum(/\b(\d+)\s+ignored\b/i) +
    sum(/\b(\d+)\s+filtered out\b/i) +
    countLines.filter(line => /^--- SKIP:/.test(line)).length;
  const failed =
    sum(/\b(\d+)\s+failed\b/i) +
    countLines.filter(line => /^--- FAIL:/.test(line)).length;
  const total = passed + skipped + failed;
  if (total <= 0) return null;
  return { passed, skipped, total };
}

/**
 * Extract TOTAL line/statement coverage percentage from pytest-cov terminal output.
 *
 * Parses the final coverage summary line emitted by ``--cov-report=term-missing``:
 * ``TOTAL    350    30    91%``.  Returns the percentage (91) as a number, or null
 * when no coverage summary line is found (no coverage instrumentation active).
 *
 * The returned percentage is a FIRST-PASS TOTAL over all instrumented modules, not
 * a diff-aware "coverage of changed lines" metric.  A per-file union with the PR
 * diff is a future refinement.
 */
export function extractLineCoverage(output: string): number | null {
  if (!output) return null;
  // Match "TOTAL" summary line from pytest-cov. Captures the coverage %.
  const match = output.match(/^\s*TOTAL\s+\d+\s+\d+\s+(\d+(?:\.\d+)?)%/im);
  if (!match) return null;
  const pct = parseFloat(match[1]);
  return isFinite(pct) ? pct : null;
}

/**
 * Extract a diff-aware changed-line coverage percentage from pytest-cov terminal output.
 *
 * Parses the per-file coverage table from ``--cov-report=term-missing`` and computes a
 * weighted average: for each file touched by the PR diff, its coverage percentage is
 * weighted by the number of changed lines in that file.
 *
 * This is an approximation that prefers simplicity over line-level precision — it
 * assumes coverage within a file is uniformly distributed.  A line-level intersection
 * with the diff (via ``--cov-report=json``) is a further refinement.
 *
 * @param output Raw pytest-cov terminal output containing the per-file table.
 * @param changedLineCounts Map of repo-relative file path → count of changed/added lines.
 *   Paths are first tried as-is, then with the first directory component stripped (to
 *   handle the common case where tests run from a subdirectory like ``backend/``).
 * @returns The weighted changed-line coverage percentage, or null when no per-file
 *   coverage data is found or ``changedLineCounts`` is empty.
 */
export function extractChangedLineCoverage(
  output: string,
  changedLineCounts: Record<string, number>,
): number | null {
  if (!output) return null;
  const changedFiles = Object.keys(changedLineCounts).filter(f => changedLineCounts[f] > 0);
  if (changedFiles.length === 0) return null;

  // Parse per-file coverage from pytest-cov terminal output.  Format per data line:
  //   backend/app/foo.py   100    10    90%
  // Skip the header row (Name), the dashed separator, and the TOTAL summary.
  const fileCoverage: Record<string, number> = {};
  for (const line of output.split('\n')) {
    const trimmed = line.trim();
    if (!trimmed || trimmed === 'Name' || /^(?:-+|TOTAL)/.test(trimmed)) continue;
    const m = trimmed.match(/^(\S+(?:\/\S+)*)\s+(\d+)\s+(\d+)\s+(\d+(?:\.\d+)?)%/);
    if (m) {
      const pct = parseFloat(m[4]);
      if (isFinite(pct)) fileCoverage[m[1]] = pct;
    }
  }

  const fileCoverageKeys = Object.keys(fileCoverage);
  if (fileCoverageKeys.length === 0) return null;

  // Compute weighted average: weight each changed file's coverage by its
  // changed-line count.  Try the diff path as-is first, then strip the first
  // directory component (handles tests run from a project subdirectory).
  let weightedSum = 0;
  let totalChangedLines = 0;
  for (const filePath of changedFiles) {
    const changedCount = changedLineCounts[filePath];
    // Try the full repo-relative path (e.g. ``backend/app/foo.py``).
    let covPct: number | null = null;
    if (filePath in fileCoverage) {
      covPct = fileCoverage[filePath];
    } else {
      // Fallback: strip the first directory (``backend/app/foo.py`` → ``app/foo.py``).
      const slashIdx = filePath.indexOf('/');
      const stripped = slashIdx !== -1 ? filePath.slice(slashIdx + 1) : null;
      if (stripped !== null && stripped in fileCoverage) {
        covPct = fileCoverage[stripped];
      }
    }
    if (covPct !== null) {
      weightedSum += covPct * changedCount;
      totalChangedLines += changedCount;
    }
  }

  if (totalChangedLines === 0) return null;
  return Math.round((weightedSum / totalChangedLines) * 100) / 100;
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
  // A `bash -c '<cmd>'` wrapper runs its command string as a single argv
  // element; appending selectors as further argv would land them in $0/$1…
  // (positional params of the shell), NOT as arguments to the wrapped pytest.
  // So never target-append to a wrapped runner — let it run its full suite.
  if (runner.cmd === 'bash' && runner.args[0] === '-c') return false;
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

// ── Declared acceptance-test probe ───────────────────────────────────────────
// When an implementation task declared, at plan time, a named acceptance test
// (``acceptance_evidence`` with ``kind="test"``), the no-CI merge gate must
// confirm that specific test actually EXISTS and RAN in the PR's suite — not
// just that the suite as a whole passed. These two helpers are the pure,
// unit-testable core of that probe; the execution + timeout discipline lives in
// the verify-tests handler.

/** A plan-time acceptance-test declaration as it travels on the task context. */
export interface DeclaredAcceptanceTest {
  descriptor: string;
  selector?: string;
}

/** Build a TARGETED runner that runs ONLY the declared acceptance test, reusing
 *  the same pytest-selector targeting the gate uses for CI-failure replay.
 *
 *  Conservative: it only knows how to target a pytest-style selector (a
 *  ``…test_x.py::test_y`` node id, or a ``testing/…`` path) against an
 *  already-detected pytest runner. When the declaration carries no usable
 *  selector, or no detected runner accepts pytest selectors, it returns ``null``
 *  — the caller then degrades to a "could not run the check" fall-open rather
 *  than fabricating an arbitrary command. ``runners`` is passed in (not detected
 *  here) so the function stays pure and the caller controls detection. */
export function buildAcceptanceTestRunner(
  declared: DeclaredAcceptanceTest,
  runners: TestRunner[],
): TestRunner | null {
  const selector = (declared.selector || declared.descriptor || '').trim();
  if (!selector) return null;
  // Only a pytest-shaped selector can be targeted today. A bare prose
  // descriptor (no ``.py`` node id / ``testing/`` path) is not runnable as a
  // selector, so we decline rather than guess.
  const looksPytest = /\.py(::|$)/.test(selector) || /(^|\/)testing\//.test(selector);
  if (!looksPytest) return null;
  const targeted = buildTargetedPytestRunners(runners, [selector]);
  return targeted.length > 0 ? targeted[0] : null;
}

/** Classify whether the declared acceptance test's targeted run actually
 *  executed the test. Thin map over :func:`classifyTestExecution`:
 *    - ``ran``     — the test executed (passed or failed; execution is what we
 *                    confirm here, the suite gate owns pass/fail);
 *    - ``skipped`` — the selector matched but every collected test was
 *                    skipped/deselected/xfail'd (it did NOT actually run);
 *    - ``absent``  — the selector matched nothing (the promised test is missing
 *                    / zero-collection / unknown node id). */
export function classifyAcceptanceTestRun(output: string): 'ran' | 'skipped' | 'absent' {
  const cls = classifyTestExecution(output);
  if (cls === 'no_tests') return 'absent';
  if (cls === 'all_skipped') return 'skipped';
  return 'ran';
}

// isDockerRunner is used by callers in index.ts via re-export
export { isDockerRunner };

// ── Diff-aware documented / PR-introduced command discovery ──────────────────
// The no-CI merge gate runs the repo's STORED suite + a cheap acceptance probe,
// but it never executes commands the PR ITSELF introduces or documents — a newly
// added package.json script, a newly added Makefile/justfile/Taskfile target, a
// newly declared console-script entrypoint, or a command shown in a touched
// README/docs usage block. So a PR can document/add a command, claim it works,
// and merge with that claim entirely unexercised. These helpers discover that
// small set of commands CONSERVATIVELY (structured sources preferred over
// free-text prose) and ALLOWLISTED (every candidate is refused unless it clears
// the destructive/network/privilege denylist), so the gate can run them. The
// execution + timeout discipline lives in the verify-tests handler; this module
// is the pure, unit-testable discovery + safety logic.

/** A single command the PR introduces/documents, ready to run by the gate. */
export interface DocumentedCommand {
  /** Shell command string to execute (e.g. ``npm run build``, ``make test``). */
  command: string;
  /** Absolute working directory to run it from. */
  cwd: string;
  /** Where it was discovered (``package.json scripts.build``, ``make target test``…). */
  source: string;
  /** Human label surfaced in the verdict (``npm run build (agent/package.json)``). */
  label: string;
  /** Heavier build commands get the larger build timeout; cheap ones the baseline. */
  heavy: boolean;
}

// Hard cap on discovered commands so a PR touching many manifests cannot fan out
// an unbounded set of probe processes. The handler runs each ONCE under a
// bounded timeout (no retries beyond the gate's existing discipline).
export const MAX_DOCUMENTED_COMMANDS = 4;

// Executables that are destructive, escalate privilege, touch the network, or
// mutate global state. A documented/PR-introduced command — OR any sub-command
// it chains into, OR the BODY of a script/target it would invoke — that runs one
// of these is refused outright.
const UNSAFE_EXECUTABLES = new Set([
  'rm', 'rmdir', 'sudo', 'su', 'doas', 'curl', 'wget', 'nc', 'ncat', 'netcat',
  'ssh', 'scp', 'sftp', 'telnet', 'ftp', 'rsync', 'docker', 'podman', 'kubectl',
  'helm', 'systemctl', 'service', 'mkfs', 'dd', 'chmod', 'chown', 'chgrp',
  'kill', 'killall', 'pkill', 'reboot', 'shutdown', 'poweroff', 'halt', 'mount',
  'umount', 'apt', 'apt-get', 'yum', 'dnf', 'pacman', 'apk', 'brew', 'snap',
  'git', 'gh', 'aws', 'gcloud', 'az', 'terraform', 'ansible',
]);

// Sub-command verbs that fetch from the network, escalate, or publish/deploy.
// Refused wherever they appear as a token after the runner — covers
// ``npm publish``, ``pip install x``, ``cargo install``, ``go get``, and any
// ``<runner> deploy|release|publish`` shape.
const UNSAFE_VERBS = new Set([
  'install', 'publish', 'deploy', 'release', 'add', 'get', 'push', 'login',
  'logout', 'uninstall', 'remove', 'update', 'upgrade', 'token', 'audit',
]);

// Leading ``NAME=value`` env-assignment tokens (``FOO=1 rm -rf /``) hide the
// real executable from a head-only check — strip them before inspecting.
const ENV_ASSIGN_RE = /^[A-Za-z_][A-Za-z0-9_]*=/;

// Wrapper commands that run ANOTHER command passed as their argument
// (``env curl …``, ``timeout 5 rm …``, ``xargs rm``). They mask the real
// executable, so a documented/introduced command that leads with one is
// refused outright rather than trying to peel and re-validate the inner shape.
const WRAPPER_COMMANDS = new Set([
  'env', 'time', 'timeout', 'xargs', 'nice', 'ionice', 'command', 'exec',
  'nohup', 'setsid', 'stdbuf', 'chrt', 'sudo', 'su', 'doas',
]);

// Basename of a token, lowercased — so an absolute/relative path (``/bin/rm``,
// ``./curl``) is matched against the denylist by its final component.
function commandBasename(token: string): string {
  const slash = token.lastIndexOf('/');
  return (slash >= 0 ? token.slice(slash + 1) : token).toLowerCase();
}

function isSafeSubCommand(sub: string): boolean {
  let tokens = sub.split(/\s+/).filter(Boolean);
  if (tokens.length === 0) return false;
  // Strip leading env-assignment tokens so ``FOO=1 rm -rf /`` is judged on ``rm``.
  while (tokens.length > 0 && ENV_ASSIGN_RE.test(tokens[0])) tokens = tokens.slice(1);
  if (tokens.length === 0) return false;
  const head = commandBasename(tokens[0]);
  // Reject wrapper commands that hide the real executable (``env``, ``timeout``,
  // ``xargs``, ``sudo`` …) before any other check.
  if (WRAPPER_COMMANDS.has(head)) return false;
  // Check the basename of EVERY token (not just the head) against the denylist
  // so an absolute path (``/bin/rm``), a wrapper-smuggled executable, or a
  // chained dangerous binary that survived sub-command splitting is refused.
  if (tokens.some(t => UNSAFE_EXECUTABLES.has(commandBasename(t)))) return false;
  // Inline code-eval (``bash -c``, ``node -e``, ``python -c`` …) executes
  // arbitrary code — refuse regardless of what follows.
  if (
    /^(bash|sh|zsh|ksh|node|deno|bun|python3?|perl|ruby|php)$/.test(head) &&
    tokens.slice(1).some(t => /^(-c|-e|--eval|--command|--exec)$/i.test(t))
  ) {
    return false;
  }
  if (tokens.slice(1).some(t => UNSAFE_VERBS.has(t.toLowerCase()))) return false;
  return true;
}

/**
 * Whether a command (or a script/target body the gate would invoke) is safe to
 * run. CONSERVATIVE — refuses command substitution, redirection to absolute /
 * system paths, and any sub-command that invokes a destructive/network/privilege
 * executable or a network-install / deploy / publish verb. A chain of SAFE
 * commands (``tsc && vite build``) is allowed; a chain that smuggles a dangerous
 * command (``build && rm -rf /``) is refused. When in doubt, returns false.
 */
export function isSafeDocumentedCommand(command: string): boolean {
  const cmd = (command || '').trim();
  if (!cmd) return false;
  // Command substitution — hidden execution.
  if (/`|\$\(/.test(cmd)) return false;
  // Output redirection to an absolute / system path (``> /etc/x``, ``>>/usr/…``).
  if (/>>?\s*\//.test(cmd)) return false;
  // Validate EACH chained sub-command.
  const subs = cmd.split(/&&|\|\||;|\|/).map(s => s.trim()).filter(Boolean);
  if (subs.length === 0) return false;
  return subs.every(isSafeSubCommand);
}

// Known-safe runner first-tokens for LAST-RESORT free-text (README/docs)
// extraction. A prose command is considered only when its first token is one of
// these AND it additionally clears ``isSafeDocumentedCommand``. Structured
// sources do NOT use this list — they are preferred and gated on the body
// denylist instead.
const ALLOWLISTED_RUNNERS = new Set([
  'npm', 'yarn', 'pnpm', 'npx', 'node', 'make', 'just', 'task', 'cargo', 'go',
  'python', 'python3', 'pytest', 'tox', 'nox', 'bundle', 'rake', 'mix',
  'dotnet', 'mvn', 'gradle', './gradlew', 'gradlew', 'composer', 'poetry',
  'ruff', 'eslint', 'tsc', 'vitest', 'jest', 'deno', 'bun',
]);

/** Whether a free-text command's first token is a known-safe runner shape. */
export function isAllowlistedRunnerShape(command: string): boolean {
  const tokens = command.trim().split(/\s+/).filter(Boolean);
  if (tokens.length === 0) return false;
  return ALLOWLISTED_RUNNERS.has(tokens[0].toLowerCase());
}

function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

// The script/target names worth exercising — the verbs the direction cares about
// (test/build/lint/check + type-check aliases). Bounded so a manifest declaring
// dozens of scripts cannot fan out.
const INTERESTING_NAMES = ['test', 'build', 'lint', 'check', 'typecheck', 'type-check'];

// A name is "newly introduced" when a declaration line for it appears in the
// PR's added diff lines. Matches both quoted JSON keys (``"build":``) and bare
// target/key declarations (``build:`` / ``  build:``).
function addedDeclaresName(addedLines: string[], name: string): boolean {
  const esc = escapeRegExp(name);
  const jsonKey = new RegExp(`"${esc}"\\s*:`);
  const decl = new RegExp(`(^|\\s)${esc}\\s*[:=]`);
  return addedLines.some(l => jsonKey.test(l) || decl.test(l));
}

function parsePackageJsonCommands(
  text: string, addedLines: string[], cwd: string, label: string,
): DocumentedCommand[] {
  let parsed: unknown;
  try { parsed = JSON.parse(text); } catch { return []; }
  const scripts = (parsed && typeof parsed === 'object'
    ? (parsed as { scripts?: Record<string, unknown> }).scripts
    : undefined);
  if (!scripts || typeof scripts !== 'object') return [];
  const out: DocumentedCommand[] = [];
  for (const name of INTERESTING_NAMES) {
    const body = scripts[name];
    if (typeof body !== 'string' || !body.trim()) continue;
    if (!addedDeclaresName(addedLines, name)) continue;   // not newly introduced
    if (!isSafeDocumentedCommand(body)) continue;         // script BODY denylist
    const command = name === 'test' ? 'npm test' : `npm run ${name}`;
    out.push({
      command, cwd, source: `package.json scripts.${name}`,
      label: `${command} (${label})`, heavy: name === 'build',
    });
  }
  return out;
}

function indentWidth(line: string): number {
  const m = line.match(/^[ \t]*/);
  return m ? m[0].length : 0;
}

// The body lines belonging to a make/just/task target: every line after the
// declaration that is MORE indented than the declaration, until a same-or-less
// indented (non-blank) line. Works for tab-indented make recipes, indented just
// recipes, and nested Taskfile ``cmds:`` blocks.
function targetBodyLines(lines: string[], declIndex: number): string[] {
  const baseIndent = indentWidth(lines[declIndex]);
  const body: string[] = [];
  for (let i = declIndex + 1; i < lines.length; i++) {
    const line = lines[i];
    if (!line.trim()) continue;                 // blanks don't end the block
    if (indentWidth(line) <= baseIndent) break; // dedent → block ended
    body.push(line.trim());
  }
  return body;
}

function parseTargetCommands(
  text: string, addedLines: string[], cwd: string, label: string,
  kind: 'make' | 'just' | 'task',
): DocumentedCommand[] {
  const lines = text.split(/\r?\n/);
  const runner = kind === 'make' ? 'make' : kind === 'just' ? 'just' : 'task';
  // make/just targets sit at column 0; Taskfile targets are indented under
  // ``tasks:``. Both forms end in ``:``.
  const declRe = (name: string): RegExp => kind === 'task'
    ? new RegExp(`^\\s+${escapeRegExp(name)}\\s*:`)
    : new RegExp(`^${escapeRegExp(name)}\\s*:`);
  const out: DocumentedCommand[] = [];
  for (const name of INTERESTING_NAMES) {
    const re = declRe(name);
    const declIndex = lines.findIndex(l => re.test(l));
    if (declIndex < 0) continue;
    if (!addedDeclaresName(addedLines, name)) continue;   // not newly introduced
    // Refuse if any recipe/command line in the target body is dangerous.
    const body = targetBodyLines(lines, declIndex)
      .map(l => l.replace(/^[-*]\s*/, '').replace(/^cmd:\s*/, '').trim())
      .filter(Boolean);
    if (!body.every(isSafeDocumentedCommand)) continue;
    const command = `${runner} ${name}`;
    out.push({
      command, cwd, source: `${kind} target ${name}`,
      label: `${command} (${label})`, heavy: name === 'build',
    });
  }
  return out;
}

// Console-script / entrypoint names declared in pyproject.toml
// (``[project.scripts]`` / ``[tool.poetry.scripts]``) or setup.cfg
// (``console_scripts`` under ``[options.entry_points]``).
function parseConsoleScriptNames(text: string, kind: 'pyproject' | 'setup.cfg'): string[] {
  const lines = text.split(/\r?\n/);
  const names: string[] = [];
  if (kind === 'pyproject') {
    let inScripts = false;
    for (const raw of lines) {
      const line = raw.trim();
      if (/^\[/.test(line)) {
        inScripts = /^\[(project\.scripts|tool\.poetry\.scripts)\]/.test(line);
        continue;
      }
      if (!inScripts) continue;
      const m = line.match(/^([A-Za-z][\w.-]*)\s*=/);
      if (m) names.push(m[1]);
    }
    return names;
  }
  // setup.cfg: a ``console_scripts =`` key whose entries are indented beneath it.
  let inConsole = false;
  let consoleIndent = -1;
  for (const raw of lines) {
    if (!raw.trim()) continue;
    const indent = indentWidth(raw);
    const trimmed = raw.trim();
    if (inConsole) {
      if (indent <= consoleIndent) {
        inConsole = false; // dedent — re-evaluate this line below
      } else {
        const m = trimmed.match(/^([A-Za-z][\w.-]*)\s*=/);
        if (m) names.push(m[1]);
        continue;
      }
    }
    if (/^console_scripts\s*=/.test(trimmed)) {
      inConsole = true;
      consoleIndent = indent;
    }
  }
  return names;
}

function parseConsoleScriptCommands(
  text: string, addedLines: string[], cwd: string, label: string,
  kind: 'pyproject' | 'setup.cfg',
): DocumentedCommand[] {
  const out: DocumentedCommand[] = [];
  for (const name of parseConsoleScriptNames(text, kind)) {
    if (!/^[A-Za-z][\w.-]*$/.test(name)) continue;        // sane binary name only
    if (!addedDeclaresName(addedLines, name)) continue;   // not newly introduced
    // Exercise the entrypoint cheaply + safely: ``<name> --help``. The entrypoint
    // must be installed (provisioning runs the project install), so a missing
    // binary degrades to could_not_run downstream rather than blocking.
    const command = `${name} --help`;
    if (!isSafeDocumentedCommand(command)) continue;
    out.push({
      command, cwd, source: `console_script ${name}`,
      label: `${command} (${label})`, heavy: false,
    });
  }
  return out;
}

// Lines inside fenced code blocks (``` / ~~~), trimmed. Used for LAST-RESORT
// README/docs extraction.
function extractFencedCommandLines(text: string): string[] {
  const lines = text.split(/\r?\n/);
  const out: string[] = [];
  let inFence = false;
  for (const raw of lines) {
    const trimmed = raw.trim();
    if (/^(```|~~~)/.test(trimmed)) { inFence = !inFence; continue; }
    if (inFence && trimmed) out.push(trimmed);
  }
  return out;
}

function parseDocumentedProseCommands(
  text: string, addedLines: string[], cwd: string, label: string,
): DocumentedCommand[] {
  const addedSet = new Set(addedLines.map(l => l.trim()));
  const out: DocumentedCommand[] = [];
  for (const line of extractFencedCommandLines(text)) {
    // Only commands the PR NEWLY documented (the fenced line is in the diff).
    if (!addedSet.has(line)) continue;
    // Strip a leading shell prompt (``$ `` / ``> ``).
    const cmd = line.replace(/^[$>]\s+/, '').trim();
    if (!cmd) continue;
    if (!isAllowlistedRunnerShape(cmd)) continue;         // strict runner shape
    if (!isSafeDocumentedCommand(cmd)) continue;          // denylist
    out.push({
      command: cmd, cwd, source: `documented command (${label})`,
      label: `${cmd} (${label})`, heavy: /(^|\s)build(\s|$)/.test(cmd),
    });
  }
  return out;
}

/** Input for {@link discoverDocumentedCommands}. */
export interface DiscoverDocumentedCommandsInput {
  /** PR-vs-base changed files, repo-root-relative (``git diff --name-only``). */
  changedFiles: string[];
  /** Added diff lines (``+`` lines, ``+`` stripped, trimmed). */
  addedLines: string[];
  /**
   * Added diff lines keyed by the repo-relative file they belong to. When
   * present, a script/target/entrypoint is judged "newly introduced" ONLY when
   * its declaration appears in the added lines of the SAME manifest being
   * parsed — so a matching declaration shape in an unrelated file cannot make a
   * pre-existing script in a separately-changed manifest look new. Falls back to
   * the global {@link addedLines} when a file has no entry (keeps callers/tests
   * that pass only the flat list working unchanged).
   */
  addedLinesByFile?: Record<string, string[]>;
  /** Read a repo-relative file's text; return null when absent/unreadable. */
  readFile: (relPath: string) => string | null;
  /** Workspace root (absolute). Defaults to the module workspace. */
  workspace?: string;
  /** Max commands to return. Defaults to {@link MAX_DOCUMENTED_COMMANDS}. */
  max?: number;
}

/**
 * Discover the small, allowlisted set of commands a PR INTRODUCES or DOCUMENTS,
 * diffing against the base. STRUCTURED sources (added package.json scripts,
 * added Makefile/justfile/Taskfile targets, declared console-script entrypoints)
 * are preferred; free-text README/docs extraction is LAST-RESORT and only pulls
 * fenced-block lines matching a strict runner allowlist. Every candidate clears
 * the destructive/network/privilege denylist (script/target BODIES too). Bounded
 * to ``max`` commands and deduped by cwd+command. Pure — all filesystem access
 * goes through the injected ``readFile`` so the logic is unit-testable.
 */
export function discoverDocumentedCommands(
  input: DiscoverDocumentedCommandsInput,
): DocumentedCommand[] {
  const workspace = input.workspace ?? WORKSPACE;
  const max = input.max ?? MAX_DOCUMENTED_COMMANDS;
  const out: DocumentedCommand[] = [];
  const seen = new Set<string>();
  const push = (c: DocumentedCommand): void => {
    if (out.length >= max) return;
    const key = `${c.cwd}::${c.command}`;
    if (seen.has(key)) return;
    seen.add(key);
    out.push(c);
  };
  const dirOf = (relPath: string): string => {
    const slash = relPath.lastIndexOf('/');
    const dir = slash >= 0 ? relPath.slice(0, slash) : '';
    return dir ? `${workspace}/${dir}` : workspace;
  };

  // Added lines scoped to ONE file: the per-file map when supplied, else the
  // global list (so a declaration shape in an unrelated changed file cannot make
  // a pre-existing script in a separately-changed manifest look newly added).
  const addedFor = (rel: string): string[] =>
    input.addedLinesByFile?.[rel] ?? input.addedLines;

  // Structured sources first; defer README/docs to a last-resort pass.
  const proseFiles: string[] = [];
  for (const rel of input.changedFiles) {
    if (out.length >= max) break;
    const slash = rel.lastIndexOf('/');
    const base = slash >= 0 ? rel.slice(slash + 1) : rel;
    const lower = base.toLowerCase();
    const cwd = dirOf(rel);
    const added = addedFor(rel);
    if (lower === 'package.json') {
      const text = input.readFile(rel);
      if (text != null) for (const c of parsePackageJsonCommands(text, added, cwd, rel)) push(c);
    } else if (lower === 'makefile' || lower === 'gnumakefile') {
      const text = input.readFile(rel);
      if (text != null) for (const c of parseTargetCommands(text, added, cwd, rel, 'make')) push(c);
    } else if (lower === 'justfile') {
      const text = input.readFile(rel);
      if (text != null) for (const c of parseTargetCommands(text, added, cwd, rel, 'just')) push(c);
    } else if (lower === 'taskfile.yml' || lower === 'taskfile.yaml') {
      const text = input.readFile(rel);
      if (text != null) for (const c of parseTargetCommands(text, added, cwd, rel, 'task')) push(c);
    } else if (lower === 'pyproject.toml' || lower === 'setup.cfg') {
      const text = input.readFile(rel);
      if (text != null) for (const c of parseConsoleScriptCommands(text, added, cwd, rel, lower === 'setup.cfg' ? 'setup.cfg' : 'pyproject')) push(c);
    } else if (/\.(md|markdown|rst)$/i.test(lower) || lower === 'readme') {
      proseFiles.push(rel);
    }
  }

  // Last-resort: free-text README/docs. Prose commands run from the repo root
  // (docs commonly live under docs/ but are invoked from the project root).
  if (out.length < max) {
    for (const rel of proseFiles) {
      if (out.length >= max) break;
      const text = input.readFile(rel);
      if (text != null) for (const c of parseDocumentedProseCommands(text, addedFor(rel), workspace, rel)) push(c);
    }
  }

  return out;
}

// ── Newly-introduced test-skip / disable marker discovery ────────────────────
// The inverse of the documented-command probe: a PR can make a red suite look
// green by DISABLING tests instead of fixing them. This is a VISIBILITY-FIRST
// signal — it never blocks the merge — so an operator can see "this PR disables
// N test(s)" in the merge-gate verdict.

/** One newly-introduced test-skip / disable marker, enough to render a row. */
export interface DisabledTestMarker {
  /** Repo-relative path of the TEST file the marker was added to. */
  file: string;
  /** The marker kind that matched (``it.skip``, ``@pytest.mark.skip`` …). */
  marker: string;
  /** The trimmed offending added line. */
  line: string;
}

// Hard cap on reported markers so a PR that mass-disables a suite cannot fan out
// an unbounded list into the verdict artifact (subject to the payload budget).
export const MAX_DISABLED_TEST_MARKERS = 20;

// Path heuristics for "this is a TEST file" — restrict the scan so unrelated
// prose or a non-test ``# ignore``-style comment cannot false-positive. Mirrors
// the common ecosystem conventions: ``*.test.*`` / ``*.spec.*`` (JS/TS), Go
// ``*_test.go``, Python ``test_*.py`` / ``*_test.py``, Ruby ``*_spec.rb``,
// Rust ``*_test.rs`` / ``*_spec.rs``, Java/Kotlin ``*Test.java`` / ``*Test.kt``,
// and anything under a ``tests/`` / ``test/`` / ``__tests__/`` / ``spec/`` directory.
const TEST_FILE_PATTERNS: RegExp[] = [
  /\.(test|spec)\.[a-z0-9]+$/i,
  /_test\.go$/i,
  /(^|\/)test_[^/]*\.py$/i,
  /_test\.py$/i,
  /_spec\.rb$/i,
  /_test\.rs$/i,
  /_spec\.rs$/i,
  /Test\.(java|kt|kts)$/i,
  /(^|\/)(tests?|__tests__|spec)\//i,
];

/** Whether a repo-relative path is a TEST file by the conventions above. */
export function isTestFilePath(relPath: string): boolean {
  const p = (relPath || '').trim();
  if (!p) return false;
  return TEST_FILE_PATTERNS.some(re => re.test(p));
}

/** Detect the language(s) of a test file based on its path and extension. */
function detectTestLanguages(relPath: string): string[] {
  const p = (relPath || '').trim().toLowerCase();
  const languages = new Set<string>();

  // File extension-based detection
  if (p.endsWith('.ts') || p.endsWith('.tsx') || p.endsWith('.js') || p.endsWith('.jsx')) {
    languages.add('js-ts');
  } else if (p.endsWith('.py')) {
    languages.add('python');
  } else if (p.endsWith('.go')) {
    languages.add('go');
  } else if (p.endsWith('.rs')) {
    languages.add('rust');
  } else if (p.endsWith('.rb')) {
    languages.add('ruby');
  } else if (p.endsWith('.java') || p.endsWith('.kt') || p.endsWith('.kts')) {
    languages.add('jvm');
  }

  // Directory-based detection (for ambiguous test files under tests/ directories)
  if (p.includes('/__tests__/') || p.includes('/tests/') || p.includes('/test/') || p.includes('/spec/')) {
    // If directory alone can't determine the language, we need to preserve multi-language detection
    if (languages.size === 0) {
      // Ambiguous case: /tests/ dir but no clear extension. This shouldn't happen
      // since test files should have extensions, but be conservative.
      return ['js-ts', 'python', 'go', 'rust', 'ruby', 'jvm'];
    }
  }

  return Array.from(languages);
}

// Each marker is anchored so it matches REAL usage (``it.skip(`` …), not a
// substring inside an identifier or a longer marker name. ``kind`` is the
// human-facing label surfaced in the verdict. ``languages`` restricts the
// marker to only test files of the specified language(s).
const DISABLED_TEST_MARKERS: Array<{ kind: string; re: RegExp; languages: string[] }> = [
  // JS / TS (Jest / Vitest / Mocha / Jasmine).
  { kind: 'it.skip', re: /\bit\.skip\b/, languages: ['js-ts'] },
  { kind: 'test.skip', re: /\btest\.skip\b/, languages: ['js-ts'] },
  { kind: 'describe.skip', re: /\bdescribe\.skip\b/, languages: ['js-ts'] },
  { kind: 'context.skip', re: /\bcontext\.skip\b/, languages: ['js-ts'] },
  { kind: 'xit', re: /\bxit\s*\(/, languages: ['js-ts'] },
  { kind: 'xdescribe', re: /\bxdescribe\s*\(/, languages: ['js-ts'] },
  { kind: 'xtest', re: /\bxtest\s*\(/, languages: ['js-ts'] },
  // Python — pytest markers and unittest decorators.
  { kind: '@pytest.mark.skipif', re: /@pytest\.mark\.skipif\b/, languages: ['python'] },
  { kind: '@pytest.mark.skip', re: /@pytest\.mark\.skip\b/, languages: ['python'] },
  { kind: '@pytest.mark.xfail', re: /@pytest\.mark\.xfail\b/, languages: ['python'] },
  { kind: '@unittest.skip', re: /@(?:unittest\.)?skip(?:If|Unless)?\b/, languages: ['python'] },
  // Go — testing.T skip calls.
  { kind: 't.Skip()', re: /\b[a-zA-Z_]\w*\.Skip(?:Now)?\s*\(/, languages: ['go'] },
  // Rust — ignored test attribute.
  { kind: '#[ignore]', re: /#\[\s*ignore\b/, languages: ['rust'] },
  // JUnit (Java/Kotlin).
  { kind: '@Disabled', re: /@Disabled\b/, languages: ['jvm'] },
  { kind: '@Ignore', re: /@Ignore\b/, languages: ['jvm'] },
  // RSpec (Ruby) — ``pending`` marks an example as not-yet-passing.
  // Avoid false positives on string literals ("pending"), symbols (:pending),
  // and identifiers containing "pending". Match only genuine RSpec calls:
  // pending followed by space/paren/brace/do or end-of-line.
  { kind: 'pending', re: /(?<!:)\bpending\b(?:\s|$|\(|\{|do\b)/, languages: ['ruby'] },
];

// Whether a (trimmed) added line is a comment we should NOT scan for markers —
// so prose like ``// it.skip is deprecated`` does not false-positive. Careful
// about Rust attributes: ``#[ignore]`` leads with ``#[`` and is NOT a comment,
// whereas a Python/Ruby ``# ignore`` comment is.
function isCommentLine(line: string): boolean {
  if (line.startsWith('//') || line.startsWith('*') || line.startsWith('/*')) return true;
  if (line.startsWith('#') && !line.startsWith('#[')) return true;
  return false;
}

/** Input for {@link discoverNewlyDisabledTests}. */
export interface DiscoverDisabledTestsInput {
  /** PR-vs-base changed files, repo-root-relative (``git diff --name-only``). */
  changedFiles: string[];
  /**
   * Added diff lines keyed by the repo-relative file they belong to. ONLY a
   * file's OWN added lines are scanned, so a pre-existing legitimate skip (an
   * unchanged line, or a skip in an unrelated file) is never reported.
   */
  addedLinesByFile: Record<string, string[]>;
  /** Max markers to return. Defaults to {@link MAX_DISABLED_TEST_MARKERS}. */
  max?: number;
}

/**
 * Discover the test-skip / disable markers a PR NEWLY introduces. Scans ONLY the
 * added lines (``+`` lines) of changed TEST files, so pre-existing skips and
 * non-test files are ignored. Anchored, ecosystem-aware marker matching keeps
 * prose / comments from false-positiving. Language-scoped markers are only checked
 * for languages matching the test file (e.g., RSpec 'pending' only in Ruby files).
 * Pure — no filesystem access — so it is directly unit-testable. Bounded to ``max`` markers.
 */
export function discoverNewlyDisabledTests(
  input: DiscoverDisabledTestsInput,
): DisabledTestMarker[] {
  const max = input.max ?? MAX_DISABLED_TEST_MARKERS;
  const out: DisabledTestMarker[] = [];
  for (const rel of input.changedFiles) {
    if (out.length >= max) break;
    if (!isTestFilePath(rel)) continue;
    // A changed test file may have no added-lines entry (binary / deletion / a
    // change with no ``+`` lines) — guard the lookup before indexing.
    if (!Object.prototype.hasOwnProperty.call(input.addedLinesByFile, rel)) continue;
    const added = input.addedLinesByFile[rel];
    if (added.length === 0) continue;

    // Detect the file's language(s) and filter markers accordingly.
    const fileLanguages = detectTestLanguages(rel);

    for (const raw of added) {
      if (out.length >= max) break;
      const line = (raw || '').trim();
      if (!line || isCommentLine(line)) continue;
      // Only check markers whose language(s) match the file's detected language(s).
      const hit = DISABLED_TEST_MARKERS.find(
        m => m.languages.some(lang => fileLanguages.includes(lang)) && m.re.test(line),
      );
      if (hit) out.push({ file: rel, marker: hit.kind, line });
    }
  }
  return out;
}
