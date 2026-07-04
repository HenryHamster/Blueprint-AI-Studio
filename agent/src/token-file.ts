/**
 * Live agent-token FILE — the fix for the resident-session "write goes 401 after
 * ~1h" bug.
 *
 * Background: agent JWTs carry a 1h TTL (backend AGENT_TOKEN_TTL_HOURS). The
 * Node agent proactively refreshes its IN-MEMORY token at ~45min
 * (utils/agent-token.ts), so the main loop's own backend calls (heartbeat,
 * message poll, session heartbeat) — which read the refreshed apiHeaders — keep
 * working, and the LLM stream (authenticated to Anthropic, not Blueprint) is
 * unaffected. BUT the provisioned bash helpers (./strategic, ./messages,
 * ./fetch, ./design-doc, ./playbook) authenticate with the AGENT_API_TOKEN env
 * var, which is a SNAPSHOT captured when the long-lived resident Claude
 * subprocess was spawned. A running process's env cannot be mutated by the
 * parent, so the helpers keep using the spawn-time token; once it hits the 1h
 * TTL every helper write 401s ("Agent token expired") while streaming survives.
 *
 * Fix: the Node process mirrors its CURRENT token to a private on-disk file at
 * spawn and on every refresh; the helper scripts read that file FRESH on each
 * invocation (falling back to the env for one-shot agents). Because the file is
 * kept within the TTL by the existing 45-min proactive refresh, helpers always
 * authenticate with a live token. The 1h security TTL is unchanged — this only
 * makes the helpers see the rotated token instead of the stale snapshot.
 *
 * Security: the file lives OUTSIDE any cloned/committed workspace (so it can
 * never land in a PR), in a directory the agent user owns (dir 0700, file
 * 0600), written atomically (temp + rename) so a concurrent helper read never
 * sees a half-written token. The token value is never logged.
 */

import fs from 'fs';
import os from 'os';
import path from 'path';

/**
 * Env var carrying the ABSOLUTE path of the live agent-token file to the
 * provisioned bash helpers (and shared between the Node writer and the helper
 * readers so they always agree on one location).
 */
export const AGENT_TOKEN_FILE_ENV = 'AGENT_TOKEN_FILE';

/**
 * Resolve the intended absolute path of the live agent-token file. Honors an
 * explicit `AGENT_TOKEN_FILE` override (so tests and operators can redirect it);
 * otherwise a private per-agent path under the agent user's home (falling back
 * to the OS temp dir), deliberately NOT under the cloned workspace so it can
 * never be committed.
 */
export function agentTokenFilePath(): string {
  const override = process.env[AGENT_TOKEN_FILE_ENV];
  if (override && override.trim()) return override.trim();
  const base = os.homedir() || os.tmpdir();
  return path.join(base, '.blueprint', 'agent-token.jwt');
}

/**
 * Publish the token-file location into `process.env[AGENT_TOKEN_FILE_ENV]` (once)
 * so it is (a) propagated verbatim to child helper scripts by buildBaseCliEnv and
 * (b) the single path `writeAgentTokenFile` writes to. Idempotent: an existing
 * value is kept. Returns the resolved path. Called once at agent startup — NOT at
 * module load — so unit tests that merely import this module never start writing
 * a token file.
 */
export function ensureAgentTokenFileEnv(): string {
  const existing = process.env[AGENT_TOKEN_FILE_ENV];
  if (existing && existing.trim()) return existing.trim();
  const resolved = agentTokenFilePath();
  process.env[AGENT_TOKEN_FILE_ENV] = resolved;
  return resolved;
}

/**
 * Mirror the current agent token to the configured token file (dir 0700, file
 * 0600, written atomically via temp-file + rename).
 *
 * No-op unless `AGENT_TOKEN_FILE` is configured (so unit tests that do not opt in
 * by setting it never write anywhere) and the token is non-empty. Best-effort:
 * never throws — a write failure simply leaves the helpers on their
 * `AGENT_API_TOKEN` env fallback — and never logs the token value. Returns true
 * only when the file was actually written.
 */
export function writeAgentTokenFile(token: string): boolean {
  const configured = process.env[AGENT_TOKEN_FILE_ENV];
  if (!configured || !configured.trim() || !token) return false;
  const target = configured.trim();
  try {
    fs.mkdirSync(path.dirname(target), { recursive: true, mode: 0o700 });
    // temp + atomic rename so a helper reading concurrently never observes a
    // truncated/half-written token.
    const tmp = `${target}.tmp`;
    fs.writeFileSync(tmp, token, { encoding: 'utf-8', mode: 0o600 });
    fs.renameSync(tmp, target);
    // Re-assert perms in case a pre-existing file had looser bits (no-op on
    // platforms that don't model POSIX modes, e.g. Windows dev).
    try { fs.chmodSync(target, 0o600); } catch { /* best effort */ }
    return true;
  } catch {
    return false;
  }
}

/**
 * Bash function, emitted verbatim into every provisioned helper that
 * authenticates to the orchestrator, that resolves the agent token into `$TOKEN`.
 *
 * It prefers the live token FILE (`$AGENT_TOKEN_FILE`, kept fresh by the agent
 * process at spawn + on every ~45min refresh) so a long-lived resident session
 * authenticates with the ROTATED token instead of the stale spawn-time env
 * snapshot, and falls back to `$AGENT_API_TOKEN` for one-shot agents or when the
 * file is unset/empty. Re-callable after a 401 to re-read a token that rotated
 * mid-session. It never echoes the token value. Returns non-zero (and prints a
 * clear error) only when NEITHER source yields a token, so a genuinely
 * unauthenticated helper still fails loudly.
 *
 * Returned as an array of single-quoted lines joined with newlines so no bash
 * `$`/`${...}` needs escaping through a TypeScript template literal (per the
 * project's generated-code escaping guidance).
 */
export function agentTokenShellFn(): string {
  return [
    '# Resolve the agent token into $TOKEN, preferring the live token file the',
    '# agent process keeps fresh (written at spawn + on every ~45min refresh) so a',
    '# long-lived resident session authenticates with the ROTATED token, not the',
    '# stale spawn-time env snapshot. Falls back to AGENT_API_TOKEN when the file',
    '# is unset/empty. Re-callable after a 401 to re-read a rotated token.',
    '_load_agent_token() {',
    '  if [ -n "${AGENT_TOKEN_FILE:-}" ] && [ -s "${AGENT_TOKEN_FILE}" ]; then',
    '    TOKEN=$(cat "${AGENT_TOKEN_FILE}" 2>/dev/null)',
    '  else',
    '    TOKEN=""',
    '  fi',
    '  [ -n "$TOKEN" ] || TOKEN="${AGENT_API_TOKEN:-}"',
    '  if [ -z "$TOKEN" ]; then',
    '    echo "[ERROR] no agent token available (AGENT_TOKEN_FILE and AGENT_API_TOKEN both empty)" >&2',
    '    return 1',
    '  fi',
    '  return 0',
    '}',
  ].join('\n');
}
