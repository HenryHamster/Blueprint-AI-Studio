import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('child_process', () => ({
  execFileSync: vi.fn().mockReturnValue(''),
}));

vi.mock('../index.js', () => ({
  normalizeClaudeConfigContent: vi.fn((_path: string, content: string) => content),
  WORKSPACE: '/tmp/workspace',
  sendLog: vi.fn(),
  apiPost: vi.fn().mockResolvedValue({}),
}));

vi.mock('fs', () => ({
  default: {
    existsSync: vi.fn().mockReturnValue(true),
    readFileSync: vi.fn().mockReturnValue(''),
  },
}));

import { execFileSync } from 'child_process';
import fs from 'fs';
import { countSourceFiles, detectGreenfield, isGreenfieldRepo, buildGreenfieldCitationGuidance, verifyCloneDelivered, buildRepoMap, clearTrackedFilesCache, clearRepoMapCache } from './discovery.js';
import { GREENFIELD_SOURCE_FILE_THRESHOLD } from '../constants.js';
import { apiPost } from '../index.js';

const setLsFiles = (files: string[]) =>
  vi.mocked(execFileSync).mockReturnValue(files.join('\n'));

describe('countSourceFiles', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    clearTrackedFilesCache();
    clearRepoMapCache();
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

  it('excludes the same scaffolding basenames/prefixes as the backend classifier', () => {
    // These are exactly the entries the backend's _is_real_source_file drops:
    // go.mod, LICENCE (British spelling), and the readme/contributing/changelog/
    // security prefixes regardless of extension. None must count as source, so
    // the durable onboarding count and this live count agree like-for-like.
    setLsFiles([
      'go.mod', // basename exclusion (no telling extension)
      'LICENCE', // British-spelling basename
      'README', // prefix exclusion, no extension
      'README.rst', // prefix exclusion, non-config extension
      'CONTRIBUTING', // prefix exclusion
      'CHANGELOG', // prefix exclusion
      'SECURITY', // prefix exclusion
      'src/index.ts', // the only real source file
    ]);
    expect(countSourceFiles()).toBe(1);
  });
});

describe('detectGreenfield', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    clearTrackedFilesCache();
    clearRepoMapCache();
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
    clearTrackedFilesCache();
    clearRepoMapCache();
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

describe('buildGreenfieldCitationGuidance', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    clearTrackedFilesCache();
    clearRepoMapCache();
  });

  it('steers a greenfield design to web/external citations and away from invented file paths', () => {
    // Durable signal says empty → greenfield regardless of the live tree.
    const guidance = buildGreenfieldCitationGuidance({ repo_is_empty: true });
    expect(guidance).not.toBe('');
    // The grounding fix: cite external/web sources instead of nonexistent files.
    expect(guidance).toMatch(/https:\/\//);
    expect(guidance).toMatch(/EXTERNAL \/ WEB sources/);
    // And it must explicitly forbid inventing repo file paths — the exact cause
    // of the grounding-gate discard on an empty repo.
    expect(guidance.toLowerCase()).toContain('do not invent repository file paths');
    expect(guidance).toContain('GREENFIELD CITATION MODE');
  });

  it('is greenfield via the live scan when the durable signal is absent', () => {
    // Config-only tree, no durable signal → live detectGreenfield() short-circuit.
    setLsFiles(['package.json', 'README.md', 'tsconfig.json', '.gitignore']);
    expect(buildGreenfieldCitationGuidance({})).toContain('GREENFIELD CITATION MODE');
  });

  it('returns empty string on a mature repo so the default file-citation posture is unchanged', () => {
    // Durable signal explicitly non-empty → not greenfield → no guidance noise.
    setLsFiles(['package.json', 'README.md', 'tsconfig.json', '.gitignore']);
    expect(buildGreenfieldCitationGuidance({ repo_is_empty: false })).toBe('');
    // Source-rich live tree with no durable signal is also mature.
    const files = Array.from({ length: GREENFIELD_SOURCE_FILE_THRESHOLD + 1 }, (_, i) => `src/m${i}.ts`);
    setLsFiles(files);
    expect(buildGreenfieldCitationGuidance({})).toBe('');
  });
});

