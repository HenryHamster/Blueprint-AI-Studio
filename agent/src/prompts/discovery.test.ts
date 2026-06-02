import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('child_process', () => ({
  execFileSync: vi.fn().mockReturnValue(''),
}));

vi.mock('../index.js', () => ({
  WORKSPACE: '/tmp/workspace',
  sendLog: vi.fn(),
}));

import { execFileSync } from 'child_process';
import { countSourceFiles, detectGreenfield } from './discovery.js';
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

  it('returns 0 when git ls-files fails', () => {
    vi.mocked(execFileSync).mockImplementation(() => {
      throw new Error('not a git repo');
    });
    expect(countSourceFiles()).toBe(0);
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
});
