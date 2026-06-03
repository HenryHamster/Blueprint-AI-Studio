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
import { detectTestRunners, classifyTestExecution } from './test-runners.js';
import type { BuildConfig } from './test-runners.js';

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
    fs.readdirSync.mockImplementation((p: string) =>
      p.endsWith('/services') ? ['api'] : [],
    );
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
    fs.readdirSync.mockImplementation((p: string) =>
      p.endsWith('/apps') ? ['web'] : [],
    );
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
    fs.readdirSync.mockImplementation((p: string) =>
      p.endsWith('/apps') ? ['web'] : [],
    );
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
    fs.readdirSync.mockImplementation((p: string) =>
      p.endsWith('/packages') ? ['core', 'svc'] : [],
    );

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(2);
    const byCwd = Object.fromEntries(runners.map(r => [r.cwd, r]));
    expect(byCwd['/tmp/workspace/packages/core'].cmd).toBe('cargo');
    expect(byCwd['/tmp/workspace/packages/core'].args).toEqual(['test']);
    expect(byCwd['/tmp/workspace/packages/svc'].cmd).toBe('go');
    expect(byCwd['/tmp/workspace/packages/svc'].args).toEqual(['test', './...']);
  });

  it('src/ as a single package directory (pytest.ini at src/) → python3 -m pytest at src', () => {
    const fs = mockFs;
    fs.existsSync.mockImplementation((p: string) =>
      p === '/tmp/workspace/src' || p === '/tmp/workspace/src/pytest.ini',
    );
    // readdirSync of src/ returns no child dirs → only the src-itself package fires.
    fs.readdirSync.mockReturnValue([]);

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
    fs.readdirSync.mockImplementation((p: string) =>
      p.endsWith('/services') ? ['a', 'b'] : [],
    );

    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(2);
    const cwds = runners.map(r => r.cwd).sort();
    expect(cwds).toEqual(['/tmp/workspace/services/a', '/tmp/workspace/services/b']);
  });

  it('nested package with no manifest → no runner (graceful)', () => {
    const fs = mockFs;
    fs.existsSync.mockImplementation((p: string) => p === '/tmp/workspace/services');
    fs.readdirSync.mockImplementation((p: string) =>
      p.endsWith('/services') ? ['empty-pkg'] : [],
    );

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
    fs.readdirSync.mockImplementation((p: string) =>
      p.endsWith('/services') ? ['api'] : [],
    );

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
