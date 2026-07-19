import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';
import path from 'path';

// ── Module mocks ─────────────────────────────────────────────────────────────
// fs mock — supports both default-import (`import fs from 'fs'`) and the
// `fs.promises.*` async APIs that reset.ts uses.
const fsPromisesMocks = vi.hoisted(() => ({
  unlink: vi.fn(),
  rm: vi.fn(),
  stat: vi.fn(),
}));

vi.mock('fs', () => ({
  default: {
    existsSync: vi.fn().mockReturnValue(false),
    readFileSync: vi.fn().mockReturnValue(''),
    writeFileSync: vi.fn(),
    appendFileSync: vi.fn(),
    mkdirSync: vi.fn(),
    readdirSync: vi.fn().mockReturnValue([]),
    unlinkSync: vi.fn(),
    promises: fsPromisesMocks,
  },
}));

// child_process mock — reset.ts wraps `execFile` with promisify, so we
// supply an execFile that accepts (cmd, args, opts, callback) and routes
// through a vi.fn the tests can configure per-case.
const execFileImpl = vi.hoisted(() => vi.fn());

vi.mock('child_process', () => ({
  execFile: (
    cmd: string,
    args: string[],
    opts: unknown,
    cb: (err: Error | null, stdout: string, stderr: string) => void,
  ) => {
    // promisify expects the callback to be the LAST arg. We forward
    // through to the test's mock impl, which can choose to throw or resolve.
    Promise.resolve()
      .then((): unknown => execFileImpl(cmd, args, opts))
      .then(
        (result: unknown) => {
          const out = typeof result === 'string' ? result : '';
          cb(null, out, '');
        },
        (err: unknown) => {
          cb(err instanceof Error ? err : new Error(String(err)), '', '');
        },
      );
  },
}));

// os mock — we want a deterministic homedir() for the ~/.claude assertion.
vi.mock('os', () => ({
  default: {
    homedir: vi.fn().mockReturnValue('/home/agent'),
  },
}));

// ── Imports (after mocks) ────────────────────────────────────────────────────
import { resetForNextRole, WorkspaceMissingError, clearStaleProviderCredentials, TMP_OUTPUT_NAMES } from './reset.js';
import {
  registerChildPid,
  killAllChildren,
  _getRegisteredPids,
  clearPidRegistry,
} from './processTracking.js';

// Narrow helper: process.kill has TypeScript overloads that make a class-level
// `let killSpy: ReturnType<typeof vi.spyOn<typeof process, 'kill'>>` refuse
// to typecheck. We hide the spy install in a function and infer the return
// type — same convention as elsewhere in the codebase.
function installKillSpy() {
  return vi.spyOn(process, 'kill').mockImplementation(() => true);
}
type KillSpy = ReturnType<typeof installKillSpy>;

function installConsoleSpy(method: 'log' | 'error') {
  return vi.spyOn(console, method).mockImplementation(() => undefined);
}
type ConsoleSpy = ReturnType<typeof installConsoleSpy>;

// ── processTracking ──────────────────────────────────────────────────────────
describe('processTracking', () => {
  let killSpy: KillSpy | null = null;

  beforeEach(() => {
    clearPidRegistry();
    killSpy = installKillSpy();
  });

  afterEach(() => {
    killSpy?.mockRestore();
    killSpy = null;
    clearPidRegistry();
  });

  it('register + killAllChildren signals each registered PID', () => {
    registerChildPid(111);
    registerChildPid(222);

    const result = killAllChildren('SIGKILL');

    expect(result.killed.sort()).toEqual([111, 222]);
    expect(result.alreadyDead).toEqual([]);
    // Liveness probe with signal 0:
    expect(killSpy).toHaveBeenCalledWith(111, 0);
    expect(killSpy).toHaveBeenCalledWith(222, 0);
    // Then SIGKILL delivery:
    expect(killSpy).toHaveBeenCalledWith(111, 'SIGKILL');
    expect(killSpy).toHaveBeenCalledWith(222, 'SIGKILL');
  });

  it('uses SIGKILL by default when no signal arg is given', () => {
    registerChildPid(333);

    killAllChildren();

    expect(killSpy).toHaveBeenCalledWith(333, 'SIGKILL');
  });

  it('reports ESRCH probes as alreadyDead, not killed', () => {
    registerChildPid(444);
    killSpy?.mockImplementation(((_pid: number, sig?: number | NodeJS.Signals) => {
      if (sig === 0) {
        const err = new Error('No such process') as NodeJS.ErrnoException;
        err.code = 'ESRCH';
        throw err;
      }
      return true;
    }) as typeof process.kill);

    const result = killAllChildren('SIGKILL');

    expect(result.killed).toEqual([]);
    expect(result.alreadyDead).toEqual([444]);
    // We should NOT have signalled SIGKILL once the probe said dead:
    expect(killSpy).not.toHaveBeenCalledWith(444, 'SIGKILL');
  });

  it('_getRegisteredPids reflects what was registered', () => {
    registerChildPid(10);
    registerChildPid(20);
    expect(_getRegisteredPids().sort((a, b) => a - b)).toEqual([10, 20]);
  });

  it('ignores non-finite or non-positive PIDs', () => {
    registerChildPid(0);
    registerChildPid(-1);
    registerChildPid(Number.NaN);
    expect(_getRegisteredPids()).toEqual([]);
  });
});

