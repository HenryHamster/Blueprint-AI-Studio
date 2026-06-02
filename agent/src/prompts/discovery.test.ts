import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('child_process', () => ({
  execFileSync: vi.fn().mockReturnValue(''),
}));

vi.mock('../index.js', () => ({
  WORKSPACE: '/tmp/workspace',
  sendLog: vi.fn(),
}));

import { execFileSync } from 'child_process';
import { countSourceFiles, detectGreenfield, isGreenfieldRepo } from './discovery.js';
import { GREENFIELD_SOURCE_FILE_THRESHOLD } from '../constants.js';

const setLsFiles = (files: string[]) =>
  vi.mocked(execFileSync).mockReturnValue(files.join('\n') as never);

describe('countSourceFiles', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('counts real source files and ignores config / scaffolding', () => {
    setLsFiles([
      'package.json',
      'package-lock.json',
      'README.md',
      'tsconfig.json',
      '.gitignore',
      'pnpm-lock.yaml',
      'pyproject.toml',
      'LICENSE',
      'Dockerfile',
      'src/index.ts', // the only real source file
    ]);
    expect(countSourceFiles()).toBe(1);
  });

  it('counts each real source file across languages', () => {
    setLsFiles([
      'src/a.ts',
      'src/b.tsx',
      'backend/app/main.py',
      'cmd/server/main.go',
      'lib/util.rs',
      'README.md', // excluded
    ]);
    expect(countSourceFiles()).toBe(5);
  });

  it('returns null when git ls-files fails', () => {
    vi.mocked(execFileSync).mockImplementation(() => {
      throw new Error('not a git repo');
    });
    expect(countSourceFiles()).toBeNull();
  });
});

describe('detectGreenfield', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('returns true for a config-only tree (no real source)', () => {
    setLsFiles(['package.json', 'README.md', 'tsconfig.json', '.gitignore']);
    expect(detectGreenfield()).toBe(true);
  });

  it('returns true at exactly the threshold', () => {
    const files = Array.from({ length: GREENFIELD_SOURCE_FILE_THRESHOLD }, (_, i) => `src/m${i}.ts`);
    setLsFiles(files);
    expect(detectGreenfield()).toBe(true);
  });

  it('returns false once source count exceeds the threshold', () => {
    const files = Array.from({ length: GREENFIELD_SOURCE_FILE_THRESHOLD + 1 }, (_, i) => `src/m${i}.ts`);
    setLsFiles(files);
    expect(detectGreenfield()).toBe(false);
  });

  it('returns false when the live scan cannot complete', () => {
    vi.mocked(execFileSync).mockImplementation(() => {
      throw new Error('git unavailable');
    });
    expect(detectGreenfield()).toBe(false);
  });
});

describe('isGreenfieldRepo', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('is greenfield when the durable signal is true even if the live clone has source', () => {
    // Source-rich tree (live detectGreenfield() would be false) — the durable
    // onboarding short-circuit still classifies it greenfield.
    const files = Array.from({ length: GREENFIELD_SOURCE_FILE_THRESHOLD + 5 }, (_, i) => `src/m${i}.ts`);
    setLsFiles(files);
    expect(isGreenfieldRepo({ repo_is_empty: true })).toBe(true);
  });

  it('is greenfield when the durable signal is absent but the live clone is near-empty', () => {
    // Config-only tree: durable signal absent, but live detection catches it
    // (lightly-scaffolded / pre-onboarding-signal repos).
    setLsFiles(['package.json', 'README.md', 'tsconfig.json', '.gitignore']);
    expect(isGreenfieldRepo({})).toBe(true);
    expect(isGreenfieldRepo({ repo_is_empty: false })).toBe(true);
  });

  it('is NOT greenfield when the durable signal is falsy and the live clone has source', () => {
    const files = Array.from({ length: GREENFIELD_SOURCE_FILE_THRESHOLD + 1 }, (_, i) => `src/m${i}.ts`);
    setLsFiles(files);
    expect(isGreenfieldRepo({ repo_is_empty: false })).toBe(false);
    expect(isGreenfieldRepo({})).toBe(false);
  });

  it('falls back to the durable signal when the live scan fails', () => {
    vi.mocked(execFileSync).mockImplementation(() => {
      throw new Error('git unavailable');
    });
    expect(isGreenfieldRepo({ repo_is_empty: true })).toBe(true);
    expect(isGreenfieldRepo({ repo_is_empty: false })).toBe(false);
    expect(isGreenfieldRepo({})).toBe(false);
  });
});
