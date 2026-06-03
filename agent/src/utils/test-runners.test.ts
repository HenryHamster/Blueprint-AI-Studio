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
  detectTestRunners, detectAutoTestRunners, describeRunnerDirectory, classifyTestExecution,
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

  it('a real failing test ("1 failed") → ran (never misread as no_tests)', () => {
    expect(classifyTestExecution(
      'Test Files  1 failed (1)\n     Tests  1 failed | 2 passed (3)',
    )).toBe('ran');
  });

  it('a real passing run ("2 passed") → ran', () => {
    expect(classifyTestExecution('2 passed in 0.50s')).toBe('ran');
  });

  it('empty / ambiguous output → ran (conservative default)', () => {
    expect(classifyTestExecution('')).toBe('ran');
    expect(classifyTestExecution('some unrelated output')).toBe('ran');
  });
});
