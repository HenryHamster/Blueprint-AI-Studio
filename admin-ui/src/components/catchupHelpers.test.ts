import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import {
  getCatchUpViewedKey,
  getLastCatchUpViewed,
  formatDuration,
  windowToHours,
  TIMEOUT_SUBTYPE_LABELS,
  EXTERNAL_API_SUBTYPE_LABELS,
  RATE_LIMIT_KIND_LABELS,
} from './catchupHelpers';

describe('getCatchUpViewedKey', () => {
  it('returns the base key when no email is provided', () => {
    expect(getCatchUpViewedKey()).toBe('blueprint-admin:lastCatchUpViewed');
    expect(getCatchUpViewedKey(undefined)).toBe('blueprint-admin:lastCatchUpViewed');
  });

  it('returns the base key when email is null', () => {
    expect(getCatchUpViewedKey(null)).toBe('blueprint-admin:lastCatchUpViewed');
  });

  it('returns a per-operator scoped key when email is provided', () => {
    expect(getCatchUpViewedKey('alice@example.com')).toBe('blueprint-admin:lastCatchUpViewed:alice@example.com');
  });

  it('scoped key differs from base key', () => {
    const base = getCatchUpViewedKey();
    const scoped = getCatchUpViewedKey('bob@example.com');
    expect(scoped).not.toBe(base);
  });
});

describe('getLastCatchUpViewed', () => {
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

  it('returns null for a different key when nothing written', () => {
    store['blueprint-admin:lastCatchUpViewed'] = String(Date.now());
    expect(getLastCatchUpViewed('alice@example.com')).toBeNull();
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

describe('windowToHours', () => {
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
