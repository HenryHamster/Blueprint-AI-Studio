import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';

// ── Module mocks ─────────────────────────────────────────────────────────────
// Mock fs.existsSync so we can flip warm-vs-cold detection deterministically.
const fsMocks = vi.hoisted(() => ({
  existsSync: vi.fn().mockReturnValue(false),
}));

vi.mock('fs', () => ({
  default: {
    existsSync: fsMocks.existsSync,
  },
}));

// child_process.execFileSync is the cold path's `rm -rf` AND every warm-path
// `git ...` invocation. Mock it so each test can drive success/failure per
// invocation.
const execFileSyncMock = vi.hoisted(() => vi.fn().mockReturnValue(''));

vi.mock('child_process', () => ({
  execFileSync: execFileSyncMock,
}));

// Mock the index-level cloneWithRetry so we can assert it's invoked on the
// cold path — and NOT invoked on the warm path. We also need `log` since
// workspacePrep imports it for warning output.
const cloneWithRetryMock = vi.hoisted(() => vi.fn().mockResolvedValue(undefined));
const logMock = vi.hoisted(() => vi.fn());

vi.mock('./index.js', () => ({
  cloneWithRetry: cloneWithRetryMock,
  log: logMock,
}));

// ── Imports (after mocks) ────────────────────────────────────────────────────
import { prepareWorkspaceForRole } from './workspacePrep.js';

