import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import {
  getCatchUpViewedKey,
  getLastCatchUpViewed,
  markCatchUpViewed,
  formatDuration,
  windowToHours,
  resolveLastCatchUpViewed,
} from './catchupHelpers';

describe('getCatchUpViewedKey', () => {
  it('returns the global key when no userEmail is provided', () => {
    expect(getCatchUpViewedKey()).toBe('blueprint-admin:lastCatchUpViewed');
    expect(getCatchUpViewedKey(null)).toBe('blueprint-admin:lastCatchUpViewed');
    expect(getCatchUpViewedKey(undefined)).toBe('blueprint-admin:lastCatchUpViewed');
  });

  it('returns a per-operator key when userEmail is provided', () => {
    expect(getCatchUpViewedKey('alice@example.com')).toBe('blueprint-admin:lastCatchUpViewed:alice@example.com');
    expect(getCatchUpViewedKey('bob@example.com')).toBe('blueprint-admin:lastCatchUpViewed:bob@example.com');
  });

  it('scopes different operators to different keys (cross-device isolation)', () => {
    const keyA = getCatchUpViewedKey('alice@example.com');
    const keyB = getCatchUpViewedKey('bob@example.com');
    expect(keyA).not.toBe(keyB);
  });
});

describe('getLastCatchUpViewed / markCatchUpViewed', () => {
  let store: Record<string, string>;

  beforeEach(() => {
    store = {};
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation((k: string) => store[k] ?? null);
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation((k: string, v: string) => { store[k] = v; });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('returns null when no timestamp is stored', () => {
    expect(getLastCatchUpViewed()).toBeNull();
    expect(getLastCatchUpViewed('alice@example.com')).toBeNull();
  });

  it('stores and retrieves a per-operator timestamp independently of global key', () => {
    markCatchUpViewed('alice@example.com');
    expect(getLastCatchUpViewed('alice@example.com')).toBeGreaterThan(0);
    // Global key should be untouched
    expect(getLastCatchUpViewed()).toBeNull();
  });

  it('different operators have independent timestamps', () => {
    const t1 = Date.now();
    markCatchUpViewed('alice@example.com');
    const t2 = Date.now() + 1000;
    vi.spyOn(Date, 'now').mockReturnValue(t2);
    markCatchUpViewed('bob@example.com');

    const tsAlice = getLastCatchUpViewed('alice@example.com');
    const tsBob = getLastCatchUpViewed('bob@example.com');
    expect(tsAlice).toBeGreaterThanOrEqual(t1);
    expect(tsBob).toBe(t2);
    expect(tsAlice).not.toBe(tsBob);
  });

  it('global key is unaffected when per-operator key is written', () => {
    markCatchUpViewed('alice@example.com');
    expect(getLastCatchUpViewed(null)).toBeNull();
    expect(getLastCatchUpViewed(undefined)).toBeNull();
  });

  it('falls back gracefully when localStorage throws', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('storage unavailable'); });
    expect(getLastCatchUpViewed('alice@example.com')).toBeNull();
  });
});

describe('formatDuration', () => {
  it('formats seconds', () => {
    expect(formatDuration(45)).toBe('45s');
  });

  it('formats minutes', () => {
    expect(formatDuration(90)).toBe('2m');
  });

  it('formats hours with one decimal for short hours', () => {
    expect(formatDuration(5400)).toBe('1.5h');
  });

  it('formats hours rounded for long durations', () => {
    expect(formatDuration(72000)).toBe('20h');
  });
});

describe('resolveLastCatchUpViewed', () => {
  it('returns null when both timestamps are null', () => {
    expect(resolveLastCatchUpViewed(null, null)).toBeNull();
  });

  it('returns backend timestamp when local is null', () => {
    expect(resolveLastCatchUpViewed(null, 1000)).toBe(1000);
  });

  it('returns local timestamp when backend is null', () => {
    expect(resolveLastCatchUpViewed(1000, null)).toBe(1000);
  });

  it('prefers backend when backend is newer (cross-device)', () => {
    expect(resolveLastCatchUpViewed(1000, 2000)).toBe(2000);
  });

  it('returns local when local is newer', () => {
    expect(resolveLastCatchUpViewed(2000, 1000)).toBe(2000);
  });

  it('returns either when timestamps are equal', () => {
    expect(resolveLastCatchUpViewed(1000, 1000)).toBe(1000);
  });
});

describe('windowToHours', () => {
  // Lock the clock: the implementation calls Date.now() internally, and the
  // 'recent timestamp' test computes `Date.now() - 2h` then expects ceil to
  // round to 2. Without a fake clock even 1ms of wall-time drift between the
  // two Date.now() calls makes elapsed = 2.0000…n h → Math.ceil = 3, so the
  // test only passed when both calls landed in the same ms (fast laptop) and
  // flaked on CI (run 26354374578 — "expected 3 to be 2").
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-04-24T12:00:00Z'));
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('returns 24 for last_visit when lastViewed is null', () => {
    expect(windowToHours('last_visit', null)).toBe(24);
  });

  it('returns elapsed hours for last_visit with a recent timestamp', () => {
    const twoHoursAgo = Date.now() - 2 * 60 * 60 * 1000;
    expect(windowToHours('last_visit', twoHoursAgo)).toBe(2);
  });

  it('returns fixed values for named windows', () => {
    expect(windowToHours('6h', null)).toBe(6);
    expect(windowToHours('24h', null)).toBe(24);
    expect(windowToHours('7d', null)).toBe(168);
  });
});
