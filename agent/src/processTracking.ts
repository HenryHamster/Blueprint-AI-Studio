// Module-level tracking for child PIDs spawned by agent code.
//
// Phase 1 of the warm-VM continuity plan needs to kill leftover children
// (Claude Code subprocess, test runners, git operations) between roles
// running on the same warm workspace. Other modules register PIDs here as
// they spawn; the reset routine drains the set on role boundaries.
//
// The set holds raw OS PIDs (numbers). It is not a process-group abstraction:
// callers register the leader PID returned by `spawn`. To kill a whole group
// callers may pass a negative PID via the OS interface elsewhere — this
// module only stores what was registered.

const childPids = new Set<number>();

/**
 * Register a PID spawned by agent code so the reset routine can ensure it
 * is dead before the next role starts.
 */
export function registerChildPid(pid: number): void {
  if (typeof pid !== 'number' || !Number.isFinite(pid) || pid <= 0) return;
  childPids.add(pid);
}

/**
 * Remove a PID from the registry once we know the process exited cleanly.
 * Safe to call with a PID that was never registered.
 */
export function unregisterChildPid(pid: number): void {
  childPids.delete(pid);
}

/**
 * Send `signal` (default SIGKILL) to every still-alive registered PID.
 *
 * Liveness is checked via `process.kill(pid, 0)` — the zero-signal idiom
 * which performs a permission/existence check without delivering any
 * signal. ESRCH means the process is gone (reported in `alreadyDead`).
 * EPERM means the process is ALIVE but we lack rights to signal it; the
 * reset routine therefore CANNOT kill it before the next role starts,
 * which silently violates the warm-VM "no leaked children across roles"
 * guarantee. Such PIDs are reported in `unkillable` and a warning is
 * emitted so operators can spot the leak in production logs. ESRCH on
 * the actual signal delivery (race: died between probe and signal) is
 * still reported as `alreadyDead`.
 *
 * Returns lists of PIDs we actually signalled, ones already gone, and
 * ones that were alive but unsignalable. Does not throw.
 */
export function killAllChildren(signal: NodeJS.Signals = 'SIGKILL'): {
  killed: number[];
  alreadyDead: number[];
  unkillable: number[];
} {
  const killed: number[] = [];
  const alreadyDead: number[] = [];
  const unkillable: number[] = [];
  for (const pid of childPids) {
    let alive = true;
    let canSignal = true;
    try {
      // Zero-signal probe: returns silently if the process exists and we
      // can signal it. Throws ESRCH if it's gone, EPERM if we lack rights.
      process.kill(pid, 0);
    } catch (err) {
      const code = (err as NodeJS.ErrnoException).code;
      if (code === 'ESRCH') {
        alive = false;
        alreadyDead.push(pid);
      } else if (code === 'EPERM') {
        // The process IS alive but we cannot signal it. The reset routine
        // will NOT be able to kill it either, so this PID will leak across
        // the role boundary. Track it separately and warn so operators
        // can investigate the privilege mismatch in production.
        canSignal = false;
        unkillable.push(pid);
        console.warn(
          `[processTracking] EPERM on liveness probe for pid=${String(pid)}: ` +
            `process is alive but we lack rights to signal it. ` +
            `It will leak across the role boundary.`,
        );
      } else {
        // Unknown error: treat as gone to avoid hanging on something we
        // can't probe.
        alive = false;
        alreadyDead.push(pid);
      }
    }
    if (!alive || !canSignal) continue;
    try {
      process.kill(pid, signal);
      killed.push(pid);
    } catch (err) {
      const code = (err as NodeJS.ErrnoException).code;
      if (code === 'ESRCH') {
        // Race: died between the probe and the signal.
        alreadyDead.push(pid);
      } else if (code === 'EPERM') {
        // Race: the probe succeeded but rights were revoked before the
        // actual signal (e.g. process changed UID via setuid). Treat the
        // same as the probe-time EPERM path.
        unkillable.push(pid);
        console.warn(
          `[processTracking] EPERM on signal delivery for pid=${String(pid)}: ` +
            `unable to deliver ${signal}. PID will leak across the role boundary.`,
        );
      } else {
        alreadyDead.push(pid);
      }
    }
  }
  return { killed, alreadyDead, unkillable };
}

/** Test-only: snapshot of currently registered PIDs. */
export function _getRegisteredPids(): number[] {
  return Array.from(childPids);
}

/** Clears the registry. Used by reset.ts between roles AND by tests for fixture cleanup. */
export function clearPidRegistry(): void {
  childPids.clear();
}
