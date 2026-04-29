import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';

import {
  registerChildPid,
  killAllChildren,
  clearPidRegistry,
} from './processTracking.js';

// ── helpers (mirror the reset.test.ts pattern) ──────────────────────────────
//
// process.kill has TypeScript overloads that make a class-level
// `let killSpy: ReturnType<typeof vi.spyOn<typeof process, 'kill'>>` refuse
// to typecheck. Hide the spy install in a function and infer the return type.
function installKillSpy() {
  return vi.spyOn(process, 'kill').mockImplementation(() => true);
}
type KillSpy = ReturnType<typeof installKillSpy>;

function installWarnSpy() {
  return vi.spyOn(console, 'warn').mockImplementation(() => undefined);
}
type WarnSpy = ReturnType<typeof installWarnSpy>;

// ── EPERM differentiation (warm-VM continuity rev, deferred fix) ─────────────
//
// Pre-fix: EPERM (probe says "process is alive but we lack rights") was lumped
// with ESRCH into `alreadyDead`. That silently violates the "no leaked
// children across roles" guarantee — the next role inherits the process, and
// the operator never sees the leak. Post-fix: EPERM goes to a distinct
// `unkillable` bucket and is announced via console.warn.
describe('killAllChildren — EPERM differentiation', () => {
  let killSpy: KillSpy | null = null;
  let warnSpy: WarnSpy | null = null;

  beforeEach(() => {
    clearPidRegistry();
    killSpy = installKillSpy();
    warnSpy = installWarnSpy();
  });

  afterEach(() => {
    killSpy?.mockRestore();
    warnSpy?.mockRestore();
    killSpy = null;
    warnSpy = null;
    clearPidRegistry();
  });

  it('reports EPERM at probe time as unkillable, not alreadyDead', () => {
    registerChildPid(555);
    killSpy?.mockImplementation(((_pid: number, sig?: number | NodeJS.Signals) => {
      if (sig === 0) {
        const err = new Error('Operation not permitted') as NodeJS.ErrnoException;
        err.code = 'EPERM';
        throw err;
      }
      return true;
    }) as typeof process.kill);

    const result = killAllChildren('SIGKILL');

    expect(result.killed).toEqual([]);
    expect(result.alreadyDead).toEqual([]);
    expect(result.unkillable).toEqual([555]);
    // No SIGKILL attempt once probe says EPERM — it would just fail again.
    expect(killSpy).not.toHaveBeenCalledWith(555, 'SIGKILL');
    // A leak warning MUST be emitted so operators can spot the leak.
    expect(warnSpy).toHaveBeenCalled();
    const warnArgs = warnSpy?.mock.calls.flat() ?? [];
    expect(warnArgs.some((a) => String(a).includes('555'))).toBe(true);
    expect(warnArgs.some((a) => String(a).toLowerCase().includes('eperm'))).toBe(true);
  });

  it('reports EPERM raised on signal delivery (race) as unkillable', () => {
    // Probe succeeds, but rights are revoked before the actual SIGKILL —
    // models a setuid race or a privilege change between probe and signal.
    registerChildPid(666);
    killSpy?.mockImplementation(((_pid: number, sig?: number | NodeJS.Signals) => {
      if (sig === 0) {
        return true; // probe OK
      }
      const err = new Error('Operation not permitted') as NodeJS.ErrnoException;
      err.code = 'EPERM';
      throw err;
    }) as typeof process.kill);

    const result = killAllChildren('SIGKILL');

    expect(result.killed).toEqual([]);
    expect(result.alreadyDead).toEqual([]);
    expect(result.unkillable).toEqual([666]);
    expect(warnSpy).toHaveBeenCalled();
  });

  it('keeps ESRCH in alreadyDead (regression guard for existing reset.ts log)', () => {
    // The fix MUST NOT reclassify ESRCH — reset.ts and reset.test.ts depend
    // on ESRCH continuing to land in alreadyDead.
    registerChildPid(777);
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
    expect(result.alreadyDead).toEqual([777]);
    expect(result.unkillable).toEqual([]);
    // ESRCH does NOT warrant a leak warning.
    expect(warnSpy).not.toHaveBeenCalled();
  });

  it('partitions a mixed batch into killed / alreadyDead / unkillable', () => {
    registerChildPid(1001); // alive, kill OK
    registerChildPid(1002); // ESRCH (gone)
    registerChildPid(1003); // EPERM (alive but unsignalable)
    killSpy?.mockImplementation(((pid: number, sig?: number | NodeJS.Signals) => {
      if (pid === 1002) {
        const err = new Error('No such process') as NodeJS.ErrnoException;
        err.code = 'ESRCH';
        throw err;
      }
      if (pid === 1003) {
        const err = new Error('Operation not permitted') as NodeJS.ErrnoException;
        err.code = 'EPERM';
        throw err;
      }
      // 1001 and any signal-delivery for 1001 succeed.
      if (sig === 0) return true;
      return true;
    }) as typeof process.kill);

    const result = killAllChildren('SIGKILL');

    expect(result.killed).toEqual([1001]);
    expect(result.alreadyDead).toEqual([1002]);
    expect(result.unkillable).toEqual([1003]);
    expect(warnSpy).toHaveBeenCalled();
  });
});
