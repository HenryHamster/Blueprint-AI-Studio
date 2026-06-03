import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('child_process', () => ({
  execFileSync: vi.fn().mockReturnValue(''),
}));

vi.mock('../index.js', () => ({
  WORKSPACE: '/tmp/workspace',
  sendLog: vi.fn(),
}));

import { execFileSync } from 'child_process';
import { countSourceFiles, detectGreenfield, isGreenfieldRepo, verifyCloneDelivered } from './discovery.js';
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
  });

  it('is NOT greenfield when the durable signal explicitly says non-empty, even if the live clone is near-empty', () => {
    // The disagreement (durable: non-empty vs live: empty) must NEVER silently
    // resolve to greenfield — that would scaffold into a repo that has code.
    // The durable signal is authoritative; verifyCloneDelivered handles the
    // empty-tree case as a clone failure separately.
    setLsFiles(['package.json', 'README.md', 'tsconfig.json', '.gitignore']);
    expect(isGreenfieldRepo({ repo_is_empty: false })).toBe(false);
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

describe('verifyCloneDelivered', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  // Route the execFileSync mock by the git subcommand verifyCloneDelivered runs:
  //   git rev-parse HEAD            → head
  //   git rev-parse --verify origin/<branch> → originHead (or throw if undefined)
  //   git ls-files                  → lsFiles joined (via countSourceFiles)
  const routeGit = (opts: {
    head?: string | (() => never);
    originHead?: string | (() => never);
    lsFiles?: string[] | (() => never);
  }) => {
    vi.mocked(execFileSync).mockImplementation((_cmd, args) => {
      const a = Array.isArray(args) ? (args as string[]) : [];
      if (a[0] === 'rev-parse' && a[1] === 'HEAD') {
        if (typeof opts.head === 'function') return opts.head();
        return (opts.head ?? '') as never;
      }
      if (a[0] === 'rev-parse' && a[1] === '--verify') {
        if (typeof opts.originHead === 'function') return opts.originHead();
        if (opts.originHead === undefined) throw new Error('unknown revision');
        return opts.originHead as never;
      }
      if (a[0] === 'ls-files') {
        if (typeof opts.lsFiles === 'function') return opts.lsFiles();
        return ((opts.lsFiles ?? []).join('\n')) as never;
      }
      return '' as never;
    });
  };

  const populatedTree = Array.from({ length: GREENFIELD_SOURCE_FILE_THRESHOLD + 5 }, (_, i) => `src/m${i}.ts`);
  const nearEmptyTree = ['package.json', 'README.md', '.gitignore'];

  it('passes for a populated tree whose HEAD matches origin/<branch>', () => {
    routeGit({ head: 'abc123', originHead: 'abc123', lsFiles: populatedTree });
    expect(() => { verifyCloneDelivered({ repo_is_empty: false }, 'main'); }).not.toThrow();
  });

  it('throws clone_failed when the durable signal says non-empty but the tree is empty', () => {
    // The core bug: a successful-but-empty clone of a known-non-empty repo must
    // be treated as a clone problem, NOT a greenfield repo.
    routeGit({ head: 'abc123', originHead: 'abc123', lsFiles: nearEmptyTree });
    try {
      verifyCloneDelivered({ repo_is_empty: false }, 'main');
      throw new Error('expected verifyCloneDelivered to throw');
    } catch (e) {
      expect((e as Error & { failureCategory?: string }).failureCategory).toBe('clone_failed');
      expect((e as Error).message).toContain('did not deliver');
    }
  });

  it('passes for a genuinely empty repo when the durable signal is absent', () => {
    // repo_is_empty absent → no durable disagreement → empty tree is allowed
    // (the live greenfield detector legitimately concludes greenfield).
    routeGit({ head: 'abc123', originHead: 'abc123', lsFiles: nearEmptyTree });
    expect(() => { verifyCloneDelivered({}, 'main'); }).not.toThrow();
  });

  it('passes for a genuinely empty repo when the durable signal says empty', () => {
    routeGit({ head: 'abc123', originHead: 'abc123', lsFiles: nearEmptyTree });
    expect(() => { verifyCloneDelivered({ repo_is_empty: true }, 'main'); }).not.toThrow();
  });

  it('throws clone_failed when HEAD does not match origin/<branch> (partial/wrong checkout)', () => {
    routeGit({ head: 'abc123', originHead: 'def456', lsFiles: populatedTree });
    try {
      verifyCloneDelivered({ repo_is_empty: false }, 'main');
      throw new Error('expected verifyCloneDelivered to throw');
    } catch (e) {
      expect((e as Error & { failureCategory?: string }).failureCategory).toBe('clone_failed');
      expect((e as Error).message).toContain('does not match');
    }
  });

  it('throws clone_failed when HEAD cannot be resolved at all', () => {
    routeGit({ head: () => { throw new Error('fatal: not a git repository'); } });
    try {
      verifyCloneDelivered({ repo_is_empty: false }, 'main');
      throw new Error('expected verifyCloneDelivered to throw');
    } catch (e) {
      expect((e as Error & { failureCategory?: string }).failureCategory).toBe('clone_failed');
      expect((e as Error).message).toContain('no resolvable HEAD');
    }
  });

  it('does not fail on a missing origin/<branch> ref alone (durable check is the net)', () => {
    // origin ref absent (rev-parse --verify throws) but tree is populated and
    // durable says non-empty → no clone failure.
    routeGit({ head: 'abc123', originHead: () => { throw new Error('unknown revision'); }, lsFiles: populatedTree });
    expect(() => { verifyCloneDelivered({ repo_is_empty: false }, 'main'); }).not.toThrow();
  });

  it('does not claim a clone failure when the ls-files scan itself fails', () => {
    // countSourceFiles() → null (scan unavailable); we must not throw on a
    // failed scan, only on a confirmed empty tree.
    routeGit({ head: 'abc123', originHead: 'abc123', lsFiles: () => { throw new Error('git unavailable'); } });
    expect(() => { verifyCloneDelivered({ repo_is_empty: false }, 'main'); }).not.toThrow();
  });

  it('skips the branch check when no branch is provided', () => {
    routeGit({ head: 'abc123', lsFiles: populatedTree });
    expect(() => { verifyCloneDelivered({ repo_is_empty: false }); }).not.toThrow();
  });
});
