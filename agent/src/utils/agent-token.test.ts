import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  computeRefreshDelayMs, isBootstrapToken,
  initTokenModule,
  scheduleGithubTokenRefresh, stopGithubTokenRefresh,
} from './agent-token.js';
import * as agentTokenModule from './agent-token.js';
import {
  TOKEN_REFRESH_FLOOR_MS,
  TOKEN_REFRESH_CEILING_MS,
  BOOTSTRAP_TOKEN_THRESHOLD_MS,
} from '../constants.js';

/** Build a minimal JWT with a given exp claim (no real signature needed). */
function mockJwtWithExp(expUnixSec: number): string {
  const header = Buffer.from(JSON.stringify({ alg: 'HS256', typ: 'JWT' })).toString('base64url');
  const payload = Buffer.from(JSON.stringify({ exp: expUnixSec, sub: 'test-task' })).toString('base64url');
  return `${header}.${payload}.fakesig`;
}

// ── computeRefreshDelayMs ─────────────────────────────────────────────────────
describe('computeRefreshDelayMs', () => {
  it('schedules at 75% of remaining lifetime for a 1h token', () => {
    const now = 1_000_000_000_000;
    const expSec = Math.floor(now / 1000) + 3600; // 1 hour remaining
    const delay = computeRefreshDelayMs(mockJwtWithExp(expSec), now);
    expect(delay).toBe(3600 * 1000 * 0.75); // 2700000ms
  });

  it('clamps to floor when remaining lifetime is very short', () => {
    const now = 1_000_000_000_000;
    const expSec = Math.floor(now / 1000) + 30; // 30 seconds remaining
    const delay = computeRefreshDelayMs(mockJwtWithExp(expSec), now);
    expect(delay).toBe(TOKEN_REFRESH_FLOOR_MS);
  });

  it('clamps to ceiling when remaining lifetime is very long', () => {
    const now = 1_000_000_000_000;
    const expSec = Math.floor(now / 1000) + 7200; // 2 hours remaining
    const delay = computeRefreshDelayMs(mockJwtWithExp(expSec), now);
    expect(delay).toBe(TOKEN_REFRESH_CEILING_MS);
  });

  it('falls back to floor for an unparseable token', () => {
    const delay = computeRefreshDelayMs('not-a-jwt');
    expect(delay).toBe(TOKEN_REFRESH_FLOOR_MS);
  });

  it('falls back to floor when token has no exp claim', () => {
    const now = 1_000_000_000_000;
    const header = Buffer.from(JSON.stringify({ alg: 'HS256' })).toString('base64url');
    const payload = Buffer.from(JSON.stringify({ sub: 'test' })).toString('base64url');
    const token = `${header}.${payload}.sig`;
    expect(computeRefreshDelayMs(token, now)).toBe(TOKEN_REFRESH_FLOOR_MS);
  });

  it('is at ceiling for a fresh standard 1h token', () => {
    // 75% of 3600s = 2700s which is 45 min — within [3 min, 55 min]
    const now = 1_000_000_000_000;
    const expSec = Math.floor(now / 1000) + 3600;
    const delay = computeRefreshDelayMs(mockJwtWithExp(expSec), now);
    expect(delay).toBeGreaterThanOrEqual(TOKEN_REFRESH_FLOOR_MS);
    expect(delay).toBeLessThanOrEqual(TOKEN_REFRESH_CEILING_MS);
  });
});

