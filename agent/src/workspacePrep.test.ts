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
const registerSizeCapMergeDriverMock = vi.hoisted(() => vi.fn());

vi.mock('./index.js', () => ({
  cloneWithRetry: cloneWithRetryMock,
  log: logMock,
  GIT_REFERENCE_CACHE_ROOT: '/opt/agent-cache/git',
  registerSizeCapMergeDriver: registerSizeCapMergeDriverMock,
}));

// ── Imports (after mocks) ────────────────────────────────────────────────────
import {
  prepareWorkspaceForRole, resolveReferenceRepo,
  getLastWorkspacePrepOutcome, resetWorkspacePrepOutcome,
} from './workspacePrep.js';

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
    registerSizeCapMergeDriverMock.mockReset();
    delete process.env.BLUEPRINT_WARM_HANDOFF;
    resetWorkspacePrepOutcome();
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
    // The sizecap merge driver is registered after the clone so the
    // .gitattributes mapping is actually active.
    expect(registerSizeCapMergeDriverMock).toHaveBeenCalledWith('/ws');
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
    // Warm reuse also (re-)registers the sizecap merge driver.
    expect(registerSizeCapMergeDriverMock).toHaveBeenCalledWith('/ws');
  });

  // ── Review-fix lane pilot: warm-resume guard ───────────────────────────────
  describe('warm path: review-fix lane pilot resume guard', () => {
    afterEach(() => {
      delete process.env.BLUEPRINT_LANE_MODE;
      delete process.env.BLUEPRINT_LANE_CHECKPOINT_SHA;
    });

    it('fix side (allowLaneResume default) + lane env → hard-resets to checkpoint SHA, not origin', async () => {
      process.env.BLUEPRINT_WARM_HANDOFF = '1';
      process.env.BLUEPRINT_LANE_MODE = '1';
      process.env.BLUEPRINT_LANE_CHECKPOINT_SHA = 'deadbeefcafe1234';
      fsMocks.existsSync.mockReturnValue(true);

      await prepareWorkspaceForRole({
        workspace: '/ws',
        repoUrl: 'https://x-access-token:NEW@github.com/o/r.git',
        githubToken: 'NEW',
        branch: 'feature/x',
        baseBranch: 'main',
        // allowLaneResume omitted → defaults to true (env-driven, fix side).
      });

      const gitArgsList = execFileSyncMock.mock.calls
        .filter(c => c[0] === 'git')
        .map(c => (c[1] as string[]).join(' '));
      // Lane resume: hard-reset to the checkpoint SHA …
      expect(gitArgsList.some(a => a.includes('reset --hard deadbeefcafe1234'))).toBe(true);
      // … and NOT to origin/<branch> (the classic warm reset is replaced).
      expect(gitArgsList.some(a => a.includes('reset --hard origin/feature/x'))).toBe(false);
    });

    it('review/audit side (allowLaneResume=false) + lane env LEAKED → still resets to origin, never the checkpoint', async () => {
      process.env.BLUEPRINT_WARM_HANDOFF = '1';
      // A lane env should never reach a cold reviewer claim, but assert the
      // hard guard holds even if it leaked: the reviewer stays independent.
      process.env.BLUEPRINT_LANE_MODE = '1';
      process.env.BLUEPRINT_LANE_CHECKPOINT_SHA = 'deadbeefcafe1234';
      fsMocks.existsSync.mockReturnValue(true);

      await prepareWorkspaceForRole({
        workspace: '/ws',
        repoUrl: 'https://x-access-token:NEW@github.com/o/r.git',
        githubToken: 'NEW',
        branch: 'feature/x',
        baseBranch: 'main',
        allowLaneResume: false,
      });

      const gitArgsList = execFileSyncMock.mock.calls
        .filter(c => c[0] === 'git')
        .map(c => (c[1] as string[]).join(' '));
      // Guard wins: classic reset to origin/<branch>, never the checkpoint SHA.
      expect(gitArgsList.some(a => a.includes('reset --hard origin/feature/x'))).toBe(true);
      expect(gitArgsList.some(a => a.includes('reset --hard deadbeefcafe1234'))).toBe(false);
    });
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

  // ── Warm path: full-clone request unshallows a shallow workspace ────────────
  //
  // R4-H4: release_audit / qualifier-smoketest pass depth 0 (a FULL-clone
  // request) so the merge-base of `main...<candidate_sha>` is reachable. The
  // cold path honors depth 0 by omitting --depth; the warm path's plain
  // `git fetch origin <branch>` did NOT — it kept role 1's shallow boundary.
  // The fix: when a full clone is requested AND the warm workspace is shallow
  // (a `.git/shallow` file exists) the warm path runs
  // `git fetch --unshallow origin <branch>`. Shallow (depth > 0) requests and
  // already-complete repos are untouched.
  describe('warm path: full-clone request unshallows a shallow workspace', () => {
    /**
     * Drives fs.existsSync so `.git` reports present (warm detection) and
     * `.git/shallow` reports present iff `shallow` is true. Path separators
     * differ by OS, so match on the path tail rather than an exact string.
     */
    function setExistsSync(opts: { gitDir: boolean; shallow: boolean }): void {
      fsMocks.existsSync.mockImplementation((p: unknown) => {
        const s = String(p).replace(/\\/g, '/');
        if (s.endsWith('/.git/shallow')) return opts.shallow;
        if (s.endsWith('/.git')) return opts.gitDir;
        return false;
      });
    }

    it('depth=0 + shallow repo → runs git fetch --unshallow, no cold clone', async () => {
      process.env.BLUEPRINT_WARM_HANDOFF = '1';
      setExistsSync({ gitDir: true, shallow: true });

      await prepareWorkspaceForRole({
        workspace: '/ws',
        repoUrl: 'https://x-access-token:tok@github.com/o/r.git',
        githubToken: 'tok',
        branch: 'main',
        baseBranch: 'main',
        depth: 0,
      });

      // The warm path ran an --unshallow fetch scoped to the branch.
      const unshallow = execFileSyncMock.mock.calls.find(c =>
        c[0] === 'git' && Array.isArray(c[1])
        && (c[1] as string[]).includes('fetch')
        && (c[1] as string[]).includes('--unshallow'),
      );
      expect(unshallow).toBeDefined();
      expect((unshallow?.[1] as string[]).join(' ')).toBe(
        '-C /ws fetch --unshallow origin main',
      );
      // Warm path completed — no cold rm/clone fallback.
      expect(cloneWithRetryMock).not.toHaveBeenCalled();
      const rmCalls = execFileSyncMock.mock.calls.filter(
        c => c[0] === 'rm' && Array.isArray(c[1]) && (c[1] as string[]).includes('-rf'),
      );
      expect(rmCalls.length).toBe(0);
    });

    it('depth=0 + NON-shallow repo → does NOT run --unshallow (already complete)', async () => {
      process.env.BLUEPRINT_WARM_HANDOFF = '1';
      // .git exists (warm) but no .git/shallow → repo is already complete.
      setExistsSync({ gitDir: true, shallow: false });

      await prepareWorkspaceForRole({
        workspace: '/ws',
        repoUrl: 'https://x-access-token:tok@github.com/o/r.git',
        githubToken: 'tok',
        branch: 'main',
        depth: 0,
      });

      const unshallow = execFileSyncMock.mock.calls.find(c =>
        c[0] === 'git' && Array.isArray(c[1]) && (c[1] as string[]).includes('--unshallow'),
      );
      expect(unshallow).toBeUndefined();
      // Warm path still succeeded.
      expect(cloneWithRetryMock).not.toHaveBeenCalled();
    });

    it('shallow depth (depth > 0) → does NOT unshallow even if .git/shallow exists', async () => {
      process.env.BLUEPRINT_WARM_HANDOFF = '1';
      // Repo IS shallow, but the caller asked for a shallow (depth 50) clone —
      // a shallow request must be left exactly as-is.
      setExistsSync({ gitDir: true, shallow: true });

      await prepareWorkspaceForRole({
        workspace: '/ws',
        repoUrl: 'https://x-access-token:tok@github.com/o/r.git',
        githubToken: 'tok',
        branch: 'feature/x',
        depth: 50,
      });

      const unshallow = execFileSyncMock.mock.calls.find(c =>
        c[0] === 'git' && Array.isArray(c[1]) && (c[1] as string[]).includes('--unshallow'),
      );
      expect(unshallow).toBeUndefined();
    });

    it('depth omitted (defaults to DEFAULT_CLONE_DEPTH, a shallow request) → does NOT unshallow', async () => {
      process.env.BLUEPRINT_WARM_HANDOFF = '1';
      setExistsSync({ gitDir: true, shallow: true });

      await prepareWorkspaceForRole({
        workspace: '/ws',
        repoUrl: 'https://x-access-token:tok@github.com/o/r.git',
        githubToken: 'tok',
        branch: 'feature/x',
        // depth omitted → DEFAULT_CLONE_DEPTH (> 0): a shallow request.
      });

      const unshallow = execFileSyncMock.mock.calls.find(c =>
        c[0] === 'git' && Array.isArray(c[1]) && (c[1] as string[]).includes('--unshallow'),
      );
      expect(unshallow).toBeUndefined();
    });

    it('depth=0 + --unshallow errors "complete repository" → tolerated, warm path succeeds', async () => {
      process.env.BLUEPRINT_WARM_HANDOFF = '1';
      setExistsSync({ gitDir: true, shallow: true });

      // The .git/shallow check passed but the repo became complete before the
      // fetch (e.g. a concurrent fetch). git errors with the benign
      // "--unshallow on a complete repository does not make sense" — the warm
      // path must swallow exactly that error and continue (the repo already
      // has full history, which is what the full-clone request wanted).
      execFileSyncMock.mockImplementation((cmd: string, args: string[]) => {
        if (cmd === 'git' && args.includes('--unshallow')) {
          throw new Error('fatal: --unshallow on a complete repository does not make sense');
        }
        return '';
      });

      await prepareWorkspaceForRole({
        workspace: '/ws',
        repoUrl: 'https://x-access-token:tok@github.com/o/r.git',
        githubToken: 'tok',
        branch: 'main',
        depth: 0,
      });

      // Benign error swallowed → NO cold fallback.
      expect(cloneWithRetryMock).not.toHaveBeenCalled();
      const rmCalls = execFileSyncMock.mock.calls.filter(
        c => c[0] === 'rm' && Array.isArray(c[1]) && (c[1] as string[]).includes('-rf'),
      );
      expect(rmCalls.length).toBe(0);
      // The warm path still ran reset --hard (i.e. it continued past the fetch).
      const resetCall = execFileSyncMock.mock.calls.find(c =>
        c[0] === 'git' && Array.isArray(c[1]) && (c[1] as string[]).includes('reset'),
      );
      expect(resetCall).toBeDefined();
    });

    it('depth=0 + --unshallow fails with a REAL error → falls through to cold clone', async () => {
      process.env.BLUEPRINT_WARM_HANDOFF = '1';
      setExistsSync({ gitDir: true, shallow: true });

      // A genuine fetch failure (network) must NOT be swallowed — handing the
      // handler a still-shallow repo would non-deterministically break the
      // qualifier. It must propagate so we fall back to a cold full clone.
      execFileSyncMock.mockImplementation((cmd: string, args: string[]) => {
        if (cmd === 'git' && args.includes('--unshallow')) {
          throw new Error('fatal: unable to access remote: network unreachable');
        }
        return '';
      });

      await prepareWorkspaceForRole({
        workspace: '/ws',
        repoUrl: 'https://x-access-token:tok@github.com/o/r.git',
        githubToken: 'tok',
        branch: 'main',
        depth: 0,
      });

      // Cold path taken: rm -rf + cloneWithRetry, and the cold clone honors
      // the depth-0 full-clone request (no --depth flag).
      const rmCall = execFileSyncMock.mock.calls.find(
        c => c[0] === 'rm' && Array.isArray(c[1]) && (c[1] as string[]).includes('-rf'),
      );
      expect(rmCall).toBeDefined();
      expect(cloneWithRetryMock).toHaveBeenCalledTimes(1);
      expect(cloneWithRetryMock).toHaveBeenCalledWith([
        'clone', '--branch', 'main',
        'https://x-access-token:tok@github.com/o/r.git', '/ws',
      ]);
      // The warm-path failure was logged.
      const logged = logMock.mock.calls.map(c => String(c[0])).join('\n');
      expect(logged).toMatch(/warm path failed/i);
    });

    it('cold path (warm handoff unset) + depth=0 + shallow file present → unaffected, plain cold full clone', async () => {
      // No BLUEPRINT_WARM_HANDOFF → cold path. Even with a stray .git/shallow,
      // the cold path just rm+clone (full, depth 0) — the unshallow logic is
      // warm-path-only and must never run here.
      setExistsSync({ gitDir: true, shallow: true });

      await prepareWorkspaceForRole({
        workspace: '/ws',
        repoUrl: 'https://x-access-token:tok@github.com/o/r.git',
        githubToken: 'tok',
        branch: 'main',
        depth: 0,
      });

      const gitCalls = execFileSyncMock.mock.calls.filter(c => c[0] === 'git');
      expect(gitCalls.length).toBe(0); // no git fetch/unshallow on the cold path
      expect(cloneWithRetryMock).toHaveBeenCalledWith([
        'clone', '--branch', 'main',
        'https://x-access-token:tok@github.com/o/r.git', '/ws',
      ]);
    });
  });

  // ── Cold path: pre-baked git object cache reuse ─────────────────────────────
  //
  // The image seeds a mirror at GIT_REFERENCE_CACHE_ROOT/<owner>/<repo>.git.
  // The cold path appends `--reference-if-able <mirror>` when that path exists
  // so every role's cold clone (review-fix / specialized / research /
  // design-doc) gets the object-reuse speedup, not just setupWorkspace's
  // standard path.
  describe('cold path: reference-cache reuse', () => {
    it('mirror exists → clone args include --reference-if-able', async () => {
      // BLUEPRINT_WARM_HANDOFF unset (cold path). existsSync is only consulted
      // by resolveReferenceRepo here; return true for the mirror path.
      fsMocks.existsSync.mockImplementation((p: unknown) => {
        const s = String(p).replace(/\\/g, '/');
        return s.endsWith('/opt/agent-cache/git/o/r.git');
      });

      await prepareWorkspaceForRole({
        workspace: '/ws',
        repoUrl: 'https://x-access-token:tok@github.com/o/r.git',
        githubToken: 'tok',
        branch: 'feature/x',
        depth: 50,
      });

      expect(cloneWithRetryMock).toHaveBeenCalledTimes(1);
      expect(cloneWithRetryMock).toHaveBeenCalledWith([
        'clone', '--depth', '50', '--branch', 'feature/x',
        '--reference-if-able', '/opt/agent-cache/git/o/r.git',
        'https://x-access-token:tok@github.com/o/r.git', '/ws',
      ]);
    });

    it('mirror exists + depth=0 → --reference-if-able with full-clone args', async () => {
      fsMocks.existsSync.mockImplementation((p: unknown) =>
        String(p).replace(/\\/g, '/').endsWith('/opt/agent-cache/git/o/r.git'));

      await prepareWorkspaceForRole({
        workspace: '/ws',
        repoUrl: 'https://x-access-token:tok@github.com/o/r.git',
        githubToken: 'tok',
        branch: 'main',
        depth: 0,
      });

      expect(cloneWithRetryMock).toHaveBeenCalledWith([
        'clone', '--branch', 'main',
        '--reference-if-able', '/opt/agent-cache/git/o/r.git',
        'https://x-access-token:tok@github.com/o/r.git', '/ws',
      ]);
    });

    it('mirror absent → clone args exclude --reference-if-able', async () => {
      // existsSync returns false (the default) → no mirror → plain cold clone.
      fsMocks.existsSync.mockReturnValue(false);

      await prepareWorkspaceForRole({
        workspace: '/ws',
        repoUrl: 'https://x-access-token:tok@github.com/o/r.git',
        githubToken: 'tok',
        branch: 'feature/x',
        depth: 50,
      });

      const cloneArgs = cloneWithRetryMock.mock.calls[0][0] as string[];
      expect(cloneArgs).not.toContain('--reference-if-able');
      expect(cloneArgs).toEqual([
        'clone', '--depth', '50', '--branch', 'feature/x',
        'https://x-access-token:tok@github.com/o/r.git', '/ws',
      ]);
    });
  });

  // ── Lane workspace-mode ──────────────────────────────────────────────────────
  //
  // When BLUEPRINT_LANE_MODE=1 and BLUEPRINT_LANE_CHECKPOINT_SHA=<sha> are set,
  // the warm path must hard-reset to the checkpoint SHA instead of origin/<branch>.
  // If the reset fails (SHA unreachable), the outer catch must fall back to cold.
  describe('lane workspace-mode', () => {
    beforeEach(() => {
      delete process.env.BLUEPRINT_LANE_MODE;
      delete process.env.BLUEPRINT_LANE_CHECKPOINT_SHA;
    });

    afterEach(() => {
      delete process.env.BLUEPRINT_LANE_MODE;
      delete process.env.BLUEPRINT_LANE_CHECKPOINT_SHA;
    });

    it('lane mode: resets to checkpoint SHA, not origin/<branch>', async () => {
      process.env.BLUEPRINT_WARM_HANDOFF = '1';
      process.env.BLUEPRINT_LANE_MODE = '1';
      process.env.BLUEPRINT_LANE_CHECKPOINT_SHA = 'abc1234def5678901234567890123456789012ab';
      fsMocks.existsSync.mockReturnValue(true);

      await prepareWorkspaceForRole({
        workspace: '/ws',
        repoUrl: 'https://x-access-token:tok@github.com/o/r.git',
        githubToken: 'tok',
        branch: 'feat/lane',
      });

      const gitArgsList = execFileSyncMock.mock.calls
        .filter(c => c[0] === 'git')
        .map(c => (c[1] as string[]).join(' '));

      // Must reset to the checkpoint SHA, not origin/feat/lane.
      expect(gitArgsList.some(a =>
        a.includes('reset --hard abc1234def5678901234567890123456789012ab'),
      )).toBe(true);
      expect(gitArgsList.some(a =>
        a.includes('reset --hard origin/feat/lane'),
      )).toBe(false);

      // Warm path completed — no cold fallback.
      expect(cloneWithRetryMock).not.toHaveBeenCalled();

      // Logged the lane-mode reset.
      const logged = logMock.mock.calls.map(c => String(c[0])).join('\n');
      expect(logged).toMatch(/lane-mode reset to checkpoint/i);
    });

    it('lane mode: SHA unreachable → falls back to cold clone', async () => {
      process.env.BLUEPRINT_WARM_HANDOFF = '1';
      process.env.BLUEPRINT_LANE_MODE = '1';
      process.env.BLUEPRINT_LANE_CHECKPOINT_SHA = 'deadbeef00000000000000000000000000000000';
      fsMocks.existsSync.mockReturnValue(true);

      execFileSyncMock.mockImplementation((cmd: string, args: string[]) => {
        if (cmd === 'git' && Array.isArray(args) && args.includes('reset')
            && args.includes('deadbeef00000000000000000000000000000000')) {
          throw new Error('fatal: unknown revision or path not in the working tree');
        }
        return '';
      });

      await prepareWorkspaceForRole({
        workspace: '/ws',
        repoUrl: 'https://x-access-token:tok@github.com/o/r.git',
        githubToken: 'tok',
        branch: 'feat/lane',
      });

      // Cold path taken after the warm-path failure.
      const rmCall = execFileSyncMock.mock.calls.find(
        c => c[0] === 'rm' && Array.isArray(c[1]) && (c[1] as string[]).includes('-rf'),
      );
      expect(rmCall).toBeDefined();
      expect(cloneWithRetryMock).toHaveBeenCalledTimes(1);
      const logged = logMock.mock.calls.map(c => String(c[0])).join('\n');
      expect(logged).toMatch(/warm path failed/i);
    });

    it('lane mode off (BLUEPRINT_LANE_MODE unset): warm path uses origin/<branch>', async () => {
      process.env.BLUEPRINT_WARM_HANDOFF = '1';
      // BLUEPRINT_LANE_MODE intentionally not set.
      fsMocks.existsSync.mockReturnValue(true);

      await prepareWorkspaceForRole({
        workspace: '/ws',
        repoUrl: 'https://x-access-token:tok@github.com/o/r.git',
        githubToken: 'tok',
        branch: 'feat/normal',
      });

      const gitArgsList = execFileSyncMock.mock.calls
        .filter(c => c[0] === 'git')
        .map(c => (c[1] as string[]).join(' '));
      // Non-lane warm path: reset to origin/<branch>.
      expect(gitArgsList.some(a =>
        a.includes('reset --hard origin/feat/normal'),
      )).toBe(true);
      expect(cloneWithRetryMock).not.toHaveBeenCalled();
    });

    it('lane mode=1 but no checkpoint SHA: falls back to origin/<branch>', async () => {
      process.env.BLUEPRINT_WARM_HANDOFF = '1';
      process.env.BLUEPRINT_LANE_MODE = '1';
      // BLUEPRINT_LANE_CHECKPOINT_SHA intentionally not set.
      fsMocks.existsSync.mockReturnValue(true);

      await prepareWorkspaceForRole({
        workspace: '/ws',
        repoUrl: 'https://x-access-token:tok@github.com/o/r.git',
        githubToken: 'tok',
        branch: 'feat/lane-no-sha',
      });

      const gitArgsList = execFileSyncMock.mock.calls
        .filter(c => c[0] === 'git')
        .map(c => (c[1] as string[]).join(' '));
      // Without a SHA, fall back to normal reset target.
      expect(gitArgsList.some(a =>
        a.includes('reset --hard origin/feat/lane-no-sha'),
      )).toBe(true);
      expect(cloneWithRetryMock).not.toHaveBeenCalled();
    });
  });

  // ── WorkspacePrepOutcome: module-level recorder ─────────────────────────────
  //
  // After prepareWorkspaceForRole returns, getLastWorkspacePrepOutcome() holds a
  // structured record of what actually happened. This is what enrichStatusPayload
  // reads and emits under the key `workspace_prep`.
  describe('WorkspacePrepOutcome recorder', () => {
    afterEach(() => {
      delete process.env.BLUEPRINT_LANE_MODE;
      delete process.env.BLUEPRINT_LANE_CHECKPOINT_SHA;
    });

    it('genuine cold start (no warm handoff) → path=cold, cold_fallback_reason=null, lane_phase=false', async () => {
      // BLUEPRINT_WARM_HANDOFF unset → pure cold path, no warm attempt.
      await prepareWorkspaceForRole({
        workspace: '/ws',
        repoUrl: 'https://x-access-token:tok@github.com/o/r.git',
        githubToken: 'tok',
        branch: 'main',
      });

      const outcome = getLastWorkspacePrepOutcome();
      expect(outcome).not.toBeNull();
      expect(outcome?.path).toBe('cold');
      expect(outcome?.warm_reuse_succeeded).toBe(false);
      expect(outcome?.checkpoint_reset_applied).toBe(false);
      expect(outcome?.cold_fallback_reason).toBeNull();
      expect(outcome?.lane_phase).toBe(false);
    });

    it('warm success (non-lane) → path=warm, warm_reuse_succeeded=true, checkpoint_reset_applied=false', async () => {
      process.env.BLUEPRINT_WARM_HANDOFF = '1';
      fsMocks.existsSync.mockReturnValue(true);

      await prepareWorkspaceForRole({
        workspace: '/ws',
        repoUrl: 'https://x-access-token:tok@github.com/o/r.git',
        githubToken: 'tok',
        branch: 'feature/x',
      });

      const outcome = getLastWorkspacePrepOutcome();
      expect(outcome).not.toBeNull();
      expect(outcome?.path).toBe('warm');
      expect(outcome?.warm_reuse_succeeded).toBe(true);
      expect(outcome?.checkpoint_reset_applied).toBe(false);
      expect(outcome?.cold_fallback_reason).toBeNull();
      expect(outcome?.lane_phase).toBe(false);
    });

    it('lane warm success with checkpoint reset → path=warm, warm_reuse_succeeded=true, checkpoint_reset_applied=true', async () => {
      process.env.BLUEPRINT_WARM_HANDOFF = '1';
      process.env.BLUEPRINT_LANE_MODE = '1';
      process.env.BLUEPRINT_LANE_CHECKPOINT_SHA = 'abc1234def5678901234567890123456789012ab';
      fsMocks.existsSync.mockReturnValue(true);

      await prepareWorkspaceForRole({
        workspace: '/ws',
        repoUrl: 'https://x-access-token:tok@github.com/o/r.git',
        githubToken: 'tok',
        branch: 'feat/lane',
      });

      const outcome = getLastWorkspacePrepOutcome();
      expect(outcome).not.toBeNull();
      expect(outcome?.path).toBe('warm');
      expect(outcome?.warm_reuse_succeeded).toBe(true);
      expect(outcome?.checkpoint_reset_applied).toBe(true);
      expect(outcome?.cold_fallback_reason).toBeNull();
      expect(outcome?.lane_phase).toBe(true);
    });

    it('lane warm mode but no checkpoint SHA → path=warm, checkpoint_reset_applied=false', async () => {
      process.env.BLUEPRINT_WARM_HANDOFF = '1';
      process.env.BLUEPRINT_LANE_MODE = '1';
      // BLUEPRINT_LANE_CHECKPOINT_SHA intentionally not set.
      fsMocks.existsSync.mockReturnValue(true);

      await prepareWorkspaceForRole({
        workspace: '/ws',
        repoUrl: 'https://x-access-token:tok@github.com/o/r.git',
        githubToken: 'tok',
        branch: 'feat/lane-no-sha',
      });

      const outcome = getLastWorkspacePrepOutcome();
      expect(outcome?.path).toBe('warm');
      expect(outcome?.warm_reuse_succeeded).toBe(true);
      expect(outcome?.checkpoint_reset_applied).toBe(false);
      expect(outcome?.lane_phase).toBe(true);
    });

    it('warm failure → cold fallback: path=cold, warm_reuse_succeeded=false, non-null cold_fallback_reason', async () => {
      process.env.BLUEPRINT_WARM_HANDOFF = '1';
      fsMocks.existsSync.mockReturnValue(true);

      execFileSyncMock.mockImplementation((cmd: string, args: string[]) => {
        if (cmd === 'git') {
          const arg0 = args.includes('-C') ? args[2] : args[0];
          if (arg0 === 'fetch') throw new Error('network unreachable');
        }
        return '';
      });

      await prepareWorkspaceForRole({
        workspace: '/ws',
        repoUrl: 'https://x-access-token:tok@github.com/o/r.git',
        githubToken: 'tok',
        branch: 'feature/x',
      });

      const outcome = getLastWorkspacePrepOutcome();
      expect(outcome).not.toBeNull();
      expect(outcome?.path).toBe('cold');
      expect(outcome?.warm_reuse_succeeded).toBe(false);
      expect(outcome?.checkpoint_reset_applied).toBe(false);
      expect(outcome?.cold_fallback_reason).not.toBeNull();
      expect(outcome?.cold_fallback_reason).toMatch(/^warm_path_error:/);
      expect(outcome?.cold_fallback_reason).toMatch(/network unreachable/);
      // Reason is bounded (≤ 220 chars including prefix).
      expect((outcome?.cold_fallback_reason ?? '').length).toBeLessThanOrEqual(220);
      expect(outcome?.lane_phase).toBe(false);
    });

    it('warm failure redacts tokenized repo URLs from the fallback reason and log output', async () => {
      process.env.BLUEPRINT_WARM_HANDOFF = '1';
      fsMocks.existsSync.mockReturnValue(true);

      execFileSyncMock.mockImplementation((cmd: string, args: string[]) => {
        if (cmd === 'git' && Array.isArray(args) && args.includes('remote')) {
          throw new Error(
            'Command failed: git -C /ws remote set-url origin https://x-access-token:tok@github.com/o/r.git\n'
              + 'fatal: authentication failed',
          );
        }
        return '';
      });

      await prepareWorkspaceForRole({
        workspace: '/ws',
        repoUrl: 'https://x-access-token:tok@github.com/o/r.git',
        githubToken: 'tok',
        branch: 'feature/x',
      });

      const outcome = getLastWorkspacePrepOutcome();
      expect(outcome?.path).toBe('cold');
      expect(outcome?.cold_fallback_reason).toContain('https://x-access-token:***@github.com/o/r.git');
      expect(outcome?.cold_fallback_reason).not.toContain('https://x-access-token:tok@github.com/o/r.git');
      expect(logMock.mock.calls.some(call =>
        String(call[0]).includes('https://x-access-token:***@github.com/o/r.git'),
      )).toBe(true);
      expect(logMock.mock.calls.some(call =>
        String(call[0]).includes('https://x-access-token:tok@github.com/o/r.git'),
      )).toBe(false);
    });

    it('lane warm failure → cold fallback: lane_phase=true even on cold path', async () => {
      process.env.BLUEPRINT_WARM_HANDOFF = '1';
      process.env.BLUEPRINT_LANE_MODE = '1';
      process.env.BLUEPRINT_LANE_CHECKPOINT_SHA = 'deadbeef00000000000000000000000000000000';
      fsMocks.existsSync.mockReturnValue(true);

      execFileSyncMock.mockImplementation((cmd: string, args: string[]) => {
        if (cmd === 'git' && Array.isArray(args) && args.includes('reset')
            && args.includes('deadbeef00000000000000000000000000000000')) {
          throw new Error('fatal: unknown revision or path not in the working tree');
        }
        return '';
      });

      await prepareWorkspaceForRole({
        workspace: '/ws',
        repoUrl: 'https://x-access-token:tok@github.com/o/r.git',
        githubToken: 'tok',
        branch: 'feat/lane',
      });

      const outcome = getLastWorkspacePrepOutcome();
      expect(outcome?.path).toBe('cold');
      expect(outcome?.warm_reuse_succeeded).toBe(false);
      expect(outcome?.checkpoint_reset_applied).toBe(false);
      expect(outcome?.cold_fallback_reason).toMatch(/^warm_path_error:/);
      expect(outcome?.lane_phase).toBe(true);
    });

    it('review side (allowLaneResume=false): lane_phase=false even when BLUEPRINT_LANE_MODE=1', async () => {
      process.env.BLUEPRINT_WARM_HANDOFF = '1';
      process.env.BLUEPRINT_LANE_MODE = '1';
      process.env.BLUEPRINT_LANE_CHECKPOINT_SHA = 'deadbeefcafe1234';
      fsMocks.existsSync.mockReturnValue(true);

      await prepareWorkspaceForRole({
        workspace: '/ws',
        repoUrl: 'https://x-access-token:tok@github.com/o/r.git',
        githubToken: 'tok',
        branch: 'feature/x',
        allowLaneResume: false,
      });

      const outcome = getLastWorkspacePrepOutcome();
      expect(outcome?.path).toBe('warm');
      expect(outcome?.warm_reuse_succeeded).toBe(true);
      expect(outcome?.checkpoint_reset_applied).toBe(false);
      // allowLaneResume=false → lane_phase is false (gate disabled).
      expect(outcome?.lane_phase).toBe(false);
    });

    it('resetWorkspacePrepOutcome clears the recorded outcome', async () => {
      await prepareWorkspaceForRole({
        workspace: '/ws',
        repoUrl: 'https://x-access-token:tok@github.com/o/r.git',
        githubToken: 'tok',
        branch: 'main',
      });
      expect(getLastWorkspacePrepOutcome()).not.toBeNull();
      resetWorkspacePrepOutcome();
      expect(getLastWorkspacePrepOutcome()).toBeNull();
    });

    it('enrichStatusPayload integration: getLastWorkspacePrepOutcome returns what workspace_prep would carry', async () => {
      process.env.BLUEPRINT_WARM_HANDOFF = '1';
      process.env.BLUEPRINT_LANE_MODE = '1';
      process.env.BLUEPRINT_LANE_CHECKPOINT_SHA = 'abc1234def5678901234567890123456789012ab';
      fsMocks.existsSync.mockReturnValue(true);

      await prepareWorkspaceForRole({
        workspace: '/ws',
        repoUrl: 'https://x-access-token:tok@github.com/o/r.git',
        githubToken: 'tok',
        branch: 'feat/lane',
      });

      // The value getLastWorkspacePrepOutcome() returns is exactly what
      // index.ts's enrichStatusPayload embeds under the key `workspace_prep`.
      const outcome = getLastWorkspacePrepOutcome();
      expect(outcome).toEqual({
        path: 'warm',
        warm_reuse_succeeded: true,
        checkpoint_reset_applied: true,
        cold_fallback_reason: null,
        lane_phase: true,
      });
    });
  });

  // ── resolveReferenceRepo unit behavior ──────────────────────────────────────
  describe('resolveReferenceRepo', () => {
    it('returns the mirror path when it exists on disk', () => {
      fsMocks.existsSync.mockImplementation((p: unknown) =>
        String(p).replace(/\\/g, '/').endsWith('/opt/agent-cache/git/o/r.git'));
      expect(resolveReferenceRepo('https://x-access-token:tok@github.com/o/r.git'))
        .toBe('/opt/agent-cache/git/o/r.git');
    });

    it('returns null when the mirror does not exist', () => {
      fsMocks.existsSync.mockReturnValue(false);
      expect(resolveReferenceRepo('https://x-access-token:tok@github.com/o/r.git')).toBeNull();
    });

    it('returns null for an unparseable URL', () => {
      fsMocks.existsSync.mockReturnValue(true);
      expect(resolveReferenceRepo('not a url')).toBeNull();
    });

    it('returns null when the path is not exactly owner/repo', () => {
      fsMocks.existsSync.mockReturnValue(true);
      // Nested path (owner/group/repo) — not the 2-segment owner/repo layout.
      expect(resolveReferenceRepo('https://github.com/o/g/r.git')).toBeNull();
    });
  });
});