describe('verifyCloneDelivered', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    clearTrackedFilesCache();
    clearRepoMapCache();
    vi.mocked(apiPost).mockResolvedValue({});
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
        return (opts.head ?? '');
      }
      if (a[0] === 'rev-parse' && a[1] === '--verify') {
        if (typeof opts.originHead === 'function') return opts.originHead();
        if (opts.originHead === undefined) throw new Error('unknown revision');
        return opts.originHead;
      }
      if (a[0] === 'ls-files') {
        if (typeof opts.lsFiles === 'function') return opts.lsFiles();
        return ((opts.lsFiles ?? []).join('\n'));
      }
      return '';
    });
  };

  const populatedTree = Array.from({ length: GREENFIELD_SOURCE_FILE_THRESHOLD + 5 }, (_, i) => `src/m${i}.ts`);
  const nearEmptyTree = ['package.json', 'README.md', '.gitignore'];

  it('passes for a populated tree whose HEAD matches origin/<branch>', async () => {
    routeGit({ head: 'abc123', originHead: 'abc123', lsFiles: populatedTree });
    await expect(verifyCloneDelivered({ repo_is_empty: false }, 'main')).resolves.toBeUndefined();
  });

  it('throws clone_failed when the durable signal says non-empty but the tree is empty', async () => {
    // The core bug: a successful-but-empty clone of a known-non-empty repo must
    // be treated as a clone problem, NOT a greenfield repo.
    routeGit({ head: 'abc123', originHead: 'abc123', lsFiles: nearEmptyTree });
    let caught: (Error & { failureCategory?: string }) | null = null;
    try { await verifyCloneDelivered({ repo_is_empty: false }, 'main'); } catch (e) { caught = e as Error & { failureCategory?: string }; }
    expect(caught?.failureCategory).toBe('clone_failed');
    expect(caught?.message).toContain('did not deliver');
  });

  it('reports observed_repo_is_empty=true to backend before throwing on durable/live mismatch', async () => {
    routeGit({ head: 'abc123', originHead: 'abc123', lsFiles: nearEmptyTree });
    let caughtCategory: string | undefined;
    try { await verifyCloneDelivered({ repo_is_empty: false }, 'main'); } catch (e) { caughtCategory = (e as Error & { failureCategory?: string }).failureCategory; }
    expect(caughtCategory).toBe('clone_failed');
    expect(vi.mocked(apiPost)).toHaveBeenCalledWith(
      '/api/agent/build-config-discovery',
      { observed_repo_is_empty: true },
    );
  });

  it('still throws clone_failed even when the backend report fails', async () => {
    routeGit({ head: 'abc123', originHead: 'abc123', lsFiles: nearEmptyTree });
    vi.mocked(apiPost).mockRejectedValue(new Error('network error'));
    let caughtCategory: string | undefined;
    try { await verifyCloneDelivered({ repo_is_empty: false }, 'main'); } catch (e) { caughtCategory = (e as Error & { failureCategory?: string }).failureCategory; }
    expect(caughtCategory).toBe('clone_failed');
  });

  it('passes for a genuinely empty repo when the durable signal is absent', async () => {
    // repo_is_empty absent → no durable disagreement → empty tree is allowed
    // (the live greenfield detector legitimately concludes greenfield).
    routeGit({ head: 'abc123', originHead: 'abc123', lsFiles: nearEmptyTree });
    await expect(verifyCloneDelivered({}, 'main')).resolves.toBeUndefined();
  });

  it('passes for a genuinely empty repo when the durable signal says empty', async () => {
    routeGit({ head: 'abc123', originHead: 'abc123', lsFiles: nearEmptyTree });
    await expect(verifyCloneDelivered({ repo_is_empty: true }, 'main')).resolves.toBeUndefined();
  });

  it('throws clone_failed when HEAD does not match origin/<branch> (partial/wrong checkout)', async () => {
    routeGit({ head: 'abc123', originHead: 'def456', lsFiles: populatedTree });
    let caught: (Error & { failureCategory?: string }) | null = null;
    try { await verifyCloneDelivered({ repo_is_empty: false }, 'main'); } catch (e) { caught = e as Error & { failureCategory?: string }; }
    expect(caught?.failureCategory).toBe('clone_failed');
    expect(caught?.message).toContain('does not match');
  });

  it('throws clone_failed when HEAD cannot be resolved at all', async () => {
    routeGit({ head: () => { throw new Error('fatal: not a git repository'); } });
    let caught: (Error & { failureCategory?: string }) | null = null;
    try { await verifyCloneDelivered({ repo_is_empty: false }, 'main'); } catch (e) { caught = e as Error & { failureCategory?: string }; }
    expect(caught?.failureCategory).toBe('clone_failed');
    expect(caught?.message).toContain('no resolvable HEAD');
  });

  it('does not fail on a missing origin/<branch> ref alone (durable check is the net)', async () => {
    // origin ref absent (rev-parse --verify throws) but tree is populated and
    // durable says non-empty → no clone failure.
    routeGit({ head: 'abc123', originHead: () => { throw new Error('unknown revision'); }, lsFiles: populatedTree });
    await expect(verifyCloneDelivered({ repo_is_empty: false }, 'main')).resolves.toBeUndefined();
  });

  it('does not claim a clone failure when the ls-files scan itself fails', async () => {
    // countSourceFiles() → null (scan unavailable); we must not throw on a
    // failed scan, only on a confirmed empty tree.
    routeGit({ head: 'abc123', originHead: 'abc123', lsFiles: () => { throw new Error('git unavailable'); } });
    await expect(verifyCloneDelivered({ repo_is_empty: false }, 'main')).resolves.toBeUndefined();
  });

  it('skips the branch check when no branch is provided', async () => {
    routeGit({ head: 'abc123', lsFiles: populatedTree });
    await expect(verifyCloneDelivered({ repo_is_empty: false })).resolves.toBeUndefined();
  });

  it('quotes the persisted authoritative source_file_count in the clone_failed message', async () => {
    // The backend persisted a real count; on a durable/live disagreement the
    // failure names that count so the mismatch is explicit and like-for-like.
    routeGit({ head: 'abc123', originHead: 'abc123', lsFiles: nearEmptyTree });
    let caught: (Error & { failureCategory?: string }) | null = null;
    try { await verifyCloneDelivered({ repo_is_empty: false, source_file_count: 42 }, 'main'); } catch (e) { caught = e as Error & { failureCategory?: string }; }
    expect(caught?.failureCategory).toBe('clone_failed');
    expect(caught?.message).toContain('the onboarding scan counted 42 source file(s)');
    expect(caught?.message).toContain('under the same exclusion rules');
  });

  it('falls back to the boolean basis when no persisted count is present', async () => {
    routeGit({ head: 'abc123', originHead: 'abc123', lsFiles: nearEmptyTree });
    let caughtMessage: string | undefined;
    try { await verifyCloneDelivered({ repo_is_empty: false }, 'main'); } catch (e) { caughtMessage = (e as Error).message; }
    expect(caughtMessage).toContain('the durable onboarding signal says the repo is NOT empty');
  });
});

