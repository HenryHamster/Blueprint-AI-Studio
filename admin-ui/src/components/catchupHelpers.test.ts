import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import {
  getCatchUpViewedKey,
  getLastCatchUpViewed,
  markCatchUpViewed,
  formatDuration,
  windowToHours,
  resolveLastCatchUpViewed,
  computeClassTrend,
  TIMEOUT_SUBTYPE_LABELS,
  EXTERNAL_API_SUBTYPE_LABELS,
  RATE_LIMIT_KIND_LABELS,
} from './catchupHelpers';
import type { TrendPeriod } from './catchupHelpers';

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

function makePeriod(failed_by_class: Record<string, number>): TrendPeriod {
  return { period_end: '2026-01-01', success_rate: null, completed: 0, failed: 0, failed_by_class };
}

describe('computeClassTrend', () => {
  it('returns neutral arrow when fewer than 4 periods', () => {
    const history = [makePeriod({ CODE_ERROR: 1 }), makePeriod({ CODE_ERROR: 2 })];
    expect(computeClassTrend(history, 'CODE_ERROR')).toEqual({ arrow: '–', color: 'text-neutral-500' });
  });

  it('returns up arrow when recent 2 periods exceed prior 2 periods', () => {
    const history = [
      makePeriod({ CODE_ERROR: 1 }),
      makePeriod({ CODE_ERROR: 1 }),
      makePeriod({ CODE_ERROR: 3 }),
      makePeriod({ CODE_ERROR: 3 }),
    ];
    expect(computeClassTrend(history, 'CODE_ERROR')).toEqual({ arrow: '↑', color: 'text-red-400' });
  });

  it('returns down arrow when recent 2 periods are below prior 2 periods', () => {
    const history = [
      makePeriod({ CODE_ERROR: 4 }),
      makePeriod({ CODE_ERROR: 4 }),
      makePeriod({ CODE_ERROR: 1 }),
      makePeriod({ CODE_ERROR: 1 }),
    ];
    expect(computeClassTrend(history, 'CODE_ERROR')).toEqual({ arrow: '↓', color: 'text-emerald-400' });
  });

  it('returns neutral arrow when recent and prior counts are equal', () => {
    const history = [
      makePeriod({ CODE_ERROR: 2 }),
      makePeriod({ CODE_ERROR: 2 }),
      makePeriod({ CODE_ERROR: 2 }),
      makePeriod({ CODE_ERROR: 2 }),
    ];
    expect(computeClassTrend(history, 'CODE_ERROR')).toEqual({ arrow: '–', color: 'text-neutral-500' });
  });

  it('returns neutral when failure class is absent from all periods', () => {
    const history = [
      makePeriod({ RATE_LIMIT: 5 }),
      makePeriod({ RATE_LIMIT: 5 }),
      makePeriod({ RATE_LIMIT: 5 }),
      makePeriod({ RATE_LIMIT: 5 }),
    ];
    expect(computeClassTrend(history, 'CODE_ERROR')).toEqual({ arrow: '–', color: 'text-neutral-500' });
  });

  it('uses only the last 4 periods when history is longer', () => {
    const history = [
      makePeriod({ CODE_ERROR: 100 }),
      makePeriod({ CODE_ERROR: 100 }),
      makePeriod({ CODE_ERROR: 1 }),
      makePeriod({ CODE_ERROR: 1 }),
      makePeriod({ CODE_ERROR: 3 }),
      makePeriod({ CODE_ERROR: 3 }),
    ];
    // Last 4: [1,1,3,3] → prior=[1,1]=2, recent=[3,3]=6 → up
    expect(computeClassTrend(history, 'CODE_ERROR')).toEqual({ arrow: '↑', color: 'text-red-400' });
  });
});

describe('failure subtype label maps', () => {
  it('EXTERNAL_API_SUBTYPE_LABELS covers the backend EXTERNAL_API_SUBTYPE_* values', () => {
    expect(EXTERNAL_API_SUBTYPE_LABELS.provider_auth).toBe('provider auth');
    expect(EXTERNAL_API_SUBTYPE_LABELS.provider_quota).toBe('provider quota');
    expect(EXTERNAL_API_SUBTYPE_LABELS.provider_model_config).toBe('model config');
    expect(EXTERNAL_API_SUBTYPE_LABELS.github_pr_create).toBe('GitHub PR');
    expect(EXTERNAL_API_SUBTYPE_LABELS.db_connectivity).toBe('DB connectivity');
    expect(EXTERNAL_API_SUBTYPE_LABELS.network_transient).toBe('network blip');
    expect(EXTERNAL_API_SUBTYPE_LABELS.other).toBe('other');
  });

  it('RATE_LIMIT_KIND_LABELS covers the persisted rate_limit_kind values', () => {
    expect(RATE_LIMIT_KIND_LABELS.rate_limit).toBe('rate limit');
    expect(RATE_LIMIT_KIND_LABELS.quota_exhausted).toBe('quota exhausted');
    expect(RATE_LIMIT_KIND_LABELS.unknown).toBe('unknown');
  });

  it('TIMEOUT_SUBTYPE_LABELS still exposes the OOM and cascade keys consumed by the dashboard', () => {
    expect(TIMEOUT_SUBTYPE_LABELS.oom).toBe('OOM');
    expect(TIMEOUT_SUBTYPE_LABELS.cascade_cancellation).toBe('cascade');
  });
});
