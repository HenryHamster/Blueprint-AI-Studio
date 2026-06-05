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
  classifySetupFailure, classifyTestExecution,
  matchesEnvironmentFailureSignature,
  detectAcceptanceProbes, classifyProbeOutcome,
} from './test-runners.js';
import type { BuildConfig, TestRunner } from './test-runners.js';

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
    expect(runners[0].cwd).toBe('/tmp/workspace');
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
    expect(runners[0].cwd).toBe('/tmp/workspace');
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
    expect(runners[0].cwd).toBe('/tmp/workspace');
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
    expect(runners[0].cwd).toBe('/tmp/workspace');
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
    expect(runners[0].cwd).toBe('/tmp/workspace');
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
    expect(runners[0].cwd).toBe('/tmp/workspace');
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
      p === '/tmp/workspace/services' || p.endsWith('/services/api/pyproject.toml'),
    );
    fs.readdirSync.mockImplementation((p: string) => {
      if (p === '/tmp/workspace') return ['services'];
      return p.endsWith('/services') ? ['api'] : [];
    });
    fs.readFileSync.mockImplementation((p: string) =>
      p.endsWith('pyproject.toml') ? '[tool.pytest.ini_options]\n' : '',
    );

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(1);
    expect(runners[0].cmd).toBe('python3');
    expect(runners[0].args).toEqual(['-m', 'pytest', '-x', '-q']);
    expect(runners[0].cwd).toBe('/tmp/workspace/services/api');
  });

  it('apps/web/package.json with scripts.test → npm test at apps/web', () => {
    const fs = mockFs;
    fs.existsSync.mockImplementation((p: string) =>
      p === '/tmp/workspace/apps' || p.endsWith('/apps/web/package.json'),
    );
    fs.readdirSync.mockImplementation((p: string) => {
      if (p === '/tmp/workspace') return ['apps'];
      return p.endsWith('/apps') ? ['web'] : [];
    });
    fs.readFileSync.mockImplementation((p: string) =>
      p.endsWith('package.json') ? JSON.stringify({ scripts: { test: 'vitest run' } }) : '',
    );

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(1);
    expect(runners[0].cmd).toBe('npm');
    expect(runners[0].args).toEqual(['test']);
    expect(runners[0].cwd).toBe('/tmp/workspace/apps/web');
  });

  it('apps/web/package.json WITHOUT scripts.test → no runner (conservative fall-through)', () => {
    const fs = mockFs;
    fs.existsSync.mockImplementation((p: string) =>
      p === '/tmp/workspace/apps' || p.endsWith('/apps/web/package.json'),
    );
    fs.readdirSync.mockImplementation((p: string) => {
      if (p === '/tmp/workspace') return ['apps'];
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
      p === '/tmp/workspace/packages' ||
      p.endsWith('/packages/core/Cargo.toml') ||
      p.endsWith('/packages/svc/go.mod'),
    );
    fs.readdirSync.mockImplementation((p: string) => {
      if (p === '/tmp/workspace') return ['packages'];
      return p.endsWith('/packages') ? ['core', 'svc'] : [];
    });

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(2);
    const byCwd = Object.fromEntries(runners.map(r => [r.cwd, r]));
    expect(byCwd['/tmp/workspace/packages/core'].cmd).toBe('cargo');
    expect(byCwd['/tmp/workspace/packages/core'].args).toEqual(['test']);
    expect(byCwd['/tmp/workspace/packages/svc'].cmd).toBe('go');
    expect(byCwd['/tmp/workspace/packages/svc'].args).toEqual(['test', './...']);
  });

  it('services/api/pom.xml → mvn test at services/api (maven nested)', () => {
    const fs = mockFs;
    fs.existsSync.mockImplementation((p: string) =>
      p === '/tmp/workspace/services' || p.endsWith('/services/api/pom.xml'),
    );
    fs.readdirSync.mockImplementation((p: string) => {
      if (p === '/tmp/workspace') return ['services'];
      return p.endsWith('/services') ? ['api'] : [];
    });

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(1);
    expect(runners[0].cmd).toBe('mvn');
    expect(runners[0].args).toEqual(['test']);
    expect(runners[0].cwd).toBe('/tmp/workspace/services/api');
  });

  it('packages/web/Gemfile + Rakefile → bundle exec rake test (ruby nested)', () => {
    const fs = mockFs;
    fs.existsSync.mockImplementation((p: string) =>
      p === '/tmp/workspace/packages' ||
      p.endsWith('/packages/web/Gemfile') ||
      p.endsWith('/packages/web/Rakefile'),
    );
    fs.readdirSync.mockImplementation((p: string) => {
      if (p === '/tmp/workspace') return ['packages'];
      return p.endsWith('/packages') ? ['web'] : [];
    });

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(1);
    expect(runners[0].cmd).toBe('bundle');
    expect(runners[0].args).toEqual(['exec', 'rake', 'test']);
    expect(runners[0].cwd).toBe('/tmp/workspace/packages/web');
  });

  it('apps/svc/*.csproj → dotnet test at apps/svc (.NET nested via readdir glob)', () => {
    const fs = mockFs;
    fs.existsSync.mockImplementation((p: string) => p === '/tmp/workspace/apps');
    fs.readdirSync.mockImplementation((p: string) => {
      if (p === '/tmp/workspace') return ['apps'];
      if (p.endsWith('/apps/svc')) return ['Service.csproj'];
      if (p.endsWith('/apps')) return ['svc'];
      return [];
    });

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(1);
    expect(runners[0].cmd).toBe('dotnet');
    expect(runners[0].args).toEqual(['test']);
    expect(runners[0].cwd).toBe('/tmp/workspace/apps/svc');
  });

  it('Go at repo ROOT (go.mod) → go test ./... at root', () => {
    const fs = mockFs;
    fs.existsSync.mockImplementation((p: string) => p === '/tmp/workspace/go.mod');

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(1);
    expect(runners[0].cmd).toBe('go');
    expect(runners[0].args).toEqual(['test', './...']);
    expect(runners[0].cwd).toBe('/tmp/workspace');
  });

  it('src/ as a single package directory (pytest.ini at src/) → python3 -m pytest at src', () => {
    const fs = mockFs;
    fs.existsSync.mockImplementation((p: string) =>
      p === '/tmp/workspace/src' || p === '/tmp/workspace/src/pytest.ini',
    );
    // Workspace root lists `src`; src/ itself has no child dirs → only the
    // src-itself package fires.
    fs.readdirSync.mockImplementation((p: string) =>
      p === '/tmp/workspace' ? ['src'] : [],
    );

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(1);
    expect(runners[0].cmd).toBe('python3');
    expect(runners[0].args).toEqual(['-m', 'pytest', '-x', '-q']);
    expect(runners[0].cwd).toBe('/tmp/workspace/src');
  });

  it('multiple sibling packages each emit a runner', () => {
    const fs = mockFs;
    fs.existsSync.mockImplementation((p: string) =>
      p === '/tmp/workspace/services' ||
      p.endsWith('/services/a/setup.cfg') ||
      p.endsWith('/services/b/Cargo.toml'),
    );
    fs.readdirSync.mockImplementation((p: string) => {
      if (p === '/tmp/workspace') return ['services'];
      return p.endsWith('/services') ? ['a', 'b'] : [];
    });
    fs.readFileSync.mockImplementation((p: string) =>
      p.endsWith('setup.cfg') ? '[tool:pytest]\naddopts = -q\n' : '',
    );

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(2);
    const cwds = runners.map(r => r.cwd).sort();
    expect(cwds).toEqual(['/tmp/workspace/services/a', '/tmp/workspace/services/b']);
  });

  it('nested package with no manifest → no runner (graceful)', () => {
    const fs = mockFs;
    fs.existsSync.mockImplementation((p: string) => p === '/tmp/workspace/services');
    fs.readdirSync.mockImplementation((p: string) => {
      if (p === '/tmp/workspace') return ['services'];
      return p.endsWith('/services') ? ['empty-pkg'] : [];
    });

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(0);
  });

  it('unreadable container directory → no runner (readdirSync throws, caught)', () => {
    const fs = mockFs;
    fs.existsSync.mockImplementation((p: string) => p === '/tmp/workspace/services');
    fs.readdirSync.mockImplementation(() => { throw new Error('EACCES'); });

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(0);
  });

  it('never emits a bare `pytest` cmd from the nested scan', () => {
    const fs = mockFs;
    fs.existsSync.mockImplementation((p: string) =>
      p === '/tmp/workspace/services' || p.endsWith('/services/api/pytest.ini'),
    );
    fs.readdirSync.mockImplementation((p: string) => {
      if (p === '/tmp/workspace') return ['services'];
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
      p === '/tmp/workspace/services' || p.endsWith('/services/api/pytest.ini'),
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
    expect(runners[0].cwd).toBe('/tmp/workspace');
    expect(runners[0].cmd).toBe('python3');
  });

  it('Blueprint backend layout still wins over a nested services/ layout (self-repo unchanged)', () => {
    const fs = mockFs;
    // Both the Blueprint backend layout AND a nested services/ package exist.
    fs.existsSync.mockImplementation((p: string) =>
      p.endsWith('/backend/pytest.ini') ||
      p.endsWith('/scripts/test-backend.sh') ||
      p === '/tmp/workspace/services' ||
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
      p === '/tmp/workspace/server' || p.endsWith('/server/api/pytest.ini'),
    );
    fs.readdirSync.mockImplementation((p: string) => {
      if (p === '/tmp/workspace') return ['server'];
      return p.endsWith('/server') ? ['api'] : [];
    });

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(1);
    expect(runners[0].cmd).toBe('python3');
    expect(runners[0].args).toEqual(['-m', 'pytest', '-x', '-q']);
    expect(runners[0].cwd).toBe('/tmp/workspace/server/api');
  });

  it('web/app/package.json with scripts.test (NON-allowlist) → npm test at web/app', () => {
    const fs = mockFs;
    fs.existsSync.mockImplementation((p: string) =>
      p === '/tmp/workspace/web' || p.endsWith('/web/app/package.json'),
    );
    fs.readdirSync.mockImplementation((p: string) => {
      if (p === '/tmp/workspace') return ['web'];
      return p.endsWith('/web') ? ['app'] : [];
    });
    fs.readFileSync.mockImplementation((p: string) =>
      p.endsWith('package.json') ? JSON.stringify({ scripts: { test: 'vitest run' } }) : '',
    );

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(1);
    expect(runners[0].cmd).toBe('npm');
    expect(runners[0].cwd).toBe('/tmp/workspace/web/app');
  });

  it('tools/cli/go.mod (NON-allowlist container) → go test ./... at tools/cli', () => {
    const fs = mockFs;
    fs.existsSync.mockImplementation((p: string) =>
      p === '/tmp/workspace/tools' || p.endsWith('/tools/cli/go.mod'),
    );
    fs.readdirSync.mockImplementation((p: string) => {
      if (p === '/tmp/workspace') return ['tools'];
      return p.endsWith('/tools') ? ['cli'] : [];
    });

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(1);
    expect(runners[0].cmd).toBe('go');
    expect(runners[0].args).toEqual(['test', './...']);
    expect(runners[0].cwd).toBe('/tmp/workspace/tools/cli');
  });

  it('app/Cargo.toml (singular, NON-allowlist top-level package) → cargo test at app', () => {
    const fs = mockFs;
    fs.existsSync.mockImplementation((p: string) =>
      p === '/tmp/workspace/app' || p.endsWith('/app/Cargo.toml'),
    );
    fs.readdirSync.mockImplementation((p: string) =>
      p === '/tmp/workspace' ? ['app'] : [],
    );

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(1);
    expect(runners[0].cmd).toBe('cargo');
    expect(runners[0].args).toEqual(['test']);
    expect(runners[0].cwd).toBe('/tmp/workspace/app');
  });

  it('skips vendored/dependency/build dirs and dotdirs (no spurious runner from node_modules)', () => {
    const fs = mockFs;
    // node_modules contains a package.json with a test script, but it must be
    // skipped — only the real suite under server/ should emit a runner.
    fs.existsSync.mockImplementation((p: string) =>
      p === '/tmp/workspace/server' ||
      p.endsWith('/server/api/pytest.ini') ||
      p.endsWith('/node_modules/foo/package.json') ||
      p.endsWith('/.git/pkg/package.json'),
    );
    fs.readdirSync.mockImplementation((p: string) => {
      if (p === '/tmp/workspace') return ['server', 'node_modules', '.git', 'vendor', '.venv'];
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
    expect(runners[0].cwd).toBe('/tmp/workspace/server/api');
    expect(runners[0].cmd).toBe('python3');
  });

  it('bounds the walk to ~40 directories (no runaway emission on a huge repo)', () => {
    const fs = mockFs;
    // 100 top-level package dirs, each carrying a real Cargo.toml. A bounded
    // walk must emit far fewer than 100 runners (cap is 40 inspected dirs).
    const topDirs = Array.from({ length: 100 }, (_, i) => `pkg${i}`);
    fs.existsSync.mockImplementation((p: string) => p.endsWith('/Cargo.toml'));
    fs.readdirSync.mockImplementation((p: string) =>
      p === '/tmp/workspace' ? topDirs : [],
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
      p === '/tmp/workspace/services' || p.endsWith('/services/api/pytest.ini'),
    );
    fs.readdirSync.mockImplementation((p: string) => {
      if (p === '/tmp/workspace') return ['services'];
      return p.endsWith('/services') ? ['api'] : [];
    });

    const auto = detectAutoTestRunners();
    const baseline = detectTestRunners(undefined);

    expect(auto).toEqual(baseline);
    expect(auto).toHaveLength(1);
    expect(auto[0].cmd).toBe('python3');
    expect(auto[0].cwd).toBe('/tmp/workspace/services/api');
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
    expect(describeRunnerDirectory(mk('python3', ['-m', 'pytest', '-x', '-q'], '/tmp/workspace/services/api')))
      .toEqual({ path: 'services/api', type: 'python', test_cmd: 'python3 -m pytest -x -q' });
  });

  it('maps a root runner to an empty path', () => {
    expect(describeRunnerDirectory(mk('npm', ['test'], '/tmp/workspace')))
      .toEqual({ path: '', type: 'node', test_cmd: 'npm test' });
  });

  it('derives type from the runner command kind', () => {
    expect(describeRunnerDirectory(mk('go', ['test', './...'], '/tmp/workspace/svc')).type).toBe('go');
    expect(describeRunnerDirectory(mk('cargo', ['test'], '/tmp/workspace/core')).type).toBe('rust');
    expect(describeRunnerDirectory(mk('tox', [], '/tmp/workspace')).type).toBe('python');
  });

  // make targets, bash/sh script suites, and unrecognized commands have no
  // dedicated language bucket; they MUST map to the schema-valid catch-all
  // 'other' so the recovered directory entry stays inside the
  // BuildConfigDirectoryEntry.type enum (^(python|node|go|rust|other)$) and does
  // not force the backend's schema-invalid raw-store fallback.
  it('maps make/shell/unknown commands to the schema-valid "other" type', () => {
    expect(describeRunnerDirectory(mk('make', ['test'], '/tmp/workspace')).type).toBe('other');
    expect(describeRunnerDirectory(mk('bash', ['test.sh'], '/tmp/workspace')).type).toBe('other');
    expect(describeRunnerDirectory(mk('sh', ['run-tests.sh'], '/tmp/workspace')).type).toBe('other');
    expect(describeRunnerDirectory(mk('gradle', ['test'], '/tmp/workspace')).type).toBe('other');
  });

  it('unwraps a `bash -c` runner back to the original test_cmd (not the wrapper)', () => {
    // A shell-composed test_cmd is run via `bash -c '<cmd>'`. The recovered
    // directory descriptor must reconstruct the ORIGINAL command so a stale
    // stored config converges onto what the operator wrote, and type keys off
    // the unwrapped first token.
    expect(describeRunnerDirectory(mk('bash', ['-c', 'go test ./... && go vet ./...'], '/tmp/workspace/core')))
      .toEqual({ path: 'core', type: 'go', test_cmd: 'go test ./... && go vet ./...' });
    expect(describeRunnerDirectory(mk('bash', ['-c', 'pytest -k "not slow" || true'], '/tmp/workspace/svc')))
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

  // ── Conservative invariant: any real pass/fail keeps it a real 'ran' ──
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
    } as BuildConfig);
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
    } as BuildConfig);
    expect(probes.filter(p => p.ecosystem === 'python' && p.cwd === WORKSPACE)).toHaveLength(1);
  });

  // ── declared build_cmd probes ──────────────────────────────────────────────
  it('build_config dir with build_cmd → declared probe (plain command tokenized)', () => {
    const probes = detectAcceptanceProbes({
      directories: [{ path: 'admin-ui', type: 'node', build_cmd: 'npm run build' }],
    } as BuildConfig);
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
    } as BuildConfig);
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
    } as BuildConfig);
    const declared = probes.find(p => p.declared);
    expect(declared?.ecosystem).toBe('build');
  });

  it('declared build_cmd is preferred over the generic probe for the same dir', () => {
    // Same dir declares both a build_cmd AND a python type hint: only the
    // declared probe is emitted — no competing generic compileall probe.
    const probes = detectAcceptanceProbes({
      directories: [{ path: 'backend', type: 'python', build_cmd: 'make build', test_cmd: 'pytest' }],
    } as BuildConfig);
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
    } as BuildConfig);
    const atRoot = probes.filter(p => p.cwd === WORKSPACE);
    expect(atRoot).toHaveLength(1);
    expect(atRoot[0].declared).toBe(true);
  });

  it('a dir with only test_cmd (no build_cmd) still emits the generic probe', () => {
    const probes = detectAcceptanceProbes({
      directories: [{ path: 'backend', type: 'python', test_cmd: 'pytest' }],
    } as BuildConfig);
    expect(probes.some(p => p.declared)).toBe(false);
    expect(probes.find(p => p.ecosystem === 'python')?.cmd).toBe('python3');
  });

  it('empty/whitespace build_cmd is ignored (no declared probe)', () => {
    const probes = detectAcceptanceProbes({
      directories: [{ path: 'backend', type: 'python', build_cmd: '   ', test_cmd: 'pytest' }],
    } as BuildConfig);
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