// ── resetForNextRole ─────────────────────────────────────────────────────────
describe('resetForNextRole', () => {
  let killSpy: KillSpy | null = null;
  let consoleErrorSpy: ConsoleSpy | null = null;
  let consoleLogSpy: ConsoleSpy | null = null;

  beforeEach(() => {
    clearPidRegistry();
    killSpy = installKillSpy();
    consoleErrorSpy = installConsoleSpy('error');
    consoleLogSpy = installConsoleSpy('log');
    fsPromisesMocks.unlink.mockReset();
    fsPromisesMocks.rm.mockReset();
    fsPromisesMocks.stat.mockReset();
    execFileImpl.mockReset();
    // Default: success for fs ops + git invocations.
    fsPromisesMocks.unlink.mockResolvedValue(undefined);
    fsPromisesMocks.rm.mockResolvedValue(undefined);
    fsPromisesMocks.stat.mockResolvedValue({});
    execFileImpl.mockResolvedValue('');
  });

  afterEach(() => {
    killSpy?.mockRestore();
    consoleErrorSpy?.mockRestore();
    consoleLogSpy?.mockRestore();
    killSpy = null;
    consoleErrorSpy = null;
    consoleLogSpy = null;
    clearPidRegistry();
  });

  it('attempts to unlink each known /tmp file for the prior task', async () => {
    await resetForNextRole({ workspace: '/ws', priorTaskId: 'X' });

    const unlinkPaths = fsPromisesMocks.unlink.mock.calls.map((c) => c[0] as string);
    expect(unlinkPaths).toContain('/tmp/agent_prompt_X.txt');
    expect(unlinkPaths).toContain('/tmp/review_verdict_X.json');
    expect(unlinkPaths).toContain('/tmp/panel_review_lens_correctness_X.json');
    expect(unlinkPaths).toContain('/tmp/panel_review_lens_security_X.json');
    expect(unlinkPaths).toContain('/tmp/panel_review_lens_code_quality_X.json');
    expect(unlinkPaths).toContain('/tmp/panel_review_lens_test_coverage_X.json');
    expect(unlinkPaths).toContain('/tmp/plan_output_X.json');
    expect(unlinkPaths).toContain('/tmp/research_findings_X.json');
    expect(unlinkPaths).toContain('/tmp/security_findings_X.json');
    expect(unlinkPaths).toContain('/tmp/smoketest_result_X.json');
    expect(unlinkPaths).toContain('/tmp/verify_tests_result_X.json');
    expect(unlinkPaths).toContain('/tmp/debug_report_X.json');
    expect(unlinkPaths).toContain('/tmp/perf_report_X.json');
    expect(unlinkPaths).toContain('/tmp/investigation_report_X.json');
    expect(unlinkPaths).toContain('/tmp/strategic_output_X.json');
    expect(unlinkPaths).toContain('/tmp/design_document_X.json');
    expect(unlinkPaths).toContain('/tmp/bootstrap_overview_X.json');
    expect(unlinkPaths).toContain('/tmp/command_memory_X.json');
    expect(unlinkPaths).toContain('/tmp/web_research_document_X.json');
    expect(unlinkPaths).toContain('/tmp/release_audit_report_X.json');
    expect(unlinkPaths).toContain('/tmp/release_audit_challenge_X.json');
    expect(unlinkPaths).toContain('/tmp/self_audit_findings_X.json');
    expect(unlinkPaths).toContain('/tmp/self_review_X.json');
    expect(unlinkPaths).toContain('/tmp/symbols_X.json');
    expect(unlinkPaths).toContain('/tmp/fix_responses_X.json');
    expect(unlinkPaths).toContain('/tmp/fix_sr_context_X.json');
    expect(unlinkPaths).toContain('/tmp/test_baseline_X.json');
    expect(unlinkPaths).toContain('/tmp/fix_parallelized_X.json');
    expect(unlinkPaths).toContain('/tmp/bootstrap_domain_X.json');
    expect(unlinkPaths).toContain('/tmp/research_insights_X.json');
    expect(unlinkPaths).toContain('/tmp/consumed_artifacts_X.json');
    expect(unlinkPaths).toContain('/tmp/problem_model_X.json');
    // Stale basenames that were incorrectly in the old list must be absent.
    expect(unlinkPaths).not.toContain('/tmp/debug_output_X.json');
    expect(unlinkPaths).not.toContain('/tmp/perf_output_X.json');
    expect(unlinkPaths).not.toContain('/tmp/design_doc_output_X.json');
  });

  it('throws a self-describing workspace error instead of spawn git ENOENT when the workspace is absent', async () => {
    const missing = new Error('ENOENT') as NodeJS.ErrnoException;
    missing.code = 'ENOENT';
    fsPromisesMocks.stat.mockRejectedValue(missing);

    await expect(resetForNextRole({ workspace: '/missing/workspace', priorTaskId: 'X' }))
      .rejects.toThrow(WorkspaceMissingError);
    await expect(resetForNextRole({ workspace: '/missing/workspace', priorTaskId: 'X' }))
      .rejects.toThrow('workspace missing at /missing/workspace — cannot hand off to next role');
    expect(execFileImpl).not.toHaveBeenCalledWith('git', ['reset', '--hard', 'HEAD'], expect.anything());
  });

  it('survives ENOENT on /tmp unlink and continues to subsequent steps', async () => {
    fsPromisesMocks.unlink.mockImplementation(() => {
      const err = new Error('not found') as NodeJS.ErrnoException;
      err.code = 'ENOENT';
      return Promise.reject(err);
    });

    await expect(resetForNextRole({ workspace: '/ws', priorTaskId: 'X' })).resolves.toBeUndefined();

    // Subsequent steps still ran:
    expect(fsPromisesMocks.rm).toHaveBeenCalled();
    // ENOENT during /tmp cleanup is silent — we should NOT have logged
    // a per-file error for it.
    const errorMessages =
      consoleErrorSpy?.mock.calls.map((c) => String(c[0])) ?? [];
    expect(errorMessages.some((m) => m.includes('ENOENT'))).toBe(false);
  });

  it('logs but does not throw when an unexpected unlink error occurs', async () => {
    fsPromisesMocks.unlink.mockImplementation(() => {
      const err = new Error('permission denied') as NodeJS.ErrnoException;
      err.code = 'EPERM';
      return Promise.reject(err);
    });

    await expect(resetForNextRole({ workspace: '/ws', priorTaskId: 'X' })).resolves.toBeUndefined();

    const errorMessages =
      consoleErrorSpy?.mock.calls.map((c) => String(c[0])) ?? [];
    expect(errorMessages.some((m) => m.includes('step 2 unlink'))).toBe(true);
  });

  it('wipes ~/.claude using fs.promises.rm with recursive: true', async () => {
    // Force HOME to the mocked-os value so resetForNextRole resolves
    // process.env.HOME first (matching the spawned subprocess's HOME).
    const priorHome = process.env.HOME;
    process.env.HOME = '/home/agent';
    try {
      await resetForNextRole({ workspace: '/ws', priorTaskId: 'X' });

      expect(fsPromisesMocks.rm).toHaveBeenCalledWith(
        // process.env.HOME='/home/agent'; path.join handles separators.
        expect.stringContaining('.claude'),
        expect.objectContaining({ recursive: true, force: true }),
      );
      const rmPath = fsPromisesMocks.rm.mock.calls
        .map((c) => c[0] as string)
        .find((p) => p.includes('.claude'));
      expect(rmPath).toMatch(/[\\/]home[\\/]agent[\\/]\.claude$/);
    } finally {
      if (priorHome === undefined) delete process.env.HOME;
      else process.env.HOME = priorHome;
    }
  });

  it('preserves boot-materialized provider credentials on the same-account fast path', async () => {
    // Regression guard (review iter 1): resetForNextRole must NOT clear the
    // account-scoped provider credential material. Same-account warm claims
    // (chain continuity / backlog reuse, both default-ON) carry no
    // secret_manifest, so performWarmRetarget never runs to re-materialize.
    // Wiping ~/.claude.json (Claude Code onboarding bypass), ~/.gemini settings
    // and the provider cred dirs here would leave role 2+ without config that
    // nothing re-creates. Only the account-divergent path (performWarmRetarget)
    // may clear these, and it re-materializes immediately afterwards.
    const priorHome = process.env.HOME;
    process.env.HOME = '/home/agent';
    try {
      await resetForNextRole({ workspace: '/ws', priorTaskId: 'X' });

      const rmPaths = fsPromisesMocks.rm.mock.calls.map((c) => c[0] as string);
      // The ~/.claude SESSION dir wipe (step 3) is still expected...
      // (path.join emits host separators; match both like the sibling tests.)
      expect(rmPaths.some((p) => /[\\/]home[\\/]agent[\\/]\.claude$/.test(p))).toBe(true);
      // ...but NONE of the account-scoped provider credential paths.
      expect(rmPaths).not.toContain('/etc/agent.env');
      expect(rmPaths).not.toContain('/home/agent/.claude.json');
      expect(rmPaths).not.toContain('/home/agent/.anthropic');
      expect(rmPaths).not.toContain('/home/agent/.openai');
      expect(rmPaths).not.toContain('/home/agent/.gemini');
      // And the sudo fallback (env-file removal) must not be invoked either.
      const sudoRm = execFileImpl.mock.calls.find(
        (c) => c[0] === 'sudo' && Array.isArray(c[1]) && (c[1] as string[])[0] === 'rm',
      );
      expect(sudoRm).toBeUndefined();
    } finally {
      if (priorHome === undefined) delete process.env.HOME;
      else process.env.HOME = priorHome;
    }
  });

  it('uses process.env.HOME (not os.homedir) when both differ', async () => {
    // The Claude Code subprocess HOME is process.env.HOME, which can disagree
    // with os.homedir() in container/sudo contexts. Reset must wipe the dir
    // the subprocess actually reads.
    const priorHome = process.env.HOME;
    process.env.HOME = '/var/agent-home';
    try {
      await resetForNextRole({ workspace: '/ws', priorTaskId: 'X' });
      const rmPath = fsPromisesMocks.rm.mock.calls
        .map((c) => c[0] as string)
        .find((p) => p.includes('.claude'));
      // Must match the env var, NOT the mocked os.homedir of /home/agent.
      expect(rmPath).toMatch(/[\\/]var[\\/]agent-home[\\/]\.claude$/);
      expect(rmPath).not.toMatch(/[\\/]home[\\/]agent[\\/]/);
    } finally {
      if (priorHome === undefined) delete process.env.HOME;
      else process.env.HOME = priorHome;
    }
  });

  it('falls back to os.homedir when process.env.HOME is unset', async () => {
    const priorHome = process.env.HOME;
    delete process.env.HOME;
    try {
      await resetForNextRole({ workspace: '/ws', priorTaskId: 'X' });
      const rmPath = fsPromisesMocks.rm.mock.calls
        .map((c) => c[0] as string)
        .find((p) => p.includes('.claude'));
      // os.homedir() is mocked to /home/agent in this test file.
      expect(rmPath).toMatch(/[\\/]home[\\/]agent[\\/]\.claude$/);
    } finally {
      if (priorHome === undefined) delete process.env.HOME;
      else process.env.HOME = priorHome;
    }
  });

  it('runs git reset --hard HEAD then git clean preserving caches', async () => {
    await resetForNextRole({ workspace: '/ws', priorTaskId: 'X' });

    const gitCalls = execFileImpl.mock.calls.filter((c) => c[0] === 'git');
    expect(gitCalls.length).toBeGreaterThanOrEqual(2);

    const resetCall = gitCalls.find(
      (c) => Array.isArray(c[1]) && (c[1] as string[])[0] === 'reset',
    );
    expect(resetCall).toBeDefined();
    expect(resetCall?.[1]).toEqual(['reset', '--hard', 'HEAD']);
    // Workspace cwd must be threaded through:
    expect((resetCall?.[2] as { cwd?: string }).cwd).toBe('/ws');

    const cleanCall = gitCalls.find(
      (c) => Array.isArray(c[1]) && (c[1] as string[])[0] === 'clean',
    );
    expect(cleanCall).toBeDefined();
    const cleanArgs = cleanCall?.[1] as string[];
    expect(cleanArgs).toContain('-fdx');
    expect(cleanArgs).toContain('-e');
    expect(cleanArgs).toContain('node_modules');
    expect(cleanArgs).toContain('.testmondata');
    expect(cleanArgs).toContain('.vitest_cache');
    expect(cleanArgs).toContain('.pnpm-store');
  });

  it('throws when git reset --hard fails (workspace gone is unrecoverable)', async () => {
    execFileImpl.mockImplementation((cmd: string, args: string[]) => {
      if (cmd === 'git' && args[0] === 'reset') {
        return Promise.reject(new Error('not a git repository'));
      }
      return Promise.resolve('');
    });

    await expect(resetForNextRole({ workspace: '/ws', priorTaskId: 'X' })).rejects.toThrow(
      /not a git repository/,
    );
  });

  it('does not run fetch/rebase when baseBranch is omitted', async () => {
    await resetForNextRole({ workspace: '/ws', priorTaskId: 'X' });

    const gitArgs = execFileImpl.mock.calls
      .filter((c) => c[0] === 'git')
      .map((c) => (c[1] as string[])[0]);
    expect(gitArgs).not.toContain('fetch');
    expect(gitArgs).not.toContain('rebase');
  });

  it('runs fetch then rebase against origin/<baseBranch> when set', async () => {
    await resetForNextRole({
      workspace: '/ws',
      priorTaskId: 'X',
      baseBranch: 'main',
    });

    const gitCalls = execFileImpl.mock.calls.filter((c) => c[0] === 'git');
    const fetchCall = gitCalls.find((c) => (c[1] as string[])[0] === 'fetch');
    const rebaseCall = gitCalls.find((c) => (c[1] as string[])[0] === 'rebase');

    expect(fetchCall?.[1]).toEqual(['fetch', 'origin', 'main']);
    expect(rebaseCall?.[1]).toEqual(['rebase', 'origin/main']);
  });

  it('aborts cleanly and does not throw when rebase fails', async () => {
    execFileImpl.mockImplementation((cmd: string, args: string[]) => {
      if (cmd === 'git' && args[0] === 'rebase' && args[1] !== '--abort') {
        return Promise.reject(new Error('CONFLICT (content): merge conflict'));
      }
      return Promise.resolve('');
    });

    await expect(
      resetForNextRole({
        workspace: '/ws',
        priorTaskId: 'X',
        baseBranch: 'main',
      }),
    ).resolves.toBeUndefined();

    const rebaseAbortCall = execFileImpl.mock.calls.find(
      (c) => c[0] === 'git' && (c[1] as string[]).includes('--abort'),
    );
    expect(rebaseAbortCall).toBeDefined();
    expect(rebaseAbortCall?.[1]).toEqual(['rebase', '--abort']);
  });

  it('integrates: kills registered PIDs and clears the registry for next role', async () => {
    registerChildPid(7777);
    registerChildPid(8888);

    await resetForNextRole({ workspace: '/ws', priorTaskId: 'X' });

    // Both PIDs were probed and SIGKILLed:
    expect(killSpy).toHaveBeenCalledWith(7777, 0);
    expect(killSpy).toHaveBeenCalledWith(8888, 0);
    expect(killSpy).toHaveBeenCalledWith(7777, 'SIGKILL');
    expect(killSpy).toHaveBeenCalledWith(8888, 'SIGKILL');
    // Registry was drained for the next role.
    expect(_getRegisteredPids()).toEqual([]);
  });

  it('logs via console.error on individual step failures without throwing', async () => {
    // Make /tmp unlink fail on a non-ENOENT error so we exercise the
    // logging path explicitly.
    fsPromisesMocks.unlink.mockImplementation(() => {
      const err = new Error('disk full') as NodeJS.ErrnoException;
      err.code = 'ENOSPC';
      return Promise.reject(err);
    });

    await expect(
      resetForNextRole({ workspace: '/ws', priorTaskId: 'X' }),
    ).resolves.toBeUndefined();

    expect(consoleErrorSpy).toHaveBeenCalled();
  });

  it('step 1 logs unkillable count when EPERM is reported', async () => {
    // killAllChildren returns three buckets: killed / alreadyDead / unkillable.
    // Pre-fix the reset step destructured only {killed, alreadyDead} — leaving
    // EPERM-bucket leaks invisible to the operator. Verify the post-fix log
    // surfaces the unkillable count.
    registerChildPid(9001); // alive, kill OK
    registerChildPid(9002); // ESRCH (gone)
    registerChildPid(9003); // EPERM (alive but unsignalable)
    killSpy?.mockImplementation(((pid: number, sig?: number | NodeJS.Signals) => {
      if (pid === 9002) {
        const err = new Error('No such process') as NodeJS.ErrnoException;
        err.code = 'ESRCH';
        throw err;
      }
      if (pid === 9003) {
        const err = new Error('Operation not permitted') as NodeJS.ErrnoException;
        err.code = 'EPERM';
        throw err;
      }
      // 9001 — probe succeeds, signal succeeds.
      if (sig === 0) return true;
      return true;
    }) as typeof process.kill);

    await resetForNextRole({ workspace: '/ws', priorTaskId: 'X' });

    const logMessages =
      consoleLogSpy?.mock.calls.map((c) => String(c[0])) ?? [];
    const step1Log = logMessages.find((m) => m.includes('killAllChildren'));
    expect(step1Log).toBeDefined();
    expect(step1Log).toMatch(/signalled 1/);
    expect(step1Log).toMatch(/1 already gone/);
    expect(step1Log).toMatch(/1 unkillable \(EPERM\)/);
  });

  it('step 1 omits the unkillable suffix when no EPERM is reported', async () => {
    // Regression guard: the new suffix must only appear when there are
    // unkillable PIDs. A clean run should still log the existing format
    // without the trailing "X unkillable (EPERM)" clause.
    registerChildPid(9100);
    // Default killSpy returns true for all signals (probe + SIGKILL).

    await resetForNextRole({ workspace: '/ws', priorTaskId: 'X' });

    const logMessages =
      consoleLogSpy?.mock.calls.map((c) => String(c[0])) ?? [];
    const step1Log = logMessages.find((m) => m.includes('killAllChildren'));
    expect(step1Log).toBeDefined();
    expect(step1Log).not.toMatch(/unkillable/);
  });
});

