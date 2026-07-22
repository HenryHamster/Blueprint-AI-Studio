/**
 * Live agent-token FILE — the fix for the long-lived-session "write goes 401 after
 * ~1h" bug.
 *
 * Background: agent JWTs carry a 1h TTL (backend AGENT_TOKEN_TTL_HOURS). The
 * Node agent proactively refreshes its IN-MEMORY token at ~45min
 * (utils/agent-token.ts), so the main loop's own backend calls (heartbeat,
 * message poll, session heartbeat) — which read the refreshed apiHeaders — keep
 * working, and the LLM stream (authenticated to Anthropic, not Blueprint) is
 * unaffected. BUT the provisioned bash helpers (./strategic, ./messages,
 * ./fetch, ./design-doc, ./playbook) authenticate with the AGENT_API_TOKEN env
 * var, which is a SNAPSHOT captured when the long-lived Claude
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
 * Env var carrying the ABSOLUTE path of the live GitHub token file.
 */
export const GITHUB_TOKEN_FILE_ENV = 'GITHUB_TOKEN_FILE';

/**
 * Resolve the intended absolute path of the live GitHub token file. Honors an
 * explicit `GITHUB_TOKEN_FILE` override; otherwise a private per-agent path under
 * the agent user's home, deliberately NOT under the cloned workspace.
 */
export function githubTokenFilePath(): string {
  const override = process.env[GITHUB_TOKEN_FILE_ENV];
  if (override && override.trim()) return override.trim();
  const base = os.homedir() || os.tmpdir();
  return path.join(base, '.blueprint', 'github-token.txt');
}

/**
 * Publish the GitHub token-file location into `process.env[GITHUB_TOKEN_FILE_ENV]`
 * (once) so it is propagated to child helper scripts. Idempotent. Returns the
 * resolved path. Called once at role start — NOT at module load.
 */
export function ensureGithubTokenFileEnv(): string {
  const existing = process.env[GITHUB_TOKEN_FILE_ENV];
  if (existing && existing.trim()) return existing.trim();
  const resolved = githubTokenFilePath();
  process.env[GITHUB_TOKEN_FILE_ENV] = resolved;
  return resolved;
}

/**
 * Mirror the current GitHub installation token to the configured token file
 * (dir 0700, file 0600, written atomically via temp-file + rename).
 *
 * No-op unless `GITHUB_TOKEN_FILE` is configured and the token is non-empty.
 * Best-effort: never throws, never logs the token value. Returns true only when
 * the file was actually written.
 */
export function writeGithubTokenFile(token: string): boolean {
  const configured = process.env[GITHUB_TOKEN_FILE_ENV];
  if (!configured || !configured.trim() || !token) return false;
  const target = configured.trim();
  try {
    fs.mkdirSync(path.dirname(target), { recursive: true, mode: 0o700 });
    const tmp = `${target}.tmp`;
    fs.writeFileSync(tmp, token, { encoding: 'utf-8', mode: 0o600 });
    fs.renameSync(tmp, target);
    try { fs.chmodSync(target, 0o600); } catch { /* best effort */ }
    return true;
  } catch {
    return false;
  }
}

/**
 * Bash function for generated helpers that need to build JSON request bodies
 * without assuming `jq` exists on the VM.
 *
 * The exact encoder uses python3 when present, then node. If neither is
 * available, fail loudly rather than silently emitting invalid JSON.
 */
export function jsonStrShellFn(): string {
  return [
    '# Encode one shell value as a JSON string literal without requiring jq.',
    '_json_str() {',
    '  if command -v python3 >/dev/null 2>&1; then',
    "    printf '%s' \"${1-}\" | python3 -c 'import json,sys; sys.stdout.write(json.dumps(sys.stdin.read()))'",
    '    return $?',
    '  fi',
    '  if command -v node >/dev/null 2>&1; then',
    "    printf '%s' \"${1-}\" | node -e 'const fs=require(\"fs\"); process.stdout.write(JSON.stringify(fs.readFileSync(0,\"utf8\")));'",
    '    return $?',
    '  fi',
    '  echo "[ERROR] no JSON string encoder available (python3/node missing)" >&2',
    '  return 1',
    '}',
    '_json_str "" >/dev/null || exit 1',
  ].join('\n');
}

/**
 * Bash functions, emitted into helper scripts that need a fresh GitHub
 * installation token before a git push or GitHub API call.
 *
 * `_load_github_token()` resolves the token into `$GH_TOKEN`: prefers the live
 * token file (`$GITHUB_TOKEN_FILE`, kept fresh by the Node agent at spawn and on
 * every ~45min refresh), falls back to the backend API endpoint, and returns
 * non-zero with a clear error when neither source yields a token.
 *
 * `_set_github_remote()` rewrites the git origin URL with the fresh token.
 * Usage: _set_github_remote <owner> <repo>  (call after _load_github_token)
 *
 * Returned as an array of single-quoted lines joined with newlines so no bash
 * `$`/`${...}` needs escaping through a TypeScript template literal.
 */
