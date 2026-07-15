import { spawn } from 'node:child_process';
import { createHash, randomUUID } from 'node:crypto';
import {
  mkdirSync,
  readdirSync,
  readFileSync,
  realpathSync,
  renameSync,
  rmSync,
  rmdirSync,
  statSync,
  writeFileSync,
} from 'node:fs';
import path from 'node:path';
import { createInterface } from 'node:readline';

const WEEKLY_WINDOW_MINUTES = 7 * 24 * 60;
const DEFAULT_TIMEOUT_MS = 12_000;
const GRACEFUL_EXIT_MS = 500;
const FORCED_EXIT_SETTLE_MS = 1_500;
const RESET_LOCK_PUBLICATION_GRACE_MS = 30_000;
const RESET_QUARANTINE_MAX_MS = 5 * 60_000;
const RESET_LOCK_FILE = '.blueprint-codex-weekly-reset.lock';
const RESET_LOCK_OWNER_FILE = 'owner.json';
const RESET_STATE_FILE = '.blueprint-codex-weekly-reset.json';

interface RateLimitWindow {
  usedPercent?: unknown;
  windowDurationMins?: unknown;
  resetsAt?: unknown;
}

interface RateLimitSnapshot {
  limitId?: unknown;
  primary?: RateLimitWindow | null;
  secondary?: RateLimitWindow | null;
}

interface ResetCredit {
  id?: unknown;
  resetType?: unknown;
  status?: unknown;
  expiresAt?: unknown;
}

interface RateLimitsResponse {
  rateLimits?: RateLimitSnapshot | null;
  rateLimitsByLimitId?: Record<string, RateLimitSnapshot> | null;
  rateLimitResetCredits?: {
    availableCount?: unknown;
    credits?: ResetCredit[] | null;
  } | null;
}

export interface CodexWeeklyLimitState {
  exhausted: boolean;
  usedPercent: number | null;
  resetsAt: number | null;
  availableCreditId: string | null;
  availableCreditCount: number;
  creditDetailsAvailable: boolean;
}

export type CodexResetPreflightResult =
  | ({ status: 'not_exhausted' | 'no_credit' | 'reset' | 'nothing_to_reset' } & CodexWeeklyLimitState)
  | { status: 'probe_failed'; reason: string };

const OAUTH_PROBE_ENV_KEYS = [
  'HOME',
  'PATH',
  'LANG',
  'TERM',
  'CODEX_HOME',
  'HTTP_PROXY',
  'HTTPS_PROXY',
  'NO_PROXY',
  'http_proxy',
  'https_proxy',
  'no_proxy',
  'SSL_CERT_FILE',
  'SSL_CERT_DIR',
  'TMPDIR',
  'TMP',
  'TEMP',
  'SystemRoot',
  'SYSTEMROOT',
  'WINDIR',
  'ComSpec',
  'COMSPEC',
  'PATHEXT',
] as const;

/** Minimal OAuth-only environment for the trusted Codex app-server probe. */
export function codexOAuthProbeEnv(source: NodeJS.ProcessEnv): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = { RUST_LOG: 'error' };
  for (const key of OAUTH_PROBE_ENV_KEYS) {
    const value = source[key];
    if (value) env[key] = value;
  }
  // Explicit defense-in-depth: API keys can select a different account than
  // the OAuth CODEX_HOME whose reset credits we intend to inspect.
  delete env.OPENAI_API_KEY;
  delete env.CODEX_API_KEY;
  return env;
}

interface ResetGuard {
  accountIdentity: string;
  statePath: string;
  retainUntilChildExit(childPid?: number): void;
  confirmChildExit(): void;
  release(): void;
}

interface ResetLockOwner {
  ownerToken: string;
  pid: number;
  createdAt: number;
  cleanupUnconfirmed?: boolean;
  quarantineAt?: number;
  childPid?: number;
}

interface AccountContext {
  codexHome: string;
  accountIdentity: string;
}

function resolveAccountContext(env: NodeJS.ProcessEnv): AccountContext | null {
  const configuredHome = env.CODEX_HOME?.trim();
  if (!configuredHome) return null;
  let codexHome: string;
  try {
    codexHome = realpathSync(path.resolve(configuredHome));
    if (!statSync(codexHome).isDirectory()) return null;
  } catch {
    return null;
  }
  let accountId: string;
  try {
    const auth = JSON.parse(readFileSync(path.join(codexHome, 'auth.json'), 'utf8')) as {
      tokens?: { account_id?: unknown };
    };
    const raw = auth.tokens?.account_id;
    if (typeof raw !== 'string' || !raw.trim()) return null;
    accountId = raw.trim();
  } catch {
    return null;
  }
  // Only the one-way digest leaves this function; raw account/auth values are
  // never persisted, sent to app-server, or included in diagnostics.
  const accountIdentity = createHash('sha256').update(accountId).digest('hex');
  return { codexHome, accountIdentity };
}

