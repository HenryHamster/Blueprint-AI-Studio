// Per-workspace disk reservation and accounting.
//
// A workspace declares a disk reservation when it is created (see
// prepareWorkspaceForRole in workspacePrep.ts). The reservation:
//   1. Admits the workspace up front — if the workspace filesystem cannot
//      satisfy the reservation plus the configured low-disk floor, workspace
//      preparation fails fast instead of starting a task that dies mid-run
//      on ENOSPC.
//   2. Stays held for the workspace's lifetime and is released on teardown —
//      including the abnormal-exit path where preparation throws partway, so
//      a failed prep cannot silently leak a reservation and starve later
//      admission.
//   3. Makes per-workspace usage observable against a declared number
//      (getWorkspaceDiskAccounting) rather than inferred from a full disk.
//
// The reservation size is operator-tunable via BLUEPRINT_RUNNER_AGENT_DISK_
// RESERVATION_BYTES — the same knob the fleet runner's resource ledger
// consumes, forwarded to the agent through the isolation env allowlist.
// Unset → DEFAULT_AGENT_DISK_RESERVATION_BYTES (a measured default, see the
// constant below); an explicit 0 disables per-workspace reservation. The
// low-disk floor uses BLUEPRINT_RUNNER_MIN_DISK_BYTES.
//
// Reservations are held in-process keyed by workspace path. The runner daemon
// reaps the workspace directory when the agent exits, so in-process state is
// the right lifetime; a prep that fails abnormally releases its reservation
// explicitly, so the only way a reservation can outlive its workspace is a
// prep failure that forgets to release — exactly the path the regression
// tests guard.
import { execFileSync } from 'child_process';
import { log } from './index.js';
import { sanitize } from './utils/sanitize.js';

/** Env knob: bytes each workspace reserves for itself (0 disables). */
export const AGENT_DISK_RESERVATION_ENV = 'BLUEPRINT_RUNNER_AGENT_DISK_RESERVATION_BYTES';
/** Env knob: low-disk floor a reservation must fit above. */
export const MIN_FREE_DISK_ENV = 'BLUEPRINT_RUNNER_MIN_DISK_BYTES';

/**
 * Default per-workspace disk reservation, in bytes.
 *
 * Measured 2026-08-06 by building a real workspace for this repo through the
 * same setup path the fleet uses — `git clone --depth 100` (as setupWorkspace
 * and prepareWorkspaceForRole do) plus `npm ci` at the repo root, `agent/`,
 * and `admin-ui/` (the exact `./check` install commands) plus a Python venv
 * installing `backend/requirements.txt` — and du-measuring each component:
 *
 *   source tree (no .git / node_modules)    85 MiB
 *   .git (depth-100 clone)                  21 MiB
 *   root node_modules (npm ci)             509 MiB
 *   agent node_modules (npm ci)            120 MiB
 *   admin-ui node_modules (npm ci)          76 MiB
 *   backend .venv (pip install)            404 MiB
 *   ─────────────────────────────────────────────
 *   measured high-water total             1215 MiB (1.19 GiB)
 *
 * The default is sized for that high-water — all dep trees present in one
 * workspace, which a warm VM accumulates across roles — with headroom for
 * growth, rounded up to 2 GiB. 2 GiB is also the figure the backend already
 * uses as its workspace reserve floor for container-image jobs
 * (`_CONTAINER_WORKSPACE_MIN_FREE_DISK_BYTES_DEFAULT` in
 * orchestrator_runner_dispatch.py), so the agent and dispatch layers agree.
 * Set BLUEPRINT_RUNNER_AGENT_DISK_RESERVATION_BYTES=0 to disable.
 */
export const DEFAULT_AGENT_DISK_RESERVATION_BYTES = 2 * 1024 * 1024 * 1024; // 2 GiB

const _workspaceDiskReservations = new Map<string, number>();

/** Raised when a workspace's disk reservation cannot be satisfied. */
export class DiskReservationError extends Error {
  constructor(
    readonly workspace: string,
    readonly reservationBytes: number,
    readonly freeBytes: number | null,
    readonly minFreeBytes: number,
  ) {
    const detail = freeBytes !== null
      ? `${freeBytes} bytes free but at least ${minFreeBytes + reservationBytes} required`
      : 'free disk could not be measured';
    super(
      `disk reservation of ${reservationBytes} bytes for workspace ${workspace} `
        + `cannot be satisfied: ${detail}`,
    );
    this.name = 'DiskReservationError';
  }
}

function envBytes(name: string): number {
  const raw = process.env[name];
  if (!raw || !raw.trim()) return 0;
  const value = Number.parseInt(raw.trim(), 10);
  return Number.isFinite(value) && value > 0 ? value : 0;
}

