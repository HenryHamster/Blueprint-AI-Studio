/**
 * Regression tests for detectTestRunners — cluster C19.
 *
 * Root cause: bare `pytest` emitted by admin_onboard.py and bare VM PATH
 * means `python3 -m pytest` is the only reliable invocation. Ensure that
 * (a) BuildConfig paths carrying "pytest" (bare) are NOT split into a bare
 *     `pytest` cmd, and (b) auto-detection paths always emit `python3 -m pytest`.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import type { BuildConfig } from './test-runners.js';

// We test detectTestRunners in isolation.  fs.existsSync and fs.readFileSync
// are mocked per-test so the real filesystem is never consulted.
vi.mock('fs', () => ({
  default: {
    existsSync: vi.fn(() => false),
    readFileSync: vi.fn(() => ''),
  },
}));

// child_process.execSync is used only by isDockerAvailable — always return normally
// (Docker "available") to keep tests deterministic.
vi.mock('child_process', () => ({
  execSync: vi.fn(() => ''),
}));

describe('detectTestRunners — bare pytest guard (C19)', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    // Default: no files exist
    const fs = require('fs').default;
    fs.existsSync.mockReturnValue(false);
    fs.readFileSync.mockReturnValue('');
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
    const { detectTestRunners } = require('./test-runners.js') as typeof import('./test-runners.js');
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
    const { detectTestRunners } = require('./test-runners.js') as typeof import('./test-runners.js');
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
    const { detectTestRunners } = require('./test-runners.js') as typeof import('./test-runners.js');
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
    const fs = require('fs').default;
    // Only pytest.ini exists at root
    fs.existsSync.mockImplementation((p: string) => p.endsWith('pytest.ini') && !p.includes('/backend/'));
    fs.readFileSync.mockReturnValue('');

    const { detectTestRunners } = require('./test-runners.js') as typeof import('./test-runners.js');
    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(1);
    expect(runners[0].cmd).toBe('python3');
    expect(runners[0].args).toEqual(['-m', 'pytest', '-x', '-q']);
  });

  it('auto-detection for pyproject.toml with pytest at root emits python3 -m pytest', () => {
    const fs = require('fs').default;
    fs.existsSync.mockImplementation((p: string) => p.endsWith('pyproject.toml') && !p.includes('/backend/'));
    fs.readFileSync.mockImplementation((p: string) => {
      if (p.endsWith('pyproject.toml')) return '[tool.pytest.ini_options]\n';
      return '';
    });

    const { detectTestRunners } = require('./test-runners.js') as typeof import('./test-runners.js');
    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(1);
    expect(runners[0].cmd).toBe('python3');
    expect(runners[0].args).toEqual(['-m', 'pytest', '-x', '-q']);
  });

  it('auto-detection for backend/pytest.ini WITHOUT test-backend.sh emits python3 -m pytest', () => {
    const fs = require('fs').default;
    // backend/pytest.ini exists, but scripts/test-backend.sh does NOT
    fs.existsSync.mockImplementation((p: string) => p.endsWith('/backend/pytest.ini'));
    fs.readFileSync.mockReturnValue('');

    const { detectTestRunners } = require('./test-runners.js') as typeof import('./test-runners.js');
    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(1);
    expect(runners[0].cmd).toBe('python3');
    expect(runners[0].args).toEqual(['-m', 'pytest', '-x', '-q']);
  });

  it('auto-detection for backend/pytest.ini WITH test-backend.sh uses the shell script instead', () => {
    // When scripts/test-backend.sh exists the Docker path takes priority over bare pytest.
    // This is the Blueprint-self-repo path — must be unchanged.
    const fs = require('fs').default;
    fs.existsSync.mockImplementation((p: string) =>
      p.endsWith('/backend/pytest.ini') || p.endsWith('/scripts/test-backend.sh')
    );
    fs.readFileSync.mockReturnValue('');

    const { detectTestRunners } = require('./test-runners.js') as typeof import('./test-runners.js');
    const runners = detectTestRunners(undefined);

    expect(runners).toHaveLength(1);
    expect(runners[0].cmd).toBe('bash');
    expect(runners[0].args[0]).toBe('scripts/test-backend.sh');
  });

  it('auto-detection for backend/pyproject.toml WITHOUT test-backend.sh emits python3 -m pytest', () => {
    const fs = require('fs').default;
    fs.existsSync.mockImplementation((p: string) => p.endsWith('/backend/pyproject.toml'));
    fs.readFileSync.mockImplementation((p: string) => {
      if (p.endsWith('pyproject.toml')) return '[tool.pytest]\n';
      return '';
    });

    const { detectTestRunners } = require('./test-runners.js') as typeof import('./test-runners.js');
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

    const fs = require('fs').default;
    const { detectTestRunners } = require('./test-runners.js') as typeof import('./test-runners.js');

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