// ── resetForNextRole — integration: full /tmp artifact surface ───────────────
describe('resetForNextRole — integration: full /tmp artifact surface', () => {
  let killSpy: KillSpy | null = null;
  let consoleErrorSpy: ConsoleSpy | null = null;
  let consoleLogSpy: ConsoleSpy | null = null;

  beforeEach(() => {
    clearPidRegistry();
    killSpy = installKillSpy();
    consoleErrorSpy = installConsoleSpy('error');
    consoleLogSpy = installConsoleSpy('log');
    fsPromisesMocks.unlink.mockReset();
    fsPromisesMocks.rm.mockReset();
    execFileImpl.mockReset();
    fsPromisesMocks.unlink.mockResolvedValue(undefined);
    fsPromisesMocks.rm.mockResolvedValue(undefined);
    execFileImpl.mockResolvedValue('');
  });

  afterEach(() => {
    killSpy?.mockRestore();
    consoleErrorSpy?.mockRestore();
    consoleLogSpy?.mockRestore();
    killSpy = null;
    consoleErrorSpy = null;
    consoleLogSpy = null;
    clearPidRegistry();
  });

  it('unlinks every path derived from TMP_OUTPUT_NAMES for the prior task', async () => {
    const taskId = 'integration-test-task-abc123';

    await resetForNextRole({ workspace: '/ws', priorTaskId: taskId });

    const unlinkPaths = fsPromisesMocks.unlink.mock.calls.map((c) => c[0] as string);

    // Dynamic check: every name in TMP_OUTPUT_NAMES must appear in unlink calls.
    // This keeps coverage in sync with TMP_OUTPUT_NAMES automatically — adding a
    // new name to the list will cause this test to verify it is also cleaned up.
    for (const name of TMP_OUTPUT_NAMES) {
      expect(unlinkPaths).toContain(`/tmp/${name}_${taskId}.json`);
    }

    // The agent prompt file is cleaned up explicitly (outside TMP_OUTPUT_NAMES).
    expect(unlinkPaths).toContain(`/tmp/agent_prompt_${taskId}.txt`);

    // Total unlink count: one per TMP_OUTPUT_NAMES entry + the agent_prompt file.
    // Verifies no extra stale or unexpected paths are touched.
    expect(unlinkPaths).toHaveLength(TMP_OUTPUT_NAMES.length + 1);
  });

  it('unlinks every path derived from TMP_OUTPUT_NAMES for the prior task — rm covers viewport_review dir', async () => {
    const taskId = 'integration-test-task-abc123';

    await resetForNextRole({ workspace: '/ws', priorTaskId: taskId });

    // The viewport_review dir is cleaned via fs.promises.rm (recursive),
    // not unlink, because it is a directory containing screenshot files.
    const rmPaths = fsPromisesMocks.rm.mock.calls.map((c) => c[0] as string);
    expect(rmPaths).toContain(`/tmp/viewport_review_${taskId}`);
    // Must use { recursive: true, force: true } so non-empty dirs are removed
    // and missing dirs are silently skipped.
    const viewportRmCall = fsPromisesMocks.rm.mock.calls.find(
      (c) => c[0] === `/tmp/viewport_review_${taskId}`,
    );
    expect(viewportRmCall?.[1]).toEqual(expect.objectContaining({ recursive: true, force: true }));

    // handleFix downloads its screenshots into /tmp/viewport_fix_<task> — it
    // must be cleaned with the same recursive rm so it does not leak across
    // reused warm workspaces.
    expect(rmPaths).toContain(`/tmp/viewport_fix_${taskId}`);
    const viewportFixRmCall = fsPromisesMocks.rm.mock.calls.find(
      (c) => c[0] === `/tmp/viewport_fix_${taskId}`,
    );
    expect(viewportFixRmCall?.[1]).toEqual(expect.objectContaining({ recursive: true, force: true }));
  });

  it('includes "symbols" to prevent ctags file leak on warm VMs between roles', () => {
    // handlers/standard.ts:393 writes /tmp/symbols_{taskId}.json (ctags symbol
    // index) during workspace prep. If 'symbols' is absent from TMP_OUTPUT_NAMES
    // the file leaks across warm-VM role boundaries and accumulates until the VM
    // is recycled. Regression guard: dropping 'symbols' from the list fails this
    // test immediately.
    expect(TMP_OUTPUT_NAMES).toContain('symbols');
  });
});

