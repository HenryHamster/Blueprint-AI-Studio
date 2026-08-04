/**
 * walkthrough-stack — boot an ephemeral, isolated candidate-SHA stack.
 *
 * The `walkthrough_e2e` evidence lane must EXERCISE the candidate commit, not
 * the currently-deployed engine. This module boots a throwaway stack built
 * straight from the checked-out candidate source tree:
 *
 *   - a candidate backend via `docker compose` (postgres:15 `db` + `app`
 *     built from the candidate `backend/` directory, listening on a host port),
 *   - alembic migrations applied to head against that DB,
 *   - a health/version gate that reads the SHA the backend is ACTUALLY serving,
 *   - the walkthrough fixture project seeded against the local DB,
 *   - a served admin-ui frontend (vite dev server — the lighter serving path,
 *     no from-scratch production bundle) proxied at the candidate backend.
 *
 * HONESTY / FAIL-CLOSED: every step is a hard gate. If the stack cannot be
 * built, migrated, health-checked, seeded, or served at the candidate SHA,
 * `boot()` throws — the caller must then post ZERO evidence. There is never a
 * silent fallback to the deployed engine.
 *
 * TEARDOWN: `teardown()` is idempotent and never throws. It SIGKILLs the
 * frontend process and runs `docker compose down -v` so containers, the DB
 * volume, and host ports are always released, on every exit path.
 */

import { execFileSync, spawn, type ChildProcess } from 'child_process';
import { randomBytes } from 'crypto';
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'fs';
import * as path from 'path';

export interface CandidateStack {
  /** Base URL of the candidate backend the harness must drive (NOT ORCHESTRATOR_URL). */
  backendUrl: string;
  /** Base URL of the served candidate frontend the harness screenshots. */
  frontendUrl: string;
  /** Admin bearer token accepted by the local candidate backend. */
  adminToken: string;
  /** SQLAlchemy asyncpg URL of the local candidate DB (for seeding). */
  databaseUrl: string;
  /** The engine SHA the local stack reports serving, from GET /api/version. */
  exercisedEngineSha: string;
}

export interface CandidateStackOptions {
  workspace: string;
  candidateSha: string;
  /** Admin token to configure on the stack; a throwaway one is generated when absent. */
  adminToken?: string;
  /** Host port for the candidate backend (default 8080). */
  backendPort?: number;
  /** Host port for the candidate postgres (default 5432). */
  dbPort?: number;
  /** Host port for the served candidate frontend (default 3001). */
  frontendPort?: number;
  /** Overall boot budget in ms (default 8 min). */
  bootTimeoutMs?: number;
  /** Progress logger. */
  log?: (msg: string) => void;
  /**
   * Whether to boot the admin-ui frontend. Defaults to true. The frontend is a
   * hard boot gate: the harness cannot screenshot the candidate UI without it,
   * so a served-frontend failure fails the whole boot closed.
   */
  bootFrontend?: boolean;
}

// 20 min: a runner-side boot on a cold image cache pays a postgres pull plus a
// full backend pip-install docker build before the first health probe can pass.
const DEFAULT_BOOT_TIMEOUT_MS = 20 * 60_000;
const HEALTH_POLL_INTERVAL_MS = 3_000;

/**
 * File protocol with a runner-side stack supervisor (scripts/walkthrough_stack.py).
 *
 * The agent sandbox deliberately has no docker socket, so on local runners the
 * RUNNER boots the candidate stack. The runner advertises itself by writing
 * `supervisor.json` into this directory BEFORE the agent spawns; the agent then
 * writes `request.json` (candidate SHA + throwaway stack secrets), polls
 * `response.json` for the loopback ports, and writes `teardown.json` when done.
 * Without the marker the legacy in-sandbox `docker compose` path runs unchanged
 * (agent VMs with a real docker daemon).
 */
const STACK_PROTOCOL_DIR = '.walkthrough-stack';

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Docker-compose project names must be lowercase and may contain only
 * [a-z0-9_-]. Derive a unique-per-run name from the candidate SHA plus random
 * entropy so parallel walkthrough VMs never share a compose project.
 */
export function deriveComposeProject(candidateSha: string): string {
  const shaPart = (candidateSha || '').toLowerCase().replace(/[^a-z0-9]/g, '').slice(0, 8) || 'nosha';
  const rand = randomBytes(4).toString('hex');
  return `wt-${shaPart}-${rand}`;
}

export class CandidateStackController {
  private readonly opts: Required<Omit<CandidateStackOptions, 'log' | 'adminToken'>> & {
    log: (msg: string) => void;
    adminToken: string;
  };

  private readonly composeProject: string;
  private readonly composeFile: string;
  private readonly childEnv: NodeJS.ProcessEnv;

