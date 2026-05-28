import {
  TOKEN_REFRESH_FRACTION, TOKEN_REFRESH_FLOOR_MS, TOKEN_REFRESH_CEILING_MS,
  BOOTSTRAP_TOKEN_THRESHOLD_MS, BOOTSTRAP_CONFIRMATION_DELAY_MS, API_TIMEOUT,
} from '../constants.js';

// Dependencies injected by index.ts at startup to avoid a circular import.
interface TokenModuleConfig {
  getOrchestratorUrl: () => string;
  getHeaders: () => Record<string, string>;
  getToken: () => string;
  setToken: (newToken: string) => void;
  log: (msg: string) => void;
}

let _config: TokenModuleConfig | null = null;

export function initTokenModule(config: TokenModuleConfig): void {
  _config = config;
}

function cfg(): TokenModuleConfig {
  if (!_config) throw new Error('initTokenModule() must be called before using the token module');
  return _config;
}

export let tokenRefreshTimer: ReturnType<typeof setTimeout> | null = null;

function decodeJwtExp(token: string): number | null {
  try {
    const parts = token.split('.');
    if (parts.length < 2) return null;
    const b64 = parts[1].replace(/-/g, '+').replace(/_/g, '/');
    const json = Buffer.from(b64, 'base64').toString('utf8');
    const payload = JSON.parse(json) as { exp?: number };
    return typeof payload.exp === 'number' ? payload.exp : null;
  } catch {
    return null;
  }
}

/** Compute how many ms from `now` to schedule the next proactive refresh.
 *  Uses TOKEN_REFRESH_FRACTION * remaining lifetime, clamped to [FLOOR, CEILING]. */
export function computeRefreshDelayMs(token: string, now = Date.now()): number {
  const exp = decodeJwtExp(token);
  if (exp === null) return TOKEN_REFRESH_FLOOR_MS;
  const remainingMs = exp * 1000 - now;
  const target = remainingMs * TOKEN_REFRESH_FRACTION;
  return Math.max(TOKEN_REFRESH_FLOOR_MS, Math.min(TOKEN_REFRESH_CEILING_MS, target));
}

/** Returns true when `token` has a remaining lifetime that exceeds the normal
 *  short-TTL window, indicating it is a long bootstrap token. */
export function isBootstrapToken(token: string, now = Date.now()): boolean {
  const exp = decodeJwtExp(token);
  if (exp === null) return false;
  return (exp * 1000 - now) > BOOTSTRAP_TOKEN_THRESHOLD_MS;
}

export async function tryRefreshToken(): Promise<boolean> {
  const { getOrchestratorUrl, getHeaders, setToken, log } = cfg();
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => { controller.abort(); }, API_TIMEOUT);
      const res = await fetch(`${getOrchestratorUrl()}/api/agent/refresh-token`, {
        method: 'POST', headers: { ...getHeaders() }, body: JSON.stringify({}), signal: controller.signal,
      });
      clearTimeout(timeout);
      if (res.ok) {
        const data = await res.json() as { token?: string };
        if (data.token) {
          setToken(data.token);
          log('Agent token refreshed successfully');
          return true;
        }
      }
      // Don't retry client errors (4xx) — only transient/network failures
      if (res.status >= 400 && res.status < 500) return false;
    } catch (e) {
      log(`Token refresh attempt ${attempt + 1}/3 failed: ${String(e)}`);
    }
    if (attempt < 2) await new Promise(r => setTimeout(r, 1000 * (attempt + 1))); // 1s, 2s backoff
  }
  return false;
}

export function scheduleProactiveTokenRefresh(token: string): void {
  // Defensively clear any existing timer so callers that re-schedule without
  // an intervening stop (e.g. warm-poll rotation then runRoleOnce) don't orphan
  // a self-re-scheduling setTimeout, which would otherwise accumulate one extra
  // refresh schedule per warm-VM rotation.
  stopProactiveTokenRefresh();

  const { getToken, log } = cfg();

  // Bootstrap detection: if holding a long bootstrap JWT, fire a one-time
  // early confirmation refresh so the agent quickly downgrades to the short
  // TTL rather than waiting for the normal cadence (which would fire near the
  // 55-min ceiling, long after short tasks have finished).
  if (isBootstrapToken(token)) {
    log(`Proactive token refresh: bootstrap token detected — early confirmation refresh in ${BOOTSTRAP_CONFIRMATION_DELAY_MS / 1000}s`);
    tokenRefreshTimer = setTimeout(() => {
      void (async () => {
        log('Proactive token refresh: bootstrap confirmation refresh firing');
        const ok = await tryRefreshToken();
        if (ok) {
          log('Proactive token refresh: bootstrap confirmation succeeded — now on short TTL');
        } else {
          log('Proactive token refresh: bootstrap confirmation failed — continuing on existing token');
        }
        // Regardless of success/failure, reschedule from the current token's
        // real exp. On success AGENT_TOKEN is now the short-TTL token; on
        // failure it stays the bootstrap token and the normal cadence retries.
        scheduleProactiveTokenRefresh(getToken());
      })();
    }, BOOTSTRAP_CONFIRMATION_DELAY_MS);
    return;
  }

  const delay = computeRefreshDelayMs(token);
  log(`Proactive token refresh: next refresh in ${Math.round(delay / 1000)}s`);
  tokenRefreshTimer = setTimeout(() => {
    void (async () => {
      log('Proactive token refresh: refreshing agent JWT before expiry');
      const ok = await tryRefreshToken();
      if (ok) {
        log('Proactive token refresh: succeeded');
      } else {
        log('Proactive token refresh: failed — reactive 401 path will retry');
      }
      scheduleProactiveTokenRefresh(getToken());
    })();
  }, delay);
}

export function stopProactiveTokenRefresh(): void {
  if (tokenRefreshTimer) { clearTimeout(tokenRefreshTimer); tokenRefreshTimer = null; }
}
