import { describe, expect, it } from 'vitest';

import {
  AUTONOMOUS_EXPLORATION_INTERVAL_MS,
  actionableFiredWatchRevision,
  buildAutonomousPrompt,
  firedWatchRevision,
  hasPendingOwnedWork,
  shouldRunAutonomousTurn,
  wakeBarrierRevision,
  type ResidentWakeBarrierReceipt,
  type StrategicWatchReceipt,
} from './strategic-resident-lib.js';

const baseGate = {
  currentWorkStateRev: 'semantic-1',
  lastTurnWorkStateRev: 'semantic-1',
  previousTurnAgendaContinue: false,
  followupTurnPending: false,
  operatorTurnJustCompleted: false,
  currentFiredWatchRev: '',
  lastTurnFiredWatchRev: '',
  ownedWorkPending: false,
  msSinceLastAutonomousTurn: 0,
  explorationIntervalMs: AUTONOMOUS_EXPLORATION_INTERVAL_MS,
};

describe('Resident semantic autonomous wake gate', () => {
  it('suppresses unchanged-state exploration while owned work is pending', () => {
    expect(shouldRunAutonomousTurn({
      ...baseGate,
      ownedWorkPending: true,
      msSinceLastAutonomousTurn: AUTONOMOUS_EXPLORATION_INTERVAL_MS * 10,
    })).toBe(false);
  });

  it('still wakes pending work for true decision boundaries', () => {
    expect(shouldRunAutonomousTurn({
      ...baseGate,
      ownedWorkPending: true,
      currentWorkStateRev: 'semantic-2',
    })).toBe(true);
    expect(shouldRunAutonomousTurn({
      ...baseGate,
      ownedWorkPending: true,
      operatorTurnJustCompleted: true,
    })).toBe(true);
    expect(shouldRunAutonomousTurn({
      ...baseGate,
      ownedWorkPending: true,
      previousTurnAgendaContinue: true,
    })).toBe(true);
    expect(shouldRunAutonomousTurn({
      ...baseGate,
      ownedWorkPending: true,
      followupTurnPending: true,
    })).toBe(true);
  });

  it('wakes immediately for a new watch and retries an unacked receipt on exploration', () => {
    expect(shouldRunAutonomousTurn({
      ...baseGate,
      ownedWorkPending: true,
      currentFiredWatchRev: 'watch-a:fired',
      lastTurnFiredWatchRev: '',
    })).toBe(true);
    expect(shouldRunAutonomousTurn({
      ...baseGate,
      ownedWorkPending: false,
      currentFiredWatchRev: 'watch-a:fired',
      lastTurnFiredWatchRev: 'watch-a:fired',
      msSinceLastAutonomousTurn: AUTONOMOUS_EXPLORATION_INTERVAL_MS - 1,
    })).toBe(false);
    expect(shouldRunAutonomousTurn({
      ...baseGate,
      ownedWorkPending: false,
      currentFiredWatchRev: 'watch-a:fired',
      lastTurnFiredWatchRev: 'watch-a:fired',
      msSinceLastAutonomousTurn: AUTONOMOUS_EXPLORATION_INTERVAL_MS,
    })).toBe(true);
    expect(shouldRunAutonomousTurn({
      ...baseGate,
      currentFiredWatchRev: '',
      lastTurnFiredWatchRev: 'watch-a:fired',
    })).toBe(false);
  });

  it('always permits the first orientation turn even when work is already pending', () => {
    expect(shouldRunAutonomousTurn({
      ...baseGate,
      ownedWorkPending: true,
      msSinceLastAutonomousTurn: Number.POSITIVE_INFINITY,
    })).toBe(true);
  });
});

describe('Resident watch and owned-work summaries', () => {
  it('fingerprints watch agenda identity independent of order or additive receipt fields', () => {
    const a: StrategicWatchReceipt = {
      id: 'watch-a',
      state: 'fired',
      receipt: { title: 'first' },
    };
    const b: StrategicWatchReceipt = {
      id: 'watch-b',
      state: 'expired',
      receipt: { reason: 'deadline' },
    };
    expect(firedWatchRevision([a, b])).toBe('watch-a:fired|watch-b:expired');
    expect(firedWatchRevision([
      { ...b, receipt: { reason: 'changed additive detail' } },
      { ...a, receipt: { title: 'changed additive detail' } },
    ])).toBe('watch-a:fired|watch-b:expired');
  });

  it('delivers exact semantic barrier receipts with stable one-shot identity', () => {
    const receipt: ResidentWakeBarrierReceipt = {
      id: 'barrier-release-v7',
      event_type: 'release.serving_confirmed',
      summary: 'Promoted v7 is serving abc123.',
      context: { served_sha: 'abc123', version_number: 7 },
    };
    expect(wakeBarrierRevision([receipt])).toBe('barrier-release-v7');
    const prompt = buildAutonomousPrompt(
      [], [], [], false, 3, '', false, [], [receipt],
    );
    expect(prompt).toContain('SEMANTIC OUTCOME RECEIPTS');
    expect(prompt).toContain('release.serving_confirmed');
    expect(prompt).toContain('served_sha');
  });

  it('coalesces successful child watches until the parallel frontier drains', () => {
    const completed: StrategicWatchReceipt = {
      id: 'watch-success',
      state: 'fired',
      terminal_status: 'COMPLETED',
    };
    const failed: StrategicWatchReceipt = {
      id: 'watch-failure',
      state: 'fired',
      terminal_status: 'FAILED',
    };
    const expired: StrategicWatchReceipt = {
      id: 'watch-expired',
      state: 'expired',
    };
    expect(actionableFiredWatchRevision([completed], { in_flight: 2 })).toBe('');
    expect(actionableFiredWatchRevision(
      [completed, failed, expired],
      { in_flight: 2 },
    )).toBe('watch-expired:expired|watch-failure:fired');
    expect(actionableFiredWatchRevision(
      [completed, failed, expired],
      { in_flight: 0 },
    )).toBe('watch-expired:expired|watch-failure:fired|watch-success:fired');
  });

  it('suppresses exploration only for active work or watches that have not fired', () => {
    expect(hasPendingOwnedWork({ in_flight: 2 })).toBe(true);
    expect(hasPendingOwnedWork({ pending_watches: 1 })).toBe(true);
    expect(hasPendingOwnedWork({ fired_watches: 1 })).toBe(false);
    expect(hasPendingOwnedWork({ pending_barriers: 1 })).toBe(false);
    expect(hasPendingOwnedWork({ in_flight: 0, pending_watches: 0 })).toBe(false);
  });

  it('lets an unchanged open release barrier retry on sparse exploration', () => {
    expect(shouldRunAutonomousTurn({
      ...baseGate,
      ownedWorkPending: hasPendingOwnedWork({ pending_barriers: 1 }),
      msSinceLastAutonomousTurn: AUTONOMOUS_EXPLORATION_INTERVAL_MS,
    })).toBe(true);
  });
});