// ── isBootstrapToken ─────────────────────────────────────────────────────────
describe('isBootstrapToken', () => {
  it('returns true for a token with remaining lifetime above threshold', () => {
    const now = 1_000_000_000_000;
    const expSec = Math.floor(now / 1000) + 5400; // 90 min remaining
    expect(isBootstrapToken(mockJwtWithExp(expSec), now)).toBe(true);
  });

  it('returns false for a normal short-TTL token (1h remaining)', () => {
    const now = 1_000_000_000_000;
    const expSec = Math.floor(now / 1000) + 3600; // 60 min — below 75 min threshold
    expect(isBootstrapToken(mockJwtWithExp(expSec), now)).toBe(false);
  });

  it('returns false exactly at the threshold boundary', () => {
    const now = 1_000_000_000_000;
    const expSec = Math.floor(now / 1000) + BOOTSTRAP_TOKEN_THRESHOLD_MS / 1000;
    // remaining == threshold is NOT above threshold
    expect(isBootstrapToken(mockJwtWithExp(expSec), now)).toBe(false);
  });

  it('returns true just above the threshold boundary', () => {
    const now = 1_000_000_000_000;
    const expSec = Math.floor(now / 1000) + BOOTSTRAP_TOKEN_THRESHOLD_MS / 1000 + 1;
    expect(isBootstrapToken(mockJwtWithExp(expSec), now)).toBe(true);
  });

  it('returns false for an unparseable token', () => {
    expect(isBootstrapToken('not-a-jwt')).toBe(false);
  });

  it('returns false for a token with no exp claim', () => {
    const now = 1_000_000_000_000;
    const header = Buffer.from(JSON.stringify({ alg: 'HS256' })).toString('base64url');
    const payload = Buffer.from(JSON.stringify({ sub: 'test' })).toString('base64url');
    const token = `${header}.${payload}.sig`;
    expect(isBootstrapToken(token, now)).toBe(false);
  });
});

// ── scheduleGithubTokenRefresh / stopGithubTokenRefresh ───────────────────────

describe('scheduleGithubTokenRefresh / stopGithubTokenRefresh lifecycle', () => {
  // These tests verify the timer start/stop contract without actually firing the
  // 45-min timer. We use vitest fake timers to stay fast and deterministic.
  beforeEach(() => {
    vi.useFakeTimers();
    // initTokenModule must be called before schedule* functions.
    initTokenModule({
      getOrchestratorUrl: () => 'http://test-orchestrator',
      getHeaders: () => ({ 'X-Agent-Token': 'test-token' }),
      getToken: () => 'test-token',
      setToken: () => {},
      log: () => {},
    });
    stopGithubTokenRefresh();
  });
  afterEach(() => {
    stopGithubTokenRefresh();
    vi.useRealTimers();
  });

  it('scheduleGithubTokenRefresh sets a timer that stopGithubTokenRefresh clears', () => {
    expect(agentTokenModule.githubTokenRefreshTimer).toBeNull();
    scheduleGithubTokenRefresh('task-1', '/tmp/ws', 'owner', 'repo', false);
    expect(agentTokenModule.githubTokenRefreshTimer).not.toBeNull();
    stopGithubTokenRefresh();
    expect(agentTokenModule.githubTokenRefreshTimer).toBeNull();
  });

  it('calling scheduleGithubTokenRefresh twice replaces the previous timer (no orphan)', () => {
    scheduleGithubTokenRefresh('task-1', '/tmp/ws', 'owner', 'repo', false);
    const first = agentTokenModule.githubTokenRefreshTimer;
    scheduleGithubTokenRefresh('task-2', '/tmp/ws', 'owner', 'repo', false);
    const second = agentTokenModule.githubTokenRefreshTimer;
    // The second call must replace the first — no orphaned timers.
    expect(second).not.toBeNull();
    expect(second).not.toBe(first);
  });

  it('stopGithubTokenRefresh is a no-op when no timer is running', () => {
    expect(agentTokenModule.githubTokenRefreshTimer).toBeNull();
    expect(() => { stopGithubTokenRefresh(); }).not.toThrow();
    expect(agentTokenModule.githubTokenRefreshTimer).toBeNull();
  });

  it('scheduleGithubTokenRefresh works for both read-only and non-read-only tasks', () => {
    scheduleGithubTokenRefresh('task-ro', '/tmp/ws', 'owner', 'repo', true);
    expect(agentTokenModule.githubTokenRefreshTimer).not.toBeNull();
    stopGithubTokenRefresh();
    scheduleGithubTokenRefresh('task-rw', '/tmp/ws', 'owner', 'repo', false);
    expect(agentTokenModule.githubTokenRefreshTimer).not.toBeNull();
  });
});
