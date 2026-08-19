import { describe, it, expect } from 'vitest';
import {
  isHeartbeatTransient,
  isHeartbeatAuthRetryable,
  parseHeartbeatStatus,
  TRANSIENT_HEARTBEAT_STATUSES,
  AUTH_RETRY_HEARTBEAT_STATUSES,
} from './heartbeat-classify.js';

describe('parseHeartbeatStatus', () => {
  it('extracts the status from the real agent error format', () => {
    expect(
      parseHeartbeatStatus('POST /api/agent/heartbeat failed (429): Rate exceeded.')
    ).toBe(429);
  });

  it('returns null when no status is present', () => {
    expect(parseHeartbeatStatus('network error: ECONNRESET')).toBeNull();
    expect(parseHeartbeatStatus('')).toBeNull();
  });
});

describe('isHeartbeatTransient', () => {
  // The regression: a transient 429 must NOT trigger self-termination.
  it('treats 429 (rate-limited) as transient', () => {
    expect(
      isHeartbeatTransient('POST /api/agent/heartbeat failed (429): Rate exceeded.')
    ).toBe(true);
  });

  it('treats 409 / 408 / 425 as transient', () => {
    for (const code of [408, 409, 425]) {
      expect(isHeartbeatTransient(`POST /api/agent/heartbeat failed (${code}): x`)).toBe(
        true
      );
    }
  });

  it('does NOT treat hard 4xx (reassignment/auth) as transient → self-terminate', () => {
    for (const code of [400, 401, 403, 404, 410]) {
      expect(isHeartbeatTransient(`POST /api/agent/heartbeat failed (${code}): x`)).toBe(
        false
      );
    }
  });

  it('does NOT treat 5xx or status-less errors as transient (this branch only gates 4xx self-terminate)', () => {
    expect(isHeartbeatTransient('failed (503): upstream')).toBe(false);
    expect(isHeartbeatTransient('ECONNRESET')).toBe(false);
  });

  it('TRANSIENT set is exactly {408,409,425,429}', () => {
    expect([...TRANSIENT_HEARTBEAT_STATUSES].sort((a, b) => a - b)).toEqual([
      408, 409, 425, 429,
    ]);
  });
});

describe('isHeartbeatAuthRetryable', () => {
  // Kill-path regression (NeurIPS task 38b16082, 2026-08-18): 401/403 were
  // treated as instantly fatal with zero credential-refresh retry, orphaning
  // a pushed commit. These must be classified as worth a retry cascade.
  it('treats 401 and 403 as auth-retryable', () => {
    for (const code of [401, 403]) {
      expect(
        isHeartbeatAuthRetryable(`POST /api/agent/heartbeat failed (${code}): x`)
      ).toBe(true);
    }
  });

  it('does NOT treat genuinely-gone / malformed 4xx as auth-retryable', () => {
    for (const code of [400, 404, 409, 410, 422]) {
      expect(
        isHeartbeatAuthRetryable(`POST /api/agent/heartbeat failed (${code}): x`)
      ).toBe(false);
    }
  });

  it('does NOT treat the transient set as auth-retryable (disjoint from TRANSIENT)', () => {
    for (const code of [408, 409, 425, 429]) {
      expect(
        isHeartbeatAuthRetryable(`POST /api/agent/heartbeat failed (${code}): x`)
      ).toBe(false);
    }
  });

  it('does NOT treat 5xx or status-less errors as auth-retryable', () => {
    expect(isHeartbeatAuthRetryable('failed (503): upstream')).toBe(false);
    expect(isHeartbeatAuthRetryable('ECONNRESET')).toBe(false);
  });

  it('AUTH_RETRY set is exactly {401,403}', () => {
    expect([...AUTH_RETRY_HEARTBEAT_STATUSES].sort((a, b) => a - b)).toEqual([
      401, 403,
    ]);
  });

  it('AUTH_RETRY and TRANSIENT sets are disjoint', () => {
    const overlap = [...AUTH_RETRY_HEARTBEAT_STATUSES].filter((s) =>
      TRANSIENT_HEARTBEAT_STATUSES.has(s)
    );
    expect(overlap).toEqual([]);
  });
});