  private composeUp = false;
  private frontendProc: ChildProcess | null = null;
  private tornDown = false;
  /** Loopback ports returned by a runner-side supervisor (null → legacy path). */
  private runnerPorts: { backend: number; db: number } | null = null;

  constructor(options: CandidateStackOptions) {
    const adminToken = options.adminToken || `wt-admin-${randomBytes(12).toString('hex')}`;
    this.opts = {
      workspace: options.workspace,
      candidateSha: options.candidateSha,
      adminToken,
      backendPort: options.backendPort ?? 8080,
      dbPort: options.dbPort ?? 5432,
      frontendPort: options.frontendPort ?? 3001,
      bootTimeoutMs: options.bootTimeoutMs ?? DEFAULT_BOOT_TIMEOUT_MS,
      bootFrontend: options.bootFrontend ?? true,
      log: options.log ?? (() => {}),
    };
    this.composeProject = deriveComposeProject(this.opts.candidateSha);
    this.composeFile = path.join(this.opts.workspace, 'docker-compose.yml');
    // Throwaway, non-production secrets: the stack is ephemeral and never
    // handles real GitHub/webhook traffic. `${VAR?}` in docker-compose.yml
    // errors only when a var is UNSET (empty is accepted), but we pass real
    // placeholder values so any at-import parsing in the app does not choke.
    this.childEnv = {
      ...process.env,
      COMPOSE_PROJECT_NAME: this.composeProject,
      POSTGRES_PASSWORD: `wt-pg-${randomBytes(8).toString('hex')}`,
      AGENT_JWT_SECRET: randomBytes(24).toString('hex'),
      WS_TICKET_SECRET: randomBytes(24).toString('hex'),
      GITHUB_APP_ID: '0',
      GITHUB_PRIVATE_KEY: '',
      GITHUB_WEBHOOK_SECRET: 'walkthrough-local',
      ANTHROPIC_API_KEY: process.env.ANTHROPIC_API_KEY || '',
      LOCAL_MODE: 'true',
      ADMIN_API_KEY: this.opts.adminToken,
      // The app reports this via GET /api/version as the engine SHA it serves.
      // Because the image is BUILT from the checked-out candidate tree, labelling
      // it with the candidate SHA is honest — and it is the seam a backend guard
      // enforces against candidate_sha.
      DEPLOY_SHA: this.opts.candidateSha,
      WALKTHROUGH_APP_PORT: String(this.opts.backendPort),
      WALKTHROUGH_DB_PORT: String(this.opts.dbPort),
    };
  }

  get backendUrl(): string {
    return `http://127.0.0.1:${this.runnerPorts?.backend ?? this.opts.backendPort}`;
  }

  get frontendUrl(): string {
    return `http://localhost:${this.opts.frontendPort}`;
  }

  get databaseUrl(): string {
    const dbPort = this.runnerPorts?.db ?? this.opts.dbPort;
    return `postgresql+asyncpg://postgres:${this.childEnv.POSTGRES_PASSWORD}@127.0.0.1:${dbPort}/postgres`;
  }

  private get protocolDir(): string {
    return path.join(this.opts.workspace, STACK_PROTOCOL_DIR);
  }

  /** Whether a runner-side stack supervisor advertised itself for this task. */
  runnerSupervisorAvailable(): boolean {
    return existsSync(path.join(this.protocolDir, 'supervisor.json'));
  }

  private compose(args: string[], timeoutMs: number): string {
    return execFileSync(
      'docker',
      ['compose', '-p', this.composeProject, '-f', this.composeFile, ...args],
      { cwd: this.opts.workspace, env: this.childEnv, encoding: 'utf-8', stdio: ['ignore', 'pipe', 'pipe'], timeout: timeoutMs },
    );
  }

