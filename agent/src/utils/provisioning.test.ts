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
  },
}));

const mockFs = fs as unknown as { existsSync: ReturnType<typeof vi.fn> };
const WORKSPACE = process.env.AGENT_WORKSPACE || '/tmp/workspace';

describe('detectProvisioningSteps', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockFs.existsSync.mockReturnValue(false);
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

  it('does NOT auto-provision when a lockfile or requirements.txt is present', () => {
    mockFs.existsSync.mockImplementation((p: string) =>
      p === `${WORKSPACE}/pyproject.toml` || p === `${WORKSPACE}/requirements.txt`,
    );
    expect(detectProvisioningSteps(undefined)).toHaveLength(0);
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
  // (bundle exec / gradle / mvn / mix / composer) but none of those commands
  // installs its own deps, so the gate must provision them first.
  it.each([
    ['Gemfile', 'bundle', ['install']],
    ['build.gradle', './gradlew', ['dependencies', '--no-daemon']],
    ['build.gradle.kts', './gradlew', ['dependencies', '--no-daemon']],
    ['pom.xml', 'mvn', ['-B', 'dependency:go-offline']],
    ['mix.exs', 'mix', ['deps.get']],
    ['composer.json', 'composer', ['install']],
  ])('auto-provisions an extra-ecosystem manifest %s with %s', (manifest, cmd, args) => {
    mockFs.existsSync.mockImplementation((p: string) => p === `${WORKSPACE}/${manifest}`);
    const steps = detectProvisioningSteps(undefined);
    expect(steps).toHaveLength(1);
    expect(steps[0].cmd).toBe(cmd);
    expect(steps[0].args).toEqual(args);
    expect(steps[0].cwd).toBe(WORKSPACE);
    expect(steps[0].declared).toBeUndefined();
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
});
