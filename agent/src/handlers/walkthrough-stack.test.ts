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
  beforeEach(() => {
    vi.clearAllMocks();
    // The runner injects this env; a stray value must not leak into unrelated
    // controller constructions.
    delete process.env.WALKTHROUGH_STACK_DIR;
  });

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

  it('legacy compose path passes a valid per-boot Fernet key as GITHUB_PAT_ENCRYPTION_KEY', async () => {
    // Boot through the legacy in-sandbox path and capture the env the docker
    // compose invocation received (compose interpolates ${VAR} from this env).
    // `start` scopes the search to calls made after a boot boundary, because
    // mock calls accumulate across boots within a single test.
    const composeEnvForKey = (start: number): string => {
      const dockerCall = vi.mocked(execFileSync).mock.calls.slice(start).find((call) => call[0] === 'docker');
      if (!dockerCall) throw new Error('expected a docker compose call on the legacy path');
      const key = (dockerCall[2] as { env: NodeJS.ProcessEnv }).env.GITHUB_PAT_ENCRYPTION_KEY;
      if (!key) throw new Error('legacy compose env must include GITHUB_PAT_ENCRYPTION_KEY');
      return key;
    };

    const c = new CandidateStackController({
      workspace: '/tmp/workspace',
      candidateSha: SHA,
      adminToken: 'tok',
      bootTimeoutMs: 0, // deadline already passed → DB readiness loop cannot succeed
    });
    await expect(c.boot()).rejects.toThrow(/postgres did not become ready/);
    // Migration 0609's _load_fernet() rejects an absent OR invalid key, so the
    // legacy path must pass a Fernet-valid key: 43 unpadded urlsafe base64
    // chars + one '=' pad, decoding to 32 bytes.
    const key1 = composeEnvForKey(0);
    expect(key1).toMatch(/^[A-Za-z0-9_-]{43}=$/);
    expect(Buffer.from(key1, 'base64url')).toHaveLength(32);

    // Per-boot randomness: a second controller mints a different key so a stack
    // re-boot never reuses the previous stack's encryption key.
    const before = vi.mocked(execFileSync).mock.calls.length;
    const c2 = new CandidateStackController({
      workspace: '/tmp/workspace',
      candidateSha: SHA,
      adminToken: 'tok',
      bootTimeoutMs: 0,
    });
    await expect(c2.boot()).rejects.toThrow(/postgres did not become ready/);
    expect(composeEnvForKey(before)).not.toBe(key1);
  });

  it('boot() runs npm ci at the workspace root, deferred to the frontend leg (after the backend is up)', async () => {
    const ws = mkdtempSync(path.join(os.tmpdir(), 'wt-stack-npmci-'));
    const logs: string[] = [];
    // Mock the probes so the legacy boot reaches the step-7 frontend leg.
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ status: 200, json: () => ({ sha: SHA }) }));
    try {
      const c = new CandidateStackController({
        workspace: ws,
        candidateSha: SHA,
        adminToken: 'tok',
        bootTimeoutMs: 20_000,
        log: (m) => logs.push(m),
      });
      await expect(c.boot()).resolves.toMatchObject({ exercisedEngineSha: SHA });
    } finally {
      vi.unstubAllGlobals();
    }
    // The depth-1 clone ships no node_modules; boot must run npm ci at the
    // workspace ROOT (both the root playwright dep and admin-ui workspace deps).
    const npmCiCalls = vi
      .mocked(execFileSync)
      .mock.calls.filter((call) => call[0] === 'npm' && call[1]?.[0] === 'ci');
    expect(npmCiCalls.length).toBeGreaterThan(0);
    expect(npmCiCalls[0][2]).toMatchObject({ cwd: ws });
    // Playwright's postinstall must not attempt a browser download into the
    // read-only bind-mounted cache: npm ci only works when the env carries the
    // skip flag, regardless of which chromium build the runner's cache holds.
    expect(npmCiCalls[0][2]?.env).toMatchObject({ PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD: '1' });
    expect(logs.some((m) => m.includes('npm ci'))).toBe(true);
    // npm ci is DEFERRED past the boot-path decision + backend boot.
    const calls = vi.mocked(execFileSync).mock.calls;
    const upIdx = calls.findIndex((call) => call[0] === 'docker' && JSON.stringify(call[1] ?? []).includes('up'));
    const ciIdx = calls.findIndex((call) => call[0] === 'npm' && call[1]?.[0] === 'ci');
    expect(upIdx).toBeGreaterThanOrEqual(0);
    expect(ciIdx).toBeGreaterThan(upIdx);
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

