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

/** Build a TestRunner argv from an operator-configured test command.
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
function runnerFromTestCmd(testCmd: string, cwd: string): { cmd: string; args: string[]; cwd: string } {
  if (SHELL_OPERATORS_RE.test(testCmd)) {
    return { cmd: 'bash', args: ['-c', testCmd], cwd };
  }
  const tokens = tokenizeCommand(testCmd);
  if (tokens.length === 0) return { cmd: 'bash', args: ['-c', testCmd], cwd };
  return { cmd: tokens[0], args: tokens.slice(1), cwd };
}

export function detectTestRunners(buildConfig?: BuildConfig): TestRunner[] {
  const { existsSync, readFileSync, readdirSync } = fs;
  const runners: TestRunner[] = [];

  // --- BuildConfig-based detection (takes precedence over auto-detection) ---
  if (buildConfig?.directories) {
    for (const dir of buildConfig.directories) {
      if (dir.test_cmd) {
        const cwd = dir.path ? `${WORKSPACE}/${dir.path}` : WORKSPACE;
        runners.push({ ...runnerFromTestCmd(dir.test_cmd, cwd), label: `${dir.test_cmd} (${dir.path || 'root'})` });
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
      const entries = _readdir(absDir) as unknown as string[];
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
        topLevelEntries = readdirSync(WORKSPACE) as unknown as string[];
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
            children = readdirSync(`${WORKSPACE}/${entry}`) as unknown as string[];
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
 * CONSERVATIVE BY DESIGN: returns 'no_tests' ONLY on a positive signature match.
 * Anything else — including a genuine failure like "1 failed" or "2 passed",
 * empty output, or an unrecognized format — defaults to 'ran' so that a real
 * verdict is never misread as "no tests".
 */
export function classifyTestExecution(output: string): 'ran' | 'no_tests' {
  const lower = output.toLowerCase();
  const lines = output.split(/\r?\n/);
  const trimmedLines = lines.map(line => line.trim());
  // npm placeholder: the default `package.json` test script prints
  // "Error: no test specified" and exits 1 without running anything. Anchor to
  // the runner-emitted line shape so a real suite that merely asserts on the
  // literal "Error: no test specified" as data (e.g. `AssertionError: expected
  // 'Error: no test specified' in output`) stays a real run.
  const hasRealFailureEvidence = trimmedLines.some(line => (
    /^FAILED\s+\S+/.test(line) ||
    /^FAIL\s{2,}\S+/.test(line) ||
    /^[✕✗×⨯]\s+\S+/.test(line) ||
    /^AssertionError:/i.test(line) ||
    /^Traceback \(most recent call last\):/i.test(line) ||
    /\b[1-9]\d*\s+failed\b/i.test(line) ||
    /\btest result:\s+ok\.\s+[1-9]\d*\s+passed;\s+[1-9]\d*\s+failed\b/i.test(line)
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
  const missingCommandPatterns = [
    /^(?:bash|sh):\s+\S+:\s+command not found\s*$/i,
    /^zsh:\s+command not found:\s+\S+\s*$/i,
    /^error:\s+command not found:\s+\S+\s*$/i,
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

// isDockerRunner is used by callers in index.ts via re-export
export { isDockerRunner };
