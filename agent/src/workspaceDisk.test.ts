import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';

// ── Module mocks ─────────────────────────────────────────────────────────────
// child_process.execFileSync drives the df probe behind freeDiskBytes and the
// du probe behind getWorkspaceDiskAccounting; mock it so each test can control
// the reported free disk. execFile and spawn are supplied too because the real
// index.js module graph (loaded below through the delegating mock) imports them.
const execFileSyncMock = vi.hoisted(() => vi.fn().mockReturnValue(''));

vi.mock('child_process', () => ({
  execFileSync: execFileSyncMock,
  execFile: vi.fn(),
  spawn: vi.fn().mockReturnValue({
    stderr: { on: vi.fn() },
    stdout: { on: vi.fn() },
    on: vi.fn(),
    kill: vi.fn(),
  }),
}));

// workspaceDisk imports `log` from index.js (the full agent entrypoint). Use a
// delegating factory — spread the real module and override only `log` — so the
// mock cannot drift from index.js's exports (the mock-factory-export-coverage
// guard flags full-replacement factories that omit real exports) and so the
// unit under test loads without bootstrapping the whole agent.
const logMock = vi.hoisted(() => vi.fn());

vi.mock('./index.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./index.js')>();
  return { ...actual, log: logMock };
});

// ── Imports (after mocks) ────────────────────────────────────────────────────
import {
  DEFAULT_AGENT_DISK_RESERVATION_BYTES,
  AGENT_DISK_RESERVATION_ENV,
  MIN_FREE_DISK_ENV,
  workspaceDiskReservationBytes,
  workspaceMinFreeDiskBytes,
  reserveWorkspaceDisk,
  getWorkspaceDiskReservation,
  resetWorkspaceDiskReservations,
  DiskReservationError,
} from './workspaceDisk.js';

function mockDfFreeKb(freeKb: number): void {
  execFileSyncMock.mockImplementation((cmd: string) =>
    cmd === 'df'
      ? `Filesystem 1K-blocks Used Available Use% Mounted on\n/dev/root 1000000 500000 ${freeKb} 50% /\n`
      : '');
}

describe('workspaceDisk default reservation', () => {
  beforeEach(() => {
    execFileSyncMock.mockReset();
    execFileSyncMock.mockReturnValue('');
    // Clear any env stubs from a prior test so each test starts with both
    // knobs unset (the unset state is what the default path expects).
    vi.unstubAllEnvs();
    resetWorkspaceDiskReservations();
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    resetWorkspaceDiskReservations();
  });

  it('reserves a measured non-zero per-workspace disk default', () => {
    // The shipped default is a real, measured figure — never 0, which would
    // silently disable the per-workspace admission guard.
    expect(DEFAULT_AGENT_DISK_RESERVATION_BYTES).toBeGreaterThan(0);
    // With the env knob unset, the reservation resolves to that shipped
    // default, so the guard is live by default rather than opt-in.
    expect(workspaceDiskReservationBytes()).toBe(DEFAULT_AGENT_DISK_RESERVATION_BYTES);
  });

  it('an explicit env override wins over the default; 0 still disables', () => {
    vi.stubEnv(AGENT_DISK_RESERVATION_ENV, String(8 * 1024 * 1024 * 1024));
    expect(workspaceDiskReservationBytes()).toBe(8 * 1024 * 1024 * 1024);

    vi.stubEnv(AGENT_DISK_RESERVATION_ENV, '0');
    expect(workspaceDiskReservationBytes()).toBe(0);
  });

  it('blank env falls back to the default; an unparsable value disables', () => {
    vi.stubEnv(AGENT_DISK_RESERVATION_ENV, '   ');
    expect(workspaceDiskReservationBytes()).toBe(DEFAULT_AGENT_DISK_RESERVATION_BYTES);

    vi.stubEnv(AGENT_DISK_RESERVATION_ENV, 'not-a-number');
    expect(workspaceDiskReservationBytes()).toBe(0);
  });

  it('the low-disk floor knob is independent and still defaults to 0', () => {
    expect(workspaceMinFreeDiskBytes()).toBe(0);
    vi.stubEnv(MIN_FREE_DISK_ENV, String(1024 * 1024));
    expect(workspaceMinFreeDiskBytes()).toBe(1024 * 1024);
  });

  it('reserveWorkspaceDisk holds the measured default when env is unset and disk suffices', () => {
    mockDfFreeKb(4 * 1024 * 1024); // 4 GiB free ≥ 2 GiB default reservation.
    const held = reserveWorkspaceDisk('/ws');
    expect(held).toBe(DEFAULT_AGENT_DISK_RESERVATION_BYTES);
    expect(getWorkspaceDiskReservation('/ws')).toBe(DEFAULT_AGENT_DISK_RESERVATION_BYTES);
  });

  it('reserveWorkspaceDisk rejects admission when free disk cannot hold the default', () => {
    mockDfFreeKb(1024); // 1 MiB free < 2 GiB default reservation.
    expect(() => reserveWorkspaceDisk('/ws')).toThrowError(DiskReservationError);
    expect(getWorkspaceDiskReservation('/ws')).toBeNull();
  });
});