export function codexOAuthAccountIdentity(env: NodeJS.ProcessEnv): string | null {
  return resolveAccountContext(env)?.accountIdentity ?? null;
}

function readResetLockOwner(lockPath: string): ResetLockOwner | null {
  try {
    const parsed = JSON.parse(
      readFileSync(path.join(lockPath, RESET_LOCK_OWNER_FILE), 'utf8'),
    ) as Partial<ResetLockOwner>;
    if (typeof parsed.ownerToken !== 'string' || !parsed.ownerToken) return null;
    if (typeof parsed.pid !== 'number' || typeof parsed.createdAt !== 'number') return null;
    return parsed as ResetLockOwner;
  } catch {
    return null;
  }
}

function removeResetLockDirectory(
  lockPath: string,
  expectedOwnerToken: string | null,
  allowUnpublished: boolean,
  beforeRmdir?: () => void,
): boolean {
  const owner = readResetLockOwner(lockPath);
  if (expectedOwnerToken !== null) {
    if (!owner || owner.ownerToken !== expectedOwnerToken) return false;
  } else if (!allowUnpublished) {
    return false;
  }
  try {
    for (const entry of readdirSync(lockPath)) {
      if (
        entry === RESET_LOCK_OWNER_FILE
        || (entry.startsWith('.owner.') && entry.endsWith('.tmp'))
      ) {
        rmSync(path.join(lockPath, entry), { force: true });
      }
    }
    beforeRmdir?.();
    rmdirSync(lockPath);
    return true;
  } catch {
    return false;
  }
}

export function removeOwnedResetLockDirectory(
  lockPath: string,
  ownerToken: string,
  beforeRmdir?: () => void,
): boolean {
  return removeResetLockDirectory(lockPath, ownerToken, false, beforeRmdir);
}

function publishResetLockOwner(lockPath: string, owner: ResetLockOwner): void {
  const temp = path.join(lockPath, '.owner.' + randomUUID() + '.tmp');
  try {
    writeFileSync(temp, JSON.stringify(owner), { encoding: 'utf8', mode: 0o600, flag: 'wx' });
    renameSync(temp, path.join(lockPath, RESET_LOCK_OWNER_FILE));
  } finally {
    try { rmSync(temp, { force: true }); } catch { /* already published */ }
  }
}

function processIsAlive(pid: number): boolean {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return (error as NodeJS.ErrnoException).code === 'EPERM';
  }
}

function reclaimAbandonedResetLock(lockPath: string): boolean {
  let ageMs: number;
  try {
    ageMs = Date.now() - statSync(lockPath).mtimeMs;
  } catch {
    return false;
  }
  if (ageMs < RESET_LOCK_PUBLICATION_GRACE_MS) return false;
  const owner = readResetLockOwner(lockPath);
  if (!owner) {
    return removeResetLockDirectory(lockPath, null, true);
  }
  if (owner.cleanupUnconfirmed === true) {
    const quarantineAge = Date.now() - (owner.quarantineAt ?? owner.createdAt);
    if (processIsAlive(owner.pid) && quarantineAge < RESET_QUARANTINE_MAX_MS) {
      return false;
    }
    // A dead owner or bounded quarantine is reclaimable. The account+window
    // deterministic server idempotency key still prevents a second redemption.
    return removeResetLockDirectory(lockPath, owner.ownerToken, false);
  }
  if (processIsAlive(owner.pid)) return false;
  return removeResetLockDirectory(lockPath, owner.ownerToken, false);
}

