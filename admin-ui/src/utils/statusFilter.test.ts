import { describe, it, expect } from 'vitest';
import { matchesStatusFilter } from './statusFilter';
import { STATUS_OPTIONS } from '../constants';

const ALL_STATUSES = STATUS_OPTIONS.map(o => o.value);

describe('matchesStatusFilter', () => {
  it('matches a status that is explicitly selected', () => {
    expect(matchesStatusFilter('FAILED', ['FAILED', 'COMPLETED'])).toBe(true);
    expect(matchesStatusFilter('BLOCKED', ['BLOCKED'])).toBe(true);
  });

  it('hides a known status that is not selected (active filtering)', () => {
    expect(matchesStatusFilter('COMPLETED', ['FAILED'])).toBe(false);
    expect(matchesStatusFilter('BLOCKED', ['QUEUED', 'IN_PROGRESS'])).toBe(false);
  });

  it('hides an unknown status while the user is actively narrowing the view', () => {
    // A strict subset is selected → the user is filtering; an unknown status is
    // excluded along with everything else they did not pick.
    expect(matchesStatusFilter('SOME_FUTURE_STATUS', ['FAILED'], ALL_STATUSES)).toBe(false);
  });

  it('shows an unknown status when the filter is full (escape hatch)', () => {
    // No active filtering (all known statuses selected) → a status this UI build
    // predates fails OPEN instead of vanishing. This is the generalized fix that
    // stops the next backend-added TaskStatus from being invisible in every view.
    expect(matchesStatusFilter('SOME_FUTURE_STATUS', ALL_STATUSES, ALL_STATUSES)).toBe(true);
  });

  it('still shows every known status when the filter is full', () => {
    for (const status of ALL_STATUSES) {
      expect(matchesStatusFilter(status, ALL_STATUSES, ALL_STATUSES)).toBe(true);
    }
  });

  it('hides everything (known and unknown) when the selection is empty', () => {
    expect(matchesStatusFilter('FAILED', [], ALL_STATUSES)).toBe(false);
    expect(matchesStatusFilter('SOME_FUTURE_STATUS', [], ALL_STATUSES)).toBe(false);
  });

  it('defaults the known set to the STATUS_OPTIONS values', () => {
    // Calling without an explicit `known` argument must use STATUS_OPTIONS, so a
    // full live selection passes PARKED (a now-known status) and fails open for
    // a genuinely unknown one.
    expect(matchesStatusFilter('PARKED', ALL_STATUSES)).toBe(true);
    expect(matchesStatusFilter('SOME_FUTURE_STATUS', ALL_STATUSES)).toBe(true);
  });
});