// ── Runner-side supervisor protocol ─────────────────────────────────────────
// The agent sandbox has no docker socket on local runners; when the runner
// advertises a stack supervisor (supervisor.json written before spawn), boot()
// must go through the file protocol and never touch docker itself.

import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, existsSync, statSync } from 'fs';
import * as os from 'os';
import * as path from 'path';

function protoWorkspace(): { ws: string; proto: string } {
  const ws = mkdtempSync(path.join(os.tmpdir(), 'wt-stack-proto-'));
  const proto = path.join(ws, '.walkthrough-stack');
  mkdirSync(proto, { recursive: true });
  writeFileSync(path.join(proto, 'supervisor.json'), JSON.stringify({ version: 1 }));
  return { ws, proto };
}

describe('runner-side supervisor protocol', () => {
  beforeEach(() => vi.clearAllMocks());

  it('writes request.json with the stack secrets and adopts the responded ports', async () => {
    const { ws, proto } = protoWorkspace();
    const c = new CandidateStackController({
      workspace: ws,
      walkthroughStackDir: proto,
      candidateSha: SHA,
      adminToken: 'tok-0123456789',
      bootTimeoutMs: 4_000,
      bootFrontend: false,
    });

    // Play the runner: answer the request as soon as it appears.
    const responder = (async () => {
      const requestPath = path.join(proto, 'request.json');
      for (let i = 0; i < 200 && !existsSync(requestPath); i++) {
        await new Promise((r) => setTimeout(r, 25));
      }
      const req = JSON.parse(readFileSync(requestPath, 'utf-8')) as Record<string, string>;
      expect(req.candidate_sha).toBe(SHA);
      expect(req.admin_token).toBe('tok-0123456789');
      expect(req.pg_password).toMatch(/^wt-pg-[0-9a-f]{16}$/);
      expect(req.agent_jwt_secret).toMatch(/^[0-9a-f]{48}$/);
      // The request carries stack secrets — owner-only, per runner convention.
      // Asserted on every POSIX platform: a mode leak is exactly the regression
      // this test exists to catch, and the 0o600 literal is the pin.
      //
      // Gated off on win32 because the behaviour under test cannot exist there,
      // not as a convenience: Windows has no POSIX mode bits. Node's fs.chmod on
      // win32 can only toggle the read-only attribute, and statSync().mode & 0o777
      // reports 0o666 no matter what the code did — so the check can neither pass
      // nor ever detect a real leak. (Owner-only access is an NTFS ACL question
      // there, which this file protocol does not model.) Throwing inside this
      // responder also strands the promise, which is why the phantom failure used
      // to cascade into an unhandled rejection and a misleading boot() message.
      // Every platform the agent actually runs on — Linux runners, Linux/macOS
      // CI — keeps the assertion.
      if (process.platform !== 'win32') {
        expect(statSync(requestPath).mode & 0o777).toBe(0o600);
      }
      writeFileSync(
        path.join(proto, 'response.json'),
        JSON.stringify({ status: 'ok', backend_port: 18123, db_port: 15123 }),
      );
    })();

    // The stack "boots" per the protocol, then the health probe hits
    // 127.0.0.1:18123 where nothing listens — boot fails closed THERE, which
    // proves the supervisor leg completed and the ports were adopted.
    await expect(c.boot()).rejects.toThrow(/health check never returned 200/);
    await responder;
    expect(c.backendUrl).toBe('http://127.0.0.1:18123');
    expect(c.databaseUrl).toContain('@127.0.0.1:15123/');
    // The agent never invoked docker on this path.
    expect(vi.mocked(execFileSync).mock.calls.filter((call) => call[0] === 'docker')).toHaveLength(0);
  }, 20_000);

  it('fails closed on a supervisor error response without touching docker', async () => {
    const { ws, proto } = protoWorkspace();
    writeFileSync(
      path.join(proto, 'response.json'),
      JSON.stringify({ status: 'error', detail: 'docker daemon unreachable' }),
    );
    const c = new CandidateStackController({
      workspace: ws,
      walkthroughStackDir: proto,
      candidateSha: SHA,
      adminToken: 'tok-0123456789',
      bootTimeoutMs: 8_000,
    });
    await expect(c.boot()).rejects.toThrow(/docker daemon unreachable/);
    expect(vi.mocked(execFileSync).mock.calls.filter((call) => call[0] === 'docker')).toHaveLength(0);
    // npm ci is deferred past the boot-path decision — the supervisor-leg
    // failure surfaces without paying workspace-level npm ci (npm/node,
    // network, playwright postinstall). bootFrontend defaults true here.
    const npmCiCalls = vi
      .mocked(execFileSync)
      .mock.calls.filter((call) => call[0] === 'npm' && call[1]?.[0] === 'ci');
    expect(npmCiCalls).toHaveLength(0);
  });

  it('teardown in runner mode signals teardown.json instead of compose down', async () => {
    const { ws, proto } = protoWorkspace();
    writeFileSync(
      path.join(proto, 'response.json'),
      JSON.stringify({ status: 'ok', backend_port: 18124, db_port: 15124 }),
    );
    const c = new CandidateStackController({
      workspace: ws,
      walkthroughStackDir: proto,
      candidateSha: SHA,
      adminToken: 'tok-0123456789',
      bootTimeoutMs: 2_000,
      bootFrontend: false,
    });
    await expect(c.boot()).rejects.toThrow(/health check/); // ports adopted, backend absent
    await expect(c.teardown()).resolves.toBeUndefined();
    expect(existsSync(path.join(proto, 'teardown.json'))).toBe(true);
    // No compose down was issued on the runner-managed path.
    const composeCalls = vi
      .mocked(execFileSync)
      .mock.calls.filter((call) => JSON.stringify(call[1] ?? []).includes('compose'));
    expect(composeCalls).toHaveLength(0);
  }, 20_000);

  it('falls back to the legacy in-sandbox path when no supervisor marker exists', async () => {
    const ws = mkdtempSync(path.join(os.tmpdir(), 'wt-stack-legacy-'));
    const logs: string[] = [];
    const c = new CandidateStackController({
      workspace: ws,
      candidateSha: SHA,
      adminToken: 'tok',
      bootTimeoutMs: 0, // deadline already passed → DB readiness loop cannot succeed
      log: (m) => logs.push(m),
    });
    await expect(c.boot()).rejects.toThrow(/postgres did not become ready/);
    // Legacy path DID reach for docker compose.
    expect(vi.mocked(execFileSync).mock.calls.some((call) => call[0] === 'docker')).toBe(true);
    // No protocol dir was passed → the legacy default is logged, not silent.
    expect(logs.some((m) => m.includes('using the in-sandbox docker compose path'))).toBe(true);
  });
});