function tryAcquireResetGuard(context: AccountContext): ResetGuard | null {
  const { codexHome, accountIdentity } = context;
  const lockPath = path.join(codexHome, RESET_LOCK_FILE);
  const statePath = path.join(codexHome, RESET_STATE_FILE);

  const ownerToken = randomUUID();
  const tryCreate = (): boolean => {
    try {
      mkdirSync(lockPath, { mode: 0o700 });
      return true;
    } catch {
      return false;
    }
  };
  let acquired = tryCreate();
  if (!acquired && reclaimAbandonedResetLock(lockPath)) {
    acquired = tryCreate();
  }
  if (!acquired) return null;
  const owner: ResetLockOwner = {
    ownerToken,
    pid: process.pid,
    createdAt: Date.now(),
  };
  try {
    publishResetLockOwner(lockPath, owner);
  } catch {
    removeResetLockDirectory(lockPath, null, true);
    return null;
  }
  let retainedForChild = false;
  return {
    accountIdentity,
    statePath,
    retainUntilChildExit: childPid => {
      retainedForChild = true;
      try {
        publishResetLockOwner(lockPath, {
          ...owner,
          cleanupUnconfirmed: true,
          quarantineAt: Date.now(),
          ...(Number.isInteger(childPid) ? { childPid } : {}),
        });
      } catch {
        // Fail closed in-process. After a crash, dead-owner cleanup reclaims any
        // partial publication after the bounded grace.
      }
    },
    confirmChildExit: () => {
      if (!retainedForChild) return;
      retainedForChild = false;
      removeOwnedResetLockDirectory(lockPath, ownerToken);
    },
    release: () => {
      if (!retainedForChild) {
        removeOwnedResetLockDirectory(lockPath, ownerToken);
      }
    },
  };
}

async function acquireResetGuard(
  env: NodeJS.ProcessEnv,
  waitMs: number,
): Promise<ResetGuard | null> {
  const context = resolveAccountContext(env);
  if (!context) return null;
  const deadline = Date.now() + Math.max(0, waitMs);
  for (;;) {
    const guard = tryAcquireResetGuard(context);
    if (guard) return guard;
    const remaining = deadline - Date.now();
    if (remaining <= 0) return null;
    await new Promise(resolve => setTimeout(resolve, Math.min(100, remaining)));
  }
}

function handledWeeklyWindow(
  statePath: string,
  accountIdentity: string,
): number | null {
  try {
    const parsed = JSON.parse(readFileSync(statePath, 'utf8')) as {
      accountIdentity?: unknown;
      weeklyResetsAt?: unknown;
    };
    if (parsed.accountIdentity !== accountIdentity) return null;
    return finiteNumber(parsed.weeklyResetsAt);
  } catch {
    return null;
  }
}

function persistHandledWeeklyWindow(
  statePath: string,
  accountIdentity: string,
  weeklyResetsAt: number,
): void {
  const dir = path.dirname(statePath);
  mkdirSync(dir, { recursive: true, mode: 0o700 });
  const temp = path.join(dir, `.${path.basename(statePath)}.${process.pid}.${randomUUID()}.tmp`);
  try {
    writeFileSync(
      temp,
      JSON.stringify({
        accountIdentity,
        weeklyResetsAt,
        handledAt: Math.floor(Date.now() / 1000),
      }),
      { encoding: 'utf8', mode: 0o600 },
    );
    renameSync(temp, statePath);
  } finally {
    try { rmSync(temp, { force: true }); } catch { /* already activated */ }
  }
}