/** Reserved bytes each workspace declares for itself (0 = reservation disabled). */
export function workspaceDiskReservationBytes(): number {
  const raw = process.env[AGENT_DISK_RESERVATION_ENV];
  // Unset or blank → the measured default. An explicit 0 still disables.
  if (raw === undefined || !raw.trim()) return DEFAULT_AGENT_DISK_RESERVATION_BYTES;
  const value = Number.parseInt(raw.trim(), 10);
  return Number.isFinite(value) && value > 0 ? value : 0;
}

/** Low-disk floor on the workspace filesystem; a reservation must fit above it. */
export function workspaceMinFreeDiskBytes(): number {
  return envBytes(MIN_FREE_DISK_ENV);
}

/** Free bytes on the filesystem holding `root`, or null when unmeasurable. */
export function freeDiskBytes(root: string): number | null {
  try {
    const out = execFileSync('df', ['-k', root], { encoding: 'utf-8', timeout: 5000 });
    const line = out.split('\n')[1];
    if (line) {
      const parts = line.trim().split(/\s+/);
      if (parts.length >= 4) {
        const freeKb = Number.parseInt(parts[3], 10);
        if (Number.isFinite(freeKb)) return freeKb * 1024;
      }
    }
  } catch (err) {
    log(`workspaceDisk: df probe failed for ${root}: ${sanitize(String(err)).slice(0, 160)}`);
  }
  return null;
}

/** Bytes currently reserved by `workspace`, or null when none is held. */
export function getWorkspaceDiskReservation(workspace: string): number | null {
  return _workspaceDiskReservations.get(workspace) ?? null;
}

/**
 * Declare and hold a disk reservation for `workspace`. Idempotent per
 * workspace (a warm role reuses the reservation it already holds). Throws
 * DiskReservationError — fail-fast admission — when the reservation cannot be
 * satisfied: free disk below the low-disk floor plus this reservation plus
 * every other reservation this process still holds.
 */
export function reserveWorkspaceDisk(workspace: string): number {
  const reservationBytes = workspaceDiskReservationBytes();
  if (reservationBytes <= 0) return 0;
  const existing = _workspaceDiskReservations.get(workspace);
  if (existing !== undefined) return existing;
  const minFreeBytes = workspaceMinFreeDiskBytes();
  const freeBytes = freeDiskBytes(workspace);
  if (freeBytes === null) {
    // Probe failure — fail open (matches the runner ledger), but still record
    // the reservation so accounting stays observable.
    log(`workspaceDisk: cannot measure free disk for ${workspace}; recording reservation without admission check`);
    _workspaceDiskReservations.set(workspace, reservationBytes);
    return reservationBytes;
  }
  let otherReservedBytes = 0;
  for (const [path, bytes] of _workspaceDiskReservations) {
    if (path !== workspace) otherReservedBytes += bytes;
  }
  if (freeBytes < minFreeBytes + otherReservedBytes + reservationBytes) {
    throw new DiskReservationError(workspace, reservationBytes, freeBytes, minFreeBytes);
  }
  _workspaceDiskReservations.set(workspace, reservationBytes);
  return reservationBytes;
}

/** Release the disk reservation held by `workspace` (no-op when none is held). */
export function releaseWorkspaceDiskReservation(workspace: string): void {
  _workspaceDiskReservations.delete(workspace);
}

/** Clear all held reservations (test isolation; the map is empty at process start). */
export function resetWorkspaceDiskReservations(): void {
  _workspaceDiskReservations.clear();
}

export interface WorkspaceDiskAccounting {
  reserved_bytes: number;
  actual_bytes: number | null;
  overcommit_bytes: number | null;
  free_bytes: number | null;
}

/**
 * Report actual per-workspace disk usage against its reservation. `actual_bytes`
 * is measured with `du -sk` (best-effort); `overcommit_bytes` is positive when
 * the workspace uses more than it reserved. Null usage means the workspace does
 * not exist or du failed.
 */
export function getWorkspaceDiskAccounting(workspace: string): WorkspaceDiskAccounting {
  const reservedBytes = getWorkspaceDiskReservation(workspace) ?? workspaceDiskReservationBytes();
  let actualBytes: number | null = null;
  try {
    const out = execFileSync('du', ['-sk', workspace], { encoding: 'utf-8', timeout: 30_000 });
    const first = out.split('\n')[0];
    if (first) {
      const kb = Number.parseInt(first.trim().split(/\s+/)[0], 10);
      if (Number.isFinite(kb)) actualBytes = kb * 1024;
    }
  } catch { /* accounting is best-effort */ }
  return {
    reserved_bytes: reservedBytes,
    actual_bytes: actualBytes,
    overcommit_bytes: actualBytes !== null ? actualBytes - reservedBytes : null,
    free_bytes: freeDiskBytes(workspace),
  };
}
