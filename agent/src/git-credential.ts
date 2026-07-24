/**
 * Git read-token file for a long-lived agent VM.
 *
 * A provisioned workspace clones the repo with a GitHub App
 * installation token embedded in the remote URL. That token expires after
 * 60 minutes and is never refreshed, so `git fetch origin` fails with an
 * invalid-token auth error after ~1 hour.
 *
 * Fix: mirror the current read token to a private on-disk file (same .blueprint/
 * directory as agent-token.jwt), configure git to use a credential helper that
 * reads that file on every git operation, and update the remote URL to remove
 * the embedded token so git always asks the helper. A long-lived session's
 * main loop calls /api/agent/git-read-token on a ~8-minute interval to keep the
 * file current before the 60-minute GitHub token TTL.
 *
 * Security: the file lives outside any cloned/committed workspace, in a directory
 * the agent user owns (dir 0700, file 0600), written atomically (temp + rename)
 * so a concurrent git operation never sees a half-written token.
 */

import fs from 'fs';
import os from 'os';
import path from 'path';
import { execFileSync } from 'child_process';

/** Refresh interval: 8 min. The backend caches installation tokens for up to
 *  50 min (against a 60 min GitHub TTL), so a served token can have as little
 *  as 10 min of remaining life. Refreshing every 8 min ensures the client
 *  always fetches a fresh token before the previous one expires. */
export const GIT_TOKEN_REFRESH_MS = 8 * 60 * 1000;

/** Env var propagated to git subprocesses so the credential helper can locate
 *  the token file without hardcoding the path. */
export const GH_READ_TOKEN_FILE_ENV = 'GH_READ_TOKEN_FILE';

/** Env var that overrides the credential helper script path (used in tests). */
export const GH_CREDENTIAL_HELPER_PATH_ENV = 'GH_CREDENTIAL_HELPER_PATH';

/** Absolute path of the git read-token file.
 *  Honors GH_READ_TOKEN_FILE env var when set (matching the helper script). */
export function gitTokenFilePath(): string {
  const fromEnv = process.env[GH_READ_TOKEN_FILE_ENV];
  if (fromEnv) return fromEnv;
  const base = os.homedir() || os.tmpdir();
  return path.join(base, '.blueprint', 'gh-read-token');
}

/** Absolute path of the git credential helper script.
 *  Honors GH_CREDENTIAL_HELPER_PATH env var when set (used in tests). */
export function gitCredentialHelperPath(): string {
  const fromEnv = process.env[GH_CREDENTIAL_HELPER_PATH_ENV];
  if (fromEnv) return fromEnv;
  const base = os.homedir() || os.tmpdir();
  return path.join(base, '.blueprint', 'git-credential-helper');
}

/**
 * Mirror the current GitHub read token to the token file.
 * Written atomically (temp + rename) so a concurrent git credential-helper
 * invocation never sees a half-written token. Returns true when the file was
 * actually written.
 */
export function writeGitTokenFile(token: string): boolean {
  if (!token) return false;
  const target = gitTokenFilePath();
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
 * Write the git credential helper script to disk and publish the token-file
 * path into `process.env`. Safe to call before a workspace exists (no git
 * commands are run). Idempotent — subsequent calls overwrite the same file.
 *
 * Call this before any git clone that must authenticate without an embedded
 * token. Pass the path returned by `gitCredentialHelperPath()` as the `-c
 * credential.https://github.com.helper=<path>` option on the git clone
 * command line so git resolves credentials via the helper during the clone.
 */
export function ensureGitCredentialHelperScript(): string {
  const helperPath = gitCredentialHelperPath();
  const tokenFile = gitTokenFilePath();

  const helperLines = [
    '#!/bin/bash',
    '# Git credential helper for a long-lived agent VM (read-only GitHub access).',
    '# Called by git when credentials are needed for github.com HTTPS operations.',
    '# Reads the GitHub App installation token from GH_READ_TOKEN_FILE, kept fresh',
    '# by the session main loop (~8 min interval; tokens expire after 60 min).',
    '# Falls back to the canonical path when the env var is unset.',
    'if [ "$1" = "get" ]; then',
    `  token_file="\${GH_READ_TOKEN_FILE:-${tokenFile}}"`,
    '  token=""',
    '  if [ -n "$token_file" ] && [ -s "$token_file" ]; then',
    '    token=$(cat "$token_file" 2>/dev/null || true)',
    '  fi',
    '  if [ -n "$token" ]; then',
    '    printf "protocol=https\\n"',
    '    printf "host=github.com\\n"',
    '    printf "username=x-access-token\\n"',
    '    printf "password=%s\\n" "$token"',
    '  fi',
    'fi',
  ];

  fs.mkdirSync(path.dirname(helperPath), { recursive: true, mode: 0o700 });
  fs.writeFileSync(helperPath, helperLines.join('\n') + '\n', { mode: 0o755 });

  if (!process.env[GH_READ_TOKEN_FILE_ENV]) {
    process.env[GH_READ_TOKEN_FILE_ENV] = tokenFile;
  }

  return helperPath;
}

/**
 * Configure git in `workspace` to use a credential helper that reads the
 * token file, then rewrite the remote URL to remove any embedded token so
 * git delegates credential lookup to the helper on every fetch/pull/ls-remote.
 *
 * Also publishes `GH_READ_TOKEN_FILE` into `process.env` so child processes
 * (the Claude session and git subprocesses it spawns) inherit the
 * token file path.
 *
 * Call this after the workspace is cloned and the initial token is written via
 * writeGitTokenFile(), so the helper immediately finds a valid token.
 */
export function setupGitCredentialHelper(
  owner: string,
  repo: string,
  workspace: string,
): void {
  const helperPath = ensureGitCredentialHelperScript();

  // Configure git to use the helper for github.com HTTPS credentials.
  // Use the url-scoped form so other hosts are unaffected.
  execFileSync('git', [
    '-C', workspace,
    'config', 'credential.https://github.com.helper', helperPath,
  ]);

  // Replace any token-embedded remote URL with a plain HTTPS URL so git
  // always invokes the credential helper instead of using the stale baked-in
  // token. The helper provides fresh credentials on every git network call.
  execFileSync('git', [
    '-C', workspace,
    'remote', 'set-url', 'origin',
    `https://github.com/${owner}/${repo}.git`,
  ]);
}
