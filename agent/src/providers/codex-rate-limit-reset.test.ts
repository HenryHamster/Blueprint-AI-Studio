import { EventEmitter } from 'node:events';
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  rmdirSync,
  utimesSync,
  writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { PassThrough } from 'node:stream';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('node:child_process', () => ({ spawn: vi.fn() }));

import { spawn } from 'node:child_process';
import {
  codexOAuthAccountIdentity,
  codexOAuthProbeEnv,
  codexWeeklyResetIdempotencyKey,
  inspectCodexWeeklyLimit,
  maybeConsumeCodexWeeklyReset,
  removeOwnedResetLockDirectory,
} from './codex-rate-limit-reset.js';

interface TestResetCredit {
  id: string;
  resetType: string;
  status: string;
  expiresAt: number;
}

function rateLimitResponse(usedPercent: number, credits: TestResetCredit[] | null = []) {
  return {
    rateLimits: {
      limitId: 'codex',
      primary: { usedPercent, windowDurationMins: 10_080, resetsAt: 2_000_000_000 },
      secondary: { usedPercent: 100, windowDurationMins: 300, resetsAt: 2_000_000_000 },
    },
    rateLimitResetCredits: {
      availableCount: credits === null ? 1 : credits.length,
      credits,
    },
  };
}

function fakeAppServer(
  response: ReturnType<typeof rateLimitResponse>,
  resetOutcome = 'reset',
  autoClose = true,
) {
  const child = new EventEmitter() as EventEmitter & {
    stdin: PassThrough;
    stdout: PassThrough;
    stderr: PassThrough;
    kill: ReturnType<typeof vi.fn>;
  };
  child.stdin = new PassThrough();
  child.stdout = new PassThrough();
  child.stderr = new PassThrough();
  child.kill = vi.fn();
  let closed = false;
  const close = () => {
    if (closed) return;
    closed = true;
    queueMicrotask(() => child.emit('close', 0));
  };
  child.stdin.on('finish', () => { if (autoClose) close(); });
  child.kill.mockImplementation(() => { close(); return true; });
  const requests: Array<Record<string, unknown>> = [];
  let input = '';
  child.stdin.on('data', chunk => {
    input += chunk.toString('utf8');
    for (;;) {
      const newline = input.indexOf('\n');
      if (newline < 0) break;
      const raw = input.slice(0, newline);
      input = input.slice(newline + 1);
      if (!raw) continue;
      const request = JSON.parse(raw) as Record<string, unknown>;
      requests.push(request);
      if (request.id === 1) {
        queueMicrotask(() => child.stdout.write(`${JSON.stringify({ id: 1, result: {} })}\n`));
      } else if (request.id === 2) {
        const reply = request.params === null
          ? { id: 2, result: response }
          : { id: 2, error: { message: 'params must be null' } };
        queueMicrotask(() => child.stdout.write(`${JSON.stringify(reply)}\n`));
      } else if (request.id === 3) {
        queueMicrotask(() => child.stdout.write(`${JSON.stringify({ id: 3, result: { outcome: resetOutcome } })}\n`));
      }
    }
  });
  return { child, requests, close };
}

describe('inspectCodexWeeklyLimit', () => {
  it('uses the seven-day window rather than an exhausted five-hour window', () => {
    const state = inspectCodexWeeklyLimit(rateLimitResponse(42));
    expect(state.exhausted).toBe(false);
    expect(state.usedPercent).toBe(42);
  });

  it('does not treat a longer monthly window as the weekly window', () => {
    const response = rateLimitResponse(25);
    response.rateLimits.primary = {
      usedPercent: 100,
      windowDurationMins: 30 * 24 * 60,
      resetsAt: 2_000_000_000,
    };
    response.rateLimits.secondary = {
      usedPercent: 25,
      windowDurationMins: 10_080,
      resetsAt: 2_000_000_000,
    };
    const state = inspectCodexWeeklyLimit(response);
    expect(state.exhausted).toBe(false);
    expect(state.usedPercent).toBe(25);
  });

  it('selects one exact, unexpired Codex reset credit', () => {
    const state = inspectCodexWeeklyLimit(rateLimitResponse(100, [
      { id: 'expired', resetType: 'codexRateLimits', status: 'available', expiresAt: 50 },
      { id: 'credit-1', resetType: 'codexRateLimits', status: 'available', expiresAt: 500 },
    ]), 100);
    expect(state).toMatchObject({
      exhausted: true,
      availableCreditId: 'credit-1',
      availableCreditCount: 2,
      creditDetailsAvailable: true,
    });
  });
});