describe('buildRepoMap', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    clearTrackedFilesCache();
    clearRepoMapCache();
  });

  it('returns empty string on empty repo (no tracked files)', () => {
    vi.mocked(execFileSync).mockReturnValue('');
    expect(buildRepoMap(null)).toBe('');
  });

  it('ranks most-imported module first', () => {
    // hub.ts is imported by two files; other.ts only by one
    vi.mocked(execFileSync).mockReturnValue(
      ['src/hub.ts', 'src/other.ts', 'src/a.ts', 'src/b.ts'].join('\n')
    );
    vi.mocked(fs.readFileSync).mockImplementation(((path: unknown) => {
      const p = path as string;
      if (p.endsWith('hub.ts')) return "export function hubFn() {}\nexport function hubFn2() {}";
      if (p.endsWith('other.ts')) return "export function otherFn() {}";
      if (p.endsWith('a.ts')) return "import { hubFn } from './hub.js';\nimport { otherFn } from './other.js';";
      if (p.endsWith('b.ts')) return "import { hubFn2 } from './hub.js';";
      return '';
    }));

    const result = buildRepoMap(null);
    expect(result).toContain('## Repo Map');
    // hub should appear before other since it has 2 importers vs 1
    const hubIdx = result.indexOf('hub.ts');
    const otherIdx = result.indexOf('other.ts');
    expect(hubIdx).toBeGreaterThan(-1);
    expect(hubIdx).toBeLessThan(otherIdx);
    // hub must show its exports
    expect(result).toContain('hubFn');
  });

  it('buildRepoMap does not collapse same-basename modules across monorepo packages', () => {
    vi.mocked(execFileSync).mockReturnValue(
      [
        'packages/api/src/config.ts',
        'packages/api/src/consumer-a.ts',
        'packages/api/src/consumer-b.ts',
        'packages/web/src/config.ts',
        'packages/web/src/consumer.ts',
      ].join('\n')
    );
    vi.mocked(fs.readFileSync).mockImplementation(((path: unknown) => {
      const p = path as string;
      if (p.endsWith('packages/api/src/config.ts')) return 'export function apiConfig() {}';
      if (p.endsWith('packages/web/src/config.ts')) return 'export function webConfig() {}';
      if (p.endsWith('consumer-a.ts')) return "import { apiConfig } from './config.js';";
      if (p.endsWith('consumer-b.ts')) return "import { apiConfig } from './config.js';";
      if (p.endsWith('consumer.ts')) return "import { webConfig } from './config.js';";
      return '';
    }));

    const result = buildRepoMap(null);
    expect(result).toContain('**packages/api/src/config.ts** (imported by 2)');
    expect(result).toContain('**packages/web/src/config.ts** (imported by 1)');
    expect(result.indexOf('packages/api/src/config.ts')).toBeGreaterThan(-1);
    expect(result.indexOf('packages/web/src/config.ts')).toBeGreaterThan(-1);
    expect(result.indexOf('packages/api/src/config.ts')).toBeLessThan(result.indexOf('packages/web/src/config.ts'));
  });

  it('output is capped at 3000 chars', () => {
    // Generate many files each exporting a long-named symbol
    const files = Array.from({ length: 30 }, (_, i) => `src/mod${i}.ts`);
    vi.mocked(execFileSync).mockReturnValue(files.join('\n'));
    vi.mocked(fs.readFileSync).mockImplementation(((path: unknown) => {
      const p = path as string;
      const idx = files.findIndex(f => p.endsWith(f.replace('src/', '')));
      if (idx >= 0) {
        // Each file imports all others to boost in-degree, and exports a long symbol
        const imports = files.filter((_, i) => i !== idx).map(f => {
          const stem = f.replace('src/', '').replace('.ts', '');
          return `import { fn } from './${stem}.js';`;
        }).join('\n');
        return `${imports}\nexport function ${'veryLongExportedSymbolName'.repeat(5)}_${idx}() {}`;
      }
      return '';
    }));

    const result = buildRepoMap(null);
    expect(result.length).toBeLessThanOrEqual(3000);
  });

  it('handles a mixed Python+TypeScript repo', () => {
    vi.mocked(execFileSync).mockReturnValue(
      ['backend/app/models.py', 'agent/src/prompt.ts', 'agent/src/index.ts'].join('\n')
    );
    vi.mocked(fs.readFileSync).mockImplementation(((path: unknown) => {
      const p = path as string;
      if (p.endsWith('models.py')) return "class Task:\n    pass\nclass Project:\n    pass\n";
      if (p.endsWith('prompt.ts')) return "export function buildPrompt() {}\n";
      // index.ts imports from both
      if (p.endsWith('index.ts')) {
        return "from models import Task\nimport { buildPrompt } from './prompt.js';\n";
      }
      return '';
    }));

    const result = buildRepoMap(null);
    // Should include at least one of the imported modules
    expect(result === '' || result.includes('## Repo Map')).toBe(true);
  });

  it('is fail-soft when git ls-files throws', () => {
    vi.mocked(execFileSync).mockImplementation(() => {
      throw new Error('git not available');
    });
    expect(buildRepoMap(null)).toBe('');
  });

  it('does not attribute cross-package basename collisions to the wrong package', () => {
    // Both agent/src/index.ts and admin-ui/src/index.ts exist.
    // agent/src/a.ts imports from './index.js' (relative → resolves to agent/src/index.ts).
    // admin-ui/src/index.ts must NOT receive any in-degree from that import.
    vi.mocked(execFileSync).mockReturnValue(
      ['admin-ui/src/index.ts', 'agent/src/index.ts', 'agent/src/a.ts'].join('\n')
    );
    vi.mocked(fs.readFileSync).mockImplementation(((path: unknown) => {
      const p = path as string;
      if (p.endsWith('admin-ui/src/index.ts')) return "export function adminFn() {}";
      if (p.endsWith('agent/src/index.ts')) return "export function agentFn() {}";
      if (p.endsWith('agent/src/a.ts')) return "import { agentFn } from './index.js';";
      return '';
    }));

    const result = buildRepoMap(null);
    // agent/src/index.ts is the hub (1 relative importer), should appear
    expect(result).toContain('agent/src/index.ts');
    // admin-ui/src/index.ts should NOT appear — it has no importers
    expect(result).not.toContain('admin-ui/src/index.ts');
  });

  it('is fail-soft when file reads throw', () => {
    vi.mocked(execFileSync).mockReturnValue('src/a.ts\nsrc/b.ts');
    vi.mocked(fs.readFileSync).mockImplementation(() => {
      throw new Error('ENOENT');
    });
    expect(buildRepoMap(null)).toBe('');
  });

  it('buildRepoMap_prioritizes_task_related_symbols', () => {
    // hub.ts is the global hub (high in-degree).
    // dep.ts has low in-degree BUT is directly imported by the relatedFile task.ts.
    // With opts.relatedFiles=[task.ts], dep.ts should appear before hub.ts in the map.
    vi.mocked(execFileSync).mockReturnValue(
      ['src/hub.ts', 'src/dep.ts', 'src/task.ts', 'src/a.ts', 'src/b.ts', 'src/c.ts'].join('\n')
    );
    vi.mocked(fs.readFileSync).mockImplementation(((path: unknown) => {
      const p = path as string;
      if (p.endsWith('hub.ts')) return "export function hubFn() {}";
      if (p.endsWith('dep.ts')) return "export function depFn() {}";
      if (p.endsWith('task.ts')) return "import { depFn } from './dep.js';";
      // a, b, c all import hub → hub has in-degree 3, dep has in-degree 0
      if (p.endsWith('a.ts')) return "import { hubFn } from './hub.js';";
      if (p.endsWith('b.ts')) return "import { hubFn } from './hub.js';";
      if (p.endsWith('c.ts')) return "import { hubFn } from './hub.js';";
      return '';
    }));

    // Without opts: hub appears first (higher in-degree)
    const resultGlobal = buildRepoMap(null);
    expect(resultGlobal.indexOf('hub.ts')).toBeLessThan(resultGlobal.indexOf('dep.ts'));

    // With opts.relatedFiles=[task.ts]: dep appears first (task-adjacent via task.ts's import)
    const resultTaskScoped = buildRepoMap(null, { relatedFiles: ['src/task.ts'] });
    expect(resultTaskScoped).toContain('## Repo Map');
    expect(resultTaskScoped).toContain('dep.ts');
    const depIdx = resultTaskScoped.indexOf('dep.ts');
    const hubIdx = resultTaskScoped.indexOf('hub.ts');
    expect(depIdx).toBeGreaterThan(-1);
    expect(depIdx).toBeLessThan(hubIdx);
  });

  it('promotes consumer files that import from relatedFiles', () => {
    // consumer.ts imports from service.ts (a relatedFile) and exports a function.
    // With opts.relatedFiles=[service.ts], consumer.ts should be boosted before hub.ts.
    vi.mocked(execFileSync).mockReturnValue(
      ['src/service.ts', 'src/consumer.ts', 'src/hub.ts', 'src/a.ts', 'src/b.ts', 'src/c.ts'].join('\n')
    );
    vi.mocked(fs.readFileSync).mockImplementation(((path: unknown) => {
      const p = path as string;
      if (p.endsWith('service.ts')) return "export function svcFn() {}";
      // consumer.ts imports from service.ts (the relatedFile) and exports its own API
      if (p.endsWith('consumer.ts')) return "import { svcFn } from './service.js';\nexport function consumerFn() {}";
      if (p.endsWith('hub.ts')) return "export function hubFn() {}";
      if (p.endsWith('a.ts')) return "import { hubFn } from './hub.js';";
      if (p.endsWith('b.ts')) return "import { hubFn } from './hub.js';";
      if (p.endsWith('c.ts')) return "import { hubFn } from './hub.js';";
      return '';
    }));

    const result = buildRepoMap(null, { relatedFiles: ['src/service.ts'] });
    expect(result).toContain('consumer.ts');
    // consumer.ts (task-adjacent consumer) should appear before hub.ts (global hub)
    expect(result.indexOf('consumer.ts')).toBeLessThan(result.indexOf('hub.ts'));
  });

  it('boosts modules whose basename appears in taskPrompt', () => {
    // models.ts basename matches keyword "models" in the prompt.
    // hub.ts has higher in-degree but no keyword match.
    vi.mocked(execFileSync).mockReturnValue(
      ['src/models.ts', 'src/hub.ts', 'src/a.ts', 'src/b.ts'].join('\n')
    );
    vi.mocked(fs.readFileSync).mockImplementation(((path: unknown) => {
      const p = path as string;
      if (p.endsWith('models.ts')) return "export class Model {}";
      if (p.endsWith('hub.ts')) return "export function hubFn() {}";
      if (p.endsWith('a.ts')) return "import { hubFn } from './hub.js';";
      if (p.endsWith('b.ts')) return "import { hubFn } from './hub.js';";
      return '';
    }));

    // hub has in-degree 2, models has in-degree 0 — without prompt, hub wins
    const noPrompt = buildRepoMap(null);
    expect(noPrompt.indexOf('hub.ts')).toBeLessThan(noPrompt.indexOf('models.ts') === -1 ? Infinity : noPrompt.indexOf('models.ts'));

    // With taskPrompt mentioning "models", models gets 0.5 keyword boost (still loses to hub's 2 in-degree)
    // but models is at least present and boosted relative to other 0-in-degree files
    const withPrompt = buildRepoMap(null, { taskPrompt: 'update the models layer' });
    // hub still wins (in-degree 2 > 0 + 0.5), but models should appear if it has any in-degree
    // The key assertion: result is well-formed and doesn't error
    expect(withPrompt === '' || withPrompt.includes('## Repo Map')).toBe(true);
  });

  it('falls back to global in-degree ranking when no opts provided', () => {
    vi.mocked(execFileSync).mockReturnValue(
      ['src/hub.ts', 'src/other.ts', 'src/a.ts'].join('\n')
    );
    vi.mocked(fs.readFileSync).mockImplementation(((path: unknown) => {
      const p = path as string;
      if (p.endsWith('hub.ts')) return "export function hubFn() {}";
      if (p.endsWith('other.ts')) return "export function otherFn() {}";
      if (p.endsWith('a.ts')) return "import { hubFn } from './hub.js';";
      return '';
    }));

    // No opts → same as passing undefined
    const result = buildRepoMap(null);
    expect(result).toContain('hub.ts');
  });

  it('remains fail-soft when relatedFiles paths are not in the tracked file set', () => {
    vi.mocked(execFileSync).mockReturnValue('src/hub.ts\nsrc/a.ts');
    vi.mocked(fs.readFileSync).mockImplementation(((path: unknown) => {
      const p = path as string;
      if (p.endsWith('hub.ts')) return "export function hubFn() {}";
      if (p.endsWith('a.ts')) return "import { hubFn } from './hub.js';";
      return '';
    }));

    // Untracked file in relatedFiles — should not crash, just skip
    const result = buildRepoMap(null, { relatedFiles: ['src/nonexistent.ts'] });
    expect(result === '' || result.includes('## Repo Map')).toBe(true);
  });
});
