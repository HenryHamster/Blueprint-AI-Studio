import { describe, it, expect } from 'vitest';
import {
  formatRemainingMinutes,
  getStagedWaitingInfo,
  STAGED_STALE_DAYS,
  priorityColors,
  statusColors,
} from './directionHelpers';
import type { Direction } from '../components/DirectionsPanel';

function makeDirection(overrides: Partial<Direction> = {}): Direction {
  return {
    id: 'd-test',
    text: 'Test direction',
    priority: 'medium',
    added_at: '2026-01-01T00:00:00Z',
    status: 'active',
    ...overrides,
  };
}

describe('formatRemainingMinutes', () => {
  it('returns empty string for zero minutes', () => {
    expect(formatRemainingMinutes(0)).toBe('');
  });

  it('returns empty string for negative minutes', () => {
    expect(formatRemainingMinutes(-1)).toBe('');
    expect(formatRemainingMinutes(-100)).toBe('');
  });

  it('returns empty string for non-finite values', () => {
    expect(formatRemainingMinutes(Infinity)).toBe('');
    expect(formatRemainingMinutes(NaN)).toBe('');
  });

  it('formats sub-60 minutes with ceiling', () => {
    expect(formatRemainingMinutes(1)).toBe('1m left');
    expect(formatRemainingMinutes(30)).toBe('30m left');
    expect(formatRemainingMinutes(59)).toBe('59m left');
    // Fractional: ceil(0.5) = 1
    expect(formatRemainingMinutes(0.5)).toBe('1m left');
  });

  it('formats exactly 60 minutes as 1h left (no minutes component)', () => {
    expect(formatRemainingMinutes(60)).toBe('1h left');
  });

  it('formats hours + minutes when the minute remainder is non-zero', () => {
    expect(formatRemainingMinutes(90)).toBe('1h 30m left');
    expect(formatRemainingMinutes(121)).toBe('2h 1m left');
  });

  it('formats whole hours without the minute component', () => {
    expect(formatRemainingMinutes(120)).toBe('2h left');
    expect(formatRemainingMinutes(180)).toBe('3h left');
  });
});

describe('getStagedWaitingInfo', () => {
  it('returns null when staged_at is absent', () => {
    expect(getStagedWaitingInfo(makeDirection())).toBeNull();
  });

  it('returns null when staged_at is null', () => {
    expect(getStagedWaitingInfo(makeDirection({ staged_at: null }))).toBeNull();
  });

  it('returns label, days, and isStale for a recent staged_at', () => {
    const recentDate = new Date(Date.now() - 2 * 24 * 60 * 60 * 1000).toISOString();
    const result = getStagedWaitingInfo(makeDirection({ staged_at: recentDate }));
    expect(result).not.toBeNull();
    if (!result) throw new Error('expected result');
    expect(result.label).toMatch(/staged /);
    expect(result.days).toBeGreaterThanOrEqual(1);
    expect(result.days).toBeLessThan(STAGED_STALE_DAYS);
    expect(result.isStale).toBe(false);
  });

  it('marks a long-ago staged date as stale (>= STAGED_STALE_DAYS)', () => {
    // 20 days ago — clearly past the 14-day threshold
    const oldDate = new Date(Date.now() - 20 * 24 * 60 * 60 * 1000).toISOString();
    const result = getStagedWaitingInfo(makeDirection({ staged_at: oldDate }));
    expect(result).not.toBeNull();
    if (!result) throw new Error('expected result');
    expect(result.days).toBeGreaterThanOrEqual(STAGED_STALE_DAYS);
    expect(result.isStale).toBe(true);
  });

  it('counts days from the staged_at timestamp to now', () => {
    // Exactly 5 days ago
    const fiveDaysAgo = new Date(Date.now() - 5 * 24 * 60 * 60 * 1000).toISOString();
    const result = getStagedWaitingInfo(makeDirection({ staged_at: fiveDaysAgo }));
    expect(result).not.toBeNull();
    if (!result) throw new Error('expected result');
    // days should be 4 or 5 depending on sub-day rounding
    expect(result.days).toBeGreaterThanOrEqual(4);
    expect(result.days).toBeLessThanOrEqual(5);
  });

  it('treats a staged_at in the future as 0 days (no negative days)', () => {
    const futureDate = new Date(Date.now() + 24 * 60 * 60 * 1000).toISOString();
    const result = getStagedWaitingInfo(makeDirection({ staged_at: futureDate }));
    expect(result).not.toBeNull();
    if (!result) throw new Error('expected result');
    expect(result.days).toBe(0);
    expect(result.isStale).toBe(false);
  });
});

describe('STAGED_STALE_DAYS', () => {
  it('is a positive number below the backend alert threshold (~21d)', () => {
    expect(STAGED_STALE_DAYS).toBeGreaterThan(0);
    expect(STAGED_STALE_DAYS).toBeLessThan(21);
  });
});

describe('priorityColors', () => {
  it('has entries for high, medium, and low priorities', () => {
    expect(typeof priorityColors.high).toBe('string');
    expect(typeof priorityColors.medium).toBe('string');
    expect(typeof priorityColors.low).toBe('string');
  });

  it('each entry is a non-empty Tailwind class string', () => {
    for (const value of Object.values(priorityColors)) {
      expect(value.length).toBeGreaterThan(0);
    }
  });
});

describe('statusColors', () => {
  it('has entries for all documented direction statuses', () => {
    const expectedStatuses = ['active', 'completed', 'paused', 'proposed', 'expired', 'staged'];
    for (const status of expectedStatuses) {
      expect(typeof statusColors[status]).toBe('string');
      expect(statusColors[status].length).toBeGreaterThan(0);
    }
  });
});
