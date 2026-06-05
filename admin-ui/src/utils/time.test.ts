import { describe, it, expect } from 'vitest';
import { snapshotFreshness } from './time';

const STALE_AFTER = 120;

describe('snapshotFreshness', () => {
  it('returns no label and not stale when no timestamp/age present', () => {
    expect(snapshotFreshness(null, null, STALE_AFTER)).toEqual({ asOfLabel: null, stale: false });
    expect(snapshotFreshness(undefined, undefined, STALE_AFTER)).toEqual({ asOfLabel: null, stale: false });
  });

  it('emits a relative as-of label when a timestamp is present', () => {
    const at = new Date(Date.now() - 30_000).toISOString();
    expect(snapshotFreshness(at, 30, STALE_AFTER).asOfLabel).toMatch(/ago|just now/);
  });

  it('is fresh when age equals the stale threshold', () => {
    expect(snapshotFreshness('2026-06-05T00:00:00', STALE_AFTER, STALE_AFTER).stale).toBe(false);
  });

  it('marks stale once age exceeds the threshold', () => {
    expect(snapshotFreshness('2026-06-05T00:00:00', STALE_AFTER + 1, STALE_AFTER).stale).toBe(true);
  });

  it('does not mark stale when a timestamp is present but age is null', () => {
    const r = snapshotFreshness('2026-06-05T00:00:00', null, STALE_AFTER);
    expect(r.asOfLabel).toMatch(/ago|just now/);
    expect(r.stale).toBe(false);
  });
});