// ── Single probe contract (the regression this module exists for) ──────────
// The runner writes the supervisor marker at the workspace bind ROOT (one level
// ABOVE the agent's own workspace) and passes that sandbox-side path to the
// agent explicitly. The probe must resolve EXACTLY that path — never a join
// against the agent's workspace, which silently misses the marker.

describe('runner-side supervisor probe path contract', () => {
  beforeEach(() => vi.clearAllMocks());

  it('probes the runner-passed protocol dir verbatim and writes the request there', async () => {
    // Simulate the bwrap layout: the agent's workspace (repo checkout) and the
    // runner-side protocol dir are DIFFERENT directories — the marker sits at
    // the workspace bind root, one level above the agent's workspace.
    const base = mkdtempSync(path.join(os.tmpdir(), 'wt-stack-layout-'));
    const agentWorkspace = path.join(base, 'agent'); // the checkout root (/workspace/agent)
    const runnerProtoDir = path.join(base, '.walkthrough-stack'); // the bind root (/workspace/.walkthrough-stack)
    mkdirSync(agentWorkspace, { recursive: true });
    mkdirSync(runnerProtoDir, { recursive: true });
    writeFileSync(path.join(runnerProtoDir, 'supervisor.json'), JSON.stringify({ version: 1 }));

    const c = new CandidateStackController({
      workspace: agentWorkspace,
      walkthroughStackDir: runnerProtoDir,
      candidateSha: SHA,
      adminToken: 'tok-0123456789',
      bootTimeoutMs: 4_000,
      bootFrontend: false,
    });

    const responder = (async () => {
      const requestPath = path.join(runnerProtoDir, 'request.json');
      for (let i = 0; i < 200 && !existsSync(requestPath); i++) {
        await new Promise((r) => setTimeout(r, 25));
      }
      // The request landed in the RUNNER-PASSED protocol dir — not a join under
      // the agent's own workspace.
      expect(requestPath.startsWith(path.join(agentWorkspace, '.walkthrough-stack'))).toBe(false);
      expect(existsSync(path.join(agentWorkspace, '.walkthrough-stack', 'request.json'))).toBe(false);
      const req = JSON.parse(readFileSync(requestPath, 'utf-8')) as Record<string, string>;
      expect(req.candidate_sha).toBe(SHA);
      writeFileSync(
        path.join(runnerProtoDir, 'response.json'),
        JSON.stringify({ status: 'ok', backend_port: 18223, db_port: 15223 }),
      );
    })();

    await expect(c.boot()).rejects.toThrow(/health check never returned 200/);
    await responder;
    expect(c.backendUrl).toBe('http://127.0.0.1:18223');
    expect(c.databaseUrl).toContain('@127.0.0.1:15223/');
    // The agent never invoked docker on the supervisor path.
    expect(vi.mocked(execFileSync).mock.calls.filter((call) => call[0] === 'docker')).toHaveLength(0);
  }, 20_000);

  it('does NOT detect a marker left at the legacy workspace-relative location', async () => {
    // A marker at <workspace>/.walkthrough-stack (the OLD probe derivation) must
    // be invisible: the probe resolves only the runner-passed protocol dir, so
    // the disagreement cannot be reintroduced by re-joining onto the workspace.
    const base = mkdtempSync(path.join(os.tmpdir(), 'wt-stack-stale-'));
    const agentWorkspace = path.join(base, 'agent');
    const runnerProtoDir = path.join(base, '.walkthrough-stack');
    mkdirSync(agentWorkspace, { recursive: true });
    mkdirSync(path.join(agentWorkspace, '.walkthrough-stack'), { recursive: true });
    writeFileSync(path.join(agentWorkspace, '.walkthrough-stack', 'supervisor.json'), JSON.stringify({ version: 1 }));

    const logs: string[] = [];
    const c = new CandidateStackController({
      workspace: agentWorkspace,
      walkthroughStackDir: runnerProtoDir,
      candidateSha: SHA,
      adminToken: 'tok',
      bootTimeoutMs: 0, // fail fast on the legacy DB-readiness loop
      log: (m) => logs.push(m),
    });
    expect(c.runnerSupervisorAvailable()).toBe(false);
    await expect(c.boot()).rejects.toThrow(/postgres did not become ready/);
    // Legacy path DID reach for docker compose.
    expect(vi.mocked(execFileSync).mock.calls.some((call) => call[0] === 'docker')).toBe(true);
    // The protocol dir was passed but the marker is absent → visible warning.
    expect(logs.some((m) => m.includes('No runner-side stack supervisor marker'))).toBe(true);
  });

  it('logs a visible degradation when the protocol dir is passed but the marker is absent', async () => {
    const base = mkdtempSync(path.join(os.tmpdir(), 'wt-stack-degrade-'));
    const runnerProtoDir = path.join(base, '.walkthrough-stack');
    mkdirSync(runnerProtoDir, { recursive: true }); // dir exists, but no supervisor.json
    const logs: string[] = [];
    const c = new CandidateStackController({
      workspace: base,
      walkthroughStackDir: runnerProtoDir,
      candidateSha: SHA,
      adminToken: 'tok',
      bootTimeoutMs: 0,
      log: (m) => logs.push(m),
    });
    await expect(c.boot()).rejects.toThrow(/postgres did not become ready/);
    expect(logs.some((m) => m.includes('No runner-side stack supervisor marker'))).toBe(true);
  });
});
