import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { expectCalledWith } from './typed-mock-call.js';

// Mock the two token-persistence modules the GitHub-token refresh callback
// touches so the refresh path can be exercised without writing real files or
// shelling out to git. Only the symbols agent-token.ts imports need to exist.
vi.mock('../token-file.js', () => ({
  writeGithubTokenFile: vi.fn(),
  jsonFieldShellFn: vi.fn(() => ''),
}));
vi.mock('../git-credential.js', () => ({
  writeGitTokenFile: vi.fn(),
  setupGitCredentialHelper: vi.fn(),
}));

import {
  computeRefreshDelayMs, isBootstrapToken,
  initTokenModule,
  scheduleProactiveTokenRefresh, stopProactiveTokenRefresh,
  scheduleGithubTokenRefresh, stopGithubTokenRefresh,
} from './agent-token.js';
import * as agentTokenModule from './agent-token.js';
import { writeGithubTokenFile } from '../token-file.js';
import { writeGitTokenFile, setupGitCredentialHelper } from '../git-credential.js';
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

// ── scheduleProactiveTokenRefresh / stopProactiveTokenRefresh ─────────────────

describe('scheduleProactiveTokenRefresh / stopProactiveTokenRefresh lifecycle', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    initTokenModule({
      getOrchestratorUrl: () => 'http://test-orchestrator',
      getHeaders: () => ({ 'X-Agent-Token': 'test-token' }),
      getToken: () => 'test-token',
      setToken: () => {},
      log: () => {},
    });
    stopProactiveTokenRefresh();
  });
  afterEach(() => {
    stopProactiveTokenRefresh();
    vi.useRealTimers();
  });

  it('calling scheduleProactiveTokenRefresh twice replaces the previous timer (no orphan)', () => {
    const now = 1_000_000_000_000;
    const expSec1 = Math.floor(now / 1000) + 3600;
    const expSec2 = Math.floor(now / 1000) + 7200;

    scheduleProactiveTokenRefresh(mockJwtWithExp(expSec1));
    const first = agentTokenModule.tokenRefreshTimer;
    scheduleProactiveTokenRefresh(mockJwtWithExp(expSec2));
    const second = agentTokenModule.tokenRefreshTimer;
    // The second call must replace the first — no orphaned timers.
    expect(second).not.toBeNull();
    expect(second).not.toBe(first);
  });

  it('stopProactiveTokenRefresh is a no-op when no timer is running', () => {
    expect(agentTokenModule.tokenRefreshTimer).toBeNull();
    expect(() => { stopProactiveTokenRefresh(); }).not.toThrow();
    expect(agentTokenModule.tokenRefreshTimer).toBeNull();
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

// ── GitHub token refresh callback: credential-exposure hardening ──────────────
// Finding 11: the refresh must NOT re-embed the installation token in the git
// remote URL (which persists it in .git/config and briefly in the git argv). It
// keeps the origin URL clean and delegates auth to the credential helper.
describe('scheduleGithubTokenRefresh refresh callback (no token in remote URL)', () => {
  // The local const in agent-token.ts (not exported); mirrored here.
  const GITHUB_TOKEN_REFRESH_MS = 45 * 60 * 1000;
  const FRESH_TOKEN = 'ghs_fresh_write_token';

  beforeEach(() => {
    vi.useFakeTimers();
    initTokenModule({
      getOrchestratorUrl: () => 'http://test-orchestrator',
      getHeaders: () => ({ 'X-Agent-Token': 'test-token' }),
      getToken: () => 'test-token',
      setToken: () => {},
      log: () => {},
    });
    stopGithubTokenRefresh();
    vi.mocked(writeGithubTokenFile).mockReset();
    vi.mocked(writeGitTokenFile).mockReset();
    vi.mocked(setupGitCredentialHelper).mockReset();
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ data: { token: FRESH_TOKEN } }),
    })));
  });
  afterEach(() => {
    stopGithubTokenRefresh();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it('write task: refreshes token file + credential helper, never embeds token in a remote URL', async () => {
    scheduleGithubTokenRefresh('task-rw', '/tmp/ws', 'owner', 'repo', false);
    await vi.advanceTimersByTimeAsync(GITHUB_TOKEN_REFRESH_MS);
    stopGithubTokenRefresh(); // cancel the self-reschedule before assertions

    // Fresh token mirrored to both on-disk token files (helper reads the git one).
    expectCalledWith(writeGithubTokenFile, FRESH_TOKEN);
    expectCalledWith(writeGitTokenFile, FRESH_TOKEN);
    // Auth delegated to the credential helper against a CLEAN origin URL — the
    // token is never written into .git/config nor passed on the git command line.
    expectCalledWith(setupGitCredentialHelper, 'owner', 'repo', '/tmp/ws');
    // Regression guard: the raw token must never appear in any git-remote argument.
    expect(vi.mocked(setupGitCredentialHelper).mock.calls.flat().join(' ')).not.toContain(FRESH_TOKEN);
  });

  it('read-only task: refreshes all token files (same as write task — stale git creds break push too)', async () => {
    scheduleGithubTokenRefresh('task-ro', '/tmp/ws', 'owner', 'repo', true);
    await vi.advanceTimersByTimeAsync(GITHUB_TOKEN_REFRESH_MS);
    stopGithubTokenRefresh();

    expectCalledWith(writeGithubTokenFile, FRESH_TOKEN);
    expectCalledWith(writeGitTokenFile, FRESH_TOKEN);
    expectCalledWith(setupGitCredentialHelper, 'owner', 'repo', '/tmp/ws');
    expect(vi.mocked(setupGitCredentialHelper).mock.calls.flat().join(' ')).not.toContain(FRESH_TOKEN);
  });

  it('stale generation refresh does not re-arm after teardown', async () => {
    // Simulate the race the generation-stamp guard exists for: a refresh
    // callback is mid-flight (fetch not yet resolved) when the task tears
    // down / a warm-VM rotation calls stopGithubTokenRefresh(). The in-flight
    // callback must NOT reschedule itself once it resumes, because its
    // captured generation is now stale.
    // The fetch mock fires teardown (stopGithubTokenRefresh) the instant the
    // in-flight refresh dispatches its request — mirroring a task teardown /
    // warm-VM rotation landing while the callback is still awaiting the
    // network response, before it gets to its trailing reschedule check.
    vi.stubGlobal('fetch', vi.fn(() => {
      stopGithubTokenRefresh();
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ data: { token: FRESH_TOKEN } }) });
    }));

    const logSpy = vi.fn();
    initTokenModule({
      getOrchestratorUrl: () => 'http://test-orchestrator',
      getHeaders: () => ({ 'X-Agent-Token': 'test-token' }),
      getToken: () => 'test-token',
      setToken: () => {},
      log: logSpy,
    });

    scheduleGithubTokenRefresh('task-stale', '/tmp/ws', 'owner', 'repo', false);
    await vi.advanceTimersByTimeAsync(GITHUB_TOKEN_REFRESH_MS);

    expect(vi.mocked(fetch)).toHaveBeenCalledTimes(1);
    expectCalledWith(logSpy, 'GitHub token refresh: generation superseded during refresh — not re-arming');

    // The stale-generation callback must NOT have re-armed a new timer.
    expect(agentTokenModule.githubTokenRefreshTimer).toBeNull();
  });
});
