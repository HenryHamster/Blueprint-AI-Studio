import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('child_process', () => ({
  execFileSync: vi.fn().mockReturnValue(''),
  spawn: vi.fn().mockReturnValue({
    stdout: { on: vi.fn() },
    stderr: { on: vi.fn() },
    on: vi.fn(),
    kill: vi.fn(),
    exitCode: null,
    signalCode: null,
  }),
}));

import { CandidateStackController, deriveComposeProject } from './walkthrough-stack.js';
import { execFileSync } from 'child_process';

const SHA = 'abcdef1234567890abcdef1234567890abcdef12';

describe('deriveComposeProject', () => {
  it('produces a lowercase, compose-safe, unique-per-run project name', () => {
    const a = deriveComposeProject(SHA);
    const b = deriveComposeProject(SHA);
    expect(a).toMatch(/^wt-[a-z0-9]{1,8}-[0-9a-f]{8}$/);
    expect(a).not.toBe(b); // random suffix → parallel stacks never collide
  });

  it('tolerates a missing/blank SHA', () => {
    expect(deriveComposeProject('')).toMatch(/^wt-nosha-[0-9a-f]{8}$/);
  });
});

describe('CandidateStackController', () => {
  beforeEach(() => vi.clearAllMocks());

  it('exposes URLs and a DB URL derived from the configured ports', () => {
    const c = new CandidateStackController({
      workspace: '/tmp/workspace',
      candidateSha: SHA,
      adminToken: 'tok',
      backendPort: 18080,
      dbPort: 15432,
      frontendPort: 13001,
    });
    expect(c.backendUrl).toBe('http://127.0.0.1:18080');
    expect(c.frontendUrl).toBe('http://localhost:13001');
    expect(c.databaseUrl).toMatch(/^postgresql\+asyncpg:\/\/postgres:.+@127\.0\.0\.1:15432\/postgres$/);
  });

  it('boot() fails closed when the stack never becomes ready (deadline exceeded)', async () => {
    const c = new CandidateStackController({
      workspace: '/tmp/workspace',
      candidateSha: SHA,
      adminToken: 'tok',
      bootTimeoutMs: 0, // deadline already passed → DB readiness loop cannot succeed
    });
    await expect(c.boot()).rejects.toThrow(/postgres did not become ready/);
  });

  it('teardown() is idempotent and never throws even when compose down errors', async () => {
    const c = new CandidateStackController({
      workspace: '/tmp/workspace',
      candidateSha: SHA,
      adminToken: 'tok',
      bootTimeoutMs: 0,
    });
    // Provision compose resources (boot marks composeUp before failing).
    await expect(c.boot()).rejects.toThrow();

    // Make the teardown `docker compose down` throw; teardown must swallow it.
    vi.mocked(execFileSync).mockImplementationOnce(() => {
      throw new Error('compose down blew up');
    });
    await expect(c.teardown()).resolves.toBeUndefined();
    // Second teardown is a no-op (idempotent) and issues no further compose call.
    const before = vi.mocked(execFileSync).mock.calls.length;
    await expect(c.teardown()).resolves.toBeUndefined();
    expect(vi.mocked(execFileSync).mock.calls.length).toBe(before);
  });
});