export function githubTokenShellFn(): string {
  return [
    '# Resolve a fresh GitHub installation token into $GH_TOKEN, preferring the',
    '# live token file (kept fresh by the Node agent at spawn + ~45min refresh) and',
    '# falling back to the backend API. Re-callable to re-read a rotated token.',
    '_load_github_token() {',
    '  GH_TOKEN=""',
    '  if [ -n "${GITHUB_TOKEN_FILE:-}" ] && [ -s "${GITHUB_TOKEN_FILE}" ]; then',
    '    GH_TOKEN=$(cat "${GITHUB_TOKEN_FILE}" 2>/dev/null)',
    '  fi',
    '  if [ -z "$GH_TOKEN" ] && [ -n "${ORCHESTRATOR_URL:-}" ] && [ -n "${TASK_ID:-}" ] && [ -n "${TOKEN:-}" ]; then',
    '    _GT_RESP=$(curl -f -sS --max-time 30 -H "X-Agent-Token: $TOKEN" "${ORCHESTRATOR_URL}/api/agent/github/token?task_id=${TASK_ID}" 2>/dev/null) || _GT_RESP=""',
    '    GH_TOKEN=$(printf "%s" "$_GT_RESP" | _json_field data.token 2>/dev/null || true)',
    '  fi',
    '  if [ -z "$GH_TOKEN" ]; then',
    '    echo "[ERROR] no GitHub token available (GITHUB_TOKEN_FILE empty and backend API fallback failed)" >&2',
    '    return 1',
    '  fi',
    '  return 0',
    '}',
    '# Rewrite the git origin remote URL with the fresh GitHub token.',
    '# Must call _load_github_token first. Usage: _set_github_remote <owner> <repo>',
    '_set_github_remote() {',
    '  git remote set-url origin "https://x-access-token:${GH_TOKEN}@github.com/$1/$2.git"',
    '}',
  ].join('\n');
}

/**
 * Bash function, emitted into helpers that PARSE a JSON response without `jq`.
 * On the strategist VM class `jq` is not reliably present, and the helpers used
 * `jq -r '.field.sub // empty'` to extract fields from orchestrator/GitHub
 * responses. `_json_field` replaces that: it reads JSON from stdin and prints
 * the value at a dot-separated path via `python3 -c` (standard on every agent
 * VM). An optional second argument sets the default printed when the path is
 * absent or null (defaults to empty, matching `jq -r '… // empty'`).
 *
 * Usage: printf '%s' "$JSON" | _json_field data.token
 *        printf '%s' "$JSON" | _json_field mergeable null
 *
 * Returned as an array of single-quoted lines joined with newlines so no bash
 * `$`/`${...}` needs escaping through a TypeScript template literal. The python3
 * inline code is inside a single-quoted argument so bash expansion is off;
 * Python string literals use double quotes to avoid conflicting with the
 * enclosing bash single quotes, and TS emits them via single-quoted strings.
 */
export function jsonFieldShellFn(): string {
  return [
    '# Extract a dot-path field from stdin JSON via python3 (no jq dependency).',
    '# Usage: printf "%s" "$JSON" | _json_field <dot.path> [default]',
    '# Reads stdin, walks the dot-separated path through nested objects, and',
    '# prints the field value if present and non-null; otherwise prints the',
    '# default (empty when omitted — matches jq -r \'… // empty\').',
    '_json_field() {',
    "  python3 -c '",
    'import json, sys',
    'try:',
    '    data = json.loads(sys.stdin.read())',
    '    path = sys.argv[1]',
    '    default = sys.argv[2] if len(sys.argv) > 2 else ""',
    '    parts = path.split(".")',
    '    for p in parts:',
    '        if isinstance(data, dict):',
    '            data = data.get(p)',
    '        else:',
    '            data = None',
    '            break',
    '    if data is None:',
    '        sys.stdout.write(default)',
    '    elif isinstance(data, str):',
    '        sys.stdout.write(data)',
    '    else:',
    '        sys.stdout.write(json.dumps(data))',
    'except Exception:',
    '    sys.stdout.write(sys.argv[2] if len(sys.argv) > 2 else "")',
    "' \"$1\" \"${2:-}\"",
    '}',
  ].join('\n');
}

/**
 * Bash function, emitted verbatim into every provisioned helper that
 * authenticates to the orchestrator, that resolves the agent token into `$TOKEN`.
 *
 * It prefers the live token FILE (`$AGENT_TOKEN_FILE`, kept fresh by the agent
 * process at spawn + on every ~45min refresh) so a long-lived session
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
    '# long-lived session authenticates with the ROTATED token, not the',
    '# stale spawn-time env snapshot. Falls back to AGENT_API_TOKEN when the file',
    '# is unset/empty. Re-callable after a 401 to re-read a rotated token.',
    '_agent_token_looks_like_jwt() {',
    '  case "${1:-}" in',
    '    *.*.*) return 0 ;;',
    '    *) return 1 ;;',
    '  esac',
    '}',
    '_load_agent_token() {',
    '  if [ -n "${AGENT_TOKEN_FILE:-}" ] && [ -s "${AGENT_TOKEN_FILE}" ]; then',
    '    TOKEN=$(cat "${AGENT_TOKEN_FILE}" 2>/dev/null)',
    '  else',
    '    TOKEN=""',
    '  fi',
    '  if [ -n "$TOKEN" ] && ! _agent_token_looks_like_jwt "$TOKEN"; then',
    '    echo "[WARN] ignoring invalid AGENT_TOKEN_FILE contents; falling back to AGENT_API_TOKEN" >&2',
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
