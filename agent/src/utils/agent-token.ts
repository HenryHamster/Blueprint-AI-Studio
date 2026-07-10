import {
  TOKEN_REFRESH_FRACTION, TOKEN_REFRESH_FLOOR_MS, TOKEN_REFRESH_CEILING_MS,
  BOOTSTRAP_TOKEN_THRESHOLD_MS, BOOTSTRAP_CONFIRMATION_DELAY_MS, API_TIMEOUT,
} from '../constants.js';
import { writeGithubTokenFile } from '../token-file.js';
import { writeGitTokenFile, setupGitCredentialHelper } from '../git-credential.js';

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
// Generation number to detect stale reschedules during warm-VM task rotation.
let _proactiveTokenRefreshGen = 0;

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

  // Increment generation so any in-flight refresh from a prior task
  // won't reschedule itself and overwrite this task's timer during rotation.
  _proactiveTokenRefreshGen++;
  const currentGen = _proactiveTokenRefreshGen;

  const { getToken, log } = cfg();

  // Bootstrap detection: if holding a long bootstrap JWT, fire a one-time
  // early confirmation refresh so the agent quickly downgrades to the short
  // TTL rather than waiting for the normal cadence (which would fire near the
  // 55-min ceiling, long after short tasks have finished).
  if (isBootstrapToken(token)) {
    log(`Proactive token refresh: bootstrap token detected — early confirmation refresh in ${BOOTSTRAP_CONFIRMATION_DELAY_MS / 1000}s`);
    tokenRefreshTimer = setTimeout(() => {
      void (async () => {
        // Skip if a new schedule has taken over during task rotation.
        if (_proactiveTokenRefreshGen !== currentGen) {
          log('Proactive token refresh: generation mismatch (task rotated), skipping reschedule');
          return;
        }
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
        // Generation check protects against reschedule during rotation.
        scheduleProactiveTokenRefresh(getToken());
      })();
    }, BOOTSTRAP_CONFIRMATION_DELAY_MS);
    return;
  }

  const delay = computeRefreshDelayMs(token);
  log(`Proactive token refresh: next refresh in ${Math.round(delay / 1000)}s`);
  tokenRefreshTimer = setTimeout(() => {
    void (async () => {
      // Skip if a new schedule has taken over during task rotation.
      if (_proactiveTokenRefreshGen !== currentGen) {
        log('Proactive token refresh: generation mismatch (task rotated), skipping reschedule');
        return;
      }
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

/** How often to proactively refresh the GitHub installation token.
 *
 * The backend caches installation tokens for up to ~50 minutes while GitHub's
 * token TTL is 60 minutes. A task can therefore start with a token that has
 * only ~10 minutes of useful life left. Refresh every 8 minutes so git pushes
 * do not inherit an almost-expired cached token before the first refresh fires.
 */
const GITHUB_TOKEN_REFRESH_MS = 8 * 60 * 1000;

export let githubTokenRefreshTimer: ReturnType<typeof setTimeout> | null = null;

// Monotonic generation stamp for the GitHub-token refresh schedule. Every
// scheduleGithubTokenRefresh (via its leading stopGithubTokenRefresh) and every
// stopGithubTokenRefresh bumps it. A refresh callback captures the generation it
// was armed under and only reschedules itself if that generation is still
// current — so a 45-min refresh caught in flight at task teardown or a warm-VM
// task rotation cannot clear the NEXT task's freshly-armed timer and re-arm with
// the finished task's captured params (which would silently disable proactive
// GitHub token refresh for the new task).
let githubRefreshGeneration = 0;

/**
 * Schedule a proactive GitHub installation token refresh every 8 min, mirroring
 * scheduleProactiveTokenRefresh for the agent JWT. On each fire:
 *   1. Calls GET /api/agent/github/token?task_id=<taskId> (hits backend cache —
 *      zero GitHub API calls when the 50-min backend cache is warm).
 *   2. Writes the fresh token to GITHUB_TOKEN_FILE via writeGithubTokenFile.
 *   3. If NOT read-only: refreshes the on-disk git token file and (re)configures
 *      the credential helper that serves it, keeping the persisted origin URL
 *      clean (https://github.com/owner/repo.git) so the installation token is
 *      never written into .git/config nor exposed in the git process argv.
 *      Reuses git-credential.ts — the same mechanism the strategic resident
 *      installs at role start — rather than embedding the token in the URL.
 *   4. Reschedules itself.
 */
export function scheduleGithubTokenRefresh(
  taskId: string,
  workspace: string,
  owner: string,
  repo: string,
  isReadOnly: boolean,
): void {
  stopGithubTokenRefresh();

  const { getOrchestratorUrl, getHeaders, log } = cfg();

  // Capture the generation this schedule belongs to. stopGithubTokenRefresh()
  // (called just above, and on every teardown/re-schedule) bumps the counter, so
  // the trailing self-reschedule below can tell whether a newer task has since
  // taken over the single module-level timer.
  const myGeneration = githubRefreshGeneration;

  githubTokenRefreshTimer = setTimeout(() => {
    void (async () => {
      log('GitHub token refresh: refreshing installation token before expiry');
      try {
        const controller = new AbortController();
        const timeout = setTimeout(() => { controller.abort(); }, API_TIMEOUT);
        const res = await fetch(
          `${getOrchestratorUrl()}/api/agent/github/token?task_id=${encodeURIComponent(taskId)}`,
          { headers: { ...getHeaders() }, signal: controller.signal },
        );
        clearTimeout(timeout);
        if (res.ok) {
          const data = await res.json() as { data?: { token?: string } };
          const newToken = (data.data)?.token;
          if (newToken) {
            writeGithubTokenFile(newToken);
            if (!isReadOnly) {
              // Keep the installation token OUT of the persisted remote URL /
              // .git/config and out of the git process argv. Rather than
              // `git remote set-url origin https://x-access-token:<token>@…`
              // (which lands the secret in .git/config for the task lifetime and
              // briefly in the git argv), refresh the on-disk token file and
              // (re)configure the credential helper that reads it, leaving the
              // origin URL clean. Git then fetches fresh credentials from the
              // helper on every network call. Reuses the same mechanism the
              // strategic resident installs (git-credential.ts) instead of
              // inventing a divergent one. Best-effort: a failure here just
              // leaves the previously-configured auth in place.
              try {
                writeGitTokenFile(newToken);
                setupGitCredentialHelper(owner, repo, workspace);
              } catch { /* best effort */ }
            }
            log('GitHub token refresh: succeeded');
          } else {
            log('GitHub token refresh: backend returned no token — continuing with current token');
          }
        } else {
          log(`GitHub token refresh: backend returned ${res.status} — continuing with current token`);
        }
      } catch (e) {
        log(`GitHub token refresh failed: ${String(e)}`);
      }
      // Only reschedule if this refresh still owns the active schedule. A newer
      // scheduleGithubTokenRefresh (warm-VM task rotation) or a
      // stopGithubTokenRefresh (task teardown) bumped the generation while this
      // async body was in flight; re-arming here with this task's captured
      // taskId/workspace/owner/repo would clobber the newer task's timer and
      // silently disable its proactive refresh.
      if (githubRefreshGeneration === myGeneration) {
        scheduleGithubTokenRefresh(taskId, workspace, owner, repo, isReadOnly);
      } else {
        log('GitHub token refresh: generation superseded during refresh — not re-arming');
      }
    })();
  }, GITHUB_TOKEN_REFRESH_MS);
}

export function stopGithubTokenRefresh(): void {
  // Bump the generation so any in-flight refresh callback armed under the prior
  // generation skips its trailing self-reschedule instead of re-arming a timer
  // after teardown / for a superseded task.
  githubRefreshGeneration++;
  if (githubTokenRefreshTimer) { clearTimeout(githubTokenRefreshTimer); githubTokenRefreshTimer = null; }
}