// ── Tests ────────────────────────────────────────────────────────────────────
describe('prepareWorkspaceForRole', () => {
  beforeEach(() => {
    execFileSyncMock.mockReset();
    execFileSyncMock.mockReturnValue('');
    cloneWithRetryMock.mockReset();
    cloneWithRetryMock.mockResolvedValue(undefined);
    fsMocks.existsSync.mockReset();
    fsMocks.existsSync.mockReturnValue(false);
    logMock.mockReset();
    delete process.env.BLUEPRINT_WARM_HANDOFF;
  });

  afterEach(() => {
    delete process.env.BLUEPRINT_WARM_HANDOFF;
  });

  // ── Cold path ──────────────────────────────────────────────────────────────
  it('cold path: BLUEPRINT_WARM_HANDOFF unset → rm -rf + cloneWithRetry', async () => {
    await prepareWorkspaceForRole({
      workspace: '/ws',
      repoUrl: 'https://x-access-token:tok@github.com/o/r.git',
      githubToken: 'tok',
      branch: 'feature/x',
      depth: 50,
    });

    // execFileSync called exactly once with rm -rf <workspace>.
    const rmCall = execFileSyncMock.mock.calls.find(
      c => c[0] === 'rm' && Array.isArray(c[1]) && (c[1] as string[]).includes('-rf'),
    );
    expect(rmCall).toBeDefined();
    expect(rmCall?.[1]).toEqual(['-rf', '/ws']);

    // cloneWithRetry called with the canonical args.
    expect(cloneWithRetryMock).toHaveBeenCalledTimes(1);
    expect(cloneWithRetryMock).toHaveBeenCalledWith([
      'clone', '--depth', '50', '--branch', 'feature/x',
      'https://x-access-token:tok@github.com/o/r.git', '/ws',
    ]);
    // No git invocations on the cold path.
    const gitCalls = execFileSyncMock.mock.calls.filter(c => c[0] === 'git');
    expect(gitCalls.length).toBe(0);
  });

  it('cold path: depth=0 → omit --depth flag (full clone semantics)', async () => {
    await prepareWorkspaceForRole({
      workspace: '/ws',
      repoUrl: 'https://x-access-token:tok@github.com/o/r.git',
      githubToken: 'tok',
      branch: 'main',
      depth: 0,
    });
    expect(cloneWithRetryMock).toHaveBeenCalledWith([
      'clone', '--branch', 'main',
      'https://x-access-token:tok@github.com/o/r.git', '/ws',
    ]);
  });

  it('cold path: BLUEPRINT_WARM_HANDOFF=1 BUT no .git directory → still cold', async () => {
    process.env.BLUEPRINT_WARM_HANDOFF = '1';
    fsMocks.existsSync.mockReturnValue(false);

    await prepareWorkspaceForRole({
      workspace: '/ws',
      repoUrl: 'https://x-access-token:tok@github.com/o/r.git',
      githubToken: 'tok',
      branch: 'main',
    });

    // .git missing on the warm-handoff signal → fallthrough to cold clone,
    // no git fetch/checkout/reset/clean ever runs.
    const gitCalls = execFileSyncMock.mock.calls.filter(c => c[0] === 'git');
    expect(gitCalls.length).toBe(0);
    expect(cloneWithRetryMock).toHaveBeenCalled();
  });

  // ── Warm path ──────────────────────────────────────────────────────────────
  it('warm path: env=1 + .git exists → fetch+checkout+reset+clean, no rm', async () => {
    process.env.BLUEPRINT_WARM_HANDOFF = '1';
    fsMocks.existsSync.mockReturnValue(true);

    await prepareWorkspaceForRole({
      workspace: '/ws',
      repoUrl: 'https://x-access-token:NEW@github.com/o/r.git',
      githubToken: 'NEW',
      branch: 'feature/x',
      baseBranch: 'main',
    });

    // No rm -rf call.
    const rmCalls = execFileSyncMock.mock.calls.filter(
      c => c[0] === 'rm' && Array.isArray(c[1]) && (c[1] as string[]).includes('-rf'),
    );
    expect(rmCalls.length).toBe(0);
    // No cloneWithRetry call.
    expect(cloneWithRetryMock).not.toHaveBeenCalled();

    // Verify the warm-path git commands ran in the expected order.
    const gitArgsList = execFileSyncMock.mock.calls
      .filter(c => c[0] === 'git')
      .map(c => (c[1] as string[]).join(' '));
    // Remote URL rotated:
    expect(gitArgsList.some(a =>
      a.includes('remote') && a.includes('set-url') && a.includes('origin')
        && a.includes('https://x-access-token:NEW@github.com/o/r.git'),
    )).toBe(true);
    // Branch fetch:
    expect(gitArgsList.some(a => a.startsWith('-C /ws fetch origin feature/x'))).toBe(true);
    // Base-branch fetch (different from branch):
    expect(gitArgsList.some(a => a.startsWith('-C /ws fetch origin main'))).toBe(true);
    // Checkout (either direct or -B from origin):
    expect(gitArgsList.some(a => /checkout (-B )?feature\/x/.test(a))).toBe(true);
    // Reset hard:
    expect(gitArgsList.some(a => a.includes('reset --hard origin/feature/x'))).toBe(true);
    // Clean preserves the cache directories:
    const cleanLine = gitArgsList.find(a => a.includes('clean -fdx'));
    expect(cleanLine).toBeDefined();
    expect(cleanLine).toContain('-e node_modules');
    expect(cleanLine).toContain('-e .testmondata');
    expect(cleanLine).toContain('-e .vitest_cache');
    expect(cleanLine).toContain('-e .pnpm-store');
  });

  it('warm path failure: git fetch throws → falls through to cold clone', async () => {
    process.env.BLUEPRINT_WARM_HANDOFF = '1';
    fsMocks.existsSync.mockReturnValue(true);

    // First git invocation (remote set-url) succeeds; the second (fetch
    // origin <branch>) throws. The implementation should catch and fall
    // through to the cold-path rm + clone.
    let gitCallIdx = 0;
    execFileSyncMock.mockImplementation((cmd: string, args: string[]) => {
      if (cmd === 'git') {
        gitCallIdx += 1;
        // 1st git call = set-url; 2nd = fetch — throw on the fetch.
        const arg0 = args.includes('-C') ? args[2] : args[0];
        if (arg0 === 'fetch') {
          throw new Error('network unreachable');
        }
        return '';
      }
      // rm -rf — succeed silently.
      return '';
    });

    await prepareWorkspaceForRole({
      workspace: '/ws',
      repoUrl: 'https://x-access-token:NEW@github.com/o/r.git',
      githubToken: 'NEW',
      branch: 'feature/x',
    });

    // Cold path was taken after the warm-path failure.
    const rmCall = execFileSyncMock.mock.calls.find(
      c => c[0] === 'rm' && Array.isArray(c[1]) && (c[1] as string[]).includes('-rf'),
    );
    expect(rmCall).toBeDefined();
    expect(cloneWithRetryMock).toHaveBeenCalledTimes(1);
    // We logged the warning about the warm path failure.
    const logged = logMock.mock.calls.map(c => String(c[0])).join('\n');
    expect(logged).toMatch(/warm path failed/i);
    // gitCallIdx was incremented at least twice (set-url + fetch).
    expect(gitCallIdx).toBeGreaterThanOrEqual(2);
  });

  it('warm path: when branch === baseBranch, baseBranch fetch is skipped', async () => {
    process.env.BLUEPRINT_WARM_HANDOFF = '1';
    fsMocks.existsSync.mockReturnValue(true);

    await prepareWorkspaceForRole({
      workspace: '/ws',
      repoUrl: 'https://x-access-token:tok@github.com/o/r.git',
      githubToken: 'tok',
      branch: 'main',
      baseBranch: 'main',
    });

    const fetchCalls = execFileSyncMock.mock.calls
      .filter(c => c[0] === 'git' && (c[1] as string[]).includes('fetch'))
      .map(c => (c[1] as string[]).join(' '));
    // Exactly one fetch (the branch fetch); no separate baseBranch fetch.
    expect(fetchCalls.length).toBe(1);
    expect(fetchCalls[0]).toContain('fetch origin main');
  });

  it('warm path: checkout fallback via -B when direct checkout fails', async () => {
    process.env.BLUEPRINT_WARM_HANDOFF = '1';
    fsMocks.existsSync.mockReturnValue(true);

    let firstCheckoutSeen = false;
    execFileSyncMock.mockImplementation((cmd: string, args: string[]) => {
      if (cmd === 'git') {
        const sub = args.includes('-C') ? args[2] : args[0];
        if (sub === 'checkout') {
          // First plain `checkout <branch>` fails; second (with -B) succeeds.
          const hasDashB = args.includes('-B');
          if (!hasDashB && !firstCheckoutSeen) {
            firstCheckoutSeen = true;
            throw new Error('pathspec did not match any file');
          }
        }
      }
      return '';
    });

    await prepareWorkspaceForRole({
      workspace: '/ws',
      repoUrl: 'https://x-access-token:tok@github.com/o/r.git',
      githubToken: 'tok',
      branch: 'feature/y',
    });

    // Cold path NOT taken — warm fallback within the warm path succeeded.
    expect(cloneWithRetryMock).not.toHaveBeenCalled();
    // -B fallback ran:
    const checkoutB = execFileSyncMock.mock.calls.find(c =>
      c[0] === 'git' && Array.isArray(c[1]) && (c[1] as string[]).includes('checkout')
      && (c[1] as string[]).includes('-B'),
    );
    expect(checkoutB).toBeDefined();
    expect((checkoutB?.[1] as string[]).join(' ')).toContain('-B feature/y origin/feature/y');
  });
});
