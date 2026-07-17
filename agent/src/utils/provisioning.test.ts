/**
 * Tests for detectProvisioningSteps — the no-CI verify gate's dependency
 * provisioning detector. Covers the operator-declared install_cmd path and the
 * pyproject-only Python auto-provision the per-runner hardcoded installs miss.
 *
 * fs is mocked per-test so the real filesystem is never consulted.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import fs from 'fs';
import { detectProvisioningSteps } from './test-runners.js';
import type { BuildConfig } from './test-runners.js';

vi.mock('fs', () => ({
  default: {
    existsSync: vi.fn(() => false),
    readFileSync: vi.fn(() => ''),
    readdirSync: vi.fn(() => []),
    statSync: vi.fn(() => ({ isDirectory: () => true })),
  },
}));

const mockFs = fs as unknown as {
  existsSync: ReturnType<typeof vi.fn>;
  readdirSync: ReturnType<typeof vi.fn>;
  statSync: ReturnType<typeof vi.fn>;
};
const WORKSPACE = process.env.AGENT_WORKSPACE || '/tmp/workspace';

describe('detectProvisioningSteps', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockFs.existsSync.mockReturnValue(false);
    // readdirSync drives the glob-based .NET detector; reset its implementation
    // each test (clearAllMocks does not clear mockImplementation) so a per-test
    // readdir stub never leaks into the next test.
    mockFs.readdirSync.mockReset();
    mockFs.readdirSync.mockReturnValue([]);
    mockFs.statSync.mockReset();
    mockFs.statSync.mockReturnValue({ isDirectory: () => true });
  });

  it('emits the operator-declared install_cmd as an authoritative step', () => {
    const buildConfig: BuildConfig = {
      directories: [{ path: '', type: 'python', install_cmd: 'make setup' }],
    };
    const steps = detectProvisioningSteps(buildConfig);
    expect(steps).toHaveLength(1);
    expect(steps[0].cmd).toBe('make');
    expect(steps[0].args).toEqual(['setup']);
    expect(steps[0].cwd).toBe(WORKSPACE);
    expect(steps[0].declared).toBe(true);
  });

  it('wraps a shell-operator install_cmd via bash -c (no shell interpolation)', () => {
    const buildConfig: BuildConfig = {
      directories: [{ path: 'pkg', type: 'node', install_cmd: 'npm ci && npm run prepare' }],
    };
    const steps = detectProvisioningSteps(buildConfig);
    expect(steps).toHaveLength(1);
    expect(steps[0].cmd).toBe('bash');
    expect(steps[0].args).toEqual(['-c', 'npm ci && npm run prepare']);
    expect(steps[0].cwd).toBe(`${WORKSPACE}/pkg`);
  });

  it('skips a stale removed directory while preserving valid sibling provisioning', () => {
    mockFs.statSync.mockImplementation((p: string) => {
      if (p === `${WORKSPACE}/local-domexception`) throw new Error('ENOENT');
      return { isDirectory: () => p === `${WORKSPACE}/agent` };
    });
    const buildConfig: BuildConfig = {
      directories: [
        { path: 'local-domexception', type: 'node', install_cmd: 'npm ci' },
        { path: 'agent', type: 'node', install_cmd: 'npm ci' },
      ],
    };

    expect(detectProvisioningSteps(buildConfig)).toEqual([
      expect.objectContaining({ cmd: 'npm', args: ['ci'], cwd: `${WORKSPACE}/agent`, declared: true }),
    ]);
  });

  it('skips a configured path that exists but is not a directory', () => {
    mockFs.statSync.mockReturnValue({ isDirectory: () => false });
    const buildConfig: BuildConfig = {
      directories: [{ path: 'package.json', type: 'node', install_cmd: 'npm ci' }],
    };

    expect(detectProvisioningSteps(buildConfig)).toEqual([]);
  });

  it('auto-provisions a pyproject-only Python project with pip install .', () => {
    // pyproject.toml present, no poetry.lock / requirements.txt.
    mockFs.existsSync.mockImplementation((p: string) => p === `${WORKSPACE}/pyproject.toml`);
    const steps = detectProvisioningSteps(undefined);
    expect(steps).toHaveLength(1);
    expect(steps[0].cmd).toBe('pip');
    expect(steps[0].args).toEqual(['install', '.']);
    expect(steps[0].cwd).toBe(WORKSPACE);
    expect(steps[0].declared).toBeUndefined();
  });

  it('auto-provisions Poetry with poetry install when poetry.lock is present', () => {
    // poetry.lock + pyproject.toml signals Poetry manages deps — auto-provision with poetry install.
    mockFs.existsSync.mockImplementation((p: string) =>
      p === `${WORKSPACE}/pyproject.toml` || p === `${WORKSPACE}/poetry.lock`,
    );
    const steps = detectProvisioningSteps(undefined);
    expect(steps).toHaveLength(1);
    expect(steps[0].cmd).toBe('poetry');
    expect(steps[0].args).toEqual(['install']);
    expect(steps[0].cwd).toBe(WORKSPACE);
    expect(steps[0].declared).toBeUndefined();
  });

  it('declared install_cmd suppresses Poetry auto-provision for the same dir', () => {
    mockFs.existsSync.mockImplementation((p: string) =>
      p === `${WORKSPACE}/pyproject.toml` || p === `${WORKSPACE}/poetry.lock`,
    );
    const buildConfig: BuildConfig = {
      directories: [{ path: '', type: 'python', install_cmd: 'poetry install --no-root' }],
    };
    const steps = detectProvisioningSteps(buildConfig);
    expect(steps).toHaveLength(1);
    expect(steps[0].declared).toBe(true);
    expect(steps[0].cmd).toBe('poetry');
  });

  it('auto-provisions Pipenv with pipenv install --system when Pipfile is present', () => {
    // Pipfile signals Pipenv manages deps — auto-provision with pipenv install --system
    // (--system installs to ambient interpreter, not a virtualenv).
    mockFs.existsSync.mockImplementation((p: string) => p === `${WORKSPACE}/Pipfile`);
    const steps = detectProvisioningSteps(undefined);
    expect(steps).toHaveLength(1);
    expect(steps[0].cmd).toBe('pipenv');
    expect(steps[0].args).toEqual(['install', '--system']);
    expect(steps[0].cwd).toBe(WORKSPACE);
    expect(steps[0].declared).toBeUndefined();
  });

  it('declared install_cmd suppresses Pipenv auto-provision for the same dir', () => {
    mockFs.existsSync.mockImplementation((p: string) => p === `${WORKSPACE}/Pipfile`);
    const buildConfig: BuildConfig = {
      directories: [{ path: '', type: 'python', install_cmd: 'pipenv install --system --skip-lock' }],
    };
    const steps = detectProvisioningSteps(buildConfig);
    expect(steps).toHaveLength(1);
    expect(steps[0].declared).toBe(true);
    expect(steps[0].cmd).toBe('pipenv');
    expect(steps[0].args).toEqual(['install', '--system', '--skip-lock']);
  });

  it('auto-provisions a bare setup.py repo with pip install .', () => {
    // setup.py without pyproject.toml, poetry.lock, or requirements.txt → pip install .
    mockFs.existsSync.mockImplementation((p: string) => p === `${WORKSPACE}/setup.py`);
    const steps = detectProvisioningSteps(undefined);
    expect(steps).toHaveLength(1);
    expect(steps[0].cmd).toBe('pip');
    expect(steps[0].args).toEqual(['install', '.']);
    expect(steps[0].cwd).toBe(WORKSPACE);
    expect(steps[0].declared).toBeUndefined();
  });

  it('auto-provisions a bare setup.cfg repo with pip install .', () => {
    // setup.cfg without pyproject.toml, poetry.lock, or requirements.txt → pip install .
    mockFs.existsSync.mockImplementation((p: string) => p === `${WORKSPACE}/setup.cfg`);
    const steps = detectProvisioningSteps(undefined);
    expect(steps).toHaveLength(1);
    expect(steps[0].cmd).toBe('pip');
    expect(steps[0].args).toEqual(['install', '.']);
    expect(steps[0].cwd).toBe(WORKSPACE);
  });

  it('does NOT auto-provision bare setup.py when pyproject.toml is present', () => {
    // pyproject.toml takes precedence (pyproject-only or poetry branch fires instead)
    mockFs.existsSync.mockImplementation((p: string) =>
      p === `${WORKSPACE}/pyproject.toml` || p === `${WORKSPACE}/setup.py`,
    );
    const steps = detectProvisioningSteps(undefined);
    expect(steps).toHaveLength(1);
    expect(steps[0].cmd).toBe('pip');
    expect(steps[0].args).toEqual(['install', '.']);
    // This is from pyproject-only (no poetry.lock, no requirements.txt)
  });

  it('does NOT auto-provision bare setup.py when Pipfile is present', () => {
    // Pipfile takes precedence over bare setup.py (Pipenv branch fires instead)
    mockFs.existsSync.mockImplementation((p: string) =>
      p === `${WORKSPACE}/Pipfile` || p === `${WORKSPACE}/setup.py`,
    );
    const steps = detectProvisioningSteps(undefined);
    expect(steps).toHaveLength(1);
    expect(steps[0].cmd).toBe('pipenv');
    expect(steps[0].args).toEqual(['install', '--system']);
  });

  // requirements.txt auto-provision: a Python repo without a pyproject.toml (or with one)
  // but with a requirements.txt should provision before probes run.
  it('auto-provisions a requirements.txt project with pip install -r requirements.txt', () => {
    mockFs.existsSync.mockImplementation((p: string) => p === `${WORKSPACE}/requirements.txt`);
    const steps = detectProvisioningSteps(undefined);
    expect(steps).toHaveLength(1);
    expect(steps[0].cmd).toBe('pip');
    expect(steps[0].args).toEqual(['install', '-r', 'requirements.txt']);
    expect(steps[0].cwd).toBe(WORKSPACE);
    expect(steps[0].declared).toBeUndefined();
  });

  it('provisions via requirements-dev.txt when requirements.txt is absent', () => {
    mockFs.existsSync.mockImplementation((p: string) => p === `${WORKSPACE}/requirements-dev.txt`);
    const steps = detectProvisioningSteps(undefined);
    expect(steps).toHaveLength(1);
    expect(steps[0].cmd).toBe('pip');
    expect(steps[0].args).toEqual(['install', '-r', 'requirements-dev.txt']);
    expect(steps[0].cwd).toBe(WORKSPACE);
  });

  it('provisions via requirements/base.txt when top-level requirements files are absent', () => {
    mockFs.existsSync.mockImplementation((p: string) => p === `${WORKSPACE}/requirements/base.txt`);
    const steps = detectProvisioningSteps(undefined);
    expect(steps).toHaveLength(1);
    expect(steps[0].cmd).toBe('pip');
    expect(steps[0].args).toEqual(['install', '-r', 'requirements/base.txt']);
    expect(steps[0].cwd).toBe(WORKSPACE);
  });

  it('prefers requirements.txt over requirements-dev.txt (requirements file priority)', () => {
    mockFs.existsSync.mockImplementation((p: string) =>
      p === `${WORKSPACE}/requirements.txt` || p === `${WORKSPACE}/requirements-dev.txt`,
    );
    const steps = detectProvisioningSteps(undefined);
    expect(steps).toHaveLength(1);
    expect(steps[0].args).toEqual(['install', '-r', 'requirements.txt']);
  });

  it('emits requirements.txt step (not pyproject pip install .) when both are present', () => {
    // pyproject-only branch is suppressed because requirements.txt exists;
    // requirements.txt branch fires instead.
    mockFs.existsSync.mockImplementation((p: string) =>
      p === `${WORKSPACE}/pyproject.toml` || p === `${WORKSPACE}/requirements.txt`,
    );
    const steps = detectProvisioningSteps(undefined);
    expect(steps).toHaveLength(1);
    expect(steps[0].args).toEqual(['install', '-r', 'requirements.txt']);
  });

  it('declared install_cmd suppresses the requirements.txt fallback for the same dir', () => {
    mockFs.existsSync.mockImplementation((p: string) => p === `${WORKSPACE}/requirements.txt`);
    const buildConfig: BuildConfig = {
      directories: [{ path: '', type: 'python', install_cmd: 'pip install -r requirements.txt --no-deps' }],
    };
    const steps = detectProvisioningSteps(buildConfig);
    expect(steps).toHaveLength(1);
    expect(steps[0].declared).toBe(true);
  });

  it('declared install_cmd suppresses the pyproject-only fallback for the same dir', () => {
    mockFs.existsSync.mockImplementation((p: string) => p === `${WORKSPACE}/pyproject.toml`);
    const buildConfig: BuildConfig = {
      directories: [{ path: '', type: 'python', install_cmd: 'poetry install' }],
    };
    const steps = detectProvisioningSteps(buildConfig);
    expect(steps).toHaveLength(1);
    expect(steps[0].declared).toBe(true);
    expect(steps[0].cmd).toBe('poetry');
  });

  it('returns no steps when nothing is declared and no pyproject is present', () => {
    expect(detectProvisioningSteps(undefined)).toEqual([]);
    expect(detectProvisioningSteps({ directories: [{ path: '', type: 'node' }] })).toEqual([]);
  });

  // Extra-ecosystem auto-provision: the suite detector launches these runners
  // (bundle exec / gradle / mvn / mix / composer / go / cargo) but none of those
  // commands installs its own deps, so the gate must provision them first.
  it.each([
    ['Gemfile', 'bundle', ['install']],
    ['build.gradle', './gradlew', ['dependencies', '--no-daemon']],
    ['build.gradle.kts', './gradlew', ['dependencies', '--no-daemon']],
    ['pom.xml', 'mvn', ['-B', 'dependency:go-offline']],
    ['mix.exs', 'mix', ['deps.get']],
    ['composer.json', 'composer', ['install']],
    ['go.mod', 'go', ['mod', 'download']],
    ['Cargo.toml', 'cargo', ['fetch']],
  ])('auto-provisions an extra-ecosystem manifest %s with %s', (manifest, cmd, args) => {
    mockFs.existsSync.mockImplementation((p: string) => p === `${WORKSPACE}/${manifest}`);
    const steps = detectProvisioningSteps(undefined);
    expect(steps).toHaveLength(1);
    expect(steps[0].cmd).toBe(cmd);
    expect(steps[0].args).toEqual(args);
    expect(steps[0].cwd).toBe(WORKSPACE);
    expect(steps[0].declared).toBeUndefined();
  });

  // .NET is glob-detected (*.sln / *.csproj), not a fixed manifest name, so it
  // keys off readdirSync rather than existsSync like the other extra families.
  it('auto-provisions a .NET project (*.csproj) with dotnet restore', () => {
    const mockReaddir = fs.readdirSync as unknown as ReturnType<typeof vi.fn>;
    mockReaddir.mockImplementation((p: string) =>
      p === WORKSPACE ? ['App.csproj', 'Program.cs'] : [],
    );
    const steps = detectProvisioningSteps(undefined);
    expect(steps).toHaveLength(1);
    expect(steps[0].cmd).toBe('dotnet');
    expect(steps[0].args).toEqual(['restore']);
    expect(steps[0].cwd).toBe(WORKSPACE);
    expect(steps[0].declared).toBeUndefined();
  });

  it('declared install_cmd suppresses the .NET restore fallback for the same dir', () => {
    const mockReaddir = fs.readdirSync as unknown as ReturnType<typeof vi.fn>;
    mockReaddir.mockImplementation((p: string) => (p === WORKSPACE ? ['Sln.sln'] : []));
    const buildConfig: BuildConfig = {
      directories: [{ path: '', type: 'other', install_cmd: 'dotnet restore --no-cache' }],
    };
    const steps = detectProvisioningSteps(buildConfig);
    expect(steps).toHaveLength(1);
    expect(steps[0].declared).toBe(true);
    expect(steps[0].args).toEqual(['restore', '--no-cache']);
  });

  it('declared install_cmd suppresses the extra-ecosystem fallback for the same dir', () => {
    mockFs.existsSync.mockImplementation((p: string) => p === `${WORKSPACE}/Gemfile`);
    const buildConfig: BuildConfig = {
      directories: [{ path: '', type: 'other', install_cmd: 'bundle install --frozen' }],
    };
    const steps = detectProvisioningSteps(buildConfig);
    expect(steps).toHaveLength(1);
    expect(steps[0].declared).toBe(true);
    expect(steps[0].args).toEqual(['install', '--frozen']);
  });

  it('detects an extra-ecosystem manifest under a build_config directory', () => {
    mockFs.existsSync.mockImplementation((p: string) => p === `${WORKSPACE}/api/composer.json`);
    const steps = detectProvisioningSteps({ directories: [{ path: 'api', type: 'other' }] });
    expect(steps).toHaveLength(1);
    expect(steps[0].cmd).toBe('composer');
    expect(steps[0].cwd).toBe(`${WORKSPACE}/api`);
  });

  // Node/JS auto-provision: install node_modules ONCE up front so the suite (any
  // test command, not just literal `npm`) AND the cheap probes run provisioned.
  // The committed lockfile selects the package manager.
  it.each([
    ['yarn.lock', 'yarn', ['install', '--frozen-lockfile']],
    ['pnpm-lock.yaml', 'pnpm', ['install', '--frozen-lockfile']],
    ['package-lock.json', 'npm', ['ci']],
  ])('selects the package manager from the committed lockfile %s', (lockfile, cmd, args) => {
    mockFs.existsSync.mockImplementation((p: string) =>
      p === `${WORKSPACE}/package.json` || p === `${WORKSPACE}/${lockfile}`,
    );
    const steps = detectProvisioningSteps(undefined);
    expect(steps).toHaveLength(1);
    expect(steps[0].cmd).toBe(cmd);
    expect(steps[0].args).toEqual(args);
    expect(steps[0].cwd).toBe(WORKSPACE);
    expect(steps[0].declared).toBeUndefined();
  });

  it('falls back to npm install for a bare package.json with no lockfile', () => {
    mockFs.existsSync.mockImplementation((p: string) => p === `${WORKSPACE}/package.json`);
    const steps = detectProvisioningSteps(undefined);
    expect(steps).toHaveLength(1);
    expect(steps[0].cmd).toBe('npm');
    expect(steps[0].args).toEqual(['install']);
    expect(steps[0].cwd).toBe(WORKSPACE);
  });

  it('prefers yarn.lock over pnpm-lock.yaml and package-lock.json (lockfile precedence)', () => {
    mockFs.existsSync.mockImplementation((p: string) =>
      p === `${WORKSPACE}/package.json` ||
      p === `${WORKSPACE}/yarn.lock` ||
      p === `${WORKSPACE}/pnpm-lock.yaml` ||
      p === `${WORKSPACE}/package-lock.json`,
    );
    const steps = detectProvisioningSteps(undefined);
    expect(steps).toHaveLength(1);
    expect(steps[0].cmd).toBe('yarn');
  });

  it('declared install_cmd suppresses the Node fallback for the same dir', () => {
    mockFs.existsSync.mockImplementation((p: string) =>
      p === `${WORKSPACE}/package.json` || p === `${WORKSPACE}/package-lock.json`,
    );
    const buildConfig: BuildConfig = {
      directories: [{ path: '', type: 'node', install_cmd: 'npm ci --legacy-peer-deps' }],
    };
    const steps = detectProvisioningSteps(buildConfig);
    expect(steps).toHaveLength(1);
    expect(steps[0].declared).toBe(true);
    expect(steps[0].args).toEqual(['ci', '--legacy-peer-deps']);
  });

  it('detects a Node manifest under a build_config directory', () => {
    mockFs.existsSync.mockImplementation((p: string) =>
      p === `${WORKSPACE}/web/package.json` || p === `${WORKSPACE}/web/pnpm-lock.yaml`,
    );
    const steps = detectProvisioningSteps({ directories: [{ path: 'web', type: 'node' }] });
    expect(steps).toHaveLength(1);
    expect(steps[0].cmd).toBe('pnpm');
    expect(steps[0].cwd).toBe(`${WORKSPACE}/web`);
  });
});