// ── clearStaleProviderCredentials ────────────────────────────────────────────
describe('clearStaleProviderCredentials', () => {
  let consoleErrorSpy: ConsoleSpy | null = null;

  // path.resolve() on the host running the tests (Windows CI included) maps
  // '/home/agent/...' to an absolute form with the host's separators; resolve
  // the expectation the same way so assertions are platform-agnostic.
  const resolved = (p: string) => path.resolve(p);

  beforeEach(() => {
    consoleErrorSpy = installConsoleSpy('error');
    fsPromisesMocks.rm.mockReset();
    execFileImpl.mockReset();
    fsPromisesMocks.rm.mockResolvedValue(undefined);
    execFileImpl.mockResolvedValue('');
  });

  afterEach(() => {
    consoleErrorSpy?.mockRestore();
    consoleErrorSpy = null;
  });

  it('removes every provider credential path when the manifest owns none of them', async () => {
    await clearStaleProviderCredentials([], '/home/agent');

    const rmCalls = fsPromisesMocks.rm.mock.calls;
    const rmPaths = rmCalls.map((c) => c[0] as string);
    expect(rmPaths).toContain(resolved('/home/agent/.anthropic'));
    expect(rmPaths).toContain(resolved('/home/agent/.openai'));
    expect(rmPaths).toContain(resolved('/home/agent/.gemini'));
    expect(rmPaths).toContain(resolved('/home/agent/.claude.json'));
    // Every removal must be recursive + force so dirs and missing paths are fine.
    for (const c of rmCalls) {
      expect(c[1]).toEqual(expect.objectContaining({ recursive: true, force: true }));
    }
  });

  it('NEVER removes the systemd env file (materialize rewrote it in place)', async () => {
    // Regression guard for the 2026-06-06 poisoned-VM window: removing
    // /etc/agent.env here would recreate the configless gap that
    // write-new-then-swap exists to close.
    await clearStaleProviderCredentials([], '/home/agent');

    const rmPaths = fsPromisesMocks.rm.mock.calls.map((c) => c[0] as string);
    expect(rmPaths.some((p) => p.includes('agent.env'))).toBe(false);
    const sudoCall = execFileImpl.mock.calls.find((c) => c[0] === 'sudo');
    expect(sudoCall).toBeUndefined();
  });

  it('preserves a path the new manifest owns exactly (~/.claude.json on an anthropic re-target)', async () => {
    await clearStaleProviderCredentials(['/home/agent/.claude.json'], '/home/agent');

    const rmPaths = fsPromisesMocks.rm.mock.calls.map((c) => c[0] as string);
    expect(rmPaths).not.toContain(resolved('/home/agent/.claude.json'));
    // The other provider paths are still stale and must go.
    expect(rmPaths).toContain(resolved('/home/agent/.anthropic'));
    expect(rmPaths).toContain(resolved('/home/agent/.openai'));
    expect(rmPaths).toContain(resolved('/home/agent/.gemini'));
  });

  it('preserves a directory that CONTAINS a manifest-owned file (~/.gemini for settings.json)', async () => {
    await clearStaleProviderCredentials(['/home/agent/.gemini/settings.json'], '/home/agent');

    const rmPaths = fsPromisesMocks.rm.mock.calls.map((c) => c[0] as string);
    expect(rmPaths).not.toContain(resolved('/home/agent/.gemini'));
    // Stale paths from the prior account are still removed.
    expect(rmPaths).toContain(resolved('/home/agent/.claude.json'));
    expect(rmPaths).toContain(resolved('/home/agent/.anthropic'));
    expect(rmPaths).toContain(resolved('/home/agent/.openai'));
  });

  it('does not preserve on prefix-only matches (.gemini-extra is not .gemini)', async () => {
    await clearStaleProviderCredentials(['/home/agent/.gemini-extra/settings.json'], '/home/agent');

    const rmPaths = fsPromisesMocks.rm.mock.calls.map((c) => c[0] as string);
    expect(rmPaths).toContain(resolved('/home/agent/.gemini'));
  });

  it('falls back to `sudo rm -rf` when the agent user lacks permission', async () => {
    fsPromisesMocks.rm.mockImplementation((p: string) => {
      if (p === resolved('/home/agent/.claude.json')) {
        const err = new Error('permission denied') as NodeJS.ErrnoException;
        err.code = 'EACCES';
        return Promise.reject(err);
      }
      return Promise.resolve(undefined);
    });

    await clearStaleProviderCredentials([], '/home/agent');

    const sudoCall = execFileImpl.mock.calls.find((c) => c[0] === 'sudo');
    expect(sudoCall).toBeDefined();
    expect(sudoCall?.[1]).toEqual(['rm', '-rf', resolved('/home/agent/.claude.json')]);
  });

  it('resolves (no throw) and logs when an unexpected rm error occurs', async () => {
    fsPromisesMocks.rm.mockRejectedValue(new Error('something weird'));
    await expect(clearStaleProviderCredentials([], '/home/agent')).resolves.toBeUndefined();
    expect(consoleErrorSpy).toHaveBeenCalled();
  });
});