  /**
   * Boot the full candidate stack. Throws on ANY failure so the caller fails
   * closed (posts no evidence). Guarantee: whether this resolves or rejects,
   * the caller must still call teardown().
   */
  async boot(): Promise<CandidateStack> {
    const deadline = Date.now() + this.opts.bootTimeoutMs;
    const log = this.opts.log;

    if (this.runnerSupervisorAvailable()) {
      // Local runner: the sandbox has no docker socket; the runner-side
      // supervisor boots the fixed-shape stack and hands back loopback ports.
      log('Runner-side stack supervisor detected — requesting candidate stack boot...\n');
      await this.bootViaRunnerSupervisor(deadline);
    } else {
      // Legacy path (agent VMs with a real docker daemon): boot in-process.
      // 1. Build the candidate image and bring up the DB first so migrations
      //    can run before the app performs its startup schema check.
      log(`Building candidate image + starting postgres (compose project ${this.composeProject})...\n`);
      this.composeUp = true; // mark BEFORE up so a mid-build failure is still torn down
      this.compose(['up', '-d', '--build', 'db'], Math.max(60_000, deadline - Date.now()));

      // 2. Wait for postgres to accept connections.
      await this.waitForDb(deadline);

      // 3. Apply migrations to head in a one-off container (the app is not up
      //    yet, so there is no racing schema check). alembic.ini lives at /app.
      log('Applying alembic migrations to head against the candidate DB...\n');
      this.compose(
        ['run', '--rm', '--no-deps', 'app', 'alembic', 'upgrade', 'head'],
        Math.max(60_000, deadline - Date.now()),
      );

      // 4. Start the app now that the schema is at head.
      log('Starting candidate backend...\n');
      this.compose(['up', '-d', 'app'], Math.max(60_000, deadline - Date.now()));
    }

    // 5. Health-check the candidate backend, then read the SHA it serves.
    await this.waitForBackendHealth(deadline);
    const exercisedEngineSha = await this.readEngineSha();
    log(`Candidate backend healthy; serving engine SHA ${exercisedEngineSha.slice(0, 12)}.\n`);

    // 6. Seed the walkthrough fixture project against the local DB.
    log('Seeding walkthrough fixture project against the candidate DB...\n');
    execFileSync('python3', [path.join('scripts', 'walkthrough', 'seed_test_data.py')], {
      cwd: this.opts.workspace,
      env: { ...this.childEnv, DATABASE_URL: this.databaseUrl },
      encoding: 'utf-8',
      stdio: ['ignore', 'pipe', 'pipe'],
      timeout: 60_000,
    });

    // 7. Serve the candidate frontend (vite dev server proxied at the local backend).
    if (this.opts.bootFrontend) {
      await this.bootFrontend(deadline);
    }

    return {
      backendUrl: this.backendUrl,
      frontendUrl: this.frontendUrl,
      adminToken: this.opts.adminToken,
      databaseUrl: this.databaseUrl,
      exercisedEngineSha,
    };
  }

  /**
   * Ask the runner-side supervisor to boot the stack, then adopt the loopback
   * ports it publishes. The sandbox shares the host network namespace, so
   * 127.0.0.1:<port> reaches the containers directly. Throws (fail-closed) on
   * an error response or when no response lands before the deadline.
   */
  private async bootViaRunnerSupervisor(deadline: number): Promise<void> {
    mkdirSync(this.protocolDir, { recursive: true });
    const request = {
      version: 1,
      candidate_sha: this.opts.candidateSha,
      admin_token: this.opts.adminToken,
      pg_password: this.childEnv.POSTGRES_PASSWORD || '',
      agent_jwt_secret: this.childEnv.AGENT_JWT_SECRET || '',
      ws_ticket_secret: this.childEnv.WS_TICKET_SECRET || '',
      // Parity with the legacy compose env: the candidate backend receives the
      // same key the sandbox itself was vended (may be empty).
      anthropic_api_key: process.env.ANTHROPIC_API_KEY || '',
    };
    const requestPath = path.join(this.protocolDir, 'request.json');
    writeFileSync(`${requestPath}.tmp`, JSON.stringify(request));
    renameSync(`${requestPath}.tmp`, requestPath);

    const responsePath = path.join(this.protocolDir, 'response.json');
    while (Date.now() < deadline) {
      if (existsSync(responsePath)) {
        let parsed: { status?: unknown; detail?: unknown; backend_port?: unknown; db_port?: unknown };
        try {
          parsed = JSON.parse(readFileSync(responsePath, 'utf-8')) as typeof parsed;
        } catch {
          // Torn read of an in-flight atomic replace — retry next poll.
          await sleep(HEALTH_POLL_INTERVAL_MS);
          continue;
        }
        if (parsed.status === 'ok') {
          const backend = Number(parsed.backend_port);
          const db = Number(parsed.db_port);
          if (!Number.isInteger(backend) || !Number.isInteger(db)) {
            throw new Error('runner stack supervisor returned malformed ports');
          }
          this.runnerPorts = { backend, db };
          return;
        }
        const detail = typeof parsed.detail === 'string' && parsed.detail ? parsed.detail : 'no detail';
        throw new Error(`runner stack supervisor reported boot failure: ${detail}`);
      }
      await sleep(HEALTH_POLL_INTERVAL_MS);
    }
    throw new Error('runner-side stack boot never completed before the deadline (no supervisor response)');
  }