describe('maybeConsumeCodexWeeklyReset', () => {
  let codexHome: string;
  const oauthEnv = (extra: NodeJS.ProcessEnv = {}): NodeJS.ProcessEnv => ({
    HOME: codexHome,
    PATH: process.env.PATH,
    CODEX_HOME: codexHome,
    ...extra,
  });

  beforeEach(() => {
    vi.mocked(spawn).mockReset();
    codexHome = mkdtempSync(path.join(tmpdir(), 'bp-codex-reset-'));
    writeFileSync(
      path.join(codexHome, 'auth.json'),
      JSON.stringify({ tokens: { account_id: 'account-a', access_token: 'token-one' } }),
    );
  });

  afterEach(() => rmSync(codexHome, { recursive: true, force: true }));

  it('redeems the exact credit with an idempotency key when weekly usage is exhausted', async () => {
    const { child, requests } = fakeAppServer(rateLimitResponse(100, [
      { id: 'credit-1', resetType: 'codexRateLimits', status: 'available', expiresAt: 2_000_000_000 },
      { id: 'credit-2', resetType: 'codexRateLimits', status: 'available', expiresAt: 2_000_000_000 },
    ]));
    vi.mocked(spawn).mockReturnValue(child as never);

    const result = await maybeConsumeCodexWeeklyReset({
      command: 'codex',
      env: oauthEnv(),
    });

    expect(result.status).toBe('reset');
    const consume = requests.find(request => request.id === 3);
    expect(consume).toMatchObject({
      method: 'account/rateLimitResetCredit/consume',
      params: { creditId: 'credit-1', idempotencyKey: expect.any(String) },
    });
    expect(requests.find(request => request.id === 2)).toMatchObject({ params: null });
  });

  it('does not redeem when only the short window is exhausted', async () => {
    const { child, requests } = fakeAppServer(rateLimitResponse(25, [
      { id: 'credit-1', resetType: 'codexRateLimits', status: 'available', expiresAt: 2_000_000_000 },
    ]));
    vi.mocked(spawn).mockReturnValue(child as never);

    const result = await maybeConsumeCodexWeeklyReset({ command: 'codex', env: oauthEnv() });

    expect(result.status).toBe('not_exhausted');
    expect(requests.some(request => request.id === 3)).toBe(false);
  });

  it('does not use an aggregate credit count without an exact credit ID', async () => {
    const { child, requests } = fakeAppServer(rateLimitResponse(100, null));
    vi.mocked(spawn).mockReturnValue(child as never);

    const result = await maybeConsumeCodexWeeklyReset({ command: 'codex', env: oauthEnv() });

    expect(result).toMatchObject({
      status: 'no_credit',
      exhausted: true,
      availableCreditCount: 1,
      creditDetailsAvailable: false,
    });
    expect(requests.some(request => request.id === 3)).toBe(false);
  });

  it('uses one stable idempotency key for one account/window', () => {
    const first = codexWeeklyResetIdempotencyKey('account-a', 2_000_000_000);
    expect(codexWeeklyResetIdempotencyKey('account-a', 2_000_000_000)).toBe(first);
    expect(codexWeeklyResetIdempotencyKey('account-a', 2_000_000_001)).not.toBe(first);
    expect(codexWeeklyResetIdempotencyKey('account-b', 2_000_000_000)).not.toBe(first);
  });

  it('derives identity from account_id, stable across token rotation', () => {
    const first = codexOAuthAccountIdentity(oauthEnv());
    writeFileSync(
      path.join(codexHome, 'auth.json'),
      JSON.stringify({ tokens: { account_id: 'account-a', access_token: 'token-two' } }),
    );
    expect(codexOAuthAccountIdentity(oauthEnv())).toBe(first);
    writeFileSync(
      path.join(codexHome, 'auth.json'),
      JSON.stringify({ tokens: { account_id: 'account-b', access_token: 'token-two' } }),
    );
    expect(codexOAuthAccountIdentity(oauthEnv())).not.toBe(first);
    expect(first).not.toContain('account-a');
  });

  it('refuses automatic reset when auth.json has no stable account_id', async () => {
    writeFileSync(
      path.join(codexHome, 'auth.json'),
      JSON.stringify({ tokens: { access_token: 'token-only' } }),
    );
    const result = await maybeConsumeCodexWeeklyReset({ command: 'codex', env: oauthEnv() });
    expect(result.status).toBe('probe_failed');
    expect(spawn).not.toHaveBeenCalled();
  });

  it('persists the handled window so a stale second snapshot cannot spend credit two', async () => {
    const first = fakeAppServer(rateLimitResponse(100, [
      { id: 'credit-1', resetType: 'codexRateLimits', status: 'available', expiresAt: 2_100_000_000 },
    ]));
    const staleSecond = fakeAppServer(rateLimitResponse(100, [
      { id: 'credit-2', resetType: 'codexRateLimits', status: 'available', expiresAt: 2_100_000_000 },
    ]));
    vi.mocked(spawn)
      .mockReturnValueOnce(first.child as never)
      .mockReturnValueOnce(staleSecond.child as never);

    expect((await maybeConsumeCodexWeeklyReset({ command: 'codex', env: oauthEnv() })).status).toBe('reset');
    expect((await maybeConsumeCodexWeeklyReset({ command: 'codex', env: oauthEnv() })).status).toBe('nothing_to_reset');
    expect(first.requests.some(request => request.id === 3)).toBe(true);
    expect(staleSecond.requests.some(request => request.id === 3)).toBe(false);
  });

  it('scopes a persisted handled-window marker to account_id', async () => {
    const accountA = fakeAppServer(rateLimitResponse(100, [
      { id: 'credit-a', resetType: 'codexRateLimits', status: 'available', expiresAt: 2_100_000_000 },
    ]));
    const accountB = fakeAppServer(rateLimitResponse(100, [
      { id: 'credit-b', resetType: 'codexRateLimits', status: 'available', expiresAt: 2_100_000_000 },
    ]));
    vi.mocked(spawn)
      .mockReturnValueOnce(accountA.child as never)
      .mockReturnValueOnce(accountB.child as never);

    expect((await maybeConsumeCodexWeeklyReset({ command: 'codex', env: oauthEnv() })).status).toBe('reset');
    writeFileSync(
      path.join(codexHome, 'auth.json'),
      JSON.stringify({ tokens: { account_id: 'account-b', access_token: 'token-b' } }),
    );
    expect((await maybeConsumeCodexWeeklyReset({ command: 'codex', env: oauthEnv() })).status).toBe('reset');
    expect(accountA.requests.some(request => request.id === 3)).toBe(true);
    expect(accountB.requests.some(request => request.id === 3)).toBe(true);
    const keyA = (accountA.requests.find(request => request.id === 3)?.params as { idempotencyKey: string }).idempotencyKey;
    const keyB = (accountB.requests.find(request => request.id === 3)?.params as { idempotencyKey: string }).idempotencyKey;
    expect(keyB).not.toBe(keyA);
  });

  it('refuses automatic redemption without a stable weekly resetsAt', async () => {
    const response = rateLimitResponse(100, [
      { id: 'credit-1', resetType: 'codexRateLimits', status: 'available', expiresAt: 2_100_000_000 },
    ]);
    response.rateLimits.primary.resetsAt = null as never;
    const { child, requests } = fakeAppServer(response);
    vi.mocked(spawn).mockReturnValue(child as never);

    const result = await maybeConsumeCodexWeeklyReset({ command: 'codex', env: oauthEnv() });
    expect(result.status).toBe('probe_failed');
    expect(requests.some(request => request.id === 3)).toBe(false);
  });

  it('serializes account preflights across processes', async () => {
    mkdirSync(path.join(codexHome, '.blueprint-codex-weekly-reset.lock'));
    const result = await maybeConsumeCodexWeeklyReset({
      command: 'codex', env: oauthEnv(), timeoutMs: 1_000,
    });
    expect(result.status).toBe('probe_failed');
    expect(spawn).not.toHaveBeenCalled();
  });

  it('waits briefly for a local owner then re-acquires safely', async () => {
    const lockPath = path.join(codexHome, '.blueprint-codex-weekly-reset.lock');
    mkdirSync(lockPath);
    const { child } = fakeAppServer(rateLimitResponse(10));
    vi.mocked(spawn).mockReturnValue(child as never);
    setTimeout(() => rmdirSync(lockPath), 20);
    const result = await maybeConsumeCodexWeeklyReset({
      command: 'codex', env: oauthEnv(), timeoutMs: 1_500,
    });
    expect(result.status).toBe('not_exhausted');
    expect(spawn).toHaveBeenCalledOnce();
  });

  it('passes only the minimal OAuth environment to app-server', async () => {
    const { child } = fakeAppServer(rateLimitResponse(10));
    vi.mocked(spawn).mockReturnValue(child as never);
    await maybeConsumeCodexWeeklyReset({
      command: 'codex',
      env: oauthEnv({
        OPENAI_API_KEY: 'secret-openai',
        CODEX_API_KEY: 'secret-codex',
        AGENT_API_TOKEN: 'secret-agent',
        ORCHESTRATOR_URL: 'https://orchestrator',
        GITHUB_TOKEN_FILE: '/secret/github-token',
      }),
    });
    const spawnedEnv = vi.mocked(spawn).mock.calls[0]?.[2]?.env as NodeJS.ProcessEnv;
    expect(spawnedEnv.CODEX_HOME).toBe(codexHome);
    expect(spawnedEnv.OPENAI_API_KEY).toBeUndefined();
    expect(spawnedEnv.CODEX_API_KEY).toBeUndefined();
    expect(spawnedEnv.AGENT_API_TOKEN).toBeUndefined();
    expect(spawnedEnv.ORCHESTRATOR_URL).toBeUndefined();
    expect(spawnedEnv.GITHUB_TOKEN_FILE).toBeUndefined();
    expect(codexOAuthProbeEnv(oauthEnv()).RUST_LOG).toBe('error');
  });

  it('waits for app-server close before resolving', async () => {
    const { child, close } = fakeAppServer(rateLimitResponse(10), 'reset', false);
    vi.mocked(spawn).mockReturnValue(child as never);
    let resolved = false;
    const pending = maybeConsumeCodexWeeklyReset({ command: 'codex', env: oauthEnv() })
      .then(result => { resolved = true; return result; });
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(child.stdin.writableEnded).toBe(true);
    expect(resolved).toBe(false);
    close();
    expect((await pending).status).toBe('not_exhausted');
  });

  it('blocks an injected successor through the final directory-removal gap', () => {
    const lockPath = path.join(codexHome, '.blueprint-codex-weekly-reset.lock');
    mkdirSync(lockPath);
    writeFileSync(
      path.join(lockPath, 'owner.json'),
      JSON.stringify({
        ownerToken: 'current-owner',
        pid: process.pid,
        createdAt: Date.now(),
      }),
    );
    let successorBlocked = false;
    expect(removeOwnedResetLockDirectory(lockPath, 'current-owner', () => {
      expect(() => mkdirSync(lockPath)).toThrow();
      successorBlocked = true;
    })).toBe(true);
    expect(successorBlocked).toBe(true);
    expect(existsSync(lockPath)).toBe(false);
  });

  it('recovers missing and corrupt owner publication after bounded grace', async () => {
    const lockPath = path.join(codexHome, '.blueprint-codex-weekly-reset.lock');
    for (const corrupt of [false, true]) {
      mkdirSync(lockPath);
      if (corrupt) {
        writeFileSync(path.join(lockPath, 'owner.json'), '{partial');
      }
      const old = new Date(Date.now() - 60_000);
      utimesSync(lockPath, old, old);
      const { child } = fakeAppServer(rateLimitResponse(10));
      vi.mocked(spawn).mockReturnValueOnce(child as never);
      expect((await maybeConsumeCodexWeeklyReset({
        command: 'codex',
        env: oauthEnv(),
      })).status).toBe('not_exhausted');
      expect(existsSync(lockPath)).toBe(false);
    }
  });

  it('reclaims a persisted quarantine after its owner process died', async () => {
    const lockPath = path.join(codexHome, '.blueprint-codex-weekly-reset.lock');
    mkdirSync(lockPath);
    writeFileSync(
      path.join(lockPath, 'owner.json'),
      JSON.stringify({
        ownerToken: 'crashed-owner',
        pid: 999_999,
        createdAt: Date.now() - 60_000,
        cleanupUnconfirmed: true,
        quarantineAt: Date.now(),
      }),
    );
    const old = new Date(Date.now() - 60_000);
    utimesSync(lockPath, old, old);
    const { child } = fakeAppServer(rateLimitResponse(10));
    vi.mocked(spawn).mockReturnValue(child as never);
    expect((await maybeConsumeCodexWeeklyReset({
      command: 'codex',
      env: oauthEnv(),
    })).status).toBe('not_exhausted');
  });

  it('reclaims a live-owner quarantine after the deterministic safety bound', async () => {
    const lockPath = path.join(codexHome, '.blueprint-codex-weekly-reset.lock');
    mkdirSync(lockPath);
    writeFileSync(
      path.join(lockPath, 'owner.json'),
      JSON.stringify({
        ownerToken: 'old-live-owner',
        pid: process.pid,
        createdAt: Date.now() - 10 * 60_000,
        cleanupUnconfirmed: true,
        quarantineAt: Date.now() - 10 * 60_000,
      }),
    );
    const old = new Date(Date.now() - 10 * 60_000);
    utimesSync(lockPath, old, old);
    const { child } = fakeAppServer(rateLimitResponse(10));
    vi.mocked(spawn).mockReturnValue(child as never);
    expect((await maybeConsumeCodexWeeklyReset({
      command: 'codex',
      env: oauthEnv(),
    })).status).toBe('not_exhausted');
  });

  it('keeps the account guard while a kill-resistant wrapper can survive', async () => {
    const { child, close } = fakeAppServer(rateLimitResponse(10), 'reset', false);
    child.kill.mockImplementation(() => true);
    vi.mocked(spawn).mockReturnValue(child as never);

    const startedAt = Date.now();
    const result = await maybeConsumeCodexWeeklyReset({
      command: 'codex',
      env: oauthEnv(),
      timeoutMs: 3_000,
    });
    expect(Date.now() - startedAt).toBeLessThan(2_500);
    expect(result.status).toBe('not_exhausted');
    expect(child.kill).toHaveBeenCalled();
    const lockPath = path.join(codexHome, '.blueprint-codex-weekly-reset.lock');
    expect(existsSync(lockPath)).toBe(true);
    expect(
      (JSON.parse(
        readFileSync(path.join(lockPath, 'owner.json'), 'utf8'),
      ) as { cleanupUnconfirmed?: boolean })
        .cleanupUnconfirmed,
    ).toBe(true);

    // Even an apparently stale quarantine is never reclaimed while cleanup is
    // unconfirmed; the second preflight fails closed without spawning.
    const old = new Date(Date.now() - 10 * 60_000);
    utimesSync(lockPath, old, old);
    const second = await maybeConsumeCodexWeeklyReset({
      command: 'codex',
      env: oauthEnv(),
      timeoutMs: 1_000,
    });
    expect(second.status).toBe('probe_failed');
    expect(spawn).toHaveBeenCalledTimes(1);

    close();
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(existsSync(lockPath)).toBe(false);
  });
});