export function codexWeeklyResetIdempotencyKey(
  accountIdentity: string,
  weeklyResetsAt: number,
): string {
  const hex = createHash('sha256')
    .update(`blueprint-codex-weekly-reset-v1\0${accountIdentity}\0${weeklyResetsAt}`)
    .digest('hex');
  // UUID-shaped deterministic key; the protocol recommends UUIDs and requires
  // retries of the same logical attempt to reuse one key.
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-5${hex.slice(13, 16)}-a${hex.slice(17, 20)}-${hex.slice(20, 32)}`;
}

function finiteNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function codexSnapshot(response: RateLimitsResponse): RateLimitSnapshot | null {
  const byId = response.rateLimitsByLimitId;
  if (byId && typeof byId === 'object') {
    if (byId.codex && typeof byId.codex === 'object') return byId.codex;
    const exact = Object.values(byId).find(snapshot => snapshot?.limitId === 'codex');
    if (exact) return exact;
  }
  return response.rateLimits && typeof response.rateLimits === 'object'
    ? response.rateLimits
    : null;
}

/**
 * Extract the seven-day Codex window and one exact redeemable reset credit.
 * Exact IDs keep concurrent workers from consuming separate credits for the
 * same exhausted window.
 */
export function inspectCodexWeeklyLimit(
  response: RateLimitsResponse,
  nowSeconds = Math.floor(Date.now() / 1000),
): CodexWeeklyLimitState {
  const snapshot = codexSnapshot(response);
  const windows = [snapshot?.primary, snapshot?.secondary]
    .filter((window): window is RateLimitWindow => Boolean(window && typeof window === 'object'))
    .map(window => ({ window, duration: finiteNumber(window.windowDurationMins) }))
    // 0.144.1 reports the weekly Codex bucket as exactly 10,080 minutes.
    // Longer windows are not weekly and must never authorize credit use.
    .filter(item => item.duration === WEEKLY_WINDOW_MINUTES);
  const weekly = windows[0]?.window ?? null;
  const usedPercent = finiteNumber(weekly?.usedPercent);
  const resetsAt = finiteNumber(weekly?.resetsAt);

  const summary = response.rateLimitResetCredits;
  const availableCreditCount = Math.max(0, Math.trunc(finiteNumber(summary?.availableCount) ?? 0));
  const credits = Array.isArray(summary?.credits) ? summary.credits : null;
  const availableCredit = credits?.find(credit => {
    if (credit?.status !== 'available' || credit?.resetType !== 'codexRateLimits') return false;
    if (typeof credit.id !== 'string' || !credit.id) return false;
    const expiresAt = finiteNumber(credit.expiresAt);
    return expiresAt === null || expiresAt > nowSeconds;
  });

  return {
    exhausted: usedPercent !== null && usedPercent >= 100,
    usedPercent,
    resetsAt,
    availableCreditId: typeof availableCredit?.id === 'string' ? availableCredit.id : null,
    availableCreditCount,
    creditDetailsAvailable: credits !== null,
  };
}

interface AppServerMessage {
  id?: unknown;
  result?: unknown;
  error?: { message?: unknown } | null;
}

function errorMessage(message: AppServerMessage): string | null {
  const value = message.error?.message;
  return typeof value === 'string' && value ? value.slice(0, 240) : null;
}

/**
 * Read real Codex limits and redeem one credit only when the seven-day window
 * is exhausted. Probe failures fail open to normal Codex execution.
 */
export async function maybeConsumeCodexWeeklyReset(options: {
  command: string;
  env: NodeJS.ProcessEnv;
  timeoutMs?: number;
}): Promise<CodexResetPreflightResult> {
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const startedAt = Date.now();
  const guard = await acquireResetGuard(
    options.env,
    Math.min(2_000, Math.max(0, timeoutMs - 1_000)),
  );
  if (!guard) {
    return {
      status: 'probe_failed',
      reason: 'Codex reset preflight lacks a stable writable account identity or another preflight owns it',
    };
  }
  try {
    return await runCodexResetPreflight(
      {
        ...options,
        timeoutMs: Math.max(1_000, timeoutMs - (Date.now() - startedAt)),
      },
      guard,
    );
  } finally {
    guard.release();
  }
}

async function runCodexResetPreflight(
  options: { command: string; env: NodeJS.ProcessEnv; timeoutMs?: number },
  guard: ResetGuard,
): Promise<CodexResetPreflightResult> {
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;

  return await new Promise<CodexResetPreflightResult>(resolve => {
    let resolved = false;
    let pendingResult: CodexResetPreflightResult | null = null;
    let lastState: CodexWeeklyLimitState | null = null;
    let stdoutBytes = 0;
    let processExited = false;
    let operationTimer: NodeJS.Timeout;
    let killTimer: NodeJS.Timeout | null = null;
    let forcedSettleTimer: NodeJS.Timeout | null = null;
    const child = spawn(options.command, ['app-server', '--listen', 'stdio://'], {
      env: codexOAuthProbeEnv(options.env),
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    const lines = createInterface({ input: child.stdout });

    const settle = (): void => {
      if (resolved || !pendingResult) return;
      resolved = true;
      clearTimeout(operationTimer);
      if (killTimer) clearTimeout(killTimer);
      if (forcedSettleTimer) clearTimeout(forcedSettleTimer);
      resolve(pendingResult);
    };
    const finish = (result: CodexResetPreflightResult): void => {
      if (pendingResult) return;
      pendingResult = result;
      clearTimeout(operationTimer);
      lines.close();
      try { child.stdin.end(); } catch { /* already closed */ }
      if (processExited) {
        settle();
        return;
      }
      killTimer = setTimeout(() => {
        if (!processExited) {
          try { child.kill('SIGKILL'); } catch { /* already exited */ }
        }
      }, GRACEFUL_EXIT_MS);
      killTimer.unref();
      forcedSettleTimer = setTimeout(() => {
        if (!processExited) {
          try { child.kill('SIGKILL'); } catch { /* already exited */ }
        }
        if (!processExited) {
          // Return within the hard bound, but quarantine the account lock until
          // the wrapper really closes. A kill-resistant child must never overlap
          // a second credit preflight.
          guard.retainUntilChildExit(child.pid);
        }
        settle();
      }, FORCED_EXIT_SETTLE_MS);
      forcedSettleTimer.unref();
    };
    const send = (message: unknown): void => {
      if (!pendingResult && !child.stdin.destroyed) {
        try {
          child.stdin.write(`${JSON.stringify(message)}\n`);
        } catch {
          finish({ status: 'probe_failed', reason: 'Codex app-server input closed unexpectedly' });
        }
      }
    };
    operationTimer = setTimeout(
      () => finish({ status: 'probe_failed', reason: 'Codex usage probe timed out' }),
      timeoutMs,
    );
    operationTimer.unref();

    child.once('error', error => {
      finish({ status: 'probe_failed', reason: `Codex app-server unavailable: ${error.message.slice(0, 180)}` });
    });
    child.once('close', code => {
      processExited = true;
      guard.confirmChildExit();
      if (!pendingResult) {
        finish({ status: 'probe_failed', reason: `Codex app-server exited before replying (${code ?? 'signal'})` });
      }
      settle();
    });
    child.stdin.on('error', () => {
      if (!pendingResult) {
        finish({ status: 'probe_failed', reason: 'Codex app-server input failed' });
      }
    });
    child.stdout.on('error', () => {
      if (!pendingResult) {
        finish({ status: 'probe_failed', reason: 'Codex app-server output failed' });
      }
    });
    // Drain but never surface stderr: diagnostics may contain local metadata.
    child.stderr.on('data', () => undefined);
    child.stderr.on('error', () => undefined);

    lines.on('line', line => {
      stdoutBytes += Buffer.byteLength(line, 'utf8');
      if (stdoutBytes > 1_000_000) {
        finish({ status: 'probe_failed', reason: 'Codex usage probe exceeded output limit' });
        return;
      }
      let message: AppServerMessage;
      try {
        message = JSON.parse(line) as AppServerMessage;
      } catch {
        return;
      }
      if (message.id === 1 && message.error) {
        finish({ status: 'probe_failed', reason: errorMessage(message) ?? 'Codex app-server initialization failed' });
        return;
      }
      if (message.id === 2) {
        if (message.error) {
          finish({ status: 'probe_failed', reason: errorMessage(message) ?? 'Codex rate-limit read failed' });
          return;
        }
        const response = message.result && typeof message.result === 'object'
          ? message.result as RateLimitsResponse
          : {};
        lastState = inspectCodexWeeklyLimit(response);
        if (!lastState.exhausted) {
          finish({ status: 'not_exhausted', ...lastState });
        } else if (lastState.resetsAt === null || lastState.resetsAt <= 0) {
          finish({ status: 'probe_failed', reason: 'Exhausted weekly window lacks a stable reset identity' });
        } else if (
          handledWeeklyWindow(guard.statePath, guard.accountIdentity) === lastState.resetsAt
        ) {
          finish({ status: 'nothing_to_reset', ...lastState });
        } else if (!lastState.availableCreditId) {
          finish({ status: 'no_credit', ...lastState });
        } else {
          const idempotencyKey = codexWeeklyResetIdempotencyKey(
            guard.accountIdentity,
            lastState.resetsAt,
          );
          send({
            method: 'account/rateLimitResetCredit/consume',
            id: 3,
            params: { creditId: lastState.availableCreditId, idempotencyKey },
          });
        }
        return;
      }
      if (message.id === 3) {
        if (message.error || !lastState) {
          finish({ status: 'probe_failed', reason: errorMessage(message) ?? 'Codex reset-credit redemption failed' });
          return;
        }
        const result = message.result && typeof message.result === 'object'
          ? message.result as { outcome?: unknown }
          : {};
        const outcome = result.outcome;
        if (outcome === 'reset' || outcome === 'alreadyRedeemed') {
          if (lastState.resetsAt !== null) {
            try {
              persistHandledWeeklyWindow(
                guard.statePath,
                guard.accountIdentity,
                lastState.resetsAt,
              );
            }
            catch { /* stable server idempotency still prevents a second reset */ }
          }
          finish({ status: 'reset', ...lastState });
        } else if (outcome === 'nothingToReset') {
          finish({ status: 'nothing_to_reset', ...lastState });
        } else if (outcome === 'noCredit') {
          finish({ status: 'no_credit', ...lastState, availableCreditId: null, availableCreditCount: 0 });
        } else {
          finish({ status: 'probe_failed', reason: 'Codex returned an unknown reset-credit outcome' });
        }
      }
    });

    send({
      method: 'initialize',
      id: 1,
      params: {
        clientInfo: { name: 'blueprint_agent', title: 'Blueprint Agent', version: '2.0.0' },
        capabilities: { experimentalApi: true },
      },
    });
    send({ method: 'initialized', params: {} });
    send({ method: 'account/rateLimits/read', id: 2, params: null });
  });
}