  private async waitForDb(deadline: number): Promise<void> {
    while (Date.now() < deadline) {
      try {
        this.compose(['exec', '-T', 'db', 'pg_isready', '-U', 'postgres'], 15_000);
        return;
      } catch {
        await sleep(HEALTH_POLL_INTERVAL_MS);
      }
    }
    throw new Error('candidate postgres did not become ready before the boot deadline');
  }

  private async waitForBackendHealth(deadline: number): Promise<void> {
    let lastDetail = 'no response';
    while (Date.now() < deadline) {
      try {
        const res = await fetch(`${this.backendUrl}/health`, {
          headers: { Authorization: `Bearer ${this.opts.adminToken}` },
        });
        if (res.status === 200) return;
        lastDetail = `HTTP ${res.status}`;
      } catch (e) {
        lastDetail = e instanceof Error ? e.message : String(e);
      }
      await sleep(HEALTH_POLL_INTERVAL_MS);
    }
    throw new Error(`candidate backend health check never returned 200 (last: ${lastDetail})`);
  }

  private async readEngineSha(): Promise<string> {
    const res = await fetch(`${this.backendUrl}/api/version`);
    if (res.status !== 200) {
      throw new Error(`candidate backend /api/version returned HTTP ${res.status}`);
    }
    const body = (await res.json()) as { sha?: unknown };
    const sha = typeof body.sha === 'string' ? body.sha.trim() : '';
    if (!sha) {
      throw new Error('candidate backend /api/version did not report a usable sha');
    }
    return sha;
  }

  private async bootFrontend(deadline: number): Promise<void> {
    const log = this.opts.log;
    const adminUiDir = path.join(this.opts.workspace, 'admin-ui');
    const frontendEnv = {
      ...process.env,
      VITE_DEV_PROXY_TARGET: this.backendUrl,
      VITE_DASHBOARD_API_TOKEN: this.opts.adminToken,
    };
    log('Serving candidate admin-ui (vite dev server) against the candidate backend...\n');
    this.frontendProc = spawn(
      'npm',
      ['run', 'dev', '--', '--port', String(this.opts.frontendPort), '--host', 'localhost'],
      { cwd: adminUiDir, env: frontendEnv, stdio: ['ignore', 'pipe', 'pipe'] },
    );
    // Surface a fast, explicit failure if the process dies (e.g. missing deps).
    // Hold the flag on an object so it is re-read (not flow-narrowed to null)
    // after the closure assignment inside the poll loop below.
    const procState: { exited: string | null } = { exited: null };
    this.frontendProc.on('exit', (code, signal) => {
      procState.exited = `frontend process exited early (code=${code ?? 'null'}, signal=${signal ?? 'null'})`;
    });

    let lastDetail = 'no response';
    while (Date.now() < deadline) {
      if (procState.exited) throw new Error(procState.exited);
      try {
        const res = await fetch(this.frontendUrl, { method: 'GET' });
        if (res.status >= 200 && res.status < 500) return;
        lastDetail = `HTTP ${res.status}`;
      } catch (e) {
        lastDetail = e instanceof Error ? e.message : String(e);
      }
      await sleep(HEALTH_POLL_INTERVAL_MS);
    }
    throw new Error(`candidate frontend did not serve before the boot deadline (last: ${lastDetail})`);
  }

  /** Idempotent, never-throwing teardown of every provisioned resource. */
  async teardown(): Promise<void> {
    if (this.tornDown) return;
    this.tornDown = true;
    const log = this.opts.log;

    if (this.frontendProc) {
      try {
        this.frontendProc.kill('SIGTERM');
        await sleep(1_500);
        if (this.frontendProc.exitCode === null && this.frontendProc.signalCode === null) {
          this.frontendProc.kill('SIGKILL');
        }
      } catch {
        /* best effort */
      }
      this.frontendProc = null;
    }

    if (this.runnerPorts !== null) {
      // Runner-managed stack: signal early teardown; the supervisor also
      // always tears down when this agent process exits (belt and braces).
      try {
        log('Signalling runner-side candidate stack teardown...\n');
        writeFileSync(
          path.join(this.protocolDir, 'teardown.json'),
          JSON.stringify({ requested_at: new Date().toISOString() }),
        );
      } catch (e) {
        log(`[warn] could not signal runner-side teardown (runner reaps on exit): ${e instanceof Error ? e.message : String(e)}\n`);
      }
      this.runnerPorts = null;
    }

    if (this.composeUp) {
      try {
        log('Tearing down candidate stack (docker compose down -v)...\n');
        this.compose(['down', '-v', '--remove-orphans'], 120_000);
      } catch (e) {
        log(`[warn] candidate stack teardown reported an error (continuing): ${e instanceof Error ? e.message : String(e)}\n`);
      }
      this.composeUp = false;
    }
  }
}
